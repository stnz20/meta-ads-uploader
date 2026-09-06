import os
import re
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise SystemExit(
            f"\n[config] Pflichtfeld '{key}' fehlt oder ist leer.\n"
            f"         Bitte in .env eintragen (siehe .env.example).\n"
        )
    return val


def mask_token(text: str) -> str:
    """Ersetzt Meta Access Tokens in Log-Ausgaben durch '***'."""
    # Meta tokens starten mit EAA gefolgt von langen alphanumerischen Strings
    masked = re.sub(r"(EAA[A-Za-z0-9]{8})[A-Za-z0-9]+", r"\1***", str(text))
    # Zusätzlich: Werte, die wie API-Secrets aussehen (32+ alphanum. Zeichen)
    masked = re.sub(r"([A-Za-z0-9]{8})[A-Za-z0-9]{24,}", r"\1***", masked)
    return masked


META_APP_ID = _require("META_APP_ID")
META_APP_SECRET = _require("META_APP_SECRET")
META_ACCESS_TOKEN = _require("META_ACCESS_TOKEN")
META_AD_ACCOUNT_ID = _require("META_AD_ACCOUNT_ID")
META_PAGE_ID = _require("META_PAGE_ID")

_sa_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")
if not os.path.isabs(_sa_file):
    _sa_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), _sa_file)
GOOGLE_SERVICE_ACCOUNT_FILE = _sa_file
GOOGLE_SHEET_ID = _require("GOOGLE_SHEET_ID")

ADSETS_TAB = "ad_sets"
ADS_TAB = "ads"
LOG_TAB = "log"

# ABO-Testing-Kampagne — Basis für den "best-performenden Referenz je Produkt"-Vorschlag
# im Upload-Assistant (meiste Käufe pro Produkt-Ad-Set im gewählten Zeitfenster).
ABO_TESTING_CAMPAIGN_ID = os.environ.get(
    "ABO_TESTING_CAMPAIGN_ID", "120240969369520641"
).strip()

# Sekunden Pause zwischen Meta API-Calls (Rate Limit Schutz)
API_CALL_DELAY = 0.5

# Ad Namer
ADS_DRIVE_FOLDER_ID = _require("ADS_DRIVE_FOLDER_ID")  # UGC-Ablage (Default-Ziel)
NAMING_SHEET_GID = int(os.environ.get("NAMING_SHEET_GID", "279155632"))

# Static-Ablage — Ad Naming routet STA-Dateien hierher, alles andere nach
# ADS_DRIVE_FOLDER_ID (UGC). Leer → alles nach UGC (altes Verhalten).
# Fallback auf FRAMEIO_DEST_STATIC_FOLDER_ID für Abwärtskompatibilität der Secrets.
STATIC_DRIVE_FOLDER_ID = (
    os.environ.get("STATIC_DRIVE_FOLDER_ID")
    or os.environ.get("FRAMEIO_DEST_STATIC_FOLDER_ID")
    or ""
).strip()

# Frame.io-Import — Zwischen-Ordner: Rohdateien landen hier, danach benennt
# Ad Naming sie um und verschiebt in UGC/Static-Ablage (optional; ohne diesen
# Key ist nur die Import-Seite deaktiviert, der Rest der App läuft normal).
FRAMEIO_STAGING_FOLDER_ID = os.environ.get("FRAMEIO_STAGING_FOLDER_ID", "").strip()
