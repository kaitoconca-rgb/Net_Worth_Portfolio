import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
import pytz
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
        st.text_input("Inserisci Password", type="password", on_change=password_guessed, key="password")
        return False
    return st.session_state.get("password_correct", False)

if not check_password():
    st.stop()

# --- 1. CONFIGURAZIONE ---
st.set_page_config(page_title="Executive Portfolio Console", layout="wide")

ticker_map = {
    "LU2885245055": "8OU9.DE", "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "SJP6.DE", "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS", "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE"
}

@st.cache_data(ttl=600)
def get_fx_rate():
    try:
        t = yf.Ticker("EURAUD=X")
        return float(t.fast_info['last_price'])
    except: return 1.6450

# --- 2. DATI ---
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

# --- 3. PREZZI & DIAGNOSTICA ---
ticker_diag = {}
cache_prezzi = {}

def fetch_price(isin, manual_val):
    symbol = ticker_map.get(isin)
    if pd.notnull(manual_val) and manual_val > 0:
        ticker_diag[isin] = {"status": "MANUALE", "delay": "0 min"}
        return float(manual_val)
    try:
        t = yf.Ticker(symbol)
        curr = t.fast_info['last_price']
        lmt = t.fast_info.get('last_market_time')
        delay = "N/D"
        if lmt:
            diff = datetime.now(pytz.utc) - lmt.astimezone(pytz.utc)
            delay = f"{int(diff.total_seconds()/60)} min"
        ticker_diag[isin] = {"status": "LIVE", "delay": delay}
        return float(curr) if curr else None
    except:
        ticker_diag[isin] = {"status": "ERRORE", "delay": "∞"}
        return None

fx_now = get_fx_rate()
with st.spinner("Sincronizzazione..."):
    for isin in df_raw['ISIN'].unique():
        # Prende il manual override dalla prima riga disponibile per quell'ISIN
        m_val = df_raw[df_raw['ISIN'] == isin]['Manual_Override'].iloc[0]
        cache_prezzi[isin] = fetch_price(isin, m_val)

# Applichiamo i prezzi
df_raw['Price_Now'] = df_raw['ISIN'].map(cache_prezzi).fillna(df_raw['Prezzo_Acq'])
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Gain_EUR'] = df_raw['Att_EUR'] - df_raw['Inv_EUR']
df_raw['Att_AUD'] = df_raw['Att_EUR'] * fx_now
df_raw['Gain_AUD'] = df_raw['Gain_EUR'] * fx_now 

# --- 4. INTERFACCIA ---
st.title("🏛️ Claudio's Portfolio Command Center")
tab1, tab2, tab3, tab4 = st.tabs(["📊 Performance", "💸 Simulatore Tasse", "📈 Storico", "🛠️ System Logs"])

with tab1:
    t_inv, t_att = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    st.metric("Valore Portafoglio (€)", f"€{t_att:,.2f}", f"€{(t_att - t_inv):,.2f}")
    
    col1, col2 = st.columns([1, 2])
    with col1:
        st.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', hole=0.4, title="Allocation"), use_container_width=True)
    with col2:
        agg = df_raw.groupby('ISIN')['Gain_EUR'].sum().reset_index()
        st.plotly_chart(px.bar(agg, x='ISIN', y='Gain_EUR', title="Gain per Asset (€)"), use_container_width=True)
    
    st.dataframe(df_raw.groupby('ISIN').agg({'Qty':'sum','Inv_EUR':'sum','Att_EUR':'sum','Gain_EUR':'sum'}).style.format(precision=2), use_container_width=True)

with tab2:
    st.subheader("Simulatore CGT (ATO)")
    df_raw['% Vendi'] = 0.0
    ed = st.data_editor(df_raw[['Data', 'ISIN', 'Qty', 'Prezzo_Acq', 'Price_Now', 'Gain_AUD', '% Vendi']], hide_index=True)
    if ed['% Vendi'].sum() > 0:
        sel = ed[ed['% Vendi'] > 0].copy()
        sel['G_Sim'] = sel['Gain_AUD'] * (sel['% Vendi'] / 100)
        tax = sel.apply(lambda r: r['G_Sim']*0.5 if r['G_Sim']>0 and (datetime.now()-pd.to_datetime(r['Data'], dayfirst=True)).days>=365 else r['G_Sim'], axis=1).sum()
        st.info(f"Imponibile stimato: ${max(0, tax):,.2f} AUD")

with tab3:
    st.subheader("Evoluzione Storica")
    # ORDINE CRONOLOGICO
    h = df_raw.sort_values('Date_DT').copy()
    h['Inv_Cum'] = h['Inv_EUR'].cumsum()
    
    # CALCOLO CORRETTO VALORE: Quantità cumulata per ISIN nel tempo
    h['Qty_Cum'] = h.groupby('ISIN')['Qty'].cumsum()
    # Sommiamo il valore attuale di tutte le quote possedute fino a quella data
    # Per farlo bene, dobbiamo iterare sulle date
    dates = sorted(h['Date_DT'].unique())
    history = []
    for d in dates:
        # Prendi tutto ciò che è stato comprato fino a questa data
        sub = h[h['Date_DT'] <= d]
        v_market = (sub['Qty'] * sub['Price_Now']).sum()
        v_inv = sub['Inv_EUR'].sum()
        history.append({'Data': d, 'Investito': v_inv, 'Valore': v_market})
    
    df_history = pd.DataFrame(history)
    
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df_history['Data'], y=df_history['Investito'], name="Capitale Investito", fill='tozeroy', line_color='gray'))
    fig_h = fig.add_trace(go.Scatter(x=df_history['Data'], y=df_history['Valore'], name="Valore di Mercato (Prezzi Oggi)", fill='tonexty', line_color='blue'))
    
    st.plotly_chart(fig, use_container_width=True)
    st.caption("Nota: Il valore storico è calcolato moltiplicando le quote possedute in passato per il prezzo attuale.")

with tab4:
    diag_df = pd.DataFrame([{"ISIN": k, "Stato": v["status"], "Ritardo": v["delay"], "Prezzo": f"{cache_prezzi.get(k):.2f} €"} for k, v in ticker_diag.items()])
    st.table(diag_df)
