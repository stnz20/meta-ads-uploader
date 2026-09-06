"""Read/write operations on the Ad Naming Convention sheet tab."""
from __future__ import annotations
import re
from datetime import datetime
from itertools import zip_longest
import gspread

# Column letters matching the sheet header in row 5:
# A=empty | B=NUMBER/ID | C=Format | D=Product | E=Description | F=CREATOR | G=HOOK/VERSION
# H=empty | I=LP URL | J=LP ID | K=Output AdSet | L=Output Ad | M=Asset Link
# N=Ready? | O=Uploaded? | P=When? | Q=Performance | R=To Do | S=Datum
_COL_ID         = "B"
_COL_FORMAT     = "C"
_COL_PRODUCT    = "D"
_COL_DESC       = "E"
_COL_CREATOR    = "F"
_COL_VERSION    = "G"
_COL_LP_URL     = "I"
_COL_LP_ID      = "J"
_COL_ADSET_OUT  = "K"
_COL_AD_OUT     = "L"
_COL_ASSET_LINK = "M"
_COL_READY      = "N"
_COL_READY_DATE = "U"  # "Ready am" — Datum, an dem Ready=yes gesetzt wurde (DD.MM.YYYY)

# Full write range: columns A–S
_WRITE_RANGE_START = "A"
_WRITE_RANGE_END   = "S"
_NUM_COLS = 19  # A through S


def _col_letter_to_index(letter: str) -> int:
    """Converts a column letter (A=0, B=1, ...) to a 0-based index."""
    return ord(letter.upper()) - ord("A")


def get_naming_worksheet(spreadsheet: gspread.Spreadsheet, gid: int) -> gspread.Worksheet:
    for ws in spreadsheet.worksheets():
        if ws.id == gid:
            return ws
    raise ValueError(f"Worksheet with GID {gid} not found in spreadsheet.")


def get_next_start_id(ws: gspread.Worksheet) -> tuple[int, int]:
    """
    Finds the first MetaAd row that has an ID in col B but no Format in col C.
    Returns (meta_id_number, sheet_row_number) — the first free slot.
    Falls back to max_id+1 after the last MetaAd row if no free slot exists.
    Reads B and C together via ws.get() so they are guaranteed to be aligned.
    """
    # Read cols B and C together — always aligned, empty cells returned as ""
    bc = ws.get("B:C")  # list of [b_val, c_val] per row

    max_id = 0
    for i, row_vals in enumerate(bc, start=1):
        id_val = row_vals[0] if len(row_vals) > 0 else ""
        fmt_val = row_vals[1] if len(row_vals) > 1 else ""

        m = re.match(r"MetaAd-(\d+)", str(id_val).strip())
        if not m:
            continue
        num = int(m.group(1))
        max_id = max(max_id, num)

        if not str(fmt_val).strip():
            return num, i  # first free slot

    # No empty slot found — append after last MetaAd row
    last_meta_row = _find_last_meta_row(ws)
    return max_id + 1, last_meta_row + 1


def get_max_meta_id(ws: gspread.Worksheet) -> int:
    """Legacy: returns highest MetaAd number. Use get_next_start_id() instead."""
    num, _ = get_next_start_id(ws)
    return num - 1


def _find_last_meta_row(ws: gspread.Worksheet) -> int:
    """Returns the 1-indexed sheet row of the last MetaAd entry in column B."""
    col_b = ws.col_values(_col_letter_to_index(_COL_ID) + 1)
    last_row = 0
    for i, v in enumerate(col_b, start=1):
        if re.match(r"MetaAd-\d+", str(v).strip()):
            last_row = i
    return last_row


def _find_row_for_id(ws: gspread.Worksheet, meta_id: str) -> int:
    """
    Returns the sheet row that already has meta_id in col B (pre-filled slot).
    Falls back to last_meta_row + 1 if not found.
    """
    col_b = ws.col_values(_col_letter_to_index(_COL_ID) + 1)
    for i, val in enumerate(col_b, start=1):
        if str(val).strip() == meta_id:
            return i
    return _find_last_meta_row(ws) + 1


def _apply_row_formatting(ws: gspread.Worksheet, row: int) -> None:
    """
    Applies the standard naming-convention row formatting:
      C–G : green background  rgb(0.90, 0.99, 0.53)
      K–L : grey bg + red bold text
    """
    green_bg = {
        "backgroundColor": {"red": 0.90, "green": 0.99, "blue": 0.53}
    }
    output_fmt = {
        "backgroundColor": {"red": 0.95, "green": 0.96, "blue": 0.96},
        "textFormat": {
            "foregroundColor": {"red": 1.0, "green": 0.0, "blue": 0.0},
            "bold": True,
        },
    }
    ws.format(f"C{row}:G{row}", green_bg)
    ws.format(f"K{row}:L{row}", output_fmt)


