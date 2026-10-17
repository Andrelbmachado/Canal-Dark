"""Reels do Facebook pela Graph API: envio em três fases, status, erros e `authorize` (sem rede)."""
from urllib.parse import parse_qs

import httpx
import keyring
import pytest
from keyring.backend import KeyringBackend

from canaldark.publish import facebook as fb
from canaldark.publish.base import AuthExpired, RetryLater, load_secret, save_secret

GRAPH = "https://graph.facebook.com/v23.0"
RUPLOAD = "https://rupload.facebook.com/video-upload/v23.0"


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


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


def base_url(request: httpx.Request) -> str:
    return str(request.url.copy_with(query=None))


def graph_router(statuses, seen, start_response=None):
    """Responde como a Graph API: start → video_id, rupload → success, finish → success, status em sequência."""
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = base_url(request)
        if request.method == "POST" and url == f"{GRAPH}/123/video_reels":
            if form(request)["upload_phase"] == "start":
                return start_response or httpx.Response(200, json={"video_id": "V9",
                                                                   "upload_url": "https://rupload.facebook.com/video-upload/V9"})
            return httpx.Response(200, json={"success": True})
        if request.method == "POST" and url == f"{RUPLOAD}/V9":
            return httpx.Response(200, json={"success": True})
        if request.method == "GET" and url == f"{GRAPH}/V9":
            return httpx.Response(200, json={"status": next(statuses), "id": "V9"})
        return httpx.Response(404, json={"error": {"message": f"rota inesperada: {request.method} {url}"}})
    return handler


async def _upload(handler, video, progress=None):
    async def on_progress(p):
        if progress is not None:
            progress.append(p)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await fb.FacebookUploader("teste", client=client).upload(video, "Título", "Legenda #tag", ["#tag"],
                                                                         on_progress)


@pytest.fixture
def video(tmp_path, vault, monkeypatch):
    monkeypatch.setattr(fb, "POLL_EVERY", 0)
    save_secret("facebook", "teste", {"token": "PTK", "page_id": "123"})
    f = tmp_path / "5.mp4"
    f.write_bytes(b"abc")
    return f


async def test_three_phase_upload(video):
    statuses = iter([{"video_status": "processing", "publishing_phase": {"status": "not_started"}},
                     {"video_status": "ready", "publishing_phase": {"status": "complete"}}])
    seen, progress = [], []
    res = await _upload(graph_router(statuses, seen), video, progress)

    assert (res.id_externo, res.url, res.status) == ("V9", "https://www.facebook.com/reel/V9", "publicado")
    start, up, finish, *polls = seen
    assert form(start) == {"upload_phase": "start", "access_token": "PTK"}
    assert up.headers["Authorization"] == "OAuth PTK"
    assert (up.headers["offset"], up.headers["file_size"], up.content) == ("0", "3", b"abc")
    assert form(finish) == {"upload_phase": "finish", "video_id": "V9", "video_state": "PUBLISHED",
                            "description": "Legenda #tag", "access_token": "PTK"}
    assert len(polls) == 2
    assert all((p.url.params["fields"], p.url.params["access_token"]) == ("status", "PTK") for p in polls)
    assert progress[-1] == 100


async def test_publishing_phase_complete_also_finishes(video):
    statuses = iter([{"video_status": "processing", "publishing_phase": {"status": "complete"}}])
    seen = []
    res = await _upload(graph_router(statuses, seen), video)
    assert res.id_externo == "V9" and len([r for r in seen if r.method == "GET"]) == 1


@pytest.mark.parametrize("response", [
    httpx.Response(400, json={"error": {"code": 190, "message": "Error validating access token"}}),
    httpx.Response(401, json={"error": {"message": "Unauthorized"}}),
])
async def test_expired_token_raises_auth_expired(video, response):
    seen = []
    with pytest.raises(AuthExpired):
        await _upload(graph_router(iter([]), seen, start_response=response), video)
    assert len(seen) == 1  # parou na primeira chamada; nada foi enviado


@pytest.mark.parametrize("code", [4, 17, 32, 613])
async def test_rate_limit_raises_retry_later(video, code):
    response = httpx.Response(400, json={"error": {"code": code, "message": "Application request limit reached"}})
    with pytest.raises(RetryLater):
        await _upload(graph_router(iter([]), [], start_response=response), video)


async def test_other_graph_error_is_a_plain_failure(video):
    response = httpx.Response(500, json={"error": {"code": 2, "message": "Service temporarily unavailable"}})
    with pytest.raises(RuntimeError, match="Service temporarily unavailable") as info:
        await _upload(graph_router(iter([]), [], start_response=response), video)
    assert not isinstance(info.value, (AuthExpired, RetryLater))


async def test_processing_error_stops_polling(video):
    statuses = iter([{"video_status": "error",
                      "processing_phase": {"status": "error", "error": {"message": "Vídeo longo demais"}}}])
    with pytest.raises(RuntimeError, match="Vídeo longo demais"):
        await _upload(graph_router(statuses, []), video)


async def test_polling_gives_up_after_timeout(video, monkeypatch):
    monkeypatch.setattr(fb, "POLL_TIMEOUT", 0)

    def processing():
        while True:
            yield {"video_status": "processing", "publishing_phase": {"status": "not_started"}}

    with pytest.raises(RuntimeError, match="demorou"):
        await _upload(graph_router(processing(), []), video)


def test_authorize_saves_page_token(vault):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"access_token": "PAGETK", "name": "Canal Teste", "id": "123"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        name = fb.authorize("teste", "USERTK", "123", client=client)

    assert name == "Canal Teste"
    assert load_secret("facebook", "teste") == {"token": "PAGETK", "page_id": "123"}
    (req,) = seen
    assert (req.method, base_url(req)) == ("GET", f"{GRAPH}/123")
    assert (req.url.params["fields"], req.url.params["access_token"]) == ("access_token,name", "USERTK")


@pytest.mark.parametrize("response", [
    httpx.Response(400, json={"error": {"code": 190, "message": "Invalid OAuth access token"}}),
    httpx.Response(200, json={"name": "Página de outra pessoa", "id": "123"}),   # sem token: não é admin da Página
])
def test_authorize_rejected_saves_nothing(vault, response):
    with httpx.Client(transport=httpx.MockTransport(lambda request: response)) as client:
        with pytest.raises(AuthExpired):
            fb.authorize("teste", "USERTK", "123", client=client)
    assert vault == {}
