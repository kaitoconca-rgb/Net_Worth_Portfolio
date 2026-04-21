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

# --- 3. LOGICA CORE ---
fx_now = get_current_fx()
fx_history = get_historical_fx_series()

def get_fx_at_date(dt):
    if fx_history is None: return 1.6450
    try:
        val = fx_history.asof(dt)
        return float(val) if not pd.isna(val) else 1.6450
    except: return 1.6450

@st.cache_data(ttl=600)
def get_all_prices(isins):
    prices = {}
    for isin in isins:
        symbol = ticker_map.get(isin)
        try:
            t = yf.Ticker(symbol)
            prices[isin] = float(t.fast_info['last_price'])
        except: prices[isin] = None
    return prices

prices_now = get_all_prices(df_raw['ISIN'].unique())
df_raw['Price_Now'] = df_raw['ISIN'].map(prices_now).fillna(df_raw['Prezzo_Acq'])

# Calcoli base
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Gain_EUR'] = df_raw['Att_EUR'] - df_raw['Inv_EUR']
df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['Date_DT'].apply(get_fx_at_date)
df_raw['Att_AUD'] = df_raw['Att_EUR'] * fx_now
df_raw['Gain_AUD'] = df_raw['Att_AUD'] - df_raw['Inv_AUD']

# --- 4. INTERFACCIA ---
st.title("🏛️ Claudio's Portfolio Command Center")
tab1, tab2, tab3, tab4 = st.tabs(["📊 Performance", "💸 Simulatore Tasse", "📈 Storico", "🛠️ System Logs"])

with tab1:
    # Totali per allineamento summary
    t_inv_eur, t_att_eur = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    t_gain_eur = t_att_eur - t_inv_eur
    t_roi_eur = (t_gain_eur / t_inv_eur) * 100 if t_inv_eur > 0 else 0

    t_inv_aud, t_att_aud = df_raw['Inv_AUD'].sum(), df_raw['Att_AUD'].sum()
    t_gain_aud = t_att_aud - t_inv_aud
    t_roi_aud = (t_gain_aud / t_inv_aud) * 100 if t_inv_aud > 0 else 0

    # Summary On Top Allineata
    col_eur, col_aud, col_fx = st.columns([1.2, 1.2, 0.6])
    with col_eur:
        st.subheader("Performance EUR")
        st.metric("Valore Totale", f"€{t_att_eur:,.2f}", f"€{t_gain_eur:,.2f}")
        st.metric("ROI Totale EUR", f"{t_roi_eur:.2f}%")
    with col_aud:
        st.subheader("Performance AUD")
        st.metric("Valore Totale", f"${t_att_aud:,.2f}", f"${t_gain_aud:,.2f}")
        st.metric("ROI Totale AUD", f"{t_roi_aud:.2f}%")
    with col_fx:
        st.subheader("Market")
        st.metric("EUR/AUD", f"{fx_now:.4f}")
        st.caption(f"Aggiornato: {datetime.now().strftime('%H:%M:%S')}")

    st.divider()
    
    # Grafici
    c1, c2 = st.columns([1, 2])
    with c1:
        st.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', hole=0.4, title="Asset Allocation"), use_container_width=True)
    with c2:
        agg_bar = df_raw.groupby('ISIN').agg({'Gain_EUR': 'sum', 'Gain_AUD': 'sum'}).reset_index()
        fig = go.Figure()
        fig.add_trace(go.Bar(name='Gain EUR', x=agg_bar['ISIN'], y=agg_bar['Gain_EUR'], marker_color='#1f77b4'))
        fig.add_trace(go.Bar(name='Gain AUD', x=agg_bar['ISIN'], y=agg_bar['Gain_AUD'], marker_color='#2ca02c'))
        fig.update_layout(barmode='group', title="Delta Gain: EUR vs AUD (FX Impact)")
        st.plotly_chart(fig, use_container_width=True)

    # Tabella blindata
    st.dataframe(
        df_raw.groupby('ISIN').agg({
            'Inv_EUR':'sum', 'Att_EUR':'sum', 'Gain_EUR':'sum',
            'Inv_AUD':'sum', 'Att_AUD':'sum', 'Gain_AUD':'sum'
        }).style.format("€{:,.2f}").format({"Inv_AUD":"${:,.2f}", "Att_AUD":"${:,.2f}", "Gain_AUD":"${:,.2f}"}),
        use_container_width=True
    )

with tab2:
    st.subheader("Simulatore Vendita & Impatto Fiscale (ATO)")
    
    col_sim1, col_sim2 = st.columns([1, 3])
    with col_sim1:
        tax_rate = st.slider("Marginal Tax Rate (%)", 0, 45, 37, step=1)
        st.info("Nota: Gli asset detenuti da >1 anno godono del 50% di sconto CGT.")

    # Data Editor per simulazione
    df_sim = df_raw.copy()
    df_sim['% Vendita'] = 0.0
    
    edited_df = st.data_editor(
        df_sim[['ISIN', 'Data', 'Qty', 'Att_EUR', 'Gain_EUR', 'Att_AUD', 'Gain_AUD', '% Vendita']],
        hide_index=True,
        column_config={
            "% Vendita": st.column_config.NumberColumn(format="%.0f%%", min_value=0, max_value=100)
        },
        use_container_width=True
    )

    # Calcolo Impatto
    sel = edited_df[edited_df['% Vendita'] > 0].copy()
    if not sel.empty:
        sel['EUR_Realizzato'] = sel['Att_EUR'] * (sel['% Vendita'] / 100)
        sel['AUD_Realizzato'] = sel['Att_AUD'] * (sel['% Vendita'] / 100)
        sel['Gain_AUD_Sim'] = sel['Gain_AUD'] * (sel['% Vendita'] / 100)
        
        # Logica CGT Discount (12 mesi)
        def calc_tax(row):
            days = (datetime.now() - pd.to_datetime(row['Data'], dayfirst=True)).days
            taxable_gain = row['Gain_AUD_Sim']
            if taxable_gain <= 0: return 0.0
            if days >= 365: taxable_gain *= 0.5
            return taxable_gain * (tax_rate / 100)

        sel['Tax_AUD'] = sel.apply(calc_tax, axis=1)
        
        # Visualizzazione Risultati
        res_eur = sel['EUR_Realizzato'].sum()
        res_aud = sel['AUD_Realizzato'].sum()
        total_tax = sel['Tax_AUD'].sum()
        net_aud = res_aud - total_tax

        st.divider()
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Liquidità EUR", f"€{res_eur:,.2f}")
        r2.metric("Liquidità AUD (Lorda)", f"${res_aud:,.2f}")
        r3.metric("Tasse Stimate (AUD)", f"-${total_tax:,.2f}", delta_color="inverse")
        r4.metric("Netto AUD (Post-Tax)", f"${net_aud:,.2f}")
    else:
        st.write("Inserisci una percentuale di vendita nella tabella sopra per vedere l'impatto.")

with tab3:
    st.subheader("Evoluzione Storica Portafoglio")
    # Logica semplificata per grafico storico
    h = df_raw.sort_values('Date_DT').copy()
    h['Inv_Cum'] = h['Inv_EUR'].cumsum()
    h['Valore_Cum'] = h['Att_EUR'].cumsum()
    fig_h = px.area(h, x='Date_DT', y=['Inv_Cum', 'Valore_Cum'], title="Trend Investito vs Attuale (€)")
    st.plotly_chart(fig_h, use_container_width=True)

with tab4:
    st.write("Diagnostica Prezzi Live")
    st.json(prices_now)
