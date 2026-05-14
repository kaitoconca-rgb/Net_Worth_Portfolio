import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, date
import time
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
    "IE0008471009": "EXW1.DE", "IE00BFM15T99": "36B2.MU", "IE00B8GKDB10": "VHYL.MI",
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
df_raw['Tipo'] = df_input['Tipo'].str.upper().fillna('BUY')
df_raw['Qty'] = pd.to_numeric(df_input['Cantidad'], errors='coerce')
df_raw['Inv_EUR'] = pd.to_numeric(df_input['Importe Cargado'], errors='coerce')
df_raw['Prezzo_Acq'] = pd.to_numeric(df_input['Precio'], errors='coerce') 
df_raw['Manual_Price'] = pd.to_numeric(df_input['Price'], errors='coerce')
df_raw.loc[df_raw['Tipo'] == 'SELL', 'Qty'] = -df_raw['Qty'].abs()
df_raw = df_raw.dropna(subset=['ISIN', 'Qty']).sort_values('Data')
def get_fx_at(dt):
    try: return float(fx_hist.asof(dt))
    except: return 1.6500

df_raw['Inv_AUD'] = df_raw['Inv_EUR'] * df_raw['Data'].apply(get_fx_at)

# --- 3. PREZZI E STORICO (OTTIMIZZATO DAL 01/10/2025) ---
@st.cache_data(ttl=3600)
def get_full_market_context(isins_list, current_ticker_map):
    prices_hist = {}
    logs = {}
    for isin in isins_list:
        symbol = current_ticker_map.get(isin)
        try:
            # Scarichiamo solo quanto strettamente necessario per la Timeline
            h = yf.download(symbol, start="2025-10-01", progress=False)['Close']
            if isinstance(h, pd.DataFrame): h = h.iloc[:, 0]
            
            # Se la tabella è arrivata ma l'ultimo valore è NaN, proviamo a recuperare il prezzo "live"
            if not h.empty and pd.isna(h.iloc[-1]):
                t_obj = yf.Ticker(symbol)
                last_price_data = t_obj.history(period="1d")['Close']
                if not last_price_data.empty:
                    # Riempiamo l'ultimo valore NaN con il prezzo live trovato
                    h.iloc[-1] = last_price_data.iloc[-1]

            if not h.empty:
                prices_hist[isin] = h
                current_val = float(h.iloc[-1])
               
                
                # 2. RECUPERO TIMESTAMP "LIVE" DALLA SCHEDA INFO
                market_time = None
                try:
                    # Interroghiamo direttamente il prezzo di mercato live e il suo timestamp
                    # regularMarketTime restituisce un timestamp Unix
                    ts = t_obj.info.get('regularMarketTime')
                    if ts:
                        market_time = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                except:
                    pass

                # Se info non risponde, usiamo l'indice della tabella come fallback
                if not market_time:
                    market_time = h.index[-1].strftime("%Y-%m-%d") + " (EOD)"


                
                logs[isin] = {
                    "status": "LIVE", 
                    "Price": f"€{current_val:.2f}",
                    "Market Time": market_time, # Ora esatta del dato Yahoo
                    "updated": datetime.now().strftime("%H:%M"), 
                    "source": f"Yahoo ({symbol})"
                }
            else:
                raise ValueError()
        except:
            prices_hist[isin] = None
            logs[isin] = {"status": "FALLBACK", "Price": "N/A", "updated": "-", "source": f"Error with {symbol}"}
    return prices_hist, logs
hist_map, diag_logs = get_full_market_context(df_raw['ISIN'].unique().tolist(), ticker_map)

# 2. Creazione del Portfolio Aggregato (Collassa BUY e SELL)
portfolio = df_raw.groupby('ISIN').agg({
    'Qty': 'sum',
    'Inv_EUR': 'sum',
    'Inv_AUD': 'sum'
}).reset_index()

# 3. Rimuovo le posizioni chiuse (se Qty è quasi zero, l'asset è venduto al 100%)
portfolio = portfolio[portfolio['Qty'].abs() > 0.001]