def read_ad_link_rows(ws: gspread.Worksheet) -> list[dict]:
    """
    Reads columns L and M (ad_name, asset_link) from the naming worksheet.
    Returns rows where column L starts with 'MetaAd-' (skips headers/spacers).
    Each entry: {"sheet_row": int, "ad_name": str, "asset_link": str}
    """
    lm = ws.get("L:M")
    result = []
    for i, row_vals in enumerate(lm, start=1):  # i = 1-based sheet row
        ad_name = row_vals[0].strip() if len(row_vals) > 0 else ""
        asset_link = row_vals[1].strip() if len(row_vals) > 1 else ""
        if not ad_name.startswith("MetaAd-"):
            continue
        result.append({"sheet_row": i, "ad_name": ad_name, "asset_link": asset_link})
    return result


def read_row_by_id(ws: gspread.Worksheet, meta_id: str) -> dict | None:
    """
    Reads the full A–S row for the given MetaAd-ID.
    Returns a dict with sheet_row + all known fields, or None if the ID is not in col B.
    """
    col_b = ws.col_values(_col_letter_to_index(_COL_ID) + 1)
    target_row = 0
    for i, val in enumerate(col_b, start=1):
        if str(val).strip() == meta_id:
            target_row = i
            break
    if not target_row:
        return None

    row_vals = ws.get(f"{_WRITE_RANGE_START}{target_row}:{_WRITE_RANGE_END}{target_row}")
    values = row_vals[0] if row_vals else []
    # Pad to _NUM_COLS so indexing is safe
    values = list(values) + [""] * (_NUM_COLS - len(values))

    def _v(letter: str) -> str:
        return str(values[_col_letter_to_index(letter)]).strip()

    return {
        "sheet_row":   target_row,
        "meta_id":     _v(_COL_ID),
        "fmt":         _v(_COL_FORMAT),
        "product":     _v(_COL_PRODUCT),
        "description": _v(_COL_DESC),
        "creator":     _v(_COL_CREATOR),
        "version":     _v(_COL_VERSION),
        "lp_url":      _v(_COL_LP_URL),
        "lp_id":       _v(_COL_LP_ID),
        "adset_name":  _v(_COL_ADSET_OUT),
        "ad_name":     _v(_COL_AD_OUT),
        "asset_link":  _v(_COL_ASSET_LINK),
    }


def update_row_fields(ws: gspread.Worksheet, row: int, updates: dict) -> None:
    """
    Selectively updates individual cells of a sheet row.
    updates: {"C": "VID-60", "M": "https://drive.google.com/...", ...}
    Uses batch_update so the call costs one API request.
    """
    if not updates:
        return
    batch = [
        {"range": f"{col}{row}", "values": [[val]]}
        for col, val in updates.items()
    ]
    ws.batch_update(batch, value_input_option="USER_ENTERED")


def append_ad_row(ws: gspread.Worksheet, row_data: dict) -> None:
    """
    Writes to the pre-filled slot for meta_id in col B if it exists,
    otherwise appends after the last MetaAd row.
    Applies standard cell formatting (green C–G, grey+red+bold K–L).
    """
    target_row = _find_row_for_id(ws, row_data["meta_id"])

    # Build a row of 19 empty strings (A–S), then fill named columns
    row = [""] * _NUM_COLS
    row[_col_letter_to_index(_COL_ID)]         = row_data["meta_id"]
    row[_col_letter_to_index(_COL_FORMAT)]      = row_data["fmt"]
    row[_col_letter_to_index(_COL_PRODUCT)]     = row_data["product"]
    row[_col_letter_to_index(_COL_DESC)]        = row_data["description"]
    row[_col_letter_to_index(_COL_CREATOR)]     = row_data["creator"]
    row[_col_letter_to_index(_COL_VERSION)]     = row_data["version"]
    row[_col_letter_to_index(_COL_LP_URL)]      = row_data.get("lp_url", "")
    row[_col_letter_to_index(_COL_LP_ID)]       = row_data.get("lp_id", "")
    row[_col_letter_to_index(_COL_ADSET_OUT)]   = row_data.get("adset_name", "")
    row[_col_letter_to_index(_COL_AD_OUT)]      = row_data.get("ad_name", "")
    row[_col_letter_to_index(_COL_ASSET_LINK)]  = row_data.get("asset_link", "")
    row[_col_letter_to_index(_COL_READY)]       = "yes"

    cell_range = f"{_WRITE_RANGE_START}{target_row}:{_WRITE_RANGE_END}{target_row}"
    ws.update(cell_range, [row], value_input_option="USER_ENTERED")
    _apply_row_formatting(ws, target_row)

    # Datum der Freigabe (Ready=yes) in Spalte U setzen — gezielter Write, damit
    # Spalte T ("Check if really live (Anna)") außerhalb des A–S-Bereichs unberührt bleibt.
    update_row_fields(ws, target_row, {_COL_READY_DATE: datetime.now().strftime("%d.%m.%Y")})
