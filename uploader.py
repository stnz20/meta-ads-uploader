from __future__ import annotations
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from facebook_business.adobjects.adaccount import AdAccount

import config
from config import mask_token
from sheets.reader import (
    AdSetRow, AdRow,
    read_adsets, read_ads,
    write_status, append_log,
)
from meta.campaign import get_campaign
from meta.adset import create_adset
from meta.ad import create_ad, get_existing_ad_names
from meta.creative_defaults import fetch_creative_defaults, apply_defaults, CreativeDefaults

import gspread

logger = logging.getLogger(__name__)

# Maximale Anzahl paralleler Ad-Uploads
# 3 ist ein guter Wert: schnell genug, ohne Meta Rate Limits zu triggern
MAX_PARALLEL_ADS = 3

# Lock für Google Sheets Schreibzugriffe (verhindert gleichzeitige API-Calls)
_sheets_lock = threading.Lock()

# Prozessweiter Lock gegen ÜBERLAPPENDE Upload-Läufe (Fix Doppel-Upload):
# Der Streamlit-Upload läuft in einem daemon-Thread. Bei WebSocket-Reconnect/Rerun
# oder erneutem Klick auf "Upload starten" kann ein zweiter Lauf starten, während
# der erste noch läuft — beide lesen dann dieselben pending-Zeilen und legen jede
# Ad doppelt an. Dieser Lock lässt in derselben App-Instanz nur EINEN echten Upload
# gleichzeitig zu (Dry-Runs sind ausgenommen, sie legen nichts an).
_run_lock = threading.Lock()


