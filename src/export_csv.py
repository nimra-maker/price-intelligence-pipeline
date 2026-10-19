"""Export the price history to CSV files for Power BI."""
import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "processed" / "prices.db"
OUT_DIR = ROOT / "data" / "export"

QUERIES = {
    "price_history": """
        SELECT s.snapshot_date, s.product_id, p.title, p.product_type, p.handle,
               s.price, s.compare_at_price, s.in_stock
        FROM store_price_snapshots s
        JOIN store_products p ON p.product_id = s.product_id""",
    "price_changes": """
        WITH h AS (
            SELECT snapshot_date, product_id, price,
                   LAG(price) OVER (PARTITION BY product_id ORDER BY snapshot_date) AS prev_price
            FROM store_price_snapshots)
        SELECT h.snapshot_date, h.product_id, p.title, p.product_type,
               h.prev_price AS old_price, h.price AS new_price,
               ROUND(100.0 * (h.price - h.prev_price) / h.prev_price, 1) AS change_pct
        FROM h JOIN store_products p ON p.product_id = h.product_id
        WHERE h.prev_price IS NOT NULL AND h.price <> h.prev_price""",
    "stock_changes": """
        WITH h AS (
            SELECT snapshot_date, product_id, in_stock,
                   LAG(in_stock) OVER (PARTITION BY product_id ORDER BY snapshot_date) AS prev_stock
            FROM store_price_snapshots)
        SELECT h.snapshot_date, h.product_id, p.title, p.product_type,
               CASE WHEN h.in_stock = 0 THEN 'Sold out' ELSE 'Back in stock' END AS event
        FROM h JOIN store_products p ON p.product_id = h.product_id
        WHERE h.prev_stock IS NOT NULL AND h.in_stock <> h.prev_stock""",
    "new_arrivals": """
        SELECT p.product_id, p.title, p.product_type, f.first_seen, s.price
        FROM (SELECT product_id, MIN(snapshot_date) AS first_seen
              FROM store_price_snapshots GROUP BY product_id) f
        JOIN store_products p ON p.product_id = f.product_id
        JOIN store_price_snapshots s
             ON s.product_id = f.product_id AND s.snapshot_date = f.first_seen
        WHERE f.first_seen > (SELECT MIN(snapshot_date) FROM store_price_snapshots)""",
    "daily_summary": """
        SELECT snapshot_date, COUNT(*) AS products,
               ROUND(AVG(price), 0) AS avg_price, SUM(in_stock = 0) AS sold_out
        FROM store_price_snapshots
        GROUP BY snapshot_date ORDER BY snapshot_date""",
}


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        for name, query in QUERIES.items():
            df = pd.read_sql(query, conn)
            df.to_csv(OUT_DIR / f"{name}.csv", index=False, encoding="utf-8-sig")
            print(f"{name}.csv: {len(df)} rows")
    finally:
        conn.close()


if __name__ == "__main__":
    main()