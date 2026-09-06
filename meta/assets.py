from __future__ import annotations
import gc
import io
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import av
import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.service_account import Credentials
from PIL import Image

import config
from auth.credentials import get_service_account_creds

logger = logging.getLogger(__name__)

_NETWORK_ERRORS = (
    ConnectionResetError,
    ConnectionError,
    TimeoutError,
)


_RETRY_HTTP_CODES = {408, 429, 500, 502, 503, 504}

# Meta lehnt ein Video-Encoding als "nicht unterstützt" ab mit diesem Fehler.
# Tritt v.a. bei HEVC/H.265, 10-bit (yuv420p10le) oder ProRes-Exporten auf.
_UNSUPPORTED_VIDEO_CODE = 352
_UNSUPPORTED_VIDEO_SUBCODE = 1363024


class _UnsupportedVideoFormat(ValueError):
    """Meta lehnt das Video-Encoding ab (code 352 / subcode 1363024).

    Signalisiert dem Upload-Wrapper, dass sich ein Re-Encode nach
    H.264/8-bit/mp4 lohnt und der Upload danach erneut versucht werden soll.
    """


def _is_unsupported_video_error(err: dict) -> bool:
    """True, wenn Metas error-JSON auf ein nicht unterstütztes Video-Format zeigt."""
    return (
        err.get("code") == _UNSUPPORTED_VIDEO_CODE
        or err.get("error_subcode") == _UNSUPPORTED_VIDEO_SUBCODE
    )


def _retry(fn, retries: int = 5, delay: int = 10, label: str = ""):
    """Wiederholt fn() bei Netzwerkfehlern und HTTP-Fehlern mit exponentiellem Backoff.

    Erkennt auch retryable HTTP-Status auf erfolgreich zurückgegebenen
    requests.Response-Objekten (raise_for_status wird vom Caller meist erst danach
    aufgerufen — zu spät für Retries).
    """
    for attempt in range(retries):
        try:
            result = fn()
        except _NETWORK_ERRORS as e:
            if attempt < retries - 1:
                wait = delay * (2 ** attempt)
                logger.warning("Netzwerkfehler%s: %s — warte %ds, Versuch %d/%d …",
                               f" ({label})" if label else "", e, wait, attempt + 1, retries)
                time.sleep(wait)
                continue
            raise
        except Exception as e:
            err_str = str(e).lower()
            # HTTP Status Codes die einen Retry rechtfertigen
            is_retryable_http = any(str(code) in str(e) for code in _RETRY_HTTP_CODES)
            is_network = any(x in err_str for x in ("connection", "reset by peer", "broken pipe", "timed out", "timeout"))
            if (is_retryable_http or is_network) and attempt < retries - 1:
                wait = delay * (2 ** attempt)
                logger.warning("Fehler%s: %s — warte %ds, Versuch %d/%d …",
                               f" ({label})" if label else "", e, wait, attempt + 1, retries)
                time.sleep(wait)
                continue
            raise

        # Erfolgreich aufgerufen — bei retryable HTTP-Status trotzdem retrien.
        if isinstance(result, requests.Response) and result.status_code in _RETRY_HTTP_CODES:
            if attempt < retries - 1:
                wait = delay * (2 ** attempt)
                logger.warning("HTTP %d%s — warte %ds, Versuch %d/%d …",
                               result.status_code, f" ({label})" if label else "",
                               wait, attempt + 1, retries)
                time.sleep(wait)
                continue
            # Letzter Versuch: Response trotzdem zurückgeben, raise_for_status beim Caller wirft.
        return result


def _meta_error_detail(response) -> str:
    """
    Zieht Metas eigentliche Fehlerbegründung aus dem Response-Body.
    Ein blankes "400 Bad Request" verrät nicht, WARUM Meta ablehnt —
    der Grund steht im error-JSON (error_user_msg/message + Codes).
    Gibt "" zurück, wenn kein verwertbarer Body da ist.
    """
    if response is None:
        return ""
    try:
        err = response.json().get("error", {})
    except (ValueError, AttributeError):
        body = (getattr(response, "text", "") or "").strip()
        return config.mask_token(body[:500])
    if not err:
        return ""
    msg = err.get("error_user_msg") or err.get("message") or "Unbekannter Fehler"
    parts = [msg]
    if err.get("error_user_title"):
        parts.insert(0, err["error_user_title"])
    codes = []
    if err.get("code") is not None:
        codes.append(f"code {err['code']}")
    if err.get("error_subcode") is not None:
        codes.append(f"subcode {err['error_subcode']}")
    if codes:
        parts.append(f"({', '.join(codes)})")
    return config.mask_token(" — ".join(parts))


GDRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
VIDEO_CHUNK_SIZE = 4 * 1024 * 1024   # 4 MB pro Chunk — kleinere Chunks = weniger Timeouts

# Cache-Verzeichnis im Projektordner
_PROJECT_ROOT    = Path(__file__).parent.parent
CACHE_DIR        = _PROJECT_ROOT / "cache"
UPLOAD_CACHE_FILE = CACHE_DIR / "uploads.json"
DOWNLOAD_CACHE_DIR = CACHE_DIR / "downloads"


# ---------------------------------------------------------------------------
# Cache-Hilfsfunktionen
# ---------------------------------------------------------------------------

