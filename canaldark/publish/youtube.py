"""YouTube upload through the free YouTube Data API v3 (OAuth of the user's own account)."""
import asyncio
import json
from pathlib import Path

from canaldark.publish.base import AuthExpired, PublishResult, RetryLater, load_secret, save_secret

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CHUNK = 8 * 1024 * 1024
CATEGORY_GAMING, CATEGORY_NEWS = "20", "25"


def authorize(account: str, client_secret: Path) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES).run_local_server(port=0)
    save_secret("youtube", account, json.loads(creds.to_json()))


class YouTubeUploader:
    def __init__(self, account: str, category: str = CATEGORY_NEWS):
        self.account = account
        self.category = category

    def _service(self):
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_info(load_secret("youtube", self.account), SCOPES)
        if not creds.valid:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                raise AuthExpired(f"token do YouTube ({self.account}) expirou: {e}") from e
            save_secret("youtube", self.account, json.loads(creds.to_json()))
        return build("youtube", "v3", credentials=creds, cache_discovery=False)

    def _upload_sync(self, video: Path, body: dict, report) -> PublishResult:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        try:
            request = self._service().videos().insert(
                part="snippet,status", body=body,
                media_body=MediaFileUpload(str(video), mimetype="video/mp4", chunksize=CHUNK, resumable=True))
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    report(status.progress() * 99)
        except HttpError as e:
            reason = str(e)
            if e.resp.status == 401:
                raise AuthExpired(f"YouTube recusou o token ({self.account}): {reason}") from e
            if e.resp.status in (403, 429) and ("quota" in reason.lower() or "rateLimit" in reason):
                raise RetryLater(f"cota do YouTube esgotada por hoje: {reason}") from e
            raise
        vid = response["id"]
        return PublishResult(id_externo=vid, url=f"https://youtube.com/shorts/{vid}")

    async def upload(self, video, title, description, tags, on_progress, synthetic=False) -> PublishResult:
        loop = asyncio.get_running_loop()
        body = {
            "snippet": {"title": title[:100], "description": description[:5000],
                        "tags": [t.lstrip("#") for t in tags][:15], "categoryId": self.category,
                        "defaultLanguage": "pt-BR"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False,
                       "containsSyntheticMedia": synthetic},
        }

        def report(p: float) -> None:
            asyncio.run_coroutine_threadsafe(on_progress(p), loop)

        result = await asyncio.to_thread(self._upload_sync, video, body, report)
        await on_progress(100)
        return result
