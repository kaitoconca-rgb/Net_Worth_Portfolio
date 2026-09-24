"""Investment income ledger: interest, dividends and bond coupons.

Sep 2026: one place for all income the accountant needs, each entry with
gross, tax withheld, net, currency, the payer and its country, and the
Reserve Bank AUD rate on the payment date. Stored in the `dividends` table
(income_type = 'dividend' | 'coupon' | 'interest') so existing entries,
snapshots and the cash link keep working.

Interest is normally already in the bank balance you enter on the Cash page,
so by default recording interest does NOT add it to the balance again.
Dividends and coupons do (as before), unless you untick the box.
"""
from datetime import date

import pandas as pd
import streamlit as st
from sqlalchemy import text as sql_text

TYPE_LABELS = {"dividend": "Dividend", "coupon": "Bond coupon", "interest": "Interest"}
LABEL_TO_TYPE = {v: k for k, v in TYPE_LABELS.items()}
COUNTRIES = ["AU", "IE", "LU", "IT", "ES", "DE", "NL", "FR", "US", "GB", "BR", "CH", "PT", "BE", "AT", "Other"]
CURRENCIES = ["EUR", "AUD", "USD", "GBP", "BRL", "CHF", "NZD"]
BTP_PAYER = "Italian Treasury (BTP)"


def fy_label(d):
    d = pd.Timestamp(d)
    return f"FY{str(d.year + 1 if d.month >= 7 else d.year)[-2:]}"


