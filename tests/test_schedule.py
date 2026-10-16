from datetime import datetime

from canaldark.models import ChannelConfig
from canaldark.publish.schedule import next_slot
from canaldark.rules import SP

CFG = ChannelConfig(id="gta6", nome="GTA", fontes=[{"tipo": "google_news", "busca": "GTA 6"}],
                    formatos={"noticia": 1.0}, estilo_manchete="a", persona_roteiro="b", voz={"id": "pm_alex"},
                    horarios=["09:00", "12:00", "18:00", "21:00"], contas={"youtube": "gta6"}, max_por_dia=4)


def at(h, m=0, day=16):
    return datetime(2026, 10, day, h, m, tzinfo=SP)


def test_next_slot_skips_past_times():
    assert next_slot(CFG, at(15), 0, False) == at(18)


def test_current_slot_is_due_within_grace():
    assert next_slot(CFG, at(18, 10), 1, False) == at(18)


def test_slot_already_used_moves_to_next():
    assert next_slot(CFG, at(18, 10), 2, False, last_published=at(18, 1)) == at(21)


def test_daily_cap_moves_to_tomorrow():
    assert next_slot(CFG, at(10), 4, False) == at(9, day=17)


def test_after_last_slot_goes_to_tomorrow():
    assert next_slot(CFG, at(23), 1, False) == at(9, day=17)


def test_urgent_publishes_now():
    assert next_slot(CFG, at(15, 7), 0, True) == at(15, 7)