# 4. Calcolo dei prezzi correnti per le posizioni aperte (Enhanced Fallback)
def get_current_val(row):
    manual = df_raw[df_raw['ISIN'] == row['ISIN']]['Manual_Price'].iloc[-1]
    if pd.notnull(manual) and manual > 0: 
        return manual
    
    h = hist_map.get(row['ISIN'])
    if h is not None and not h.empty:
        val = float(h.iloc[-1])
        if val > 0: return val
        
    # Se tutto fallisce, usa l'ultimo prezzo di acquisto noto per non azzerare il valore
    return df_raw[df_raw['ISIN'] == row['ISIN']]['Prezzo_Acq'].iloc[-1]

portfolio['Price_Now'] = portfolio.apply(get_current_val, axis=1)
portfolio['Att_EUR'] = portfolio['Qty'] * portfolio['Price_Now']
portfolio['Att_AUD'] = portfolio['Att_EUR'] * fx_now
# --- 4. INTERFACCIA ---
tab1, tab2, tab3, tab4 = st.tabs(["📊 Performance", "💸 Simulatore ATO", "📈 Timeline", "🛠️ Diagnostics"])

with tab1:
    st.header("Performance Complessiva")

    # --- 1. VERIFICA E PREPARAZIONE COLONNE ---
    # Controlliamo come si chiama la colonna AUD nel tuo foglio
    col_aud = 'Prezzo_Acq_AUD' if 'Prezzo_Acq_AUD' in df_raw.columns else None
    
    # Se non esiste, cerchiamo 'Prezzo_AUD' o simili
    if not col_aud:
        for c in df_raw.columns:
            if 'AUD' in c.upper() and 'PREZZO' in c.upper():
                col_aud = c
                break

    # --- 2. LOGICA DI CALCOLO INTEGRATA ---
    
    # Calcolo Investito Storico (Acquisti)
    df_buys = df_raw[df_raw['Qty'] > 0].copy()
    total_invested_eur = (df_buys['Qty'] * df_buys['Prezzo_Acq']).sum()
    
    # Se abbiamo la colonna AUD la usiamo, altrimenti convertiamo dall'EUR col cambio attuale
    if col_aud:
        total_invested_aud = (df_buys['Qty'] * df_buys[col_aud]).sum()
    else:
        total_invested_aud = total_invested_eur / FX_AUD_EUR

    # Calcolo Valore Attuale e Performance per Asset
    asset_performance = []
    current_market_value_eur = 0

    for isin in df_raw['ISIN'].unique():
        asset_data = df_raw[df_raw['ISIN'] == isin]
        net_qty = asset_data['Qty'].sum()
        
        # Prezzo attuale
        h = hist_map.get(isin)
        p_now = h.iloc[-1] if (h is not None and not h.empty) else asset_data['Prezzo_Acq'].iloc[0]
        
        # Valore attuale
        v_at_market = max(0, net_qty * p_now)
        current_market_value_eur += v_at_market
        
        # Profitto Realizzato + Non Realizzato (EUR)
        cash_in_eur = (asset_data[asset_data['Qty'] < 0]['Qty'].abs() * asset_data[asset_data['Qty'] < 0]['Prezzo_Acq']).sum()
        cash_out_eur = (asset_data[asset_data['Qty'] > 0]['Qty'] * asset_data[asset_data['Qty'] > 0]['Prezzo_Acq']).sum()
        profit_eur = (v_at_market + cash_in_eur) - cash_out_eur
        
        # Profitto Realizzato + Non Realizzato (AUD)
        if col_aud:
            cash_in_aud = (asset_data[asset_data['Qty'] < 0]['Qty'].abs() * asset_data[asset_data['Qty'] < 0][col_aud]).sum()
            cash_out_aud = (asset_data[asset_data['Qty'] > 0]['Qty'] * asset_data[asset_data['Qty'] > 0][col_aud]).sum()
            v_at_market_aud = v_at_market / FX_AUD_EUR
            profit_aud = (v_at_market_aud + cash_in_aud) - cash_out_aud
        else:
            profit_aud = profit_eur / FX_AUD_EUR
            v_at_market_aud = v_at_market / FX_AUD_EUR
        
        asset_performance.append({
            'ISIN': isin,
            'Profit_EUR': profit_eur,
            'Profit_AUD': profit_aud,
            'Current_Value': v_at_market
        })

    df_perf = pd.DataFrame(asset_performance)
    
    # KPI Globali
    total_profit_eur = df_perf['Profit_EUR'].sum()
    total_profit_aud = df_perf['Profit_AUD'].sum()
    current_value_aud = current_market_value_eur / FX_AUD_EUR
    
    roi_eur = (total_profit_eur / total_invested_eur) * 100 if total_invested_eur != 0 else 0
    roi_aud = (total_profit_aud / total_invested_aud) * 100 if total_invested_aud != 0 else 0

    # --- 3. VISUALIZZAZIONE KPI ---
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("Investito EUR (Storico)", f"€{total_invested_eur:,.0f}")
        st.metric("Valore Attuale EUR", f"€{current_market_value_eur:,.0f}", f"€{total_profit_eur:,.0f}")
    with c2:
        st.metric("Investito AUD (Storico)", f"${total_invested_aud:,.0f}")
        st.metric("Valore Attuale AUD", f"${current_value_aud:,.0f}", f"${total_profit_aud:,.0f}")
    with c3:
        st.metric("ROI Totale (EUR)", f"{roi_eur:.2f}%")
        st.metric("ROI Totale (AUD)", f"{roi_aud:.2f}%")

    st.divider()

    # --- 4. GRAFICI ---
    col_left, col_right = st.columns(2)
    with col_left:
        st.subheader("Allocation %")
        df_pie = df_perf[df_perf['Current_Value'] > 0]
        fig_pie = px.pie(df_pie, values='Current_Value', names='ISIN', hole=0.4)
        st.plotly_chart(fig_pie, use_container_width=True)
        
    with col_right:
        st.subheader("FX Impact: Profitto EUR vs AUD")
        fig_bar = px.bar(
            df_perf, 
            x='ISIN', 
            y=['Profit_EUR', 'Profit_AUD'],
            barmode='group',
            labels={'value': 'Profitto (€/$)', 'variable': 'Valuta'},
            color_discrete_map={'Profit_EUR': '#1f77b4', 'Profit_AUD': '#2ca02c'}
        )
        st.plotly_chart(fig_bar, use_container_width=True)
