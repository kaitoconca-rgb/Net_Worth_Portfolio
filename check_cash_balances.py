"""
check_cash_balances.py
─────────────────────────────────────────────────────────────────
Standalone script — run this directly, no Streamlit needed.
Checks what's actually in Postgres right now for the Cash + Super
accounts, so we know whether opening balances were ever migrated,
or whether we're starting from zero.

Usage:
    py -3.14 -m pip install psycopg2-binary
    py -3.14 check_cash_balances.py
"""

import psycopg2

# ── PASTE YOUR REAL CONNECTION STRING HERE ──────────────────────
# Use the pooler string that already worked earlier, with your real
# password substituted in place of the placeholder.
PG_CONN_STRING = "postgresql://postgres.rqaoqweyggtyzycjwxen:ClaKaito2011?@aws-0-ap-northeast-1.pooler.supabase.com:6543/postgres"

CASH_AND_SUPER_ACCOUNTS = {
    "CBA":             "160aa9c5-b55d-466b-b85b-90f37a2e04e1",
    "Me Bank":         "d3dc4451-3b8f-401c-b05f-db6b2666f3d5",
    "Rabobank":        "01d890ac-0c5f-482c-8419-be8ec241701d",
    "Up":              "414ff8f6-6c43-4d8b-921f-fdcf1f57c755",
    "Trade Republic":  "92592fb8-d5d3-4318-9ca8-7bc84c338251",
    "N26 Cash":        "48ca6a9e-373a-4af1-957f-167090b13f45",
    "BPM Cash":        "d5920f95-da3d-4246-af04-7dcb0bc3f46e",
    "BPM Bonds":       "f6be25f1-53a0-453c-a22e-3c49995379ce",
    "C6 Cash":         "2c2cfdd1-67b4-446a-91bb-d2827b630b79",
    "C6 Investments":  "cf6fa923-9c00-4599-bac9-02b3afa6d69d",
    "Mercer Super":    "79b626ee-4563-48c9-975d-ecefc6221fe7",
}

def main():
    conn = psycopg2.connect(PG_CONN_STRING)
    cur = conn.cursor()

    print(f"{'Account':<18} {'# transactions':<16} {'Sum(amount) = balance':<25}")
    print("-" * 60)

    for name, acc_id in CASH_AND_SUPER_ACCOUNTS.items():
        cur.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(amount), 0)
            FROM transactions
            WHERE account_id = %s
            """,
            (acc_id,),
        )
        count, balance = cur.fetchone()
        print(f"{name:<18} {count:<16} {balance:<25}")

    print("\nIf all rows show 0 transactions / 0 balance, opening balances")
    print("were never migrated for Cash/Super — likely because the Sheets")
    print("'Cash' tab read failed silently during migrate_to_postgres.py,")
    print("same root cause as today's dashboard $0 for Cash/Super.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()