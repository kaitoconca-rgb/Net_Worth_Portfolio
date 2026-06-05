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
    eur_aud = 1 / fx_data['Close'].iloc[-1] if not fx_data.empty else 1.65
except:
    eur_aud = 1.65

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
        if isinstance(hist, pd.DataFrame): 
            hist = hist.iloc[:, 0]
        return now, hist
    except: 
        return 1.6500, None

fx_now, fx_hist = get_fx_data()

# --- 2. DATI ---
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
    try: 
        return float(fx_hist.asof(dt))
    except: 
        return 1.6500

df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['Data'].apply(get_fx_at)

# --- 3. PREZZI E STORICO ---
@st.cache_data(ttl=3600)
def get_full_market_context(isins_list, current_ticker_map):
    prices_hist = {}
    logs = {}
    for isin in isins_list:
        symbol = current_ticker_map.get(isin)
        try:
            h = yf.download(symbol, start="2025-10-01", progress=False)['Close']
            if isinstance(h, pd.DataFrame): 
                h = h.iloc[:, 0]
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
        if val > 0: 
            return val
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
    
    # Safe division for FX calculations
    buy_mask = asset_data['Tipo'] == 'BUY'
    inv_eur_buy_sum = asset_data[buy_mask]['Inv_EUR'].sum()
    if inv_eur_buy_sum != 0:
        fx_acquisto = asset_data[buy_mask]['Inv_AUD'].sum() / inv_eur_buy_sum
    else:
        fx_acquisto = 0
        
    sell_mask = asset_data['Tipo'] == 'SELL'
    inv_eur_sell_sum = asset_data[sell_mask]['Inv_EUR'].abs().sum()
    if inv_eur_sell_sum != 0:
        fx_vendita = asset_data[sell_mask]['Inv_AUD'].abs().sum() / inv_eur_sell_sum
    else:
        fx_vendita = 0
        
    cash_in_eur = asset_data[sell_mask]['Inv_EUR'].abs().sum()
    cash_out_eur = asset_data[buy_mask]['Inv_EUR'].sum()
    profit_eur = (v_at_market_eur + cash_in_eur) - cash_out_eur
    cash_in_aud = asset_data[sell_mask]['Inv_AUD'].abs().sum()
    cash_out_aud = asset_data[buy_mask]['Inv_AUD'].sum()
    v_at_market_aud = v_at_market_eur * fx_now 
    profit_aud = (v_at_market_aud + cash_in_aud) - cash_out_aud
    asset_performance.append({
        'ISIN': isin, 'Profit_EUR': profit_eur, 'Profit_AUD': profit_aud,
        'Current_Value': v_at_market_eur, 'Data Acquisto': data_acquisto,
        'Data Vendita': data_vendita, 'FX Acquisto': fx_acquisto, 'FX Vendita': fx_vendita
    })

df_perf = pd.DataFrame(asset_performance)

# vendite_effettuate calculation removed as it wasn't used
# The df_dettaglio_vendite variable has been removed

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

# ── CASH TOTAL (hoisted for dashboard) ───────────────────────────────────────
@st.cache_data(ttl=0)
def get_cash_total_for_dashboard():
    try:
        ACCOUNTS_CURR = {
            "CBA": "AUD", "Me Bank": "AUD", "Rabobank": "AUD",
            "Up": "AUD", "Vanguard ETF": "AUD", "Revolut Metals": "AUD",
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
tab0, tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
    "🌐 Dashboard", "📊 Performance", "💸 Simulatore ATO", "📈 Timeline",
    "💱 FX Analysis", "🌱 Raiz", "🏦 Cash", "🛠️ Diagnostics"
])

with tab0:
    st.header("🌐 Net Worth Dashboard")
    european_aud = current_market_value_eur * fx_now
    total_net_worth_aud = european_aud + raiz_total_aud + cash_total_aud
    total_net_worth_eur = total_net_worth_aud / fx_now if fx_now else 0
    st.markdown("### Total Net Worth")
    c1, c2 = st.columns(2)
    c1.metric("Total (AUD)", f"${total_net_worth_aud:,.2f}")
    c2.metric("Total (EUR)", f"€{total_net_worth_eur:,.2f}")
    st.divider()
    st.markdown("### Breakdown")
    b1, b2, b3 = st.columns(3)
    b1.metric("🇪🇺 European Portfolio", f"${european_aud:,.2f}", f"€{current_market_value_eur:,.2f}")
    b2.metric("🌱 Raiz Portfolio", f"${raiz_total_aud:,.2f}")
    b3.metric("🏦 Cash & Savings", f"${cash_total_aud:,.2f}")
    st.divider()
    st.markdown("### Asset Allocation")
    col_pie, col_bar = st.columns(2)
    df_alloc = pd.DataFrame([
        {"Category": "🇪🇺 European Portfolio", "Value (AUD)": european_aud},
        {"Category": "🌱 Raiz Portfolio", "Value (AUD)": raiz_total_aud},
        {"Category": "🏦 Cash & Savings", "Value (AUD)": cash_total_aud},
    ])
    with col_pie:
        fig_alloc_pie = px.pie(df_alloc, values="Value (AUD)", names="Category", hole=0.45,
                               color_discrete_sequence=["#2980b9", "#27ae60", "#f39c12"])
        fig_alloc_pie.update_layout(height=350, margin=dict(t=20, b=20))
        st.plotly_chart(fig_alloc_pie, use_container_width=True)
    with col_bar:
        fig_alloc_bar = px.bar(df_alloc, x="Category", y="Value (AUD)", color="Category",
                               color_discrete_sequence=["#2980b9", "#27ae60", "#f39c12"],
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
        st.info("Net worth history will appear here after the first end-of-month snapshot. Use the button below to save manually.")
    if st.button("💾 Save Snapshot Now", key="dashboard_snapshot_btn", help="Manually record today's net worth"):
        ok, err = save_net_worth_snapshot(total_net_worth_aud, force=True)
        if ok:
            st.success(f"✅ Snapshot saved: ${total_net_worth_aud:,.2f} AUD")
            st.rerun()
        else:
            st.error(f"Could not save: {err}")

with tab1:
    st.header("Performance Complessiva")
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
        with st.expander("Visualizza Dettaglio Posizioni Chiuse (es. LU)"):
            st.write("Questi asset sono stati venduti completamente. Il profitto è già consolidato.")
            st.dataframe(df_realized[['ISIN', 'Profit_EUR', 'Profit_AUD']].style.format(
                {'Profit_EUR': '€{:,.2f}', 'Profit_AUD': '${:,.2f}'}),
                hide_index=True, use_container_width=True)

# The rest of the tabs (tab2 through tab7) remain unchanged
# [tab2, tab3, tab4, tab5, tab6, tab7 code continues as before...]
