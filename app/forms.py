from datetime import timedelta
from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils import timezone

from .models import (
    AgentProfile, Bollard, QuayConfig, Role, ShippingLine, Vessel, VesselCall,
    get_quay, is_tcn, user_line, user_role, validate_imo,
)
from .scheduler import find_working_conflict, get_working_call

DT_FMT = "%Y-%m-%dT%H:%M"


def DT_WIDGET():
    return forms.DateTimeInput(attrs={"type": "datetime-local"}, format=DT_FMT)
MIN_STAY = timedelta(hours=2)
MAX_STAY = timedelta(days=5)


def _fmt(dt):
    return timezone.localtime(dt).strftime("%d/%m/%Y à %H:%M")


class VesselCallForm(forms.Form):
    """Déclaration d'escale.
    - Agent de ligne : navire, ETA, ETB, ETD, tirant d'eau, voyage.
    - Agent TCN : tout cela + ligne, bollard, ATB (accostage réel) et ATD (départ réel)."""
    line = forms.ChoiceField(label="Ligne maritime", choices=ShippingLine.choices, required=False)
    vessel = forms.ModelChoiceField(
        label="Navire", queryset=Vessel.objects.none(), required=False, empty_label="Nouveau navire",
    )
    name = forms.CharField(label="Nom du navire", max_length=80)
    imo = forms.CharField(label="N° IMO", max_length=7, min_length=7, validators=[validate_imo])
    loa = forms.DecimalField(label="LOA (m)", max_digits=6, decimal_places=2, min_value=Decimal("30"))
    draft = forms.DecimalField(
        label="Tirant d'eau à l'arrivée (m)", max_digits=4, decimal_places=2,
        min_value=Decimal("1"), widget=forms.NumberInput(attrs={"step": "0.1"}),
    )
    voyage = forms.CharField(label="N° de voyage", max_length=30, required=False)
    eta = forms.DateTimeField(label="ETA (arrivée)", input_formats=[DT_FMT], widget=DT_WIDGET())
    etb = forms.DateTimeField(label="ETB (accostage prévu)", input_formats=[DT_FMT], widget=DT_WIDGET())
    etd = forms.DateTimeField(label="ETD (départ prévu)", input_formats=[DT_FMT], widget=DT_WIDGET())
    atb = forms.DateTimeField(label="ATB (accostage réel)", input_formats=[DT_FMT], widget=DT_WIDGET(), required=False)
    atd = forms.DateTimeField(label="ATD (départ réel)", input_formats=[DT_FMT], widget=DT_WIDGET(), required=False)
    bollard = forms.ModelChoiceField(
        label="Bollard de départ (arrière du navire)", queryset=Bollard.objects.none(),
        required=False, empty_label="Automatique (début du quai)",
    )

    TCN_ONLY = ("line", "bollard", "atb", "atd")

    def __init__(self, *args, user=None, instance=None, **kwargs):
        self.user = user
        self.instance = instance
        self.quay = get_quay()
        if instance is not None and "initial" not in kwargs:
            bollard = None
            if instance.berth_from is not None:
                bollard = self.quay.bollards.filter(position_m=instance.berth_from).first()
            kwargs["initial"] = {
                "line": instance.line,
                "vessel": instance.vessel_id,
                "name": instance.vessel.name,
                "imo": instance.vessel.imo,
                "loa": instance.vessel.loa,
                "draft": instance.draft,
                "voyage": instance.voyage,
                "eta": instance.eta,
                "etb": instance.berthing_request,
                "etd": instance.etd,
                "atb": instance.actual_berthing,
                "atd": instance.actual_departure,
                "bollard": bollard.pk if bollard else None,
            }
        super().__init__(*args, **kwargs)

        self.is_tcn = is_tcn(user)
        self.user_line = user_line(user)

        if self.is_tcn:
            self.fields["line"].required = True
            self.fields["line"].initial = instance.line if instance else None
            self.fields["vessel"].queryset = Vessel.objects.all()
            self.fields["bollard"].queryset = self.quay.bollards.all()
        else:
            # L'agent de ligne ne choisit ni la ligne (celle de son rôle), ni la position, ni ATB / ATD
            for name in self.TCN_ONLY:
                del self.fields[name]
            self.fields["vessel"].queryset = Vessel.objects.filter(line=self.user_line)

        self.fields["loa"].widget.attrs["max"] = float(self.quay.length_m)
        self.fields["draft"].widget.attrs["max"] = float(self.quay.max_draft_m)
        self.fields["draft"].help_text = f"Maximum autorisé au quai : {self.quay.max_draft_m} m"

    # ──────────────────────────────────────────────
    def get_line(self):
        if self.is_tcn:
            return self.cleaned_data.get("line")
        return self.user_line

    def clean_imo(self):
        return self.cleaned_data["imo"].strip()

    def clean_name(self):
        return " ".join(self.cleaned_data["name"].upper().split())

    def clean_loa(self):
        loa = self.cleaned_data["loa"]
        if loa > self.quay.length_m:
            raise forms.ValidationError(f"LOA supérieure à la longueur du quai ({self.quay.length_m} m).")
        return loa

    def clean_draft(self):
        draft = self.cleaned_data["draft"]
        if draft > self.quay.max_draft_m:
            raise forms.ValidationError(
                f"Tirant d'eau supérieur au maximum autorisé au quai ({self.quay.max_draft_m} m)."
            )
        return draft

    def clean(self):
        cd = super().clean()
        line = self.get_line()
        if not line:
            raise forms.ValidationError("Aucune ligne maritime n'est associée à cette escale.")

        imo = cd.get("imo")
        if imo:
            existing = Vessel.objects.filter(imo=imo).first()
            if existing and existing.line != line:
                self.add_error("imo", f"Ce n° IMO est déjà enregistré pour la ligne {existing.get_line_display()}.")

        bollard, loa = cd.get("bollard"), cd.get("loa")
        if bollard and loa and bollard.position_m + loa > self.quay.length_m:
            self.add_error("bollard", "Le navire dépasserait la fin du quai depuis ce bollard.")

        eta, etb, etd = cd.get("eta"), cd.get("etb"), cd.get("etd")
        atb, atd = cd.get("atb"), cd.get("atd")
        if not (eta and etb and etd):
            return cd
        if etb < eta:
            self.add_error("etb", "L'ETB (accostage) ne peut pas être avant l'ETA (arrivée).")
            return cd
        if etd <= etb:
            self.add_error("etd", "L'ETD doit être après l'ETB.")
            return cd
        if etd - etb < MIN_STAY:
            self.add_error("etd", "Durée à quai minimale : 2 heures.")
            return cd
        if etd - etb > MAX_STAY:
            self.add_error("etd", "Durée à quai maximale : 5 jours.")
            return cd

        # Heures réelles (agent TCN)
        if atd and not atb:
            self.add_error("atd", "Renseignez d'abord l'ATB (accostage réel).")
            return cd
        if atb and atd and atd <= atb:
            self.add_error("atd", "L'ATD doit être après l'ATB.")
            return cd
        now = timezone.now()
        if atb and atb > now + timedelta(minutes=15):
            self.add_error("atb", "L'ATB est une heure réelle : elle ne peut pas être dans le futur.")
            return cd
        if atd and atd > now + timedelta(minutes=15):
            self.add_error("atd", "L'ATD est une heure réelle : elle ne peut pas être dans le futur.")
            return cd

        exclude_pk = getattr(self.instance, "pk", None)
        if atb and not atd:
            working = get_working_call()
            if working and working.pk != exclude_pk:
                raise forms.ValidationError(
                    f"{working.vessel.name} est déjà à quai (en opération). Saisissez son ATD avant l'ATB de ce navire."
                )

        if not atb:
            eta_changed = self.instance is None or self.instance.berthing_request != etb
            if eta_changed and etb < now - timedelta(minutes=30):
                self.add_error("etb", "L'ETB ne peut pas être dans le passé.")
                return cd

            # ── Règle : vessel working in berth ──
            conflict = find_working_conflict(etb, etd, exclude_pk=exclude_pk)
            if conflict:
                working, _, end = conflict
                raise forms.ValidationError(
                    f"Tu ne peux pas : il y a un vessel working in berth. "
                    f"{working.vessel.name} ({working.line}) est en opération au quai jusqu'au {_fmt(end)}. "
                    f"Choisis un ETB après la fin des opérations."
                )

        if imo:
            dup = VesselCall.objects.filter(
                vessel__imo=imo,
                status__in=[VesselCall.Status.PLANNED, VesselCall.Status.WORKING],
                eta__lt=etd, etd__gt=eta,
            )
            if exclude_pk:
                dup = dup.exclude(pk=exclude_pk)
            if dup.exists():
                raise forms.ValidationError("Ce navire a déjà une escale déclarée sur cette période.")
        return cd

    def save(self):
        S = VesselCall.Status
        cd = self.cleaned_data
        line = self.get_line()
        vessel, _ = Vessel.objects.update_or_create(
            imo=cd["imo"], defaults={"name": cd["name"], "loa": cd["loa"], "line": line}
        )
        call = self.instance or VesselCall(agent=self.user)
        call.vessel = vessel
        call.line = line
        call.voyage = cd.get("voyage", "")
        call.draft = cd["draft"]
        call.eta = cd["eta"]
        call.etb = cd["etb"]
        call.etd = cd["etd"]
        if self.is_tcn:
            bollard = cd.get("bollard")
            call.berth_from = bollard.position_m if bollard else None
            atb, atd = cd.get("atb"), cd.get("atd")
            call.actual_berthing, call.actual_departure = atb, atd
            if atd:                                   # départ réel → escale terminée
                call.status = S.COMPLETED
                call.planned_start, call.planned_end = atb, atd
                call.shift_reason = ""
            elif atb:                                 # accostage réel → navire à quai
                call.status = S.WORKING
                call.planned_start, call.planned_end = atb, max(cd["etd"], atb + timedelta(hours=1))
                call.shift_reason = ""
            elif call.status in (S.WORKING, S.COMPLETED):
                call.status = S.PLANNED               # ATB effacé : retour au planning
        if call.status == S.PLANNED:
            call.planned_start = call.planned_start or cd["etb"]
            call.planned_end = call.planned_end or cd["etd"]
        call.save()
        return call


