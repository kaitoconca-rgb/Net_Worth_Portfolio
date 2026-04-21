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
st.set_page_config(page_title="Claudio's Executive Console", layout="wide")

ticker_map = {
    "LU2885245055": "8OU9.DE", "IE0032077012": "EQQQ.DE", "IE00B02KXL92": "DJMC.AS",
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "8OU9.DE", "IE00B8GKDB10": "VHYL.MI",
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

# --- 3. PREZZI E STORICO ---
@st.cache_data(ttl=3600)
def get_full_market_context(isins_list):
    prices_hist = {}
    logs = {}
    for isin in isins_list:
        symbol = ticker_map.get(isin)
        try:
            h = yf.download(symbol, start="2024-09-01", progress=False)['Close']
            if isinstance(h, pd.DataFrame): h = h.iloc[:, 0]
            if not h.empty:
                prices_hist[isin] = h
                logs[isin] = {"status": "LIVE", "updated": datetime.now().strftime("%H:%M"), "source": "Yahoo"}
            else: raise ValueError()
        except:
            prices_hist[isin] = None
            logs[isin] = {"status": "FALLBACK", "updated": "-", "source": "Acq Price"}
    return prices_hist, logs

hist_map, diag_logs = get_full_market_context(df_raw['ISIN'].unique().tolist())

def get_current_price(row):
    if pd.notnull(row['Manual_Price']) and row['Manual_Price'] > 0: return row['Manual_Price']
    h = hist_map.get(row['ISIN'])
    if h is not None and not h.empty: return float(h.iloc[-1])
    return row['Prezzo_Acq']

df_raw['Price_Now'] = df_raw.apply(get_current_price, axis=1)

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
    m1.metric("Investito EUR", f"€{t_inv_eur:,.0f}")
    m1.metric("Valore Attuale EUR", f"€{t_att_eur:,.0f}", f"€{t_att_eur - t_inv_eur:,.0f}")
    m2.metric("Investito AUD (Storico)", f"${t_inv_aud:,.0f}")
    m2.metric("Valore Attuale AUD", f"${t_att_aud:,.0f}", f"${t_att_aud - t_inv_aud:,.0f}")
    m3.metric("ROI Totale (EUR)", f"{(t_att_eur/t_inv_eur-1)*100:.2f}%")
    m3.metric("ROI Totale (AUD)", f"{(t_att_aud/t_inv_aud-1)*100:.2f}%")

    st.divider()

    g1, g2 = st.columns([1, 1.5])
    with g1:
        st.plotly_chart(px.pie(df_raw, values='Att_EUR', names='ISIN', hole=0.4, title="Allocation %"), use_container_width=True)
    with g2:
        agg = df_raw.groupby('ISIN').agg({'Inv_EUR':'sum','Att_EUR':'sum','Inv_AUD':'sum','Att_AUD':'sum'}).reset_index()
        agg['Gain_EUR'] = agg['Att_EUR'] - agg['Inv_EUR']
        agg['Gain_AUD'] = agg['Att_AUD'] - agg['Inv_AUD']
        fig_fx = go.Figure(data=[
            go.Bar(name='Profit EUR (€)', x=agg['ISIN'], y=agg['Gain_EUR'], marker_color='#1f77b4'),
            go.Bar(name='Profit AUD ($)', x=agg['ISIN'], y=agg['Gain_AUD'], marker_color='#2ca02c')
        ])
        fig_fx.update_layout(title="FX Impact: Profitto EUR vs AUD", barmode='group')
        st.plotly_chart(fig_fx, use_container_width=True)

    st.subheader("Dettaglio Asset")
    st.dataframe(
        agg.style.format({
            'Inv_EUR': '€{:,.2f}', 'Att_EUR': '€{:,.2f}', 'Gain_EUR': '€{:,.2f}',
            'Inv_AUD': '${:,.2f}', 'Att_AUD': '${:,.2f}', 'Gain_AUD': '${:,.2f}'
        }).map(lambda x: 'color: red' if isinstance(x, (int, float)) and x < 0 else 'color: green' if isinstance(x, (int, float)) and x > 0 else '', 
               subset=['Gain_EUR', 'Gain_AUD']),
        use_container_width=True, hide_index=True
    )

with tab2:
    st.subheader("Simulatore Cash-out & Tasse")
    tax_r = st.slider("Marginal Tax Rate (%)", 0.0, 45.0, 37.0)
    
    df_sim = df_raw.copy()
    df_sim['% Vendi'] = 0.0
    ed = st.data_editor(df_sim[['ISIN','Data','Qty','Att_EUR','Inv_EUR','Att_AUD','Inv_AUD','% Vendi']], hide_index=True)
    
    sel = ed[ed['% Vendi'] > 0].copy()
    if not sel.empty:
        # Calcoli di Gain per riga
        sel['EUR_Gain_Realizzato'] = (sel['Att_EUR'] - sel['Inv_EUR']) * (sel['% Vendi']/100)
        sel['AUD_Gain_Realizzato'] = (sel['Att_AUD'] - sel['Inv_AUD']) * (sel['% Vendi']/100)
        sel['E_Out'] = sel['Att_EUR'] * (sel['% Vendi']/100)
        sel['A_Out'] = sel['Att_AUD'] * (sel['% Vendi']/100)
        
        def cgt_calc_row(row):
            gain = (row['Att_AUD'] - row['Inv_AUD']) * (row['% Vendi']/100)
            if gain <= 0: return 0.0
            mult = 0.5 if (datetime.now() - row['Data'].to_pydatetime()).days > 365 else 1.0
            return gain * mult * (tax_r/100)
        
        sel['Tassa_Asset'] = sel.apply(cgt_calc_row, axis=1)
        
        st.divider()
        # Header Totali
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Cash out EUR", f"€{sel['E_Out'].sum():,.2f}")
        r2.metric("Cash out AUD (Lordo)", f"${sel['A_Out'].sum():,.2f}")
        r3.metric("Tasse ATO (AUD)", f"-${sel['Tassa_Asset'].sum():,.2f}", delta_color="inverse")
        r4.metric("Netto AUD", f"${(sel['A_Out'].sum() - sel['Tassa_Asset'].sum()):,.2f}")

        # Nuova Sezione: Dettaglio Gain Asset (Richiesto)
        st.write("### Dettaglio Gain Realizzato")
        dettaglio_gain = sel[['ISIN', 'Data', '% Vendi', 'EUR_Gain_Realizzato', 'AUD_Gain_Realizzato', 'Tassa_Asset']]
        st.dataframe(
            dettaglio_gain.style.format({
                'EUR_Gain_Realizzato': '€{:,.2f}',
                'AUD_Gain_Realizzato': '${:,.2f}',
                'Tassa_Asset': '${:,.2f}',
                '% Vendi': '{:.0f}%'
            }).map(lambda x: 'color: red' if isinstance(x, (int, float)) and x < 0 else 'color: green' if isinstance(x, (int, float)) and x > 0 else '', 
                   subset=['EUR_Gain_Realizzato', 'AUD_Gain_Realizzato']),
            use_container_width=True, hide_index=True
        )

with tab3:
    st.subheader("Evoluzione Reale del Portafoglio (Market Value)")
    date_range = pd.date_range(date(2024, 10, 1), date.today())
    daily_history = []
    for d in date_range:
        posizioni = df_raw[df_raw['Data'].dt.date <= d.date()]
        valore_giorno = 0
        for _, pos in posizioni.iterrows():
            h = hist_map.get(pos['ISIN'])
            p_hist = h.asof(d) if (h is not None and not h.empty) else pos['Prezzo_Acq']
            valore_giorno += pos['Qty'] * p_hist
        daily_history.append({'Date': d, 'MarketValue': valore_giorno})
    df_h = pd.DataFrame(daily_history)
    st.plotly_chart(px.area(df_h, x='Date', y='MarketValue', title="Capitale da Ottobre 2024 ad Oggi (€)"), use_container_width=True)

with tab4:
    st.subheader("Data Health Check")
    st.write(f"FX EURAUD Live: {fx_now}")
    st.table(pd.DataFrame.from_dict(diag_logs, orient='index'))
