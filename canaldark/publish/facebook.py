"""Facebook Page Reels through the free Graph API (Reels Publishing API, Page access token)."""
import asyncio
import time
from pathlib import Path

import httpx

from canaldark.publish.base import AuthExpired, PublishResult, RetryLater, load_secret, save_secret

VERSION = "v23.0"
GRAPH = f"https://graph.facebook.com/{VERSION}"
RUPLOAD = f"https://rupload.facebook.com/video-upload/{VERSION}"
FB_DAILY_CAP = 30  # Meta: 30 API-published reels per Page in a rolling 24 h
POLL_EVERY = 5
POLL_TIMEOUT = 600
RATE_LIMIT_CODES = {4, 17, 32, 613}


def authorize(account: str, user_token: str, page_id: str, client: httpx.Client | None = None) -> str:
    """Trades the user token for the Page token, stores it and returns the Page name."""
    http = client or httpx.Client(timeout=30)
    try:
        r = http.get(f"{GRAPH}/{page_id}", params={"fields": "access_token,name", "access_token": user_token})
    finally:
        if client is None:
            http.close()
    if r.status_code != 200:
        raise AuthExpired(f"o Facebook recusou o token: {r.text[:300]}")
    data = r.json()
    if not data.get("access_token"):
        raise AuthExpired("o Facebook não devolveu o token da Página: confira se a sua conta administra a Página "
                          "e se o token tem as permissões pages_manage_posts e pages_read_engagement")
    save_secret("facebook", account, {"token": data["access_token"], "page_id": page_id})
    return data.get("name") or page_id


def _check(r: httpx.Response, account: str) -> dict:
    try:
        data = r.json() if r.content else {}
    except ValueError:
        data = {}
    err = data.get("error") if isinstance(data, dict) else None
    if r.status_code >= 400 or err:
        code = (err or {}).get("code")
        msg = (err or {}).get("message") or r.text[:300]
        if code == 190 or r.status_code == 401:
            raise AuthExpired(f"token do Facebook ({account}) expirou: {msg}")
        if code in RATE_LIMIT_CODES:
            raise RetryLater(f"limite do Facebook atingido: {msg}")
        raise RuntimeError(f"Facebook respondeu {r.status_code}: {msg}")
    return data if isinstance(data, dict) else {}


def _failure(status: dict) -> str | None:
    """Facebook's own message when the reel failed to upload, process or publish; None while all is fine."""
    for phase in ("uploading_phase", "processing_phase", "publishing_phase"):
        info = status.get(phase) or {}
        if info.get("status") == "error":
            err = info.get("error") or info.get("errors") or {}
            if isinstance(err, list):
                err = err[0] if err else {}
            return (err.get("message") if isinstance(err, dict) else str(err)) or phase
    if status.get("video_status") in ("error", "upload_failed", "expired"):
        return status["video_status"]
    return None


class FacebookUploader:
    """Start an upload session, send the file to rupload, publish it and wait until the reel is ready."""

    def __init__(self, account: str, client: httpx.AsyncClient | None = None):
        self.account = account
        self.client = client

    async def upload(self, video: Path, title, description, tags, on_progress, synthetic=False) -> PublishResult:
        secret = load_secret("facebook", self.account)
        token, page = secret["token"], secret["page_id"]
        client = self.client or httpx.AsyncClient(timeout=120)
        try:
            start = _check(await client.post(f"{GRAPH}/{page}/video_reels", data={
                "upload_phase": "start", "access_token": token}), self.account)
            video_id = start["video_id"]
            await on_progress(5)
            size = video.stat().st_size
            _check(await client.post(f"{RUPLOAD}/{video_id}", content=video.read_bytes(), headers={
                "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)}), self.account)
            await on_progress(60)
            _check(await client.post(f"{GRAPH}/{page}/video_reels", data={
                "upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED",
                "description": description, "access_token": token}), self.account)
            await on_progress(70)
            await self._wait_ready(client, video_id, token)
            await on_progress(100)
            return PublishResult(id_externo=video_id, url=f"https://www.facebook.com/reel/{video_id}")
        finally:
            if self.client is None:
                await client.aclose()

    async def _wait_ready(self, client: httpx.AsyncClient, video_id: str, token: str) -> None:
        deadline = time.monotonic() + POLL_TIMEOUT
        while True:
            status = _check(await client.get(f"{GRAPH}/{video_id}", params={
                "fields": "status", "access_token": token}), self.account).get("status") or {}
            publishing = status.get("publishing_phase") or {}
            # the docs spell the finished phase both "complete" and "completed"
            if status.get("video_status") == "ready" or publishing.get("status") in ("complete", "completed"):
                return
            failure = _failure(status)
            if failure:
                raise RuntimeError(f"o Facebook não processou o vídeo: {failure}")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"o Facebook demorou mais de {POLL_TIMEOUT // 60} min para processar o vídeo")
            await asyncio.sleep(POLL_EVERY)
