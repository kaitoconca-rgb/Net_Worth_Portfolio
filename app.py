import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go
from datetime import datetime, date
from streamlit_gsheets import GSheetsConnection

# --- 0. PROTEZIONE ---
if "password_correct" not in st.session_state:
    st.text_input("Password", type="password", on_change=lambda: st.session_state.update({"password_correct": st.session_state["password"] == st.secrets["auth"]["password"]}), key="password")
    if not st.session_state.get("password_correct"): st.stop()

# --- 1. CONFIGURAZIONE ---
st.set_page_config(page_title="Claudio Executive: Tax & Exit Simulator", layout="wide")

TICKER_MAP = {
    "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "36B2.MU", 
    "IE00B8GKDB10": "VHYL.MI", "IE00B3RBWM25": "VWRL.AS", 
    "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE"
}

@st.cache_data(ttl=1800)
def get_live_price(isin):
    if isin == "LU2885245055": return None
    try:
        t = yf.Ticker(TICKER_MAP.get(isin, ""))
        return t.fast_info.get('last_price')
    except: return None

# --- 2. CARICAMENTO DATI ---
conn = st.connection("gsheets", type=GSheetsConnection)
df_raw = conn.read(ttl=0).dropna(subset=['ISIN', 'Cantidad'])
df_raw.columns = [c.strip() for c in df_raw.columns]

df = pd.DataFrame({
    'Data': pd.to_datetime(df_raw['Fecha Valor'], dayfirst=True),
    'ISIN': df_raw['ISIN'],
    'Qty': pd.to_numeric(df_raw['Cantidad'], errors='coerce'),
    'Inv_EUR': pd.to_numeric(df_raw['Importe Cargado'], errors='coerce'),
    'P_Acq': pd.to_numeric(df_raw['Precio'], errors='coerce'),
    'P_Man': pd.to_numeric(df_raw['Price'], errors='coerce')
})

# Cambio e Prezzi
fx_now = yf.Ticker("EURAUD=X").fast_info.get('last_price', 1.65)
prices = {isin: get_live_price(isin) for isin in df['ISIN'].unique()}

def get_final_price(r):
    if pd.notnull(r['P_Man']) and r['P_Man'] > 0: return r['P_Man']
    return prices.get(r['ISIN']) or r['P_Acq']

df['Price_Now'] = df.apply(get_final_price, axis=1)

# Calcoli base AUD (Cambio attuale applicato a tutto per visione d'uscita)
df['Valore_AUD'] = df['Qty'] * df['Price_Now'] * fx_now
df['Investito_AUD_Attuale'] = df['Inv_EUR'] * fx_now
df['Gain_Lordo_AUD'] = df['Valore_AUD'] - df['Investito_AUD_Attuale']
df['Days_Held'] = (pd.Timestamp.now() - df['Data']).dt.days

# --- 3. INTERFACCIA ---
t1, t2, t3 = st.tabs(["📊 Portfolio", "💰 Simulatore Exit Tax", "🛠️ Debug"])

with t1:
    c1, c2, c3 = st.columns(3)
    c1.metric("Valore Totale AUD", f"${df['Valore_AUD'].sum():,.0f}")
    c2.metric("Gain Lordo AUD", f"${df['Gain_Lordo_AUD'].sum():,.0f}")
    c3.metric("Cambio EUR/AUD", f"{fx_now:.4f}")
    st.divider()
    st.dataframe(df[['ISIN', 'Data', 'Qty', 'P_Acq', 'Price_Now', 'Valore_AUD']].style.format(precision=2), use_container_width=True)

with t2:
    st.subheader("Simulatore Vendita Asset (Logica ATO)")
    
    # PARAMETRI DI SIMULAZIONE
    col_param1, col_param2 = st.columns(2)
    with col_param1:
        percent_to_sell = st.slider("Percentuale di asset da vendere", 0, 100, 100, step=5)
    with col_param2:
        tax_rate = st.number_input("Tua aliquota marginale (%)", value=45.0) / 100

    # Calcolo Simulazione
    sim = df.copy()
    sim['Qty_Sold'] = sim['Qty'] * (percent_to_sell / 100)
    sim['Cash_In_Lordo'] = sim['Qty_Sold'] * sim['Price_Now'] * fx_now
    sim['Cost_Basis_Sold'] = (sim['Inv_EUR'] * (percent_to_sell / 100)) * fx_now
    sim['Gain_Sim'] = sim['Cash_In_Lordo'] - sim['Cost_Basis_Sold']
    
    # Applicazione Automatica Sconto CGT 50%
    def calc_tax(row):
        if row['Gain_Sim'] <= 0: return 0.0
        # Regola ATO: Detenzione > 1 anno = 50% sconto
        discount = 0.5 if row['Days_Held'] > 365 else 1.0
        return row['Gain_Sim'] * discount * tax_rate

    sim['Tax_Due'] = sim.apply(calc_tax, axis=1)
    sim['Net_Cash_In'] = sim['Cash_In_Lordo'] - sim['Tax_Due']
    sim['CGT_Discount_Applied'] = sim['Days_Held'] > 365

    # Riepilogo Simulazione
    s1, s2, s3 = st.columns(3)
    s1.metric("Cash-out Lordo", f"${sim['Cash_In_Lordo'].sum():,.0f}")
    s2.metric("Tasse ATO Stimate", f"-${sim['Tax_Due'].sum():,.0f}", delta_color="inverse")
    s3.metric("Netto in Tasca", f"${sim['Net_Cash_In'].sum():,.0f}")

    st.divider()
    
    # Tabella Simulazione con formattazione automatica
    st.write(f"Dettaglio vendita del {percent_to_sell}% delle posizioni:")
    
    # Visualizzazione pulita
    display_cols = ['ISIN', 'Data', 'Qty_Sold', 'Gain_Sim', 'CGT_Discount_Applied', 'Tax_Due', 'Net_Cash_In']
    st.dataframe(
        sim[display_cols].style.format({
            'Qty_Sold': '{:.2f}', 'Gain_Sim': '${:,.2f}', 
            'Tax_Due': '${:,.2f}', 'Net_Cash_In': '${:,.2f}'
        }).map(lambda x: 'background-color: #d4edda' if x is True else '', subset=['CGT_Discount_Applied']),
        use_container_width=True
    )

with t3:
    st.write("Diagnostica Prezzi:")
    st.table(df[['ISIN', 'Price_Now', 'Days_Held']])
