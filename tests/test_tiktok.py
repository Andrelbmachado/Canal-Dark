"""TikTok pela Content Posting API: pedaços, rascunho, post direto, token e OAuth com PKCE (sem rede)."""
import hashlib
import http.client
import json
import re
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import keyring
import pytest
from keyring.backend import KeyringBackend

from canaldark.publish import tiktok as tt
from canaldark.publish.base import AuthExpired, RetryLater, load_secret, save_secret

API = "https://open.tiktokapis.com/v2"
UPLOAD_URL = "https://open-upload.tiktokapis.com/video/?upload_id=67890&upload_token=Xza123"
MB = 1024**2


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        self.data.pop((service, username), None)


@pytest.fixture
def vault():
    """Keyring em memória: nenhum teste toca o chaveiro de verdade."""
    previous = keyring.get_keyring()
    memory = MemoryKeyring()
    keyring.set_keyring(memory)
    yield memory.data
    keyring.set_keyring(previous)


def secret(**over) -> dict:
    base = {"access_token": "ACT", "refresh_token": "RFT", "expires_at": time.time() + 3600, "open_id": "OID",
            "client_key": "CK", "client_secret": "CS"}
    return {**base, **over}


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def ok(data: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data, "error": {"code": "ok", "message": "", "log_id": "L1"}})


def api_error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"data": {}, "error": {"code": code, "message": f"erro {code}", "log_id": "L2"}})


TOKEN_REPLY = {"access_token": "ACT2", "refresh_token": "RFT2", "expires_in": 86400, "open_id": "OID",
               "refresh_expires_in": 31536000, "scope": "user.info.basic,video.upload,video.publish",
               "token_type": "Bearer"}


def router(seen: list, statuses, *, creator: dict | None = None, creator_response=None, init_response=None):
    """Responde como a API do TikTok; `statuses` dá, em ordem, o `data` de cada status/fetch."""
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url)
        if request.method == "POST" and url == f"{API}/oauth/token/":
            return httpx.Response(200, json=TOKEN_REPLY)
        if request.method == "POST" and url == f"{API}/post/publish/creator_info/query/":
            return creator_response or ok(creator or {})
        if request.method == "POST" and url in (f"{API}/post/publish/inbox/video/init/",
                                                f"{API}/post/publish/video/init/"):
            return init_response or ok({"publish_id": "PUB1", "upload_url": UPLOAD_URL})
        if request.method == "PUT" and url == UPLOAD_URL:
            first, last, size = map(int, re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",
                                                      request.headers["Content-Range"]).groups())
            return httpx.Response(201 if last == size - 1 else 206)
        if request.method == "POST" and url == f"{API}/post/publish/status/fetch/":
            data = next(statuses)
            return ok(data if isinstance(data, dict) else {"status": data})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": f"{request.method} {url}"}})
    return handler


async def _upload(handler, video, *, modo: str, description: str = "Legenda #tag"):
    async def on_progress(p):
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await tt.TikTokUploader("teste", modo=modo, client=client).upload(
            video, "Título", description, ["#tag"], on_progress)


@pytest.fixture
def video(tmp_path, vault, monkeypatch):
    monkeypatch.setattr(tt, "POLL_EVERY", 0)
    save_secret("tiktok", "teste", secret())
    f = tmp_path / "5.mp4"
    f.write_bytes(b"abc")
    return f


def test_chunk_plan():
    assert tt.chunk_plan(3 * MB) == (3 * MB, 1) and tt.chunk_plan(64 * MB) == (64 * MB, 1)
    assert tt.chunk_plan(100 * MB) == (10 * MB, 10) and tt.chunk_plan(105 * MB) == (10 * MB, 10)


@pytest.mark.parametrize("final", ["SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"])
async def test_inbox_upload_returns_rascunho(video, monkeypatch, final):
    monkeypatch.setattr(tt, "MAX_SINGLE", 8)  # força pedaços com um arquivo pequeno: 11 bytes em pedaços de 4
    monkeypatch.setattr(tt, "CHUNK", 4)
    video.write_bytes(b"0123456789A")
    seen = []
    res = await _upload(router(seen, iter(["PROCESSING_UPLOAD", final])), video, modo="rascunho")

    assert (res.id_externo, res.url, res.status) == ("PUB1", "", "rascunho")
    init, put1, put2, *polls = seen
    assert str(init.url) == f"{API}/post/publish/inbox/video/init/"
    assert init.headers["Authorization"] == "Bearer ACT"
    assert json.loads(init.content) == {"source_info": {"source": "FILE_UPLOAD", "video_size": 11, "chunk_size": 4,
                                                        "total_chunk_count": 2}}
    assert [(p.headers["Content-Range"], p.content) for p in (put1, put2)] == [("bytes 0-3/11", b"0123"),
                                                                              ("bytes 4-10/11", b"456789A")]
    assert put1.headers["Content-Type"] == put2.headers["Content-Type"] == "video/mp4"
    assert [json.loads(p.content) for p in polls] == [{"publish_id": "PUB1"}] * 2
    assert all(p.headers["Authorization"] == "Bearer ACT" for p in polls)


