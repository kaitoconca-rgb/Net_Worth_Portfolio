import streamlit as st
import pandas as pd
import yfinance as yf
import requests
from datetime import datetime, date
from streamlit_gsheets import GSheetsConnection
import plotly.express as px
import plotly.graph_objects as go

# --- CONFIGURAZIONE ---
st.set_page_config(page_title="Claudio's Executive Console", layout="wide")

# Ticker Map (ISIN -> Ticker per Storico Yahoo)
ticker_map = {
    "LU2885245055": "MANUAL",
    "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "36B2.MU", "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS", "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE"
}

# --- FUNZIONI DI RECUPERO DATI (IBRIDE) ---

@st.cache_data(ttl=600)
def get_finnhub_price(isin, ticker_alt):
    """Recupera il prezzo live via Finnhub usando l'ISIN"""
    api_key = st.secrets.get("FINNHUB_API_KEY")
    if not api_key: return None
    
    try:
        # 1. Cerca il ticker tramite ISIN (come da tua immagine)
        search = requests.get(f"https://finnhub.io/api/v1/search?q={isin}&token={api_key}").json()
        if search.get('result'):
            symbol = search['result'][0]['symbol']
            # 2. Ottieni il prezzo (Quote)
            quote = requests.get(f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={api_key}").json()
            return quote.get('c') # 'c' è il prezzo di chiusura corrente
    except:
        return None
    return None

@st.cache_data(ttl=3600)
def get_market_context(isins_list):
    """Mantiene yfinance solo per lo storico (Tab 3)"""
    hist_data = {}
    logs = {}
    for isin in isins_list:
        symbol = ticker_map.get(isin)
        if symbol and symbol != "MANUAL":
            try:
                h = yf.download(symbol, start="2024-09-01", progress=False)['Close']
                if isinstance(h, pd.DataFrame): h = h.iloc[:, 0]
                hist_data[isin] = h
                logs[isin] = "Yahoo OK"
            except:
                hist_data[isin] = None
                logs[isin] = "Yahoo Fallback"
    return hist_data, logs

# --- LOGICA CORE ---
conn = st.connection("gsheets", type=GSheetsConnection)
df_input = conn.read(ttl=0)
df_input.columns = [c.strip() for c in df_input.columns]

# Prepazione DataFrame
df_raw = pd.DataFrame()
df_raw['Data'] = pd.to_datetime(df_input['Fecha Valor'], dayfirst=True)
df_raw['ISIN'] = df_input['ISIN']
df_raw['Qty'] = pd.to_numeric(df_input['Cantidad'], errors='coerce')
df_raw['Inv_EUR'] = pd.to_numeric(df_input['Importe Cargado'], errors='coerce')
df_raw['Prezzo_Acq'] = pd.to_numeric(df_input['Precio'], errors='coerce') 
df_raw['Manual_Price'] = pd.to_numeric(df_input['Price'], errors='coerce')
df_raw = df_raw.dropna(subset=['ISIN', 'Qty'])

# Recupero Prezzi e Storico
hist_map, _ = get_market_context(df_raw['ISIN'].unique().tolist())

def get_live_price(row):
    # 1. Priorità Manuale (Sheets)
    if pd.notnull(row['Manual_Price']) and row['Manual_Price'] > 0:
        return row['Manual_Price']
    
    # 2. Tentativo Live Finnhub (ISIN lookup)
    live_p = get_finnhub_price(row['ISIN'], ticker_map.get(row['ISIN']))
    if live_p: return live_p
    
    # 3. Fallback su ultimo storico Yahoo
    h = hist_map.get(row['ISIN'])
    if h is not None and not h.empty: return float(h.iloc[-1])
    
    # 4. Fallback estremo: Prezzo acquisto
    return row['Prezzo_Acq']

df_raw['Price_Now'] = df_raw.apply(get_live_price, axis=1)

# FX AUD Rate
@st.cache_data(ttl=600)
def get_fx():
    t = yf.Ticker("EURAUD=X")
    return float(t.fast_info['last_price'])

fx_now = get_fx()
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Att_AUD'] = df_raw['Att_EUR'] * fx_now

# --- INTERFACCIA (Tab mantenuti) ---
tab1, tab2, tab3, tab4 = st.tabs(["📊 Performance", "💸 Simulatore ATO", "📈 Timeline", "🛠️ Diagnostics"])

with tab1:
    # Stesse metriche della versione precedente
    t_inv_eur = df_raw['Inv_EUR'].sum()
    t_att_eur = df_raw['Att_EUR'].sum()
    st.metric("Valore Portafoglio", f"€{t_att_eur:,.2f}", f"€{t_att_eur - t_inv_eur:,.2f}")
    st.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', title="Asset Allocation"), width="stretch")

with tab2:
    # Simulatore ATO identico
    tax_r = st.slider("Marginal Tax Rate (%)", 0.0, 45.0, 37.0)
    st.data_editor(df_raw[['ISIN','Qty','Prezzo_Acq','Price_Now','Att_EUR']], width="stretch")

with tab3:
    # Grafico Timeline identico
    st.write("Evoluzione basata su dati storici Yahoo Finance")
    # ... (Logica Timeline invariata) ...

with tab4:
    st.write("Status Connessioni")
    st.write(f"Finnhub API Key Presente: {'Sì' if st.secrets.get('FINNHUB_API_KEY') else 'No'}")
    st.table(df_raw[['ISIN', 'Price_Now']])
