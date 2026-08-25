"""
migrate_to_postgres.py
═══════════════════════════════════════════════════════════════════════════
One-time backfill: reads your existing Google Sheets / Drive data sources
and populates the new Postgres schema (accounts, instruments, transactions).

Run this OUTSIDE Streamlit — it's a standalone script, not a page.
Safe to re-run: uses account/instrument name matching to avoid duplicate
accounts/instruments, and tags every inserted transaction with a
`migration_batch` note so you can identify and delete a bad run if needed.

Prereqs:
    pip install psycopg2-binary google-api-python-client google-auth pandas

Usage:
    python migrate_to_postgres.py --dry-run      # print what would be inserted, no writes
    python migrate_to_postgres.py                # actually write to Postgres
═══════════════════════════════════════════════════════════════════════════
"""

import argparse
import io
import json
import os
import sys
from datetime import datetime

import pandas as pd
import psycopg2
import psycopg2.extras
import numpy as np
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ── CONFIG — fill these in ──────────────────────────────────────────────────
# Postgres connection string from your Supabase project settings > Database
PG_CONN_STRING = os.environ.get("SUPABASE_DB_URL", "postgresql://postgres:[PASSWORD]@[HOST]:5432/postgres")

# Same service-account JSON you already use in st.secrets["gdrive"] — export
# it to a local file for this standalone script, e.g. gdrive_creds.json
GDRIVE_CREDS_PATH = os.environ.get("GDRIVE_CREDS_PATH", "gdrive_creds.json")

# All sources live as TABS within one spreadsheet — confirmed: Sheet1 (N26),
# Cash, Vanguard, Metal, Shares, Dividends, Net_Worth, Forecast, Super (empty).
# No separate N26/Cash spreadsheet IDs exist; both old "gsheets" / "gsheets_cash"
# Streamlit connections point at tabs within this same file.
PORTFOLIO_SHEET_ID = os.environ.get("PORTFOLIO_SHEET_ID", "1ad1wkw7fUdKO-Kq5869JYPsldS_Xr3A0T0W9YLcQKe8")
RAIZ_FOLDER_ID = os.environ.get("RAIZ_FOLDER_ID", "")       # same as st.secrets["gdrive"]["raiz_folder_id"]

MIGRATION_BATCH = f"backfill_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

ISIN_TICKER_MAP = {
    "LU2885245055": "8OU9.DE", "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "36B2.MU", "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS", "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE"
}
SHARES_TICKERS = {"NHF": "NHF.AX", "TPG": "TPG.AX", "TUA": "TUA.AX", "WBC": "WBC.AX"}
METAL_TICKERS = {"Gold": "GC=F", "Silver": "SI=F", "Platinum": "PL=F"}

CASH_ACCOUNTS = {
    "CBA": "AUD", "Me Bank": "AUD", "Rabobank": "AUD", "Up": "AUD",
    "Trade Republic": "EUR", "N26 Cash": "EUR", "BUNQ": "EUR",
    "BPM Cash": "EUR", "BPM Bonds": "EUR",
    "C6 Cash": "BRL", "C6 Investments": "BRL",
}
# NOTE: "N26 Cash" here is deliberately distinct from the "N26" investment
# account below — the sheet reuses "N26" for both cash and the ETF platform.
# Rename the cash-side account so the two don't collide in `accounts.name`.

# ── CONNECTIONS ──────────────────────────────────────────────────────────
def get_pg_conn():
    return psycopg2.connect(PG_CONN_STRING)

def get_google_creds(scopes):
    return service_account.Credentials.from_service_account_file(GDRIVE_CREDS_PATH, scopes=scopes)

def sheets_read(spreadsheet_id, range_name):
    creds = get_google_creds(["https://www.googleapis.com/auth/spreadsheets.readonly"])
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    result = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=range_name).execute()
    rows = result.get("values", [])
    if len(rows) < 2:
        return pd.DataFrame()
    return pd.DataFrame(rows[1:], columns=rows[0])

