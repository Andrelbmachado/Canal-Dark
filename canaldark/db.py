"""SQLite: schema, deduplicação e máquina de estados dos vídeos."""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from enum import Enum
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
  url_midia TEXT,
  autor TEXT NOT NULL DEFAULT '',
  fonte TEXT NOT NULL DEFAULT '',
  fonte_autorizada INTEGER NOT NULL DEFAULT 0,
  tom TEXT NOT NULL DEFAULT 'serio',
  exige_cc INTEGER NOT NULL DEFAULT 0,
  estado TEXT NOT NULL,
  etapa_progresso REAL NOT NULL DEFAULT 0,
  erro TEXT,
  fila_erro TEXT,
  nota REAL NOT NULL DEFAULT 0,
  duracao REAL,
  estilo TEXT,
  manchete TEXT,
  caminho_local TEXT,
  caminho_capa TEXT,
  caminho_final TEXT,
  remoto_bruto TEXT,
  remoto_final TEXT,
  pacote INTEGER,
  destinos TEXT,
  criado_em TEXT NOT NULL,
  atualizado_em TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_videos_estado ON videos(estado, id);
CREATE INDEX IF NOT EXISTS idx_videos_canal ON videos(canal, estado);
CREATE TABLE IF NOT EXISTS publicacoes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id INTEGER NOT NULL REFERENCES videos(id),
  plataforma TEXT NOT NULL,
  conta TEXT,
  id_externo TEXT,
  url TEXT,
  status TEXT NOT NULL DEFAULT 'publicado',
  motivo TEXT,
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

S = State

# Estado "trabalhando" → fila de onde ele saiu (para onde volta numa queda)
WORKING: dict[State, State] = {S.baixando: S.descoberto, S.enviando: S.baixado, S.descendo: S.separado,
                               S.editando: S.pronto, S.publicando: S.editado, S.arquivando: S.publicado}

ALLOWED: dict[State, set[State]] = {
    S.descoberto: {S.baixando, S.erro, S.descartado},
    S.baixando: {S.baixado, S.descoberto, S.erro},
    S.baixado: {S.enviando, S.erro, S.descartado},
    S.enviando: {S.no_drive, S.baixado, S.erro},
    S.no_drive: {S.separado, S.descartado},
    S.separado: {S.descendo, S.erro, S.descartado},
    S.descendo: {S.pronto, S.separado, S.erro},
    S.pronto: {S.editando, S.descartado},
    S.editando: {S.editado, S.revisar, S.pronto, S.erro},
    S.revisar: {S.editado, S.pronto, S.descartado},
    S.editado: {S.publicando, S.pronto, S.descartado},
    S.publicando: {S.publicado, S.editado, S.erro},
    S.publicado: {S.arquivando},
    S.arquivando: {S.arquivado, S.publicado, S.erro},
    S.erro: {S.descoberto, S.baixado, S.separado, S.pronto, S.editado, S.publicado, S.descartado},
    S.arquivado: set(),
    S.descartado: set(),
}

TAB_STATES = {"captacao": (S.descoberto, S.baixando, S.baixado, S.enviando, S.no_drive),
              "edicao": (S.separado, S.descendo, S.pronto, S.editando, S.revisar, S.editado),
              "publicacao": (S.editado, S.publicando, S.publicado, S.arquivando, S.arquivado)}
TAB_OF_QUEUE = {S.descoberto: "captacao", S.baixado: "captacao", S.separado: "edicao", S.pronto: "edicao",
                S.editado: "publicacao", S.publicado: "publicacao"}
# Vídeos que ocupam o pacote local do canal (erro e descartado não seguram o próximo pacote)
PACK_STATES = (S.separado, S.descendo, S.pronto, S.editando, S.revisar, S.editado, S.publicando, S.publicado,
               S.arquivando)
# Vídeos que já têm o estilo da edição: base da regra de não repetir 3 vezes
STYLED_STATES = (S.revisar, S.editado, S.publicando, S.publicado, S.arquivando, S.arquivado)

COLUMNS = set(VideoRow.model_fields) - {"id"}
_INSERT_FIXED = {"canal", "url_origem", "titulo_origem"}
_ORDER_TERM = re.compile(r"\s*(\w+)(\s+(?:ASC|DESC))?\s*", re.I)


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


