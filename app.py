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
    elif not st.session_state["password_correct"]:
        st.text_input("Inserisci Password", type="password", on_change=password_guessed, key="password")
        st.error("😕 Password errata")
        return False
    return True

if not check_password():
    st.stop()

# --- 1. CONFIGURAZIONE & TICKERS ---
st.set_page_config(page_title="Executive Portfolio Console", layout="wide")

ticker_map = {
    "LU2885245055": "X062.DE", # Amundi MSCI World - Ticker stabile
    "IE0032077012": "EQQQ.DE",
    "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE",
    "IE00BFM15T99": "SJPD.AS",
    "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS",
    "IE00B3VVMM84": "VFEM.DE",
    "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI",
    "IE0005042456": "IUSA.DE"
}

# --- 2. FUNZIONI DATI ---
@st.cache_data(ttl=600)
def get_live_data(isin):
    ticker = ticker_map.get(isin)
    if not ticker: return 0.0
    try:
        data = yf.download(ticker, period="5d", progress=False)
        if not data.empty:
            if isinstance(data.columns, pd.MultiIndex): data.columns = data.columns.get_level_values(0)
            return float(data['Close'].iloc[-1])
    except: pass
    return 0.0

@st.cache_data(ttl=600)
def get_fx_rate():
    try:
        data = yf.download("EURAUD=X", period="5d", progress=False)
        if isinstance(data.columns, pd.MultiIndex): data.columns = data.columns.get_level_values(0)
        return float(data['Close'].iloc[-1])
    except: return 1.6450

# --- 3. CARICAMENTO E LOGICA PREZZI ---
conn = st.connection("gsheets", type=GSheetsConnection)
df_input = conn.read(ttl=0)
df_input.columns = [c.strip() for c in df_input.columns]

df_raw = pd.DataFrame()
df_raw['Data'] = df_input['Fecha Valor']
df_raw['ISIN'] = df_input['ISIN']
df_raw['Qty'] = pd.to_numeric(df_input['Cantidad'], errors='coerce')
df_raw['Inv_EUR'] = pd.to_numeric(df_input['Importe Cargado'], errors='coerce')
df_raw['Prezzo_Acq'] = pd.to_numeric(df_input['Precio'], errors='coerce') # Storico da 'Precio'

df_raw = df_raw.dropna(subset=['ISIN', 'Qty'])
df_raw['Date_DT'] = pd.to_datetime(df_raw['Data'], dayfirst=True)

# Logica Override (Price) vs Live
manual_prices = pd.to_numeric(df_input['Price'], errors='coerce')
market_fx = get_fx_rate()
fx_hist = yf.download("EURAUD=X", start="2025-09-01", progress=False)['Close']

prices_now = []
for i, row in df_raw.iterrows():
    if i < len(manual_prices) and pd.notnull(manual_prices[i]) and manual_prices[i] > 0:
        prices_now.append(float(manual_prices[i]))
    else:
        val = get_live_data(row['ISIN'])
        if val <= 0: # Fallback anti-zero
            val = float(row['Prezzo_Acq']) if pd.notnull(row['Prezzo_Acq']) else 10.0
        prices_now.append(val)

df_raw['Price_Now'] = prices_now

# Calcoli Performance
df_raw['FX_Acq'] = df_raw['Date_DT'].apply(lambda x: fx_hist.asof(x) if not fx_hist.empty else 1.63)
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Gain_EUR'] = df_raw['Att_EUR'] - df_raw['Inv_EUR']
df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['FX_Acq']
df_raw['Att_AUD'] = df_raw['Att_EUR'] * market_fx
df_raw['Gain_AUD'] = df_raw['Att_AUD'] - df_raw['Inv_AUD']

# --- 4. UI ---
st.title("🏛️ Claudio's Executive Portfolio")

if st.sidebar.button("🔄 Refresh Data"):
    st.cache_data.clear()
    st.rerun()

tab1, tab2, tab3 = st.tabs(["📊 Performance Summary", "💸 Dettaglio & Simulatore", "📈 Storia"])

with tab1:
    # 1. Metriche Globali
    st.subheader("Stato Patrimoniale Globale")
    t_inv_eur, t_att_eur = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    t_inv_aud, t_att_aud = df_raw['Inv_AUD'].sum(), df_raw['Att_AUD'].sum()
    
    summary_df = pd.DataFrame({
        "Valuta": ["EURO (€)", "AUD ($)"],
        "Investito": [f"€{t_inv_eur:,.2f}", f"${t_inv_aud:,.2f}"],
        "Attuale": [f"€{t_att_eur:,.2f}", f"${t_att_aud:,.2f}"],
        "Gain Totale": [f"€{(t_att_eur - t_inv_eur):,.2f}", f"${(t_att_aud - t_inv_aud):,.2f}"],
        "ROI": [f"{((t_att_eur-t_inv_eur)/t_inv_eur*100):.2f}%", f"{((t_att_aud-t_inv_aud)/t_inv_aud*100):.2f}%"]
    })
    st.table(summary_df)

    # 2. Grafici
    c1, c2 = st.columns([1, 2])
    c1.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', title="Allocazione Asset (€)"), use_container_width=True)
    
    agg_plot = df_raw.groupby('ISIN').agg({'Gain_EUR': 'sum', 'Gain_AUD': 'sum'}).reset_index()
    fig_comp = go.Figure()
    fig_comp.add_trace(go.Bar(name='Gain/Loss EUR (€)', x=agg_plot['ISIN'], y=agg_plot['Gain_EUR'], marker_color='#3366CC'))
    fig_comp.add_trace(go.Bar(name='Gain/Loss AUD ($)', x=agg_plot['ISIN'], y=agg_plot['Gain_AUD'], marker_color='#109618'))
    fig_comp.update_layout(title="Gain/Loss se vendessi oggi (EUR vs AUD)", barmode='group', legend=dict(orientation="h", y=1.1))
    c2.plotly_chart(fig_comp, use_container_width=True)

    # 3. Tabella Aggregata
    st.subheader("Performance Aggregata per Titolo")
    agg_table = df_raw.groupby('ISIN').agg({
        'Qty': 'sum', 
        'Inv_EUR': 'sum', 
        'Att_EUR': 'sum', 
        'Gain_EUR': 'sum', 
        'Gain_AUD': 'sum'
    }).reset_index()
    st.dataframe(agg_table.style.format(precision=2), use_container_width=True, hide_index=True)

with tab2:
    st.subheader("Analisi Singoli Lotti & Simulatore")
    df_raw['% Vendi'] = 0.0
    cols_display = ['Data', 'ISIN', 'Qty', 'Prezzo_Acq', 'Price_Now', 'Att_EUR', 'Gain_EUR', 'Gain_AUD
