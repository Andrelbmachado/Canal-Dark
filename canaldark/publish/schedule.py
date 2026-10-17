"""Posting schedule: fixed daily slots per channel, in America/Sao_Paulo time."""
from datetime import datetime, time, timedelta

from canaldark.models import ChannelConfig
from canaldark.rules import SP

GRACE = timedelta(minutes=60)  # a slot stays open this long after its time


def _slots(cfg: ChannelConfig, day) -> list[datetime]:
    return sorted(datetime.combine(day, time(int(h[:2]), int(h[3:])), tzinfo=SP) for h in cfg.horarios)


def next_slot(cfg: ChannelConfig, now: datetime, published_today: int, urgent: bool,
              last_published: datetime | None = None) -> datetime | None:
    now = now.astimezone(SP)
    tomorrow = _slots(cfg, now.date() + timedelta(days=1))
    if published_today >= cfg.max_por_dia:
        return tomorrow[0] if tomorrow else None
    if urgent:
        return now
    today = _slots(cfg, now.date())
    due = [s for s in today if s <= now]
    if due and now - due[-1] <= GRACE and (last_published is None or last_published < due[-1]):
        return due[-1]
    upcoming = [s for s in today if s > now]
    return upcoming[0] if upcoming else (tomorrow[0] if tomorrow else None)
