import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
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

# --- 1. CONFIGURAZIONE & MAPPING OTTIMIZZATO ---
st.set_page_config(page_title="Executive Portfolio Console", layout="wide")

# Ticker ottimizzati per stabilità su Yahoo Finance
ticker_map = {
    "LU2885245055": "8OU9.DE",
    "IE0032077012": "EQQQ.DE",
    "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE",
    "IE00BFM15T99": "SJP6.F",    # Japan - Francoforte Floor
    "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS",
    "IE00B3VVMM84": "VFEM.DE",
    "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.L",    # Global Quality Div - Londra
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

# --- 3. LOGICA PREZZI LIVE ROBUSTA ---
unique_errors = set()

def fetch_live_price(isin, manual_val):
    if pd.notnull(manual_val) and manual_val > 0:
        return float(manual_val)
    
    symbol = ticker_map.get(isin)
    if not symbol: return None

    try:
        t = yf.Ticker(symbol)
        price = t.fast_info['last_price']
        if price and not pd.isna(price):
            return float(price)
        hist = t.history(period="1d")
        if not hist.empty:
            return float(hist['Close'].iloc[-1])
    except:
        pass
    return None

market_fx = get_fx_rate()
fx_hist = yf.download("EURAUD=X", start="2025-09-01", progress=False)['Close']

with st.spinner("Sincronizzazione avanzata mercati..."):
    prices_now = []
    cache_prezzi = {}
    
    for _, row in df_raw.iterrows():
        isin = row['ISIN']
        if isin not in cache_prezzi:
            p = fetch_live_price(isin, row['Manual_Override'])
            cache_prezzi[isin] = p
        
        current_p = cache_prezzi[isin]
        if current_p is None:
            current_p = float(row['Prezzo_Acq'])
            unique_errors.add(isin)
        prices_now.append(current_p)

df_raw['Price_Now'] = prices_now
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Gain_EUR'] = df_raw['Att_EUR'] - df_raw['Inv_EUR']
df_raw['FX_Acq'] = df_raw['Date_DT'].apply(lambda x: fx_hist.asof(x) if not fx_hist.empty else 1.63)
df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['FX_Acq']
df_raw['Att_AUD'] = df_raw['Att_EUR'] * market_fx
df_raw['Gain_AUD'] = df_raw['Att_AUD'] - df_raw['Inv_AUD']

# --- 4. INTERFACCIA ---
st.title("🏛️ Claudio's Portfolio Command Center")

tab1, tab2, tab3, tab4 = st.tabs(["📊 Performance", "💸 Simulatore Tasse", "📈 Storico", "🛠️ System Logs"])

with tab1:
    # Riepilogo Globale
    t_inv_eur, t_att_eur = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    t_inv_aud, t_att_aud = df_raw['Inv_AUD'].sum(), df_raw['Att_AUD'].sum()
    t_gain_eur, t_gain_aud = t_att_eur - t_inv_eur, t_att_aud - t_inv_aud

    st.subheader("Riepilogo Globale Portafoglio")
    summary_df = pd.DataFrame({
        "Metrica": ["Total Invested", "Total Value", "Gain / Loss", "ROI %"],
        "EURO (€)": [f"€{t_inv_eur:,.2f}", f"€{t_att_eur:,.2f}", f"€{t_gain_eur:,.2f}", f"{(t_gain_eur/t_inv_
