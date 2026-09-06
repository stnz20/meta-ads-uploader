from __future__ import annotations
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import gspread

logger = logging.getLogger(__name__)

ADSET_COLS_ORDERED = [
    "source_campaign_id", "ad_set_name", "daily_budget",
    "start_time", "end_time", "targeting_override",
    "page_id", "source_adset_id", "bid_amount",
    "dco_mode",
    "status", "created_id",
]
ADS_COLS_ORDERED = [
    "ad_set_ref", "ad_name", "image_url", "image_hash", "video_id", "thumbnail_url",
    "image_url_2", "image_url_3", "image_url_4", "image_url_5", "image_url_6",
    "image_url_7", "image_url_8", "image_url_9", "image_url_10",
    "body", "body_2", "body_3", "body_4", "body_5",
    "headline", "headline_2", "headline_3",
    "description", "description_2", "description_3",
    "cta", "destination_url", "display_link", "instagram_actor_id",
    "page_id", "advertiser_id",
    "dco_mode",
    "status", "created_id",
]

REQUIRED_ADSET_FIELDS = {"source_campaign_id", "ad_set_name", "start_time"}
# daily_budget ist optional — bei CBO-Kampagnen wird es ignoriert
# body, headline, cta, destination_url sind optional wenn source_adset_id gesetzt ist
# (Werte werden dann aus dem Referenz-Ad Set übernommen)
REQUIRED_AD_FIELDS = {"ad_set_ref", "ad_name"}

# Spalten-Suffixe für Text-Variationen (Meta rotiert diese automatisch)
_BODY_COLS = ["body", "body_2", "body_3", "body_4", "body_5"]
_HEADLINE_COLS = ["headline", "headline_2", "headline_3"]
_DESC_COLS = ["description", "description_2", "description_3"]
# Zusatz-Asset-Slots für 3-2-2-DCO (image_url ist Slot 1, image_url_2..10 = 9 Extras)
_EXTRA_ASSET_COLS = [
    "image_url_2", "image_url_3", "image_url_4", "image_url_5", "image_url_6",
    "image_url_7", "image_url_8", "image_url_9", "image_url_10",
]


def _is_yes(value) -> bool:
    return str(value or "").strip().lower() in ("yes", "true", "1", "y")


def _collect_variations(row: dict, col_names: list[str]) -> list[str]:
    return [str(row[c]).strip() for c in col_names if row.get(c) and str(row[c]).strip()]


@dataclass
class AdSetRow:
    row_index: int
    source_campaign_id: str
    ad_set_name: str
    daily_budget: int          # Cent (0 = nicht gesetzt, z.B. bei CBO)
    start_time: str            # ISO-8601
    end_time: Optional[str]
    targeting_override: Optional[dict]
    page_id: Optional[str]     # Überschreibt META_PAGE_ID aus .env (optional)
    source_adset_id: Optional[str]  # Referenz-Ad Set ID — wenn leer, wird erstes der Kampagne genommen
    bid_amount: Optional[int]  # Cent — Pflicht bei Bid-Cap-Kampagnen (LOWEST_COST_WITH_BID_CAP)
    dco_mode: bool = False     # 3-2-2 Dynamic Creative: Ad Set wird mit is_dynamic_creative=True angelegt
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class AdRow:
    row_index: int
    ad_set_ref: str
    ad_name: str
    # Asset: genau eines der drei muss gesetzt sein
    image_url: Optional[str]       # URL — nur Einzel-Variation möglich
    image_url_2: Optional[str]     # zweites Bild für Static Ads (16x9 + 4x5 Placement-Customization)
    image_hash: Optional[str]      # Meta Hash — Multi-Variation möglich
    video_id: Optional[str]        # Meta Video ID — Multi-Variation möglich
    thumbnail_url: Optional[str]   # Thumbnail für Videos (Pflicht bei Video-Ads)
    # 3-2-2 DCO: zusätzliche Asset-URLs (image_url ist Slot 1, asset_urls enthält image_url + 2..6)
    asset_urls: list[str] = field(default_factory=list)
    dco_mode: bool = False
    # Text-Variationen (mindestens 1 Eintrag je Liste)
    bodies: list[str] = field(default_factory=list)
    headlines: list[str] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)  # optional, kann leer sein
    cta: str = ""
    destination_url: str = ""
    display_link: Optional[str] = None
    instagram_actor_id: Optional[str] = None  # Instagram-Konto für die Ad (optional, sonst aus Referenz)
    page_id: Optional[str] = None             # Facebook Page Override (optional, sonst aus Ad Set)
    advertiser_id: Optional[str] = None       # Advertiser für Identity-Bereich (optional)
    url_tags: Optional[str] = None            # URL-Parameter (UTM-Tracking), sonst aus Referenz-Ad Set
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_multi_variation(self) -> bool:
        return len(self.bodies) > 1 or len(self.headlines) > 1 or len(self.descriptions) > 1

    @property
    def supports_multi_variation(self) -> bool:
        """asset_feed_spec braucht image_hash oder video_id, nicht image_url."""
        return bool(self.image_hash or self.video_id)

    @property
    def is_dco(self) -> bool:
        """3-2-2-Modus aktiv und mindestens 2 Assets vorhanden."""
        return self.dco_mode and len(self.asset_urls) >= 2


