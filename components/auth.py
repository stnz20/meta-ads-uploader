from __future__ import annotations
import os
import streamlit as st

# ── Credential loading ────────────────────────────────────────────────────────
# Local: .env file (loaded by dotenv in app.py)
# Streamlit Cloud: st.secrets → pushed into os.environ so the rest of the
#                  codebase can read them via os.environ / config.py as usual
try:
    for _k, _v in st.secrets.items():
        os.environ.setdefault(_k, str(_v))
except Exception:
    # st.secrets not available (e.g. local run without secrets.toml) — fine
    pass

# Local fallback — expliziter Pfad damit es auch funktioniert wenn cwd != Projektordner
try:
    from dotenv import load_dotenv
    import pathlib
    _env_file = pathlib.Path(__file__).parent.parent / ".env"
    load_dotenv(dotenv_path=_env_file)
except ImportError:
    pass

_REQUIRED_VARS = [
    "META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN",
    "META_AD_ACCOUNT_ID", "META_PAGE_ID", "GOOGLE_SHEET_ID", "ADS_DRIVE_FOLDER_ID",
]


def missing_env_vars() -> list[str]:
    return [k for k in _REQUIRED_VARS if not os.environ.get(k, "").strip()]


@st.cache_resource
def _get_meta_account():
    from auth.meta_auth import init_meta_api
    return init_meta_api()


@st.cache_resource
def _get_sheet():
    from auth.sheets_auth import get_sheet_client
    return get_sheet_client()


def require_connections():
    """Returns (account, sheet). Shows error and stops if credentials are missing."""
    missing = missing_env_vars()
    if missing:
        st.error(f"⚙️ Fehlende Variablen in `.env` / Secrets: `{', '.join(missing)}`")
        st.stop()
    try:
        account = _get_meta_account()
        sheet = _get_sheet()
        return account, sheet
    except SystemExit as e:
        st.error(f"Konfigurationsfehler: {e}")
        st.stop()
    except Exception as e:
        st.error(f"Verbindungsfehler: {e}")
        st.stop()


def status_indicators() -> None:
    """Renders connection status badges."""
    missing = missing_env_vars()
    if missing:
        st.error(f"⚙️ Fehlend in `.env` / Secrets: {', '.join(missing)}")
        return
    col1, col2 = st.columns(2)
    with col1:
        try:
            _get_meta_account()
            st.success("Meta API  ✅")
        except Exception as e:
            st.error(f"Meta API  ❌  {e}")
    with col2:
        try:
            _get_sheet()
            st.success("Google Sheets  ✅")
        except Exception as e:
            st.error(f"Google Sheets  ❌  {e}")