with tab2:
    st.subheader("Simulatore Cash-out & Tasse (ATO compliant)")
    tax_brackets = {
        "0% (fino a $18,200)": 0.0,
        "16% ($18,201 – $45,000)": 16.0,
        "30% ($45,001 – $135,000)": 30.0,
        "37% ($135,001 – $190,000)": 37.0,
        "45% (oltre $190,000)": 45.0
    }
    selected_bracket = st.select_slider("Marginal Tax Rate", options=list(tax_brackets.keys()), value="37% ($135,001 – $190,000)")
    tax_r = tax_brackets[selected_bracket]
    
    st.info(f"Calcolo basato su un'aliquota del **{tax_r}%**.")
    
    # Per il simulatore usiamo il portfolio corrente
    df_sim = portfolio.copy()
    df_sim['% Vendi'] = 0.0
    ed = st.data_editor(df_sim[['ISIN','Qty','Price_Now','Att_EUR','Inv_EUR','Att_AUD','Inv_AUD','% Vendi']], hide_index=True, use_container_width=True)
    
    sel = ed[ed['% Vendi'] > 0].copy()
    if not sel.empty:
        sel['E_Out'] = sel['Att_EUR'] * (sel['% Vendi']/100)
        sel['A_Out'] = sel['Att_AUD'] * (sel['% Vendi']/100)
        
        # Semplificazione CGT: usiamo l'aliquota selezionata
        sel['Tassa_Stima'] = (sel['A_Out'] - (sel['Inv_AUD'] * sel['% Vendi']/100)).clip(lower=0) * (tax_r/100)
        
        st.divider()
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Cash out EUR", f"€{sel['E_Out'].sum():,.2f}")
        r2.metric("Cash out AUD (Lordo)", f"${sel['A_Out'].sum():,.2f}")
        r3.metric("Tasse Stimate (AUD)", f"-${sel['Tassa_Stima'].sum():,.2f}", delta_color="inverse")
        r4.metric("Netto AUD", f"${(sel['A_Out'].sum() - sel['Tassa_Stima'].sum()):,.2f}")

