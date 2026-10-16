"""Pricing report: SQL facts -> LLM summary -> number check -> saved report."""
import json
import os
import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "processed" / "prices.db"
REPORTS_DIR = ROOT / "reports"

load_dotenv(ROOT / ".env")
API_KEY = os.getenv("GROQ_API_KEY")
MODEL = os.getenv("GROQ_MODEL")

QUERIES = {
    "overall": """
        SELECT COUNT(*) AS total_products,
               SUM(discount_pct > 0) AS discounted_products,
               ROUND(100.0 * SUM(discount_pct > 0) / COUNT(*), 1) AS pct_discounted,
               ROUND(AVG(CASE WHEN discount_pct > 0 THEN discount_pct END), 1) AS avg_discount_when_discounted
        FROM price_snapshots;""",
    "categories": """
        SELECT p.category, COUNT(*) AS products,
               ROUND(100.0 * SUM(s.discount_pct > 0) / COUNT(*), 1) AS pct_discounted,
               ROUND(AVG(CASE WHEN s.discount_pct > 0 THEN s.discount_pct END), 1) AS avg_discount_when_discounted
        FROM products p JOIN price_snapshots s USING (product_id)
        GROUP BY p.category ORDER BY avg_discount_when_discounted DESC;""",
    "top_brands": """
        SELECT p.brand, COUNT(*) AS products, ROUND(AVG(s.discount_pct), 1) AS avg_discount_pct
        FROM products p JOIN price_snapshots s USING (product_id)
        GROUP BY p.brand HAVING COUNT(*) >= 20
        ORDER BY avg_discount_pct DESC LIMIT 10;""",
}


def get_facts():
    conn = sqlite3.connect(DB_PATH)
    try:
        return {
            name: json.loads(pd.read_sql(sql, conn).to_json(orient="records"))
            for name, sql in QUERIES.items()
        }
    finally:
        conn.close()


def generate_summary(facts):
    if not API_KEY or not MODEL:
        raise RuntimeError("Set GROQ_API_KEY and GROQ_MODEL in your .env file")
    system_msg = (
        "You are a retail pricing analyst. Use ONLY the numbers in the data provided. "
        "Do not invent figures, causes or trends over time. The data is a single price snapshot."
    )
    user_msg = (
        "Write a 5-sentence summary for a business manager, then 2 recommendations. "
        f"Data (JSON): {json.dumps(facts)}"
    )
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.2,
        },
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"LLM call failed: {resp.status_code} {resp.text}")
    return resp.json()["choices"][0]["message"]["content"]


NUM_RE = re.compile(r"\d[\d,]*\.?\d*")


def flatten_numbers(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from flatten_numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from flatten_numbers(v)
    elif isinstance(obj, (int, float)):
        yield float(obj)


def unverified_numbers(summary, facts):
    """Return numbers in the summary that do not appear in the SQL results."""
    allowed = set(flatten_numbers(facts))
    flagged = []
    for raw in NUM_RE.findall(summary):
        text = raw.rstrip(".,").replace(",", "")
        if not text:
            continue
        n = float(text)
        if n == int(n) and n <= 10:  # ignore small counts like "5 sentences"
            continue
        matched = any(
            abs(n - a) < 0.051 or (n == int(n) and round(a) == n) for a in allowed
        )
        if not matched:
            flagged.append(raw.rstrip(".,"))
    return flagged


def save_report(summary, flagged):
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"report_{date.today().isoformat()}.md"
    if flagged:
        status = "Check these numbers, not found in the SQL results: " + ", ".join(flagged)
    else:
        status = "All numbers in this summary matched the SQL results."
    path.write_text(
        f"# Pricing report - {date.today():%d %b %Y}\n\n{summary}\n\n---\n*Verification: {status}*\n",
        encoding="utf-8",
    )
    return path


def main():
    facts = get_facts()
    summary = generate_summary(facts)
    flagged = unverified_numbers(summary, facts)
    path = save_report(summary, flagged)

    if "--json" in sys.argv:  # used by n8n
        print(json.dumps({"summary": summary, "flagged": flagged, "report_path": str(path)}))
        return

    print("Report saved:", path)
    print("Unverified numbers:", flagged if flagged else "none")