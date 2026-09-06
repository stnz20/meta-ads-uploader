"""
Einmaliges Hilfsskript: erzeugt den Google-OAuth-Refresh-Token eines Workspace-Nutzers für Drive-Uploads.

Vorbereitung (einmalig, Google Cloud Console — gleiches Projekt wie der Service Account):
  1. console.cloud.google.com → APIs & Services → OAuth consent screen
     → User type "Internal" (Workspace: keine Verifikation, Token läuft nicht ab)
  2. Credentials → Create Credentials → OAuth client ID → Typ "Desktop app"
     → Client-ID + Client-Secret in .env eintragen:
        GOOGLE_OAUTH_CLIENT_ID=...
        GOOGLE_OAUTH_CLIENT_SECRET=...

Dann lokal auf dem Mac ausführen (öffnet den Browser, Login mit dem Workspace-Nutzer):
  python3 mint_drive_refresh_token.py

Das ausgegebene GOOGLE_OAUTH_REFRESH_TOKEN in .env UND in die Streamlit-Cloud-Secrets
(Settings → Secrets) eintragen.
"""
from __future__ import annotations
import os
import sys

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main() -> None:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    load_dotenv()

    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        sys.exit(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET fehlen in .env.\n"
            "Anleitung siehe Docstring oben (Google Cloud Console → OAuth client, Desktop app)."
        )

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
    )
    # prompt="consent" erzwingt ein frisches Refresh Token, auch bei erneutem Lauf
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        sys.exit("Kein Refresh Token erhalten — bitte erneut ausführen.")

    print("\n✅ Erfolgreich. Diese Zeile in .env und Streamlit-Cloud-Secrets eintragen:\n")
    print(f'GOOGLE_OAUTH_REFRESH_TOKEN="{creds.refresh_token}"')


if __name__ == "__main__":
    main()
