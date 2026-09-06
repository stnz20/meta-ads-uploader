"""Bridge zwischen Naming-Convention-Tab und ad_sets/ads-Tabs.

Portiert die Kernlogik aus BatchUpload.gs (generateRows / _getReadyRows /
_lookupRef / _doneAdSetNames) nach Python, damit der Streamlit-Flow die
Vorbereitung selbst übernehmen kann — kein manueller Apps-Script-Aufruf mehr.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional

import gspread

from sheets.reader import with_backoff

logger = logging.getLogger(__name__)

ADSETS_TAB = "ad_sets"
ADS_TAB = "ads"

_NAMING_HEADER_MARKER = "Ready for upload?"
_HEADER_SCAN_ROWS = 30


# ──────────────────────────────────────────────────────────────────────────────
# Naming Convention lesen
# ──────────────────────────────────────────────────────────────────────────────

def _read_naming_header(ws: gspread.Worksheet) -> tuple[dict[str, int], int]:
    """Erkennt die Header-Zeile automatisch über das Marker-Feld 'Ready for upload?'.

    Returns: (col_map: {header → 0-basierter Index}, header_sheet_row 1-basiert)
    """
    rng = with_backoff(lambda: ws.get(f"A1:Z{_HEADER_SCAN_ROWS}"))
    for i, row in enumerate(rng):
        if any(str(c or "").strip() == _NAMING_HEADER_MARKER for c in row):
            col_map = {str(h or "").strip(): idx for idx, h in enumerate(row) if str(h or "").strip()}
            return col_map, i + 1
    raise ValueError(f"Header-Zeile mit '{_NAMING_HEADER_MARKER}' nicht in den ersten {_HEADER_SCAN_ROWS} Zeilen gefunden.")


def read_ready_rows(naming_ws: gspread.Worksheet) -> list[dict]:
    """Liefert alle Naming-Convention-Zeilen mit Ready=yes und Uploaded? leer.

    Wendet die Versions-Normalisierung an: Zeilen, die zum selben Concept gehören
    (gleicher Output-AdSet-Name nach Entfernen von '_MetaAd-{id}_'), werden zur
    selben Ad-Set-Zeile zusammengefasst — alle bekommen den AdSet-Namen der V1
    (kleinste MetaAd-ID) zugewiesen.
    """
    col_map, header_row = _read_naming_header(naming_ws)
    data = with_backoff(lambda: naming_ws.get(f"A{header_row + 1}:Z"))

    rc = col_map.get("Ready for upload?")
    uc = col_map.get("Uploaded?")
    asc = col_map.get("Output Ad Set Naming →")
    anc = col_map.get("Output Ad Naming →")
    alc = col_map.get("Asset Link")
    luc = col_map.get("LP URL")
    lic = col_map.get("LP ID")
    # 3-2-2 Override-Spalten (optional — leer = Default aus Referenz-Ad-Set via apply_defaults)
    b1c = col_map.get("Body 1 Override")
    b2c = col_map.get("Body 2 Override")
    h1c = col_map.get("Headline 1 Override")
    h2c = col_map.get("Headline 2 Override")

    if rc is None or asc is None or anc is None:
        raise ValueError("Pflicht-Spalten fehlen im Naming-Tab: Ready / Output Ad Set / Output Ad Naming")

    def _v(values: list[str], idx: Optional[int]) -> str:
        if idx is None or idx >= len(values):
            return ""
        return str(values[idx] or "").strip()

    rows: list[dict] = []
    for i, values in enumerate(data):
        ready = _v(values, rc).lower() == "yes"
        # Uploaded?-Dropdown: Live/Queued = schon behandelt (überspringen);
        # leer oder "Not Yet" = noch upload-fähig.
        already = _v(values, uc).strip().lower() in ("live", "queued")
        ad_set_name = _v(values, asc)
        ad_name = _v(values, anc)
        if not (ready and not already and ad_set_name and ad_name):
            continue
        rows.append({
            "adSetName": ad_set_name,
            "adName": ad_name,
            "assetLink": _v(values, alc),
            "lpUrl": _v(values, luc),
            "lpId": _v(values, lic),
            "body1Override": _v(values, b1c),
            "body2Override": _v(values, b2c),
            "headline1Override": _v(values, h1c),
            "headline2Override": _v(values, h2c),
            "sheetRow": header_row + 1 + i,
            "uploadedCol1Based": (uc + 1) if uc is not None else None,
        })

    _normalize_versions(rows)
    return rows


def _normalize_versions(rows: list[dict]) -> None:
    """Gruppiert nach Concept (AdSet-Name ohne MetaAd-ID) und weist allen Versionen
    den AdSet-Namen der V1 (kleinste MetaAd-ID) zu — in-place mutation."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        key = re.sub(r"_MetaAd-\d+_", "_", r["adSetName"])
        groups.setdefault(key, []).append(r)

    for group in groups.values():
        if len(group) <= 1:
            continue

        def _meta_id(r: dict) -> int:
            m = re.search(r"MetaAd-(\d+)", r["adSetName"])
            return int(m.group(1)) if m else 0

        group.sort(key=_meta_id)
        v1_name = group[0]["adSetName"]
        for r in group[1:]:
            r["adSetName"] = v1_name


