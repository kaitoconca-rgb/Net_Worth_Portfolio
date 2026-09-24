"""Investment properties, read live from Zarpia.

Sep 2026. Zarpia holds the financials of every investment property; this app
reads them and builds the Australian financial-year view the accountant asks
for, per property and for the whole portfolio: weeks available / rented, rent,
expenses in the accountant's categories, capital items, and (for Spain) the
non-resident tax on the rent.

Zarpia exposes read-only views (schema nw_feed) through a dedicated login
(nw_feed_reader) that can see only this owner's properties. Every view carries
property_id, so a property added in Zarpia shows up here with no code change.

Income comes from two places in Zarpia:
  * icnea bookings (Spanish agent) - Zarpia's own Australian-FY accruals;
  * manual / Gmail / Outlook income (nw_feed.manual_income) - spread over the
    nights of the stay, like Zarpia's reports.

Where the data comes from (get_feed):
  * live: a connection string in Streamlit secrets (ZARPIA_FEED_CONN_STRING).
    Each read is retried once (connection blips). Every successful read is
    also saved as a copy in this app's own database (public.zarpia_snapshot)
    when the data has changed.
  * if the live read still fails (Zarpia's database down or over quota, a
    permission problem...), the newest saved copy is used and the page says
    so, with the date of the copy and the error -- never partial figures.
  * with no live link configured, the saved copy is used.
Older copies (single property, no property_id) still load.
"""
import json
import time
from datetime import date, datetime

import pandas as pd
import streamlit as st
from sqlalchemy import text as sql_text

SPANISH_NR_RATE = 0.24   # IRNR rate for non-EU residents (Modelo 210), on gross rent

COUNTRY_NAMES = {"ES": "Spain", "IT": "Italy", "AU": "Australia", "FR": "France", "PT": "Portugal"}

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


def fy_of(d):
    return d.year + 1 if d.month >= 7 else d.year


def md(text):
    """Escape '$' so Streamlit markdown doesn't read 'A$ ... A$' as a maths formula."""
    return text.replace("$", "\\$")


def property_currency(prop_row):
    c = str(prop_row.get("country") or "")
    if c == "AU":
        return "AUD"
    if c == "OTHER":
        return str(prop_row.get("other_currency_code") or "EUR")
    return "EUR"


def feed_configured():
    try:
        return bool(st.secrets.get("ZARPIA_FEED_CONN_STRING"))
    except Exception:
        return False


def _conn():
    return st.connection("zarpia_feed", type="sql", url=st.secrets["ZARPIA_FEED_CONN_STRING"],
                         pool_pre_ping=True)


FEED_COLS = {
    "property": ["id", "name", "country", "municipality", "acquisition_cost", "other_currency_code"],
    "units": ["id", "name", "unit_type", "cadastral_value_total", "imputation_rate_pct",
              "income_allocation_pct", "disposal_date", "property_id"],
    "bookings": ["booking_ref", "arrival", "departure", "nights", "included_in_tax_calc", "income_total",
                 "agent_statement", "statement_date", "property_id"],
    "accruals": ["booking_ref", "accrual_basis", "tax_year", "nights_in_year", "accrued_amount", "is_estimate",
                 "property_id"],
    "expenses": ["id", "expense_date", "category", "treatment", "supplier", "concept", "amount", "currency",
                 "unit", "included_in_tax_calc", "pdf_filename", "source", "property_id"],
    "statements": ["statement", "settlement_date", "gross_amount", "owner_amount", "net_amount", "currency",
                   "property_id"],
    "manual_income": ["income_ref", "source", "checkin", "checkout", "nights", "amount", "currency",
                      "accrual_date", "tax_year", "included_in_tax_calc", "property_id"],
}
SORT = {"bookings": "arrival", "expenses": "expense_date", "statements": "settlement_date",
        "manual_income": "checkin"}


def _single_property_ids(feed):
    """Pre-149 feeds / older copies cover one property and have no property_id."""
    if len(feed["property"]) != 1:
        return
    pid = str(feed["property"].iloc[0]["id"])
    for k in FEED_COLS:
        if k != "property" and len(feed[k]):
            feed[k]["property_id"] = feed[k]["property_id"].where(feed[k]["property_id"].notna(), pid)


def _frames_from_json(data):
    feed = {k: pd.DataFrame(data.get(k) or [], columns=cols) for k, cols in FEED_COLS.items()}
    _single_property_ids(feed)
    return feed


