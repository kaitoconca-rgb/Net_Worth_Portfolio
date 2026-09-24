"""Account registry for Claudio's Executive Console.

Sep 2026: bank / savings accounts used to be hard-coded in app.py (a dict of
names -> UUIDs), so opening Raisin or dropping bunq meant editing code. They
now live in the `accounts` table and are managed from the Cash page:
add, edit, close / reopen, and record transfers between your own accounts.

Closing an account never deletes it: it gets a closed_on date and drops off
the dashboard, but its transactions and income stay for past-year reports.

Platform accounts that have their own data feed (N26 ETFs, Raiz, Vanguard,
CommSec, Revolut Metals, Mercer Super) are found by `role` rather than a
fixed UUID, so they can be renamed freely.
"""
import uuid
from datetime import date

import pandas as pd
import streamlit as st
from sqlalchemy import text as sql_text

# Categories whose balance counts as "cash & savings" on the dashboard.
CASH_CATEGORIES = ("cash", "savings", "term_deposit", "bonds")
CATEGORY_LABELS = {
    "cash": "Everyday / cash",
    "savings": "Savings (instant access)",
    "term_deposit": "Term deposit",
    "bonds": "Bonds",
    "investment": "Investments",
    "super": "Super",
    "commodities": "Commodities",
}
COUNTRY_FLAGS = {
    "AU": "🇦🇺", "DE": "🇩🇪", "IT": "🇮🇹", "ES": "🇪🇸", "NL": "🇳🇱", "BR": "🇧🇷",
    "FR": "🇫🇷", "IE": "🇮🇪", "GB": "🇬🇧", "US": "🇺🇸", "LU": "🇱🇺", "PT": "🇵🇹",
    "CH": "🇨🇭", "BE": "🇧🇪", "AT": "🇦🇹", "LT": "🇱🇹", "EE": "🇪🇪",
}
CURRENCIES = ["AUD", "EUR", "USD", "GBP", "BRL", "CHF", "NZD"]

# Fallback UUIDs for the platform accounts, used only if a role hasn't been
# assigned in the database yet (keeps the app working mid-migration).
ROLE_FALLBACK_IDS = {
    "n26_etf": "818cca44-648f-469b-ac01-7366dfda9cc8",
    "raiz": "ec7a3f4e-adbb-4d9b-a24e-1b179d29e916",
    "vanguard": "8c4ee8bf-29b5-4533-99c7-84850e656e07",
    "commsec": "d11dbbea-8a63-42da-9329-ab85ec00bea8",
    "metals": "d2e04bcf-04fc-4151-bcb5-3ff64ccf1f97",
    "super": "79b626ee-4563-48c9-975d-ecefc6221fe7",
}

SCHEMA_STMTS = (
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS institution text",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS country text",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS category text",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS role text",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS opened_on date",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS closed_on date",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS interest_rate numeric",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS term_start date",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS maturity_date date",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS principal numeric",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS hide_when_zero boolean NOT NULL DEFAULT false",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS notes text",
    "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS sort_order integer",
)


def ensure_accounts_schema(conn):
    """Idempotent. Adds the registry columns if they're missing."""
    with conn.session as s:
        s.execute(sql_text("SET LOCAL lock_timeout = '5s'"))
        for stmt in SCHEMA_STMTS:
            s.execute(sql_text(stmt))
        s.commit()


