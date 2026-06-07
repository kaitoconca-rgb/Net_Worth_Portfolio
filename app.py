import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, date
import time
from streamlit_gsheets import GSheetsConnection

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
        # Cash accounts only — Super, Vanguard, Metals removed
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

# ── GLOBAL SAVE FUNCTIONS ─────────────────────────────────────────────────────
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
            spreadsheetId="1ad1wkw7fUdKO-Kq5869JYPsldS_Xr3A0T0W9YLcQKe8",
            range="Cash!A1",
            valueInputOption="RAW",
            body={"values": rows}
        ).execute()
        return True, None
    except Exception as e:
        import traceback
        return False, traceback.format_exc()

def save_net_worth_snapshot(total, force=False):
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
        result = service.spreadsheets().values().get(
            spreadsheetId="1ad1wkw7fUdKO-Kq5869JYPsldS_Xr3A0T0W9YLcQKe8",
            range="Net_Worth!A:B"
        ).execute()
        existing = result.get('values', [['Date', 'Total_AUD']])
        today = date.today()
        if not force and len(existing) > 1:
            try:
                last_date = pd.to_datetime(existing[-1][0])
                if last_date.year == today.year and last_date.month == today.month:
                    return False, "Already saved this month"
            except:
                pass
        existing.append([today.strftime('%Y-%m-%d'), str(round(total, 2))])
        service.spreadsheets().values().update(
            spreadsheetId="1ad1wkw7fUdKO-Kq5869JYPsldS_Xr3A0T0W9YLcQKe8",
            range="Net_Worth!A1",
            valueInputOption="RAW",
            body={"values": existing}
        ).execute()
        return True, None
    except Exception as e:
        import traceback
        return False, traceback.format_exc()

@st.cache_data(ttl=60)
def load_net_worth_history():
    try:
        conn_nw = st.connection("gsheets_networth", type=GSheetsConnection)
        df_nw = conn_nw.read(ttl=60)
        df_nw.columns = [c.strip() for c in df_nw.columns]
        df_nw['Date'] = pd.to_datetime(df_nw['Date'])
        df_nw['Total_AUD'] = pd.to_numeric(df_nw['Total_AUD'], errors='coerce')
        return df_nw.dropna()
    except:
        return pd.DataFrame(columns=['Date', 'Total_AUD'])

