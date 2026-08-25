"""
One-time migration: Net_Worth and Dividends sheets -> Postgres.
Run locally once: py -3.13 migrate_networth_dividends.py
"""
import streamlit as st
import pandas as pd
from google.oauth2 import service_account
from googleapiclient.discovery import build
from sqlalchemy import create_engine, text

# --- Load secrets manually (this script runs outside Streamlit's app context) ---
import toml
secrets = toml.load(".streamlit/secrets.toml")

gs = secrets["gdrive"]
creds = service_account.Credentials.from_service_account_info({
    "type": "service_account",
    "project_id": gs["project_id"],
    "private_key_id": gs["private_key_id"],
    "private_key": gs["private_key"],
    "client_email": gs["client_email"],
    "client_id": gs.get("client_id", ""),
    "token_uri": "https://oauth2.googleapis.com/token",
}, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
service = build("sheets", "v4", credentials=creds, cache_discovery=False)

PORTFOLIO_SHEET_ID = "1ad1wkw7fUdKO-Kq5869JYPsldS_Xr3A0T0W9YLcQKe8"

# --- Postgres connection string from secrets.toml [connections.postgresql] ---
pg = secrets["connections"]["postgresql"]
db_url = pg["url"] if "url" in pg else (
    f"postgresql://{pg['username']}:{pg['password']}@{pg['host']}:{pg['port']}/{pg['database']}"
)
engine = create_engine(db_url)

def read_sheet(range_name, expected_header=None):
    result = service.spreadsheets().values().get(
        spreadsheetId=PORTFOLIO_SHEET_ID, range=range_name
    ).execute()
    rows = result.get("values", [])
    if len(rows) < 2:
        return pd.DataFrame()
    header = expected_header if expected_header else rows[0]
    data_rows = rows[1:] if not expected_header else rows[1:]
    ncols = len(header)
    fixed_rows = []
    for r in data_rows:
        r = list(r) + [""] * (ncols - len(r))  # pad short rows
        r = r[:ncols]                           # truncate long rows
        fixed_rows.append(r)
    return pd.DataFrame(fixed_rows, columns=header)

# ============== NET WORTH HISTORY ==============
print("Reading Net_Worth sheet...")
NET_WORTH_HEADER = ['Date','Total_AUD','Contributions_AUD','Market_Gains_AUD','FX_Impact_AUD',
                     'Starting_Balance_AUD','Contribution_Breakdown','N26_AUD','Raiz_AUD','Vanguard_AUD',
                     'Shares_AUD','Commodities_AUD','Super_AUD','Cash_AUD','EUR_Cash_AUD',
                     'EUR_Cash_Deposits_AUD','AUD_Cash_Interest_AUD','EUR_Cash_Interest_AUD',
                     'N26_Dividends_Received_AUD','Shares_Dividends_Received_AUD','N26_EUR_Value']
df_nw = read_sheet("Net_Worth!A:U", expected_header=NET_WORTH_HEADER)
df_nw.columns = [c.strip() for c in df_nw.columns]
print(f"Found {len(df_nw)} rows.")

def _num(row, col, default=0.0):
    try:
        v = row.get(col, "")
        return float(v) if v not in ("", None) else default
    except:
        return default

inserted = 0
with engine.begin() as conn:
    for _, row in df_nw.iterrows():
        try:
            date_val = pd.to_datetime(row["Date"]).date()
        except:
            continue
        conn.execute(text("""
            INSERT INTO net_worth_snapshots
                (snapshot_date, total_aud, contributions_aud, market_gains_aud, fx_impact_aud,
                 starting_balance_aud, contribution_breakdown, n26_aud, raiz_aud, vanguard_aud,
                 shares_aud, commodities_aud, super_aud, cash_aud, eur_cash_aud,
                 eur_cash_deposits_aud, aud_cash_interest_aud, eur_cash_interest_aud,
                 n26_dividends_aud, shares_dividends_aud, n26_eur_value)
            VALUES
                (:snapshot_date, :total_aud, :contributions_aud, :market_gains_aud, :fx_impact_aud,
                 :starting_balance_aud, :contribution_breakdown, :n26_aud, :raiz_aud, :vanguard_aud,
                 :shares_aud, :commodities_aud, :super_aud, :cash_aud, :eur_cash_aud,
                 :eur_cash_deposits_aud, :aud_cash_interest_aud, :eur_cash_interest_aud,
                 :n26_dividends_aud, :shares_dividends_aud, :n26_eur_value)
            ON CONFLICT (snapshot_date) DO NOTHING
        """), {
            "snapshot_date": date_val,
            "total_aud": _num(row, "Total_AUD"),
            "contributions_aud": _num(row, "Contributions_AUD"),
            "market_gains_aud": _num(row, "Market_Gains_AUD"),
            "fx_impact_aud": _num(row, "FX_Impact_AUD"),
            "starting_balance_aud": _num(row, "Starting_Balance_AUD"),
            "contribution_breakdown": str(row.get("Contribution_Breakdown", "")),
            "n26_aud": _num(row, "N26_AUD"),
            "raiz_aud": _num(row, "Raiz_AUD"),
            "vanguard_aud": _num(row, "Vanguard_AUD"),
            "shares_aud": _num(row, "Shares_AUD"),
            "commodities_aud": _num(row, "Commodities_AUD"),
            "super_aud": _num(row, "Super_AUD"),
            "cash_aud": _num(row, "Cash_AUD"),
            "eur_cash_aud": _num(row, "EUR_Cash_AUD"),
            "eur_cash_deposits_aud": _num(row, "EUR_Cash_Deposits_AUD"),
            "aud_cash_interest_aud": _num(row, "AUD_Cash_Interest_AUD"),
            "eur_cash_interest_aud": _num(row, "EUR_Cash_Interest_AUD"),
            "n26_dividends_aud": _num(row, "N26_Dividends_Received_AUD"),
            "shares_dividends_aud": _num(row, "Shares_Dividends_Received_AUD"),
            "n26_eur_value": _num(row, "N26_EUR_Value"),
        })
        inserted += 1
print(f"Inserted {inserted} net worth snapshot rows.")

# ============== DIVIDENDS ==============
print("\nReading Dividends sheet...")
DIVIDENDS_HEADER = ['Date', 'Portfolio', 'Amount', 'Currency', 'Processed']
df_div = read_sheet("Dividends!A:E", expected_header=DIVIDENDS_HEADER)
df_div.columns = [c.strip() for c in df_div.columns] if not df_div.empty else []
print(f"Found {len(df_div)} rows.")

div_inserted = 0
with engine.begin() as conn:
    for _, row in df_div.iterrows():
        try:
            date_val = pd.to_datetime(row.get("Date", ""), dayfirst=True).date()
        except:
            continue
        try:
            amt = float(row.get("Amount", 0))
        except:
            amt = 0.0
        processed = str(row.get("Processed", "")).strip().upper() == "YES"
        conn.execute(text("""
            INSERT INTO dividends (div_date, portfolio, amount, currency, processed)
            VALUES (:div_date, :portfolio, :amount, :currency, :processed)
        """), {
            "div_date": date_val,
            "portfolio": str(row.get("Portfolio", "")),
            "amount": amt,
            "currency": str(row.get("Currency", "AUD")),
            "processed": processed,
        })
        div_inserted += 1
print(f"Inserted {div_inserted} dividend rows.")

print("\nDone. Verify in Supabase before removing Sheets code from app.py.")
# ============== FORECAST SETTINGS ==============
print("\nReading Forecast sheet...")
FORECAST_HEADER = ['Category', 'Key', 'Value']
df_fc = read_sheet("Forecast!A:C", expected_header=FORECAST_HEADER)
print(f"Found {len(df_fc)} rows.")

fc_inserted = 0
with engine.begin() as conn:
    for _, row in df_fc.iterrows():
        cat = str(row.get("Category", "")).strip()
        key = str(row.get("Key", "")).strip()
        if not cat or not key:
            continue
        raw_val = str(row.get("Value", "0")).replace('%', '').strip()
        try:
            val = float(raw_val)
        except:
            val = 0.0
        conn.execute(text("""
            INSERT INTO forecast_settings (category, key, value)
            VALUES (:category, :key, :value)
            ON CONFLICT (category, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """), {"category": cat, "key": key, "value": val})
        fc_inserted += 1
print(f"Inserted/updated {fc_inserted} forecast setting rows.")