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

# --- 3. LOGICA PREZZI (FIXED CACHING) ---
@st.cache_data(ttl=600)
def get_all_prices(isins_list): # Riceve una lista, non un array numpy
    prices = {}
    for isin in isins_list:
        symbol = ticker_map.get(isin)
        try:
            t = yf.Ticker(symbol)
            prices[isin] = float(t.fast_info['last_price'])
        except: prices[isin] = None
    return prices

# FIX: convertiamo l'output di unique() in lista per evitare UnhashableParamError
isins_to_fetch = df_raw['ISIN'].unique().tolist()
prices_now = get_all_prices(isins_to_fetch)

df_raw['Price_Now'] = df_raw['ISIN'].map(prices_now).fillna(df_raw['Prezzo_Acq'])

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
tab1, tab2, tab3, tab4 = st.tabs(["📊 Performance", "💸 Simulatore Tasse", "📈 Storico", "🛠️ System Logs"])

with tab1:
    # Calcolo Totali
    t_inv_eur, t_att_eur = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    t_gain_eur = t_att_eur - t_inv_eur
    t_roi_eur = (t_gain_eur / t_inv_eur) * 100 if t_inv_eur > 0 else 0

    t_inv_aud, t_att_aud = df_raw['Inv_AUD'].sum(), df_raw['Att_AUD'].sum()
    t_gain_aud = t_att_aud - t_inv_aud
    t_roi_aud = (t_gain_aud / t_inv_aud) * 100 if t_inv_aud > 0 else 0

    # Summary On Top Migliorata
    m_col1, m_col2, m_col3 = st.columns(3)
    with m_col1:
        st.metric("Totale Portafoglio EUR", f"€{t_att_eur:,.2f}", f"€{t_gain_eur:,.2f}")
        st.metric("ROI Globale EUR", f"{t_roi_eur:.2f}%")
    with m_col2:
        st.metric("Totale Portafoglio AUD", f"${t_att_aud:,.2f}", f"${t_gain_aud:,.2f}")
        st.metric("ROI Globale AUD", f"{t_roi_aud:.2f}%")
    with m_col3:
        st.metric("FX Rate EUR/AUD", f"{fx_now:.4f}")
        st.info(f"Dati aggiornati al {datetime.now().strftime('%d/%m %H:%M')}")

    st.divider()
    
    # Grafici e Tabella di Sintesi
    c1, c2 = st.columns([1, 2])
    with c1:
        st.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', hole=0.4, title="Asset Allocation"), use_container_width=True)
    with c2:
        agg_bar = df_raw.groupby('ISIN').agg({'Gain_EUR': 'sum', 'Gain_AUD': 'sum'}).reset_index()
        fig = go.Figure()
        fig.add_trace(go.Bar(name='Gain EUR (€)', x=agg_bar['ISIN'], y=agg_bar['Gain_EUR'], marker_color='#1f77b4'))
        fig.add_trace(go.Bar(name='Gain AUD ($)', x=agg_bar['ISIN'], y=agg_bar['Gain_AUD'], marker_color='#2ca02c'))
        fig.update_layout(barmode='group', title="Impatto Cambio su Gain/Loss")
        st.plotly_chart(fig, use_container_width=True)

    # Tabella Analitica
    st.subheader("Dettaglio Asset")
    df_table = df_raw.groupby('ISIN').agg({
        'Qty':'sum', 'Inv_EUR':'sum', 'Att_EUR':'sum', 'Gain_EUR':'sum',
        'Inv_AUD':'sum', 'Att_AUD':'sum', 'Gain_AUD':'sum'
    }).reset_index()
    
    def color_negative_red(val):
        color = 'red' if val < 0 else 'green' if val > 0 else 'inherit'
        return f'color: {color}'

    st.dataframe(
        df_table.style.format({
            'Inv_EUR': '€{:,.2f}', 'Att_EUR': '€{:,.2f}', 'Gain_EUR': '€{:,.2f}',
            'Inv_AUD': '${:,.2f}', 'Att_AUD': '${:,.2f}', 'Gain_AUD': '${:,.2f}'
        }).map(color_negative_red, subset=['Gain_EUR', 'Gain_AUD']),
        use_container_width=True, hide_index=True
    )

with tab2:
    st.subheader("Simulatore Vendita & Capital Gain (ATO)")
    
    s_col1, s_col2 = st.columns([1, 3])
    with s_col1:
        tax_rate = st.slider("Your Marginal Tax Rate (%)", 0.0, 45.0, 37.0, 0.5)
    
    # Preparazione dati simulazione
    df_sim = df_raw.copy()
    df_sim['% Vendita'] = 0.0
    
    # Editor interattivo
    edited_sim = st.data_editor(
        df_sim[['ISIN', 'Data', 'Qty', 'Att_EUR', 'Gain_EUR', 'Att_AUD', 'Gain_AUD', '% Vendita']],
        column_config={"% Vendita": st.column_config.NumberColumn(format="%d%%", min_value=0, max_value=100)},
        hide_index=True, use_container_width=True
    )

    # Calcolo Risultati Simulazione
    selling = edited_sim[edited_sim['% Vendita'] > 0].copy()
    if not selling.empty:
        selling['EUR_Out'] = selling['Att_EUR'] * (selling['% Vendita'] / 100)
        selling['AUD_Out'] = selling['Att_AUD'] * (selling['% Vendita'] / 100)
        selling['Gain_AUD_Sim'] = selling['Gain_AUD'] * (selling['% Vendita'] / 100)
        
        # Logica Fiscale Australia (Discount 50% se > 12 mesi)
        def apply_tax(row):
            if row['Gain_AUD_Sim'] <= 0: return 0.0
            days = (datetime.now() - pd.to_datetime(row['Data'], dayfirst=True)).days
            base_gain = row['Gain_AUD_Sim']
            if days >= 365: base_gain *= 0.5
            return base_gain * (tax_rate / 100)

        selling['Tax_Est'] = selling.apply(apply_tax, axis=1)
        
        total_eur = selling['EUR_Out'].sum()
        total_aud = selling['AUD_Out'].sum()
        total_tax = selling['Tax_Est'].sum()

        st.divider()
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Cash-out EUR", f"€{total_eur:,.2f}")
        r2.metric("Cash-out AUD (Lordo)", f"${total_aud:,.2f}")
        r3.metric("Tasse Stimate (AUD)", f"-${total_tax:,.2f}", delta_color="inverse")
        r4.metric("Netto AUD (Post-Tax)", f"${(total_aud - total_tax):,.2f}")
    else:
        st.info("Trascina o scrivi una percentuale nella colonna '% Vendita' per iniziare.")

with tab3:
    st.subheader("Trend Storico Investimento")
    h_data = df_raw.sort_values('Date_DT').copy()
    h_data['Inv_Cum'] = h_data['Inv_EUR'].cumsum()
    h_data['Val_Cum'] = h_data['Att_EUR'].cumsum()
    fig_h = px.line(h_data, x='Date_DT', y=['Inv_Cum', 'Val_Cum'], 
                    labels={'value': 'EUR', 'Date_DT': 'Data'}, title="Crescita Portafoglio (€)")
    st.plotly_chart(fig_h, use_container_width=True)

with tab4:
    st.write("Diagnostica Prezzi")
    st.write(prices_now)