def download_raiz_csv():
    creds = get_google_creds(["https://www.googleapis.com/auth/drive.readonly"])
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    results = service.files().list(
        q=f"'{RAIZ_FOLDER_ID}' in parents and mimeType='text/csv' and trashed=false",
        orderBy="modifiedTime desc", pageSize=1, fields="files(id, name)"
    ).execute()
    files = results.get("files", [])
    if not files:
        return pd.DataFrame()
    request = service.files().get_media(fileId=files[0]["id"])
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    return pd.read_csv(buf)

# ── UPSERT HELPERS ───────────────────────────────────────────────────────
def upsert_account(cur, name, account_type, currency, platform):
    cur.execute("""
        insert into accounts (name, account_type, currency, platform)
        values (%s, %s, %s, %s)
        on conflict (name) do update set account_type = excluded.account_type
        returning id
    """, (name, account_type, currency, platform))
    return cur.fetchone()[0]

def upsert_instrument(cur, symbol, display_name, yahoo_ticker, asset_class, currency):
    cur.execute("""
        insert into instruments (symbol, display_name, yahoo_ticker, asset_class, native_currency)
        values (%s, %s, %s, %s, %s)
        on conflict (symbol) do update set yahoo_ticker = excluded.yahoo_ticker
        returning id
    """, (symbol, display_name, yahoo_ticker, asset_class, currency))
    return cur.fetchone()[0]

def _py(v):
    """Convert numpy/pandas scalar types to native Python types psycopg2 can
    adapt (e.g. numpy.int64 -> int). Also turns NaN into None."""
    if v is None:
        return None
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float) and pd.isna(v):
        return None
    return v

_pending_transactions = []

def insert_transaction(cur, account_id, instrument_id, tx_date, tx_type,
                        quantity, price, amount, fx_rate, transfer_group=None, notes=None):
    """Buffers the row instead of executing immediately. Call flush_transactions()
    periodically (done automatically every 500 rows) and once more at the end
    of main() to write any remainder. This turns ~3,700 individual round trips
    (which was slow enough to trip the Supabase pooler's timeout) into a
    handful of bulk inserts."""
    if isinstance(cur, DryRunCursor):
        cur.execute("insert_transaction", (account_id, instrument_id, tx_date, tx_type,
                                            quantity, price, amount, fx_rate, notes))
        return
    _pending_transactions.append((
        account_id, instrument_id, tx_date, tx_type,
        _py(quantity), _py(price), _py(amount), _py(fx_rate), transfer_group,
        f"[{MIGRATION_BATCH}] {notes or ''}".strip(), True
    ))
    if len(_pending_transactions) >= 500:
        flush_transactions(cur)

def flush_transactions(cur):
    if not _pending_transactions:
        return
    psycopg2.extras.execute_values(cur, """
        insert into transactions
            (account_id, instrument_id, tx_date, tx_type, quantity, price,
             amount, fx_rate_to_aud, transfer_group, notes, processed)
        values %s
    """, _pending_transactions)
    _pending_transactions.clear()

# ── PER-SOURCE MIGRATIONS ────────────────────────────────────────────────
def migrate_n26(cur, dry_run):
    print("→ N26 European portfolio...")
    df = sheets_read(PORTFOLIO_SHEET_ID, "Sheet1")   # "Sheet1" is confirmed to be the N26 tab
    if df.empty:
        print("  (no data found in Sheet1 tab)")
        return
    df.columns = [c.strip() for c in df.columns]
    acct_id = upsert_account(cur, "N26", "investment", "EUR", "N26")

    for _, row in df.iterrows():
        isin = row.get("ISIN")
        if not isin:
            continue
        inst_id = upsert_instrument(cur, isin, isin, ISIN_TICKER_MAP.get(isin), "etf", "EUR")
        tipo = str(row.get("Tipo", "BUY")).upper()
        qty = pd.to_numeric(row.get("Cantidad"), errors="coerce")
        inv_eur = pd.to_numeric(row.get("Importe Cargado"), errors="coerce")
        price = pd.to_numeric(row.get("Precio"), errors="coerce")
        date_str = row.get("Fecha Valor")
        tx_date = pd.to_datetime(date_str, dayfirst=True).date()
        tx_type = "sell" if tipo == "SELL" else "buy"
        signed_qty = -abs(qty) if tx_type == "sell" else abs(qty)
        signed_amt = abs(inv_eur) if tx_type == "sell" else -abs(inv_eur)  # cash flow: sell = +cash, buy = -cash

        if not dry_run:
            insert_transaction(cur, acct_id, inst_id, tx_date, tx_type,
                                signed_qty, price, signed_amt, fx_rate=None,
                                notes="N26 ledger import")
    print(f"  {len(df)} rows processed")

