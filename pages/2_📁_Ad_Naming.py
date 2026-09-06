import os
from dotenv import load_dotenv
load_dotenv()

import re
import streamlit as st
import pandas as pd

import config
from components.auth import require_connections
from ad_namer.drive import (
    get_drive_service, folder_id_from_url, list_folder_files,
    search_files_by_name_prefix,
    get_video_duration_seconds, move_and_rename_file, trash_file, make_drive_link,
    is_image, get_file_extension,
)
from ad_namer.naming import (
    LP_DEFAULTS, LP_ADVERTORIALS, duration_to_suffix, resolve_lp, lp_url_for_id,
    build_ad_name, build_adset_name, parse_agency_filename, normalize_adset_names,
)
from ad_namer.sheet_ops import (
    get_naming_worksheet, get_next_start_id, append_ad_row,
    read_row_by_id, update_row_fields,
)

st.set_page_config(page_title="Ad Naming — Mammaly Meta Ads", page_icon="📁", layout="wide")
st.title("📁 Ad Naming")
st.caption("Drive-Ordner analysieren → Vorschläge prüfen → Dateien umbenennen + Sheet befüllen")


def _fmt_code(file_meta: dict, fmt_type: str) -> str:
    mime = file_meta.get("mimeType", "")
    if is_image(mime):
        return "STA"
    secs = get_video_duration_seconds(file_meta)
    if secs is not None:
        return f"{fmt_type}-{duration_to_suffix(secs)}"
    return f"{fmt_type}-?"


