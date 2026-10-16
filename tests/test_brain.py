import json
from pathlib import Path

import httpx
import pytest
import respx

from canaldark import brain
from canaldark.brain import BrainError, ask_json
from canaldark.models import Package

FAKE = str(Path(__file__).parent / "fakes" / "fake_claude.py")
PKG = {"formato": "noticia", "manchete": ["GTA 6 confirmado"], "roteiro": "r", "fonte": "Rockstar",
       "titulo_post": "t", "descricao_post": "d", "hashtags": ["#gta6"]}
OLLAMA = "http://localhost:11434/api/generate"


@pytest.fixture
def queue(tmp_path, monkeypatch):
    q = tmp_path / "q.json"
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_QUEUE", str(q))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("CANALDARK_CLAUDE_BIN", FAKE)

    def set_(items):
        q.write_text(json.dumps(items))
        return log
    return set_


def claude_ok(obj):
    return {"stdout": {"type": "result", "result": "Aqui está:\n" + json.dumps(obj), "session_id": "s1"}}


async def test_uses_claude_when_available(queue):
    queue([claude_ok(PKG)])
    with respx.mock(assert_all_called=False) as m:
        route = m.post(OLLAMA)
        pkg = await ask_json("p", Package)
    assert isinstance(pkg, Package) and pkg.fonte == "Rockstar"
    assert not route.called


async def test_falls_back_to_ollama_when_claude_missing(monkeypatch):
    monkeypatch.setenv("CANALDARK_CLAUDE_BIN", "/nao/existe")
    with respx.mock() as m:
        m.post(OLLAMA).mock(return_value=httpx.Response(200, json={"response": json.dumps(PKG)}))
        pkg = await ask_json("p", Package)
    assert pkg.manchete == ["GTA 6 confirmado"]


async def test_falls_back_on_usage_limit(queue):
    queue([{"exit": 1, "stderr": "Claude usage limit reached"}])
    with respx.mock() as m:
        m.post(OLLAMA).mock(return_value=httpx.Response(200, json={"response": json.dumps(PKG)}))
        pkg = await ask_json("p", Package)
    assert pkg.fonte == "Rockstar"


async def test_retries_once_on_invalid_json(queue):
    log = queue([claude_ok({"formato": "x"}), claude_ok(PKG)])
    pkg = await ask_json("p", Package)
    assert pkg.fonte == "Rockstar"
    calls = log.read_text().splitlines()
    assert len(calls) == 2
    assert "inválid" in json.loads(calls[1])[1].lower() or "valid" in calls[1].lower()


async def test_brain_error_when_both_fail(monkeypatch):
    monkeypatch.setenv("CANALDARK_CLAUDE_BIN", "/nao/existe")
    with respx.mock() as m:
        m.post(OLLAMA).mock(side_effect=httpx.ConnectError("recusado"))
        with pytest.raises(BrainError) as e:
            await ask_json("p", Package)
    msg = str(e.value).lower()
    assert "claude" in msg and "ollama" in msg


def test_extract_json_handles_fences():
    assert brain.extract_json('blá ```json\n{"a": {"b": 1}}\n``` fim') == {"a": {"b": 1}}


async def test_triage_sends_short_ids_and_caps_items(queue):
    from canaldark.models import ChannelConfig, SourceItem
    cfg = ChannelConfig(id="gta6", nome="GTA", fontes=[{"tipo": "google_news", "busca": "x"}],
                        formatos={"noticia": 1.0}, estilo_manchete="a", persona_roteiro="b",
                        voz={"id": "pm_alex"}, horarios=["09:00"], contas={"youtube": "gta6"})
    items = [SourceItem(url=f"https://news.google.com/rss/articles/{'X' * 200}{i}", title=f"Notícia {i}",
                        source_name="s") for i in range(140)]
    log = queue([claude_ok({"itens": [{"id": 1, "nota": 8, "grupo": "a"}, {"id": 2, "nota": 3, "grupo": "b"},
                                      {"id": 999, "nota": 9, "grupo": "c"}]})])
    res = await brain.triage(cfg, items)
    prompt = json.loads(log.read_text().splitlines()[0])[1]
    assert "news.google.com" not in prompt and prompt.count("título:") == brain.TRIAGE_MAX
    assert [(r.url, r.nota) for r in res] == [(items[0].url, 8), (items[1].url, 3)]
