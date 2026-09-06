from dotenv import load_dotenv
load_dotenv()

import os
import tempfile

import pandas as pd
import streamlit as st

import config
from auth.drive_user_oauth import get_user_drive_service, missing_oauth_vars
from ad_namer.drive import upload_file_to_folder, make_drive_link
from frameio import FrameioShareError, list_share_assets, parse_share_url
from frameio.client import resolve_download_url, download_asset

st.set_page_config(page_title="Frame.io Import — Mammaly Meta Ads", page_icon="🎬", layout="wide")
st.title("🎬 Frame.io Import")
st.caption(
    "Überträgt Creatives aus einem oder mehreren Frame.io-Share-Links serverseitig in "
    "einen Drive-Zwischen-Ordner — dein lokales Internet wird nicht genutzt. Die Dateien "
    "behalten hier ihren Original-Namen; das **Umbenennen in die Namenskonvention** und "
    "das Eintragen ins Sheet macht danach die Seite **📁 Ad Naming** (Quelle = "
    "Zwischen-Ordner)."
)

# ── Feature-Gate: ohne die optionalen Secrets bleibt nur diese Seite deaktiviert ──
_missing = missing_oauth_vars()
if not config.FRAMEIO_STAGING_FOLDER_ID:
    _missing.append("FRAMEIO_STAGING_FOLDER_ID")
if _missing:
    st.warning(
        "Diese Seite ist noch nicht konfiguriert. Fehlende Keys in den Secrets "
        "(Streamlit Cloud → Settings → Secrets bzw. lokale .env):\n\n"
        + "\n".join(f"- `{k}`" for k in _missing)
        + "\n\n`FRAMEIO_STAGING_FOLDER_ID` = ID eines neuen Drive-Ordners (z. B. "
        "„Frame.io Import“). Refresh Token erzeugen: `python3 mint_drive_refresh_token.py`."
    )
    st.stop()

_STAGING_FOLDER_ID = config.FRAMEIO_STAGING_FOLDER_ID
_STAGING_URL = f"https://drive.google.com/drive/folders/{_STAGING_FOLDER_ID}"
_SIZE_WARN_BYTES = 2 * 1024**3  # ~2 GB — Streamlit-Cloud-Disk/RAM-Puffer


def _human_size(n: int) -> str:
    if not n:
        return "–"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _clean_token(raw: str) -> str:
    t = (raw or "").strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return t


def _token_field(key: str) -> str:
    """Token-Eingabe, die den zuletzt genutzten Token dieser Session vorschlägt."""
    st.session_state.setdefault(key, st.session_state.get("fio_last_token", ""))
    return st.text_input(
        "Frame.io Zugangs-Token", type="password", key=key,
        help="Der Bearer-Token aus dem Browser — gilt ca. 8 Stunden. Wird innerhalb "
             "der Sitzung als Vorschlag gemerkt.",
    )


def _remember_token(clean: str) -> None:
    if clean:
        st.session_state["fio_last_token"] = clean


@st.cache_resource
def _user_drive():
    return get_user_drive_service()