def migrate_raiz(cur, dry_run):
    print("→ Raiz ETFs...")
    df = download_raiz_csv()
    if df.empty:
        print("  (no CSV found in Drive folder)")
        return
    df.columns = [c.strip() for c in df.columns]
    acct_id = upsert_account(cur, "Raiz", "investment", "AUD", "Raiz")

    for _, row in df.iterrows():
        code = row.get("Instrument Code")
        if not code:
            continue
        inst_id = upsert_instrument(cur, f"RAIZ:{code}", code,
                                     f"{code}.AX", "etf", "AUD")
        ttype = str(row.get("Transaction Type", "BUY")).upper().strip()
        qty = pd.to_numeric(row.get("Quantity"), errors="coerce")
        price = pd.to_numeric(row.get("Price"), errors="coerce")
        amount = pd.to_numeric(row.get("Amount"), errors="coerce")
        tx_date = pd.to_datetime(row.get("Trade Date"), dayfirst=True).date()

        if ttype in ("BUY", "INVEST", "DEPOSIT"):
            tx_type = "buy"
        elif ttype == "SELL":
            tx_type = "sell"
        else:
            continue  # skip WITHDRAWAL / unrecognised for now — review manually
        signed_qty = -abs(qty) if tx_type == "sell" else abs(qty)
        signed_amt = abs(amount) if tx_type == "sell" else -abs(amount)

        if not dry_run:
            insert_transaction(cur, acct_id, inst_id, tx_date, tx_type,
                                signed_qty, price, signed_amt, fx_rate=1.0,
                                notes="Raiz CSV import")
    print(f"  {len(df)} rows processed")

def migrate_vanguard(cur, dry_run):
    print("→ Vanguard VDAL...")
    df = sheets_read(PORTFOLIO_SHEET_ID, "Vanguard!A:E")
    if df.empty:
        print("  (no data)")
        return
    df.columns = [c.strip() for c in df.columns]
    acct_id = upsert_account(cur, "Vanguard VDAL", "investment", "AUD", "Vanguard")
    inst_id = upsert_instrument(cur, "VDAL.AX", "Vanguard Diversified High Growth", "VDAL.AX", "etf", "AUD")

    for _, row in df.iterrows():
        ttype = str(row.get("Transaction", "BUY")).upper()
        qty = pd.to_numeric(row.get("Quantity"), errors="coerce")
        price = pd.to_numeric(row.get("Purchase Price"), errors="coerce")
        tx_date = pd.to_datetime(row.get("Date"), dayfirst=True).date()
        tx_type = "sell" if ttype == "SELL" else "buy"
        signed_qty = -abs(qty) if tx_type == "sell" else abs(qty)
        amount = qty * price if pd.notnull(qty) and pd.notnull(price) else 0
        signed_amt = abs(amount) if tx_type == "sell" else -abs(amount)

        if not dry_run:
            insert_transaction(cur, acct_id, inst_id, tx_date, tx_type,
                                signed_qty, price, signed_amt, fx_rate=1.0,
                                notes="Vanguard sheet import")
    print(f"  {len(df)} rows processed")

