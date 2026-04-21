import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, date
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
def get_fx_data():
    try:
        t = yf.Ticker("EURAUD=X")
        now = float(t.fast_info['last_price'])
        hist = yf.download("EURAUD=X", start="2024-01-01", progress=False)['Close']
        if isinstance(hist, pd.DataFrame): hist = hist.iloc[:, 0]
        return now, hist
    except: return 1.6500, None

fx_now, fx_hist = get_fx_data()

# --- 2. DATI ---
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

# --- 3. PREZZI E STORICO (ROBUSTO) ---
@st.cache_data(ttl=3600)
def get_full_market_context(isins_list):
    prices_hist = {}
    logs = {}
    for isin in isins_list:
        symbol = ticker_map.get(isin)
        if symbol:
            try:
                h = yf.download(symbol, start="2024-09-01", progress=False)['Close']
                if isinstance(h, pd.DataFrame): h = h.iloc[:, 0]
                if not h.empty:
                    prices_hist[isin] = h
                    logs[isin] = {"status": "LIVE", "updated": datetime.now().strftime("%H:%M"), "source": "Yahoo"}
                else: raise ValueError("Empty")
            except:
                prices_hist[isin] = None
                logs[isin] = {"status": "FALLBACK", "updated": "-", "source": "Acq Price"}
        else:
            prices_hist[isin] = None
            logs[isin] = {"status": "MANUAL", "updated": "-", "source": "GSheets"}
    return prices_hist, logs

hist_map, diag_logs = get_full_market_context(df_raw['ISIN'].unique().tolist())

def get_current_price(row):
    if pd.notnull(row['Manual_Price']) and row['Manual_Price'] > 0: return row['Manual_Price']
    h = hist_map.get(row['ISIN'])
    if h is not None and not h.empty: return float(h.iloc[-1])
    return row['Prezzo_Acq']

df_raw['Price_Now'] = df_raw.apply(get_current_price, axis=1)

# Calcoli FX Storici
def get_fx_at(dt):
    try: return float(fx_hist.asof(dt))
    except: return 1.6500

df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['Data'].apply(get_fx_at)
df_raw['Att_EUR'] = df_raw['Qty'] * df_raw['Price_Now']
df_raw['Att_AUD'] = df_raw['Att_EUR'] * fx_now

# --- 4. INTERFACCIA ---
tab1, tab2, tab3, tab4 = st.tabs(["📊 Performance", "💸 Simulatore ATO", "📈 Timeline", "🛠️ Diagnostics"])

with tab1:
    t_inv_eur, t_att_eur = df_raw['Inv_EUR'].sum(), df_raw['Att_EUR'].sum()
    t_inv_aud, t_att_aud = df_raw['Inv_AUD'].sum(), df_raw['Att_AUD'].sum()
    
    m1, m2, m3 = st.columns(3)
    m1.metric("Capitale EUR", f"€{t_att_eur:,.0f}", f"Net: €{t_att_eur - t_inv_eur:,.0f}")
    m2.metric("Capitale AUD", f"${t_att_aud:,.0f}", f"Net: ${t_att_aud - t_inv_aud:,.0f}")
    m3.metric("ROI Totale", f"{(t_att_eur/t_inv_eur-1)*100:.1f}% (EUR)", f"{(t_att_aud/t_inv_aud-1)*100:.1f}% (AUD)")

    st.divider()
    # Grafico FX Impact
    agg = df_raw.groupby('ISIN').agg({'Inv_EUR':'sum','Att_EUR':'sum','Inv_AUD':'sum','Att_AUD':'sum'}).reset_index()
    agg['Gain_EUR'] = agg['Att_EUR'] - agg['Inv_EUR']
    agg['Gain_AUD'] = agg['Att_AUD'] - agg['Inv_AUD']
    
    fig_fx = go.Figure(data=[
        go.Bar(name='Profit EUR (€)', x=agg['ISIN'], y=agg['Gain_EUR'], marker_color='#1f77b4'),
        go.Bar(name='Profit AUD ($)', x=agg['ISIN'], y=agg['Gain_AUD'], marker_color='#2ca02c')
    ])
    fig_fx.update_layout(title="Confronto Profitto Reale (EUR vs AUD) - Evidenzia erosione valutaria", barmode='group')
    st.plotly_chart(fig_fx, use_container_width=True)

with tab2:
    st.subheader("Simulatore Cash-out & Tasse")
    tax_r = st.slider("Marginal Tax Rate (%)", 0.0, 45.0, 37.0)
    
    df_sim = df_raw.copy()
    df_sim['% Vendi'] = 0.0
    ed = st.data_editor(df_sim[['ISIN','Data','Qty','Att_EUR','Att_AUD','Inv_AUD','% Vendi']], hide_index=True)
    
    sel = ed[ed['% Vendi'] > 0].copy()
    if not sel.empty:
        sel['E_Out'] = sel['Att_EUR'] * (sel['% Vendi']/100)
        sel['A_Out'] = sel['Att_AUD'] * (sel['% Vendi']/100)
        sel['G_AUD'] = (sel['Att_AUD'] - sel['Inv_AUD']) * (sel['% Vendi']/100)
        
        def cgt_calc(row):
            if row['G_AUD'] <= 0: return 0.0
            mult = 0.5 if (datetime.now() - row['Data'].to_pydatetime()).days > 365 else 1.0
            return row['G_AUD'] * mult * (tax_r/100)
        
        total_tax = sel.apply(cgt_calc, axis=1).sum()
        
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Cash EUR", f"€{sel['E_Out'].sum():,.2f}")
        r2.metric("Cash AUD (Lordo)", f"${sel['A_Out'].sum():,.2f}")
        r3.metric("Tasse ATO (AUD)", f"-${total_tax:,.2f}", delta_color="inverse")
        r4.metric("Netto AUD", f"${(sel['A_Out'].sum() - total_tax):,.2f}")

with tab3:
    st.subheader("Evoluzione Reale del Portafoglio (Market Value)")
    # Ricostruzione giornaliera basata su quantità storiche e prezzi storici
    start_point = date(2024, 10, 1)
    end_point = date.today()
    date_range = pd.date_range(start_point, end_point)
    
    daily_history = []
    for d in date_range:
        # 1. Filtra asset posseduti in questa data
        posizioni = df_raw[df_raw['Data'].dt.date <= d.date()]
        valore_giorno = 0
        for _, pos in posizioni.iterrows():
            # 2. Cerca prezzo storico ISIN
            h = hist_map.get(pos['ISIN'])
            p_hist = h.asof(d) if (h is not None and not h.empty) else pos['Prezzo_Acq']
            valore_giorno += pos['Qty'] * p_hist
        daily_history.append({'Date': d, 'MarketValue': valore_giorno})
    
    df_h = pd.DataFrame(daily_history)
    fig_h = px.area(df_h, x='Date', y='MarketValue', title="Capitale da Ottobre 2024 ad Oggi (€)")
    fig_h.update_yaxes(title="Valore di Mercato Totale (€)")
    st.plotly_chart(fig_h, use_container_width=True)

with tab4:
    st.subheader("Data Health Check")
    st.write(f"FX EURAUD Live: {fx_now}")
    d_df = pd.DataFrame.from_dict(diag_logs, orient='index')
    st.table(d_df)