def _token_help():
    with st.expander("🔑 Wie komme ich an den Zugangs-Token? (einmal pro ~8 h)"):
        st.markdown(
            "Frame.io gibt Share-Inhalte nur mit einem kurzlebigen Zugangs-Token heraus. "
            "So holst du ihn aus dem Browser:\n\n"
            "1. Öffne den **Share-Link** im Chrome und drücke **F12** → Tab **Network**\n"
            "2. Tippe ins Filterfeld `graphql` und lade die Seite neu (**Cmd+R**)\n"
            "3. Klick einen **graphql**-Eintrag an → Tab **Headers** → Abschnitt "
            "**Request Headers**\n"
            "4. Kopiere den Wert von **`authorization`** (`Bearer eyJ…`). Du kannst die "
            "ganze Zeile einfügen — das `Bearer ` entferne ich automatisch.\n\n"
            "Der Token gilt nur für diesen Share (bzw. ggf. für alle Shares desselben "
            "Frame.io-Accounts) und läuft nach ~8 h ab."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Gemeinsamer Transfer: eine Job-Liste sequenziell Frame.io → Drive
# job = {"asset": FrameioAsset, "ref": ShareRef, "token": str}
# ─────────────────────────────────────────────────────────────────────────────
def _run_transfer(jobs: list[dict], dest_folder_id: str) -> list[dict]:
    drive = _user_drive()
    progress = st.progress(0.0)
    status_box = st.empty()
    results: list[dict] = []
    n = len(jobs)

    for i, job in enumerate(jobs):
        asset = job["asset"]
        status_box.info(f"Übertrage {i + 1}/{n}: {asset.name} …")
        ext = os.path.splitext(asset.name)[1]
        tmp_path = os.path.join(tempfile.gettempdir(), f"fio_{asset.id}{ext}")
        try:
            url = resolve_download_url(job["ref"], asset, job["token"])

            def _on_progress(done: int, total: int, _i=i):
                frac = (done / total) if total else 0.0
                progress.progress(min((_i + frac * 0.5) / n, 1.0))

            download_asset(url, tmp_path, _on_progress)
            progress.progress(min((i + 0.5) / n, 1.0))

            status_box.info(f"Lade {i + 1}/{n} nach Drive hoch: {asset.name} …")
            meta = upload_file_to_folder(drive, tmp_path, asset.name, dest_folder_id)
            results.append({"name": asset.name, "ok": True,
                            "link": make_drive_link(meta["id"])})
        except Exception as e:
            results.append({"name": asset.name, "ok": False, "error": str(e)})
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        progress.progress((i + 1) / n)

    status_box.empty()
    return results


def _show_results(results: list[dict]):
    ok = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    if ok:
        st.success(f"✅ {len(ok)} Datei(en) übertragen nach Drive:")
        for r in ok:
            st.markdown(f"- [{r['name']}]({r['link']})")
    if failed:
        st.error(f"❌ {len(failed)} Datei(en) fehlgeschlagen:")
        for r in failed:
            st.markdown(f"- **{r['name']}**: {r['error']}")
    if ok:
        st.info(
            "Nächster Schritt: **📁 Ad Naming** öffnen und als Quelle den "
            f"[Frame.io-Import-Ordner]({_STAGING_URL}) angeben — dort werden die Dateien "
            "umbenannt, ins Sheet eingetragen und in die UGC-/Static-Ablage verschoben."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Modus
# ─────────────────────────────────────────────────────────────────────────────
mode = st.radio("Modus", ["Einzelner Link", "Mehrere Links (Batch)"], horizontal=True)
_token_help()

# ═════════════════════════════════════════════════════════════════════════════
# EINZELNER LINK
# ═════════════════════════════════════════════════════════════════════════════
if mode == "Einzelner Link":
    st.subheader("Schritt 1: Frame.io-Share laden")
    share_url = st.text_input("Frame.io Share-Link", placeholder="https://next.frame.io/share/…")
    token = _token_field("fio_token_single")

    if st.button("📥 Assets laden", type="primary", width="stretch"):
        clean = _clean_token(token)
        try:
            ref = parse_share_url(share_url)
            with st.spinner("Lade Asset-Liste von Frame.io …"):
                assets = list_share_assets(ref, clean)
            if not assets:
                st.warning("Der Share enthält keine Dateien (oder Download ist deaktiviert).")
            else:
                _remember_token(clean)
                st.session_state["fio_ref"] = ref
                st.session_state["fio_token"] = clean
                st.session_state["fio_assets"] = assets
                st.session_state.pop("fio_results", None)
        except FrameioShareError as e:
            st.error(str(e))
        except Exception as e:
            st.error(f"Unerwarteter Fehler beim Laden: {e}")

    if st.session_state.get("fio_assets"):
        assets = st.session_state["fio_assets"]
        st.subheader(f"Schritt 2: Auswahl ({len(assets)} Datei(en) im Share)")
        st.caption("Den **Namen** kannst du hier direkt anpassen (wird so in Drive hochgeladen).")
        df = pd.DataFrame([
            {"Übertragen": True, "Name": a.name, "Ordner": a.path or "—",
             "Typ": a.media_type, "Größe": _human_size(a.size_bytes)}
            for a in assets
        ])
        edited = st.data_editor(df, hide_index=True, width="stretch",
                                disabled=["Ordner", "Typ", "Größe"], key="fio_editor")
        records = edited.to_dict("records")
        selected = []
        for a, row in zip(assets, records):
            if row["Übertragen"]:
                new_name = str(row["Name"]).strip()
                if new_name:
                    a.name = new_name  # editierten Namen übernehmen
                selected.append(a)

        big = [a for a in selected if a.size_bytes > _SIZE_WARN_BYTES]
        if big:
            st.warning("Sehr große Datei(en) (> 2 GB) — kann am Speicherlimit scheitern: "
                       + ", ".join(a.name for a in big))

        st.caption(f"Ziel: [Frame.io-Import-Ordner in Drive]({_STAGING_URL})")

        if st.button(f"🚀 Transfer starten ({len(selected)} Datei(en))",
                     type="primary", width="stretch", disabled=not selected):
            ref = st.session_state["fio_ref"]
            token = st.session_state.get("fio_token", "")
            jobs = [{"asset": a, "ref": ref, "token": token} for a in selected]
            st.session_state["fio_results"] = _run_transfer(jobs, _STAGING_FOLDER_ID)

    if st.session_state.get("fio_results"):
        _show_results(st.session_state["fio_results"])

# ═════════════════════════════════════════════════════════════════════════════
# BATCH — mehrere Links
# ═════════════════════════════════════════════════════════════════════════════
else:
    st.subheader("Schritt 1: Links + Tokens eingeben")
    st.caption(
        "Pro Zeile ein Share-Link und der zugehörige Zugangs-Token. "
        "**Tipp:** Wenn alle Links aus demselben Frame.io-Account kommen, reicht oft ein "
        "einziger Token — trag ihn dann als globalen Token ein und lass die Token-Spalte leer."
    )
    st.session_state.setdefault("fio_token_batch", st.session_state.get("fio_last_token", ""))
    global_token = st.text_input(
        "Globaler Token (optional)", type="password", key="fio_token_batch",
        help="Wird für alle Zeilen ohne eigenen Token verwendet. Innerhalb der Sitzung "
             "als Vorschlag gemerkt.",
    )

    empty = pd.DataFrame([{"Link": "", "Token": ""} for _ in range(3)])
    grid = st.data_editor(empty, num_rows="dynamic", width="stretch", hide_index=True,
                          key="fio_batch_grid",
                          column_config={
                              "Link": st.column_config.TextColumn("Share-Link", width="large"),
                              "Token": st.column_config.TextColumn("Token (optional)", width="medium"),
                          })

    if st.button("📥 Assets aller Links laden", type="primary", width="stretch"):
        gtok = _clean_token(global_token)
        rows = [r for r in grid.to_dict("records") if (r.get("Link") or "").strip()]
        batch: list[dict] = []
        errors: list[str] = []
        with st.spinner(f"Lade {len(rows)} Link(s) …"):
            for idx, r in enumerate(rows, 1):
                link = r["Link"].strip()
                tok = _clean_token(r.get("Token") or "") or gtok
                label = f"Link {idx}"
                try:
                    ref = parse_share_url(link)
                    if not tok:
                        raise FrameioShareError("Kein Token (weder Zeile noch global).")
                    for a in list_share_assets(ref, tok):
                        batch.append({"asset": a, "ref": ref, "token": tok, "share": label})
                except FrameioShareError as e:
                    errors.append(f"{label}: {e}")
                except Exception as e:
                    errors.append(f"{label}: Unerwarteter Fehler — {e}")
        for e in errors:
            st.error(e)
        if batch:
            _remember_token(gtok)
            st.session_state["fio_batch"] = batch
            st.session_state.pop("fio_batch_results", None)
        elif not errors:
            st.warning("Keine Dateien gefunden.")

    if st.session_state.get("fio_batch"):
        batch = st.session_state["fio_batch"]
        st.subheader(f"Schritt 2: Auswahl ({len(batch)} Datei(en) aus allen Links)")
        st.caption("Den **Namen** kannst du hier direkt anpassen (wird so in Drive hochgeladen).")
        df = pd.DataFrame([
            {"Übertragen": True, "Share": j["share"], "Name": j["asset"].name,
             "Ordner": j["asset"].path or "—", "Typ": j["asset"].media_type,
             "Größe": _human_size(j["asset"].size_bytes)}
            for j in batch
        ])
        edited = st.data_editor(df, hide_index=True, width="stretch",
                                disabled=["Share", "Ordner", "Typ", "Größe"],
                                key="fio_batch_editor")
        selected = []
        for j, row in zip(batch, edited.to_dict("records")):
            if row["Übertragen"]:
                new_name = str(row["Name"]).strip()
                if new_name:
                    j["asset"].name = new_name  # editierten Namen übernehmen
                selected.append(j)

        big = [j["asset"] for j in selected if j["asset"].size_bytes > _SIZE_WARN_BYTES]
        if big:
            st.warning("Sehr große Datei(en) (> 2 GB) — kann am Speicherlimit scheitern: "
                       + ", ".join(a.name for a in big))

        st.caption(f"Ziel: [Frame.io-Import-Ordner in Drive]({_STAGING_URL})")

        if st.button(f"🚀 Transfer starten ({len(selected)} Datei(en))",
                     type="primary", width="stretch", disabled=not selected):
            st.session_state["fio_batch_results"] = _run_transfer(selected, _STAGING_FOLDER_ID)

    if st.session_state.get("fio_batch_results"):
        _show_results(st.session_state["fio_batch_results"])