def _latest_copy(_pg):
    """(copied_at, raw dict) of the newest saved copy, or (None, None)."""
    try:
        df = _pg.query("SELECT copied_at, data::text AS data FROM public.zarpia_snapshot "
                       "ORDER BY id DESC LIMIT 1", ttl=0)
    except Exception:
        return None, None
    if df.empty:
        return None, None
    return pd.to_datetime(df.iloc[0]["copied_at"]), json.loads(df.iloc[0]["data"])


def _feed_from_raw(raw):
    return _typed(_frames_from_json(raw))


def load_snapshot(_pg):
    """Newest saved copy as a feed (with copied_at), or None if there isn't one."""
    copied_at, raw = _latest_copy(_pg)
    if raw is None:
        return None
    feed = _feed_from_raw(raw)
    feed["copied_at"] = copied_at
    return feed


FEED_QUERIES = {
    "property": "SELECT * FROM nw_feed.property ORDER BY name",
    "units": "SELECT * FROM nw_feed.units",
    "bookings": "SELECT * FROM nw_feed.bookings ORDER BY arrival",
    "accruals": "SELECT * FROM nw_feed.booking_accruals",
    "expenses": "SELECT * FROM nw_feed.expenses ORDER BY expense_date",
    "statements": "SELECT * FROM nw_feed.agent_statements ORDER BY settlement_date",
    "manual_income": "SELECT * FROM nw_feed.manual_income ORDER BY checkin",
}


def _records(df):
    """DataFrame -> JSON-safe records: UUID / date / Decimal -> str or number,
    NaN / NaT -> null (Postgres jsonb rejects NaN)."""
    clean = df.astype(object).where(df.notna(), None)
    return json.loads(json.dumps(clean.to_dict("records"), default=str))


@st.cache_data(ttl=3600, show_spinner="Reading your properties from Zarpia…")
def fetch_live_raw():
    """All feed views, as plain JSON-able records. Every view must be read:
    one failing view fails the whole read (no partial figures). Retried once."""
    last_err = None
    for attempt in range(2):
        try:
            c = _conn()
            raw = {}
            for k, sql in FEED_QUERIES.items():
                df = c.query(sql, ttl=0)
                raw[k] = _records(df)
            return raw
        except Exception as e:          # connection blip, Zarpia down, missing view / permission
            last_err = e
            if attempt == 0:
                time.sleep(2)
    raise last_err


def _save_copy(pg, raw):
    """Store the live data as the fallback copy, only when it changed."""
    try:
        _, latest = _latest_copy(pg)
        if latest is not None and latest == json.loads(json.dumps(raw)):
            return
        with pg.session as s:
            s.execute(sql_text("INSERT INTO public.zarpia_snapshot (copied_at, data) VALUES (now(), :d)"),
                      {"d": json.dumps(raw)})
            # keep the last 30 copies
            s.execute(sql_text("DELETE FROM public.zarpia_snapshot WHERE id NOT IN "
                               "(SELECT id FROM public.zarpia_snapshot ORDER BY id DESC LIMIT 30)"))
            s.commit()
    except Exception:
        pass                             # saving the copy must never break the page


def get_feed(pg=None):
    """(feed, status). status: {"mode": "live"|"copy", "copied_at": ts|None, "error": str|None}.
    Raises only if there is no live link AND no saved copy."""
    if feed_configured():
        try:
            raw = fetch_live_raw()
            if pg is not None and not st.session_state.get("zf_copy_saved"):
                _save_copy(pg, raw)
                st.session_state["zf_copy_saved"] = True
            return _feed_from_raw(raw), {"mode": "live", "copied_at": None, "error": None}
        except Exception as e:
            err = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}" if str(e) else type(e).__name__
            feed = load_snapshot(pg) if pg is not None else None
            if feed is None:
                raise RuntimeError(f"Couldn't read Zarpia and there is no saved copy ({err})") from e
            return feed, {"mode": "copy", "copied_at": feed["copied_at"], "error": err}
    feed = load_snapshot(pg) if pg is not None else None
    if feed is None:
        raise RuntimeError("Zarpia not connected and no saved copy")
    return feed, {"mode": "copy", "copied_at": feed["copied_at"], "error": None}


def status_text(status):
    """One line for captions / the accountant pack."""
    if status["mode"] == "live":
        return f"Zarpia (live, read {datetime.now():%d %b %Y})"
    when = f"{status['copied_at']:%d %b %Y %H:%M} UTC" if status.get("copied_at") is not None else "unknown date"
    return f"Zarpia (saved copy of {when})"


