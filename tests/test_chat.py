import json
from pathlib import Path

import httpx
import pytest
import respx

from canaldark.chat import mcp_server
from canaldark.chat.bridge import ChatBridge

FAKE = str(Path(__file__).parent / "fakes" / "fake_claude.py")
TOOLS = {"buscar_agora", "refazer", "pausar", "retomar", "aprovar", "descartar", "publicar_agora", "status",
         "adicionar_fonte"}


def _stream(session, text, denied=()):
    lines = [{"type": "system", "subtype": "init", "session_id": session},
             {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}, "session_id": session},
             {"type": "result", "subtype": "success", "result": text, "session_id": session,
              "permission_denials": [{"tool_name": f"mcp__canaldark__{t}", "tool_input": {}} for t in denied]}]
    return "\n".join(json.dumps(x) for x in lines) + "\n"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    q, log = tmp_path / "q.json", tmp_path / "log.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_QUEUE", str(q))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("CANALDARK_CLAUDE_BIN", FAKE)

    def load(*items):
        q.write_text(json.dumps([{"stdout": s} for s in items]))
    load.calls = lambda: [json.loads(line) for line in log.read_text().splitlines()]
    return load


async def _all(gen):
    return [m async for m in gen]


async def test_mcp_tools_listed():
    names = {t.name for t in await mcp_server.server.list_tools()}
    assert names == TOOLS


async def test_status_tool_calls_api(monkeypatch):
    monkeypatch.setattr(mcp_server, "BASE", "http://localhost:8000")
    with respx.mock() as m:
        route = m.get("http://localhost:8000/api/status").mock(
            return_value=httpx.Response(200, json={"gta6": {"publicado": 3}}))
        out = await mcp_server.status("gta6")
    assert route.calls[0].request.url.params["channel"] == "gta6"
    assert "publicado" in out


async def test_bridge_resumes_session(fake, tmp_path):
    fake(_stream("S1", "Oi! Tudo rodando."), _stream("S1", "Pausei a edição."))
    bridge = ChatBridge(tmp_path / "mcp.json", port=8000)
    first = await _all(bridge.send("status?"))
    await _all(bridge.send("pausa a edição"))
    calls = fake.calls()
    assert "--resume" not in calls[0]
    assert calls[1][calls[1].index("--resume") + 1] == "S1"
    assert first[-1]["texto"] == "Oi! Tudo rodando."
    assert "--verbose" in calls[0] and "stream-json" in calls[0]
    assert json.loads((tmp_path / "mcp.json").read_text())["mcpServers"]["canaldark"]


async def test_destructive_tool_requires_confirmation(fake, tmp_path):
    fake(_stream("S2", "Quer que eu descarte o vídeo 4?", denied=["descartar"]), _stream("S2", "Descartado."))
    bridge = ChatBridge(tmp_path / "mcp.json", port=8000)
    msgs = await _all(bridge.send("descarta o vídeo 4"))
    assert msgs[-1]["confirmar"] == ["descartar"]
    allowed = fake.calls()[0][fake.calls()[0].index("--allowedTools") + 1]
    assert "mcp__canaldark__descartar" not in allowed
    await _all(bridge.send("Confirmado", liberar=["descartar"]))
    allowed = fake.calls()[1][fake.calls()[1].index("--allowedTools") + 1]
    assert "mcp__canaldark__descartar" in allowed


async def test_chat_without_claude_reports_clearly(tmp_path, monkeypatch):
    monkeypatch.setenv("CANALDARK_CLAUDE_BIN", str(tmp_path / "nao-existe"))
    msgs = await _all(ChatBridge(tmp_path / "mcp.json", port=8000).send("oi"))
    assert msgs == [{"papel": "sistema", "texto": "Claude Code não encontrado neste computador"}]
