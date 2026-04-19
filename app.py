import streamlit as st
import pandas as pd
import yfinance as yf
import os
import plotly.express as px
from datetime import datetime

# --- AGGIUNGI QUESTO PER LA PASSWORD ---
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
    else:
        return True

if not check_password():
    st.stop()  # Ferma l'app qui se la password non è corretta
# ---------------------------------------

# --- 1. CONFIGURAZIONE ---
st.set_page_config(page_title="Executive Portfolio Console", layout="wide")

ticker_map = {
    "LU2885245055": "8OU9.DE", "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS", 
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "SJPD.AS", "IE00B8GKDB10": "VHYL.MI", 
    "IE00B3RBWM25": "VWRL.AS", "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE", 
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE"
}

# --- 2. FUNZIONI CACHED ---
@st.cache_data(ttl=3600)
def get_live_data(isin):
    ticker = ticker_map.get(isin)
    try:
        data = yf.download(ticker, period="5d", progress=False)
        if not data.empty:
            if isinstance(data.columns, pd.MultiIndex): data.columns = data.columns.get_level_values(0)
            return float(data['Close'].iloc[-1])
    except: pass
    return 10.76

@st.cache_data(ttl=3600)
def get_fx_rate():
    try:
        data = yf.download("EURAUD=X", period="5d", progress=False)
        if isinstance(data.columns, pd.MultiIndex): data.columns = data.columns.get_level_values(0)
        return float(data['Close'].iloc[-1])
    except: return 1.6450

# --- 3. IMPORT DATI ---
st.sidebar.header("Data Management")
uploaded_file = st.sidebar.file_uploader("Importa Portfolio CSV", type="csv")

if uploaded_file is not None:
    # Se carichi un file a mano, usa quello
    df_input = pd.read_csv(uploaded_file)
elif os.path.exists("portfolio.csv"):
    # Se NON carichi nulla, guarda se esiste il file 'portfolio.csv' su GitHub
    df_input = pd.read_csv("portfolio.csv")
else:
    st.info("Carica un file CSV o aggiungi portfolio.csv su GitHub per iniziare.")
    st.stop()

# Pulizia e mapping colonne
df_input.columns = [c.strip() for c in df_input.columns]
def find_col(keywords, df):
    for k in keywords:
        for col in df.columns:
            if k.lower() in col.lower(): return col
    return None

c_date, c_isin, c_qty, c_inv = find_col(['data','fecha'], df_input), find_col(['isin'], df_input), find_col(['qty','cant'], df_input), find_col(['inv','imp','euro'], df_input)
df_raw = df_input[[c_date, c_isin, c_qty, c_inv]].copy()
df_raw.columns = ['Data', 'ISIN', 'Qty', 'Inv_EUR']
df_raw['Date_DT'] = pd.to_datetime(df_raw['Data'], dayfirst=True)

# --- 4. ENGINE DI CALCOLO ---
market_fx = get_fx_rate()
fx_hist = yf.download("EURAUD=X", start="2025-01-01", progress=False)['Close']

with st.spinner("Aggiornamento mercati in corso..."):
    df_raw['Price_Now'] = [get_live_data(isin) for isin in df_raw['ISIN']]
    df_raw['FX_Acq'] = df_raw['Date_DT'].apply(lambda x: fx_hist.asof(x) if not fx_hist.empty else 1.63)
    df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
    df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['FX_Acq']
    df_raw['Att_AUD'] = df_raw['Att_EUR'] * market_fx
    df_raw['Gain_AUD'] = df_raw['Att_AUD'] - df_raw['Inv_AUD']

# --- 5. UI ---
st.title("🏛️ Claudio's Executive Portfolio")
st.sidebar.metric("EUR/AUD Spot", f"{market_fx:.4f}")
tax_rate = st.sidebar.select_slider("Aliquota ATO", options=[0.19, 0.32, 0.37, 0.45, 0.47], value=0.47)

tab1, tab2, tab3 = st.tabs(["📊 Performance Summary", "💸 Detail & Simulator", "📈 History"])