@st.cache_data(ttl=60, show_spinner=False)
def load_income(_conn, version=0):
    df = _conn.query(
        """
        SELECT id::text AS id, div_date, COALESCE(income_type, 'dividend') AS income_type,
               portfolio, payer, country, security, gross_amount, tax_withheld, amount,
               currency, fx_rate_to_aud, processed, (transaction_id IS NOT NULL) AS linked
        FROM dividends
        ORDER BY div_date DESC, created_at DESC
        """,
        ttl=0,
    )
    for c in ("gross_amount", "tax_withheld", "amount", "fx_rate_to_aud"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    return df


def _v(x):
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(x, str):
        x = x.strip()
        return x or None
    return x


def render_income_section(conn, accounts_df, cash_accounts, aud_rate_on, on_balance_change):
    cash_like = accounts_df[accounts_df["category"].isin(("cash", "savings", "term_deposit", "bonds"))]

    st.markdown("### 💰 Record income")
    st.caption("Interest, dividends and bond coupons, with gross, tax withheld and the payer's country - "
               "what your accountant needs. The Reserve Bank AUD rate for the payment date is stored with it.")

    inc_label = st.radio("Type", list(TYPE_LABELS.values()), horizontal=True, key="inc_type_v2")
    itype = LABEL_TO_TYPE[inc_label]
    open_names = list(cash_accounts.keys())
    all_cash_names = list(cash_like["name"])

    # The "where from" choice sits outside the form so the defaults below follow it.
    src_account = None
    if itype == "interest":
        src_account = st.selectbox(
            "Account that paid it", all_cash_names,
            index=all_cash_names.index("Trade Republic") if "Trade Republic" in all_cash_names else 0,
            help="Closed accounts are listed too, for past years.", key="inc_src_account")
        acc_row = cash_like[cash_like["name"] == src_account].iloc[0]
        source = src_account
        payer_default = acc_row["institution"] or src_account
        country_default = acc_row["country"] or "Other"
        ccy_default = acc_row["currency"]
    elif itype == "coupon":
        source = "BPM"
        payer_default, country_default, ccy_default = BTP_PAYER, "IT", "EUR"
    else:
        source = st.selectbox("Held at", ["N26", "CommSec", "Other"], key="inc_held_at")
        payer_default = "ETFs held with N26" if source == "N26" else ("CommSec shares" if source == "CommSec" else "")
        country_default = {"N26": "IE", "CommSec": "AU"}.get(source, "Other")
        ccy_default = {"N26": "EUR", "CommSec": "AUD"}.get(source, "EUR")

    with st.form(f"income_form_{itype}_{source}", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            inc_date = st.date_input("Payment date", value=date.today())
            security = None
            if itype == "coupon":
                st.text_input("Held at", value="Banco BPM", disabled=True)
                security = st.text_input("BTP name / ISIN", placeholder="e.g. BTP 3.85% 2029 - IT0005...")
            elif itype == "dividend":
                security = st.text_input("Fund / share (optional)", placeholder="e.g. VHYL / IE00B8GKDB10")
        with c2:
            payer = st.text_input("Payer", value=payer_default,
                                  help="Bank, fund or issuer that paid the income.")
            country = st.selectbox("Country of the payer", COUNTRIES,
                                   index=COUNTRIES.index(country_default) if country_default in COUNTRIES else len(COUNTRIES) - 1,
                                   help="Where the income comes from. AU income is pre-filled by the ATO.")
            currency = st.selectbox("Currency", CURRENCIES,
                                    index=CURRENCIES.index(ccy_default) if ccy_default in CURRENCIES else 0)
        with c3:
            gross = st.number_input("Gross amount", min_value=0.0, step=1.0, format="%.2f")
            tax = st.number_input("Tax withheld", min_value=0.0, step=0.01, format="%.2f",
                                  help="e.g. 19% Spanish tax on Trade Republic interest. BTP coupons: 0 "
                                       "(AIRE-registered). Leave 0 if none.")
            add_to_balance = st.checkbox(
                "Also add the net amount to an account balance",
                value=itype != "interest",
                help="Leave unticked if the bank balance you enter on the Cash page already includes it "
                     "(usual for interest), otherwise it's counted twice.")
            dest_default = {"coupon": "BPM Cash", "interest": source if itype == "interest" else None}.get(
                itype, "N26 Cash" if source == "N26" else None)
            dest = st.selectbox("Account credited", open_names,
                                index=open_names.index(dest_default) if dest_default in open_names else 0)
        submitted = st.form_submit_button(f"💾 Record {inc_label.lower()}", type="primary")

    if submitted:
        if gross <= 0:
            st.warning("Enter a gross amount greater than zero.")
            return
        if tax > gross:
            st.warning("Tax withheld can't be more than the gross amount.")
            return
        net = round(gross - tax, 2)
        fx = aud_rate_on(currency, inc_date)
        try:
            with conn.session as s:
                tx_id = None
                if add_to_balance:
                    dest_id, dest_ccy = cash_accounts[dest]
                    dest_fx = aud_rate_on(dest_ccy, inc_date)
                    amt_dest = net if dest_ccy == currency else net * fx / dest_fx
                    tx_id = s.execute(sql_text("""
                        INSERT INTO transactions (account_id, tx_date, tx_type, amount, fx_rate_to_aud, notes, processed)
                        VALUES (CAST(:acc AS uuid), :d, :tt, :amt, :fx, :notes, true)
                        RETURNING id
                    """), {"acc": dest_id, "d": inc_date, "tt": "interest" if itype == "interest" else "deposit",
                           "amt": round(amt_dest, 2), "fx": dest_fx,
                           "notes": f"[{itype}:{source}] {net:.2f} {currency} net from {payer or source}"}).scalar()
                s.execute(sql_text("""
                    INSERT INTO dividends (div_date, portfolio, amount, currency, processed, transaction_id,
                        account_id, income_type, gross_amount, tax_withheld, security, payer, country, fx_rate_to_aud)
                    VALUES (:d, :src, :net, :ccy, :processed, :tx, :acc, :t, :g, :tax, :sec, :payer, :country, :fx)
                """), {"d": inc_date, "src": source, "net": net, "ccy": currency,
                       # interest is already estimated in snapshots from account rates
                       "processed": itype == "interest", "tx": tx_id,
                       "acc": (cash_accounts[dest][0] if add_to_balance
                               else (cash_like.loc[cash_like["name"] == src_account, "id"].iloc[0]
                                     if src_account else None)),
                       "t": itype, "g": gross, "tax": tax, "sec": _v(security), "payer": _v(payer),
                       "country": country, "fx": fx})
                s.commit()
            load_income.clear()
            if add_to_balance:
                on_balance_change()
            st.success(f"✅ Recorded {inc_label.lower()}: {net:,.2f} {currency} net "
                       f"(A${net * fx:,.2f} at the RBA rate of {fx:.4f}).")
            st.rerun()
        except Exception:
            import traceback
            st.error(f"Could not record it: {traceback.format_exc()}")

    # ── Entries ──────────────────────────────────────────────────────────────
    st.divider()
    st.markdown("### 📋 Income entered")
    st.caption("Correct the type, payer, country, security, gross or tax here. The net amount and any "
               "cash entry aren't changed.")
    df = load_income(conn)
    if df.empty:
        st.info("Nothing recorded yet.")
        return

    view = pd.DataFrame({
        "id": df["id"],
        "Date": pd.to_datetime(df["div_date"]).dt.date,
        "Type": df["income_type"].map(TYPE_LABELS).fillna("Dividend"),
        "Source": df["portfolio"],
        "Payer": df["payer"],
        "Country": df["country"],
        "Security": df["security"],
        "Gross": df["gross_amount"],
        "Tax withheld": df["tax_withheld"],
        "Net": df["amount"],
        "Currency": df["currency"],
        "AUD rate": df["fx_rate_to_aud"],
    })
    f1, f2 = st.columns(2)
    fy_opts = ["All"] + sorted({fy_label(d) for d in view["Date"]}, reverse=True)
    fy_filter = f1.selectbox("Financial year", fy_opts, key="inc_fy_filter")
    type_filter = f2.multiselect("Types", list(TYPE_LABELS.values()), default=list(TYPE_LABELS.values()),
                                 key="inc_type_filter")
    shown = view[view["Type"].isin(type_filter)]
    if fy_filter != "All":
        shown = shown[shown["Date"].map(fy_label) == fy_filter]

    edited = st.data_editor(
        shown, key="income_editor", hide_index=True, use_container_width=True,
        disabled=["Date", "Source", "Net", "Currency", "AUD rate"],
        column_config={
            "id": None,
            "Type": st.column_config.SelectboxColumn("Type", options=list(TYPE_LABELS.values()), required=True),
            "Country": st.column_config.SelectboxColumn("Country", options=COUNTRIES),
            "Gross": st.column_config.NumberColumn("Gross", format="%.2f", min_value=0.0),
            "Tax withheld": st.column_config.NumberColumn("Tax withheld", format="%.2f", min_value=0.0),
            "Net": st.column_config.NumberColumn("Net", format="%.2f"),
            "AUD rate": st.column_config.NumberColumn("AUD rate", format="%.4f",
                                                      help="Reserve Bank rate on the payment date"),
        },
    )
    if st.button("💾 Save corrections", key="save_income_corrections"):
        cols = ["Type", "Payer", "Country", "Security", "Gross", "Tax withheld"]
        before = shown.set_index("id")
        changed = 0
        with conn.session as s:
            for _, row in edited.iterrows():
                b = before.loc[row["id"]]
                if all(_v(b[c]) == _v(row[c]) for c in cols):
                    continue
                t = LABEL_TO_TYPE.get(row["Type"], "dividend")
                s.execute(sql_text("""
                    UPDATE dividends SET income_type = :t, payer = :payer, country = :country,
                        security = :sec, gross_amount = :g, tax_withheld = :tax,
                        portfolio = CASE WHEN :t = 'coupon' THEN 'BPM' ELSE portfolio END
                    WHERE id = CAST(:id AS uuid)
                """), {"t": t, "payer": _v(row["Payer"]), "country": _v(row["Country"]), "sec": _v(row["Security"]),
                       "g": _v(row["Gross"]), "tax": _v(row["Tax withheld"]), "id": row["id"]})
                changed += 1
            s.commit()
        load_income.clear()
        st.success(f"✅ Saved {changed} correction(s).")
        st.rerun()

    # ── Summary by financial year ────────────────────────────────────────────
    st.divider()
    st.markdown("### 🧾 For your accountant - by Australian financial year")
    d = view.copy()
    d["FY"] = d["Date"].map(fy_label)
    fy_list = sorted(d["FY"].unique(), reverse=True)
    sel = st.selectbox("Financial year", fy_list, key="inc_fy_summary")
    f = d[d["FY"] == sel].copy()
    f["Gross (or net)"] = f["Gross"].fillna(f["Net"])
    f["Tax withheld"] = f["Tax withheld"].fillna(0.0)
    rate = f["AUD rate"].fillna(1.0)
    f["Gross AUD"] = f["Gross (or net)"] * rate
    f["Tax AUD"] = f["Tax withheld"] * rate
    f["Net AUD"] = f["Net"] * rate
    f["Country"] = f["Country"].fillna("?")
    summary = (f.groupby(["Type", "Country", "Currency"], dropna=False)
                 .agg(Payments=("Net", "size"), Gross=("Gross (or net)", "sum"), Tax=("Tax withheld", "sum"),
                      Net=("Net", "sum"), Gross_AUD=("Gross AUD", "sum"), Tax_AUD=("Tax AUD", "sum"))
                 .reset_index()
                 .rename(columns={"Tax": "Tax withheld", "Gross_AUD": "Gross A$", "Tax_AUD": "Tax withheld A$"}))
    st.dataframe(summary.style.format({"Gross": "{:,.2f}", "Tax withheld": "{:,.2f}", "Net": "{:,.2f}",
                                       "Gross A$": "{:,.2f}", "Tax withheld A$": "{:,.2f}"}),
                 use_container_width=True, hide_index=True)
    foreign = f[f["Country"] != "AU"]
    m1, m2, m3 = st.columns(3)
    m1.metric("Foreign income (gross, A$)", f"{foreign['Gross AUD'].sum():,.2f}")
    m2.metric("Foreign tax withheld (A$)", f"{foreign['Tax AUD'].sum():,.2f}")
    m3.metric("Australian income (A$)", f"{f.loc[f['Country'] == 'AU', 'Gross AUD'].sum():,.2f}",
              help="Pre-filled by the ATO - shown for completeness.")
    notes = []
    if f["Gross"].isna().any():
        notes.append("some older entries have no gross/tax - their net is shown as gross")
    if f["AUD rate"].isna().any():
        notes.append("some entries have no AUD rate yet - shown at 1.0")
    if notes:
        st.caption("⚠️ " + "; ".join(notes) + ".")
    st.caption("Interest is counted when it's paid into the account.")
    st.download_button(
        f"⬇️ Download {sel} detail (CSV)",
        f[["Date", "Type", "Source", "Payer", "Country", "Security", "Gross (or net)", "Tax withheld", "Net",
           "Currency", "AUD rate", "Gross AUD", "Tax AUD", "Net AUD"]]
          .assign(Date=lambda x: pd.to_datetime(x["Date"]).dt.strftime("%Y-%m-%d"))
          .to_csv(index=False).encode("utf-8"),
        file_name=f"investment_income_{sel}.csv", mime="text/csv", key="inc_fy_csv",
    )


def ensure_income_ledger_schema(conn):
    with conn.session as s:
        s.execute(sql_text("SET LOCAL lock_timeout = '5s'"))
        s.execute(sql_text("ALTER TABLE dividends ADD COLUMN IF NOT EXISTS payer text"))
        s.execute(sql_text("ALTER TABLE dividends ADD COLUMN IF NOT EXISTS country text"))
        s.commit()