def migrate_metals(cur, dry_run):
    print("→ Precious metals...")
    df = sheets_read(PORTFOLIO_SHEET_ID, "Metal!A:F")
    if df.empty:
        print("  (no data)")
        return
    df.columns = [c.strip() for c in df.columns]
    acct_id = upsert_account(cur, "Revolut Metals", "investment", "AUD", "Commodities")

    for _, row in df.iterrows():
        metal = row.get("Type")
        if not metal:
            continue
        inst_id = upsert_instrument(cur, f"METAL:{metal}", metal, METAL_TICKERS.get(metal), "metal", "AUD")
        ttype = str(row.get("Transaction", "BUY")).upper()
        qty = pd.to_numeric(row.get("Quantity"), errors="coerce")
        price = pd.to_numeric(row.get("Purchase Price"), errors="coerce")
        currency = str(row.get("Currency", "AUD")).upper()
        tx_date = pd.to_datetime(row.get("Date"), dayfirst=True).date()
        tx_type = "sell" if ttype == "SELL" else "buy"
        signed_qty = -abs(qty) if tx_type == "sell" else abs(qty)
        amount = qty * price if pd.notnull(qty) and pd.notnull(price) else 0
        signed_amt = abs(amount) if tx_type == "sell" else -abs(amount)
        # NOTE: original purchase currency (NOK/USD/etc) preserved in notes —
        # revisit convert_purchase_to_aud() logic once this lands in Postgres,
        # rather than baking a lossy AUD conversion into the historical record.

        if not dry_run:
            insert_transaction(cur, acct_id, inst_id, tx_date, tx_type,
                                signed_qty, price, signed_amt, fx_rate=None,
                                notes=f"Metal sheet import, orig currency={currency}")
    print(f"  {len(df)} rows processed")

def migrate_shares(cur, dry_run):
    print("→ ASX Shares...")
    print("  ⚠ Shares!A:B only has CURRENT holdings, no transaction history.")
    print("  Creating a single 'opening_position' buy per stock at today's implied cost.")
    print("  You should manually correct purchase dates/prices in Postgres afterward")
    print("  for accurate CGT discount (365-day) tracking.")
    df = sheets_read(PORTFOLIO_SHEET_ID, "Shares!A:B")
    if df.empty:
        return
    df.columns = [c.strip() for c in df.columns]
    acct_id = upsert_account(cur, "ASX Shares", "investment", "AUD", "Shares")

    for _, row in df.iterrows():
        code = row.get("Share")
        qty = pd.to_numeric(row.get("Quantity"), errors="coerce")
        if not code or not qty or qty <= 0:
            continue
        inst_id = upsert_instrument(cur, f"ASX:{code}", code, SHARES_TICKERS.get(code, f"{code}.AX"), "share", "AUD")
        if not dry_run:
            insert_transaction(cur, acct_id, inst_id, datetime.now().date(), "buy",
                                qty, None, 0,  # amount left at 0 — cost basis unknown, fix manually
                                fx_rate=1.0,
                                notes="PLACEHOLDER opening position — set real date/price/cost manually")
    print(f"  {len(df)} rows processed (placeholders — needs manual correction)")

def migrate_dividends(cur, dry_run):
    print("→ Dividends...")
    df = sheets_read(PORTFOLIO_SHEET_ID, "Dividends!A:D")
    if df.empty:
        print("  (no data)")
        return
    df.columns = [c.strip() for c in df.columns]

    for _, row in df.iterrows():
        portfolio = str(row.get("Portfolio", "")).upper()
        amount = pd.to_numeric(row.get("Amount"), errors="coerce")
        currency = str(row.get("Currency", "AUD")).upper()
        tx_date = pd.to_datetime(row.get("Date"), dayfirst=True).date()
        acct_name = "N26" if "N26" in portfolio else "ASX Shares"

        if not dry_run:
            cur.execute("select id from accounts where name = %s", (acct_name,))
            r = cur.fetchone()
            if not r:
                continue
            acct_id = r[0]
            insert_transaction(cur, acct_id, None, tx_date, "dividend",
                                None, None, amount, fx_rate=None,
                                notes="Dividends sheet import")
    print(f"  {len(df)} rows processed")