def _validate_adset(row: dict, row_index: int) -> list[str]:
    errors = []
    missing = REQUIRED_ADSET_FIELDS - {k for k, v in row.items() if v}
    if missing:
        errors.append(f"Fehlende Pflichtfelder: {missing}")
    if row.get("daily_budget"):
        try:
            budget = int(row["daily_budget"])
            if budget <= 0:
                errors.append("daily_budget muss > 0 sein (oder leer lassen bei CBO-Kampagnen)")
        except ValueError:
            errors.append("daily_budget ist keine gültige Zahl (oder leer lassen bei CBO-Kampagnen)")
    if row.get("start_time"):
        try:
            dt = datetime.fromisoformat(str(row["start_time"]).replace("Z", "+00:00"))
            # Wenn kein Timezone-Info → als UTC behandeln
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt < datetime.now(timezone.utc):
                errors.append("start_time liegt in der Vergangenheit")
        except ValueError:
            errors.append("start_time ist kein gültiges ISO-8601-Datum")
    if row.get("targeting_override"):
        try:
            json.loads(row["targeting_override"])
        except json.JSONDecodeError:
            errors.append("targeting_override ist kein gültiges JSON")
    return errors


def _validate_ad(row: dict, row_index: int) -> list[str]:
    errors = []
    missing = REQUIRED_AD_FIELDS - {k for k, v in row.items() if v}
    if missing:
        errors.append(f"Fehlende Pflichtfelder: {missing}")
    # Asset-Prüfung nur wenn KEIN Referenz-Ad Set gesetzt
    # (asset kann aus Referenz kommen)
    has_asset = any([row.get("image_url"), row.get("image_hash"), row.get("video_id")])
    has_reference = bool(row.get("ad_set_ref"))  # Referenz-Defaults kommen über das Ad Set
    if not has_asset and not has_reference:
        errors.append("Eines der Felder image_url, image_hash oder video_id ist Pflicht")
    # 3-2-2 DCO: mindestens 2 Assets nötig
    if _is_yes(row.get("dco_mode")):
        asset_count = sum(1 for c in (["image_url"] + _EXTRA_ASSET_COLS) if str(row.get(c, "")).strip())
        if asset_count < 2:
            errors.append(
                f"dco_mode=yes erfordert mindestens 2 Asset-Links (image_url + image_url_2..10), gefunden: {asset_count}"
            )
    return errors


