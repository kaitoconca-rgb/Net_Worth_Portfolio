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
        st.text_input("Inserisci Password", type="password", on_change=password_guessed, key="password")
        return False
    return st.session_state.get("password_correct", False)

if not check_password():
    st.stop()

# --- 1. CONFIGURAZIONE ---
st.set_page_config(page_title="Claudio's Executive Console", layout="wide")

ticker_map = {
    "LU2885245055": "8OU9.DE", "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "SJP6.DE", "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS", "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE"
}

@st.cache_data(ttl=600)
def get_current_fx():
    try:
        t = yf.Ticker("EURAUD=X")
        return float(t.fast_info['last_price'])
    except: return 1.6450

@st.cache_data(ttl=3600)
def get_historical_fx_series():
    try:
        fx_raw = yf.download("EURAUD=X", start="2023-01-01", progress=False)['Close']
        if isinstance(fx_raw, pd.DataFrame): return fx_raw.iloc[:, 0]
        return fx_raw
    except: return None

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
df_raw['Manual_Price'] = pd.to_numeric(df_input['Price'], errors='coerce')
df_raw = df_raw.dropna(subset=['ISIN', 'Qty'])
df_raw['Date_DT'] = pd.to_datetime(df_raw['Data'], dayfirst=True)

# --- 3. LOGICA PREZZI & CAMBI ---
@st.cache_data(ttl=600)
def fetch_live_prices(isins):
    res = {}
    for isin in isins:
        symbol = ticker_map.get(isin)
        try:
            t = yf.Ticker(symbol)
            res[isin] = float(t.fast_info['last_price'])
        except: res[isin] = None
    return res

live_p = fetch_live_prices(df_raw['ISIN'].unique().tolist())
fx_now = get_current_fx()
fx_h = get_historical_fx_series()

def get_fx_at(dt):
    try: return float(fx_h.asof(dt))
    except: return 1.6450

# Applica prezzi e calcola AUD Storico
df_raw['Price_Now'] = df_raw.apply(lambda r: r['Manual_Price'] if pd.notnull(r['Manual_Price']) and r['Manual_Price']>0 else (live_p.get(r['ISIN']) or r['Prezzo_Acq']), axis=1)
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['Date_DT'].apply(get_fx_at)
df_raw['Att_AUD'] = df_raw['Att_EUR'] * fx_now

# --- 4. INTERFACCIA ---
tab1, tab2, tab3, tab4 = st.tabs(["📊 Performance", "💸 Simulatore", "📈 Storico", "🛠️ Logs"])

with tab1:
    # Metriche in alto
    t_inv_eur, t_att_eur = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    t_inv_aud, t_att_aud = df_raw['Inv_AUD'].sum(), df_raw['Att_AUD'].sum()
    
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Investito EUR", f"€{t_inv_eur:,.0f}")
        st.metric("Valore Attuale EUR", f"€{t_att_eur:,.0f}", f"€{t_att_eur - t_inv_eur:,.0f}")
    with c2:
        st.metric("Investito AUD (Storico)", f"${t_inv_aud:,.0f}")
        st.metric("Valore Attuale AUD", f"${t_att_aud:,.0f}", f"${t_att_aud - t_inv_aud:,.0f}")
    with c3:
        st.metric("ROI (EUR / AUD)", f"{(t_att_eur/t_inv_eur-1)*100:.1f}%", f"{(t_att_aud/t_inv_aud-1)*100:.1f}%")
        st.metric("Cambio EUR/AUD", f"{fx_now:.4f}")

    st.divider()
    
    # Grafici
    g1, g2 = st.columns([1, 2])
    with g1:
        st.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', hole=0.4, title="Allocation %"), use_container_width=True)
    with g2:
        agg = df_raw.groupby('ISIN').agg({'Att_EUR': 'sum', 'Inv_EUR': 'sum'}).reset_index()
        fig_b = go.Figure(data=[
            go.Bar(name='Investito', x=agg['ISIN'], y=agg['Inv_EUR'], marker_color='lightgray'),
            go.Bar(name='Attuale', x=agg['ISIN'], y=agg['Att_EUR'], marker_color='blue')
        ])
        fig_b.update_layout(barmode='group', title="Performance per Asset (EUR)")
        st.plotly_chart(fig_b, use_container_width=True)

    # Tabella
    df_t = df_raw.groupby('ISIN').agg({'Inv_EUR':'sum','Att_EUR':'sum','Inv_AUD':'sum','Att_AUD':'sum'}).reset_index()
    st.dataframe(df_t.style.format("€{:,.2f}").format({"Inv_AUD":"${:,.2f}","Att_AUD":"${:,.2f}"}), use_container_width=True)

with tab2:
    tr = st.slider("Tax Rate (%)", 0.0, 45.0, 37.0)
    df_sim = df_raw.copy()
    df_sim['% Vendita'] = 0.0
    ed = st.data_editor(df_sim[['ISIN','Data','Qty','Att_AUD','Inv_AUD','% Vendita']], hide_index=True)
    sel = ed[ed['% Vendita']>0].copy()
    if not sel.empty:
        gain = (sel['Att_AUD'] - sel['Inv_AUD']) * (sel['% Vendita']/100)
        taxable = gain.apply(lambda x: x*0.5 if x>0 else x).sum() # Semplificato 1 anno
        st.metric("Tasse Stimate (AUD)", f"${taxable*(tr/100):,.2f}")

with tab3:
    st.subheader("Evoluzione Capitale (Oct 2025 - Oggi)")
    # Logica per ricostruire la crescita cumulativa
    h_df = df_raw.sort_values('Date_DT').copy()
    h_df['Cumulative_Inv'] = h_df['Inv_EUR'].cumsum()
    # Per il valore attuale storico facciamo una stima basata sui flussi
    fig_h = px.area(h_df, x='Date_DT', y='Cumulative_Inv', title="Crescita Capitale Investito (EUR)",
                    labels={'Cumulative_Inv': 'Capitale Totale (€)', 'Date_DT': 'Tempo'})
    fig_h.add_scatter(x=[h_df['Date_DT'].max()], y=[t_att_eur], mode='markers+text', 
                      text=[f"Valore Attuale: €{t_att_eur:,.0f}"], textposition="top left", name="Valore Corrente")
    st.plotly_chart(fig_h, use_container_width=True)
    st.info("Il grafico mostra l'accumulo dei versamenti. Il punto finale indica il valore attuale di mercato (€214k).")

with tab4:
    st.write("Status Prezzi Live:")
    st.write(live_p)
    st.write(f"Ultimo Check: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
