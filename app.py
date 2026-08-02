import streamlit as st
import pandas as pd
import yfinance as yf
from sqlalchemy import text as sql_text
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, date
import time
import numpy as np  


# NOTE: EUR/AUD rate is fetched via get_fx_data() below (cached, using fast_info).
# fx_now is the single authoritative rate used throughout the app.

# --- 0. PROTEZIONE ---
def check_password():
    def password_guessed():
        if st.session_state.get("password", "") == st.secrets["auth"]["password"]:
            st.session_state["password_correct"] = True
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False
    if "password_correct" not in st.session_state:
        st.text_input("Inserisci Password", type="password", on_change=password_guessed, key="password")
        return False
    return st.session_state.get("password_correct", False)

# --- 1. CONFIGURAZIONE --- (must be first Streamlit call)
st.set_page_config(page_title="Claudio's Executive Console", layout="wide")

if not check_password():
    st.stop()

ticker_map = {
    "LU2885245055": "8OU9.DE", "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "36B2.MU", "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS", "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE"
}
CASH_ACCOUNTS = {
    "CBA":                 ("160aa9c5-b55d-466b-b85b-90f37a2e04e1", "AUD"),
    "Me Bank":             ("d3dc4451-3b8f-401c-b05f-db6b2666f3d5", "AUD"),
    "Rabobank":            ("01d890ac-0c5f-482c-8419-be8ec241701d", "AUD"),
    "Up":                  ("414ff8f6-6c43-4d8b-921f-fdcf1f57c755", "AUD"),
    "Trade Republic":      ("92592fb8-d5d3-4318-9ca8-7bc84c338251", "EUR"),
    "N26":                 ("48ca6a9e-373a-4af1-957f-167090b13f45", "EUR"),
    "BPM Cash":            ("d5920f95-da3d-4246-af04-7dcb0bc3f46e", "EUR"),
    "BPM Bonds":           ("f6be25f1-53a0-453c-a22e-3c49995379ce", "EUR"),
    "C6 Cash":             ("2c2cfdd1-67b4-446a-91bb-d2827b630b79", "BRL"),
    "C6 Investments":      ("cf6fa923-9c00-4599-bac9-02b3afa6d69d", "BRL"),
}
SUPER_ACCOUNT_ID = "79b626ee-4563-48c9-975d-ecefc6221fe7"

def get_pg():
    return st.connection("postgresql", type="sql", pool_pre_ping=True)

def get_or_create_instrument(symbol, display_name, asset_class, native_currency, yahoo_ticker=None):
    conn = get_pg()
    with conn.session as s:
        existing = s.execute(
            sql_text("SELECT id FROM instruments WHERE symbol = :symbol"),
            {"symbol": symbol}
        ).fetchone()
        if existing:
            return existing[0]
        result = s.execute(
            sql_text("""
                INSERT INTO instruments (symbol, display_name, asset_class, native_currency, yahoo_ticker)
                VALUES (:symbol, :display_name, :asset_class, :native_currency, :yahoo_ticker)
                RETURNING id
            """),
            {"symbol": symbol, "display_name": display_name, "asset_class": asset_class,
             "native_currency": native_currency, "yahoo_ticker": yahoo_ticker}
        )
        new_id = result.fetchone()[0]
        s.commit()
        return new_id

@st.cache_data(ttl=0)
def load_transactions_for_editor(account_id, symbol_prefix=""):
    conn = get_pg()
    df = conn.query(
        """
        SELECT t.id::text AS id, t.tx_date AS "Date",
               REPLACE(i.symbol, :prefix, '') AS "Symbol",
               t.tx_type AS "Type", t.quantity AS "Quantity",
               t.price AS "Price", t.amount AS "Amount",
               COALESCE(t.notes, '') AS "Notes"
        FROM transactions t
        JOIN instruments i ON i.id = t.instrument_id
        WHERE t.account_id = :acc_id
        ORDER BY t.tx_date
        """,
        params={"acc_id": account_id, "prefix": symbol_prefix},
        ttl=0,
    )
    return df

def sync_transaction_edits(account_id, symbol_prefix, native_currency, asset_class, original_df, edited_df):
    try:
        conn = get_pg()
        original_ids = set(original_df['id'].dropna().astype(str)) if not original_df.empty else set()
        edited_ids = set(edited_df['id'].dropna().astype(str)) if 'id' in edited_df.columns else set()
        deleted_ids = original_ids - edited_ids

        with conn.session as s:
            for row_id in deleted_ids:
                s.execute(sql_text("DELETE FROM transactions WHERE id = :id"), {"id": row_id})
            s.commit()

        with conn.session as s:
            for _, row in edited_df.iterrows():
                symbol_raw = str(row.get('Symbol', '')).strip().upper()
                if not symbol_raw:
                    continue
                full_symbol = f"{symbol_prefix}{symbol_raw}"
                qty = float(row['Quantity']) if pd.notnull(row.get('Quantity')) else 0.0
                tx_type = str(row.get('Type', 'BUY')).strip().upper()
                signed_qty = -abs(qty) if tx_type == 'SELL' else abs(qty)
                price = float(row['Price']) if pd.notnull(row.get('Price')) else None
                if pd.notnull(row.get('Amount')):
                    amount = float(row['Amount'])
                elif price is not None:
                    amount = abs(signed_qty * price)
                else:
                    amount = 0.0
                notes = str(row['Notes']) if pd.notnull(row.get('Notes')) else ""
                tx_date = row['Date']

                instrument_id = get_or_create_instrument(
                    full_symbol, display_name=full_symbol,
                    asset_class=asset_class, native_currency=native_currency
                )

                row_id = row.get('id')
                if pd.isna(row_id) or str(row_id).strip() == "":
                    s.execute(
                        sql_text("""
                            INSERT INTO transactions
                                (account_id, instrument_id, tx_date, tx_type, quantity, price, amount, notes, processed)
                            VALUES
                                (:account_id, :instrument_id, :tx_date, :tx_type, :quantity, :price, :amount, :notes, true)
                        """),
                        {"account_id": account_id, "instrument_id": instrument_id, "tx_date": tx_date,
                         "tx_type": tx_type, "quantity": signed_qty, "price": price, "amount": amount, "notes": notes}
                    )
                else:
                    s.execute(
                        sql_text("""
                            UPDATE transactions
                            SET instrument_id = :instrument_id, tx_date = :tx_date, tx_type = :tx_type,
                                quantity = :quantity, price = :price, amount = :amount, notes = :notes
                            WHERE id = :id
                        """),
                        {"instrument_id": instrument_id, "tx_date": tx_date, "tx_type": tx_type,
                         "quantity": signed_qty, "price": price, "amount": amount, "notes": notes,
                         "id": str(row_id)}
                    )
            s.commit()
        return True, None
    except Exception as e:
        import traceback
        return False, traceback.format_exc()

@st.cache_data(ttl=0)
def load_cash_balances():
    try:
        conn = get_pg()
        ids = [acc_id for acc_id, _ in CASH_ACCOUNTS.values()] + [SUPER_ACCOUNT_ID]
        placeholders = ", ".join(f"'{i}'" for i in ids)
        df = conn.query(
            f"""
            SELECT account_id::text AS account_id, COALESCE(SUM(amount), 0) AS balance
            FROM transactions
            WHERE account_id IN ({placeholders})
            GROUP BY account_id
            """,
            ttl=0,
        )
        bal_by_id = dict(zip(df["account_id"], df["balance"]))
        result = {name: float(bal_by_id.get(acc_id, 0.0))
                  for name, (acc_id, _cur) in CASH_ACCOUNTS.items()}
        result["Super"] = float(bal_by_id.get(SUPER_ACCOUNT_ID, 0.0))
        return result
    except Exception as e:
        st.warning(f"Could not load cash balances from Postgres: {e}")
        return {name: 0.0 for name in CASH_ACCOUNTS}

def save_cash_balances(balances_dict):
    try:
        conn = get_pg()
        current = load_cash_balances()
        today_str = date.today().isoformat()
        rows_to_insert = []
        for name, new_balance in balances_dict.items():
            if name == "Super":
                acc_id, currency = SUPER_ACCOUNT_ID, "AUD"
            elif name in CASH_ACCOUNTS:
                acc_id, currency = CASH_ACCOUNTS[name]
            else:
                continue
            old_balance = current.get(name, 0.0)
            delta = round(float(new_balance) - float(old_balance), 2)
            if abs(delta) < 0.005:
                continue
            tx_type = "deposit" if delta > 0 else "withdrawal"
            if currency == "AUD":
                fx_rate = 1.0
            elif currency == "EUR":
                fx_rate = fx_now
            else:
                try:
                    fx_rate = float(yf.Ticker("BRLAUD=X").fast_info['last_price'])
                except Exception:
                    fx_rate = 0.27
            rows_to_insert.append({
                "account_id": acc_id,
                "tx_date": today_str,
                "tx_type": tx_type,
                "amount": delta,
                "fx_rate_to_aud": fx_rate,
                "notes": f"[manual_cash_update_{today_str}] balance set to {new_balance:,.2f} {currency}",
            })
        if not rows_to_insert:
            return True, None
        with conn.session as s:
            for row in rows_to_insert:
                s.execute(
                    sql_text(
                        """
                        INSERT INTO transactions
                            (account_id, tx_date, tx_type, amount, fx_rate_to_aud, notes, processed)
                        VALUES
                            (:account_id, :tx_date, :tx_type, :amount, :fx_rate_to_aud, :notes, true)
                        """
                    ),
                    row,
                )
            s.commit()
        load_cash_balances.clear()
        return True, None
    except Exception as e:
        import traceback
        return False, traceback.format_exc()




@st.cache_data(ttl=300)
def get_fx_data():
    try:
        t = yf.Ticker("EURAUD=X")
        now = float(t.fast_info['last_price'])
        hist = yf.download("EURAUD=X", start="2024-01-01", progress=False)['Close']
        if isinstance(hist, pd.DataFrame): hist = hist.iloc[:, 0]
        return now, hist
    except: return 1.6500, None

fx_now, fx_hist = get_fx_data()

# --- 2. DATI N26 (European Portfolio) ---
N26_ACCOUNT_ID = "818cca44-648f-469b-ac01-7366dfda9cc8"

@st.cache_data(ttl=0)
def load_n26_transactions():
    conn = get_pg()
    return conn.query(
        """
        SELECT t.tx_date, i.symbol AS isin, t.tx_type, t.quantity, t.price, t.amount
        FROM transactions t
        JOIN instruments i ON i.id = t.instrument_id
        WHERE t.account_id = :acc_id
        ORDER BY t.tx_date
        """,
        params={"acc_id": N26_ACCOUNT_ID},
        ttl=0,
    )

df_input = load_n26_transactions()

df_raw = pd.DataFrame()
df_raw['Data'] = pd.to_datetime(df_input['tx_date'])
df_raw['ISIN'] = df_input['isin']
df_raw['Tipo'] = df_input['tx_type'].str.upper()
df_raw['Qty'] = pd.to_numeric(df_input['quantity'], errors='coerce')  # already signed (negative on sell) from backfill
df_raw['Inv_EUR'] = pd.to_numeric(df_input['amount'], errors='coerce').abs()
df_raw['Prezzo_Acq'] = pd.to_numeric(df_input['price'], errors='coerce')
df_raw['Manual_Price'] = np.nan  # manual overrides not migrated — flag if you relied on these
df_raw = df_raw.dropna(subset=['ISIN', 'Qty']).sort_values('Data')

def get_fx_at(dt):
    try: return float(fx_hist.asof(dt))
    except: return 1.6500

df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['Data'].apply(get_fx_at)

# --- 3. PREZZI E STORICO N26 ---
@st.cache_data(ttl=3600)
def get_full_market_context(isins_list, current_ticker_map):
    prices_hist = {}
    logs = {}
    for isin in isins_list:
        symbol = current_ticker_map.get(isin)
        try:
            h = yf.download(symbol, start="2025-10-01", progress=False)['Close']
            if isinstance(h, pd.DataFrame): h = h.iloc[:, 0]
            if not h.empty and pd.isna(h.iloc[-1]):
                t_obj = yf.Ticker(symbol)
                last_price_data = t_obj.history(period="1d")['Close']
                if not last_price_data.empty:
                    h.iloc[-1] = last_price_data.iloc[-1]
            if not h.empty:
                prices_hist[isin] = h
                current_val = float(h.iloc[-1])
                market_time = None
                try:
                    ts = t_obj.info.get('regularMarketTime')
                    if ts:
                        market_time = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                except:
                    pass
                if not market_time:
                    market_time = h.index[-1].strftime("%Y-%m-%d") + " (EOD)"
                logs[isin] = {
                    "status": "LIVE",
                    "Price": f"€{current_val:.2f}",
                    "Market Time": market_time,
                    "updated": datetime.now().strftime("%H:%M"),
                    "source": f"Yahoo ({symbol})"
                }
            else:
                raise ValueError()
        except:
            prices_hist[isin] = None
            logs[isin] = {"status": "FALLBACK", "Price": "N/A", "updated": "-", "source": f"Error with {symbol}"}
    return prices_hist, logs

hist_map, diag_logs = get_full_market_context(df_raw['ISIN'].unique().tolist(), ticker_map)

portfolio = df_raw.groupby('ISIN').agg({'Qty': 'sum', 'Inv_EUR': 'sum', 'Inv_AUD': 'sum'}).reset_index()
portfolio = portfolio[portfolio['Qty'].abs() > 0.001]

def get_current_val(row):
    manual = df_raw[df_raw['ISIN'] == row['ISIN']]['Manual_Price'].iloc[-1]
    if pd.notnull(manual) and manual > 0:
        return manual
    h = hist_map.get(row['ISIN'])
    if h is not None and not h.empty:
        val = float(h.iloc[-1])
        if val > 0: return val
    return df_raw[df_raw['ISIN'] == row['ISIN']]['Prezzo_Acq'].iloc[-1]

portfolio['Price_Now'] = portfolio.apply(get_current_val, axis=1)
portfolio['Att_EUR'] = portfolio['Qty'] * portfolio['Price_Now']
portfolio['Att_AUD'] = portfolio['Att_EUR'] * fx_now

asset_performance = []
current_market_value_eur = 0

for isin in df_raw['ISIN'].unique():
    asset_data = df_raw[df_raw['ISIN'] == isin].sort_values('Data')
    net_qty = asset_data['Qty'].sum()
    h = hist_map.get(isin)
    p_now = h.iloc[-1] if (h is not None and not h.empty) else asset_data['Prezzo_Acq'].iloc[0]
    v_at_market_eur = max(0, net_qty * p_now)
    current_market_value_eur += v_at_market_eur
    data_acquisto = asset_data[asset_data['Tipo'] == 'BUY']['Data'].min()
    data_vendita = asset_data[asset_data['Tipo'] == 'SELL']['Data'].max()
    fx_acquisto = asset_data[asset_data['Tipo'] == 'BUY']['Inv_AUD'].sum() / asset_data[asset_data['Tipo'] == 'BUY']['Inv_EUR'].sum() if not asset_data[asset_data['Tipo'] == 'BUY'].empty else 0
    fx_vendita = asset_data[asset_data['Tipo'] == 'SELL']['Inv_AUD'].abs().sum() / asset_data[asset_data['Tipo'] == 'SELL']['Inv_EUR'].abs().sum() if not asset_data[asset_data['Tipo'] == 'SELL'].empty else 0
    cash_in_eur = asset_data[asset_data['Tipo'] == 'SELL']['Inv_EUR'].abs().sum()
    cash_out_eur = asset_data[asset_data['Tipo'] == 'BUY']['Inv_EUR'].sum()
    profit_eur = (v_at_market_eur + cash_in_eur) - cash_out_eur
    cash_in_aud = asset_data[asset_data['Tipo'] == 'SELL']['Inv_AUD'].abs().sum()
    cash_out_aud = asset_data[asset_data['Tipo'] == 'BUY']['Inv_AUD'].sum()
    v_at_market_aud = v_at_market_eur * fx_now
    profit_aud = (v_at_market_aud + cash_in_aud) - cash_out_aud
    asset_performance.append({
        'ISIN': isin, 'Profit_EUR': profit_eur, 'Profit_AUD': profit_aud,
        'Current_Value': v_at_market_eur, 'Data Acquisto': data_acquisto,
        'Data Vendita': data_vendita, 'FX Acquisto': fx_acquisto, 'FX Vendita': fx_vendita
    })

df_perf = pd.DataFrame(asset_performance)

vendite_effettuate = []
df_sells = df_raw[df_raw['Tipo'] == 'SELL'].copy()
for _, row in df_sells.iterrows():
    isin = row['ISIN']
    data_v = row['Data']
    qty_v = abs(row['Qty'])
    prezzo_v = row['Prezzo_Acq']
    incasso_eur = abs(row['Inv_EUR'])
    incasso_aud = abs(row['Inv_AUD'])
    acquisti_precedenti = df_raw[(df_raw['ISIN'] == isin) & (df_raw['Tipo'] == 'BUY') & (df_raw['Data'] < data_v)]
    if not acquisti_precedenti.empty:
        pmc_eur = acquisti_precedenti['Inv_EUR'].sum() / acquisti_precedenti['Qty'].sum()
        pmc_aud = acquisti_precedenti['Inv_AUD'].sum() / acquisti_precedenti['Qty'].sum()
        costo_base_eur = qty_v * pmc_eur
        costo_base_aud = qty_v * pmc_aud
        profit_eur = incasso_eur - costo_base_eur
        profit_aud = incasso_aud - costo_base_aud
        fx_acquisto = pmc_aud / pmc_eur
        fx_vendita = incasso_aud / incasso_eur
    else:
        profit_eur = profit_aud = fx_acquisto = fx_vendita = 0
    vendite_effettuate.append({
        'Data': data_v, 'ISIN': isin, 'Quantità': qty_v, 'Prezzo Vendita': prezzo_v,
        'FX Acquisto (PMC)': fx_acquisto, 'FX Vendita': fx_vendita,
        'Profit_EUR': profit_eur, 'Profit_AUD': profit_aud
    })
df_dettaglio_vendite = pd.DataFrame(vendite_effettuate)

# ── RAIZ TOTAL (hoisted for dashboard) ───────────────────────────────────────
RAIZ_ACCOUNT_ID = "ec7a3f4e-adbb-4d9b-a24e-1b179d29e916"

@st.cache_data(ttl=300)
def _load_raiz_csv_raw():
    """
    Load Raiz transactions from Postgres (migrated from the old CSV pipeline).
    Single source of truth — used by both the dashboard total and Tab 5.
    NOTE: IVV split adjustment was already applied once during the original
    migration/backfill, so quantity/price here are already split-adjusted —
    do not reapply it.
    """
    conn = get_pg()
    df = conn.query(
        """
        SELECT t.tx_date AS "Trade Date",
               REPLACE(i.symbol, 'RAIZ:', '') AS "Instrument Code",
               t.tx_type AS "Transaction Type",
               t.quantity AS "Quantity",
               t.price AS "Price",
               t.amount AS "Amount"
        FROM transactions t
        JOIN instruments i ON i.id = t.instrument_id
        WHERE t.account_id = :acc_id
        ORDER BY t.tx_date
        """,
        params={"acc_id": RAIZ_ACCOUNT_ID},
        ttl=0,
    )
    if df.empty:
        return pd.DataFrame(), ""
    df['Trade Date'] = pd.to_datetime(df['Trade Date'])
    df['Transaction Type'] = df['Transaction Type'].str.upper()
    df['Amount'] = df['Amount'].abs()  # magnitude, matches old CSV convention

    # IVV stock split adjustment (2022-12-09, factor 15.317277).
    # Confirmed via cross-check against the live Raiz app: migrated quantities
    # were NOT split-adjusted despite an earlier assumption that they were.
    IVV_SPLIT_DATE   = pd.Timestamp('2022-12-09')
    IVV_SPLIT_FACTOR = 15.317277
    ivv_pre = (df['Instrument Code'] == 'IVV') & (df['Trade Date'] < IVV_SPLIT_DATE)
    df.loc[ivv_pre, 'Quantity'] = df.loc[ivv_pre, 'Quantity'] * IVV_SPLIT_FACTOR
    df.loc[ivv_pre, 'Price']    = df.loc[ivv_pre, 'Price']    / IVV_SPLIT_FACTOR

    label = f"Postgres · last synced {date.today().isoformat()}"
    return df, label

@st.cache_data(ttl=300)
def _get_raiz_live_prices_shared(codes_tuple):
    """
    Fetch live ASX prices for Raiz ETFs. Shared between dashboard and Tab 5.
    Uses yf.download (5d period) as primary — more reliable than fast_info.
    Falls back to fast_info, then to None.
    """
    RAIZ_TICKERS = {
        'AAA': 'AAA.AX', 'STW': 'STW.AX', 'IAA': 'IAA.AX',
        'IEU': 'IEU.AX', 'IAF': 'IAF.AX', 'RCB': 'RCB.AX', 'IVV': 'IVV.AX'
    }
    prices = {}
    for code in codes_tuple:
        ticker = RAIZ_TICKERS.get(code)
        price = None
        if ticker:
            # Primary: yf.download (most reliable for ASX)
            try:
                h = yf.download(ticker, period='5d', progress=False)['Close']
                if isinstance(h, pd.DataFrame): h = h.iloc[:, 0]
                h = h.dropna()
                if not h.empty:
                    price = float(h.iloc[-1])
            except:
                pass
            # Fallback: fast_info
            if not price:
                try:
                    price = float(yf.Ticker(ticker).fast_info['last_price'])
                    if price <= 0:
                        price = None
                except:
                    pass
        prices[code] = price
    return prices

def get_raiz_total_for_dashboard():
    """Compute current Raiz portfolio value using shared cached CSV and prices."""
    try:
        df, _ = _load_raiz_csv_raw()
        if df.empty:
            return 0.0
        holdings = df.groupby('Instrument Code')['Quantity'].sum().reset_index()
        holdings = holdings[holdings['Quantity'].abs() > 0.0001]
        codes = tuple(holdings['Instrument Code'].unique())
        prices = _get_raiz_live_prices_shared(codes)
        total = 0.0
        for _, row in holdings.iterrows():
            code  = row['Instrument Code']
            price = prices.get(code)
            if not price:
                # Fallback to most recent CSV price
                recent = df[df['Instrument Code'] == code].sort_values('Trade Date', ascending=False)
                price = float(recent.iloc[0]['Price']) if not recent.empty else 0.0
            total += row['Quantity'] * price
        return total
    except Exception:
        return 0.0

raiz_total_aud = get_raiz_total_for_dashboard()