def _typed(feed):
    for k, col in SORT.items():
        feed[k] = feed[k].sort_values(col, kind="stable", na_position="last").reset_index(drop=True)
    for k in ("bookings", "expenses", "statements", "manual_income"):
        for col in feed[k].columns:
            if col in ("arrival", "departure", "expense_date", "settlement_date", "statement_date",
                       "checkin", "checkout", "accrual_date"):
                feed[k][col] = pd.to_datetime(feed[k][col]).dt.date
    for k in FEED_COLS:
        if k != "property":
            feed[k]["property_id"] = feed[k]["property_id"].astype(str)
    feed["property"]["id"] = feed["property"]["id"].astype(str)
    feed["bookings"]["income_total"] = pd.to_numeric(feed["bookings"]["income_total"], errors="coerce").astype(float)
    feed["bookings"]["nights"] = pd.to_numeric(feed["bookings"]["nights"], errors="coerce")
    feed["expenses"]["amount"] = pd.to_numeric(feed["expenses"]["amount"], errors="coerce").astype(float)
    feed["accruals"]["accrued_amount"] = pd.to_numeric(feed["accruals"]["accrued_amount"], errors="coerce").astype(float)
    for col in ("tax_year", "nights_in_year"):
        feed["accruals"][col] = pd.to_numeric(feed["accruals"][col], errors="coerce")
    mi = feed["manual_income"]
    mi["amount"] = pd.to_numeric(mi["amount"], errors="coerce").astype(float)
    mi["nights"] = pd.to_numeric(mi["nights"], errors="coerce")
    mi["tax_year"] = pd.to_numeric(mi["tax_year"], errors="coerce")
    return feed


def _nights_in(start_col, end_col, start, end):
    """Nights of each stay falling in [start, end] (a night = the date it starts)."""
    a = pd.to_datetime(start_col).clip(lower=pd.Timestamp(start))
    d = pd.to_datetime(end_col).clip(upper=pd.Timestamp(end) + pd.Timedelta(days=1))
    return (d - a).dt.days.clip(lower=0).fillna(0)


def _manual_in_window(mi, start, end):
    """Manual income in [start, end]: spread over the nights of the stay; with no
    stay dates, the whole amount on its accrual date. Returns (rows with
    amount_in / nights_in, rows that have neither - not counted in any year)."""
    mi = mi.copy()
    if mi.empty:
        mi["amount_in"] = pd.Series(dtype=float)
        mi["nights_in"] = pd.Series(dtype=int)
        return mi, mi
    dated = mi["checkin"].notna() & mi["checkout"].notna()
    nights_total = (pd.to_datetime(mi["checkout"]) - pd.to_datetime(mi["checkin"])).dt.days
    nights_in = _nights_in(mi["checkin"], mi["checkout"], start, end)
    share = (nights_in / nights_total.where(nights_total > 0)).fillna(0)
    by_accrual = ~dated & mi["accrual_date"].notna()
    acc_in = by_accrual & mi["accrual_date"].apply(lambda d: d is not None and pd.notna(d) and start <= d <= end)
    amt = mi["amount"].fillna(0.0)
    mi["amount_in"] = (amt * share).where(dated, 0.0) + amt.where(acc_in, 0.0)
    mi["nights_in"] = nights_in.where(dated, 0).astype(int)
    return mi, mi[~dated & ~by_accrual]


def properties(feed):
    """One row per property: id, name, country, currency."""
    p = feed["property"].copy()
    p["currency"] = [property_currency(r) for _, r in p.iterrows()]
    return p.sort_values("name", key=lambda c: c.astype(str).str.lower(), kind="stable").reset_index(drop=True)


def available_fys(feed):
    """Australian financial years with any rent or expenses, newest first."""
    ys = set()
    acc = feed["accruals"]
    ys |= {int(y) for y in acc.loc[acc["accrual_basis"] == "au_fy", "tax_year"].dropna()}
    for col in ("checkin", "accrual_date"):
        ys |= {fy_of(d) for d in feed["manual_income"][col].dropna()}
    ys |= {fy_of(d) for d in feed["expenses"]["expense_date"].dropna()}
    return sorted(ys, reverse=True)