# --- 4. INTERFACCIA ---
(tab0, tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8, tab9) = st.tabs([
    "🌐 Dashboard",
    "📊 N26 Performance",
    "💸 N26 Simulatore ATO",
    "📈 N26 Timeline",
    "💱 N26 FX Analysis",
    "🌱 Raiz & Vanguard",
    "🪙 Commodities",
    "🏛️ Super",
    "🏦 Cash",
    "🛠️ Diagnostics"
])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 0 — DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════
with tab0:
    st.header("🌐 Net Worth Dashboard")
    european_aud = current_market_value_eur * fx_now
    raiz_vanguard_aud = raiz_total_aud + vanguard_total_aud
    total_net_worth_aud = european_aud + raiz_vanguard_aud + commodities_total_aud + super_total_aud + cash_total_aud
    total_net_worth_eur = total_net_worth_aud / fx_now if fx_now else 0

    st.markdown("### Total Net Worth")
    c1, c2 = st.columns(2)
    c1.metric("Total (AUD)", f"${total_net_worth_aud:,.2f}")
    c2.metric("Total (EUR)", f"€{total_net_worth_eur:,.2f}")
    st.divider()

    st.markdown("### Breakdown")
    b1, b2, b3, b4, b5 = st.columns(5)
    b1.metric("🇪🇺 N26 European", f"${european_aud:,.2f}", f"€{current_market_value_eur:,.2f}")
    b2.metric("🌱 Raiz & Vanguard", f"${raiz_vanguard_aud:,.2f}",
              f"Raiz ${raiz_total_aud:,.0f} + VDAL ${vanguard_total_aud:,.0f}")
    b3.metric("🪙 Commodities", f"${commodities_total_aud:,.2f}", "Metals (XAU/XAG/XPT)")
    b4.metric("🏛️ Super", f"${super_total_aud:,.2f}", "Mercer SmartPath")
    b5.metric("🏦 Cash & Savings", f"${cash_total_aud:,.2f}")
    st.divider()

    st.markdown("### Asset Allocation")
    col_pie, col_bar = st.columns(2)
    df_alloc = pd.DataFrame([
        {"Category": "🇪🇺 N26 European", "Value (AUD)": european_aud},
        {"Category": "🌱 Raiz & Vanguard", "Value (AUD)": raiz_vanguard_aud},
        {"Category": "🪙 Commodities", "Value (AUD)": commodities_total_aud},
        {"Category": "🏛️ Super", "Value (AUD)": super_total_aud},
        {"Category": "🏦 Cash & Savings", "Value (AUD)": cash_total_aud},
    ])
    colours = ["#2980b9", "#27ae60", "#f39c12", "#8e44ad", "#e67e22"]
    with col_pie:
        fig_alloc_pie = px.pie(df_alloc, values="Value (AUD)", names="Category", hole=0.45,
                               color_discrete_sequence=colours)
        fig_alloc_pie.update_layout(height=350, margin=dict(t=20, b=20))
        st.plotly_chart(fig_alloc_pie, use_container_width=True)
    with col_bar:
        fig_alloc_bar = px.bar(df_alloc, x="Category", y="Value (AUD)", color="Category",
                               color_discrete_sequence=colours,
                               labels={"Value (AUD)": "AUD $"})
        fig_alloc_bar.update_layout(height=350, showlegend=False, yaxis_tickprefix="$", margin=dict(t=20, b=20))
        st.plotly_chart(fig_alloc_bar, use_container_width=True)
    st.divider()

    st.markdown("### Net Worth History")
    today = date.today()
    import calendar
    last_day = calendar.monthrange(today.year, today.month)[1]
    if today.day == last_day:
        ok, err = save_net_worth_snapshot(total_net_worth_aud, force=False)
        if ok:
            st.success(f"✅ Monthly snapshot saved: ${total_net_worth_aud:,.2f} AUD")
    df_history = load_net_worth_history()
    if not df_history.empty:
        fig_history = go.Figure()
        fig_history.add_trace(go.Scatter(
            x=df_history['Date'], y=df_history['Total_AUD'],
            mode='lines+markers', name='Net Worth (AUD)',
            line=dict(color='#2980b9', width=2), marker=dict(size=8),
            fill='tozeroy', fillcolor='rgba(41,128,185,0.08)'
        ))
        fig_history.update_layout(height=350, hovermode="x unified",
                                   yaxis=dict(title="AUD $", tickprefix="$"), margin=dict(t=20, b=30))
        st.plotly_chart(fig_history, use_container_width=True)
    else:
        st.info("Net worth history will appear here after the first end-of-month snapshot.")
    if st.button("💾 Save Snapshot Now", key="dashboard_snapshot_btn"):
        ok, err = save_net_worth_snapshot(total_net_worth_aud, force=True)
        if ok:
            st.success(f"✅ Snapshot saved: ${total_net_worth_aud:,.2f} AUD")
            st.rerun()
        else:
            st.error(f"Could not save: {err}")

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

    @st.cache_data(ttl=300)
    def load_raiz_csv():
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaIoBaseDownload
            import io
            gs = st.secrets["gdrive"]
            creds_dict = {
                "type": gs.get("type", "service_account"), "project_id": gs["project_id"],
                "private_key_id": gs["private_key_id"], "private_key": gs["private_key"],
                "client_email": gs["client_email"], "client_id": gs.get("client_id", ""),
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
                return None, None, "No CSV files found"
            latest = files[0]
            request = service.files().get_media(fileId=latest["id"])
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            buffer.seek(0)
            df = pd.read_csv(buffer)
            return df, f"{latest['name']} • {latest['modifiedTime'][:10]}", None
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
        IVV_SPLIT_DATE = pd.Timestamp('2022-12-09')
        IVV_SPLIT_FACTOR = 15.317277
        ivv_mask = df_csv['Instrument Code'] == 'IVV'
        pre_split_mask = ivv_mask & (df_csv['Trade Date'] < IVV_SPLIT_DATE)
        df_csv.loc[pre_split_mask, 'Quantity'] = df_csv.loc[pre_split_mask, 'Quantity'] * IVV_SPLIT_FACTOR
        df_csv.loc[pre_split_mask, 'Price'] = df_csv.loc[pre_split_mask, 'Price'] / IVV_SPLIT_FACTOR
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

        @st.cache_data(ttl=300)
        def get_raiz_live_prices(codes_tuple):
            prices = {}
            for code in codes_tuple:
                ticker = RAIZ_TICKER_MAP.get(code)
                if not ticker:
                    prices[code] = None
                    continue
                try:
                    t = yf.Ticker(ticker)
                    p = t.fast_info.get('last_price', None)
                    if p and float(p) > 0:
                        prices[code] = float(p)
                        continue
                except: pass
                try:
                    h = yf.download(ticker, period='5d', progress=False)['Close']
                    if isinstance(h, pd.DataFrame): h = h.iloc[:, 0]
                    h = h.dropna()
                    if not h.empty:
                        prices[code] = float(h.iloc[-1])
                        continue
                except: pass
                prices[code] = None
            return prices

        live_prices_raiz = get_raiz_live_prices(tuple(holdings_raiz['Instrument Code'].unique()))

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

    @st.cache_data(ttl=300)
    def load_vanguard_data():
        try:
            df_v = _sheets_read(PORTFOLIO_SHEET_ID, "Vanguard!A:E")
            if df_v.empty:
                return pd.DataFrame(), None
            df_v.columns = [c.strip() for c in df_v.columns]
            return df_v, None
        except Exception as e:
            return None, str(e)

    df_vdal, vdal_error = load_vanguard_data()

    if vdal_error:
        st.error(f"Could not load Vanguard data: {vdal_error}")
    elif df_vdal is not None and not df_vdal.empty:
        df_vdal['Date'] = pd.to_datetime(df_vdal['Date'], dayfirst=True)
        df_vdal['Quantity'] = pd.to_numeric(df_vdal['Quantity'], errors='coerce').fillna(0)
        df_vdal['Purchase Price'] = pd.to_numeric(df_vdal['Purchase Price'], errors='coerce')
        df_vdal.loc[df_vdal['Transaction'].str.upper() == 'SELL', 'Quantity'] = -df_vdal['Quantity'].abs()
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

    # ── COMBINED SUMMARY ──────────────────────────────────────────────────────
    st.subheader("📊 Combined Raiz + Vanguard Summary")
    combined_value = raiz_total_aud + vanguard_total_aud
    df_combined = pd.DataFrame([
        {"Portfolio": "🌱 Raiz ETFs", "Value (AUD)": raiz_total_aud},
        {"Portfolio": "📈 Vanguard VDAL", "Value (AUD)": vanguard_total_aud},
    ])
    cs1, cs2 = st.columns(2)
    with cs1:
        fig_combined = px.pie(df_combined, values="Value (AUD)", names="Portfolio", hole=0.4,
                              title=f"Total: ${combined_value:,.2f} AUD",
                              color_discrete_sequence=["#27ae60", "#2ecc71"])
        fig_combined.update_layout(height=300)
        st.plotly_chart(fig_combined, use_container_width=True)
    with cs2:
        st.metric("Raiz", f"${raiz_total_aud:,.2f}")
        st.metric("Vanguard VDAL", f"${vanguard_total_aud:,.2f}")
        st.metric("Combined Total", f"${combined_value:,.2f}", delta=f"€{combined_value/fx_now:,.2f} EUR equiv.")

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

    # Metal ticker map — Yahoo futures prices are per troy oz in USD
    # Revolut quantities: Gold in troy oz, Silver in troy oz, Platinum in troy oz
    METAL_CONFIG = {
        'Gold':     {'ticker': 'GC=F',  'symbol': 'XAU', 'unit': 'troy oz', 'colour': '#f39c12'},
        'Silver':   {'ticker': 'SI=F',  'symbol': 'XAG', 'unit': 'troy oz', 'colour': '#95a5a6'},
        'Platinum': {'ticker': 'PL=F',  'symbol': 'XPT', 'unit': 'troy oz', 'colour': '#8e44ad'},
    }

    @st.cache_data(ttl=300)
    def get_metal_prices():
        prices = {}
        for metal, cfg in METAL_CONFIG.items():
            try:
                t = yf.Ticker(cfg['ticker'])
                p = float(t.fast_info['last_price'])
                prices[metal] = {'usd': p, 'aud': p * usd_to_aud}
            except:
                prices[metal] = {'usd': None, 'aud': None}
        return prices

    @st.cache_data(ttl=300)
    def load_metal_data():
        try:
            df_m = _sheets_read(PORTFOLIO_SHEET_ID, "Metal!A:F")
            if df_m.empty:
                return pd.DataFrame(), None
            df_m.columns = [c.strip() for c in df_m.columns]
            return df_m, None
        except Exception as e:
            return None, str(e)

    @st.cache_data(ttl=3600)
    def get_hist_fx_rate(from_currency, to_currency, dt_str):
        if from_currency == to_currency:
            return 1.0
        ticker = f"{from_currency}{to_currency}=X"
        try:
            hist = yf.download(ticker, start=dt_str, period="5d", progress=False)['Close']
            if isinstance(hist, pd.DataFrame): hist = hist.iloc[:, 0]
            hist = hist.dropna()
            if not hist.empty:
                return float(hist.iloc[0])
        except:
            pass
        try:
            return float(yf.Ticker(ticker).fast_info['last_price'])
        except:
            return 1.0

    def convert_purchase_to_aud(total_cost, currency, date_str):
        currency = str(currency).strip().upper()
        if currency in ('AUD', 'A$'):
            return total_cost
        elif currency == 'BTC':
            return total_cost * get_hist_fx_rate('BTC', 'AUD', date_str)
        elif currency in ('CAD', 'CA$'):
            return total_cost * get_hist_fx_rate('CAD', 'AUD', date_str)
        elif currency in ('NOK', 'SEK', 'DKK', 'KR'):
            for kr in ['NOK', 'SEK', 'DKK']:
                rate = get_hist_fx_rate(kr, 'AUD', date_str)
                if 0.10 < rate < 0.25:
                    return total_cost * rate
            return total_cost * get_hist_fx_rate('SEK', 'AUD', date_str)
        elif currency == 'EUR':
            return total_cost * get_hist_fx_rate('EUR', 'AUD', date_str)
        elif currency == 'USD':
            return total_cost * get_hist_fx_rate('USD', 'AUD', date_str)
        else:
            return total_cost * get_hist_fx_rate(currency, 'AUD', date_str)

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

        st.info(f"USD/AUD: {usd_to_aud:.4f} | EUR/AUD: {fx_now:.4f} — Purchase prices converted to AUD at historical rates")

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
                                {m['Net Qty (troy oz)']:.4f} troy oz
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
                                Live: {m['Live Price (USD)']} USD/oz
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

# ══════════════════════════════════════════════════════════════════════════════
# TAB 7 — SUPER
# ══════════════════════════════════════════════════════════════════════════════
with tab7:
    st.header("🏛️ Superannuation — Mercer SmartPath (Born 1969–1973)")
    st.caption("Manual balance — Mercer Super does not publish a public unit price feed. Update when you receive your statement.")

    # Load current super balance from Cash sheet
    def load_super_balance():
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
                conn_c2 = st.connection("gsheets_cash", type=GSheetsConnection)
                df_c2 = conn_c2.read(ttl=0, usecols=[0, 1])
                df_c2.columns = [c.strip() for c in df_c2.columns]
                df_c2 = df_c2.dropna(subset=['Account'])
                df_c2['Balance'] = pd.to_numeric(df_c2['Balance'], errors='coerce').fillna(0)
                all_balances = df_c2.set_index('Account')['Balance'].to_dict()
                all_balances['Super'] = new_super_balance
                ok, err = save_cash_balances(all_balances)
                if ok:
                    st.success(f"✅ Super balance updated to ${new_super_balance:,.2f}")
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

    def load_cash_balances():
        try:
            conn_cash = st.connection("gsheets_cash", type=GSheetsConnection)
            df = conn_cash.read(ttl=0, usecols=[0, 1])
            df.columns = [c.strip() for c in df.columns]
            df = df.dropna(subset=['Account'])
            df['Balance'] = pd.to_numeric(df['Balance'], errors='coerce').fillna(0)
            return df.set_index('Account')['Balance'].to_dict()
        except Exception as e:
            st.warning(f"Could not load cash balances: {e}")
            return {a["name"]: 0.0 for a in ACCOUNTS}

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
    st.write(f"**FX EUR/AUD Live:** {fx_now:.4f}")
    st.write(f"**FX USD/AUD:** {get_usd_aud():.4f}")

    st.subheader("N26 European Portfolio — Price Feed Status")
    st.table(pd.DataFrame.from_dict(diag_logs, orient='index'))

    st.subheader("Metal Prices")
    metal_diag = []
    for metal, cfg in METAL_CONFIG.items():
        p = metal_prices.get(metal, {})
        metal_diag.append({
            "Metal": metal, "Ticker": cfg['ticker'],
            "Price (USD)": f"${p['usd']:,.2f}" if p.get('usd') else "N/A",
            "Price (AUD)": f"${p['aud']:,.2f}" if p.get('aud') else "N/A",
            "Status": "🟢 LIVE" if p.get('usd') else "🔴 FALLBACK"
        })
    st.table(pd.DataFrame(metal_diag))

    st.subheader("Vanguard VDAL")
    try:
        t = yf.Ticker("VDAL.AX")
        vdal_p = float(t.fast_info['last_price'])
        st.success(f"🟢 VDAL.AX: ${vdal_p:.4f} AUD")
    except:
        st.error("🔴 VDAL.AX price unavailable")
