import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
import pytz
from streamlit_gsheets import GSheetsConnection

# --- 0. PROTEZIONE ---
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

# --- 1. CONFIGURAZIONE ---
st.set_page_config(page_title="Executive Portfolio Console", layout="wide")

ticker_map = {
    "LU2885245055": "8OU9.DE", "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "SJP6.DE", "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS", "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE"
}

@st.cache_data(ttl=600)
def get_current_fx():
    try:
        t = yf.Ticker("EURAUD=X")
        return float(t.fast_info['last_price'])
    except: return 1.6450

@st.cache_data(ttl=3600)
def get_historical_fx_series():
    try:
        fx_raw = yf.download("EURAUD=X", start="2023-01-01", progress=False)['Close']
        if isinstance(fx_raw, pd.DataFrame):
            return fx_raw.iloc[:, 0]
        return fx_raw
    except: return None

# --- 2. DATI ---
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

# --- 3. LOGICA PREZZI & CAMBI ---
ticker_diag = {}
cache_prezzi = {}

def fetch_price(isin, manual_val):
    symbol = ticker_map.get(isin)
    if pd.notnull(manual_val) and manual_val > 0:
        ticker_diag[isin] = {"status": "MANUALE", "delay": "0 min"}
        return float(manual_val)
    try:
        t = yf.Ticker(symbol)
        curr = t.fast_info['last_price']
        ticker_diag[isin] = {"status": "LIVE", "delay": "Live"}
        return float(curr) if curr else None
    except:
        ticker_diag[isin] = {"status": "ERRORE", "delay": "∞"}
        return None

fx_now = get_current_fx()
fx_history = get_historical_fx_series()

def get_fx_at_date(dt):
    if fx_history is None: return 1.6450
    try:
        val = fx_history.asof(dt)
        return float(val) if not pd.isna(val) else 1.6450
    except: return 1.6450

with st.spinner("Calcolo Performance Multi-Currency..."):
    for isin in df_raw['ISIN'].unique():
        m_val = df_raw[df_raw['ISIN'] == isin]['Manual_Override'].iloc[0]
        cache_prezzi[isin] = fetch_price(isin, m_val)

df_raw['Price_Now'] = df_raw['ISIN'].map(cache_prezzi).fillna(df_raw['Prezzo_Acq'])
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Gain_EUR'] = df_raw['Att_EUR'] - df_raw['Inv_EUR']

# Calcolo AUD con logica storica per evidenziare erosione/guadagno cambio
df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['Date_DT'].apply(get_fx_at_date)
df_raw['Att_AUD'] = df_raw['Att_EUR'] * fx_now
df_raw['Gain_AUD'] = df_raw['Att_AUD'] - df_raw['Inv_AUD']

# --- 4. INTERFACCIA ---
st.title("🏛️ Claudio's Portfolio Command Center")
tab1, tab2, tab3, tab4 = st.tabs(["📊 Performance", "💸 Simulatore Tasse", "📈 Storico", "🛠️ System Logs"])

