"""Scrape Sowears product prices and save a dated snapshot to SQLite."""
import sqlite3
import time
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "processed" / "prices.db"
STORE = "https://sowears.net"
HEADERS = {"User-Agent": "portfolio-price-tracker (learning project)"}


def fetch_products():
    products = []
    for page in range(1, 21):
        resp = requests.get(
            f"{STORE}/products.json",
            params={"limit": 250, "page": page},
            headers=HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()["products"]
        if not batch:
            break
        products.extend(batch)
        time.sleep(2)
    return products


def to_rows(products):
    today = date.today().isoformat()
    items, snaps = [], []
    for p in products:
        variants = [v for v in (p.get("variants") or []) if v.get("price")]
        if not variants:
            continue
        cheapest = min(variants, key=lambda v: float(v["price"]))
        price = float(cheapest["price"])
        compare = float(cheapest["compare_at_price"]) if cheapest.get("compare_at_price") else None
        in_stock = int(any(v.get("available") for v in variants))
        ptype = (p.get("product_type") or "").strip().title() or "Uncategorized"
        items.append((p["id"], p["title"].strip(), ptype, p["handle"]))
        snaps.append((today, p["id"], price, compare, in_stock))
    return items, snaps


def save(items, snaps):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS store_products (
                product_id INTEGER PRIMARY KEY,
                title TEXT, product_type TEXT, handle TEXT)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS store_price_snapshots (
                snapshot_date TEXT, product_id INTEGER,
                price REAL, compare_at_price REAL, in_stock INTEGER,
                PRIMARY KEY (snapshot_date, product_id))"""
        )
        conn.executemany("INSERT OR REPLACE INTO store_products VALUES (?,?,?,?)", items)
        conn.executemany("INSERT OR REPLACE INTO store_price_snapshots VALUES (?,?,?,?,?)", snaps)
        conn.commit()
    finally:
        conn.close()


def main():
    products = fetch_products()
    if len(products) < 100:  # safety: don't save a broken or partial fetch
        raise RuntimeError(f"Only {len(products)} products fetched, expected hundreds. Not saving.")
    items, snaps = to_rows(products)
    save(items, snaps)
    print(f"Saved {len(snaps)} products for {snaps[0][0]}")


if __name__ == "__main__":
    main()