def property_fy_summary(feed, fy_year, property_id=None):
    """Australian-FY figures for one property, in the property's own currency."""
    props = properties(feed)
    if property_id is None:
        if len(props) != 1:
            raise ValueError("property_id is required when the feed has several properties")
        property_id = props.iloc[0]["id"]
    property_id = str(property_id)
    prow = props[props["id"] == property_id].iloc[0]
    only = lambda df: df[df["property_id"] == property_id]

    start, end = fy_bounds(fy_year)
    fy_days = (end - start).days + 1

    # Agent (icnea) bookings: Zarpia's own Australian-FY accruals.
    bk = only(feed["bookings"]).copy()
    bk["rented"] = bk["income_total"].fillna(0) > 0
    bk["nights_fy"] = _nights_in(bk["arrival"], bk["departure"], start, end).astype(int)
    owner_nights = int(bk.loc[~bk["rented"], "nights_fy"].sum())
    acc = only(feed["accruals"])
    acc_fy = acc[(acc["accrual_basis"] == "au_fy") & (acc["tax_year"] == fy_year)]
    rent_icnea = float(acc_fy["accrued_amount"].sum())
    nights_icnea = int(pd.to_numeric(acc_fy["nights_in_year"], errors="coerce").fillna(0).sum())

    # Manual / Gmail / Outlook income (left out if excluded from the tax calc in Zarpia).
    mi_all = only(feed["manual_income"])
    mi_all = mi_all[mi_all["included_in_tax_calc"].fillna(True).astype(bool)]
    mi, undated = _manual_in_window(mi_all, start, end)
    mi_fy = mi[mi["amount_in"].abs() > 0.004]
    rent_manual = float(mi_fy["amount_in"].sum())
    nights_manual = int(mi_fy["nights_in"].sum())

    rent = rent_icnea + rent_manual
    rented_nights = nights_icnea + nights_manual

    # Rent by calendar half (Spain and Italy tax by calendar year).
    h2 = (start, date(fy_year - 1, 12, 31))
    h1 = (date(fy_year, 1, 1), end)
    per_night = (bk["income_total"].fillna(0) / bk["nights"].replace(0, pd.NA)).fillna(0)
    rent_h2 = float((per_night * _nights_in(bk["arrival"], bk["departure"], *h2)).sum())
    rent_h1 = float((per_night * _nights_in(bk["arrival"], bk["departure"], *h1)).sum())
    rent_h2 += float(_manual_in_window(mi_all, *h2)[0]["amount_in"].sum())
    rent_h1 += float(_manual_in_window(mi_all, *h1)[0]["amount_in"].sum())

    ex = only(feed["expenses"])
    ex = ex[(pd.to_datetime(ex["expense_date"]) >= pd.Timestamp(start))
            & (pd.to_datetime(ex["expense_date"]) <= pd.Timestamp(end))].copy()
    ex["Accountant category"] = [accountant_category(r) for _, r in ex.iterrows()]
    included = ex["included_in_tax_calc"].fillna(True).astype(bool)
    capital = ex["treatment"].fillna("").astype(str).str.startswith("capital")
    deductible = ex[included & ~capital]
    by_cat = deductible.groupby("Accountant category")["amount"].sum().reindex(CATS).fillna(0.0)

    country = str(prow["country"] or "")
    spain = country == "ES"
    manual_rows = mi_fy.rename(columns={"amount_in": "amount in FY", "nights_in": "nights in FY"})
    return {
        "property_id": property_id, "name": str(prow["name"]), "country": country,
        "country_name": COUNTRY_NAMES.get(country, country), "currency": prow["currency"],
        "fy": f"FY{str(fy_year)[-2:]}", "fy_year": fy_year, "start": start, "end": end,
        "weeks_available": round((fy_days - owner_nights) / 7, 1),
        "weeks_rented": round(rented_nights / 7, 1),
        "owner_nights": owner_nights, "rented_nights": rented_nights,
        "rent": rent, "rent_icnea": rent_icnea, "rent_manual": rent_manual,
        "rent_h2": rent_h2, "rent_h1": rent_h1,
        "expenses_by_cat": by_cat, "expenses_total": float(by_cat.sum()),
        "deductible_detail": deductible, "capital_items": ex[included & capital],
        "excluded": ex[~included],
        "spanish_tax": spain,
        "spanish_tax_h2": round(rent_h2 * SPANISH_NR_RATE, 2) if spain else None,
        "spanish_tax_h1": round(rent_h1 * SPANISH_NR_RATE, 2) if spain else None,
        "bookings": bk[bk["nights_fy"] > 0],
        "manual_income": manual_rows,
        "undated_income": undated,
        "has_activity": bool(abs(rent) > 0.004 or len(ex) or (bk["nights_fy"] > 0).any()),
    }


