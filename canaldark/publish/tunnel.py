"""Optional free public URL for Instagram (Cloudflare quick tunnel over a local file server)."""
import asyncio
import re
import shutil
import socket
import sys
from pathlib import Path

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Tunnel:
    def __init__(self, timeout: float = 30):
        self.timeout = timeout
        self.url: str | None = None
        self._dir: Path | None = None
        self._server = None
        self._cloudflared = None

    async def start(self, directory: Path) -> str:
        if self.url and self._dir == directory:
            return self.url
        await self.stop()
        exe = shutil.which("cloudflared")
        if not exe:
            raise RuntimeError("cloudflared não encontrado; instale-o ou use o envio direto do Instagram")
        port = _free_port()
        self._server = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(directory),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        self._cloudflared = await asyncio.create_subprocess_exec(
            exe, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        self.url = await asyncio.wait_for(self._read_url(), self.timeout)
        self._dir = directory
        return self.url

    async def _read_url(self) -> str:
        async for raw in self._cloudflared.stderr:
            m = URL_RE.search(raw.decode(errors="replace"))
            if m:
                return m.group(0)
        raise RuntimeError("cloudflared encerrou sem informar a URL do túnel")

    async def stop(self) -> None:
        for proc in (self._cloudflared, self._server):
            if proc and proc.returncode is None:
                proc.terminate()
                await proc.wait()
        self._cloudflared = self._server = None
        self.url = self._dir = None
