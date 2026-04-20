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

# --- 2. CARICAMENTO E LOGICA DATI ---
conn = st.connection("gsheets", type=GSheetsConnection)
df_input = conn.read(ttl=0)
df_input.columns = [c.strip() for c in df_input.columns]

df_raw = pd.DataFrame()
df_raw['Data'] = df_input['Fecha Valor']
df_raw['ISIN'] = df_input['ISIN']
df_raw['Qty'] = pd.to_numeric(df_input['Cantidad'], errors='coerce')
df_raw['Inv_EUR'] = pd.to_numeric(df_input['Importe Cargado'], errors='coerce')
df_raw['Prezzo_Acq'] = pd.to_numeric(df_input['Precio'], errors='coerce') 

df_raw = df_raw.dropna(subset=['ISIN', 'Qty'])
df_raw['Date_DT'] = pd.to_datetime(df_raw['Data'], dayfirst=True)

manual_prices = pd.to_numeric(df_input['Price'], errors='coerce')
market_fx = get_fx_rate()
fx_hist = yf.download("EURAUD=X", start="2025-09-01", progress=False)['Close']

prices_now = []
for i, row in df_raw.iterrows():
    if i < len(manual_prices) and pd.notnull(manual_prices[i]) and manual_prices[i] > 0:
        prices_now.append(float(manual_prices[i]))
    else:
        try:
            t = ticker_map.get(row['ISIN'])
            d_l = yf.download(t, period="5d", progress=False)
            if isinstance(d_l.columns, pd.MultiIndex): d_l.columns = d_l.columns.get_level_values(0)
            prices_now.append(float(d_l['Close'].iloc[-1]))
        except:
            prices_now.append(float(row['Prezzo_Acq']))

df_raw['Price_Now'] = prices_now
df_raw['FX_Acq'] = df_raw['Date_DT'].apply(lambda x: fx_hist.asof(x) if not fx_hist.empty else 1.63)
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Gain_EUR'] = df_raw['Att_EUR'] - df_raw['Inv_EUR']
df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['FX_Acq']
df_raw['Att_AUD'] = df_raw['Att_EUR'] * market_fx
df_raw['Gain_AUD'] = df_raw['Att_AUD'] - df_raw['Inv_AUD']

# --- 3. UI ---
st.title("🏛️ Claudio's Executive Portfolio")

tab1, tab2, tab3 = st.tabs(["📊 Riepilogo", "💸 Dettaglio & Simulatore", "📈 Storia Evolutiva"])

with tab1:
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Investito (€)", f"€{df_raw['Inv_EUR'].sum():,.0f}")
    m2.metric("Valore (€)", f"€{df_raw['Att_EUR'].sum():,.0f}")
    m3.metric("Valore (AUD)", f"${df_raw['Att_AUD'].sum():,.0f}")
    roi = ((df_raw['Att_EUR'].sum()/df_raw['Inv_EUR'].sum())-1)*100
    m4.metric("ROI (EUR)", f"{roi:.2f}%")

    c1, c2 = st.columns([1, 2])
    with c1:
        st.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', hole=0.4, title="Allocation"), use_container_width=True)
    with c2:
        agg_plot = df_raw.groupby('ISIN').agg({'Gain_EUR': 'sum', 'Gain_AUD': 'sum'}).reset_index()
        fig_comp = go.Figure()
        fig_comp.add_trace(go.Bar(name='Gain EUR (€)', x=agg_plot['ISIN'], y=agg_plot['Gain_EUR'], marker_color='#3366CC'))
        fig_comp.add_trace(go.Bar(name='Gain AUD ($)', x=agg_plot['ISIN'], y=agg_plot['Gain_AUD'], marker_color='#109618'))
        fig_comp.update_layout(title="Confronto Gain EUR vs AUD", barmode='group', legend=dict(orientation="h", y=1.1))
        st.plotly_chart(fig_comp, use_container_width=True)

with tab2:
    st.subheader("Simulatore di Vendita ed Impatto Fiscale (ATO)")
    df_raw['% Vendi'] = 0.0
    cols_sim = ['Data', 'ISIN', 'Qty', 'Prezzo_Acq', 'Price_Now', 'Gain_AUD', '% Vendi']
    
    edited_df = st.data_editor(
        df_raw[cols_sim], 
        column_config={"% Vendi": st.column_config.NumberColumn("Vendi %", min_value=0, max_value=100, step=1, format="%d%%")},
        hide_index=True, use_container_width=True
    )
    
    if edited_df['% Vendi'].sum() > 0:
        sel = edited_df[edited_df['% Vendi'] > 0].copy()
        # Calcolo logica tasse
        sel['Days'] = (datetime.now() - pd.to_datetime(sel['Data'], dayfirst=True)).dt.days
        sel['Inv_AUD_Orig'] = df_raw.loc[sel.index, 'Inv_AUD']
        # Ricavo = (Valore Attuale AUD) * % scelta
        sel['R_Gain_AUD'] = (sel['Qty'] * sel['Price_Now'] * market_fx * sel['% Vendi']/100) - (sel['Inv_AUD_Orig'] * sel['% Vendi']/100)
        # Sconto 50% se tenuto > 365 gg
        sel['Taxable'] = sel.apply(lambda r: r['R_Gain_AUD'] * 0.5 if (r['R_Gain_AUD'] > 0 and r['Days'] >= 365) else r['R_Gain_AUD'], axis=1)
        
        tot_gain_aud = sel['R_Gain_AUD'].sum()
        est_tax = max(0, sel['Taxable'].sum()) * 0.47 # Aliquota massima stimata
        
        c1, c2, c3 = st.columns(3)
        c1.metric("Gain Lordo Realizzato", f"${tot_gain_aud:,.2f} AUD")
        c2.metric("Tasse Stimate (47%)", f"- ${est_tax:,.2f} AUD")
        c3.metric("Netto in Tasca", f"${(tot_gain_aud - est_tax):,.2f} AUD", delta_color="normal")

with tab3:
    st.subheader("Evoluzione Storica (€)")
    start_date = df_raw['Date_DT'].min()
    with st.spinner("Caricamento storico..."):
        all_hist = {}
        for isin in df_raw['ISIN'].unique():
            t = ticker_map.get(isin)
            if t:
                h = yf.download(t, start=start_date, progress=False)['Close']
                if not h.empty:
                    if isinstance(h, pd.DataFrame): h = h.iloc[:, 0]
                    all_hist[isin] = h.reindex(pd.date_range(start_date, datetime.now()), method='ffill')

        dates = pd.date_range(start_date, datetime.now().date())
        history_values = [sum(float(all_hist[l['ISIN']].asof(d) if l['ISIN'] in all_hist and pd.notnull(all_hist[l['ISIN']].asof(d)) else l['Prezzo_Acq']) * l['Qty'] 
                          for _, l in df_raw[df_raw['Date_DT'].dt.date <= d.date()].iterrows()) for d in dates]
        
        fig_h = px.area(pd.DataFrame({'Data': dates, 'Valore': history_values}), x='Data', y='Valore')
        fig_h.update_traces(line_shape='hv')
        st.plotly_chart(fig_h, use_container_width=True)
