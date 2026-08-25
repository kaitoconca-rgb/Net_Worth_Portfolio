"""
sync_raiz_to_postgres.py
─────────────────────────────────────────────────────────────────
Reusable sync script — run this any time you have a new/updated Raiz
CSV in your Google Drive folder, to bring Postgres up to date.

Safe to re-run: it fingerprints existing transactions (by date + ISIN +
quantity + price + type) and only inserts rows that aren't already there.
Nothing is ever duplicated or overwritten.

Usage:
    py -3.14 -m pip install psycopg2-binary google-api-python-client google-auth pandas
    py -3.14 sync_raiz_to_postgres.py --dry-run     # preview what would be inserted
    py -3.14 sync_raiz_to_postgres.py               # actually insert new rows
"""

import argparse
import io
from datetime import datetime

import pandas as pd
import psycopg2
import psycopg2.extras
import numpy as np
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ── CONFIG — fill these in ──────────────────────────────────────────────────
PG_CONN_STRING = url = "postgresql://postgres.rqaoqweyggtyzycjwxen:ClaKaito2011?@aws-0-ap-northeast-1.pooler.supabase.com:6543/postgres"
GDRIVE_CREDS_PATH = "gdrive_creds.json"
RAIZ_FOLDER_ID = "13lzwthpCR1-F1-IORbBM1Yhq-BMOyjfy"  # confirmed correct folder
RAIZ_ACCOUNT_ID = "ec7a3f4e-adbb-4d9b-a24e-1b179d29e916"

SYNC_BATCH = f"raiz_sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def get_pg_conn():
    return psycopg2.connect(PG_CONN_STRING)