# ─────────────────────────────────────────────────────────────
#  Utilisateurs et rôles (gérés par l'agent TCN)
# ─────────────────────────────────────────────────────────────
User = get_user_model()


class UserForm(forms.Form):
    username = forms.CharField(label="Nom d'utilisateur", max_length=150)
    first_name = forms.CharField(label="Prénom", max_length=150, required=False)
    last_name = forms.CharField(label="Nom", max_length=150, required=False)
    email = forms.EmailField(label="E-mail", required=False)
    role = forms.ChoiceField(label="Rôle", choices=Role.choices)
    company = forms.CharField(label="Agence / service", max_length=120, required=False)
    phone = forms.CharField(label="Téléphone", max_length=30, required=False)
    is_active = forms.BooleanField(label="Compte actif", required=False, initial=True)
    password1 = forms.CharField(label="Mot de passe", widget=forms.PasswordInput(render_value=False), required=False)
    password2 = forms.CharField(label="Confirmer le mot de passe", widget=forms.PasswordInput(render_value=False), required=False)

    def __init__(self, *args, instance=None, current_user=None, **kwargs):
        self.instance = instance
        self.current_user = current_user
        if instance is not None and "initial" not in kwargs:
            profile = getattr(instance, "agent_profile", None)
            kwargs["initial"] = {
                "username": instance.username, "first_name": instance.first_name,
                "last_name": instance.last_name, "email": instance.email,
                "role": user_role(instance) or "", "is_active": instance.is_active,
                "company": profile.company if profile else "", "phone": profile.phone if profile else "",
            }
        super().__init__(*args, **kwargs)
        if instance is None:
            self.fields["password1"].required = True
            self.fields["password2"].required = True
        else:
            self.fields["password1"].help_text = "Laisser vide pour ne pas changer le mot de passe."

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        qs = User.objects.filter(username__iexact=username)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("Ce nom d'utilisateur existe déjà.")
        return username

    def clean(self):
        cd = super().clean()
        p1, p2 = cd.get("password1"), cd.get("password2")
        if p1 or p2:
            if p1 != p2:
                self.add_error("password2", "Les deux mots de passe ne correspondent pas.")
            else:
                try:
                    validate_password(p1, self.instance)
                except forms.ValidationError as e:
                    self.add_error("password1", e)
        if self.instance is not None and self.instance == self.current_user:
            if cd.get("role") != Role.TCN:
                self.add_error("role", "Vous ne pouvez pas retirer votre propre rôle TCN.")
            if not cd.get("is_active"):
                self.add_error("is_active", "Vous ne pouvez pas désactiver votre propre compte.")
        return cd

    def save(self):
        cd = self.cleaned_data
        user = self.instance or User()
        user.username = cd["username"]
        user.first_name = cd.get("first_name", "")
        user.last_name = cd.get("last_name", "")
        user.email = cd.get("email", "")
        user.is_active = cd.get("is_active", False) if self.instance is not None else True
        # Un agent TCN a accès à l'administration Django ; un agent de ligne non
        if not user.is_superuser:
            user.is_staff = cd["role"] == Role.TCN
        if cd.get("password1"):
            user.set_password(cd["password1"])
        user.save()
        AgentProfile.objects.update_or_create(
            user=user, defaults={"role": cd["role"], "company": cd.get("company", ""), "phone": cd.get("phone", "")}
        )
        return user