with tab3:
    st.subheader("Evoluzione Reale del Portafoglio (Market Value)")
    
    # 1. Definiamo il range temporale
    date_range = pd.date_range(date(2025, 10, 1), date.today())
    df_raw['Data_Solo'] = df_raw['Data'].dt.date
    all_isins = df_raw['ISIN'].unique()
    
    daily_data = []

    for d in date_range:
        current_date = d.date()
        # Calcoliamo la quantità posseduta esattamente in questo giorno
        snapshot = df_raw[df_raw['Data_Solo'] <= current_date].groupby('ISIN')['Qty'].sum()
        
        day_total = 0 
        
        for isin in all_isins:
            qty = snapshot.get(isin, 0)
            
            # Se non possedevo l'asset in quel giorno, mettiamo 0 e passiamo oltre
            if abs(qty) < 0.001:
                daily_data.append({'Date': d, 'ISIN': isin, 'MarketValue': 0.0, 'TotalDay': 0.0})
                continue
                
            # Recupero Prezzo
            h = hist_map.get(isin)
            p_hist = None
            
            if h is not None and not h.empty:
                try:
                    # Cerchiamo il prezzo più vicino disponibile
                    p_hist = h.asof(d)
                except:
                    p_hist = None
            
            # --- LOGICA DI RIPRISTINO AGGRESSIVA ---
            # Se Yahoo non restituisce nulla, usiamo il prezzo di acquisto nel ledger
            if p_hist is None or pd.isna(p_hist) or p_hist == 0:
                # Trova il primo prezzo di acquisto disponibile per questo ISIN nel tuo Excel
                ledger_price = df_raw[df_raw['ISIN'] == isin]['Prezzo_Acq'].dropna()
                p_hist = ledger_price.iloc[0] if not ledger_price.empty else 0
            
            valore_asset = float(qty * p_hist)
            day_total += valore_asset
            
            daily_data.append({
                'Date': d, 
                'ISIN': isin, 
                'MarketValue': valore_asset,
                'TotalDay': 0.0 
            })
        
        # Inseriamo il totale giornaliero per l'hover
        for item in daily_data:
            if item['Date'] == d:
                item['TotalDay'] = day_total

    df_timeline = pd.DataFrame(daily_data)

    if not df_timeline.empty:
        fig_timeline = px.area(
            df_timeline, 
            x='Date', 
            y='MarketValue', 
            color='ISIN',
            title="Evoluzione Capitale (€) - Storico Completo",
            custom_data=['TotalDay']
        )
        
        # 1. FORZIAMO LA VISUALIZZAZIONE DI TUTTI GLI ELEMENTI
        fig_timeline.update_layout(
            hovermode="x unified", # Mantiene gli asset raggruppati per data
            hoverlabel=dict(
                namelength=-1,     # Forza Plotly a non troncare i nomi lunghi degli ISIN
                bgcolor="white",   # Sfondo bianco per migliore leggibilità
                font_size=12,      # Dimensione font leggibile
            )
        )

        # 2. OTTIMIZZIAMO IL TOOLTIP PER MOSTRARE TUTTI I 10 ASSET
        fig_timeline.update_traces(
            hovertemplate="€%{y:,.2f}<extra></extra>" 
        )

        # 3. ESPANDIAMO L'ALTEZZA DEL GRAFICO (opzionale)
        # Se la tabella è molto lunga, aumentare l'altezza del grafico aiuta a non farla uscire dai bordi
        fig_timeline.update_layout(
            height=600, 
            yaxis_title="Valore Mercato (€)",
            xaxis_title="Timeline",
            legend_title="Asset (ISIN)",
            # Rimuoviamo il vincolo di visualizzazione per permettere a tutti i 10 asset di apparire
            hoverdistance=100, 
            spikedistance=1000
        )
        
        st.plotly_chart(fig_timeline, use_container_width=True)
with tab4:
    st.subheader("Data Health Check")
    st.write(f"FX EURAUD Live: {fx_now:.4f}")
    st.table(pd.DataFrame.from_dict(diag_logs, orient='index'))