def portfolio_fy_summaries(feed, fy_year):
    """One summary per property with any rent, expenses or bookings in the FY."""
    out = [property_fy_summary(feed, fy_year, pid) for pid in properties(feed)["id"]]
    return [s for s in out if s["has_activity"]]


def rate_for(aud_avg_for_fy, fy, currency):
    """A$ per unit of the property's currency, ATO average for the FY (None if unknown)."""
    if currency == "AUD":
        return 1.0
    try:
        return aud_avg_for_fy(fy, currency)
    except TypeError:                   # older app.py: EUR only
        return aud_avg_for_fy(fy) if currency == "EUR" else None


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
This login can only read your property summary views - nothing else in Zarpia.
To switch it off later: `ALTER ROLE nw_feed_reader NOLOGIN;`
"""


def _sym(ccy):
    return {"EUR": "€", "AUD": "A$"}.get(ccy, f"{ccy} ")


def portfolio_table(summaries, rate_of):
    rows = []
    for s in summaries:
        r = rate_of(s["currency"])
        net = s["rent"] - s["expenses_total"]
        rows.append({"Property": s["name"], "Country": s["country_name"], "Currency": s["currency"],
                     "Weeks rented": s["weeks_rented"], "Rent": s["rent"],
                     "Deductible expenses": s["expenses_total"], "Net before depreciation": net,
                     "Rent A$": s["rent"] * r if r else None,
                     "Expenses A$": s["expenses_total"] * r if r else None,
                     "Net A$": net * r if r else None})
    return pd.DataFrame(rows)


def _render_detail(s, rate):
    ccy, cur = s["currency"], _sym(s["currency"])
    show_aud = bool(rate) and ccy != "AUD"
    aud = lambda v: f"A${v * rate:,.0f}" if show_aud else None
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Weeks available", f"{s['weeks_available']}", help=f"{s['owner_nights']} owner-use nights excluded")
    c2.metric("Weeks rented", f"{s['weeks_rented']}", help=f"{s['rented_nights']} booked nights ÷ 7")
    c3.metric("Rent (gross)", f"{cur}{s['rent']:,.2f}", aud(s["rent"]), delta_color="off")
    c4.metric("Deductible expenses", f"{cur}{s['expenses_total']:,.2f}", aud(s["expenses_total"]),
              delta_color="off")
    if show_aud:
        st.caption(md(f"A$ at the ATO's {s['fy']} average rate ({1 / rate:.4f} {ccy} per A$ = "
                      f"{rate:.4f} A$ per {ccy}), the same rate Zarpia uses."))
    elif ccy != "AUD":
        st.caption(md(f"No ATO average rate for {ccy} in {s['fy']} - A$ not shown."))

    st.markdown("#### Expenses by accountant category")
    t = s["expenses_by_cat"].reset_index()
    t.columns = ["Category", ccy]
    if show_aud:
        t["A$"] = t[ccy] * rate
    st.dataframe(t.style.format({ccy: "{:,.2f}", "A$": "{:,.2f}"}), use_container_width=True, hide_index=True)
    if s["expenses_by_cat"].get("Strata special levy", 0):
        st.caption("Special levies (derrama): purpose to confirm - capital if they fund improvements.")

    fy = s["fy_year"]
    if s["spanish_tax"]:
        st.markdown("#### Spanish non-resident tax on the rent (24%)")
        st.dataframe(pd.DataFrame([
            {"Period": f"Jul-Dec {fy - 1}", "Rent (EUR)": s["rent_h2"], "Tax (EUR)": s["spanish_tax_h2"],
             "Filed with": f"{fy - 1} returns (by Jan {fy})"},
            {"Period": f"Jan-Jun {fy}", "Rent (EUR)": s["rent_h1"], "Tax (EUR)": s["spanish_tax_h1"],
             "Filed with": f"{fy} returns (by Jan {fy + 1})"},
        ]).style.format({"Rent (EUR)": "{:,.2f}", "Tax (EUR)": "{:,.2f}"}), use_container_width=True, hide_index=True)
        st.caption("Calculated on gross rent. Tax on deemed income for days not rented (imputación) and the "
                   "amounts actually paid come from your Modelo 210 filings.")
    elif s["country"] != "AU":
        st.markdown(f"#### Rent by calendar half ({s['country_name']} taxes by calendar year)")
        st.dataframe(pd.DataFrame([
            {"Period": f"Jul-Dec {fy - 1}", f"Rent ({ccy})": s["rent_h2"]},
            {"Period": f"Jan-Jun {fy}", f"Rent ({ccy})": s["rent_h1"]},
        ]).style.format({f"Rent ({ccy})": "{:,.2f}"}), use_container_width=True, hide_index=True)
        st.caption(f"Tax in {s['country_name']}: see this property on Zarpia's Tax page (not estimated here).")

    if not s["undated_income"].empty:
        st.warning(f"{len(s['undated_income'])} income entry(ies) in Zarpia have no stay or accrual date, so they "
                   "aren't counted in any year - add the dates in Zarpia.")
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
    if len(s["bookings"]):
        with st.expander(f"Agent bookings ({len(s['bookings'])})"):
            b = s["bookings"][["booking_ref", "arrival", "departure", "nights", "nights_fy", "income_total",
                               "agent_statement", "rented"]].rename(columns={"nights_fy": f"nights in {s['fy']}"})
            st.dataframe(b, use_container_width=True, hide_index=True)
    if len(s["manual_income"]):
        with st.expander(f"Other income ({len(s['manual_income'])})"):
            st.dataframe(s["manual_income"][["source", "checkin", "checkout", "nights", "amount", "amount in FY",
                                             "nights in FY"]], use_container_width=True, hide_index=True)


def render_property_page(aud_avg_for_fy, pg=None):
    st.header("🏠 Investment properties (from Zarpia)")
    try:
        feed, status = get_feed(pg)
    except Exception as e:
        st.error(str(e))
        st.markdown(SETUP_HELP)
        return
    if feed_configured() and st.button("🔄 Refresh from Zarpia", key="zf_refresh"):
        fetch_live_raw.clear()
        st.session_state.pop("zf_copy_saved", None)
        st.rerun()
    if status["error"]:
        st.warning(f"Couldn't read Zarpia live just now, so this page shows the **saved copy from "
                   f"{status['copied_at']:%d %b %Y %H:%M} UTC** - complete up to that date; anything added in "
                   f"Zarpia since isn't included. Try **Refresh from Zarpia** in a few minutes. "
                   f"If it keeps failing, check that Zarpia's Supabase project is up (quota) - "
                   f"error: `{status['error']}`")
    elif status["mode"] == "copy":
        st.caption(f"Saved copy from Zarpia, {status['copied_at']:%d %b %Y %H:%M} UTC (no live link configured).")

    years = available_fys(feed)
    if not years:
        st.info("No rent or expenses in Zarpia yet.")
        return
    today = date.today()
    current = today.year if today.month < 7 else today.year + 1
    past = [y for y in years if y < current] or years
    fy = st.selectbox("Australian financial year", years, index=years.index(past[0]),
                      format_func=lambda y: f"FY{str(y)[-2:]} (1 Jul {y - 1} - 30 Jun {y})"
                      + (" - in progress" if y == current else ""), key="zf_fy")
    summaries = portfolio_fy_summaries(feed, fy)
    if not summaries:
        st.info(f"No rent or expenses for any property in FY{str(fy)[-2:]}.")
        return
    rate_of = lambda ccy: rate_for(aud_avg_for_fy, fy, ccy)

    st.markdown(f"#### Portfolio - FY{str(fy)[-2:]}")
    pt = portfolio_table(summaries, rate_of)
    if len(pt) > 1:
        pt = pd.concat([pt, pd.DataFrame([{
            "Property": "Total (A$)", "Rent A$": pt["Rent A$"].sum(min_count=1),
            "Expenses A$": pt["Expenses A$"].sum(min_count=1), "Net A$": pt["Net A$"].sum(min_count=1)}])],
            ignore_index=True)
    money = {c: "{:,.2f}" for c in ("Rent", "Deductible expenses", "Net before depreciation", "Rent A$",
                                    "Expenses A$", "Net A$")}
    st.dataframe(pt.style.format(money, na_rep=""), use_container_width=True, hide_index=True)
    st.caption(md("Rent / expenses / net in each property's own currency; A$ at the ATO's average rate for the "
                  "year. Net is before depreciation and before foreign tax."))

    st.markdown("#### Property detail")
    names = {s["property_id"]: f"{s['name']} ({s['country_name']})" for s in summaries}
    pid = (st.selectbox("Property", list(names), format_func=names.get, key="zf_prop")
           if len(summaries) > 1 else summaries[0]["property_id"])
    s = next(x for x in summaries if x["property_id"] == pid)
    _render_detail(s, rate_of(s["currency"]))
