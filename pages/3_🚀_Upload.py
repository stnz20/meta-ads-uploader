from dotenv import load_dotenv
load_dotenv()

import logging
import queue
import threading
import time
from datetime import datetime, timedelta

import streamlit as st
import pandas as pd
import gspread

import config
from components.auth import require_connections
from ad_namer import batch_prepare
from ad_namer.sheet_ops import get_naming_worksheet
from sheets.reader import with_backoff
import uploader as _uploader

# Cache-TTL für Sheet-Reads — verhindert dass jeder Streamlit-Rerun
# (z.B. Klick im Editor) neue API-Reads triggert und die 60/min-Quota sprengt.
_CACHE_TTL_SECONDS = 30


def _cached_read(key: str, loader):
    """Cached den Loader-Returnwert pro Streamlit-Session für _CACHE_TTL_SECONDS.

    Bei jeder Aktion die das Sheet ändert (Upload, Vorbereitung) wird der Cache
    via st.session_state[key+"_invalidate"] = True invalidiert.
    """
    cache_key = f"_sheet_cache_{key}"
    time_key = f"_sheet_cache_time_{key}"
    invalidate_key = f"_sheet_cache_invalidate_{key}"

    if st.session_state.get(invalidate_key):
        st.session_state.pop(cache_key, None)
        st.session_state.pop(time_key, None)
        st.session_state[invalidate_key] = False

    cached = st.session_state.get(cache_key)
    cached_at = st.session_state.get(time_key, 0)
    if cached is not None and (time.time() - cached_at) < _CACHE_TTL_SECONDS:
        return cached

    value = loader()
    st.session_state[cache_key] = value
    st.session_state[time_key] = time.time()
    return value


def _invalidate_all_caches() -> None:
    for k in list(st.session_state.keys()):
        if k.startswith("_sheet_cache_") and not k.startswith("_sheet_cache_invalidate_"):
            st.session_state.pop(k, None)
        if k.startswith("_sheet_cache_time_"):
            st.session_state.pop(k, None)


@st.cache_data(ttl=300, show_spinner=False)
def _attr_status_cached(adset_id: str) -> dict:
    """Attribution-Status eines Referenz-Ad-Sets, gecacht (ändert sich selten).

    Testing-Ad-Sets sollen 7-Tage-Klick haben — siehe Hinweis im Editor.
    """
    from meta.reference_fetcher import fetch_attribution_status
    return fetch_attribution_status(adset_id)


def _attr_display(adset_id: str) -> str:
    """Kompaktes Attributions-Label fürs Editor-Feld (✅ = 7d-click, ⚠️ = abweichend)."""
    if not adset_id:
        return ""
    s = _attr_status_cached(adset_id)
    if not s.get("ok"):
        return "❓ n/v"
    if s["is_7day_click"]:
        return "✅ 7d-click"
    return f"⚠️ {s['label'] or 'kein 7d-click'}"

st.set_page_config(page_title="Upload — Meta Ads Uploader", page_icon="🚀", layout="wide")
st.title("🚀 Upload")
st.caption("Ads aus dem Sheet zu Meta hochladen")

account, sheet = require_connections()


class _QueueHandler(logging.Handler):
    """Schreibt Log-Records in eine Queue — für Live-Ausgabe im UI."""
    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        self.log_queue.put(self.format(record))


# ── Naming Convention → ad_sets / ads vorbereiten ────────────────────────────
#
# Liest ready-Zeilen aus dem Naming-Convention-Tab und schreibt sie nach Klick
# direkt in ad_sets/ads — kein Apps-Script-Aufruf im Sheet mehr nötig.

def _id_from_label(label: str) -> str:
    return label.split(" — ", 1)[0].strip() if label else ""


try:
    naming_ws = get_naming_worksheet(sheet, config.NAMING_SHEET_GID)
    ready_rows = _cached_read("naming_ready_rows", lambda: batch_prepare.read_ready_rows(naming_ws))
except Exception as e:
    st.warning(f"Naming Convention konnte nicht gelesen werden: {e}")
    naming_ws = None
    ready_rows = []