@st.cache_data(ttl=300, show_spinner=False)
def load_accounts(_conn):
    df = _conn.query(
        """
        SELECT id::text AS id, name, account_type, currency, is_active,
               institution, country, category, role, opened_on, closed_on,
               interest_rate, term_start, maturity_date, principal,
               COALESCE(hide_when_zero, false) AS hide_when_zero, notes, sort_order
        FROM accounts
        ORDER BY COALESCE(sort_order, 999), name
        """,
        ttl=0,
    )
    if df.empty:
        return df
    # Rows created before the registry existed have no category yet.
    df["category"] = df["category"].fillna(
        df["account_type"].map({"cash": "cash", "investment": "investment", "super": "super"})
    ).fillna("cash")
    df["currency"] = df["currency"].fillna("AUD").str.upper()
    for c in ("interest_rate", "principal"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    df["is_open"] = df["closed_on"].isna() & df["is_active"].fillna(True)
    return df


def clear_account_caches():
    load_accounts.clear()


def cash_accounts(df, include_closed=False):
    """{name: (id, currency)} for bank/savings/term-deposit/bond accounts."""
    if df is None or df.empty:
        return {}
    sel = df[df["category"].isin(CASH_CATEGORIES)]
    if not include_closed:
        sel = sel[sel["is_open"]]
    return {r["name"]: (r["id"], r["currency"]) for _, r in sel.iterrows()}


def role_id(df, role):
    if df is not None and not df.empty:
        hit = df[df["role"] == role]
        if not hit.empty:
            return hit.iloc[0]["id"]
    return ROLE_FALLBACK_IDS.get(role)


def flag_for(row):
    return COUNTRY_FLAGS.get(str(row.get("country") or "").upper(), "🏦")


def term_deposit_status(row, today=None):
    """Expected interest and days left for a term deposit row (simple interest)."""
    today = today or date.today()
    principal = float(row.get("principal") or 0)
    rate = float(row.get("interest_rate") or 0)
    start, mat = row.get("term_start"), row.get("maturity_date")
    if not principal or not rate or pd.isna(start) or pd.isna(mat):
        return None
    start, mat = pd.to_datetime(start).date(), pd.to_datetime(mat).date()
    term_days = max((mat - start).days, 0)
    elapsed = min(max((today - start).days, 0), term_days)
    return {
        "expected_interest": principal * rate / 100 * term_days / 365,
        "accrued_interest": principal * rate / 100 * elapsed / 365,
        "days_left": (mat - today).days,
        "maturity_date": mat,
    }


# ─────────────────────────────── UI ──────────────────────────────────────────

def _none_if_blank(v):
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, str) and not v.strip():
        return None
    return v


