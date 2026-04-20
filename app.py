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

# --- 1. CONFIGURAZIONE & MAPPATURA ---
st.set_page_config(page_title="Executive Portfolio Console", layout="wide")

ticker_map = {
    "LU2885245055": "8OU9.DE", "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS", 
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "SJPD.AS", "IE00B8GKDB10": "VHYL.MI", 
    "IE00B3RBWM25": "VWRL.AS", "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE", 
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE"
}

# --- 2. CARICAMENTO DATI ---
@st.cache_data(ttl=600)
def get_live_data(isin):
    ticker = ticker_map.get(isin)
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

conn = st.connection("gsheets", type=GSheetsConnection)
df_input = conn.read(ttl=0)
df_input.columns = [c.strip() for c in df_input.columns]

# Mappatura colonne
df_raw = pd.DataFrame()
df_raw['Data'] = df_input['Fecha Valor']
df_raw['ISIN'] = df_input['ISIN']
df_raw['Qty'] = pd.to_numeric(df_input['Cantidad'], errors='coerce')
df_raw['Inv_EUR'] = pd.to_numeric(df_input['Importe Cargado'], errors='coerce')
df_raw['Manual_Price'] = pd.to_numeric(df_input['Price'], errors='coerce')
df_raw = df_raw.dropna(subset=['ISIN'])
df_raw['Date_DT'] = pd.to_datetime(df_raw['Data'], dayfirst=True)

# --- 3. LOGICA DI CALCOLO (AUD & EUR) ---
market_fx = get_fx_rate()
fx_hist = yf.download("EURAUD=X", start="2025-01-01", progress=False)['Close']

prices_now = []
for _, row in df_raw.iterrows():
    if pd.notnull(row['Manual_Price']) and row['Manual_Price'] > 0:
        prices_now.append(float(row['Manual_Price']))
    else:
        prices_now.append(get_live_data(row['ISIN']))

df_raw['Price_Now'] = prices_now
df_raw['FX_Acq'] = df_raw['Date_DT'].apply(lambda x: fx_hist.asof(x) if not fx_hist.empty else 1.63)
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Gain_EUR'] = df_raw['Att_EUR'] - df_raw['Inv_EUR']
df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['FX_Acq']
df_raw['Att_AUD'] = df_raw['Att_EUR'] * market_fx
df_raw['Gain_AUD'] = df_raw['Att_AUD'] - df_raw['Inv_AUD']

# --- 4. UI ---
st.title("🏛️ Claudio's Executive Portfolio")

# Sidebar
if st.sidebar.button("🔄 Refresh Data"):
    st.cache_data.clear()
    st.rerun()
st.sidebar.metric("EUR/AUD Spot", f"{market_fx:.4f}")
tax_rate = st.sidebar.select_slider("Aliquota ATO", options=[0.19, 0.32, 0.37, 0.45, 0.47], value=0.47)

tab1, tab2, tab3 = st.tabs(["📊 Performance Summary", "💸 Detail & Simulator", "📈 History"])

with tab1:
    c1, c2 = st.columns(2)
    # Pie Chart - Asset Allocation EUR
    fig_pie = px.pie(df_raw, values='Att_EUR', names='ISIN', title="Allocation by Asset (€)")
    c1.plotly_chart(fig_pie, use_container_width=True)
    
    # Bar Chart - Gain/Loss AUD (Acquisition vs Now)
    fig_bar = px.bar(df_raw, x='ISIN', y='Gain_AUD', color='Gain_AUD', 
                     color_continuous_scale='RdYlGn', title="Net Gain/Loss per Asset ($ AUD)")
    c2.plotly_chart(fig_bar, use_container_width=True)

    st.divider()
    # Tabella Summary
    t_inv_eur, t_att_eur = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    t_inv_aud, t_att_aud = df_raw['Inv_AUD'].sum(), df_raw['Att_AUD'].sum()
    
    st.subheader("Global Health Score")
    summary_data = {
        "Currency": ["EURO (€)", "AUD ($)"],
        "Invested": [f"€{t_inv_eur:,.2f}", f"${t_inv_aud:,.2f}"],
        "Current": [f"€{t_att_eur:,.2f}", f"${t_att_aud:,.2f}"],
        "Total Gain": [f"€{(t_att_eur - t_inv_eur):,.2f}", f"${(t_att_aud - t_inv_aud):,.2f}"],
        "ROI": [f"{((t_att_eur-t_inv_eur)/t_inv_eur*100):.2f}%", f"{((t_att_aud-t_inv_aud)/t_inv_aud*100):.2f}%"]
    }
    st.table(pd.DataFrame(summary_data))

with tab2:
    st.subheader("Asset Detail (Including EUR Gain)")
    cols_to_show = ['Data', 'ISIN', 'Qty', 'Inv_EUR', 'Price_Now', 'Att_EUR', 'Gain_EUR', 'Inv_AUD', 'Att_AUD', 'Gain_AUD', 'FX_Acq']
    df_raw['% Vendi'] = 0.0
    
    edited = st.data_editor(
        df_raw[cols_to_show + ['% Vendi']], 
        hide_index=True, use_container_width=True,
        column_config={"FX_Acq": None, "Gain_EUR": st.column_config.NumberColumn("Gain €", format="%.2f")}
    )
    
    # Simulatore Tasse
    if edited['% Vendi'].sum() > 0:
        sel = edited[edited['% Vendi'] > 0].copy()
        sel['Days'] = (datetime.now() - pd.to_datetime(sel['Data'], dayfirst=True)).dt.days
        sel['Realized_Gain_AUD'] = (sel['Qty'] * sel['Price_Now'] * market_fx * sel['% Vendi']/100) - (sel['Inv_EUR'] * sel['FX_Acq'] * sel['% Vendi']/100)
        sel['Taxable'] = sel.apply(lambda r: r['Realized_Gain_AUD'] * 0.5 if (r['Realized_Gain_AUD'] > 0 and r['Days'] >= 365) else r['Realized_Gain_AUD'], axis=1)
        
        v_netto = sel['Realized_Gain_AUD'].sum() - (max(0, sel['Taxable'].sum()) * tax_rate)
        st.success(f"💰 Netto stimato dalla vendita: **${v_netto:,.2f} AUD** (Tasse stimate: ${max(0, sel['Taxable'].sum()) * tax_rate:,.2f})")

with tab3:
    st.subheader("Capital Evolution")
    # Scarichiamo la storia dei ticker presenti nel portafoglio
    unique_tickers = [ticker_map[i] for i in df_raw['ISIN'].unique() if i in ticker_map]
    hist_data = yf.download(unique_tickers, start="2025-01-01", progress=False)['Close'].ffill()
    
    if not hist_data.empty:
        # Calcoliamo il valore storico del portafoglio giorno per giorno
        daily_portfolio = pd.DataFrame(index=hist_data.index)
        daily_portfolio['Total_Value_EUR'] = 0.0
        
        for d in hist_data.index:
            active_lots = df_raw[df_raw['Date_DT'] <= d]
            current_val = 0
            for _, lot in active_lots.iterrows():
                t = ticker_map.get(lot['ISIN'])
                if t in hist_data.columns:
                    price_at_date = hist_data.loc[d, t]
                    current_val += price_at_date * lot['Qty']
            daily_portfolio.loc[d, 'Total_Value_EUR'] = current_val
            
        fig_hist = px.area(daily_portfolio, y='Total_Value_EUR', title="Portfolio Value Evolution (€)")
        st.plotly_chart(fig_hist, use_container_width=True)
