"""
add_commsec_purchase.py
─────────────────────────────────────────────────────────────────
One-time infrastructure + repeatable purchase-entry script for the
CommSec account in the Net Worth Postgres DB.

WHAT THIS DOES, IN ORDER:
  1. Rebrands the existing "ASX Shares" account (id d11dbbea-...) to
     "CommSec" — it already holds NHF/TPG/TUA/WBC, confirmed to be the
     same CommSec account you're now buying VAS/VGS/ASIA through.
     Safe to re-run: it's a plain UPDATE, not a new account.
  2. Registers VAS, VGS and ASIA as instruments (idempotent upsert —
     matches the pattern used in migrate_to_postgres.py /
     sync_raiz_to_postgres.py for every other platform in this app).
  3. Inserts the "buy" transactions listed in the PURCHASES list below.
     Fingerprints against what's already in Postgres (same date + code +
     quantity + price), so running this twice will NOT double-count.

HOW TO USE:
  1. Edit the PURCHASES list below with your real trade details for
     today's VAS / VGS / ASIA buy (quantity, price per unit, and the
     total amount actually debited incl. brokerage).
  2. Preview first:
       py -3.14 -m pip install psycopg2-binary
       py -3.14 add_commsec_purchase.py --dry-run
  3. If it looks right:
       py -3.14 add_commsec_purchase.py

This mirrors sync_raiz_to_postgres.py's structure so it fits the rest
of the repo's conventions.
"""

import argparse
from datetime import date
import streamlit as st
import psycopg2
import psycopg2.extras

# ── CONFIG ───────────────────────────────────────────────────────────────
# Same pooler connection string used by every other script in this repo.
# TODO (security): this is currently plaintext in several files in this
# repo (this one included) and in .streamlit/secrets.toml. Consider moving
# it to a single environment variable (e.g. NETWORTH_PG_URL) that every
# script reads, so the real credential only has to live in one place.
PG_CONN_STRING = PG_CONN_STRING = st.secrets["PG_CONN_STRING"]

COMMSEC_ACCOUNT_ID = "d11dbbea-8a63-42da-9329-ab85ec00bea8"  # was "ASX Shares"

# New instruments to register. yahoo_ticker is informational — the live
# app.py actually resolves prices via its own SHARES_TICKERS dict (already
# updated to include these three), but keeping this column populated keeps
# it consistent with how every other instrument in the table was set up.
NEW_INSTRUMENTS = [
    # (code, display_name, yahoo_ticker)
    ("VAS",  "Vanguard Australian Shares Index ETF",              "VAS.AX"),
    ("VGS",  "Vanguard MSCI Index International Shares ETF",      "VGS.AX"),
    ("ASIA", "BetaShares Asia Technology Tigers ETF",              "ASIA.AX"),
]

# ── ✏️ EDIT THIS — your real CommSec purchase(s) ────────────────────────────
# amount = total AUD actually debited from your CommSec account for that
# trade, INCLUDING brokerage (so cost-basis/P&L tracking is accurate).
# quantity/price should match your contract note.
PURCHASES = [
    {"code": "VAS",  "quantity": 437.0, "price": 113.980000, "amount": 49869.03, "trade_date": "2026-08-25"},
    {"code": "VGS",  "quantity": 156.0, "price": 159.980000, "amount": 24986.83, "trade_date": "2026-08-25"},
    {"code": "ASIA", "quantity": 1286.0, "price": 19.396501, "amount": 24973.85, "trade_date": "2026-08-25"},
]


def get_pg_conn():
    return psycopg2.connect(PG_CONN_STRING)


def rebrand_account(cur, dry_run):
    print("→ Rebranding account to CommSec...")
    if dry_run:
        print(f"  DRY RUN — would UPDATE accounts SET name='CommSec', "
              f"platform='CommSec' WHERE id='{COMMSEC_ACCOUNT_ID}'")
        return
    cur.execute(
        """
        UPDATE accounts
        SET name = 'CommSec', platform = 'CommSec'
        WHERE id = %s
        RETURNING name, platform
        """,
        (COMMSEC_ACCOUNT_ID,),
    )
    row = cur.fetchone()
    if row:
        print(f"  ✅ Account now: name={row[0]!r}, platform={row[1]!r}")
    else:
        print(f"  ⚠ No account found with id {COMMSEC_ACCOUNT_ID} — nothing renamed.")