# ──────────────────────────────────────────────────────────────────────────────
# Referenz-Lookup (Campaign / Page / IG / CTA aus bestehender Ad Set ID)
# ──────────────────────────────────────────────────────────────────────────────

def _all_records_safe(ws: gspread.Worksheet) -> list[dict]:
    """get_all_records mit Duplicate-Header-Fallback (frühe Header-Zeilen können Leerzellen haben).

    Reads sind mit Backoff/Retry für 429 Rate Limits geschützt.
    """
    try:
        return with_backoff(ws.get_all_records)
    except gspread.exceptions.GSpreadException:
        headers = with_backoff(lambda: ws.row_values(1))
        return with_backoff(lambda: ws.get_all_records(expected_headers=headers))


def lookup_reference(
    sheet: gspread.Spreadsheet,
    ref_adset_id: str,
    *,
    ad_sets_records: Optional[list[dict]] = None,
    ads_records: Optional[list[dict]] = None,
) -> dict:
    """Sucht eine Referenz-Ad-Set-ID in ad_sets (source_adset_id oder created_id)
    und holt zugehörige Page/IG/CTA aus der ersten passenden Zeile in ads.

    Liefert {"campId", "cta", "srcId", "pageId", "igId"}. Wenn die ID nicht im
    Sheet ist, wird sie trotzdem als srcId zurückgegeben — campId bleibt leer
    (muss dann manuell ergänzt werden).

    `ad_sets_records` / `ads_records` können vorgeladen übergeben werden, um
    redundante API-Reads zu vermeiden (wichtig bei Batch-Aufrufen).
    """
    if not ref_adset_id:
        return {"campId": "", "cta": "", "srcId": "", "pageId": "", "igId": ""}

    if ad_sets_records is None:
        ad_sets_records = _all_records_safe(sheet.worksheet(ADSETS_TAB))

    ad_set_row = None
    for r in ad_sets_records:
        src = str(r.get("source_adset_id", "")).strip()
        cre = str(r.get("created_id", "")).strip()
        if src == ref_adset_id or cre == ref_adset_id:
            ad_set_row = r
            break
    if not ad_set_row:
        return {"campId": "", "cta": "", "srcId": ref_adset_id, "pageId": "", "igId": ""}

    ad_set_name = str(ad_set_row.get("ad_set_name", "")).strip()
    ad_row = None
    if ad_set_name:
        if ads_records is None:
            ads_records = _all_records_safe(sheet.worksheet(ADS_TAB))
        for r in ads_records:
            if str(r.get("ad_set_ref", "")).strip() == ad_set_name:
                ad_row = r
                break

    return {
        "campId": str(ad_set_row.get("source_campaign_id", "")).strip(),
        "srcId": ref_adset_id,
        "pageId": str(ad_set_row.get("page_id", "")).strip(),
        "cta": str((ad_row or {}).get("cta", "")).strip(),
        "igId": str((ad_row or {}).get("instagram_actor_id", "")).strip(),
    }


