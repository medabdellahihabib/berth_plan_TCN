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


class Role(models.TextChoices):
    """Rôle d'un utilisateur : agent TCN (planificateur) ou agent d'une des 4 lignes."""
    TCN = "TCN", "Agent TCN (planificateur)"
    MAE = "MAE", "Agent Maersk"
    HLC = "HLC", "Agent Hapag-Lloyd"
    CMA = "CMA", "Agent CMA CGM"
    ARK = "ARK", "Agent Arkas"


# Fenêtres prioritaires par défaut : 0 = lundi ... 6 = dimanche (datetime.weekday()).
# Les valeurs réellement utilisées sont modifiables dans Paramétrage (modèle PriorityDay).
PRIORITY_DAYS = {
    "MAE": (0, 5),  # lundi, samedi
    "HLC": (3, 4),  # jeudi, vendredi
    "CMA": (2, 6),  # mercredi, dimanche
    "ARK": (1,),    # mardi
}
DEFAULT_DAY_OWNER = {day: line for line, days in PRIORITY_DAYS.items() for day in days}

WEEKDAYS = [(0, "Lundi"), (1, "Mardi"), (2, "Mercredi"), (3, "Jeudi"),
            (4, "Vendredi"), (5, "Samedi"), (6, "Dimanche")]
FREE_COLOR = "#6c7a96"   # jour sans ligne prioritaire

LINE_COLORS = {
    "MAE": "#42b0d5",
    "HLC": "#f58220",
    "CMA": "#e5533d",
    "ARK": "#2ec4a6",
}


DAY_OWNERS_CACHE_KEY = "berth:day-owners"


def get_day_owners():
    """{jour (0-6): ligne prioritaire ou None} — paramétrable, mis en cache 5 min."""
    from django.core.cache import cache
    owners = cache.get(DAY_OWNERS_CACHE_KEY)
    if owners is None:
        rows = {p.weekday: (p.line or None) for p in PriorityDay.objects.all()}
        if len(rows) < 7:          # première utilisation : valeurs par défaut
            for day, _ in WEEKDAYS:
                if day not in rows:
                    PriorityDay.objects.get_or_create(weekday=day, defaults={"line": DEFAULT_DAY_OWNER.get(day, "")})
                    rows[day] = DEFAULT_DAY_OWNER.get(day)
        owners = rows
        cache.set(DAY_OWNERS_CACHE_KEY, owners, 300)
    return owners


def line_days(owners=None):
    """{ligne: [jours]} à partir des fenêtres paramétrées."""
    owners = owners or get_day_owners()
    return {code: [d for d, _ in WEEKDAYS if owners.get(d) == code] for code in ShippingLine.values}


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


def user_role(user):
    """Rôle de l'utilisateur : TCN, MAE, HLC, CMA ou ARK (None si inconnu)."""
    if not user or not user.is_authenticated:
        return None
    profile = getattr(user, "agent_profile", None)
    if profile is not None:
        role = profile.role or profile.line
        if role:
            return role
    # Compte sans profil : un administrateur Django est considéré comme agent TCN
    if user.is_superuser or user.is_staff:
        return Role.TCN
    return None


def is_tcn(user):
    """L'utilisateur est-il un agent TCN (planificateur) ?"""
    return user_role(user) == Role.TCN


def user_line(user):
    """Ligne maritime de l'agent connecté (None pour un agent TCN)."""
    role = user_role(user)
    return role if role in ShippingLine.values else None


def validate_imo(value):
    """Un n° IMO = 7 chiffres, le dernier est une clé de contrôle."""
    value = str(value).strip()
    if not (len(value) == 7 and value.isdigit()):
        raise ValidationError("Le n° IMO doit contenir exactement 7 chiffres.")
    checksum = sum(int(d) * (7 - i) for i, d in enumerate(value[:6])) % 10
    if checksum != int(value[6]):
        raise ValidationError("N° IMO invalide (clé de contrôle incorrecte).")


