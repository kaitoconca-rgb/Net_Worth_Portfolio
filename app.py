import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
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
    elif not st.session_state["password_correct"]:
        st.text_input("Inserisci Password", type="password", on_change=password_guessed, key="password")
        st.error("😕 Password errata")
        return False
    return True

if not check_password():
    st.stop()

# --- 1. CONFIGURAZIONE & TICKERS ---
st.set_page_config(page_title="Executive Portfolio Console", layout="wide")

# Mapping con ticker alternativi per LU (Amundi MSCI World)
ticker_map = {
    "LU2885245055": "X062.DE", # Ticker primario più stabile
    "IE0032077012": "EQQQ.DE",
    "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE",
    "IE00BFM15T99": "SJPD.AS",
    "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS",
    "IE00B3VVMM84": "VFEM.DE",
    "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI",
    "IE0005042456": "IUSA.DE"
}

# --- 2. FUNZIONI DATI ---
@st.cache_data(ttl=600)
def get_live_data(isin):
    ticker = ticker_map.get(isin)
    if not ticker: return 0.0
    try:
        data = yf.download(ticker, period="5d", progress=False)
        if not data.empty:
            if isinstance(data.columns, pd.MultiIndex): data.columns = data.columns.get_level_values(0)
            return float(data['Close'].iloc[-1])
    except: pass
    return 0.0

@st.cache_data(ttl=600)
def get_fx_rate():
    try:
        data = yf.download("EURAUD=X", period="5d", progress=False)
        if isinstance(data.columns, pd.MultiIndex): data.columns = data.columns.get_level_values(0)
        return float(data['Close'].iloc[-1])
    except: return 1.6450

# --- 3. CARICAMENTO E LOGICA PREZZI ---
conn = st.connection("gsheets", type=GSheetsConnection)
df_input = conn.read(ttl=0)
df_input.columns = [c.strip() for c in df_input.columns]

df_raw = pd.DataFrame()
df_raw['Data'] = df_input['Fecha Valor']
df_raw['ISIN'] = df_input['ISIN']
df_raw['Qty'] = pd.to_numeric(df_input['Cantidad'], errors='coerce')
df_raw['Inv_EUR'] = pd.to_numeric(df_input['Importe Cargado'], errors='coerce')
df_raw['Prezzo_Acq'] = pd.to_numeric(df_input['Precio'], errors='coerce') # Storico

df_raw = df_raw.dropna(subset=['ISIN', 'Qty'])
df_raw['Date_DT'] = pd.to_datetime(df_raw['Data'], dayfirst=True)

manual_prices = pd.to_numeric(df_input['Price'], errors='coerce')
market_fx = get_fx_rate()
fx_hist = yf.download("EURAUD=X", start="2025-09-01", progress=False)['Close']

prices_now = []
for i, row in df_raw.iterrows():
    # A. Priorità 1: Override manuale (colonna Price)
    if i < len(manual_prices) and pd.notnull(manual_prices[i]) and manual_prices[i] > 0:
        prices_now.append(float(manual_prices[i]))
    else:
        # B. Priorità 2: Yahoo Finance
        val = get_live_data(row['ISIN'])
        # C. Priorità 3: Fallback su Prezzo di Acquisto (EVITA LO ZERO)
        if val <= 0:
            val = float(row['Prezzo_Acq']) if pd.notnull(row['Prezzo_Acq']) else 10.0
        prices_now.append(val)

df_raw['Price_Now'] = prices_now

# Calcoli Performance
df_raw['FX_Acq'] = df_raw['Date_DT'].apply(lambda x: fx_hist.asof(x) if not fx_hist.empty else 1.63)
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Gain_EUR'] = df_raw['Att_EUR'] - df_raw['Inv_EUR']
df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['FX_Acq']
df_raw['Att_AUD'] = df_raw['Att_EUR'] * market_fx
df_raw['Gain_AUD'] = df_raw['Att_AUD'] - df_raw['Inv_AUD']

# --- 4. UI ---
st.title("🏛️ Claudio's Executive Portfolio")

if st.sidebar.button("🔄 Refresh Data"):
    st.cache_data.clear()
    st.rerun()

tab1, tab2, tab3 = st.tabs(["📊 Performance Summary", "💸 Dettaglio & Simulatore", "📈 Storia"])

with tab1:
    c1, c2 = st.columns([1, 2])
    c1.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', title="Allocazione Asset (€)"), use_container_width=True)
    
    agg_plot = df_raw.groupby('ISIN').agg({'Gain_EUR': 'sum', 'Gain_AUD': 'sum'}).reset_index()
    fig_comp = go.Figure()
    fig_comp.add_trace(go.Bar(name='Gain/Loss EUR (€)', x=agg_plot['ISIN'], y=agg_plot['Gain_EUR'], marker_color='#3366CC'))
    fig_comp.add_trace(go.Bar(name='Gain/Loss AUD ($)', x=agg_plot['ISIN'], y=agg_plot['Gain_AUD'], marker_color='#109618'))
    fig_comp.update_layout(title="Confronto Guadagno/Perdita (EUR vs AUD)", barmode='group', legend=dict(orientation="h", y=1.1))
    c2.plotly_chart(fig_comp, use_container_width=True)

with tab2:
    st.subheader("Analisi Singoli Lotti")
    df_raw['% Vendi'] = 0.0
    cols_display = ['Data', 'ISIN', 'Qty', 'Prezzo_Acq', 'Price_Now', 'Att_EUR', 'Gain_EUR', 'Gain_AUD', '% Vendi']
    
    st.data_editor(
        df_raw[cols_display], 
        column_config={
            "Prezzo_Acq": st.column_config.NumberColumn("Prezzo Acq (Precio)", format="€%.4f"),
            "Price_Now": st.column_config.NumberColumn("Prezzo Attuale (Live)", format="€%.4f"),
        },
        hide_index=True, 
        use_container_width=True
    )

with tab3:
    st.subheader("Evoluzione Storica")
    # Partenza dal tuo investimento di Ottobre
    first_p = df_raw['Date_DT'].min() if not df_raw.empty else pd.Timestamp("2025-10-01")
        
    with st.spinner("Calcolo storico..."):
        all_h_prices = {}
        for isin in df_raw['ISIN'].unique():
            t = ticker_map.get(isin)
            if t:
                h = yf.download(t, start=first_p, progress=False)['Close']
                if not h.empty:
                    if isinstance(h, pd.DataFrame): h = h.iloc[:, 0]
                    all_h_prices[isin] = h.reindex(pd.date_range(start=first_p, end=datetime.now()), method='ffill')
        
        if all_h_prices:
            dates = pd.date_range(start=first_p, end=datetime.now().date())
            history = []
            for d in dates:
                active_lots = df_raw[df_raw['Date_DT'].dt.date <= d.date()]
                val = sum(all_h_prices[l['ISIN']].asof(pd.Timestamp(d)) * l['Qty'] for _, l in active_lots.iterrows() if l['ISIN'] in all_h_prices)
                history.append(val)
            
            hist_df = pd.DataFrame({'Data': dates, 'Valore': history})
            st.plotly_chart(px.area(hist_df, x='Data', y='Valore', title="Crescita Portafoglio (€)"), use_container_width=True)
