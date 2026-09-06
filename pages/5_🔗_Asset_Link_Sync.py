import os
from dotenv import load_dotenv
load_dotenv()

import re
import streamlit as st
import pandas as pd

import config
from components.auth import require_connections
from ad_namer.drive import (
    get_drive_service, folder_id_from_url, list_folder_files, make_drive_link,
    get_file_extension, rename_file,
)
from ad_namer.naming import build_ad_name
from ad_namer.sheet_ops import get_naming_worksheet

st.set_page_config(page_title="Asset Link Sync — Mammaly Meta Ads", page_icon="🔗", layout="wide")
st.title("🔗 Asset Link Sync")
st.caption(
    "Findet Asset-Dateien (UGC, Static, Video, …) in Drive-Ordnern anhand der Meta-Ad-ID "
    "und trägt den Link in Spalte M (Asset Link) im Naming-Convention-Tab nach — nur für "
    "Zeilen, in denen die Spalte leer ist. Dateien, die **nur** die `MetaAd-{ID}` tragen "
    "(z. B. `MetaAd-1234.mp4`), werden vorher automatisch in den vollen Namen umbenannt."
)

# Vorbelegte Drive-Ordner (optional): ASSET_SYNC_FOLDER_URLS in .env, kommagetrennt
DEFAULT_FOLDER_URLS = "\n".join(
    u.strip() for u in os.getenv("ASSET_SYNC_FOLDER_URLS", "").split(",") if u.strip()
)
_META_ID_RE = re.compile(r"^MetaAd-\d+$")
_FILE_META_ID_RE = re.compile(r"^(MetaAd-\d+)_")
# Datei trägt NUR die ID (+ optionale Endung), z. B. "MetaAd-1234.mp4" → muss umbenannt werden
_FILE_BARE_ID_RE = re.compile(r"^(MetaAd-\d+)(?:\.[A-Za-z0-9]+)?$")


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Eingabe + Scan
# ─────────────────────────────────────────────────────────────────────────────
st.subheader("Schritt 1: Drive-Ordner scannen")

folder_input = st.text_area(
    "Google Drive Ordner-URL(s)",
    value=DEFAULT_FOLDER_URLS,
    height=120,
    help=(
        "Eine URL pro Zeile. Z.B. UGC-Ordner + Static-Ordner. "
        "Dateinamen müssen mit `MetaAd-{ID}_` beginnen oder nur die `MetaAd-{ID}` tragen "
        "(letztere werden automatisch umbenannt)."
    ),
)

