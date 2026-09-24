"""Reserve Bank of Australia exchange rates for Claudio's Executive Console.

Sep 2026: AUD conversions used Yahoo Finance market rates, and when Yahoo
failed the app silently used a guess (EUR = 1.65, other currencies = 1.0).
The ATO accepts RBA rates, so the official daily rates (table F11.1, 4pm AEST)
are now stored in `fx_rates` and used for every historical conversion:
N26 purchase costs, dividends, coupons, commodity purchases.

Stored as AUD per 1 unit of the foreign currency (the RBA publishes the
inverse, foreign currency per A$1):
    fx_rates(rate_date, from_currency='EUR', to_currency='AUD', rate=1.6533, source='RBA F11.1')

Weekends / public holidays use the previous business day's rate.
"""
import csv
import io
from datetime import date, datetime, timedelta

import pandas as pd
import requests
import streamlit as st
from sqlalchemy import text as sql_text

RBA_URL = "https://www.rba.gov.au/statistics/tables/csv/f11.1-data.csv"
RBA_SOURCE = "RBA F11.1"
# Series we keep (RBA publishes ~20; these cover what you hold or might).
RBA_SERIES = {
    "FXREUR": "EUR", "FXRUSD": "USD", "FXRUKPS": "GBP", "FXRSF": "CHF", "FXRNZD": "NZD",
    "FXRCD": "CAD", "FXRJY": "JPY", "FXRSD": "SGD", "FXRHKD": "HKD", "FXRCR": "CNY",
}
MAX_LOOKBACK_DAYS = 7

SCHEMA_STMTS = (
    "ALTER TABLE fx_rates ADD COLUMN IF NOT EXISTS source text",
    "CREATE UNIQUE INDEX IF NOT EXISTS fx_rates_date_pair_uniq ON fx_rates (rate_date, from_currency, to_currency)",
    "ALTER TABLE dividends ADD COLUMN IF NOT EXISTS fx_rate_to_aud numeric",
)

# Fills AUD rates on rows recorded before this existed. Only touches NULLs,
# so it's cheap to run after every refresh.
BACKFILL_STMTS = (
    """UPDATE dividends SET fx_rate_to_aud = 1
       WHERE fx_rate_to_aud IS NULL AND upper(currency) = 'AUD'""",
    """UPDATE dividends d SET fx_rate_to_aud = (
           SELECT f.rate FROM fx_rates f
           WHERE f.from_currency = upper(d.currency) AND f.to_currency = 'AUD'
             AND f.rate_date <= d.div_date AND f.rate_date > d.div_date - 7
           ORDER BY f.rate_date DESC LIMIT 1)
       WHERE d.fx_rate_to_aud IS NULL AND upper(d.currency) <> 'AUD'""",
    """UPDATE transactions t SET fx_rate_to_aud = (
           SELECT f.rate FROM fx_rates f, accounts a
           WHERE a.id = t.account_id AND f.from_currency = upper(a.currency) AND f.to_currency = 'AUD'
             AND f.rate_date <= t.tx_date AND f.rate_date > t.tx_date - 7
           ORDER BY f.rate_date DESC LIMIT 1)
       WHERE t.fx_rate_to_aud IS NULL
         AND t.account_id IN (SELECT id FROM accounts WHERE upper(currency) <> 'AUD')""",
    """UPDATE transactions SET fx_rate_to_aud = 1
       WHERE fx_rate_to_aud IS NULL
         AND account_id IN (SELECT id FROM accounts WHERE upper(currency) = 'AUD')""",
)


def ensure_fx_schema(conn):
    with conn.session as s:
        s.execute(sql_text("SET LOCAL lock_timeout = '5s'"))
        for stmt in SCHEMA_STMTS:
            s.execute(sql_text(stmt))
        s.commit()


