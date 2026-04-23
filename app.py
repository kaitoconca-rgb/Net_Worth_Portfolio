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

# --- 1. CONFIGURAZIONE E MAPPATURA BLINDATA ---
st.set_page_config(page_title="Claudio Executive Portfolio", layout="wide")

TICKER_MAP = {
    "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "36B2.MU", 
    "IE00B8GKDB10": "VHYL.MI", "IE00B3RBWM25": "VWRL.AS", 
    "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE",
    "LU2885245055": "MANUAL"
}

@st.cache_data(ttl=3600)
def get_market_data(isin):
    ticker_sym = TICKER_MAP.get(isin)
    if not ticker_sym or ticker_sym == "MANUAL": return None, None
    try:
        t = yf.Ticker(ticker_sym)
        price = t.fast_info.get('last_price')
        hist = t.history(period="2y")['Close']
        if not hist.empty: hist.index = hist.index.tz_localize(None)
        return price, hist
    except: return None, None

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

# Prezzi e FX
prices_cache, hists_cache = {}, {}
for isin in df['ISIN'].unique():
    p, h = get_market_data(isin)
    prices_cache[isin] = p
    hists_cache[isin] = h

t_fx = yf.Ticker("EURAUD=X")
fx_now = t_fx.fast_info.get('last_price', 1.65)

# --- 3. MOTORE LOGICO E FISCALE ---
def fetch_price(r):
    if pd.notnull(r['P_Man']) and r['P_Man'] > 0: return r['P_Man']
    return prices_cache.get(r['ISIN']) or r['P_Acq']

df['Price_Now'] = df.apply(fetch_price, axis=1)

# Calcoli Valutari e CGT
df['Valore_AUD'] = df['Qty'] * df['Price_Now'] * fx_now
df['Inv_AUD_Oggi'] = df['Inv_EUR'] * fx_now
df['Gain_AUD'] = df['Valore_AUD'] - df['Inv_AUD_Oggi']
df['Days_Held'] = (pd.Timestamp.now() - df['Data']).dt.days
df['CGT_Discount'] = df['Days_Held'] > 365

def calc_tax(row, rate=0.45):
    if row['Gain_AUD'] <= 0: return 0.0
    mult = 0.5 if row['CGT_Discount'] else 1.0
    return row['Gain_AUD'] * mult * rate

df['Tax_ATO'] = df.apply(calc_tax, axis=1)

# --- 4. INTERFACCIA BASELINE ---
t1, t2, t3, t4 = st.tabs(["📊 Performance", "💸 Simulatore Exit Tax", "📈 Timeline", "🛠️ Diagnostics"])

with t1:
    # Metriche come da baseline approvata
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Investito (EUR)", f"€{df['Inv_EUR'].sum():,.0f}")
    c2.metric("Valore Attuale (AUD)", f"${df['Valore_AUD'].sum():,.0f}")
    c3.metric("Gain Lordo (AUD)", f"${df['Gain_AUD'].sum():,.0f}")
    c4.metric("Tasse ATO Est.", f"-${df['Tax_ATO'].sum():,.0f}", delta_color="inverse")
    
    st.divider()
    st.subheader("Profitto Netto Post-Tasse per Asset (AUD)")
    agg_net = df.groupby('ISIN').agg({'Gain_AUD':'sum', 'Tax_ATO':'sum'}).reset_index()
    agg_net['Net_AUD'] = agg_net['Gain_AUD'] - agg_net['Tax_ATO']
    st.plotly_chart(px.bar(agg_net, x='ISIN', y='Net_AUD', color_discrete_sequence=['#2ecc71']), use_container_width=True)

with t2:
    st.subheader("Simulatore Vendita Proporzionale")
    p_sell = st.slider("Percentuale di vendita", 0, 100, 100)
    tax_rate = st.number_input("Aliquota Marginale (%)", 0.0, 47.0, 45.0) / 100
    
    sim = df.copy()
    sim['Qty_S'] = sim['Qty'] * (p_sell/100)
    sim['Gain_S'] = (sim['Valore_AUD'] - sim['Inv_AUD_Oggi']) * (p_sell/100)
    sim['Tax_S'] = sim.apply(lambda r: (r['Gain_S'] * (0.5 if r['CGT_Discount'] else 1.0) * tax_rate) if r['Gain_S'] > 0 else 0, axis=1)
    sim['Net_S'] = (sim['Valore_AUD'] * (p_sell/100)) - sim['Tax_S']
    
    s1, s2, s3 = st.columns(3)
    s1.metric("Cash-out Lordo", f"${sim['Valore_AUD'].sum()*(p_sell/100):,.0f}")
    s2.metric("Tasse da pagare", f"${sim['Tax_S'].sum():,.0f}")
    s3.metric("Netto Disponibile", f"${sim['Net_S'].sum():,.0f}")
    
    st.divider()
    st.dataframe(sim[['ISIN', 'Data', 'P_Acq', 'Price_Now', 'Gain_S', 'CGT_Discount', 'Tax_S', 'Net_S']].style.format(precision=2).map(lambda x: 'background-color: #d4edda' if x is True else '', subset=['CGT_Discount']), use_container_width=True)

with t3:
    # Timeline
    dr = pd.date_range(df['Data'].min(), date.today(), freq='D')
    t_data = []
    for d in dr:
        sub = df[df['Data'].dt.date <= d.date()]
        val = sum(r['Qty'] * (hists_cache[r['ISIN']].asof(d) if hists_cache[r['ISIN']] is not None else r['P_Acq']) for _, r in sub.iterrows())
        t_data.append({'Date': d, 'Value': val})
    st.plotly_chart(px.area(pd.DataFrame(t_data), x='Date', y='Value', title="Evoluzione Capitale (€)"), use_container_width=True)

with t4:
    st.write("Diagnostica")
    st.table(df[['ISIN', 'Price_Now', 'Days_Held']])
