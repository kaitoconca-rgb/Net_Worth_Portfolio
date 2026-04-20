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

# --- 1. CONFIGURAZIONE ---
st.set_page_config(page_title="Executive Portfolio Console", layout="wide")

ticker_map = {
    "LU2885245055": "8OU9.DE", "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS", 
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "SJPD.AS", "IE00B8GKDB10": "VHYL.MI", 
    "IE00B3RBWM25": "VWRL.AS", "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE", 
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE"
}

# --- 2. FUNZIONI DATI ---
@st.cache_data(ttl=600)
def get_live_data(isin):
    ticker = ticker_map.get(isin)
    try:
        data = yf.download(ticker, period="5d", progress=False)
        if not data.empty:
            if isinstance(data.columns, pd.MultiIndex): data.columns = data.columns.get_level_values(0)
            return float(data['Close'].iloc[-1])
    except: pass
    return 10.76

@st.cache_data(ttl=600)
def get_fx_rate():
    try:
        data = yf.download("EURAUD=X", period="5d", progress=False)
        if isinstance(data.columns, pd.MultiIndex): data.columns = data.columns.get_level_values(0)
        return float(data['Close'].iloc[-1])
    except: return 1.6450

# --- 3. CARICAMENTO E CALCOLI ---
conn = st.connection("gsheets", type=GSheetsConnection)
df_input = conn.read(ttl=0)
df_input.columns = [c.strip() for c in df_input.columns]

df_raw = pd.DataFrame()
df_raw['Data'] = df_input['Fecha Valor']
df_raw['ISIN'] = df_input['ISIN']
df_raw['Qty'] = pd.to_numeric(df_input['Cantidad'], errors='coerce')
df_raw['Inv_EUR'] = pd.to_numeric(df_input['Importe Cargado'], errors='coerce')
df_raw['Manual_Price'] = pd.to_numeric(df_input['Price'], errors='coerce')
df_raw = df_raw.dropna(subset=['ISIN', 'Qty'])
df_raw['Date_DT'] = pd.to_datetime(df_raw['Data'], dayfirst=True)

market_fx = get_fx_rate()
fx_hist = yf.download("EURAUD=X", start="2025-09-01", progress=False)['Close']

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

if st.sidebar.button("🔄 Refresh Data"):
    st.cache_data.clear()
    st.rerun()
tax_rate = st.sidebar.select_slider("Aliquota ATO", options=[0.19, 0.32, 0.37, 0.45, 0.47], value=0.47)

tab1, tab2, tab3 = st.tabs(["📊 Performance Summary", "💸 Detail & Simulator", "📈 History"])

with tab1:
    # Grafico 1: Asset Allocation
    c1, c2 = st.columns([1, 2])
    c1.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', title="Allocation (€)"), use_container_width=True)
    
    # Grafico 2: CONFRONTO GAIN EUR VS AUD (Nuova Richiesta)
    # Aggreghiamo per ISIN per vedere il totale
    agg_plot = df_raw.groupby('ISIN').agg({'Gain_EUR': 'sum', 'Gain_AUD': 'sum'}).reset_index()
    
    fig_comp = go.Figure()
    fig_comp.add_trace(go.Bar(name='Gain/Loss EUR (€)', x=agg_plot['ISIN'], y=agg_plot['Gain_EUR'], marker_color='#3366CC'))
    fig_comp.add_trace(go.Bar(name='Gain/Loss AUD ($)', x=agg_plot['ISIN'], y=agg_plot['Gain_AUD'], marker_color='#109618'))
    
    fig_comp.update_layout(
        title="Comparison: Realized Gain/Loss if Sold Today (EUR vs AUD)",
        barmode='group',
        yaxis_title="Profit/Loss",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
    )
    c2.plotly_chart(fig_comp, use_container_width=True)

    st.subheader("Asset Performance Aggregated")
    agg_table = df_raw.groupby('ISIN').agg({
        'Qty': 'sum', 
        'Inv_EUR': 'sum', 
        'Att_EUR': 'sum', 
        'Gain_EUR': 'sum', 
        'Inv_AUD': 'sum', 
        'Att_AUD': 'sum', 
        'Gain_AUD': 'sum'
    }).reset_index()
    st.dataframe(agg_table.style.format(precision=2), use_container_width=True, hide_index=True)

