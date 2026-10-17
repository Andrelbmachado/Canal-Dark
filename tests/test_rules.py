from datetime import datetime
from zoneinfo import ZoneInfo

from canaldark.rules import SP, TSE_BLACKOUTS, in_tse_blackout, publish_allowed


def test_window_is_the_planned_one():
    assert SP == ZoneInfo("America/Sao_Paulo")
    assert TSE_BLACKOUTS == [(datetime(2026, 10, 22, tzinfo=SP), datetime(2026, 10, 27, tzinfo=SP))]


def test_blackout_edges():
    assert not in_tse_blackout(datetime(2026, 10, 21, 23, 59, tzinfo=SP))
    assert in_tse_blackout(datetime(2026, 10, 22, 0, 0, tzinfo=SP))
    assert in_tse_blackout(datetime(2026, 10, 26, 23, 59, tzinfo=SP))
    assert not in_tse_blackout(datetime(2026, 10, 27, 0, 0, tzinfo=SP))


def test_blackout_converts_utc():
    # 22/10 02:00 UTC ainda é 21/10 23:00 em São Paulo
    assert not in_tse_blackout(datetime(2026, 10, 22, 2, 0, tzinfo=ZoneInfo("UTC")))


def test_blackout_only_for_politics_with_flag(cfg_factory):
    inside = datetime(2026, 10, 24, 12, tzinfo=SP)
    politics = cfg_factory(regras="politica", pausa_eleitoral=True)
    assert not publish_allowed(politics, inside)
    assert publish_allowed(politics, datetime(2026, 10, 27, 0, 0, tzinfo=SP))
    assert publish_allowed(cfg_factory(regras="politica", pausa_eleitoral=False), inside)
    assert publish_allowed(cfg_factory(regras="padrao"), inside)
