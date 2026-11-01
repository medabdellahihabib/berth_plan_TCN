from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


# ─────────────────────────────────────────────────────────────
#  Lignes maritimes & fenêtres prioritaires
# ─────────────────────────────────────────────────────────────
class ShippingLine(models.TextChoices):
    MAE = "MAE", "Maersk"
    HLC = "HLC", "Hapag-Lloyd"
    CMA = "CMA", "CMA CGM"
    ARK = "ARK", "Arkas"


# 0 = lundi ... 6 = dimanche (datetime.weekday())
PRIORITY_DAYS = {
    "MAE": (0, 5),  # lundi, samedi
    "HLC": (3, 4),  # jeudi, vendredi
    "CMA": (2, 6),  # mercredi, dimanche
    "ARK": (1,),    # mardi
}

# Jour -> ligne propriétaire de la fenêtre
DAY_OWNER = {day: line for line, days in PRIORITY_DAYS.items() for day in days}

LINE_COLORS = {
    "MAE": "#42b0d5",
    "HLC": "#f58220",
    "CMA": "#e5533d",
    "ARK": "#2ec4a6",
}


def get_quay():
    """Configuration du quai (créée automatiquement au premier appel, modifiable dans l'admin)."""
    quay = QuayConfig.objects.prefetch_related("bollards").first()
    if quay is None:
        quay = QuayConfig.objects.create(
            pk=1, length_m=Decimal(str(getattr(settings, "BERTH_QUAY_LENGTH", 350)))
        )
        quay.generate_bollards()
        quay = QuayConfig.objects.prefetch_related("bollards").get(pk=1)
    return quay


def quay_length():
    """Longueur du quai en mètres (paramétrée dans Admin > Configuration du quai)."""
    return float(get_quay().length_m)


def user_line(user):
    """Ligne maritime de l'agent connecté (None pour un planificateur sans profil)."""
    if not user or not user.is_authenticated:
        return None
    profile = getattr(user, "agent_profile", None)
    return profile.line if profile else None


def validate_imo(value):
    """Un n° IMO = 7 chiffres, le dernier est une clé de contrôle."""
    value = str(value).strip()
    if not (len(value) == 7 and value.isdigit()):
        raise ValidationError("Le n° IMO doit contenir exactement 7 chiffres.")
    checksum = sum(int(d) * (7 - i) for i, d in enumerate(value[:6])) % 10
    if checksum != int(value[6]):
        raise ValidationError("N° IMO invalide (clé de contrôle incorrecte).")


# ─────────────────────────────────────────────────────────────
#  Quai : métrage + bollards (paramétrables dans l'admin)
# ─────────────────────────────────────────────────────────────
class QuayConfig(models.Model):
    name = models.CharField("Nom du quai", max_length=60, default="Quai TCN")
    length_m = models.DecimalField(
        "Longueur du quai (m)", max_digits=7, decimal_places=2, default=Decimal("350"),
        validators=[MinValueValidator(Decimal("50"))],
    )
    start_m = models.DecimalField(
        "Métrage de départ (m)", max_digits=7, decimal_places=2, default=Decimal("0"),
        help_text="Métrage affiché au début du quai (ex. 0, ou 400 si le quai commence au PK 400).",
    )
    max_draft_m = models.DecimalField(
        "Tirant d'eau maximum (m)", max_digits=4, decimal_places=2, default=Decimal("12.00"),
    )
    bollard_spacing_m = models.DecimalField(
        "Espacement des bollards (m)", max_digits=5, decimal_places=2, default=Decimal("25"),
        help_text="Sert à générer automatiquement les bollards (action « Régénérer les bollards »).",
    )
    bollard_prefix = models.CharField("Préfixe des bollards", max_length=5, default="B")

    class Meta:
        verbose_name = "Configuration du quai"
        verbose_name_plural = "Configuration du quai"

    def __str__(self):
        return f"{self.name} ({self.length_m} m)"

    def save(self, *args, **kwargs):
        self.pk = 1  # une seule configuration
        super().save(*args, **kwargs)

    def generate_bollards(self):
        """Recrée les bollards tous les `bollard_spacing_m` mètres, de 0 à la longueur du quai."""
        self.bollards.all().delete()
        spacing = max(self.bollard_spacing_m, Decimal("5"))
        pos, i, items = Decimal("0"), 1, []
        while pos <= self.length_m:
            items.append(Bollard(quay=self, name=f"{self.bollard_prefix}{i:02d}", position_m=pos))
            pos += spacing
            i += 1
        Bollard.objects.bulk_create(items)
        return len(items)