with tab2:
    st.subheader("Financial Health Summary")
    t_inv_eur, t_att_eur = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    t_inv_aud, t_att_aud = df_raw['Inv_AUD'].sum(), df_raw['Att_AUD'].sum()
    
    summary_df = pd.DataFrame({
        "Currency": ["EURO (€)", "AUD ($)"],
        "Invested": [f"€{t_inv_eur:,.2f}", f"${t_inv_aud:,.2f}"],
        "Current": [f"€{t_att_eur:,.2f}", f"${t_att_aud:,.2f}"],
        "Total Gain": [f"€{(t_att_eur - t_inv_eur):,.2f}", f"${(t_att_aud - t_inv_aud):,.2f}"],
        "ROI": [f"{((t_att_eur-t_inv_eur)/t_inv_eur*100):.2f}%", f"{((t_att_aud-t_inv_aud)/t_inv_aud*100):.2f}%"]
    })
    st.table(summary_df)

    st.subheader("Individual Lots & Tax Simulator")
    df_raw['% Vendi'] = 0.0
    cols_editor = ['Data', 'ISIN', 'Qty', 'Inv_EUR', 'Price_Now', 'Att_EUR', 'Gain_EUR', 'Inv_AUD', 'Att_AUD', 'Gain_AUD', '% Vendi']
    edited = st.data_editor(df_raw[cols_editor], hide_index=True, use_container_width=True)
    
    if edited['% Vendi'].sum() > 0:
        sel = edited[edited['% Vendi'] > 0].copy()
        sel['Days'] = (datetime.now() - pd.to_datetime(sel['Data'], dayfirst=True)).dt.days
        sel['R_Gain_AUD'] = (sel['Qty'] * sel['Price_Now'] * market_fx * sel['% Vendi']/100) - (sel['Inv_AUD'] * sel['% Vendi']/100)
        sel['Taxable'] = sel.apply(lambda r: r['R_Gain_AUD'] * 0.5 if (r['R_Gain_AUD'] > 0 and r['Days'] >= 365) else r['R_Gain_AUD'], axis=1)
        tax = max(0, sel['Taxable'].sum()) * tax_rate
        st.success(f"💰 Netto stimato: **${(sel['R_Gain_AUD'].sum() - tax):,.2f} AUD** (Tasse: ${tax:,.2f})")

with tab3:
    st.subheader("Historical Capital Evolution")
    first_purchase = df_raw['Date_DT'].min()
    if pd.isnull(first_purchase): first_purchase = pd.Timestamp("2025-10-01")
        
    with st.spinner("Calcolo storico progressivo..."):
        all_h_prices = {}
        for isin in df_raw['ISIN'].unique():
            t = ticker_map.get(isin)
            if t:
                h = yf.download(t, start=first_purchase, progress=False)['Close']
                if not h.empty:
                    if isinstance(h, pd.DataFrame): h = h.iloc[:, 0]
                    all_h_prices[isin] = h.reindex(pd.date_range(start=first_purchase, end=datetime.now()), method='ffill')
        
        if all_h_prices:
            dates = pd.date_range(start=first_purchase, end=datetime.now().date())
            history = [sum(all_h_prices[lot['ISIN']].asof(pd.Timestamp(d)) * lot['Qty'] for _, lot in df_raw[df_raw['Date_DT'].dt.date <= d.date()].iterrows() if lot['ISIN'] in all_h_prices) for d in dates]
            hist_df = pd.DataFrame({'Date': dates, 'Value': history})
            if not hist_df.empty:
                hist_df.iloc[-1, hist_df.columns.get_loc('Value')] = df_raw['Att_EUR'].sum()
                fig = px.area(hist_df, x='Date', y='Value', title="Portfolio Growth (€)")
                st.plotly_chart(fig, use_container_width=True)