def _sql(value):
    """Converte o valor de um campo para o SQLite: State vira texto, Path vira str, bool vira 0/1."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    if value is None or isinstance(value, (str, int, float, bytes)):
        return value
    raise TypeError(f"valor não suportado no banco: {type(value).__name__}")


def _columns(fields: dict, allowed: set[str]) -> dict:
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"colunas desconhecidas: {sorted(bad)}")
    return {k: _sql(v) for k, v in fields.items()}


def _order_by(order: str) -> str:
    terms = []
    for term in order.split(","):
        m = _ORDER_TERM.fullmatch(term)
        if not m or m[1] not in VideoRow.model_fields:
            raise ValueError(f"ordem inválida: {order!r}")
        terms.append(term.strip())
    return ", ".join(terms)


async def get_video(db: aiosqlite.Connection, video_id: int) -> VideoRow | None:
    async with db.execute("SELECT * FROM videos WHERE id = ?", (video_id,)) as cur:
        return _row(await cur.fetchone())


async def seen(db: aiosqlite.Connection, url: str) -> bool:
    async with db.execute("SELECT 1 FROM vistos WHERE hash = ?", (url_hash(url),)) as cur:
        return await cur.fetchone() is not None


async def insert_video(db: aiosqlite.Connection, channel: str, url: str, title: str, **fields) -> int | None:
    """Grava um vídeo novo em `descoberto`; devolve None se o link já foi visto.

    `fields` aceita qualquer coluna de VideoRow (inclusive `estado`, para montar cenários nos testes)."""
    values = _columns(fields, COLUMNS - _INSERT_FIXED)
    h, ts = url_hash(url), now_iso()
    row = {"canal": channel, "url_origem": url, "titulo_origem": title, "estado": S.descoberto.value,
           "criado_em": ts, "atualizado_em": ts, **values}
    cur = await db.execute("INSERT OR IGNORE INTO vistos (hash, visto_em) VALUES (?, ?)", (h, ts))
    if cur.rowcount == 0:
        await db.commit()
        return None
    try:
        cur = await db.execute(f"INSERT INTO videos ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                               tuple(row.values()))
    except Exception:
        await db.execute("DELETE FROM vistos WHERE hash = ?", (h,))
        await db.commit()
        raise
    await db.commit()
    return cur.lastrowid


async def transition(db: aiosqlite.Connection, video_id: int, to: State, *, error: str | None = None) -> None:
    row = await get_video(db, video_id)
    if row is None:
        raise InvalidTransition(f"vídeo {video_id} não existe")
    current = row.estado
    if to not in ALLOWED[current]:
        raise InvalidTransition(f"{current.value} → {to.value} não é permitido")
    sets: dict = {"estado": to.value, "atualizado_em": now_iso()}
    if current == S.erro:
        sets.update(erro=None, fila_erro=None, etapa_progresso=0)
    if to == S.erro:
        sets["erro"] = error
        sets["fila_erro"] = WORKING.get(current, current).value
    elif error is not None:
        sets["erro"] = error
    cols = ", ".join(f"{k} = ?" for k in sets)
    cur = await db.execute(f"UPDATE videos SET {cols} WHERE id = ? AND estado = ?",
                           (*sets.values(), video_id, current.value))
    await db.commit()
    if cur.rowcount == 0:
        raise InvalidTransition(f"vídeo {video_id} saiu de {current.value} antes de ir para {to.value}")


async def claim(db: aiosqlite.Connection, from_state: State, to_state: State, *,
                channels: list[str] | None = None, video_id: int | None = None,
                order: str = "id") -> VideoRow | None:
    """Pega um vídeo de `from_state` e o passa para `to_state` numa só instrução (seguro entre trabalhadores)."""
    if to_state not in ALLOWED[from_state]:
        raise InvalidTransition(f"{from_state.value} → {to_state.value} não é permitido")
    order_sql = _order_by(order)
    where, params = ["estado = ?"], [from_state.value]
    if video_id is not None:
        where.append("id = ?")
        params.append(video_id)
    if channels is not None:
        if not channels:
            return None
        where.append(f"canal IN ({', '.join('?' * len(channels))})")
        params.extend(channels)
    sql = (f"UPDATE videos SET estado = ?, etapa_progresso = 0, atualizado_em = ? "
           f"WHERE id = (SELECT id FROM videos WHERE {' AND '.join(where)} ORDER BY {order_sql} LIMIT 1) "
           f"RETURNING *")
    async with db.execute(sql, (to_state.value, now_iso(), *params)) as cur:
        r = await cur.fetchone()
    await db.commit()
    return _row(r)


async def update_fields(db: aiosqlite.Connection, video_id: int, **fields) -> None:
    values = _columns(fields, COLUMNS)
    if not values:
        return
    values.setdefault("atualizado_em", now_iso())
    cols = ", ".join(f"{k} = ?" for k in values)
    await db.execute(f"UPDATE videos SET {cols} WHERE id = ?", (*values.values(), video_id))
    await db.commit()


async def set_progress(db: aiosqlite.Connection, video_id: int, pct: float) -> None:
    await db.execute("UPDATE videos SET etapa_progresso = ? WHERE id = ?", (max(0.0, min(100.0, pct)), video_id))
    await db.commit()


async def count_states(db: aiosqlite.Connection, channel: str, states: Iterable[State]) -> int:
    values = [State(s).value for s in states]
    if not values:
        return 0
    async with db.execute(f"SELECT COUNT(*) FROM videos WHERE canal = ? AND estado IN ({', '.join('?' * len(values))})",
                          (channel, *values)) as cur:
        return (await cur.fetchone())[0]


async def recent_styles(db: aiosqlite.Connection, channel: str, limit: int = 2) -> list[str]:
    """Estilo dos últimos vídeos já editados do canal, o mais recente primeiro."""
    marks = ", ".join("?" * len(STYLED_STATES))
    async with db.execute(
        f"SELECT estilo FROM videos WHERE canal = ? AND estilo IS NOT NULL AND estado IN ({marks}) "
        f"ORDER BY atualizado_em DESC, id DESC LIMIT ?",
        (channel, *(s.value for s in STYLED_STATES), limit),
    ) as cur:
        return [r["estilo"] for r in await cur.fetchall()]


async def recover(db: aiosqlite.Connection) -> int:
    """Depois de uma queda, devolve cada vídeo "trabalhando" para a sua fila e zera o progresso."""
    total, ts = 0, now_iso()
    for working, queue in WORKING.items():
        cur = await db.execute("UPDATE videos SET estado = ?, etapa_progresso = 0, atualizado_em = ? WHERE estado = ?",
                               (queue.value, ts, working.value))
        total += cur.rowcount
    await db.commit()
    return total
