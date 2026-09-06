"""Bulk-fill 'Output Ad Naming' (Spalte K im 'new naming convention' Tab).

Findet alle Zeilen, in denen die 6 Pflicht-Quellspalten (MetaAd-ID, Format,
Product, Description, Creator, Version) befüllt sind, aber 'Output Ad Naming'
leer ist. Befüllt diese mit `build_ad_name()` — ohne LP-Suffix.

Header werden DYNAMISCH über Spalten-Namen aufgelöst (nicht über Buchstaben),
sodass das Skript robust gegen Layout-Verschiebungen ist.

Usage:
    python3 -m ad_namer.fill_naming           # Dry-Run (default)
    python3 -m ad_namer.fill_naming --write   # Tatsächlich schreiben
"""
from __future__ import annotations

import argparse
import logging

from dotenv import load_dotenv
load_dotenv()

import gspread

import config
from auth.sheets_auth import get_sheet_client
from ad_namer.sheet_ops import get_naming_worksheet
from ad_namer.naming import build_ad_name
from sheets.reader import with_backoff

logger = logging.getLogger(__name__)

# Welche Header-Strings akzeptiert werden (erste Übereinstimmung gewinnt)
_HEADER_ALIASES: dict[str, list[str]] = {
    "meta_id":     ["NUMBER / IDENTIFIER", "Number / Identifier", "MetaAd-ID", "ID"],
    "fmt":         ["Format"],
    "product":     ["Product", "Produkt"],
    "description": ["Description in max. 3 Words", "Description"],
    "creator":     ["CREATOR", "Creator"],
    "version":     ["HOOK / VERSION", "Version", "HOOK"],
    "ad_name_out": ["Output Ad Naming →", "Output Ad Naming", "Output Ad"],
}

_HEADER_SCAN_ROWS = 30


def _find_header_row(ws: gspread.Worksheet) -> tuple[int, dict[str, int]]:
    """Findet die Header-Zeile (über 'Output Ad Naming') und mappt alle Felder.

    Returns: (header_row_1based, {field → 0-basierter Col-Index})
    """
    rng = with_backoff(lambda: ws.get(f"A1:Z{_HEADER_SCAN_ROWS}"))
    for i, row in enumerate(rng):
        cells = [str(c or "").strip() for c in row]
        if any(alias in cells for alias in _HEADER_ALIASES["ad_name_out"]):
            mapping: dict[str, int] = {}
            for field, aliases in _HEADER_ALIASES.items():
                for a in aliases:
                    if a in cells:
                        mapping[field] = cells.index(a)
                        break
                else:
                    raise ValueError(
                        f"Header für '{field}' nicht gefunden. Erwartete Aliases: {aliases}"
                    )
            return i + 1, mapping
    raise ValueError(
        f"Header-Zeile mit 'Output Ad Naming' nicht in den ersten {_HEADER_SCAN_ROWS} Zeilen gefunden."
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bulk-fill 'Output Ad Naming' im Naming-Convention-Sheet (ohne LP-Suffix)."
    )
    parser.add_argument("--write", action="store_true",
                        help="Tatsächlich ins Sheet schreiben (Default: nur Dry-Run).")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    sheet = get_sheet_client()
    ws = get_naming_worksheet(sheet, config.NAMING_SHEET_GID)
    logger.info("Sheet geöffnet: '%s' / Tab '%s' (GID %s)",
                sheet.title, ws.title, ws.id)

    header_row, col_map = _find_header_row(ws)
    col_letters = {
        k: gspread.utils.rowcol_to_a1(1, v + 1).rstrip("1")
        for k, v in col_map.items()
    }
    logger.info("Header in Zeile %d. Spalten-Mapping: %s", header_row, col_letters)

    data = with_backoff(lambda: ws.get(f"A{header_row + 1}:Z"))

    fills: list[dict] = []
    for i, values in enumerate(data):
        def _v(field: str) -> str:
            idx = col_map[field]
            if idx >= len(values):
                return ""
            return str(values[idx] or "").strip()

        meta_id = _v("meta_id")
        if not meta_id or not meta_id.startswith("MetaAd-"):
            continue

        if _v("ad_name_out"):
            continue  # schon befüllt

        fmt = _v("fmt")
        product = _v("product")
        description = _v("description")
        creator = _v("creator")
        version = _v("version")

        missing = [name for name, val in [
            ("Format", fmt),
            ("Product", product),
            ("Description", description),
            ("Creator", creator),
            ("Version", version),
        ] if not val]
        if missing:
            logger.debug("Zeile %d (%s) übersprungen — fehlt: %s",
                         header_row + 1 + i, meta_id, ", ".join(missing))
            continue

        ad_name = build_ad_name(meta_id, fmt, product, description, creator, version)
        sheet_row = header_row + 1 + i
        fills.append({"row": sheet_row, "meta_id": meta_id, "value": ad_name})
        logger.info("  Zeile %d: %s → %s", sheet_row, meta_id, ad_name)

    if not fills:
        logger.info("Keine leeren Output-Ad-Naming-Zellen gefunden, die alle Pflichtfelder haben.")
        return 0

    logger.info("─── %d Zeilen zum Befüllen ───", len(fills))

    if not args.write:
        logger.info("DRY-RUN — nichts geschrieben. Mit --write tatsächlich ausführen.")
        return 0

    col_letter = col_letters["ad_name_out"]
    batch = [
        {"range": f"{col_letter}{f['row']}", "values": [[f["value"]]]}
        for f in fills
    ]
    with_backoff(lambda: ws.batch_update(batch, value_input_option="USER_ENTERED"))
    logger.info("✅ %d Zellen geschrieben in Spalte %s", len(fills), col_letter)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
