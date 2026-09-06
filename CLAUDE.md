# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Google-Sheet-driven bulk uploader for Meta Ads (Mammaly). Reads `ad_sets`/`ads` tabs from a Google Sheet, clones targeting/creative defaults from reference campaigns/adsets, uploads Drive assets (video/image) to Meta, and creates Ad Sets + Ads via the `facebook-business` SDK. A second subsystem (`ad_namer/`) renames agency creative files in Google Drive to the Mammaly naming convention and fills the Naming-Convention sheet. Two frontends: CLI (`main.py`) and a Streamlit multipage app (`app.py` + `pages/`).

## Setup & common commands

```bash
pip install -r requirements.txt        # needs ffmpeg libs for PyAV (see Gotchas)
cp .env.example .env                   # META_*, GOOGLE_SHEET_ID, ADS_DRIVE_FOLDER_ID …
```

| Command | Purpose |
|---|---|
| `python3 main.py --dry-run` | Full run without Meta API calls or Sheet writes — always do this first |
| `python3 main.py` | Real upload: create adsets + ads from the Sheet |
| `python3 main.py --sheet-id <ID>` | Override `GOOGLE_SHEET_ID` |
| `python3 main.py --source-campaign-id / --source-adset-id <ID>` | Override reference IDs for all new adsets |
| `python3 main.py --fetch-meta-config` | Write pages/IG accounts/advertisers into the `meta_config` tab |
| `streamlit run app.py` | UI: Dashboard, Ad Naming, Upload, History, Asset Link Sync |
| `python3 ad_namer.py analyze <agency_folder_url>` → review → `python3 ad_namer.py execute` | Legacy CLI renamer (two-step, writes `proposals.json`) |

There are no tests. Logs land in `upload-*.log` / `dryrun-*.log` in the repo root.

## Architecture

- `main.py` — CLI entry; `os.chdir`s to the project root and loads `.env` from there, so it works from any cwd.
- `uploader.py` — orchestration: read Sheet → validate URLs → validate campaigns → create adsets → create ads in parallel (`MAX_PARALLEL_ADS = 3`, Sheets writes behind a lock). Writes DONE/ERROR status + `created_id` back to the Sheet and appends to the `log` tab — **skipped entirely in dry-run**.
- `sheets/reader.py` — row parsing/validation for `ad_sets` and `ads` tabs (`AdSetRow`, `AdRow`), backoff for gspread.
- `meta/` — SDK wrappers: `adset.py`/`ad.py`/`campaign.py` (creation, cloning from `source_adset_id`), `creative_defaults.py` (copies bodies/headlines/CTA/URL from the first ad of a reference adset, cached per ref-ID), `assets.py` (Drive download → Meta upload, chunked video upload, first-frame thumbnail via PyAV, retry/backoff, disk cache in `cache/`), `config_fetcher.py`, `reference_fetcher.py`.
- `ad_namer/` (package) — Drive ops, naming logic, `batch_prepare.py` (Python port of `BatchUpload.gs`: Naming tab → `ad_sets`/`ads` rows). Top-level `ad_namer.py` is the older CLI that imports the same package.
- `auth/` — `meta_auth.py` (SDK init from `.env`), `sheets_auth.py`/`credentials.py` (Google service account).
- `pages/`, `components/` — Streamlit UI; secrets for the hosted app in `.streamlit/secrets.toml` (see example).
- `BatchUpload.gs` — Apps Script living in the Google Sheet itself; source of truth for the "Ready for upload?" batch flow, mirrored by `batch_prepare.py`. Keep both in sync when changing tab/column layout.
- `migrate_nature_protect.py` — one-off migration script, not part of the pipeline.

## Gotchas & conventions

- **credentials.json** (Google service account, gitignored) must be shared as **Editor** on the Google Sheet, the agency Drive folders, and the Ablage folder (`ADS_DRIVE_FOLDER_ID`). Path configurable via `GOOGLE_SERVICE_ACCOUNT_FILE`, resolved relative to the repo.
- **PyAV (`av`)**, not the ffmpeg CLI, reads video durations and extracts thumbnail frames — it needs ffmpeg shared libraries at install time (`brew install ffmpeg` on macOS).
- `META_ACCESS_TOKEN` must be a **long-lived System User token** (ads_management, ads_read, business_management, pages_show_list) — see `META-APP-SETUP.md`. `config.mask_token()` masks tokens in all log/error output; keep using it for any new logging of exceptions.
- **Naming convention** (`ad_namer/naming.py`): ad = `{meta_id}_{fmt}_{product}_{description}_{creator}_{version}`, adset = `{YYYYMMDD}_{meta_id}_{fmt}_{product}_{description}_{creator}_{lp_id}`. Agency abbreviations (NP, LB, AH …) map to product names via `PRODUCT_ABBREVIATIONS`; LP URLs/IDs per product in `LP_DEFAULTS`. Versions of the same concept share the V1 adset name (`normalize_adset_names`).
- **Sheet is the state store**: `status`/`created_id` columns decide what gets (re)processed; already-DONE adsets are reused so new ads can join them. Naming tab header row is found by the marker "Ready for upload?" (`NAMING_SHEET_GID` in `.env`).
- Rate limiting: `API_CALL_DELAY = 0.5`s between Meta calls, max 3 parallel ad uploads — raise with care.
- `cache/` holds Drive-download and Meta-upload caches keyed by Drive file ID — delete entries there if a re-upload of the same file is needed.
- Repo path contains a space ("Meta Ads Uploader") — always quote paths in shell commands.