@pytest.mark.parametrize("options,expected", [
    (["SELF_ONLY"], "SELF_ONLY"),
    (["FOLLOWER_OF_CREATOR", "PUBLIC_TO_EVERYONE", "SELF_ONLY"], "PUBLIC_TO_EVERYONE"),
])
async def test_direct_post_uses_creator_privacy(video, options, expected):
    description = "Legenda #tag " + "x" * 3000
    seen = []
    creator = {"creator_username": "canal_teste", "creator_nickname": "Canal", "privacy_level_options": options}
    res = await _upload(router(seen, iter(["PROCESSING_UPLOAD", "PUBLISH_COMPLETE"]), creator=creator), video,
                        modo="publicar", description=description)

    assert (res.id_externo, res.url, res.status) == ("PUB1", "https://www.tiktok.com/@canal_teste", "publicado")
    query, init, put, *polls = seen
    assert str(query.url) == f"{API}/post/publish/creator_info/query/"
    assert str(init.url) == f"{API}/post/publish/video/init/"
    body = json.loads(init.content)
    assert body["post_info"]["privacy_level"] == expected
    title = body["post_info"]["title"]
    assert len(title) == 2200 and description.startswith(title)
    assert body["source_info"] == {"source": "FILE_UPLOAD", "video_size": 3, "chunk_size": 3, "total_chunk_count": 1}
    assert (put.headers["Content-Range"], put.content) == ("bytes 0-2/3", b"abc")
    assert len(polls) == 2


async def test_direct_post_without_username_has_empty_url(video):
    creator = {"privacy_level_options": ["SELF_ONLY"]}
    res = await _upload(router([], iter(["PUBLISH_COMPLETE"]), creator=creator), video, modo="publicar")
    assert (res.url, res.status) == ("", "publicado")


@pytest.mark.parametrize("status,code,exc", [
    (401, "access_token_invalid", AuthExpired),
    (401, "scope_not_authorized", AuthExpired),
    (429, "rate_limit_exceeded", RetryLater),
    (403, "spam_risk_too_many_posts", RetryLater),
    (403, "spam_risk_too_many_pending_share", RetryLater),   # caixa de entrada: no máximo 5 rascunhos em 24 h
])
async def test_api_errors_map_to_auth_and_retry(video, status, code, exc):
    seen = []
    with pytest.raises(exc):
        await _upload(router(seen, iter([]), init_response=api_error(status, code)), video, modo="rascunho")
    assert [r.method for r in seen] == ["POST"]  # nada foi enviado


async def test_other_api_error_is_a_plain_failure(video):
    with pytest.raises(RuntimeError, match="invalid_param") as info:
        await _upload(router([], iter([]), init_response=api_error(400, "invalid_param")), video, modo="rascunho")
    assert not isinstance(info.value, (AuthExpired, RetryLater))


async def test_error_inside_http_200_body_is_detected(video):
    # o creator_info responde 200 mesmo quando falha; vale o error.code do corpo
    seen = []
    with pytest.raises(RetryLater):
        await _upload(router(seen, iter([]), creator_response=api_error(200, "spam_risk_too_many_posts")), video,
                      modo="publicar")
    assert len(seen) == 1


async def test_failed_status_raises_with_reason(video):
    statuses = iter([{"status": "FAILED", "fail_reason": "file_format_check_failed"}])
    with pytest.raises(RuntimeError, match="file_format_check_failed"):
        await _upload(router([], statuses), video, modo="rascunho")


async def test_status_polling_gives_up_after_timeout(video, monkeypatch):
    monkeypatch.setattr(tt, "POLL_TIMEOUT", 0)

    def processing():
        while True:
            yield "PROCESSING_UPLOAD"

    with pytest.raises(RuntimeError, match="demorou"):
        await _upload(router([], processing()), video, modo="rascunho")


async def test_upload_refreshes_token_before_init(video):
    save_secret("tiktok", "teste", secret(expires_at=time.time() + 30))
    seen = []
    await _upload(router(seen, iter(["SEND_TO_USER_INBOX"])), video, modo="rascunho")
    refresh, init, *_ = seen
    assert str(refresh.url) == f"{API}/oauth/token/" and init.headers["Authorization"] == "Bearer ACT2"


