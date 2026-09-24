"""Cost base of holdings and realised gains, in AUD, for the accountant.

Sep 2026. Two sources:

1. Trade ledgers already in `transactions` (N26 ETFs/funds, CommSec...):
   buys and sells are matched first-in-first-out per security, and each side
   is converted to AUD at the rate stored on the transaction (Reserve Bank
   rate on the trade date).

2. `tax_lots` for holdings that have no trade ledger - Italian BTPs at BPM,
   inherited assets, gifts: one row per parcel with how and when it was
   acquired, its cost (for an inheritance, the market value at the date of
   death), and - once sold or matured - the proceeds.

Everything is converted with Reserve Bank rates: acquisition at the rate on
the acquisition date, disposal at the rate on the disposal date. Not tax
advice: the pack flags items (inheritances, bonds) the accountant should
check.
"""
from datetime import date

import pandas as pd
import streamlit as st
from sqlalchemy import text as sql_text

ACQ_TYPES = {"purchase": "Bought", "inheritance": "Inherited", "gift": "Gift", "other": "Other"}
DISP_TYPES = {"sale": "Sold", "maturity": "Matured / redeemed", "other": "Other"}

SCHEMA_STMTS = (
    """CREATE TABLE IF NOT EXISTS tax_lots (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        account_id uuid REFERENCES accounts(id),
        asset text NOT NULL,
        isin text,
        quantity numeric NOT NULL,
        currency text NOT NULL DEFAULT 'EUR',
        acquired_on date NOT NULL,
        acquisition_type text NOT NULL DEFAULT 'purchase',
        cost_native numeric NOT NULL,
        fx_rate_to_aud numeric,
        disposed_on date,
        disposal_type text,
        proceeds_native numeric,
        disposal_fx_rate_to_aud numeric,
        notes text,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
)


def ensure_lots_schema(conn):
    with conn.session as s:
        s.execute(sql_text("SET LOCAL lock_timeout = '5s'"))
        for stmt in SCHEMA_STMTS:
            s.execute(sql_text(stmt))
        s.commit()


def fy_label(d):
    d = pd.Timestamp(d)
    return f"FY{str(d.year + 1 if d.month >= 7 else d.year)[-2:]}"


@st.cache_data(ttl=60, show_spinner=False)
def load_lots(_conn, version=0):
    df = _conn.query(
        """
        SELECT l.id::text AS id, a.name AS account, l.asset, l.isin, l.quantity, l.currency,
               l.acquired_on, l.acquisition_type, l.cost_native, l.fx_rate_to_aud,
               l.disposed_on, l.disposal_type, l.proceeds_native, l.disposal_fx_rate_to_aud, l.notes
        FROM tax_lots l LEFT JOIN accounts a ON a.id = l.account_id
        ORDER BY l.acquired_on, l.asset
        """, ttl=0)
    for c in ("quantity", "cost_native", "fx_rate_to_aud", "proceeds_native", "disposal_fx_rate_to_aud"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    return df


@st.cache_data(ttl=60, show_spinner=False)
def load_trades(_conn, version=0):
    return _conn.query(
        """
        SELECT a.name AS account, a.currency AS acc_ccy, i.symbol AS isin,
               COALESCE(i.display_name, i.symbol) AS asset, t.tx_date, t.tx_type,
               t.quantity, t.amount, t.fx_rate_to_aud
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        JOIN instruments i ON i.id = t.instrument_id
        WHERE t.tx_type IN ('buy', 'sell') AND t.quantity IS NOT NULL AND t.quantity <> 0
          AND a.category = 'investment' AND upper(a.currency) <> 'AUD'
        ORDER BY t.tx_date, t.created_at
        """, ttl=0)


def fifo_disposals(trades, aud_rate_on):
    """Match sells to earlier buys (FIFO) per account+security. One row per matched piece."""
    out = []
    if trades is None or trades.empty:
        return pd.DataFrame(out)
    t = trades.copy()
    t["quantity"] = pd.to_numeric(t["quantity"], errors="coerce").astype(float)
    t["amount"] = pd.to_numeric(t["amount"], errors="coerce").astype(float)
    t["fx"] = pd.to_numeric(t["fx_rate_to_aud"], errors="coerce").astype(float)
    for (acc, isin), g in t.groupby(["account", "isin"], sort=False):
        lots = []  # [qty_left, unit_cost_native, fx, date]
        for _, r in g.iterrows():
            fx = r["fx"] if pd.notna(r["fx"]) else aud_rate_on(r["acc_ccy"], r["tx_date"])
            qty = abs(r["quantity"])
            if r["tx_type"] == "buy":
                lots.append([qty, abs(r["amount"]) / qty if qty else 0.0, fx, r["tx_date"]])
                continue
            unit_proceeds = abs(r["amount"]) / qty if qty else 0.0
            remaining = qty
            while remaining > 1e-9 and lots:
                lot = lots[0]
                take = min(lot[0], remaining)
                cost_n, proc_n = take * lot[1], take * unit_proceeds
                out.append({
                    "Source": f"{acc} trades", "Asset": r["asset"], "ISIN": isin,
                    "Acquired": pd.to_datetime(lot[3]).date(), "How acquired": "Bought",
                    "Disposed": pd.to_datetime(r["tx_date"]).date(), "How disposed": "Sold",
                    "Quantity": take, "Currency": r["acc_ccy"],
                    "Cost": cost_n, "Cost FX": lot[2], "Cost A$": cost_n * lot[2],
                    "Proceeds": proc_n, "Proceeds FX": fx, "Proceeds A$": proc_n * fx,
                })
                lot[0] -= take
                remaining -= take
                if lot[0] <= 1e-9:
                    lots.pop(0)
            if remaining > 1e-6:
                out.append({"Source": f"{acc} trades", "Asset": r["asset"], "ISIN": isin, "Acquired": None,
                            "How acquired": "UNKNOWN - no matching purchase",
                            "Disposed": pd.to_datetime(r["tx_date"]).date(), "How disposed": "Sold",
                            "Quantity": remaining, "Currency": r["acc_ccy"], "Cost": None, "Cost FX": None,
                            "Cost A$": None, "Proceeds": remaining * unit_proceeds, "Proceeds FX": fx,
                            "Proceeds A$": remaining * unit_proceeds * fx})
    return pd.DataFrame(out)


def lot_disposals(lots, aud_rate_on):
    d = lots[lots["disposed_on"].notna()].copy()
    rows = []
    for _, r in d.iterrows():
        cfx = r["fx_rate_to_aud"] if pd.notna(r["fx_rate_to_aud"]) else aud_rate_on(r["currency"], r["acquired_on"])
        pfx = (r["disposal_fx_rate_to_aud"] if pd.notna(r["disposal_fx_rate_to_aud"])
               else aud_rate_on(r["currency"], r["disposed_on"]))
        rows.append({
            "Source": r["account"] or "Tax lots", "Asset": r["asset"], "ISIN": r["isin"],
            "Acquired": pd.to_datetime(r["acquired_on"]).date(),
            "How acquired": ACQ_TYPES.get(r["acquisition_type"], r["acquisition_type"]),
            "Disposed": pd.to_datetime(r["disposed_on"]).date(),
            "How disposed": DISP_TYPES.get(r["disposal_type"], r["disposal_type"] or "Other"),
            "Quantity": r["quantity"], "Currency": r["currency"],
            "Cost": r["cost_native"], "Cost FX": cfx, "Cost A$": r["cost_native"] * cfx,
            "Proceeds": r["proceeds_native"], "Proceeds FX": pfx, "Proceeds A$": (r["proceeds_native"] or 0) * pfx,
            "Notes": r["notes"],
        })
    return pd.DataFrame(rows)


def realised_gains(conn, aud_rate_on):
    df = pd.concat([fifo_disposals(load_trades(conn), aud_rate_on),
                    lot_disposals(load_lots(conn), aud_rate_on)], ignore_index=True)
    if df.empty:
        return df
    df["Gain / (loss) A$"] = df["Proceeds A$"] - df["Cost A$"]
    df["Held > 12 months"] = [
        (pd.Timestamp(dsp) - pd.Timestamp(acq)).days > 365 if acq is not None and pd.notna(acq) else None
        for acq, dsp in zip(df["Acquired"], df["Disposed"])]
    df["FY"] = df["Disposed"].map(fy_label)
    return df.sort_values("Disposed")


# ─────────────────────────────── UI ──────────────────────────────────────────

def render_lots_page(conn, accounts_df, aud_rate_on):
    st.header("🧾 Cost base & capital gains")
    st.caption("What each holding cost you in AUD, and the AUD gain or loss when it's sold or matures - "
               "at Reserve Bank rates on each date. Trades in N26 are matched automatically (first in, first "
               "out). Holdings without a trade history - BTPs at BPM, inherited or gifted assets - are "
               "recorded here as parcels.")
    tab_g, tab_h, tab_add, tab_disp = st.tabs(
        ["Realised gains by year", "Parcels held", "➕ Add a parcel", "✅ Record a sale / maturity"])

    with tab_g:
        g = realised_gains(conn, aud_rate_on)
        if g.empty:
            st.info("No sales or maturities recorded yet.")
        else:
            fys = sorted(g["FY"].unique(), reverse=True)
            sel = st.selectbox("Financial year", fys, key="cg_fy")
            f = g[g["FY"] == sel]
            m1, m2, m3 = st.columns(3)
            m1.metric("Gains (A$)", f"{f.loc[f['Gain / (loss) A$'] > 0, 'Gain / (loss) A$'].sum():,.2f}")
            m2.metric("Losses (A$)", f"{f.loc[f['Gain / (loss) A$'] < 0, 'Gain / (loss) A$'].sum():,.2f}")
            m3.metric("Net (A$)", f"{f['Gain / (loss) A$'].sum():,.2f}")
            cols = ["Asset", "ISIN", "How acquired", "Acquired", "How disposed", "Disposed", "Quantity",
                    "Currency", "Cost", "Cost FX", "Cost A$", "Proceeds", "Proceeds FX", "Proceeds A$",
                    "Gain / (loss) A$", "Held > 12 months", "Source"]
            st.dataframe(f[cols].style.format({
                "Quantity": "{:,.4f}", "Cost": "{:,.2f}", "Cost FX": "{:.4f}", "Cost A$": "{:,.2f}",
                "Proceeds": "{:,.2f}", "Proceeds FX": "{:.4f}", "Proceeds A$": "{:,.2f}",
                "Gain / (loss) A$": "{:,.2f}"}, na_rep="-"), use_container_width=True, hide_index=True)
            if f["How acquired"].str.startswith("UNKNOWN").any():
                st.warning("Some sales have no matching purchase in the app - add the purchase so the cost is known.")
            if (f["How acquired"] == "Inherited").any() or f["ISIN"].fillna("").str.startswith("IT").any():
                st.caption("For your accountant: inherited parcels use the market value at the date of death as "
                           "cost; for bonds, whether the AUD result is a capital gain or a foreign-exchange / "
                           "traditional-security gain is for them to decide.")
            st.download_button(f"⬇️ Download {sel} (CSV)", f[cols + ["FY"]].to_csv(index=False).encode("utf-8"),
                               file_name=f"capital_gains_{sel}.csv", mime="text/csv", key="cg_csv")

    lots = load_lots(conn)
    with tab_h:
        open_lots = lots[lots["disposed_on"].isna()]
        if open_lots.empty:
            st.info("No parcels recorded. Add BTPs or other holdings without a trade history in '➕ Add a parcel'.")
        else:
            v = open_lots.copy()
            v["Cost A$"] = [c * (fx if pd.notna(fx) else aud_rate_on(ccy, d)) for c, fx, ccy, d in
                            zip(v["cost_native"], v["fx_rate_to_aud"], v["currency"], v["acquired_on"])]
            v["How acquired"] = v["acquisition_type"].map(ACQ_TYPES)
            st.dataframe(v[["account", "asset", "isin", "quantity", "currency", "How acquired", "acquired_on",
                            "cost_native", "fx_rate_to_aud", "Cost A$", "notes"]].rename(columns={
                                "account": "Account", "asset": "Asset", "isin": "ISIN", "quantity": "Quantity / face",
                                "currency": "Ccy", "acquired_on": "Acquired", "cost_native": "Cost",
                                "fx_rate_to_aud": "AUD rate", "notes": "Notes"}).style.format(
                {"Quantity / face": "{:,.2f}", "Cost": "{:,.2f}", "AUD rate": "{:.4f}", "Cost A$": "{:,.2f}"},
                na_rep="-"), use_container_width=True, hide_index=True)

    inv_accounts = accounts_df[accounts_df["category"].isin(("bonds", "investment", "cash", "savings"))]
    acc_names = list(inv_accounts["name"])
    with tab_add:
        with st.form("lot_add_form", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            acc = c1.selectbox("Held in", acc_names,
                               index=acc_names.index("BPM Bonds") if "BPM Bonds" in acc_names else 0)
            asset = c1.text_input("Asset", placeholder="e.g. BTP 3.85% 2029")
            isin = c1.text_input("ISIN", placeholder="IT0005...")
            how = c2.selectbox("How acquired", list(ACQ_TYPES), format_func=ACQ_TYPES.get)
            acq_date = c2.date_input("Date acquired", value=date.today(),
                                     help="For an inheritance: the date of death.")
            ccy = c2.selectbox("Currency", ["EUR", "AUD", "USD", "GBP"])
            qty = c3.number_input("Quantity or face value", min_value=0.0, step=1000.0, format="%.4f")
            price = c3.number_input("Price (% of face for bonds, or per unit)", min_value=0.0, value=100.0,
                                    step=0.01, format="%.4f",
                                    help="For an inheritance: the market price at the date of death.")
            is_bond = c3.checkbox("Price is % of face value (bonds)", value=True)
            notes = st.text_input("Notes", placeholder="e.g. inherited from father's estate, 1/3 share")
            if st.form_submit_button("Add parcel", type="primary"):
                if not asset.strip() or qty <= 0:
                    st.warning("Enter the asset and a quantity.")
                else:
                    cost = qty * price / 100 if is_bond else qty * price
                    fx = aud_rate_on(ccy, acq_date)
                    with conn.session as s:
                        s.execute(sql_text("""
                            INSERT INTO tax_lots (account_id, asset, isin, quantity, currency, acquired_on,
                                acquisition_type, cost_native, fx_rate_to_aud, notes)
                            VALUES ((SELECT id FROM accounts WHERE name = :acc), :asset, :isin, :qty, :ccy, :d,
                                :how, :cost, :fx, :notes)
                        """), {"acc": acc, "asset": asset.strip(), "isin": isin.strip() or None, "qty": qty,
                               "ccy": ccy, "d": acq_date, "how": how, "cost": round(cost, 2), "fx": fx,
                               "notes": notes.strip() or None})
                        s.commit()
                    load_lots.clear()
                    st.success(f"Added {asset.strip()}: cost {cost:,.2f} {ccy} = A${cost * fx:,.2f} "
                               f"(RBA {fx:.4f}).")
                    st.rerun()

    with tab_disp:
        open_lots = lots[lots["disposed_on"].isna()]
        if open_lots.empty:
            st.info("No open parcels.")
        else:
            labels = {r["id"]: f"{r['asset']} - {r['quantity']:,.2f} {r['currency']} ({ACQ_TYPES.get(r['acquisition_type'])} "
                               f"{pd.to_datetime(r['acquired_on']):%d %b %Y})" for _, r in open_lots.iterrows()}
            with st.form("lot_dispose_form", clear_on_submit=True):
                lot_id = st.selectbox("Parcel", list(labels), format_func=labels.get)
                c1, c2 = st.columns(2)
                how = c1.selectbox("What happened", list(DISP_TYPES), format_func=DISP_TYPES.get, index=1)
                d = c1.date_input("Date", value=date.today())
                proceeds = c2.number_input("Proceeds received (whole parcel, before fees)", min_value=0.0,
                                           step=100.0, format="%.2f",
                                           help="For a bond redeemed at par: the face value.")
                if st.form_submit_button("Record", type="primary"):
                    r = open_lots[open_lots["id"] == lot_id].iloc[0]
                    fx = aud_rate_on(r["currency"], d)
                    with conn.session as s:
                        s.execute(sql_text("""
                            UPDATE tax_lots SET disposed_on = :d, disposal_type = :how, proceeds_native = :p,
                                disposal_fx_rate_to_aud = :fx
                            WHERE id = CAST(:id AS uuid)
                        """), {"d": d, "how": how, "p": proceeds, "fx": fx, "id": lot_id})
                        s.commit()
                    load_lots.clear()
                    st.success("Recorded. See 'Realised gains by year'.")
                    st.rerun()
