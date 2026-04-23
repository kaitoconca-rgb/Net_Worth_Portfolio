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
st.set_page_config(page_title="Executive Portfolio & Tax Console", layout="wide")

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

# --- 2. CARICAMENTO ---
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

prices_cache, hists_cache = {}, {}
for isin in df['ISIN'].unique():
    p, h = get_market_data(isin)
    prices_cache[isin] = p
    hists_cache[isin] = h

t_fx = yf.Ticker("EURAUD=X")
fx_now = t_fx.fast_info.get('last_price', 1.65)

# --- 3. LOGICA FISCALE ATO ---
def apply_price(r):
    if pd.notnull(r['P_Man']) and r['P_Man'] > 0: return r['P_Man'], "Manual"
    p = prices_cache.get(r['ISIN'])
    return (p, "Yahoo") if p else (r['P_Acq'], "Fallback")

df[['Price_Now', 'Source']] = df.apply(lambda r: pd.Series(apply_price(r)), axis=1)

df['Valore_EUR'] = df['Qty'] * df['Price_Now']
df['Valore_AUD'] = df['Valore_EUR'] * fx_now
df['Investito_AUD_Oggi'] = df['Inv_EUR'] * fx_now 
df['Gain_AUD'] = df['Valore_AUD'] - df['Investito_AUD_Oggi']
df['Days_Held'] = (pd.Timestamp.now() - df['Data']).dt.days

def calculate_ato_tax(row):
    if row['Gain_AUD'] <= 0: return 0.0
    # Sconto 50% se detenuto > 1 anno
    taxable_gain = row['Gain_AUD'] * 0.5 if row['Days_Held'] > 365 else row['Gain_AUD']
    return taxable_gain * 0.45 # Aliquota executive stimata

df['Tax_AUD'] = df.apply(calculate_ato_tax, axis=1)
df['Net_AUD'] = df['Gain_AUD'] - df['Tax_AUD']

# --- 4. INTERFACCIA ---
t1, t2, t3, t4 = st.tabs(["📊 Performance", "💸 Simulatore Fiscale", "📈 Timeline", "🛠️ Diagnostics"])

with t1:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Investito (EUR)", f"€{df['Inv_EUR'].sum():,.0f}")
    c2.metric("Attuale (AUD)", f"${df['Valore_AUD'].sum():,.0f}")
    c3.metric("Gain Lordo (AUD)", f"${df['Gain_AUD'].sum():,.0f}")
    c4.metric("Tasse ATO (Est.)", f"-${df['Tax_AUD'].sum():,.0f}", delta_color="inverse")

    st.divider()
    st.subheader("Profitto Netto Post-Tasse per Asset (AUD)")
    agg_net = df.groupby('ISIN')['Net_AUD'].sum().reset_index()
    st.plotly_chart(px.bar(agg_net, x='ISIN', y='Net_AUD', color_discrete_sequence=['#2ecc71']), use_container_width=True)

with t2:
    st.subheader("Simulatore Cash-out & Tasse ATO")
    sim_df = df[['ISIN', 'Data', 'P_Acq', 'Price_Now', 'Gain_AUD', 'Days_Held', 'Tax_AUD', 'Net_AUD']].copy()
    sim_df['50%_Discount'] = sim_df['Days_Held'] > 365
    
    # FIX: Usiamo .map() invece di .applymap() per Pandas > 2.0
    st.data_editor(
        sim_df.style.format({
            'P_Acq': '{:.2f}', 'Price_Now': '{:.2f}', 
            'Gain_AUD': '${:,.2f}', 'Tax_AUD': '${:,.2f}', 'Net_AUD': '${:,.2f}'
        }).map(lambda x: 'background-color: #d4edda' if x is True else '', subset=['50%_Discount']),
        use_container_width=True
    )

with t3:
    dr = pd.date_range(df['Data'].min(), date.today(), freq='D')
    t_data = []
    for d in dr:
        sub = df[df['Data'].dt.date <= d.date()]
        val = sum(r['Qty'] * (hists_cache[r['ISIN']].asof(d) if hists_cache[r['ISIN']] is not None else r['P_Acq']) for _, r in sub.iterrows())
        t_data.append({'Date': d, 'Value': val})
    st.plotly_chart(px.area(pd.DataFrame(t_data), x='Date', y='Value', title="Evoluzione Capitale (€)"), use_container_width=True)

with t4:
    st.write("Diagnostica")
    st.table(df[['ISIN', 'Price_Now', 'Source']].drop_duplicates())
