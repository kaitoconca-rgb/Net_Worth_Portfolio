import streamlit as st
import pandas as pd
import yfinance as yf
import requests
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, date
from streamlit_gsheets import GSheetsConnection

# --- 0. PROTEZIONE (ASSOLUTA) ---
def check_password():
    if "password_correct" not in st.session_state:
        st.text_input("Inserisci Password", type="password", on_change=lambda: st.session_state.update({"password_correct": st.session_state["password"] == st.secrets["auth"]["password"]}), key="password")
        return False
    return st.session_state["password_correct"]

if not check_password():
    st.stop()

# --- 1. CONFIGURAZIONE ---
st.set_page_config(page_title="Claudio's Executive Console", layout="wide")

ticker_map = {
    "LU2885245055": "MANUAL",
    "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "36B2.MU", "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS", "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE"
}

# --- 2. RECUPERO DATI (CON LOGICA DI SICUREZZA) ---

@st.cache_data(ttl=3600) # Cache lunga per non stressare le API
def get_live_price_finnhub(isin):
    api_key = st.secrets.get("FINNHUB_API_KEY")
    if not api_key: return None
    try:
        # Step 1: Search ISIN
        res = requests.get(f"https://finnhub.io/api/v1/search?q={isin}&token={api_key}", timeout=5).json()
        if res.get('result'):
            symbol = res['result'][0]['symbol']
            # Step 2: Get Quote
            q = requests.get(f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={api_key}", timeout=5).json()
            return q.get('c')
    except: return None
    return None

@st.cache_data(ttl=3600)
def get_yahoo_hist(isins_list):
    data = {}
    for isin in isins_list:
        sym = ticker_map.get(isin)
        if sym and sym != "MANUAL":
            try:
                h = yf.download(sym, start="2024-09-01", progress=False)['Close']
                if isinstance(h, pd.DataFrame): h = h.iloc[:, 0]
                data[isin] = h
            except: data[isin] = None
    return data

# --- 3. ELABORAZIONE ---
conn = st.connection("gsheets", type=GSheetsConnection)
df_input = conn.read(ttl=0)
df_input.columns = [c.strip() for c in df_input.columns]

df_raw = pd.DataFrame()
df_raw['Data'] = pd.to_datetime(df_input['Fecha Valor'], dayfirst=True)
df_raw['ISIN'] = df_input['ISIN']
df_raw['Qty'] = pd.to_numeric(df_input['Cantidad'], errors='coerce')
df_raw['Inv_EUR'] = pd.to_numeric(df_input['Importe Cargado'], errors='coerce')
df_raw['Prezzo_Acq'] = pd.to_numeric(df_input['Precio'], errors='coerce') 
df_raw['Manual_Price'] = pd.to_numeric(df_input['Price'], errors='coerce')
df_raw = df_raw.dropna(subset=['ISIN', 'Qty']).sort_values('Data')

hist_map = get_yahoo_hist(df_raw['ISIN'].unique().tolist())

def master_price_engine(row):
    # 1. Manuale
    if pd.notnull(row['Manual_Price']) and row['Manual_Price'] > 0:
        return row['Manual_Price'], "Sheets"
    # 2. Finnhub
    f_p = get_live_price_finnhub(row['ISIN'])
    if f_p and f_p > 0: return f_p, "Finnhub"
    # 3. Yahoo Last
    h = hist_map.get(row['ISIN'])
    if h is not None and not h.empty: return float(h.iloc[-1]), "Yahoo"
    # 4. Fallback
    return row['Prezzo_Acq'], "Acq Price"

results = df_raw.apply(master_price_engine, axis=1)
df_raw['Price_Now'] = [r[0] for r in results]
df_raw['Source'] = [r[1] for r in results]

# FX
@st.cache_data(ttl=3600)
def get_fx():
    t = yf.Ticker("EURAUD=X")
    now = float(t.fast_info['last_price'])
    hist = yf.download("EURAUD=X", start="2024-01-01", progress=False)['Close']
    if isinstance(hist, pd.DataFrame): hist = hist.iloc[:, 0]
    return now, hist

fx_now, fx_hist = get_fx()
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Att_AUD'] = df_raw['Att_EUR'] * fx_now

# --- 4. TABS (RIPRISTINATI) ---
t1, t2, t3, t4 = st.tabs(["📊 Performance", "💸 Simulatore", "📈 Timeline", "🛠️ Diagnostics"])

with t1:
    st.metric("Valore Totale EUR", f"€{df_raw['Att_EUR'].sum():,.2f}")
    st.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', hole=0.4), use_container_width=True)
    # Tabella dettagliata inclusa
    agg = df_raw.groupby('ISIN').agg({'Inv_EUR':'sum','Att_EUR':'sum'}).reset_index()
    st.dataframe(agg, use_container_width=True)

with t2:
    st.data_editor(df_raw[['ISIN', 'Qty', 'Prezzo_Acq', 'Price_Now', 'Source']], use_container_width=True)

with t3:
    # Timeline ricostruita correttamente
    dates = pd.date_range(date(2024, 10, 1), date.today())
    vals = []
    for d in dates:
        temp = df_raw[df_raw['Data'].dt.date <= d.date()]
        v = sum(pos['Qty'] * (hist_map.get(pos['ISIN']).asof(d) if (hist_map.get(pos['ISIN']) is not None and not hist_map.get(pos['ISIN']).empty) else pos['Prezzo_Acq']) for _, pos in temp.iterrows())
        vals.append({'Date': d, 'Value': v})
    st.plotly_chart(px.area(pd.DataFrame(vals), x='Date', y='Value'), use_container_width=True)

with t4:
    st.write("Verifica Fonti Attuali:")
    st.dataframe(df_raw[['ISIN', 'Price_Now', 'Source']].drop_duplicates())
