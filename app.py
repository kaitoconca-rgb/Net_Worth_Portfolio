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
    "LU2885245055": "X062.DE", "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "SJPD.AS", "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS", "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE"
}

@st.cache_data(ttl=600)
def get_fx_rate():
    try:
        d = yf.download("EURAUD=X", period="5d", progress=False)
        if isinstance(d.columns, pd.MultiIndex): d.columns = d.columns.get_level_values(0)
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

# Logica Prezzo Attuale (Live o Override colonna Price)
manual_prices = pd.to_numeric(df_input['Price'], errors='coerce')
market_fx = get_fx_rate()

prices_now = []
for i, row in df_raw.iterrows():
    if i < len(manual_prices) and pd.notnull(manual_prices[i]) and manual_prices[i] > 0:
        prices_now.append(float(manual_prices[i]))
    else:
        try:
            t = ticker_map.get(row['ISIN'])
            d_live = yf.download(t, period="5d", progress=False)
            if isinstance(d_live.columns, pd.MultiIndex): d_live.columns = d_live.columns.get_level_values(0)
            val = d_live['Close'].iloc[-1]
            prices_now.append(float(val))
        except:
            prices_now.append(float(row['Prezzo_Acq']))

df_raw['Price_Now'] = prices_now
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Gain_EUR'] = df_raw['Att_EUR'] - df_raw['Inv_EUR']
df_raw['Att_AUD'] = df_raw['Att_EUR'] * market_fx

# --- 3. UI ---
st.title("🏛️ Claudio's Executive Portfolio")

tab1, tab2, tab3 = st.tabs(["📊 Riepilogo", "💸 Dettagli", "📈 Storia Evolutiva"])

with tab1:
    # Metriche
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Investito (€)", f"€{df_raw['Inv_EUR'].sum():,.0f}")
    m2.metric("Valore (€)", f"€{df_raw['Att_EUR'].sum():,.0f}")
    m3.metric("Valore (AUD)", f"${df_raw['Att_AUD'].sum():,.0f}")
    roi = ((df_raw['Att_EUR'].sum()/df_raw['Inv_EUR'].sum())-1)*100
    m4.metric("ROI Globale", f"{roi:.2f}%", delta=f"{roi:.2f}%")

    # Grafici
    c1, c2 = st.columns([1, 2])
    with c1:
        st.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', hole=0.4, title="Asset Allocation"), use_container_width=True)
    with c2:
        agg = df_raw.groupby('ISIN')['Gain_EUR'].sum().reset_index()
        st.plotly_chart(px.bar(agg, x='ISIN', y='Gain_EUR', title="Guadagno/Perdita per Titolo (€)", color='Gain_EUR', color_continuous_scale='RdYlGn'), use_container_width=True)
    
    st.subheader("Performance Aggregata")
    st_agg = df_raw.groupby('ISIN').agg({'Qty':'sum','Inv_EUR':'sum','Att_EUR':'sum','Gain_EUR':'sum'}).reset_index()
    st.dataframe(st_agg.style.format(precision=2), use_container_width=True, hide_index=True)

with tab2:
    st.subheader("Dettaglio Analitico per Lotto")
    st.data_editor(df_raw[['Data','ISIN','Qty','Prezzo_Acq','Price_Now','Att_EUR','Gain_EUR']], use_container_width=True, hide_index=True)

with tab3:
    st.subheader("Evoluzione del Capitale (€)")
    start_date = df_raw['Date_DT'].min()
    
    with st.spinner("Calcolo cronologico in corso..."):
        all_hist = {}
        for isin in df_raw['ISIN'].unique():
            t = ticker_map.get(isin)
            if t:
                h = yf.download(t, start=start_date, progress=False)['Close']
                if not h.empty:
                    if isinstance(h, pd.DataFrame): h = h.iloc[:, 0]
                    all_hist[isin] = h.reindex(pd.date_range(start_date, datetime.now()), method='ffill')

        dates = pd.date_range(start_date, datetime.now().date())
        history_values = []
        for d in dates:
            current_df = df_raw[df_raw['Date_DT'].dt.date <= d.date()]
            total_day = 0
            for _, lot in current_df.iterrows():
                isin = lot['ISIN']
                p_series = all_hist.get(isin)
                p = None
                if p_series is not None:
                    try:
                        p = p_series.asof(d)
                    except: p = None
                
                # Se il prezzo non esiste per quel giorno o è nullo, usa il Prezzo di Acquisto
                if pd.isna(p) or p <= 0:
                    p = float(lot['Prezzo_Acq'])
                
                total_day += float(p) * float(lot['Qty'])
            history_values.append(total_day)
        
        hist_df = pd.DataFrame({'Data': dates, 'Valore': history_values})
        fig_hist = px.area(hist_df, x='Data', y='Valore', title="Andamento del Portafoglio (€) - Da Ottobre ad oggi")
        fig_hist.update_traces(line_shape='hv', line_color='#1f77b4')
        fig_hist.update_layout(hovermode="x unified", yaxis_title="Valore di Mercato (€)")
        st.plotly_chart(fig_hist, use_container_width=True)
