import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
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
        st.text_input("Password", type="password", on_change=password_guessed, key="password")
        return False
    return st.session_state.get("password_correct", False)

if not check_password():
    st.stop()

# --- 1. CONFIGURAZIONE ---
st.set_page_config(page_title="Executive Portfolio Console", layout="wide")

ticker_map = {
    "LU2885245055": "X062.DE", "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "SJPD.AS", "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS", "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE"
}

@st.cache_data(ttl=600)
def get_fx_rate():
    try:
        d = yf.download("EURAUD=X", period="5d", progress=False)
        return float(d['Close'].iloc[-1])
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
df_raw['Prezzo_Acq'] = pd.to_numeric(df_input['Precio'], errors='coerce') # PRECIO storico

df_raw = df_raw.dropna(subset=['ISIN', 'Qty'])
df_raw['Date_DT'] = pd.to_datetime(df_raw['Data'], dayfirst=True)

# Logica Prezzo Attuale (Live o Manuale colonna Price)
manual_prices = pd.to_numeric(df_input['Price'], errors='coerce')
market_fx = get_fx_rate()

prices_now = []
for i, row in df_raw.iterrows():
    if i < len(manual_prices) and pd.notnull(manual_prices[i]) and manual_prices[i] > 0:
        prices_now.append(float(manual_prices[i]))
    else:
        try:
            t = ticker_map.get(row['ISIN'])
            val = yf.download(t, period="5d", progress=False)['Close'].iloc[-1]
            prices_now.append(float(val))
        except:
            prices_now.append(float(row['Prezzo_Acq']))

df_raw['Price_Now'] = prices_now
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Gain_EUR'] = df_raw['Att_EUR'] - df_raw['Inv_EUR']
# Semplificazione AUD per velocità
df_raw['Att_AUD'] = df_raw['Att_EUR'] * market_fx

# --- 3. UI ---
st.title("🏛️ Claudio's Executive Portfolio")

tab1, tab2, tab3 = st.tabs(["📊 Riepilogo", "💸 Dettagli", "📈 Storia Evolutiva"])

with tab1:
    # Metriche
    c1, c2, c3 = st.columns(3)
    c1.metric("Totale Investito (€)", f"€{df_raw['Inv_EUR'].sum():,.2f}")
    c2.metric("Valore Attuale (€)", f"€{df_raw['Att_EUR'].sum():,.2f}")
    c3.metric("ROI Globale", f"{((df_raw['Att_EUR'].sum()/df_raw['Inv_EUR'].sum())-1)*100:.2f}%")

    # Grafici
    col_left, col_right = st.columns([1, 2])
    with col_left:
        st.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', title="Allocazione"), use_container_width=True)
    with col_right:
        agg = df_raw.groupby('ISIN')['Gain_EUR'].sum().reset_index()
        st.plotly_chart(px.bar(agg, x='ISIN', y='Gain_EUR', title="Gain/Loss per Titolo (€)"), use_container_width=True)
    
    st.subheader("Performance Aggregata")
    st.dataframe(df_raw.groupby('ISIN').agg({'Qty':'sum','Inv_EUR':'sum','Att_EUR':'sum','Gain_EUR':'sum'}).reset_index(), use_container_width=True)

with tab2:
    st.subheader("Dettaglio singoli lotti")
    st.data_editor(df_raw[['Data','ISIN','Qty','Prezzo_Acq','Price_Now','Att_EUR','Gain_EUR']], use_container_width=True)

with tab3:
    st.subheader("Evoluzione Storica (€)")
    start_date = df_raw['Date_DT'].min()
    
    with st.spinner("Ricostruzione storica..."):
        all_hist = {}
        for isin in df_raw['ISIN'].unique():
            t = ticker_map.get(isin)
            if t:
                h = yf.download(t, start=start_date, progress=False)['Close']
                if not h.empty:
                    # Reindex per tappare i buchi dei weekend e forward fill
                    all_hist[isin] = h.reindex(pd.date_range(start_date, datetime.now()), method='ffill')

        dates = pd.date_range(start_date, datetime.now().date())
        history_values = []
        for d in dates:
            # Filtra solo i lotti acquistati fino a questa data d
            current_df = df_raw[df_raw['Date_DT'].dt.date <= d.date()]
            total_day = 0
            for _, lot in current_df.iterrows():
                isin = lot['ISIN']
                p = all_hist[isin].asof(d) if isin in all_hist else None
                # Se Yahoo non ha il prezzo (es. primo giorno), usa il Prezzo di Acquisto
                if pd.isnull(p) or p <= 0:
                    p = lot['Prezzo_Acq']
                total_day += p * lot['Qty']
            history_values.append(total_day)
        
        fig_hist = px.area(pd.DataFrame({'Data': dates, 'Valore': history_values}), x='Data', y='Valore')
        fig_hist.update_traces(line_shape='hv')
        st.plotly_chart(fig_hist, use_container_width=True)
