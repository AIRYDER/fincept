"""Test NewsAPI key — check how far back we can fetch articles."""
import os
import datetime as dt
import httpx

os.environ["NEWSAPI_KEY"] = "e25076a3f6b8426083f86079f8a5bf36"

key = os.environ["NEWSAPI_KEY"]

# Test different date ranges to see what the key allows
test_ranges = [
    ("Last 30 days", dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30), dt.datetime.now(dt.timezone.utc)),
    ("Last 90 days", dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90), dt.datetime.now(dt.timezone.utc)),
    ("Last 6 months", dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=180), dt.datetime.now(dt.timezone.utc)),
    ("Last 1 year", dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=365), dt.datetime.now(dt.timezone.utc)),
    ("Last 2 years", dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=730), dt.datetime.now(dt.timezone.utc)),
    ("Last 5 years", dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1825), dt.datetime.now(dt.timezone.utc)),
]

import time
import traceback

for label, start, end in test_ranges:
    time.sleep(2)  # Rate limit
    try:
        start_str = start.strftime("%Y-%m-%d")
        end_str = end.strftime("%Y-%m-%d")
        r = httpx.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": "AAPL OR Apple stock",
                "from": start_str,
                "to": end_str,
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": 5,
                "apiKey": key,
            },
            timeout=30,
        )
    except Exception as exc:
        print(f"  {label:20s}: EXCEPTION — {exc}", flush=True)
        traceback.print_exc()
        continue
    if r.status_code == 200:
        data = r.json()
        total = data.get("totalResults", 0)
        articles = data.get("articles", [])
        earliest = articles[-1].get("publishedAt", "n/a") if articles else "n/a"
        latest = articles[0].get("publishedAt", "n/a") if articles else "n/a"
        print(f"  {label:20s}: HTTP 200, totalResults={total}, earliest={earliest}, latest={latest}", flush=True)
    else:
        try:
            error = r.json().get("message", r.text[:200])
        except Exception:
            error = r.text[:200]
        print(f"  {label:20s}: HTTP {r.status_code} — {error}", flush=True)
