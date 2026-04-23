import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go
from datetime import datetime, date
from streamlit_gsheets import GSheetsConnection

# --- 0. PROTEZIONE ---
if "password_correct" not in st.session_state:
    if st.text_input("Password", type="password") == st.secrets["auth"]["password"]:
        st.session_state["password_correct"] = True
        st.rerun()
    st.stop()

# --- 1. CONFIGURAZIONE & MAPPATURA HARD-CODED ---
st.set_page_config(page_title="Executive Portfolio", layout="wide")

# Mappa Ticker: Usiamo solo questi per eliminare i Fallback da ricerca ISIN
TICKER_MAP = {
    "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "36B2.MU", 
    "IE00B8GKDB10": "VHYL.MI", "IE00B3RBWM25": "VWRL.AS", 
    "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE",
    "LU2885245055": "MANUAL"
}

@st.cache_data(ttl=600)
def get_data_bulk(isins):
    prices = {}
    histories = {}
    tickers_to_fetch = [TICKER_MAP[i] for i in isins if i in TICKER_MAP and TICKER_MAP[i] != "MANUAL"]
    
    if tickers_to_fetch:
        data = yf.download(tickers_to_fetch, start="2024-09-01", group_by='ticker', progress=False)
        for isin, ticker in TICKER_MAP.items():
            if ticker in data.columns.levels[0]:
                prices[isin] = data[ticker]['Close'].iloc[-1]
                histories[isin] = data[ticker]['Close'].tz_localize(None)
    return prices, histories

# --- 2. CARICAMENTO DATI ---
conn = st.connection("gsheets", type=GSheetsConnection)
df_raw = conn.read(ttl=0).dropna(subset=['ISIN', 'Cantidad'])
df_raw.columns = [c.strip() for c in df_raw.columns]

# Setup DataFrame Core
df = pd.DataFrame({
    'Data': pd.to_datetime(df_raw['Fecha Valor'], dayfirst=True),
    'ISIN': df_raw['ISIN'],
    'Qty': pd.to_numeric(df_raw['Cantidad'], errors='coerce'),
    'Inv_EUR': pd.to_numeric(df_raw['Importe Cargado'], errors='coerce'),
    'P_Acq': pd.to_numeric(df_raw['Precio'], errors='coerce'),
    'P_Man': pd.to_numeric(df_raw['Price'], errors='coerce')
}).sort_values('Data')

# Recupero Prezzi e FX
prices_now, histories = get_data_bulk(df['ISIN'].unique().tolist())
t_fx = yf.Ticker("EURAUD=X")
fx_now = t_fx.fast_info['last_price']

# --- 3. CALCOLI LOGICI (RICHIESTA UTENTE) ---
def get_price(r):
    if pd.notnull(r['P_Man']) and r['P_Man'] > 0: return r['P_Man'], "Manual"
    p = prices_now.get(r['ISIN'])
    return (p, "Yahoo") if p else (r['P_Acq'], "Fallback")

df[['Price_Now', 'Source']] = df.apply(lambda r: pd.Series(get_price(r)), axis=1)

# Metriche Richieste
df['Valore_Attuale_EUR'] = df['Qty'] * df['Price_Now']
df['Valore_Attuale_AUD'] = df['Valore_Attuale_EUR'] * fx_now
df['Investito_AUD_Oggi'] = df['Inv_EUR'] * fx_now # Valore investito al cambio odierno (come richiesto)

# Calcolo Gain/Loss
df['Gain_Loss_EUR'] = df['Valore_Attuale_EUR'] - df['Inv_EUR']
df['Gain_Loss_AUD'] = df['Valore_Attuale_AUD'] - df['Investito_AUD_Oggi']

# --- 4. VISUALIZZAZIONE ---
t1, t2, t3 = st.tabs(["📊 Performance", "📈 Timeline", "🛠️ Diagnostics"])

with t1:
    # Riepilogo Metriche (Problema 1)
    i_eur = df['Inv_EUR'].sum()
    i_aud_now = df['Investito_AUD_Oggi'].sum()
    a_eur = df['Valore_Attuale_EUR'].sum()
    a_aud = df['Valore_Attuale_AUD'].sum()
    g_eur = df['Gain_Loss_EUR'].sum()
    g_aud = df['Gain_Loss_AUD'].sum()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Investito", f"€{i_eur:,.0f}", f"${i_aud_now:,.0f} (AUD Oggi)")
    c2.metric("Valore Attuale", f"€{a_eur:,.0f}", f"${a_aud:,.0f}")
    c3.metric("Gain/Loss Totale", f"€{g_eur:,.0f}", f"${g_aud:,.0f}")
    c4.metric("ROI %", f"{(g_eur/i_eur)*100:.2f}%", f"{(g_aud/i_aud_now)*100:.2f}%")

    st.divider()

    # Grafico Gain/Loss EUR vs AUD (Problema 2)
    st.subheader("Confronto Guadagno/Perdita: EUR vs AUD (al cambio odierno)")
    agg = df.groupby('ISIN').agg({
        'Gain_Loss_EUR': 'sum',
        'Gain_Loss_AUD': 'sum'
    }).reset_index()

    fig = go.Figure(data=[
        go.Bar(name='Gain/Loss EUR', x=agg['ISIN'], y=agg['Gain_Loss_EUR'], marker_color='#003366'),
        go.Bar(name='Gain/Loss AUD', x=agg['ISIN'], y=agg['Gain_Loss_AUD'], marker_color='#EF553B')
    ])
    fig.update_layout(barmode='group', height=400)
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Dettaglio Posizioni")
    st.dataframe(df[['ISIN', 'Data', 'Inv_EUR', 'Investito_AUD_Oggi', 'Valore_Attuale_EUR', 'Valore_Attuale_AUD', 'Gain_Loss_EUR', 'Gain_Loss_AUD']].style.format(precision=2), use_container_width=True)

with t3:
    st.write("Verifica Sorgenti Prezzi")
    st.table(df[['ISIN', 'Price_Now', 'Source']].drop_duplicates())
