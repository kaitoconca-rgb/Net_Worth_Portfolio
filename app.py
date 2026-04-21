import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
import pytz
from streamlit_gsheets import GSheetsConnection

# --- 0. PROTEZIONE PASSWORD ---
def check_password():
    def password_guessed():
        if st.session_state["password"] == st.secrets["auth"]["password"]:
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

# --- 1. CONFIGURAZIONE & MAPPING (RIPRISTINATO MILANO) ---
st.set_page_config(page_title="Executive Portfolio Console", layout="wide")

ticker_map = {
    "LU2885245055": "8OU9.DE",
    "IE0032077012": "EQQQ.DE",
    "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE",
    "IE00BFM15T99": "SJP6.DE",
    "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS",
    "IE00B3VVMM84": "VFEM.DE",
    "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI", # RIPRISTINATO ticker precedente
    "IE0005042456": "IUSA.DE"
}

@st.cache_data(ttl=600)
def get_fx_rate():
    try:
        t = yf.Ticker("EURAUD=X")
        val = t.fast_info['last_price']
        return float(val) if val else 1.6450
    except: return 1.6450

# --- 2. CARICAMENTO DATI ---
conn = st.connection("gsheets", type=GSheetsConnection)
df_input = conn.read(ttl=0)
df_input.columns = [c.strip() for c in df_input.columns]

df_raw = pd.DataFrame()
df_raw['Data'] = df_input['Fecha Valor']
df_raw['ISIN'] = df_input['ISIN']
df_raw['Qty'] = pd.to_numeric(df_input['Cantidad'], errors='coerce')
df_raw['Inv_EUR'] = pd.to_numeric(df_input['Importe Cargado'], errors='coerce')
df_raw['Prezzo_Acq'] = pd.to_numeric(df_input['Precio'], errors='coerce') 
df_raw['Manual_Override'] = pd.to_numeric(df_input['Price'], errors='coerce')

df_raw = df_raw.dropna(subset=['ISIN', 'Qty'])
df_raw['Date_DT'] = pd.to_datetime(df_raw['Data'], dayfirst=True)

# --- 3. LOGICA PREZZI LIVE CON ANALISI RITARDO ---
ticker_diag = {}

def fetch_live_price_diag(isin, manual_val):
    symbol = ticker_map.get(isin)
    now_utc = datetime.now(pytz.utc)
    
    if pd.notnull(manual_val) and manual_val > 0:
        ticker_diag[isin] = {"status": "MANUALE", "delay": "0 min", "msg": "Priorità Sheet"}
        return float(manual_val)
    
    if not symbol: return None
    
    try:
        t = yf.Ticker(symbol)
        f_info = t.fast_info
        current = f_info['last_price']
        last_market_time = f_info.get('last_market_time')
        
        delay_str = "N/D"
        status = "LIVE"
        
        if last_market_time:
            diff = now_utc - last_market_time.astimezone(pytz.utc)
            minutes = int(diff.total_seconds() / 60)
            if minutes > 1440:
                delay_str = f"{minutes // 1440} giorni"
                status = "FERMO"
            elif minutes > 60:
                delay_str = f"{minutes // 60} ore"
                status = "FERMO"
            else:
                delay_str = f"{minutes} min"
                status = "LIVE" if minutes < 30 else "RITARDO"

        ticker_diag[isin] = {"status": status, "delay": delay_str, "msg": f"Ticker: {symbol}"}
        return float(current) if current else None
    except:
        if isin == "IE00BFM15T99": 
            ticker_diag[isin] = {"status": "FIX", "delay": "N/D", "msg": "Emergency 7.02"}
            return 7.02
        ticker_diag[isin] = {"status": "ERRORE", "delay": "∞", "msg": "Errore Connessione"}
        return None

market_fx = get_fx_rate()
fx_hist = yf.download("EURAUD=X", start="2025-09-01", progress=False)['Close']

with st.spinner("Riconnessione mercati..."):
    prices_now = []
    cache_prezzi = {}
    for _, row in df_raw.iterrows():
        isin = row['ISIN']
        if isin not in cache_prezzi:
            cache_prezzi[isin] = fetch_live_price_diag(isin, row['Manual_Override'])
        p = cache_prezzi[isin] if cache_prezzi[isin] else float(row['Prezzo_Acq'])
        prices_now.append(p)

df_raw['Price_Now'] = prices_now
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['Date_DT'].apply(lambda x: fx_hist.asof(x) if not fx_hist.empty else 1.63)
df_raw['Att_AUD'] = df_raw['Att_EUR'] * market_fx

# --- 4. INTERFACCIA ---
st.title("🏛️ Claudio's Portfolio Command Center")

tab1, tab2, tab3, tab4 = st.tabs(["📊 Performance", "💸 Simulatore Tasse", "📈 Storico", "🛠️ System Logs"])

with tab1:
    t_inv_eur, t_att_eur = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    st.metric("Valore Reale Portafoglio (€)", f"€{t_att_eur:,.2f}", f"€{(t_att_eur - t_inv_eur):,.2f}")
    
    st_agg = df_raw.groupby('ISIN').agg({'Qty':'sum','Inv_EUR':'sum','Att_EUR':'sum'}).reset_index()
    st_agg['Gain (€)'] = st_agg['Att_EUR'] - st_agg['Inv_EUR']
    st.dataframe(st_agg.style.format(precision=2), use_container_width=True, hide_index=True)

with tab4:
    st.subheader("🛠️ Monitoraggio Flussi Dati (Ticker Ripristinati)")
    
    diag_list = []
    for isin, info in ticker_diag.items():
        diag_list.append({
            "ISIN": isin,
            "Stato": info['status'],
            "Ritardo": info['delay'],
            "Prezzo": f"{cache_prezzi.get(isin):.2f} €" if cache_prezzi.get(isin) else "N/A",
            "Dettaglio": info['msg']
        })
    
    df_diag = pd.DataFrame(diag_list)

    def style_row(row):
        color = ''
        if row.Stato == 'LIVE': color = 'background-color: #d4edda; color: black'
        elif row.Stato == 'FERMO': color = 'background-color: #fff3cd; color: black'
        elif row.Stato == 'ERRORE': color = 'background-color: #f8d7da; color: black'
        return [color] * len(row)

    st.table(df_diag.style.apply(style_row, axis=1))
    
    now_syd = datetime.now(pytz.timezone('Australia/Sydney'))
    st.divider()
    st.caption(f"Ultimo Refresh: {now_syd.strftime('%H:%M:%S')} (Sydney)")
