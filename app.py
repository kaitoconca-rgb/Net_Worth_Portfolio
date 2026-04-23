import streamlit as st
import pandas as pd
import yfinance as yf
import requests
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, date
from streamlit_gsheets import GSheetsConnection

# --- 0. PROTEZIONE ---
def check_password():
    if "password_correct" not in st.session_state:
        st.text_input("Password", type="password", on_change=lambda: st.session_state.update({"password_correct": st.session_state["password"] == st.secrets["auth"]["password"]}), key="password")
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

# --- 2. MOTORE PREZZI ---
@st.cache_data(ttl=600)
def get_finnhub_price(isin):
    api_key = st.secrets.get("FINNHUB_API_KEY")
    if not api_key: return None
    try:
        res = requests.get(f"https://finnhub.io/api/v1/search?q={isin}&token={api_key}", timeout=2).json()
        if res.get('result'):
            symbol = res['result'][0]['symbol']
            q = requests.get(f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={api_key}", timeout=2).json()
            return float(q['c']) if q.get('c') else None
    except: return None

@st.cache_data(ttl=3600)
def get_yahoo_data(isins):
    hist, current = {}, {}
    for isin in isins:
        sym = ticker_map.get(isin)
        if sym and sym != "MANUAL":
            try:
                t = yf.Ticker(sym)
                current[isin] = t.fast_info['last_price']
                h = t.history(start="2024-09-01")['Close']
                hist[isin] = h
            except: pass
    return hist, current

# --- 3. CARICAMENTO E CALCOLI ---
conn = st.connection("gsheets", type=GSheetsConnection)
df_in = conn.read(ttl=0).dropna(subset=['ISIN', 'Cantidad'])
df_in.columns = [c.strip() for c in df_in.columns]

df = pd.DataFrame({
    'Data': pd.to_datetime(df_in['Fecha Valor'], dayfirst=True),
    'ISIN': df_in['ISIN'],
    'Qty': pd.to_numeric(df_in['Cantidad'], errors='coerce'),
    'Inv_EUR': pd.to_numeric(df_in['Importe Cargado'], errors='coerce'),
    'P_Acq': pd.to_numeric(df_in['Precio'], errors='coerce'),
    'P_Man': pd.to_numeric(df_in['Price'], errors='coerce')
}).sort_values('Data')

y_hist, y_curr = get_yahoo_data(df['ISIN'].unique())

def engine(row):
    if pd.notnull(row['P_Man']) and row['P_Man'] > 0: return row['P_Man'], "Manual"
    f_p = get_finnhub_price(row['ISIN'])
    if f_p: return f_p, "Finnhub"
    y_p = y_curr.get(row['ISIN'])
    if y_p: return y_p, "Yahoo"
    return row['P_Acq'], "Fallback"

res = df.apply(engine, axis=1)
df['Price_Now'] = [r[0] for r in res]
df['Source'] = [r[1] for r in res]

# FX
t_fx = yf.Ticker("EURAUD=X")
fx_now = t_fx.fast_info['last_price']
fx_hist = t_fx.history(start="2024-01-01")['Close']

df['Att_EUR'] = df['Qty'] * df['Price_Now']
df['Att_AUD'] = df['Att_EUR'] * fx_now
df['Inv_AUD'] = df['Inv_EUR'] * df['Data'].apply(lambda x: fx_hist.asof(x) if not fx_hist.empty else 1.65)

# --- 4. INTERFACCIA ---
t1, t2, t3, t4 = st.tabs(["📊 Performance", "💸 Simulatore", "📈 Timeline", "🛠️ Diagnostics"])

with t1:
    c1, c2, c3 = st.columns(3)
    c1.metric("Portafoglio EUR", f"€{df['Att_EUR'].sum():,.0f}", f"€{df['Att_EUR'].sum()-df['Inv_EUR'].sum():,.0f}")
    c2.metric("Portafoglio AUD", f"${df['Att_AUD'].sum():,.0f}", f"${df['Att_AUD'].sum()-df['Inv_AUD'].sum():,.0f}")
    c3.metric("ROI %", f"{(df['Att_EUR'].sum()/df['Inv_EUR'].sum()-1)*100:.2f}%")
    
    st.divider()
    col_a, col_b = st.columns([1, 1.5])
    with col_a: st.plotly_chart(px.pie(df, values='Att_EUR', names='ISIN', hole=0.4, title="Allocation"), use_container_width=True)
    with col_b:
        agg = df.groupby('ISIN').agg({'Inv_EUR':'sum','Att_EUR':'sum','Inv_AUD':'sum','Att_AUD':'sum'}).reset_index()
        fig = go.Figure(data=[
            go.Bar(name='Gain EUR', x=agg['ISIN'], y=agg['Att_EUR']-agg['Inv_EUR']),
            go.Bar(name='Gain AUD', x=agg['ISIN'], y=agg['Att_AUD']-agg['Inv_AUD'])
        ])
        fig.update_layout(barmode='group', title="Profitto per Asset (EUR vs AUD)")
        st.plotly_chart(fig, use_container_width=True)
    
    st.subheader("Dettaglio Asset")
    # CORREZIONE: Formattazione sicura per evitare ValueError
    st.dataframe(agg.style.format({
        'Inv_EUR': '{:,.2f}', 'Att_EUR': '{:,.2f}', 
        'Inv_AUD': '{:,.2f}', 'Att_AUD': '{:,.2f}'
    }), use_container_width=True)

with t2:
    st.data_editor(df[['ISIN', 'Data', 'Qty', 'P_Acq', 'Price_Now', 'Source']], use_container_width=True)

with t3:
    dr = pd.date_range(date(2024, 10, 1), date.today())
    timeline = []
    for d in dr:
        sub = df[df['Data'].dt.date <= d.date()]
        val = sum(p['Qty'] * (y_hist.get(p['ISIN']).asof(d) if (y_hist.get(p['ISIN']) is not None and not y_hist.get(p['ISIN']).empty) else p['P_Acq']) for _, p in sub.iterrows())
        timeline.append({'Date': d, 'Value': val})
    st.plotly_chart(px.area(pd.DataFrame(timeline), x='Date', y='Value', title="Evoluzione Portafoglio (€)"), use_container_width=True)

with t4:
    st.write("Diagnostica Fonti")
    st.dataframe(df[['ISIN', 'Price_Now', 'Source']].drop_duplicates())
