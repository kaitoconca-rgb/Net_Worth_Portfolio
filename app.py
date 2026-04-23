import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go
from datetime import datetime
from streamlit_gsheets import GSheetsConnection

# --- CONFIGURAZIONE ---
st.set_page_config(page_title="Executive Portfolio Baseline", layout="wide")

# --- AUTH ---
if "password_correct" not in st.session_state:
    st.text_input("Password", type="password", on_change=lambda: st.session_state.update({"password_correct": st.session_state["password"] == st.secrets["auth"]["password"]}), key="password")
    if not st.session_state.get("password_correct"): st.stop()

# --- DATA ENGINE ---
conn = st.connection("gsheets", type=GSheetsConnection)
df_raw = conn.read(ttl=0).dropna(subset=['ISIN', 'Cantidad'])
df_raw.columns = [c.strip() for c in df_raw.columns]

df = pd.DataFrame({
    'Data': pd.to_datetime(df_raw['Fecha Valor'], dayfirst=True),
    'ISIN': df_raw['ISIN'],
    'Qty': pd.to_numeric(df_raw['Cantidad'], errors='coerce'),
    'Inv_EUR': pd.to_numeric(df_raw['Importe Cargado'], errors='coerce'),
    'P_Acq': pd.to_numeric(df_raw['Precio'], errors='coerce'),
    'P_Sheets': pd.to_numeric(df_raw['Price'], errors='coerce')
})

# Cambio EURAUD Odierno
fx_now = yf.Ticker("EURAUD=X").fast_info.get('last_price', 1.65)

# LOGICA PREZZO ATTUALE (La tua Baseline)
# Se P_Sheets esiste usa quello, altrimenti usa P_Acq (evita lo zero)
df['Price_Now'] = df['P_Sheets'].fillna(df['P_Acq'])

# --- LOGICA FISCALE ATO ---
df['Valore_EUR'] = df['Qty'] * df['Price_Now']
df['Valore_AUD'] = df['Valore_EUR'] * fx_now
df['Investito_AUD_FX_Oggi'] = df['Inv_EUR'] * fx_now
df['Gain_AUD'] = df['Valore_AUD'] - df['Investito_AUD_FX_Oggi']
df['Days_Held'] = (pd.Timestamp.now() - df['Data']).dt.days

# Calcolo Tassa (Sconto 50% se > 365 giorni)
def calc_tax(row):
    if row['Gain_AUD'] <= 0: return 0.0
    discount = 0.5 if row['Days_Held'] > 365 else 1.0
    return row['Gain_AUD'] * discount * 0.45

df['Tax_ATO'] = df.apply(calc_tax, axis=1)
df['Net_AUD'] = df['Valore_AUD'] - df['Tax_ATO']

# --- INTERFACCIA ---
t1, t2, t3 = st.tabs(["📊 Performance", "💸 Simulatore Tasse", "🛠️ Diagnostics"])

with t1:
    # Metriche principali
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Investito (EUR)", f"€{df['Inv_EUR'].sum():,.0f}")
    m2.metric("Valore Attuale (AUD)", f"${df['Valore_AUD'].sum():,.0f}")
    m3.metric("Gain Lordo (AUD)", f"${df['Gain_AUD'].sum():,.0f}")
    m4.metric("Tassa Est. ATO", f"-${df['Tax_ATO'].sum():,.0f}", delta_color="inverse")

    st.divider()
    
    # Tabella Dettagliata
    st.subheader("Dettaglio Asset (EUR / AUD)")
    st.dataframe(df[['ISIN', 'Data', 'Inv_EUR', 'Valore_EUR', 'Valore_AUD', 'Gain_AUD', 'Tax_ATO']].style.format(precision=2), use_container_width=True)

with t2:
    st.subheader("Simulatore Cash-out (Logica d'Uscita)")
    perc_sale = st.slider("Quanto vuoi vendere del portafoglio? (%)", 0, 100, 100)
    
    sim_gain = df['Gain_AUD'].sum() * (perc_sale / 100)
    sim_tax = df['Tax_ATO'].sum() * (perc_sale / 100)
    sim_cash = (df['Valore_AUD'].sum() * (perc_sale / 100)) - sim_tax
    
    s1, s2, s3 = st.columns(3)
    s1.metric("Incasso Lordo", f"${df['Valore_AUD'].sum()*(perc_sale/100):,.0f}")
    s2.metric("Tasse ATO Pro-quota", f"-${sim_tax:,.0f}")
    s3.metric("Netto Cash-in", f"${sim_cash:,.0f}")

with t3:
    st.write("Verifica dei prezzi usati (Source of Truth):")
    st.table(df[['ISIN', 'Price_Now', 'Days_Held']])