def done_ad_set_options(
    sheet: gspread.Spreadsheet,
    *,
    ad_sets_records: Optional[list[dict]] = None,
) -> list[tuple[str, str]]:
    """Alle bereits erfolgreich hochgeladenen Ad Sets als (id, label).

    Label-Format: 'source_adset_id — ad_set_name'. Neueste zuerst.
    """
    if ad_sets_records is None:
        ad_sets_records = _all_records_safe(sheet.worksheet(ADSETS_TAB))
    records = ad_sets_records
    out: list[tuple[str, str]] = []
    for r in records:
        status = str(r.get("status", "")).strip().upper()
        if status != "DONE":
            continue
        # Bei DONE-Zeilen liegt die echte Meta-Ad-Set-ID in created_id;
        # source_adset_id ist die Referenz, mit der diese Zeile erstellt wurde.
        src = str(r.get("created_id", "")).strip() or str(r.get("source_adset_id", "")).strip()
        if not src:
            continue
        name = str(r.get("ad_set_name", "")).strip()
        label = f"{src} — {name}" if name else src
        out.append((src, label))
    # Neueste zuerst (untere Zeilen im Sheet)
    out.reverse()
    # Dedup, erste Vorkommen behalten
    seen, dedup = set(), []
    for src, label in out:
        if src in seen:
            continue
        seen.add(src)
        dedup.append((src, label))
    return dedup


# ──────────────────────────────────────────────────────────────────────────────
# Smart Default: zuletzt genutzte Referenz pro Produkt vorschlagen
# ──────────────────────────────────────────────────────────────────────────────

_PRODUCT_RE = re.compile(r"_MetaAd-\d+_[A-Z]+-[0-9A-Zx]+_([^_]+)_")


def _product_from_adset_name(ad_set_name: str) -> str:
    """Extrahiert das Produkt-Token aus dem AdSet-Namen.

    Format: '{Date}_MetaAd-{ID}_{Format}_{Product}_{Description}_{Creator}_{LP-ID}'
    Produkt = Segment direkt hinter dem Format (2 Segmente nach der MetaAd-ID).
    Funktioniert auch für Formate ohne Bindestrich (z. B. 'STA').
    Beispiel: '2025-05-18_MetaAd-1710_VID-30_NatureProtect_Ad17_DoDont_Aditor_Meta-Offer-TL-NP'
              → 'NatureProtect'
    """
    parts = ad_set_name.split("_")
    for i, p in enumerate(parts):
        if re.match(r"MetaAd-\d+$", p) and len(parts) > i + 2:
            return parts[i + 2]
    m = _PRODUCT_RE.search(ad_set_name)
    return m.group(1) if m else ""


def _lp_id_from_adset_name(name: str) -> str:
    """LP-ID = letztes Unterstrich-Segment des Ad-Set-Namens.

    Format: '{Date}_{MetaAd-ID}_{Format}_{Product}_{Description}_{Creator}_{LP-ID}'
    LP-IDs nutzen Bindestriche (z. B. 'Meta-Offer-TL-NP'), daher liefert das
    letzte '_'-Segment die vollständige LP-ID. Achtung: ältere Ad-Set-Namen enden
    teils ohne LP-ID (z. B. mit Creator) — daher nur als Fallback verwenden.
    """
    return name.rsplit("_", 1)[-1] if name else ""


def _norm_url(url: str) -> str:
    """Normalisiert eine URL für robusten Vergleich: ohne Schema/www/Query/Fragment,
    lowercase, ohne trailing slash."""
    u = str(url or "").strip().lower().split("?")[0].split("#")[0]
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
            break
    if u.startswith("www."):
        u = u[4:]
    return u.rstrip("/")