def _load_upload_cache() -> dict:
    if UPLOAD_CACHE_FILE.exists():
        try:
            with open(UPLOAD_CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_upload_cache(cache: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    with open(UPLOAD_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def _get_cached_upload(file_id: str) -> "UploadedAsset | None":
    entry = _load_upload_cache().get(file_id)
    if entry:
        asset = UploadedAsset(
            image_hash=entry.get("image_hash"),
            video_id=entry.get("video_id"),
            thumbnail_hash=entry.get("thumbnail_hash"),
            image_hash_2=entry.get("image_hash_2"),
            aspect_ratio=entry.get("aspect_ratio"),
            aspect_ratio_2=entry.get("aspect_ratio_2"),
        )
        logger.info("✅ Upload-Cache-Treffer für %s — überspringe Download & Upload.", file_id)
        return asset
    return None


def _save_cached_upload(file_id: str, asset: "UploadedAsset") -> None:
    cache = _load_upload_cache()
    cache[file_id] = {
        "image_hash":     asset.image_hash,
        "video_id":       asset.video_id,
        "thumbnail_hash": asset.thumbnail_hash,
        "image_hash_2":   asset.image_hash_2,
        "aspect_ratio":   asset.aspect_ratio,
        "aspect_ratio_2": asset.aspect_ratio_2,
        "cached_at":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _save_upload_cache(cache)
    logger.info("Upload-Ergebnis gecacht für %s.", file_id)


def _get_cached_download(file_id: str) -> "tuple[bytes, str, str] | None":
    meta_file = DOWNLOAD_CACHE_DIR / f"{file_id}.json"
    data_file = DOWNLOAD_CACHE_DIR / f"{file_id}.bin"
    if meta_file.exists() and data_file.exists():
        try:
            with open(meta_file) as f:
                meta = json.load(f)
            with open(data_file, "rb") as f:
                data = f.read()
            size_mb = len(data) // (1024 * 1024)
            logger.info("✅ Download-Cache-Treffer: %s (%d MB) — überspringe Google Drive Download.",
                        meta["filename"], size_mb)
            return data, meta["filename"], meta["mimetype"]
        except Exception as e:
            logger.warning("Download-Cache beschädigt für %s: %s — lade neu herunter.", file_id, e)
    return None


def _save_cached_download(file_id: str, data: bytes, filename: str, mimetype: str) -> None:
    DOWNLOAD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    meta_file = DOWNLOAD_CACHE_DIR / f"{file_id}.json"
    data_file = DOWNLOAD_CACHE_DIR / f"{file_id}.bin"
    with open(meta_file, "w") as f:
        json.dump({"filename": filename, "mimetype": mimetype}, f)
    with open(data_file, "wb") as f:
        f.write(data)
    logger.info("Download gecacht: %s (%d MB).", filename, len(data) // (1024 * 1024))


# ---------------------------------------------------------------------------
# Asset-Datenklasse
# ---------------------------------------------------------------------------

@dataclass
class UploadedAsset:
    """Ergebnis eines Asset-Uploads zu Meta."""
    image_hash: str | None = None
    video_id: str | None = None
    thumbnail_hash: str | None = None  # nur bei Videos gesetzt
    image_hash_2: str | None = None    # zweites Bild für Static Ads (Placement-Customization)
    # Seitenverhältnis (Breite/Höhe) der beiden Bilder — gemessen, nicht aus dem
    # Dateinamen geraten. Entscheidet, welches Bild in die vertikalen Placements
    # (Stories/Reels) geroutet wird. None = unbekannt (z.B. alter Cache-Eintrag).
    aspect_ratio: float | None = None
    aspect_ratio_2: float | None = None

    @property
    def is_video(self) -> bool:
        return self.video_id is not None


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------

def _extract_gdrive_id(url: str) -> str | None:
    patterns = [
        r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)",
        r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)",
        r"id=([a-zA-Z0-9_-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def is_gdrive_url(url: str) -> bool:
    return "drive.google.com" in url


def _get_gdrive_creds() -> Credentials:
    # Cloud (GOOGLE_CREDENTIALS_JSON) oder lokal (credentials.json) — via zentralem Helper
    creds = get_service_account_creds(GDRIVE_SCOPES)
    creds.refresh(GoogleAuthRequest())
    return creds


def _get_cached_download_path(file_id: str) -> "tuple[Path, str, str] | None":
    """Wie _get_cached_download, gibt aber den Dateipfad zurück (lädt NICHT in den RAM)."""
    meta_file = DOWNLOAD_CACHE_DIR / f"{file_id}.json"
    data_file = DOWNLOAD_CACHE_DIR / f"{file_id}.bin"
    if meta_file.exists() and data_file.exists():
        try:
            with open(meta_file) as f:
                meta = json.load(f)
            size_mb = data_file.stat().st_size // (1024 * 1024)
            logger.info("✅ Download-Cache-Treffer: %s (%d MB) — überspringe Google Drive Download.",
                        meta["filename"], size_mb)
            return data_file, meta["filename"], meta["mimetype"]
        except Exception as e:
            logger.warning("Download-Cache beschädigt für %s: %s — lade neu herunter.", file_id, e)
    return None


def _download_to_path(file_url: str) -> tuple[Path, str, str]:
    """
    Lädt eine Datei von Google Drive STREAMEND direkt auf die Disk herunter
    (Chunk → sofort in Datei schreiben, nie die ganze Datei im RAM halten).
    Gibt (path, filename, mimetype) zurück. Mit lokalem Cache.

    Das ist der speicherschonende Pfad für große Videos: vermeidet den
    OOM-Kill, der entsteht, wenn das ganze Video als bytes im RAM liegt.
    """
    file_id = _extract_gdrive_id(file_url)
    if not file_id:
        raise ValueError(f"Konnte keine Google Drive File-ID aus URL extrahieren: {file_url}")

    # Download-Cache prüfen
    cached = _get_cached_download_path(file_id)
    if cached:
        return cached

    creds = _get_gdrive_creds()
    auth_header = {"Authorization": f"Bearer {creds.token}"}

    # Metadaten holen
    meta_resp = requests.get(
        f"https://www.googleapis.com/drive/v3/files/{file_id}",
        params={"fields": "name,mimeType,size"},
        headers=auth_header,
    )
    meta_resp.raise_for_status()
    meta = meta_resp.json()
    filename = meta.get("name", f"{file_id}.bin")
    mimetype = meta.get("mimeType", "application/octet-stream")
    total_size = int(meta.get("size", 0))
    size_mb = total_size // (1024 * 1024)

    logger.info("Google Drive: %s (%s, %d MB) — wird heruntergeladen …", filename, mimetype, size_mb)

    dl_resp = _retry(
        lambda: requests.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            params={"alt": "media"},
            headers=auth_header,
            stream=True,
            timeout=300,
        ),
        label="Google Drive Download",
    )
    dl_resp.raise_for_status()

    if not total_size:
        total_size = int(dl_resp.headers.get("content-length", 0))
    chunk_size = 8 * 1024 * 1024  # 8 MB pro Chunk

    DOWNLOAD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data_file = DOWNLOAD_CACHE_DIR / f"{file_id}.bin"
    tmp_file = DOWNLOAD_CACHE_DIR / f"{file_id}.bin.part"
    downloaded = 0
    last_logged_pct = -1

    # Streamend in eine temporäre Datei schreiben, dann atomar umbenennen.
    # So bleibt kein halber Download als gültiger Cache-Eintrag zurück.
    with open(tmp_file, "wb") as out:
        for chunk in dl_resp.iter_content(chunk_size=chunk_size):
            if chunk:
                out.write(chunk)
                downloaded += len(chunk)
                if total_size:
                    pct = downloaded * 100 // total_size
                    # Nur alle 10% loggen um Terminal nicht zu fluten
                    if pct >= last_logged_pct + 10:
                        logger.info("  Download: %d MB / %d MB (%d%%) …",
                                    downloaded // (1024 * 1024), total_size // (1024 * 1024), pct)
                        last_logged_pct = pct

    tmp_file.replace(data_file)
    logger.info("Download abgeschlossen: %s (%d MB)", filename, downloaded // (1024 * 1024))

    # Cache-Metadaten schreiben
    meta_file = DOWNLOAD_CACHE_DIR / f"{file_id}.json"
    with open(meta_file, "w") as f:
        json.dump({"filename": filename, "mimetype": mimetype}, f)

    return data_file, filename, mimetype


def download_from_gdrive(file_url: str) -> tuple[bytes, str, str]:
    """
    Bytes-Variante (für kleine Dateien wie Thumbnail-Bilder).
    Delegiert an _download_to_path und liest die Datei dann ein.
    NICHT für große Videos verwenden — siehe _download_to_path.
    """
    path, filename, mimetype = _download_to_path(file_url)
    with open(path, "rb") as f:
        return f.read(), filename, mimetype


_STATIC_RATIO_RE   = re.compile(r"^(MetaAd-\d+)_STA-(4x5|16x9)_")
_STATIC_GENERIC_RE = re.compile(r"^(MetaAd-\d+)_STA_")


def _drive_search_files(query: str, auth: dict) -> list[dict]:
    """GET https://…/drive/v3/files mit gegebener Query. Wirft bei API-Fehler."""
    resp = requests.get(
        "https://www.googleapis.com/drive/v3/files",
        params={"q": query, "fields": "files(id,name)", "pageSize": 25},
        headers=auth,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("files", [])


def find_complementary_static_url(image_url: str) -> str | None:
    """
    Wenn image_url eine Static-Ad-Datei der Form
        MetaAd-NNNN_STA-(4x5|16x9)_…   ← explizites Ratio im Namen
      oder
        MetaAd-NNNN_STA_…              ← generisches STA, zwei Ratios als Geschwister
    ist, sucht im SELBEN Drive-Ordner nach der zweiten Format-Variante.
    Gibt den Drive-Link zurück oder None bei: kein Match, keine Treffer,
    mehrere Treffer (Ambiguität).
    """
    file_id = _extract_gdrive_id(image_url)
    if not file_id:
        return None

    creds = _get_gdrive_creds()
    auth = {"Authorization": f"Bearer {creds.token}"}

    try:
        meta_resp = requests.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            params={"fields": "name,parents"},
            headers=auth,
            timeout=30,
        )
        meta_resp.raise_for_status()
        meta = meta_resp.json()
    except Exception as e:
        logger.warning("Auto-Detect: Drive-Metadaten konnten nicht gelesen werden (%s): %s", file_id, e)
        return None

    name = meta.get("name", "")
    parents = meta.get("parents", [])
    parent_clause = f" and '{parents[0]}' in parents" if parents else ""
    common_clause = " and trashed = false and mimeType contains 'image/'"

    # ── Fall 1: explizites Ratio im Filename — suche das jeweils andere Ratio
    m = _STATIC_RATIO_RE.match(name)
    if m:
        meta_id, ratio = m.group(1), m.group(2)
        other_ratio = "16x9" if ratio == "4x5" else "4x5"
        prefix = f"{meta_id}_STA-{other_ratio}_"
        query = f"name contains '{prefix}'{common_clause}{parent_clause}"
        try:
            files = _drive_search_files(query, auth)
        except Exception as e:
            logger.warning("Auto-Detect: Drive-Suche fehlgeschlagen für %s: %s", prefix, e)
            return None
        files = [f for f in files if f["id"] != file_id]
        if len(files) == 0:
            logger.info("Auto-Detect: kein zweites Format (%s) für %s gefunden", other_ratio, meta_id)
            return None
        if len(files) > 1:
            names = ", ".join(f["name"] for f in files)
            logger.warning("Auto-Detect: mehrdeutig (%d Treffer) für %s_STA-%s_* — wird ignoriert: %s",
                           len(files), meta_id, other_ratio, names)
            return None
        logger.info("Auto-Detect: zweites Format gefunden: %s", files[0]["name"])
        return f"https://drive.google.com/file/d/{files[0]['id']}/view?usp=drive_link"

    # ── Fall 2: generisches STA — suche Geschwister mit gleichem MetaAd-Präfix
    m = _STATIC_GENERIC_RE.match(name)
    if m:
        meta_id = m.group(1)
        prefix = f"{meta_id}_STA_"
        query = f"name contains '{prefix}'{common_clause}{parent_clause}"
        try:
            files = _drive_search_files(query, auth)
        except Exception as e:
            logger.warning("Auto-Detect: Drive-Suche fehlgeschlagen für %s: %s", prefix, e)
            return None
        # Primäre Datei aus der Trefferliste entfernen
        siblings = [f for f in files if f["id"] != file_id]
        if len(siblings) == 0:
            logger.info("Auto-Detect: kein Geschwister für %s_STA_* gefunden", meta_id)
            return None
        if len(siblings) > 1:
            names = ", ".join(f["name"] for f in siblings)
            logger.warning("Auto-Detect: mehrdeutig (%d Geschwister) für %s_STA_* — wird ignoriert: %s",
                           len(siblings), meta_id, names)
            return None
        logger.info("Auto-Detect: zweites Format gefunden: %s", siblings[0]["name"])
        return f"https://drive.google.com/file/d/{siblings[0]['id']}/view?usp=drive_link"

    return None


# ---------------------------------------------------------------------------
# Meta Upload — mit Upload-Cache
# ---------------------------------------------------------------------------

def get_or_upload_gdrive_asset(file_url: str) -> UploadedAsset:
    """
    Hauptfunktion für Google Drive Assets:
    1. Upload-Cache prüfen → direkt zurückgeben wenn schon hochgeladen
    2. Download-Cache prüfen → nicht nochmal von Drive laden wenn schon lokal
    3. Hochladen zu Meta → Ergebnis cachen
    """
    file_id = _extract_gdrive_id(file_url)
    if not file_id:
        raise ValueError(f"Konnte keine Google Drive File-ID aus URL extrahieren: {file_url}")

    # 1. Upload-Cache: schon bei Meta hochgeladen?
    cached_upload = _get_cached_upload(file_id)
    if cached_upload:
        return cached_upload

    # 2. Download STREAMEND auf Disk (kein Full-Bytes im RAM → kein OOM bei großen Videos)
    asset_path, filename, mimetype = _download_to_path(file_url)

    # 3. Zu Meta hochladen (liest vom Pfad, nicht aus dem RAM)
    asset = _upload_asset_to_meta_path(asset_path, filename, mimetype)

    # 4. Upload-Ergebnis cachen
    _save_cached_upload(file_id, asset)

    # Speicher nach jedem Asset freigeben (wichtig auf Streamlit Cloud, ~2.7 GB Limit)
    gc.collect()

    return asset


def extract_first_frame(video_bytes: bytes) -> bytes:
    """Extrahiert den ersten Frame eines Videos als JPEG-Bytes."""
    logger.info("Extrahiere ersten Frame als Thumbnail …")
    buf = io.BytesIO(video_bytes)
    container = av.open(buf)
    try:
        for frame in container.decode(video=0):
            img = frame.to_image()  # PIL Image
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=90)
            logger.info("Ersten Frame extrahiert (%dx%d)", img.width, img.height)
            return out.getvalue()
    finally:
        container.close()
    raise ValueError("Kein Video-Frame gefunden")


def extract_first_frame_path(video_path: Path) -> bytes:
    """
    Extrahiert den ersten Frame als JPEG-Bytes — liest direkt vom Dateipfad.
    FFmpeg/PyAV liest nur die nötigen Header+Frames vom Datenträger statt das
    ganze Video in den RAM zu kopieren (BytesIO). Wichtig gegen OOM bei großen Videos.
    """
    logger.info("Extrahiere ersten Frame als Thumbnail …")
    container = av.open(str(video_path))
    try:
        for frame in container.decode(video=0):
            img = frame.to_image()  # PIL Image
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=90)
            logger.info("Ersten Frame extrahiert (%dx%d)", img.width, img.height)
            return out.getvalue()
    finally:
        container.close()
    raise ValueError("Kein Video-Frame gefunden")


def _wait_for_video_ready(video_id: str, max_wait_seconds: int = 600, poll_interval: int = 15) -> None:
    """
    Wartet bis Meta das Video verarbeitet hat (Status: ready).
    Pollt alle poll_interval Sekunden, bricht nach max_wait_seconds ab.

    Nutzt v23.0 direkt via REST statt SDK (das v25.0 forciert).
    """
    logger.info("Warte auf Meta Video-Verarbeitung (ID: %s) …", video_id)
    elapsed = 0
    status_url = f"https://graph.facebook.com/v23.0/{video_id}"
    while elapsed < max_wait_seconds:
        try:
            resp = requests.get(status_url, params={
                "access_token": config.META_ACCESS_TOKEN,
                "fields": "status,length,format,permalink_url",
            }, timeout=30)
            data = resp.json()
            if "error" in data:
                raise ValueError(data["error"].get("message", "Status-Fehler"))
            status = data.get("status", {})
            video_status = status.get("video_status", "unknown")
            progress = status.get("processing_progress", 0)
            err_phase = status.get("processing_phase", {}).get("errors") or status.get("uploading_phase", {}).get("errors")
            length = data.get("length")
            fmt_info = data.get("format") or []
            # Nur Filter-Namen statt full embed_html — sonst KB pro Poll
            fmt_short = [f.get("filter") for f in fmt_info if isinstance(f, dict)]
            err_suffix = f" phase_errors={err_phase}" if err_phase else ""
            logger.info("  Video-Status: %s (%d%%) length=%ss formats=%s%s",
                        video_status, progress, length, fmt_short, err_suffix)

            if video_status == "ready":
                logger.info("✅ Video bereit für Verwendung in Ads.")
                return
            if video_status in ("error", "invalid"):
                # Manchmal transienter Meta-Fehler — nochmal pollen bevor aufgeben
                if elapsed < 60:
                    logger.warning("  Video-Status 'error' — warte noch kurz und prüfe nochmal …")
                else:
                    raise ValueError(f"Meta Video-Verarbeitung fehlgeschlagen: {status}")
        except Exception as e:
            if "fehlgeschlagen" in str(e):
                raise
            logger.warning("  Status-Abfrage fehlgeschlagen: %s — nochmal versuchen …", e)

        time.sleep(poll_interval)
        elapsed += poll_interval

    raise ValueError(
        f"Timeout: Video {video_id} nach {max_wait_seconds}s immer noch nicht bereit. "
        "Bitte später nochmal versuchen."
    )


def upload_asset_to_meta(image_or_video_bytes: bytes, filename: str, mimetype: str) -> UploadedAsset:
    """
    Erkennt automatisch ob Bild oder Video und lädt entsprechend hoch.
    Bei Videos: ersten Frame extrahieren und als Thumbnail hochladen.
    Wartet bis Meta das Video fertig verarbeitet hat.
    """
    is_video = mimetype.startswith("video/")
    if is_video:
        thumb_bytes = extract_first_frame(image_or_video_bytes)
        thumb_name = filename.rsplit(".", 1)[0] + "_thumb.jpg"
        thumbnail_hash = _upload_image(thumb_bytes, thumb_name)
        for attempt in range(2):
            video_id = _upload_video(image_or_video_bytes, filename)
            try:
                _wait_for_video_ready(video_id)
                return UploadedAsset(video_id=video_id, thumbnail_hash=thumbnail_hash)
            except ValueError as e:
                if attempt == 0 and "fehlgeschlagen" in str(e):
                    logger.warning("Video-Verarbeitung fehlgeschlagen — nochmal hochladen (Versuch 2) …")
                    time.sleep(30)
                    continue
                raise
    else:
        image_hash = _upload_image(image_or_video_bytes, filename)
        return UploadedAsset(image_hash=image_hash)


def _upload_asset_to_meta_path(asset_path: Path, filename: str, mimetype: str) -> UploadedAsset:
    """
    Wie upload_asset_to_meta, arbeitet aber mit einem Dateipfad statt bytes.
    Speicherschonend: das ganze Video liegt nie komplett im RAM.
    """
    is_video = mimetype.startswith("video/")
    if is_video:
        thumb_bytes = extract_first_frame_path(asset_path)
        thumb_name = filename.rsplit(".", 1)[0] + "_thumb.jpg"
        thumbnail_hash = _upload_image(thumb_bytes, thumb_name)
        del thumb_bytes
        gc.collect()
        for attempt in range(2):
            video_id = _upload_video_from_path(asset_path, filename)
            try:
                _wait_for_video_ready(video_id)
                return UploadedAsset(video_id=video_id, thumbnail_hash=thumbnail_hash)
            except ValueError as e:
                if attempt == 0 and "fehlgeschlagen" in str(e):
                    logger.warning("Video-Verarbeitung fehlgeschlagen — nochmal hochladen (Versuch 2) …")
                    time.sleep(30)
                    continue
                raise
    else:
        # Bilder sind klein → in den RAM laden ist unkritisch.
        with open(asset_path, "rb") as f:
            image_bytes = f.read()
        image_hash = _upload_image(image_bytes, filename)
        return UploadedAsset(image_hash=image_hash,
                             aspect_ratio=_image_aspect_ratio(image_bytes, filename))


def _video_mime(filename: str) -> str:
    mime = "video/mp4"
    if filename.lower().endswith(".mov"):
        mime = "video/quicktime"
    elif filename.lower().endswith(".webm"):
        mime = "video/webm"
    return mime


def _transcode_to_h264(src_path: Path) -> Path:
    """Re-encodet ein Video nach H.264 (8-bit yuv420p) + AAC-Stereo in einen mp4.

    Fallback, wenn Meta den Originalexport (HEVC/10-bit/ProRes …) mit
    code 352 / subcode 1363024 als "nicht unterstütztes Format" ablehnt.
    Nutzt PyAV (libx264) statt ffmpeg-CLI → läuft auch auf Streamlit Cloud
    ohne apt-Zusatzpakete. Gibt den Pfad der neuen mp4 zurück.
    """
    dst_path = src_path.with_name(src_path.stem + "_h264.mp4")
    logger.info("Transkodiere '%s' → H.264/mp4 (Meta-kompatibel) …", src_path.name)

    # +faststart schiebt den moov-Atom nach vorne (schnelleres Meta-Processing).
    with av.open(str(src_path)) as in_c, \
         av.open(str(dst_path), mode="w", options={"movflags": "+faststart"}) as out_c:
        in_v = in_c.streams.video[0]
        cc = in_v.codec_context
        # libx264/yuv420p verlangt gerade Kantenlängen.
        width = cc.width - (cc.width % 2)
        height = cc.height - (cc.height % 2)
        rate = in_v.average_rate or in_v.base_rate or 30

        out_v = out_c.add_stream("libx264", rate=rate)
        out_v.width = width
        out_v.height = height
        out_v.pix_fmt = "yuv420p"
        out_v.options = {"crf": "20", "preset": "medium", "profile": "high"}
        # Zeitbasis des Originals übernehmen (meist 1/1000000). Ohne das leitet der
        # Encoder sie aus `rate` ab — bei krummen Bildraten (z.B. 29,985 statt 30,
        # typisch für KI-Animationen) ist dieses Raster zu grob für die
        # Mikrosekunden-Timestamps der Quelle. Der Rundungsfehler summiert sich, bis
        # zwei Frames auf denselben Tick fallen, und der Muxer bricht mitten im Video
        # mit "Invalid argument (22)" ab. Die feine Zeitbasis hält die Timestamps
        # eindeutig; Dauer und Bildrate des Originals bleiben exakt erhalten.
        out_v.codec_context.time_base = in_v.time_base

        in_a = in_c.streams.audio[0] if in_c.streams.audio else None
        out_a = None
        resampler = None
        if in_a is not None:
            sample_rate = in_a.codec_context.sample_rate or 48000
            out_a = out_c.add_stream("aac", rate=sample_rate)
            # AAC will planar float; auf Stereo downmixen (Ads sind praktisch immer stereo).
            resampler = av.AudioResampler(format="fltp", layout="stereo", rate=sample_rate)

        demux_streams = [in_v] + ([in_a] if in_a is not None else [])
        for packet in in_c.demux(*demux_streams):
            if packet.dts is None:
                continue  # leeres Flush-Paket vom Demuxer
            if packet.stream.type == "video":
                for frame in packet.decode():
                    # Original-Timestamps behalten — sonst staucht der Muxer die
                    # Framerate (frame.pts=None → kaputte fps im Container).
                    for out_pkt in out_v.encode(frame):
                        out_c.mux(out_pkt)
            elif packet.stream.type == "audio" and out_a is not None:
                for frame in packet.decode():
                    frame.pts = None
                    for r_frame in resampler.resample(frame):
                        for out_pkt in out_a.encode(r_frame):
                            out_c.mux(out_pkt)

        # Encoder (und Resampler) leerlaufen lassen — sonst fehlen die letzten Frames.
        for out_pkt in out_v.encode():
            out_c.mux(out_pkt)
        if out_a is not None:
            for r_frame in resampler.resample(None):
                for out_pkt in out_a.encode(r_frame):
                    out_c.mux(out_pkt)
            for out_pkt in out_a.encode():
                out_c.mux(out_pkt)

    logger.info("Transkodierung fertig → %s (%d MB)",
                dst_path.name, dst_path.stat().st_size // (1024 * 1024))
    return dst_path


def _upload_video_from_path(video_path: Path, filename: str, allow_transcode: bool = True) -> str:
    """
    Lädt ein Video vom Dateipfad zu Meta hoch.

    Lehnt Meta das Encoding als "nicht unterstützt" ab (code 352 / subcode
    1363024), wird das Video einmalig nach H.264/mp4 transkodiert und der
    Upload erneut versucht (`allow_transcode` verhindert eine Endlosschleife).
    """
    try:
        return _upload_video_worker(video_path, filename)
    except _UnsupportedVideoFormat as e:
        if not allow_transcode:
            raise
        logger.warning("Meta lehnt Video-Format ab (%s) — transkodiere nach "
                       "H.264/mp4 und versuche erneut …", e)
        transcoded = _transcode_to_h264(video_path)
        try:
            return _upload_video_from_path(transcoded, transcoded.name, allow_transcode=False)
        finally:
            # Transkodierte Datei ist ephemer; der Cache hält weiter das Original.
            transcoded.unlink(missing_ok=True)


def _upload_video_worker(video_path: Path, filename: str) -> str:
    """
    Ein Upload-Versuch (ohne Transcode-Fallback).

    Single-POST nur bis ~80 MB (lädt die Datei in den RAM — bei kleinen Files OK).
    Darüber: Chunked-Upload, der die Chunks per seek() direkt von der Disk liest,
    sodass immer nur EIN Chunk im RAM liegt → kein OOM bei großen Videos.
    """
    file_size = video_path.stat().st_size
    file_size_mb = file_size // (1024 * 1024)

    SINGLE_POST_LIMIT_MB = 80
    if file_size_mb > SINGLE_POST_LIMIT_MB:
        return _upload_video_chunked_from_path(video_path, filename, file_size)

    logger.info("Starte Video-Upload (%d MB) — Single-POST …", file_size_mb)

    base_url = f"https://graph.facebook.com/v23.0/{config.META_AD_ACCOUNT_ID}/advideos"
    token = config.META_ACCESS_TOKEN
    mime = _video_mime(filename)

    with open(video_path, "rb") as f:
        video_bytes = f.read()  # < 80 MB, unkritisch

    try:
        resp = _retry(
            lambda: requests.post(
                base_url,
                data={"access_token": token},
                files={"source": (filename, io.BytesIO(video_bytes), mime)},
                timeout=900,
            ),
            label="Video Upload (Single-POST)",
        )
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 413:
            logger.warning("Single-POST 413 Too Large — falle auf Chunked Upload zurück …")
            del video_bytes
            gc.collect()
            return _upload_video_chunked_from_path(video_path, filename, file_size)
        # Nicht unterstütztes Encoding? → Signal an den Wrapper, zu transkodieren.
        if e.response is not None:
            try:
                err = e.response.json().get("error", {})
            except ValueError:
                err = {}
            if _is_unsupported_video_error(err):
                raise _UnsupportedVideoFormat(_meta_error_detail(e.response)) from e
        # Metas eigentliche Begründung aus dem Response-Body ziehen — sonst
        # bleibt nur das nichtssagende "400 Bad Request" übrig.
        detail = _meta_error_detail(e.response)
        if detail:
            raise ValueError(f"Video Upload Fehler ({filename}): {detail}") from e
        raise

    data = resp.json()
    if "error" in data:
        err = data["error"]
        if _is_unsupported_video_error(err):
            raise _UnsupportedVideoFormat(_meta_error_detail(resp))
        msg = err.get("error_user_msg") or err.get("message", "Unbekannter Fehler")
        raise ValueError(f"Video Upload Fehler: {msg}")

    video_id = str(data.get("id") or data.get("video_id"))
    if not video_id or video_id == "None":
        raise ValueError(f"Keine Video-ID in Meta-Antwort: {data}")

    logger.info("Video hochgeladen → ID: %s", video_id)
    return video_id


def _upload_video_chunked_from_path(video_path: Path, filename: str, file_size: int) -> str:
    """
    Chunked-Upload, der die Chunks per seek() direkt von der Disk liest.
    Es liegt immer nur EIN Chunk im RAM — speicherschonend für große Videos.
    """
    logger.info("Starte Video-Upload (%d MB) — Chunked (streaming von Disk) …",
                file_size // (1024 * 1024))

    base_url = f"https://graph.facebook.com/v23.0/{config.META_AD_ACCOUNT_ID}/advideos"
    token = config.META_ACCESS_TOKEN

    start_resp = _retry(
        lambda: requests.post(base_url, data={
            "access_token": token,
            "upload_phase": "start",
            "file_size": file_size,
        }, timeout=60),
        label="Video Upload Start",
    )
    start_resp.raise_for_status()
    start_data = start_resp.json()
    if "error" in start_data:
        raise ValueError(f"Video Upload Start Fehler: {start_data['error']}")

    upload_session_id = start_data["upload_session_id"]
    video_id = str(start_data["video_id"])
    start_offset = int(start_data["start_offset"])
    end_offset = int(start_data["end_offset"])

    with open(video_path, "rb") as f:
        while start_offset < file_size:
            f.seek(start_offset)
            chunk = f.read(end_offset - start_offset)
            logger.info("Upload Chunk: %d MB / %d MB …",
                        end_offset // (1024 * 1024), file_size // (1024 * 1024))
            transfer_resp = _retry(
                lambda: requests.post(base_url, data={
                    "access_token": token,
                    "upload_phase": "transfer",
                    "upload_session_id": upload_session_id,
                    "start_offset": start_offset,
                }, files={"video_file_chunk": (filename, io.BytesIO(chunk), "video/mp4")},
                timeout=180),
                label=f"Video-Chunk {start_offset // (1024*1024)}MB",
            )
            transfer_resp.raise_for_status()
            transfer_data = transfer_resp.json()
            if "error" in transfer_data:
                if _is_unsupported_video_error(transfer_data["error"]):
                    raise _UnsupportedVideoFormat(_meta_error_detail(transfer_resp))
                raise ValueError(f"Video Upload Transfer Fehler: {transfer_data['error']}")
            start_offset = int(transfer_data["start_offset"])
            end_offset = int(transfer_data["end_offset"])
            del chunk
            time.sleep(0.2)

    finish_resp = _retry(
        lambda: requests.post(base_url, data={
            "access_token": token,
            "upload_phase": "finish",
            "upload_session_id": upload_session_id,
            "title": filename,
        }, timeout=60),
        label="Video Upload Finish",
    )
    finish_resp.raise_for_status()
    finish_data = finish_resp.json()
    if "error" in finish_data:
        if _is_unsupported_video_error(finish_data["error"]):
            raise _UnsupportedVideoFormat(_meta_error_detail(finish_resp))
        raise ValueError(f"Video Upload Finish Fehler: {finish_data['error']}")

    logger.info("Video hochgeladen → ID: %s", video_id)
    return video_id


# Formate die Meta direkt akzeptiert → MIME-Type + Dateiendung
_META_IMAGE_FORMATS = {
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
    "GIF": ("image/gif", "gif"),
}


def _normalize_image_for_meta(image_bytes: bytes, filename: str) -> tuple[bytes, str, str]:
    """Erkennt das echte Bildformat und liefert (bytes, dateiname_mit_endung, mimetype).

    Meta lehnt Uploads mit "Dateityp nicht unterstützt" (subcode 1487411) ab,
    wenn MIME-Type/Endung nicht zum tatsächlichen Bildinhalt passen — z.B. weil
    der Dateiname keine Endung hat oder das Bild WebP/HEIC/AVIF ist.
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            fmt = img.format
    except Exception as e:
        raise ValueError(f"Bild '{filename}' konnte nicht gelesen werden: {e}")

    base = filename.rsplit(".", 1)[0]

    if fmt in _META_IMAGE_FORMATS:
        mime, ext = _META_IMAGE_FORMATS[fmt]
        return image_bytes, f"{base}.{ext}", mime

    # Nicht unterstütztes Format (WebP, HEIC, AVIF, TIFF, …) → zu PNG konvertieren.
    logger.info("Bildformat '%s' wird von Meta nicht unterstützt — konvertiere '%s' zu PNG …",
                fmt, filename)
    with Image.open(io.BytesIO(image_bytes)) as img:
        out = io.BytesIO()
        img.save(out, format="PNG")
    return out.getvalue(), f"{base}.png", "image/png"


def _image_aspect_ratio(image_bytes: bytes, filename: str) -> float | None:
    """Seitenverhältnis (Breite/Höhe) eines Bildes — None, wenn nicht lesbar.

    Basis für die Placement-Zuordnung bei Static Ads: das schmalere/höhere Bild
    geht in Stories + Reels, das breitere in Feed & Co. Wird gemessen statt aus
    dem Dateinamen abgeleitet — die Namen ('…_V3.png' / '…_V3-1.png') sagen
    nichts über das Format aus.
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            if not img.height:
                return None
            return img.width / img.height
    except Exception as e:
        logger.warning("Seitenverhältnis von '%s' nicht lesbar: %s", filename, e)
        return None


def _upload_image(image_bytes: bytes, filename: str) -> str:
    """Lädt ein Bild zu Meta hoch → gibt Image Hash zurück."""
    time.sleep(config.API_CALL_DELAY)
    image_bytes, filename, mimetype = _normalize_image_for_meta(image_bytes, filename)
    response = _retry(
        lambda: requests.post(
            f"https://graph.facebook.com/v25.0/{config.META_AD_ACCOUNT_ID}/adimages",
            files={"filename": (filename, io.BytesIO(image_bytes), mimetype)},
            data={"access_token": config.META_ACCESS_TOKEN},
            timeout=120,
        ),
        label="Meta Bild-Upload",
    )

    # Metas Fehler-JSON auslesen BEVOR raise_for_status den generischen
    # "400 Bad Request" wirft — sonst geht die eigentliche Begründung verloren.
    try:
        data = response.json()
    except ValueError:
        data = {}

    if "error" in data:
        err = data["error"]
        msg = err.get("error_user_msg") or err.get("message", "Unbekannter Fehler")
        details = []
        if err.get("error_user_title"):
            details.append(err["error_user_title"])
        if err.get("code"):
            details.append(f"code {err['code']}")
        if err.get("error_subcode"):
            details.append(f"subcode {err['error_subcode']}")
        suffix = f" ({', '.join(details)})" if details else ""
        raise ValueError(
            f"Meta Bild-Upload Fehler für '{filename}' ({len(image_bytes)} Bytes): {msg}{suffix}"
        )

    response.raise_for_status()

    for file_info in data.get("images", {}).values():
        image_hash = file_info.get("hash")
        if image_hash:
            logger.info("Bild hochgeladen → Hash: %s…", image_hash[:12])
            return image_hash

    raise ValueError(f"Kein Image Hash in Meta-Antwort: {data}")


def _upload_video(video_bytes: bytes, filename: str) -> str:
    """
    Lädt ein Video zu Meta hoch.

    Strategie: Single-POST-Upload (kein chunking) für Files < 1 GB.
    Der chunked Endpoint /advideos hat einen Reassembly-Bug — die Chunks
    kommen an, werden aber nicht korrekt zusammengesetzt (Resultat:
    length=0, width=0, height=0, video_status=error).
    Single-POST umgeht das komplett.
    """
    file_size = len(video_bytes)
    file_size_mb = file_size // (1024 * 1024)

    # Threshold: Single-POST nur bis ~80 MB. Darüber 413 Errors von Meta.
    # Chunked Upload funktioniert wieder zuverlässig (Meta hat den Reassembly-Bug
    # gefixt der vor ~1h auftrat).
    SINGLE_POST_LIMIT_MB = 80
    if file_size_mb > SINGLE_POST_LIMIT_MB:
        return _upload_video_chunked(video_bytes, filename)

    logger.info("Starte Video-Upload (%d MB) — Single-POST …", file_size_mb)

    base_url = f"https://graph.facebook.com/v23.0/{config.META_AD_ACCOUNT_ID}/advideos"
    token = config.META_ACCESS_TOKEN

    # MIME-Type: explizit setzen (nicht application/octet-stream)
    mime = "video/mp4"
    if filename.lower().endswith(".mov"):
        mime = "video/quicktime"
    elif filename.lower().endswith(".webm"):
        mime = "video/webm"

    try:
        resp = _retry(
            lambda: requests.post(
                base_url,
                data={"access_token": token},
                files={"source": (filename, io.BytesIO(video_bytes), mime)},
                timeout=900,  # 15 Min für große Files
            ),
            label="Video Upload (Single-POST)",
        )
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        # 413 Too Large → Fallback auf Chunked Upload
        if e.response is not None and e.response.status_code == 413:
            logger.warning("Single-POST 413 Too Large — falle auf Chunked Upload zurück …")
            return _upload_video_chunked(video_bytes, filename)
        raise

    data = resp.json()
    if "error" in data:
        err = data["error"]
        msg = err.get("error_user_msg") or err.get("message", "Unbekannter Fehler")
        raise ValueError(f"Video Upload Fehler: {msg}")

    video_id = str(data.get("id") or data.get("video_id"))
    if not video_id or video_id == "None":
        raise ValueError(f"Keine Video-ID in Meta-Antwort: {data}")

    logger.info("Video hochgeladen → ID: %s", video_id)
    return video_id


def _upload_video_chunked(video_bytes: bytes, filename: str) -> str:
    """
    Fallback Chunked-Upload für sehr große Files (> 1 GB).
    Wird in der Praxis selten gebraucht.
    """
    file_size = len(video_bytes)
    logger.info("Starte Video-Upload (%d MB) — Chunked …", file_size // (1024 * 1024))

    base_url = f"https://graph.facebook.com/v23.0/{config.META_AD_ACCOUNT_ID}/advideos"
    token = config.META_ACCESS_TOKEN

    start_resp = _retry(
        lambda: requests.post(base_url, data={
            "access_token": token,
            "upload_phase": "start",
            "file_size": file_size,
        }, timeout=60),
        label="Video Upload Start",
    )
    start_resp.raise_for_status()
    start_data = start_resp.json()
    if "error" in start_data:
        raise ValueError(f"Video Upload Start Fehler: {start_data['error']}")

    upload_session_id = start_data["upload_session_id"]
    video_id = str(start_data["video_id"])
    start_offset = int(start_data["start_offset"])
    end_offset = int(start_data["end_offset"])

    while start_offset < file_size:
        chunk = video_bytes[start_offset:end_offset]
        logger.info("Upload Chunk: %d MB / %d MB …",
                    end_offset // (1024 * 1024), file_size // (1024 * 1024))
        transfer_resp = _retry(
            lambda: requests.post(base_url, data={
                "access_token": token,
                "upload_phase": "transfer",
                "upload_session_id": upload_session_id,
                "start_offset": start_offset,
            }, files={"video_file_chunk": (filename, io.BytesIO(chunk), "video/mp4")},
            timeout=180),
            label=f"Video-Chunk {start_offset // (1024*1024)}MB",
        )
        transfer_resp.raise_for_status()
        transfer_data = transfer_resp.json()
        if "error" in transfer_data:
            raise ValueError(f"Video Upload Transfer Fehler: {transfer_data['error']}")
        start_offset = int(transfer_data["start_offset"])
        end_offset = int(transfer_data["end_offset"])
        time.sleep(0.2)

    finish_resp = _retry(
        lambda: requests.post(base_url, data={
            "access_token": token,
            "upload_phase": "finish",
            "upload_session_id": upload_session_id,
            "title": filename,
        }, timeout=60),
        label="Video Upload Finish",
    )
    finish_resp.raise_for_status()
    finish_data = finish_resp.json()
    if "error" in finish_data:
        if _is_unsupported_video_error(finish_data["error"]):
            raise _UnsupportedVideoFormat(_meta_error_detail(finish_resp))
        raise ValueError(f"Video Upload Finish Fehler: {finish_data['error']}")

    logger.info("Video hochgeladen → ID: %s", video_id)
    return video_id
