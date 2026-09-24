"""Benalmadena rental property, read live from Zarpia.

Sep 2026. Zarpia's database exposes a few read-only views (schema nw_feed)
for this property only, through a dedicated login (nw_feed_reader) that
cannot see anything else. This module reads them and builds the Australian
financial-year summary your accountant asks for: weeks available / rented,
rent, expenses in the accountant's categories, capital items, and the
Spanish non-resident tax on the rent.

Two ways to get the data:
  * live: a connection string in Streamlit secrets (ZARPIA_FEED_CONN_STRING);
  * snapshot (what's used today): a copy of the same views stored in this
    app's own database, table public.zarpia_snapshot (one JSON row per copy;
    the newest row is used). Claude refreshes it on request.
"""
import json
from datetime import date

import pandas as pd
import streamlit as st

SPANISH_NR_RATE = 0.24   # IRNR rate for non-EU residents (Modelo 210), on gross rent

# Accountant's categories (the Australian rental-schedule wording)
CATS = ["Agent fees", "Strata levy", "Strata special levy", "Council rates", "Water rates", "Insurance",
        "Repairs & maintenance", "Bank fees", "Loan interest", "Other"]

LINEN_WORDS = ("sabana", "sábana", "toalla", "linen", "towel")


def accountant_category(row):
    cat = str(row.get("category") or "").lower()
    concept = str(row.get("concept") or "").lower()
    supplier = str(row.get("supplier") or "").lower()
    if cat in ("agency_fees", "iva_igic"):
        return "Agent fees"
    if cat == "other" and ("icnea" in supplier or "sun property" in supplier):
        return "Agent fees"          # credits / refunds on the agent's statement
    if cat == "community":
        return "Strata levy"
    if cat == "maintenance" and "derrama" in concept:
        return "Strata special levy"
    if cat == "maintenance" and any(w in concept for w in LINEN_WORDS):
        return "Other"               # low-cost guest consumables
    if cat in ("maintenance", "repairs"):
        return "Repairs & maintenance"
    if cat.startswith("municipality"):
        return "Council rates"
    if cat == "water":
        return "Water rates"
    if cat == "insurance":
        return "Insurance"
    if cat in ("bank_fees", "bank"):
        return "Bank fees"
    if cat in ("mortgage_interest", "loan_interest", "interest"):
        return "Loan interest"
    return "Other"


def fy_bounds(fy_year):
    """FY26 -> 1 Jul 2025 .. 30 Jun 2026 (fy_year = 2026)."""
    return date(fy_year - 1, 7, 1), date(fy_year, 6, 30)


def feed_configured():
    try:
        return bool(st.secrets.get("ZARPIA_FEED_CONN_STRING"))
    except Exception:
        return False


def _conn():
    return st.connection("zarpia_feed", type="sql", url=st.secrets["ZARPIA_FEED_CONN_STRING"],
                         pool_pre_ping=True)


FEED_COLS = {
    "property": ["id", "name", "country", "municipality", "acquisition_cost"],
    "units": ["id", "name", "unit_type", "cadastral_value_total", "imputation_rate_pct",
              "income_allocation_pct", "disposal_date"],
    "bookings": ["booking_ref", "arrival", "departure", "nights", "included_in_tax_calc", "income_total",
                 "agent_statement", "statement_date"],
    "accruals": ["booking_ref", "accrual_basis", "tax_year", "nights_in_year", "accrued_amount", "is_estimate"],
    "expenses": ["id", "expense_date", "category", "treatment", "supplier", "concept", "amount", "currency",
                 "unit", "included_in_tax_calc", "pdf_filename", "source"],
    "statements": ["statement", "settlement_date", "gross_amount", "owner_amount", "net_amount", "currency"],
}
SORT = {"bookings": "arrival", "expenses": "expense_date", "statements": "settlement_date"}


def load_snapshot(_pg):
    """Newest copy from public.zarpia_snapshot, or None if there isn't one."""
    try:
        df = _pg.query("SELECT copied_at, data::text AS data FROM public.zarpia_snapshot "
                       "ORDER BY id DESC LIMIT 1", ttl=0)
    except Exception:
        return None
    if df.empty:
        return None
    data = json.loads(df.iloc[0]["data"])
    feed = {k: pd.DataFrame(data.get(k) or [], columns=cols) for k, cols in FEED_COLS.items()}
    for k, col in SORT.items():
        feed[k] = feed[k].sort_values(col, kind="stable").reset_index(drop=True)
    feed["copied_at"] = pd.to_datetime(df.iloc[0]["copied_at"])
    return _typed(feed)


@st.cache_data(ttl=3600, show_spinner="Reading Benalmadena from Zarpia…")
def load_feed():
    c = _conn()
    q = lambda sql: c.query(sql, ttl=0)
    feed = {
        "property": q("SELECT * FROM nw_feed.property"),
        "units": q("SELECT * FROM nw_feed.units"),
        "bookings": q("SELECT * FROM nw_feed.bookings ORDER BY arrival"),
        "accruals": q("SELECT * FROM nw_feed.booking_accruals"),
        "expenses": q("SELECT * FROM nw_feed.expenses ORDER BY expense_date"),
        "statements": q("SELECT * FROM nw_feed.agent_statements ORDER BY settlement_date"),
    }
    return _typed(feed)