def suggest_references(
    sheet: gspread.Spreadsheet,
    ready_ad_set_names: list[str],
    *,
    lp_url_by_name: Optional[dict[str, str]] = None,
    lp_by_name: Optional[dict[str, str]] = None,
    ad_sets_records: Optional[list[dict]] = None,
    ads_records: Optional[list[dict]] = None,
    purchases_by_adset: Optional[dict[str, int]] = None,
) -> dict[str, str]:
    """Pro Ad-Set-Namen: schlage das **best-performende** DONE-Ad-Set desselben
    **Produkts** aus der ABO-Testing-Kampagne als Referenz vor. Liefert
    {adSetName → src_adset_id} (leerer String wenn nichts passt).

    Priorität der Auflösung:
      1. **Beste Performance je Produkt** — Ad-Set mit den meisten Käufen (aus
         `purchases_by_adset`, {created_id → purchases}), nur unter DONE-Sheet-Zeilen,
         damit die ID im Referenz-Dropdown wählbar ist. Nur Ad-Sets mit >0 Käufen.
      2. **Zuletzt hochgeladenes** DONE-Ad-Set desselben Produkts (untere Sheet-Zeile = neuer).
      3. **Gleiche LP-URL** (Spalte I ↔ `destination_url`), dann **LP-ID** (Spalte J / Namens-Suffix).

    `purchases_by_adset` kommt aus `meta.reference_fetcher.fetch_adset_purchases`
    (Meta Insights der Testing-Kampagne). Fehlt es/ist leer, greift ab Stufe 2.
    `lp_url_by_name`/`lp_by_name` liefern die Ready-Werte; Referenzen aus dem Sheet.
    """
    if ad_sets_records is None:
        ad_sets_records = _all_records_safe(sheet.worksheet(ADSETS_TAB))
    if ads_records is None:
        ads_records = _all_records_safe(sheet.worksheet(ADS_TAB))

    # destination_url je Ad-Set-Name (erste passende Ad gewinnt)
    url_by_adset_name: dict[str, str] = {}
    for r in ads_records:
        ref = str(r.get("ad_set_ref", "")).strip()
        du = str(r.get("destination_url", "")).strip()
        if ref and du and ref not in url_by_adset_name:
            url_by_adset_name[ref] = du

    purchases_by_adset = purchases_by_adset or {}

    # Referenz-Kandidaten je Produkt aufbauen:
    #  - best_by_product : DONE-Ad-Set mit den meisten Käufen (>0) je Produkt
    #  - latest_by_product/url/lp : Fallbacks (untere Sheet-Zeile = neuer → gewinnt)
    best_by_product: dict[str, str] = {}
    best_purch_by_product: dict[str, int] = {}
    latest_by_product: dict[str, str] = {}
    latest_by_url: dict[str, str] = {}
    latest_by_lp: dict[str, str] = {}
    for r in ad_sets_records:
        if str(r.get("status", "")).strip().upper() != "DONE":
            continue
        created_id = str(r.get("created_id", "")).strip()
        src = created_id or str(r.get("source_adset_id", "")).strip()
        if not src:
            continue
        name = str(r.get("ad_set_name", "")).strip()
        prod = _product_from_adset_name(name)
        if prod:
            latest_by_product[prod] = src
            # Best-Performer: nur echte, in Meta erstellte Ad-Sets mit Käufen zählen
            purch = purchases_by_adset.get(created_id) if created_id else None
            if purch and purch > best_purch_by_product.get(prod, 0):
                best_purch_by_product[prod] = purch
                best_by_product[prod] = created_id
        du = url_by_adset_name.get(name, "")
        if du:
            latest_by_url[_norm_url(du)] = src
        lp_id = _lp_id_from_adset_name(name)
        if lp_id:
            latest_by_lp[lp_id] = src

    lp_url_by_name = lp_url_by_name or {}
    lp_by_name = lp_by_name or {}
    suggestions: dict[str, str] = {}
    for name in ready_ad_set_names:
        prod = _product_from_adset_name(name)
        # 1. Best-performendes Testing-Ad-Set desselben Produkts
        src = best_by_product.get(prod, "") if prod else ""
        # 2. Zuletzt hochgeladenes DONE-Ad-Set desselben Produkts
        if not src:
            src = latest_by_product.get(prod, "") if prod else ""
        # 3. Gleiche LP-URL, dann LP-ID (neue Produkte ohne Historie)
        if not src:
            url = _norm_url(lp_url_by_name.get(name, ""))
            src = latest_by_url.get(url, "") if url else ""
        if not src:
            lp_id = str(lp_by_name.get(name, "")).strip() or _lp_id_from_adset_name(name)
            src = latest_by_lp.get(lp_id, "") if lp_id else ""
        suggestions[name] = src
    return suggestions


