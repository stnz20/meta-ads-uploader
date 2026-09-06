"""
Shared helper: returns Google service-account credentials.

Two modes:
  - Local:         reads from credentials.json (path in GOOGLE_SERVICE_ACCOUNT_FILE)
  - Streamlit Cloud: reads from GOOGLE_CREDENTIALS_JSON env var (full JSON string)
"""
from __future__ import annotations
import json
import os

from google.oauth2.service_account import Credentials


def get_service_account_creds(scopes: list[str]) -> Credentials:
    """Returns Credentials from JSON env var (cloud) or file (local)."""
    raw_json = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()
    if raw_json:
        info = json.loads(raw_json)
        return Credentials.from_service_account_info(info, scopes=scopes)

    # Local fallback — read path from env (default: credentials.json)
    sa_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")
    # Relativer Pfad → relativ zum Projekt-Root auflösen (nicht zum cwd)
    if not os.path.isabs(sa_file):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sa_file = os.path.join(project_root, sa_file)
    return Credentials.from_service_account_file(sa_file, scopes=scopes)
