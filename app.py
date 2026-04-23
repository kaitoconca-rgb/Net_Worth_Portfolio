import streamlit as st
import pandas as pd
import yfinance as yf
import requests
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, date
from streamlit_gsheets import GSheetsConnection

# --- 0. PROTEZIONE (RIPRISTINATA) ---
def check_password():
    if "password_correct" not in st.session_state:
        st.text_input("Inserisci Password", type="password", on_change=lambda: st.session_state.update({"password_correct": st.session_state["password"] == st.secrets["auth"]["password"]}), key="password")
        return False
    return st.session_state["password_correct"]

if not check_password():
    st.stop()

# --- 1. CONFIGURAZIONE ---
st.set_page_config(page_title="Claudio's Executive Console", layout="wide")

ticker_map = {
    "LU2885245055": "MANUAL",
    "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "36B2.MU", "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS", "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE"
}

# --- 2. MOTORE PREZZI (ANTIPANICO) ---

@st.cache_data(ttl=600)
def get_live_price_finnhub(isin):
    api_key = st.secrets.get("FINNHUB_API_KEY")
    if not api_key or isin == "MANUAL": return None
    try:
        res = requests.get(f"https://finnhub.io/api/v1/search?q={isin}&token={api_key}", timeout=2).json()
        if res.get('result'):
            symbol = res['result'][0]['symbol']
            q = requests.get(f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={api_key}", timeout=2).json()
            p = q.get('c')
            return float(p) if p and p > 0 else None
    except: return None
    return None

@st.cache_data(ttl=3600)
def get_yahoo_hist_map(isins_list):
    data = {}
    for isin in isins_list:
        sym = ticker_map.get(isin)
        if sym and sym != "MANUAL":
            try:
                h = yf.download(sym, start="2024-09-01", progress=False)['Close']
                if isinstance(h, pd.DataFrame): h = h.iloc[:, 0]
                data[isin] = h
            except: data[isin] = None
    return data

# --- 3. LOGICA CORE ---
conn = st.connection("gsheets", type=GSheetsConnection)
df_input = conn.read(ttl=0)
df_input.columns = [c.strip() for c in df_input.columns]

df_raw = pd.DataFrame()
df_raw['Data'] = pd.to_datetime(df_input['Fecha Valor'], dayfirst=True)
df_raw['ISIN'] = df_input['ISIN']
df_raw['Qty'] = pd.to_numeric(df_input['Cantidad'], errors='coerce')
df_raw['Inv_EUR'] = pd.to_numeric(df_input['Importe Cargado'], errors='coerce')
df_raw['Prezzo_Acq'] = pd.to_numeric(df_input['Precio'], errors='coerce') 
df_raw['Manual_Price'] = pd.to_numeric(df_input['Price'], errors='coerce')
df_raw = df_raw.dropna(subset=['ISIN', 'Qty']).sort_values('Data')

hist_map = get_yahoo_hist_map(df_raw['ISIN'].unique().tolist())

def get_final_price(row):
    # 1. Manuale (Sempre prioritario)
    if pd.notnull(row['Manual_Price']) and row['Manual_Price'] > 0:
        return row['Manual_Price'], "Manual"
    # 2. Finnhub Live
    fp = get_live_price_finnhub(row['ISIN'])
    if fp: return fp, "Finnhub"
    # 3. Yahoo Storico (Ultimo disponibile)
    h = hist_map.get(row['ISIN'])
    if h is not None and not h.empty:
        return float(h.iloc[-1]), "Yahoo Hist"
    # 4. PARACADUTE: Prezzo Acquisto (Evita il NULL)
    return row['Prezzo_Acq'], "Fallback (Acq)"

res_prices = df_raw.apply(get_final_price, axis=1)
df_raw['Price_Now'] = [r[0] for r in res_prices]
df_raw['Source'] = [r[1] for r in res_prices]

# FX Rate
@st.cache_data(ttl=600)
def get_fx():
    try:
        t = yf.Ticker("EURAUD=X")
        now = float(t.fast_info['last_price'])
        hist = yf.download("EURAUD=X", start="2024-01-01", progress=False)['Close']
        if isinstance(hist, pd.DataFrame): hist = hist.iloc[:, 0]
        return now, hist
    except: return 1.65, None

fx_now, fx_hist = get_fx()
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Att_AUD'] = df_raw['Att_EUR'] * fx_now
df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['Data'].apply(lambda x: float(fx_hist.asof(x)) if fx_hist is not None else 1.65)

# --- 4. INTERFACCIA RIPRISTINATA ---
t1, t2, t3, t4 = st.tabs(["📊 Performance", "💸 Simulatore", "📈 Timeline", "🛠️ Diagnostics"])

with t1:
    m1, m2, m3 = st.columns(3)
    m1.metric("Totale EUR", f"€{df_raw['Att_EUR'].sum():,.0f}", f"€{df_raw['Att_EUR'].sum()-df_raw['Inv_EUR'].sum():,.0f}")
    m2.metric("Totale AUD", f"${df_raw['Att_AUD'].sum():,.0f}", f"${df_raw['Att_AUD'].sum()-df_raw['Inv_AUD'].sum():,.0f}")
    m3.metric("ROI %", f"{(df_raw['Att_EUR'].sum()/df_raw['Inv_EUR'].sum()-1)*100:.2f}%")
    
    st.divider()
    g1, g2 = st.columns([1, 1.5])
    with g1: st.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', hole=0.4, title="Allocation"), use_container_width=True)
    with g2:
        # RIPRISTINATO GRAFICO A BARRE FX IMPACT
        agg = df_raw.groupby('ISIN').agg({'Inv_EUR':'sum','Att_EUR':'sum','Inv_AUD':'sum','Att_AUD':'sum'}).reset_index()
        fig = go.Figure(data=[
            go.Bar(name='Gain EUR', x=agg['ISIN'], y=agg['Att_EUR']-agg['Inv_EUR']),
            go.Bar(name='Gain AUD', x=agg['ISIN'], y=agg['Att_AUD']-agg['Inv_AUD'])
        ])
        fig.update_layout(barmode='group', title="FX Impact: Profitto EUR vs AUD")
        st.plotly_chart(fig, use_container_width=True)
    
    st.subheader("Dettaglio Portafoglio")
    st.dataframe(agg.style.format("{:,.2f}"), use_container_width=True)

with t2:
    st.subheader("Simulatore Vendita")
    df_raw['% Vendi'] = 0.0
    st.data_editor(df_raw[['ISIN', 'Data', 'Qty', 'Prezzo_Acq', 'Price_Now', 'Source', '% Vendi']], use_container_width=True)

with t3:
    # RIPRISTINATA TIMELINE AREA CHART
    st.subheader("Evoluzione Capitale (€)")
    dr = pd.date_range(date(2024, 10, 1), date.today())
    timeline = []
    for d in dr:
        sub = df_raw[df_raw['Data'].dt.date <= d.date()]
        val = sum(p['Qty'] * (hist_map.get(p['ISIN']).asof(d) if (hist_map.get(p['ISIN']) is not None and not hist_map.get(p['ISIN']).empty) else p['Prezzo_Acq']) for _, p in sub.iterrows())
        timeline.append({'Date': d, 'Value': val})
    st.plotly_chart(px.area(pd.DataFrame(timeline), x='Date', y='Value'), use_container_width=True)

with t4:
    st.write("Verifica Integrità Dati")
    st.dataframe(df_raw[['ISIN', 'Price_Now', 'Source']].drop_duplicates())