# ──────────────────────────────────────────────────────────────────────────────
# Zeilen in ad_sets / ads schreiben
# ──────────────────────────────────────────────────────────────────────────────

def _headers(ws: gspread.Worksheet) -> list[str]:
    return [str(h or "").strip() for h in with_backoff(lambda: ws.row_values(1))]


def _ensure_headers(ws: gspread.Worksheet, required: list[str]) -> list[str]:
    """Hängt fehlende Spalten an die Header-Zeile an und gibt die neue Header-Liste zurück.

    Verhindert dass _make_row Werte stillschweigend verwirft, weil die Spalte
    im Sheet noch nicht existiert (z.B. nach Feature-Rollout).
    """
    current = _headers(ws)
    missing = [c for c in required if c not in current]
    if not missing:
        return current
    new_headers = current + missing
    # Sheet ggf. um Spalten erweitern, dann Header-Zeile schreiben
    if ws.col_count < len(new_headers):
        with_backoff(lambda: ws.add_cols(len(new_headers) - ws.col_count))
    end_col = gspread.utils.rowcol_to_a1(1, len(new_headers))
    with_backoff(lambda: ws.update(f"A1:{end_col}", [new_headers]))
    logger.info("Header in '%s' erweitert um: %s", ws.title, ", ".join(missing))
    return new_headers


def _make_row(headers: list[str], data: dict) -> list:
    return [data.get(h, "") for h in headers]


def _today_iso_datetime(date_str: str) -> str:
    """Akzeptiert 'YYYY-MM-DD' oder volles ISO; gibt ISO mit T10:00:00 zurück."""
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", date_str):
        return date_str
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return f"{date_str}T10:00:00"
    return date_str


