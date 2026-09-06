from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from sheets.reader import AdRow

logger = logging.getLogger(__name__)

_TIMEOUT = 8
_MAX_REDIRECTS = 5
_MAX_WORKERS = 8
_RETRIES = 2
_RETRY_WAIT = 1.5

# Vollständiger Browser-User-Agent: Shopify/Cloudflare lassen den kurzen
# "Mozilla/5.0" unter Last (mehrere parallele Requests) intermittierend mit 503
# abblitzen — ein vollständiger UA wird zuverlässig durchgelassen.
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml,*/*"}

# Codes, die belegen dass die Seite existiert (ggf. mit Bot-/Auth-Schutz).
_EXISTS_CODES = {200, 401, 403}
# Temporäre Server-/Rate-Limit-Codes: kein Beleg für eine kaputte URL.
_TRANSIENT_CODES = {429, 500, 502, 503, 504}


def _is_gdrive(url: str) -> bool:
    return "drive.google.com" in url


def _check_url(url: str) -> str | None:
    """Gibt None zurück wenn OK, sonst eine Fehlermeldung."""
    if not url.startswith("https://"):
        return f"URL muss mit https:// beginnen: {url}"
    for attempt in range(_RETRIES + 1):
        try:
            r = requests.head(url, timeout=_TIMEOUT, allow_redirects=True, headers=_HEADERS)
            # Manche Server lehnen HEAD ab — GET als Fallback
            if r.status_code in (405, 403, 501):
                r = requests.get(
                    url, timeout=_TIMEOUT, allow_redirects=True,
                    headers=_HEADERS, stream=True,
                )
                r.close()
            status = r.status_code

            if status in _EXISTS_CODES:
                return None  # Seite existiert (ggf. Bot-Schutz) — Meta kann sie aufrufen
            if status in _TRANSIENT_CODES:
                if attempt < _RETRIES:
                    time.sleep(_RETRY_WAIT * (attempt + 1))
                    continue
                # Nach Retries weiterhin temporär → durchlassen. Ein 429/5xx
                # belegt keine kaputte URL; Meta crawlt die Seite ohnehin selbst.
                logger.warning(
                    "⚠ %s liefert HTTP %d (temporär/Rate-Limit) — wird durchgelassen.",
                    url, status,
                )
                return None
            # Echter Fehler (z.B. 404)
            return f"nicht erreichbar (HTTP {status}): {url}"
        except requests.exceptions.Timeout:
            if attempt < _RETRIES:
                time.sleep(_RETRY_WAIT)
                continue
            return f"Timeout nach {_TIMEOUT}s: {url}"
        except requests.exceptions.ConnectionError as e:
            if attempt < _RETRIES:
                time.sleep(_RETRY_WAIT)
                continue
            return f"Verbindungsfehler: {url} — {e}"
        except requests.exceptions.RequestException as e:
            return f"Request-Fehler: {url} — {e}"
    return None


def _urls_for_row(row: AdRow) -> list[tuple[str, str]]:
    """Gibt (feldname, url) Paare zurück die geprüft werden sollen."""
    candidates = [
        ("destination_url", row.destination_url),
        ("image_url", row.image_url),
        ("image_url_2", row.image_url_2),
        ("thumbnail_url", row.thumbnail_url),
    ]
    return [
        (field, url)
        for field, url in candidates
        if url and not _is_gdrive(url)
    ]


def validate_ad_urls(ad_rows: list[AdRow]) -> list[tuple[int, str]]:
    """
    Prüft alle URLs aller AdRows per HTTP-Request.
    Gibt (row_index, Fehlermeldung) für jede kaputte URL zurück.
    Google-Drive-URLs werden übersprungen.
    Läuft parallel mit max. 8 Threads.
    """
    tasks: list[tuple[int, str, str]] = []  # (row_index, field, url)
    for row in ad_rows:
        for field, url in _urls_for_row(row):
            tasks.append((row.row_index, field, url))

    if not tasks:
        return []

    # Jede eindeutige URL nur EINMAL prüfen. Mehrere Ad-Zeilen teilen sich oft
    # dieselbe Landingpage — würde man pro Zeile prüfen, feuern bei _MAX_WORKERS
    # Threads mehrere parallele Requests auf dieselbe URL und lösen serverseitiges
    # Rate-Limiting (HTTP 429/503) aus.
    unique_urls = sorted({url for _, _, url in tasks})
    logger.info(
        "Prüfe %d eindeutige URL(s) (aus %d Feld-Treffern) vor dem Upload …",
        len(unique_urls), len(tasks),
    )

    results: dict[str, str | None] = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(_check_url, u): u for u in unique_urls}
        for future in as_completed(futures):
            url = futures[future]
            results[url] = future.result()

    errors: list[tuple[int, str]] = []
    for row_index, field, url in tasks:
        err = results.get(url)
        if err:
            errors.append((row_index, f"{field} {err}"))

    if errors:
        logger.warning("%d URL(s) ungültig — betroffene Zeilen werden übersprungen.", len(errors))
    else:
        logger.info("Alle URLs erreichbar ✓")

    return errors
