"""
rebuild_commsec_history.py  (v2)
─────────────────────────────────────────────────────────────────
Replaces ALL FOUR of the original migration-time placeholder holdings
(TPG, WBC, NHF, TUA) with sourced cost-basis data:

  - TPG / TUA: your CSV shows a 2013 buy of 168 units under "TPM" (TPG
    Telecom's ticker before the 2020 Vodafone Hutchison merger). That
    merger/demerger (ATO Class Ruling CR 2020/41, implemented 13 Jul 2020)
    converted each TPM share into 1 new TPG share + 0.5 Tuas (TUA) share
    — 168 TPM -> 168 TPG + 84 TUA, which matches your current TUA holding
    exactly. Demerger rollover relief means no CGT event at the time, and
    the ORIGINAL cost base + acquisition date (22/11/2013) carries over,
    split between TPG and TUA in proportion to their relative market value
    right after implementation (per TPG's own ASX announcement, 17 Jul
    2020: TPG $8.2603/share, TUA $0.6799/share). This script computes that
    split rather than hardcoding a guess — see the numbers printed when
    you run it.
  - NHF: nib Holdings demutualised and listed on the ASX in Nov 2007;
    eligible policyholders (that's you, per your health cover) received
    free shares. The ATO has since confirmed a cost base of $0.85/share
    for those shares (nib's own announcement — see SOURCES below). Your
    300 units get that cost base, dated to the Nov 2007 listing.
  - WBC: your CSV confirms 65 units (27 in 2013 + 38 in 2015). Your live
    CommSec balance says 67 — you said you were "probably awarded" the
    other 2, without knowing the source. This script adds those 2 units
    with a $0.00 placeholder cost basis, explicitly flagged
    "[UNCONFIRMED]" in both the database and the app's Lot Detail table.
    If you find what created them (bonus issue, DRP, priority offer),
    replace that one entry with the real figures.

SOURCES (fetched at the time this script was written — re-verify before
relying on this for an actual tax return):
  - nib cost base: https://www.nib.com.au/docs/cgt-cost-base-of-nib-shares-confirmed
  - TPG/TUA market values: https://www.tpg.com.au/sites/tpg/files/investors/TPMAnnouncementreMktValues17July20FINAL.pdf
  - ATO Class Ruling CR 2020/41 (TPG Telecom scheme of arrangement)

WHAT THIS DOES NOT TOUCH:
  - VAS / VGS / ASIA — already correct from add_commsec_purchase.py.

DISCLAIMER: this is not tax advice. The TPG/TUA cost-base split and the
NHF acquisition date are computed from published, sourced figures, but
you should have your accountant confirm before filing on this basis —
especially the demerger apportionment, which the ATO ruling may specify
more precisely than the market-value-ratio method used here.

HOW TO USE:
    py -3.14 -m pip install psycopg2-binary
    py -3.14 rebuild_commsec_history.py --dry-run     # preview only
    py -3.14 rebuild_commsec_history.py               # commit
"""

import argparse

import psycopg2

# ── CONFIG ───────────────────────────────────────────────────────────────
PG_CONN_STRING = "postgresql://postgres.rqaoqweyggtyzycjwxen:ClaKaito2011?@aws-0-ap-northeast-1.pooler.supabase.com:6543/postgres"
COMMSEC_ACCOUNT_ID = "d11dbbea-8a63-42da-9329-ab85ec00bea8"

# All four originally-placeholder codes now have sourced data — wipe their
# existing transaction(s) before re-importing.
CODES_TO_REPLACE = ["TPG", "WBC", "NHF", "TUA"]

NEW_INSTRUMENT_NAMES = {
    "SUL": "Super Retail Group",
    "ORG": "Origin Energy",
    "WPL": "Woodside Petroleum",   # sold 2015, well before the 2022 Woodside Energy rename
    "NAB": "National Australia Bank",
    "TOL": "Toll Holdings",
    "WES": "Wesfarmers",
}
TICKER_RENAMES = {"TPM": "TPG"}  # old ticker -> current instrument code

