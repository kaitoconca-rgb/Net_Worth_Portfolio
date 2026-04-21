import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
import pytz
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
    return st.session_state.get("password_correct", False)

if not check_password():
    st.stop()

# --- 1. CONFIGURAZIONE & MAPPING ---
st.set_page_config(page_title="Executive Portfolio Console", layout="wide")

ticker_map = {
    "LU2885245055": "8OU9.DE",
    "IE0032077012": "EQQQ.DE",
    "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE",
    "IE00BFM15T99": "SJP6.DE",
    "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS",
    "IE00B3VVMM84": "VFEM.DE",
    "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI",
    "IE0005042456": "IUSA.DE"
}

@st.cache_data(ttl=600)
def get_fx_rate():
    try:
        t = yf.Ticker("EURAUD=X")
        val = t.fast_info['last_price']
        return float(val) if val else 1.6450
    except: return 1.6450

# --- 2. CARICAMENTO DATI ---
conn = st.connection("gsheets", type=GSheetsConnection)
df_input = conn.read(ttl=0)
df_input.columns = [c.strip() for c in df_input.columns]

df_raw = pd.DataFrame()
df_raw['Data'] = df_input['Fecha Valor']
df_raw['ISIN'] = df_input['ISIN']
df_raw['Qty'] = pd.to_numeric(df_input['Cantidad'], errors='coerce')
df_raw['Inv_EUR'] = pd.to_numeric(df_input['Importe Cargado'], errors='coerce')
df_raw['Prezzo_Acq'] = pd.to_numeric(df_input['Precio'], errors='coerce') 
df_raw['Manual_Override'] = pd.to_numeric(df_input['Price'], errors='coerce')

df_raw = df_raw.dropna(subset=['ISIN', 'Qty'])
df_raw['Date_DT'] = pd.to_datetime(df_raw['Data'], dayfirst=True)

# --- 3. LOGICA PREZZI LIVE & DIAGNOSTICA (SOLO PER TAB 4) ---
ticker_diag = {}

def fetch_live_price_diag(isin, manual_val):
    symbol = ticker_map.get(isin)
    now_utc = datetime.now(pytz.utc)
    if pd.notnull(manual_val) and manual_val > 0:
        ticker_diag[isin] = {"status": "MANUALE", "delay": "0 min"}
        return float(manual_val)
    if not symbol: return None
    try:
        t = yf.Ticker(symbol)
        f_info = t.fast_info
        current = f_info['last_price']
        lmt = f_info.get('last_market_time')
        delay, status = "N/D", "LIVE"
        if lmt:
            diff = now_utc - lmt.astimezone(pytz.utc)
            mins = int(diff.total_seconds() / 60)
            delay = f"{mins} min" if mins < 60 else f"{mins//60} ore"
            status = "LIVE" if mins < 30 else "FERMO"
        ticker_diag[isin] = {"status": status, "delay": delay}
        return float(current) if current else None
    except:
        ticker_diag[isin] = {"status": "ERRORE", "delay": "∞"}
        return None

market_fx = get_fx_rate()
fx_hist_raw = yf.download("EURAUD=X", start="2024-09-01", progress=False)['Close']
fx_hist = fx_hist_raw.iloc[:, 0] if isinstance(fx_hist_raw, pd.DataFrame) else fx_hist_raw

with st.spinner("Ripristino configurazione originale..."):
    prices_now = []
    cache_prezzi = {}
    for _, row in df_raw.iterrows():
        isin = row['ISIN']
        if isin not in cache_prezzi:
            cache_prezzi[isin] = fetch_live_price_diag(isin, row['Manual_Override'])
        # Se Yahoo fallisce, usa il prezzo di acquisizione come paracadute
        p = cache_prezzi[isin] if cache_prezzi[isin] is not None else float(row['Prezzo_Acq'])
        prices_now.append(p)

df_raw['Price_Now'] = prices_now
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Gain_EUR'] = df_raw['Att_EUR'] - df_raw['Inv_EUR']

def get_historical_fx(dt):
    try:
        val = fx_hist.asof(dt)
        return float(val) if not pd.isna(val) else 1.63
    except: return 1.63

df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['Date_DT'].apply(get_historical_fx)
df_raw['Att_AUD'] = df_raw['Att_EUR'] * market_fx
df_raw['Gain_AUD'] = df_raw['Att_AUD'] - df_raw['Inv_AUD']

# --- 4. INTERFACCIA RIPRISTINATA ---
tab1, tab2, tab3, tab4 = st.tabs(["📊 Performance", "💸 Simulatore Tasse", "📈 Storico", "🛠️ System Logs"])

