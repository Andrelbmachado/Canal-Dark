"""MCP server (stdio) that lets the local Claude Code control the panel through its HTTP API."""
import json
import os
from typing import Literal

import httpx
from mcp.server.mcpserver import MCPServer

BASE = os.environ.get("CANALDARK_URL", "http://localhost:8000")
server = MCPServer("canaldark", instructions=(
    "Ferramentas do painel Canal Dark (Shorts/Reels automáticos). Responda ao usuário em português do Brasil. "
    "Etapas: captacao, edicao, publicacao. Estados: descoberto, baixando, baixado, editando, editado, revisar, "
    "publicando, publicado, erro."))


async def _call(method: str, path: str, **kw) -> str:
    async with httpx.AsyncClient(base_url=BASE, timeout=120) as c:
        try:
            r = await c.request(method, path, **kw)
        except httpx.ConnectError:
            return f"o painel não está rodando em {BASE}; inicie com `canaldark run`"
    body = r.json() if r.content else {}
    if r.status_code >= 400:
        return f"erro {r.status_code}: {body.get('detail', body)}"
    return json.dumps(body, ensure_ascii=False)


def _clean(**kw) -> dict:
    return {k: v for k, v in kw.items() if v is not None}


@server.tool()
async def buscar_agora(canal: str, consulta: str | None = None) -> str:
    """Busca novidades agora nas fontes do canal; com `consulta`, busca esse assunto no Google Notícias."""
    return await _call("POST", "/api/capture-now", params=_clean(channel=canal, query=consulta))


@server.tool()
async def refazer(video_id: int, campo: Literal["manchete", "roteiro", "formato"], instrucao: str) -> str:
    """Refaz a edição de um vídeo seguindo a instrução (ex.: manchete mais curta)."""
    return await _call("POST", f"/api/videos/{video_id}/redo", json={"campo": campo, "instrucao": instrucao})


@server.tool()
async def pausar(canal: str | None = None, etapa: Literal["captacao", "edicao", "publicacao"] | None = None,
                 ate: str | None = None) -> str:
    """Pausa um canal, uma etapa ou tudo. `ate` é data e hora ISO (horário de Brasília) para retomar sozinho."""
    return await _call("POST", "/api/pause", json={"channel": canal, "stage": etapa, "until": ate})


@server.tool()
async def retomar(canal: str | None = None, etapa: Literal["captacao", "edicao", "publicacao"] | None = None) -> str:
    """Retoma o que foi pausado. Sem argumentos, retoma tudo."""
    return await _call("POST", "/api/resume", json={"channel": canal, "stage": etapa})


@server.tool()
async def aprovar(video_id: int) -> str:
    """Aprova um vídeo que está em revisão; ele é renderizado e segue para publicação."""
    return await _call("POST", f"/api/videos/{video_id}/approve")


@server.tool()
async def descartar(video_id: int) -> str:
    """Descarta um vídeo (não volta a ser captado)."""
    return await _call("POST", f"/api/videos/{video_id}/discard")


@server.tool()
async def publicar_agora(video_id: int) -> str:
    """Publica agora um vídeo editado, sem esperar o próximo horário."""
    return await _call("POST", f"/api/videos/{video_id}/publish-now")


@server.tool()
async def status(canal: str | None = None) -> str:
    """Resumo: vídeos por estado em cada canal, pausas e últimos erros."""
    return await _call("GET", "/api/status", params=_clean(channel=canal))


@server.tool()
async def adicionar_fonte(canal: str, url: str) -> str:
    """Adiciona uma fonte ao canal: feed RSS, canal do YouTube (URL com channel_id) ou subreddit."""
    return await _call("POST", "/api/sources", json={"channel": canal, "url": url})


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