# ── TPM -> TPG + TUA demerger split (computed, not hardcoded) ──────────────
TPM_QTY, TPM_COST, TPM_DATE = 168, 717.99, "2013-11-22"
NEW_TPG_QTY = TPM_QTY                 # 1 new TPG share per TPM share
NEW_TUA_QTY = TPM_QTY // 2            # 1 TUA share per 2 TPM shares -> 84
TPG_MV, TUA_MV = 8.2603, 0.6799       # per-share market values, 17 Jul 2020 ASX announcement
_tpg_mv_total = NEW_TPG_QTY * TPG_MV
_tua_mv_total = NEW_TUA_QTY * TUA_MV
_total_mv = _tpg_mv_total + _tua_mv_total
TPG_COST_BASE = TPM_COST * (_tpg_mv_total / _total_mv)
TUA_COST_BASE = TPM_COST * (_tua_mv_total / _total_mv)
TPG_PRICE = TPG_COST_BASE / NEW_TPG_QTY
TUA_PRICE = TUA_COST_BASE / NEW_TUA_QTY

# ── NHF demutualisation ─────────────────────────────────────────────────────
NHF_QTY, NHF_PRICE, NHF_DATE = 300, 0.85, "2007-11-01"

# ── WBC mystery 2 units ──────────────────────────────────────────────────────
WBC_UNKNOWN_QTY, WBC_UNKNOWN_PRICE, WBC_UNKNOWN_DATE = 2, 0.0, "2015-10-19"

# Trades transcribed straight from ConfirmationDetails_1.csv (VERIFIED).
# amount: negative = cash out (buy), positive = cash in (sell).
CSV_TRADES = [
    {"code": "SUL", "side": "sell", "qty": 120, "price": 13.646, "amount": 1627.46, "date": "2023-11-17", "conf": "147487519"},
    {"code": "ORG", "side": "sell", "qty": 178, "price": 7.410,  "amount": 1299.03, "date": "2019-04-18", "conf": "89564317"},
    {"code": "ORG", "side": "buy",  "qty": 115, "price": 5.550,  "amount": 658.20,  "date": "2015-11-02", "conf": "69324827"},
    {"code": "WBC", "side": "buy",  "qty": 38,  "price": 31.305, "amount": 1209.54, "date": "2015-10-19", "conf": "69145049"},
    {"code": "WPL", "side": "sell", "qty": 22,  "price": 28.340, "amount": 603.53,  "date": "2015-09-15", "conf": "68716932"},
    {"code": "NAB", "side": "sell", "qty": 26,  "price": 34.500, "amount": 877.05,  "date": "2015-07-22", "conf": "67913969"},
    {"code": "TOL", "side": "sell", "qty": 141, "price": 5.710,  "amount": 785.16,  "date": "2013-10-21", "conf": "60670702"},
    {"code": "SUL", "side": "buy",  "qty": 53,  "price": 14.000, "amount": 761.95,  "date": "2013-10-21", "conf": "60671301"},
    {"code": "SUL", "side": "buy",  "qty": 67,  "price": 12.400, "amount": 850.75,  "date": "2013-10-09", "conf": "60532179"},
    {"code": "WES", "side": "sell", "qty": 19,  "price": 41.200, "amount": 762.85,  "date": "2013-09-30", "conf": "60443052"},
    {"code": "TOL", "side": "buy",  "qty": 141, "price": 5.900,  "amount": 842.90,  "date": "2013-03-15", "conf": "58231475"},
    {"code": "WPL", "side": "buy",  "qty": 22,  "price": 36.690, "amount": 818.18,  "date": "2013-03-15", "conf": "58231478"},
    {"code": "WES", "side": "buy",  "qty": 19,  "price": 42.890, "amount": 825.91,  "date": "2013-03-15", "conf": "58231479"},
    {"code": "WBC", "side": "buy",  "qty": 27,  "price": 30.640, "amount": 838.28,  "date": "2013-03-15", "conf": "58231481"},
    {"code": "ORG", "side": "buy",  "qty": 63,  "price": 13.090, "amount": 835.67,  "date": "2013-03-15", "conf": "58231473"},
    {"code": "NAB", "side": "buy",  "qty": 26,  "price": 30.980, "amount": 816.48,  "date": "2013-03-15", "conf": "58231483"},
]