def _typed(feed):
    for k in ("bookings", "expenses", "statements"):
        for col in feed[k].columns:
            if col in ("arrival", "departure", "expense_date", "settlement_date", "statement_date"):
                feed[k][col] = pd.to_datetime(feed[k][col]).dt.date
    for col in ("income_total",):
        feed["bookings"][col] = pd.to_numeric(feed["bookings"][col], errors="coerce").astype(float)
    feed["expenses"]["amount"] = pd.to_numeric(feed["expenses"]["amount"], errors="coerce").astype(float)
    feed["accruals"]["accrued_amount"] = pd.to_numeric(feed["accruals"]["accrued_amount"], errors="coerce").astype(float)
    for col in ("tax_year", "nights_in_year"):
        feed["accruals"][col] = pd.to_numeric(feed["accruals"][col], errors="coerce")
    feed["bookings"]["nights"] = pd.to_numeric(feed["bookings"]["nights"], errors="coerce")
    return feed


def _nights_in(bk, start, end):
    """Nights of each booking falling in [start, end] (a night = the date it starts)."""
    a = pd.to_datetime(bk["arrival"]).clip(lower=pd.Timestamp(start))
    d = pd.to_datetime(bk["departure"]).clip(upper=pd.Timestamp(end) + pd.Timedelta(days=1))
    return (d - a).dt.days.clip(lower=0)


def property_fy_summary(feed, fy_year):
    start, end = fy_bounds(fy_year)
    bk = feed["bookings"].copy()
    bk["rented"] = bk["income_total"].fillna(0) > 0
    bk["nights_fy"] = _nights_in(bk, start, end)
    fy_days = (end - start).days + 1
    owner_nights = int(bk.loc[~bk["rented"], "nights_fy"].sum())

    acc = feed["accruals"]
    acc_fy = acc[(acc["accrual_basis"] == "au_fy") & (acc["tax_year"] == fy_year)]
    rent = float(acc_fy["accrued_amount"].sum())
    rented_nights = int(pd.to_numeric(acc_fy["nights_in_year"], errors="coerce").fillna(0).sum())

    # Rent by calendar half (Spain taxes by calendar year): prorate each booking per night.
    per_night = bk["income_total"].fillna(0) / bk["nights"].replace(0, pd.NA)
    h2_start, h2_end = start, date(fy_year - 1, 12, 31)
    h1_start, h1_end = date(fy_year, 1, 1), end
    rent_h2 = float((per_night.fillna(0) * _nights_in(bk, h2_start, h2_end)).sum())
    rent_h1 = float((per_night.fillna(0) * _nights_in(bk, h1_start, h1_end)).sum())

    ex = feed["expenses"]
    ex = ex[(pd.to_datetime(ex["expense_date"]) >= pd.Timestamp(start))
            & (pd.to_datetime(ex["expense_date"]) <= pd.Timestamp(end))].copy()
    ex["Accountant category"] = ex.apply(accountant_category, axis=1)
    included = ex["included_in_tax_calc"].fillna(True).astype(bool)
    capital = ex["treatment"].fillna("").str.startswith("capital")
    deductible = ex[included & ~capital]
    by_cat = deductible.groupby("Accountant category")["amount"].sum().reindex(CATS).fillna(0.0)

    return {
        "fy": f"FY{str(fy_year)[-2:]}", "start": start, "end": end,
        "weeks_available": round((fy_days - owner_nights) / 7, 1),
        "weeks_rented": round(rented_nights / 7, 1),
        "owner_nights": owner_nights, "rented_nights": rented_nights,
        "rent": rent, "rent_h2": rent_h2, "rent_h1": rent_h1,
        "expenses_by_cat": by_cat, "expenses_total": float(by_cat.sum()),
        "deductible_detail": deductible, "capital_items": ex[included & capital],
        "excluded": ex[~included],
        "spanish_tax_h2": round(rent_h2 * SPANISH_NR_RATE, 2),
        "spanish_tax_h1": round(rent_h1 * SPANISH_NR_RATE, 2),
        "bookings": bk[bk["nights_fy"] > 0],
    }


SETUP_HELP = """
**Optional - live link instead of the copy:**

1. In the **Zarpia** Supabase project, open the SQL editor and run (choose your own password):
   ```sql
   ALTER ROLE nw_feed_reader WITH LOGIN PASSWORD 'choose-a-long-password';
   ```
2. In Supabase → **Connect** → *Session pooler*, copy the connection string. Replace the user
   `postgres.hprkxucgpfxztiugrokr` with **`nw_feed_reader.hprkxucgpfxztiugrokr`** and put your password in.
3. In Streamlit → your app → **Settings → Secrets**, add:
   ```toml
   ZARPIA_FEED_CONN_STRING = "postgresql://nw_feed_reader.hprkxucgpfxztiugrokr:PASSWORD@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"
   ```
This login can only read the Benalmadena summary views - nothing else in Zarpia.
To switch it off later: `ALTER ROLE nw_feed_reader NOLOGIN;`
"""


