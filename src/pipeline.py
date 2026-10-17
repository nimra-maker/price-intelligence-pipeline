"""Weekly Sowears price-change report: SQL facts -> LLM summary -> number check -> report -> email."""
import html
import json
import os
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
DB_PATH = Path(os.getenv("PRICES_DB") or ROOT / "data" / "processed" / "prices.db")
REPORTS_DIR = ROOT / "reports"
API_KEY = os.getenv("GROQ_API_KEY")
MODEL = os.getenv("GROQ_MODEL")

CTE = """
WITH latest AS (SELECT product_id, price, in_stock FROM store_price_snapshots WHERE snapshot_date = :latest),
     base   AS (SELECT product_id, price, in_stock FROM store_price_snapshots WHERE snapshot_date = :base)
"""


def sql(conn, query, **params):
    return json.loads(pd.read_sql(query, conn, params=params).to_json(orient="records"))


def pick_dates(conn):
    """Latest snapshot, and a baseline about 7 days earlier (or the oldest one we have)."""
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT snapshot_date FROM store_price_snapshots ORDER BY snapshot_date")]
    if not dates:
        raise RuntimeError("No snapshots in the database yet")
    latest = dates[-1]
    if len(dates) == 1:
        return latest, None
    target = (date.fromisoformat(latest) - timedelta(days=7)).isoformat()
    older = [d for d in dates[:-1] if d <= target]
    return latest, (older[-1] if older else dates[0])


def get_facts():
    conn = sqlite3.connect(DB_PATH)
    try:
        latest, base = pick_dates(conn)
        facts = {
            "store": "Sowears (Pakistani women's fashion)",
            "currency": "PKR",
            "latest_snapshot": latest,
            "overview": sql(conn, """
                SELECT COUNT(*) AS products_tracked,
                       ROUND(AVG(price)) AS avg_price,
                       SUM(in_stock = 0) AS sold_out_products
                FROM store_price_snapshots WHERE snapshot_date = :latest""", latest=latest)[0],
        }
        if base is None:
            facts["compared_with"] = None
            facts["note"] = ("Tracking has only just started: there is a single snapshot, "
                             "so no price changes can be reported yet.")
            return facts

        p = {"latest": latest, "base": base}
        facts["compared_with"] = base
        facts["days_between_snapshots"] = (date.fromisoformat(latest) - date.fromisoformat(base)).days
        facts["changes"] = sql(conn, CTE + """
            SELECT COUNT(*) AS products_compared,
                   SUM(l.price < b.price) AS price_drops,
                   SUM(l.price > b.price) AS price_rises,
                   SUM(l.price = b.price) AS unchanged
            FROM latest l JOIN base b ON b.product_id = l.product_id""", **p)[0]
        facts["biggest_drops"] = sql(conn, CTE + """
            SELECT p.title, p.product_type, b.price AS old_price, l.price AS new_price,
                   ROUND(100.0 * (l.price - b.price) / b.price, 1) AS change_pct
            FROM latest l JOIN base b ON b.product_id = l.product_id
                          JOIN store_products p ON p.product_id = l.product_id
            WHERE l.price < b.price ORDER BY change_pct ASC LIMIT 8""", **p)
        facts["biggest_rises"] = sql(conn, CTE + """
            SELECT p.title, p.product_type, b.price AS old_price, l.price AS new_price,
                   ROUND(100.0 * (l.price - b.price) / b.price, 1) AS change_pct
            FROM latest l JOIN base b ON b.product_id = l.product_id
                          JOIN store_products p ON p.product_id = l.product_id
            WHERE l.price > b.price ORDER BY change_pct DESC LIMIT 5""", **p)
        facts["changes_by_category"] = sql(conn, CTE + """
            SELECT p.product_type, COUNT(*) AS products_compared,
                   SUM(l.price < b.price) AS drops, SUM(l.price > b.price) AS rises
            FROM latest l JOIN base b ON b.product_id = l.product_id
                          JOIN store_products p ON p.product_id = l.product_id
            GROUP BY p.product_type
            HAVING SUM(l.price <> b.price) > 0
            ORDER BY SUM(l.price <> b.price) DESC LIMIT 6""", **p)
        facts["new_products"] = sql(conn, CTE + """
            SELECT COUNT(*) AS count FROM latest
            WHERE product_id NOT IN (SELECT product_id FROM base)""", **p)[0]
        facts["new_product_examples"] = sql(conn, CTE + """
            SELECT p.title, p.product_type, l.price
            FROM latest l JOIN store_products p ON p.product_id = l.product_id
            WHERE l.product_id NOT IN (SELECT product_id FROM base) LIMIT 5""", **p)
        facts["removed_products"] = sql(conn, CTE + """
            SELECT COUNT(*) AS count FROM base
            WHERE product_id NOT IN (SELECT product_id FROM latest)""", **p)[0]
        return facts
    finally:
        conn.close()


def generate_summary(facts):
    if not API_KEY or not MODEL:
        raise RuntimeError("Set GROQ_API_KEY and GROQ_MODEL")
    system_msg = (
        "You are a retail pricing analyst. Use ONLY the numbers in the data provided. "
        "Do not invent figures or causes. Prices are in PKR. The crossed-out 'was' price is "
        "not used here; only talk about price changes between the two snapshot dates. "
        "If compared_with is null, say tracking has just begun and briefly describe the "
        "current catalog instead of inventing changes."
    )
    user_msg = (
        "Write a 5-sentence summary of the price movements for a business manager, "
        f"then 2 recommendations. Data (JSON): {json.dumps(facts)}"
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
        yield abs(float(obj))
    elif isinstance(obj, str):  # dates and product names contain numbers too
        for m in NUM_RE.findall(obj):
            t = m.rstrip(".,").replace(",", "")
            if t:
                yield float(t)


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
        f"# Sowears price report - {date.today():%d %b %Y}\n\n{summary}\n\n---\n*Verification: {status}*\n",
        encoding="utf-8",
    )
    return path


def send_email(summary, flagged):
    api_key = os.getenv("BREVO_API_KEY")
    sender = os.getenv("EMAIL_SENDER")
    recipient = os.getenv("EMAIL_RECIPIENT")
    if not (api_key and sender and recipient):
        print("Email skipped: set BREVO_API_KEY, EMAIL_SENDER, EMAIL_RECIPIENT")
        return

    prefix = "[CHECK NUMBERS] " if flagged else ""
    body = html.escape(summary).replace("\n", "<br>")
    if flagged:
        body += "<br><br><i>Numbers not found in the SQL results: " + html.escape(", ".join(flagged)) + "</i>"

    resp = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": api_key, "accept": "application/json", "content-type": "application/json"},
        json={
            "sender": {"name": "Pricing Pipeline", "email": sender},
            "to": [{"email": recipient}],
            "subject": f"{prefix}Weekly Sowears price report - {date.today():%d %b %Y}",
            "htmlContent": f"<html><body>{body}</body></html>",
        },
        timeout=30,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"Brevo error: {resp.status_code} {resp.text}")
    print("Email sent to", recipient)


def main():
    facts = get_facts()
    summary = generate_summary(facts)
    flagged = unverified_numbers(summary, facts)
    path = save_report(summary, flagged)
    print("Report saved:", path)
    print("Unverified numbers:", flagged if flagged else "none")
    send_email(summary, flagged)


if __name__ == "__main__":
    main()