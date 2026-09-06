import gspread
import config
from auth.credentials import get_service_account_creds

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_sheet_client() -> gspread.Spreadsheet:
    creds = get_service_account_creds(SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(config.GOOGLE_SHEET_ID)