tab_new, tab_replace = st.tabs(["➕ Neu hinzufügen", "♻️ Ersetzen"])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1: Neue Ads hinzufügen (bestehender Workflow)
# ══════════════════════════════════════════════════════════════════════════════
with tab_new:
    st.subheader("Schritt 1: Ordner & Parameter")

    _PRODUCT_OPTIONS = ["— auto-detect —"] + list(LP_DEFAULTS.keys())
    _LP_SOURCES = ["PDP (Standard)", "Advertorial"]

    folder_input = st.text_area(
        "Google Drive Ordner-URL(s)",
        placeholder="https://drive.google.com/drive/folders/…\nMehrere Ordner: eine URL pro Zeile",
        height=100,
    )
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        creator = st.text_input("Creator", value=os.getenv("DEFAULT_CREATOR", ""))
    with c2:
        fmt_type = st.selectbox("Ad-Typ", ["VID", "UGC", "VSL", "STA"])
    with c3:
        product_select = st.selectbox("Produkt", _PRODUCT_OPTIONS)
    with c4:
        lp_source = st.selectbox("LP-Quelle", _LP_SOURCES)

    # LP-Felder dynamisch vorbefüllen: Produkt-/Quellen-Wechsel setzt neue
    # Defaults, danach bleiben die Felder frei editierbar.
    if product_select != "— auto-detect —":
        if lp_source == "Advertorial":
            _lp_default = LP_ADVERTORIALS.get(product_select)
            if _lp_default is None:
                st.info(f"Kein Advertorial für **{product_select}** hinterlegt — PDP wird verwendet.")
                _lp_default = LP_DEFAULTS.get(product_select, ("", ""))
        else:
            _lp_default = LP_DEFAULTS.get(product_select, ("", ""))
    else:
        _lp_default = ("", "")

    _lp_sig = (product_select, lp_source)
    if st.session_state.get("_lp_sig") != _lp_sig:
        st.session_state["lp_url_field"] = _lp_default[0]
        st.session_state["lp_id_field"] = _lp_default[1]
        st.session_state["_lp_sig"] = _lp_sig

    lc1, lc2 = st.columns([1, 2])
    with lc1:
        lp_id_override = st.text_input(
            "LP-ID", key="lp_id_field",
            help="Wird bei Produkt-/Quellen-Wechsel automatisch gesetzt, bleibt aber editierbar.",
        )
    with lc2:
        lp_url_override = st.text_input("LP-URL", key="lp_url_field", placeholder="https://…")

    submitted = st.button("🔍 Analysieren", width="stretch")

    if submitted:
        urls = [u.strip() for u in folder_input.strip().splitlines() if u.strip()]
        if not urls:
            st.error("Bitte mindestens eine Ordner-URL eingeben.")
        else:
            _, sheet = require_connections()
            drive = get_drive_service(config.GOOGLE_SERVICE_ACCOUNT_FILE)

            with st.spinner("Dateien werden geladen…"):
                all_files: list[dict] = []
                for url in urls:
                    try:
                        fid = folder_id_from_url(url)
                        files = list_folder_files(drive, fid)
                        all_files.extend(sorted(files, key=lambda x: x["name"]))
                    except Exception as e:
                        st.error(f"Ordner {url}: {e}")

            if not all_files:
                st.warning("Keine Dateien gefunden.")
            else:
                ws = get_naming_worksheet(sheet, config.NAMING_SHEET_GID)
                start_id, _ = get_next_start_id(ws)

                product_override = product_select if product_select != "— auto-detect —" else None

                if lp_id_override.strip() and lp_url_override.strip():
                    lp_override = (lp_url_override.strip(), lp_id_override.strip())
                elif product_override:
                    lp_override = LP_DEFAULTS.get(product_override, ("", ""))
                else:
                    lp_override = None

                proposals: list[dict] = []
                for i, f in enumerate(all_files):
                    meta_id = f"MetaAd-{start_id + i}"
                    parsed = parse_agency_filename(f["name"], creator=creator)
                    eff_fmt_type = parsed.get("fmt_type") or fmt_type
                    fmt = _fmt_code(f, eff_fmt_type)
                    product = product_override or parsed["product"] or "???"
                    description = parsed["description"] or "???"
                    version = parsed["version"]
                    lp_url, lp_id = lp_override if lp_override else LP_DEFAULTS.get(product, ("", ""))
                    ext = get_file_extension(f["name"])
                    ad_name = build_ad_name(meta_id, fmt, product, description, creator, version)
                    adset_name = build_adset_name(meta_id, fmt, product, description, creator, lp_id)
                    src_parents = f.get("parents", [])
                    proposals.append({
                        "source_file_id": f["id"],
                        "source_file_name": f["name"],
                        "source_parent_id": src_parents[0] if src_parents else fid,
                        "ext": ext,
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
                        "new_filename": ad_name + ext,
                        "asset_link": "",
                    })

                normalize_adset_names(proposals)
                st.session_state["proposals"] = proposals
                unknown_count = sum(1 for p in proposals if "???" in p["new_filename"])
                if unknown_count:
                    st.warning(f"⚠️  {unknown_count} Datei(en) haben unbekannte Felder — bitte in der Tabelle korrigieren.")
                st.success(f"{len(proposals)} Datei(en) analysiert.")


    if st.session_state.get("proposals"):
        proposals: list[dict] = st.session_state["proposals"]

        st.divider()
        st.subheader("Schritt 2: Vorschläge prüfen & anpassen")
        st.caption(
            "Bearbeite alle Felder direkt in der Tabelle (auch Meta ID und LP). Eine bekannte "
            "LP-ID löst die LP-URL automatisch auf; eine manuell geänderte LP-URL hat Vorrang. "
            "Zeilen die du nicht anlegen willst, kannst du markieren und mit 🗑️ löschen. "
            "Ad-Name, AdSet-Name und Dateiname werden beim Ausführen neu berechnet."
        )

        EDITABLE = ["meta_id", "fmt", "product", "description", "creator", "version", "lp_id", "lp_url"]
        ALL_COLS = ["source_file_id", "source_file_name"] + EDITABLE + ["new_filename"]

        df = pd.DataFrame(proposals)[ALL_COLS]
        edited = st.data_editor(
            df,
            width="stretch",
            num_rows="dynamic",
            column_config={
                "source_file_id":   None,  # stabiler Schlüssel — ausgeblendet
                "source_file_name": st.column_config.TextColumn("Original-Datei", disabled=True),
                "meta_id":          st.column_config.TextColumn("Meta ID"),
                "fmt":              st.column_config.TextColumn("Format"),
                "product":          st.column_config.SelectboxColumn(
                                        "Produkt", options=list(LP_DEFAULTS.keys())),
                "description":      st.column_config.TextColumn("Konzept"),
                "creator":          st.column_config.TextColumn("Creator"),
                "version":          st.column_config.TextColumn("Version"),
                "lp_id":            st.column_config.TextColumn(
                                        "LP-ID",
                                        help="Bekannte LP-ID → URL wird beim Ausführen automatisch aufgelöst"),
                "lp_url":           st.column_config.TextColumn(
                                        "LP-URL",
                                        help="Manuell geänderte URL hat Vorrang vor der LP-ID-Auflösung"),
                "new_filename":     st.column_config.TextColumn("Vorschau Name", disabled=True),
            },
            key="proposals_editor",
        )

        if st.button("▶️ Ausführen", type="primary", width="stretch"):
            by_id = {p["source_file_id"]: p for p in proposals}
            active: list[dict] = []
            for _, row in edited.iterrows():
                sid = row.get("source_file_id")
                if pd.isna(sid) or not str(sid).strip():
                    continue  # neu hinzugefügte Zeile ohne Quelldatei → überspringen
                p = by_id.get(str(sid).strip())
                if p is None:
                    continue
                orig_lp_url = p["lp_url"]
                for col in EDITABLE:
                    val = row[col]
                    p[col] = "" if pd.isna(val) else str(val).strip()
                if not p["product"]:
                    p["product"] = "???"  # Dropdown leer gelassen → Validierung greift
                # LP pro Zeile auflösen: manuell editierte URL gewinnt, sonst
                # bekannte LP-ID → URL, sonst Produkt-Default.
                if not (p["lp_url"] and p["lp_url"] != orig_lp_url):
                    resolved_url = lp_url_for_id(p["lp_id"])
                    if resolved_url:
                        p["lp_url"] = resolved_url
                    elif not p["lp_url"]:
                        p["lp_url"], p["lp_id"] = resolve_lp(p["product"], p["lp_id"])
                p["ad_name"] = build_ad_name(
                    p["meta_id"], p["fmt"], p["product"], p["description"],
                    p["creator"], p["version"],
                )
                p["adset_name"] = build_adset_name(
                    p["meta_id"], p["fmt"], p["product"], p["description"],
                    p["creator"], p["lp_id"],
                )
                p["new_filename"] = p["ad_name"] + p["ext"]
                active.append(p)

            # Nur die verbliebenen (nicht gelöschten) Vorschläge weiterverarbeiten
            proposals = active
            st.session_state["proposals"] = active

            still_unknown = [p for p in proposals if "???" in p["new_filename"]]
            missing_lp = [p for p in proposals if not p["lp_url"] or not p["lp_id"]]
            if not proposals:
                st.warning("Keine Vorschläge übrig — alle Zeilen wurden gelöscht.")
            elif still_unknown:
                st.error(f"⛔  {len(still_unknown)} Datei(en) haben noch unbekannte Felder — bitte korrigieren.")
            elif missing_lp:
                _ids = ", ".join(p["meta_id"] for p in missing_lp[:5])
                if len(missing_lp) > 5:
                    _ids += ", …"
                st.error(f"⛔  {len(missing_lp)} Zeile(n) ohne LP-ID/LP-URL — bitte in der Tabelle setzen ({_ids}).")
            else:
                _, sheet = require_connections()
                drive = get_drive_service(config.GOOGLE_SERVICE_ACCOUNT_FILE)
                ws = get_naming_worksheet(sheet, config.NAMING_SHEET_GID)

                progress = st.progress(0)
                status_box = st.empty()
                ok, errors = 0, 0

                for i, p in enumerate(proposals):
                    status_box.info(f"Verarbeite {i + 1}/{len(proposals)}: {p['source_file_name']}")
                    try:
                        # STA-Ads → Static-Ablage, alles andere → UGC-Ablage
                        is_static = p["fmt"].upper().startswith("STA")
                        dest_folder = (
                            config.STATIC_DRIVE_FOLDER_ID
                            if is_static and config.STATIC_DRIVE_FOLDER_ID
                            else config.ADS_DRIVE_FOLDER_ID
                        )
                        new_file = move_and_rename_file(
                            drive, p["source_file_id"], p["new_filename"],
                            dest_folder,
                            source_folder_id=p.get("source_parent_id", ""),
                        )
                        p["asset_link"] = make_drive_link(new_file["id"])
                        append_ad_row(ws, p)
                        ok += 1
                    except Exception as e:
                        st.error(f"Fehler bei **{p['source_file_name']}**: {e}")
                        errors += 1
                    progress.progress((i + 1) / len(proposals))

                status_box.empty()
                if errors == 0:
                    st.success(f"✅  {ok} Datei(en) erfolgreich umbenannt und ins Sheet eingetragen!")
                    del st.session_state["proposals"]
                else:
                    st.warning(f"⚠️  {ok} erfolgreich, {errors} Fehler.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2: Ersetzen (Update bestehender Ads)
# ══════════════════════════════════════════════════════════════════════════════
with tab_replace:
    st.subheader("Bestehende Ads durch aktualisierte Dateien ersetzen")
    st.caption(
        "Die alte Datei im Ablage-Ordner wird in den **Papierkorb** verschoben (30 Tage wiederherstellbar). "
        "Die neue Datei wird aus dem Quell-Ordner in die Ablage gelegt und korrekt umbenannt. "
        "Im Sheet bleiben Produkt/Description/Creator/Version/LP unverändert — nur Format und Asset-Link werden aktualisiert."
    )

    replace_input = st.text_area(
        "Ersetzungen (eine pro Zeile, Format: `MetaAd-ID | Quell-Ordner-URL`)",
        placeholder=(
            "MetaAd-1001 | https://drive.google.com/drive/folders/<ORDNER-ID>\n"
            "MetaAd-1002 | https://drive.google.com/drive/folders/<ORDNER-ID>"
        ),
        height=120,
        key="replace_input",
    )

    preview_clicked = st.button("🔍 Vorschau", width="stretch", key="replace_preview_btn")

    def _parse_replace_lines(text: str) -> list[tuple[str, str]]:
        pairs = []
        for line in text.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            if "|" not in line:
                pairs.append(("__error__", f"Zeile ohne `|`: {line}"))
                continue
            mid, url = [p.strip() for p in line.split("|", 1)]
            if not re.match(r"^MetaAd-\d+$", mid):
                pairs.append(("__error__", f"Ungültige MetaAd-ID: {mid}"))
                continue
            if "drive.google.com" not in url and not url.startswith("http"):
                pairs.append(("__error__", f"Ungültige URL: {url}"))
                continue
            pairs.append((mid, url))
        return pairs

    if preview_clicked:
        pairs = _parse_replace_lines(replace_input)
        errors_in_input = [p for p in pairs if p[0] == "__error__"]
        if errors_in_input:
            for _, msg in errors_in_input:
                st.error(msg)
            st.stop()

        if not pairs:
            st.warning("Bitte mindestens eine Zeile eingeben.")
            st.stop()

        _, sheet = require_connections()
        drive = get_drive_service(config.GOOGLE_SERVICE_ACCOUNT_FILE)
        ws    = get_naming_worksheet(sheet, config.NAMING_SHEET_GID)

        preview_rows = []
        replace_jobs: list[dict] = []
        with st.spinner("Lese Sheet und Drive…"):
            for meta_id, folder_url in pairs:
                existing = read_row_by_id(ws, meta_id)
                if existing is None:
                    preview_rows.append({
                        "meta_id": meta_id, "status": "❌ Sheet-Zeile fehlt",
                        "alte_datei": "", "neue_datei": "",
                        "altes_format": "", "neues_format": "",
                        "format_geändert": "",
                    })
                    continue

                try:
                    folder_id = folder_id_from_url(folder_url)
                    src_files = list_folder_files(drive, folder_id)
                except Exception as e:
                    preview_rows.append({
                        "meta_id": meta_id, "status": f"❌ Quell-Ordner: {e}",
                        "alte_datei": "", "neue_datei": "",
                        "altes_format": existing["fmt"], "neues_format": "",
                        "format_geändert": "",
                    })
                    continue

                if len(src_files) == 0:
                    preview_rows.append({
                        "meta_id": meta_id, "status": "❌ leerer Quell-Ordner",
                        "alte_datei": "", "neue_datei": "",
                        "altes_format": existing["fmt"], "neues_format": "",
                        "format_geändert": "",
                    })
                    continue
                if len(src_files) > 1:
                    preview_rows.append({
                        "meta_id": meta_id, "status": f"❌ {len(src_files)} Dateien im Quell-Ordner (erwartet: 1)",
                        "alte_datei": "", "neue_datei": ", ".join(f["name"] for f in src_files),
                        "altes_format": existing["fmt"], "neues_format": "",
                        "format_geändert": "",
                    })
                    continue

                new_file = src_files[0]
                old_fmt = existing["fmt"]
                m = re.match(r"(VID|UGC|VSL|STA)", old_fmt) if old_fmt else None
                fmt_type = m.group(1) if m else "VID"
                new_fmt = _fmt_code(new_file, fmt_type)

                old_matches = [
                    f for f in search_files_by_name_prefix(drive, meta_id)
                    if config.ADS_DRIVE_FOLDER_ID in f.get("parents", [])
                ]
                if len(old_matches) > 1:
                    preview_rows.append({
                        "meta_id": meta_id,
                        "status": f"❌ {len(old_matches)} Dateien für {meta_id} in Ablage (Mehrdeutigkeit)",
                        "alte_datei": ", ".join(f["name"] for f in old_matches),
                        "neue_datei": new_file["name"],
                        "altes_format": old_fmt, "neues_format": new_fmt,
                        "format_geändert": "ja" if old_fmt != new_fmt else "nein",
                    })
                    continue

                old_name = old_matches[0]["name"] if old_matches else "(keine alte Datei gefunden)"

                preview_rows.append({
                    "meta_id": meta_id, "status": "✅ bereit",
                    "alte_datei": old_name,
                    "neue_datei": new_file["name"],
                    "altes_format": old_fmt, "neues_format": new_fmt,
                    "format_geändert": "ja" if old_fmt != new_fmt else "nein",
                })

                ext = get_file_extension(new_file["name"])
                new_ad_name = build_ad_name(
                    meta_id, new_fmt, existing["product"], existing["description"],
                    existing["creator"], existing["version"],
                )
                new_adset_name = build_adset_name(
                    meta_id, new_fmt, existing["product"], existing["description"],
                    existing["creator"], existing["lp_id"],
                )
                replace_jobs.append({
                    "meta_id": meta_id,
                    "sheet_row": existing["sheet_row"],
                    "old_file_id": old_matches[0]["id"] if old_matches else None,
                    "old_file_name": old_name,
                    "new_file_id": new_file["id"],
                    "new_file_name": new_file["name"],
                    "source_folder_id": folder_id,
                    "new_filename": new_ad_name + ext,
                    "new_fmt": new_fmt,
                    "new_ad_name": new_ad_name,
                    "new_adset_name": new_adset_name,
                })

        st.session_state["replace_preview"] = preview_rows
        st.session_state["replace_jobs"] = replace_jobs

    if st.session_state.get("replace_preview"):
        st.divider()
        st.markdown("**Vorschau**")
        st.dataframe(
            pd.DataFrame(st.session_state["replace_preview"]),
            width="stretch", hide_index=True,
        )

        ready = st.session_state.get("replace_jobs") or []
        if not ready:
            st.warning("Keine ausführbaren Jobs.")
        else:
            st.caption(f"{len(ready)} Ersetzung(en) bereit zur Ausführung.")
            if st.button("♻️ Jetzt ersetzen (alte Dateien → Papierkorb)",
                         type="primary", width="stretch", key="replace_execute_btn"):
                _, sheet = require_connections()
                drive = get_drive_service(config.GOOGLE_SERVICE_ACCOUNT_FILE)
                ws    = get_naming_worksheet(sheet, config.NAMING_SHEET_GID)

                progress = st.progress(0)
                status_box = st.empty()
                ok, errors = 0, 0
                results = []

                for i, job in enumerate(ready):
                    status_box.info(f"Verarbeite {i + 1}/{len(ready)}: {job['meta_id']}")
                    try:
                        if job["old_file_id"]:
                            trash_file(drive, job["old_file_id"])
                        moved = move_and_rename_file(
                            drive, job["new_file_id"], job["new_filename"],
                            config.ADS_DRIVE_FOLDER_ID,
                            source_folder_id=job["source_folder_id"],
                        )
                        new_link = make_drive_link(moved["id"])
                        update_row_fields(ws, job["sheet_row"], {
                            "C": job["new_fmt"],
                            "K": job["new_adset_name"],
                            "L": job["new_ad_name"],
                            "M": new_link,
                        })
                        results.append({
                            "meta_id": job["meta_id"], "status": "✅",
                            "neue_datei": job["new_filename"], "link": new_link,
                        })
                        ok += 1
                    except Exception as e:
                        st.error(f"Fehler bei **{job['meta_id']}**: {e}")
                        results.append({
                            "meta_id": job["meta_id"], "status": f"❌ {e}",
                            "neue_datei": "", "link": "",
                        })
                        errors += 1
                    progress.progress((i + 1) / len(ready))

                status_box.empty()
                st.dataframe(pd.DataFrame(results), width="stretch", hide_index=True)
                if errors == 0:
                    st.success(f"✅  {ok} Ad(s) erfolgreich ersetzt.")
                    del st.session_state["replace_preview"]
                    del st.session_state["replace_jobs"]
                else:
                    st.warning(f"⚠️  {ok} erfolgreich, {errors} Fehler.")