# Queued → Live selbstheilend beim Laden: markiert Naming-Zeilen, deren Ads
# inzwischen alle DONE sind — auch wenn es gerade nichts hochzuladen gibt.
# Früher lief dieser Abgleich nur am Ende eines echten Uploads; wurden Ads in
# einer früheren Session hochgeladen (oder bevor es das Feature gab), blieben die
# Naming-Zeilen für immer auf "Queued" hängen. Über _cached_read gedrosselt, damit
# nicht jeder Streamlit-Rerun einen Sheet-Write auslöst.
if naming_ws is not None:
    try:
        n_reconciled = _cached_read(
            "naming_reconcile",
            lambda: batch_prepare.mark_uploaded_done(sheet, naming_ws),
        )
        if n_reconciled:
            st.success(
                f"📋 {n_reconciled} Naming-Zeile(n) → Live abgeglichen "
                f"(Ads waren bereits hochgeladen)."
            )
    except Exception as e:
        st.warning(f"Naming-Status-Abgleich fehlgeschlagen: {e}")

prepare_pending = False

if ready_rows:
    st.subheader("📋 Aus Naming Convention vorbereiten")
    # Gruppieren nach AdSet-Name (nach Versions-Normalisierung)
    grouped: dict[str, list[dict]] = {}
    for r in ready_rows:
        grouped.setdefault(r["adSetName"], []).append(r)

    ad_set_names = list(grouped.keys())
    st.caption(f"{len(ad_set_names)} Ad Set(s) · {len(ready_rows)} Ad(s) bereit")
    st.caption(
        "💡 Referenz wählen: entweder aus den DONE-Ad-Sets (Dropdown) oder eine "
        "beliebige Meta-Ad-Set-ID in **Referenz-ID (Meta)** einfügen — funktioniert "
        "auch mit Ad Sets aus anderen Kampagnen, die nicht im Sheet stehen."
    )
    st.caption(
        "🎯 Referenz-Ad-Sets sollten **7-Tage-Klick**-Attribution haben. Die Spalte "
        "**Attribution** zeigt den Status der vorgeschlagenen Referenz (✅ = 7d-click, "
        "⚠️ = abweichend); markierte Abweichungen werden unter der Tabelle gemeldet."
    )

    # Optionen + Vorschläge laden — ad_sets nur EINMAL lesen, gecacht über Reruns
    try:
        ad_sets_records = _cached_read(
            "ad_sets_records",
            lambda: batch_prepare._all_records_safe(sheet.worksheet(batch_prepare.ADSETS_TAB)),
        )
        ads_records = _cached_read(
            "ads_records",
            lambda: batch_prepare._all_records_safe(sheet.worksheet(batch_prepare.ADS_TAB)),
        )
        ref_options = batch_prepare.done_ad_set_options(sheet, ad_sets_records=ad_sets_records)
        lp_url_by_name = {r["adSetName"]: r["lpUrl"] for r in ready_rows}
        lp_by_name = {r["adSetName"]: r["lpId"] for r in ready_rows}
        # Performance der ABO-Testing-Kampagne (meiste Käufe je Produkt-Ad-Set,
        # letzte 7 Tage) — Basis für den Referenz-Vorschlag. Fehlschlag ist unkritisch:
        # suggest_references fällt dann auf "letztes pro Produkt / LP" zurück.
        purchases_by_adset = {}
        if config.ABO_TESTING_CAMPAIGN_ID:
            try:
                from meta.reference_fetcher import fetch_adset_purchases
                purchases_by_adset = _cached_read(
                    "abo_testing_purchases",
                    lambda: fetch_adset_purchases(
                        config.ABO_TESTING_CAMPAIGN_ID, date_preset="last_7d"
                    ),
                )
            except Exception as e:
                logging.warning("Testing-Performance nicht ladbar: %s", e)
        suggestions = batch_prepare.suggest_references(
            sheet, ad_set_names,
            lp_url_by_name=lp_url_by_name, lp_by_name=lp_by_name,
            ad_sets_records=ad_sets_records, ads_records=ads_records,
            purchases_by_adset=purchases_by_adset,
        )
    except Exception as e:
        st.error(f"Referenzen konnten nicht geladen werden: {e}")
        ref_options = []
        suggestions = {}

    ref_labels = [""] + [label for _src, label in ref_options]
    label_by_id = {src: label for src, label in ref_options}

    # Default-Tabelle bauen
    df_rows = []
    for name in ad_set_names:
        suggested_id = suggestions.get(name, "")
        suggested_label = label_by_id.get(suggested_id, suggested_id) if suggested_id else ""
        n_ads = len(grouped[name])
        df_rows.append({
            "Upload": False,
            "Ad Set": name,
            "# Ads": n_ads,
            "Budget (€)": 25 * n_ads,
            "3-2-2 Modus": False,
            "Referenz-Ad-Set": suggested_label,
            "Referenz-ID (Meta)": "",
            "Attribution": _attr_display(suggested_id),
            "Advertiser": "",
            "Page Override": "",
            "IG Override": "",
            "Display Link": "",
        })

    start_iso = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")

    c_left, c_right = st.columns([3, 1])
    with c_right:
        st.caption(f"⏱️ Startzeit: {start_iso}  (Upload-Zeit + 2 Std.)")

    with c_left:
        edited = st.data_editor(
            pd.DataFrame(df_rows),
            key="prepare_editor",
            width="stretch",
            hide_index=True,
            disabled=["Ad Set", "# Ads", "Attribution"],
            column_config={
                "Upload": st.column_config.CheckboxColumn(
                    help="Markieren = dieses Ad-Set hochladen.",
                    width="small",
                    default=False,
                ),
                "Ad Set": st.column_config.TextColumn(width="large"),
                "# Ads": st.column_config.NumberColumn(width="small"),
                "Budget (€)": st.column_config.NumberColumn(
                    help="25 € pro Ad vorausgefüllt. Bei CBO-Kampagnen leeren — Meta ignoriert das Ad-Set-Budget dann.",
                    min_value=0,
                    step=5,
                    width="small",
                ),
                "3-2-2 Modus": st.column_config.CheckboxColumn(
                    help="Dynamic Creative: 1 Ad mit allen Assets + 2 Bodies + 2 Headlines (Prof. T 3-2-2). Sonst klassisch 1 Ad pro Asset.",
                    width="small",
                    default=False,
                ),
                "Referenz-Ad-Set": st.column_config.SelectboxColumn(
                    options=ref_labels,
                    help="Aus bestehenden DONE-Ad-Sets wählen",
                    width="medium",
                ),
                "Referenz-ID (Meta)": st.column_config.TextColumn(
                    help="Beliebige Meta-Ad-Set-ID aus IRGENDEINER Kampagne einfügen. "
                         "Wenn gesetzt, hat dieses Feld Vorrang vor der Dropdown-Auswahl — "
                         "Kampagne/Page/IG/CTA werden direkt von Meta gezogen.",
                    width="medium",
                ),
                "Attribution": st.column_config.TextColumn(
                    help="Attribution der VORGESCHLAGENEN Referenz. ✅ = 7-Tage-Klick, "
                         "⚠️ = weicht ab, ❓ = nicht lesbar. Gilt für den Dropdown-Vorschlag; "
                         "bei manueller Referenz-ID siehe Hinweis unter der Tabelle.",
                    width="small",
                ),
                "Advertiser": st.column_config.TextColumn(width="small"),
                "Page Override": st.column_config.TextColumn(width="small"),
                "IG Override": st.column_config.TextColumn(width="small"),
                "Display Link": st.column_config.TextColumn(width="small"),
            },
        )

    # Picks aus dem Editor in das references-dict übersetzen
    picked_references: dict[str, dict] = {}
    for _, r in edited.iterrows():
        # Nur markierte Ad-Sets vorbereiten
        if not bool(r.get("Upload", False)):
            continue
        # Budget (€) → Cent; leer/0 = nicht setzen (z. B. CBO-Kampagnen)
        budget_raw = r.get("Budget (€)")
        budget_eur = 0.0 if pd.isna(budget_raw) else float(budget_raw or 0)
        daily_budget = int(round(budget_eur * 100)) if budget_eur > 0 else ""
        # Manuelle Meta-ID hat Vorrang vor der Dropdown-Auswahl
        manual_raw = r.get("Referenz-ID (Meta)")
        manual_id = "" if pd.isna(manual_raw) else str(manual_raw).strip()
        if manual_id:
            ref_id = manual_id
        else:
            raw = r["Referenz-Ad-Set"]
            if pd.isna(raw):
                continue
            ref_id = _id_from_label(str(raw))
        if not ref_id or ref_id.lower() == "nan":
            continue
        picked_references[str(r["Ad Set"])] = {
            "refAdSet": ref_id,
            "advertiser": str(r["Advertiser"] or "").strip(),
            "pageOverride": str(r["Page Override"] or "").strip(),
            "igOverride": str(r["IG Override"] or "").strip(),
            "displayLink": str(r["Display Link"] or "").strip(),
            "dcoMode": bool(r.get("3-2-2 Modus", False)),
            "dailyBudget": daily_budget,
        }

    # Attribution-Check der TATSÄCHLICH markierten Referenzen (deckt auch manuell
    # eingegebene Referenz-IDs ab, die in der Editor-Spalte nicht live aktualisiert wird).
    attr_deviations = []
    for adset_name, cfg in picked_references.items():
        ref_id = cfg["refAdSet"]
        s = _attr_status_cached(ref_id)
        if not s.get("ok"):
            attr_deviations.append(f"- **{adset_name}** → Referenz `{ref_id}`: Attribution nicht lesbar (❓)")
        elif not s["is_7day_click"]:
            attr_deviations.append(
                f"- **{adset_name}** → Referenz `{ref_id}`: **{s['label'] or 'unbekannt'}** — kein 7-Tage-Klick"
            )
    if attr_deviations:
        st.warning(
            "⚠️ **Referenz-Attribution weicht von 7-Tage-Klick ab** "
            "(Testing-Ad-Sets sollten 7d-click sein):\n\n" + "\n".join(attr_deviations)
        )

    n_picked = len(picked_references)
    n_unpicked = len(ad_set_names) - n_picked
    if n_picked == 0:
        st.info("Kein Ad-Set markiert. Markiere die gewünschten Ad-Sets in der Spalte **Upload** und wähle eine Referenz.")
    elif n_unpicked > 0:
        st.warning(f"{n_unpicked} Ad Set(s) nicht markiert oder ohne Referenz — werden übersprungen.")
    else:
        st.success(f"Alle {n_picked} Ad Set(s) markiert und mit Referenz.")

    prepare_pending = n_picked > 0
    prepare_state = {
        "naming_ws": naming_ws,
        "ready_rows": ready_rows,
        "references": picked_references,
        "start_date": start_iso,
    }

    st.divider()