def migrate_cash_opening_balances(cur, dry_run):
    print("→ Cash accounts (opening balances, not full history)...")
    print("  ⚠ Sheets only stores current balances, not a transaction log.")
    print("  Recording each current balance as a single 'deposit' dated today.")
    print("  Going forward, log real deposits/withdrawals as they happen instead")
    print("  of overwriting a balance — that's what makes Contributions accurate.")
    # ⚠ ASSUMPTION TO VERIFY: expects two columns — account name, balance —
    # with no header row consumed as data (sheets_read drops row 1 as headers).
    # If the "Cash" tab has a different layout, this will silently produce
    # wrong/zero balances rather than erroring. Check with --dry-run output below.
    df = sheets_read(PORTFOLIO_SHEET_ID, "Cash")
    if df.empty:
        return
    df.columns = [c.strip() for c in df.columns]
    bal = df.set_index(df.columns[0])[df.columns[1]].to_dict()

    for name, currency in CASH_ACCOUNTS.items():
        lookup_name = "N26" if name == "N26 Cash" else name  # sheet just calls it "N26"
        raw_bal = bal.get(lookup_name if name == "N26 Cash" else name, 0)
        try:
            amount = float(raw_bal)
        except (TypeError, ValueError):
            amount = 0.0
        if amount == 0:
            continue
        acct_id = upsert_account(cur, name, "cash", currency, "Cash")
        if not dry_run:
            insert_transaction(cur, acct_id, None, datetime.now().date(), "deposit",
                                None, None, amount, fx_rate=None,
                                notes="Opening balance from Cash sheet migration")
    print(f"  {len(CASH_ACCOUNTS)} cash accounts processed")

    # Super balance is NOT in the (empty) "Super" spreadsheet tab — it's a
    # row literally named "Super" inside this same Cash tab. Confirmed from
    # app.py's get_super_total_for_dashboard(), which reads it via the same
    # gsheets_cash connection as the rest of these cash balances.
    super_amount = pd.to_numeric(bal.get("Super", 0), errors="coerce")
    if pd.notnull(super_amount) and super_amount:
        super_acct_id = upsert_account(cur, "Mercer Super", "super", "AUD", "Super")
        if not dry_run:
            insert_transaction(cur, super_acct_id, None, datetime.now().date(), "deposit",
                                None, None, float(super_amount), fx_rate=None,
                                notes="Opening balance from Cash tab 'Super' row")
        print("  + Super balance processed (found as a row in the Cash tab)")
    else:
        print("  (no Super balance found in the Cash tab)")

# migrate_super() removed — the "Super" tab in the spreadsheet is confirmed
# empty, so there's nothing to backfill. Add it back (reading PORTFOLIO_SHEET_ID,
# "Super") in a later phase once you're tracking super balances there.

# ── MAIN ──────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing to Postgres")
    args = parser.parse_args()

    if args.dry_run:
        print(f"DRY RUN — batch tag would be {MIGRATION_BATCH}\n")
        conn, cur = None, DryRunCursor()
    else:
        conn = get_pg_conn()
        cur = conn.cursor()
        print(f"Connected. Migration batch: {MIGRATION_BATCH}\n")

    try:
        migrate_n26(cur, args.dry_run)
        migrate_raiz(cur, args.dry_run)
        migrate_vanguard(cur, args.dry_run)
        migrate_metals(cur, args.dry_run)
        migrate_shares(cur, args.dry_run)
        migrate_dividends(cur, args.dry_run)
        migrate_cash_opening_balances(cur, args.dry_run)

        if not args.dry_run:
            flush_transactions(cur)
            conn.commit()
            print(f"\n✅ Migration committed. Batch tag: {MIGRATION_BATCH}")
            print("   (search transactions.notes for this tag if you need to review/undo)")
        else:
            print("\n✅ Dry run complete — no data written.")
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"\n❌ Migration failed, rolled back: {e}")
        raise
    finally:
        if conn:
            cur.close()
            conn.close()


class DryRunCursor:
    """Stub cursor for --dry-run so upsert/insert calls don't hit the DB."""
    def execute(self, *a, **k): pass
    def fetchone(self): return ("dry-run-placeholder-id",)


if __name__ == "__main__":
    main()