def build_trades():
    """Assemble the final TRADES list: CSV rows + the two derived/unconfirmed entries."""
    trades = []
    for t in CSV_TRADES:
        note = f"[VERIFIED] CommSec confirmation #{t['conf']}"
        trades.append({**t, "note": note})

    trades.append({
        "code": "TPG", "side": "buy", "qty": NEW_TPG_QTY, "price": round(TPG_PRICE, 6),
        "amount": 0.0, "date": TPM_DATE,
        "note": (f"[DERIVED] Demerger from TPM (168 units, {TPM_DATE}, orig. cost ${TPM_COST}) "
                 f"per ATO CR 2020/41 — cost base split {_tpg_mv_total/_total_mv*100:.2f}% TPG / "
                 f"{_tua_mv_total/_total_mv*100:.2f}% TUA by relative market value at implementation."),
    })
    trades.append({
        "code": "TUA", "side": "buy", "qty": NEW_TUA_QTY, "price": round(TUA_PRICE, 6),
        "amount": 0.0, "date": TPM_DATE,
        "note": (f"[DERIVED] Demerger from TPM (168 units, {TPM_DATE}, orig. cost ${TPM_COST}) "
                 f"per ATO CR 2020/41 — 1 TUA per 2 TPM held. Acquisition date carried over from "
                 f"original TPM purchase under demerger rollover relief."),
    })
    trades.append({
        "code": "NHF", "side": "buy", "qty": NHF_QTY, "price": NHF_PRICE,
        "amount": 0.0, "date": NHF_DATE,
        "note": ("[DERIVED] nib demutualisation, Nov 2007 — free shares to eligible "
                 "policyholders. Cost base $0.85/share per ATO-confirmed determination "
                 "(nib CGT cost base announcement). Exact day of Nov 2007 not critical — "
                 "either way it's held well past the 12-month CGT discount threshold."),
    })
    trades.append({
        "code": "WBC", "side": "buy", "qty": WBC_UNKNOWN_QTY, "price": WBC_UNKNOWN_PRICE,
        "amount": 0.0, "date": WBC_UNKNOWN_DATE,
        "note": ("[UNCONFIRMED] 2 units your live CommSec balance shows beyond the CSV-confirmed "
                 "65 (27+38). Origin/price/date unknown — you said 'probably awarded'. Placeholder "
                 "$0.00 cost, date reused from your last known real WBC buy. Replace this row once "
                 "you find the actual source (bonus issue / DRP / priority offer)."),
    })
    return trades


def get_pg_conn():
    return psycopg2.connect(PG_CONN_STRING)


def preview_and_clear_existing(cur, dry_run):
    print(f"→ Checking existing transactions for {CODES_TO_REPLACE}...")
    cur.execute(
        """
        SELECT t.id, REPLACE(i.symbol, 'ASX:', ''), t.tx_date, t.tx_type, t.quantity, t.price, t.notes
        FROM transactions t
        JOIN instruments i ON i.id = t.instrument_id
        WHERE t.account_id = %s AND REPLACE(i.symbol, 'ASX:', '') = ANY(%s)
        """,
        (COMMSEC_ACCOUNT_ID, CODES_TO_REPLACE),
    )
    rows = cur.fetchall()
    if not rows:
        print("  (nothing to clear)")
        return
    for r in rows:
        print(f"  existing: id={r[0]} {r[1]} {r[2]} {r[3]} qty={r[4]} price={r[5]} notes={r[6]!r}")

    if dry_run:
        print(f"  DRY RUN — would DELETE these {len(rows)} row(s) before re-importing.")
        return

    ids = [str(r[0]) for r in rows]
    cur.execute("DELETE FROM transactions WHERE id = ANY(%s::uuid[])", (ids,))
    print(f"  ✅ Deleted {len(rows)} old row(s) for {CODES_TO_REPLACE}.")