def fetch_rba_rates():
    """Download F11.1 and return a long DataFrame: rate_date, currency, aud_per_unit."""
    resp = requests.get(
        RBA_URL, timeout=30,
        headers={"User-Agent": "Mozilla/5.0 (personal net worth app; RBA F11.1 daily rates)"},
    )
    resp.raise_for_status()
    text = resp.content.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    header = next((r for r in rows if r and r[0].strip() == "Series ID"), None)
    if header is None:
        raise ValueError("RBA file format changed: no 'Series ID' row")
    cols = {i: RBA_SERIES[h.strip()] for i, h in enumerate(header) if h.strip() in RBA_SERIES}
    out = []
    for r in rows:
        if not r:
            continue
        try:
            d = datetime.strptime(r[0].strip(), "%d-%b-%Y").date()
        except ValueError:
            continue
        for i, ccy in cols.items():
            if i < len(r) and r[i].strip():
                try:
                    per_aud = float(r[i])
                except ValueError:
                    continue
                if per_aud > 0:
                    out.append((d, ccy, 1.0 / per_aud))
    if not out:
        raise ValueError("No rates found in the RBA file")
    return pd.DataFrame(out, columns=["rate_date", "currency", "aud_per_unit"])


def _last_business_day(today):
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def refresh_rba_rates(conn, force=False):
    """Download new RBA rates if the stored ones are stale. Returns a status dict."""
    status = {"ok": False, "new_rows": 0, "latest": None, "error": None}
    try:
        latest = conn.query(
            "SELECT max(rate_date) AS d FROM fx_rates WHERE source = :src",
            params={"src": RBA_SOURCE}, ttl=0,
        )["d"].iloc[0]
        latest = pd.to_datetime(latest).date() if pd.notna(latest) else None
        status["latest"] = latest
        if not force and latest and latest >= _last_business_day(date.today()):
            status["ok"] = True
            return status
        df = fetch_rba_rates()
        if latest and not force:
            df = df[df["rate_date"] > latest]
        with conn.session as s:
            s.execute(sql_text("SET LOCAL statement_timeout = '60s'"))
            # Only one app session updates at a time; others just use what's stored.
            got_lock = s.execute(sql_text("SELECT pg_try_advisory_xact_lock(424242)")).scalar()
            if not got_lock:
                s.rollback()
                status["ok"] = True
                return status
            if not df.empty:
                # One statement for all rows (row-by-row inserts took minutes over the network).
                s.execute(sql_text("""
                    INSERT INTO fx_rates (rate_date, from_currency, to_currency, rate, source)
                    SELECT u.d, u.c, 'AUD', u.r, :src
                    FROM unnest(CAST(:ds AS date[]), CAST(:cs AS text[]), CAST(:rs AS numeric[])) AS u(d, c, r)
                    ON CONFLICT (rate_date, from_currency, to_currency)
                    DO UPDATE SET rate = EXCLUDED.rate, source = EXCLUDED.source
                """), {"ds": [r.rate_date for r in df.itertuples()],
                       "cs": [r.currency for r in df.itertuples()],
                       "rs": [round(float(r.aud_per_unit), 8) for r in df.itertuples()],
                       "src": RBA_SOURCE})
            for stmt in BACKFILL_STMTS:
                s.execute(sql_text(stmt))
            s.commit()
        status.update(ok=True, new_rows=len(df), latest=max(df["rate_date"]) if not df.empty else latest)
    except Exception as e:  # network down, RBA site changed...
        status["error"] = f"{type(e).__name__}: {e}"[:300]
    return status


@st.cache_data(ttl=3600, show_spinner=False)
def load_rba_series(_conn, version=None):
    """{currency: pd.Series(AUD per unit, indexed by date)}"""
    df = _conn.query(
        "SELECT rate_date, from_currency, rate FROM fx_rates "
        "WHERE to_currency = 'AUD' AND source = :src ORDER BY rate_date",
        params={"src": RBA_SOURCE}, ttl=0,
    )
    out = {}
    if df.empty:
        return out
    df["rate_date"] = pd.to_datetime(df["rate_date"])
    df["rate"] = df["rate"].astype(float)
    for ccy, g in df.groupby("from_currency"):
        out[ccy] = g.set_index("rate_date")["rate"]
    return out


def rba_rate(series, ccy, d):
    """AUD per 1 `ccy` on date d (previous business day if d isn't one). None if unknown."""
    ccy = str(ccy or "").upper().strip()
    if ccy in ("AUD", "A$"):
        return 1.0
    s = series.get(ccy) if series else None
    if s is None or s.empty:
        return None
    ts = pd.Timestamp(d)
    sub = s[s.index <= ts]
    if sub.empty:
        return None
    if (ts - sub.index[-1]).days > MAX_LOOKBACK_DAYS:
        return None
    return float(sub.iloc[-1])


def rba_latest(series, ccy):
    s = series.get(str(ccy or "").upper()) if series else None
    return float(s.iloc[-1]) if s is not None and not s.empty else None