# ── SHARED SHEETS API HELPER ──────────────────────────────────────────────────
def _sheets_read(spreadsheet_id, range_name):
    """Read a range from Google Sheets using the gdrive service account."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    gs = st.secrets["gdrive"]
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
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=range_name
    ).execute()
    rows = result.get("values", [])
    if len(rows) < 2:
        return pd.DataFrame()
    return pd.DataFrame(rows[1:], columns=rows[0])

PORTFOLIO_SHEET_ID = "1ad1wkw7fUdKO-Kq5869JYPsldS_Xr3A0T0W9YLcQKe8"

# ── VANGUARD TOTAL (hoisted for dashboard) ────────────────────────────────────
VANGUARD_ACCOUNT_ID = "8c4ee8bf-29b5-4533-99c7-84850e656e07"

@st.cache_data(ttl=300)
def load_vanguard_transactions_pg():
    conn = get_pg()
    df = conn.query(
        """
        SELECT tx_date AS "Date", tx_type, quantity AS "Quantity",
               price AS "Purchase Price", amount
        FROM transactions
        WHERE account_id = :acc_id
        ORDER BY tx_date
        """,
        params={"acc_id": VANGUARD_ACCOUNT_ID},
        ttl=0,
    )
    df["Date"] = pd.to_datetime(df["Date"])
    df["Transaction"] = df["tx_type"].str.upper()
    df["Amount"] = pd.to_numeric(df["amount"], errors="coerce").abs()  # magnitude, matches old Sheet convention
    return df[["Date", "Transaction", "Quantity", "Purchase Price", "Amount"]]

@st.cache_data(ttl=300)
def get_vanguard_total_for_dashboard():
    try:
        df_v = load_vanguard_transactions_pg()
        if df_v.empty:
            return 0.0
        net_qty = df_v['Quantity'].sum()
        if abs(net_qty) < 0.001:
            return 0.0
        try:
            t = yf.Ticker("VDAL.AX")
            price = float(t.fast_info['last_price'])
        except:
            df_v['Purchase Price'] = pd.to_numeric(df_v['Purchase Price'], errors='coerce')
            price = float(df_v['Purchase Price'].dropna().iloc[-1])
        return max(0.0, net_qty * price)
    except:
        return 0.0

vanguard_total_aud = get_vanguard_total_for_dashboard()

# ── SHARES TOTAL (hoisted for dashboard) ─────────────────────────────────────
SHARES_TICKERS = {
    'NHF': 'NHF.AX',
    'TPG': 'TPG.AX',
    'TUA': 'TUA.AX',
    'WBC': 'WBC.AX',
}

SHARES_ACCOUNT_ID = "d11dbbea-8a63-42da-9329-ab85ec00bea8"

@st.cache_data(ttl=300)
def get_shares_data():
    try:
        conn = get_pg()
        df_raw_shares = conn.query(
            """
            SELECT REPLACE(i.symbol, 'ASX:', '') AS "Share",
                   SUM(t.quantity) AS "Quantity"
            FROM transactions t
            JOIN instruments i ON i.id = t.instrument_id
            WHERE t.account_id = :acc_id
            GROUP BY i.symbol
            """,
            params={"acc_id": SHARES_ACCOUNT_ID},
            ttl=0,
        )
        if df_raw_shares.empty:
            return pd.DataFrame(), 0.0
        df_s = df_raw_shares
        df_s['Quantity'] = pd.to_numeric(df_s['Quantity'], errors='coerce').fillna(0)
        df_s = df_s[df_s['Quantity'] > 0].copy()
        rows = []
        total = 0.0
        for _, row in df_s.iterrows():
            code = str(row['Share']).strip()
            qty = row['Quantity']
            ticker = SHARES_TICKERS.get(code, f"{code}.AX")
            price = None
            try:
                h = yf.download(ticker, period='5d', progress=False)['Close']
                if isinstance(h, pd.DataFrame): h = h.iloc[:, 0]
                h = h.dropna()
                if not h.empty:
                    price = float(h.iloc[-1])
            except:
                pass
            value = qty * price if price else 0.0
            total += value
            rows.append({
                'Code': code,
                'Name': {'NHF': 'NIB Holdings', 'TPG': 'TPG Telecom',
                         'TUA': 'Tuas Limited', 'WBC': 'Westpac'}.get(code, code),
                'Ticker': ticker,
                'Quantity': qty,
                'Live Price (AUD)': price,
                'Value (AUD)': value,
                'Source': '🟢 Live' if price else '🔴 N/A'
            })
        return pd.DataFrame(rows), total
    except Exception as e:
        return pd.DataFrame(), 0.0

df_shares, shares_total_aud = get_shares_data()

METALS_ACCOUNT_ID = "d2e04bcf-04fc-4151-bcb5-3ff64ccf1f97"

@st.cache_data(ttl=0)
def load_metal_data():
    try:
        conn = get_pg()
        df_m = conn.query(
            """
            SELECT t.tx_date AS "Date",
                   TRIM(REPLACE(i.symbol, 'METAL:', '')) AS "Type",
                   t.tx_type,
                   t.quantity AS "Quantity",
                   t.price AS "Purchase Price",
                   t.amount
            FROM transactions t
            JOIN instruments i ON i.id = t.instrument_id
            WHERE t.account_id = :acc_id
            ORDER BY t.tx_date
            """,
            params={"acc_id": METALS_ACCOUNT_ID},
            ttl=0,
        )
        if df_m.empty:
            return pd.DataFrame(), None
        df_m["Date"] = pd.to_datetime(df_m["Date"])
        df_m["Transaction"] = df_m["tx_type"].str.upper()
        df_m["Currency"] = "AUD"  # all Revolut Metals purchases confirmed AUD
        return df_m[["Date", "Type", "Transaction", "Quantity", "Purchase Price", "Currency"]], None
    except Exception as e:
        return None, str(e)

# ── COMMODITIES TOTAL (hoisted for dashboard) ─────────────────────────────────
@st.cache_data(ttl=300)
def get_commodities_total_for_dashboard():
    try:
        df_m, _err = load_metal_data()
        if df_m is None or df_m.empty:
            return 0.0

        METAL_TICKERS = {'Gold': 'GC=F', 'Silver': 'SI=F', 'Platinum': 'PL=F'}
        try:
            usd_aud = float(yf.Ticker("AUDUSD=X").fast_info['last_price'])
            usd_aud = 1 / usd_aud if usd_aud > 0 else 1.58
        except:
            usd_aud = 1.58

        holdings = df_m.groupby('Type')['Quantity'].sum().reset_index()
        holdings = holdings[holdings['Quantity'].abs() > 0.00001]
        total = 0.0
        for _, row in holdings.iterrows():
            metal = row['Type']
            ticker = METAL_TICKERS.get(metal)
            price_usd = None
            if ticker:
                try:
                    t = yf.Ticker(ticker)
                    price_usd = float(t.fast_info['last_price'])
                except:
                    pass
            if price_usd:
                price_aud = price_usd * usd_aud
                total += row['Quantity'] * price_aud
            else:
                # Purchase Price is already in AUD — no conversion needed
                recent = df_m[df_m['Type'] == metal].sort_values('Date', ascending=False)
                if not recent.empty:
                    pp = pd.to_numeric(recent.iloc[0]['Purchase Price'], errors='coerce')
                    if pd.notnull(pp):
                        total += row['Quantity'] * float(pp)
        return total
    except:
        return 0.0

commodities_total_aud = get_commodities_total_for_dashboard()

# ── CASH TOTAL (hoisted for dashboard) ───────────────────────────────────────
@st.cache_data(ttl=0)
def get_cash_total_for_dashboard():
    try:
        ACCOUNTS_CURR = {
            "CBA": "AUD", "Me Bank": "AUD", "Rabobank": "AUD", "Up": "AUD",
            "Trade Republic": "EUR", "N26": "EUR",
            "BPM Cash": "EUR", "BPM Bonds": "EUR",
            "C6 Cash": "BRL", "C6 Investments": "BRL",
        }
        bal = load_cash_balances()
        brl_rate = 0.27
        try:
            brl_rate = float(yf.Ticker("BRLAUD=X").fast_info['last_price'])
        except:
            pass
        total = 0.0
        for name, currency in ACCOUNTS_CURR.items():
            b = bal.get(name, 0.0)
            if currency == "AUD":
                total += b
            elif currency == "EUR":
                total += b * fx_now
            else:
                total += b * brl_rate
        return total
    except:
        return 0.0

cash_total_aud = get_cash_total_for_dashboard()

# ── SUPER TOTAL (hoisted for dashboard) ──────────────────────────────────────
@st.cache_data(ttl=0)
def get_super_total_for_dashboard():
    try:
        bal = load_cash_balances()
        return float(bal.get("Super", 0.0))
    except:
        return 0.0

super_total_aud = get_super_total_for_dashboard()

# ── GLOBAL SAVE FUNCTIONS ─────────────────────────────────────────────────────
# save_cash_balances() and load_cash_balances() now live near the top of the
# file, next to CASH_ACCOUNTS — they read/write Postgres, not Sheets.
def calculate_weighted_interest(prev_balance, prev_date, curr_balance, curr_date, transactions, interest_rate_pct):
    """
    Calculate interest using weighted average balance based on transaction dates.
    """
    # Ensure transactions is a list (not None)
    if transactions is None:
        transactions = []
    
    prev_date_obj = prev_date if hasattr(prev_date, 'date') else prev_date
    curr_date_obj = curr_date if hasattr(curr_date, 'date') else curr_date
    
    days_in_period = (curr_date_obj - prev_date_obj).days
    if days_in_period <= 0:
        return 0.0
    
    if not transactions:
        # No transactions - simple constant balance
        avg_balance = prev_balance
    else:
        # Sort transactions by date
        sorted_txs = sorted(transactions, key=lambda x: x['date'])
        
        total_weighted_balance = 0
        total_days = 0
        current_balance = prev_balance
        last_date = prev_date_obj
        
        for tx in sorted_txs:
            tx_date = tx['date'].date() if hasattr(tx['date'], 'date') else tx['date']
            last_date_obj = last_date.date() if hasattr(last_date, 'date') else last_date
            
            days = (tx_date - last_date_obj).days
            if days > 0:
                total_weighted_balance += current_balance * days
                total_days += days
            
            # Apply transaction (amount can be positive or negative)
            current_balance += tx['amount']
            last_date = tx['date']
        
        # Add remaining days after last transaction
        last_date_obj = last_date.date() if hasattr(last_date, 'date') else last_date
        days = (curr_date_obj - last_date_obj).days
        if days > 0:
            total_weighted_balance += current_balance * days
            total_days += days
        
        avg_balance = total_weighted_balance / total_days if total_days > 0 else prev_balance
    
    # Calculate interest
    daily_rate = interest_rate_pct / 100 / 365
    interest_earned = avg_balance * daily_rate * days_in_period
    
    return interest_earned

def get_cash_transactions_for_period(start_date, end_date):
    """
    Get all cash-affecting transactions between two dates.
    Returns empty list if no transactions found.
    """
    transactions = []
    
    # N26 transactions (Buying = cash outflow, Selling = cash inflow)
    try:
        n26_txs = df_raw[(df_raw['Data'].dt.date >= start_date) & (df_raw['Data'].dt.date <= end_date)]
        for _, tx in n26_txs.iterrows():
            fx_rate = get_fx_at(tx['Data'])
            if tx['Tipo'] == 'BUY':
                transactions.append({
                    'date': tx['Data'],
                    'amount': -tx['Inv_EUR'] * fx_rate,
                    'type': 'N26_Investment'
                })
            elif tx['Tipo'] == 'SELL':
                transactions.append({
                    'date': tx['Data'],
                    'amount': tx['Inv_EUR'] * fx_rate,
                    'type': 'N26_Sale'
                })
    except Exception as e:
        pass  # Silently continue, return empty list
    
    # Raiz transactions
    try:
        raiz_df = get_raiz_transactions()
        if raiz_df is not None and not raiz_df.empty and 'Transaction Type' in raiz_df.columns:
            raiz_period = raiz_df[(raiz_df['Trade Date'].dt.date >= start_date) & (raiz_df['Trade Date'].dt.date <= end_date)]
            for _, tx in raiz_period.iterrows():
                txtype = str(tx['Transaction Type']).upper().strip()
                if txtype == 'BUY':
                    transactions.append({
                        'date': tx['Trade Date'],
                        'amount': -abs(tx['Amount']),
                        'type': 'Raiz_Deposit'
                    })
                elif txtype == 'WITHDRAWAL':
                    transactions.append({
                        'date': tx['Trade Date'],
                        'amount': abs(tx['Amount']),
                        'type': 'Raiz_Withdrawal'
                    })
    except Exception as e:
        pass  # Silently continue
    
    # Return sorted list (will be empty if no transactions)
    return sorted(transactions, key=lambda x: x['date']) if transactions else []
    
   
def save_net_worth_snapshot(total, force=False):
    """
    Save a net worth snapshot with clean, non-overlapping attribution.

    Attribution identity (must sum to total_change):
        total_change = contributions + market_gains + aud_cash_interest
                     + eur_cash_interest + dividends + fx_impact + unexplained

    Each component is calculated directly (not as residuals of others),
    except contributions which is the final residual to close the gap.
    This ensures the waterfall always reconciles.
    """
    try:
        # ── Current snapshot values ───────────────────────────────────────
        n26_aud       = current_market_value_eur * fx_now
        raiz_aud      = raiz_total_aud
        vanguard_aud  = vanguard_total_aud
        shares_aud    = shares_total_aud
        commodities_aud = commodities_total_aud
        super_aud     = super_total_aud
        cash_aud      = cash_total_aud  # all cash in AUD equiv

        # EUR cash sub-total (EUR accounts only, in AUD)
        EUR_CASH_ACCOUNTS = ["Trade Republic", "N26", "BPM Cash", "BPM Bonds"]  # BUNQ dropped, account closed
        bal = load_cash_balances()
        eur_cash_eur  = sum(bal.get(a, 0.0) for a in EUR_CASH_ACCOUNTS)
        eur_cash_aud  = eur_cash_eur * fx_now

        # ── Read existing history from Postgres ────────────────────────────
        pg_conn = get_pg()
        df_existing = pg_conn.query(
            """
            SELECT snapshot_date, total_aud, n26_aud, raiz_aud, vanguard_aud,
                   shares_aud, commodities_aud, super_aud, cash_aud, eur_cash_aud
            FROM net_worth_snapshots
            ORDER BY snapshot_date
            """,
            ttl=0,
        )

        today = date.today()
        if not force and not df_existing.empty:
            try:
                last_date = pd.to_datetime(df_existing.iloc[-1]['snapshot_date'])
                if last_date.year == today.year and last_date.month == today.month:
                    return False, "Already saved this month"
            except:
                pass

        # Find the most recent row that is NOT dated today, so a same-day
        # re-save doesn't collapse the attribution window to zero days.
        baseline_row = None
        if not df_existing.empty:
            df_not_today = df_existing[pd.to_datetime(df_existing['snapshot_date']).dt.date != today]
            if not df_not_today.empty:
                baseline_row = df_not_today.iloc[-1]

        # ── Initialise all attribution to zero ───────────────────────────
        starting_balance      = 0.0
        contributions         = 0.0
        market_gains          = 0.0
        fx_impact             = 0.0
        aud_cash_interest     = 0.0
        eur_cash_interest     = 0.0
        eur_cash_deposits_aud = 0.0
        n26_dividends         = 0.0
        shares_dividends      = 0.0
        contribution_breakdown = ""

        if baseline_row is not None:
            prev = baseline_row
            def _f(val, default=0.0):
                try: return float(val) if pd.notnull(val) else default
                except: return default

            prev_date         = pd.to_datetime(prev['snapshot_date']).date()
            prev_total        = _f(prev['total_aud'])
            starting_balance  = prev_total
            prev_n26          = _f(prev['n26_aud']);  prev_raiz    = _f(prev['raiz_aud'])
            prev_vanguard     = _f(prev['vanguard_aud']); prev_shares = _f(prev['shares_aud'])
            prev_comm         = _f(prev['commodities_aud']); prev_super = _f(prev['super_aud'])
            prev_cash         = _f(prev['cash_aud']); prev_eur_cash_aud = _f(prev['eur_cash_aud'])
            days_in_period    = (today - prev_date).days

            # ── 1. INTEREST — weighted average rate × average balance ─────
            # Only meaningful for periods ≥ 7 days (avoids test-save noise)
            if days_in_period >= 7:
                aud_rate = 5.35  # % p.a. default
                eur_rate = 2.00  # % p.a. default
                try:
                    df_fc = pg_conn.query(
                        "SELECT category, key, value FROM forecast_settings WHERE category = 'Interest'",
                        ttl=0,
                    )
                    if not df_fc.empty:
                        aud_accs = ['CBA','Me Bank','Rabobank','Up']
                        eur_accs = ['Trade Republic','N26','BPM Cash','BPM Bonds']
                        def _weighted_rate(accs):
                            total_bal = rate_num = 0.0
                            for acc in accs:
                                b = bal.get(acc, 0.0)
                                r_row = df_fc[df_fc['key'] == acc]
                                r = float(r_row['value'].iloc[0]) if not r_row.empty else 0.0
                                rate_num  += r * b
                                total_bal += b
                            return (rate_num / total_bal) if total_bal > 0 else 0.0
                        aud_rate = _weighted_rate(aud_accs) or aud_rate
                        eur_rate = _weighted_rate(eur_accs) or eur_rate
                except:
                    pass

                aud_cash_only = cash_aud - eur_cash_aud
                prev_aud_cash = prev_cash - prev_eur_cash_aud
                avg_aud = (prev_aud_cash + aud_cash_only) / 2
                aud_cash_interest = avg_aud * (aud_rate / 100) * (days_in_period / 365)

                avg_eur_aud = (prev_eur_cash_aud + eur_cash_aud) / 2
                eur_cash_interest = avg_eur_aud * (eur_rate / 100) * (days_in_period / 365)

            # ── 2. DIVIDENDS — read unprocessed rows from Postgres ─────────
            processed_ids = []
            try:
                df_div_unprocessed = pg_conn.query(
                    """
                    SELECT id, div_date, portfolio, amount, currency
                    FROM dividends
                    WHERE processed = false
                    ORDER BY div_date
                    """,
                    ttl=0,
                )
                if not df_div_unprocessed.empty:
                    for _, drow in df_div_unprocessed.iterrows():
                        amt = float(drow['amount']) if pd.notnull(drow['amount']) else 0.0
                        cur = str(drow['currency']).upper().strip()
                        div_date_val = pd.to_datetime(drow['div_date'])
                        if cur.startswith('EUR'):
                            amt_aud = amt * get_fx_at(div_date_val)
                        elif cur.startswith('USD'):
                            try:
                                amt_aud = amt / float(yf.Ticker("AUDUSD=X").fast_info['last_price'])
                            except:
                                amt_aud = amt * 1.58
                        else:
                            amt_aud = amt
                        port = str(drow['portfolio']).upper()
                        if 'N26' in port:
                            n26_dividends += amt_aud
                        else:
                            shares_dividends += amt_aud
                        processed_ids.append(str(drow['id']))
            except:
                processed_ids = []

            # ── 3. MARKET GAINS ─────────────────────────────────────────────
            curr_investments = n26_aud + raiz_aud + vanguard_aud + shares_aud + commodities_aud + super_aud
            prev_investments = prev_n26 + prev_raiz + prev_vanguard + prev_shares + prev_comm + prev_super
            period_contrib_total, breakdown_dict = calculate_period_contributions(prev_date, today)
            market_gains = (curr_investments - prev_investments) - period_contrib_total

            # ── 4. FX IMPACT ─────────────────────────────────────────────
            eur_cash_change_aud = eur_cash_aud - prev_eur_cash_aud
            eur_deposits_from_tx = -breakdown_dict.get('N26', 0.0)
            fx_impact = eur_cash_change_aud - eur_deposits_from_tx - eur_cash_interest
            eur_cash_deposits_aud = eur_deposits_from_tx

            # ── 5. CONTRIBUTIONS — residual ────────────────────────────────
            total_change = total - prev_total
            contributions = (total_change
                             - market_gains
                             - aud_cash_interest
                             - eur_cash_interest
                             - (n26_dividends + shares_dividends)
                             - fx_impact)

            contribution_breakdown = "; ".join(
                f"{k}: ${v:,.0f}" for k, v in breakdown_dict.items() if v > 0
            )

        # ── Upsert snapshot row + mark dividends processed, atomically ────
        with pg_conn.session as s:
            if 'processed_ids' in dir() and processed_ids:
                placeholders = ", ".join(f"'{i}'" for i in processed_ids)
                s.execute(sql_text(
                    f"UPDATE dividends SET processed = true WHERE id IN ({placeholders})"
                ))
            s.execute(
                sql_text("""
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
                    ON CONFLICT (snapshot_date) DO UPDATE SET
                        total_aud = EXCLUDED.total_aud,
                        contributions_aud = EXCLUDED.contributions_aud,
                        market_gains_aud = EXCLUDED.market_gains_aud,
                        fx_impact_aud = EXCLUDED.fx_impact_aud,
                        starting_balance_aud = EXCLUDED.starting_balance_aud,
                        contribution_breakdown = EXCLUDED.contribution_breakdown,
                        n26_aud = EXCLUDED.n26_aud,
                        raiz_aud = EXCLUDED.raiz_aud,
                        vanguard_aud = EXCLUDED.vanguard_aud,
                        shares_aud = EXCLUDED.shares_aud,
                        commodities_aud = EXCLUDED.commodities_aud,
                        super_aud = EXCLUDED.super_aud,
                        cash_aud = EXCLUDED.cash_aud,
                        eur_cash_aud = EXCLUDED.eur_cash_aud,
                        eur_cash_deposits_aud = EXCLUDED.eur_cash_deposits_aud,
                        aud_cash_interest_aud = EXCLUDED.aud_cash_interest_aud,
                        eur_cash_interest_aud = EXCLUDED.eur_cash_interest_aud,
                        n26_dividends_aud = EXCLUDED.n26_dividends_aud,
                        shares_dividends_aud = EXCLUDED.shares_dividends_aud,
                        n26_eur_value = EXCLUDED.n26_eur_value
                """),
                {
                    "snapshot_date": today,
                    "total_aud": float(round(total, 2)),
                    "contributions_aud": float(round(contributions, 2)),
                    "market_gains_aud": float(round(market_gains, 2)),
                    "fx_impact_aud": float(round(fx_impact, 2)),
                    "starting_balance_aud": float(round(starting_balance, 2)),
                    "contribution_breakdown": contribution_breakdown,
                    "n26_aud": float(round(n26_aud, 2)),
                    "raiz_aud": float(round(raiz_aud, 2)),
                    "vanguard_aud": float(round(vanguard_aud, 2)),
                    "shares_aud": float(round(shares_aud, 2)),
                    "commodities_aud": float(round(commodities_aud, 2)),
                    "super_aud": float(round(super_aud, 2)),
                    "cash_aud": float(round(cash_aud, 2)),
                    "eur_cash_aud": float(round(eur_cash_aud, 2)),
                    "eur_cash_deposits_aud": float(round(eur_cash_deposits_aud, 2)),
                    "aud_cash_interest_aud": float(round(aud_cash_interest, 2)),
                    "eur_cash_interest_aud": float(round(eur_cash_interest, 2)),
                    "n26_dividends_aud": float(round(n26_dividends, 2)),
                    "shares_dividends_aud": float(round(shares_dividends, 2)),
                    "n26_eur_value": float(round(current_market_value_eur, 2)),
                }
            )
            s.commit()
        return True, None
    except Exception as e:
        import traceback
        return False, traceback.format_exc()
@st.cache_data(ttl=60)
def load_net_worth_history():
    try:
        conn = get_pg()
        df_nw = conn.query(
            """
            SELECT
                snapshot_date AS "Date",
                total_aud AS "Total_AUD",
                contributions_aud AS "Contributions_AUD",
                market_gains_aud AS "Market_Gains_AUD",
                fx_impact_aud AS "FX_Impact_AUD",
                starting_balance_aud AS "Starting_Balance_AUD",
                contribution_breakdown AS "Contribution_Breakdown",
                n26_aud AS "N26_AUD",
                raiz_aud AS "Raiz_AUD",
                vanguard_aud AS "Vanguard_AUD",
                shares_aud AS "Shares_AUD",
                commodities_aud AS "Commodities_AUD",
                super_aud AS "Super_AUD",
                cash_aud AS "Cash_AUD",
                eur_cash_aud AS "EUR_Cash_AUD",
                eur_cash_deposits_aud AS "EUR_Cash_Deposits_AUD",
                aud_cash_interest_aud AS "AUD_Cash_Interest_AUD",
                eur_cash_interest_aud AS "EUR_Cash_Interest_AUD",
                n26_dividends_aud AS "N26_Dividends_Received_AUD",
                shares_dividends_aud AS "Shares_Dividends_Received_AUD",
                n26_eur_value AS "N26_EUR_Value"
            FROM net_worth_snapshots
            ORDER BY snapshot_date
            """,
            ttl=0,
        )
        if df_nw.empty:
            return pd.DataFrame(columns=['Date', 'Total_AUD'])
        df_nw['Date'] = pd.to_datetime(df_nw['Date'])
        numeric_cols = [c for c in df_nw.columns if c not in ('Date', 'Contribution_Breakdown')]
        for col in numeric_cols:
            df_nw[col] = pd.to_numeric(df_nw[col], errors='coerce').fillna(0)
        df_nw['Contribution_Breakdown'] = df_nw['Contribution_Breakdown'].fillna("")
        return df_nw.dropna(subset=['Total_AUD'])
    except Exception as e:
        return pd.DataFrame(columns=['Date', 'Total_AUD'])
        # ==================== NET WORTH CHANGE ANALYSIS ====================
def analyze_net_worth_change(df_history, start_date, end_date):
    """
    Read saved attribution columns for the period and return a clean summary.
    All values come directly from what was recorded at save time — no re-derivation.
    The components sum exactly to total_change by construction (contributions is the residual).
    """
    mask = (df_history['Date'].dt.date >= start_date) & (df_history['Date'].dt.date <= end_date)
    df_period = df_history[mask].sort_values('Date').copy()
    if len(df_period) < 2:
        return None

    start_row = df_period.iloc[0]
    end_row   = df_period.iloc[-1]
    start_value  = float(start_row['Total_AUD'])
    end_value    = float(end_row['Total_AUD'])
    total_change = end_value - start_value
    total_change_pct = (total_change / start_value * 100) if start_value != 0 else 0

    def _col(name):
        """Sum a column across all rows in the period except the first (which is the baseline)."""
        rows = df_period.iloc[1:]   # attribution rows: each row stores what CHANGED since prev
        if name in rows.columns:
            return float(rows[name].sum())
        return 0.0

    market_gains      = _col('Market_Gains_AUD')
    total_contributions = _col('Contributions_AUD')
    aud_cash_interest = _col('AUD_Cash_Interest_AUD')
    eur_cash_interest = _col('EUR_Cash_Interest_AUD')
    n26_dividends     = _col('N26_Dividends_Received_AUD')
    shares_dividends  = _col('Shares_Dividends_Received_AUD')
    fx_impact         = _col('FX_Impact_AUD')
    total_cash_interest = aud_cash_interest + eur_cash_interest
    total_dividends     = n26_dividends + shares_dividends

    # ── Per-platform market gain breakdown ─────────────────────────────────────
    # Each platform: end_value - start_value from the snapshot columns.
    # For N26 specifically we split: price gain (EUR) vs FX gain.
    # FX gain on N26 = start_EUR_value * (fx_end - fx_start)
    # Price gain on N26 = total_N26_AUD_change - FX_gain

    def _snap(col, row):
        return float(row[col]) if col in row.index and pd.notna(row[col]) else 0.0

    platform_gains = {}
    for col, label in [
        ('Raiz_AUD',       '🌱 Raiz'),
        ('Vanguard_AUD',   '📈 Vanguard'),
        ('Shares_AUD',     '🇦🇺 ASX Shares'),
        ('Commodities_AUD','🪙 Commodities'),
        ('Super_AUD',      '🏛️ Super'),
    ]:
        gain = _snap(col, end_row) - _snap(col, start_row)
        platform_gains[label] = gain

    # ── Cash balance change (AUD vs EUR, separate from interest) ───────────
    cash_start_total = _snap('Cash_AUD', start_row)
    cash_end_total    = _snap('Cash_AUD', end_row)
    eur_cash_start    = _snap('EUR_Cash_AUD', start_row)
    eur_cash_end      = _snap('EUR_Cash_AUD', end_row)
    aud_cash_start    = cash_start_total - eur_cash_start
    aud_cash_end      = cash_end_total - eur_cash_end

    cash_breakdown = {
        'aud_cash_start': aud_cash_start, 'aud_cash_end': aud_cash_end,
        'aud_cash_change': aud_cash_end - aud_cash_start,
        'eur_cash_start': eur_cash_start, 'eur_cash_end': eur_cash_end,
        'eur_cash_change': eur_cash_end - eur_cash_start,
        'total_cash_start': cash_start_total, 'total_cash_end': cash_end_total,
        'total_cash_change': cash_end_total - cash_start_total,
    }
    # Where cash moved to, as recorded at save time (each row logs what left
    # cash and went into investments during that period)
    movement_notes = []
    if 'Contribution_Breakdown' in df_period.columns:
        for _, r in df_period.iloc[1:].iterrows():
            note = str(r.get('Contribution_Breakdown', '')).strip()
            if note:
                movement_notes.append(f"{r['Date'].strftime('%d %b %Y')}: {note}")
    cash_breakdown['movement_notes'] = movement_notes

    # N26: decompose into price gain vs FX gain
    n26_start_aud = _snap('N26_AUD', start_row)
    n26_end_aud   = _snap('N26_AUD', end_row)
    n26_total_change_aud = n26_end_aud - n26_start_aud

    n26_start_eur = _snap('N26_EUR_Value', start_row)
    n26_end_eur   = _snap('N26_EUR_Value', end_row)

    if n26_start_eur > 0 and n26_end_eur > 0:
        # Reconstruct FX rates from stored values
        fx_start = n26_start_aud / n26_start_eur if n26_start_eur != 0 else 0
        fx_end   = n26_end_aud   / n26_end_eur   if n26_end_eur   != 0 else 0
        # FX impact on N26 = start EUR exposure * FX rate change
        n26_fx_gain   = n26_start_eur * (fx_end - fx_start)
        # Price gain = change in EUR value * average FX rate
        n26_eur_change = n26_end_eur - n26_start_eur
        n26_price_gain = n26_eur_change * ((fx_start + fx_end) / 2)
        platform_gains['🇪🇺 N26 (price, net of FX)'] = n26_price_gain
        platform_gains['💱 N26 FX effect'] = n26_fx_gain
    else:
        # Fallback: no EUR data available, show total AUD change
        platform_gains['🇪🇺 N26'] = n26_total_change_aud

    def _pct(v):
        return (v / abs(total_change) * 100) if total_change != 0 else 0

    # Detect old rows with no attribution data (all zeros) — warn user but don't show as "unexplained"
    attr_cols = ['Contributions_AUD','Market_Gains_AUD','AUD_Cash_Interest_AUD',
                 'EUR_Cash_Interest_AUD','FX_Impact_AUD']
    rows_with_data = df_period.iloc[1:]
    has_zero_rows = False
    if not rows_with_data.empty and all(c in rows_with_data.columns for c in attr_cols):
        zero_mask = (rows_with_data[attr_cols].abs().sum(axis=1) == 0)
        has_zero_rows = bool(zero_mask.any())

    return {
        'start_date':        start_row['Date'].date(),
        'end_date':          end_row['Date'].date(),
        'start_value':       start_value,
        'end_value':         end_value,
        'total_change':      total_change,
        'total_change_pct':  total_change_pct,
        'market_gains':      market_gains,
        'total_contributions': total_contributions,
        'aud_cash_interest': aud_cash_interest,
        'eur_cash_interest': eur_cash_interest,
        'total_cash_interest': total_cash_interest,
        'n26_dividends':     n26_dividends,
        'shares_dividends':  shares_dividends,
        'total_dividends':   total_dividends,
        'fx_impact':         fx_impact,
        'has_zero_rows':     has_zero_rows,
        'market_pct':        _pct(market_gains),
        'contrib_pct':       _pct(total_contributions),
        'interest_pct':      _pct(total_cash_interest),
        'dividends_pct':     _pct(total_dividends),
        'fx_pct':            _pct(fx_impact),
        'days':              (end_row['Date'] - start_row['Date']).days,
        'platform_gains':    platform_gains,
        'cash_breakdown':    cash_breakdown,
    }
# ==================== ADDITION: CONTRIBUTION TRACKING ====================
# Add these functions right after load_net_worth_history() and before the tabs

@st.cache_data(ttl=300)
def get_raiz_transactions():
    """Get Raiz transaction history for contribution tracking"""
    try:
        df, _label = _load_raiz_csv_raw()
        return df
    except:
        return pd.DataFrame()

@st.cache_data(ttl=300)
def get_vanguard_transactions():
    """Get Vanguard transaction history"""
    try:
        return load_vanguard_transactions_pg()
    except:
        return pd.DataFrame()

@st.cache_data(ttl=300)
def get_commodities_transactions():
    """Get commodities transaction history"""
    try:
        df_m, _err = load_metal_data()
        return df_m if df_m is not None else pd.DataFrame()
    except:
        return pd.DataFrame()

def calculate_period_contributions(start_date, end_date):
    """Calculate contributions across all asset classes between two dates"""
    breakdown = {}
    total = 0.0
    
   # N26 Contributions
    # NOTE: uses fx_now (today's rate) rather than the historical trade-date
    # rate. This is deliberate: this figure only feeds Dashboard attribution
    # (market gains / FX impact), which compares against cash-side balances
    # that are also converted at fx_now. Using one consistent rate for both
    # sides of a transfer makes them net to zero as they should, instead of
    # leaking a rate-mismatch into Market Gains / FX Impact.
    # This does NOT affect df_raw, Tabs 1-4, or any CGT/lot-level figures —
    # those all continue to use the real historical FX rate via get_fx_at().
    try:
        n26_contrib = df_raw[
            (df_raw['Tipo'] == 'BUY') & 
            (df_raw['Data'].dt.date >= start_date) & 
            (df_raw['Data'].dt.date <= end_date)
        ].copy()
        n26_total = 0.0
        for _, row in n26_contrib.iterrows():
            n26_total += row['Inv_EUR'] * fx_now
        breakdown['N26'] = n26_total
        total += n26_total
    except:
        breakdown['N26'] = 0.0
    
    # Raiz Contributions
    # Raiz CSV uses 'INVEST' for regular deposits (not 'DEPOSIT').
    # Amount is negative for buys (cash outflow), so use abs().
    try:
        raiz_df = get_raiz_transactions()
        if not raiz_df.empty and 'Transaction Type' in raiz_df.columns:
            raiz_df['_txtype'] = raiz_df['Transaction Type'].str.upper().str.strip()
            raiz_period = raiz_df[
                (raiz_df['Trade Date'].dt.date >= start_date) & 
                (raiz_df['Trade Date'].dt.date <= end_date) &
                (raiz_df['_txtype'] == 'BUY')
            ]
            raiz_total = raiz_period['Amount'].abs().sum()
            breakdown['Raiz'] = raiz_total
            total += raiz_total
    except:
        breakdown['Raiz'] = 0.0
    
    # Vanguard Contributions
    try:
        vanguard_df = get_vanguard_transactions()
        if not vanguard_df.empty:
            vanguard_period = vanguard_df[
                (vanguard_df['Date'].dt.date >= start_date) & 
                (vanguard_df['Date'].dt.date <= end_date) &
                (vanguard_df['Transaction'].str.upper() == 'BUY')
            ]
            vanguard_total = vanguard_period['Amount'].sum()
            breakdown['Vanguard'] = vanguard_total
            total += vanguard_total
    except:
        breakdown['Vanguard'] = 0.0
    
    # Commodities Contributions
    try:
        commodities_df = get_commodities_transactions()
        if not commodities_df.empty:
            commodities_period = commodities_df[
                (commodities_df['Date'].dt.date >= start_date) & 
                (commodities_df['Date'].dt.date <= end_date) &
                (commodities_df['Transaction'].str.upper() == 'BUY')
            ]
            commodities_total = (commodities_period['Quantity'] * commodities_period['Purchase Price']).sum()
            breakdown['Commodities'] = commodities_total
            total += commodities_total
    except:
        breakdown['Commodities'] = 0.0
    
    return total, breakdown


# ==================== CONTRIBUTION TRACKING FUNCTIONS ====================



# ── METAL / COMMODITY FUNCTIONS (top-level so cache clears work) ─────────────
METAL_CONFIG = {
    'Gold':     {'ticker': 'XAUAUD=X', 'symbol': 'XAU', 'unit': 'unit', 'colour': '#f39c12'},
    'Silver':   {'ticker': 'XAGAUD=X', 'symbol': 'XAG', 'unit': 'unit', 'colour': '#95a5a6'},
    'Platinum': {'ticker': 'XPTAUD=X', 'symbol': 'XPT', 'unit': 'unit', 'colour': '#8e44ad'},
}

@st.cache_data(ttl=300)
def get_metal_prices():
    # Strategy: fetch USD spot price via GC=F/SI=F/PL=F then convert to AUD
    # using AUDUSD=X rate. More reliable than direct AUD cross tickers.
    USD_TICKERS = {
        'Gold':     'GC=F',
        'Silver':   'SI=F',
        'Platinum': 'PL=F',
    }
    # Get AUD/USD rate
    aud_usd = None
    for fx_ticker in ['AUDUSD=X', 'USDAUD=X']:
        try:
            h = yf.download(fx_ticker, period='5d', progress=False)['Close']
            if isinstance(h, pd.DataFrame): h = h.iloc[:, 0]
            h = h.dropna()
            if not h.empty:
                rate = float(h.iloc[-1])
                if fx_ticker == 'AUDUSD=X':
                    aud_usd = rate          # AUD per 1 USD = 1/rate
                    usd_to_aud_live = 1 / rate
                else:
                    usd_to_aud_live = rate
                break
        except:
            continue
    if aud_usd is None:
        usd_to_aud_live = 1.58  # fallback

    prices = {}
    for metal, cfg in METAL_CONFIG.items():
        usd_ticker = USD_TICKERS.get(metal)
        price_aud = None
        # Try USD futures first
        if usd_ticker:
            try:
                h = yf.download(usd_ticker, period='5d', progress=False)['Close']
                if isinstance(h, pd.DataFrame): h = h.iloc[:, 0]
                h = h.dropna()
                if not h.empty:
                    price_usd = float(h.iloc[-1])
                    price_aud = price_usd * usd_to_aud_live
            except:
                pass
        # Try direct AUD cross as fallback
        if price_aud is None:
            try:
                h = yf.download(cfg['ticker'], period='5d', progress=False)['Close']
                if isinstance(h, pd.DataFrame): h = h.iloc[:, 0]
                h = h.dropna()
                if not h.empty:
                    price_aud = float(h.iloc[-1])
            except:
                pass
        prices[metal] = {'usd': None, 'aud': price_aud}
    return prices

@st.cache_data(ttl=3600)
def get_hist_fx_rate(from_currency, to_currency, dt_str):
    if from_currency == to_currency:
        return 1.0
    # Try the direct cross first, then the inverse
    for ticker, invert in [(f"{from_currency}{to_currency}=X", False),
                           (f"{to_currency}{from_currency}=X", True)]:
        try:
            hist = yf.download(ticker, start=dt_str, period="5d", progress=False)['Close']
            if isinstance(hist, pd.DataFrame): hist = hist.iloc[:, 0]
            hist = hist.dropna()
            if not hist.empty:
                rate = float(hist.iloc[0])
                return (1 / rate) if invert else rate
        except:
            continue
    return 1.0

def convert_purchase_to_aud(total_cost, currency, date_str):
    currency = str(currency).strip().upper()
    if currency in ('AUD', 'A$'):
        return total_cost
    elif currency == 'BTC':
        return total_cost * get_hist_fx_rate('BTC', 'AUD', date_str)
    elif currency in ('CAD', 'CA$'):
        return total_cost * get_hist_fx_rate('CAD', 'AUD', date_str)
    elif currency in ('NOK', 'SEK', 'DKK', 'KR', 'KR.'):
        # Try NOK first (most common Revolut Kr), then SEK, DKK
        for kr in ['NOK', 'SEK', 'DKK']:
            rate = get_hist_fx_rate(kr, 'AUD', date_str)
            if rate and rate > 0:
                return total_cost * rate
        return total_cost * 0.14  # fallback: ~0.14 AUD per Kr
    elif currency == 'EUR':
        return total_cost * get_hist_fx_rate('EUR', 'AUD', date_str)
    elif currency == 'USD':
        return total_cost * get_hist_fx_rate('USD', 'AUD', date_str)
    else:
        return total_cost * get_hist_fx_rate(currency, 'AUD', date_str)

# --- 4. INTERFACCIA ---
(tab0, tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9, tab10, tab11) = st.tabs([
    "🌐 Dashboard",
    "📊 N26 Performance",
    "💸 N26 Simulatore ATO",
    "📈 N26 Timeline",
    "💱 N26 FX Analysis",
    "🌱 Raiz & Vanguard",
    "🪙 Commodities",
    "🏛️ Super",
    "🏦 Cash",
    "🛠️ Diagnostics",
    "📈 Forecast",
    "📝 Data Entry"
])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 0 — DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════
with tab0:

    # ── Always pull fresh values ──────────────────────────────────────────────
    # These are already computed at module level from each platform's own cached
    # function. We just assemble the total here — no re-fetching.
    n26_aud     = current_market_value_eur * fx_now
    raiz_aud    = raiz_total_aud
    vdal_aud    = vanguard_total_aud
    shares_aud  = shares_total_aud
    metals_aud  = commodities_total_aud
    super_aud   = super_total_aud
    cash_aud    = cash_total_aud
    total_nw    = n26_aud + raiz_aud + vdal_aud + shares_aud + metals_aud + super_aud + cash_aud
    total_nw_eur = total_nw / fx_now if fx_now else 0

    # ── SECTION 1: Total Net Worth ────────────────────────────────────────────
    st.header("🌐 Net Worth Dashboard")
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Net Worth (AUD)", f"${total_nw:,.2f}")
    c2.metric("Total Net Worth (EUR)", f"€{total_nw_eur:,.2f}")
    c3.metric("EUR/AUD Rate", f"{fx_now:.4f}", help="Live rate from Yahoo Finance")
    st.divider()

    # ── SECTION 2: Platform Breakdown ─────────────────────────────────────────
    st.markdown("### Platform Breakdown")
    p1, p2, p3, p4, p5, p6, p7 = st.columns(7)
    p1.metric("🇪🇺 N26",        f"${n26_aud:,.0f}",   f"€{current_market_value_eur:,.0f}")
    p2.metric("🌱 Raiz",        f"${raiz_aud:,.0f}")
    p3.metric("📈 Vanguard",    f"${vdal_aud:,.0f}")
    p4.metric("🇦🇺 Shares",     f"${shares_aud:,.0f}")
    p5.metric("🪙 Metals",      f"${metals_aud:,.0f}")
    p6.metric("🏛️ Super",       f"${super_aud:,.0f}")
    p7.metric("🏦 Cash",        f"${cash_aud:,.0f}")
    st.divider()

    # ── SECTION 3: Allocation Charts ──────────────────────────────────────────
    st.markdown("### Asset Allocation")
    df_alloc = pd.DataFrame([
        {"Platform": "🇪🇺 N26 European",    "Value": n26_aud},
        {"Platform": "🌱 Raiz ETFs",         "Value": raiz_aud},
        {"Platform": "📈 Vanguard VDAL",     "Value": vdal_aud},
        {"Platform": "🇦🇺 ASX Shares",       "Value": shares_aud},
        {"Platform": "🪙 Precious Metals",   "Value": metals_aud},
        {"Platform": "🏛️ Super",             "Value": super_aud},
        {"Platform": "🏦 Cash & Savings",    "Value": cash_aud},
    ])
    COLOURS = ["#2980b9","#27ae60","#2ecc71","#1abc9c","#f39c12","#8e44ad","#e67e22"]
    col_pie, col_bar = st.columns(2)
    with col_pie:
        fig_pie = px.pie(df_alloc, values="Value", names="Platform", hole=0.45,
                         color_discrete_sequence=COLOURS)
        fig_pie.update_layout(height=350, margin=dict(t=10, b=10))
        st.plotly_chart(fig_pie, use_container_width=True)
    with col_bar:
        fig_bar = px.bar(df_alloc, x="Platform", y="Value", color="Platform",
                         color_discrete_sequence=COLOURS)
        fig_bar.update_layout(height=350, showlegend=False,
                              yaxis_tickprefix="$", margin=dict(t=10, b=10))
        st.plotly_chart(fig_bar, use_container_width=True)
    st.divider()

    # ── SECTION 4: Net Worth History ──────────────────────────────────────────
    st.markdown("### Net Worth History")

    # Auto-save on last day of month (silent, no force)
    _today = date.today()
    import calendar as _cal
    if _today.day == _cal.monthrange(_today.year, _today.month)[1]:
        _ok, _err = save_net_worth_snapshot(total_nw, force=False)
        if _ok:
            st.success(f"✅ Monthly snapshot auto-saved: ${total_nw:,.2f}")

    # Load history — always fresh on this tab
    load_net_worth_history.clear()
    df_hist = load_net_worth_history()

    if df_hist.empty:
        st.info("No snapshots yet. Click 'Save Snapshot' below to start tracking.")
    else:
        # History line chart
        fig_hist = go.Figure()
        fig_hist.add_trace(go.Scatter(
            x=df_hist['Date'], y=df_hist['Total_AUD'],
            mode='lines+markers+text',
            line=dict(color='#2980b9', width=2),
            marker=dict(size=10, color='#2980b9'),
            fill='tozeroy', fillcolor='rgba(41,128,185,0.08)',
            text=[f"${v:,.0f}" for v in df_hist['Total_AUD']],
            textposition='top center',
            textfont=dict(size=11, color='#2980b9'),
            hovertemplate='%{x|%Y-%m-%d}<br><b>$%{y:,.2f}</b><extra></extra>'
        ))
        if len(df_hist) > 1:
            _delta    = df_hist['Total_AUD'].iloc[-1] - df_hist['Total_AUD'].iloc[0]
            _delta_pct = _delta / df_hist['Total_AUD'].iloc[0] * 100
            _col      = "#27ae60" if _delta >= 0 else "#e74c3c"
            _sign     = "+" if _delta >= 0 else ""
            fig_hist.add_annotation(
                x=df_hist['Date'].iloc[-1], y=df_hist['Total_AUD'].iloc[-1],
                text=f"  {_sign}${_delta:,.0f} ({_sign}{_delta_pct:.1f}%) "
                     f"since {df_hist['Date'].iloc[0].strftime('%d %b %Y')}",
                showarrow=False, xanchor='left', yanchor='middle',
                font=dict(size=12, color=_col)
            )
        fig_hist.update_layout(height=380, hovermode="x unified",
                               yaxis=dict(title="AUD $", tickprefix="$"),
                               margin=dict(t=20, b=20, r=220))
        st.plotly_chart(fig_hist, use_container_width=True)

        # Summary stats row
        if len(df_hist) > 1:
            _first = df_hist['Total_AUD'].iloc[0]
            _last  = df_hist['Total_AUD'].iloc[-1]
            _chg   = _last - _first
            s1, s2, s3, s4 = st.columns(4)
            s1.metric("First Recorded",   f"${_first:,.0f}", df_hist['Date'].iloc[0].strftime('%d %b %Y'))
            s2.metric("Latest Snapshot",  f"${_last:,.0f}",  df_hist['Date'].iloc[-1].strftime('%d %b %Y'))
            s3.metric("Total Change",     f"${_chg:+,.0f}",  f"{_chg/_first*100:+.1f}%",
                      delta_color="normal" if _chg >= 0 else "inverse")
            # Cumulative contributions if available
            if 'Contributions_AUD' in df_hist.columns:
                _contrib = df_hist['Contributions_AUD'].sum()
                _gains   = df_hist['Market_Gains_AUD'].sum() if 'Market_Gains_AUD' in df_hist.columns else 0
                s4.metric("Total Contributions", f"${_contrib:+,.0f}",
                          f"Market gains: ${_gains:+,.0f}")

    st.divider()

    # ── SECTION 5: Period Analysis ────────────────────────────────────────────
    st.markdown("### 🔍 Period Analysis — What Drove the Change?")

    if df_hist.empty or len(df_hist) < 2:
        st.info("Save at least two snapshots to enable period analysis.")
    else:
        _min_d = df_hist['Date'].min().date()
        _max_d = df_hist['Date'].max().date()

        # Auto-reset date range when new snapshot detected
        if st.session_state.get('_dash_max_date') != _max_d:
            st.session_state['_dash_max_date']   = _max_d
            st.session_state['_dash_start_date'] = _min_d
            st.session_state['_dash_end_date']   = _max_d

        dc1, dc2, dc3 = st.columns([2, 2, 1])
        with dc1:
            _sel_start = st.date_input("From", min_value=_min_d, max_value=_max_d,
                                       key="_dash_start_date")
        with dc2:
            _sel_end = st.date_input("To", min_value=_min_d, max_value=_max_d,
                                     key="_dash_end_date")
        with dc3:
            st.markdown("<div style='margin-top:26px'>", unsafe_allow_html=True)
            if st.button("🔄 Refresh", key="_dash_refresh"):
                load_net_worth_history.clear()
                st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)

        if _sel_start >= _sel_end:
            st.warning("Start date must be before end date.")
        else:
            analysis = analyze_net_worth_change(df_hist, _sel_start, _sel_end)

            if not analysis:
                st.warning("Not enough data points in this range — try a wider date range.")
            else:
                # Warn if old zero-attribution rows are in range
                if analysis.get('has_zero_rows'):
                    st.warning(
                        "⚠️ Some snapshots in this range have no attribution data "
                        "(saved before tracking was introduced). "
                        "Clear columns C–G and P–T in those rows to remove this warning."
                    )

                # ── 5a. Summary row ───────────────────────────────────────
                st.markdown(
                    f"#### {analysis['start_date'].strftime('%d %b %Y')} → "
                    f"{analysis['end_date'].strftime('%d %b %Y')}  "
                    f"({analysis['days']} days)"
                )
                m1, m2, m3 = st.columns(3)
                m1.metric("Starting Net Worth", f"${analysis['start_value']:,.2f}")
                m2.metric("Ending Net Worth",   f"${analysis['end_value']:,.2f}",
                          delta=f"${analysis['total_change']:+,.0f} "
                                f"({analysis['total_change_pct']:+.2f}%)",
                          delta_color="normal" if analysis['total_change'] >= 0 else "inverse")
                m3.metric("Period", f"{analysis['days']} days",
                          f"{analysis['days']/365:.2f} years")
                st.divider()

                # ── 5b. Attribution tiles ─────────────────────────────────
                st.markdown("#### 📊 Change Attribution")
                st.caption(
                    "These five components sum exactly to the total change. "
                    "Contributions = everything not explained by market moves, "
                    "interest, dividends or FX (i.e. new money deposited)."
                )

                def _tile(icon, label, value, pct):
                    c = "#27ae60" if value >= 0 else "#e74c3c"
                    return (
                        f'<div style="text-align:center;padding:12px 6px;'
                        f'background:#f8f9fa;border-radius:10px;min-height:95px;">'
                        f'<div style="font-size:1.4rem">{icon}</div>'
                        f'<div style="font-size:0.8rem;font-weight:600;margin:2px 0">{label}</div>'
                        f'<div style="font-size:1.1rem;color:{c};font-weight:bold">${value:+,.0f}</div>'
                        f'<div style="font-size:0.7rem;color:#888">{abs(pct):.0f}% of change</div>'
                        f'</div>'
                    )

                t1, t2, t3, t4, t5 = st.columns(5)
                t1.markdown(_tile("📈","Market Gains",    analysis['market_gains'],         analysis['market_pct']),    unsafe_allow_html=True)
                t2.markdown(_tile("💰","Contributions",   analysis['total_contributions'],   analysis['contrib_pct']),   unsafe_allow_html=True)
                t3.markdown(_tile("🏦","Cash Interest",   analysis['total_cash_interest'],   analysis['interest_pct']),  unsafe_allow_html=True)
                t4.markdown(_tile("💸","Dividends",       analysis['total_dividends'],       analysis['dividends_pct']), unsafe_allow_html=True)
                t5.markdown(_tile("💱","FX Impact",       analysis['fx_impact'],             analysis['fx_pct']),        unsafe_allow_html=True)

                # ── 5c. Platform breakdown under Market Gains ─────────────
                with st.expander("📋 Market Gains — by platform (net of FX)"):
                    pg = analysis.get('platform_gains', {})
                    if pg:
                        _rows = [
                            {"Platform": k, "Gain (AUD)": v,
                             "% of Mkt Gain": (v/analysis['market_gains']*100
                                               if analysis['market_gains'] != 0 else 0)}
                            for k, v in sorted(pg.items(), key=lambda x: abs(x[1]), reverse=True)
                        ]
                        _df_pg = pd.DataFrame(_rows)
                        st.dataframe(
                            _df_pg.style
                            .format({"Gain (AUD)": "${:+,.0f}", "% of Mkt Gain": "{:+.1f}%"})
                            .map(lambda v: "color:#27ae60" if isinstance(v,(int,float)) and v>0
                                 else ("color:#e74c3c" if isinstance(v,(int,float)) and v<0 else ""),
                                 subset=["Gain (AUD)","% of Mkt Gain"]),
                            use_container_width=True, hide_index=True
                        )
                        _df_plot = _df_pg[_df_pg["Gain (AUD)"].abs() > 0]
                        if not _df_plot.empty:
                            _fig_pg = px.bar(
                                _df_plot, x="Platform", y="Gain (AUD)",
                                color="Gain (AUD)",
                                color_continuous_scale=["#e74c3c","#95a5a6","#27ae60"],
                                color_continuous_midpoint=0,
                                text=_df_plot["Gain (AUD)"].apply(lambda v: f"${v:+,.0f}")
                            )
                            _fig_pg.update_traces(textposition="outside")
                            _fig_pg.add_hline(y=0, line_dash="dash", line_color="grey", opacity=0.4)
                            _fig_pg.update_layout(height=300, showlegend=False,
                                                  coloraxis_showscale=False,
                                                  yaxis_tickprefix="$",
                                                  margin=dict(t=10,b=10))
                            st.plotly_chart(_fig_pg, use_container_width=True)
                    else:
                        st.caption("Platform breakdown available after next snapshot.")

                # Interest/dividend detail
                with st.expander("📋 Interest & Dividend detail"):
                    _id1, _id2, _id3, _id4 = st.columns(4)
                    _id1.metric("AUD Cash Interest", f"${analysis['aud_cash_interest']:+,.2f}")
                    _id2.metric("EUR Cash Interest", f"${analysis['eur_cash_interest']:+,.2f}")
                    _id3.metric("N26 Dividends",     f"${analysis['n26_dividends']:+,.2f}")
                    _id4.metric("Shares Dividends",  f"${analysis['shares_dividends']:+,.2f}")

                # Cash balance change detail
                with st.expander("💰 Cash Balance Change (AUD vs EUR)"):
                    cb = analysis.get('cash_breakdown', {})
                    if cb:
                        cb1, cb2, cb3 = st.columns(3)
                        cb1.metric("🇦🇺 AUD Cash", f"${cb['aud_cash_end']:,.0f}",
                                   delta=f"${cb['aud_cash_change']:+,.0f}",
                                   delta_color="normal" if cb['aud_cash_change'] >= 0 else "inverse",
                                   help=f"Was ${cb['aud_cash_start']:,.0f}")
                        cb2.metric("🇪🇺 EUR Cash (AUD equiv)", f"${cb['eur_cash_end']:,.0f}",
                                   delta=f"${cb['eur_cash_change']:+,.0f}",
                                   delta_color="normal" if cb['eur_cash_change'] >= 0 else "inverse",
                                   help=f"Was ${cb['eur_cash_start']:,.0f}")
                        cb3.metric("Total Cash", f"${cb['total_cash_end']:,.0f}",
                                   delta=f"${cb['total_cash_change']:+,.0f}",
                                   delta_color="normal" if cb['total_cash_change'] >= 0 else "inverse",
                                   help=f"Was ${cb['total_cash_start']:,.0f}")
                        st.caption(
                            "This is the raw cash balance movement — includes interest earned, "
                            "money added or withdrawn, and money moved into investments (e.g. "
                            "EUR cash → N26 buys)."
                        )
                        if cb.get('movement_notes'):
                            st.markdown("**Recorded transfers into investments during this period:**")
                            for note in cb['movement_notes']:
                                st.write(f"• {note}")
                        else:
                            st.caption("No recorded platform transfers in this period.")
                    else:
                        st.caption("Cash breakdown available after next snapshot.")

                st.divider()

                # ── 5d. Waterfall chart ───────────────────────────────────
                st.markdown("#### 📊 Visual Breakdown")
                _wf_components = [
                    ("Market Gains",    analysis['market_gains']),
                    ("Contributions",   analysis['total_contributions']),
                    ("Cash Interest",   analysis['total_cash_interest']),
                    ("Dividends",       analysis['total_dividends']),
                    ("FX Impact",       analysis['fx_impact']),
                ]
                _wf_labels  = ["Start"] + [c[0] for c in _wf_components] + ["End"]
                _wf_values  = ([analysis['start_value']]
                               + [c[1] for c in _wf_components]
                               + [analysis['end_value']])
                _wf_measure = ["absolute"] + ["relative"]*len(_wf_components) + ["total"]
                _fig_wf = go.Figure(go.Waterfall(
                    orientation="v",
                    measure=_wf_measure,
                    x=_wf_labels,
                    y=_wf_values,
                    text=[f"${v:,.0f}" for v in _wf_values],
                    textposition="outside",
                    connector=dict(line=dict(color="rgba(150,150,150,0.3)", width=1, dash="dot")),
                    increasing=dict(marker_color="#27ae60"),
                    decreasing=dict(marker_color="#e74c3c"),
                    totals=dict(marker_color="#2980b9"),
                ))
                _fig_wf.update_layout(
                    title=(f"${analysis['start_value']:,.0f} → ${analysis['end_value']:,.0f}  "
                           f"({'+'if analysis['total_change']>=0 else ''}"
                           f"{analysis['total_change']:,.0f})"),
                    yaxis=dict(title="AUD $", tickprefix="$"),
                    height=420, showlegend=False,
                    margin=dict(t=50, b=20)
                )
                st.plotly_chart(_fig_wf, use_container_width=True)

                # ── 5e. Cumulative stacked view ───────────────────────────
                st.markdown("#### 📈 Cumulative Change Over Period")
                _mask_p = ((df_hist['Date'].dt.date >= _sel_start) &
                           (df_hist['Date'].dt.date <= _sel_end))
                _df_p = df_hist[_mask_p].sort_values('Date').copy()
                if len(_df_p) > 1 and 'Contributions_AUD' in _df_p.columns:
                    _df_p['Cum_Contributions'] = _df_p['Contributions_AUD'].cumsum()
                    _df_p['Cum_Market']        = _df_p['Market_Gains_AUD'].cumsum() if 'Market_Gains_AUD' in _df_p.columns else 0
                    _df_p['Cum_Interest']      = (_df_p.get('AUD_Cash_Interest_AUD', 0) + _df_p.get('EUR_Cash_Interest_AUD', 0)).cumsum()
                    _fig_cum = go.Figure()
                    _base = float(_df_p.iloc[0]['Total_AUD'])
                    _fig_cum.add_trace(go.Scatter(
                        x=_df_p['Date'],
                        y=_base + _df_p['Cum_Contributions'],
                        name='Base + Contributions', mode='lines',
                        line=dict(color='#27ae60', width=1.5),
                        fill=None,
                    ))
                    _fig_cum.add_trace(go.Scatter(
                        x=_df_p['Date'], y=_df_p['Total_AUD'],
                        name='Actual Net Worth', mode='lines',
                        line=dict(color='#2980b9', width=2.5),
                        fill='tonexty', fillcolor='rgba(41,128,185,0.15)',
                        hovertemplate='Actual: $%{y:,.0f}<extra></extra>'
                    ))
                    _fig_cum.update_layout(
                        height=320, hovermode='x unified',
                        yaxis=dict(tickprefix="$"),
                        legend=dict(orientation="h", y=1.05),
                        margin=dict(t=30, b=10)
                    )
                    st.plotly_chart(_fig_cum, use_container_width=True)

                # ── 5f. Best / Worst months ───────────────────────────────
                if len(df_hist) > 2:
                    st.markdown("#### 🏆 Best & Worst Months (all history)")
                    _df_hw = df_hist.copy()
                    _df_hw['Monthly_Change'] = _df_hw['Total_AUD'].diff()
                    _df_hw['Monthly_Pct']    = _df_hw['Total_AUD'].pct_change() * 100
                    _bm = _df_hw.loc[_df_hw['Monthly_Change'].idxmax()]
                    _wm = _df_hw.loc[_df_hw['Monthly_Change'].idxmin()]
                    _hw1, _hw2 = st.columns(2)
                    with _hw1:
                        st.metric("📈 Best Month",
                                  f"${_bm['Monthly_Change']:+,.0f}",
                                  f"{_bm['Monthly_Pct']:+.1f}%",
                                  help=f"Month ending {_bm['Date'].strftime('%b %Y')}")
                    with _hw2:
                        st.metric("📉 Worst Month",
                                  f"${_wm['Monthly_Change']:+,.0f}",
                                  f"{_wm['Monthly_Pct']:+.1f}%",
                                  help=f"Month ending {_wm['Date'].strftime('%b %Y')}")

    st.divider()

    # ── SECTION 6: Save Snapshot ──────────────────────────────────────────────
    _sc1, _sc2 = st.columns([1, 3])
    with _sc1:
        if st.button("💾 Save Snapshot Now", type="primary", key="dash_save_btn"):
            _ok, _err = save_net_worth_snapshot(total_nw, force=True)
            if _ok:
                load_net_worth_history.clear()
                st.rerun()
            else:
                st.error(f"Save failed: {_err}")
    with _sc2:
        st.caption(
            f"Saves current values: N26 ${n26_aud:,.0f} · "
            f"Raiz ${raiz_aud:,.0f} · Vanguard ${vdal_aud:,.0f} · "
            f"Shares ${shares_aud:,.0f} · Metals ${metals_aud:,.0f} · "
            f"Super ${super_aud:,.0f} · Cash ${cash_aud:,.0f} · "
            f"**Total ${total_nw:,.0f}**"
        )

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — N26 PERFORMANCE
# ══════════════════════════════════════════════════════════════════════════════
with tab1:
    st.header("📊 N26 European Portfolio — Performance")
    df_realized = df_perf[df_perf['Current_Value'] < 0.01].copy()
    df_unrealized = df_perf[df_perf['Current_Value'] >= 0.01].copy()
    active_isins = df_unrealized['ISIN'].tolist()
    df_active_ledger = df_raw[df_raw['ISIN'].isin(active_isins)]
    active_inv_eur = df_active_ledger[df_active_ledger['Tipo'] == 'BUY']['Inv_EUR'].sum()
    active_inv_aud = df_active_ledger[df_active_ledger['Tipo'] == 'BUY']['Inv_AUD'].sum()
    curr_val_aud = current_market_value_eur * fx_now
    col_a, col_b = st.columns(2)
    col_a.metric("Profitto Totale EUR", f"€{df_perf['Profit_EUR'].sum():,.0f}", f"Incassato: €{df_realized['Profit_EUR'].sum():,.0f}")
    col_b.metric("Profitto Totale AUD", f"${df_perf['Profit_AUD'].sum():,.0f}", f"Incassato: ${df_realized['Profit_AUD'].sum():,.0f}")
    st.divider()
    st.subheader("Analisi Portafoglio Attivo")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.write("**Esposizione EUR**")
        st.metric("Investito", f"€{active_inv_eur:,.0f}")
        st.metric("Valore", f"€{current_market_value_eur:,.0f}")
        diff_eur = current_market_value_eur - active_inv_eur
        st.write(f"**Plusvalenza: €{diff_eur:,.2f}**")
    with c2:
        st.write("**Esposizione AUD**")
        st.metric("Investito", f"${active_inv_aud:,.0f}")
        st.metric("Valore", f"${curr_val_aud:,.0f}")
        diff_aud = curr_val_aud - active_inv_aud
        st.write(f"**Plusvalenza: ${diff_aud:,.2f}**")
    with c3:
        st.write("**Rendimento % (ROI)**")
        roi_eur = (df_unrealized['Profit_EUR'].sum() / active_inv_eur * 100) if active_inv_eur != 0 else 0
        roi_aud = (df_unrealized['Profit_AUD'].sum() / active_inv_aud * 100) if active_inv_aud != 0 else 0
        st.metric("ROI Attivo (EUR)", f"{roi_eur:.2f}%")
        st.metric("ROI Attivo (AUD)", f"{roi_aud:.2f}%")
    st.divider()
    st.subheader("Storico Operazioni di Vendita")
    df_vendite = df_raw[df_raw['Tipo'].str.upper() == 'SELL'].copy()
    if not df_vendite.empty:
        def get_asset_history(row):
            isin = row['ISIN']
            buys = df_raw[(df_raw['ISIN'] == isin) & (df_raw['Tipo'].str.upper() == 'BUY')]
            data_acq_val = buys['Data'].min() if not buys.empty else None
            total_bought = buys['Qty'].sum() if not buys.empty else 0
            total_inv_eur_buy = buys['Inv_EUR'].sum() if not buys.empty else 0
            total_inv_aud_buy = buys['Inv_AUD'].sum() if not buys.empty else 0
            pmc_eur = total_inv_eur_buy / total_bought if total_bought != 0 else 0
            avg_fx_buy = total_inv_aud_buy / total_inv_eur_buy if total_inv_eur_buy != 0 else 0
            inv_eur_sell = abs(row['Inv_EUR'])
            inv_aud_sell = abs(row['Inv_AUD'])
            qty_sold = abs(row['Qty'])
            prezzo_vend_unit = inv_eur_sell / qty_sold if qty_sold != 0 else 0
            fx_sell_val = inv_aud_sell / inv_eur_sell if inv_eur_sell != 0 else 0
            return pd.Series({
                'Data Acquisto': data_acq_val, 'Tot_Qty_Acquistata': total_bought,
                'Prezzo_Acquisto_PMC': pmc_eur, 'Valore_Acquisto_Tot_EUR': total_inv_eur_buy,
                'FX_Acquisto_Medio': avg_fx_buy, 'Prezzo_Vendita_Unitario': prezzo_vend_unit,
                'Valore_Vendita_EUR': inv_eur_sell, 'FX_Vendita': fx_sell_val
            })
        res = df_vendite.apply(get_asset_history, axis=1)
        df_vendite = pd.concat([df_vendite, res], axis=1)
        df_vendite = df_vendite.rename(columns={'Data': 'Data Vendita'})
        cols_to_show = ['ISIN', 'Data Acquisto', 'Data Vendita', 'Qty', 'Tot_Qty_Acquistata',
                        'Prezzo_Acquisto_PMC', 'Valore_Acquisto_Tot_EUR', 'FX_Acquisto_Medio',
                        'Prezzo_Vendita_Unitario', 'Valore_Vendita_EUR', 'FX_Vendita', 'Profit_EUR', 'Profit_AUD']
        final_view = [c for c in cols_to_show if c in df_vendite.columns]
        st.dataframe(df_vendite[final_view].style.format({
            'Data Acquisto': lambda x: x.strftime('%Y-%m-%d') if pd.notnull(x) else "-",
            'Data Vendita': lambda x: x.strftime('%Y-%m-%d') if pd.notnull(x) else "-",
            'Qty': '{:.2f}', 'Tot_Qty_Acquistata': '{:.2f}',
            'Prezzo_Acquisto_PMC': '€{:.4f}', 'Valore_Acquisto_Tot_EUR': '€{:,.2f}',
            'FX_Acquisto_Medio': '{:.4f}', 'Prezzo_Vendita_Unitario': '€{:.4f}',
            'Valore_Vendita_EUR': '€{:,.2f}', 'FX_Vendita': '{:.4f}',
            'Profit_EUR': '€{:,.2f}', 'Profit_AUD': '${:,.2f}'
        }), use_container_width=True)
    else:
        st.info("Nessuna vendita registrata.")
    st.divider()
    col_left, col_right = st.columns(2)
    with col_left:
        st.subheader("Allocation % (Solo Attivi)")
        fig_pie = px.pie(df_unrealized, values='Current_Value', names='ISIN', hole=0.4)
        st.plotly_chart(fig_pie, use_container_width=True)
    with col_right:
        st.subheader("Profitto per Asset (Inclusi Chiusi)")
        fig_bar = px.bar(df_perf, x='ISIN', y=['Profit_EUR', 'Profit_AUD'], barmode='group',
                         labels={'value': 'Profitto (€/$)', 'variable': 'Valuta'},
                         color_discrete_map={'Profit_EUR': '#1f77b4', 'Profit_AUD': '#2ca02c'})
        st.plotly_chart(fig_bar, use_container_width=True)
    if not df_realized.empty:
        with st.expander("Visualizza Dettaglio Posizioni Chiuse"):
            st.dataframe(df_realized[['ISIN', 'Profit_EUR', 'Profit_AUD']].style.format(
                {'Profit_EUR': '€{:,.2f}', 'Profit_AUD': '${:,.2f}'}),
                hide_index=True, use_container_width=True)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — N26 SIMULATORE ATO
# ══════════════════════════════════════════════════════════════════════════════
with tab2:
    st.header("💸 N26 — Simulatore Cash-out & Tasse ATO")
    tax_brackets = {
        "0% (fino a AUD 18,200)": 0.0, "16% (AUD 18,201 – 45,000)": 16.0,
        "30% (AUD 45,001 – 135,000)": 30.0, "37% (AUD 135,001 – 190,000)": 37.0,
        "45% (oltre AUD 190,000)": 45.0
    }
    selected_bracket = st.select_slider("Marginal Tax Rate", options=list(tax_brackets.keys()), value="37% (AUD 135,001 – 190,000)")
    tax_r = tax_brackets[selected_bracket]
    st.info("L'impatto fiscale ATO è calcolato sulla plusvalenza del singolo lotto in AUD.")
    lotti_aperti = []
    for isin in df_raw['ISIN'].unique():
        asset_ledger = df_raw[df_raw['ISIN'] == isin].sort_values('Data').copy()
        h = hist_map.get(isin)
        p_now = float(h.iloc[-1]) if (h is not None and not h.empty) else asset_ledger['Prezzo_Acq'].iloc[0]
        manual = asset_ledger['Manual_Price'].iloc[-1]
        if pd.notnull(manual) and manual > 0:
            p_now = manual
        buys = asset_ledger[asset_ledger['Tipo'] == 'BUY'].copy()
        total_sold = abs(asset_ledger[asset_ledger['Tipo'] == 'SELL']['Qty'].sum())
        for idx, buy_row in buys.iterrows():
            qty_iniziale = buy_row['Qty']
            if total_sold > 0:
                if total_sold >= qty_iniziale:
                    total_sold -= qty_iniziale
                    qty_residua = 0.0
                else:
                    qty_residua = qty_iniziale - total_sold
                    total_sold = 0.0
            else:
                qty_residua = qty_iniziale
            if qty_residua > 0.001:
                quota_lotto = qty_residua / qty_iniziale
                inv_eur_residual = buy_row['Inv_EUR'] * quota_lotto
                inv_aud_residual = buy_row['Inv_AUD'] * quota_lotto
                att_eur_val = qty_residua * p_now
                att_aud_val = att_eur_val * fx_now
                var_eur = ((att_eur_val / inv_eur_residual) - 1) * 100 if inv_eur_residual > 0 else 0
                var_aud = ((att_aud_val / inv_aud_residual) - 1) * 100 if inv_aud_residual > 0 else 0
                gain_eur = att_eur_val - inv_eur_residual
                gain_aud = att_aud_val - inv_aud_residual
                alert_status = "⚠️ FX LOSS" if (gain_eur > 0 and gain_aud < 0) else "✅ OK"
                giorni_possesso = (datetime.now().date() - buy_row['Data'].date()).days
                cgt_discount = "50% Disc" if giorni_possesso >= 365 else "No Disc"
                lotti_aperti.append({
                    'ISIN': isin, 'Data Acquisto': buy_row['Data'].strftime('%Y-%m-%d'),
                    'Stato': alert_status, 'CGT': cgt_discount, 'Qty Residua': qty_residua,
                    'Prezzo Acq (€)': buy_row['Prezzo_Acq'], 'Inv EUR (€)': inv_eur_residual,
                    'Att EUR (€)': att_eur_val, 'Var % EUR': var_eur, 'Inv AUD ($)': inv_aud_residual,
                    'Att AUD ($)': att_aud_val, 'Var % AUD': var_aud, '% Vendi': 0.0
                })
    df_sim_lotti = pd.DataFrame(lotti_aperti)
    if not df_sim_lotti.empty:
        tot_inv_eur = df_sim_lotti['Inv EUR (€)'].sum()
        tot_att_eur = df_sim_lotti['Att EUR (€)'].sum()
        tot_inv_aud = df_sim_lotti['Inv AUD ($)'].sum()
        tot_att_aud = df_sim_lotti['Att AUD ($)'].sum()
        st.markdown("### 📊 Stato Attuale del Portafoglio Aperto")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Capitale Investito (EUR)", f"€{tot_inv_eur:,.2f}")
        m2.metric("Valore Attuale (EUR)", f"€{tot_att_eur:,.2f}", f"€{(tot_att_eur - tot_inv_eur):+,.2f}")
        m3.metric("Capitale Investito (AUD)", f"AUD {tot_inv_aud:,.2f}")
        m4.metric("Valore Attuale (AUD)", f"AUD {tot_att_aud:,.2f}", f"AUD {(tot_att_aud - tot_inv_aud):+,.2f}")
        st.divider()
    column_config = {
        "ISIN": st.column_config.TextColumn("ISIN", width="medium"),
        "Data Acquisto": st.column_config.TextColumn("Data Acquisto", width="small"),
        "Stato": st.column_config.TextColumn("Stato", width="small"),
        "CGT": st.column_config.TextColumn("ATO CGT", width="small"),
        "Qty Residua": st.column_config.NumberColumn("Qty Disp.", format="%.2f"),
        "Prezzo Acq (€)": st.column_config.NumberColumn("Prezzo Acq", format="€%.2f"),
        "Inv EUR (€)": st.column_config.NumberColumn("Costo Base Lot (€)", format="€%.2f"),
        "Att EUR (€)": st.column_config.NumberColumn("Val. Corrente (€)", format="€%.2f"),
        "Var % EUR": st.column_config.NumberColumn("Var % (€)", format="%.2f%%"),
        "Inv AUD ($)": st.column_config.NumberColumn("Costo Base Lot (AUD)", format="$%.2f"),
        "Att AUD ($)": st.column_config.NumberColumn("Val. Corrente (AUD)", format="$%.2f"),
        "Var % AUD": st.column_config.NumberColumn("Var % (AUD)", format="%.2f%%"),
        "% Vendi": st.column_config.NumberColumn("% Vendi", min_value=0, max_value=100, step=5, format="%d%%")
    }
    display_cols = ['ISIN', 'Data Acquisto', 'Stato', 'CGT', 'Qty Residua', 'Prezzo Acq (€)',
                    'Inv EUR (€)', 'Att EUR (€)', 'Var % EUR', 'Inv AUD ($)', 'Att AUD ($)', 'Var % AUD', '% Vendi']
    ed = st.data_editor(df_sim_lotti[display_cols], column_config=column_config, hide_index=True, use_container_width=True)
    sel = ed[ed['% Vendi'] > 0].copy()
    if not sel.empty:
        sel['E_Out'] = sel['Att EUR (€)'] * (sel['% Vendi']/100)
        sel['A_Out'] = sel['Att AUD ($)'] * (sel['% Vendi']/100)
        sel['Lotto_Gain_AUD'] = (sel['Att AUD ($)'] - sel['Inv AUD ($)']) * (sel['% Vendi']/100)
        def calcola_gain_tassabile(row):
            if row['Lotto_Gain_AUD'] > 0 and row['CGT'] == "50% Disc":
                return row['Lotto_Gain_AUD'] * 0.5
            return row['Lotto_Gain_AUD']
        sel['Lotto_Gain_Tassabile_AUD'] = sel.apply(calcola_gain_tassabile, axis=1)
        total_realized_gain_aud = sel['Lotto_Gain_AUD'].sum()
        total_taxable_gain_aud = sel['Lotto_Gain_Tassabile_AUD'].sum()
        stima_tassa = max(0, total_taxable_gain_aud * (tax_r/100))
        st.divider()
        st.subheader("Riepilogo Simulazione di Vendita Selettiva (Lotti)")
        r1, r2, r3, r4, r5 = st.columns(5)
        r1.metric("Cash out EUR", f"€{sel['E_Out'].sum():,.2f}")
        r2.metric("Cash out AUD", f"AUD {sel['A_Out'].sum():,.2f}")
        if total_realized_gain_aud < 0:
            benefit = abs(total_realized_gain_aud * (tax_r/100))
            r3.metric("Minusvalenza Totale AUD", f"-AUD {abs(total_realized_gain_aud):,.2f}", delta="Deducibile")
            r4.metric("Tax Saving Stimato", f"AUD {benefit:,.2f}")
            r5.metric("Netto in Tasca AUD", f"AUD {sel['A_Out'].sum():,.2f}")
        else:
            sconto_applicato = total_realized_gain_aud - total_taxable_gain_aud
            r3.metric("Tasse Stimate (Con Sconto)", f"-AUD {stima_tassa:,.2f}",
                      delta="Sconto CGT 50% applicato" if sconto_applicato > 0 else None, delta_color="inverse")
            r4.metric("Netto Stimato (Post-Tax)", f"AUD {(sel['A_Out'].sum() - stima_tassa):,.2f}")
            r5.metric("Plusvalenza Lorda AUD", f"AUD {total_realized_gain_aud:,.2f}")
    else:
        st.write("⬆️ Inserisci una percentuale nella colonna '% Vendi' per valutare l'impatto fiscale.")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — N26 TIMELINE
# ══════════════════════════════════════════════════════════════════════════════
with tab3:
    st.header("📈 N26 — Evoluzione Reale del Portafoglio (Market Value)")
    date_range = pd.date_range(date(2025, 10, 1), date.today())
    df_raw['Data_Solo'] = df_raw['Data'].dt.date
    all_isins = df_raw['ISIN'].unique()
    daily_data = []
    for d in date_range:
        current_date = d.date()
        snapshot = df_raw[df_raw['Data_Solo'] <= current_date].groupby('ISIN')['Qty'].sum()
        day_total = 0
        for isin in all_isins:
            qty = snapshot.get(isin, 0)
            if abs(qty) < 0.001:
                daily_data.append({'Date': d, 'ISIN': isin, 'MarketValue': 0.0, 'TotalDay': 0.0})
                continue
            h = hist_map.get(isin)
            p_hist = None
            if h is not None and not h.empty:
                try: p_hist = h.asof(d)
                except: p_hist = None
            if p_hist is None or pd.isna(p_hist) or p_hist == 0:
                ledger_price = df_raw[df_raw['ISIN'] == isin]['Prezzo_Acq'].dropna()
                p_hist = ledger_price.iloc[0] if not ledger_price.empty else 0
            valore_asset = float(qty * p_hist)
            day_total += valore_asset
            daily_data.append({'Date': d, 'ISIN': isin, 'MarketValue': valore_asset, 'TotalDay': 0.0})
        for item in daily_data:
            if item['Date'] == d:
                item['TotalDay'] = day_total
    df_timeline = pd.DataFrame(daily_data)
    if not df_timeline.empty:
        fig_timeline = px.area(df_timeline, x='Date', y='MarketValue', color='ISIN',
                               title="Evoluzione Capitale (€) - Storico Completo", custom_data=['TotalDay'])
        fig_timeline.update_layout(
            hovermode="x unified", hoverlabel=dict(namelength=-1, bgcolor="white", font_size=12),
            height=600, yaxis_title="Valore Mercato (€)", xaxis_title="Timeline",
            legend_title="Asset (ISIN)", hoverdistance=100, spikedistance=1000)
        fig_timeline.update_traces(hovertemplate="€%{y:,.2f}<extra></extra>")
        st.plotly_chart(fig_timeline, use_container_width=True)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — N26 FX ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════
with tab4:
    st.header("💱 N26 — FX Impact Analysis — AUD/EUR")
    st.markdown("### EUR/AUD Exchange Rate (Oct 2025 → Today)")
    if fx_hist is not None and not fx_hist.empty:
        fx_display = fx_hist[fx_hist.index >= "2025-10-01"].copy()
        fx_display.index = pd.to_datetime(fx_display.index)
        fig_fx = go.Figure()
        fig_fx.add_trace(go.Scatter(x=fx_display.index, y=fx_display.values, mode='lines', name='EUR/AUD',
                                    line=dict(color='#f39c12', width=2), fill='tozeroy', fillcolor='rgba(243,156,18,0.10)'))
        fig_fx.add_hline(y=fx_now, line_dash="dash", line_color="red",
                         annotation_text=f"Today: {fx_now:.4f}", annotation_position="bottom right")
        fig_fx.update_layout(height=350, yaxis_title="AUD per 1 EUR", xaxis_title="Date",
                             hovermode="x unified", margin=dict(t=30, b=30))
        st.plotly_chart(fig_fx, use_container_width=True)
    else:
        st.warning("FX history not available.")
    st.divider()

    st.markdown("### Portfolio Value: EUR vs AUD (Oct 2025 → Today)")
    date_range_fx = pd.date_range("2025-10-01", date.today())
    df_raw['Data_Solo'] = df_raw['Data'].dt.date
    fx_timeline_rows = []
    for d in date_range_fx:
        current_date = d.date()
        snapshot = df_raw[df_raw['Data_Solo'] <= current_date].groupby('ISIN')['Qty'].sum()
        day_val_eur = 0.0
        for isin in df_raw['ISIN'].unique():
            qty = snapshot.get(isin, 0)
            if abs(qty) < 0.001: continue
            h = hist_map.get(isin)
            p = None
            if h is not None and not h.empty:
                try: p = h.asof(d)
                except: p = None
            if p is None or pd.isna(p) or p == 0:
                ledger_price = df_raw[df_raw['ISIN'] == isin]['Prezzo_Acq'].dropna()
                p = ledger_price.iloc[0] if not ledger_price.empty else 0
            day_val_eur += float(qty * p)
        fx_day = None
        if fx_hist is not None and not fx_hist.empty:
            try:
                fx_day = float(fx_hist.asof(d))
            except: fx_day = None
        if fx_day is None or pd.isna(fx_day) or fx_day == 0:
            fx_day = fx_now
        fx_timeline_rows.append({'Date': d, 'Value_EUR': day_val_eur, 'Value_AUD': day_val_eur * fx_day, 'FX_Rate': fx_day})
    df_fx_timeline = pd.DataFrame(fx_timeline_rows)
    if not df_fx_timeline.empty:
        fig_dual = go.Figure()
        fig_dual.add_trace(go.Scatter(x=df_fx_timeline['Date'], y=df_fx_timeline['Value_EUR'],
                                      mode='lines', name='Value (EUR €)', line=dict(color='#2980b9', width=2), yaxis='y1'))
        fig_dual.add_trace(go.Scatter(x=df_fx_timeline['Date'], y=df_fx_timeline['Value_AUD'],
                                      mode='lines', name='Value (AUD $)', line=dict(color='#27ae60', width=2, dash='dot'), yaxis='y2'))
        fig_dual.update_layout(height=400, hovermode="x unified",
                               yaxis=dict(title="EUR €", tickprefix="€", side='left'),
                               yaxis2=dict(title="AUD $", tickprefix="$", side='right', overlaying='y'),
                               legend=dict(orientation="h", y=1.08), margin=dict(t=40, b=30))
        st.plotly_chart(fig_dual, use_container_width=True)
        st.divider()

    st.markdown("### Market Return Over Time: EUR vs AUD")
    st.caption("Daily unrealised + realised gain/loss.")
    total_inv_eur_all = df_raw[df_raw['Tipo'] == 'BUY']['Inv_EUR'].sum()
    total_inv_aud_all = df_raw[df_raw['Tipo'] == 'BUY']['Inv_AUD'].sum()
    mr_rows = []
    for d in date_range_fx:
        current_date = d.date()
        fx_day = fx_now
        if fx_hist is not None and not fx_hist.empty:
            try:
                v = fx_hist.asof(d)
                if v and not pd.isna(v): fx_day = float(v)
            except: pass
        day_mr_eur = 0.0
        day_mr_aud = 0.0
        day_fx_impact = 0.0
        for isin in df_raw['ISIN'].unique():
            asset_ledger = df_raw[df_raw['ISIN'] == isin].sort_values('Data')
            ledger_to_date = asset_ledger[asset_ledger['Data'].dt.date <= current_date]
            if ledger_to_date.empty: continue
            buys_to_date = ledger_to_date[ledger_to_date['Tipo'] == 'BUY']
            sells_to_date = ledger_to_date[ledger_to_date['Tipo'] == 'SELL']
            net_qty = ledger_to_date['Qty'].sum()
            is_closed = abs(net_qty) < 0.001
            h = hist_map.get(isin)
            p_today = None
            if h is not None and not h.empty:
                try: p_today = h.asof(d)
                except: p_today = None
            if p_today is None or pd.isna(p_today) or p_today == 0:
                ledger_price = df_raw[df_raw['ISIN'] == isin]['Prezzo_Acq'].dropna()
                p_today = float(ledger_price.iloc[0]) if not ledger_price.empty else 0
            total_sold_fifo = abs(sells_to_date['Qty'].sum()) if not sells_to_date.empty else 0.0
            for _, buy_row in buys_to_date.iterrows():
                qty_ini = buy_row['Qty']
                if total_sold_fifo > 0:
                    if total_sold_fifo >= qty_ini:
                        total_sold_fifo -= qty_ini
                        qty_res = 0.0
                    else:
                        qty_res = qty_ini - total_sold_fifo
                        total_sold_fifo = 0.0
                else:
                    qty_res = qty_ini
                qty_for_calc = qty_ini if is_closed else qty_res
                if qty_for_calc < 0.001: continue
                quota = qty_for_calc / qty_ini
                cost_eur = buy_row['Inv_EUR'] * quota
                cost_aud = buy_row['Inv_AUD'] * quota
                fx_at_purchase = cost_aud / cost_eur if cost_eur > 0 else fx_day
                if is_closed:
                    total_buy_qty = buys_to_date['Qty'].sum()
                    lot_share = qty_for_calc / total_buy_qty
                    proceeds_eur = abs(sells_to_date['Inv_EUR'].sum()) * lot_share
                    proceeds_aud = abs(sells_to_date['Inv_AUD'].sum()) * lot_share
                    val_eur_today = proceeds_eur
                    val_aud_today = proceeds_aud
                else:
                    val_eur_today = qty_for_calc * float(p_today)
                    val_aud_today = val_eur_today * fx_day
                lot_mr_eur = val_eur_today - cost_eur
                lot_mr_aud = val_aud_today - cost_aud
                lot_fx_impact = val_eur_today * (fx_day - fx_at_purchase)
                day_mr_eur += lot_mr_eur
                day_mr_aud += lot_mr_aud
                day_fx_impact += lot_fx_impact
        mr_rows.append({'Date': d, 'Market Return (EUR)': day_mr_eur, 'Market Return (AUD)': day_mr_aud,
                        'FX Impact (AUD)': day_fx_impact, 'FX Rate': fx_day})
    df_mr_timeline = pd.DataFrame(mr_rows)
    if not df_mr_timeline.empty:
        fig_mr = go.Figure()
        fig_mr.add_trace(go.Scatter(x=df_mr_timeline['Date'], y=df_mr_timeline['Market Return (EUR)'],
                                    mode='lines', name='Market Return (EUR €)', line=dict(color='#2980b9', width=2), yaxis='y1'))
        fig_mr.add_trace(go.Scatter(x=df_mr_timeline['Date'], y=df_mr_timeline['Market Return (AUD)'],
                                    mode='lines', name='Market Return (AUD $)', line=dict(color='#27ae60', width=2, dash='dot'), yaxis='y2'))
        fig_mr.add_trace(go.Scatter(x=df_mr_timeline['Date'], y=df_mr_timeline['FX Impact (AUD)'],
                                    mode='lines', name='FX Impact (AUD $)', line=dict(color='#e74c3c', width=1.5, dash='dashdot'),
                                    fill='tozeroy', fillcolor='rgba(231,76,60,0.07)', yaxis='y2'))
        fig_mr.add_hline(y=0, line_dash="dash", line_color="grey", opacity=0.4, yref='y1')
        fig_mr.update_layout(
            height=420, hovermode="x unified",
            yaxis=dict(title="EUR € gain/loss", side='left', zeroline=True, zerolinecolor='#bdc3c7'),
            yaxis2=dict(title="AUD $ gain/loss", side='right', overlaying='y', scaleanchor='y', scaleratio=1,
                        zeroline=True, zerolinecolor='#bdc3c7', showticklabels=False),
            legend=dict(orientation="h", y=1.08), margin=dict(t=40, b=30))
        st.plotly_chart(fig_mr, use_container_width=True)
        last = df_mr_timeline.iloc[-1]
        gap = last['FX Impact (AUD)']
        gap_colour = "#27ae60" if gap >= 0 else "#e74c3c"
        gap_label = "added" if gap >= 0 else "subtracted"
        st.markdown(f"""
            <div style="background:#f8f9fa; border-left:4px solid #7f8c8d;
                        padding:10px 16px; border-radius:4px; font-size:0.9rem; margin-top:4px;">
                <b>Today's FX gap:</b> Converting your current EUR return at today's rate gives
                <b>€{last['Market Return (EUR)']:,.2f} × {fx_now:.4f} = ${last['Market Return (EUR)'] * fx_now:,.2f} AUD</b> —
                the AUD return line sits at <b>${last['Market Return (AUD)']:,.2f}</b>,
                meaning historical FX movements have <span style="color:{gap_colour}"><b>{gap_label} ${abs(gap):,.2f} AUD</b></span>
                to your return over the period.
            </div>""", unsafe_allow_html=True)
    st.divider()

    st.markdown("### Portfolio FX Impact Decomposition (Per Lot)")
    fx_decomp_rows = []
    for isin in df_raw['ISIN'].unique():
        asset_ledger = df_raw[df_raw['ISIN'] == isin].sort_values('Data').copy()
        h = hist_map.get(isin)
        p_now_fx = float(h.iloc[-1]) if (h is not None and not h.empty) else asset_ledger['Prezzo_Acq'].iloc[0]
        manual = asset_ledger['Manual_Price'].iloc[-1]
        if pd.notnull(manual) and manual > 0:
            p_now_fx = manual
        buys = asset_ledger[asset_ledger['Tipo'] == 'BUY'].copy()
        sells = asset_ledger[asset_ledger['Tipo'] == 'SELL'].copy()
        net_qty = asset_ledger['Qty'].sum()
        is_closed = abs(net_qty) < 0.001
        total_sold_fifo = abs(sells['Qty'].sum())
        for _, buy_row in buys.iterrows():
            qty_ini = buy_row['Qty']
            if total_sold_fifo > 0:
                if total_sold_fifo >= qty_ini:
                    total_sold_fifo -= qty_ini
                    qty_res = 0.0
                else:
                    qty_res = qty_ini - total_sold_fifo
                    total_sold_fifo = 0.0
            else:
                qty_res = qty_ini
            qty_for_calc = qty_ini if is_closed else qty_res
            if qty_for_calc < 0.001: continue
            quota = qty_for_calc / qty_ini
            cost_eur = buy_row['Inv_EUR'] * quota
            cost_aud = buy_row['Inv_AUD'] * quota
            fx_buy = cost_aud / cost_eur if cost_eur > 0 else fx_now
            if is_closed:
                total_buy_qty = buys['Qty'].sum()
                lot_share = qty_for_calc / total_buy_qty
                proceeds_eur = abs(sells['Inv_EUR'].sum()) * lot_share
                proceeds_aud = abs(sells['Inv_AUD'].sum()) * lot_share
                val_eur_now = proceeds_eur
                val_aud_now = proceeds_aud
                fx_sell = proceeds_aud / proceeds_eur if proceeds_eur > 0 else fx_now
            else:
                val_eur_now = qty_for_calc * p_now_fx
                val_aud_now = val_eur_now * fx_now
                fx_sell = fx_now
            market_return_eur = val_eur_now - cost_eur
            market_return_aud_at_purchase_fx = market_return_eur * fx_buy
            fx_impact_aud = val_eur_now * (fx_sell - fx_buy)
            total_pl_aud = val_aud_now - cost_aud
            giorni = (datetime.now().date() - buy_row['Data'].date()).days
            status = "🔒 Closed" if is_closed else "✅ Open"
            fx_decomp_rows.append({
                'ISIN': isin, 'Status': status, 'Date Purchased': buy_row['Data'].strftime('%Y-%m-%d'),
                'Days Held': giorni, 'Qty': qty_for_calc, 'Cost (EUR)': cost_eur,
                'Value Now (EUR)': val_eur_now, 'Market Return (EUR)': market_return_eur,
                'FX at Purchase': fx_buy, 'FX at Sale/Today': fx_sell, 'FX Δ': fx_sell - fx_buy,
                'Market Return in AUD (at purchase FX)': market_return_aud_at_purchase_fx,
                'FX Impact (AUD)': fx_impact_aud, 'Total P&L (AUD)': total_pl_aud,
            })
    df_decomp = pd.DataFrame(fx_decomp_rows)
    if not df_decomp.empty:
        tot_mkt_eur = df_decomp['Market Return (EUR)'].sum()
        tot_mkt_aud = df_decomp['Market Return in AUD (at purchase FX)'].sum()
        tot_fx_aud = df_decomp['FX Impact (AUD)'].sum()
        tot_pl_aud = df_decomp['Total P&L (AUD)'].sum()
        sm1, sm2, sm3, sm4 = st.columns(4)
        sm1.metric("Market Return (EUR)", f"€{tot_mkt_eur:,.2f}")
        sm2.metric("Market Return (AUD at buy FX)", f"${tot_mkt_aud:,.2f}")
        sm3.metric("FX Impact (AUD)", f"${tot_fx_aud:,.2f}",
                   delta=f"{'▲' if tot_fx_aud >= 0 else '▼'} {abs(tot_fx_aud/tot_pl_aud*100):.1f}% of total P&L" if tot_pl_aud != 0 else None,
                   delta_color="normal" if tot_fx_aud >= 0 else "inverse")
        sm4.metric("Total P&L (AUD)", f"${tot_pl_aud:,.2f}")
        df_closed_decomp = df_decomp[df_decomp['Status'] == '🔒 Closed']
        df_open_decomp = df_decomp[df_decomp['Status'] == '✅ Open']
        realised_eur = df_closed_decomp['Market Return (EUR)'].sum()
        realised_aud = df_closed_decomp['Total P&L (AUD)'].sum()
        unrealised_eur = df_open_decomp['Market Return (EUR)'].sum()
        unrealised_aud = df_open_decomp['Total P&L (AUD)'].sum()
        r_colour = "#27ae60" if realised_aud >= 0 else "#e74c3c"
        u_colour = "#27ae60" if unrealised_aud >= 0 else "#e74c3c"
        st.markdown(f"""
            <div style="background:#f8f9fa; border-left:4px solid #7f8c8d;
                        padding:10px 16px; border-radius:4px; font-size:0.9rem; margin-top:8px;">
                <b>P&L Composition:</b>&nbsp;&nbsp;
                🔒 <b>Realised:</b>
                    <span style="color:{r_colour}">€{realised_eur:,.2f} EUR</span> /
                    <span style="color:{r_colour}">${realised_aud:,.2f} AUD</span>
                &nbsp;&nbsp;|&nbsp;&nbsp;
                ✅ <b>Unrealised:</b>
                    <span style="color:{u_colour}">€{unrealised_eur:,.2f} EUR</span> /
                    <span style="color:{u_colour}">${unrealised_aud:,.2f} AUD</span>
            </div>""", unsafe_allow_html=True)
        st.divider()
        df_bar_fx = df_decomp.groupby(['ISIN', 'Status']).agg({
            'Market Return in AUD (at purchase FX)': 'sum', 'FX Impact (AUD)': 'sum', 'Total P&L (AUD)': 'sum'
        }).reset_index()
        df_bar_fx['Label'] = df_bar_fx.apply(lambda r: f"{r['ISIN']} 🔒" if r['Status'] == '🔒 Closed' else r['ISIN'], axis=1)
        df_open_bar = df_bar_fx[df_bar_fx['Status'] == '✅ Open']
        df_closed_bar = df_bar_fx[df_bar_fx['Status'] == '🔒 Closed']
        fig_decomp_bar = go.Figure()
        fig_decomp_bar.add_trace(go.Bar(name='Market Return — Open (AUD)', x=df_open_bar['Label'],
                                        y=df_open_bar['Market Return in AUD (at purchase FX)'], marker_color='#2980b9'))
        fig_decomp_bar.add_trace(go.Bar(name='FX Impact — Open (AUD)', x=df_open_bar['Label'],
                                        y=df_open_bar['FX Impact (AUD)'], marker_color='#e74c3c'))
        fig_decomp_bar.add_trace(go.Bar(name='Market Return — Sold 🔒 (AUD)', x=df_closed_bar['Label'],
                                        y=df_closed_bar['Market Return in AUD (at purchase FX)'],
                                        marker=dict(color='#85c1e9', pattern=dict(shape="/"))))
        fig_decomp_bar.add_trace(go.Bar(name='FX Impact — Sold 🔒 (AUD)', x=df_closed_bar['Label'],
                                        y=df_closed_bar['FX Impact (AUD)'],
                                        marker=dict(color='#f1948a', pattern=dict(shape="/"))))
        fig_decomp_bar.update_layout(
            barmode='stack', title="AUD P&L Split: Market Return vs FX Impact",
            yaxis_title="AUD $", height=400, hovermode="x unified", legend=dict(orientation="h", y=1.15))
        st.plotly_chart(fig_decomp_bar, use_container_width=True)
        st.markdown("#### Lot-Level Detail")
        def highlight_closed(row):
            if row['Status'] == '🔒 Closed':
                return ['background-color: rgba(231,76,60,0.08)'] * len(row)
            return [''] * len(row)
        st.dataframe(df_decomp.style.apply(highlight_closed, axis=1)
                     .map(lambda v: 'color: #27ae60' if isinstance(v, (int, float)) and v > 0
                          else ('color: #e74c3c' if isinstance(v, (int, float)) and v < 0 else ''),
                          subset=['Market Return (EUR)', 'FX Impact (AUD)', 'Total P&L (AUD)'])
                     .format({'Qty': '{:.4f}', 'Cost (EUR)': '€{:,.2f}', 'Value Now (EUR)': '€{:,.2f}',
                              'Market Return (EUR)': '€{:,.2f}', 'FX at Purchase': '{:.4f}',
                              'FX at Sale/Today': '{:.4f}', 'FX Δ': '{:+.4f}',
                              'Market Return in AUD (at purchase FX)': '${:,.2f}',
                              'FX Impact (AUD)': '${:,.2f}', 'Total P&L (AUD)': '${:,.2f}'}),
                     use_container_width=True, hide_index=True)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — RAIZ & VANGUARD
# ══════════════════════════════════════════════════════════════════════════════
with tab5:
    st.header("🌱 Raiz & Vanguard — ASX Portfolio")

    # ── RAIZ SECTION ──────────────────────────────────────────────────────────
    st.subheader("🌱 Raiz — ETF Portfolio")
    ETF_NAMES = {
        "AAA": "Betashares Cash", "STW": "SPDR ASX 200", "IAA": "iShares Asia 50",
        "IEU": "iShares Europe", "IAF": "iShares Bond", "RCB": "Russell Corp Bond", "IVV": "iShares S&P 500",
    }

    # Use shared cached loader — same data as Dashboard, no duplicate download
    def load_raiz_csv():
        try:
            df, label = _load_raiz_csv_raw()
            if df.empty:
                return None, None, "No CSV files found"
            return df, label, None
        except Exception as e:
            return None, None, str(e)

    df_csv, csv_label, csv_error = load_raiz_csv()
    if csv_error:
        st.error(f"Could not load Raiz CSV: {csv_error}")
        uploaded = st.file_uploader("Upload Raiz Trade Statement CSV", type="csv")
        if uploaded:
            df_csv = pd.read_csv(uploaded)
            csv_label = uploaded.name
        else:
            st.info("Download your trade statement from Raiz and upload it above.")
            df_csv = None

    if df_csv is not None:
        st.caption(f"📂 {csv_label}")
        if st.button("🔄 Refresh Prices", key="raiz_price_refresh"):
            st.cache_data.clear()
            st.rerun()

        df_csv.columns = [c.strip() for c in df_csv.columns]
        df_csv['Trade Date'] = pd.to_datetime(df_csv['Trade Date'], dayfirst=True)
        df_csv['Quantity'] = pd.to_numeric(df_csv['Quantity'], errors='coerce')
        df_csv['Price'] = pd.to_numeric(df_csv['Price'], errors='coerce')
        df_csv['Amount'] = pd.to_numeric(df_csv['Amount'], errors='coerce')
        df_csv['Trade Date Only'] = df_csv['Trade Date'].dt.date
        #IVV_SPLIT_DATE = pd.Timestamp('2022-12-09')
        #IVV_SPLIT_FACTOR = 15.317277
        #ivv_mask = df_csv['Instrument Code'] == 'IVV'
       #pre_split_mask = ivv_mask & (df_csv['Trade Date'] < IVV_SPLIT_DATE)
        #df_csv.loc[pre_split_mask, 'Quantity'] = df_csv.loc[pre_split_mask, 'Quantity'] * IVV_SPLIT_FACTOR
       #df_csv.loc[pre_split_mask, 'Price'] = df_csv.loc[pre_split_mask, 'Price'] / IVV_SPLIT_FACTOR
        df_csv.loc[df_csv['Transaction Type'] == 'SELL', 'Quantity'] = -df_csv['Quantity'].abs()

        val_date_raiz = st.date_input("Raiz valuation date", value=date.today(), key="raiz_val_date")
        df_val = df_csv[df_csv['Trade Date Only'] <= val_date_raiz].copy()
        holdings_raiz = df_val.groupby('Instrument Code')['Quantity'].sum().reset_index()
        holdings_raiz = holdings_raiz[holdings_raiz['Quantity'].abs() > 0.0001].copy()
        holdings_raiz.rename(columns={'Quantity': 'Net_Qty'}, inplace=True)

        RAIZ_TICKER_MAP = {
            'AAA': 'AAA.AX', 'STW': 'STW.AX', 'IAA': 'IAA.AX',
            'IEU': 'IEU.AX', 'IAF': 'IAF.AX', 'RCB': 'RCB.AX', 'IVV': 'IVV.AX'
        }

        # Use shared price fetcher — same cache as Dashboard
        live_prices_raiz = _get_raiz_live_prices_shared(tuple(holdings_raiz['Instrument Code'].unique()))

        def get_most_recent_csv_price(code):
            p = live_prices_raiz.get(code)
            if p and p > 0: return p
            recent = df_csv[df_csv['Instrument Code'] == code].sort_values('Trade Date', ascending=False)
            return float(recent.iloc[0]['Price']) if not recent.empty else 0.0

        holdings_raiz['Current_Price'] = holdings_raiz['Instrument Code'].apply(get_most_recent_csv_price)
        holdings_raiz['Value_AUD'] = holdings_raiz['Net_Qty'] * holdings_raiz['Current_Price']
        holdings_raiz['ETF Name'] = holdings_raiz['Instrument Code'].map(ETF_NAMES)
        df_buys_raiz = df_csv[(df_csv['Trade Date Only'] <= val_date_raiz) & (df_csv['Transaction Type'] == 'BUY')]
        cost_basis_raiz = df_buys_raiz.groupby('Instrument Code')['Amount'].sum().reset_index()
        cost_basis_raiz.rename(columns={'Amount': 'Cost_Basis_AUD'}, inplace=True)
        holdings_raiz = holdings_raiz.merge(cost_basis_raiz, on='Instrument Code', how='left').fillna(0)
        holdings_raiz['P&L_AUD'] = holdings_raiz['Value_AUD'] - holdings_raiz['Cost_Basis_AUD']
        holdings_raiz['ROI_%'] = (holdings_raiz['P&L_AUD'] / holdings_raiz['Cost_Basis_AUD'] * 100).where(holdings_raiz['Cost_Basis_AUD'] > 0, 0)
        raiz_total = holdings_raiz['Value_AUD'].sum()
        raiz_cost = holdings_raiz['Cost_Basis_AUD'].sum()
        raiz_pl = holdings_raiz['P&L_AUD'].sum()

        r1, r2, r3 = st.columns(3)
        r1.metric("Raiz Portfolio Value (AUD)", f"${raiz_total:,.2f}")
        r2.metric("Raiz Cost Basis (AUD)", f"${raiz_cost:,.2f}")
        r3.metric("Raiz P&L (AUD)", f"${raiz_pl:,.2f}",
                  delta=f"{raiz_pl/raiz_cost*100:.2f}%" if raiz_cost > 0 else None,
                  delta_color="normal" if raiz_pl >= 0 else "inverse")

        display_cols_raiz = ['Instrument Code', 'ETF Name', 'Net_Qty', 'Current_Price', 'Cost_Basis_AUD', 'Value_AUD', 'P&L_AUD', 'ROI_%']
        st.dataframe(holdings_raiz[display_cols_raiz].style
                     .map(lambda v: 'color: #27ae60' if isinstance(v, (int, float)) and v > 0
                          else ('color: #e74c3c' if isinstance(v, (int, float)) and v < 0 else ''),
                          subset=['P&L_AUD', 'ROI_%'])
                     .format({'Net_Qty': '{:.4f}', 'Current_Price': '${:.4f}', 'Cost_Basis_AUD': '${:,.2f}',
                              'Value_AUD': '${:,.2f}', 'P&L_AUD': '${:,.2f}', 'ROI_%': '{:.2f}%'}),
                     use_container_width=True, hide_index=True)

        col_r1, col_r2 = st.columns(2)
        with col_r1:
            fig_raiz_pie = px.pie(holdings_raiz, values='Value_AUD', names='ETF Name', hole=0.4,
                                  title=f"Raiz Allocation — ${raiz_total:,.2f}")
            fig_raiz_pie.update_layout(height=350)
            st.plotly_chart(fig_raiz_pie, use_container_width=True)
        with col_r2:
            fig_raiz_bar = px.bar(holdings_raiz, x='ETF Name', y='P&L_AUD', color='P&L_AUD',
                                  color_continuous_scale=['#e74c3c', '#95a5a6', '#27ae60'], title="Raiz P&L by ETF")
            fig_raiz_bar.add_hline(y=0, line_dash="dash", line_color="grey", opacity=0.5)
            fig_raiz_bar.update_layout(height=350, coloraxis_showscale=False)
            st.plotly_chart(fig_raiz_bar, use_container_width=True)

        with st.expander("📊 Raiz Price Sources"):
            price_info = []
            for code in holdings_raiz['Instrument Code'].unique():
                live_p = live_prices_raiz.get(code)
                recent = df_csv[df_csv['Instrument Code'] == code].sort_values('Trade Date', ascending=False)
                csv_p = float(recent.iloc[0]['Price']) if not recent.empty else 0
                csv_date_str = recent.iloc[0]['Trade Date'].strftime('%Y-%m-%d') if not recent.empty else 'Unknown'
                source = "🟢 Yahoo Live" if (live_p and live_p > 0) else "🟡 CSV fallback"
                price_used = live_p if (live_p and live_p > 0) else csv_p
                price_info.append({"ETF": code, "Price Used": f"${price_used:.4f}", "Source": source,
                                   "CSV Last Price": f"${csv_p:.4f}", "CSV Last Date": csv_date_str})
            st.dataframe(pd.DataFrame(price_info), hide_index=True, use_container_width=True)

        with st.expander("📋 Raiz Full Trade History"):
            st.dataframe(df_csv[['Trade Date', 'Transaction Type', 'Instrument Code', 'Quantity', 'Price', 'Amount']]
                         .sort_values('Trade Date', ascending=False)
                         .style.format({'Trade Date': lambda x: x.strftime('%Y-%m-%d'), 'Quantity': '{:.6f}',
                                        'Price': '${:.4f}', 'Amount': '${:,.4f}'}),
                         use_container_width=True, hide_index=True)

    st.divider()

    # ── VANGUARD SECTION ──────────────────────────────────────────────────────
    st.subheader("📈 Vanguard VDAL — ASX ETF")

    def load_vanguard_data():
        try:
            df_v = load_vanguard_transactions_pg()
            return df_v, None
        except Exception as e:
            return None, str(e)

    df_vdal, vdal_error = load_vanguard_data()

    if vdal_error:
        st.error(f"Could not load Vanguard data: {vdal_error}")
    elif df_vdal is not None and not df_vdal.empty:
        df_vdal = df_vdal.sort_values('Date')

        # Live price
        vdal_live_price = None
        try:
            t = yf.Ticker("VDAL.AX")
            vdal_live_price = float(t.fast_info['last_price'])
        except:
            pass
        if not vdal_live_price:
            vdal_live_price = float(df_vdal['Purchase Price'].dropna().iloc[-1])

        net_qty_vdal = df_vdal['Quantity'].sum()
        total_value_vdal = max(0.0, net_qty_vdal * vdal_live_price)

        # Cost basis (BUY lots only, FIFO residual)
        buys_vdal = df_vdal[df_vdal['Transaction'].str.upper() == 'BUY'].copy()
        total_cost_vdal = (buys_vdal['Quantity'] * buys_vdal['Purchase Price']).sum()
        total_pl_vdal = total_value_vdal - total_cost_vdal
        roi_vdal = (total_pl_vdal / total_cost_vdal * 100) if total_cost_vdal > 0 else 0

        v1, v2, v3, v4 = st.columns(4)
        v1.metric("VDAL Live Price", f"${vdal_live_price:.4f} AUD",
                  help="VDAL.AX via Yahoo Finance")
        v2.metric("Net Holdings", f"{net_qty_vdal:.2f} units")
        v3.metric("Market Value", f"${total_value_vdal:,.2f} AUD")
        v4.metric("P&L", f"${total_pl_vdal:,.2f} AUD",
                  delta=f"{roi_vdal:.2f}%",
                  delta_color="normal" if total_pl_vdal >= 0 else "inverse")

        st.markdown("#### Lot Detail")
        lots_vdal = []
        sold_qty = abs(df_vdal[df_vdal['Transaction'].str.upper() == 'SELL']['Quantity'].sum())
        for _, row in buys_vdal.iterrows():
            qty_ini = row['Quantity']
            if sold_qty > 0:
                if sold_qty >= qty_ini:
                    sold_qty -= qty_ini
                    qty_res = 0.0
                else:
                    qty_res = qty_ini - sold_qty
                    sold_qty = 0.0
            else:
                qty_res = qty_ini
            if qty_res < 0.001: continue
            cost = qty_res * row['Purchase Price']
            val = qty_res * vdal_live_price
            pl = val - cost
            days = (date.today() - row['Date'].date()).days
            lots_vdal.append({
                'Date': row['Date'].strftime('%Y-%m-%d'),
                'Qty': qty_res,
                'Purchase Price': row['Purchase Price'],
                'Current Price': vdal_live_price,
                'Cost (AUD)': cost,
                'Value (AUD)': val,
                'P&L (AUD)': pl,
                'ROI %': (pl/cost*100) if cost > 0 else 0,
                'Days Held': days,
                'CGT': '50% Disc' if days >= 365 else 'No Disc'
            })
        df_lots_vdal = pd.DataFrame(lots_vdal)
        if not df_lots_vdal.empty:
            st.dataframe(df_lots_vdal.style
                         .map(lambda v: 'color: #27ae60' if isinstance(v, (int, float)) and v > 0
                              else ('color: #e74c3c' if isinstance(v, (int, float)) and v < 0 else ''),
                              subset=['P&L (AUD)', 'ROI %'])
                         .format({'Qty': '{:.2f}', 'Purchase Price': '${:.4f}',
                                  'Current Price': '${:.4f}',
                                  'Cost (AUD)': '${:,.2f}', 'Value (AUD)': '${:,.2f}',
                                  'P&L (AUD)': '${:,.2f}', 'ROI %': '{:.2f}%'}),
                         use_container_width=True, hide_index=True)

        with st.expander("📋 VDAL Full Trade History"):
            st.dataframe(df_vdal[['Date', 'Transaction', 'Quantity', 'Purchase Price']]
                         .style.format({'Date': lambda x: x.strftime('%Y-%m-%d'),
                                        'Quantity': '{:.2f}', 'Purchase Price': '${:.4f}'}),
                         use_container_width=True, hide_index=True)

    st.divider()

    # ── ASX SHARES SECTION ────────────────────────────────────────────────────
    st.subheader("🇦🇺 ASX Shares")
    if st.button("🔄 Refresh Share Prices", key="shares_refresh"):
        st.cache_data.clear()
        st.rerun()

    if not df_shares.empty:
        sh1, sh2 = st.columns(2)
        sh1.metric("Total Value (AUD)", f"${shares_total_aud:,.2f}")
        sh2.metric("Holdings", f"{len(df_shares)} stocks")

        st.dataframe(df_shares[['Code', 'Name', 'Quantity', 'Live Price (AUD)', 'Value (AUD)', 'Source']].style
                     .format({'Quantity': '{:.0f}',
                              'Live Price (AUD)': lambda x: f'${x:,.4f}' if x else 'N/A',
                              'Value (AUD)': lambda x: f'${x:,.2f}' if x else 'N/A'}),
                     use_container_width=True, hide_index=True)

        col_sp, col_sb = st.columns(2)
        with col_sp:
            fig_shares_pie = px.pie(df_shares[df_shares['Value (AUD)'] > 0],
                                    values='Value (AUD)', names='Name', hole=0.4,
                                    title=f"Shares Allocation — ${shares_total_aud:,.2f}",
                                    color_discrete_sequence=px.colors.qualitative.Set3)
            fig_shares_pie.update_layout(height=320)
            st.plotly_chart(fig_shares_pie, use_container_width=True)
        with col_sb:
            fig_shares_bar = px.bar(df_shares[df_shares['Value (AUD)'] > 0],
                                    x='Name', y='Value (AUD)', color='Name',
                                    color_discrete_sequence=px.colors.qualitative.Set3,
                                    title="Value by Stock (AUD)")
            fig_shares_bar.update_layout(height=320, showlegend=False, yaxis_tickprefix="$")
            st.plotly_chart(fig_shares_bar, use_container_width=True)
    else:
        st.info("No share data available. Check your Shares tab in Google Sheets.")

    st.divider()

    # ── COMBINED SUMMARY ──────────────────────────────────────────────────────
    st.subheader("📊 Combined ASX Portfolio Summary")
    combined_value = raiz_total_aud + vanguard_total_aud + shares_total_aud
    df_combined = pd.DataFrame([
        {"Portfolio": "🌱 Raiz ETFs", "Value (AUD)": raiz_total_aud},
        {"Portfolio": "📈 Vanguard VDAL", "Value (AUD)": vanguard_total_aud},
        {"Portfolio": "🇦🇺 ASX Shares", "Value (AUD)": shares_total_aud},
    ])
    cs1, cs2 = st.columns(2)
    with cs1:
        fig_combined = px.pie(df_combined, values="Value (AUD)", names="Portfolio", hole=0.4,
                              title=f"Total: ${combined_value:,.2f} AUD",
                              color_discrete_sequence=["#27ae60", "#2ecc71", "#1abc9c"])
        fig_combined.update_layout(height=300)
        st.plotly_chart(fig_combined, use_container_width=True)
    with cs2:
        st.metric("Raiz ETFs", f"${raiz_total_aud:,.2f}")
        st.metric("Vanguard VDAL", f"${vanguard_total_aud:,.2f}")
        st.metric("ASX Shares", f"${shares_total_aud:,.2f}")
        st.metric("Combined Total", f"${combined_value:,.2f}",
                  delta=f"€{combined_value/fx_now:,.2f} EUR equiv.")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 6 — COMMODITIES (Revolut Metals)
# ══════════════════════════════════════════════════════════════════════════════
with tab6:
    st.header("🪙 Commodities — Revolut Precious Metals")
    st.caption("Holdings from your Metal Google Sheet tab. Prices fetched live from Yahoo Finance (USD futures → AUD).")

    # USD→AUD rate
    @st.cache_data(ttl=600)
    def get_usd_aud():
        try:
            rate = float(yf.Ticker("AUDUSD=X").fast_info['last_price'])
            return 1 / rate if rate > 0 else 1.58
        except:
            return 1.58

    usd_to_aud = get_usd_aud()

    df_metal, metal_error = load_metal_data()
    metal_prices = get_metal_prices()

    if metal_error:
        st.error(f"Could not load Metal sheet: {metal_error}")
    elif df_metal is not None and not df_metal.empty:
        df_metal['Date'] = pd.to_datetime(df_metal['Date'], dayfirst=True)
        df_metal['Quantity'] = pd.to_numeric(df_metal['Quantity'], errors='coerce').fillna(0)
        df_metal['Purchase Price'] = pd.to_numeric(df_metal['Purchase Price'], errors='coerce')
        if 'Currency' not in df_metal.columns:
            df_metal['Currency'] = 'AUD'
        df_metal['Currency'] = df_metal['Currency'].fillna('AUD').str.strip().str.upper()
        df_metal.loc[df_metal['Transaction'].str.upper() == 'SELL', 'Quantity'] = -df_metal['Quantity'].abs()
        df_metal = df_metal.sort_values('Date')

        def row_cost_aud(row):
            if row['Transaction'].upper() != 'BUY':
                return 0.0
            date_str = row['Date'].strftime('%Y-%m-%d')
            return convert_purchase_to_aud(
                row['Purchase Price'] * abs(row['Quantity']),
                row['Currency'],
                date_str
            )
        df_metal['Cost_AUD'] = df_metal.apply(row_cost_aud, axis=1)

        if st.button("🔄 Refresh Metal Prices", key="metal_refresh"):
            st.cache_data.clear()
            st.rerun()

        st.info(f"EUR/AUD: {fx_now:.4f} — Live prices: AUD spot (XAUAUD, XAGAUD, XPTAUD) — Purchase prices converted to AUD at historical rates")

        # Per-metal cards
        metals_summary = []
        for metal, cfg in METAL_CONFIG.items():
            df_m_metal = df_metal[df_metal['Type'] == metal].copy()
            if df_m_metal.empty:
                continue
            net_qty = df_m_metal['Quantity'].sum()
            if abs(net_qty) < 0.00001:
                continue

            price_info = metal_prices.get(metal, {})
            price_usd = price_info.get('usd')
            price_aud = price_info.get('aud')

            buys = df_m_metal[df_m_metal['Transaction'].str.upper() == 'BUY']
            cost_aud = buys['Cost_AUD'].sum()

            if price_aud:
                value_aud = net_qty * price_aud
            else:
                last_pp = df_m_metal['Purchase Price'].dropna().iloc[-1]
                last_curr = df_m_metal['Currency'].iloc[-1]
                last_date = df_m_metal['Date'].iloc[-1].strftime('%Y-%m-%d')
                value_aud = convert_purchase_to_aud(net_qty * float(last_pp), last_curr, last_date)

            value_eur = value_aud / fx_now
            cost_eur = cost_aud / fx_now
            pl_aud = value_aud - cost_aud
            pl_eur = value_eur - cost_eur
            roi = (pl_aud / cost_aud * 100) if cost_aud > 0 else 0

            metals_summary.append({
                'Metal': metal,
                'Symbol': cfg['symbol'],
                'Net Qty (troy oz)': net_qty,
                'Live Price (USD)': f"${price_usd:,.2f}" if price_usd else "N/A",
                'Live Price (AUD)': price_aud,
                'Cost (AUD)': cost_aud,
                'Cost (EUR)': cost_eur,
                'Value (AUD)': value_aud,
                'Value (EUR)': value_eur,
                'P&L (AUD)': pl_aud,
                'P&L (EUR)': pl_eur,
                'ROI %': roi,
                'colour': cfg['colour'],
            })

        if metals_summary:
            # Summary metrics
            total_cost_metals = sum(m['Cost (AUD)'] for m in metals_summary)
            total_value_metals = sum(m['Value (AUD)'] for m in metals_summary)
            total_pl_metals = total_value_metals - total_cost_metals
            total_roi_metals = (total_pl_metals / total_cost_metals * 100) if total_cost_metals > 0 else 0

            mc1, mc2, mc3, mc4 = st.columns(4)
            mc1.metric("Total Cost (AUD)", f"${total_cost_metals:,.2f}")
            mc2.metric("Total Value (AUD)", f"${total_value_metals:,.2f}")
            mc3.metric("Total P&L (AUD)", f"${total_pl_metals:,.2f}",
                       delta=f"{total_roi_metals:.2f}%",
                       delta_color="normal" if total_pl_metals >= 0 else "inverse")
            mc4.metric("Total Value (EUR)", f"€{total_value_metals/fx_now:,.2f}")
            st.divider()

            # Per metal cards
            cols = st.columns(len(metals_summary))
            for i, m in enumerate(metals_summary):
                with cols[i]:
                    colour = m['colour']
                    pl_sign = "+" if m['P&L (AUD)'] >= 0 else ""
                    st.markdown(f"""
                        <div style="border-left: 4px solid {colour}; padding: 12px 16px;
                                    background: #f8f9fa; border-radius: 6px; margin-bottom: 8px;">
                            <div style="font-size:1.1rem; font-weight:700; color:{colour};">
                                {m['Metal']} ({m['Symbol']})
                            </div>
                            <div style="font-size:0.85rem; color:#555; margin: 4px 0;">
                                {m['Net Qty (troy oz)']:.4f} units
                            </div>
                            <div style="font-size:1.2rem; font-weight:600;">
                                ${m['Value (AUD)']:,.2f} AUD
                            </div>
                            <div style="font-size:0.85rem; color:#555;">
                                €{m['Value (EUR)']:,.2f} EUR
                            </div>
                            <div style="font-size:0.9rem; color:{'#27ae60' if m['P&L (AUD)'] >= 0 else '#e74c3c'}; margin-top:6px;">
                                {pl_sign}${m['P&L (AUD)']:,.2f} AUD ({pl_sign}{m['ROI %']:.2f}%)
                            </div>
                            <div style="font-size:0.8rem; color:#888; margin-top:4px;">
                                Live: {('$' + f"{m['Live Price (AUD)']:,.2f}") if m['Live Price (AUD)'] else 'N/A'} AUD/unit
                            </div>
                        </div>""", unsafe_allow_html=True)

            st.divider()

            # Allocation chart
            col_mp, col_mb = st.columns(2)
            df_metals_chart = pd.DataFrame([{
                'Metal': m['Metal'], 'Value (AUD)': m['Value (AUD)'],
                'P&L (AUD)': m['P&L (AUD)']
            } for m in metals_summary])
            with col_mp:
                fig_metals_pie = px.pie(df_metals_chart, values='Value (AUD)', names='Metal', hole=0.4,
                                        title="Metals Allocation",
                                        color_discrete_sequence=[m['colour'] for m in metals_summary])
                fig_metals_pie.update_layout(height=300)
                st.plotly_chart(fig_metals_pie, use_container_width=True)
            with col_mb:
                fig_metals_bar = px.bar(df_metals_chart, x='Metal', y='P&L (AUD)', color='Metal',
                                        color_discrete_sequence=[m['colour'] for m in metals_summary],
                                        title="P&L by Metal (AUD)")
                fig_metals_bar.add_hline(y=0, line_dash="dash", line_color="grey", opacity=0.5)
                fig_metals_bar.update_layout(height=300, showlegend=False)
                st.plotly_chart(fig_metals_bar, use_container_width=True)

            st.divider()

            # Lot detail per metal
            st.subheader("Lot Detail")
            for metal in [m['Metal'] for m in metals_summary]:
                cfg = METAL_CONFIG[metal]
                df_m_metal = df_metal[df_metal['Type'] == metal].copy()
                price_aud = metal_prices.get(metal, {}).get('aud')
                with st.expander(f"📋 {metal} ({cfg['symbol']}) — Trade History"):
                    lots = []
                    sold_qty = abs(df_m_metal[df_m_metal['Transaction'].str.upper() == 'SELL']['Quantity'].sum())
                    buys_m = df_m_metal[df_m_metal['Transaction'].str.upper() == 'BUY'].copy()
                    for _, row in buys_m.iterrows():
                        qty_ini = row['Quantity']
                        if sold_qty > 0:
                            if sold_qty >= qty_ini:
                                sold_qty -= qty_ini
                                qty_res = 0.0
                            else:
                                qty_res = qty_ini - sold_qty
                                sold_qty = 0.0
                        else:
                            qty_res = qty_ini
                        if qty_res < 0.00001: continue
                        # Cost in AUD using historical FX at purchase date
                        ratio = qty_res / qty_ini if qty_ini > 0 else 1.0
                        cost_aud_lot = row['Cost_AUD'] * ratio
                        val_aud_lot = qty_res * price_aud if price_aud else cost_aud_lot
                        pl_lot = val_aud_lot - cost_aud_lot
                        days = (date.today() - row['Date'].date()).days
                        curr = row.get('Currency', 'AUD')
                        lots.append({
                            'Date': row['Date'].strftime('%Y-%m-%d'),
                            'Currency': curr,
                            'Qty': qty_res,
                            'Purchase Price (orig)': row['Purchase Price'],
                            'Cost (AUD)': cost_aud_lot,
                            'Live Price (AUD)': price_aud,
                            'Value (AUD)': val_aud_lot,
                            'P&L (AUD)': pl_lot,
                            'Days Held': days,
                            'CGT': '50% Disc' if days >= 365 else 'No Disc'
                        })
                    if lots:
                        df_lots = pd.DataFrame(lots)
                        st.dataframe(df_lots.style
                                     .map(lambda v: 'color: #27ae60' if isinstance(v, (int, float)) and v > 0
                                          else ('color: #e74c3c' if isinstance(v, (int, float)) and v < 0 else ''),
                                          subset=['P&L (AUD)'])
                                     .format({'Qty': '{:.6f}',
                                              'Purchase Price (orig)': '{:.4f}',
                                              'Cost (AUD)': '${:,.2f}',
                                              'Live Price (AUD)': lambda x: f'${x:,.2f}' if x else 'N/A',
                                              'Value (AUD)': '${:,.2f}',
                                              'P&L (AUD)': '${:,.2f}'}),
                                     use_container_width=True, hide_index=True)
                        # Totals row
                        tot_qty = df_lots['Qty'].sum()
                        tot_cost = df_lots['Cost (AUD)'].sum()
                        tot_val = df_lots['Value (AUD)'].sum()
                        tot_pl = df_lots['P&L (AUD)'].sum()
                        tot_roi = (tot_pl / tot_cost * 100) if tot_cost > 0 else 0
                        t1, t2, t3, t4, t5 = st.columns(5)
                        t1.metric("Net Qty", f"{tot_qty:.6f}")
                        t2.metric("Total Cost (AUD)", f"${tot_cost:,.2f}")
                        t3.metric("Live Price (AUD)", f"${price_aud:,.2f}" if price_aud else "N/A")
                        t4.metric("Total Value (AUD)", f"${tot_val:,.2f}")
                        t5.metric("P&L", f"${tot_pl:,.2f}", delta=f"{tot_roi:.2f}%",
                                  delta_color="normal" if tot_pl >= 0 else "inverse")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 7 — SUPER
# ══════════════════════════════════════════════════════════════════════════════
with tab7:
    st.header("🏛️ Superannuation — Mercer SmartPath (Born 1969–1973)")
    st.caption("Manual balance — Mercer Super does not publish a public unit price feed. Update when you receive your statement.")

    # Load current super balance from Cash sheet
    def load_super_balance():
        try:
            bal = load_cash_balances()
            return float(bal.get("Super", 0.0))
        except:
            return 0.0

    current_super = load_super_balance()

    st.markdown("### Current Balance")
    s1, s2, s3 = st.columns(3)
    s1.metric("Super Balance (AUD)", f"${current_super:,.2f}")
    s2.metric("Equivalent (EUR)", f"€{current_super/fx_now:,.2f}")
    s3.metric("Fund", "Mercer SmartPath")

    st.divider()
    st.markdown("### Update Balance")
    st.markdown("""
        <div style="background:#eaf4fb; border-left:4px solid #2980b9; padding:10px 16px;
                    border-radius:4px; font-size:0.88rem; margin-bottom:16px;">
            💡 <b>How to find your balance:</b> Log into
            <a href="https://www.mercersuper.com.au" target="_blank">mercersuper.com.au</a>
            → Member Portal → Account Balance. Update this whenever you receive a statement
            or after making contributions.
        </div>""", unsafe_allow_html=True)

    new_super_balance = st.number_input(
        "Mercer SmartPath Balance (AUD)",
        min_value=0.0,
        value=current_super,
        step=1000.0,
        format="%.2f",
        help="Enter the current balance shown in your Mercer member portal"
    )

    col_btn, col_info = st.columns([1, 3])
    with col_btn:
        if st.button("💾 Save Super Balance", type="primary", key="super_save_btn"):
            # Load all current cash balances first, then update just Super
            try:
                all_balances = load_cash_balances()
                all_balances['Super'] = new_super_balance
                ok, err = save_cash_balances(all_balances)
                if ok:
                    st.success(f"✅ Super balance updated to ${new_super_balance:,.2f}")
                    get_super_total_for_dashboard.clear()
                    get_cash_total_for_dashboard.clear()
                    st.rerun()
                else:
                    st.error(f"Could not save: {err}")
            except Exception as e:
                st.error(f"Error: {e}")

    st.divider()
    st.markdown("### About Mercer SmartPath (Born 1969–1973)")
    st.markdown("""
        <div style="background:#f8f9fa; padding:12px 16px; border-radius:6px; font-size:0.88rem; line-height:1.6;">
            <b>Strategy:</b> Lifecycle / target-date fund — automatically shifts from growth to defensive
            assets as you approach retirement.<br>
            <b>Your cohort (1969–1973):</b> Currently positioned with a meaningful growth allocation,
            progressively de-risking toward retirement age (~60–65).<br>
            <b>Benchmark performance:</b> Mercer SmartPath has delivered an average of ~8.6% p.a.
            over the 10-year period to March 2026 (across member cohorts).<br>
            <b>Annual fee:</b> ~$440/year flat fee for your cohort (plus indirect costs).<br>
            <b>Contributions tax:</b> 15% on concessional contributions inside super.
        </div>""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 8 — CASH
