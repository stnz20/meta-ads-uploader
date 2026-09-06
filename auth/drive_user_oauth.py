"""
Shared helper: Google-Drive-Zugriff mit dem OAuth eines Workspace-Nutzers (Refresh Token).

Warum: Der Service Account hat 0 My-Drive-Quota — neue Dateien in My-Drive-Ordner
hochladen schlägt mit storageQuotaExceeded fehl. Uploads (z. B. Frame.io-Import)
laufen deshalb über die Quota eines Workspace-Nutzers.

Benötigte Env-Vars / Secrets (siehe .env.example):
  GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_REFRESH_TOKEN
Refresh Token einmalig erzeugen: python mint_drive_refresh_token.py
"""
from __future__ import annotations
import os

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
_TOKEN_URI = "https://oauth2.googleapis.com/token"

_OAUTH_VARS = (
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_REFRESH_TOKEN",
)


def user_oauth_configured() -> bool:
    """True, wenn alle drei OAuth-Secrets gesetzt sind."""
    return all(os.environ.get(v, "").strip() for v in _OAUTH_VARS)


def missing_oauth_vars() -> list[str]:
    return [v for v in _OAUTH_VARS if not os.environ.get(v, "").strip()]


def get_user_drive_creds() -> Credentials:
    """Credentials aus dem Refresh Token — google-api-python-client refresht automatisch."""
    if not user_oauth_configured():
        raise RuntimeError(
            "Google-OAuth nicht konfiguriert — fehlende Secrets: "
            + ", ".join(missing_oauth_vars())
        )
    return Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"].strip(),
        token_uri=_TOKEN_URI,
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"].strip(),
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"].strip(),
        scopes=[DRIVE_SCOPE],
    )


def get_user_drive_service():
    """Drive-v3-Client, der als Workspace-Nutzer agiert (eigene Quota)."""
    return build("drive", "v3", credentials=get_user_drive_creds(), cache_discovery=False)