if st.button("📂 Ordner scannen", type="primary", width="stretch"):
    _, sheet = require_connections()
    drive = get_drive_service(config.GOOGLE_SERVICE_ACCOUNT_FILE)

    urls = [u.strip() for u in folder_input.strip().splitlines() if u.strip()]
    if not urls:
        st.error("Bitte mindestens eine Ordner-URL eingeben.")
        st.stop()

    folder_ids: list[str] = []
    for url in urls:
        try:
            folder_ids.append(folder_id_from_url(url))
        except Exception as e:
            st.error(f"Ungültige URL `{url}`: {e}")
            st.stop()

    with st.spinner("Sheet und Drive werden gelesen…"):
        ws = get_naming_worksheet(sheet, config.NAMING_SHEET_GID)
        bcm = ws.get("B:M")  # B=ID, C=Format, …, M=Asset Link

        # Alle Dateien aus allen Ordnern sammeln und nach MetaAd-ID indexieren.
        # files_by_meta_id      → Vollnamen (MetaAd-{ID}_…), Link nur eintragen
        # bare_files_by_meta_id → nur ID (MetaAd-{ID}[.ext]), erst umbenennen
        files_by_meta_id: dict[str, list[dict]] = {}
        bare_files_by_meta_id: dict[str, list[dict]] = {}
        folder_errors: list[str] = []
        total_files = 0
        total_bare = 0
        for fid, url in zip(folder_ids, urls):
            try:
                files = list_folder_files(drive, fid)
            except Exception as e:
                folder_errors.append(f"{url}: {e}")
                continue
            for f in files:
                m = _FILE_META_ID_RE.match(f["name"])
                if m:
                    files_by_meta_id.setdefault(m.group(1), []).append(f)
                    total_files += 1
                    continue
                mb = _FILE_BARE_ID_RE.match(f["name"])
                if mb:
                    bare_files_by_meta_id.setdefault(mb.group(1), []).append(f)
                    total_bare += 1

        for err in folder_errors:
            st.error(f"Ordner-Fehler — {err}")

        st.caption(
            f"Gescannt: {len(folder_ids)} Ordner · "
            f"{total_files} Datei(en) mit `MetaAd-…_` Prefix · "
            f"{total_bare} Datei(en) nur mit `MetaAd-{{ID}}` · "
            f"{len(files_by_meta_id)} eindeutige Meta-IDs (Vollname)."
        )

        candidates: list[dict] = []
        for i, row_vals in enumerate(bcm, start=1):  # i = 1-based sheet row
            def _cell(idx: int) -> str:
                return row_vals[idx].strip() if len(row_vals) > idx else ""

            # bcm = B:M → B=0, C=1, D=2, E=3, F=4, G=5, …, M=11
            meta_id     = _cell(0)
            fmt         = _cell(1)
            product     = _cell(2)
            description = _cell(3)
            creator     = _cell(4)
            version     = _cell(5)
            link        = _cell(11)

            if not _META_ID_RE.match(meta_id):
                continue
            if link:
                continue

            matches = files_by_meta_id.get(meta_id, [])

            # Mehrdeutigkeit erst per Format-Prefix entschärfen (z.B. MetaAd-1234_UGC-30_…)
            if len(matches) > 1 and fmt:
                fmt_filtered = [
                    f for f in matches
                    if f["name"].startswith(f"{meta_id}_{fmt}_")
                ]
                if len(fmt_filtered) == 1:
                    matches = fmt_filtered

            # Static-Pärchen-Heuristik: Bei STA-Format und genau 2 Treffern, die nur
            # durch den "-N"-Suffix vor der Endung unterscheiden (z.B. _V4.png +
            # _V4-1.png) → primärer Link (ohne Suffix) wird gewählt. Die zweite
            # Aspect-Ratio-Variante wird beim Upload automatisch via
            # find_complementary_static_url() dazugesucht.
            if len(matches) > 1 and fmt.startswith("STA"):
                def _is_alt_variant(name: str) -> bool:
                    # Pattern: ..._VN-M.ext (z.B. _V4-1.png, _V10-2.jpg)
                    return bool(re.search(r"_V\d+-\d+\.[A-Za-z0-9]+$", name))
                primaries = [f for f in matches if not _is_alt_variant(f["name"])]
                if len(primaries) == 1:
                    matches = primaries

            if len(matches) == 1:
                file = matches[0]
                candidates.append({
                    "sheet_row":  i,
                    "meta_id":    meta_id,
                    "fmt":        fmt,
                    "file_name":  file["name"],
                    "new_name":   "",
                    "drive_link": make_drive_link(file["id"]),
                    "rename":     None,
                    "status":     "✅ gefunden",
                })
                continue
            if len(matches) > 1:
                candidates.append({
                    "sheet_row":  i,
                    "meta_id":    meta_id,
                    "fmt":        fmt,
                    "file_name":  ", ".join(m["name"] for m in matches),
                    "new_name":   "",
                    "drive_link": "",
                    "rename":     None,
                    "status":     f"❓ mehrdeutig ({len(matches)} Treffer)",
                })
                continue

            # Kein Vollnamen-Treffer → Dateien prüfen, die nur die ID tragen
            bare = bare_files_by_meta_id.get(meta_id, [])
            if len(bare) == 1:
                file = bare[0]
                if all([fmt, product, description, creator, version]):
                    new_base = build_ad_name(
                        meta_id, fmt, product, description, creator, version
                    )
                    new_name = new_base + get_file_extension(file["name"])
                    candidates.append({
                        "sheet_row":  i,
                        "meta_id":    meta_id,
                        "fmt":        fmt,
                        "file_name":  file["name"],
                        "new_name":   new_name,
                        "drive_link": make_drive_link(file["id"]),
                        "rename":     {"file_id": file["id"], "new_name": new_name},
                        "status":     "🔧 umbenennen + Link",
                    })
                else:
                    candidates.append({
                        "sheet_row":  i,
                        "meta_id":    meta_id,
                        "fmt":        fmt,
                        "file_name":  file["name"],
                        "new_name":   "",
                        "drive_link": "",
                        "rename":     None,
                        "status":     "⚠️ Name unvollständig (C–G fehlen)",
                    })
            elif len(bare) > 1:
                candidates.append({
                    "sheet_row":  i,
                    "meta_id":    meta_id,
                    "fmt":        fmt,
                    "file_name":  ", ".join(m["name"] for m in bare),
                    "new_name":   "",
                    "drive_link": "",
                    "rename":     None,
                    "status":     f"❓ mehrdeutig ({len(bare)} Treffer)",
                })
            else:
                candidates.append({
                    "sheet_row":  i,
                    "meta_id":    meta_id,
                    "fmt":        fmt,
                    "file_name":  "—",
                    "new_name":   "",
                    "drive_link": "",
                    "rename":     None,
                    "status":     "⚠️ nicht gefunden",
                })

    st.session_state["asset_sync_candidates"] = candidates


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Vorschau + Schreiben
# ─────────────────────────────────────────────────────────────────────────────
candidates = st.session_state.get("asset_sync_candidates") or []