def generate_upload_rows(
    sheet: gspread.Spreadsheet,
    naming_ws: gspread.Worksheet,
    *,
    ready_rows: list[dict],
    references: dict[str, dict],
    start_date: str,
) -> dict:
    """Schreibt Zeilen in ad_sets/ads und markiert Naming-Zeilen als "Queued".

    Args:
        sheet: gspread Spreadsheet.
        naming_ws: Naming-Convention-Worksheet.
        ready_rows: Output von read_ready_rows().
        references: {adSetName → {refAdSet, advertiser, pageOverride, igOverride, displayLink, campIdOverride?}}.
        start_date: 'YYYY-MM-DD' für start_time aller Ad Sets.

    Returns: {"ad_sets_written": int, "ads_written": int, "skipped": list[str], "errors": list[str]}
    """
    from sheets.reader import ADSET_COLS_ORDERED, ADS_COLS_ORDERED

    ad_sets_ws = sheet.worksheet(ADSETS_TAB)
    ads_ws = sheet.worksheet(ADS_TAB)
    ad_sets_hdrs = _ensure_headers(ad_sets_ws, ADSET_COLS_ORDERED)
    ads_hdrs = _ensure_headers(ads_ws, ADS_COLS_ORDERED)
    start_time = _today_iso_datetime(start_date)

    # Records einmalig laden — alle Referenz-Lookups arbeiten danach in-memory,
    # sonst N × 2 Sheets-Read-Calls in der Schleife (Quota-Killer).
    ad_sets_records = _all_records_safe(ad_sets_ws)
    ads_records = _all_records_safe(ads_ws)

    # Gruppiere ready_rows nach adSetName
    groups: dict[str, list[dict]] = {}
    for r in ready_rows:
        groups.setdefault(r["adSetName"], []).append(r)

    errors: list[str] = []
    skipped: list[str] = []
    ad_set_rows_out: list[list] = []
    ad_rows_out: list[list] = []
    naming_queued_rows: list[int] = []  # 1-basierte Sheet-Zeilennummern

    uploaded_col_1based: Optional[int] = None
    if ready_rows:
        uploaded_col_1based = ready_rows[0].get("uploadedCol1Based")

    for ad_set_name, ads_in_set in groups.items():
        ref_cfg = references.get(ad_set_name) or {}
        ref_id = str(ref_cfg.get("refAdSet", "")).strip()
        if not ref_id:
            skipped.append(f"{ad_set_name}: keine Referenz gewählt")
            continue

        ref = lookup_reference(
            sheet, ref_id,
            ad_sets_records=ad_sets_records,
            ads_records=ads_records,
        )
        from_sheet = bool(ref["campId"])
        camp_id = ref["campId"] or str(ref_cfg.get("campIdOverride", "")).strip()

        if not camp_id:
            # Nicht im Sheet gefunden → beliebiges Meta-Ad-Set direkt von der Graph-API holen.
            from meta.reference_fetcher import fetch_adset_reference
            meta_ref = fetch_adset_reference(ref_id)
            if meta_ref["campId"]:
                # Sheet-Werte (falls vorhanden) behalten Vorrang, leere mit Meta auffüllen
                for k, v in meta_ref.items():
                    if v and not ref.get(k):
                        ref[k] = v
                camp_id = ref["campId"]

        if not camp_id:
            errors.append(f"{ad_set_name}: Kampagnen-ID konnte aus Referenz '{ref_id}' nicht ermittelt werden")
            continue

        if from_sheet and not ref["cta"]:
            # Referenz war im Sheet bekannt, aber CTA-Spalte leer → das ist ein echter Datenfehler.
            # Bei Meta-Referenzen wird das CTA beim Upload über creative_defaults nachgefüllt.
            errors.append(f"{ad_set_name}: CTA fehlt für Referenz '{ref_id}' (in ads-Tab prüfen)")
            continue

        page_override = str(ref_cfg.get("pageOverride", "")).strip()
        ig_override = str(ref_cfg.get("igOverride", "")).strip()
        effective_page = page_override or ref["pageId"]
        effective_ig = ig_override or ref["igId"]
        advertiser = str(ref_cfg.get("advertiser", "")).strip()
        display_link = str(ref_cfg.get("displayLink", "")).strip()

        dco_mode = bool(ref_cfg.get("dcoMode"))

        ad_set_rows_out.append(_make_row(ad_sets_hdrs, {
            "source_campaign_id": camp_id,
            "ad_set_name": ad_set_name,
            "daily_budget": ref_cfg.get("dailyBudget", ""),
            "start_time": start_time,
            "end_time": "",
            "targeting_override": "",
            "page_id": effective_page,
            "source_adset_id": ref["srcId"],
            "dco_mode": "yes" if dco_mode else "",
            "status": "",
            "created_id": "",
        }))

        if dco_mode:
            # 3-2-2: alle gruppierten Naming-Zeilen → 1 ads-Zeile mit gebündelten Assets.
            # Override-Texte: erste nicht-leere Werte aus den gruppierten Zeilen.
            asset_links = [a["assetLink"] for a in ads_in_set if a.get("assetLink")]
            extra_slots = [
                "image_url_2", "image_url_3", "image_url_4", "image_url_5", "image_url_6",
                "image_url_7", "image_url_8", "image_url_9", "image_url_10",
            ]
            extras = {col: "" for col in extra_slots}
            for col, url in zip(extra_slots, asset_links[1:1 + len(extra_slots)]):
                extras[col] = url
            if len(asset_links) > 1 + len(extra_slots):
                logger.warning(
                    "Ad Set '%s': %d Assets gefunden — nur die ersten %d werden uploaded (Slot-Limit)",
                    ad_set_name, len(asset_links), 1 + len(extra_slots),
                )

            primary_ad = ads_in_set[0]
            # Override-Texte: erstes nicht-leeres Override aus den gruppierten Zeilen
            def _first_nonempty(key: str) -> str:
                for a in ads_in_set:
                    val = str(a.get(key, "")).strip()
                    if val:
                        return val
                return ""

            ad_rows_out.append(_make_row(ads_hdrs, {
                "ad_set_ref": primary_ad["adSetName"],
                "ad_name": primary_ad["adName"],
                "image_url": asset_links[0] if asset_links else "",
                **extras,
                "image_hash": "", "video_id": "", "thumbnail_url": "",
                "body":       _first_nonempty("body1Override"),
                "body_2":     _first_nonempty("body2Override"),
                "body_3": "", "body_4": "", "body_5": "",
                "headline":   _first_nonempty("headline1Override"),
                "headline_2": _first_nonempty("headline2Override"),
                "headline_3": "",
                "description": "", "description_2": "", "description_3": "",
                "cta": ref["cta"],
                "destination_url": primary_ad["lpUrl"],
                "display_link": display_link,
                "instagram_actor_id": effective_ig,
                "page_id": effective_page,
                "advertiser_id": advertiser,
                "dco_mode": "yes",
                "status": "",
                "created_id": "",
            }))
            # Alle gruppierten Naming-Zeilen werden als "Queued" markiert
            for ad in ads_in_set:
                naming_queued_rows.append(ad["sheetRow"])
            continue

        for ad in ads_in_set:
            ad_rows_out.append(_make_row(ads_hdrs, {
                "ad_set_ref": ad["adSetName"],
                "ad_name": ad["adName"],
                "image_url": ad["assetLink"],
                "image_url_2": "", "image_url_3": "", "image_url_4": "",
                "image_url_5": "", "image_url_6": "", "image_url_7": "",
                "image_url_8": "", "image_url_9": "", "image_url_10": "",
                "image_hash": "", "video_id": "", "thumbnail_url": "",
                "body": "", "body_2": "", "body_3": "", "body_4": "", "body_5": "",
                "headline": "", "headline_2": "", "headline_3": "",
                "description": "", "description_2": "", "description_3": "",
                "cta": ref["cta"],
                "destination_url": ad["lpUrl"],
                "display_link": display_link,
                "instagram_actor_id": effective_ig,
                "page_id": effective_page,
                "advertiser_id": advertiser,
                "dco_mode": "",
                "status": "",
                "created_id": "",
            }))
            naming_queued_rows.append(ad["sheetRow"])

    if errors:
        return {
            "ad_sets_written": 0, "ads_written": 0,
            "skipped": skipped, "errors": errors,
        }

    if ad_set_rows_out:
        ad_sets_ws.append_rows(ad_set_rows_out, value_input_option="USER_ENTERED")
    if ad_rows_out:
        ads_ws.append_rows(ad_rows_out, value_input_option="USER_ENTERED")

    # Naming-Zeilen auf "Queued" setzen (Batch) — gültige Dropdown-Option in Spalte O
    if naming_queued_rows and uploaded_col_1based:
        col_letter = gspread.utils.rowcol_to_a1(1, uploaded_col_1based).rstrip("1")
        batch = [
            {"range": f"{col_letter}{r}", "values": [["Queued"]]}
            for r in naming_queued_rows
        ]
        naming_ws.batch_update(batch, value_input_option="USER_ENTERED")

    return {
        "ad_sets_written": len(ad_set_rows_out),
        "ads_written": len(ad_rows_out),
        "skipped": skipped,
        "errors": [],
    }