def download_raiz_csv():
    creds = service_account.Credentials.from_service_account_file(
        GDRIVE_CREDS_PATH, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    results = service.files().list(
        q=f"'{RAIZ_FOLDER_ID}' in parents and mimeType='text/csv' and trashed=false",
        orderBy="modifiedTime desc", pageSize=1, fields="files(id, name, modifiedTime)"
    ).execute()
    files = results.get("files", [])
    if not files:
        raise RuntimeError("No CSV found in the Raiz Drive folder.")
    latest = files[0]
    print(f"Using CSV: {latest['name']} (modified {latest['modifiedTime']})")
    request = service.files().get_media(fileId=latest["id"])
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    df = pd.read_csv(buf)
    df.columns = [c.strip() for c in df.columns]
    df['Trade Date'] = pd.to_datetime(df['Trade Date'], dayfirst=True)
    df['Quantity'] = pd.to_numeric(df['Quantity'], errors='coerce')
    df['Price'] = pd.to_numeric(df['Price'], errors='coerce')
    df['Amount'] = pd.to_numeric(df['Amount'], errors='coerce')
    # IVV stock split adjustment — same as original migration, applied once
    IVV_SPLIT_DATE = pd.Timestamp('2022-12-09')
    IVV_SPLIT_FACTOR = 15.317277
    ivv_pre = (df['Instrument Code'] == 'IVV') & (df['Trade Date'] < IVV_SPLIT_DATE)
    df.loc[ivv_pre, 'Quantity'] = df.loc[ivv_pre, 'Quantity'] * IVV_SPLIT_FACTOR
    df.loc[ivv_pre, 'Price'] = df.loc[ivv_pre, 'Price'] / IVV_SPLIT_FACTOR
    return df


def get_existing_fingerprints(cur):
    """Returns a set of (date, code, quantity, price, tx_type) tuples already in Postgres."""
    cur.execute(
        """
        SELECT t.tx_date, REPLACE(i.symbol, 'RAIZ:', ''), t.quantity, t.price, t.tx_type
        FROM transactions t
        JOIN instruments i ON i.id = t.instrument_id
        WHERE t.account_id = %s
        """,
        (RAIZ_ACCOUNT_ID,),
    )
    fingerprints = set()
    for tx_date, code, qty, price, tx_type in cur.fetchall():
        fingerprints.add((
            tx_date.isoformat() if tx_date else None,
            code,
            round(float(qty), 6) if qty is not None else None,
            round(float(price), 6) if price is not None else None,
            tx_type,
        ))
    return fingerprints


def upsert_instrument(cur, symbol, display_name):
    cur.execute(
        """
        insert into instruments (symbol, display_name, asset_class, native_currency)
        values (%s, %s, 'etf', 'AUD')
        on conflict (symbol) do update set display_name = excluded.display_name
        returning id
        """,
        (symbol, display_name),
    )
    return cur.fetchone()[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    df = download_raiz_csv()
    df = df.dropna(subset=['Instrument Code', 'Quantity'])

    conn = get_pg_conn()
    cur = conn.cursor()

    existing = get_existing_fingerprints(cur)
    print(f"Found {len(existing)} existing Raiz transactions in Postgres.")

    new_rows = []
    for _, row in df.iterrows():
        code = row['Instrument Code']
        ttype_raw = str(row.get('Transaction Type', 'BUY')).upper().strip()
        if ttype_raw in ('BUY', 'INVEST', 'DEPOSIT'):
            tx_type = 'buy'
        elif ttype_raw == 'SELL':
            tx_type = 'sell'
        else:
            continue  # withdrawals etc — same skip behaviour as original migration

        qty = row['Quantity']
        price = row['Price']
        amount = row['Amount']
        tx_date = row['Trade Date'].date()

        signed_qty = -abs(qty) if tx_type == 'sell' else abs(qty)
        signed_amt = abs(amount) if tx_type == 'sell' else -abs(amount)

        fingerprint = (
            tx_date.isoformat(),
            code,
            round(float(signed_qty), 6),
            round(float(price), 6) if pd.notnull(price) else None,
            tx_type,
        )
        if fingerprint in existing:
            continue  # already migrated

        new_rows.append({
            "code": code,
            "tx_date": tx_date,
            "tx_type": tx_type,
            "quantity": signed_qty,
            "price": price if pd.notnull(price) else None,
            "amount": signed_amt,
        })

    print(f"Found {len(new_rows)} new rows to insert.")
    for r in new_rows[:20]:
        print(f"  {r['tx_date']}  {r['code']:6s}  {r['tx_type']:5s}  qty={r['quantity']:.4f}  amount={r['amount']:.2f}")
    if len(new_rows) > 20:
        print(f"  ... and {len(new_rows) - 20} more")

    if args.dry_run:
        print("\nDRY RUN — nothing written.")
        cur.close()
        conn.close()
        return

    if not new_rows:
        print("\nNothing to insert — Postgres is already up to date.")
        cur.close()
        conn.close()
        return

    inst_id_cache = {}
    insert_rows = []
    for r in new_rows:
        code = r["code"]
        if code not in inst_id_cache:
            inst_id_cache[code] = upsert_instrument(cur, f"RAIZ:{code}", code)
        insert_rows.append((
            RAIZ_ACCOUNT_ID,
            inst_id_cache[code],
            r["tx_date"],
            r["tx_type"],
            None if pd.isna(r["quantity"]) else float(r["quantity"]),
            None if r["price"] is None or pd.isna(r["price"]) else float(r["price"]),
            None if pd.isna(r["amount"]) else float(r["amount"]),
            1.0,  # fx_rate_to_aud — Raiz is AUD-native
            None,
            f"[{SYNC_BATCH}] Raiz sync — new row",
            True,
        ))

    psycopg2.extras.execute_values(cur, """
        insert into transactions
            (account_id, instrument_id, tx_date, tx_type, quantity, price,
             amount, fx_rate_to_aud, transfer_group, notes, processed)
        values %s
    """, insert_rows)
    conn.commit()
    print(f"\n✅ Inserted {len(insert_rows)} new transactions. Batch tag: {SYNC_BATCH}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()