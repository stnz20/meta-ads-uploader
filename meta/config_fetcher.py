from __future__ import annotations
import logging

import gspread
from facebook_business.adobjects.adaccount import AdAccount

import config

logger = logging.getLogger(__name__)

META_CONFIG_TAB = "meta_config"
_HEADER = ["type", "id", "name"]


def _get_pages(account: AdAccount) -> list[dict]:
    try:
        pages = account.get_assigned_pages(fields=["id", "name"])
        return [{"type": "page", "id": p["id"], "name": p.get("name", "")} for p in pages]
    except Exception as e:
        logger.warning("Pages konnten nicht geladen werden: %s", e)
        return []


def _get_instagram_accounts(account: AdAccount) -> list[dict]:
    try:
        accounts = account.get_instagram_accounts(fields=["id", "name", "username"])
        result = []
        for a in accounts:
            name = a.get("name") or a.get("username") or ""
            result.append({"type": "instagram", "id": a["id"], "name": name})
        return result
    except Exception as e:
        logger.warning("Instagram-Konten konnten nicht geladen werden: %s", e)
        return []


def _get_advertisers(account: AdAccount) -> list[dict]:
    """Advertiser = Pages (in Standard-Setups ist die Page der Advertiser im Identity-Bereich)."""
    try:
        pages = account.get_assigned_pages(fields=["id", "name"])
        return [{"type": "advertiser", "id": p["id"], "name": p.get("name", "")} for p in pages]
    except Exception as e:
        logger.warning("Advertiser konnten nicht geladen werden: %s", e)
        return []


def fetch_and_write_meta_config(sheet: gspread.Spreadsheet, account: AdAccount) -> None:
    logger.info("Lade Meta Config (Pages, Instagram, Advertiser) …")

    entries = []
    entries.extend(_get_pages(account))
    entries.extend(_get_instagram_accounts(account))
    entries.extend(_get_advertisers(account))

    if not entries:
        logger.error("Keine Einträge gefunden — Config-Tab wird nicht aktualisiert.")
        return

    try:
        ws = sheet.worksheet(META_CONFIG_TAB)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=META_CONFIG_TAB, rows=200, cols=3)

    rows = [_HEADER] + [[e["type"], e["id"], e["name"]] for e in entries]
    ws.update("A1", rows)
    ws.freeze(rows=1)

    logger.info("Meta Config geschrieben: %d Einträge in Tab '%s'", len(entries), META_CONFIG_TAB)
    logger.info("  • %d Pages", sum(1 for e in entries if e["type"] == "page"))
    logger.info("  • %d Instagram-Konten", sum(1 for e in entries if e["type"] == "instagram"))
    logger.info("  • %d Advertiser", sum(1 for e in entries if e["type"] == "advertiser"))
    logger.info("Jetzt: 'Tabelle aktualisieren' im Batch-Upload Tab ausführen.")
