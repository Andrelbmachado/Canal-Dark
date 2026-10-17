"""TikTok through the free Content Posting API: inbox drafts (before the app audit) or direct posts."""
import asyncio
import hashlib
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from canaldark.publish.base import AuthExpired, PublishResult, RetryLater, load_secret, save_secret

API = "https://open.tiktokapis.com/v2"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
SCOPES = "user.info.basic,video.upload,video.publish"
TIKTOK_DAILY_CAP = 15  # conservative: TikTok does not publish its real caps
CHUNK = 10 * 1024**2
MAX_SINGLE = 64 * 1024**2
REFRESH_MARGIN = 300  # refresh the access token when it expires in less than 5 min
POLL_EVERY = 5
POLL_TIMEOUT = 600
AUTH_TIMEOUT = 300
AUTH_CODES = {"access_token_invalid", "scope_not_authorized"}
# daily caps (posts, inbox drafts, app's active users) and the per-minute rate limit
RETRY_CODES = {"rate_limit_exceeded", "spam_risk_too_many_posts", "spam_risk_too_many_pending_share",
               "reached_active_user_cap"}
DONE = {"SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"}


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunks): one piece up to MAX_SINGLE, else CHUNK pieces and the last one takes the rest."""
    if size <= MAX_SINGLE:
        return size, 1
    return CHUNK, size // CHUNK


def pkce_challenge(verifier: str) -> str:
    """TikTok's desktop PKCE uses the hex SHA-256 of the verifier, not the RFC 7636 base64url."""
    return hashlib.sha256(verifier.encode()).hexdigest()


def build_auth_url(client_key: str, redirect_uri: str, state: str, code_challenge: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_key": client_key, "scope": SCOPES, "response_type": "code", "redirect_uri": redirect_uri,
        "state": state, "code_challenge": code_challenge, "code_challenge_method": "S256"})


def _json(r: httpx.Response) -> dict:
    try:
        data = r.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _token_secret(data: dict, client_key: str, client_secret: str, previous: dict | None = None) -> dict:
    previous = previous or {}
    return {**previous,
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or previous.get("refresh_token", ""),
            "expires_at": int(time.time()) + int(data.get("expires_in", 86400)),
            "open_id": data.get("open_id") or previous.get("open_id", ""),
            "client_key": client_key, "client_secret": client_secret}


def _wait_for_callback(port: int, url: str, open_browser) -> dict[str, str]:
    """Serves http://localhost:{port}/callback until TikTok redirects the browser back with the result."""
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parts = urlsplit(self.path)
            if parts.path.rstrip("/") != "/callback":
                self.send_error(404)
                return
            result.update({k: v[0] for k, v in parse_qs(parts.query).items()})
            text = ("Pronto! Pode fechar esta aba e voltar ao terminal." if "code" in result
                    else "A autorização do TikTok não deu certo. Volte ao terminal.")
            body = f"<!doctype html><meta charset='utf-8'><p>{text}</p>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep the terminal clean
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 0.5
    try:
        print(f"Abrindo o navegador para autorizar o TikTok. Se ele não abrir, acesse:\n{url}")
        open_browser(url)
        deadline = time.monotonic() + AUTH_TIMEOUT
        while not result:
            if time.monotonic() > deadline:
                raise TimeoutError(f"o TikTok não voltou para http://localhost:{port}/callback a tempo; "
                                   f"rode o comando de novo")
            server.handle_request()
    finally:
        server.server_close()
    return result


def authorize(account: str, client_key: str, client_secret: str, *, port: int = 8765,
              open_browser=webbrowser.open, client: httpx.Client | None = None) -> str:
    """OAuth with PKCE through a local callback; stores the tokens in the keyring and returns the open_id."""
    verifier = secrets.token_urlsafe(64)  # 86 unreserved characters (43 to 128 allowed)
    state = secrets.token_urlsafe(16)
    redirect_uri = f"http://localhost:{port}/callback"
    params = _wait_for_callback(port, build_auth_url(client_key, redirect_uri, state, pkce_challenge(verifier)),
                                open_browser)
    if params.get("state") != state:
        raise RuntimeError("o retorno do TikTok veio com outro state; rode o comando de novo")
    if "error" in params or "code" not in params:
        reason = params.get("error_description") or params.get("error") or "sem código de autorização"
        raise RuntimeError(f"o TikTok não autorizou: {reason}")
    http = client or httpx.Client(timeout=30)
    try:
        r = http.post(f"{API}/oauth/token/", data={
            "client_key": client_key, "client_secret": client_secret, "code": params["code"],
            "grant_type": "authorization_code", "redirect_uri": redirect_uri, "code_verifier": verifier})
    finally:
        if client is None:
            http.close()
    data = _json(r)
    if r.status_code != 200 or not data.get("access_token"):
        reason = data.get("error_description") or data.get("error") or r.text[:300]
        raise RuntimeError(f"o TikTok não trocou o código pelo token: {reason}")
    save_secret("tiktok", account, _token_secret(data, client_key, client_secret))
    return data.get("open_id", "")


