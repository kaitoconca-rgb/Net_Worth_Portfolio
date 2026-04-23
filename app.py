import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime, date
from streamlit_gsheets import GSheetsConnection

# --- 0. AUTH & CONFIG ---
st.set_page_config(page_title="Executive Portfolio Console", layout="wide")

if "password_correct" not in st.session_state:
    st.text_input("Password", type="password", on_change=lambda: st.session_state.update({"password_correct": st.session_state["password"] == st.secrets["auth"]["password"]}), key="password")
    if not st.session_state.get("password_correct"): st.stop()

# --- 1. DATA ENGINE ---
conn = st.connection("gsheets", type=GSheetsConnection)
df_raw = conn.read(ttl=0).dropna(subset=['ISIN', 'Cantidad'])
df_raw.columns = [c.strip() for c in df_raw.columns]

# Caricamento e pulizia
df = pd.DataFrame({
    'Data': pd.to_datetime(df_raw['Fecha Valor'], dayfirst=True),
    'ISIN': df_raw['ISIN'],
    'Qty': pd.to_numeric(df_raw['Cantidad'], errors='coerce'),
    'Inv_EUR': pd.to_numeric(df_raw['Importe Cargado'], errors='coerce'),
    'P_Acq': pd.to_numeric(df_raw['Precio'], errors='coerce'),
    'P_Sheets': pd.to_numeric(df_raw['Price'], errors='coerce') # Prezzo manuale da GSheets
})

# Cambio EURAUD
fx_now = yf.Ticker("EURAUD=X").fast_info.get('last_price', 1.65)

# Logica Prezzo: Priorità al prezzo inserito in Sheets (Source of Truth)
df['Price_Now'] = df['P_Sheets'].fillna(df['P_Acq']) 

# --- 2. LOGICA FISCALE ATO (BLINDATA) ---
df['Valore_AUD_Attuale'] = df['Qty'] * df['Price_Now'] * fx_now
df['Investito_AUD_Oggi'] = df['Inv_EUR'] * fx_now
df['Gain_AUD'] = df['Valore_AUD_Attuale'] - df['Investito_AUD_Oggi']
df['Days_Held'] = (pd.Timestamp.now() - df['Data']).dt.days

def calc_tax_ato(row):
    if row['Gain_AUD'] <= 0: return 0.0
    # Sconto 50% se detenuto > 365 giorni
    multiplier = 0.5 if row['Days_Held'] > 365 else 1.0
    return row['Gain_AUD'] * multiplier * 0.45 # Aliquota 45%

df['Tax_ATO'] = df.apply(calc_tax_ato, axis=1)
df['Net_AUD'] = df['Gain_AUD'] - df['Tax_ATO']

# --- 3. INTERFACCIA ---
tab1, tab2, tab3 = st.tabs(["📊 Performance", "💸 Simulatore Tasse", "📈 Timeline"])

with tab1:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Investito (EUR)", f"€{df['Inv_EUR'].sum():,.0f}")
    c2.metric("Valore Attuale (AUD)", f"${df['Valore_AUD_Attuale'].sum():,.0f}")
    c3.metric("Gain Lordo (AUD)", f"${df['Gain_AUD'].sum():,.0f}")
    c4.metric("Tasse Stimate (AUD)", f"-${df['Tax_ATO'].sum():,.0f}", delta_color="inverse")

    st.divider()
    
    # Grafico Real Gain EUR vs AUD
    agg = df.groupby('ISIN').agg({'Inv_EUR':'sum', 'Valore_AUD_Attuale':'sum', 'Gain_AUD':'sum'}).reset_index()
    fig = go.Figure(data=[
        go.Bar(name='Gain Lordo AUD', x=agg['ISIN'], y=agg['Gain_AUD'], marker_color='#2ecc71')
    ])
    fig.update_layout(title="Guadagno Lordo per Asset (AUD)")
    st.plotly_chart(fig, use_container_width=True)

with tab2:
    st.subheader("Simulatore Cash-out Proporzionale")
    perc_venda = st.slider("Percentuale di vendita asset", 0, 100, 100)
    
    sim = df.copy()
    sim['Qty_S'] = sim['Qty'] * (perc_venda/100)
    sim['Gain_S'] = sim['Gain_AUD'] * (perc_venda/100)
    sim['Tax_S'] = sim['Tax_ATO'] * (perc_venda/100)
    sim['Cash_Net_AUD'] = (sim['Valore_AUD_Attuale'] * (perc_venda/100)) - sim['Tax_S']

    s1, s2, s3 = st.columns(3)
    s1.metric("Cash Lordo", f"${sim['Valore_AUD_Attuale'].sum()*(perc_venda/100):,.0f}")
    s2.metric("Impatto Fiscale", f"-${sim['Tax_S'].sum():,.0f}")
    s3.metric("Netto Disponibile", f"${sim['Cash_Net_AUD'].sum():,.0f}")

    st.divider()
    # Tabella con evidenza sconto 50%
    st.dataframe(
        sim[['ISIN', 'Data', 'P_Acq', 'Price_Now', 'Gain_S', 'Tax_S', 'Cash_Net_AUD']]
        .style.format(precision=2)
        .map(lambda x: 'background-color: #d4edda' if (pd.Timestamp.now() - pd.to_datetime(sim.loc[sim.index[0], 'Data'])).days > 365 else '', subset=['ISIN']),
        use_container_width=True
    )

with tab3:
    st.info("La Timeline richiede dati storici live. Se i prezzi sono in 'Fallback', questo grafico mostrerà una linea piatta basata sui prezzi di acquisto.")