# ══════════════════════════════════════════════════════════════════════════════
with tab8:
    st.header("🏦 Cash & Savings Accounts")
    st.caption("Pure cash and deposit accounts only. Super, Vanguard and Revolut Metals are tracked in their own tabs.")

    # Cash-only accounts (Super removed — managed in Super tab, Vanguard and Revolut Metals in their tabs)
    ACCOUNTS = [
        {"name": "CBA",            "currency": "AUD", "flag": "🇦🇺"},
        {"name": "Me Bank",        "currency": "AUD", "flag": "🇦🇺"},
        {"name": "Rabobank",       "currency": "AUD", "flag": "🇦🇺"},
        {"name": "Up",             "currency": "AUD", "flag": "🇦🇺"},
        {"name": "Trade Republic", "currency": "EUR", "flag": "🇩🇪"},
        {"name": "N26",            "currency": "EUR", "flag": "🇩🇪"},
        {"name": "BUNQ",           "currency": "EUR", "flag": "🇳🇱"},
        {"name": "BPM Cash",       "currency": "EUR", "flag": "🇮🇹"},
        {"name": "BPM Bonds",      "currency": "EUR", "flag": "🇮🇹"},
        {"name": "C6 Cash",        "currency": "BRL", "flag": "🇧🇷"},
        {"name": "C6 Investments", "currency": "BRL", "flag": "🇧🇷"},
    ]

    @st.cache_data(ttl=600)
    def get_brl_aud():
        try:
            return float(yf.Ticker("BRLAUD=X").fast_info['last_price'])
        except:
            return 0.27

    brl_to_aud = get_brl_aud()

    current_balances = load_cash_balances()

    if st.button("🔄 Refresh from Sheet", key="cash_refresh_btn"):
        st.rerun()

    st.markdown("### Update Balances")
    aud_accounts = [a for a in ACCOUNTS if a["currency"] == "AUD"]
    eur_accounts = [a for a in ACCOUNTS if a["currency"] == "EUR"]
    brl_accounts = [a for a in ACCOUNTS if a["currency"] == "BRL"]

    col_aud, col_eur, col_brl = st.columns(3)
    new_balances = {}

    with col_aud:
        st.markdown("**🇦🇺 AUD Accounts**")
        for acc in aud_accounts:
            new_balances[acc["name"]] = st.number_input(
                f"{acc['flag']} {acc['name']} (AUD)", min_value=0.0,
                value=float(current_balances.get(acc["name"], 0.0)), step=100.0, format="%.2f")

    with col_eur:
        st.markdown("**🇪🇺 EUR Accounts**")
        for acc in eur_accounts:
            new_balances[acc["name"]] = st.number_input(
                f"{acc['flag']} {acc['name']} (EUR)", min_value=0.0,
                value=float(current_balances.get(acc["name"], 0.0)), step=100.0, format="%.2f")

    with col_brl:
        st.markdown("**🇧🇷 BRL Accounts**")
        for acc in brl_accounts:
            new_balances[acc["name"]] = st.number_input(
                f"{acc['flag']} {acc['name']} (BRL)", min_value=0.0,
                value=float(current_balances.get(acc["name"], 0.0)), step=100.0, format="%.2f")
        st.caption(f"BRL/AUD rate: {brl_to_aud:.4f}")

    # Preserve Super balance when saving cash (don't overwrite it)
    if st.button("💾 Save Balances", type="primary", key="cash_save_btn"):
        all_balances_to_save = dict(new_balances)
        all_balances_to_save['Super'] = current_balances.get('Super', 0.0)
        ok, err = save_cash_balances(all_balances_to_save)
        if ok:
            st.success("✅ Balances saved!")
            get_cash_total_for_dashboard.clear()
            get_super_total_for_dashboard.clear()
            st.rerun()
        else:
            st.error(f"Could not save: {err}")

    st.divider()

    total_aud_cash = sum(new_balances[a["name"]] for a in ACCOUNTS if a["currency"] == "AUD")
    total_eur_cash = sum(new_balances[a["name"]] for a in ACCOUNTS if a["currency"] == "EUR")
    total_brl_cash = sum(new_balances[a["name"]] for a in ACCOUNTS if a["currency"] == "BRL")
    total_eur_in_aud = total_eur_cash * fx_now
    total_brl_in_aud = total_brl_cash * brl_to_aud
    total_cash_aud = total_aud_cash + total_eur_in_aud + total_brl_in_aud
    total_cash_eur = total_cash_aud / fx_now if fx_now else 0

    st.markdown("### Summary")
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("AUD Cash", f"${total_aud_cash:,.2f}")
    m2.metric("EUR Cash", f"€{total_eur_cash:,.2f}")
    m3.metric("BRL Cash", f"R${total_brl_cash:,.2f}")
    m4.metric("Total in AUD", f"${total_cash_aud:,.2f}", help=f"EUR @ {fx_now:.4f} · BRL @ {brl_to_aud:.4f}")
    m5.metric("Total in EUR", f"€{total_cash_eur:,.2f}")
    st.divider()

    st.markdown("### Account Breakdown")
    rows = []
    for acc in ACCOUNTS:
        bal = new_balances[acc["name"]]
        if acc["currency"] == "AUD":
            bal_aud, bal_eur = bal, bal / fx_now if fx_now else 0
        elif acc["currency"] == "EUR":
            bal_eur, bal_aud = bal, bal * fx_now
        else:
            bal_aud = bal * brl_to_aud
            bal_eur = bal_aud / fx_now if fx_now else 0
        rows.append({"Account": f"{acc['flag']} {acc['name']}", "Currency": acc["currency"],
                     "Balance": bal, "Value (AUD)": bal_aud, "Value (EUR)": bal_eur})
    df_cash = pd.DataFrame(rows)
    st.dataframe(df_cash.style.format({"Balance": "{:,.2f}", "Value (AUD)": "${:,.2f}", "Value (EUR)": "€{:,.2f}"}),
                 use_container_width=True, hide_index=True)
    st.divider()

    df_cash_plot = df_cash[df_cash["Value (AUD)"] > 0]
    if not df_cash_plot.empty:
        st.markdown("### Allocation")
        fig_cash_pie = px.pie(df_cash_plot, values="Value (AUD)", names="Account", hole=0.4,
                              title=f"Total Cash: ${total_cash_aud:,.2f} AUD",
                              color_discrete_sequence=px.colors.qualitative.Set2)
        fig_cash_pie.update_layout(height=400, margin=dict(t=40, b=20))
        st.plotly_chart(fig_cash_pie, use_container_width=True)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 9 — DIAGNOSTICS