with tab1:
    st.subheader("Global Portfolio Health")
    t_inv_eur, t_att_eur = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    t_inv_aud, t_att_aud = df_raw['Inv_AUD'].sum(), df_raw['Att_AUD'].sum()

    summary_df = pd.DataFrame({
        "Currency": ["EURO (€)", "AUSTRALIAN DOLLAR ($)"],
        "Invested": [f"{t_inv_eur:,.2f}", f"{t_inv_aud:,.2f}"],
        "Current": [f"{t_att_eur:,.2f}", f"{t_att_aud:,.2f}"],
        "Gain/Loss": [f"{(t_att_eur - t_inv_eur):,.2f}", f"{(t_att_aud - t_inv_aud):,.2f}"],
        "ROI": [f"{((t_att_eur-t_inv_eur)/t_inv_eur*100):.2f}%", f"{((t_att_aud-t_inv_aud)/t_inv_aud*100):.2f}%"]
    })
    st.table(summary_df)
    
    st.divider()
    agg = df_raw.groupby('ISIN').agg({'Inv_EUR':'sum', 'Att_EUR':'sum', 'Gain_AUD':'sum'}).reset_index()
    c1, c2 = st.columns(2)
    with c1:
        st.dataframe(agg.style.format(precision=2, thousands="."), use_container_width=True, hide_index=True)
    with c2:
        st.plotly_chart(px.bar(agg, x='ISIN', y='Gain_AUD', color='Gain_AUD', color_continuous_scale='RdYlGn'), use_container_width=True)

with tab2:
    st.subheader("Lotti Dettagliati & Simulatore")
    df_raw['% Vendi'] = 0
    # FX_Acq deve essere inclusa per evitare KeyError nel calcolo dei totali
    cols_to_edit = ['Data', 'ISIN', 'Qty', 'Inv_EUR', 'Price_Now', 'Att_EUR', 'Inv_AUD', 'Att_AUD', 'Gain_AUD', 'FX_Acq', '% Vendi']
    
    edited = st.data_editor(
        df_raw[cols_to_edit],
        hide_index=True, use_container_width=True,
        column_config={
            "FX_Acq": None, # Nasconde la colonna ma la mantiene disponibile
            "Price_Now": st.column_config.NumberColumn("Price €", format="%.4f"),
            "% Vendi": st.column_config.NumberColumn("% Sel")
        }
    )

    # Ricalcolo Totali Tab 2
    st.markdown("### 📈 Riepilogo Selezione")
    c_att_eur = (edited['Qty'] * edited['Price_Now']).sum()
    c_inv_aud = (edited['Inv_EUR'] * edited['FX_Acq']).sum() # Ora FX_Acq esiste!
    c_att_aud = c_att_eur * market_fx
    
    m1, m2, m3 = st.columns(3)
    m1.metric("Valore Attuale €", f"€{c_att_eur:,.2f}")
    m2.metric("Valore Attuale $", f"${c_att_aud:,.2f}")
    m3.metric("Gain Totale $", f"${(c_att_aud - c_inv_aud):,.2f}")

    if edited['% Vendi'].sum() > 0:
        st.divider()
        sel = edited[edited['% Vendi'] > 0].copy()
        sel['Days'] = (datetime.now() - pd.to_datetime(sel['Data'], dayfirst=True)).dt.days
        sel['C_Gain_AUD'] = (sel['Qty'] * sel['Price_Now'] * market_fx) - (sel['Inv_EUR'] * sel['FX_Acq'])
        sel['Taxable'] = sel.apply(lambda r: r['C_Gain_AUD'] * 0.5 if (r['C_Gain_AUD'] > 0 and r['Days'] >= 365) else r['C_Gain_AUD'], axis=1)
        
        v_aud = (sel['Qty'] * sel['Price_Now'] * market_fx * sel['% Vendi'] / 100).sum()
        v_tax = (sel['Taxable'] * sel['% Vendi'] / 100).sum() * tax_rate
        st.success(f"**Netto Stimato: ${v_aud - max(0, v_tax):,.2f}** | Tasse: ${v_tax:,.2f}")

with tab3:
    st.subheader("Historical Capital Evolution (€)")
    ticks = [ticker_map.get(i) for i in df_raw['ISIN'].unique() if ticker_map.get(i)]
    h_prices = yf.download(ticks, start="2025-10-01", progress=False)['Close'].ffill()
    
    if not h_prices.empty:
        h_prices.index = pd.to_datetime(h_prices.index)
        daily_val = pd.DataFrame(index=h_prices.index)
        daily_val['Value'] = 0.0
        for date in h_prices.index:
            lots = df_raw[df_raw['Date_DT'] <= date]
            val = 0
            for _, r in lots.iterrows():
                t = ticker_map.get(r['ISIN'])
                p = h_prices.loc[date, t] if (t in h_prices.columns and not pd.isna(h_prices.loc[date, t])) else r['Price_Now']
                val += p * r['Qty']
            daily_val.at[date, 'Value'] = val
        st.plotly_chart(px.area(daily_val, y='Value', color_discrete_sequence=['#00CC96']), use_container_width=True)
