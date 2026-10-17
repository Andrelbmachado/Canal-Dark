"""Leitura das fontes de um canal: YouTube, TikTok e Instagram pelo yt-dlp; Pexels e Pixabay pela API."""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from urllib.parse import quote_plus, urlsplit, urlunsplit

import httpx
import yt_dlp

from canaldark.models import Candidate, SourceSpec

Extractor = Callable[[str, dict], Awaitable[dict]]  # (url, opções do yt-dlp) → info com "entries"

CC_FILTER = "EgIwAQ%3D%3D"  # filtro "Creative Commons" da busca do YouTube
# O YouTube tirou a ordem "por data de envio" da busca e o yt-dlp atual não tem mais "ytsearchdate";
# o mais perto disso é filtrar por "esta semana" + "vídeos" (a nota já prefere os mais novos).
RECENT_FILTER = "EgQIAxAB"
CHANNEL_TABS = {"shorts", "videos", "featured", "streams", "live", "playlists", "community", "posts", "podcasts",
                "releases", "about"}
BOT_MARKERS = ("Sign in to confirm", "not a bot")
BOT_MESSAGE = ("o YouTube pediu para confirmar que não é um robô: defina cookies_navegador no canal (ex.: chrome) "
               "e tente de novo")
PEXELS_URL = "https://api.pexels.com/videos/search"
PIXABAY_URL = "https://pixabay.com/api/videos/"
TIMEOUT = 30.0
_YT_CHANNEL_ID = re.compile(r"UC[\w-]{22}")


class SourceError(Exception):
    """Uma fonte não pôde ser lida; a mensagem vai para o log da aba Captação."""


def is_bot_check(message: str) -> bool:
    return any(marker in message for marker in BOT_MARKERS)


def _channel_shorts(url: str) -> str:
    url = url.strip()
    if url.startswith("@"):
        url = "https://www.youtube.com/" + url
    elif "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    if segments and segments[-1].lower() in CHANNEL_TABS:  # link colado já numa aba do canal
        segments.pop()
    return urlunsplit((parts.scheme, parts.netloc, "/" + "/".join([*segments, "shorts"]), "", ""))


def listing_url(spec: SourceSpec, limit: int = 30) -> str:
    if spec.tipo == "youtube_canal":
        return _channel_shorts(spec.url)
    if spec.tipo == "youtube_busca":
        sp = CC_FILTER if spec.so_creative_commons else RECENT_FILTER
        return f"https://www.youtube.com/results?search_query={quote_plus(spec.busca.strip())}&sp={sp}"
    if spec.tipo == "pexels":
        return PEXELS_URL
    if spec.tipo == "pixabay":
        return PIXABAY_URL
    return spec.url.strip()


# ---- yt-dlp ------------------------------------------------------------------


def _extract_sync(url: str, opts: dict) -> dict:
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False) or {}


def ytdlp_extractor(cookies_browser: str | None = None) -> Extractor:
    """O extrator padrão: yt-dlp numa thread, com os cookies do navegador quando o canal pede."""
    async def extract(url: str, opts: dict) -> dict:
        if cookies_browser:
            opts = {**opts, "cookiesfrombrowser": (cookies_browser,)}
        return await asyncio.to_thread(_extract_sync, url, opts)
    return extract


def _author(entry: dict, spec: SourceSpec) -> str:
    uploader_id = str(entry.get("uploader_id") or "").strip()
    # o TikTok e o Instagram põem um número em uploader_id; isso não serve de crédito
    if uploader_id and not uploader_id.isdigit() and not _YT_CHANNEL_ID.fullmatch(uploader_id):
        return uploader_id if uploader_id.startswith("@") else "@" + uploader_id
    handle_field = {"tiktok_perfil": "uploader", "instagram_perfil": "channel"}.get(spec.tipo)
    handle = str(entry.get(handle_field) or "").strip() if handle_field else ""
    if handle:
        return "@" + handle.lstrip("@")
    for key in ("channel", "uploader"):
        name = str(entry.get(key) or "").strip()
        if name:
            return name
    return spec.rotulo()


def _published(entry: dict) -> datetime | None:
    ts = entry.get("timestamp")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    day = entry.get("upload_date")
    if isinstance(day, str):
        try:
            return datetime.strptime(day, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _thumbnail(entry: dict) -> str | None:
    thumbs = [t for t in entry.get("thumbnails") or [] if isinstance(t, dict) and t.get("url")]
    return thumbs[-1]["url"] if thumbs else entry.get("thumbnail")


def candidate_from_entry(entry: dict, spec: SourceSpec) -> Candidate | None:
    """Converte uma entrada "flat" do yt-dlp num candidato; None para lives, canais e entradas incompletas."""
    url = entry.get("url") or entry.get("webpage_url")
    title = (entry.get("title") or "").strip()
    if not url or not title or entry.get("live_status") in ("is_live", "is_upcoming"):
        return None
    if entry.get("ie_key") == "YoutubeTab":  # canal ou playlist no meio dos resultados da busca
        return None
    views = entry.get("view_count")
    duration = entry.get("duration")
    return Candidate(
        url=url, titulo=title, autor=_author(entry, spec), fonte=spec.rotulo(),
        duracao=float(duration) if isinstance(duration, (int, float)) else None,
        views=int(views) if isinstance(views, (int, float)) else None,
        publicado_em=_published(entry), licenca=entry.get("license"), capa=_thumbnail(entry),
    )


def _short(message: str, limit: int = 300) -> str:
    message = message.removeprefix("ERROR: ").strip()
    return message if len(message) <= limit else message[: limit - 1] + "…"


async def _list_ytdlp(spec: SourceSpec, extract: Extractor, limit: int) -> list[Candidate]:
    opts = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "playlistend": limit,
        # sem isto os resultados da busca vêm sem data e a nota perde "views por hora" e o bônus de 48 h
        "extractor_args": {"youtubetab": {"approximate_date": [""]}},
    }
    try:
        try:
            info = await extract(listing_url(spec, limit), opts)
        except Exception as e:
            if spec.tipo != "youtube_busca" or spec.so_creative_commons or is_bot_check(str(e)):
                raise
            # se o YouTube recusar o filtro de data, a busca simples (por relevância) ainda traz vídeos
            info = await extract(f"ytsearch{limit}:{spec.busca.strip()}", opts)
    except SourceError:
        raise
    except Exception as e:
        if is_bot_check(str(e)):
            raise SourceError(BOT_MESSAGE) from e
        raise SourceError(f"yt-dlp: {_short(str(e))}") from e
    found = []
    for entry in (info or {}).get("entries") or []:
        if isinstance(entry, dict) and (c := candidate_from_entry(entry, spec)):
            found.append(c)
    return found[:limit]