# ─────────────────────────────────────────────────────────────
#  Paramétrage du quai et des bollards
# ─────────────────────────────────────────────────────────────
class QuaySettingsForm(forms.ModelForm):
    nb_bollards = forms.IntegerField(
        label="Nombre de bollards", min_value=2, max_value=300,
        help_text="Utilisé par le bouton « Générer » : bollards répartis uniformément sur le quai.",
    )

    class Meta:
        model = QuayConfig
        fields = ["name", "length_m", "start_m", "max_draft_m", "bollard_prefix"]
        labels = {
            "name": "Nom du quai",
            "length_m": "Longueur du quai (m)",
            "start_m": "Métrage de départ (m)",
            "max_draft_m": "Tirant d'eau maximum (m)",
            "bollard_prefix": "Préfixe des bollards",
        }
        widgets = {
            "length_m": forms.NumberInput(attrs={"step": "0.5"}),
            "start_m": forms.NumberInput(attrs={"step": "0.5"}),
            "max_draft_m": forms.NumberInput(attrs={"step": "0.1"}),
        }

    def clean_length_m(self):
        length = self.cleaned_data["length_m"]
        if length < 50:
            raise forms.ValidationError("Le quai doit mesurer au moins 50 m.")
        return length

    def clean_max_draft_m(self):
        draft = self.cleaned_data["max_draft_m"]
        if not (Decimal("2") <= draft <= Decimal("30")):
            raise forms.ValidationError("Valeur attendue entre 2 et 30 m.")
        return draft


class BaseBollardFormSet(BaseInlineFormSet):
    quay_length = None  # renseigné par la vue avec la longueur saisie

    def clean(self):
        super().clean()
        names, positions = set(), set()
        for form in self.forms:
            if not hasattr(form, "cleaned_data") or not form.cleaned_data or form.cleaned_data.get("DELETE"):
                continue
            name = (form.cleaned_data.get("name") or "").strip().upper()
            pos = form.cleaned_data.get("position_m")
            if name in names:
                form.add_error("name", "Nom déjà utilisé.")
            if pos is not None:
                if self.quay_length is not None and pos > self.quay_length:
                    form.add_error("position_m", f"Au-delà du quai ({self.quay_length} m).")
                if pos in positions:
                    form.add_error("position_m", "Position déjà occupée.")
                positions.add(pos)
            names.add(name)
            form.cleaned_data["name"] = name
            form.instance.name = name


BollardFormSet = inlineformset_factory(
    QuayConfig, Bollard, formset=BaseBollardFormSet,
    fields=["name", "position_m"], extra=0, can_delete=True,
    widgets={"position_m": forms.NumberInput(attrs={"step": "0.5"})},
)