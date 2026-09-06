#!/usr/bin/env python3
import streamlit as st

st.set_page_config(
    page_title="Mammaly Meta Ads",
    page_icon="🐾",
    layout="wide",
    initial_sidebar_state="expanded",
)

from components.auth import status_indicators

st.title("🐾 Mammaly Meta Ads Uploader")
st.markdown("Wähle eine Seite aus dem Menü links.")

st.divider()
st.subheader("Verbindungsstatus")
status_indicators()

st.divider()
st.markdown("""
**Workflow:**

1. **📁 Ad Naming** — Drive-Ordner analysieren, Dateien umbenennen, Naming Sheet befüllen
2. **🚀 Upload** — Ads aus dem Sheet zu Meta hochladen
3. **📊 History** — Upload-Log einsehen
""")