def get_or_create_instrument(cur, code, dry_run):
    mapped_code = TICKER_RENAMES.get(code, code)
    symbol = f"ASX:{mapped_code}"
    cur.execute("SELECT id FROM instruments WHERE symbol = %s", (symbol,))
    row = cur.fetchone()
    if row:
        return row[0]

    display_name = NEW_INSTRUMENT_NAMES.get(mapped_code, mapped_code)
    if dry_run:
        print(f"  DRY RUN — would create new instrument {symbol} ({display_name})")
        return None

    cur.execute(
        """
        insert into instruments (symbol, display_name, yahoo_ticker, asset_class, native_currency)
        values (%s, %s, %s, 'share', 'AUD')
        on conflict (symbol) do update set display_name = excluded.display_name
        returning id
        """,
        (symbol, display_name, f"{mapped_code}.AX"),
    )
    new_id = cur.fetchone()[0]
    print(f"  ✅ Created instrument {symbol} -> id {new_id}")
    return new_id


def import_trades(cur, trades, dry_run):
    print(f"→ Importing {len(trades)} trades (CSV-verified + derived + flagged)...")
    for t in trades:
        inst_id = get_or_create_instrument(cur, t["code"], dry_run)
        signed_qty = -abs(t["qty"]) if t["side"] == "sell" else abs(t["qty"])
        signed_amt = abs(t["amount"]) if t["side"] == "sell" else -abs(t["amount"])

        if dry_run:
            mapped_code = TICKER_RENAMES.get(t["code"], t["code"])
            print(f"  DRY RUN — {t['note'][:11]:11s} {t['side'].upper():4s} {mapped_code:5s} "
                  f"qty={signed_qty:<6}  price=${t['price']:<10.6f}  date={t['date']}")
            continue

        cur.execute(
            """
            insert into transactions
                (account_id, instrument_id, tx_date, tx_type, quantity, price,
                 amount, fx_rate_to_aud, transfer_group, notes, processed)
            values (%s, %s, %s, %s, %s, %s, %s, 1.0, NULL, %s, true)
            """,
            (COMMSEC_ACCOUNT_ID, inst_id, t["date"], t["side"], signed_qty, t["price"], signed_amt, t["note"]),
        )
    if not dry_run:
        print(f"  ✅ Inserted {len(trades)} trades.")


def print_reconciliation(trades):
    net = {}
    for t in trades:
        code = TICKER_RENAMES.get(t["code"], t["code"])
        signed = -t["qty"] if t["side"] == "sell" else t["qty"]
        net[code] = net.get(code, 0) + signed
    print("\n→ Net position per code after this import:")
    for code, qty in sorted(net.items()):
        flag = "  <- fully exited, kept for CGT history" if qty == 0 else ""
        print(f"  {code:6s} {qty:+6.0f}{flag}")
    print(f"\n→ TPG/TUA cost-base split: TPG {_tpg_mv_total/_total_mv*100:.2f}% "
          f"(${TPG_COST_BASE:.2f} / {NEW_TPG_QTY} units = ${TPG_PRICE:.4f}/share), "
          f"TUA {_tua_mv_total/_total_mv*100:.2f}% (${TUA_COST_BASE:.2f} / {NEW_TUA_QTY} units "
          f"= ${TUA_PRICE:.4f}/share). Check these sum back to ${TPG_COST_BASE + TUA_COST_BASE:.2f} "
          f"≈ original ${TPM_COST}.")
    print("\n⚠ Everything is now sourced except the 2 mystery WBC units — still [UNCONFIRMED].")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    trades = build_trades()

    conn = get_pg_conn()
    cur = conn.cursor()

    preview_and_clear_existing(cur, args.dry_run)
    import_trades(cur, trades, args.dry_run)
    print_reconciliation(trades)

    if args.dry_run:
        print("\nDRY RUN — nothing written. Re-run without --dry-run to commit.")
        conn.rollback()
    else:
        conn.commit()
        print("\n✅ Done. Refresh the Streamlit app (clear caches) to see the CommSec Lot Detail table update.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
