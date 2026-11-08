from datetime import datetime, time, timedelta

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import IntegrityError
from django.db.models import Q
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.contrib.auth import get_user_model
from django.shortcuts import get_object_or_404, redirect
from django.shortcuts import render as django_render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .forms import DT_FMT, BollardFormSet, QuaySettingsForm, UserForm, VesselCallForm
from .models import (
    FREE_COLOR, LINE_COLORS, WEEKDAYS, PriorityDay, Role, ShippingLine, VesselCall,
    berth_span, get_day_owners, get_quay, is_tcn, line_days, user_line, user_role,
)
from .scheduler import (
    get_working_call, recompute_plan, window_owner, working_interval,
)

S = VesselCall.Status
DAY_NAMES = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
DAY_SHORT = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]


def render(request, template, context=None):
    """render() + rôle de l'utilisateur, disponible dans tous les templates."""
    context = dict(context or {})
    role = user_role(request.user)
    context.setdefault("is_tcn", role == Role.TCN)
    context.setdefault("user_role", role)
    context.setdefault("user_role_label", Role(role).label if role else "")
    context.setdefault("role_color", LINE_COLORS.get(role, "#f0b429"))
    return django_render(request, template, context)


def _tcn_required(request):
    """Renvoie une redirection si l'utilisateur n'est pas agent TCN, sinon None."""
    if not is_tcn(request.user):
        messages.error(request, "Cette page est réservée aux agents TCN.")
        return redirect("home")
    return None


def _parse_dt(value):
    """Lit une date/heure saisie dans un champ datetime-local (heure locale du terminal)."""
    try:
        return timezone.make_aware(datetime.strptime(value, DT_FMT))
    except (TypeError, ValueError):
        return None


def _fmt(dt):
    return timezone.localtime(dt).strftime("%d/%m à %H:%M") if dt else "—"


def _iso(dt):
    return timezone.localtime(dt).isoformat() if dt else None


def _lines_payload():
    days = line_days()
    return {
        code: {
            "code": code,
            "name": label,
            "color": LINE_COLORS[code],
            "days": days[code],
            "day_names": [DAY_NAMES[d] for d in days[code]] or ["aucun jour"],
        }
        for code, label in ShippingLine.choices
    }


def _quay_payload(quay):
    return {
        "name": quay.name,
        "length": float(quay.length_m),
        "start_m": float(quay.start_m),
        "max_draft": float(quay.max_draft_m),
        "bollards": [{"name": b.name, "pos": float(b.position_m)} for b in quay.bollards.all()],
    }


def _week_strip(days=7):
    today = timezone.localdate()
    owners = get_day_owners()
    out = []
    for i in range(days):
        d = today + timedelta(days=i)
        owner = owners.get(d.weekday())
        out.append({
            "date": d.isoformat(), "short": DAY_SHORT[d.weekday()], "num": d.strftime("%d/%m"),
            "owner": owner or "Libre", "color": LINE_COLORS.get(owner, FREE_COLOR),
        })
    return out