# ══════════════════════════════════════════════════════════════════════════════
with tab9:
    st.header("🛠️ Diagnostics")
    st.markdown("System status, data sources, and debug information.")
    
    # FX Rates
    st.subheader("💱 Current Exchange Rates")
    col_fx1, col_fx2, col_fx3 = st.columns(3)
    with col_fx1:
        st.metric("EUR/AUD", f"{fx_now:.4f}", help="Euro to Australian Dollar")
    with col_fx2:
        try:
            usd_aud_rate = get_usd_aud()
            st.metric("USD/AUD", f"{usd_aud_rate:.4f}", help="US Dollar to Australian Dollar")
        except:
            st.metric("USD/AUD", "N/A")
    with col_fx3:
        try:
            brl_aud_rate = float(yf.Ticker("BRLAUD=X").fast_info['last_price'])
            st.metric("BRL/AUD", f"{brl_aud_rate:.4f}", help="Brazilian Real to Australian Dollar")
        except:
            st.metric("BRL/AUD", "N/A")
    
    st.divider()
    
    # N26 Price Feed Status
    st.subheader("📊 N26 European Portfolio — Price Feed Status")
    if diag_logs:
        df_diag = pd.DataFrame.from_dict(diag_logs, orient='index')
        st.dataframe(df_diag, use_container_width=True)
    else:
        st.info("No N26 price data available")
    
    st.divider()
    
    # Metal Prices
    st.subheader("🪙 Metal Prices")
    try:
        metal_prices = get_metal_prices()
        metal_diag = []
        for metal, cfg in METAL_CONFIG.items():
            p = metal_prices.get(metal, {})
            metal_diag.append({
                "Metal": metal,
                "Ticker": cfg['ticker'],
                "Price (AUD)": f"${p['aud']:,.2f}" if p.get('aud') else "N/A",
                "Status": "🟢 LIVE" if p.get('aud') else "🔴 FALLBACK"
            })
        st.table(pd.DataFrame(metal_diag))
    except Exception as e:
        st.warning(f"Could not fetch metal prices: {e}")
    
    st.divider()
    
    # Vanguard VDAL Status
    st.subheader("📈 Vanguard VDAL")
    try:
        t = yf.Ticker("VDAL.AX")
        vdal_p = float(t.fast_info['last_price'])
        st.success(f"🟢 VDAL.AX: ${vdal_p:.4f} AUD")
        
        # Get additional info
        try:
            info = t.info
            st.caption(f"Bid: ${info.get('bid', 'N/A')} | Ask: ${info.get('ask', 'N/A')} | Volume: {info.get('volume', 'N/A')}")
        except:
            pass
    except:
        st.error("🔴 VDAL.AX price unavailable")
    
    st.divider()
    
    # Portfolio Values
    st.subheader("💰 Current Portfolio Values (AUD)")
    col_p1, col_p2, col_p3 = st.columns(3)
    with col_p1:
        st.metric("N26 European", f"${current_market_value_eur * fx_now:,.2f}")
        st.metric("Raiz ETFs", f"${raiz_total_aud:,.2f}")
        st.metric("Vanguard VDAL", f"${vanguard_total_aud:,.2f}")
    with col_p2:
        st.metric("ASX Shares", f"${shares_total_aud:,.2f}")
        st.metric("Commodities", f"${commodities_total_aud:,.2f}")
        st.metric("Super", f"${super_total_aud:,.2f}")
    with col_p3:
        st.metric("Cash Total", f"${cash_total_aud:,.2f}")
        st.metric("Total Net Worth", f"${total_nw:,.2f}")
    
    st.divider()
    
    # Cache Management
    st.subheader("🔄 Cache Management")
    if st.button("Clear All Caches", type="secondary"):
        st.cache_data.clear()
        st.success("Caches cleared! The app will reload data on next interaction.")
        st.rerun()
    
    st.caption("Use this button if data seems outdated or after making changes to Google Sheets.")
    
    st.divider()
    
    # Google Sheet Status
    st.subheader("📋 Google Sheet Status")
    try:
        df_check = _sheets_read(PORTFOLIO_SHEET_ID, "Net_Worth!A1:A5")
        if not df_check.empty:
            st.success(f"✅ Connected to portfolio sheet: {PORTFOLIO_SHEET_ID}")
            st.caption(f"Last 5 rows available")
        else:
            st.warning("⚠️ Connected but no data found")
    except Exception as e:
        st.error(f"❌ Could not connect to Google Sheet: {e}")
    
    # Last Snapshot Info
    st.subheader("📸 Last Net Worth Snapshot")
    try:
        df_history = load_net_worth_history()
        if not df_history.empty:
            last_row = df_history.iloc[-1]
            st.write(f"**Date:** {last_row['Date'].strftime('%Y-%m-%d')}")
            st.write(f"**Total Net Worth:** ${last_row['Total_AUD']:,.2f}")
            if 'Contributions_AUD' in last_row.index:
                st.write(f"**Contributions:** ${last_row['Contributions_AUD']:,.2f}")
                st.write(f"**Market Gains:** ${last_row['Market_Gains_AUD']:,.2f}")
                st.write(f"**FX Impact:** ${last_row['FX_Impact_AUD']:,.2f}")
        else:
            st.info("No snapshots saved yet")
    except Exception as e:
        st.warning(f"Could not load snapshot history: {e}")
