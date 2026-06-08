import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, date, timedelta
import time
from streamlit_gsheets import GSheetsConnection
import calendar

# --- FETCH CURRENT FX RATE ---
try:
    fx_data = yf.Ticker("EURAUD=X").history(period="1d")
    FX_AUD_EUR = 1 / fx_data['Close'].iloc[-1]
except:
    FX_AUD_EUR = 0.61

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

if not check_password():
    st.stop()

# --- 1. CONFIGURAZIONE ---
st.set_page_config(page_title="Claudio's Executive Console", layout="wide")

ticker_map = {
    "LU2885245055": "8OU9.DE", "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "36B2.MU", "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS", "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE"
}

@st.cache_data(ttl=600)
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
conn = st.connection("gsheets", type=GSheetsConnection)
df_input = conn.read(ttl=0)
df_input.columns = [c.strip() for c in df_input.columns]

df_raw = pd.DataFrame()
df_raw['Data'] = pd.to_datetime(df_input['Fecha Valor'], dayfirst=True)
df_raw['ISIN'] = df_input['ISIN']
df_raw['Tipo'] = df_input['Tipo'].str.upper().fillna('BUY')
df_raw['Qty'] = pd.to_numeric(df_input['Cantidad'], errors='coerce')
df_raw['Inv_EUR'] = pd.to_numeric(df_input['Importe Cargado'], errors='coerce')
df_raw['Prezzo_Acq'] = pd.to_numeric(df_input['Precio'], errors='coerce')
df_raw['Manual_Price'] = pd.to_numeric(df_input['Price'], errors='coerce')
df_raw.loc[df_raw['Tipo'] == 'SELL', 'Qty'] = -df_raw['Qty'].abs()
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

# ── RAIZ TOTAL (hoisted for dashboard) ───────────────────────────────────────
@st.cache_data(ttl=300)
def get_raiz_total_for_dashboard():
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload
        import io
        gs = st.secrets["gdrive"]
        creds_dict = {
            "type": gs.get("type", "service_account"),
            "project_id": gs["project_id"],
            "private_key_id": gs["private_key_id"],
            "private_key": gs["private_key"],
            "client_email": gs["client_email"],
            "client_id": gs.get("client_id", ""),
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        creds = service_account.Credentials.from_service_account_info(
            creds_dict, scopes=["https://www.googleapis.com/auth/drive.readonly"])
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        folder_id = st.secrets["gdrive"]["raiz_folder_id"]
        results = service.files().list(
            q=f"'{folder_id}' in parents and mimeType='text/csv' and trashed=false",
            orderBy="modifiedTime desc", pageSize=1, fields="files(id, name, modifiedTime)"
        ).execute()
        files = results.get("files", [])
        if not files:
            return 0.0
        request = service.files().get_media(fileId=files[0]["id"])
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        buffer.seek(0)
        df = pd.read_csv(buffer)
        df.columns = [c.strip() for c in df.columns]
        df['Trade Date'] = pd.to_datetime(df['Trade Date'], dayfirst=True)
        df['Quantity'] = pd.to_numeric(df['Quantity'], errors='coerce')
        df['Price'] = pd.to_numeric(df['Price'], errors='coerce')
        df['Amount'] = pd.to_numeric(df['Amount'], errors='coerce')
        IVV_SPLIT_DATE = pd.Timestamp('2022-12-09')
        IVV_SPLIT_FACTOR = 15.317277
        ivv_pre = (df['Instrument Code'] == 'IVV') & (df['Trade Date'] < IVV_SPLIT_DATE)
        df.loc[ivv_pre, 'Quantity'] = df.loc[ivv_pre, 'Quantity'] * IVV_SPLIT_FACTOR
        df.loc[ivv_pre, 'Price'] = df.loc[ivv_pre, 'Price'] / IVV_SPLIT_FACTOR
        df.loc[df['Transaction Type'] == 'SELL', 'Quantity'] = -df['Quantity'].abs()
        RAIZ_TICKERS = {
            'AAA': 'AAA.AX', 'STW': 'STW.AX', 'IAA': 'IAA.AX',
            'IEU': 'IEU.AX', 'IAF': 'IAF.AX', 'RCB': 'RCB.AX', 'IVV': 'IVV.AX'
        }
        holdings = df.groupby('Instrument Code')['Quantity'].sum().reset_index()
        holdings = holdings[holdings['Quantity'].abs() > 0.0001]
        total = 0.0
        for _, row in holdings.iterrows():
            code = row['Instrument Code']
            ticker = RAIZ_TICKERS.get(code)
            price = None
            if ticker:
                try:
                    t = yf.Ticker(ticker)
                    p = t.fast_info.get('last_price', None)
                    if p and float(p) > 0:
                        price = float(p)
                except:
                    pass
            if not price:
                recent = df[df['Instrument Code'] == code].sort_values('Trade Date', ascending=False)
                price = float(recent.iloc[0]['Price']) if not recent.empty else 0.0
            total += row['Quantity'] * price
        return total
    except:
        return 0.0

raiz_total_aud = get_raiz_total_for_dashboard()

# ── VANGUARD TOTAL (hoisted for dashboard) ────────────────────────────────────
@st.cache_data(ttl=300)
def get_vanguard_total_for_dashboard():
    try:
        df_v = _sheets_read(PORTFOLIO_SHEET_ID, "Vanguard!A:E")
        if df_v.empty:
            return 0.0
        df_v.columns = [c.strip() for c in df_v.columns]
        df_v['Quantity'] = pd.to_numeric(df_v['Quantity'], errors='coerce').fillna(0)
        df_v.loc[df_v['Transaction'].str.upper() == 'SELL', 'Quantity'] = -df_v['Quantity'].abs()
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

@st.cache_data(ttl=300)
def get_shares_data():
    try:
        df_s = _sheets_read(PORTFOLIO_SHEET_ID, "Shares!A:B")
        if df_s.empty:
            return pd.DataFrame(), 0.0
        df_s.columns = [c.strip() for c in df_s.columns]
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

# ── COMMODITIES TOTAL (hoisted for dashboard) ─────────────────────────────────
@st.cache_data(ttl=300)
def get_commodities_total_for_dashboard():
    try:
        df_m = _sheets_read(PORTFOLIO_SHEET_ID, "Metal!A:E")
        if df_m.empty:
            return 0.0
        df_m.columns = [c.strip() for c in df_m.columns]
        df_m['Quantity'] = pd.to_numeric(df_m['Quantity'], errors='coerce').fillna(0)
        df_m.loc[df_m['Transaction'].str.upper() == 'SELL', 'Quantity'] = -df_m['Quantity'].abs()

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
            "CBA": "AUD", "Me Bank": "AUD", "Rabobank": "AUD",
            "Up": "AUD",
            "Trade Republic": "EUR", "N26": "EUR", "BUNQ": "EUR",
            "BPM Cash": "EUR", "BPM Bonds": "EUR",
            "C6 Cash": "BRL", "C6 Investments": "BRL",
        }
        conn_c = st.connection("gsheets_cash", type=GSheetsConnection)
        df_c = conn_c.read(ttl=0, usecols=[0, 1])
        df_c.columns = [c.strip() for c in df_c.columns]
        df_c = df_c.dropna(subset=['Account'])
        df_c['Balance'] = pd.to_numeric(df_c['Balance'], errors='coerce').fillna(0)
        bal = df_c.set_index('Account')['Balance'].to_dict()
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
        conn_c = st.connection("gsheets_cash", type=GSheetsConnection)
        df_c = conn_c.read(ttl=0, usecols=[0, 1])
        df_c.columns = [c.strip() for c in df_c.columns]
        df_c = df_c.dropna(subset=['Account'])
        df_c['Balance'] = pd.to_numeric(df_c['Balance'], errors='coerce').fillna(0)
        bal = df_c.set_index('Account')['Balance'].to_dict()
        return float(bal.get("Super", 0.0))
    except:
        return 0.0

