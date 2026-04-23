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
            val = q.get('c')
            return float(val) if val and val > 0 else None
    except: return None

@st.cache_data(ttl=3600)
def get_yahoo_data(isins_list): # Ora accetta esplicitamente una LISTA
    hist, current = {}, {}
    for isin in isins_list:
        sym = ticker_map.get(isin)
        if sym and sym != "MANUAL":
            try:
                t = yf.Ticker(sym)
                current[isin] = t.fast_info.get('last_price')
                h = t.history(start="2024-09-01")['Close']
                if not h.empty:
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

# FIX RIGA 75: Convertiamo in lista per evitare UnhashableParamError
unique_isins = df['ISIN'].unique().tolist()
y_hist, y_curr = get_yahoo_data(unique_isins)

def price_engine(row):
    if pd.notnull(row['P_Man']) and row['P_Man'] > 0: return row['P_Man'], "Manual"
    f_p = get_finnhub_price(row['ISIN'])
    if f_p: return f_p, "Finnhub"
    y_p = y_curr.get(row['ISIN'])
    if y_p: return y_p, "Yahoo"
    return row['P_Acq'], "Fallback"

res_engine = df.apply(price_engine, axis=1)
df['Price_Now'] = [r[0] for r in res_engine]
df['Source'] = [r[1] for r in res_engine]

# FX (Con protezione fallback)
@st.cache_data(ttl=3600)
def get_fx_secure():
    try:
        t = yf.Ticker("EURAUD=X")
        now = t.fast_info.get('last_price', 1.65)
        hist = t.history(start="2024-01-01")['Close']
        return now, hist
    except:
        return 1.65, pd.Series()

fx_now, fx_hist_df = get_fx_secure()

df['Att_EUR'] = df['Qty'] * df['Price_Now']
df['Att_AUD'] = df['Att_EUR'] * fx_now
df['Inv_AUD'] = df['Inv_EUR'] * df['Data'].apply(lambda x: fx_hist_df.asof(x) if not fx_hist_df.empty else 1.65)

# --- 4. INTERFACCIA ---
t1, t2, t3, t4 = st.tabs(["📊 Performance", "💸 Simulatore", "📈 Timeline", "🛠️ Diagnostics"])

with t1:
    v_eur, i_eur = df['Att_EUR'].sum(), df['Inv_EUR'].sum()
    v_aud, i_aud = df['Att_AUD'].sum(), df['Inv_AUD'].sum()

    c1, c2, c3 = st.columns(3)
    c1.metric("EUR Value", f"€{v_eur:,.0f}", f"€{v_eur-i_eur:,.0f}")
    c2.metric("AUD Value", f"${v_aud:,.0f}", f"${v_aud-i_aud:,.0f}")
    c3.metric("ROI %", f"{((v_eur/i_eur)-1)*100:.2f}%" if i_eur > 0 else "0%")
    
    st.divider()
    col_a, col_b = st.columns([1, 1.5])
    with col_a: 
        st.plotly_chart(px.pie(df, values='Att_EUR', names='ISIN', hole=0.4), use_container_width=True)
    with col_b:
        agg = df.groupby('ISIN').agg({'Inv_EUR':'sum','Att_EUR':'sum','Inv_AUD':'sum','Att_AUD':'sum'}).reset_index()
        fig = go.Figure(data=[
            go.Bar(name='Gain EUR', x=agg['ISIN'], y=agg['Att_EUR']-agg['Inv_EUR']),
            go.Bar(name='Gain AUD', x=agg['ISIN'], y=agg['Att_AUD']-agg['Inv_AUD'])
        ])
        fig.update_layout(barmode='group', title="EUR vs AUD Profit")
        st.plotly_chart(fig, use_container_width=True)

    st.dataframe(agg.style.format({
        'Inv_EUR': '€{:,.2f}', 'Att_EUR': '€{:,.2f}', 
        'Inv_AUD': '${:,.2f}', 'Att_AUD': '${:,.2f}'
    }), use_container_width=True)

with t2:
    st.data_editor(df[['ISIN', 'Data', 'Qty', 'P_Acq', 'Price_Now', 'Source']], use_container_width=True)

with t3:
    dr = pd.date_range(date(2024, 10, 1), date.today())
    timeline_data = []
    for d in dr:
        sub = df[df['Data'].dt.date <= d.date()]
        val = sum(p['Qty'] * (y_hist.get(p['ISIN']).asof(d) if (y_hist.get(p['ISIN']) is not None and not y_hist.get(p['ISIN']).empty) else p['P_Acq']) for _, p in sub.iterrows())
        timeline_data.append({'Date': d, 'Value': val})
    st.plotly_chart(px.area(pd.DataFrame(timeline_data), x='Date', y='Value'), use_container_width=True)

with t4:
    st.table(df[['ISIN', 'Price_Now', 'Source']].drop_duplicates())
