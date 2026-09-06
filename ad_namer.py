#!/usr/bin/env python3
"""
Ad Namer — Renames agency ad files according to the Mammaly naming convention.

WORKFLOW:
  Step 1 – Analyze:
    python ad_namer.py analyze <agency_folder_url>

    Lists all files in the agency folder, parses filenames, proposes
    naming convention entries, and saves them to proposals.json.
    Review the proposals (Claude will show you a table), then confirm.

  Step 2 – Execute:
    python ad_namer.py execute [--proposals proposals.json]

    Copies each file to the ablage folder with the new name,
    then appends a row to the naming convention sheet.

SETUP:
  The service account (credentials.json) must be shared as Editor on:
    - The agency folder (source)
    - The ablage folder (destination): see ADS_DRIVE_FOLDER_ID in .env
"""
from __future__ import annotations
import argparse
import json
import logging
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import config
from ad_namer.drive import (
    get_drive_service, folder_id_from_url,
    list_folder_files, search_files_by_name_prefix,
    get_video_duration_seconds, get_image_aspect_ratio,
    move_and_rename_file, upload_file_to_folder, trash_file, make_drive_link,
    is_video, is_image, get_file_extension,
)
from ad_namer.naming import (
    PRODUCT_ABBREVIATIONS, LP_DEFAULTS,
    duration_to_suffix, build_ad_name, build_adset_name,
    parse_agency_filename, next_meta_id, normalize_adset_names,
)
from ad_namer.sheet_ops import (
    get_naming_worksheet, get_next_start_id, append_ad_row, read_ad_link_rows,
    read_row_by_id, update_row_fields,
)
import gspread
from google.oauth2.service_account import Credentials
from auth.credentials import get_service_account_creds

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

PROPOSALS_FILE = Path("proposals.json")