def _save_pending_edits(
    ws: "gspread.Worksheet",
    headers: list[str],
    orig_by_row: dict[int, dict],
    edited: pd.DataFrame,
) -> None:
    """Synchronisiert die editierte Tabelle zurück ins Sheet:
    geänderte Zellen aktualisieren, neue Zeilen anhängen, gelöschte Zeilen entfernen."""
    def _clean(v):
        return "" if (v is None or (isinstance(v, float) and pd.isna(v))) else v

    seen_rows: set[int] = set()
    cell_updates: list[dict] = []
    new_rows: list[list] = []

    for _, erow in edited.iterrows():
        raw_row = erow.get("_row")
        has_row = pd.notna(raw_row) and str(raw_row).strip() != ""
        if has_row:
            row_num = int(raw_row)
            seen_rows.add(row_num)
            orig = orig_by_row.get(row_num, {})
            for h in headers:
                new_val = _clean(erow.get(h, ""))
                if str(new_val) != str(orig.get(h, "")):
                    col_idx = headers.index(h) + 1
                    a1 = gspread.utils.rowcol_to_a1(row_num, col_idx)
                    cell_updates.append({"range": a1, "values": [[new_val]]})
        else:
            row_vals = [_clean(erow.get(h, "")) for h in headers]
            if any(str(v).strip() for v in row_vals):  # komplett leere Zeilen ignorieren
                new_rows.append(row_vals)

    deleted_rows = sorted(set(orig_by_row) - seen_rows, reverse=True)

    if cell_updates:
        with_backoff(lambda: ws.batch_update(cell_updates, value_input_option="USER_ENTERED"))
    if new_rows:
        with_backoff(lambda: ws.append_rows(new_rows, value_input_option="USER_ENTERED"))
    for row_num in deleted_rows:  # von unten nach oben, damit Indizes nicht verrutschen
        with_backoff(lambda rn=row_num: ws.delete_rows(rn))


