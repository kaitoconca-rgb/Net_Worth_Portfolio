import streamlit as st
import pandas as pd
import yfinance as yf
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, date
import time
from streamlit_gsheets import GSheetsConnection
# --- FETCH CURRENT FX RATE ---
try:
    # Attempt to get the real-time rate
    fx_data = yf.Ticker("EURAUD=X").history(period="1d")
    FX_AUD_EUR = 1 / fx_data['Close'].iloc[-1] 
except:
    # Fallback rate if Yahoo is down (approximate 0.61 EUR per 1 AUD)
    FX_AUD_EUR = 0.61
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

# =========================================================
# 1. LOGICA DI CALCOLO AGGIORNATA (Sopra i Tab)
# =========================================================

asset_performance = []
current_market_value_eur = 0

for isin in df_raw['ISIN'].unique():
    asset_data = df_raw[df_raw['ISIN'] == isin].sort_values('Data')
    net_qty = asset_data['Qty'].sum()
    
    # Prezzo attuale
    h = hist_map.get(isin)
    p_now = h.iloc[-1] if (h is not None and not h.empty) else asset_data['Prezzo_Acq'].iloc[0]
    
    # Valore attuale EUR
    v_at_market_eur = max(0, net_qty * p_now)
    current_market_value_eur += v_at_market_eur
    
    # Dati per posizioni chiuse (Date e Cambi)
    data_acquisto = asset_data[asset_data['Tipo'] == 'BUY']['Data'].min()
    data_vendita = asset_data[asset_data['Tipo'] == 'SELL']['Data'].max()
    
    # Recupero cambi storici dalle colonne gia calcolate nel tuo df_raw
    # Assumendo che tu abbia calcolato fx_rate = Inv_AUD / Inv_EUR
    fx_acquisto = asset_data[asset_data['Tipo'] == 'BUY']['Inv_AUD'].sum() / asset_data[asset_data['Tipo'] == 'BUY']['Inv_EUR'].sum() if not asset_data[asset_data['Tipo'] == 'BUY'].empty else 0
    fx_vendita = asset_data[asset_data['Tipo'] == 'SELL']['Inv_AUD'].abs().sum() / asset_data[asset_data['Tipo'] == 'SELL']['Inv_EUR'].abs().sum() if not asset_data[asset_data['Tipo'] == 'SELL'].empty else 0
    
    # Profitti
    cash_in_eur = asset_data[asset_data['Tipo'] == 'SELL']['Inv_EUR'].abs().sum()
    cash_out_eur = asset_data[asset_data['Tipo'] == 'BUY']['Inv_EUR'].sum()
    profit_eur = (v_at_market_eur + cash_in_eur) - cash_out_eur
    
    cash_in_aud = asset_data[asset_data['Tipo'] == 'SELL']['Inv_AUD'].abs().sum()
    cash_out_aud = asset_data[asset_data['Tipo'] == 'BUY']['Inv_AUD'].sum()
    v_at_market_aud = v_at_market_eur * fx_now 
    profit_aud = (v_at_market_aud + cash_in_aud) - cash_out_aud
    
    asset_performance.append({
        'ISIN': isin,
        'Profit_EUR': profit_eur,
        'Profit_AUD': profit_aud,
        'Current_Value': v_at_market_eur,
        'Data Acquisto': data_acquisto,
        'Data Vendita': data_vendita,
        'FX Acquisto': fx_acquisto,
        'FX Vendita': fx_vendita
    })

df_perf = pd.DataFrame(asset_performance)
# --- NUOVA LOGICA PER DETTAGLIO VENDITE (SPOSTA SOPRA I TAB) ---
vendite_effettuate = []

# Filtriamo solo le operazioni di vendita dal registro grezzo
df_sells = df_raw[df_raw['Tipo'] == 'SELL'].copy()

for _, row in df_sells.iterrows():
    isin = row['ISIN']
    data_v = row['Data']
    qty_v = abs(row['Qty'])
    prezzo_v = row['Prezzo_Acq'] # Prezzo a cui hai venduto
    incasso_eur = abs(row['Inv_EUR'])
    incasso_aud = abs(row['Inv_AUD'])
    
    # Per calcolare il profitto di QUESTA vendita, dobbiamo trovare il prezzo medio di carico (PMC)
    # degli acquisti precedenti a questa data per quello specifico ISIN
    acquisti_precedenti = df_raw[(df_raw['ISIN'] == isin) & 
                                 (df_raw['Tipo'] == 'BUY') & 
                                 (df_raw['Data'] < data_v)]
    
    if not acquisti_precedenti.empty:
        pmc_eur = acquisti_precedenti['Inv_EUR'].sum() / acquisti_precedenti['Qty'].sum()
        pmc_aud = acquisti_precedenti['Inv_AUD'].sum() / acquisti_precedenti['Qty'].sum()
        
        costo_base_eur = qty_v * pmc_eur
        costo_base_aud = qty_v * pmc_aud
        
        profit_eur = incasso_eur - costo_base_eur
        profit_aud = incasso_aud - costo_base_aud
        
        # Cambio all'acquisto (PMC) vs Cambio alla vendita (Effettivo)
        fx_acquisto = pmc_aud / pmc_eur
        fx_vendita = incasso_aud / incasso_eur
    else:
        # Fallback se non trova acquisti (errore dati)
        profit_eur = profit_aud = fx_acquisto = fx_vendita = 0

    vendite_effettuate.append({
        'Data': data_v,
        'ISIN': isin,
        'Quantità': qty_v,
        'Prezzo Vendita': prezzo_v,
        'FX Acquisto (PMC)': fx_acquisto,
        'FX Vendita': fx_vendita,
        'Profit_EUR': profit_eur,
        'Profit_AUD': profit_aud
    })

