from datetime import timedelta
from decimal import Decimal

from django import forms
from django.utils import timezone

from django.forms import BaseInlineFormSet, inlineformset_factory

from .models import (
    Bollard, QuayConfig, ShippingLine, Vessel, VesselCall, get_quay, user_line, validate_imo,
)
from .scheduler import find_working_conflict

DT_FMT = "%Y-%m-%dT%H:%M"
MIN_STAY = timedelta(hours=2)
MAX_STAY = timedelta(days=5)


def _fmt(dt):
    return timezone.localtime(dt).strftime("%d/%m/%Y à %H:%M")


class VesselCallForm(forms.Form):
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
    eta = forms.DateTimeField(
        label="ETA", input_formats=[DT_FMT],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format=DT_FMT),
    )
    etd = forms.DateTimeField(
        label="ETD", input_formats=[DT_FMT],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format=DT_FMT),
    )
    bollard = forms.ModelChoiceField(
        label="Bollard de départ (arrière du navire)", queryset=Bollard.objects.none(),
        required=False, empty_label="Automatique (début du quai)",
    )

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
                "etd": instance.etd,
                "bollard": bollard.pk if bollard else None,
            }
        super().__init__(*args, **kwargs)

        self.is_staff = bool(user and user.is_staff)
        self.user_line = user_line(user)

        if self.is_staff:
            self.fields["line"].required = True
            self.fields["line"].initial = self.user_line or (instance.line if instance else None)
            self.fields["vessel"].queryset = Vessel.objects.all()
            self.fields["bollard"].queryset = self.quay.bollards.all()
        else:
            # L'agent ne choisit ni la ligne (celle de son compte) ni la position au quai
            del self.fields["line"]
            del self.fields["bollard"]
            self.fields["vessel"].queryset = Vessel.objects.filter(line=self.user_line)

        self.fields["loa"].widget.attrs["max"] = float(self.quay.length_m)
        self.fields["draft"].widget.attrs["max"] = float(self.quay.max_draft_m)
        self.fields["draft"].help_text = f"Maximum autorisé au quai : {self.quay.max_draft_m} m"

    # ──────────────────────────────────────────────
    def get_line(self):
        if self.is_staff:
            return self.cleaned_data.get("line") or self.user_line
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
            raise forms.ValidationError("Aucune ligne maritime n'est associée à votre compte.")

        imo = cd.get("imo")
        if imo:
            existing = Vessel.objects.filter(imo=imo).first()
            if existing and existing.line != line:
                self.add_error("imo", f"Ce n° IMO est déjà enregistré pour la ligne {existing.get_line_display()}.")

        bollard, loa = cd.get("bollard"), cd.get("loa")
        if bollard and loa and bollard.position_m + loa > self.quay.length_m:
            self.add_error("bollard", "Le navire dépasserait la fin du quai depuis ce bollard.")

        eta, etd = cd.get("eta"), cd.get("etd")
        if not (eta and etd):
            return cd
        if etd <= eta:
            self.add_error("etd", "L'ETD doit être après l'ETA.")
            return cd
        if etd - eta < MIN_STAY:
            self.add_error("etd", "Durée d'escale minimale : 2 heures.")
            return cd
        if etd - eta > MAX_STAY:
            self.add_error("etd", "Durée d'escale maximale : 5 jours.")
            return cd

        eta_changed = self.instance is None or self.instance.eta != eta
        if eta_changed and eta < timezone.now() - timedelta(minutes=30):
            self.add_error("eta", "L'ETA ne peut pas être dans le passé.")
            return cd

        # ── Règle : vessel working in berth ──
        conflict = find_working_conflict(eta, etd, exclude_pk=getattr(self.instance, "pk", None))
        if conflict:
            working, _, end = conflict
            raise forms.ValidationError(
                f"Tu ne peux pas : il y a un vessel working in berth. "
                f"{working.vessel.name} ({working.line}) est en opération au quai jusqu'au {_fmt(end)}. "
                f"Choisis un ETA après la fin des opérations."
            )

        if imo:
            dup = VesselCall.objects.filter(
                vessel__imo=imo,
                status__in=[VesselCall.Status.PLANNED, VesselCall.Status.WORKING],
                eta__lt=etd, etd__gt=eta,
            )
            if self.instance is not None:
                dup = dup.exclude(pk=self.instance.pk)
            if dup.exists():
                raise forms.ValidationError("Ce navire a déjà une escale déclarée sur cette période.")
        return cd

    def save(self):
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
        call.etd = cd["etd"]
        if self.is_staff:
            bollard = cd.get("bollard")
            call.berth_from = bollard.position_m if bollard else None
        call.planned_start = call.planned_start or cd["eta"]
        call.planned_end = call.planned_end or cd["etd"]
        call.save()
        return call


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