def _show_pending_table(tab_name: str, label: str) -> int:
    """Editierbare Vorschau der offenen Zeilen eines Tabs. Änderungen (inkl. neuer/
    gelöschter Zeilen) werden über den Speichern-Button ins Google Sheet geschrieben."""
    try:
        ws = sheet.worksheet(tab_name)
        records = _cached_read(
            f"pending_{tab_name}",
            lambda: with_backoff(ws.get_all_records),
        )
        headers = _cached_read(
            f"headers_{tab_name}",
            lambda: with_backoff(lambda: ws.row_values(1)),
        )
        # Sheet-Zeilennummer je Record (Header = Zeile 1, Daten ab Zeile 2)
        pending = [
            (idx + 2, r) for idx, r in enumerate(records)
            if str(r.get("status", "")).strip().upper() not in ("DONE", "ERROR")
        ]
        count = len(pending)
        st.metric(label, count)
        if not pending:
            return count

        orig_by_row = {row_num: rec for row_num, rec in pending}
        df = pd.DataFrame(
            [{**{h: rec.get(h, "") for h in headers}, "_row": row_num} for row_num, rec in pending],
            columns=headers + ["_row"],
        )
        edited = st.data_editor(
            df,
            key=f"edit_{tab_name}",
            width="stretch",
            hide_index=True,
            num_rows="dynamic",
            column_config={"_row": None},  # Sheet-Zeilennummer ausblenden
        )
        if st.button(f"💾 {label} speichern", key=f"save_{tab_name}"):
            try:
                _save_pending_edits(ws, headers, orig_by_row, edited)
                _invalidate_all_caches()
                st.success(f"{label}-Tab gespeichert.")
                st.rerun()
            except Exception as e:
                st.error(f"Speichern fehlgeschlagen: {e}")
        return count
    except Exception as e:
        st.warning(f"{tab_name}-Tab: {e}")
        return 0