def render_accounts_manager(conn, balances, on_change):
    """Edit / add / close accounts and record transfers.

    balances: {account name: current balance in its own currency}
    on_change: callback that clears the app's balance caches after a write.
    """
    df = load_accounts(conn)
    cash_df = df[df["category"].isin(CASH_CATEGORIES)].copy()

    st.markdown("### ⚙️ Manage accounts")
    st.caption(
        "Add a new bank, change interest rates, or close an account. Closing keeps its "
        "history (for past tax years) and just hides it from the dashboard."
    )

    # ── Edit existing ────────────────────────────────────────────────────────
    edit_cols = ["name", "institution", "country", "currency", "category", "interest_rate",
                 "opened_on", "closed_on", "term_start", "maturity_date", "principal",
                 "hide_when_zero", "notes"]
    view = cash_df[["id"] + edit_cols].copy()
    for c in ("opened_on", "closed_on", "term_start", "maturity_date"):
        view[c] = pd.to_datetime(view[c]).dt.date
    view.insert(1, "balance", [balances.get(n, 0.0) for n in view["name"]])
    edited = st.data_editor(
        view,
        key="acct_editor",
        hide_index=True,
        use_container_width=True,
        disabled=["id", "balance"],
        column_config={
            "id": None,
            "name": st.column_config.TextColumn("Account", required=True),
            "balance": st.column_config.NumberColumn("Balance", format="%.2f"),
            "institution": st.column_config.TextColumn("Institution"),
            "country": st.column_config.SelectboxColumn("Country", options=sorted(COUNTRY_FLAGS)),
            "currency": st.column_config.SelectboxColumn("Ccy", options=CURRENCIES, required=True),
            "category": st.column_config.SelectboxColumn(
                "Type", options=list(CASH_CATEGORIES), required=True,
                help="Term deposits show expected interest and a maturity reminder."),
            "interest_rate": st.column_config.NumberColumn("Rate % p.a.", format="%.2f", min_value=0.0),
            "opened_on": st.column_config.DateColumn("Opened"),
            "closed_on": st.column_config.DateColumn("Closed", help="Set a date to close the account."),
            "term_start": st.column_config.DateColumn("Term start"),
            "maturity_date": st.column_config.DateColumn("Matures"),
            "principal": st.column_config.NumberColumn("Deposit", format="%.2f"),
            "hide_when_zero": st.column_config.CheckboxColumn("Hide if 0"),
            "notes": st.column_config.TextColumn("Notes"),
        },
    )
    if st.button("💾 Save account changes", key="acct_save_btn"):
        changed = 0
        warnings = []
        orig = view.set_index("id")
        with conn.session as s:
            for _, row in edited.iterrows():
                o = orig.loc[row["id"]]
                if all(
                    (_none_if_blank(row[c]) == _none_if_blank(o[c])) for c in edit_cols
                ):
                    continue
                closed_on = _none_if_blank(row["closed_on"])
                if closed_on and abs(float(balances.get(o["name"], 0.0))) >= 0.01:
                    warnings.append(
                        f"{row['name']} was closed with a balance of {balances.get(o['name'], 0.0):,.2f} "
                        f"{row['currency']}. It no longer counts in your net worth - record a transfer "
                        "out first if that money moved elsewhere."
                    )
                params = {c: _none_if_blank(row[c]) for c in edit_cols}
                params["is_active"] = closed_on is None
                params["id"] = row["id"]
                s.execute(sql_text("""
                    UPDATE accounts SET name=:name, institution=:institution, country=:country,
                        currency=:currency, category=:category, interest_rate=:interest_rate,
                        opened_on=:opened_on, closed_on=:closed_on, term_start=:term_start,
                        maturity_date=:maturity_date, principal=:principal,
                        hide_when_zero=COALESCE(:hide_when_zero, false), notes=:notes,
                        is_active=:is_active
                    WHERE id = CAST(:id AS uuid)
                """), params)
                changed += 1
            s.commit()
        clear_account_caches()
        on_change()
        for w in warnings:
            st.warning(w)
        st.success(f"Saved {changed} account(s).") if changed else st.info("No changes to save.")
        if changed:
            st.rerun()

    col_add, col_tr = st.columns(2)

    # ── Add account ──────────────────────────────────────────────────────────
    with col_add:
        with st.expander("➕ Add an account"):
            with st.form("acct_add_form", clear_on_submit=True):
                n_name = st.text_input("Account name", placeholder="e.g. Raisin - Cuenta Bienvenida")
                c1, c2 = st.columns(2)
                n_inst = c1.text_input("Institution")
                n_country = c2.selectbox("Country", sorted(COUNTRY_FLAGS), index=sorted(COUNTRY_FLAGS).index("AU"))
                c3, c4 = st.columns(2)
                n_ccy = c3.selectbox("Currency", CURRENCIES)
                n_cat = c4.selectbox("Type", list(CASH_CATEGORIES), format_func=lambda k: CATEGORY_LABELS[k])
                c5, c6 = st.columns(2)
                n_open = c5.date_input("Opened on", value=date.today())
                n_rate = c6.number_input("Interest rate % p.a.", min_value=0.0, step=0.05, format="%.2f")
                n_bal = st.number_input(
                    "Opening balance", min_value=0.0, step=100.0, format="%.2f",
                    help="Money already in the account. If it came from another account you track, "
                         "leave this at 0 and use 'Record a transfer' instead, so it isn't counted twice.")
                st.caption("Term deposits only:")
                c7, c8 = st.columns(2)
                n_mat = c7.date_input("Matures on", value=None)
                n_principal = c8.number_input("Deposit amount", min_value=0.0, step=1000.0, format="%.2f")
                if st.form_submit_button("Add account", type="primary"):
                    if not n_name.strip():
                        st.warning("Give the account a name.")
                    elif n_name.strip() in set(df["name"]):
                        st.warning("There's already an account with that name.")
                    else:
                        with conn.session as s:
                            new_id = s.execute(sql_text("""
                                INSERT INTO accounts (name, account_type, currency, platform, is_active,
                                    institution, country, category, opened_on, interest_rate,
                                    term_start, maturity_date, principal)
                                VALUES (:name, 'cash', :ccy, 'Cash', true, :inst, :country, :cat, :opened,
                                    :rate, :term_start, :mat, :principal)
                                RETURNING id::text
                            """), {
                                "name": n_name.strip(), "ccy": n_ccy, "inst": n_inst.strip() or None,
                                "country": n_country, "cat": n_cat, "opened": n_open,
                                "rate": n_rate or None,
                                "term_start": n_open if n_cat == "term_deposit" else None,
                                "mat": n_mat if n_cat == "term_deposit" else None,
                                "principal": (n_principal or None) if n_cat == "term_deposit" else None,
                            }).scalar()
                            if n_bal > 0:
                                s.execute(sql_text("""
                                    INSERT INTO transactions (account_id, tx_date, tx_type, amount, notes, processed)
                                    VALUES (CAST(:acc AS uuid), :d, 'deposit', :amt, :notes, true)
                                """), {"acc": new_id, "d": n_open, "amt": n_bal,
                                       "notes": "[opening_balance] balance when the account was added"})
                            s.commit()
                        clear_account_caches()
                        on_change()
                        st.success(f"Added {n_name.strip()}.")
                        st.rerun()

    # ── Transfer between own accounts ───────────────────────────────────────
    with col_tr:
        with st.expander("🔁 Record a transfer between your accounts"):
            names = list(cash_accounts(df).keys())
            if len(names) < 2:
                st.info("Add at least two accounts first.")
            else:
                with st.form("acct_transfer_form", clear_on_submit=True):
                    t_from = st.selectbox("From", names)
                    t_to = st.selectbox("To", names, index=1)
                    t_date = st.date_input("Date", value=date.today())
                    t_amt = st.number_input("Amount sent (in the 'From' account's currency)",
                                            min_value=0.0, step=100.0, format="%.2f")
                    t_recv = st.number_input(
                        "Amount received (only if the currencies differ)", min_value=0.0,
                        step=100.0, format="%.2f")
                    st.caption("Transfers move money between your own accounts: they're not new "
                               "savings and not investment gains, so they don't change your net worth.")
                    if st.form_submit_button("Record transfer", type="primary"):
                        accs = cash_accounts(df)
                        (from_id, from_ccy), (to_id, to_ccy) = accs[t_from], accs[t_to]
                        if t_from == t_to:
                            st.warning("Pick two different accounts.")
                        elif t_amt <= 0:
                            st.warning("Enter the amount sent.")
                        elif from_ccy != to_ccy and t_recv <= 0:
                            st.warning(f"{t_from} is in {from_ccy} and {t_to} in {to_ccy}: "
                                       "enter the amount received too.")
                        else:
                            received = t_amt if from_ccy == to_ccy else t_recv
                            group = str(uuid.uuid4())
                            with conn.session as s:
                                for acc, amt, kind, other in (
                                    (from_id, -t_amt, "transfer_out", t_to),
                                    (to_id, received, "transfer_in", t_from),
                                ):
                                    s.execute(sql_text("""
                                        INSERT INTO transactions (account_id, tx_date, tx_type, amount,
                                            transfer_group, notes, processed)
                                        VALUES (CAST(:acc AS uuid), :d, :kind, :amt, CAST(:g AS uuid), :notes, true)
                                    """), {"acc": acc, "d": t_date, "kind": kind, "amt": amt, "g": group,
                                           "notes": f"[transfer] {'to' if amt < 0 else 'from'} {other}"})
                                s.commit()
                            on_change()
                            st.success(f"Recorded {t_amt:,.2f} {from_ccy} from {t_from} to {t_to}.")
                            st.rerun()
