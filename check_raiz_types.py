"""
check_raiz_types.py
═══════════════════════════════════════════════════════════════════════
Diagnostic only — makes no database changes.

Downloads the same Raiz CSV the migration script uses, and prints the
distinct values in the "Transaction Type" column along with row counts.
This tells us the REAL labels Raiz uses for buys/deposits, so we can fix
migrate_to_postgres.py's classification logic instead of guessing again.

Run this from the same folder as migrate_to_postgres.py, with the same
env vars already set (GDRIVE_CREDS_PATH, RAIZ_FOLDER_ID):

    python check_raiz_types.py
═══════════════════════════════════════════════════════════════════════
"""

import os
import io
import pandas as pd
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

GDRIVE_CREDS_PATH = os.environ.get("GDRIVE_CREDS_PATH", "")
RAIZ_FOLDER_ID = os.environ.get("RAIZ_FOLDER_ID", "")

def download_raiz_csv():
    creds = service_account.Credentials.from_service_account_file(
        GDRIVE_CREDS_PATH,
        scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    results = service.files().list(
        q=f"'{RAIZ_FOLDER_ID}' in parents and mimeType='text/csv' and trashed=false",
        orderBy="modifiedTime desc", pageSize=1,
        fields="files(id, name, modifiedTime)"
    ).execute()
    files = results.get("files", [])
    if not files:
        print("No CSV found in Raiz Drive folder.")
        return pd.DataFrame()
    latest = files[0]
    print(f"Using: {latest['name']} (modified {latest['modifiedTime']})")
    request = service.files().get_media(fileId=latest["id"])
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    return pd.read_csv(buf)

df = download_raiz_csv()
if df.empty:
    print("Empty dataframe — check GDRIVE_CREDS_PATH / RAIZ_FOLDER_ID.")
else:
    df.columns = [c.strip() for c in df.columns]
    print(f"\nTotal rows: {len(df)}")
    print(f"\nColumns found: {list(df.columns)}")
    print("\nDistinct 'Transaction Type' values and counts:")
    print(df["Transaction Type"].value_counts(dropna=False))