# ── Übersicht ────────────────────────────────────────────────────────────────
st.subheader("Bereit zum Hochladen")
st.caption(
    "💡 Tabellen sind editierbar: Werte ändern, Zeilen hinzufügen (unterste Leerzeile) "
    "oder löschen (Zeile markieren → 🗑️), danach **💾 speichern** schreibt alles ins Sheet."
)

with st.expander("📦 Ad Sets", expanded=True):
    n_adsets = _show_pending_table("ad_sets", "Ad Sets")
with st.expander("🖼️ Ads", expanded=True):
    n_ads = _show_pending_table("ads", "Ads")

# ── Live-Fortschritt ─────────────────────────────────────────────────────────
# Liest den Sheet-Status periodisch neu — zeigt damit auch Uploads, die
# außerhalb der App laufen (Cloud Shell / CLI). Baseline = offene Ads beim
# ersten Rendern; Fortschritt = wie viele davon seitdem verarbeitet wurden.
#
# st.cache_data ist app-weit (über alle Sessions/Tabs geteilt): egal wie viele
# Browser das Fragment pollen, es fällt max. 1 Read-Paar pro TTL an — wichtig,
# weil App und CLI-Uploader sich dieselbe Sheets-Quota (60 Reads/min) teilen.

@st.cache_data(ttl=25, show_spinner=False)
def _live_status_counts() -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for tab in (batch_prepare.ADSETS_TAB, batch_prepare.ADS_TAB):
        records = with_backoff(sheet.worksheet(tab).get_all_records)
        pending = error = 0
        for r in records:
            s = str(r.get("status", "")).strip().upper()
            if s == "DONE":
                continue
            if s.startswith("ERROR"):
                error += 1
            else:
                pending += 1
        out[tab] = {"pending": pending, "error": error}
    return out