def read_adsets(sheet: gspread.Spreadsheet) -> tuple[list[AdSetRow], list[AdSetRow], list[tuple[int, str]]]:
    """
    Gibt zurück: (offene Zeilen, bereits erledigte Zeilen, Fehler-Zeilen)
    Erledigte Zeilen (DONE) werden mitgeliefert damit ihre Ad Set IDs
    für das Erstellen von Ads genutzt werden können.
    """
    ws = sheet.worksheet("ad_sets")
    records = with_backoff(ws.get_all_records)
    rows, done_rows, errors = [], [], []

    for i, row in enumerate(records, start=2):  # Zeile 1 = Header
        status = str(row.get("status", "")).strip().upper()
        if status == "ERROR":
            continue
        if status == "DONE":
            # Erledigte Zeile: direkt in done_rows ohne Validierung
            done_rows.append(AdSetRow(
                row_index=i,
                source_campaign_id=str(row.get("source_campaign_id", "")).strip(),
                ad_set_name=str(row.get("ad_set_name", "")).strip(),
                daily_budget=0,
                start_time="",
                end_time=None,
                targeting_override=None,
                page_id=str(row.get("page_id", "")).strip() or None,
                source_adset_id=str(row.get("created_id", "")).strip() or None,
                bid_amount=int(row["bid_amount"]) if row.get("bid_amount") else None,
                dco_mode=_is_yes(row.get("dco_mode")),
                raw=row,
            ))
            continue

        errs = _validate_adset(row, i)
        if errs:
            errors.append((i, "; ".join(errs)))
            continue
        targeting = None
        if row.get("targeting_override"):
            targeting = json.loads(row["targeting_override"])
        rows.append(AdSetRow(
            row_index=i,
            source_campaign_id=str(row["source_campaign_id"]).strip(),
            ad_set_name=str(row["ad_set_name"]).strip(),
            daily_budget=int(row["daily_budget"]) if row.get("daily_budget") else 0,
            start_time=str(row["start_time"]).strip(),
            end_time=str(row["end_time"]).strip() or None if row.get("end_time") else None,
            targeting_override=targeting,
            page_id=str(row.get("page_id", "")).strip() or None,
            source_adset_id=str(row.get("source_adset_id", "")).strip() or None,
            bid_amount=int(row["bid_amount"]) if row.get("bid_amount") else None,
            dco_mode=_is_yes(row.get("dco_mode")),
            raw=row,
        ))
    return rows, done_rows, errors


def read_ads(sheet: gspread.Spreadsheet) -> tuple[list[AdRow], list[tuple[int, str]]]:
    ws = sheet.worksheet("ads")
    records = with_backoff(ws.get_all_records)
    rows, errors = [], []

    for i, row in enumerate(records, start=2):
        status = str(row.get("status", "")).strip().upper()
        if status in ("DONE", "ERROR"):
            continue
        errs = _validate_ad(row, i)
        if errs:
            errors.append((i, "; ".join(errs)))
            continue

        bodies = _collect_variations(row, _BODY_COLS)
        headlines = _collect_variations(row, _HEADLINE_COLS)
        descriptions = _collect_variations(row, _DESC_COLS)

        # 3-2-2: image_url ist Asset-Slot 1, image_url_2..10 sind die weiteren.
        # Reihenfolge bleibt erhalten — nur nicht-leere Strings werden gesammelt.
        primary = str(row.get("image_url", "")).strip()
        asset_urls: list[str] = []
        if primary:
            asset_urls.append(primary)
        for col in _EXTRA_ASSET_COLS:
            val = str(row.get(col, "")).strip()
            if val:
                asset_urls.append(val)

        rows.append(AdRow(
            row_index=i,
            ad_set_ref=str(row["ad_set_ref"]).strip(),
            ad_name=str(row["ad_name"]).strip(),
            image_url=str(row.get("image_url", "")).strip() or None,
            image_url_2=str(row.get("image_url_2", "")).strip() or None,
            image_hash=str(row.get("image_hash", "")).strip() or None,
            video_id=str(row.get("video_id", "")).strip() or None,
            thumbnail_url=str(row.get("thumbnail_url", "")).strip() or None,
            asset_urls=asset_urls,
            dco_mode=_is_yes(row.get("dco_mode")),
            bodies=bodies,
            headlines=headlines,
            descriptions=descriptions,
            cta=str(row.get("cta", "")).strip(),
            destination_url=str(row.get("destination_url", "")).strip(),
            display_link=str(row.get("display_link", "")).strip() or None,
            instagram_actor_id=str(row.get("instagram_actor_id", "")).strip() or None,
            page_id=str(row.get("page_id", "")).strip() or None,
            advertiser_id=str(row.get("advertiser_id", "")).strip() or None,
            url_tags=str(row.get("url_tags", "")).strip() or None,
            raw=row,
        ))
    return rows, errors


