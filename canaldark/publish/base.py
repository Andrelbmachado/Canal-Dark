"""Shared publishing types and OAuth token storage in the system keyring."""
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import keyring

SERVICE = "canaldark"


class AuthExpired(Exception):
    """The account token is missing, expired or revoked: the user must reconnect."""


class RetryLater(Exception):
    """Quota or rate limit: keep the video and try again on a later slot."""


@dataclass
class PublishResult:
    id_externo: str
    url: str
    status: Literal["publicado", "rascunho"] = "publicado"   # rascunho: TikTok inbox, the user posts from the app


class Uploader(Protocol):
    async def upload(self, video: Path, title: str, description: str, tags: list[str],
                     on_progress: Callable[[float], Awaitable[None]], synthetic: bool = False) -> PublishResult: ...


def load_secret(platform: str, account: str) -> dict:
    raw = keyring.get_password(SERVICE, f"{platform}:{account}")
    if not raw:
        raise AuthExpired(f"conta {platform}:{account} não conectada; rode `canaldark auth {platform} {account}`")
    return json.loads(raw)


def save_secret(platform: str, account: str, data: dict) -> None:
    keyring.set_password(SERVICE, f"{platform}:{account}", json.dumps(data))


def account_status(platform: str, account: str) -> Literal["conectada", "falta"]:
    """Reads only the keyring (no network): "conectada" when a token is stored for platform:account."""
    try:
        raw = keyring.get_password(SERVICE, f"{platform}:{account}")
    except Exception:  # no keyring backend or a locked keychain: the account cannot be used
        return "falta"
    return "conectada" if raw else "falta"