_fragment = getattr(st, "fragment", None) or st.experimental_fragment


@_fragment(run_every="30s")
def _live_progress() -> None:
    try:
        counts = _live_status_counts()
    except Exception as e:
        st.caption(f"Fortschritt momentan nicht lesbar: {e}")
        return

    ads_pending = counts["ads"]["pending"]
    ads_error = counts["ads"]["error"]
    adsets_pending = counts["ad_sets"]["pending"]
    base = st.session_state.get("_live_progress_baseline")

    if ads_pending == 0 and adsets_pending == 0:
        if base:
            st.session_state.pop("_live_progress_baseline", None)
        st.progress(1.0, text="✅ Keine offenen Zeilen — Upload abgeschlossen.")
        return

    # Baseline setzen bzw. nach oben korrigieren, wenn neue Zeilen gequeued wurden
    if not base or ads_pending > base["pending"]:
        base = {"pending": ads_pending, "error": ads_error}
        st.session_state["_live_progress_baseline"] = base

    processed = base["pending"] - ads_pending
    new_errors = max(ads_error - base["error"], 0)
    frac = (processed / base["pending"]) if base["pending"] else 0.0
    text = f"{processed} von {base['pending']} Ads verarbeitet · {ads_pending} offen"
    if new_errors:
        text += f" · ⚠️ {new_errors} Fehler"
    st.progress(min(max(frac, 0.0), 1.0), text=text)
    extras = []
    if adsets_pending:
        extras.append(f"{adsets_pending} Ad Set(s) offen")
    if new_errors:
        extras.append("Fehler-Details im ads-Tab (status-Spalte) bzw. History")
    if extras:
        st.caption(" · ".join(extras))


if n_adsets > 0 or n_ads > 0:
    st.divider()
    st.subheader("📡 Live-Fortschritt")
    st.caption("Aktualisiert alle 30 s aus dem Sheet — zeigt auch Uploads, die per Cloud Shell/CLI laufen.")
    _live_progress()

if n_adsets == 0 and n_ads == 0 and not prepare_pending:
    st.info("Keine ausstehenden Uploads — alle Zeilen sind DONE oder ERROR.")
    st.stop()

st.divider()

# ── Overrides ─────────────────────────────────────────────────────────────────
with st.expander("🔧 IDs überschreiben (optional)", expanded=False):
    st.caption("Überschreibt source_campaign_id und source_adset_id für alle neuen Ad Sets in diesem Upload.")
    col_a, col_b = st.columns(2)
    with col_a:
        override_campaign_id = st.text_input(
            "Source Campaign ID",
            placeholder="Kampagnen-ID, z.B. 1200000000000000000",
            help="Überschreibt die source_campaign_id aus dem Sheet für alle neuen Ad Sets.",
        ).strip() or None
    with col_b:
        override_adset_id = st.text_input(
            "Source Reference Ad Set ID",
            placeholder="Ad-Set-ID, z.B. 1200000000000000000",
            help="Überschreibt die source_adset_id aus dem Sheet (Referenz für Targeting, Bid-Strategie etc.).",
        ).strip() or None

st.divider()

# ── Aktionen ─────────────────────────────────────────────────────────────────
c1, c2 = st.columns(2)
with c1:
    dry_run_btn = st.button("🧪 Dry Run", width="stretch", help="Durchlaufen ohne Meta API-Calls")
with c2:
    upload_btn = st.button("🚀 Upload starten", type="primary", width="stretch")

