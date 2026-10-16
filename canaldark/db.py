"""SQLite: schema, deduplicação e máquina de estados dos vídeos."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiosqlite

from .models import State, VideoRow

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  canal TEXT NOT NULL,
  url_origem TEXT NOT NULL,
  titulo_origem TEXT NOT NULL,
  fonte TEXT NOT NULL,
  estado TEXT NOT NULL,
  etapa_progresso REAL NOT NULL DEFAULT 0,
  formato TEXT,
  pacote_json TEXT,
  caminho_bruto TEXT,
  caminho_final TEXT,
  erro TEXT,
  etapa_erro TEXT,
  nota INTEGER,
  instrucao TEXT,
  aprovado INTEGER NOT NULL DEFAULT 0,
  criado_em TEXT NOT NULL,
  atualizado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_videos_estado ON videos(estado, id);
CREATE TABLE IF NOT EXISTS publicacoes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id INTEGER NOT NULL REFERENCES videos(id),
  plataforma TEXT NOT NULL,
  conta TEXT,
  id_externo TEXT,
  url TEXT,
  agendado_para TEXT,
  publicado_em TEXT,
  UNIQUE(video_id, plataforma)
);
CREATE TABLE IF NOT EXISTS vistos (
  hash TEXT PRIMARY KEY,
  visto_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eventos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tipo TEXT NOT NULL,
  dados TEXT NOT NULL,
  criado_em TEXT NOT NULL
);
"""

ALLOWED: dict[State, set[State]] = {
    State.descoberto: {State.baixando, State.baixado, State.erro},
    State.baixando: {State.baixado, State.erro, State.descoberto},
    State.baixado: {State.editando, State.erro},
    State.editando: {State.editado, State.revisar, State.erro, State.baixado},
    State.revisar: {State.editado, State.erro, State.baixado},
    State.editado: {State.publicando, State.erro, State.baixado},
    State.publicando: {State.publicado, State.erro, State.editado},
    State.publicado: set(),
    State.erro: {State.descoberto, State.baixado, State.editado},
}

# Estado em que cada etapa trabalha: usado para gravar onde o erro aconteceu
STAGE_OF = {
    State.descoberto: "captacao", State.baixando: "captacao",
    State.baixado: "edicao", State.editando: "edicao", State.revisar: "edicao",
    State.editado: "publicacao", State.publicando: "publicacao",
}

COLUMNS = set(VideoRow.model_fields) - {"id"}


class InvalidTransition(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")]
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()


async def open_db(path: Path) -> aiosqlite.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.executescript(SCHEMA)
    await db.commit()
    return db


def _row(r: aiosqlite.Row | None) -> VideoRow | None:
    return VideoRow(**dict(r)) if r is not None else None


async def get_video(db: aiosqlite.Connection, video_id: int) -> VideoRow | None:
    async with db.execute("SELECT * FROM videos WHERE id = ?", (video_id,)) as cur:
        return _row(await cur.fetchone())


async def insert_video(db: aiosqlite.Connection, channel: str, url: str, title: str,
                       source: str, *, state: State = State.descoberto, **fields) -> int | None:
    h = url_hash(url)
    ts = now_iso()
    cur = await db.execute("INSERT OR IGNORE INTO vistos (hash, visto_em) VALUES (?, ?)", (h, ts))
    if cur.rowcount == 0:
        return None
    cur = await db.execute(
        "INSERT INTO videos (canal, url_origem, titulo_origem, fonte, estado, criado_em, atualizado_em)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (channel, url, title, source, state.value, ts, ts),
    )
    vid = cur.lastrowid
    await db.commit()
    if fields:
        await update_fields(db, vid, **fields)
    return vid


async def transition(db: aiosqlite.Connection, video_id: int, to: State, *, error: str | None = None) -> None:
    row = await get_video(db, video_id)
    if row is None:
        raise InvalidTransition(f"vídeo {video_id} não existe")
    if to not in ALLOWED[row.estado]:
        raise InvalidTransition(f"{row.estado.value} → {to.value} não é permitido")
    sets = {"estado": to.value, "atualizado_em": now_iso()}
    if to == State.erro:
        sets["erro"] = error
        sets["etapa_erro"] = STAGE_OF.get(row.estado)
    elif error is not None:
        sets["erro"] = error
    if to in (State.descoberto, State.baixado, State.editado) and row.estado == State.erro:
        sets["erro"] = None
        sets["etapa_progresso"] = 0
    cols = ", ".join(f"{k} = ?" for k in sets)
    await db.execute(f"UPDATE videos SET {cols} WHERE id = ?", (*sets.values(), video_id))
    await db.commit()


async def claim(db: aiosqlite.Connection, from_state: State, to_state: State,
                channel: str | None = None, channels: list[str] | None = None,
                video_id: int | None = None) -> VideoRow | None:
    where = "estado = ?"
    params: list = [from_state.value]
    if video_id is not None:
        where += " AND id = ?"
        params.append(video_id)
    if channel is not None:
        where += " AND canal = ?"
        params.append(channel)
    if channels is not None:
        if not channels:
            return None
        where += f" AND canal IN ({','.join('?' * len(channels))})"
        params.extend(channels)
    sql = (f"UPDATE videos SET estado = ?, etapa_progresso = 0, atualizado_em = ? "
           f"WHERE id = (SELECT id FROM videos WHERE {where} ORDER BY id LIMIT 1) RETURNING *")
    async with db.execute(sql, (to_state.value, now_iso(), *params)) as cur:
        r = await cur.fetchone()
    await db.commit()
    return _row(r)


async def set_progress(db: aiosqlite.Connection, video_id: int, pct: float) -> None:
    await db.execute("UPDATE videos SET etapa_progresso = ? WHERE id = ?", (max(0.0, min(100.0, pct)), video_id))
    await db.commit()


async def update_fields(db: aiosqlite.Connection, video_id: int, **fields) -> None:
    bad = set(fields) - COLUMNS
    if bad:
        raise ValueError(f"colunas desconhecidas: {sorted(bad)}")
    if not fields:
        return
    fields["atualizado_em"] = now_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(f"UPDATE videos SET {cols} WHERE id = ?", (*fields.values(), video_id))
    await db.commit()
