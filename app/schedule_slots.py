"""Shared Eastern-time production slot selection and delayed-run validation.

GitHub workflow dispatches can remain queued for many minutes. Capture the
dispatch creation time, not the eventual runner start time, when selecting a slot.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
RUN_SLOTS = ((7, 30), (10, 0), (12, 30), (15, 30), (18, 30), (21, 0))
SLOT_RECOVERY_MINUTES = 55
MAX_QUEUE_DELAY = timedelta(hours=36)
SLOT_FORMAT = "%Y-%m-%dT%H:%M"


def slot_from_dispatch(dispatch_at):
    """Choose the eligible slot using when GitHub RECEIVED the dispatch."""
    if dispatch_at.tzinfo is None or dispatch_at.utcoffset() is None:
        raise ValueError("Dispatch time must contain a time zone.")
    local = dispatch_at.astimezone(ET)
    if local.weekday() >= 5:
        return None
    minute_of_day = local.hour * 60 + local.minute
    for hour, minute in RUN_SLOTS:
        age_minutes = minute_of_day - (hour * 60 + minute)
        if 0 <= age_minutes < SLOT_RECOVERY_MINUTES:
            return local.strftime("%Y-%m-%d") + f"T{hour:02d}:{minute:02d}"
    return None


def validate_accepted_slot(value, now):
    """Allow valid weekday slots to start late, including after Eastern midnight.

    The 36-hour cap avoids accepting ancient dispatches; source watermarks still
    make the next eligible cycle catch up if a dispatch is too old to execute.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.strptime(value, SLOT_FORMAT).replace(tzinfo=ET)
    except ValueError:
        return None
    if parsed.weekday() >= 5 or (parsed.hour, parsed.minute) not in RUN_SLOTS:
        return None
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Current time must contain a time zone.")
    age = now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)
    if not timedelta(0) <= age <= MAX_QUEUE_DELAY:
        return None
    return parsed.strftime(SLOT_FORMAT)


def slot_already_completed(last_completed, requested_slot):
    """A completed newer slot also supersedes a delayed older heartbeat."""
    try:
        previous = datetime.strptime(last_completed, SLOT_FORMAT)
        requested = datetime.strptime(requested_slot, SLOT_FORMAT)
    except (TypeError, ValueError):
        return False
    return previous >= requested