if dry_run_btn or upload_btn:
    dry_run = bool(dry_run_btn)

    # Vor dem Upload: ggf. Naming-Convention-Zeilen in ad_sets/ads schreiben
    if prepare_pending:
        with st.spinner("📋 Schreibe Zeilen in ad_sets und ads …"):
            try:
                prep = batch_prepare.generate_upload_rows(
                    sheet,
                    prepare_state["naming_ws"],
                    ready_rows=prepare_state["ready_rows"],
                    references=prepare_state["references"],
                    start_date=prepare_state["start_date"],
                )
            except Exception as e:
                st.error(f"Vorbereitung fehlgeschlagen: {e}")
                st.stop()
        if prep["errors"]:
            st.error("Vorbereitung abgebrochen:\n\n• " + "\n• ".join(prep["errors"]))
            st.stop()
        st.success(
            f"✅ {prep['ad_sets_written']} Ad Set(s) + {prep['ads_written']} Ad(s) geschrieben. "
            f"Naming Tab → Queued."
        )
        if prep["skipped"]:
            st.info("Übersprungen:\n\n• " + "\n• ".join(prep["skipped"]))
        _invalidate_all_caches()

    log_queue: queue.Queue = queue.Queue()
    handler = _QueueHandler(log_queue)
    handler.setFormatter(logging.Formatter("%(levelname)-8s  %(message)s"))

    result: dict = {"error": None}

    # Fortschritt aus dem Upload-Thread — dict-Zuweisungen sind atomar,
    # der UI-Loop unten liest die Werte bei jedem Tick.
    progress_state: dict = {"done": 0, "total": 0, "label": ""}

    def _on_progress(done: int, total: int, label: str) -> None:
        progress_state["done"] = done
        progress_state["total"] = total
        progress_state["label"] = label

    def _run_upload():
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            _uploader.run(
                sheet, account, dry_run=dry_run,
                override_campaign_id=override_campaign_id,
                override_adset_id=override_adset_id,
                progress_cb=_on_progress,
            )
        except Exception as e:
            result["error"] = e
        finally:
            root.removeHandler(handler)
            log_queue.put(None)  # Sentinel — signalisiert Ende

    thread = threading.Thread(target=_run_upload, daemon=True)
    thread.start()

    # ── Live-Log ─────────────────────────────────────────────────────────────
    title = "🧪 Dry Run läuft…" if dry_run else "🚀 Upload läuft…"
    with st.status(title, expanded=True) as status:
        log_lines: list[str] = []
        prog_box = st.empty()
        log_box = st.empty()

        def _render_progress() -> None:
            total = progress_state["total"]
            if not total:
                return
            done = progress_state["done"]
            text = f"{done}/{total}"
            if progress_state["label"]:
                text += f" · zuletzt: {progress_state['label']}"
            prog_box.progress(min(done / total, 1.0), text=text)

        while True:
            try:
                line = log_queue.get(timeout=0.2)
            except queue.Empty:
                # Noch nichts — Progress/Box trotzdem aktualisieren
                _render_progress()
                if log_lines:
                    log_box.code("\n".join(log_lines), language=None)
                continue

            if line is None:  # Sentinel → fertig
                break

            log_lines.append(line)
            _render_progress()
            log_box.code("\n".join(log_lines), language=None)

        thread.join()

        if isinstance(result["error"], _uploader.UploadAlreadyRunning):
            status.update(label="⏳ Upload läuft bereits", state="error", expanded=True)
            st.warning(str(result["error"]))
        elif result["error"]:
            status.update(label="❌ Fehler aufgetreten", state="error", expanded=True)
            st.error(f"Fehler: {result['error']}")
        elif dry_run:
            status.update(label="🧪 Dry Run abgeschlossen", state="complete", expanded=False)
            st.info("Dry Run abgeschlossen — keine Änderungen vorgenommen.")
        else:
            status.update(label="✅ Upload abgeschlossen", state="complete", expanded=False)
            st.success("Upload abgeschlossen! Status wurde im Sheet aktualisiert.")
            # Naming-Tab: Queued → Live für Ad-Sets, deren Ads alle DONE sind
            if not dry_run and naming_ws is not None:
                try:
                    n_marked = batch_prepare.mark_uploaded_done(sheet, naming_ws)
                    if n_marked:
                        st.success(f"📋 {n_marked} Naming-Zeile(n) → Live markiert.")
                except Exception as e:
                    st.warning(f"Naming-Tab-Status konnte nicht aktualisiert werden: {e}")
            _invalidate_all_caches()
            st.rerun()
