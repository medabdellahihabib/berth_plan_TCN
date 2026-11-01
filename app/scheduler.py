
from datetime import datetime, time, timedelta

from django.db import transaction
from django.utils import timezone

from .models import DAY_OWNER, VesselCall

ONE_MINUTE = timedelta(minutes=1)


def window_owner(dt):
    """Ligne propriétaire du jour de `dt` (heure locale du terminal)."""
    return DAY_OWNER[timezone.localtime(dt).weekday()]


def day_bounds(dt):
    local_date = timezone.localtime(dt).date()
    start = timezone.make_aware(datetime.combine(local_date, time.min))
    return start, start + timedelta(days=1)


def is_priority_call(call, now):
    """Prioritaire si l'ETA est dans la fenêtre de sa ligne et que la journée n'est pas finie."""
    if window_owner(call.eta) != call.line:
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
    start = call.actual_berthing or call.planned_start or call.eta
    end = max(call.planned_end or call.etd, now)
    return start, end


def find_working_conflict(eta, etd, exclude_pk=None):
    """Retourne (navire, début, fin) si [eta, etd] chevauche le navire en opération."""
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
    if start <= call.eta + ONE_MINUTE:
        return ""
    if blocker is None:
        return "ETA dépassé : replanifié à partir de maintenant"
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
    for call in pending:
        call.is_priority = is_priority_call(call, now)

    # Prioritaires d'abord, puis premier arrivé (ETA) / premier saisi
    ordered = sorted(pending, key=lambda c: (not c.is_priority, c.eta, c.created_at))

    for call in ordered:
        duration = call.etd - call.eta
        earliest = max(call.eta, now)
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