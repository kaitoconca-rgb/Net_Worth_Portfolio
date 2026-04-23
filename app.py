import streamlit as st
import pandas as pd
import yfinance as yf
import requests
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, date
from streamlit_gsheets import GSheetsConnection

# --- 0. PROTEZIONE (RIPRISTINATA) ---
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

# --- 1. CONFIGURAZIONE E TICKER MAP ---
st.set_page_config(page_title="Claudio's Executive Console", layout="wide")

ticker_map = {
    "LU2885245055": "MANUAL",
    "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "36B2.MU", "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS", "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE"
}

# --- 2. RECUPERO DATI IBRIDO (FINNHUB + YAHOO) ---

@st.cache_data(ttl=600)
def get_finnhub_price(isin):
    api_key = st.secrets.get("FINNHUB_API_KEY")
    if not api_key: return None
    try:
        search = requests.get(f"https://finnhub.io/api/v1/search?q={isin}&token={api_key}", timeout=5).json()
        if search.get('result'):
            symbol = search['result'][0]['symbol']
            quote = requests.get(f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={api_key}", timeout=5).json()
            return quote.get('c')
    except: return None
    return None

@st.cache_data(ttl=3600)
def get_full_market_context(isins_list):
    prices_hist = {}
    logs = {}
    for isin in isins_list:
        symbol = ticker_map.get(isin)
        if symbol == "MANUAL":
            prices_hist[isin] = None
            logs[isin] = {"status": "MANUAL", "source": "Sheets"}
            continue
        try:
            h = yf.download(symbol, start="2024-09-01", progress=False)['Close']
            if isinstance(h, pd.DataFrame): h = h.iloc[:, 0]
            if not h.empty:
                prices_hist[isin] = h
                logs[isin] = {"status": "LIVE", "source": "Yahoo"}
            else: raise ValueError()
        except:
            prices_hist[isin] = None
            logs[isin] = {"status": "FALLBACK", "source": "None"}
    return prices_hist, logs

# --- 3. CORE LOGIC ---
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

hist_map, diag_logs = get_full_market_context(df_raw['ISIN'].unique().tolist())

def get_current_price(row):
    if pd.notnull(row['Manual_Price']) and row['Manual_Price'] > 0: return row['Manual_Price']
    # Prova Finnhub Live
    f_price = get_finnhub_price(row['ISIN'])
    if f_price: return f_price
    # Prova Yahoo Hist
    h = hist_map.get(row['ISIN'])
    if h is not None and not h.empty: return float(h.iloc[-1])
    # Fallback finale
    return row['Prezzo_Acq']

df_raw['Price_Now'] = df_raw.apply(get_current_price, axis=1)

@st.cache_data(ttl=600)
def get_fx_data():
    try:
        t = yf.Ticker("EURAUD=X")
        return float(t.fast_info['last_price']), yf.download("EURAUD=X", start="2024-01-01", progress=False)['Close']
    except: return 1.65, None

fx_now, fx_hist = get_fx_data()

def get_fx_at(dt):
    try: return float(fx_hist.asof(dt))
    except: return 1.65

df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['Data'].apply(get_fx_at)
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Att_AUD'] = df_raw['Att_EUR'] * fx_now

# --- 4. INTERFACCIA COMPLETA ---
tab1, tab2, tab3, tab4 = st.tabs(["📊 Performance", "💸 Simulatore ATO", "📈 Timeline", "🛠️ Diagnostics"])

with tab1:
    t_inv_eur, t_att_eur = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    t_inv_aud, t_att_aud = df_raw['Inv_AUD'].sum(), df_raw['Att_AUD'].sum()
    
    c1, c2, c3 = st.columns(3)
    c1.metric("Investito EUR", f"€{t_inv_eur:,.0f}")
    c1.metric("Valore Attuale EUR", f"€{t_att_eur:,.0f}", f"€{t_att_eur - t_inv_eur:,.0f}")
    c2.metric("Investito AUD", f"${t_inv_aud:,.0f}")
    c2.metric("Valore Attuale AUD", f"${t_att_aud:,.0f}", f"${t_att_aud - t_inv_aud:,.0f}")
    c3.metric("ROI (EUR)", f"{(t_att_eur/t_inv_eur-1)*100:.2f}%")
    
    st.divider()
    g1, g2 = st.columns([1, 1.5])
    with g1: st.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', hole=0.4, title="Allocation"), use_container_width=True)
    with g2:
        agg = df_raw.groupby('ISIN').agg({'Inv_EUR':'sum','Att_EUR':'sum','Inv_AUD':'sum','Att_AUD':'sum'}).reset_index()
        fig = go.Figure(data=[go.Bar(name='EUR Gain', x=agg['ISIN'], y=agg['Att_EUR']-agg['Inv_EUR']), go.Bar(name='AUD Gain', x=agg['ISIN'], y=agg['Att_AUD']-agg['Inv_AUD'])])
        st.plotly_chart(fig, use_container_width=True)

with tab2:
    st.subheader("Simulatore Cash-out")
    tax_r = st.slider("Tax Rate %", 0.0, 45.0, 37.0)
    df_raw['% Vendi'] = 0.0
    st.data_editor(df_raw[['ISIN','Data','Qty','Prezzo_Acq','Price_Now','Att_EUR','Inv_EUR','% Vendi']], use_container_width=True)

with tab3:
    st.subheader("Timeline Storica")
    date_range = pd.date_range(date(2024, 10, 1), date.today())
    daily_val = []
    for d in date_range:
        mask = df_raw['Data'].dt.date <= d.date()
        temp_df = df_raw[mask]
        v = 0
        for _, pos in temp_df.iterrows():
            h = hist_map.get(pos['ISIN'])
            price = h.asof(d) if (h is not None and not h.empty) else pos['Prezzo_Acq']
            v += pos['Qty'] * price
        daily_val.append({'Date': d, 'MarketValue': v})
    st.plotly_chart(px.area(pd.DataFrame(daily_val), x='Date', y='MarketValue'), use_container_width=True)

with tab4:
    st.write("Diagnostica Prezzi")
    st.table(pd.DataFrame.from_dict(diag_logs, orient='index'))