df_dettaglio_vendite = pd.DataFrame(vendite_effettuate)
# --- 4. INTERFACCIA ---
tab1, tab2, tab3, tab4 = st.tabs(["📊 Performance", "💸 Simulatore ATO", "📈 Timeline", "🛠️ Diagnostics"])

with tab1:
    st.header("Performance Complessiva")

    # Asset Chiusi vs Attivi
    df_realized = df_perf[df_perf['Current_Value'] < 0.01].copy()
    df_unrealized = df_perf[df_perf['Current_Value'] >= 0.01].copy()
    
    # Calcolo Capitale Attivo
    active_isins = df_unrealized['ISIN'].tolist()
    df_active_ledger = df_raw[df_raw['ISIN'].isin(active_isins)]
    active_inv_eur = df_active_ledger[df_active_ledger['Tipo'] == 'BUY']['Inv_EUR'].sum()
    active_inv_aud = df_active_ledger[df_active_ledger['Tipo'] == 'BUY']['Inv_AUD'].sum()
    
    curr_val_aud = current_market_value_eur * fx_now

    # --- ROW 1: PROFITTO TOTALE ---
    col_a, col_b = st.columns(2)
    col_a.metric("Profitto Totale EUR", f"€{df_perf['Profit_EUR'].sum():,.0f}", f"Incassato: €{df_realized['Profit_EUR'].sum():,.0f}")
    col_b.metric("Profitto Totale AUD", f"${df_perf['Profit_AUD'].sum():,.0f}", f"Incassato: ${df_realized['Profit_AUD'].sum():,.0f}")

    st.divider()

    # --- ROW 2: ASSET ATTIVI (Dettaglio richiesto) ---
    st.subheader("Analisi Portafoglio Attivo")
    c1, c2, c3 = st.columns(3)
    
    with c1:
        st.write("**Esposizione EUR**")
        st.metric("Investito", f"€{active_inv_eur:,.0f}")
        st.metric("Valore", f"€{current_market_value_eur:,.0f}")
        # Nuova riga: Differenza Valore - Investito
        diff_eur = current_market_value_eur - active_inv_eur
        st.write(f"**Plusvalenza: €{diff_eur:,.2f}**")
    
    with c2:
        st.write("**Esposizione AUD**")
        st.metric("Investito", f"${active_inv_aud:,.0f}")
        st.metric("Valore", f"${curr_val_aud:,.0f}")
        # Nuova riga: Differenza Valore - Investito
        diff_aud = curr_val_aud - active_inv_aud
        st.write(f"**Plusvalenza: ${diff_aud:,.2f}**")
        
    with c3:
        st.write("**Rendimento % (ROI)**")
        roi_eur = (df_unrealized['Profit_EUR'].sum() / active_inv_eur * 100) if active_inv_eur != 0 else 0
        roi_aud = (df_unrealized['Profit_AUD'].sum() / active_inv_aud * 100) if active_inv_aud != 0 else 0
        st.metric("ROI Attivo (EUR)", f"{roi_eur:.2f}%")
        st.metric("ROI Attivo (AUD)", f"{roi_aud:.2f}%")

    st.divider()
    st.subheader("Storico Operazioni di Vendita")
    
    # 1. Filtro vendite (Tipo = SELL)
    df_vendite = df_raw[df_raw['Tipo'].str.upper() == 'SELL'].copy()

    if not df_vendite.empty:
        # Rinominiamo subito la colonna Data originale
        df_vendite = df_vendite.rename(columns={'Data': 'Data Vendita'})

        def get_asset_history(row):
            isin = row['ISIN']
            
            # Recupero storici acquisti per questo specifico ISIN
            buys = df_raw[(df_raw['ISIN'] == isin) & (df_raw['Tipo'].str.upper() == 'BUY')]
            
            # Estraiamo la data del primo acquisto (o l'ultima prima della vendita)
            data_acquisto = buys['Data'].min() if not buys.empty else None
            
            total_bought = buys['Qty'].sum() if not buys.empty else 0
            total_inv_eur_buy = buys['Inv_EUR'].sum() if not buys.empty else 0
            total_inv_aud_buy = buys['Inv_AUD'].sum() if not buys.empty else 0
            
            # Calcoli Prezzi e Cambi
            pmc_eur = total_inv_eur_buy / total_bought if total_bought != 0 else 0
            avg_fx_buy = total_inv_aud_buy / total_inv_eur_buy if total_inv_eur_buy != 0 else 0
            
            inv_eur_sell = abs(row['Inv_EUR'])
            inv_aud_sell = abs(row['Inv_AUD'])
            qty_sold = abs(row['Qty'])
            
            prezzo_vendita_unitario = inv_eur_sell / qty_sold if qty_sold != 0 else 0
            avg_fx_sell = inv_aud_sell / inv_eur_sell if inv_eur_sell != 0 else 0
            
            return pd.Series({
                'Data Acquisto': data_acquisto,
                'Tot_Qty_Acquistata': total_bought,
                'Prezzo_Acquisto_PMC': pmc_eur,
                'Valore_Acquisto_Tot_EUR': total_inv_eur_buy,
                'FX_Acquisto_Medio': avg_fx_buy,
                'Prezzo_Vendita_Unitario': prezzo_vendita_unitario,
                'Valore_Vendita_EUR': inv_eur_sell,
                'FX_Vendita': avg_fx_sell
            })

        # Calcolo e pulizia
        history_results = df_vendite.apply(get_asset_history, axis=1)
        df_vendite = df_vendite.drop(columns=[c for c in history_results.columns if c in df_vendite.columns])
        df_vendite = pd.concat([df_vendite, history_results], axis=1)

        # 2. Definizione colonne desiderate con i nuovi nomi
        desired_cols = [
            'ISIN', 
            'Data Acquisto', 
            'Data Vendita', 
            'Qty', 
            'Tot_Qty_Acquistata', 
            'Prezzo_Acquisto_PMC', 
            'Valore_Acquisto_Tot_EUR', 
            'FX_Acquisto_Medio',
            'Prezzo_Vendita_Unitario', 
            'Valore_Vendita_EUR', 
            'FX_Vendita',
            'Profit_EUR', 
            'Profit_AUD'
        ]
        
        final_cols = [c for c in desired_cols if c in df_vendite.columns]

        # 3. Visualizzazione
        st.dataframe(
            df_vendite[final_cols].style.format({
                'Data Acquisto': lambda x: x.strftime('%Y-%m-%d') if pd.notnull(x) else "-",
                'Data Vendita': lambda x: x.strftime('%Y-%m-%d') if pd.notnull(x) else "-",
                'Qty': '{:.2f}',
                'Tot_Qty_Acquistata': '{:.2f}',
                'Prezzo_Acquisto_PMC': '€{:.4f}',
                'Valore_Acquisto_Tot_EUR': '€{:,.2f}',
                'FX_Acquisto_Medio': '{:.4f}',
                'Prezzo_Vendita_Unitario': '€{:.4f}',
                'Valore_Vendita_EUR': '€{:,.2f}',
                'FX_Vendita': '{:.4f}',
                'Profit_EUR': '€{:,.2f}',
                'Profit_AUD': '${:,.2f}'
            }),
            use_container_width=True
        )
    else:
        st.info("Nessuna operazione di vendita registrata.")
    st.divider()

    # --- 4. GRAFICI ---
    col_left, col_right = st.columns(2)
    with col_left:
        st.subheader("Allocation % (Solo Attivi)")
        fig_pie = px.pie(df_unrealized, values='Current_Value', names='ISIN', hole=0.4)
        st.plotly_chart(fig_pie, use_container_width=True)
        
    with col_right:
        st.subheader("Profitto per Asset (Inclusi Chiusi)")
        # Qui LU apparirà con il suo profitto EUR e la perdita/guadagno AUD
        fig_bar = px.bar(
            df_perf, 
            x='ISIN', 
            y=['Profit_EUR', 'Profit_AUD'],
            barmode='group',
            labels={'value': 'Profitto (€/$)', 'variable': 'Valuta'},
            color_discrete_map={'Profit_EUR': '#1f77b4', 'Profit_AUD': '#2ca02c'}
        )
        st.plotly_chart(fig_bar, use_container_width=True)

    # --- 5. DETTAGLIO POSIZIONI CHIUSE (The LU Section) ---
    if not df_realized.empty:
        with st.expander("Visualizza Dettaglio Posizioni Chiuse (es. LU)"):
            st.write("Questi asset sono stati venduti completamente. Il profitto è già consolidato.")
            st.dataframe(
                df_realized[['ISIN', 'Profit_EUR', 'Profit_AUD']].style.format({
                    'Profit_EUR': '€{:,.2f}',
                    'Profit_AUD': '${:,.2f}'
                }), 
                hide_index=True, 
                use_container_width=True
            )
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
