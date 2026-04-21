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

# --- 3. LOGICA PREZZI LIVE & DIAGNOSTICA (Tab 4) ---
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
fx_hist_raw = yf.download("EURAUD=X", start="2025-09-01", progress=False)['Close']
fx_hist = fx_hist_raw.iloc[:, 0] if isinstance(fx_hist_raw, pd.DataFrame) else fx_hist_raw

with st.spinner("Sincronizzazione dati e cronologia..."):
    prices_now = []
    cache_prezzi = {}
    for _, row in df_raw.iterrows():
        isin = row['ISIN']
        if isin not in cache_prezzi:
            cache_prezzi[isin] = fetch_live_price_diag(isin, row['Manual_Override'])
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

# --- 4. INTERFACCIA ---
tab1, tab2, tab3, tab4 = st.tabs(["📊 Performance", "💸 Simulatore Tasse", "📈 Storico", "🛠️ System Logs"])

with tab1:
    t_inv_eur, t_att_eur = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    st.metric("Valore Portafoglio (€)", f"€{t_att_eur:,.2f}", f"€{(t_att_eur - t_inv_eur):,.2f}")
    
    st.subheader("Asset Allocation & Gain")
    v1, v2 = st.columns([1, 2])
    with v1:
        st.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', hole=0.4), use_container_width=True)
    with v2:
        agg_p = df_raw.groupby('ISIN').agg({'Gain_EUR': 'sum', 'Gain_AUD': 'sum'}).reset_index()
        fig_b = go.Figure()
        fig_b.add_trace(go.Bar(name='Gain EUR', x=agg_p['ISIN'], y=agg_p['Gain_EUR']))
        fig_b.add_trace(go.Bar(name='Gain AUD', x=agg_p['ISIN'], y=agg_p['Gain_AUD']))
        st.plotly_chart(fig_b, use_container_width=True)

    st_agg = df_raw.groupby('ISIN').agg({'Qty':'sum','Inv_EUR':'sum','Att_EUR':'sum','Gain_EUR':'sum'}).reset_index()
    st.dataframe(st_agg.style.format(precision=2), use_container_width=True, hide_index=True)

with tab2:
    st.subheader("Simulatore CGT (ATO)")
    df_raw['% Vendi'] = 0.0
    ed_df = st.data_editor(df_raw[['Data', 'ISIN', 'Qty', 'Price_Now', 'Gain_AUD', '% Vendi']], hide_index=True)
    if ed_df['% Vendi'].sum() > 0:
        sim = ed_df[ed_df['% Vendi'] > 0].copy()
        sim['Days'] = (datetime.now() - pd.to_datetime(sim['Data'], dayfirst=True)).dt.days
        sim['G_Sim'] = sim['Gain_AUD'] * (sim['% Vendi'] / 100)
        taxable = sim.apply(lambda r: r['G_Sim'] * 0.5 if (r['G_Sim'] > 0 and r['Days'] >= 365) else r['G_Sim'], axis=1).sum()
        st.success(f"Imponibile Stimato AUD: ${max(0, taxable):,.2f}")

with tab3:
    st.subheader("Evoluzione Temporale")
    hist_plot = df_raw.sort_values('Date_DT').copy()
    hist_plot['Inv_Cum'] = hist_plot['Inv_EUR'].cumsum()
    # Calcolo valore corrente nel tempo (approssimazione basata su acquisti progressivi)
    hist_plot['Val_Cum'] = hist_plot['Att_EUR'].cumsum()
    
    fig_hist = go.Figure()
    fig_hist.add_trace(go.Scatter(x=hist_plot['Date_DT'], y=hist_plot['Inv_Cum'], name="Investito Totale", fill='tozeroy', line_color='gray'))
    fig_hist.add_trace(go.Scatter(x=hist_plot['Date_DT'], y=hist_plot['Val_Cum'], name="Valore Corrente", fill='tonexty', line_color='blue'))
    st.plotly_chart(fig_hist, use_container_width=True)

with tab4:
    st.subheader("🛠️ Stato Aggiornamento Yahoo Finance")
    diag_list = []
    for k, v in ticker_diag.items():
        p_val = cache_prezzi.get(k)
        p_str = f"{p_val:.2f} €" if p_val is not None else "N/D"
        diag_list.append({"ISIN": k, "Status": v["status"], "Last Market Trade": v["delay"], "Price": p_str})
    st.table(pd.DataFrame(diag_list))
    st.caption(f"Ultimo Refresh: {datetime.now(pytz.timezone('Australia/Sydney')).strftime('%H:%M:%S')} Sydney")