async def test_refresh_expired_token(vault):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=TOKEN_REPLY)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        new = await tt.refresh_if_needed(secret(expires_at=time.time() + 60), "teste", client)

    (req,) = seen
    assert (req.method, str(req.url)) == ("POST", f"{API}/oauth/token/")
    assert form(req) == {"client_key": "CK", "client_secret": "CS", "grant_type": "refresh_token",
                         "refresh_token": "RFT"}
    assert (new["access_token"], new["refresh_token"], new["open_id"]) == ("ACT2", "RFT2", "OID")
    assert (new["client_key"], new["client_secret"]) == ("CK", "CS")
    assert new["expires_at"] == pytest.approx(time.time() + 86400, abs=5)
    assert load_secret("tiktok", "teste") == new


async def test_valid_token_is_not_refreshed(vault):
    def handler(request):
        raise AssertionError("não devia chamar a rede")

    current = secret(expires_at=time.time() + 600)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await tt.refresh_if_needed(current, "teste", client) == current
    assert vault == {}


async def test_refresh_rejected_raises_auth_expired(vault):
    reply = httpx.Response(400, json={"error": "invalid_grant", "error_description": "Refresh token is expired.",
                                      "log_id": "L3"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: reply)) as client:
        with pytest.raises(AuthExpired, match="Refresh token is expired"):
            await tt.refresh_if_needed(secret(expires_at=time.time() - 10), "teste", client)
    assert vault == {}


def test_auth_url_has_pkce():
    url = tt.build_auth_url("CK", "http://localhost:8765/callback", "ST4TE", "CHALLENGE")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://www.tiktok.com/v2/auth/authorize/"
    assert {k: v[0] for k, v in parse_qs(parts.query).items()} == {
        "client_key": "CK", "response_type": "code", "scope": "user.info.basic,video.upload,video.publish",
        "redirect_uri": "http://localhost:8765/callback", "state": "ST4TE", "code_challenge": "CHALLENGE",
        "code_challenge_method": "S256"}


def test_pkce_challenge_is_hex_sha256():
    # o TikTok (app de computador) pede o SHA-256 em hexadecimal, não em base64url
    assert tt.pkce_challenge("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _browser(port: int, query: str, opened: list):
    """Navegador falso: guarda a URL e chama o /callback local como o TikTok faria."""
    def open_browser(url: str) -> None:
        opened.append(url)
        state = parse_qs(urlsplit(url).query)["state"][0]

        def callback():
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/callback?" + query.format(state=state))
            conn.getresponse().read()
            conn.close()

        threading.Thread(target=callback, daemon=True).start()
    return open_browser


def test_authorize_exchanges_code_with_verifier(vault):
    port, opened, seen = _free_port(), [], []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={**TOKEN_REPLY, "access_token": "ACT", "refresh_token": "RFT"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = tt.authorize("teste", "CK", "CS", port=port, client=client,
                              open_browser=_browser(port, "code=C%2ADE&scopes=video.upload&state={state}", opened))

    assert result == "OID"
    auth = {k: v[0] for k, v in parse_qs(urlsplit(opened[0]).query).items()}
    assert auth["redirect_uri"] == f"http://localhost:{port}/callback"
    (req,) = seen
    body = form(req)
    assert str(req.url) == f"{API}/oauth/token/"
    assert {k: v for k, v in body.items() if k != "code_verifier"} == {
        "client_key": "CK", "client_secret": "CS", "code": "C*DE", "grant_type": "authorization_code",
        "redirect_uri": f"http://localhost:{port}/callback"}
    verifier = body["code_verifier"]
    assert 43 <= len(verifier) <= 128 and re.fullmatch(r"[A-Za-z0-9._~-]+", verifier)
    assert hashlib.sha256(verifier.encode()).hexdigest() == auth["code_challenge"]
    saved = load_secret("tiktok", "teste")
    assert {k: v for k, v in saved.items() if k != "expires_at"} == {
        "access_token": "ACT", "refresh_token": "RFT", "open_id": "OID", "client_key": "CK", "client_secret": "CS"}
    assert saved["expires_at"] == pytest.approx(time.time() + 86400, abs=5)


@pytest.mark.parametrize("query", [
    "code=C0DE&state=outro",                                                  # state que não é o nosso
    "error=access_denied&error_description=User%20denied&state={state}",      # o usuário recusou
])
def test_authorize_refuses_bad_callback(vault, query):
    port, seen = _free_port(), []
    with httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(500))) as client:
        with pytest.raises(RuntimeError):
            tt.authorize("teste", "CK", "CS", port=port, client=client, open_browser=_browser(port, query, []))
    assert seen == [] and vault == {}


def test_authorize_gives_up_without_callback(vault, monkeypatch):
    monkeypatch.setattr(tt, "AUTH_TIMEOUT", 0.2)
    with pytest.raises(TimeoutError):
        tt.authorize("teste", "CK", "CS", port=_free_port(), open_browser=lambda url: None)
    assert vault == {}
