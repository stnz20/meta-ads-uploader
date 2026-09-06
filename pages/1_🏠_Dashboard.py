from dotenv import load_dotenv
load_dotenv()

import streamlit as st
from components.auth import status_indicators, require_connections

st.set_page_config(page_title="Dashboard — Mammaly Meta Ads", page_icon="🏠", layout="wide")
st.title("🏠 Dashboard")

st.subheader("Verbindungsstatus")
status_indicators()

st.divider()
st.subheader("Übersicht")

try:
    _, sheet = require_connections()

    col1, col2, col3 = st.columns(3)

    with col1:
        try:
            records = sheet.worksheet("ads").get_all_records()
            pending = sum(1 for r in records if str(r.get("status", "")).strip().upper() not in ("DONE", "ERROR"))
            done = sum(1 for r in records if str(r.get("status", "")).strip().upper() == "DONE")
            st.metric("Ads bereit", pending)
            st.caption(f"{done} bereits hochgeladen")
        except Exception as e:
            st.warning(f"ads-Tab: {e}")

    with col2:
        try:
            records = sheet.worksheet("ad_sets").get_all_records()
            pending = sum(1 for r in records if str(r.get("status", "")).strip().upper() not in ("DONE", "ERROR"))
            done = sum(1 for r in records if str(r.get("status", "")).strip().upper() == "DONE")
            st.metric("Ad Sets bereit", pending)
            st.caption(f"{done} bereits erstellt")
        except Exception as e:
            st.warning(f"ad_sets-Tab: {e}")

    with col3:
        try:
            records = sheet.worksheet("log").get_all_records()
            errors = sum(1 for r in records if "ERROR" in str(r.get("result", "")))
            total = len(records)
            st.metric("Fehler (Log)", errors)
            st.caption(f"{total} Einträge gesamt")
        except Exception:
            st.metric("Fehler (Log)", "—")

except Exception:
    pass
