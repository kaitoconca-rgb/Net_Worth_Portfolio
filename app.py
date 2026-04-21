import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, timedelta
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

# --- 1. CONFIGURAZIONE & MAPPING ---
st.set_page_config(page_title="Executive Portfolio Console", layout="wide")

ticker_map = {
    "LU2885245055": "8OU9.DE", "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "SJP6.DE", "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS", "IE00B3VVMM84": "VFEM.DE", "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI", "IE0005042456": "IUSA.DE"
}

@st.cache_data(ttl=600)
def get_fx_data():
    try:
        t = yf.Ticker("EURAUD=X")
        now = float(t.fast_info['last_price'])
        hist = yf.download("EURAUD=X", start="2025-01-01", progress=False)['Close']
        return now, hist
    except: return 1.6450, None

fx_now, fx_hist = get_fx_data()

# --- 2. CARICAMENTO DATI ---
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

# --- 3. CORE ENGINE: PREZZI E STORICO ---
@st.cache_data(ttl=3600)
def get_market_history(isins_list):
    start_date = "2025-09-01"
    data = {}
    logs = {}
    for isin in isins_list:
        symbol = ticker_map.get(isin)
        if symbol:
            try:
                t = yf.Ticker(symbol)
                h = t.history(start=start_date)['Close']
                data[isin] = h
                logs[isin] = {"status": "LIVE", "last_update": datetime.now().strftime("%H:%M"), "source": "Yahoo"}
            except:
                data[isin] = None
                logs[isin] = {"status": "NULL/ERROR", "last_update": "-", "source": "Fallback"}
        else:
            data[isin] = None
            logs[isin] = {"status": "NO_TICKER", "last_update": "-", "source": "Manual"}
    return data, logs

hist_data, diag_logs = get_market_history(df_raw['ISIN'].unique().tolist())

# Calcolo Prezzi Correnti (Hierarchy: Manual > Live > Acq)
def get_current_price(isin, manual, acq):
    if pd.notnull(manual) and manual > 0: return manual
    if isin in hist_data and hist_data[isin] is not None:
        return float(hist_data[isin].iloc[-1])
    return acq

df_raw['Price_Now'] = df_raw.apply(lambda r: get_current_price(r['ISIN'], r['Manual_Price'], r['Prezzo_Acq']), axis=1)

# Calcolo AUD Storico (FX at purchase date)
def get_fx_at(dt):
    try: return float(fx_hist.asof(dt))
    except: return 1.6450

df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['Data'].apply(get_fx_at)
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Att_AUD'] = df_raw['Att_EUR'] * fx_now

# --- 4. INTERFACCIA ---
tab1, tab2, tab3, tab4 = st.tabs(["📊 Performance", "💸 Simulatore ATO", "📈 Timeline", "🛠️ Diagnostics"])

with tab1:
    c1, c2, c3 = st.columns(3)
    t_inv_eur, t_att_eur = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    t_inv_aud, t_att_aud = df_raw['Inv_AUD'].sum(), df_raw['Att_AUD'].sum()
    
    c1.metric("Portafoglio EUR", f"€{t_att_eur:,.0f}", f"Gain: €{t_att_eur - t_inv_eur:,.0f}")
    c2.metric("Portafoglio AUD", f"${t_att_aud:,.0f}", f"Gain: ${t_att_aud - t_inv_aud:,.0f}")
    c3.metric("ROI (EUR vs AUD)", f"{(t_att_eur/t_inv_eur-1)*100:.1f}%", f"{(t_att_aud/t_inv_aud-1)*100:.1f}%", delta_color="normal")

    st.divider()
    
    # Grafico impatto valutario (Richiesto Punto 1)
    st.subheader("Profitto Reale per Asset: EUR vs AUD (FX Impact)")
    agg_p = df_raw.groupby('ISIN').agg({'Inv_EUR':'sum', 'Att_EUR':'sum', 'Inv_AUD':'sum', 'Att_AUD':'sum'}).reset_index()
    agg_p['Gain_EUR'] = agg_p['Att_EUR'] - agg_p['Inv_EUR']
    agg_p['Gain_AUD'] = agg_p['Att_AUD'] - agg_p['Inv_AUD']
    
    fig_fx = go.Figure()
    fig_fx.add_trace(go.Bar(name='Gain EUR (€)', x=agg_p['ISIN'], y=agg_p['Gain_EUR'], marker_color='blue'))
    fig_fx.add_trace(go.Bar(name='Gain AUD ($)', x=agg_p['ISIN'], y=agg_p['Gain_AUD'], marker_color='green'))
    fig_fx.update_layout(barmode='group', title="Se la barra Verde è più bassa della Blu, il cambio sta erodendo i profitti")
    st.plotly_chart(fig_fx, use_container_width=True)

