"""Local web panel: REST API, realtime WebSocket and the static three-column page."""
import asyncio
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from canaldark.chat.bridge import ChatBridge
from canaldark.config import load_channels
from canaldark.db import InvalidTransition, claim, get_video, transition, update_fields
from canaldark.models import State, VideoRow
from canaldark.publish.stage import publish_video
from canaldark.rules import SP

STATIC = Path(__file__).parent / "static"
STAGE_LABEL = {"captacao": "captação", "edicao": "edição", "publicacao": "publicação"}

STAGE_WHERE = {
    "captacao": "1 = 1",
    "edicao": "estado IN ('baixado','editando','editado','revisar','publicando','publicado')"
              " OR (estado = 'erro' AND etapa_erro IN ('edicao','publicacao'))",
    "publicacao": "estado IN ('editado','publicando','publicado') OR (estado = 'erro' AND etapa_erro = 'publicacao')",
}
ORDER = {"captacao": 0, "edicao": 1, "publicacao": 2}
RUNNING = {"captacao": State.baixando, "edicao": State.editando, "publicacao": State.publicando}
QUEUED = {"captacao": State.descoberto, "edicao": State.baixado, "publicacao": State.editado}
RETRY_TO = {"captacao": State.descoberto, "edicao": State.baixado, "publicacao": State.editado}


def card_status(row: VideoRow, stage: str) -> str:
    if row.estado == State.erro:
        return "erro" if row.etapa_erro == stage else "ok"
    if row.estado == RUNNING[stage]:
        return "andamento"
    if row.estado == QUEUED[stage]:
        return "fila"
    if row.estado == State.revisar:
        return "revisar" if stage == "edicao" else "ok"
    return "ok"


def headline(row: VideoRow) -> str | None:
    if not row.pacote_json:
        return None
    return " ".join(json.loads(row.pacote_json).get("manchete", [])) or None


def card(row: VideoRow, stage: str) -> dict:
    status = card_status(row, stage)
    final = Path(row.caminho_final) if row.caminho_final else None
    return {
        "id": row.id, "canal": row.canal, "titulo": row.titulo_origem, "fonte": row.fonte,
        "manchete": headline(row), "formato": row.formato, "estado": row.estado.value, "status": status,
        "done": status == "ok", "progresso": row.etapa_progresso if status == "andamento" else (100 if status == "ok" else 0),
        "erro": row.erro if status in ("erro", "revisar") else None, "nota": row.nota,
        "thumb": f"/media/{row.id}/thumb.jpg" if final and (final.parent / "thumb.jpg").exists() else None,
        "atualizado_em": row.atualizado_em,
    }


class PauseBody(BaseModel):
    channel: str | None = None
    stage: str | None = None
    until: datetime | None = None


class ChatBody(BaseModel):
    texto: str
    liberar: list[str] = []


class RedoBody(BaseModel):
    campo: Literal["manchete", "roteiro", "formato"]
    instrucao: str


class SourceBody(BaseModel):
    channel: str
    url: str


def source_entry(url: str) -> dict:
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if "youtube.com" in host:
        cid = parse_qs(parts.query).get("channel_id", [None])[0]
        if not cid and parts.path.startswith("/channel/"):
            cid = parts.path.split("/")[2]
        if not cid:
            raise HTTPException(422, "para canal do YouTube use a URL com /channel/UC... ou ?channel_id=")
        return {"tipo": "youtube_canal", "id": cid}
    if "reddit.com" in host and parts.path.startswith("/r/"):
        return {"tipo": "reddit", "sub": parts.path.split("/")[2]}
    return {"tipo": "rss", "url": url}