import time as _time

# Worksheet- und Header-Cache: vermeidet wiederholte Read-Requests pro Session
_ws_cache: dict[str, gspread.Worksheet] = {}
_header_cache: dict[str, dict] = {}  # tab → {status_col, id_col}


def _get_ws(sheet: gspread.Spreadsheet, tab: str) -> gspread.Worksheet:
    key = f"{sheet.id}:{tab}"
    if key not in _ws_cache:
        delay = 10
        for attempt in range(6):
            try:
                _ws_cache[key] = sheet.worksheet(tab)
                break
            except Exception as e:
                if ("429" in str(e) or "Quota" in str(e)) and attempt < 5:
                    logger.warning("Sheets Rate Limit beim Laden von '%s' — warte %ds …", tab, delay)
                    _time.sleep(delay)
                    delay = min(delay * 2, 120)
                else:
                    raise
    return _ws_cache[key]


def _get_cols(sheet: gspread.Spreadsheet, tab: str) -> tuple[int, int | None]:
    """Gibt (status_col, id_col) zurück — gecacht pro Tab."""
    key = f"{sheet.id}:{tab}"
    if key not in _header_cache:
        ws = _get_ws(sheet, tab)
        headers = with_backoff(lambda: ws.row_values(1))
        _header_cache[key] = {
            "status_col": headers.index("status") + 1,
            "id_col": headers.index("created_id") + 1 if "created_id" in headers else None,
        }
    c = _header_cache[key]
    return c["status_col"], c["id_col"]


def with_backoff(fn, max_retries: int = 6):
    """Führt fn() aus — bei 429 Rate Limit mit exponentiellem Backoff wiederholen.

    Gibt das Ergebnis von fn() zurück (für Read-Calls wichtig).
    """
    delay = 10
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            if "429" in str(e) or "Quota" in str(e) or "quota" in str(e):
                if attempt < max_retries - 1:
                    logger.warning("Google Sheets Rate Limit — warte %ds (Versuch %d/%d) …",
                                   delay, attempt + 1, max_retries)
                    _time.sleep(delay)
                    delay = min(delay * 2, 120)  # max 120s
                else:
                    raise
            else:
                raise


# Alias für Backwards-Kompatibilität (intern verwendet)
_with_backoff = with_backoff


def write_status(sheet: gspread.Spreadsheet, tab: str, row_index: int, status: str, created_id: str = "") -> None:
    def _do():
        ws = _get_ws(sheet, tab)
        status_col, id_col = _get_cols(sheet, tab)
        updates = [{"range": f"{gspread.utils.rowcol_to_a1(row_index, status_col)}", "values": [[status]]}]
        if id_col and created_id:
            updates.append({"range": f"{gspread.utils.rowcol_to_a1(row_index, id_col)}", "values": [[created_id]]})
        ws.batch_update(updates)

    _with_backoff(_do)


def append_log(sheet: gspread.Spreadsheet, timestamp: str, action: str, entity_id: str, result: str) -> None:
    try:
        def _do():
            ws = _get_ws(sheet, "log")
            ws.append_row([timestamp, action, entity_id, result])
        _with_backoff(_do)
    except Exception:
        pass
