import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, timedelta
import pytz
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
    "LU2885245055": "8OU9.DE",
    "IE0032077012": "EQQQ.DE",
    "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE",
    "IE00BFM15T99": "SJP6.DE",
    "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS",
    "IE00B3VVMM84": "VFEM.DE",
    "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.DE", # Cambiato in Xetra per maggiore precisione
    "IE0005042456": "IUSA.DE"
}

@st.cache_data(ttl=600)
def get_fx_rate():
    try:
        t = yf.Ticker("EURAUD=X")
        val = t.fast_info['last_price']
        return float(val) if val else 1.6450
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
df_raw['Prezzo_Acq'] = pd.to_numeric(df_input['Precio'], errors='coerce') 
df_raw['Manual_Override'] = pd.to_numeric(df_input['Price'], errors='coerce')

df_raw = df_raw.dropna(subset=['ISIN', 'Qty'])
df_raw['Date_DT'] = pd.to_datetime(df_raw['Data'], dayfirst=True)

# --- 3. LOGICA PREZZI LIVE CON DIAGNOSTICA ---
ticker_diag = {}

def fetch_live_price_diag(isin, manual_val):
    symbol = ticker_map.get(isin)
    if pd.notnull(manual_val) and manual_val > 0:
        ticker_diag[isin] = {"status": "MANUALE", "msg": "Dato inserito da utente"}
        return float(manual_val)
    
    if not symbol: return None
    
    try:
        t = yf.Ticker(symbol)
        info = t.fast_info
        current = info['last_price']
        prev_close = info['previous_close']
        
        # Se il prezzo è identico alla chiusura precedente, potrebbe essere un dato fermo
        if current == prev_close:
            ticker_diag[isin] = {"status": "FERMO", "msg": "Prezzo uguale a chiusura (Mercato chiuso o ritardo)"}
        else:
            ticker_diag[isin] = {"status": "LIVE", "msg": f"Variazione intraday: {((current/prev_close)-1)*100:.2f}%"}
            
        return float(current) if current else None
    except:
        if isin == "IE00BFM15T99": 
            ticker_diag[isin] = {"status": "FIX", "msg": "Emergency Fix 7.02"}
            return 7.02
        ticker_diag[isin] = {"status": "ERRORE", "msg": "Impossibile contattare Yahoo"}
        return None

market_fx = get_fx_rate()
fx_hist = yf.download("EURAUD=X", start="2025-09-01", progress=False)['Close']

with st.spinner("Diagnostica mercati..."):
    prices_now = []
    cache_prezzi = {}
    for _, row in df_raw.iterrows():
        isin = row['ISIN']
        if isin not in cache_prezzi:
            cache_prezzi[isin] = fetch_live_price_diag(isin, row['Manual_Override'])
        p = cache_prezzi[isin] if cache_prezzi[isin] else float(row['Prezzo_Acq'])
        prices_now.append(p)

df_raw['Price_Now'] = prices_now
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Gain_EUR'] = df_raw['Att_EUR'] - df_raw['Inv_EUR']
df_raw['FX_Acq'] = df_raw['Date_DT'].apply(lambda x: fx_hist.asof(x) if not fx_hist.empty else 1.63)
df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['FX_Acq']
df_raw['Att_AUD'] = df_raw['Att_EUR'] * market_fx
df_raw['Gain_AUD'] = df_raw['Att_AUD'] - df_raw['Inv_AUD']

# --- 4. INTERFACCIA ---
st.title("🏛️ Claudio's Portfolio Command Center")

tab1, tab2, tab3, tab4 = st.tabs(["📊 Performance", "💸 Simulatore Tasse", "📈 Storico", "🛠️ System Logs"])

with tab1:
    # Totale intorno ai 214k
    t_inv_eur, t_att_eur = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    summary_df = pd.DataFrame({
        "Metrica": ["Total Invested", "Total Value", "Gain / Loss", "ROI %"],
        "EURO (€)": [f"€{t_inv_eur:,.2f}", f"€{t_att_eur:,.2f}", f"€{(t_att_eur - t_inv_eur):,.2f}", f"{((t_att_eur/t_inv_eur)-1)*100:.2f}%"],
        "AUD ($)": [f"${df_raw['Inv_AUD'].sum():,.2f}", f"${df_raw['Att_AUD'].sum():,.2f}", f"${(df_raw['Att_AUD'].sum()-df_raw['Inv_AUD'].sum()):,.2f}", f"{((df_raw['Att_AUD'].sum()/df_raw['Inv_AUD'].sum())-1)*100:.2f}%"]
    })
    st.table(summary_df)
    st.subheader("Riepilogo Aggregato")
    st_agg = df_raw.groupby('ISIN').agg({'Qty':'sum','Inv_EUR':'sum','Att_EUR':'sum','Gain_EUR':'sum'}).reset_index()
    st.dataframe(st_agg.style.format(precision=2), use_container_width=True, hide_index=True)

with tab4:
    st.subheader("🛠️ Diagnostica Avanzata Prezzi Live")
    
    diag_data = []
    for isin, info in ticker_diag.items():
        diag_data.append({
            "ISIN": isin,
            "Ticker": ticker_map.get(isin),
            "Stato": info['status'],
            "Dettaglio Tecnico": info['msg'],
            "Prezzo in Uso": f"€ {cache_prezzi.get(isin):.2f}" if cache_prezzi.get(isin) else "N/A"
        })
    
    df_diag = pd.DataFrame(diag_data)
    
    def color_status(val):
        color = 'white'
        if val == 'LIVE': color = '#90EE90' # Verde chiaro
        elif val == 'FERMO': color = '#FFD700' # Oro
        elif val == 'ERRORE': color = '#FFB6C1' # Rosso chiaro
        return f'background-color: {color}; color: black'

    st.table(df_diag.style.applymap(color_status, subset=['Stato']))
    
    st.divider()
    now = datetime.now(pytz.timezone('Australia/Sydney'))
    st.write(f"Ultima scansione sistema: {now.strftime('%d/%m/%Y %H:%M:%S')} Sydney Time")
    st.info("Nota: Se lo Stato è 'FERMO', Yahoo sta riportando il prezzo di chiusura. Questo accade se il mercato è chiuso o se non ci sono stati scambi recenti sul ticker selezionato.")