def _back(request, fallback="home"):
    """Retour à la page d'origine (plan ou liste des escales)."""
    ref = request.POST.get("next") or request.META.get("HTTP_REFERER", "")
    if ref and url_has_allowed_host_and_scheme(ref, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(ref)
    return redirect(fallback)


def _can_edit(call, user):
    if is_tcn(user):
        return call.status != S.CANCELLED          # le TCN peut aussi saisir ATB / ATD
    return call.status == S.PLANNED and call.agent_id == user.id


# ─────────────────────────────────────────────────────────────
#  Authentification
# ─────────────────────────────────────────────────────────────
def login_view(request):
    if request.user.is_authenticated:
        return redirect("home")

    next_url = request.POST.get("next") or request.GET.get("next") or ""
    if request.method == "POST":
        user = authenticate(
            request,
            username=request.POST.get("username", "").strip(),
            password=request.POST.get("password", ""),
        )
        if user is not None:
            login(request, user)
            if not request.POST.get("remember_me"):
                request.session.set_expiry(0)  # expire à la fermeture du navigateur
            messages.success(request, f"Bienvenue {user.get_full_name() or user.username}.")
            if next_url and url_has_allowed_host_and_scheme(
                next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
            ):
                return redirect(next_url)
            return redirect("home")
        messages.error(request, "Nom d'utilisateur ou mot de passe incorrect.")

    return render(request, "login.html", {"next": next_url})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("login")


# ─────────────────────────────────────────────────────────────
#  Berth plan
# ─────────────────────────────────────────────────────────────
@login_required
def home(request):
    recompute_plan()
    now = timezone.now()
    working = get_working_call()
    planned = VesselCall.objects.filter(status=S.PLANNED).select_related("vessel")
    today_owner = window_owner(now)
    context = {
        "working": working,
        "working_end": working_interval(working, now)[1] if working else None,
        "next_call": planned.order_by("planned_start").first(),
        "week_count": planned.filter(planned_start__lt=now + timedelta(days=7)).count(),
        "lines": _lines_payload().values(),
        "my_line": user_line(request.user),
        "today_owner": today_owner or "Libre",
        "today_color": LINE_COLORS.get(today_owner, FREE_COLOR),
        "quay": get_quay(),
        "focus": request.GET.get("focus", ""),
    }
    return render(request, "home.html", context)


# ─────────────────────────────────────────────────────────────
#  Page « Escales » (liste séparée)
# ─────────────────────────────────────────────────────────────
STATUS_FILTERS = {
    "actives": ("Actives", [S.PLANNED, S.WORKING]),
    "PLANNED": ("Planifiées", [S.PLANNED]),
    "WORKING": ("En opération", [S.WORKING]),
    "COMPLETED": ("Terminées", [S.COMPLETED]),
    "CANCELLED": ("Annulées", [S.CANCELLED]),
    "toutes": ("Toutes", None),
}


@login_required
def calls_list(request):
    recompute_plan()
    status = request.GET.get("statut", "actives")
    if status not in STATUS_FILTERS:
        status = "actives"
    line = request.GET.get("ligne", "")
    q = request.GET.get("q", "").strip()

    calls = VesselCall.objects.select_related("vessel", "agent")
    if not is_tcn(request.user):
        calls = calls.filter(agent=request.user)
    base = calls
    statuses = STATUS_FILTERS[status][1]
    if statuses:
        calls = calls.filter(status__in=statuses)
    if line in dict(ShippingLine.choices):
        calls = calls.filter(line=line)
    if q:
        calls = calls.filter(Q(vessel__name__icontains=q) | Q(vessel__imo__icontains=q) | Q(voyage__icontains=q))
    calls = list(calls.order_by("-planned_start" if status in ("COMPLETED", "CANCELLED", "toutes") else "planned_start")[:300])

    quay = get_quay()
    working = get_working_call()
    for c in calls:
        c.can_edit = _can_edit(c, request.user)
        _, _, c.bollard_from, c.bollard_to = berth_span(c, quay)

    counts = {key: (base.filter(status__in=st).count() if st else base.count())
              for key, (_, st) in STATUS_FILTERS.items()}
    return render(request, "calls.html", {
        "calls": calls,
        "working": working,
        "status": status,
        "status_tabs": [(k, label, counts[k]) for k, (label, _) in STATUS_FILTERS.items()],
        "line": line,
        "q": q,
        "lines": ShippingLine.choices,
    })


# ─────────────────────────────────────────────────────────────
#  Page « Paramétrage » : quai + bollards (planificateur)
# ─────────────────────────────────────────────────────────────
@login_required
def quay_settings(request):
    denied = _tcn_required(request)
    if denied:
        return denied

    quay = get_quay()
    formset = None
    if request.method == "POST":
        _save_windows(request)
        action = request.POST.get("action", "save")
        form = QuaySettingsForm(request.POST, instance=quay)
        if action == "generate":
            if form.is_valid():
                quay = form.save()
                n = quay.generate_bollards(form.cleaned_data["nb_bollards"])
                _after_quay_change(request, quay)
                messages.success(request, f"{n} bollards générés et répartis sur {_num_fr(quay.length_m)} m.")
                return redirect("quay_settings")
        else:
            formset = BollardFormSet(request.POST, instance=quay)
            if form.is_valid():
                formset.quay_length = form.cleaned_data["length_m"]
                if formset.is_valid():
                    quay = form.save()
                    formset.save()
                    _after_quay_change(request, quay)
                    messages.success(request, "Paramètres du quai enregistrés.")
                    return redirect("quay_settings")
            else:
                formset.is_valid()
    else:
        form = QuaySettingsForm(instance=quay, initial={"nb_bollards": quay.bollards.count() or 15})

    if formset is None:
        formset = BollardFormSet(instance=quay)
    owners = get_day_owners()
    return render(request, "settings.html", {
        "form": form,
        "formset": formset,
        "quay": quay,
        "quay_json": _quay_payload(get_quay()),
        "windows": [
            {"day": d, "name": name, "owner": owners.get(d) or "",
             "color": LINE_COLORS.get(owners.get(d), FREE_COLOR)}
            for d, name in WEEKDAYS
        ],
        "line_choices": [(code, label, LINE_COLORS[code]) for code, label in ShippingLine.choices],
        "free_color": FREE_COLOR,
    })


def _save_windows(request):
    """Enregistre les fenêtres prioritaires (un champ « window_<jour> » par jour)."""
    if not any(f"window_{d}" in request.POST for d, _ in WEEKDAYS):
        return
    valid = set(ShippingLine.values)
    changed = []
    owners = get_day_owners()
    for d, name in WEEKDAYS:
        value = request.POST.get(f"window_{d}", "")
        value = value if value in valid else ""
        if (owners.get(d) or "") != value:
            PriorityDay.objects.update_or_create(weekday=d, defaults={"line": value})
            changed.append(f"{name} → {value or 'libre'}")
    if changed:
        recompute_plan()
        messages.success(request, "Fenêtres prioritaires mises à jour : " + ", ".join(changed) + ".")


def _num_fr(v):
    v = float(v)
    return f"{v:.0f}" if v == int(v) else f"{v:.2f}".rstrip("0").replace(".", ",")


def _after_quay_change(request, quay):
    """Prévient si des escales actives ne tiennent plus sur le quai."""
    too_long = VesselCall.objects.filter(
        status__in=[S.PLANNED, S.WORKING], vessel__loa__gt=quay.length_m
    ).select_related("vessel")
    for c in too_long:
        messages.warning(request, f"{c.vessel.name} ({_num_fr(c.vessel.loa)} m) est plus long que le quai.")
    too_deep = VesselCall.objects.filter(status__in=[S.PLANNED, S.WORKING], draft__gt=quay.max_draft_m)
    if too_deep.exists():
        messages.warning(request, f"{too_deep.count()} escale(s) ont un tirant d'eau supérieur au nouveau maximum.")


# ─────────────────────────────────────────────────────────────
#  Export PDF du berth plan
# ─────────────────────────────────────────────────────────────
@login_required
def plan_pdf(request):
    try:
        from .pdf import build_plan_pdf
    except ImportError:
        return HttpResponse("Module reportlab manquant : pip install reportlab", status=500,
                            content_type="text/plain; charset=utf-8")
    recompute_plan()
    start_date = parse_date(request.GET.get("start", "") or "") or (timezone.localdate() - timedelta(days=1))
    try:
        days = max(1, min(int(request.GET.get("days", 7)), 31))
    except ValueError:
        days = 7
    pdf = build_plan_pdf(start_date, days)
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'attachment; filename="berth-plan-TCN_{start_date:%Y-%m-%d}_{days}j.pdf"'
    return resp


def _feedback(request, call):
    if call.status == S.COMPLETED:
        messages.success(request, f"{call.vessel.name} : ATD enregistré le {_fmt(call.actual_departure)}, escale terminée.")
        return
    if call.status == S.WORKING:
        messages.success(request, f"{call.vessel.name} : ATB enregistré le {_fmt(call.actual_berthing)}, navire à quai.")
        return
    start = _fmt(call.planned_start)
    owner = window_owner(call.eta)
    if call.shift_reason:
        messages.warning(
            request,
            f"{call.vessel.name} est décalé ({call.shift_reason}). Accostage prévu le {start}.",
        )
    elif call.is_priority:
        messages.success(
            request,
            f"{call.vessel.name} est planifié le {start}, dans la fenêtre prioritaire {call.line}.",
        )
    else:
        messages.success(request, f"{call.vessel.name} est planifié à son ETB, le {start}.")
        if owner and owner != call.line:
            messages.info(
                request,
                f"Cet ETA tombe dans la fenêtre {owner}. Si un navire {owner} est déclaré sur ce "
                f"créneau, votre navire sera décalé après son ETD.",
            )


def _form_context(form, title, call=None):
    working = get_working_call()
    w_interval = working_interval(working) if working else None
    vessels = [
        {"id": v.pk, "name": v.name, "imo": v.imo, "loa": float(v.loa), "line": v.line}
        for v in form.fields["vessel"].queryset
    ]
    return {
        "form": form,
        "title": title,
        "call": call,
        "vessels_json": vessels,
        "lines_json": _lines_payload(),
        "day_owner_json": {str(k): v for k, v in get_day_owners().items()},
        "working": working,
        "working_json": {
            "id": working.pk, "name": working.vessel.name, "line": working.line,
            "start": _iso(w_interval[0]), "end": _iso(w_interval[1]),
        } if working else None,
        "week": _week_strip(),
        "my_line": form.user_line,
        "quay": form.quay,
        "quay_json": _quay_payload(form.quay),
    }


@login_required
def call_create(request):
    if not user_role(request.user):
        messages.error(request, "Votre compte n'a pas de rôle (TCN ou ligne maritime). Contactez TCN.")
        return redirect("home")

    form = VesselCallForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        call = form.save()
        recompute_plan()
        call.refresh_from_db()
        _feedback(request, call)
        return redirect(f"{reverse('home')}?focus={call.pk}")
    return render(request, "call_form.html", _form_context(form, "Déclarer une escale"))


@login_required
def call_edit(request, pk):
    call = get_object_or_404(VesselCall.objects.select_related("vessel"), pk=pk)
    if not _can_edit(call, request.user):
        messages.error(request, "Cette escale ne peut plus être modifiée.")
        return redirect("home")

    form = VesselCallForm(request.POST or None, user=request.user, instance=call)
    if request.method == "POST" and form.is_valid():
        call = form.save()
        recompute_plan()
        call.refresh_from_db()
        _feedback(request, call)
        return redirect(f"{reverse('home')}?focus={call.pk}")
    return render(request, "call_form.html", _form_context(form, "Modifier l'escale", call))


@login_required
@require_POST
def call_cancel(request, pk):
    call = get_object_or_404(VesselCall, pk=pk)
    if not _can_edit(call, request.user) or call.status != S.PLANNED:
        return HttpResponseForbidden("Action non autorisée.")
    call.status = S.CANCELLED
    call.save(update_fields=["status", "updated_at"])
    recompute_plan()
    messages.info(request, f"Escale de {call.vessel.name} annulée. Le plan a été recalculé.")
    return _back(request)


@login_required
@require_POST
def call_berth(request, pk):
    """Agent TCN : saisie de l'ATB (accostage réel), début des opérations."""
    if not is_tcn(request.user):
        return HttpResponseForbidden("Réservé aux agents TCN.")
    call = get_object_or_404(VesselCall.objects.select_related("vessel"), pk=pk, status=S.PLANNED)
    working = get_working_call()
    if working:
        messages.error(request, f"{working.vessel.name} est déjà à quai. Saisissez son ATD d'abord.")
        return _back(request)

    now = timezone.now()
    atb = _parse_dt(request.POST.get("atb")) or now
    if atb > now + timedelta(minutes=15):
        messages.error(request, "L'ATB est une heure réelle : elle ne peut pas être dans le futur.")
        return _back(request)
    call.status = S.WORKING
    call.actual_berthing = atb
    call.planned_start = atb
    call.planned_end = max(call.etd, atb + timedelta(hours=1))
    call.shift_reason = ""
    try:
        call.save()
    except IntegrityError:
        messages.error(request, "Un autre navire vient d'être mis à quai.")
        return _back(request)
    recompute_plan()
    messages.success(request, f"ATB enregistré : {call.vessel.name} à quai depuis le {_fmt(atb)}.")
    return _back(request)


@login_required
@require_POST
def call_complete(request, pk):
    """Agent TCN : saisie de l'ATD (départ réel), le quai se libère."""
    if not is_tcn(request.user):
        return HttpResponseForbidden("Réservé aux agents TCN.")
    call = get_object_or_404(VesselCall.objects.select_related("vessel"), pk=pk, status=S.WORKING)
    now = timezone.now()
    atd = _parse_dt(request.POST.get("atd")) or now
    if atd > now + timedelta(minutes=15):
        messages.error(request, "L'ATD est une heure réelle : elle ne peut pas être dans le futur.")
        return _back(request)
    if call.actual_berthing and atd <= call.actual_berthing:
        messages.error(request, "L'ATD doit être après l'ATB.")
        return _back(request)
    call.status = S.COMPLETED
    call.actual_departure = atd
    call.planned_end = atd
    call.save()
    recompute_plan()
    messages.success(request, f"ATD enregistré : {call.vessel.name} a quitté le quai le {_fmt(atd)}.")
    return _back(request)


# ─────────────────────────────────────────────────────────────
#  Utilisateurs et rôles (agent TCN)
# ─────────────────────────────────────────────────────────────
@login_required
def users_list(request, pk=None):
    denied = _tcn_required(request)
    if denied:
        return denied
    User = get_user_model()
    editing = get_object_or_404(User, pk=pk) if pk else None

    form = UserForm(request.POST or None, instance=editing, current_user=request.user)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        if editing:
            messages.success(request, f"Utilisateur {user.username} mis à jour.")
        else:
            messages.success(request, f"Utilisateur {user.username} créé avec le rôle {Role(form.cleaned_data['role']).label}.")
        return redirect("users_list")

    users = list(User.objects.select_related("agent_profile").order_by("username"))
    for u in users:
        u.role_code = user_role(u)
        u.role_label = Role(u.role_code).label if u.role_code else "Aucun rôle"
        u.role_color = LINE_COLORS.get(u.role_code, "#f0b429" if u.role_code else "#5a6580")
    counts = {code: sum(1 for u in users if u.role_code == code) for code, _ in Role.choices}
    return render(request, "users.html", {
        "form": form,
        "editing": editing,
        "users": users,
        "role_counts": [(code, label, counts[code], LINE_COLORS.get(code, "#f0b429")) for code, label in Role.choices],
    })


# ─────────────────────────────────────────────────────────────
#  API temps réel (polling)
# ─────────────────────────────────────────────────────────────
def _serialize(call, user, now, quay):
    start, end = call.planned_start or call.eta, call.planned_end or call.etd
    pos_from, pos_to, b_from, b_to = berth_span(call, quay)
    if call.status == S.WORKING:
        start, end = working_interval(call, now)
    delay = (start - call.berthing_request).total_seconds() / 60 if call.status == S.PLANNED else 0
    return {
        "id": call.pk,
        "vessel": call.vessel.name,
        "imo": call.vessel.imo,
        "loa": float(call.vessel.loa),
        "voyage": call.voyage,
        "draft": float(call.draft) if call.draft is not None else None,
        "berth_from": pos_from,
        "berth_to": pos_to,
        "bollard_from": b_from.name if b_from else "",
        "bollard_to": b_to.name if b_to else "",
        "line": call.line,
        "line_name": call.get_line_display(),
        "color": LINE_COLORS.get(call.line),
        "agent": call.agent.get_full_name() or call.agent.username if call.agent else "—",
        "eta": _iso(call.eta),
        "etb": _iso(call.berthing_request),
        "etd": _iso(call.etd),
        "atb": _iso(call.actual_berthing),
        "atd": _iso(call.actual_departure),
        "start": _iso(start),
        "end": _iso(end),
        "status": call.status,
        "status_label": call.get_status_display(),
        "is_priority": call.is_priority,
        "shift_reason": call.shift_reason,
        "shifted": delay > 1,
        "delay_minutes": round(delay) if delay > 1 else 0,
        "window_owner": window_owner(call.eta),
        "mine": call.agent_id == user.id,
        "can_edit": _can_edit(call, user),
    }


@login_required
def api_plan(request):
    # Recalcul au plus toutes les 30 s, quel que soit le nombre d'agents connectés
    if cache.add("berth:recompute-lock", 1, 30):
        recompute_plan()

    now = timezone.now()
    start_date = parse_date(request.GET.get("start", "") or "") or (timezone.localdate() - timedelta(days=1))
    try:
        days = max(1, min(int(request.GET.get("days", 7)), 31))
    except ValueError:
        days = 7

    range_start = timezone.make_aware(datetime.combine(start_date, time.min))
    range_end = range_start + timedelta(days=days)

    calls = (
        VesselCall.objects.select_related("vessel", "agent")
        .exclude(status=S.CANCELLED)
        .filter(
            Q(status=S.WORKING)
            | Q(planned_start__lt=range_end, planned_end__gt=range_start)
            | Q(eta__lt=range_end, etd__gt=range_start, status=S.PLANNED)
            | Q(etb__lt=range_end, etd__gt=range_start, status=S.PLANNED)
        )
        .order_by("planned_start")
    )

    windows = []
    owners = get_day_owners()
    for i in range(days):
        d = start_date + timedelta(days=i)
        ws = timezone.make_aware(datetime.combine(d, time.min))
        windows.append({
            "date": d.isoformat(),
            "start": _iso(ws),
            "end": _iso(ws + timedelta(days=1)),
            "owner": owners.get(d.weekday()),
            "day_name": DAY_NAMES[d.weekday()],
            "day_short": DAY_SHORT[d.weekday()],
            "num": d.strftime("%d/%m"),
        })

    working = get_working_call()
    quay = get_quay()
    return JsonResponse({
        "now": _iso(now),
        "range": {"start": _iso(range_start), "end": _iso(range_end), "days": days},
        "quay": _quay_payload(quay),
        "lines": _lines_payload(),
        "free_color": FREE_COLOR,
        "windows": windows,
        "working_id": working.pk if working else None,
        "calls": [_serialize(c, request.user, now, quay) for c in calls],
        "user": {"line": user_line(request.user), "role": user_role(request.user), "is_tcn": is_tcn(request.user)},
    })