"""Inspect Redis data stores for training-relevant data."""

import json

import redis

r = redis.Redis(host="localhost", port=6379, decode_responses=True)

# Categorize keys by prefix
prefixes = {}
for key in r.keys("*"):
    prefix = key.split(":")[0] if ":" in key else key
    prefixes.setdefault(prefix, 0)
    prefixes[prefix] += 1

print("Key prefixes (top 30):")
for prefix, count in sorted(prefixes.items(), key=lambda x: -x[1])[:30]:
    print(f"  {prefix:40s} {count:5d}")

# Check sentiment observations
sent_keys = [k for k in r.keys("sentiment_features:observations:*")]
print(f"\nSentiment observation keys: {len(sent_keys)}")
for k in sent_keys[:10]:
    count = r.zcard(k)
    symbol = k.split(":")[-1]
    print(f"  {symbol}: {count} observations")

# Check news articles
news_keys = [k for k in r.keys("news:article:*")]
print(f"\nNews articles in Redis: {len(news_keys)}")
if news_keys:
    # Sample one
    sample = r.hget(news_keys[0], "data")
    if sample:
        data = json.loads(sample)
        print(f"  Sample article: {data.get('headline', 'n/a')[:80]}")
        print(f"  Symbols: {data.get('symbols', [])}")
        print(f"  Created: {data.get('created_at', 'n/a')}")
        print(f"  Source: {data.get('source', 'n/a')}")

# Check news index
news_idx = r.zcard("news:index") if r.exists("news:index") else 0
print(f"\nNews index entries: {news_idx}")

# Check feature store
feat_keys = [k for k in r.keys("features:*")]
print(f"\nFeature store keys: {len(feat_keys)}")
for k in feat_keys[:10]:
    t = r.type(k)
    print(f"  {k[:60]}: type={t}")

# Check info streams
stream_len = r.xlen("info.enriched") if r.exists("info.enriched") else 0
print(f"\ninfo.enriched stream length: {stream_len}")

stream_raw = r.xlen("info.raw") if r.exists("info.raw") else 0
print(f"info.raw stream length: {stream_raw}")

# Check sentiment signals stream
sig_len = r.xlen("sig.sentiment") if r.exists("sig.sentiment") else 0
print(f"sig.sentiment stream length: {sig_len}")

# Sample from info.enriched
if stream_len > 0:
    entries = r.xrange("info.enriched", count=3)
    print("\nSample info.enriched entries:")
    for entry_id, fields in entries:
        print(f"  {entry_id}: {list(fields.keys())}")
        if b"payload" in fields or "payload" in fields:
            payload = fields.get("payload", fields.get(b"payload", ""))
            if isinstance(payload, bytes):
                payload = payload.decode()
            data = json.loads(payload)
            print(f"    type={data.get('type', 'n/a')}")
            if "payload" in data:
                p = data["payload"]
                print(f"    headline={str(p.get('headline', ''))[:80]}")
                print(f"    symbols={p.get('symbols', [])}")
                print(f"    event_category={p.get('event_category', 'n/a')}")
