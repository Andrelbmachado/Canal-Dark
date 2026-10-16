"""Panel chat: talks to the user's local Claude Code (`claude -p`) with the canaldark MCP tools."""
import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

PREFIX = "mcp__canaldark__"
SAFE_TOOLS = ["status", "buscar_agora", "pausar", "retomar", "refazer"]
CONFIRM_TOOLS = {"aprovar", "descartar", "publicar_agora", "adicionar_fonte"}
NOT_FOUND = "Claude Code não encontrado neste computador"
SYSTEM = ("Você é o assistente do painel Canal Dark, que produz Shorts/Reels automáticos. Use as ferramentas "
          "mcp__canaldark__* para consultar e controlar o painel. Responda curto, em português do Brasil.")


class ChatBridge:
    def __init__(self, mcp_config: Path, port: int, timeout: float = 300):
        self.mcp_config = mcp_config
        self.port = port
        self.timeout = timeout
        self.session_id: str | None = None

    def _write_config(self) -> None:
        self.mcp_config.parent.mkdir(parents=True, exist_ok=True)
        self.mcp_config.write_text(json.dumps({"mcpServers": {"canaldark": {
            "command": sys.executable, "args": ["-m", "canaldark.chat.mcp_server"],
            "env": {"CANALDARK_URL": f"http://localhost:{self.port}"}}}}, indent=2))

    def _args(self, text: str, liberar: list[str]) -> list[str]:
        tools = SAFE_TOOLS + [t for t in liberar if t in CONFIRM_TOOLS]
        args = [os.environ.get("CANALDARK_CLAUDE_BIN", "claude"), "-p", text, "--output-format", "stream-json",
                "--verbose", "--mcp-config", str(self.mcp_config), "--allowedTools",
                ",".join(PREFIX + t for t in tools), "--append-system-prompt", SYSTEM]
        if self.session_id:
            args += ["--resume", self.session_id]
        return args

    async def send(self, text: str, liberar: list[str] | None = None) -> AsyncIterator[dict]:
        self._write_config()
        try:
            proc = await asyncio.create_subprocess_exec(*self._args(text, liberar or []),
                                                        stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
        except (FileNotFoundError, PermissionError):
            yield {"papel": "sistema", "texto": NOT_FOUND}
            return
        stderr_task = asyncio.create_task(proc.stderr.read())
        denied: list[str] = []
        said_anything = False
        try:
            async with asyncio.timeout(self.timeout):
                async for raw in proc.stdout:
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("session_id"):
                        self.session_id = ev["session_id"]
                    if ev.get("type") == "assistant":
                        for block in ev.get("message", {}).get("content", []):
                            if block.get("type") == "text" and block.get("text", "").strip():
                                said_anything = True
                                yield {"papel": "claude", "texto": block["text"].strip()}
                            elif block.get("type") == "tool_use":
                                name = block.get("name", "").removeprefix(PREFIX)
                                yield {"papel": "sistema", "texto": f"usando a ferramenta {name}…"}
                    elif ev.get("type") == "result":
                        for d in ev.get("permission_denials") or []:
                            name = d.get("tool_name", "").removeprefix(PREFIX)
                            if name in CONFIRM_TOOLS and name not in denied:
                                denied.append(name)
                        if not said_anything and ev.get("result"):
                            yield {"papel": "claude", "texto": str(ev["result"]).strip()}
        except TimeoutError:
            proc.kill()
            yield {"papel": "sistema", "texto": "o Claude Code demorou demais para responder"}
        err = (await stderr_task).decode(errors="replace").strip()
        code = await proc.wait()
        if denied:
            yield {"papel": "claude", "texto": "Preciso da sua confirmação para continuar.", "confirmar": denied}
        if code not in (0, None) and not said_anything:
            low = err.lower()
            hint = ("o Claude Code atingiu o limite de uso; tente mais tarde" if "limit" in low
                    else "o Claude Code não está logado; rode `claude` no terminal" if "login" in low
                    else f"o Claude Code falhou: {err[-300:] or f'código {code}'}")
            yield {"papel": "sistema", "texto": hint}