async def refresh_if_needed(secret: dict, account: str, client: httpx.AsyncClient) -> dict:
    """Trades the refresh token for a new access token when the current one expires in less than 5 min."""
    if secret.get("expires_at", 0) - time.time() >= REFRESH_MARGIN:
        return secret
    r = await client.post(f"{API}/oauth/token/", data={
        "client_key": secret["client_key"], "client_secret": secret["client_secret"],
        "grant_type": "refresh_token", "refresh_token": secret["refresh_token"]})
    data = _json(r)
    if r.status_code >= 500:
        raise RuntimeError(f"o TikTok falhou ao renovar o token ({account}): HTTP {r.status_code}")
    if not data.get("access_token"):
        reason = data.get("error_description") or data.get("error") or r.text[:300]
        raise AuthExpired(f"o TikTok recusou renovar o token ({account}): {reason}")
    new = _token_secret(data, secret["client_key"], secret["client_secret"], secret)
    save_secret("tiktok", account, new)
    return new


def _data(r: httpx.Response, account: str) -> dict:
    """The `data` of a reply; TikTok answers some failures with HTTP 200, so error.code decides."""
    body = _json(r)
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = err.get("code") or ("ok" if r.status_code < 400 else "")
    if code == "ok" and r.status_code < 400:
        return body.get("data") or {}
    msg = err.get("message") or r.text[:300]
    if code in AUTH_CODES or r.status_code == 401:
        raise AuthExpired(f"token do TikTok ({account}) recusado ({code or r.status_code}): {msg}")
    if code in RETRY_CODES or r.status_code == 429:
        raise RetryLater(f"limite do TikTok atingido ({code or r.status_code}): {msg}")
    raise RuntimeError(f"TikTok respondeu {r.status_code} ({code or 'sem código'}): {msg}")


class TikTokUploader:
    """`rascunho` sends the video to the creator's TikTok inbox; `publicar` posts it directly (audited apps)."""

    def __init__(self, account: str, modo: Literal["rascunho", "publicar"] = "rascunho",
                 client: httpx.AsyncClient | None = None):
        self.account = account
        self.modo = modo
        self.client = client

    async def upload(self, video: Path, title, description, tags, on_progress, synthetic=False) -> PublishResult:
        client = self.client or httpx.AsyncClient(timeout=120)
        try:
            secret = await refresh_if_needed(load_secret("tiktok", self.account), self.account, client)
            headers = {"Authorization": f"Bearer {secret['access_token']}",
                       "Content-Type": "application/json; charset=UTF-8"}
            size = video.stat().st_size
            chunk, total = chunk_plan(size)
            source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": total}
            username = ""
            if self.modo == "publicar":
                info = _data(await client.post(f"{API}/post/publish/creator_info/query/", headers=headers),
                             self.account)
                options = info.get("privacy_level_options") or []
                if not options:
                    raise RuntimeError("o TikTok não informou os níveis de privacidade da conta")
                privacy = "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in options else options[0]
                username = info.get("creator_username") or ""
                post_info = {"title": description[:2200], "privacy_level": privacy, "brand_content_toggle": False}
                init = _data(await client.post(f"{API}/post/publish/video/init/", headers=headers,
                                               json={"post_info": post_info, "source_info": source}), self.account)
            else:
                init = _data(await client.post(f"{API}/post/publish/inbox/video/init/", headers=headers,
                                               json={"source_info": source}), self.account)
            publish_id = init["publish_id"]
            await on_progress(5)
            await self._send(client, init["upload_url"], video, size, chunk, total, on_progress)
            final = await self._wait(client, headers, publish_id)
            await on_progress(100)
        finally:
            if self.client is None:
                await client.aclose()
        if self.modo == "rascunho" or final == "SEND_TO_USER_INBOX":
            return PublishResult(id_externo=publish_id, url="", status="rascunho")
        return PublishResult(id_externo=publish_id, url=f"https://www.tiktok.com/@{username}" if username else "")

    async def _send(self, client, url: str, video: Path, size: int, chunk: int, total: int, on_progress) -> None:
        with video.open("rb") as f:
            for i in range(total):
                first = i * chunk
                last = size - 1 if i == total - 1 else first + chunk - 1
                r = await client.put(url, content=f.read(last - first + 1), headers={
                    "Content-Range": f"bytes {first}-{last}/{size}", "Content-Type": "video/mp4"})
                if r.status_code not in (200, 201, 206):
                    raise RuntimeError(f"o TikTok recusou o pedaço {i + 1} de {total}: HTTP {r.status_code} "
                                       f"{r.text[:200]}")
                await on_progress(5 + 75 * (i + 1) / total)

    async def _wait(self, client, headers: dict, publish_id: str) -> str:
        deadline = time.monotonic() + POLL_TIMEOUT
        while True:
            st = _data(await client.post(f"{API}/post/publish/status/fetch/", headers=headers,
                                         json={"publish_id": publish_id}), self.account)
            status = st.get("status")
            if status in DONE:
                return status
            if status == "FAILED":
                raise RuntimeError(f"o TikTok não aceitou o vídeo: {st.get('fail_reason') or 'motivo não informado'}")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"o TikTok demorou mais de {POLL_TIMEOUT // 60} min para processar o vídeo")
            await asyncio.sleep(POLL_EVERY)
