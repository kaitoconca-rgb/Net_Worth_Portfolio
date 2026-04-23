import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.graph_objects as go
from datetime import datetime
from streamlit_gsheets import GSheetsConnection

# --- 0. CONFIGURAZIONE E AUTH ---
st.set_page_config(page_title="Executive Portfolio & Tax Console", layout="wide")

if "password_correct" not in st.session_state:
    st.text_input("Password", type="password", on_change=lambda: st.session_state.update({"password_correct": st.session_state["password"] == st.secrets["auth"]["password"]}), key="password")
    if not st.session_state.get("password_correct"): st.stop()

# --- 1. CARICAMENTO DATI (Sorgente: Google Sheets) ---
conn = st.connection("gsheets", type=GSheetsConnection)
df_raw = conn.read(ttl=0).dropna(subset=['ISIN', 'Cantidad'])
df_raw.columns = [c.strip() for c in df_raw.columns]

df = pd.DataFrame({
    'Data': pd.to_datetime(df_raw['Fecha Valor'], dayfirst=True),
    'ISIN': df_raw['ISIN'],
    'Qty': pd.to_numeric(df_raw['Cantidad'], errors='coerce'),
    'Inv_EUR': pd.to_numeric(df_raw['Importe Cargado'], errors='coerce'),
    'P_Acq': pd.to_numeric(df_raw['Precio'], errors='coerce'),
    'Price_Now': pd.to_numeric(df_raw['Price'], errors='coerce') # Sorgente unica per evitare Fallback
})

# Cambio EURAUD attuale
fx_now = yf.Ticker("EURAUD=X").fast_info.get('last_price', 1.65)

# --- 2. LOGICA DI CALCOLO (Baseline Richiesta) ---
df['Att_EUR'] = df['Qty'] * df['Price_Now']
df['Att_AUD'] = df['Att_EUR'] * fx_now
df['Inv_AUD'] = df['Inv_EUR'] * fx_now # Valore investito riportato ad AUD oggi

df['Gain_EUR'] = df['Att_EUR'] - df['Inv_EUR']
df['Gain_AUD'] = df['Att_AUD'] - df['Inv_AUD']

df['Days_Held'] = (pd.Timestamp.now() - df['Data']).dt.days
df['CGT_Discount'] = df['Days_Held'] > 365

# --- 3. INTERFACCIA ---
t1, t2, t3 = st.tabs(["📊 Performance", "💸 Simulatore Tasse ATO", "🛠️ Diagnostics"])

with t1:
    # 1) Metriche in EUR e AUD
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Valore Investito", f"€{df['Inv_EUR'].sum():,.0f}", f"${df['Inv_AUD'].sum():,.0f} AUD")
    c2.metric("Valore Attuale", f"€{df['Att_EUR'].sum():,.0f}", f"${df['Att_AUD'].sum():,.0f} AUD")
    c3.metric("Gain / Loss", f"€{df['Gain_EUR'].sum():,.0f}", f"${df['Gain_AUD'].sum():,.0f} AUD")
    roi_eur = (df['Gain_EUR'].sum() / df['Inv_EUR'].sum()) * 100
    c4.metric("ROI %", f"{roi_eur:.2f}%")

    st.divider()

    # 2) Grafico Gain/Loss EUR vs AUD (Problema 2 risolto)
    st.subheader("Confronto Gain Realizzato: EUR vs AUD (Cambio Attuale)")
    agg = df.groupby('ISIN').agg({'Gain_EUR': 'sum', 'Gain_AUD': 'sum'}).reset_index()
    fig = go.Figure(data=[
        go.Bar(name='Gain EUR', x=agg['ISIN'], y=agg['Gain_EUR'], marker_color='#1f77b4'),
        go.Bar(name='Gain AUD', x=agg['ISIN'], y=agg['Gain_AUD'], marker_color='#ff7f0e')
    ])
    fig.update_layout(barmode='group', title="Effetto Cambio: Se vendessi tutto oggi")
    st.plotly_chart(fig, use_container_width=True)

with t2:
    st.subheader("Simulatore Vendita e Impatto Fiscale ATO")
    p_sell = st.slider("Percentuale di portafoglio da vendere", 0, 100, 100)
    
    # Calcolo CGT
    def calc_tax(row):
        if row['Gain_AUD'] <= 0: return 0.0
        # Regola ATO: 50% sconto se > 1 anno. Aliquota stimata 45%
        discount = 0.5 if row['CGT_Discount'] else 1.0
        return row['Gain_AUD'] * (p_sell/100) * discount * 0.45

    df['Tax_Est'] = df.apply(calc_tax, axis=1)
    
    s1, s2, s3 = st.columns(3)
    s1.metric("Cash Lordo (AUD)", f"${(df['Att_AUD'].sum() * (p_sell/100)):,.0f}")
    s2.metric("Tasse ATO Stimate", f"-${df['Tax_Est'].sum():,.0f}", delta_color="inverse")
    s3.metric("Netto in Tasca (AUD)", f"${(df['Att_AUD'].sum() * (p_sell/100)) - df['Tax_Est'].sum():,.0f}")

    st.divider()
    # Tabella Simulazione con evidenza Sconto CGT
    st.dataframe(
        df[['ISIN', 'Data', 'Qty', 'P_Acq', 'Price_Now', 'CGT_Discount', 'Tax_Est']].style.format(precision=2)
        .map(lambda x: 'background-color: #d4edda' if x is True else '', subset=['CGT_Discount']),
        use_container_width=True
    )

with t3:
    st.write("Dati estratti dal Google Sheet:")
    st.table(df[['ISIN', 'Price_Now', 'Days_Held']])
