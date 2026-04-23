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

# --- 1. CONFIGURAZIONE & MAPPATURA ---
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
prices_cache, hists_cache = {}, {}
for isin in df['ISIN'].unique():
    p, h = get_market_data(isin)
    prices_cache[isin] = p
    hists_cache[isin] = h

t_fx = yf.Ticker("EURAUD=X")
fx_now = t_fx.fast_info.get('last_price', 1.65)

# --- 3. MOTORE DI CALCOLO FISCALE ATO ---
def apply_price(r):
    if pd.notnull(r['P_Man']) and r['P_Man'] > 0: return r['P_Man'], "Manual"
    p = prices_cache.get(r['ISIN'])
    return (p, "Yahoo") if p else (r['P_Acq'], "Fallback")

df[['Price_Now', 'Source']] = df.apply(lambda r: pd.Series(apply_price(r)), axis=1)

# Calcoli Valutari
df['Valore_EUR'] = df['Qty'] * df['Price_Now']
df['Valore_AUD'] = df['Valore_EUR'] * fx_now
df['Investito_AUD_Oggi'] = df['Inv_EUR'] * fx_now 

# Logica CGT (Capital Gains Tax)
df['Days_Held'] = (pd.to_datetime(date.today()) - df['Data']).dt.days
df['Gain_AUD'] = df['Valore_AUD'] - df['Investito_AUD_Oggi']

def calculate_tax(row):
    if row['Gain_AUD'] <= 0: return 0
    # Sconto 50% se detenuto > 12 mesi (365 giorni)
    taxable_gain = row['Gain_AUD'] * 0.5 if row['Days_Held'] > 365 else row['Gain_AUD']
    # Assumiamo l'aliquota marginale top del 45% (più comune per il tuo profilo executive)
    return taxable_gain * 0.45 

df['Estimated_Tax_AUD'] = df.apply(calculate_tax, axis=1)
df['Net_Profit_AUD'] = df['Gain_AUD'] - df['Estimated_Tax_AUD']

# --- 4. INTERFACCIA ---
t1, t2, t3, t4 = st.tabs(["📊 Performance", "💸 Simulatore Fiscale", "📈 Timeline", "🛠️ Diagnostics"])

with t1:
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Investito (EUR)", f"€{df['Inv_EUR'].sum():,.0f}")
    col2.metric("Valore Attuale (AUD)", f"${df['Valore_AUD'].sum():,.0f}")
    col3.metric("Gain Lordo (AUD)", f"${df['Gain_AUD'].sum():,.0f}")
    col4.metric("Tasse Est. (ATO)", f"-${df['Estimated_Tax_AUD'].sum():,.0f}", delta_color="inverse")

    st.divider()
    st.subheader("Profitto Netto Post-Tasse per Asset (AUD)")
    fig = px.bar(df.groupby('ISIN')['Net_Profit_AUD'].sum().reset_index(), 
                 x='ISIN', y='Net_Profit_AUD', color_discrete_sequence=['#2ecc71'])
    st.plotly_chart(fig, use_container_width=True)

with t2:
    st.subheader("Simulatore Vendita & Impatto Fiscale (ATO)")
    st.markdown("""
    In questo simulatore puoi vedere l'impatto della **Capital Gains Tax**. 
    Gli asset evidenziati con lo sconto del 50% sono quelli detenuti da più di un anno.
    """)
    
    sim_df = df[['ISIN', 'Data', 'P_Acq', 'Price_Now', 'Gain_AUD', 'Days_Held', 'Estimated_Tax_AUD', 'Net_Profit_AUD']].copy()
    sim_df['CGT_Discount'] = sim_df['Days_Held'] > 365
    
    st.data_editor(
        sim_df.style.format({
            'P_Acq': '{:.2f}', 'Price_Now': '{:.2f}', 
            'Gain_AUD': '${:,.2f}', 'Estimated_Tax_AUD': '${:,.2f}', 
            'Net_Profit_AUD': '${:,.2f}'
        }).applymap(lambda x: 'background-color: #d4edda' if x is True else '', subset=['CGT_Discount']),
        use_container_width=True
    )

with t3:
    # Timeline
    dr = pd.date_range(df['Data'].min(), date.today(), freq='D')
    t_list = []
    for d in dr:
        sub = df[df['Data'].dt.date <= d.date()]
        val = sum(row['Qty'] * (hists_cache[row['ISIN']].asof(d) if hists_cache[row['ISIN']] is not None else row['P_Acq']) for _, row in sub.iterrows())
        t_list.append({'Date': d, 'Value': val})
    st.plotly_chart(px.area(pd.DataFrame(t_list), x='Date', y='Value', title="Evoluzione Capitale (€)"), use_container_width=True)

with t4:
    st.write("Verifica Diagnostica Ticker")
    st.table(df[['ISIN', 'Price_Now', 'Source']].drop_duplicates())