with tab1:
    t_inv_eur, t_att_eur = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    t_inv_aud, t_att_aud = df_raw['Inv_AUD'].sum(), df_raw['Att_AUD'].sum()
    
    st.subheader("Riepilogo Globale")
    c1, c2, c3 = st.columns(3)
    c1.metric("Valore Totale (€)", f"€{t_att_eur:,.2f}", f"€{(t_att_eur - t_inv_eur):,.2f}")
    c2.metric("Valore Totale (AUD)", f"${t_att_aud:,.2f}", f"${(t_att_aud - t_inv_aud):,.2f}")
    c3.metric("Cambio EUR/AUD", f"{market_fx:.4f}")

    st.divider()
    v1, v2 = st.columns([1, 2])
    with v1:
        st.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', hole=0.4, title="Asset Allocation"), use_container_width=True)
    with v2:
        agg_p = df_raw.groupby('ISIN').agg({'Gain_EUR': 'sum', 'Gain_AUD': 'sum'}).reset_index()
        fig_b = go.Figure()
        fig_b.add_trace(go.Bar(name='Gain EUR (€)', x=agg_p['ISIN'], y=agg_p['Gain_EUR'], marker_color='#3366CC'))
        fig_b.add_trace(go.Bar(name='Gain AUD ($)', x=agg_p['ISIN'], y=agg_p['Gain_AUD'], marker_color='#109618'))
        st.plotly_chart(fig_b, use_container_width=True)

    st.subheader("Dettaglio Asset")
    st_agg = df_raw.groupby('ISIN').agg({'Qty':'sum','Inv_EUR':'sum','Att_EUR':'sum','Gain_EUR':'sum','Gain_AUD':'sum'}).reset_index()
    st.dataframe(st_agg.style.format(precision=2), use_container_width=True, hide_index=True)

with tab2:
    st.subheader("Simulatore CGT (ATO)")
    df_raw['% Vendi'] = 0.0
    ed_df = st.data_editor(df_raw[['Data', 'ISIN', 'Qty', 'Prezzo_Acq', 'Price_Now', 'Gain_AUD', '% Vendi']], hide_index=True, use_container_width=True)
    if ed_df['% Vendi'].sum() > 0:
        sim = ed_df[ed_df['% Vendi'] > 0].copy()
        sim['Days'] = (datetime.now() - pd.to_datetime(sim['Data'], dayfirst=True)).dt.days
        sim['G_Sim'] = sim['Gain_AUD'] * (sim['% Vendi'] / 100)
        sim['Taxable'] = sim.apply(lambda r: r['G_Sim'] * 0.5 if (r['G_Sim'] > 0 and r['Days'] >= 365) else r['G_Sim'], axis=1)
        st.success(f"Gain Lordo: ${sim['G_Sim'].sum():,.2f} AUD | Imponibile (Discount incl.): ${max(0, sim['Taxable'].sum()):,.2f} AUD")

with tab3:
    st.subheader("Crescita del Portafoglio (€)")
    h_df = df_raw.sort_values('Date_DT').copy()
    h_df['Investito_Cum'] = h_df['Inv_EUR'].cumsum()
    h_df['Valore_Cum'] = h_df['Att_EUR'].cumsum()
    
    fig_h = go.Figure()
    fig_h.add_trace(go.Scatter(x=h_df['Date_DT'], y=h_df['Investito_Cum'], name="Capitale Investito", fill='tozeroy', line_color='#A9A9A9'))
    fig_h.add_trace(go.Scatter(x=h_df['Date_DT'], y=h_df['Valore_Cum'], name="Valore di Mercato Attuale", fill='tonexty', line_color='#1f77b4'))
    fig_h.update_layout(xaxis_title="Data", yaxis_title="Euro (€)")
    st.plotly_chart(fig_h, use_container_width=True)

with tab4:
    st.subheader("🛠️ Diagnostica Dati Yahoo Finance")
    diag_list = []
    for k, v in ticker_diag.items():
        p_val = cache_prezzi.get(k)
        p_str = f"{p_val:.2f} €" if p_val is not None else "N/D"
        diag_list.append({"ISIN": k, "Status": v["status"], "Delay": v["delay"], "Price": p_str})
    st.table(pd.DataFrame(diag_list))
    st.caption(f"Ultimo Sync: {datetime.now(pytz.timezone('Australia/Sydney')).strftime('%H:%M:%S')} Sydney")