class Bollard(models.Model):
    quay = models.ForeignKey(QuayConfig, on_delete=models.CASCADE, related_name="bollards")
    name = models.CharField("Nom", max_length=10)
    position_m = models.DecimalField(
        "Position (m depuis le début du quai)", max_digits=7, decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )

    class Meta:
        ordering = ["position_m"]
        verbose_name = "Bollard"
        constraints = [models.UniqueConstraint(fields=["quay", "name"], name="unique_bollard_name")]

    def __str__(self):
        return f"{self.name} ({self.position_m} m)"


def berth_span(call, quay):
    """Position du navire le long du quai : (début m, fin m, bollard arrière, bollard avant)."""
    loa, length = float(call.vessel.loa), float(quay.length_m)
    start = float(call.berth_from) if call.berth_from is not None else 0.0
    start = max(0.0, min(start, length - loa))
    end = start + loa
    bollards = list(quay.bollards.all())
    b_from = next((b for b in reversed(bollards) if float(b.position_m) <= start + 0.01),
                  bollards[0] if bollards else None)
    b_to = next((b for b in bollards if float(b.position_m) >= end - 0.01),
                bollards[-1] if bollards else None)
    return start, end, b_from, b_to


# ─────────────────────────────────────────────────────────────
#  Agents
# ─────────────────────────────────────────────────────────────
class AgentProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="agent_profile"
    )
    line = models.CharField("Ligne maritime", max_length=3, choices=ShippingLine.choices)
    company = models.CharField("Agence", max_length=120, blank=True)
    phone = models.CharField("Téléphone", max_length=30, blank=True)

    class Meta:
        verbose_name = "Agent maritime"
        verbose_name_plural = "Agents maritimes"

    def __str__(self):
        return f"{self.user.username} ({self.line})"


# ─────────────────────────────────────────────────────────────
#  Navires
# ─────────────────────────────────────────────────────────────
class Vessel(models.Model):
    name = models.CharField("Nom", max_length=80)
    imo = models.CharField("N° IMO", max_length=7, unique=True, validators=[validate_imo])
    loa = models.DecimalField(
        "LOA (m)", max_digits=6, decimal_places=2, validators=[MinValueValidator(Decimal("30"))]
    )
    line = models.CharField("Ligne", max_length=3, choices=ShippingLine.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Navire"

    def __str__(self):
        return f"{self.name} (IMO {self.imo})"


# ─────────────────────────────────────────────────────────────
#  Escales (lignes du berth plan)
# ─────────────────────────────────────────────────────────────
class VesselCall(models.Model):
    class Status(models.TextChoices):
        PLANNED = "PLANNED", "Planifié"
        WORKING = "WORKING", "En opération"
        COMPLETED = "COMPLETED", "Terminé"
        CANCELLED = "CANCELLED", "Annulé"

    vessel = models.ForeignKey(Vessel, on_delete=models.PROTECT, related_name="calls")
    line = models.CharField("Ligne", max_length=3, choices=ShippingLine.choices)
    agent = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="vessel_calls"
    )
    voyage = models.CharField("N° voyage", max_length=30, blank=True)
    draft = models.DecimalField(
        "Tirant d'eau à l'arrivée (m)", max_digits=4, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("1"))],
    )
    berth_from = models.DecimalField(
        "Position au quai (m)", max_digits=7, decimal_places=2, null=True, blank=True,
        help_text="Position de l'arrière du navire le long du quai (fixée par le planificateur).",
    )

    # Saisie de l'agent
    eta = models.DateTimeField("ETA demandé")
    etd = models.DateTimeField("ETD demandé")

    # Calculé par le planificateur automatique (scheduler.py)
    planned_start = models.DateTimeField("Accostage prévu", null=True, blank=True)
    planned_end = models.DateTimeField("Départ prévu", null=True, blank=True)
    is_priority = models.BooleanField("Fenêtre prioritaire", default=False)
    shift_reason = models.CharField("Motif du décalage", max_length=255, blank=True)

    # Réel (saisi par le planificateur TCN)
    actual_berthing = models.DateTimeField("Accostage réel", null=True, blank=True)
    actual_departure = models.DateTimeField("Départ réel", null=True, blank=True)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PLANNED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["planned_start", "eta"]
        verbose_name = "Escale"
        constraints = [
            # Un seul navire en opération à la fois sur le quai
            models.UniqueConstraint(
                fields=["status"], condition=Q(status="WORKING"), name="one_vessel_working"
            ),
        ]

    def __str__(self):
        return f"{self.vessel.name} — {self.eta:%d/%m %H:%M}"

    @property
    def duration(self):
        return self.etd - self.eta

    @property
    def delay(self):
        if self.planned_start and self.planned_start > self.eta:
            return self.planned_start - self.eta
        return None

    @property
    def color(self):
        return LINE_COLORS.get(self.line, "#8b96ad")