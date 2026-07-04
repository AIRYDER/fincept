"""Quick NewsAPI test — just 2 requests."""

import datetime as dt
import os
import time

import httpx

os.environ["NEWSAPI_KEY"] = "e25076a3f6b8426083f86079f8a5bf36"
key = os.environ["NEWSAPI_KEY"]

# Test 1: last 30 days
print("Test 1: last 30 days", flush=True)
r = httpx.get(
    "https://newsapi.org/v2/everything",
    params={
        "q": "AAPL OR Apple stock",
        "from": (dt.datetime.now(dt.UTC) - dt.timedelta(days=30)).strftime("%Y-%m-%d"),
        "to": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d"),
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": 5,
        "apiKey": key,
    },
    timeout=30,
)
print(f"  HTTP {r.status_code}: {r.json().get('totalResults', 'n/a')} results", flush=True)

time.sleep(3)

# Test 2: last 1 year
print("Test 2: last 1 year", flush=True)
try:
    r = httpx.get(
        "https://newsapi.org/v2/everything",
        params={
            "q": "AAPL OR Apple stock",
            "from": (dt.datetime.now(dt.UTC) - dt.timedelta(days=365)).strftime("%Y-%m-%d"),
            "to": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d"),
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 5,
            "apiKey": key,
        },
        timeout=30,
    )
    print(f"  HTTP {r.status_code}: {r.json().get('totalResults', 'n/a')} results", flush=True)
except Exception as exc:
    print(f"  ERROR: {exc}", flush=True)

time.sleep(3)

# Test 3: last 5 years
print("Test 3: last 5 years", flush=True)
try:
    r = httpx.get(
        "https://newsapi.org/v2/everything",
        params={
            "q": "AAPL OR Apple stock",
            "from": (dt.datetime.now(dt.UTC) - dt.timedelta(days=1825)).strftime("%Y-%m-%d"),
            "to": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d"),
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 5,
            "apiKey": key,
        },
        timeout=30,
    )
    print(f"  HTTP {r.status_code}: {r.json().get('totalResults', 'n/a')} results", flush=True)
except Exception as exc:
    print(f"  ERROR: {exc}", flush=True)

print("Done!", flush=True)
