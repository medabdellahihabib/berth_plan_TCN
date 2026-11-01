from datetime import datetime, time, timedelta

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import IntegrityError
from django.db.models import Q
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .forms import VesselCallForm
from .models import (
    DAY_OWNER, LINE_COLORS, PRIORITY_DAYS, ShippingLine, VesselCall, berth_span, get_quay, user_line,
)
from .scheduler import (
    get_working_call, recompute_plan, window_owner, working_interval,
)

S = VesselCall.Status
DAY_NAMES = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
DAY_SHORT = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]


def _fmt(dt):
    return timezone.localtime(dt).strftime("%d/%m à %H:%M") if dt else "—"


def _iso(dt):
    return timezone.localtime(dt).isoformat() if dt else None


def _lines_payload():
    return {
        code: {
            "code": code,
            "name": label,
            "color": LINE_COLORS[code],
            "days": list(PRIORITY_DAYS[code]),
            "day_names": [DAY_NAMES[d] for d in PRIORITY_DAYS[code]],
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
    out = []
    for i in range(days):
        d = today + timedelta(days=i)
        owner = DAY_OWNER[d.weekday()]
        out.append({
            "date": d.isoformat(), "short": DAY_SHORT[d.weekday()], "num": d.strftime("%d/%m"),
            "owner": owner, "color": LINE_COLORS[owner],
        })
    return out


def _can_edit(call, user):
    return call.status == S.PLANNED and (user.is_staff or call.agent_id == user.id)


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

    calls = VesselCall.objects.select_related("vessel", "agent").exclude(status=S.CANCELLED)
    if not request.user.is_staff:
        calls = calls.filter(agent=request.user)
    calls = calls.filter(
        Q(status__in=[S.PLANNED, S.WORKING]) | Q(planned_end__gte=now - timedelta(days=3))
    ).order_by("planned_start")[:40]
    quay = get_quay()
    for c in calls:
        c.can_edit = _can_edit(c, request.user)
        _, _, c.bollard_from, c.bollard_to = berth_span(c, quay)

    today_owner = window_owner(now)
    context = {
        "working": working,
        "working_end": working_interval(working, now)[1] if working else None,
        "next_call": planned.order_by("planned_start").first(),
        "week_count": planned.filter(planned_start__lt=now + timedelta(days=7)).count(),
        "calls": calls,
        "lines": _lines_payload().values(),
        "my_line": user_line(request.user),
        "today_owner": today_owner,
        "today_color": LINE_COLORS[today_owner],
        "quay": get_quay(),
        "focus": request.GET.get("focus", ""),
    }
    return render(request, "home.html", context)


def _feedback(request, call):
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
        messages.success(request, f"{call.vessel.name} est planifié à son ETA, le {start}.")
        if owner != call.line:
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
        "day_owner_json": {str(k): v for k, v in DAY_OWNER.items()},
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
    if not user_line(request.user) and not request.user.is_staff:
        messages.error(request, "Votre compte n'est rattaché à aucune ligne maritime. Contactez TCN.")
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
    if not _can_edit(call, request.user):
        return HttpResponseForbidden("Action non autorisée.")
    call.status = S.CANCELLED
    call.save(update_fields=["status", "updated_at"])
    recompute_plan()
    messages.info(request, f"Escale de {call.vessel.name} annulée. Le plan a été recalculé.")
    return redirect("home")


@login_required
@require_POST
def call_berth(request, pk):
    """Planificateur TCN : le navire accoste, début des opérations."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Réservé au planificateur TCN.")
    call = get_object_or_404(VesselCall.objects.select_related("vessel"), pk=pk, status=S.PLANNED)
    working = get_working_call()
    if working:
        messages.error(request, f"{working.vessel.name} est déjà en opération au quai.")
        return redirect("home")

    now = timezone.now()
    call.status = S.WORKING
    call.actual_berthing = now
    call.planned_start = now
    call.planned_end = now + call.duration
    call.shift_reason = ""
    try:
        call.save()
    except IntegrityError:
        messages.error(request, "Un autre navire vient d'être mis en opération.")
        return redirect("home")
    recompute_plan()
    messages.success(request, f"{call.vessel.name} est à quai, opérations démarrées.")
    return redirect(f"{reverse('home')}?focus={call.pk}")


@login_required
@require_POST
def call_complete(request, pk):
    """Planificateur TCN : fin des opérations, le quai se libère."""
    if not request.user.is_staff:
        return HttpResponseForbidden("Réservé au planificateur TCN.")
    call = get_object_or_404(VesselCall.objects.select_related("vessel"), pk=pk, status=S.WORKING)
    now = timezone.now()
    call.status = S.COMPLETED
    call.actual_departure = now
    call.planned_end = now
    call.save()
    recompute_plan()
    messages.success(request, f"Opérations terminées pour {call.vessel.name}. Le quai est libre.")
    return redirect("home")


# ─────────────────────────────────────────────────────────────
#  API temps réel (polling)
# ─────────────────────────────────────────────────────────────
def _serialize(call, user, now, quay):
    start, end = call.planned_start or call.eta, call.planned_end or call.etd
    pos_from, pos_to, b_from, b_to = berth_span(call, quay)
    if call.status == S.WORKING:
        start, end = working_interval(call, now)
    delay = (start - call.eta).total_seconds() / 60 if call.status == S.PLANNED else 0
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
        "etd": _iso(call.etd),
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
        )
        .order_by("planned_start")
    )

    windows = []
    for i in range(days):
        d = start_date + timedelta(days=i)
        ws = timezone.make_aware(datetime.combine(d, time.min))
        windows.append({
            "date": d.isoformat(),
            "start": _iso(ws),
            "end": _iso(ws + timedelta(days=1)),
            "owner": DAY_OWNER[d.weekday()],
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
        "windows": windows,
        "working_id": working.pk if working else None,
        "calls": [_serialize(c, request.user, now, quay) for c in calls],
        "user": {"line": user_line(request.user), "is_staff": request.user.is_staff},
    })