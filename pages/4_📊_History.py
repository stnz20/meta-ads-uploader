from dotenv import load_dotenv
load_dotenv()

import streamlit as st
import pandas as pd
import gspread

from components.auth import require_connections

st.set_page_config(page_title="History — Mammaly Meta Ads", page_icon="📊", layout="wide")
st.title("📊 Upload-History")

_, sheet = require_connections()

try:
    records = sheet.worksheet("log").get_all_records()
except gspread.WorksheetNotFound:
    st.info("Log-Tab noch nicht vorhanden.")
    st.stop()
except Exception as e:
    st.error(f"Fehler beim Laden des Logs: {e}")
    st.stop()

if not records:
    st.info("Noch keine Einträge im Log.")
    st.stop()

df = pd.DataFrame(records)

# ── Filter ────────────────────────────────────────────────────────────────────
c1, c2 = st.columns(2)
with c1:
    result_col = "result" if "result" in df.columns else None
    if result_col:
        status_opts = ["Alle", "DONE", "ERROR"]
        status_filter = st.selectbox("Status", status_opts)
    else:
        status_filter = "Alle"
with c2:
    search = st.text_input("Suche", placeholder="Ad-ID, Name, Aktion…")

filtered = df.copy()
if status_filter != "Alle" and result_col:
    filtered = filtered[filtered[result_col].str.contains(status_filter, na=False)]
if search:
    mask = filtered.apply(lambda row: row.astype(str).str.contains(search, case=False).any(), axis=1)
    filtered = filtered[mask]

st.caption(f"{len(filtered)} von {len(df)} Einträge(n)")
st.dataframe(filtered, width="stretch", hide_index=True)

st.download_button(
    "📥 CSV exportieren",
    data=filtered.to_csv(index=False),
    file_name="upload_log.csv",
    mime="text/csv",
)