if candidates:
    st.divider()
    st.subheader("Schritt 2: Vorschau prüfen & Links eintragen")

    ok_rows       = [c for c in candidates if c["status"].startswith("✅")]
    rename_rows   = [c for c in candidates if c["status"].startswith("🔧")]
    missing_rows  = [c for c in candidates if c["status"].startswith("⚠️")]
    ambig_rows    = [c for c in candidates if c["status"].startswith("❓")]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Gefunden",       len(ok_rows))
    c2.metric("Umbenennen",     len(rename_rows))
    c3.metric("Nicht gefunden", len(missing_rows))
    c4.metric("Mehrdeutig",     len(ambig_rows))

    df_cols = ["sheet_row", "meta_id", "fmt", "file_name", "new_name", "status"]
    st.dataframe(
        pd.DataFrame(candidates)[df_cols],
        width="stretch",
        hide_index=True,
        column_config={
            "sheet_row": st.column_config.NumberColumn("Zeile", format="%d"),
            "meta_id":   "Meta-ID",
            "fmt":       "Format",
            "file_name": "Drive-Datei",
            "new_name":  "Neuer Name",
            "status":    "Status",
        },
    )

    write_rows = ok_rows + rename_rows
    if not write_rows:
        st.info("Keine eindeutigen Treffer — nichts zu schreiben.")
    else:
        btn_label = f"✏️ {len(write_rows)} Link(s) in Spalte M eintragen"
        if rename_rows:
            btn_label += f" ({len(rename_rows)} Datei(en) vorher umbenennen)"
        if st.button(btn_label, type="primary", width="stretch"):
            _, sheet = require_connections()
            ws = get_naming_worksheet(sheet, config.NAMING_SHEET_GID)

            # 1) Dateien, die nur die ID tragen, zuerst umbenennen.
            #    Nur erfolgreich umbenannte Zeilen bekommen anschließend den Link.
            rename_errors: list[str] = []
            renamed_ok: list[dict] = []
            if rename_rows:
                drive = get_drive_service(config.GOOGLE_SERVICE_ACCOUNT_FILE)
                with st.spinner(f"Benenne {len(rename_rows)} Datei(en) um…"):
                    for c in rename_rows:
                        try:
                            rename_file(
                                drive,
                                c["rename"]["file_id"],
                                c["rename"]["new_name"],
                            )
                            renamed_ok.append(c)
                        except Exception as e:
                            rename_errors.append(f"Zeile {c['sheet_row']}: {e}")

            # 2) Links für gefundene + erfolgreich umbenannte Zeilen schreiben.
            link_rows = ok_rows + renamed_ok
            updates = [
                {"range": f"M{c['sheet_row']}", "values": [[c["drive_link"]]]}
                for c in link_rows
            ]

            try:
                if updates:
                    with st.spinner(f"Schreibe {len(updates)} Zelle(n)…"):
                        ws.batch_update(updates, value_input_option="USER_ENTERED")
                st.success(
                    f"✅ {len(updates)} Link(s) eingetragen "
                    f"({len(renamed_ok)} Datei(en) umbenannt)."
                )
                if rename_errors:
                    st.error(
                        "Umbenennen fehlgeschlagen — Link nicht eingetragen:\n"
                        + "\n".join(f"• {err}" for err in rename_errors)
                    )
                if missing_rows or ambig_rows:
                    st.warning(
                        f"⚠️ {len(missing_rows)} nicht gefunden/unvollständig, "
                        f"❓ {len(ambig_rows)} mehrdeutig — diese Zeilen bleiben leer."
                    )
                if not rename_errors:
                    del st.session_state["asset_sync_candidates"]
            except Exception as e:
                st.error(f"Schreiben fehlgeschlagen: {e}")