# ──────────────────────────────────────────────────────────────────────────────
# Queued → Live (nach erfolgtem Meta-Upload)
# ──────────────────────────────────────────────────────────────────────────────

def read_queued_rows(naming_ws: gspread.Worksheet) -> tuple[list[dict], Optional[int], Optional[int]]:
    """Naming-Convention-Zeilen mit Uploaded? == "Queued".

    Returns: (rows, uploaded_col_1based, uploaded_when_col_1based)
    Jede row: {"adSetName" (versions-normalisiert), "adName", "sheetRow"}.
    """
    col_map, header_row = _read_naming_header(naming_ws)
    uc = col_map.get("Uploaded?")
    asc = col_map.get("Output Ad Set Naming →")
    anc = col_map.get("Output Ad Naming →")
    whenc = col_map.get("Uploaded When?")
    if uc is None or asc is None:
        return [], None, None

    data = with_backoff(lambda: naming_ws.get(f"A{header_row + 1}:Z"))

    def _v(values: list[str], idx: Optional[int]) -> str:
        if idx is None or idx >= len(values):
            return ""
        return str(values[idx] or "").strip()

    rows: list[dict] = []
    for i, values in enumerate(data):
        if _v(values, uc).upper() != "QUEUED":
            continue
        ad_set_name = _v(values, asc)
        if not ad_set_name:
            continue
        rows.append({
            "adSetName": ad_set_name,
            "adName": _v(values, anc),
            "sheetRow": header_row + 1 + i,
        })

    _normalize_versions(rows)
    return rows, uc + 1, (whenc + 1 if whenc is not None else None)