# ─────────────────────────────────────────────────────────────
#  Fenêtres prioritaires (paramétrables)
# ─────────────────────────────────────────────────────────────
class PriorityDay(models.Model):
    weekday = models.PositiveSmallIntegerField("Jour", choices=WEEKDAYS, unique=True)
    line = models.CharField("Ligne prioritaire", max_length=3, choices=ShippingLine.choices,
                            blank=True, default="", help_text="Vide : jour libre (premier arrivé, premier servi).")

    class Meta:
        ordering = ["weekday"]
        verbose_name = "Fenêtre prioritaire"
        verbose_name_plural = "Fenêtres prioritaires"

    def __str__(self):
        return f"{self.get_weekday_display()} : {self.line or 'libre'}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from django.core.cache import cache
        cache.delete(DAY_OWNERS_CACHE_KEY)


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

    def generate_bollards(self, count=None):
        """Recrée les bollards répartis sur le quai.
        - count : nombre de bollards, répartis uniformément de 0 à la longueur du quai ;
        - sinon : un bollard tous les `bollard_spacing_m` mètres."""
        self.bollards.all().delete()
        if count and count >= 2:
            spacing = (self.length_m / (count - 1)).quantize(Decimal("0.01"))
            positions = [(spacing * i).quantize(Decimal("0.01")) for i in range(count)]
            positions[-1] = self.length_m
            self.bollard_spacing_m = spacing
            QuayConfig.objects.filter(pk=self.pk).update(bollard_spacing_m=spacing)
        else:
            spacing = max(self.bollard_spacing_m, Decimal("5"))
            positions, pos = [], Decimal("0")
            while pos <= self.length_m:
                positions.append(pos)
                pos += spacing
        Bollard.objects.bulk_create([
            Bollard(quay=self, name=f"{self.bollard_prefix}{i:02d}", position_m=p)
            for i, p in enumerate(positions, start=1)
        ])
        return len(positions)


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
    role = models.CharField("Rôle", max_length=3, choices=Role.choices, blank=True, default="")
    line = models.CharField("Ligne maritime", max_length=3, choices=ShippingLine.choices, blank=True, default="",
                            help_text="Renseignée automatiquement à partir du rôle (vide pour un agent TCN).")
    company = models.CharField("Agence", max_length=120, blank=True)
    phone = models.CharField("Téléphone", max_length=30, blank=True)

    class Meta:
        verbose_name = "Profil utilisateur"
        verbose_name_plural = "Profils utilisateurs"

    def __str__(self):
        return f"{self.user.username} ({self.role or self.line})"

    def save(self, *args, **kwargs):
        if not self.role and self.line:
            self.role = self.line
        # la ligne suit le rôle : vide pour un agent TCN
        self.line = self.role if self.role in ShippingLine.values else ""
        super().save(*args, **kwargs)


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

    # Saisie de l'agent de ligne (et du TCN)
    eta = models.DateTimeField("ETA (arrivée)")
    etb = models.DateTimeField("ETB (accostage prévu)", null=True, blank=True)
    etd = models.DateTimeField("ETD (départ prévu)")

    # Calculé par le planificateur automatique (scheduler.py)
    planned_start = models.DateTimeField("Accostage prévu", null=True, blank=True)
    planned_end = models.DateTimeField("Départ prévu", null=True, blank=True)
    is_priority = models.BooleanField("Fenêtre prioritaire", default=False)
    shift_reason = models.CharField("Motif du décalage", max_length=255, blank=True)

    # Réel (saisi par l'agent TCN)
    actual_berthing = models.DateTimeField("ATB (accostage réel)", null=True, blank=True)
    actual_departure = models.DateTimeField("ATD (départ réel)", null=True, blank=True)

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
    def berthing_request(self):
        """Accostage demandé : ETB, ou ETA si l'ETB n'est pas renseigné."""
        return self.etb or self.eta

    @property
    def duration(self):
        return self.etd - self.berthing_request

    @property
    def delay(self):
        if self.planned_start and self.planned_start > self.berthing_request:
            return self.planned_start - self.berthing_request
        return None

    @property
    def color(self):
        return LINE_COLORS.get(self.line, "#8b96ad")