def append_source(path: Path, entry: dict) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.match(r"^fontes:\s*(#.*)?$", line)), None)
    if start is None:
        raise HTTPException(422, f"não achei a lista `fontes:` em {path.name}")
    end, indent = start + 1, "  "
    while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()):
        m = re.match(r"^(\s*)- ", lines[end])
        if m:
            indent = m.group(1)
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    first, *rest = entry.items()
    block = [f"{indent}- {first[0]}: {json.dumps(first[1], ensure_ascii=False)}\n"]
    block += [f"{indent}  {k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in rest]
    if end and not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"
    lines[end:end] = block
    path.write_text("".join(lines), encoding="utf-8")


def load_channels_safe(path: Path) -> str | None:
    import yaml

    try:
        return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("id")
    except yaml.YAMLError:
        return None


def create_app(pipeline, port: int = 8000) -> FastAPI:
    app = FastAPI(title="Canal Dark")
    db, bus = pipeline.db, pipeline.bus
    app.state.pipeline = pipeline
    background: set[asyncio.Task] = set()
    bridge = ChatBridge(pipeline.data / "mcp.json", port)
    chat_lock = asyncio.Lock()

    def in_background(coro) -> None:
        task = asyncio.create_task(coro)
        background.add(task)
        task.add_done_callback(background.discard)

    async def must_get(video_id: int) -> VideoRow:
        row = await get_video(db, video_id)
        if row is None:
            raise HTTPException(404, "vídeo não encontrado")
        return row

    async def moved(video_id: int, to: State, msg: str) -> dict:
        try:
            await transition(db, video_id, to)
        except InvalidTransition as e:
            raise HTTPException(409, str(e))
        await bus.publish("video.estado", {"id": video_id, "estado": to.value})
        await bus.log("sistema", msg, video_id=video_id)
        return {"ok": True, "estado": to.value}

    @app.get("/api/state")
    async def state():
        return {
            "canais": [{"id": c.id, "nome": c.nome} for c in pipeline.cfgs.values()],
            "pausas": [{"channel": c, "stage": s, "until": u.isoformat() if u else None}
                       for (c, s), u in pipeline.paused.items()],
            "contas_pausadas": [f"{p}:{a}" for p, a in sorted(pipeline.paused_accounts)],
            "ensaio": pipeline.dry_run,
        }

    @app.get("/api/videos")
    async def videos(stage: str = Query(..., pattern="^(captacao|edicao|publicacao)$"), channel: str | None = None,
                     before_id: int | None = None, limit: int = Query(30, ge=1, le=100)):
        sql = f"SELECT * FROM videos WHERE ({STAGE_WHERE[stage]})"
        params: list = []
        if channel:
            sql += " AND canal = ?"
            params.append(channel)
        if before_id:
            sql += " AND id < ?"
            params.append(before_id)
        async with db.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit)) as cur:
            rows = await cur.fetchall()
        return [card(VideoRow(**dict(r)), stage) for r in rows]

    @app.get("/api/videos/{video_id}")
    async def detail(video_id: int):
        row = await must_get(video_id)
        async with db.execute("SELECT plataforma, conta, url, publicado_em FROM publicacoes WHERE video_id = ?",
                              (video_id,)) as cur:
            pubs = [dict(r) for r in await cur.fetchall()]
        data = row.model_dump(mode="json")
        data["pacote"] = json.loads(row.pacote_json) if row.pacote_json else None
        data["publicacoes"] = pubs
        data["video"] = f"/media/{video_id}/final.mp4" if row.caminho_final else None
        return data

    @app.get("/media/{video_id}/{name}")
    async def media(video_id: int, name: str):
        row = await must_get(video_id)
        if name not in ("final.mp4", "thumb.jpg") or not row.caminho_final:
            raise HTTPException(404, "arquivo não encontrado")
        path = Path(row.caminho_final).parent / name
        if not path.exists():
            raise HTTPException(404, "arquivo não encontrado")
        return FileResponse(path)

    @app.post("/api/videos/{video_id}/retry")
    async def retry(video_id: int):
        row = await must_get(video_id)
        if row.estado != State.erro:
            raise HTTPException(409, "só dá para tentar de novo um vídeo com erro")
        to = RETRY_TO.get(row.etapa_erro or "", State.descoberto)
        if to == State.descoberto and not row.caminho_bruto and row.formato == "dialogo":
            to = State.baixado
        return await moved(video_id, to, f"tentando de novo a etapa de {STAGE_LABEL.get(row.etapa_erro, 'captação')}")

    @app.post("/api/videos/{video_id}/approve")
    async def approve(video_id: int):
        row = await must_get(video_id)
        if row.estado != State.revisar:
            raise HTTPException(409, "só dá para aprovar um vídeo em revisão")
        await update_fields(db, video_id, aprovado=True)
        return await moved(video_id, State.baixado, "aprovado pelo editor; vai para a renderização")

    @app.post("/api/videos/{video_id}/discard")
    async def discard(video_id: int):
        row = await must_get(video_id)
        if row.estado not in (State.revisar, State.erro, State.editado, State.descoberto):
            raise HTTPException(409, f"não dá para descartar um vídeo em {row.estado.value}")
        await db.execute("DELETE FROM publicacoes WHERE video_id = ?", (video_id,))
        await db.execute("DELETE FROM videos WHERE id = ?", (video_id,))
        await db.commit()
        for p in (row.caminho_final, row.caminho_bruto):
            if p and Path(p).exists():
                shutil.rmtree(Path(p).parent, ignore_errors=True) if p == row.caminho_final else Path(p).unlink()
        await bus.publish("video.removido", {"id": video_id})
        await bus.log("sistema", f"descartado: {row.titulo_origem}", video_id=video_id)
        return {"ok": True}

    @app.post("/api/videos/{video_id}/publish-now")
    async def publish_now(video_id: int):
        row = await must_get(video_id)
        if row.estado != State.editado:
            raise HTTPException(409, "só dá para publicar agora um vídeo editado")
        claimed = await claim(db, State.editado, State.publicando, video_id=video_id)
        if claimed is None:
            raise HTTPException(409, "o vídeo já está sendo publicado")
        in_background(publish_video(db, bus, pipeline.cfgs[row.canal], claimed, pipeline.get_uploader,
                                    pipeline.paused_accounts))
        return {"ok": True, "estado": "publicando"}

    @app.post("/api/videos/{video_id}/redo")
    async def redo(video_id: int, body: RedoBody):
        row = await must_get(video_id)
        if row.estado not in (State.editado, State.revisar, State.erro):
            raise HTTPException(409, f"não dá para refazer um vídeo em {row.estado.value}")
        await update_fields(db, video_id, instrucao=f"Refazer {body.campo}: {body.instrucao}", pacote_json=None,
                            aprovado=False)
        return await moved(video_id, State.baixado, f"refazendo {body.campo}: {body.instrucao}")

    @app.get("/api/status")
    async def status(channel: str | None = None):
        sql, params = "SELECT canal, estado, COUNT(*) AS n FROM videos", []
        if channel:
            sql, params = sql + " WHERE canal = ?", [channel]
        async with db.execute(sql + " GROUP BY canal, estado", params) as cur:
            counts: dict = {}
            for r in await cur.fetchall():
                counts.setdefault(r["canal"], {})[r["estado"]] = r["n"]
        async with db.execute("SELECT id, canal, titulo_origem, erro FROM videos WHERE estado IN ('erro','revisar')"
                              + (" AND canal = ?" if channel else "") + " ORDER BY id DESC LIMIT 10", params) as cur:
            pending = [dict(r) for r in await cur.fetchall()]
        return {**(await state()), "por_estado": counts, "atencao": pending}

    @app.post("/api/sources")
    async def add_source(body: SourceBody):
        if body.channel not in pipeline.cfgs:
            raise HTTPException(404, "canal não encontrado")
        entry = source_entry(body.url)
        folder = pipeline.root / "canais"
        path = next((p for p in sorted(folder.glob("*.y*ml")) if load_channels_safe(p) == body.channel), None)
        if path is None:
            raise HTTPException(404, f"não achei o YAML do canal {body.channel}")
        backup = path.read_text(encoding="utf-8")
        append_source(path, entry)
        try:
            pipeline.cfgs[body.channel] = load_channels(folder)[body.channel]
        except ValueError as e:
            path.write_text(backup, encoding="utf-8")
            raise HTTPException(422, str(e))
        await bus.log("sistema", f"[{body.channel}] fonte adicionada: {entry}")
        return {"ok": True, "fonte": entry}

    @app.post("/api/chat", status_code=202)
    async def chat(body: ChatBody):
        await bus.publish("chat.mensagem", {"papel": "voce", "texto": body.texto})

        async def run():
            async with chat_lock:
                async for msg in bridge.send(body.texto, body.liberar):
                    await bus.publish("chat.mensagem", msg)
        in_background(run())
        return {"ok": True}

    @app.post("/api/pause")
    async def pause(body: PauseBody):
        until = body.until.astimezone(SP) if body.until else None
        pipeline.pause(body.channel, body.stage, until)
        what = " ".join(x for x in [body.channel or "todos os canais", STAGE_LABEL.get(body.stage or "", "")] if x)
        await bus.log("sistema", f"pausado: {what}" + (f" até {until:%d/%m %H:%M}" if until else ""))
        return {"ok": True}

    @app.post("/api/resume")
    async def resume(body: PauseBody):
        pipeline.resume(body.channel, body.stage)
        await bus.log("sistema", f"retomado: {body.channel or 'todos os canais'} {STAGE_LABEL.get(body.stage or '', '')}".strip())
        return {"ok": True}

    @app.post("/api/capture-now")
    async def capture_now(channel: str, query: str | None = None):
        if channel not in pipeline.cfgs:
            raise HTTPException(404, "canal não encontrado")
        return {"novos": await pipeline.capture_now(channel, query)}

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        sub = bus.subscribe()

        async def forward():
            for item in await bus.recent_logs(200):
                await socket.send_json(item)
            async for event in sub:
                await socket.send_json(event)

        async def until_closed():
            while True:
                if (await socket.receive())["type"] == "websocket.disconnect":
                    return

        tasks = [asyncio.create_task(forward()), asyncio.create_task(until_closed())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            sub.close()

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app