super_total_aud = get_super_total_for_dashboard()

# ==================== NEW CONTRIBUTION TRACKING FUNCTIONS ====================

@st.cache_data(ttl=300)
def get_raiz_transactions():
    """Get Raiz transaction history for contribution tracking"""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload
        import io
        gs = st.secrets["gdrive"]
        creds_dict = {
            "type": gs.get("type", "service_account"),
            "project_id": gs["project_id"],
            "private_key_id": gs["private_key_id"],
            "private_key": gs["private_key"],
            "client_email": gs["client_email"],
            "client_id": gs.get("client_id", ""),
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        creds = service_account.Credentials.from_service_account_info(
            creds_dict, scopes=["https://www.googleapis.com/auth/drive.readonly"])
        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        folder_id = st.secrets["gdrive"]["raiz_folder_id"]
        results = service.files().list(
            q=f"'{folder_id}' in parents and mimeType='text/csv' and trashed=false",
            orderBy="modifiedTime desc", pageSize=1, fields="files(id, name, modifiedTime)"
        ).execute()
        files = results.get("files", [])
        if not files:
            return pd.DataFrame()
        request = service.files().get_media(fileId=files[0]["id"])
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        buffer.seek(0)
        df = pd.read_csv(buffer)
        df.columns = [c.strip() for c in df.columns]
        df['Trade Date'] = pd.to_datetime(df['Trade Date'], dayfirst=True)
        df['Amount'] = pd.to_numeric(df['Amount'], errors='coerce')
        return df
    except:
        return pd.DataFrame()

@st.cache_data(ttl=300)
def get_vanguard_transactions():
    """Get Vanguard transaction history"""
    try:
        df_v = _sheets_read(PORTFOLIO_SHEET_ID, "Vanguard!A:F")
        if df_v.empty:
            return pd.DataFrame()
        df_v.columns = [c.strip() for c in df_v.columns]
        df_v['Date'] = pd.to_datetime(df_v['Date'], dayfirst=True)
        df_v['Amount'] = pd.to_numeric(df_v['Amount'], errors='coerce').fillna(0)
        return df_v
    except:
        return pd.DataFrame()

@st.cache_data(ttl=300)
def get_commodities_transactions():
    """Get commodities transaction history"""
    try:
        df_m = _sheets_read(PORTFOLIO_SHEET_ID, "Metal!A:F")
        if df_m.empty:
            return pd.DataFrame()
        df_m.columns = [c.strip() for c in df_m.columns]
        df_m['Date'] = pd.to_datetime(df_m['Date'], dayfirst=True)
        df_m['Purchase Price'] = pd.to_numeric(df_m['Purchase Price'], errors='coerce').fillna(0)
        df_m['Quantity'] = pd.to_numeric(df_m['Quantity'], errors='coerce').fillna(0)
        return df_m
    except:
        return pd.DataFrame()

def calculate_contributions_period(start_date, end_date):
    """Calculate contributions across all asset classes between two dates"""
    breakdown = {}
    total = 0.0
    
    # 1. N26 Contributions
    try:
        n26_contrib = df_raw[
            (df_raw['Tipo'] == 'BUY') & 
            (df_raw['Data'].dt.date >= start_date) & 
            (df_raw['Data'].dt.date <= end_date)
        ].copy()
        n26_total = 0.0
        for _, row in n26_contrib.iterrows():
            fx_rate = get_fx_at(row['Data'])
            n26_total += row['Inv_EUR'] * fx_rate
        breakdown['N26 European'] = n26_total
        total += n26_total
    except:
        breakdown['N26 European'] = 0.0
    
    # 2. Raiz Contributions
    try:
        raiz_df = get_raiz_transactions()
        if not raiz_df.empty and 'Transaction Type' in raiz_df.columns:
            raiz_period = raiz_df[
                (raiz_df['Trade Date'].dt.date >= start_date) & 
                (raiz_df['Trade Date'].dt.date <= end_date) &
                (raiz_df['Transaction Type'].str.upper().str.strip() == 'DEPOSIT')
            ]
            raiz_total = raiz_period['Amount'].sum()
            breakdown['Raiz'] = raiz_total
            total += raiz_total
    except:
        breakdown['Raiz'] = 0.0
    
    # 3. Vanguard Contributions
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
    
    # 4. Commodities Contributions
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
    
    # 5. Cash Contributions (estimate based on balance changes)
    try:
        # Simplified - you can enhance this with actual cash transaction tracking
        breakdown['Cash & Super'] = 0.0  # Placeholder
    except:
        breakdown['Cash & Super'] = 0.0
    
    return total, breakdown

def calculate_fx_impact_period(start_date, end_date):
    """Calculate FX impact on net worth from currency movements"""
    try:
        start_fx = get_fx_at(pd.Timestamp(start_date))
        end_fx = get_fx_at(pd.Timestamp(end_date))
        
        # Get European portfolio value at start
        date_obj = pd.Timestamp(start_date)
        snapshot = df_raw[df_raw['Data'] <= date_obj].groupby('ISIN')['Qty'].sum()
        european_start = 0.0
        for isin in df_raw['ISIN'].unique():
            qty = snapshot.get(isin, 0)
            if abs(qty) < 0.001:
                continue
            h = hist_map.get(isin)
            p = None
            if h is not None and not h.empty:
                try:
                    p = h.asof(date_obj)
                except:
                    p = None
            if p is None or pd.isna(p) or p == 0:
                ledger_price = df_raw[df_raw['ISIN'] == isin]['Prezzo_Acq'].dropna()
                p = ledger_price.iloc[0] if not ledger_price.empty else 0
            european_start += float(qty * p)
        
        european_end = current_market_value_eur
        avg_european_exposure = (european_start + european_end) / 2
        fx_impact = avg_european_exposure * (end_fx - start_fx)
        return fx_impact
    except:
        return 0.0

# ── ENHANCED SAVE FUNCTIONS ─────────────────────────────────────────────────────
def save_cash_balances(balances_dict):
    try:
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
        }, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        rows = [["Account", "Balance"]] + [[k, v] for k, v in balances_dict.items()]
        service.spreadsheets().values().update(
            spreadsheetId=PORTFOLIO_SHEET_ID,
            range="Cash!A1",
            valueInputOption="RAW",
            body={"values": rows}
        ).execute()
        return True, None
    except Exception as e:
        import traceback
        return False, traceback.format_exc()

def save_net_worth_snapshot(total_aud, force=False):
    """Saves net worth snapshot with performance attribution"""
    try:
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
        }, scopes=["https://www.googleapis.com