class UploadAlreadyRunning(RuntimeError):
    """Wird geworfen, wenn bereits ein Upload in dieser App-Instanz läuft."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_page_id(adset_row: AdSetRow, dry_run: bool) -> str:
    """Page ID aus dem Sheet nehmen, sonst Fallback auf .env."""
    if dry_run:
        return f"DRY_RUN_PAGE_{adset_row.page_id or 'DEFAULT'}"
    return adset_row.page_id or config.META_PAGE_ID


def _write(sheet: gspread.Spreadsheet, dry_run: bool, *args, **kwargs) -> None:
    """write_status nur aufrufen wenn KEIN Dry-Run — thread-safe via Lock."""
    if not dry_run:
        with _sheets_lock:
            write_status(sheet, *args, **kwargs)


def _log(sheet: gspread.Spreadsheet, dry_run: bool, *args) -> None:
    """append_log nur aufrufen wenn KEIN Dry-Run — thread-safe via Lock."""
    if not dry_run:
        with _sheets_lock:
            append_log(sheet, *args)


def run(
    sheet: gspread.Spreadsheet,
    account: AdAccount,
    dry_run: bool = False,
    override_campaign_id: str | None = None,
    override_adset_id: str | None = None,
    progress_cb=None,
) -> None:
    """Startet einen Upload-Lauf. Serialisiert echte Uploads über einen prozessweiten
    Lock, damit nie zwei Läufe gleichzeitig dieselben pending-Zeilen abarbeiten
    (Doppel-Upload-Schutz). Dry-Runs laufen ohne Lock, da sie nichts anlegen.

    Wirft UploadAlreadyRunning, wenn bereits ein echter Upload läuft.
    """
    if dry_run:
        return _run_impl(sheet, account, dry_run, override_campaign_id,
                         override_adset_id, progress_cb)
    if not _run_lock.acquire(blocking=False):
        raise UploadAlreadyRunning(
            "Es läuft bereits ein Upload in dieser App-Instanz. Bitte warte, bis er "
            "abgeschlossen ist, bevor du erneut startest — sonst würden Ads doppelt angelegt."
        )
    try:
        return _run_impl(sheet, account, dry_run, override_campaign_id,
                         override_adset_id, progress_cb)
    finally:
        _run_lock.release()


def _run_impl(
    sheet: gspread.Spreadsheet,
    account: AdAccount,
    dry_run: bool = False,
    override_campaign_id: str | None = None,
    override_adset_id: str | None = None,
    progress_cb=None,
) -> None:
    """progress_cb: optionaler Callback (done, total, label) — 1 Einheit pro
    Ad Set bzw. Ad. Wird u.a. vom Streamlit-Upload für den Progressbar genutzt."""
    prefix = "[DRY-RUN] " if dry_run else ""

    if override_campaign_id:
        logger.info("Override: source_campaign_id = %s", override_campaign_id)
    if override_adset_id:
        logger.info("Override: source_adset_id = %s", override_adset_id)

    # --- Lesen & Validieren ---
    adset_rows, done_adset_rows, adset_errors = read_adsets(sheet)

    # Overrides auf alle neuen Ad-Set-Zeilen anwenden
    if override_campaign_id or override_adset_id:
        adset_rows = [
            AdSetRow(
                row_index=row.row_index,
                source_campaign_id=override_campaign_id or row.source_campaign_id,
                ad_set_name=row.ad_set_name,
                daily_budget=row.daily_budget,
                start_time=row.start_time,
                end_time=row.end_time,
                targeting_override=row.targeting_override,
                page_id=row.page_id,
                source_adset_id=override_adset_id or row.source_adset_id,
                bid_amount=row.bid_amount,
                dco_mode=row.dco_mode,
                raw=row.raw,
            )
            for row in adset_rows
        ]
    ad_rows, ad_errors = read_ads(sheet)

    # URL-Validierung — blockiert Upload bei kaputten Links (übersprungen im Dry-Run)
    if not dry_run and ad_rows:
        from utils.url_validator import validate_ad_urls
        url_errors = validate_ad_urls(ad_rows)
        if url_errors:
            error_indices: set[int] = set()
            for row_idx, msg in url_errors:
                logger.error("ads Zeile %d — URL ungültig: %s", row_idx, msg)
                _write(sheet, dry_run, "ads", row_idx, f"ERROR: {msg}")
                _log(sheet, dry_run, _now(), "url_validation", str(row_idx), f"ERROR: {msg}")
                error_indices.add(row_idx)
            ad_rows = [r for r in ad_rows if r.row_index not in error_indices]

    if adset_errors:
        for row_idx, msg in adset_errors:
            logger.warning("ad_sets Zeile %d übersprungen: %s", row_idx, msg)
            _write(sheet, dry_run, "ad_sets", row_idx, f"ERROR: {msg}")
            _log(sheet, dry_run, _now(), "adset_validation", str(row_idx), f"ERROR: {msg}")

    if ad_errors:
        for row_idx, msg in ad_errors:
            logger.warning("ads Zeile %d übersprungen: %s", row_idx, msg)
            _write(sheet, dry_run, "ads", row_idx, f"ERROR: {msg}")
            _log(sheet, dry_run, _now(), "ad_validation", str(row_idx), f"ERROR: {msg}")

    if not adset_rows and not done_adset_rows:
        logger.info("%sKeine Ad Sets zu verarbeiten.", prefix)
        return

    if not adset_rows:
        logger.info("%sKeine neuen Ad Sets — verarbeite nur offene Ads.", prefix)

    # Kampagnen vorab prüfen (einmalig pro unique ID, nur für neue Ad Sets)
    unique_campaigns = {r.source_campaign_id for r in adset_rows}
    for cid in unique_campaigns:
        try:
            info = get_campaign(cid)
            logger.info("%sKampagne %s (%s) validiert.", prefix, cid, info.get("name", "?"))
        except Exception as e:
            safe_msg = mask_token(str(e))
            logger.error("Kampagne %s nicht erreichbar: %s", cid, safe_msg)
            for row in adset_rows:
                if row.source_campaign_id == cid:
                    _write(sheet, dry_run, "ad_sets", row.row_index, "ERROR: Kampagne nicht gefunden")
            adset_rows = [r for r in adset_rows if r.source_campaign_id != cid]

    # Fortschritts-Reporting: 1 Einheit pro Ad Set + pro Ad (nach Validierung/
    # Kampagnen-Filter, damit total der tatsächlich verarbeiteten Menge entspricht).
    total_units = len(adset_rows) + len(ad_rows)
    done_units = 0

    def _report_progress(label: str) -> None:
        nonlocal done_units
        done_units += 1
        if progress_cb:
            try:
                progress_cb(done_units, total_units, label)
            except Exception:
                pass  # UI-Fehler dürfen den Upload nie stoppen

    if progress_cb and total_units:
        try:
            progress_cb(0, total_units, "")
        except Exception:
            pass

    # Cache für Creative-Defaults — pro Referenz-Ad-Set-ID nur einmal abrufen.
    # _defaults_cache speichert auch None (leeres/nicht ladbares Ad Set) damit
    # die API nicht für jedes Ad erneut aufgerufen wird.
    _defaults_cache: dict[str, CreativeDefaults | None] = {}

    def _get_defaults(ref_id: str) -> CreativeDefaults | None:
        if dry_run or not ref_id:
            return None
        if ref_id in _defaults_cache:
            return _defaults_cache[ref_id]
        result = fetch_creative_defaults(ref_id)
        _defaults_cache[ref_id] = result
        if result is not None:
            logger.info("Creative-Defaults gecacht für Ad Set %s", ref_id)
        return result

    # Dedup-Cache gegen Doppel-Anlage: pro Ad-Set-ID die bereits bei Meta
    # vorhandenen {ad_name: ad_id}. Wird beim ersten Ad des Ad Sets per API befüllt
    # und bei jeder Neu-Anlage fortgeschrieben. Fängt Wiederholungen ab, wenn ein
    # früherer Lauf abbrach, bevor er DONE ins Sheet schrieb.
    _existing_ads_cache: dict[str, dict[str, str]] = {}
    _dedup_lock = threading.Lock()
    _PENDING = "__PENDING__"

    def _get_existing_ads(adset_id: str) -> dict[str, str]:
        if dry_run:
            return {}
        with _dedup_lock:
            cached = _existing_ads_cache.get(adset_id)
        if cached is not None:
            return cached
        try:
            names = get_existing_ad_names(adset_id)
        except Exception as e:
            logger.warning("Dedup-Check für Ad Set %s fehlgeschlagen (%s) — fahre ohne fort",
                           adset_id, mask_token(str(e)))
            names = {}
        with _dedup_lock:
            # Falls parallel schon befüllt (inkl. Reservierungen), dessen Version behalten
            return _existing_ads_cache.setdefault(adset_id, names)

    # name → (adset_id, page_id, creative_defaults) Mapping
    # Bereits erledigte Ad Sets vorab eintragen → Ads können trotzdem erstellt werden
    adset_id_map: dict[str, tuple[str, str, CreativeDefaults | None]] = {}
    for done_row in done_adset_rows:
        existing_id = done_row.raw.get("created_id", "")
        if existing_id:
            page_id = _resolve_page_id(done_row, dry_run)
            ref_id = done_row.raw.get("source_adset_id", "")
            adset_id_map[done_row.ad_set_name] = (existing_id, page_id, _get_defaults(ref_id))
            logger.info("Bestehendes Ad Set '%s' → %s (aus Sheet)", done_row.ad_set_name, existing_id)

    # --- Ad Sets erstellen ---
    for row in adset_rows:
        page_id = _resolve_page_id(row, dry_run)
        logger.info("%sErstelle Ad Set '%s' (Page: %s) …", prefix, row.ad_set_name, page_id)
        try:
            adset_id = create_adset(account, row, dry_run=dry_run)
            adset_id_map[row.ad_set_name] = (adset_id, page_id, _get_defaults(row.source_adset_id))
            _write(sheet, dry_run, "ad_sets", row.row_index, "DONE", adset_id)
            _log(sheet, dry_run, _now(), "create_adset", adset_id, "DONE")
            logger.info("%sAd Set erstellt: %s → %s", prefix, row.ad_set_name, adset_id)
        except Exception as e:
            msg = mask_token(str(e))
            logger.error("Fehler bei Ad Set '%s': %s", row.ad_set_name, msg)
            _write(sheet, dry_run, "ad_sets", row.row_index, f"ERROR: {msg}")
            _log(sheet, dry_run, _now(), "create_adset", row.ad_set_name, f"ERROR: {msg}")
        _report_progress(row.ad_set_name)

    # --- Ads erstellen (parallel) ---
    def _process_ad(row: AdRow) -> None:
        """Verarbeitet ein einzelnes Ad — läuft parallel in einem Thread."""
        mapping = adset_id_map.get(row.ad_set_ref)
        if not mapping:
            msg = f"Ad Set '{row.ad_set_ref}' nicht gefunden oder fehlgeschlagen"
            logger.warning("%sÜberspringe Ad '%s': %s", prefix, row.ad_name, msg)
            _write(sheet, dry_run, "ads", row.row_index, f"ERROR: {msg}")
            _log(sheet, dry_run, _now(), "create_ad", row.ad_name, f"ERROR: {msg}")
            return

        adset_id, page_id, defaults = mapping
        current_row = apply_defaults(row, defaults) if defaults else row
        ad_name = current_row.ad_name

        # Dedup-Guard: existiert im Ziel-Ad-Set schon eine gleichnamige Ad, wird
        # nicht erneut angelegt (Schutz vor Doppel-Upload nach Abbruch/Wiederholung).
        # Die Reservierung mit _PENDING verhindert zusätzlich, dass zwei parallele
        # Threads dieselbe (im Sheet doppelte) Zeile gleichzeitig anlegen.
        existing = _get_existing_ads(adset_id)
        claimed_here = False
        with _dedup_lock:
            existing_id = existing.get(ad_name)
            if existing_id is None:
                existing[ad_name] = _PENDING
                claimed_here = True
        if not claimed_here:
            if existing_id == _PENDING:
                logger.warning("%sAd '%s' wird bereits parallel angelegt (Duplikat-Zeile) "
                               "— übersprungen.", prefix, ad_name)
                _write(sheet, dry_run, "ads", current_row.row_index, "DONE")
                _log(sheet, dry_run, _now(), "create_ad", ad_name, "SKIPPED_DUPLICATE_INFLIGHT")
            else:
                logger.info("%sAd '%s' existiert bereits im Ad Set %s (%s) — übersprungen (Dedup).",
                            prefix, ad_name, adset_id, existing_id)
                _write(sheet, dry_run, "ads", current_row.row_index, "DONE", existing_id)
                _log(sheet, dry_run, _now(), "create_ad", existing_id, "SKIPPED_DUPLICATE")
            return

        # Page-Override pro Ad hat Vorrang vor dem Ad-Set-Level Page
        effective_page_id = current_row.page_id or page_id
        logger.info("%sErstelle Ad '%s' …", prefix, ad_name)
        try:
            ad_id = create_ad(account, current_row, adset_id, effective_page_id, dry_run=dry_run)
            with _dedup_lock:
                existing[ad_name] = ad_id
            _write(sheet, dry_run, "ads", current_row.row_index, "DONE", ad_id)
            _log(sheet, dry_run, _now(), "create_ad", ad_id, "DONE")
            logger.info("%sAd erstellt: %s → %s", prefix, ad_name, ad_id)
        except Exception as e:
            # Reservierung zurücknehmen, damit ein späterer Retry es erneut versuchen darf
            with _dedup_lock:
                if existing.get(ad_name) == _PENDING:
                    existing.pop(ad_name, None)
            msg = mask_token(str(e))
            logger.error("Fehler bei Ad '%s': %s", ad_name, msg)
            _write(sheet, dry_run, "ads", current_row.row_index, f"ERROR: {msg}")
            _log(sheet, dry_run, _now(), "create_ad", ad_name, f"ERROR: {msg}")

    if ad_rows:
        logger.info("%sVerarbeite %d Ads mit max. %d parallelen Threads …",
                    prefix, len(ad_rows), MAX_PARALLEL_ADS)
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_ADS) as executor:
            futures = {executor.submit(_process_ad, row): row for row in ad_rows}
            for future in as_completed(futures):
                # Exceptions die aus dem Thread entkommen (sollten nicht passieren)
                try:
                    future.result()
                except Exception as e:
                    row = futures[future]
                    logger.error("Unbehandelter Fehler bei Ad '%s': %s", row.ad_name, e)
                _report_progress(futures[future].ad_name)
