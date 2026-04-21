import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
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
        if isinstance(fx_raw, pd.DataFrame): return fx_raw.iloc[:, 0]
        return fx_raw
    except: return None

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
df_raw['Manual_Price'] = pd.to_numeric(df_input['Price'], errors='coerce') # Il tuo override manuale
df_raw = df_raw.dropna(subset=['ISIN', 'Qty'])
df_raw['Date_DT'] = pd.to_datetime(df_raw['Data'], dayfirst=True)

# --- 3. LOGICA PREZZI (CON FALLBACK) ---
@st.cache_data(ttl=600)
def fetch_live_prices(isins_list):
    prices = {}
    for isin in isins_list:
        symbol = ticker_map.get(isin)
        if symbol:
            try:
                t = yf.Ticker(symbol)
                prices[isin] = float(t.fast_info['last_price'])
            except: prices[isin] = None
        else: prices[isin] = None
    return prices

live_prices = fetch_live_prices(df_raw['ISIN'].unique().tolist())

# Applicazione gerarchia: Manuale > Live > Prezzo Acquisto
def final_price_logic(row):
    if pd.notnull(row['Manual_Price']) and row['Manual_Price'] > 0:
        return row['Manual_Price']
    live = live_prices.get(row['ISIN'])
    if live is not None and live > 0:
        return live
    return row['Prezzo_Acq'] # Fallback finale

df_raw['Price_Now'] = df_raw.apply(final_price_logic, axis=1)

# Calcoli Valutari
fx_now = get_current_fx()
fx_history = get_historical_fx_series()

def get_fx_at_date(dt):
    if fx_history is None: return 1.6450
    try:
        val = fx_history.asof(dt)
        return float(val) if not pd.isna(val) else 1.6450
    except: return 1.6450

df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Gain_EUR'] = df_raw['Att_EUR'] - df_raw['Inv_EUR']
df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['Date_DT'].apply(get_fx_at_date)
df_raw['Att_AUD'] = df_raw['Att_EUR'] * fx_now
df_raw['Gain_AUD'] = df_raw['Att_AUD'] - df_raw['Inv_AUD']

# --- 4. INTERFACCIA ---
st.title("🏛️ Claudio's Portfolio Command Center")
tab1, tab2, tab3 = st.tabs(["📊 Performance", "💸 Simulatore Tasse", "📈 Storico"])

with tab1:
    # Calcolo Totali
    t_inv_eur, t_att_eur = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    t_gain_eur = t_att_eur - t_inv_eur
    t_roi_eur = (t_gain_eur / t_inv_eur) * 100 if t_inv_eur > 0 else 0

    t_inv_aud, t_att_aud = df_raw['Inv_AUD'].sum(), df_raw['Att_AUD'].sum()
    t_gain_aud = t_att_aud - t_inv_aud
    t_roi_aud = (t_gain_aud / t_inv_aud) * 100 if t_inv_aud > 0 else 0

    # Summary On Top Allineata
    c_eur, c_aud, c_mkt = st.columns([1.5, 1.5, 1])
    with c_eur:
        st.metric("Portfolio EUR", f"€{t_att_eur:,.2f}", f"€{t_gain_eur:,.2f}")
        st.metric("ROI Totale EUR", f"{t_roi_eur:.2f}%")
    with c_aud:
        st.metric("Portfolio AUD", f"${t_att_aud:,.2f}", f"${t_gain_aud:,.2f}")
        st.metric("ROI Totale AUD", f"{t_roi_aud:.2f}%")
    with c_mkt:
        st.metric("FX EUR/AUD", f"{fx_now:.4f}")
        st.caption(f"Aggiornato: {datetime.now().strftime('%H:%M:%S')}")

    st.divider()
    
    # Tabella Sintesi
    st.subheader("Riepilogo Asset")
    df_table = df_raw.groupby('ISIN').agg({
        'Inv_EUR':'sum', 'Att_EUR':'sum', 'Gain_EUR':'sum',
        'Inv_AUD':'sum', 'Att_AUD':'sum', 'Gain_AUD':'sum'
    }).reset_index()
    
    st.dataframe(
        df_table.style.format({
            'Inv_EUR': '€{:,.2f}', 'Att_EUR': '€{:,.2f}', 'Gain_EUR': '€{:,.2f}',
            'Inv_AUD': '${:,.2f}', 'Att_AUD': '${:,.2f}', 'Gain_AUD': '${:,.2f}'
        }).map(lambda x: 'color: red' if isinstance(x, (int, float)) and x < 0 else 'color: green' if isinstance(x, (int, float)) and x > 0 else '', 
               subset=['Gain_EUR', 'Gain_AUD']),
        use_container_width=True, hide_index=True
    )

with tab2:
    st.subheader("Simulatore Vendita & Impatto Fiscale (ATO)")
    
    col_param, col_info = st.columns([1, 2])
    with col_param:
        tax_rate = st.slider("Marginal Tax Rate (%)", 0.0, 45.0, 37.0, 0.5)
    
    df_sim = df_raw.copy()
    df_sim['% Vendita'] = 0.0
    
    edited_sim = st.data_editor(
        df_sim[['ISIN', 'Data', 'Qty', 'Att_EUR', 'Gain_EUR', 'Att_AUD', 'Gain_AUD', '% Vendita']],
        column_config={"% Vendita": st.column_config.NumberColumn(format="%d%%", min_value=0, max_value=100)},
        hide_index=True, use_container_width=True
    )

    selling = edited_sim[edited_sim['% Vendita'] > 0].copy()
    if not selling.empty:
        selling['EUR_Realizzato'] = selling['Att_EUR'] * (selling['% Vendita'] / 100)
        selling['AUD_Realizzato'] = selling['Att_AUD'] * (selling['% Vendita'] / 100)
        selling['Gain_AUD_Sim'] = selling['Gain_AUD'] * (selling['% Vendita'] / 100)
        
        def calc_cgt(row):
            if row['Gain_AUD_Sim'] <= 0: return 0.0
            days = (datetime.now() - pd.to_datetime(row['Data'], dayfirst=True)).days
            return row['Gain_AUD_Sim'] * 0.5 if days >= 365 else row['Gain_AUD_Sim']

        selling['Taxable_AUD'] = selling.apply(calc_cgt, axis=1)
        total_tax = selling['Taxable_AUD'].sum() * (tax_rate / 100)
        
        st.divider()
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Cash EUR", f"€{selling['EUR_Realizzato'].sum():,.2f}")
        r2.metric("Cash AUD (Lordo)", f"${selling['AUD_Realizzato'].sum():,.2f}")
        r3.metric("Tasse AUD (Stima)", f"-${total_tax:,.2f}", delta_color="inverse")
        r4.metric("Netto AUD", f"${(selling['AUD_Realizzato'].sum() - total_tax):,.2f}")

with tab3:
    st.plotly_chart(px.line(df_raw.sort_values('Date_DT'), x='Date_DT', y='Att_EUR', title="Evoluzione Valore (€)"), use_container_width=True)
