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
    return st.session_state.get("password_correct", False)

if not check_password():
    st.stop()

# --- 1. CONFIGURAZIONE & MAPPING ---
st.set_page_config(page_title="Executive Portfolio Console", layout="wide")

ticker_map = {
    "LU2885245055": "8OU9.DE",  # S&P 500 Swap
    "IE0032077012": "EQQQ.DE",  # Nasdaq 100
    "IE00B02KXL92": "DJMC.AS",  # DJ MidCap
    "IE0008471009": "EXW1.DE",  # Euro Stoxx 50
    "IE00BFM15T99": "SJP6.DE",  # Japan (Mirato a 7.02€)
    "IE00B8GKDB10": "VHYL.MI",  # All-World High Div
    "IE00B3RBWM25": "VWRL.AS",  # Vanguard All-World
    "IE00B3VVMM84": "VFEM.DE",  # Emerging Markets
    "IE00B3XXRP09": "VUSA.DE",  # S&P 500
    "IE00BZ56RN96": "GGRW.MI",  # Global Quality Div
    "IE0005042456": "IUSA.DE"   # S&P 500 Dist
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

# --- 3. LOGICA PREZZI LIVE CON RETRY ---
def fetch_live_price(isin, manual_val):
    if pd.notnull(manual_val) and manual_val > 0:
        return float(manual_val)
    
    symbol = ticker_map.get(isin)
    if symbol:
        for _ in range(2): # Prova 2 volte in caso di errore di rete
            try:
                ticker = yf.Ticker(symbol)
                hist = ticker.history(period="1d", interval="1m")
                if not hist.empty:
                    return float(hist['Close'].iloc[-1])
            except:
                continue
    return None

market_fx = get_fx_rate()
fx_hist = yf.download("EURAUD=X", start="2025-09-01", progress=False)['Close']

with st.spinner("Sincronizzazione mercati in corso..."):
    prices_now = []
    for _, row in df_raw.iterrows():
        p = fetch_live_price(row['ISIN'], row['Manual_Override'])
        if p is None:
            p = float(row['Prezzo_Acq'])
            st.warning(f"⚠️ Dati non disponibili per {row['ISIN']}. Utilizzato ultimo prezzo noto.")
        prices_now.append(p)

df_raw['Price_Now'] = prices_now
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Gain_EUR'] = df_raw['Att_EUR'] - df_raw['Inv_EUR']
df_raw['FX_Acq'] = df_raw['Date_DT'].apply(lambda x: fx_hist.asof(x) if not fx_hist.empty else 1.63)
df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['FX_Acq']
df_raw['Att_AUD'] = df_raw['Att_EUR'] * market_fx
df_raw['Gain_AUD'] = df_raw['Att_AUD'] - df_raw['Inv_AUD']

# --- 4. INTERFACCIA ---
st.title("🏛️ Claudio's Portfolio Command Center")

tab1, tab2, tab3 = st.tabs(["📊 Performance", "💸 Simulatore Tasse", "📈 Storico"])

with tab1:
    t_inv_eur, t_att_eur = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    t_inv_aud, t_att_aud = df_raw['Inv_AUD'].sum(), df_raw['Att_AUD'].sum()

    col_m1, col_m2 = st.columns(2)
    with col_m1:
        st.metric("Patrimonio in Euro", f"€{t_att_eur:,.2f}", f"€{(t_att_eur-t_inv_eur):,.2f}")
    with col_m2:
        st.metric("Patrimonio in AUD", f"${t_att_aud:,.2f}", f"${(t_att_aud-t_inv_aud):,.2f}")

    st.subheader("Asset Allocation")
    st.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', hole=0.4), use_container_width=True)

    st.subheader("Dettaglio Portafoglio")
    st.dataframe(df_raw[['Data', 'ISIN', 'Qty', 'Prezzo_Acq', 'Price_Now', 'Gain_EUR', 'Gain_AUD']].style.format(precision=2), use_container_width=True, hide_index=True)

with tab2:
    st.subheader("Simulazione Vendita Lotti")
    df_raw['% Vendi'] = 0.0
    ed_df = st.data_editor(
        df_raw[['Data', 'ISIN', 'Qty', 'Price_Now', 'Gain_AUD', '% Vendi']],
        column_config={"% Vendi": st.column_config.NumberColumn("Vendi %", min_value=0, max_value=100, format="%d%%")},
        hide_index=True, use_container_width=True
    )

    if ed_df['% Vendi'].sum() > 0:
        sim = ed_df[ed_df['% Vendi'] > 0].copy()
        sim['Days'] = (datetime.now() - pd.to_datetime(sim['Data'], dayfirst=True)).dt.days
        sim['G_AUD_Sim'] = sim['Gain_AUD'] * (sim['% Vendi'] / 100)
        sim['Taxable_AUD'] = sim.apply(lambda r: r['G_AUD_Sim'] * 0.5 if (r['G_AUD_Sim'] > 0 and r['Days'] >= 365) else r['G_AUD_Sim'], axis=1)
        
        t_gain_aud = sim['G_AUD_Sim'].sum()
        t_tax_aud = max(0, sim['Taxable_AUD'].sum()) * 0.47
        
        st.success(f"**Plusvalenza Lorda:** ${t_gain_aud:,.2f} AUD | **Tasse ATO stimate:** ${t_tax_aud:,.2f} AUD | **Netto:** ${(t_gain_aud-t_tax_aud):,.2f} AUD")

with tab3:
    st.subheader("Evoluzione Totale Patrimonio (€)")
    s_date = df_raw['Date_DT'].min()
    with st.spinner("Generazione grafico storico..."):
        h_data = {}
        for isin in df_raw['ISIN'].unique():
            tk = ticker_map.get(isin)
            if tk:
                px_h = yf.download(tk, start=s_date, progress=False)['Close']
                if not px_h.empty:
                    if isinstance(px_h, pd.DataFrame): px_h = px_h.iloc[:, 0]
                    h_data[isin] = px_h.reindex(pd.date_range(s_date, datetime.now()), method='ffill')
        
        d_range = pd.date_range(s_date, datetime.now().date())
        vals = [sum([l['Qty'] * (h_data[l['ISIN']].asof(d) if l['ISIN'] in h_data else l['Prezzo_Acq']) for _, l in df_raw[df_raw['Date_DT'].dt.date <= d.date()].iterrows()]) for d in d_range]
        st.plotly_chart(px.area(pd.DataFrame({'Data': d_range, 'Valore': vals}), x='Data', y='Valore'), use_container_width=True)
