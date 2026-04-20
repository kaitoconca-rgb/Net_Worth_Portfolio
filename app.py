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
    "LU2885245055": "8OU9.DE",  
    "IE0032077012": "EQQQ.DE", 
    "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE", 
    "IE00BFM15T99": "ISJP.DE", 
    "IE00B8GKDB10": "VHYL.MI",
    "IE00B3RBWM25": "VWRL.AS", 
    "IE00B3VVMM84": "VFEM.DE", 
    "IE00B3XXRP09": "VUSA.DE",
    "IE00BZ56RN96": "GGRW.MI", 
    "IE0005042456": "IUSA.DE"
}

@st.cache_data(ttl=600)
def get_fx_rate():
    try:
        t = yf.Ticker("EURAUD=X")
        val = t.fast_info['last_price']
        return float(val) if val else 1.6450
    except: return 1.6450

# --- 2. CARICAMENTO E ELABORAZIONE DATI ---
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

market_fx = get_fx_rate()
fx_hist = yf.download("EURAUD=X", start="2025-09-01", progress=False)['Close']

# Funzione di recupero prezzo migliorata
def get_current_price(isin, manual_val, buy_price):
    # 1. Se c'è un valore manuale nel foglio, ha la precedenza
    if pd.notnull(manual_val) and manual_val > 0:
        return float(manual_val)
    
    # 2. Altrimenti prova Yahoo Finance
    ticker_symbol = ticker_map.get(isin)
    if ticker_symbol:
        try:
            t = yf.Ticker(ticker_symbol)
            live_p = t.fast_info['last_price']
            if live_p and live_p > 0:
                return float(live_p)
        except:
            pass
            
    # 3. Fallback finale: Prezzo di acquisto (per non avere mai None)
    return float(buy_price)

# Applichiamo la logica
prices_now = []
for _, row in df_raw.iterrows():
    p = get_current_price(row['ISIN'], row['Manual_Override'], row['Prezzo_Acq'])
    prices_now.append(p)

df_raw['Price_Now'] = prices_now
df_raw['FX_Acq'] = df_raw['Date_DT'].apply(lambda x: fx_hist.asof(x) if not fx_hist.empty else 1.63)
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Gain_EUR'] = df_raw['Att_EUR'] - df_raw['Inv_EUR']
df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['FX_Acq']
df_raw['Att_AUD'] = df_raw['Att_EUR'] * market_fx
df_raw['Gain_AUD'] = df_raw['Att_AUD'] - df_raw['Inv_AUD']

# --- 3. INTERFACCIA ---
st.title("🏛️ Claudio's Portfolio Command Center")

tab1, tab2, tab3 = st.tabs(["📊 Riepilogo & Grafici", "💸 Analisi & Simulatore", "📈 Storia Evolutiva"])

with tab1:
    st.subheader("Performance Globale")
    t_inv_eur, t_att_eur = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    t_inv_aud, t_att_aud = df_raw['Inv_AUD'].sum(), df_raw['Att_AUD'].sum()

    c_m1, c_m2 = st.columns(2)
    with c_m1:
        st.table(pd.DataFrame({
            "EURO (€)": ["Investito", "Valore Attuale", "Guadagno", "ROI %"],
            "Dettaglio": [f"€{t_inv_eur:,.2f}", f"€{t_att_eur:,.2f}", f"€{(t_att_eur-t_inv_eur):,.2f}", f"{((t_att_eur/t_inv_eur)-1)*100:.2f}%"]
        }))
    with c_m2:
        st.table(pd.DataFrame({
            "AUD ($)": ["Investito", "Valore Attuale", "Guadagno", "ROI %"],
            "Dettaglio": [f"${t_inv_aud:,.2f}", f"${t_att_aud:,.2f}", f"${(t_att_aud-t_inv_aud):,.2f}", f"{((t_att_aud/t_inv_aud)-1)*100:.2f}%"]
        }))

    v1, v2 = st.columns([1, 2])
    with v1:
        st.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', hole=0.4, title="Allocazione Asset"), use_container_width=True)
    with v2:
        agg_p = df_raw.groupby('ISIN').agg({'Gain_EUR': 'sum', 'Gain_AUD': 'sum'}).reset_index()
        fig_b = go.Figure()
        fig_b.add_trace(go.Bar(name='Gain EUR (€)', x=agg_p['ISIN'], y=agg_p['Gain_EUR'], marker_color='#3366CC'))
        fig_b.add_trace(go.Bar(name='Gain AUD ($)', x=agg_p['ISIN'], y=agg_p['Gain_AUD'], marker_color='#109618'))
        fig_b.update_layout(title="Rendimento per ISIN (EUR vs AUD)", barmode='group')
        st.plotly_chart(fig_b, use_container_width=True)

    st.subheader("Dettaglio Posizioni Aggregato")
    st_agg = df_raw.groupby('ISIN').agg({'Qty':'sum','Inv_EUR':'sum','Att_EUR':'sum','Gain_EUR':'sum','Gain_AUD':'sum'}).reset_index()
    st.dataframe(st_agg.style.format(precision=2), use_container_width=True, hide_index=True)

