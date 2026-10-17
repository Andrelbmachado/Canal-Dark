"""Instagram Reels through the free Instagram Graph API (business or creator account)."""
import asyncio
from pathlib import Path

import httpx

from canaldark.publish.base import AuthExpired, PublishResult, RetryLater, load_secret, save_secret

VERSION = "v23.0"
GRAPH = f"https://graph.facebook.com/{VERSION}"
RUPLOAD = f"https://rupload.facebook.com/ig-api-upload/{VERSION}"
IG_DAILY_CAP = 25
POLL_EVERY = 5
POLL_TIMEOUT = 600
RATE_LIMIT_CODES = {4, 17, 32, 613, 9004}


def authorize(account: str, token: str, ig_user_id: str) -> str:
    r = httpx.get(f"{GRAPH}/{ig_user_id}", params={"fields": "username", "access_token": token}, timeout=30)
    if r.status_code != 200:
        raise AuthExpired(f"o Instagram recusou o token: {r.text[:300]}")
    save_secret("instagram", account, {"token": token, "ig_user_id": ig_user_id})
    return r.json().get("username", ig_user_id)


def _raise_for(r: httpx.Response, account: str) -> dict:
    data = r.json() if r.content else {}
    err = data.get("error") if isinstance(data, dict) else None
    if r.status_code >= 400 or err:
        code = (err or {}).get("code")
        msg = (err or {}).get("message", r.text[:300])
        if code == 190 or r.status_code == 401:
            raise AuthExpired(f"token do Instagram ({account}) expirou: {msg}")
        if code in RATE_LIMIT_CODES:
            raise RetryLater(f"limite do Instagram atingido: {msg}")
        raise RuntimeError(f"Instagram respondeu {r.status_code}: {msg}")
    return data


class InstagramUploader:
    """Uploads the file directly (resumable upload). With a `tunnel`, sends a public URL instead."""

    def __init__(self, account: str, tunnel=None, client: httpx.AsyncClient | None = None):
        self.account = account
        self.tunnel = tunnel
        self.client = client

    async def upload(self, video: Path, title, description, tags, on_progress, synthetic=False) -> PublishResult:
        secret = load_secret("instagram", self.account)
        token, ig = secret["token"], secret["ig_user_id"]
        caption = description[:2200]
        client = self.client or httpx.AsyncClient(timeout=120)
        try:
            params = {"media_type": "REELS", "caption": caption, "share_to_feed": "true", "access_token": token}
            if self.tunnel is not None:
                base = await self.tunnel.start(video.parent)
                params["video_url"] = f"{base}/{video.name}"
                container = _raise_for(await client.post(f"{GRAPH}/{ig}/media", data=params), self.account)["id"]
            else:
                params["upload_type"] = "resumable"
                container = _raise_for(await client.post(f"{GRAPH}/{ig}/media", data=params), self.account)["id"]
                size = video.stat().st_size
                r = await client.post(f"{RUPLOAD}/{container}", content=video.read_bytes(), headers={
                    "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)})
                _raise_for(r, self.account)
            await on_progress(25)

            waited = 0
            while True:
                st = _raise_for(await client.get(f"{GRAPH}/{container}", params={
                    "fields": "status_code,status", "access_token": token}), self.account)
                if st.get("status_code") == "FINISHED":
                    break
                if st.get("status_code") in ("ERROR", "EXPIRED"):
                    raise RuntimeError(f"o Instagram não processou o vídeo: {st.get('status')}")
                if waited >= POLL_TIMEOUT:
                    raise RuntimeError("o Instagram demorou mais de 10 min para processar o vídeo")
                await asyncio.sleep(POLL_EVERY)
                waited += POLL_EVERY
                await on_progress(min(50, 25 + waited / POLL_TIMEOUT * 25))

            media = _raise_for(await client.post(f"{GRAPH}/{ig}/media_publish", data={
                "creation_id": container, "access_token": token}), self.account)["id"]
            link = _raise_for(await client.get(f"{GRAPH}/{media}", params={
                "fields": "permalink", "access_token": token}), self.account).get("permalink", "")
            await on_progress(100)
            return PublishResult(id_externo=media, url=link)
        finally:
            if self.client is None:
                await client.aclose()
