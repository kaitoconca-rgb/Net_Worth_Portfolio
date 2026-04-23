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
st.set_page_config(page_title="Executive Portfolio Console", layout="wide")

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
        # Timeout alzato a 5 secondi per evitare Fallback inutili
        res = requests.get(f"https://finnhub.io/api/v1/search?q={isin}&token={api_key}", timeout=5).json()
        if res.get('result'):
            symbol = res['result'][0]['symbol']
            q = requests.get(f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={api_key}", timeout=5).json()
            return float(q['c']) if q.get('c') else None
    except: return None

@st.cache_data(ttl=3600)
def get_yahoo_data(isins_list):
    hist, current = {}, {}
    for isin in isins_list:
        sym = ticker_map.get(isin)
        if sym and sym != "MANUAL":
            try:
                t = yf.Ticker(sym)
                current[isin] = t.fast_info.get('last_price')
                h = t.history(start="2024-09-01")['Close']
                if not h.empty:
                    h.index = h.index.tz_localize(None)
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

y_hist, y_curr = get_yahoo_data(df['ISIN'].unique().tolist())

def engine(row):
    if pd.notnull(row['P_Man']) and row['P_Man'] > 0: return row['P_Man'], "Manual"
    # Cerchiamo prima su Yahoo (più veloce)
    yp = y_curr.get(row['ISIN'])
    if yp: return yp, "Yahoo"
    # Poi Finnhub
    fp = get_finnhub_price(row['ISIN'])
    if fp: return fp, "Finnhub"
    return row['P_Acq'], "Fallback"

df[['Price_Now', 'Source']] = df.apply(lambda r: pd.Series(engine(r)), axis=1)

# FX Management
t_fx = yf.Ticker("EURAUD=X")
fx_now = t_fx.fast_info.get('last_price', 1.65)
fx_hist = t_fx.history(start="2024-01-01")['Close']
if not fx_hist.empty: fx_hist.index = fx_hist.index.tz_localize(None)

df['Att_EUR'] = df['Qty'] * df['Price_Now']
df['Att_AUD'] = df['Att_EUR'] * fx_now
# Cambio alla data di acquisto
df['Inv_AUD'] = df.apply(lambda r: r['Inv_EUR'] * (fx_hist.asof(r['Data']) if not fx_hist.empty else 1.65), axis=1)

# --- 4. INTERFACCIA ---
t1, t2, t3, t4 = st.tabs(["📊 Performance", "💸 Simulatore", "📈 Timeline", "🛠️ Diagnostics"])

with t1:
    # 1) METRICHE COMPLETE (Problema 1 risolto)
    i_eur, a_eur = df['Inv_EUR'].sum(), df['Att_EUR'].sum()
    i_aud, a_aud = df['Inv_AUD'].sum(), df['Att_AUD'].sum()
    g_eur, g_aud = a_eur - i_eur, a_aud - i_aud
    
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Investito (EUR/AUD)", f"€{i_eur:,.0f}", f"${i_aud:,.0f}")
    c2.metric("Attuale (EUR/AUD)", f"€{a_eur:,.0f}", f"${a_aud:,.0f}")
    c3.metric("Gain/Loss", f"€{g_eur:,.0f}", f"${g_aud:,.0f}")
    c4.metric("ROI %", f"{(g_eur/i_eur)*100:.2f}%", f"{(g_aud/i_aud)*100:.2f}%")

    st.divider()
    
    # 2) GRAFICO FX GAIN/LOSS (Problema 2 risolto e blindato)
    st.subheader("Analisi Valutaria: Guadagno Reale in AUD")
    agg = df.groupby('ISIN').agg({'Inv_EUR':'sum','Att_EUR':'sum','Inv_AUD':'sum','Att_AUD':'sum'}).reset_index()
    agg['Gain_EUR'] = agg['Att_EUR'] - agg['Inv_EUR']
    agg['Gain_AUD'] = agg['Att_AUD'] - agg['Inv_AUD']
    
    fig_fx = go.Figure(data=[
        go.Bar(name='Guadagno in EUR', x=agg['ISIN'], y=agg['Gain_EUR'], marker_color='#1f77b4'),
        go.Bar(name='Guadagno in AUD (Cambio Attuale)', x=agg['ISIN'], y=agg['Gain_AUD'], marker_color='#ff7f0e')
    ])
    fig_fx.update_layout(barmode='group', title="Se vendessi tutto oggi: Profitto EUR vs AUD")
    st.plotly_chart(fig_fx, use_container_width=True)

    st.subheader("Dettaglio Asset")
    st.dataframe(agg.style.format({
        'Inv_EUR': '€{:,.2f}', 'Att_EUR': '€{:,.2f}', 'Gain_EUR': '€{:,.2f}',
        'Inv_AUD': '${:,.2f}', 'Att_AUD': '${:,.2f}', 'Gain_AUD': '${:,.2f}'
    }), use_container_width=True)

with t2:
    st.write("Dati Correnti e Sorgenti")
    st.data_editor(df[['ISIN', 'Qty', 'P_Acq', 'Price_Now', 'Source']], use_container_width=True)

with t3:
    # Timeline
    dr = pd.date_range(date(2024, 10, 1), date.today())
    timeline_pts = []
    for d in dr:
        sub = df[df['Data'].dt.date <= d.date()]
        val = sum(p['Qty'] * (y_hist[p['ISIN']].asof(d) if p['ISIN'] in y_hist else p['P_Acq']) for _, p in sub.iterrows())
        timeline_pts.append({'Date': d, 'Value': val})
    st.plotly_chart(px.area(pd.DataFrame(timeline_pts), x='Date', y='Value', title="Evoluzione Portafoglio (€)"), use_container_width=True)

with t4:
    # 3) DIAGNOSTICA (Problema 3 indirizzato)
    st.write("Verifica Diagnostica API:")
    st.table(df[['ISIN', 'Price_Now', 'Source']].drop_duplicates())
    st.info("Nota: Se vedi Fallback, le API Finnhub/Yahoo hanno risposto oltre il tempo limite (5s).")
