"""
Moteur de planification du berth plan TCN.

Règles :
1. Chaque jour peut appartenir à une ligne (fenêtre prioritaire), paramétrable dans
   Paramétrage (par défaut MAE lun/sam · ARK mar · CMA mer/dim · HLC jeu/ven).
   Un jour sans ligne est libre : premier arrivé, premier servi.
2. Un navire dont l'ETA tombe dans la fenêtre de SA ligne est prioritaire :
   il est placé en premier à son ETB (accostage demandé, ou ETA à défaut). Un navire d'une autre ligne qui
   chevauche est décalé après l'ETD du navire prioritaire, même s'il a
   été saisi avant.
3. Si la ligne prioritaire ne déclare aucun navire (ou si son navire
   n'est pas arrivé avant la fin de sa journée), le créneau est libre :
   le navire suivant accoste normalement (premier arrivé, premier servi).
4. Un navire en opération (WORKING) bloque le quai : aucune saisie
   ne peut chevaucher sa période d'opération.
"""
from datetime import datetime, time, timedelta

from django.db import transaction
from django.utils import timezone

from .models import VesselCall, get_day_owners

ONE_MINUTE = timedelta(minutes=1)


def window_owner(dt, owners=None):
    """Ligne propriétaire du jour de `dt` (heure locale du terminal), None si jour libre."""
    owners = owners if owners is not None else get_day_owners()
    return owners.get(timezone.localtime(dt).weekday())


def day_bounds(dt):
    local_date = timezone.localtime(dt).date()
    start = timezone.make_aware(datetime.combine(local_date, time.min))
    return start, start + timedelta(days=1)


def is_priority_call(call, now, owners=None):
    """Prioritaire si l'ETA est dans la fenêtre de sa ligne et que la journée n'est pas finie."""
    if window_owner(call.eta, owners) != call.line:
        return False
    _, window_end = day_bounds(call.eta)
    return now < window_end


def get_working_call():
    return (
        VesselCall.objects.filter(status=VesselCall.Status.WORKING)
        .select_related("vessel")
        .first()
    )


def working_interval(call, now=None):
    """Période où le quai est occupé par le navire en opération.
    S'il dépasse son ETD, le quai reste occupé jusqu'à la fin réelle."""
    now = now or timezone.now()
    start = call.actual_berthing or call.planned_start or call.berthing_request
    end = max(call.planned_end or call.etd, now)
    return start, end


def find_working_conflict(eta, etd, exclude_pk=None):
    """Retourne (navire, début, fin) si [eta, etd] (ou [etb, etd]) chevauche le navire en opération."""
    working = get_working_call()
    if not working or working.pk == exclude_pk:
        return None
    start, end = working_interval(working)
    if eta < end and etd > start:
        return working, start, end
    return None


def _first_fit(earliest, duration, placed):
    """Premier créneau libre >= earliest d'une durée donnée.
    placed = [(début, fin, escale), ...]. Retourne (début, escale bloquante)."""
    t, blocker = earliest, None
    for start, end, who in sorted(placed, key=lambda p: p[0]):
        if t + duration <= start:
            break
        if end > t:
            t, blocker = end, who
    return t, blocker


def _reason(call, start, blocker):
    if start <= call.berthing_request + ONE_MINUTE:
        return ""
    if blocker is None:
        return "ETB dépassé : replanifié à partir de maintenant"
    name = blocker.vessel.name
    if blocker.status == VesselCall.Status.WORKING:
        return f"Quai occupé par {name} (en opération)"
    if blocker.is_priority and blocker.line != call.line:
        return f"Fenêtre prioritaire {blocker.line} : décalé après l'ETD de {name}"
    return f"Quai occupé : décalé après {name}"


@transaction.atomic
def recompute_plan(now=None):
    """Recalcule tout le plan à partir des saisies. Retourne la liste des escales planifiées."""
    now = now or timezone.now()
    placed = []

    working = get_working_call()
    if working:
        start, end = working_interval(working, now)
        placed.append((start, end, working))

    pending = list(
        VesselCall.objects.select_for_update()
        .select_related("vessel")
        .filter(status=VesselCall.Status.PLANNED)
    )
    owners = get_day_owners()
    for call in pending:
        call.is_priority = is_priority_call(call, now, owners)

    # Prioritaires d'abord, puis premier à accoster (ETB) / premier saisi
    ordered = sorted(pending, key=lambda c: (not c.is_priority, c.berthing_request, c.created_at))

    for call in ordered:
        duration = max(call.etd - call.berthing_request, timedelta(hours=1))
        earliest = max(call.berthing_request, now)
        start, blocker = _first_fit(earliest, duration, placed)
        call.planned_start = start
        call.planned_end = start + duration
        call.shift_reason = _reason(call, start, blocker)
        placed.append((call.planned_start, call.planned_end, call))

    if pending:
        VesselCall.objects.bulk_update(
            pending, ["planned_start", "planned_end", "is_priority", "shift_reason"]
        )
    return pending