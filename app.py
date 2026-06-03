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
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "📊 Performance", "💸 Simulatore ATO", "📈 Timeline",
    "💱 FX Analysis", "🌱 Raiz", "🛠️ Diagnostics"
])

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
        # Funzione interna di calcolo
        def get_asset_history(row):
            isin = row['ISIN']
            data_corrente = row['Data'] # Usiamo il nome originale per il calcolo
            
            # Recupero storici acquisti per questo specifico ISIN
            buys = df_raw[(df_raw['ISIN'] == isin) & (df_raw['Tipo'].str.upper() == 'BUY')]
            
            # Calcolo Data Acquisto (il primo in assoluto)
            data_acq_val = buys['Data'].min() if not buys.empty else None
            
            # Calcolo Qty e Valori
            total_bought = buys['Qty'].sum() if not buys.empty else 0
            total_inv_eur_buy = buys['Inv_EUR'].sum() if not buys.empty else 0
            total_inv_aud_buy = buys['Inv_AUD'].sum() if not buys.empty else 0
            
            # Prezzi Unitari e Cambi
            pmc_eur = total_inv_eur_buy / total_bought if total_bought != 0 else 0
            avg_fx_buy = total_inv_aud_buy / total_inv_eur_buy if total_inv_eur_buy != 0 else 0
            
            inv_eur_sell = abs(row['Inv_EUR'])
            inv_aud_sell = abs(row['Inv_AUD'])
            qty_sold = abs(row['Qty'])
            
            prezzo_vend_unit = inv_eur_sell / qty_sold if qty_sold != 0 else 0
            fx_sell_val = inv_aud_sell / inv_eur_sell if inv_eur_sell != 0 else 0
            
            return pd.Series({
                'Data Acquisto': data_acq_val,
                'Tot_Qty_Acquistata': total_bought,
                'Prezzo_Acquisto_PMC': pmc_eur,
                'Valore_Acquisto_Tot_EUR': total_inv_eur_buy,
                'FX_Acquisto_Medio': avg_fx_buy,
                'Prezzo_Vendita_Unitario': prezzo_vend_unit,
                'Valore_Vendita_EUR': inv_eur_sell,
                'FX_Vendita': fx_sell_val
            })

        # Applichiamo i calcoli
        res = df_vendite.apply(get_asset_history, axis=1)
        df_vendite = pd.concat([df_vendite, res], axis=1)

        # 2. Rinominazione e Ordinamento
        df_vendite = df_vendite.rename(columns={'Data': 'Data Vendita'})

        cols_to_show = [
            'ISIN', 'Data Acquisto', 'Data Vendita', 'Qty', 
            'Tot_Qty_Acquistata', 'Prezzo_Acquisto_PMC', 'Valore_Acquisto_Tot_EUR', 
            'FX_Acquisto_Medio', 'Prezzo_Vendita_Unitario', 'Valore_Vendita_EUR', 
            'FX_Vendita', 'Profit_EUR', 'Profit_AUD'
        ]

        # Filtro finale per mostrare solo colonne esistenti
        final_view = [c for c in cols_to_show if c in df_vendite.columns]

        # 3. Render Tabella
        st.dataframe(
            df_vendite[final_view].style.format({
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
        st.info("Nessuna vendita registrata.")
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
    st.subheader("Simulatore Cash-out & Tasse (Granulare per Lotto d'Acquisto)")
    
    tax_brackets = {
        "0% (fino a AUD 18,200)": 0.0,
        "16% (AUD 18,201 – 45,000)": 16.0,
        "30% (AUD 45,001 – 135,000)": 30.0,
        "37% (AUD 135,001 – 190,000)": 37.0,
        "45% (oltre AUD 190,000)": 45.0
    }
    
    selected_bracket = st.select_slider("Marginal Tax Rate", options=list(tax_brackets.keys()), value="37% (AUD 135,001 – 190,000)")
    tax_r = tax_brackets[selected_bracket]
    
    st.info(f"L'impatto fiscale ATO è calcolato sulla plusvalenza del singolo lotto in AUD. La visualizzazione per data di acquisto ti permette di scegliere con precisione quali lotti liquidare.")

    # --- 1. COSTRUZIONE LOTTI APERTI (Dettaglio per Data) ---
    lotti_aperti = []
    
    for isin in df_raw['ISIN'].unique():
        asset_ledger = df_raw[df_raw['ISIN'] == isin].sort_values('Data').copy()
        
        # Recupero prezzo corrente / manuale
        h = hist_map.get(isin)
        p_now = float(h.iloc[-1]) if (h is not None and not h.empty) else asset_ledger['Prezzo_Acq'].iloc[0]
        manual = asset_ledger['Manual_Price'].iloc[-1]
        if pd.notnull(manual) and manual > 0:
            p_now = manual

        buys = asset_ledger[asset_ledger['Tipo'] == 'BUY'].copy()
        total_sold = abs(asset_ledger[asset_ledger['Tipo'] == 'SELL']['Qty'].sum())
        
        # Applicazione logica FIFO per quote residue
        for idx, buy_row in buys.iterrows():
            qty_iniziale = buy_row['Qty']
            if total_sold > 0:
                if total_sold >= qty_iniziale:
                    total_sold -= qty_iniziale
                    qty_residua = 0.0
                else:
                    qty_residua = qty_iniziale - total_sold
                    total_sold = 0.0
            else:
                qty_residua = qty_iniziale
            
            if qty_residua > 0.001:
                quota_lotto = qty_residua / qty_iniziale
                inv_eur_residual = buy_row['Inv_EUR'] * quota_lotto
                inv_aud_residual = buy_row['Inv_AUD'] * quota_lotto
                
                att_eur_val = qty_residua * p_now
                att_aud_val = att_eur_val * fx_now
                
                var_eur = ((att_eur_val / inv_eur_residual) - 1) * 100 if inv_eur_residual > 0 else 0
                var_aud = ((att_aud_val / inv_aud_residual) - 1) * 100 if inv_aud_residual > 0 else 0
                
                gain_eur = att_eur_val - inv_eur_residual
                gain_aud = att_aud_val - inv_aud_residual
                alert_status = "⚠️ FX LOSS" if (gain_eur > 0 and gain_aud < 0) else "✅ OK"
                
                giorni_possesso = (datetime.now().date() - buy_row['Data'].date()).days
                cgt_discount = "50% Disc" if giorni_possesso >= 365 else "No Disc"

                lotti_aperti.append({
                    'ISIN': isin,
                    'Data Acquisto': buy_row['Data'].strftime('%Y-%m-%d'),
                    'Stato': alert_status,
                    'CGT': cgt_discount,
                    'Qty Residua': qty_residua,
                    'Prezzo Acq (€)': buy_row['Prezzo_Acq'],
                    'Inv EUR (€)': inv_eur_residual,
                    'Att EUR (€)': att_eur_val,
                    'Var % EUR': var_eur,
                    'Inv AUD ($)': inv_aud_residual,
                    'Att AUD ($)': att_aud_val,
                    'Var % AUD': var_aud,
                    '% Vendi': 0.0
                })

    df_sim_lotti = pd.DataFrame(lotti_aperti)

    # --- NUOVO: RIEPILOGO VALORE TOTALE PORTAFOGLIO ATTIVO ---
    if not df_sim_lotti.empty:
        tot_inv_eur = df_sim_lotti['Inv EUR (€)'].sum()
        tot_att_eur = df_sim_lotti['Att EUR (€)'].sum()
        tot_inv_aud = df_sim_lotti['Inv AUD ($)'].sum()
        tot_att_aud = df_sim_lotti['Att AUD ($)'].sum()
        
        st.markdown("### 📊 Stato Attuale del Portafoglio Aperto")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Capitale Investito (EUR)", f"€{tot_inv_eur:,.2f}")
        m2.metric("Valore Attuale (EUR)", f"€{tot_att_eur:,.2f}", f"€{(tot_att_eur - tot_inv_eur):+,.2f}")
        m3.metric("Capitale Investito (AUD)", f"AUD {tot_inv_aud:,.2f}")
        m4.metric("Valore Attuale (AUD)", f"AUD {tot_att_aud:,.2f}", f"AUD {(tot_att_aud - tot_inv_aud):+,.2f}")
        st.divider()

    # --- 2. CONFIGURAZIONE DATA EDITOR ---
    column_config = {
        "ISIN": st.column_config.TextColumn("ISIN", width="medium"),
        "Data Acquisto": st.column_config.TextColumn("Data Acquisto", width="small"),
        "Stato": st.column_config.TextColumn("Stato", width="small"),
        "CGT": st.column_config.TextColumn("ATO CGT", width="small"),
        "Qty Residua": st.column_config.NumberColumn("Qty Disp.", format="%.2f"),
        "Prezzo Acq (€)": st.column_config.NumberColumn("Prezzo Acq", format="€%.2f"),
        "Inv EUR (€)": st.column_config.NumberColumn("Costo Base Lot (€)", format="€%.2f"),
        "Att EUR (€)": st.column_config.NumberColumn("Val. Corrente (€)", format="€%.2f"),
        "Var % EUR": st.column_config.NumberColumn("Var % (€)", format="%.2f%%"),
        "Inv AUD ($)": st.column_config.NumberColumn("Costo Base Lot (AUD)", format="$%.2f"),
        "Att AUD ($)": st.column_config.NumberColumn("Val. Corrente (AUD)", format="$%.2f"),
        "Var % AUD": st.column_config.NumberColumn("Var % (AUD)", format="%.2f%%"),
        "% Vendi": st.column_config.NumberColumn("% Vendi", min_value=0, max_value=100, step=5, format="%d%%")
    }

    display_cols = [
        'ISIN', 'Data Acquisto', 'Stato', 'CGT', 'Qty Residua', 'Prezzo Acq (€)',
        'Inv EUR (€)', 'Att EUR (€)', 'Var % EUR', 
        'Inv AUD ($)', 'Att AUD ($)', 'Var % AUD', '% Vendi'
    ]

    ed = st.data_editor(
        df_sim_lotti[display_cols],
        column_config=column_config,
        hide_index=True,
        use_container_width=True
    )
    
    # --- 3. LOGICA DI RIEPILOGO FISCALE SIMULAZIONE ---
    sel = ed[ed['% Vendi'] > 0].copy()
    
    if not sel.empty:
        sel['E_Out'] = sel['Att EUR (€)'] * (sel['% Vendi']/100)
        sel['A_Out'] = sel['Att AUD ($)'] * (sel['% Vendi']/100)
        sel['Lotto_Gain_AUD'] = (sel['Att AUD ($)'] - sel['Inv AUD ($)']) * (sel['% Vendi']/100)
        
        def calcola_gain_tassabile(row):
            if row['Lotto_Gain_AUD'] > 0 and row['CGT'] == "50% Disc":
                return row['Lotto_Gain_AUD'] * 0.5
            return row['Lotto_Gain_AUD']

        sel['Lotto_Gain_Tassabile_AUD'] = sel.apply(calcola_gain_tassabile, axis=1)
        
        total_realized_gain_aud = sel['Lotto_Gain_AUD'].sum()
        total_taxable_gain_aud = sel['Lotto_Gain_Tassabile_AUD'].sum()
        stima_tassa = max(0, total_taxable_gain_aud * (tax_r/100))
        
        st.divider()
        st.subheader("Riepilogo Simulazione di Vendita Selettiva (Lotti)")
        
        r1, r2, r3, r4, r5 = st.columns(5)
        r1.metric("Cash out EUR", f"€{sel['E_Out'].sum():,.2f}")
        r2.metric("Cash out AUD", f"AUD {sel['A_Out'].sum():,.2f}")
        
        if total_realized_gain_aud < 0:
            benefit = abs(total_realized_gain_aud * (tax_r/100))
            r3.metric("Minusvalenza Totale AUD", f"-AUD {abs(total_realized_gain_aud):,.2f}", delta="Deducibile")
            r4.metric("Tax Saving Stimato", f"AUD {benefit:,.2f}")
            r5.metric("Netto in Tasca AUD", f"AUD {sel['A_Out'].sum():,.2f}")
        else:
            sconto_applicato = total_realized_gain_aud - total_taxable_gain_aud
            r3.metric("Tasse Stimate (Con Sconto)", f"-AUD {stima_tassa:,.2f}", 
                      delta=f"Sconto CGT 50% applicato" if sconto_applicato > 0 else None, delta_color="inverse")
            r4.metric("Netto Stimato (Post-Tax)", f"AUD {(sel['A_Out'].sum() - stima_tassa):,.2f}")
            r5.metric("Plusvalenza Lorda AUD", f"AUD {total_realized_gain_aud:,.2f}")
    else:
        st.write("⬆️ Inserisci una percentuale nella colonna '% Vendi' dei singoli lotti datati per valutare l'impatto fiscale mirato.")
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
    st.subheader("💱 FX Impact Analysis — AUD/EUR")

    # ── 1. AUD/EUR EXCHANGE RATE CHART ──────────────────────────────────────
    st.markdown("### EUR/AUD Exchange Rate (Oct 2025 → Today)")

    if fx_hist is not None and not fx_hist.empty:
        # fx_hist is EURAUD (how many AUD per 1 EUR)
        fx_display = fx_hist[fx_hist.index >= "2025-10-01"].copy()
        fx_display.index = pd.to_datetime(fx_display.index)

        fig_fx = go.Figure()
        fig_fx.add_trace(go.Scatter(
            x=fx_display.index,
            y=fx_display.values,
            mode='lines',
            name='EUR/AUD',
            line=dict(color='#f39c12', width=2),
            fill='tozeroy',
            fillcolor='rgba(243,156,18,0.10)'
        ))
        fig_fx.add_hline(
            y=fx_now,
            line_dash="dash",
            line_color="red",
            annotation_text=f"Today: {fx_now:.4f}",
            annotation_position="bottom right"
        )
        fig_fx.update_layout(
            height=350,
            yaxis_title="AUD per 1 EUR",
            xaxis_title="Date",
            hovermode="x unified",
            margin=dict(t=30, b=30)
        )
        st.plotly_chart(fig_fx, use_container_width=True)
    else:
        st.warning("FX history not available — check Yahoo Finance connection.")

    st.divider()

    # ── 2. PORTFOLIO VALUE IN EUR AND AUD (HISTORICAL) ───────────────────────
    st.markdown("### Portfolio Value: EUR vs AUD (Oct 2025 → Today)")

    date_range_fx = pd.date_range("2025-10-01", date.today())
    df_raw['Data_Solo'] = df_raw['Data'].dt.date

    fx_timeline_rows = []

    for d in date_range_fx:
        current_date = d.date()
        snapshot = df_raw[df_raw['Data_Solo'] <= current_date].groupby('ISIN')['Qty'].sum()

        day_val_eur = 0.0

        for isin in df_raw['ISIN'].unique():
            qty = snapshot.get(isin, 0)
            if abs(qty) < 0.001:
                continue

            h = hist_map.get(isin)
            p = None
            if h is not None and not h.empty:
                try:
                    p = h.asof(d)
                except:
                    p = None
            if p is None or pd.isna(p) or p == 0:
                ledger_price = df_raw[df_raw['ISIN'] == isin]['Prezzo_Acq'].dropna()
                p = ledger_price.iloc[0] if not ledger_price.empty else 0

            day_val_eur += float(qty * p)

        # Get historical FX for this date
        fx_day = None
        if fx_hist is not None and not fx_hist.empty:
            try:
                fx_day = float(fx_hist.asof(d))
            except:
                fx_day = None
        if fx_day is None or pd.isna(fx_day) or fx_day == 0:
            fx_day = fx_now  # fallback

        fx_timeline_rows.append({
            'Date': d,
            'Value_EUR': day_val_eur,
            'Value_AUD': day_val_eur * fx_day,
            'FX_Rate': fx_day
        })

    df_fx_timeline = pd.DataFrame(fx_timeline_rows)

    if not df_fx_timeline.empty:
        fig_dual = go.Figure()
        fig_dual.add_trace(go.Scatter(
            x=df_fx_timeline['Date'],
            y=df_fx_timeline['Value_EUR'],
            mode='lines',
            name='Value (EUR €)',
            line=dict(color='#2980b9', width=2),
            yaxis='y1'
        ))
        fig_dual.add_trace(go.Scatter(
            x=df_fx_timeline['Date'],
            y=df_fx_timeline['Value_AUD'],
            mode='lines',
            name='Value (AUD $)',
            line=dict(color='#27ae60', width=2, dash='dot'),
            yaxis='y2'
        ))
        fig_dual.update_layout(
            height=400,
            hovermode="x unified",
            yaxis=dict(title="EUR €", tickprefix="€", side='left'),
            yaxis2=dict(title="AUD $", tickprefix="$", side='right', overlaying='y'),
            legend=dict(orientation="h", y=1.08),
            margin=dict(t=40, b=30)
        )
        st.plotly_chart(fig_dual, use_container_width=True)
        st.divider()

    # ── 2b. MARKET RETURN OVER TIME (EUR vs AUD) ─────────────────────────────
    st.markdown("### Market Return Over Time: EUR vs AUD (Oct 2025 → Today)")
    st.caption("Daily unrealised + realised gain/loss. EUR line = pure asset performance. AUD line = same return translated at the historical EUR/AUD rate each day.")
# Weighted average FX rate across all purchases (total AUD paid / total EUR paid)
    total_inv_eur_all = df_raw[df_raw['Tipo'] == 'BUY']['Inv_EUR'].sum()
    total_inv_aud_all = df_raw[df_raw['Tipo'] == 'BUY']['Inv_AUD'].sum()
    fx_weighted_purchase = total_inv_aud_all / total_inv_eur_all if total_inv_eur_all > 0 else fx_now
    mr_rows = []

    for d in date_range_fx:
        current_date = d.date()

        # Historical FX for this day
        fx_day = fx_now
        if fx_hist is not None and not fx_hist.empty:
            try:
                v = fx_hist.asof(d)
                if v and not pd.isna(v):
                    fx_day = float(v)
            except:
                pass

        day_mr_eur = 0.0
        day_mr_aud = 0.0
        day_fx_impact = 0.0

        for isin in df_raw['ISIN'].unique():
            asset_ledger = df_raw[df_raw['ISIN'] == isin].sort_values('Data')
            ledger_to_date = asset_ledger[asset_ledger['Data'].dt.date <= current_date]
            if ledger_to_date.empty:
                continue

            buys_to_date = ledger_to_date[ledger_to_date['Tipo'] == 'BUY']
            sells_to_date = ledger_to_date[ledger_to_date['Tipo'] == 'SELL']
            net_qty = ledger_to_date['Qty'].sum()
            is_closed = abs(net_qty) < 0.001

            # Current day price
            h = hist_map.get(isin)
            p_today = None
            if h is not None and not h.empty:
                try:
                    p_today = h.asof(d)
                except:
                    p_today = None
            if p_today is None or pd.isna(p_today) or p_today == 0:
                ledger_price = df_raw[df_raw['ISIN'] == isin]['Prezzo_Acq'].dropna()
                p_today = float(ledger_price.iloc[0]) if not ledger_price.empty else 0

            # ── Process each buy lot individually ──────────────────────────
            total_sold_fifo = abs(sells_to_date['Qty'].sum()) if not sells_to_date.empty else 0.0

            for _, buy_row in buys_to_date.iterrows():
                qty_ini = buy_row['Qty']

                # FIFO residual
                if total_sold_fifo > 0:
                    if total_sold_fifo >= qty_ini:
                        total_sold_fifo -= qty_ini
                        qty_res = 0.0
                    else:
                        qty_res = qty_ini - total_sold_fifo
                        total_sold_fifo = 0.0
                else:
                    qty_res = qty_ini

                qty_for_calc = qty_ini if is_closed else qty_res
                if qty_for_calc < 0.001:
                    continue

                # Lot-level purchase metrics
                quota = qty_for_calc / qty_ini
                cost_eur = buy_row['Inv_EUR'] * quota
                cost_aud = buy_row['Inv_AUD'] * quota
                fx_at_purchase = cost_aud / cost_eur if cost_eur > 0 else fx_day

                if is_closed:
                    # Use actual proceeds for closed lots
                    total_buy_qty = buys_to_date['Qty'].sum()
                    lot_share = qty_for_calc / total_buy_qty
                    proceeds_eur = abs(sells_to_date['Inv_EUR'].sum()) * lot_share
                    proceeds_aud = abs(sells_to_date['Inv_AUD'].sum()) * lot_share
                    val_eur_today = proceeds_eur
                    val_aud_today = proceeds_aud
                else:
                    val_eur_today = qty_for_calc * float(p_today)
                    val_aud_today = val_eur_today * fx_day

                # Market return for this lot
                lot_mr_eur = val_eur_today - cost_eur
                lot_mr_aud = val_aud_today - cost_aud

                # FX Impact for this lot on this day:
                # = what the EUR value is worth today in AUD at today's rate
                #   minus what it would have been worth at the purchase rate
                lot_fx_impact = val_eur_today * (fx_day - fx_at_purchase)

                day_mr_eur    += lot_mr_eur
                day_mr_aud    += lot_mr_aud
                day_fx_impact += lot_fx_impact

        mr_rows.append({
            'Date': d,
            'Market Return (EUR)': day_mr_eur,
            'Market Return (AUD)': day_mr_aud,
            'FX Impact (AUD)': day_fx_impact,
            'FX Rate': fx_day
        })

    df_mr_timeline = pd.DataFrame(mr_rows)

    if not df_mr_timeline.empty:
        fig_mr = go.Figure()

        fig_mr.add_trace(go.Scatter(
            x=df_mr_timeline['Date'],
            y=df_mr_timeline['Market Return (EUR)'],
            mode='lines',
            name='Market Return (EUR €)',
            line=dict(color='#2980b9', width=2),
            yaxis='y1'
        ))
        fig_mr.add_trace(go.Scatter(
            x=df_mr_timeline['Date'],
            y=df_mr_timeline['Market Return (AUD)'],
            mode='lines',
            name='Market Return (AUD $)',
            line=dict(color='#27ae60', width=2, dash='dot'),
            yaxis='y2'
        ))
        fig_mr.add_trace(go.Scatter(
            x=df_mr_timeline['Date'],
            y=df_mr_timeline['FX Impact (AUD)'],
            mode='lines',
            name='FX Impact (AUD $)',
            line=dict(color='#e74c3c', width=1.5, dash='dashdot'),
            fill='tozeroy',
            fillcolor='rgba(231,76,60,0.07)',
            yaxis='y2'
        ))
        # Zero line for reference
        fig_mr.add_hline(y=0, line_dash="dash", line_color="grey", opacity=0.4, yref='y1')

        fig_mr.update_layout(
            height=420,
            hovermode="x unified",
            yaxis=dict(
                title="EUR € / AUD $ gain/loss",
                side='left',
                zeroline=True,
                zerolinecolor='#bdc3c7'
            ),
            yaxis2=dict(
                title="AUD $ gain/loss",
                side='right',
                overlaying='y',
                scaleanchor='y',
                scaleratio=1,
                zeroline=True,
                zerolinecolor='#bdc3c7',
                showticklabels=False  # hide right axis ticks since scale is identical
            ),
            legend=dict(orientation="h", y=1.08),
            margin=dict(t=40, b=30)
        )

        st.plotly_chart(fig_mr, use_container_width=True)

        # Small callout: gap between EUR and AUD return today
        last = df_mr_timeline.iloc[-1]
        gap = last['FX Impact (AUD)']   # <-- was: last['Market Return (AUD)'] - last['Market Return (EUR)'] * fx_now
        gap_colour = "#27ae60" if gap >= 0 else "#e74c3c"
        gap_label  = "added" if gap >= 0 else "subtracted"
        st.markdown(
            f"""
            <div style="background:#f8f9fa; border-left:4px solid #7f8c8d;
                        padding:10px 16px; border-radius:4px; font-size:0.9rem; margin-top:4px;">
                <b>Today's FX gap:</b> Converting your current EUR return at today's rate gives 
                <b>€{last['Market Return (EUR)']:,.2f} × {fx_now:.4f} = ${last['Market Return (EUR)'] * fx_now:,.2f} AUD</b> — 
                the AUD return line sits at <b>${last['Market Return (AUD)']:,.2f}</b>, 
                meaning historical FX movements have <span style="color:{gap_colour}"><b>{gap_label} ${abs(gap):,.2f} AUD</b></span> 
                to your return over the period.
            </div>
            """,
            unsafe_allow_html=True
        )
        
    st.divider()

  # ── 3. FX DECOMPOSITION TABLE ─────────────────────────────────────────────
    st.markdown("### Portfolio FX Impact Decomposition (Per Lot)")
    st.caption("Splits your total AUD P&L into: (a) pure market return and (b) gain/loss caused purely by EUR/AUD rate movement since purchase. Includes fully sold positions.")

    fx_decomp_rows = []

    for isin in df_raw['ISIN'].unique():
        asset_ledger = df_raw[df_raw['ISIN'] == isin].sort_values('Data').copy()

        # Current price
        h = hist_map.get(isin)
        p_now_fx = float(h.iloc[-1]) if (h is not None and not h.empty) else asset_ledger['Prezzo_Acq'].iloc[0]
        manual = asset_ledger['Manual_Price'].iloc[-1]
        if pd.notnull(manual) and manual > 0:
            p_now_fx = manual

        buys = asset_ledger[asset_ledger['Tipo'] == 'BUY'].copy()
        sells = asset_ledger[asset_ledger['Tipo'] == 'SELL'].copy()
        net_qty = asset_ledger['Qty'].sum()
        is_closed = abs(net_qty) < 0.001  # fully sold position

        total_sold_fifo = abs(sells['Qty'].sum())

        for _, buy_row in buys.iterrows():
            qty_ini = buy_row['Qty']

            # FIFO: determine residual open qty for this lot
            if total_sold_fifo > 0:
                if total_sold_fifo >= qty_ini:
                    total_sold_fifo -= qty_ini
                    qty_res = 0.0
                else:
                    qty_res = qty_ini - total_sold_fifo
                    total_sold_fifo = 0.0
            else:
                qty_res = qty_ini

            # For closed positions, ALL qty was sold — use full lot for realised calc
            # For open positions, use residual qty only
            qty_for_calc = qty_ini if is_closed else qty_res

            if qty_for_calc < 0.001:
                continue

            quota = qty_for_calc / qty_ini
            cost_eur = buy_row['Inv_EUR'] * quota
            cost_aud = buy_row['Inv_AUD'] * quota
            p_buy = buy_row['Prezzo_Acq']
            fx_buy = cost_aud / cost_eur if cost_eur > 0 else fx_now

            if is_closed:
                # Realised: use actual sell proceeds
                # Apportion sell proceeds proportionally if multiple buy lots
                total_buy_qty = buys['Qty'].sum()
                lot_share = qty_for_calc / total_buy_qty
                proceeds_eur = abs(sells['Inv_EUR'].sum()) * lot_share
                proceeds_aud = abs(sells['Inv_AUD'].sum()) * lot_share
                val_eur_now = proceeds_eur
                val_aud_now = proceeds_aud
                # FX at sale (weighted average)
                fx_sell = proceeds_aud / proceeds_eur if proceeds_eur > 0 else fx_now
            else:
                val_eur_now = qty_for_calc * p_now_fx
                val_aud_now = val_eur_now * fx_now
                fx_sell = fx_now

            # ── Decomposition ──────────────────────────────────────────────
            market_return_eur = val_eur_now - cost_eur
            market_return_aud_at_purchase_fx = market_return_eur * fx_buy
            fx_impact_aud = val_eur_now * (fx_sell - fx_buy)
            total_pl_aud = val_aud_now - cost_aud

            giorni = (datetime.now().date() - buy_row['Data'].date()).days
            status = "🔒 Closed" if is_closed else "✅ Open"

            fx_decomp_rows.append({
                'ISIN': isin,
                'Status': status,
                'Date Purchased': buy_row['Data'].strftime('%Y-%m-%d'),
                'Days Held': giorni,
                'Qty': qty_for_calc,
                'Cost (EUR)': cost_eur,
                'Value Now (EUR)': val_eur_now,
                'Market Return (EUR)': market_return_eur,
                'FX at Purchase': fx_buy,
                'FX at Sale/Today': fx_sell,
                'FX Δ': fx_sell - fx_buy,
                'Market Return in AUD (at purchase FX)': market_return_aud_at_purchase_fx,
                'FX Impact (AUD)': fx_impact_aud,
                'Total P&L (AUD)': total_pl_aud,
            })

    df_decomp = pd.DataFrame(fx_decomp_rows)

    if not df_decomp.empty:
        # Summary metrics
        tot_mkt_eur = df_decomp['Market Return (EUR)'].sum()
        tot_mkt_aud = df_decomp['Market Return in AUD (at purchase FX)'].sum()
        tot_fx_aud  = df_decomp['FX Impact (AUD)'].sum()
        tot_pl_aud  = df_decomp['Total P&L (AUD)'].sum()

        sm1, sm2, sm3, sm4 = st.columns(4)
        sm1.metric("Market Return (EUR)", f"€{tot_mkt_eur:,.2f}",
                   help="Pure asset price appreciation in EUR across all lots (open + closed)")
        sm2.metric("Market Return (AUD at buy FX)", f"${tot_mkt_aud:,.2f}",
                   help="EUR market return converted at the rate that existed when each lot was purchased")
        sm3.metric("FX Impact (AUD)", f"${tot_fx_aud:,.2f}",
                   help="Gain/loss caused purely by EUR/AUD rate movement since each purchase",
                   delta=f"{'▲' if tot_fx_aud >= 0 else '▼'} {abs(tot_fx_aud/tot_pl_aud*100):.1f}% of total P&L" if tot_pl_aud != 0 else None,
                   delta_color="normal" if tot_fx_aud >= 0 else "inverse")
        sm4.metric("Total P&L (AUD)", f"${tot_pl_aud:,.2f}",
                   help="Market Return (at buy FX) + FX Impact — reconciles to actual AUD gain/loss")
# Realised vs Unrealised breakdown
        df_closed_decomp = df_decomp[df_decomp['Status'] == '🔒 Closed']
        df_open_decomp   = df_decomp[df_decomp['Status'] == '✅ Open']

        realised_eur   = df_closed_decomp['Market Return (EUR)'].sum()
        realised_aud   = df_closed_decomp['Total P&L (AUD)'].sum()
        unrealised_eur = df_open_decomp['Market Return (EUR)'].sum()
        unrealised_aud = df_open_decomp['Total P&L (AUD)'].sum()

        r_colour = "#27ae60" if realised_aud >= 0 else "#e74c3c"
        u_colour = "#27ae60" if unrealised_aud >= 0 else "#e74c3c"

        st.markdown(
            f"""
            <div style="background:#f8f9fa; border-left:4px solid #7f8c8d; 
                        padding:10px 16px; border-radius:4px; font-size:0.9rem; margin-top:8px;">
                <b>P&L Composition:</b>&nbsp;&nbsp;
                🔒 <b>Realised (sold assets):</b> 
                    <span style="color:{r_colour}">€{realised_eur:,.2f} EUR</span> / 
                    <span style="color:{r_colour}">${realised_aud:,.2f} AUD</span>
                &nbsp;&nbsp;|&nbsp;&nbsp;
                ✅ <b>Unrealised (open positions):</b> 
                    <span style="color:{u_colour}">€{unrealised_eur:,.2f} EUR</span> / 
                    <span style="color:{u_colour}">${unrealised_aud:,.2f} AUD</span>
            </div>
            """,
            unsafe_allow_html=True
        )
        st.divider()

        # Stacked bar per ISIN
        df_bar_fx = df_decomp.groupby(['ISIN', 'Status']).agg({
            'Market Return in AUD (at purchase FX)': 'sum',
            'FX Impact (AUD)': 'sum',
            'Total P&L (AUD)': 'sum'
        }).reset_index()

        # Label closed assets clearly on x-axis
        df_bar_fx['Label'] = df_bar_fx.apply(
            lambda r: f"{r['ISIN']} 🔒" if r['Status'] == '🔒 Closed' else r['ISIN'], axis=1
        )

        # Separate open and closed for distinct colouring
        df_open_bar = df_bar_fx[df_bar_fx['Status'] == '✅ Open']
        df_closed_bar = df_bar_fx[df_bar_fx['Status'] == '🔒 Closed']

        fig_decomp_bar = go.Figure()

        # Open positions — standard colours
        fig_decomp_bar.add_trace(go.Bar(
            name='Market Return — Open (AUD)',
            x=df_open_bar['Label'],
            y=df_open_bar['Market Return in AUD (at purchase FX)'],
            marker_color='#2980b9'
        ))
        fig_decomp_bar.add_trace(go.Bar(
            name='FX Impact — Open (AUD)',
            x=df_open_bar['Label'],
            y=df_open_bar['FX Impact (AUD)'],
            marker_color='#e74c3c'
        ))

        # Closed positions — muted/hatched to visually separate them
        fig_decomp_bar.add_trace(go.Bar(
            name='Market Return — Sold 🔒 (AUD)',
            x=df_closed_bar['Label'],
            y=df_closed_bar['Market Return in AUD (at purchase FX)'],
            marker=dict(
                color='#85c1e9',           # lighter blue
                pattern=dict(shape="/")    # hatching to signal "realised"
            )
        ))
        fig_decomp_bar.add_trace(go.Bar(
            name='FX Impact — Sold 🔒 (AUD)',
            x=df_closed_bar['Label'],
            y=df_closed_bar['FX Impact (AUD)'],
            marker=dict(
                color='#f1948a',           # lighter red
                pattern=dict(shape="/")    # hatching
            )
        ))

        fig_decomp_bar.update_layout(
            barmode='stack',
            title="AUD P&L Split: Market Return vs FX Impact — solid = open, hatched = fully sold 🔒",
            yaxis_title="AUD $",
            height=400,
            hovermode="x unified",
            legend=dict(orientation="h", y=1.15),
            annotations=[
                dict(
                    x=row['Label'],
                    y=max(row['Market Return in AUD (at purchase FX)'] + row['FX Impact (AUD)'], 0),
                    text="SOLD",
                    showarrow=False,
                    font=dict(size=10, color="#922b21"),
                    yshift=8
                )
                for _, row in df_closed_bar.iterrows()
            ]
        )
        st.plotly_chart(fig_decomp_bar, use_container_width=True)

        # Detail table — highlight closed rows
        st.markdown("#### Lot-Level Detail")
        
        def highlight_closed(row):
            if row['Status'] == '🔒 Closed':
                return ['background-color: rgba(231,76,60,0.08)'] * len(row)
            return [''] * len(row)

        st.dataframe(
            df_decomp.style
            .apply(highlight_closed, axis=1)
            .map(
                lambda v: 'color: #27ae60' if isinstance(v, (int, float)) and v > 0
                else ('color: #e74c3c' if isinstance(v, (int, float)) and v < 0 else ''),
                subset=['Market Return (EUR)', 'FX Impact (AUD)', 'Total P&L (AUD)']
            )
            .format({
                'Qty': '{:.4f}',
                'Cost (EUR)': '€{:,.2f}',
                'Value Now (EUR)': '€{:,.2f}',
                'Market Return (EUR)': '€{:,.2f}',
                'FX at Purchase': '{:.4f}',
                'FX at Sale/Today': '{:.4f}',
                'FX Δ': '{:+.4f}',
                'Market Return in AUD (at purchase FX)': '${:,.2f}',
                'FX Impact (AUD)': '${:,.2f}',
                'Total P&L (AUD)': '${:,.2f}',
            }),
            use_container_width=True,
            hide_index=True
        )
    else:
        st.info("No lots found to analyse.")
# ============================================================
# RAIZ TAB — paste this block into app.py
#
# STEP 1: Change your tab declaration to:
#
#   tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
#       "📊 Performance", "💸 Simulatore ATO", "📈 Timeline",
#       "💱 FX Analysis", "🌱 Raiz", "🛠️ Diagnostics"
#   ])
#
# STEP 2: Rename existing "with tab4:" (Diagnostics) → "with tab6:"
#
# STEP 3: Paste this entire block between the FX Analysis tab and Diagnostics tab
#
# STEP 4: Add to requirements.txt:
#   google-api-python-client
#   google-auth
#
# STEP 5: Add to secrets.toml:
#   [gdrive]
#   raiz_folder_id = "YOUR_GOOGLE_DRIVE_FOLDER_ID"
#   (Folder ID = the long string at the end of your Drive folder URL)
#   The service account credentials are reused from [connections][gsheets]
# ============================================================

with tab5:
    st.header("🌱 Raiz Portfolio")

    # ── ETF ticker map: ASX code → Yahoo Finance symbol ──────────────────────
    RAIZ_TICKER_MAP = {
        "AAA": "AAA.AX",   # Betashares High Interest Cash
        "STW": "STW.AX",   # SPDR S&P/ASX 200
        "IAA": "IAA.AX",   # iShares Asia 50
        "IEU": "IEU.AX",   # iShares Europe
        "IAF": "IAF.AX",   # iShares Core Composite Bond
        "RCB": "RCB.AX",   # Russell Corporate Bond (may be delisted — fallback used)
        "IVV": "IVV.AX",   # iShares S&P 500
    }

    ETF_NAMES = {
        "AAA": "Betashares Cash",
        "STW": "SPDR ASX 200",
        "IAA": "iShares Asia 50",
        "IEU": "iShares Europe",
        "IAF": "iShares Bond",
        "RCB": "Russell Corp Bond",
        "IVV": "iShares S&P 500",
    }

    # ── Live + historical prices from Yahoo ──────────────────────────────────
    @st.cache_data(ttl=3600)
    def get_raiz_prices(tickers_tuple):
        tickers = dict(tickers_tuple)
        prices = {}
        hist_prices = {}
        for code, ticker in tickers.items():
            try:
                h = yf.download(ticker, start="2025-10-01", progress=False)['Close']
                if isinstance(h, pd.DataFrame):
                    h = h.iloc[:, 0]
                if not h.empty:
                    # Try to patch last NaN with live price
                    if pd.isna(h.iloc[-1]):
                        live = yf.Ticker(ticker).history(period="1d")['Close']
                        if not live.empty:
                            h.iloc[-1] = float(live.iloc[-1])
                    prices[code] = float(h.iloc[-1]) if not pd.isna(h.iloc[-1]) else None
                    hist_prices[code] = h
                else:
                    prices[code] = None
                    hist_prices[code] = None
            except:
                prices[code] = None
                hist_prices[code] = None
        return prices, hist_prices

    # ── Load CSV from Google Drive ────────────────────────────────────────────
    @st.cache_data(ttl=300)
    def load_raiz_from_gdrive():
        """
        Reads the most recently modified CSV from the RAIZ folder in Google Drive,
        using the same service account already configured for the gsheets connection.
        Returns (DataFrame, label_string) or (None, error_string).
        """
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaIoBaseDownload
            import io

            # Detect secrets structure
            gs = st.secrets["gdrive"]

            creds_dict = {
                "type":           gs.get("type", "service_account"),
                "project_id":     gs["project_id"],
                "private_key_id": gs["private_key_id"],
                "private_key":    gs["private_key"],
                "client_email":   gs["client_email"],
                "client_id":      gs.get("client_id", ""),
                "token_uri":      "https://oauth2.googleapis.com/token",
            }

            creds = service_account.Credentials.from_service_account_info(
                creds_dict,
                scopes=["https://www.googleapis.com/auth/drive.readonly"]
            )

            service = build("drive", "v3", credentials=creds, cache_discovery=False)

            folder_id = st.secrets["gdrive"]["raiz_folder_id"]

            # Find the most recently modified CSV in the folder
            results = service.files().list(
                q=f"'{folder_id}' in parents and mimeType='text/csv' and trashed=false",
                orderBy="modifiedTime desc",
                pageSize=1,
                fields="files(id, name, modifiedTime)"
            ).execute()

            files = results.get("files", [])
            if not files:
                return None, "⚠️ No CSV files found in the configured Google Drive folder."

            latest = files[0]
            file_id   = latest["id"]
            file_name = latest["name"]
            modified  = latest["modifiedTime"][:10]

            # Download file bytes
            request    = service.files().get_media(fileId=file_id)
            buffer     = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

            buffer.seek(0)
            df = pd.read_csv(buffer)
            return df, f"{file_name}  •  last updated {modified}"

        except Exception as e:
            return None, f"❌ Could not load from Google Drive: {e}"

    # ── Fetch prices (cache key must be hashable → convert dict to tuple) ────
    raiz_prices, raiz_hist = get_raiz_prices(tuple(RAIZ_TICKER_MAP.items()))

    # ── Load data ─────────────────────────────────────────────────────────────
    with st.spinner("Loading latest Raiz CSV from Google Drive..."):
        df_raiz_raw, raiz_file_label = load_raiz_from_gdrive()

    if df_raiz_raw is None:
        # Drive load failed — show error and offer manual fallback
        st.error(raiz_file_label)
        st.markdown("**Manual fallback:** upload your CSV directly.")
        uploaded_raiz = st.file_uploader(
            "Upload Raiz Trade Statement CSV",
            type="csv",
            key="raiz_fallback_upload"
        )
        if uploaded_raiz:
            df_raiz_raw = pd.read_csv(uploaded_raiz)
            raiz_file_label = uploaded_raiz.name
        else:
            st.info(
                "Download your trade statement from the Raiz app and save it to "
                "G:\\My Drive\\Australia Tax\\RAIZ, or upload it manually above."
            )
            st.markdown(
                "**Expected columns:** `Trade Date` · `Transaction Type` · "
                "`Instrument Code` · `Quantity` · `Price` · `Amount`"
            )
            st.stop()

    # ── At this point df_raiz_raw is guaranteed to be a DataFrame ─────────────
    st.caption(f"📂 {raiz_file_label}")

    # ── Parse ─────────────────────────────────────────────────────────────────
    df_raiz = df_raiz_raw.copy()
    df_raiz.columns = [c.strip() for c in df_raiz.columns]
    df_raiz['Trade Date'] = pd.to_datetime(df_raiz['Trade Date'], dayfirst=True)
    df_raiz['Quantity']   = pd.to_numeric(df_raiz['Quantity'], errors='coerce')
    df_raiz['Price']      = pd.to_numeric(df_raiz['Price'],    errors='coerce')
    df_raiz['Amount']     = pd.to_numeric(df_raiz['Amount'],   errors='coerce')
    df_raiz['Trade Date Only'] = df_raiz['Trade Date'].dt.date

    # Sign: SELLs become negative quantity
    df_raiz.loc[df_raiz['Transaction Type'] == 'SELL', 'Quantity'] = \
        -df_raiz['Quantity'].abs()
    df_raiz = df_raiz.sort_values('Trade Date')

    # ── Net positions ─────────────────────────────────────────────────────────
   # ── Net positions ─────────────────────────────────────────────────────────
    def _invested(grp):
        return df_raiz.loc[
            (df_raiz['Instrument Code'] == grp.name) &
            (df_raiz['Transaction Type'] == 'BUY'), 'Amount'
        ].sum()

    def _proceeds(grp):
        return df_raiz.loc[
            (df_raiz['Instrument Code'] == grp.name) &
            (df_raiz['Transaction Type'] == 'SELL'), 'Amount'
        ].sum()

    holdings = (
        df_raiz.groupby('Instrument Code')['Quantity']
        .sum()
        .reset_index()
        .rename(columns={'Quantity': 'Net_Qty'})
    )
    holdings = holdings[holdings['Net_Qty'].abs() > 0.0001].copy()


    holdings['Total_Invested'] = holdings.apply(
        lambda r: df_raiz.loc[
            (df_raiz['Instrument Code'] == r['Instrument Code']) &
            (df_raiz['Transaction Type'] == 'BUY'), 'Amount'
        ].sum(), axis=1
    )
    holdings['Total_Proceeds'] = holdings.apply(
        lambda r: df_raiz.loc[
            (df_raiz['Instrument Code'] == r['Instrument Code']) &
            (df_raiz['Transaction Type'] == 'SELL'), 'Amount'
        ].sum(), axis=1
    )

    # ── Current prices with fallback to last trade price ─────────────────────
    def get_raiz_current_price(code):
        p = raiz_prices.get(code)
        if p and p > 0:
            return p
        last = df_raiz[df_raiz['Instrument Code'] == code]['Price'].dropna()
        return float(last.iloc[-1]) if not last.empty else 0.0
    # ── Add reinvested distribution units ────────────────────────────────────
    # Raiz does not export distributions — inject them using official MA weights
    # Update TOTAL_DISTRIBUTIONS_AUD periodically from the Raiz app History screen
    RAIZ_MA_WEIGHTS = {
        "STW": 0.4360, "RCB": 0.2130, "IAA": 0.1380,
        "IVV": 0.0890, "IEU": 0.0640, "IAF": 0.0300, "AAA": 0.0300,
    }
    TOTAL_DISTRIBUTIONS_AUD = st.number_input(
        "Total Reinvested Distributions (AUD) — update from Raiz app History screen",
        min_value=0,
        value=43000,
        step=500,
        help="Found in Raiz app under History → Reinvested Dividends. This amount is distributed across ETFs using the official Moderately Aggressive portfolio weights."
    )

    def add_distribution_units(row):
        code = row['Instrument Code']
        weight = RAIZ_MA_WEIGHTS.get(code, 0)
        dist_value = TOTAL_DISTRIBUTIONS_AUD * weight
        price = get_raiz_current_price(code)
        extra_units = dist_value / price if price > 0 else 0
        return row['Net_Qty'] + extra_units

    holdings['Net_Qty'] = holdings.apply(add_distribution_units, axis=1)
    holdings['Current_Price']     = holdings['Instrument Code'].map(get_raiz_current_price)
    holdings['Current_Value_AUD'] = holdings['Net_Qty'] * holdings['Current_Price']
    holdings['Unrealised_PL']     = (
        holdings['Current_Value_AUD']
        - holdings['Total_Invested']
        + holdings['Total_Proceeds']
    )
    holdings['ROI_%'] = (
        holdings['Unrealised_PL'] / holdings['Total_Invested'] * 100
    ).where(holdings['Total_Invested'] > 0)
    holdings['ETF Name'] = holdings['Instrument Code'].map(ETF_NAMES)

    # ── 1. SUMMARY METRICS ───────────────────────────────────────────────────
    total_deposited = df_raiz[df_raiz['Transaction Type'] == 'BUY']['Amount'].sum()
    total_proceeds  = df_raiz[df_raiz['Transaction Type'] == 'SELL']['Amount'].sum()
    total_value     = holdings['Current_Value_AUD'].sum()
    total_pl        = total_value - total_deposited + total_proceeds
    total_roi       = total_pl / total_deposited * 100 if total_deposited > 0 else 0

    st.markdown("### Portfolio Summary")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric(
        "Total Deposited (AUD)", f"${total_deposited:,.2f}",
        help="Sum of all BUY transactions since account opened"
    )
    m2.metric(
        "Current Market Value", f"${total_value:,.2f}",
        delta=f"${(total_value - total_deposited + total_proceeds):+,.2f} total P&L"
    )
    m3.metric(
        "Unrealised P&L", f"${total_pl:,.2f}",
        delta=f"{total_roi:+.2f}% ROI",
        delta_color="normal" if total_pl >= 0 else "inverse"
    )
    m4.metric(
        "Realised Proceeds", f"${total_proceeds:,.2f}",
        help="Cash received from all SELL transactions"
    )

    st.divider()

    # ── 2. HOLDINGS TABLE ────────────────────────────────────────────────────
    st.markdown("### Current Holdings")

    # Flag RCB if price looks stale (last Yahoo date > 30 days ago)
    rcb_warning = ""
    rcb_hist = raiz_hist.get("RCB")
    if rcb_hist is not None and not rcb_hist.empty:
        days_stale = (pd.Timestamp.today() - rcb_hist.index[-1]).days
        if days_stale > 30:
            rcb_warning = f" ⚠️ RCB price may be stale ({days_stale}d old — ETF delisted from ASX)"

    if rcb_warning:
        st.warning(rcb_warning)

    display_cols = ['Instrument Code', 'ETF Name', 'Net_Qty', 'Current_Price',
                    'Total_Invested', 'Current_Value_AUD', 'Unrealised_PL', 'ROI_%']

    st.dataframe(
        holdings[display_cols].style
        .map(
            lambda v: (
                'color: #27ae60' if isinstance(v, (int, float)) and v > 0
                else ('color: #e74c3c' if isinstance(v, (int, float)) and v < 0 else '')
            ),
            subset=['Unrealised_PL', 'ROI_%']
        )
        .format({
            'Net_Qty':            '{:.4f}',
            'Current_Price':      '${:.4f}',
            'Total_Invested':     '${:,.2f}',
            'Current_Value_AUD':  '${:,.2f}',
            'Unrealised_PL':      '${:,.2f}',
            'ROI_%':              '{:.2f}%',
        }),
        use_container_width=True,
        hide_index=True
    )

    st.divider()

    # ── 3. ALLOCATION PIE + P&L BAR ─────────────────────────────────────────
    col_l, col_r = st.columns(2)

    with col_l:
        st.markdown("#### Allocation by ETF")
        fig_raiz_pie = px.pie(
            holdings,
            values='Current_Value_AUD',
            names='ETF Name',
            hole=0.4,
            color_discrete_sequence=px.colors.qualitative.Set2
        )
        fig_raiz_pie.update_layout(height=350, margin=dict(t=20, b=20))
        st.plotly_chart(fig_raiz_pie, use_container_width=True)

    with col_r:
        st.markdown("#### Unrealised P&L by ETF")
        fig_raiz_bar = px.bar(
            holdings,
            x='ETF Name',
            y='Unrealised_PL',
            color='Unrealised_PL',
            color_continuous_scale=['#e74c3c', '#95a5a6', '#27ae60'],
            labels={'Unrealised_PL': 'P&L (AUD $)'}
        )
        fig_raiz_bar.add_hline(y=0, line_dash="dash", line_color="grey", opacity=0.5)
        fig_raiz_bar.update_layout(
            height=350,
            margin=dict(t=20, b=20),
            coloraxis_showscale=False
        )
        st.plotly_chart(fig_raiz_bar, use_container_width=True)

    st.divider()

    # ── 4. PORTFOLIO VALUE OVER TIME ─────────────────────────────────────────
    st.markdown("### Portfolio Value Over Time (Oct 2025 → Today)")
    st.caption("Market value of open positions vs cumulative amount deposited.")

    raiz_date_range = pd.date_range("2025-10-01", date.today())
    raiz_timeline   = []

    for d in raiz_date_range:
        current_date = d.date()

        snapshot = (
            df_raiz[df_raiz['Trade Date Only'] <= current_date]
            .groupby('Instrument Code')['Quantity']
            .sum()
        )

        day_value    = 0.0
        day_invested = df_raiz[
            (df_raiz['Trade Date Only'] <= current_date) &
            (df_raiz['Transaction Type'] == 'BUY')
        ]['Amount'].sum()

        for code in RAIZ_TICKER_MAP:
            qty = float(snapshot.get(code, 0))
            if abs(qty) < 0.0001:
                continue

            h = raiz_hist.get(code)
            p = None
            if h is not None and not h.empty:
                try:
                    p = h.asof(d)
                except:
                    p = None
            if p is None or pd.isna(p) or p == 0:
                last_p = df_raiz[df_raiz['Instrument Code'] == code]['Price'].dropna()
                p = float(last_p.iloc[-1]) if not last_p.empty else 0.0

            day_value += qty * float(p)

        raiz_timeline.append({
            'Date':                  d,
            'Portfolio Value (AUD)': day_value,
            'Total Invested (AUD)':  day_invested,
        })

    df_raiz_timeline = pd.DataFrame(raiz_timeline)

    if not df_raiz_timeline.empty:
        fig_raiz_time = go.Figure()
        fig_raiz_time.add_trace(go.Scatter(
            x=df_raiz_timeline['Date'],
            y=df_raiz_timeline['Portfolio Value (AUD)'],
            mode='lines',
            name='Market Value',
            line=dict(color='#27ae60', width=2),
            fill='tozeroy',
            fillcolor='rgba(39,174,96,0.08)'
        ))
        fig_raiz_time.add_trace(go.Scatter(
            x=df_raiz_timeline['Date'],
            y=df_raiz_timeline['Total Invested (AUD)'],
            mode='lines',
            name='Cumulative Invested',
            line=dict(color='#2980b9', width=2, dash='dash')
        ))
        fig_raiz_time.update_layout(
            height=400,
            hovermode="x unified",
            yaxis=dict(title="AUD $", tickprefix="$"),
            legend=dict(orientation="h", y=1.08),
            margin=dict(t=40, b=30)
        )
        st.plotly_chart(fig_raiz_time, use_container_width=True)

    st.divider()

    # ── 5. P&L OVER TIME ─────────────────────────────────────────────────────
    st.markdown("### Unrealised P&L Over Time (Oct 2025 → Today)")

    df_raiz_timeline['P&L (AUD)'] = (
        df_raiz_timeline['Portfolio Value (AUD)']
        - df_raiz_timeline['Total Invested (AUD)']
    )

    fig_raiz_pl = go.Figure()
    fig_raiz_pl.add_trace(go.Scatter(
        x=df_raiz_timeline['Date'],
        y=df_raiz_timeline['P&L (AUD)'],
        mode='lines',
        name='Unrealised P&L',
        line=dict(color='#e67e22', width=2),
        fill='tozeroy',
        fillcolor='rgba(230,126,34,0.08)'
    ))
    fig_raiz_pl.add_hline(y=0, line_dash="dash", line_color="grey", opacity=0.4)
    fig_raiz_pl.update_layout(
        height=320,
        hovermode="x unified",
        yaxis=dict(title="AUD $ gain/loss", tickprefix="$"),
        margin=dict(t=30, b=30)
    )
    st.plotly_chart(fig_raiz_pl, use_container_width=True)

    st.divider()

    # ── 6. TRADE HISTORY ─────────────────────────────────────────────────────
    with st.expander("📋 View Full Trade History"):
        st.dataframe(
            df_raiz[['Trade Date', 'Transaction Type', 'Instrument Code',
                     'Quantity', 'Price', 'Amount']]
            .sort_values('Trade Date', ascending=False)
            .style.format({
                'Trade Date':       lambda x: x.strftime('%Y-%m-%d'),
                'Quantity':         '{:.6f}',
                'Price':            '${:.4f}',
                'Amount':           '${:,.4f}',
            }),
            use_container_width=True,
            hide_index=True
        )


with tab6:
    st.subheader("Data Health Check")
    st.write(f"FX EURAUD Live: {fx_now:.4f}")
    st.table(pd.DataFrame.from_dict(diag_logs, orient='index'))