# ══════════════════════════════════════════════════════════════════════════════
# TAB 10 — FORECAST
# ══════════════════════════════════════════════════════════════════════════════
with tab10:
    st.header("📈 5-Year Financial Forecast")
    st.caption("Project your net worth over 60 months using income, expenses, investment returns and cash interest.")

    FORECAST_SHEET_ID = PORTFOLIO_SHEET_ID

    # ── LOAD / SAVE FORECAST INPUTS ───────────────────────────────────────────
    @st.cache_data(ttl=0)
    def load_forecast_inputs():
        try:
            conn = get_pg()
            df_f = conn.query(
                "SELECT category, key, value FROM forecast_settings",
                ttl=0,
            )
            if df_f.empty:
                return {}
            result = {}
            for _, row in df_f.iterrows():
                cat = str(row['category']).strip()
                key = str(row['key']).strip()
                result[f"{cat}_{key}"] = float(row['value'])
            return result
        except:
            return {}

    def save_forecast_inputs(inputs_dict):
        try:
            CATEGORY_KEY_MAP = [
                ("Income",   "rent_eur"),
                ("Expense",  "housing"),
                ("Expense",  "food"),
                ("Expense",  "transport"),
                ("Expense",  "travel"),
                ("Expense",  "health"),
                ("Expense",  "other"),
                ("Interest", "CBA"),
                ("Interest", "Me Bank"),
                ("Interest", "Rabobank"),
                ("Interest", "Up"),
                ("Interest", "Trade Republic"),
                ("Interest", "N26"),
                ("Interest", "BPM Cash"),
                ("Interest", "BPM Bonds"),
                ("Returns",  "metals_pct"),
            ]
            conn = get_pg()
            with conn.session as s:
                for cat, key in CATEGORY_KEY_MAP:
                    val = float(inputs_dict.get(f"{cat}_{key}", 0.0))
                    s.execute(
                        sql_text("""
                            INSERT INTO forecast_settings (category, key, value)
                            VALUES (:category, :key, :value)
                            ON CONFLICT (category, key) DO UPDATE
                                SET value = EXCLUDED.value, updated_at = now()
                        """),
                        {"category": cat, "key": key, "value": val}
                    )
                s.commit()
            return True, None
        except Exception as e:
            import traceback
            return False, traceback.format_exc()

    # ── COMPUTE HISTORICAL RETURNS ─────────────────────────────────────────────
    def compute_benchmark_returns():
        return {
            'n26':      11.0,
            'raiz':     10.0,
            'vanguard': 9.5,
            'shares':   10.0,
        }

    hist_returns = compute_benchmark_returns()
    forecast_inputs = load_forecast_inputs()

    # ── INPUT PANELS ──────────────────────────────────────────────────────────
    st.markdown("### ⚙️ Forecast Assumptions")
    st.caption("Edit and save — values persist in your Google Sheet.")

    col_inc, col_exp, col_int = st.columns(3)
    new_inputs = {}

    with col_inc:
        st.markdown("**💰 Monthly Income**")
        new_inputs['Income_rent_eur'] = st.number_input(
            "Spanish Rent (EUR/month)", min_value=0.0,
            value=float(forecast_inputs.get('Income_rent_eur', 500.0)),
            step=50.0, format="%.2f")
        rent_aud = new_inputs['Income_rent_eur'] * fx_now
        st.caption(f"≈ ${rent_aud:,.2f} AUD/month")

        st.markdown("**📈 Expected Investment Returns (% p.a.)**")
        new_inputs['Returns_n26_pct'] = st.number_input(
            "N26 European ETFs", min_value=-20.0, max_value=50.0,
            value=float(forecast_inputs.get('Returns_n26_pct', hist_returns['n26'])),
            step=0.5, format="%.2f")
        new_inputs['Returns_raiz_pct'] = st.number_input(
            "Raiz ETFs", min_value=-20.0, max_value=50.0,
            value=float(forecast_inputs.get('Returns_raiz_pct', hist_returns['raiz'])),
            step=0.5, format="%.2f")
        new_inputs['Returns_vanguard_pct'] = st.number_input(
            "Vanguard VDAL", min_value=-20.0, max_value=50.0,
            value=float(forecast_inputs.get('Returns_vanguard_pct', hist_returns['vanguard'])),
            step=0.5, format="%.2f")
        new_inputs['Returns_shares_pct'] = st.number_input(
            "ASX Shares", min_value=-20.0, max_value=50.0,
            value=float(forecast_inputs.get('Returns_shares_pct', hist_returns['shares'])),
            step=0.5, format="%.2f")
        new_inputs['Returns_metals_pct'] = st.number_input(
            "Precious Metals", min_value=-20.0, max_value=50.0,
            value=float(forecast_inputs.get('Returns_metals_pct', 5.0)),
            step=0.5, format="%.2f")
        new_inputs['Returns_super_pct'] = st.number_input(
            "Super (Mercer SmartPath)", min_value=-20.0, max_value=50.0,
            value=float(forecast_inputs.get('Returns_super_pct', 8.6)),
            step=0.5, format="%.2f")

    with col_exp:
        st.markdown("**💸 Monthly Expenses (AUD)**")
        expense_cats = {
            'housing': ('Housing & Rent', 'monthly'),
            'food': ('Food & Groceries', 'monthly'),
            'transport': ('Transport', 'monthly'),
            'travel': ('Travel (annual budget)', 'annual'),
            'health': ('Health & Medical', 'monthly'),
            'other': ('Other', 'monthly'),
        }
        for key, (label, freq) in expense_cats.items():
            new_inputs[f'Expense_{key}'] = st.number_input(
                f"{label} (AUD/{freq[:2]})", min_value=0.0,
                value=float(forecast_inputs.get(f'Expense_{key}', 0.0)),
                step=100.0 if freq == 'annual' else 50.0, format="%.2f")
        monthly_travel = new_inputs.get('Expense_travel', 0.0) / 12
        total_expenses = (sum(new_inputs[f'Expense_{k}'] for k in ['housing','food','transport','health','other']) + monthly_travel)
        st.metric("Total Monthly Expenses", f"${total_expenses:,.2f} AUD")

    with col_int:
        st.markdown("**🏦 Cash Interest Rates (% p.a.)**")
        interest_accounts = ['CBA', 'Me Bank', 'Rabobank', 'Up', 'Trade Republic', 'N26', 'BPM Cash', 'BPM Bonds']
        for acc in interest_accounts:
            new_inputs[f'Interest_{acc}'] = st.number_input(
                acc, min_value=0.0, max_value=20.0,
                value=float(forecast_inputs.get(f'Interest_{acc}', 0.0)),
                step=0.1, format="%.2f")

    if st.button("💾 Save Assumptions", type="primary", key="forecast_save_btn"):
        ok, err = save_forecast_inputs(new_inputs)
        if ok:
            st.success("✅ Assumptions saved!")
            st.cache_data.clear()
            st.rerun()
        else:
            st.error(f"Could not save: {err}")

    st.divider()
    st.divider()
    
    st.divider()
    
    # ==================== SCENARIO SELECTOR ====================
    st.markdown("### 📊 Forecast Scenario")
    st.caption("Choose a market outlook scenario OR use your custom returns from above.")
    
    scenario_options = {
        "Use my manual returns (above)": None,
        "Bear (Pessimistic) - 25% probability": {
            "n26": 4.0, "raiz": 3.0, "vanguard": 2.0, "shares": 3.0, "metals": 0.0, "super": 3.0,
            "description": "Prolonged market downturn, recession risk, lower corporate earnings"
        },
        "Base (Moderate) - 50% probability": {
            "n26": 9.0, "raiz": 8.0, "vanguard": 7.5, "shares": 8.0, "metals": 4.0, "super": 7.0,
            "description": "Moderate economic growth, stable inflation, normal market cycles"
        },
        "Bull (Optimistic) - 25% probability": {
            "n26": 14.0, "raiz": 13.0, "vanguard": 12.0, "shares": 13.0, "metals": 8.0, "super": 11.0,
            "description": "Strong economic growth, technological acceleration, rising corporate profits"
        }
    }
    
    selected_scenario = st.selectbox(
        "Select Market Scenario",
        options=list(scenario_options.keys()),
        index=0,
        help="Bear = 25% probability, Base = 50% probability, Bull = 25% probability"
    )
    
    # Store original manual values before override
    if 'original_manual_returns' not in st.session_state:
        st.session_state.original_manual_returns = {
            'n26': new_inputs['Returns_n26_pct'],
            'raiz': new_inputs['Returns_raiz_pct'],
            'vanguard': new_inputs['Returns_vanguard_pct'],
            'shares': new_inputs['Returns_shares_pct'],
            'metals': new_inputs['Returns_metals_pct'],
            'super': new_inputs['Returns_super_pct'],
        }
    
    # Apply scenario if selected
    if selected_scenario != "Use my manual returns (above)":
        scenario = scenario_options[selected_scenario]
        st.info(f"📈 **{selected_scenario}**: {scenario['description']}")
        
        # Override returns with scenario values
        new_inputs['Returns_n26_pct'] = scenario["n26"]
        new_inputs['Returns_raiz_pct'] = scenario["raiz"]
        new_inputs['Returns_vanguard_pct'] = scenario["vanguard"]
        new_inputs['Returns_shares_pct'] = scenario["shares"]
        new_inputs['Returns_metals_pct'] = scenario["metals"]
        new_inputs['Returns_super_pct'] = scenario["super"]
        
        # Display what was applied
        col_scen1, col_scen2, col_scen3 = st.columns(3)
        with col_scen1:
            st.metric("N26 European ETFs", f"{scenario['n26']:.1f}%")
            st.metric("Raiz ETFs", f"{scenario['raiz']:.1f}%")
        with col_scen2:
            st.metric("Vanguard VDAL", f"{scenario['vanguard']:.1f}%")
            st.metric("ASX Shares", f"{scenario['shares']:.1f}%")
        with col_scen3:
            st.metric("Precious Metals", f"{scenario['metals']:.1f}%")
            st.metric("Super (Mercer)", f"{scenario['super']:.1f}%")
    else:
        # Restore manual returns if switching back from scenario
        if 'original_manual_returns' in st.session_state:
            new_inputs['Returns_n26_pct'] = st.session_state.original_manual_returns['n26']
            new_inputs['Returns_raiz_pct'] = st.session_state.original_manual_returns['raiz']
            new_inputs['Returns_vanguard_pct'] = st.session_state.original_manual_returns['vanguard']
            new_inputs['Returns_shares_pct'] = st.session_state.original_manual_returns['shares']
            new_inputs['Returns_metals_pct'] = st.session_state.original_manual_returns['metals']
            new_inputs['Returns_super_pct'] = st.session_state.original_manual_returns['super']
        st.info("✏️ Using your custom return assumptions from above.")
    
    st.divider()
    
    # ==================== VOLATILITY & SENSITIVITY ====================
    st.markdown("### 📊 Sensitivity Analysis")
    st.caption("Adjust the return assumptions to see how sensitive your projections are to market changes.")
    
    col_sens1, col_sens2 = st.columns(2)
    with col_sens1:
        sensitivity_adjustment = st.slider(
            "Adjust all returns by (% points)",
            min_value=-5.0, max_value=5.0, value=0.0, step=0.5,
            help="Positive values increase all expected returns, negative values decrease them"
        )
    with col_sens2:
        st.metric("Impact on 5-Year NW", 
                 f"${(df_proj.iloc[-1]['Projected NW'] * (1 + sensitivity_adjustment/100) - df_proj.iloc[-1]['Projected NW']):,.0f}" if 'df_proj' in dir() else "Run projection first",
                 delta=f"{sensitivity_adjustment:+.1f}% to all returns")
    
    # Apply sensitivity adjustment
    if sensitivity_adjustment != 0:
        new_inputs['Returns_n26_pct'] = new_inputs.get('Returns_n26_pct', 11.0) + sensitivity_adjustment
        new_inputs['Returns_raiz_pct'] = new_inputs.get('Returns_raiz_pct', 10.0) + sensitivity_adjustment
        new_inputs['Returns_vanguard_pct'] = new_inputs.get('Returns_vanguard_pct', 9.5) + sensitivity_adjustment
        new_inputs['Returns_shares_pct'] = new_inputs.get('Returns_shares_pct', 10.0) + sensitivity_adjustment
        new_inputs['Returns_metals_pct'] = new_inputs.get('Returns_metals_pct', 5.0) + sensitivity_adjustment
        new_inputs['Returns_super_pct'] = new_inputs.get('Returns_super_pct', 8.6) + sensitivity_adjustment
    # ── LOAD CASH BALANCES ONCE ───────────────────────────────────────────────
    cash_bal = load_cash_balances()

    # ── MONTHLY CASH INTEREST FUNCTION ────────────────────────────────────────
    def monthly_cash_interest():
        total_int = 0.0
        for acc in interest_accounts:
            rate = new_inputs.get(f'Interest_{acc}', 0.0) / 100 / 12
            bal = cash_bal.get(acc, 0.0)
            if acc in ('Trade Republic', 'N26', 'BPM Cash', 'BPM Bonds'):
                bal = bal * fx_now
            total_int += bal * rate
        return total_int

    # ── PROJECTION ENGINE ──────────────────────────────────────────────────────
    st.markdown("### 📊 5-Year Net Worth Projection")

    start_nw = total_nw

    def annual_to_monthly(pct):
        return (1 + pct/100) ** (1/12) - 1

    monthly_r = {
        'n26':      annual_to_monthly(new_inputs.get('Returns_n26_pct', hist_returns['n26'])),
        'raiz':     annual_to_monthly(new_inputs.get('Returns_raiz_pct', hist_returns['raiz'])),
        'vanguard': annual_to_monthly(new_inputs.get('Returns_vanguard_pct', hist_returns['vanguard'])),
        'shares':   annual_to_monthly(new_inputs.get('Returns_shares_pct', hist_returns['shares'])),
        'metals':   annual_to_monthly(new_inputs.get('Returns_metals_pct', 5.0)),
        'super':    annual_to_monthly(new_inputs.get('Returns_super_pct', 8.6)),
    }

    monthly_interest = monthly_cash_interest()
    monthly_rent_aud = new_inputs.get('Income_rent_eur', 500.0) * fx_now
    monthly_expenses = total_expenses
    monthly_net_income = monthly_rent_aud + monthly_interest - monthly_expenses

    months = 60
    projection_rows = []
    nw = start_nw
    n26_v = current_market_value_eur * fx_now
    raiz_v = raiz_total_aud
    vdal_v = vanguard_total_aud
    shares_v = shares_total_aud
    metals_v = commodities_total_aud
    super_v = super_total_aud
    cash_v = cash_total_aud
    today = date.today()

    MARGINAL_RATE_PROJ = 0.19
    DIVIDEND_YIELD_PROJ = 0.02

    for m in range(1, months + 1):
        proj_date = pd.Timestamp(today) + pd.DateOffset(months=m)

        n26_v    *= (1 + monthly_r['n26'])
        raiz_v   *= (1 + monthly_r['raiz'])
        vdal_v   *= (1 + monthly_r['vanguard'])
        shares_v *= (1 + monthly_r['shares'])
        metals_v *= (1 + monthly_r['metals'])
        super_v  *= (1 + monthly_r['super'])

        cash_v += monthly_interest - monthly_expenses + monthly_rent_aud

        if m % 12 == 0:
            tax_interest_yr  = monthly_interest * 12 * MARGINAL_RATE_PROJ
            tax_dividends_yr = n26_v * DIVIDEND_YIELD_PROJ * MARGINAL_RATE_PROJ
            cash_v -= (tax_interest_yr + tax_dividends_yr)

        nw = n26_v + raiz_v + vdal_v + shares_v + metals_v + super_v + cash_v

        projection_rows.append({
            'Date': proj_date,
            'Month': m,
            'Projected NW': nw,
            'N26': n26_v,
            'Raiz': raiz_v,
            'Vanguard': vdal_v,
            'Shares': shares_v,
            'Metals': metals_v,
            'Super': super_v,
            'Cash': cash_v,
        })

    df_proj = pd.DataFrame(projection_rows)

    # ── CHART ─────────────────────────────────────────────────────────────────
    df_actual = load_net_worth_history()
    fig_proj = go.Figure()

    components = [
        ('N26', '#2980b9'), ('Raiz', '#27ae60'), ('Vanguard', '#2ecc71'),
        ('Shares', '#1abc9c'), ('Metals', '#f39c12'), ('Super', '#8e44ad'), ('Cash', '#e67e22'),
    ]
    for comp, colour in components:
        fig_proj.add_trace(go.Scatter(
            x=df_proj['Date'], y=df_proj[comp],
            name=comp, stackgroup='one',
            line=dict(color=colour, width=0.5),
            fillcolor=colour,
            hovertemplate=f"{comp}: $%{{y:,.0f}}<extra></extra>"
        ))

    fig_proj.add_trace(go.Scatter(
        x=df_proj['Date'], y=df_proj['Projected NW'],
        name='Total Projected', mode='lines',
        line=dict(color='white', width=2, dash='dot'),
        hovertemplate='Total: $%{y:,.0f}<extra></extra>'
    ))

    if not df_actual.empty:
        fig_proj.add_trace(go.Scatter(
            x=df_actual['Date'], y=df_actual['Total_AUD'],
            name='✅ Actual', mode='markers',
            marker=dict(size=12, color='#e74c3c', symbol='circle', line=dict(color='white', width=2)),
            hovertemplate='Actual: $%{y:,.2f}<extra></extra>'
        ))

    _today_ts = pd.Timestamp(today)
    fig_proj.add_shape(
        type="line", xref="x", yref="paper",
        x0=_today_ts, x1=_today_ts, y0=0, y1=1,
        line=dict(color="white", dash="dash", width=1),
        opacity=0.5,
    )
    fig_proj.add_annotation(
        x=_today_ts, y=1, xref="x", yref="paper",
        text="Today", showarrow=False,
        xanchor="right", yanchor="bottom",
    )
    fig_proj.update_layout(
        height=550, hovermode="x unified",
        yaxis=dict(title="Net Worth (AUD $)", tickprefix="$"),
        xaxis=dict(title=""),
        legend=dict(orientation="h", y=1.05),
        margin=dict(t=60, b=30),
        plot_bgcolor='rgba(0,0,0,0)',
        paper_bgcolor='rgba(0,0,0,0)'
    )
    st.plotly_chart(fig_proj, use_container_width=True)
    st.divider()
    
    # ==================== MONTE CARLO SIMULATION ====================
    with st.expander("🎲 Monte Carlo Simulation (Advanced)"):
        st.markdown("Run thousands of random market scenarios to see the range of possible outcomes.")
        
        run_mc = st.button("Run Monte Carlo Simulation (1,000 scenarios)", key="run_mc_btn")
        
        if run_mc:
            with st.spinner("Running 1,000 market scenarios..."):
                np.random.seed(42)
                
                # Historical volatilities
                volatility = {
                    'n26': 0.16, 'raiz': 0.12, 'vanguard': 0.14,
                    'shares': 0.18, 'metals': 0.22, 'super': 0.11,
                }
                monthly_vol = {k: v / (12 ** 0.5) for k, v in volatility.items()}
                
                n_simulations = 1000
                all_endings = []
                
                monthly_returns_used = {
                    'n26': monthly_r['n26'],
                    'raiz': monthly_r['raiz'],
                    'vanguard': monthly_r['vanguard'],
                    'shares': monthly_r['shares'],
                    'metals': monthly_r['metals'],
                    'super': monthly_r['super'],
                }
                
                for sim in range(n_simulations):
                    nw = start_nw
                    n26_v = current_market_value_eur * fx_now
                    raiz_v = raiz_total_aud
                    vdal_v = vanguard_total_aud
                    shares_v = shares_total_aud
                    metals_v = commodities_total_aud
                    super_v = super_total_aud
                    cash_v = cash_total_aud
                    
                    for m in range(1, months + 1):
                        n26_return = np.random.normal(monthly_returns_used['n26'], monthly_vol['n26'])
                        raiz_return = np.random.normal(monthly_returns_used['raiz'], monthly_vol['raiz'])
                        vdal_return = np.random.normal(monthly_returns_used['vanguard'], monthly_vol['vanguard'])
                        shares_return = np.random.normal(monthly_returns_used['shares'], monthly_vol['shares'])
                        metals_return = np.random.normal(monthly_returns_used['metals'], monthly_vol['metals'])
                        super_return = np.random.normal(monthly_returns_used['super'], monthly_vol['super'])
                        
                        n26_v *= (1 + n26_return)
                        raiz_v *= (1 + raiz_return)
                        vdal_v *= (1 + vdal_return)
                        shares_v *= (1 + shares_return)
                        metals_v *= (1 + metals_return)
                        super_v *= (1 + super_return)
                        
                        cash_v += monthly_interest - monthly_expenses + monthly_rent_aud
                        if m % 12 == 0:
                            cash_v -= (monthly_interest * 12 * 0.19 + n26_v * 0.02 * 0.19)
                        
                        nw = n26_v + raiz_v + vdal_v + shares_v + metals_v + super_v + cash_v
                    
                    all_endings.append(nw)
                
                endings_series = pd.Series(all_endings)
                
                col_mc1, col_mc2, col_mc3, col_mc4 = st.columns(4)
                col_mc1.metric("5th Percentile (Worst 5%)", f"${endings_series.quantile(0.05):,.0f}")
                col_mc2.metric("Median (50th Percentile)", f"${endings_series.quantile(0.5):,.0f}")
                col_mc3.metric("95th Percentile (Best 5%)", f"${endings_series.quantile(0.95):,.0f}")
                col_mc4.metric("Probability of Growth", f"{(endings_series > start_nw).mean() * 100:.1f}%")
                
                # Histogram
                fig_hist = go.Figure()
                fig_hist.add_trace(go.Histogram(x=endings_series, nbinsx=50, marker_color='#2980b9', opacity=0.7))
                fig_hist.add_vline(x=start_nw, line_dash="dash", line_color="red", annotation_text="Starting NW")
                fig_hist.add_vline(x=endings_series.quantile(0.5), line_dash="dash", line_color="green", annotation_text="Median")
                fig_hist.update_layout(title="Distribution of 5-Year Outcomes", xaxis_title="Net Worth (AUD)", xaxis_tickprefix="$", height=400)
                st.plotly_chart(fig_hist, use_container_width=True)
    # ── KEY MILESTONES ─────────────────────────────────────────────────────────
    st.markdown("### 🎯 Key Milestones")
    milestones = [500000, 750000, 1000000, 1500000, 2000000, 2500000, 3000000]
    milestone_rows = []
    for m_val in milestones:
        if m_val <= start_nw:
            milestone_rows.append({'Milestone': f"${m_val/1e6:.1f}M AUD", 'Status': '✅ Already achieved', 'ETA': '—'})
        else:
            hit = df_proj[df_proj['Projected NW'] >= m_val]
            if not hit.empty:
                eta = hit.iloc[0]['Date']
                months_away = hit.iloc[0]['Month']
                milestone_rows.append({'Milestone': f"${m_val/1e6:.1f}M AUD", 'Status': '🎯 Projected', 'ETA': eta.strftime('%b %Y'), 'Months Away': int(months_away)})
            else:
                milestone_rows.append({'Milestone': f"${m_val/1e6:.1f}M AUD", 'Status': '⏳ Beyond 5yr', 'ETA': '>2031'})
    st.dataframe(pd.DataFrame(milestone_rows), use_container_width=True, hide_index=True)

    st.divider()

    # ── MONTHLY CASH FLOW SUMMARY ──────────────────────────────────────────────
    st.markdown("### 💵 Monthly Cash Flow Assumptions")
    cf1, cf2, cf3, cf4 = st.columns(4)
    cf1.metric("Spanish Rent", f"${monthly_rent_aud:,.2f} AUD", f"€{new_inputs.get('Income_rent_eur', 500):,.2f} EUR")
    cf2.metric("Cash Interest", f"${monthly_interest:,.2f} AUD/month", f"${monthly_interest*12:,.2f} p.a.")
    cf3.metric("Total Expenses", f"-${monthly_expenses:,.2f} AUD/month")
    cf4.metric("Net Monthly Cash Flow", f"${monthly_net_income:,.2f} AUD", delta_color="normal" if monthly_net_income >= 0 else "inverse", delta=f"${monthly_net_income*12:,.2f} p.a.")

    st.divider()

    # ── YEARLY CASH FLOW & WEALTH GROWTH ───────────────────────────────────────
    st.markdown("### 📈 5-Year Wealth Growth & Cash Flow Analysis")
    st.caption("All numbers below are derived from the same monthly projection shown in the chart above.")

    if not df_proj.empty:
        projection_end_nw = df_proj.iloc[-1]['Projected NW']
        total_projected_growth = projection_end_nw - start_nw

        year_end_data = []
        for year in range(1, 6):
            year_end_month = year * 12
            year_end_row = df_proj[df_proj['Month'] == year_end_month]
            if not year_end_row.empty:
                end_nw = year_end_row['Projected NW'].iloc[0]
                start_nw_year = start_nw if year == 1 else year_end_data[-1]['End NW']
                year_growth = end_nw - start_nw_year

                rental_income_aud = new_inputs.get('Income_rent_eur', 500) * fx_now * 12
                cash_interest_annual = monthly_interest * 12
                annual_expenses = total_expenses * 12
                tax_on_interest = cash_interest_annual * 0.19
                net_cash_flow = rental_income_aud + cash_interest_annual - annual_expenses - tax_on_interest
                unrealized_gains = year_growth - net_cash_flow

                year_end_data.append({
                    'Year': year,
                    'Year Ending': year_end_row['Date'].iloc[0].strftime('%b %Y'),
                    'Start NW': start_nw_year,
                    'End NW': end_nw,
                    'Year Growth': year_growth,
                    'Net Cash Flow': net_cash_flow,
                    'Unrealized Gains': unrealized_gains,
                })

        df_yearly = pd.DataFrame(year_end_data)

        col_s1, col_s2, col_s3, col_s4 = st.columns(4)
        col_s1.metric("Starting Net Worth", f"${start_nw:,.2f}")
        col_s2.metric("Projected Net Worth (Year 5)", f"${projection_end_nw:,.2f}", delta=f"${total_projected_growth:+,.2f}")
        col_s3.metric("Total Net Cash Flow (5 Years)", f"${df_yearly['Net Cash Flow'].sum():,.2f}", delta_color="inverse")
        col_s4.metric("Total Unrealized Gains (5 Years)", f"${df_yearly['Unrealized Gains'].sum():,.2f}")

        st.divider()
        st.markdown("#### 📅 Year-by-Year Breakdown")
        st.dataframe(df_yearly.style.format({
            'Start NW': '${:,.0f}', 'End NW': '${:,.0f}', 'Year Growth': '${:,.0f}',
            'Net Cash Flow': '${:,.0f}', 'Unrealized Gains': '${:,.0f}',
        }), use_container_width=True, hide_index=True)

        st.divider()

        # Stacked bar chart
        fig_consistent = go.Figure()
        fig_consistent.add_trace(go.Bar(name='Net Cash Flow (after tax)', x=df_yearly['Year'].astype(str), y=df_yearly['Net Cash Flow'], marker_color='#e74c3c'))
        fig_consistent.add_trace(go.Bar(name='Unrealized Portfolio Gains', x=df_yearly['Year'].astype(str), y=df_yearly['Unrealized Gains'], marker_color='#27ae60'))
        fig_consistent.update_layout(title="Wealth Increase Breakdown", xaxis_title="Year", yaxis_title="Amount (AUD)", yaxis_tickprefix="$", barmode='stack', height=450)
        st.plotly_chart(fig_consistent, use_container_width=True)

        st.markdown("#### 💡 Summary")
        total_cash = df_yearly['Net Cash Flow'].sum()
        total_gains = df_yearly['Unrealized Gains'].sum()
        col_i1, col_i2, col_i3 = st.columns(3)
        col_i1.info(f"**Cash Flow:** ${total_cash:,.0f} over 5 years")
        col_i2.success(f"**Market Gains:** ${total_gains:,.0f} over 5 years")
        col_i3.info(f"**Total Wealth Increase:** ${total_projected_growth:,.0f} ({total_projected_growth/start_nw*100:.1f}%)")

    st.divider()

    # ── FORECAST vs ACTUALS TABLE ──────────────────────────────────────────────
    st.markdown("### 📋 Forecast vs Actuals")
    if not df_actual.empty:
        df_vs = df_actual.copy()
        df_vs['Month'] = df_vs['Date'].apply(lambda d: round((d - pd.Timestamp(today)).days / 30.44))
        df_vs = df_vs.merge(df_proj[['Month', 'Projected NW']].rename(columns={'Projected NW': 'Projected'}), on='Month', how='left')
        df_vs['Variance ($)'] = df_vs['Total_AUD'] - df_vs['Projected']
        df_vs['Variance (%)'] = (df_vs['Variance ($)'] / df_vs['Projected'] * 100).round(2)
        st.dataframe(df_vs[['Date', 'Total_AUD', 'Projected', 'Variance ($)', 'Variance (%)']].style.format({'Total_AUD': '${:,.2f}', 'Projected': '${:,.2f}', 'Variance ($)': '${:+,.2f}', 'Variance (%)': '{:+.2f}%'}), use_container_width=True, hide_index=True)
    else:
        st.info("No actuals yet — save a net worth snapshot from the Dashboard to start tracking.")
    st.divider()

    st.divider()

    # ── COMBINED CASH REDEPLOYMENT SIMULATOR (AUD Cash → AUD Investments + EUR Cash → N26) ──
    st.markdown("### 💡 Combined Cash Redeployment Simulator")
    st.caption("Simulate moving BOTH AUD cash to AUD investments AND EUR cash to N26 (EUR investment). See the combined 5-year impact. Uses the same return assumptions as your main projection (including Scenario and Sensitivity adjustments).")
    
    # Get current cash balances by currency
    cash_bal_check = load_cash_balances()
    
    # Calculate AUD cash (AUD accounts only)
    aud_cash_total = 0
    for acc in ['CBA', 'Me Bank', 'Rabobank', 'Up']:
        aud_cash_total += cash_bal_check.get(acc, 0.0)
    
    # Calculate EUR cash (EUR accounts only)
    eur_cash_total = 0
    for acc in ['Trade Republic', 'N26', 'BPM Cash', 'BPM Bonds']:
        eur_cash_total += cash_bal_check.get(acc, 0.0)
    eur_cash_aud_total = eur_cash_total * fx_now
    
    st.markdown("### 💰 Available Cash")
    col_aud_disp, col_eur_disp = st.columns(2)
    with col_aud_disp:
        st.metric("🇦🇺 AUD Cash Available", f"${aud_cash_total:,.2f}")
    with col_eur_disp:
        st.metric("🇪🇺 EUR Cash Available", f"€{eur_cash_total:,.2f}", f"≈ ${eur_cash_aud_total:,.2f} AUD")
    
    st.divider()
    
    # ========== SCENARIO 1: AUD Cash → AUD Investment ==========
    st.markdown("### 📊 Scenario 1: AUD Cash → AUD Investment")
    st.caption("Move AUD cash into Australian dollar investments (Raiz, Vanguard, or ASX Shares)")
    
    col_s1_1, col_s1_2, col_s1_3 = st.columns(3)
    with col_s1_1:
        aud_amount = st.number_input(
            "AUD amount to redeploy",
            min_value=0.0,
            max_value=float(aud_cash_total),
            value=min(50000.0, float(aud_cash_total)),
            step=10000.0,
            format="%.0f",
            key="aud_redeploy_amount",
            help=f"Maximum available: ${aud_cash_total:,.2f}"
        )
        st.caption(f"{aud_amount/aud_cash_total*100:.1f}% of available AUD cash" if aud_cash_total > 0 else "")
    
    with col_s1_2:
        aud_target = st.selectbox(
            "AUD investment target",
            options=["Raiz ETFs", "Vanguard VDAL", "ASX Shares"],
            key="aud_target",
            index=0
        )
        # Use the SAME returns as the main projection (including sensitivity and scenario)
        aud_return_map = {
            "Raiz ETFs": new_inputs.get('Returns_raiz_pct', 10.0),
            "Vanguard VDAL": new_inputs.get('Returns_vanguard_pct', 9.5),
            "ASX Shares": new_inputs.get('Returns_shares_pct', 10.0),
        }
        aud_return_pct = aud_return_map[aud_target]
        st.caption(f"Expected return: {aud_return_pct:.2f}% p.a. (including scenario & sensitivity)")
    
    with col_s1_3:
        aud_cash_rate = st.number_input(
            "AUD cash interest rate being forgone (% p.a.)",
            min_value=0.0, max_value=20.0,
            value=5.5,
            step=0.5,
            format="%.2f",
            key="aud_cash_rate",
            help="The interest rate you'd lose on this AUD cash"
        )
    
    st.divider()
    
    # ========== SCENARIO 2: EUR Cash → N26 (EUR Investment) ==========
    st.markdown("### 🌍 Scenario 2: EUR Cash → N26 European ETFs")
    st.caption("Move EUR cash into Euro-denominated investments (N26 portfolio)")
    
    col_s2_1, col_s2_2, col_s2_3 = st.columns(3)
    with col_s2_1:
        eur_amount = st.number_input(
            "EUR amount to redeploy",
            min_value=0.0,
            max_value=float(eur_cash_total),
            value=min(50000.0, float(eur_cash_total)),
            step=10000.0,
            format="%.0f",
            key="eur_redeploy_amount",
            help=f"Maximum available: €{eur_cash_total:,.2f}"
        )
        eur_amount_aud = eur_amount * fx_now
        st.caption(f"≈ ${eur_amount_aud:,.2f} AUD | {eur_amount/eur_cash_total*100:.1f}% of available EUR cash" if eur_cash_total > 0 else "")
    
    with col_s2_2:
        eur_target = "N26 European ETFs"
        st.info(f"**Target:** {eur_target}")
        # Use the SAME N26 return as the main projection (including sensitivity and scenario)
        eur_return_pct = new_inputs.get('Returns_n26_pct', 11.0)
        st.caption(f"Expected return: {eur_return_pct:.2f}% p.a. (including scenario & sensitivity)")
    
    with col_s2_3:
        eur_cash_rate = st.number_input(
            "EUR cash interest rate being forgone (% p.a.)",
            min_value=0.0, max_value=20.0,
            value=2.0,
            step=0.5,
            format="%.2f",
            key="eur_cash_rate",
            help="The interest rate you'd lose on this EUR cash"
        )
    
    st.divider()
    
    # ========== RUN COMBINED SIMULATION ==========
    if st.button("🔄 Run Combined Scenario", type="primary", key="combined_scenario_btn"):
        
        # Calculate monthly rates
        aud_monthly_r = annual_to_monthly(aud_return_pct)
        aud_cash_monthly_r = annual_to_monthly(aud_cash_rate)
        eur_monthly_r = annual_to_monthly(eur_return_pct)
        eur_cash_monthly_r = annual_to_monthly(eur_cash_rate)
        
        # Initialize variables
        aud_invest_val = aud_amount
        aud_invest_cb = aud_amount
        aud_cash_val = aud_amount
        
        eur_invest_val = eur_amount
        eur_invest_cb = eur_amount
        eur_cash_val = eur_amount
        
        aud_gain_pre2027 = 0.0
        eur_gain_pre2027 = 0.0
        
        CGT_TRANS_SIM = pd.Timestamp('2027-07-01')
        
        sim_rows = []
        
        for m in range(1, 61):
            proj_date_sim = pd.Timestamp(today) + pd.DateOffset(months=m)
            is_post_sim = proj_date_sim >= CGT_TRANS_SIM
            
            # AUD Investment Growth
            aud_invest_val *= (1 + aud_monthly_r)
            if not is_post_sim:
                aud_gain_pre2027 = max(0, aud_invest_val - aud_invest_cb)
            
            # AUD Cash Growth
            aud_cash_val *= (1 + aud_cash_monthly_r)
            if m % 12 == 0:
                annual_aud_interest = aud_cash_val * aud_cash_rate / 100
                aud_cash_val -= annual_aud_interest * 0.19
            
            # EUR Investment Growth
            eur_invest_val *= (1 + eur_monthly_r)
            if not is_post_sim:
                eur_gain_pre2027 = max(0, eur_invest_val - eur_invest_cb)
            
            # EUR Cash Growth
            eur_cash_val *= (1 + eur_cash_monthly_r)
            if m % 12 == 0:
                annual_eur_interest = eur_cash_val * eur_cash_rate / 100
                eur_cash_val -= annual_eur_interest * 0.19
            eur_cash_val_aud = eur_cash_val * fx_now
            
            # Calculate After-Tax Values
            # AUD Investment after CGT
            aud_gain_total = max(0, aud_invest_val - aud_invest_cb)
            if not is_post_sim:
                aud_cgt = aud_gain_total * 0.50 * 0.19
            else:
                aud_post_gain = max(0, aud_gain_total - aud_gain_pre2027)
                aud_cgt = (aud_gain_pre2027 * 0.50 * 0.19 + aud_post_gain * max(0.19, 0.30))
            aud_invest_after_cgt = aud_invest_val - aud_cgt
            
            # EUR Investment after CGT
            eur_gain_total = max(0, eur_invest_val - eur_invest_cb)
            if not is_post_sim:
                eur_cgt = eur_gain_total * 0.50 * 0.19
            else:
                eur_post_gain = max(0, eur_gain_total - eur_gain_pre2027)
                eur_cgt = (eur_gain_pre2027 * 0.50 * 0.19 + eur_post_gain * max(0.19, 0.30))
            eur_invest_after_cgt_eur = eur_invest_val - eur_cgt
            eur_invest_after_cgt_aud = eur_invest_after_cgt_eur * fx_now
            
            # Combined values
            total_redeployed_aud = aud_amount + eur_amount_aud
            total_invest_after_cgt = aud_invest_after_cgt + eur_invest_after_cgt_aud
            total_cash_after_tax = aud_cash_val + eur_cash_val_aud
            total_advantage = total_invest_after_cgt - total_cash_after_tax
            
            sim_rows.append({
                'Date': proj_date_sim,
                'Month': m,
                'AUD Investment Value (after CGT)': aud_invest_after_cgt,
                'EUR Investment Value (after CGT)': eur_invest_after_cgt_aud,
                'Total Redeployed Value (after CGT)': total_invest_after_cgt,
                'Total Cash (if kept, after tax)': total_cash_after_tax,
                'Combined Advantage': total_advantage,
            })
        
        df_combined_sim = pd.DataFrame(sim_rows)
        yr5 = df_combined_sim.iloc[-1]
        
        # ========== RESULTS DISPLAY ==========
        st.markdown("### 📊 Combined Scenario Results")
        st.caption(f"Redeploying ${aud_amount:,.0f} AUD → {aud_target} + €{eur_amount:,.0f} EUR → N26 European ETFs")
        
        # Summary metrics
        col_r1, col_r2, col_r3, col_r4 = st.columns(4)
        with col_r1:
            st.metric("Total Redeployed", f"${(aud_amount + eur_amount_aud):,.0f}",
                     help=f"AUD: ${aud_amount:,.0f} + EUR: €{eur_amount:,.0f} (≈${eur_amount_aud:,.0f})")
        with col_r2:
            st.metric("Value if Kept in Cash", f"${yr5['Total Cash (if kept, after tax)']:,.0f}",
                     delta=f"+${yr5['Total Cash (if kept, after tax)'] - (aud_amount + eur_amount_aud):,.0f}")
        with col_r3:
            st.metric("Value if Redeployed", f"${yr5['Total Redeployed Value (after CGT)']:,.0f}",
                     delta=f"+${yr5['Total Redeployed Value (after CGT)'] - (aud_amount + eur_amount_aud):,.0f}",
                     delta_color="normal")
        with col_r4:
            advantage = yr5['Combined Advantage']
            st.metric("Net Advantage", f"${advantage:+,.0f}",
                     delta=f"{(advantage/(aud_amount + eur_amount_aud)*100):+.1f}%",
                     delta_color="normal" if advantage >= 0 else "inverse")
        
        st.divider()
        
        # Chart showing comparison
        st.markdown("#### 📈 5-Year Projection: Redeployed vs Kept in Cash")
        
        fig_combined = go.Figure()
        
        fig_combined.add_trace(go.Scatter(
            x=df_combined_sim['Date'], y=df_combined_sim['Total Redeployed Value (after CGT)'],
            mode='lines', name='💰 Redeployed (AUD Investments + N26)',
            line=dict(color='#27ae60', width=3), fill='tozeroy', fillcolor='rgba(39,174,96,0.1)',
            hovertemplate='Redeployed: $%{y:,.0f}<extra></extra>'
        ))
        
        fig_combined.add_trace(go.Scatter(
            x=df_combined_sim['Date'], y=df_combined_sim['Total Cash (if kept, after tax)'],
            mode='lines', name='🏦 Kept in Cash (after tax)',
            line=dict(color='#e74c3c', width=2.5, dash='dot'),
            fill='tozeroy', fillcolor='rgba(231,76,60,0.05)',
            hovertemplate='Cash: $%{y:,.0f}<extra></extra>'
        ))
        
        fig_combined.add_trace(go.Scatter(
            x=df_combined_sim['Date'], y=df_combined_sim['Combined Advantage'],
            mode='lines', name='📊 Extra Gain',
            line=dict(color='#f39c12', width=2, dash='dash'),
            hovertemplate='Advantage: $%{y:,.0f}<extra></extra>',
            yaxis='y2'
        ))
        
        _cgt_date = pd.Timestamp('2027-07-01')
        fig_combined.add_shape(
            type="line", xref="x", yref="paper",
            x0=_cgt_date, x1=_cgt_date, y0=0, y1=1,
            line=dict(color='#e74c3c', dash='dash', width=1),
            opacity=0.6,
        )
        fig_combined.add_annotation(
            x=_cgt_date, y=1, xref="x", yref="paper",
            text="CGT change 1 Jul 2027", showarrow=False,
            xanchor="left", yanchor="top",
            font=dict(color='#e74c3c'),
        )
        
        fig_combined.update_layout(
            height=450, hovermode='x unified',
            yaxis=dict(title="Value (AUD)", tickprefix="$"),
            yaxis2=dict(title="Extra Gain (AUD)", tickprefix="$", overlaying='y', side='right', showgrid=False),
            legend=dict(orientation="h", y=1.08),
            margin=dict(t=50, b=30)
        )
        
        st.plotly_chart(fig_combined, use_container_width=True)
        
        # Breakdown by currency
        st.markdown("#### 📋 Breakdown by Currency")
        
        col_break1, col_break2 = st.columns(2)
        with col_break1:
            st.markdown("**🇦🇺 AUD Component**")
            st.metric(f"{aud_target}", f"${aud_invest_after_cgt:,.0f}",
                     delta=f"${aud_invest_after_cgt - aud_amount:+,.0f}")
            st.caption(f"vs Cash: ${aud_cash_val:,.0f} (advantage: ${aud_invest_after_cgt - aud_cash_val:+,.0f})")
        
        with col_break2:
            st.markdown("**🇪🇺 EUR Component (N26)**")
            st.metric("N26 European ETFs", f"${eur_invest_after_cgt_aud:,.0f}",
                     delta=f"${eur_invest_after_cgt_aud - eur_amount_aud:+,.0f}")
            st.caption(f"vs Cash: ${eur_cash_val_aud:,.0f} (advantage: ${eur_invest_after_cgt_aud - eur_cash_val_aud:+,.0f})")
        
        # Impact on total net worth
        st.divider()
        st.markdown("#### 💰 Impact on Total 5-Year Net Worth")
        
        current_proj_end = df_proj.iloc[-1]['Projected NW'] if not df_proj.empty else total_nw
        new_proj_end = current_proj_end + advantage
        
        col_imp1, col_imp2, col_imp3 = st.columns(3)
        col_imp1.metric("Base Projected NW (Year 5)", f"${current_proj_end:,.0f}")
        col_imp2.metric("With Redeployment", f"${new_proj_end:,.0f}",
                       delta=f"+${advantage:,.0f}",
                       delta_color="normal" if advantage >= 0 else "inverse")
        col_imp3.metric("Extra Annual Return", 
                       f"{(advantage/5/(aud_amount + eur_amount_aud)*100):+.2f}% p.a.",
                       help="Average annual extra return from redeployment")
        
        # Year-by-year table
        with st.expander("📋 View Year-by-Year Projections"):
            df_display = df_combined_sim[df_combined_sim['Month'] % 12 == 0].copy()
            df_display['Year'] = (df_display['Month'] / 12).astype(int)
            st.dataframe(
                df_display[['Year', 'Date', 'Total Redeployed Value (after CGT)', 'Total Cash (if kept, after tax)', 'Combined Advantage']]
                .style.format({
                    'Total Redeployed Value (after CGT)': '${:,.0f}',
                    'Total Cash (if kept, after tax)': '${:,.0f}',
                    'Combined Advantage': '${:+,.0f}',
                })
                .map(lambda v: 'color: #27ae60' if isinstance(v, (int, float)) and v > 0 and 'Advantage' in str(v) else ('color: #e74c3c' if isinstance(v, (int, float)) and v < 0 and 'Advantage' in str(v) else '')),
                use_container_width=True,
                hide_index=True
            )
    else:
        st.info("👆 Click 'Run Combined Scenario' to see the 5-year impact of redeploying both AUD and EUR cash.")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 11 — DATA ENTRY