def upsert_instrument(cur, symbol, display_name, yahoo_ticker, asset_class, currency, dry_run):
    if dry_run:
        print(f"  DRY RUN — would upsert instrument {symbol} ({display_name})")
        return None
    cur.execute(
        """
        insert into instruments (symbol, display_name, yahoo_ticker, asset_class, native_currency)
        values (%s, %s, %s, %s, %s)
        on conflict (symbol) do update set yahoo_ticker = excluded.yahoo_ticker
        returning id
        """,
        (symbol, display_name, yahoo_ticker, asset_class, currency),
    )
    return cur.fetchone()[0]


def register_instruments(cur, dry_run):
    print("→ Registering VAS / VGS / ASIA instruments...")
    inst_ids = {}
    for code, display_name, yahoo_ticker in NEW_INSTRUMENTS:
        symbol = f"ASX:{code}"
        inst_id = upsert_instrument(cur, symbol, display_name, yahoo_ticker, "etf", "AUD", dry_run)
        inst_ids[code] = inst_id
        if not dry_run:
            print(f"  ✅ {symbol} -> instrument id {inst_id}")
    return inst_ids


def get_existing_fingerprints(cur):
    cur.execute(
        """
        SELECT t.tx_date, REPLACE(i.symbol, 'ASX:', ''), t.quantity, t.price, t.tx_type
        FROM transactions t
        JOIN instruments i ON i.id = t.instrument_id
        WHERE t.account_id = %s
        """,
        (COMMSEC_ACCOUNT_ID,),
    )
    fingerprints = set()
    for tx_date, code, qty, price, tx_type in cur.fetchall():
        fingerprints.add((
            tx_date.isoformat() if tx_date else None,
            code,
            round(float(qty), 6) if qty is not None else None,
            round(float(price), 6) if price is not None else None,
            tx_type,
        ))
    return fingerprints


def insert_purchases(cur, inst_ids, dry_run):
    if not PURCHASES:
        print("→ No purchases listed in PURCHASES — edit the script and re-run.")
        return

    print(f"→ Processing {len(PURCHASES)} purchase(s)...")
    existing = set() if dry_run else get_existing_fingerprints(cur)

    for p in PURCHASES:
        code = p["code"]
        qty = float(p["quantity"])
        price = float(p["price"])
        amount = float(p["amount"])
        trade_date = p["trade_date"]

        fingerprint = (trade_date, code, round(qty, 6), round(price, 6), "buy")
        if fingerprint in existing:
            print(f"  ⏭  {code} on {trade_date} already in Postgres — skipping (no duplicate).")
            continue

        if dry_run:
            print(f"  DRY RUN — would insert BUY {code}  qty={qty}  price=${price}  "
                  f"amount=-${amount}  date={trade_date}")
            continue

        inst_id = inst_ids.get(code)
        if inst_id is None:
            print(f"  ⚠ No instrument id for {code} — did register_instruments run first? Skipping.")
            continue

        cur.execute(
            """
            insert into transactions
                (account_id, instrument_id, tx_date, tx_type, quantity, price,
                 amount, fx_rate_to_aud, transfer_group, notes, processed)
            values (%s, %s, %s, 'buy', %s, %s, %s, 1.0, NULL, %s, true)
            """,
            (COMMSEC_ACCOUNT_ID, inst_id, trade_date, qty, price, -abs(amount),
             f"CommSec buy — manual entry via add_commsec_purchase.py"),
        )
        print(f"  ✅ Inserted BUY {code}  qty={qty}  price=${price}  amount=-${amount}  date={trade_date}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = get_pg_conn()
    cur = conn.cursor()

    rebrand_account(cur, args.dry_run)
    inst_ids = register_instruments(cur, args.dry_run)
    insert_purchases(cur, inst_ids, args.dry_run)

    if args.dry_run:
        print("\nDRY RUN — nothing written. Re-run without --dry-run to commit.")
        conn.rollback()
    else:
        conn.commit()
        print("\n✅ Done. Refresh the Streamlit app (clear caches) to see CommSec updated.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
