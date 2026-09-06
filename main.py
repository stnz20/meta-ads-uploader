#!/usr/bin/env python3
import argparse
import logging
import sys
import os
import pathlib

# .env explizit aus dem Projektordner laden — funktioniert unabhängig vom cwd
_PROJECT_ROOT = pathlib.Path(__file__).parent
os.chdir(_PROJECT_ROOT)  # cwd = Projektordner, damit relative Pfade stimmen
from dotenv import load_dotenv
load_dotenv(dotenv_path=_PROJECT_ROOT / ".env")

from auth.meta_auth import init_meta_api
from auth.sheets_auth import get_sheet_client
import uploader


def main() -> None:
    parser = argparse.ArgumentParser(description="Meta Ads Uploader")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Alle Schritte durchlaufen, aber keine API-Calls an Meta senden.",
    )
    parser.add_argument(
        "--sheet-id",
        help="Google Sheet ID (überschreibt GOOGLE_SHEET_ID in .env).",
    )
    parser.add_argument(
        "--fetch-meta-config",
        action="store_true",
        help="Pages, Instagram-Konten und Advertiser aus Meta laden und in 'meta_config' Sheet schreiben.",
    )
    parser.add_argument(
        "--source-campaign-id",
        help="Überschreibt source_campaign_id für alle neuen Ad Sets (z.B. 120240969369520641).",
    )
    parser.add_argument(
        "--source-adset-id",
        help="Überschreibt source_adset_id für alle neuen Ad Sets (z.B. 120243523055610641).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.sheet_id:
        import config
        config.GOOGLE_SHEET_ID = args.sheet_id

    if args.dry_run:
        logging.getLogger().info("=== DRY-RUN MODUS — keine echten API-Calls ===")

    try:
        account = init_meta_api()
        sheet = get_sheet_client()
    except Exception as e:
        logging.getLogger().error("Authentifizierung fehlgeschlagen: %s", e)
        sys.exit(1)

    if args.fetch_meta_config:
        from meta.config_fetcher import fetch_and_write_meta_config
        fetch_and_write_meta_config(sheet, account)
        return

    uploader.run(
        sheet, account,
        dry_run=args.dry_run,
        override_campaign_id=args.source_campaign_id or None,
        override_adset_id=args.source_adset_id or None,
    )


if __name__ == "__main__":
    main()