with tab1:
    # --- METRICHE TOTALI (EUR & AUD) ---
    t_inv_eur, t_att_eur = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    t_gain_eur = t_att_eur - t_inv_eur
    t_roi_eur = (t_gain_eur / t_inv_eur) * 100 if t_inv_eur > 0 else 0

    t_inv_aud, t_att_aud = df_raw['Inv_AUD'].sum(), df_raw['Att_AUD'].sum()
    t_gain_aud = t_att_aud - t_inv_aud
    t_roi_aud = (t_gain_aud / t_inv_aud) * 100 if t_inv_aud > 0 else 0
    
    # RIGA 1: Valori Totali
    c1, c2, c3 = st.columns(3)
    c1.metric("Valore Portafoglio (EUR)", f"€{t_att_eur:,.2f}", f"€{t_gain_eur:,.2f}")
    c2.metric("Valore Portafoglio (AUD)", f"${t_att_aud:,.2f}", f"${t_gain_aud:,.2f}")
    c3.metric("Cambio Attuale EUR/AUD", f"{fx_now:.4f}")

    # RIGA 2: ROI & Performance
    c4, c5 = st.columns(2)
    c4.metric("ROI Totale (EUR)", f"{t_roi_eur:.2f}%")
    c5.metric("ROI Totale (AUD)", f"{t_roi_aud:.2f}%")

    st.divider()

    col_pie, col_bar = st.columns([1, 2])
    with col_pie:
        st.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', hole=0.4, title="Allocation (€)"), use_container_width=True)
    with col_bar:
        agg_bar = df_raw.groupby('ISIN').agg({'Gain_EUR': 'sum', 'Gain_AUD': 'sum'}).reset_index()
        fig_b = go.Figure()
        fig_b.add_trace(go.Bar(name='Gain EUR (€)', x=agg_bar['ISIN'], y=agg_bar['Gain_EUR'], marker_color='#1f77b4'))
        fig_b.add_trace(go.Bar(name='Gain AUD ($)', x=agg_bar['ISIN'], y=agg_bar['Gain_AUD'], marker_color='#2ca02c'))
        fig_b.update_layout(barmode='group', title="Gain/Loss per Asset: EUR vs AUD")
        st.plotly_chart(fig_b, use_container_width=True)

    # --- TABELLA DI SINTESI COMPLETA ---
    st.subheader("Riepilogo Analitico Asset")
    
    agg_table = df_raw.groupby('ISIN').agg({
        'Qty': 'sum',
        'Inv_EUR': 'sum', 'Att_EUR': 'sum', 'Gain_EUR': 'sum',
        'Inv_AUD': 'sum', 'Att_AUD': 'sum', 'Gain_AUD': 'sum'
    }).reset_index()
    
    agg_table['ROI % (EUR)'] = (agg_table['Gain_EUR'] / agg_table['Inv_EUR']) * 100
    agg_table['ROI % (AUD)'] = (agg_table['Gain_AUD'] / agg_table['Inv_AUD']) * 100
    
    display_cols = [
        'ISIN', 'Qty', 
        'Inv_EUR', 'Att_EUR', 'Gain_EUR', 'ROI % (EUR)', 
        'Inv_AUD', 'Att_AUD', 'Gain_AUD', 'ROI % (AUD)'
    ]
    
    def color_values(val):
        if isinstance(val, (int, float)):
            return 'color: red' if val < 0 else 'color: green' if val > 0 else ''
        return ''

    st.dataframe(
        agg_table[display_cols].style.format({
            'Qty': '{:,.2f}', 
            'Inv_EUR': '€{:,.2f}', 'Att_EUR': '€{:,.2f}', 'Gain_EUR': '€{:,.2f}', 'ROI % (EUR)': '{:.2f}%',
            'Inv_AUD': '${:,.2f}', 'Att_AUD': '${:,.2f}', 'Gain_AUD': '${:,.2f}', 'ROI % (AUD)': '{:.2f}%'
        }).map(color_values, subset=['Gain_EUR', 'Gain_AUD', 'ROI % (EUR)', 'ROI % (AUD)']),
        use_container_width=True, hide_index=True
    )

with tab2:
    st.subheader("Simulatore CGT (ATO)")
    df_raw['% Vendi'] = 0.0
    ed = st.data_editor(df_raw[['Data', 'ISIN', 'Qty', 'Prezzo_Acq', 'Price_Now', 'Gain_AUD', '% Vendi']], hide_index=True)
    if ed['% Vendi'].sum() > 0:
        sel = ed[ed['% Vendi'] > 0].copy()
        sel['G_Sim'] = sel['Gain_AUD'] * (sel['% Vendi'] / 100)
        tax = sel.apply(lambda r: r['G_Sim']*0.5 if r['G_Sim']>0 and (datetime.now()-pd.to_datetime(r['Data'], dayfirst=True)).days>=365 else r['G_Sim'], axis=1).sum()
        st.info(f"Imponibile stimato: ${max(0, tax):,.2f} AUD")

with tab3:
    st.subheader("Evoluzione Storica")
    h = df_raw.sort_values('Date_DT').copy()
    h['Inv_Cum'] = h['Inv_EUR'].cumsum()
    h['Valore_Cum'] = h['Att_EUR'].cumsum()
    fig_h = go.Figure()
    fig_h.add_trace(go.Scatter(x=h['Date_DT'], y=h['Inv_Cum'], name="Investito", fill='tozeroy', line_color='gray'))
    fig_h.add_trace(go.Scatter(x=h['Date_DT'], y=h['Valore_Cum'], name="Valore Corrente", fill='tonexty', line_color='blue'))
    st.plotly_chart(fig_h, use_container_width=True)

with tab4:
    rows = [{"ISIN": k, "Stato": v["status"], "Ritardo": v["delay"], "Prezzo": f"{cache_prezzi.get(k):.2f} €" if cache_prezzi.get(k) else "N/D"} for k, v in ticker_diag.items()]
    st.table(pd.DataFrame(rows))
