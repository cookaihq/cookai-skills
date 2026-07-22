from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Connection:
    access_key_id: str
    secret_access_key: str
    bucket: str
    endpoint: str
    region: str
    provider: str = "custom"
    addressing: str = "path"
    session_token: str = ""
    public_base_url: str = ""
    prefix: str = ""
    max_bytes: int = 104857600
    presign_expires: int = 3600


def mask_access_key(value: str) -> str:
    return "****" if len(value) <= 8 else value[:4] + "****" + value[-4:]