with tab2:
    st.subheader("Simulazione Vendita Strategica")
    tax_rate = st.slider("Marginal Tax Rate (%)", 0.0, 45.0, 37.0)
    
    df_sim = df_raw.copy()
    df_sim['% Vendita'] = 0.0
    ed = st.data_editor(df_sim[['ISIN','Data','Qty','Att_EUR','Att_AUD','Inv_AUD','% Vendita']], hide_index=True)
    
    sel = ed[ed['% Vendita'] > 0].copy()
    if not sel.empty:
        sel['EUR_Out'] = sel['Att_EUR'] * (sel['% Vendita']/100)
        sel['AUD_Out'] = sel['Att_AUD'] * (sel['% Vendita']/100)
        sel['Profit_AUD'] = (sel['Att_AUD'] - sel['Inv_AUD']) * (sel['% Vendita']/100)
        
        # CGT Discount Logic
        def calc_tax(row):
            if row['Profit_AUD'] <= 0: return 0.0
            discount = 0.5 if (datetime.now() - row['Data'].to_pydatetime()).days > 365 else 1.0
            return row['Profit_AUD'] * discount * (tax_rate/100)
        
        sel['Tax'] = sel.apply(calc_tax, axis=1)
        
        r1, r2, r3 = st.columns(3)
        r1.metric("Cash Realizzato EUR", f"€{sel['EUR_Out'].sum():,.2f}")
        r2.metric("Cash Realizzato AUD", f"${sel['AUD_Out'].sum():,.2f}")
        r3.metric("Impatto Fiscale ATO (AUD)", f"-${sel['Tax'].sum():,.2f}", delta_color="inverse")

with tab3:
    st.subheader("Evoluzione Storica Portafoglio (Valore di Mercato)")
    # Ricostruzione Timeline (Punto 3)
    dates = pd.date_range(start="2025-10-01", end=datetime.now())
    timeline = []
    
    for d in dates:
        # Filtra gli acquisti effettuati fino a questa data
        current_holdings = df_raw[df_raw['Data'] <= d]
        daily_val = 0
        for _, row in current_holdings.iterrows():
            # Prendi il prezzo di quel giorno (o l'ultimo disponibile)
            h = hist_data.get(row['ISIN'])
            price = h.asof(d) if (h is not None and not h.empty) else row['Prezzo_Acq']
            daily_val += row['Qty'] * price
        timeline.append({'Date': d, 'Portfolio_Value': daily_val})
    
    df_timeline = pd.DataFrame(timeline)
    fig_h = px.area(df_timeline, x='Date', y='Portfolio_Value', title="Valore Totale Portafoglio (€)")
    fig_h.add_hline(y=9000, line_dash="dot", annotation_text="Ottobre 2025")
    st.plotly_chart(fig_h, use_container_width=True)

with tab4:
    st.subheader("Data Integrity & Health Check")
    diag_df = pd.DataFrame.from_dict(diag_logs, orient='index')
    diag_df['Price_Used'] = diag_df.index.map(lambda x: df_raw[df_raw['ISIN']==x]['Price_Now'].iloc[0] if x in df_raw['ISIN'].values else 0)
    st.table(diag_df)
