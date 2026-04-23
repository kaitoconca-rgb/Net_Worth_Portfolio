import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime, date
from streamlit_gsheets import GSheetsConnection

# --- 0. PROTEZIONE ---
if "password_correct" not in st.session_state:
    st.text_input("Password", type="password", on_change=lambda: st.session_state.update({"password_correct": st.session_state["password"] == st.secrets["auth"]["password"]}), key="password")
    if not st.session_state.get("password_correct"): st.stop()

# --- 1. CONFIGURAZIONE ---
st.set_page_config(page_title="Executive Portfolio", layout="wide")

TICKER_MAP = {
    "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "36B2.MU", 
    "IE00B8GKDB10": "VHYL.MI", "IE00B3RBWM25": "VWRL.AS", 
    "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE",
    "LU2885245055": "MANUAL"
}

@st.cache_data(ttl=3600)
def get_single_ticker_data(isin):
    ticker_sym = TICKER_MAP.get(isin)
    if not ticker_sym or ticker_sym == "MANUAL": return None, None
    try:
        t = yf.Ticker(ticker_sym)
        # Prezzo Attuale
        info = t.fast_info
        price = info.get('last_price')
        # Storico per Timeline
        hist = t.history(period="max")['Close']
        if not hist.empty:
            hist.index = hist.index.tz_localize(None)
        return price, hist
    except:
        return None, None

# --- 2. CARICAMENTO DATI ---
conn = st.connection("gsheets", type=GSheetsConnection)
df_raw = conn.read(ttl=0).dropna(subset=['ISIN', 'Cantidad'])
df_raw.columns = [c.strip() for c in df_raw.columns]

df = pd.DataFrame({
    'Data': pd.to_datetime(df_raw['Fecha Valor'], dayfirst=True),
    'ISIN': df_raw['ISIN'],
    'Qty': pd.to_numeric(df_raw['Cantidad'], errors='coerce'),
    'Inv_EUR': pd.to_numeric(df_raw['Importe Cargado'], errors='coerce'),
    'P_Acq': pd.to_numeric(df_raw['Precio'], errors='coerce'),
    'P_Man': pd.to_numeric(df_raw['Price'], errors='coerce')
}).sort_values('Data')

# Recupero Prezzi e FX
unique_isins = df['ISIN'].unique()
prices_cache = {}
hists_cache = {}

for isin in unique_isins:
    p, h = get_single_ticker_data(isin)
    prices_cache[isin] = p
    hists_cache[isin] = h

t_fx = yf.Ticker("EURAUD=X")
fx_now = t_fx.fast_info.get('last_price', 1.65)

# --- 3. MOTORE LOGICO ---
def fetch_price(r):
    if pd.notnull(r['P_Man']) and r['P_Man'] > 0: return r['P_Man'], "Manual"
    p = prices_cache.get(r['ISIN'])
    if p: return p, "Yahoo"
    return r['P_Acq'], "Fallback"

df[['Price_Now', 'Source']] = df.apply(lambda r: pd.Series(fetch_price(r)), axis=1)

# Calcoli metriche richieste
df['Valore_Attuale_EUR'] = df['Qty'] * df['Price_Now']
df['Valore_Attuale_AUD'] = df['Valore_Attuale_EUR'] * fx_now
df['Investito_AUD_Oggi'] = df['Inv_EUR'] * fx_now 
df['Gain_Loss_EUR'] = df['Valore_Attuale_EUR'] - df['Inv_EUR']
df['Gain_Loss_AUD'] = df['Valore_Attuale_AUD'] - df['Investito_AUD_Oggi']

# --- 4. INTERFACCIA ---
t1, t2, t3, t4 = st.tabs(["📊 Performance", "💸 Simulatore", "📈 Timeline", "🛠️ Diagnostics"])

with t1:
    i_eur, i_aud = df['Inv_EUR'].sum(), df['Investito_AUD_Oggi'].sum()
    a_eur, a_aud = df['Valore_Attuale_EUR'].sum(), df['Valore_Attuale_AUD'].sum()
    g_eur, g_aud = a_eur - i_eur, a_aud - i_aud

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Investito", f"€{i_eur:,.0f}", f"${i_aud:,.0f} (AUD)")
    c2.metric("Valore Attuale", f"€{a_eur:,.0f}", f"${a_aud:,.0f}")
    c3.metric("Gain/Loss", f"€{g_eur:,.0f}", f"${g_aud:,.0f}")
    c4.metric("ROI %", f"{(g_eur/i_eur)*100:.2f}%", f"{(g_aud/i_aud)*100:.2f}%")

    st.divider()
    
    st.subheader("Confronto Gain/Loss: EUR vs AUD (Cambio Attuale)")
    agg = df.groupby('ISIN').agg({'Gain_Loss_EUR':'sum', 'Gain_Loss_AUD':'sum'}).reset_index()
    fig = go.Figure(data=[
        go.Bar(name='Gain EUR', x=agg['ISIN'], y=agg['Gain_Loss_EUR'], marker_color='#1f77b4'),
        go.Bar(name='Gain AUD', x=agg['ISIN'], y=agg['Gain_Loss_AUD'], marker_color='#ff7f0e')
    ])
    fig.update_layout(barmode='group')
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Dettaglio Asset")
    st.dataframe(df[['ISIN', 'Data', 'Inv_EUR', 'Valore_Attuale_EUR', 'Gain_Loss_EUR', 'Gain_Loss_AUD', 'Source']].style.format(precision=2))

with t3:
    # Timeline
    dr = pd.date_range(df['Data'].min(), date.today())
    timeline_data = []
    for d in dr:
        sub = df[df['Data'].dt.date <= d.date()]
        val_eur = 0
        for _, row in sub.iterrows():
            h = hists_cache.get(row['ISIN'])
            price_d = h.asof(d) if (h is not None and not h.empty) else row['P_Acq']
            val_eur += row['Qty'] * price_d
        timeline_data.append({'Date': d, 'Value': val_eur})
    st.plotly_chart(px.area(pd.DataFrame(timeline_data), x='Date', y='Value', title="Evoluzione Capitale (€)"), use_container_width=True)

with t4:
    st.write("Verifica Diagnostica Prezzi")
    st.table(df[['ISIN', 'Price_Now', 'Source']].drop_duplicates())