with tab2:
    st.subheader("Dettaglio Lotti & Simulatore Vendita")
    df_raw['% Vendi'] = 0.0
    cols_to_show = ['Data', 'ISIN', 'Qty', 'Prezzo_Acq', 'Price_Now', 'Gain_EUR', 'Gain_AUD', '% Vendi']

    ed_df = st.data_editor(
        df_raw[cols_to_show],
        column_config={"% Vendi": st.column_config.NumberColumn("Vendi %", min_value=0, max_value=100, format="%d%%")},
        hide_index=True, use_container_width=True
    )

    if ed_df['% Vendi'].sum() > 0:
        sim = ed_df[ed_df['% Vendi'] > 0].copy()
        sim['Days'] = (datetime.now() - pd.to_datetime(sim['Data'], dayfirst=True)).dt.days
        sim['Inv_EUR_Lot'] = df_raw.loc[sim.index, 'Inv_EUR']
        sim['Inv_AUD_Lot'] = df_raw.loc[sim.index, 'Inv_AUD']

        sim['G_EUR_Sim'] = (sim['Qty'] * sim['Price_Now'] * sim['% Vendi']/100) - (sim['Inv_EUR_Lot'] * sim['% Vendi']/100)
        sim['G_AUD_Sim'] = (sim['Qty'] * sim['Price_Now'] * market_fx * sim['% Vendi']/100) - (sim['Inv_AUD_Lot'] * sim['% Vendi']/100)
        sim['Taxable_AUD'] = sim.apply(lambda r: r['G_AUD_Sim'] * 0.5 if (r['G_AUD_Sim'] > 0 and r['Days'] >= 365) else r['G_AUD_Sim'], axis=1)

        t_gain_eur = sim['G_EUR_Sim'].sum()
        t_gain_aud = sim['G_AUD_Sim'].sum()
        t_tax_aud = max(0, sim['Taxable_AUD'].sum()) * 0.47
        t_tax_eur = t_tax_aud / market_fx

        st.markdown("---")
        st.subheader("📊 Riepilogo Vendita (Simulazione)")
        res_data = {
            "Valuta": ["EURO (€)", "AUD ($)"],
            "Gain Lordo": [f"€{t_gain_eur:,.2f}", f"${t_gain_aud:,.2f}"],
            "Tasse Stimate (47%)": [f"€{t_tax_eur:,.2f}", f"- ${t_tax_aud:,.2f}"],
            "Netto Stimato": [f"€{(t_gain_eur - t_tax_eur):,.2f}", f"${(t_gain_aud - t_tax_aud):,.2f}"]
        }
        st.table(pd.DataFrame(res_data))

with tab3:
    st.subheader("Evoluzione Storica (Valore in €)")
    s_date = df_raw['Date_DT'].min()
    with st.spinner("Recupero dati storici..."):
        h_data = {}
        for isin in df_raw['ISIN'].unique():
            tk = ticker_map.get(isin)
            if tk:
                px_h = yf.download(tk, start=s_date, progress=False)['Close']
                if not px_h.empty:
                    if isinstance(px_h, pd.DataFrame): px_h = px_h.iloc[:, 0]
                    h_data[isin] = px_h.reindex(pd.date_range(s_date, datetime.now()), method='ffill')
        
        d_range = pd.date_range(s_date, datetime.now().date())
        vals = []
        for d in d_range:
            active = df_raw[df_raw['Date_DT'].dt.date <= d.date()]
            day_sum = sum([l['Qty'] * (h_data[l['ISIN']].asof(d) if l['ISIN'] in h_data else l['Prezzo_Acq']) for _, l in active.iterrows()])
            vals.append(day_sum)
        
        fig_h = px.area(pd.DataFrame({'Data': d_range, 'Valore': vals}), x='Data', y='Valore')
        st.plotly_chart(fig_h, use_container_width=True)