# ══════════════════════════════════════════════════════════════════════════════
with tab11:
    st.header("📝 Data Entry — N26 & Raiz Transactions")
    st.caption("Add, edit, or delete rows directly. Changes save to Postgres when you click Save.")
    st.warning("⚠️ New feature — test with a dummy row first before relying on it for real entries.")

    st.markdown("### 🇪🇺 N26 Transactions")
    df_n26_edit_orig = load_transactions_for_editor(N26_ACCOUNT_ID, symbol_prefix="")
    if df_n26_edit_orig.empty:
        df_n26_edit_orig = pd.DataFrame(columns=['id', 'Date', 'Symbol', 'Type', 'Quantity', 'Price', 'Amount', 'Notes'])
    df_n26_edited = st.data_editor(
        df_n26_edit_orig,
        column_config={
            "id": None,
            "Date": st.column_config.DateColumn("Date", required=True),
            "Symbol": st.column_config.TextColumn("ISIN", required=True, help="e.g. IE00B3RBWM25"),
            "Type": st.column_config.SelectboxColumn("Type", options=["BUY", "SELL"], required=True),
            "Quantity": st.column_config.NumberColumn("Quantity", min_value=0.0, format="%.6f", required=True),
            "Price": st.column_config.NumberColumn("Price (€)", min_value=0.0, format="%.4f"),
            "Amount": st.column_config.NumberColumn("Amount (€)", min_value=0.0, format="%.2f",
                                                      help="Leave blank to auto-calculate as Quantity × Price"),
            "Notes": st.column_config.TextColumn("Notes"),
        },
        num_rows="dynamic", use_container_width=True, hide_index=True, key="n26_editor",
    )
    if st.button("💾 Save N26 Changes", type="primary", key="save_n26_edits"):
        ok, err = sync_transaction_edits(N26_ACCOUNT_ID, "", "EUR", "ETF", df_n26_edit_orig, df_n26_edited)
        if ok:
            st.success("✅ N26 transactions saved.")
            load_transactions_for_editor.clear()
            load_n26_transactions.clear()
            st.rerun()
        else:
            st.error(f"Could not save: {err}")

    st.divider()

    st.markdown("### 🌱 Raiz — Bulk CSV Upload")
    st.caption("Upload your Raiz Trade Statement CSV. Expects columns: Trade Date, Instrument Code, Transaction Type, Quantity, Price, Amount. Already-imported rows are detected and skipped automatically.")
    _raiz_csv_upload = st.file_uploader("Raiz Trade Statement CSV", type="csv", key="raiz_bulk_csv")

    if _raiz_csv_upload is not None:
        try:
            _df_bulk = pd.read_csv(_raiz_csv_upload)
            _df_bulk.columns = [c.strip() for c in _df_bulk.columns]
            required_cols = {'Trade Date', 'Instrument Code', 'Transaction Type', 'Quantity', 'Price', 'Amount'}
            missing = required_cols - set(_df_bulk.columns)
            if missing:
                st.error(f"CSV is missing required columns: {', '.join(missing)}")
            else:
                _df_bulk['Trade Date'] = pd.to_datetime(_df_bulk['Trade Date'], dayfirst=True, errors='coerce')
                _df_bulk['Quantity'] = pd.to_numeric(_df_bulk['Quantity'], errors='coerce')
                _df_bulk['Price'] = pd.to_numeric(_df_bulk['Price'], errors='coerce')
                _df_bulk['Amount'] = pd.to_numeric(_df_bulk['Amount'], errors='coerce')
                _df_bulk = _df_bulk.dropna(subset=['Trade Date', 'Instrument Code', 'Quantity'])
                _df_bulk['Transaction Type'] = _df_bulk['Transaction Type'].str.upper().str.strip()
                _df_bulk['Instrument Code'] = _df_bulk['Instrument Code'].str.strip()

                _existing_raiz = load_transactions_for_editor(RAIZ_ACCOUNT_ID, symbol_prefix="RAIZ:")
                if not _existing_raiz.empty:
                    _existing_keys = set(zip(
                        pd.to_datetime(_existing_raiz['Date']).dt.date,
                        _existing_raiz['Symbol'].str.strip(),
                        _existing_raiz['Type'].str.upper(),
                        _existing_raiz['Quantity'].abs().round(6)
                    ))
                else:
                    _existing_keys = set()

                _df_bulk['_key'] = list(zip(
                    _df_bulk['Trade Date'].dt.date,
                    _df_bulk['Instrument Code'],
                    _df_bulk['Transaction Type'],
                    _df_bulk['Quantity'].abs().round(6)
                ))
                _df_bulk['Already Imported'] = _df_bulk['_key'].isin(_existing_keys)

                st.write(f"Found {len(_df_bulk)} rows — "
                         f"{(~_df_bulk['Already Imported']).sum()} new, "
                         f"{_df_bulk['Already Imported'].sum()} already imported (will be skipped).")
                st.dataframe(
                    _df_bulk[['Trade Date', 'Instrument Code', 'Transaction Type', 'Quantity', 'Price', 'Amount', 'Already Imported']],
                    use_container_width=True, hide_index=True
                )

                _df_new = _df_bulk[~_df_bulk['Already Imported']]

                if st.button(f"📥 Import {len(_df_new)} New Transactions", type="primary", key="raiz_bulk_import_btn"):
                    if _df_new.empty:
                        st.info("Nothing new to import — all rows already exist.")
                    else:
                        try:
                            symbol_to_id = {}
                            for code in _df_new['Instrument Code'].unique():
                                full_symbol = f"RAIZ:{code}"
                                symbol_to_id[code] = get_or_create_instrument(
                                    full_symbol, display_name=full_symbol,
                                    asset_class="ETF", native_currency="AUD"
                                )
                            conn = get_pg()
                            inserted = 0
                            with conn.session as s:
                                for _, row in _df_new.iterrows():
                                    qty_signed = -abs(row['Quantity']) if row['Transaction Type'] == 'SELL' else abs(row['Quantity'])
                                    s.execute(
                                        sql_text("""
                                            INSERT INTO transactions
                                                (account_id, instrument_id, tx_date, tx_type, quantity, price, amount, notes, processed)
                                            VALUES
                                                (:account_id, :instrument_id, :tx_date, :tx_type, :quantity, :price, :amount, :notes, true)
                                        """),
                                        {"account_id": RAIZ_ACCOUNT_ID,
                                         "instrument_id": symbol_to_id[row['Instrument Code']],
                                         "tx_date": row['Trade Date'].date(),
                                         "tx_type": row['Transaction Type'].lower(),
                                         "quantity": qty_signed,
                                         "price": row['Price'] if pd.notnull(row['Price']) else None,
                                         "amount": abs(row['Amount']) if pd.notnull(row['Amount']) else 0.0,
                                         "notes": "[bulk_csv_import]"}
                                    )
                                    inserted += 1
                                s.commit()
                            st.success(f"✅ Imported {inserted} new transactions.")
                            load_transactions_for_editor.clear()
                            _load_raiz_csv_raw.clear()
                            st.rerun()
                        except Exception as e:
                            import traceback
                            st.error(f"Import failed: {traceback.format_exc()}")
        except Exception as e:
            st.error(f"Could not read CSV: {e}")

    st.divider()

    st.markdown("### 🌱 Raiz Transactions")
    df_raiz_edit_orig = load_transactions_for_editor(RAIZ_ACCOUNT_ID, symbol_prefix="RAIZ:")
    if df_raiz_edit_orig.empty:
        df_raiz_edit_orig = pd.DataFrame(columns=['id', 'Date', 'Symbol', 'Type', 'Quantity', 'Price', 'Amount', 'Notes'])
    df_raiz_edited = st.data_editor(
        df_raiz_edit_orig,
        column_config={
            "id": None,
            "Date": st.column_config.DateColumn("Date", required=True),
            "Symbol": st.column_config.SelectboxColumn("ETF Code",
                                                          options=["AAA", "STW", "IAA", "IEU", "IAF", "RCB", "IVV"],
                                                          required=True),
            "Type": st.column_config.SelectboxColumn("Type", options=["BUY", "SELL"], required=True),
            "Quantity": st.column_config.NumberColumn("Quantity", min_value=0.0, format="%.6f", required=True),
            "Price": st.column_config.NumberColumn("Price ($)", min_value=0.0, format="%.4f"),
            "Amount": st.column_config.NumberColumn("Amount ($)", min_value=0.0, format="%.2f",
                                                      help="Leave blank to auto-calculate as Quantity × Price"),
            "Notes": st.column_config.TextColumn("Notes"),
        },
        num_rows="dynamic", use_container_width=True, hide_index=True, key="raiz_editor",
    )
    if st.button("💾 Save Raiz Changes", type="primary", key="save_raiz_edits"):
        ok, err = sync_transaction_edits(RAIZ_ACCOUNT_ID, "RAIZ:", "AUD", "ETF", df_raiz_edit_orig, df_raiz_edited)
        if ok:
            st.success("✅ Raiz transactions saved.")
            load_transactions_for_editor.clear()
            _load_raiz_csv_raw.clear()
            st.rerun()
        else:
            st.error(f"Could not save: {err}")

    st.divider()

    @st.cache_data(ttl=0)
    def load_dividends_for_editor():
        conn = get_pg()
        return conn.query(
            """
            SELECT div_date AS "Date", portfolio AS "Portfolio",
                   amount AS "Amount", currency AS "Currency",
                   processed AS "Counted in Snapshot",
                   (transaction_id IS NOT NULL) AS "Linked to Cash"
            FROM dividends
            ORDER BY div_date DESC
            """,
            ttl=0,
        )

    st.markdown("### 💰 Record Dividend")
    st.caption(
        "Recording a dividend does two things at once: adds it to the relevant "
        "cash account balance immediately, and logs it for Net Worth attribution "
        "so it shows as 'Dividends' rather than a Contribution on your next snapshot."
    )

    div_col1, div_col2, div_col3 = st.columns(3)
    with div_col1:
        div_date = st.date_input("Dividend Date", value=date.today(), key="div_date_input")
        div_portfolio = st.selectbox("Source Portfolio", options=["N26", "Shares"], key="div_portfolio")
    with div_col2:
        div_currency = st.selectbox("Currency", options=["EUR", "AUD", "USD"], key="div_currency")
        div_amount = st.number_input(f"Amount ({div_currency})", min_value=0.0, step=1.0,
                                       format="%.2f", key="div_amount")
    with div_col3:
        div_dest_options = list(CASH_ACCOUNTS.keys())
        default_dest = "N26" if div_portfolio == "N26" and "N26" in div_dest_options else div_dest_options[0]
        div_dest_account = st.selectbox(
            "Deposited Into", options=div_dest_options,
            index=div_dest_options.index(default_dest) if default_dest in div_dest_options else 0,
            key="div_dest_account"
        )

    if st.button("💾 Record Dividend", type="primary", key="save_dividend_btn"):
        if div_amount <= 0:
            st.warning("Enter an amount greater than zero.")
        else:
            try:
                dest_acc_id, dest_currency = CASH_ACCOUNTS[div_dest_account]
                if div_currency == dest_currency:
                    amount_in_dest_ccy = div_amount
                elif div_currency == "EUR" and dest_currency == "AUD":
                    amount_in_dest_ccy = div_amount * fx_now
                elif div_currency == "USD":
                    try:
                        usd_aud = 1 / float(yf.Ticker("AUDUSD=X").fast_info['last_price'])
                    except:
                        usd_aud = 1.58
                    amount_in_dest_ccy = div_amount * (usd_aud if dest_currency == "AUD" else usd_aud / fx_now)
                else:
                    amount_in_dest_ccy = div_amount  # fallback, same-currency assumption

                conn = get_pg()
                with conn.session as s:
                    tx_result = s.execute(
                        sql_text("""
                            INSERT INTO transactions
                                (account_id, tx_date, tx_type, amount, fx_rate_to_aud, notes, processed)
                            VALUES
                                (:account_id, :tx_date, 'deposit', :amount, :fx_rate, :notes, true)
                            RETURNING id
                        """),
                        {
                            "account_id": dest_acc_id,
                            "tx_date": div_date,
                            "amount": amount_in_dest_ccy,
                            "fx_rate": fx_now if dest_currency == "EUR" else 1.0,
                            "notes": f"[dividend:{div_portfolio}] {div_amount:.2f} {div_currency} received",
                        }
                    )
                    new_tx_id = tx_result.fetchone()[0]

                    s.execute(
                        sql_text("""
                            INSERT INTO dividends
                                (div_date, portfolio, amount, currency, processed, transaction_id, account_id)
                            VALUES
                                (:div_date, :portfolio, :amount, :currency, false, :transaction_id, :account_id)
                        """),
                        {
                            "div_date": div_date,
                            "portfolio": div_portfolio,
                            "amount": div_amount,
                            "currency": div_currency,
                            "transaction_id": new_tx_id,
                            "account_id": dest_acc_id,
                        }
                    )
                    s.commit()

                st.success(f"✅ Dividend recorded: {div_amount:.2f} {div_currency} from {div_portfolio} → {div_dest_account}")
                load_cash_balances.clear()
                get_cash_total_for_dashboard.clear()
                load_dividends_for_editor.clear()
                st.rerun()
            except Exception as e:
                import traceback
                st.error(f"Could not record dividend: {traceback.format_exc()}")

    st.divider()
    st.markdown("### 📋 Dividends Entered")

    df_div_view = load_dividends_for_editor()
    if df_div_view.empty:
        st.info("No dividends recorded yet.")
    else:
        st.dataframe(
            df_div_view.style.format({
                "Date": lambda x: pd.to_datetime(x).strftime('%Y-%m-%d'),
                "Amount": "{:.2f}",
            }),
            use_container_width=True, hide_index=True
        )
        total_unprocessed = df_div_view[~df_div_view["Counted in Snapshot"]]
        if not total_unprocessed.empty:
            st.caption(
                f"⏳ {len(total_unprocessed)} dividend(s) not yet counted in a snapshot — "
                f"they'll be picked up next time you click 'Save Snapshot Now' on the Dashboard."
            )