_SHEET_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _get_sheet(sheet_id: str) -> gspread.Spreadsheet:
    creds = get_service_account_creds(_SHEET_SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(sheet_id)


def _format_code(file_meta: dict, fmt_type: str = "VID") -> str:
    """Determines the Format code (e.g. VID-60, STA-16x9, STA-4x5) from file metadata."""
    mime = file_meta.get("mimeType", "")
    if is_image(mime):
        ratio = get_image_aspect_ratio(file_meta)
        if ratio:
            return f"STA-{ratio}"
        return "STA"
    secs = get_video_duration_seconds(file_meta)
    if secs is not None:
        return f"{fmt_type}-{duration_to_suffix(secs)}"
    return f"{fmt_type}-?"


def cmd_analyze(
    folder_urls: list[str],
    creator: str,
    fmt_type: str,
    lp_override: tuple[str, str] | None = None,
) -> None:
    """Phase 1: List files from one or more folders, generate proposals → proposals.json"""
    drive = get_drive_service(config.GOOGLE_SERVICE_ACCOUNT_FILE)

    # Collect all files across folders (preserve folder order, sort within each)
    all_files = []
    for url in folder_urls:
        folder_id = folder_id_from_url(url)
        logger.info("Analysiere Ordner: %s", folder_id)
        files = list_folder_files(drive, folder_id)
        if not files:
            logger.error("Keine Dateien in Ordner %s gefunden.", folder_id)
            sys.exit(1)
        logger.info("  %d Datei(en) gefunden.", len(files))
        all_files.extend(sorted(files, key=lambda x: x["name"]))

    # Find first free MetaAd slot in the naming sheet
    sheet = _get_sheet(config.GOOGLE_SHEET_ID)
    ws = get_naming_worksheet(sheet, config.NAMING_SHEET_GID)
    start_id, start_row = get_next_start_id(ws)
    logger.info("Erste freie MetaAd-Zeile: MetaAd-%d (Sheet-Zeile %d)", start_id, start_row)

    proposals = []
    counter = start_id - 1

    for f in all_files:
        counter += 1
        meta_id = f"MetaAd-{counter}"
        parsed = parse_agency_filename(f["name"])
        effective_fmt_type = parsed.get("fmt_type") or fmt_type
        fmt = _format_code(f, effective_fmt_type)
        product = parsed["product"] or "???"
        description = parsed["description"] or "???"
        version = parsed["version"]
        if lp_override:
            lp_url, lp_id = lp_override
        else:
            lp_url, lp_id = LP_DEFAULTS.get(product, ("", ""))
        ext = get_file_extension(f["name"])

        ad_name = build_ad_name(meta_id, fmt, product, description, creator, version)
        adset_name = build_adset_name(meta_id, fmt, product, description, creator, lp_id)
        new_filename = ad_name + ext

        source_parents = f.get("parents", [])
        proposals.append({
            "source_file_id": f["id"],
            "source_file_name": f["name"],
            "source_parent_id": source_parents[0] if source_parents else "",
            "meta_id": meta_id,
            "fmt": fmt,
            "product": product,
            "description": description,
            "creator": creator,
            "version": version,
            "lp_url": lp_url,
            "lp_id": lp_id,
            "ad_name": ad_name,
            "adset_name": adset_name,
            "new_filename": new_filename,
            "asset_link": "",
        })

    normalize_adset_names(proposals)
    PROPOSALS_FILE.write_text(json.dumps(proposals, indent=2, ensure_ascii=False))
    logger.info("Vorschläge gespeichert in: %s", PROPOSALS_FILE)

    # Print review table
    print("\n" + "=" * 80)
    print("VORSCHAU — bitte prüfen und ggf. korrigieren:")
    print("=" * 80)
    for p in proposals:
        print(f"\n  Datei:       {p['source_file_name']}")
        print(f"  Neuer Name:  {p['new_filename']}")
        print(f"  Felder:      {p['meta_id']} | {p['fmt']} | {p['product']} | "
              f"{p['description']} | {p['creator']} | {p['version']}")
        print(f"  LP ID:       {p['lp_id']}")
        if "???" in p["new_filename"]:
            print("  ⚠️  Unbekannte Felder → bitte in proposals.json ergänzen")
    print("\n" + "=" * 80)
    print(f"Bei OK → python ad_namer.py execute")
    print("=" * 80 + "\n")


def cmd_execute(proposals_file: Path) -> None:
    """Phase 2: Copy files to ablage folder, update naming sheet."""
    if not proposals_file.exists():
        logger.error("proposals.json nicht gefunden. Bitte zuerst 'analyze' ausführen.")
        sys.exit(1)

    proposals = json.loads(proposals_file.read_text())
    still_unknown = [p for p in proposals if "???" in p["new_filename"]]
    if still_unknown:
        logger.error(
            "%d Vorschlag/Vorschläge haben noch unbekannte Felder (???). "
            "Bitte proposals.json bearbeiten.",
            len(still_unknown),
        )
        sys.exit(1)

    drive = get_drive_service(config.GOOGLE_SERVICE_ACCOUNT_FILE)
    sheet = _get_sheet(config.GOOGLE_SHEET_ID)
    ws = get_naming_worksheet(sheet, config.NAMING_SHEET_GID)

    success, errors = 0, 0
    for p in proposals:
        try:
            logger.info("Verschiebe & benenne um: %s → %s", p["source_file_name"], p["new_filename"])
            new_file = move_and_rename_file(
                drive, p["source_file_id"], p["new_filename"], config.ADS_DRIVE_FOLDER_ID,
                source_folder_id=p.get("source_parent_id", ""),
            )
            p["asset_link"] = make_drive_link(new_file["id"])
            logger.info("  ✓ Drive-Link: %s", p["asset_link"])

            append_ad_row(ws, p)
            logger.info("  ✓ Sheet-Zeile hinzugefügt")
            success += 1
        except Exception as e:
            logger.error("  ✗ Fehler bei %s: %s", p["source_file_name"], e)
            errors += 1

    proposals_file.write_text(json.dumps(proposals, indent=2, ensure_ascii=False))
    print(f"\n✅ Fertig: {success} erfolgreich, {errors} Fehler.")
    if errors:
        print("   Fehlgeschlagene Dateien → proposals.json prüfen.")


def cmd_upload(
    local_path: str,
    creator: str,
    product: str,
    description: str,
    duration_secs: int,
    lp_url: str,
    lp_id: str,
    version: str = "V1",
    fmt_type: str = "VID",
) -> None:
    """Upload a local file directly to the ablage folder with the correct name + sheet entry."""
    from pathlib import Path as _Path
    local = _Path(local_path)
    if not local.exists():
        logger.error("Datei nicht gefunden: %s", local_path)
        sys.exit(1)

    # Determine format
    fmt = f"{fmt_type}-{duration_to_suffix(duration_secs)}"

    # Get next MetaAd ID
    sheet = _get_sheet(config.GOOGLE_SHEET_ID)
    ws = get_naming_worksheet(sheet, config.NAMING_SHEET_GID)
    start_id, start_row = get_next_start_id(ws)
    meta_id = f"MetaAd-{start_id}"
    logger.info("Verwende: %s (Sheet-Zeile %d)", meta_id, start_row)

    ext = local.suffix.lower()
    ad_name    = build_ad_name(meta_id, fmt, product, description, creator, version)
    adset_name = build_adset_name(meta_id, fmt, product, description, creator, lp_id)
    new_filename = ad_name + ext

    logger.info("Neuer Name: %s", new_filename)
    logger.info("Lade hoch nach Drive…")

    drive = get_drive_service(config.GOOGLE_SERVICE_ACCOUNT_FILE)
    new_file = upload_file_to_folder(drive, local_path, new_filename, config.ADS_DRIVE_FOLDER_ID)
    asset_link = make_drive_link(new_file["id"])
    logger.info("✓ Drive-Link: %s", asset_link)

    row_data = {
        "meta_id": meta_id, "fmt": fmt, "product": product,
        "description": description, "creator": creator, "version": version,
        "lp_url": lp_url, "lp_id": lp_id,
        "ad_name": ad_name, "adset_name": adset_name, "asset_link": asset_link,
    }
    append_ad_row(ws, row_data)
    logger.info("✓ Sheet-Zeile hinzugefügt")
    print(f"\n✅ Fertig: {new_filename}")
    print(f"   Drive: {asset_link}")


def cmd_replace(meta_id: str, folder_url: str) -> None:
    """
    Replaces the asset file for an existing MetaAd-ID:
      1. Reads the sheet row for meta_id (product/description/creator/version/lp stay)
      2. Trashes the old file in the ablage folder (matched by MetaAd-ID prefix)
      3. Moves+renames the new file from folder_url into the ablage
      4. Updates only fmt / ad_name / adset_name / asset_link in the sheet
    """
    drive = get_drive_service(config.GOOGLE_SERVICE_ACCOUNT_FILE)
    sheet = _get_sheet(config.GOOGLE_SHEET_ID)
    ws    = get_naming_worksheet(sheet, config.NAMING_SHEET_GID)

    existing = read_row_by_id(ws, meta_id)
    if existing is None:
        logger.error("Sheet enthält keine Zeile für %s — Abbruch.", meta_id)
        sys.exit(1)

    # Sammle neue Datei aus dem Quell-Ordner — genau eine erwartet
    folder_id = folder_id_from_url(folder_url)
    src_files = list_folder_files(drive, folder_id)
    if not src_files:
        logger.error("Keine Datei in Quell-Ordner %s.", folder_id)
        sys.exit(1)
    if len(src_files) > 1:
        names = ", ".join(f["name"] for f in src_files)
        logger.error(
            "Quell-Ordner enthält %d Dateien (erwartet: 1): %s — Abbruch.",
            len(src_files), names,
        )
        sys.exit(1)
    new_file = src_files[0]

    # Format aus neuer Datei ableiten — alter fmt_type bleibt erhalten (VID / UGC / VSL / STA)
    old_fmt = existing["fmt"]
    fmt_type_match = re.match(r"(VID|UGC|VSL|STA)", old_fmt) if old_fmt else None
    fmt_type = fmt_type_match.group(1) if fmt_type_match else "VID"
    new_fmt = _format_code(new_file, fmt_type)

    new_ad_name = build_ad_name(
        meta_id, new_fmt, existing["product"], existing["description"],
        existing["creator"], existing["version"],
    )
    new_adset_name = build_adset_name(
        meta_id, new_fmt, existing["product"], existing["description"],
        existing["creator"], existing["lp_id"],
    )
    ext = get_file_extension(new_file["name"])
    new_filename = new_ad_name + ext

    # Alte Datei in der Ablage finden + trashen
    old_matches = [
        f for f in search_files_by_name_prefix(drive, meta_id)
        if config.ADS_DRIVE_FOLDER_ID in f.get("parents", [])
    ]
    if len(old_matches) == 0:
        logger.warning("Keine alte Datei für %s in der Ablage gefunden — überspringe Trash.", meta_id)
    elif len(old_matches) > 1:
        names = ", ".join(f["name"] for f in old_matches)
        logger.error(
            "Mehrere alte Dateien für %s in der Ablage (%s) — Abbruch.",
            meta_id, names,
        )
        sys.exit(1)
    else:
        old = old_matches[0]
        logger.info("Trashe alte Datei: %s (%s)", old["name"], old["id"])
        trash_file(drive, old["id"])

    # Neue Datei in die Ablage verschieben + umbenennen
    logger.info("Verschiebe & benenne um: %s → %s", new_file["name"], new_filename)
    moved = move_and_rename_file(
        drive, new_file["id"], new_filename, config.ADS_DRIVE_FOLDER_ID,
        source_folder_id=folder_id,
    )
    new_link = make_drive_link(moved["id"])

    # Sheet-Update: nur fmt (C), AdSet (K), AdName (L), AssetLink (M)
    updates = {
        "C": new_fmt,
        "K": new_adset_name,
        "L": new_ad_name,
        "M": new_link,
    }
    update_row_fields(ws, existing["sheet_row"], updates)

    print(f"\n✅ Ersetzt: {meta_id}")
    print(f"   Format:  {old_fmt} → {new_fmt}" if old_fmt != new_fmt else f"   Format:  {new_fmt} (unverändert)")
    print(f"   Datei:   {new_filename}")
    print(f"   Link:    {new_link}")


def cmd_fix_links() -> None:
    """Scans column M for broken/missing asset links and patches them from the ablage folder."""
    from collections import defaultdict

    drive = get_drive_service(config.GOOGLE_SERVICE_ACCOUNT_FILE)
    sheet = _get_sheet(config.GOOGLE_SHEET_ID)
    ws    = get_naming_worksheet(sheet, config.NAMING_SHEET_GID)

    rows = read_ad_link_rows(ws)

    def _is_broken(link: str) -> bool:
        if not link:                   return True  # empty
        if "/drive/folders/" in link:  return True  # folder link
        if "/file/d/" not in link:     return True  # unrecognised format
        return False

    broken = [r for r in rows if _is_broken(r["asset_link"])]
    logger.info("%d Zeilen mit Ad-Name, davon %d mit kaputtem/leerem Link", len(rows), len(broken))
    if not broken:
        print("Alle Asset-Links sind bereits korrekt. Nichts zu tun.")
        return

    logger.info("Suche alle MetaAd-Dateien (ordnerübergreifend) …")
    drive_files = search_files_by_name_prefix(drive, "MetaAd-")
    if not drive_files:
        logger.error("Keine MetaAd-Dateien gefunden.")
        sys.exit(1)
    logger.info("  %d Dateien gefunden.", len(drive_files))

    # Build lookup by MetaAd-ID prefix (first token of filename, e.g. "MetaAd-1389")
    id_map: dict[str, list[dict]] = defaultdict(list)
    for f in drive_files:
        base = f["name"].rsplit(".", 1)[0]          # strip extension
        meta_id = base.split("_")[0]                # "MetaAd-NNNN"
        if meta_id.startswith("MetaAd-"):
            id_map[meta_id].append(f)

    fixed, not_found, ambiguous = [], [], []
    for row in broken:
        # Extract MetaAd-ID from the sheet's ad_name column
        meta_id = row["ad_name"].split("_")[0]
        matches = id_map.get(meta_id, [])
        if len(matches) == 0:
            not_found.append(row["ad_name"])
            logger.warning("Nicht gefunden: %s (Zeile %d)", meta_id, row["sheet_row"])
        elif len(matches) > 1:
            ambiguous.append(row["ad_name"])
            logger.warning("Mehrdeutig (%d Treffer) für %s: %s",
                           len(matches), meta_id,
                           ", ".join(f["name"] for f in matches))
        else:
            fixed.append({
                "sheet_row": row["sheet_row"],
                "ad_name":   row["ad_name"],
                "new_link":  make_drive_link(matches[0]["id"]),
            })

    # Batch-update column M in one API call
    COL_M = 13  # A=1 … M=13
    updates = [
        {"range": gspread.utils.rowcol_to_a1(item["sheet_row"], COL_M),
         "values": [[item["new_link"]]]}
        for item in fixed
    ]
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")

    for item in fixed:
        logger.info("✓ Zeile %d: %s", item["sheet_row"], item["ad_name"])

    print(f"\n✅  Behoben: {len(fixed)}  |  Nicht gefunden: {len(not_found)}  |  Mehrdeutig: {len(ambiguous)}")
    if not_found:
        print("Nicht gefunden:", ", ".join(not_found))
    if ambiguous:
        print("Mehrdeutig:", ", ".join(ambiguous))


def main() -> None:
    parser = argparse.ArgumentParser(description="Ad Namer — Mammaly naming convention workflow")
    sub = parser.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="Schritt 1: Dateien analysieren und Vorschläge erstellen")
    a.add_argument("folders", nargs="+", help="Google Drive Ordner-URLs oder IDs (ein oder mehrere)")
    a.add_argument("--creator", required=True, help="Creator-/Agenturname für den Dateinamen")
    a.add_argument("--type", default="VID", dest="fmt_type",
                   choices=["VID", "UGC", "VSL", "STA"],
                   help="Ad-Typ für Formatcode (Standard: VID)")
    a.add_argument("--lp-id", default=None, dest="lp_id",
                   help="LP-ID überschreiben (z.B. Meta-Offer-TL-NP)")
    a.add_argument("--lp-url", default=None, dest="lp_url",
                   help="LP-URL überschreiben")

    e = sub.add_parser("execute", help="Schritt 2: Dateien umbenennen und Sheet aktualisieren")
    e.add_argument("--proposals", default="proposals.json",
                   help="Pfad zur proposals.json (Standard: proposals.json)")

    u = sub.add_parser("upload", help="Lokale Datei direkt hochladen, umbenennen und ins Sheet eintragen")
    u.add_argument("file", help="Lokaler Dateipfad")
    u.add_argument("--creator", required=True, help="Creator-Name")
    u.add_argument("--product", required=True, help="Produkt (z.B. SynGrünlipp)")
    u.add_argument("--description", required=True, help="Beschreibung (z.B. Ad18_TalkingIngredients)")
    u.add_argument("--duration", required=True, type=int, help="Videolänge in Sekunden")
    u.add_argument("--lp-url", required=True, dest="lp_url", help="LP URL")
    u.add_argument("--lp-id", required=True, dest="lp_id", help="LP ID")
    u.add_argument("--version", default="V1", help="Version (Standard: V1)")
    u.add_argument("--type", default="VID", dest="fmt_type",
                   choices=["VID", "UGC", "VSL", "STA"],
                   help="Ad-Typ (Standard: VID)")

    r = sub.add_parser("replace",
                       help="Ad-Datei ersetzen (gleiche MetaAd-ID, neue Datei aus Quell-Ordner)")
    r.add_argument("meta_id", help="MetaAd-ID (z.B. MetaAd-1788)")
    r.add_argument("folder", help="Google Drive Quell-Ordner-URL mit der neuen Datei")

    sub.add_parser("fix-links",
                   help="Kaputte Asset-Links (Ordner-URLs oder leer) in Spalte M reparieren")

    args = parser.parse_args()

    if args.cmd == "analyze":
        lp_override = (args.lp_url, args.lp_id) if args.lp_id and args.lp_url else None
        cmd_analyze(args.folders, args.creator, args.fmt_type, lp_override)
    elif args.cmd == "execute":
        cmd_execute(Path(args.proposals))
    elif args.cmd == "upload":
        cmd_upload(
            local_path=args.file,
            creator=args.creator,
            product=args.product,
            description=args.description,
            duration_secs=args.duration,
            lp_url=args.lp_url,
            lp_id=args.lp_id,
            version=args.version,
            fmt_type=args.fmt_type,
        )
    elif args.cmd == "replace":
        cmd_replace(args.meta_id, args.folder)
    elif args.cmd == "fix-links":
        cmd_fix_links()


if __name__ == "__main__":
    main()