def render_property_page(aud_avg_for_fy, pg=None):
    st.header("🏠 Benalmadena (from Zarpia)")
    if feed_configured():
        try:
            feed = load_feed()
        except Exception as e:
            st.error(f"Couldn't read from Zarpia: {e}")
            st.markdown(SETUP_HELP)
            return
        if st.button("🔄 Refresh from Zarpia", key="zf_refresh"):
            load_feed.clear()
            st.rerun()
    else:
        feed = load_snapshot(pg) if pg is not None else None
        if feed is None:
            st.info("No Benalmadena data yet - ask Claude to copy it from Zarpia.")
            return
        st.caption(f"Copied from Zarpia on {feed['copied_at']:%d %b %Y %H:%M} UTC. "
                   "Ask Claude to refresh it when new bookings or expenses are in Zarpia "
                   "(e.g. before the accountant pack).")

    years = sorted({int(y) for y in feed["accruals"].loc[feed["accruals"]["accrual_basis"] == "au_fy", "tax_year"]},
                   reverse=True)
    today = date.today()
    default_fy = today.year if today.month < 7 else today.year + 1
    past = [y for y in years if y < default_fy] or years
    fy = st.selectbox("Australian financial year", years, index=years.index(past[0]) if past else 0,
                      format_func=lambda y: f"FY{str(y)[-2:]} (1 Jul {y - 1} - 30 Jun {y})", key="zf_fy")
    s = property_fy_summary(feed, fy)
    rate = aud_avg_for_fy(fy)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Weeks available", f"{s['weeks_available']}", help=f"{s['owner_nights']} owner-use nights excluded")
    c2.metric("Weeks rented", f"{s['weeks_rented']}", help=f"{s['rented_nights']} booked nights ÷ 7")
    c3.metric("Rent (gross)", f"€{s['rent']:,.2f}", f"A${s['rent'] * rate:,.0f}" if rate else None, delta_color="off")
    c4.metric("Deductible expenses", f"€{s['expenses_total']:,.2f}",
              f"A${s['expenses_total'] * rate:,.0f}" if rate else None, delta_color="off")
    if rate:
        st.caption(f"A$ at the {s['fy']} average Reserve Bank rate ({rate:.4f} AUD per EUR).")

    st.markdown("#### Expenses by accountant category")
    t = s["expenses_by_cat"].reset_index()
    t.columns = ["Category", "EUR"]
    if rate:
        t["A$"] = t["EUR"] * rate
    st.dataframe(t.style.format({"EUR": "{:,.2f}", "A$": "{:,.2f}"}), use_container_width=True, hide_index=True)
    if s["expenses_by_cat"].get("Strata special levy", 0):
        st.caption("Special levies (derrama): purpose to confirm - capital if they fund improvements.")

    st.markdown("#### Spanish non-resident tax on the rent (24%)")
    st.dataframe(pd.DataFrame([
        {"Period": f"Jul-Dec {fy - 1}", "Rent (EUR)": s["rent_h2"], "Tax (EUR)": s["spanish_tax_h2"],
         "Filed with": f"{fy - 1} returns (by Jan {fy})"},
        {"Period": f"Jan-Jun {fy}", "Rent (EUR)": s["rent_h1"], "Tax (EUR)": s["spanish_tax_h1"],
         "Filed with": f"{fy} returns (by Jan {fy + 1})"},
    ]).style.format({"Rent (EUR)": "{:,.2f}", "Tax (EUR)": "{:,.2f}"}), use_container_width=True, hide_index=True)
    st.caption("Calculated on gross rent. Tax on deemed income for days not rented (imputación) and the "
               "amounts actually paid come from your Modelo 210 filings.")

    with st.expander(f"Expense detail ({len(s['deductible_detail'])} items)"):
        st.dataframe(s["deductible_detail"][["expense_date", "Accountant category", "supplier", "concept", "unit",
                                             "amount", "pdf_filename"]], use_container_width=True, hide_index=True)
    if not s["capital_items"].empty:
        with st.expander("Capital items (depreciate, not an immediate deduction)"):
            st.dataframe(s["capital_items"][["expense_date", "concept", "unit", "amount"]],
                         use_container_width=True, hide_index=True)
    if not s["excluded"].empty:
        with st.expander("Left out of deductions"):
            st.dataframe(s["excluded"][["expense_date", "category", "concept", "amount"]],
                         use_container_width=True, hide_index=True)
    with st.expander(f"Bookings ({len(s['bookings'])})"):
        b = s["bookings"][["booking_ref", "arrival", "departure", "nights", "nights_fy", "income_total",
                           "agent_statement", "rented"]].rename(columns={"nights_fy": f"nights in {s['fy']}"})
        st.dataframe(b, use_container_width=True, hide_index=True)