# ---- Pexels e Pixabay -----------------------------------------------------------


def _key(keys: dict[str, str] | None, service: str, name: str) -> str:
    key = (keys or {}).get(service)
    if not key:
        raise SourceError(f"chave do {name} não configurada: rode `canaldark auth {service}`")
    return key


async def _get_json(client: httpx.AsyncClient, name: str, service: str, url: str, **kwargs) -> dict:
    """GET com erros em português que nunca repetem a URL (a do Pixabay leva a chave)."""
    try:
        resp = await client.get(url, timeout=TIMEOUT, follow_redirects=True, **kwargs)
    except httpx.HTTPError as e:
        raise SourceError(f"sem conexão com o {name} ({type(e).__name__})") from None
    if resp.status_code in (401, 403):
        raise SourceError(f"o {name} recusou a chave (HTTP {resp.status_code}): confira com "
                          f"`canaldark auth {service}`")
    if resp.status_code != 200:
        raise SourceError(f"o {name} respondeu HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise SourceError(f"resposta inválida do {name}") from None
    return data if isinstance(data, dict) else {}


def _portrait(width, height) -> bool:
    return isinstance(width, int) and isinstance(height, int) and height > width


async def _list_pexels(spec: SourceSpec, client: httpx.AsyncClient, key: str, limit: int) -> list[Candidate]:
    params = {"query": spec.busca.strip(), "orientation": "portrait", "per_page": min(limit, 80)}
    data = await _get_json(client, "Pexels", "pexels", listing_url(spec), params=params,
                           headers={"Authorization": key})
    found = []
    for video in data.get("videos") or []:
        files = [f for f in video.get("video_files") or []
                 if f.get("link") and _portrait(f.get("width"), f.get("height"))]
        if not video.get("url") or not files:
            continue
        best = min(files, key=lambda f: abs(f["height"] - 1920))  # o arquivo em pé mais perto de 1080×1920
        user = (video.get("user") or {}).get("name") or "autor desconhecido"
        duration = video.get("duration")
        found.append(Candidate(
            url=video["url"], titulo="", autor=f"{user} · Pexels", fonte=spec.rotulo(),
            duracao=float(duration) if isinstance(duration, (int, float)) else None,
            licenca="Pexels", direto=best["link"], capa=video.get("image"),
        ))
    return found[:limit]


async def _list_pixabay(spec: SourceSpec, client: httpx.AsyncClient, key: str, limit: int) -> list[Candidate]:
    params = {"key": key, "q": spec.busca.strip(), "per_page": max(3, min(limit, 200)), "safesearch": "true"}
    data = await _get_json(client, "Pixabay", "pixabay", listing_url(spec), params=params)
    found = []
    for hit in data.get("hits") or []:
        videos = hit.get("videos") or {}
        large, medium = videos.get("large") or {}, videos.get("medium") or {}
        chosen = large if large.get("url") else medium
        # a API do Pixabay não filtra orientação: só entram arquivos em pé
        if not hit.get("pageURL") or not chosen.get("url") or not _portrait(chosen.get("width"), chosen.get("height")):
            continue
        views, duration = hit.get("views"), hit.get("duration")
        found.append(Candidate(
            url=hit["pageURL"], titulo="", autor=f"{hit.get('user') or 'autor desconhecido'} · Pixabay",
            fonte=spec.rotulo(), duracao=float(duration) if isinstance(duration, (int, float)) else None,
            views=int(views) if isinstance(views, (int, float)) else None, licenca="Pixabay",
            direto=chosen["url"], capa=large.get("thumbnail") or chosen.get("thumbnail") or None,
        ))
    return found[:limit]


async def list_candidates(spec: SourceSpec, *, client: httpx.AsyncClient, extract: Extractor | None = None,
                          keys: dict[str, str] | None = None, limit: int = 30) -> list[Candidate]:
    """Os vídeos mais recentes de uma fonte; falha com SourceError (em português)."""
    if spec.tipo == "pexels":
        return await _list_pexels(spec, client, _key(keys, "pexels", "Pexels"), limit)
    if spec.tipo == "pixabay":
        return await _list_pixabay(spec, client, _key(keys, "pixabay", "Pixabay"), limit)
    return await _list_ytdlp(spec, extract or ytdlp_extractor(), limit)