def mark_uploaded_done(sheet: gspread.Spreadsheet, naming_ws: gspread.Worksheet) -> int:
    """Setzt "Queued"-Naming-Zeilen auf "Live", sobald ihre eigene Ad DONE ist.

    "Live" ist eine gültige Option des strikten Uploaded?-Dropdowns (Live/Not Yet/Queued)
    — deshalb NICHT "DONE" schreiben. Pendant zur Apps-Script-Funktion markUploaded();
    wird nach dem Upload aus der Streamlit-App aufgerufen, damit der Naming-Tab nicht auf
    "Queued" hängen bleibt.

    Gematcht wird über den Ad-Namen, nicht über das Ad-Set: eine einzelne offene oder
    fehlerhafte Ad-Zeile darf nicht alle Naming-Zeilen ihres Ad-Sets blockieren.
    Nur für Naming-Zeilen ohne eigene ads-Zeile (3-2-2/DCO bündelt mehrere Zeilen zu
    einer Ad) greift der Ad-Set-Fallback.

    Returns: Anzahl der auf "Live" gesetzten Naming-Zeilen.
    """
    queued, uploaded_col_1based, when_col_1based = read_queued_rows(naming_ws)
    if not queued or not uploaded_col_1based:
        return 0

    ads_records = _all_records_safe(sheet.worksheet(ADS_TAB))
    status_by_set: dict[str, list[str]] = {}
    done_ad_names: set[str] = set()
    known_ad_names: set[str] = set()
    for r in ads_records:
        status = str(r.get("status", "")).strip().upper()
        ref = str(r.get("ad_set_ref", "")).strip()
        if ref:
            status_by_set.setdefault(ref, []).append(status)
        ad_name = str(r.get("ad_name", "")).strip()
        if ad_name:
            known_ad_names.add(ad_name)
            if status == "DONE":
                done_ad_names.add(ad_name)

    def _fully_done(name: str) -> bool:
        statuses = status_by_set.get(name)
        return bool(statuses) and all(s == "DONE" for s in statuses)

    def _is_live(row: dict) -> bool:
        ad_name = row.get("adName", "")
        if ad_name and ad_name in known_ad_names:
            return ad_name in done_ad_names
        return _fully_done(row["adSetName"])

    today = datetime.now().strftime("%Y-%m-%d")
    uploaded_letter = gspread.utils.rowcol_to_a1(1, uploaded_col_1based).rstrip("1")
    when_letter = (
        gspread.utils.rowcol_to_a1(1, when_col_1based).rstrip("1")
        if when_col_1based else None
    )

    batch: list[dict] = []
    count = 0
    for row in queued:
        if not _is_live(row):
            continue
        batch.append({"range": f"{uploaded_letter}{row['sheetRow']}", "values": [["Live"]]})
        if when_letter:
            batch.append({"range": f"{when_letter}{row['sheetRow']}", "values": [[today]]})
        count += 1

    if batch:
        naming_ws.batch_update(batch, value_input_option="USER_ENTERED")
    return count
