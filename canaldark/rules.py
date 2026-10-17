"""Calendário eleitoral: pausa a publicação do canal de política na janela do 2º turno."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .models import ChannelConfig

SP = ZoneInfo("America/Sao_Paulo")
# 72 h antes a 24 h depois do 2º turno (25/10/2026), em dias inteiros de São Paulo
TSE_BLACKOUTS = [(datetime(2026, 10, 22, tzinfo=SP), datetime(2026, 10, 27, tzinfo=SP))]


def in_tse_blackout(now: datetime) -> bool:
    local = now.astimezone(SP)
    return any(start <= local < end for start, end in TSE_BLACKOUTS)


def publish_allowed(cfg: ChannelConfig, now: datetime) -> bool:
    return not (cfg.regras == "politica" and cfg.pausa_eleitoral and in_tse_blackout(now))
