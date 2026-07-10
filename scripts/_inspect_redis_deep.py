"""Deep inspect Redis streams for training data."""

import json

import redis

r = redis.Redis(host="localhost", port=6379, decode_responses=True)

# Sample info.enriched entries — show full payload
print("=" * 80)
print("info.enriched — 2,298 events (showing first 5)")
print("=" * 80)
entries = r.xrange("info.enriched", count=5)
for entry_id, fields in entries:
    payload_raw = fields.get("payload", "")
    if isinstance(payload_raw, bytes):
        payload_raw = payload_raw.decode()
    try:
        event = json.loads(payload_raw)
    except Exception:
        print(f"  {entry_id}: (parse error)")
        continue
    p = event.get("payload", event)
    print(f"\n  ID: {entry_id}")
    print(f"  headline: {str(p.get('headline', ''))[:100]}")
    print(f"  symbols: {p.get('symbols', [])}")
    print(f"  event_category: {p.get('event_category', 'n/a')}")
    print(f"  source: {p.get('source', 'n/a')}")
    print(f"  source_type: {p.get('source_type', 'n/a')}")
    print(f"  ts_event: {p.get('ts_event', 'n/a')}")
    print(f"  source_quality: {p.get('source_quality', 'n/a')}")
    print(f"  recency_score: {p.get('recency_score', 'n/a')}")

# Sample sig.sentiment entries
print(f"\n{'=' * 80}")
print("sig.sentiment — 29 signals (showing all)")
print("=" * 80)
entries = r.xrange("sig.sentiment", count=50)
for entry_id, fields in entries:
    payload_raw = fields.get("payload", "")
    if isinstance(payload_raw, bytes):
        payload_raw = payload_raw.decode()
    try:
        event = json.loads(payload_raw)
    except Exception:
        print(f"  {entry_id}: (parse error) — {payload_raw[:100]}")
        continue
    p = event.get("payload", event)
    print(
        f"  {entry_id}: symbol={p.get('symbol', 'n/a')} score={p.get('score', 'n/a')} "
        f"conf={p.get('confidence', 'n/a')} event_type={p.get('event_type', 'n/a')}"
    )

# Sample news_alpha examples
print(f"\n{'=' * 80}")
print("news_alpha:example — 277 entries (showing first 5)")
print("=" * 80)
keys = [k for k in r.keys("news_alpha:example:*")][:5]
for key in keys:
    t = r.type(key)
    if t == "string":
        val = r.get(key)
        try:
            data = json.loads(val)
        except Exception:
            print(f"  {key}: (parse error)")
            continue
        print(f"  {key}:")
        print(f"    keys: {list(data.keys())[:10]}")
        # Show a few fields
        for k in ["symbol", "label", "features", "sentiment", "score", "headline"]:
            if k in data:
                v = data[k]
                if isinstance(v, dict):
                    print(f"    {k}: {list(v.keys())[:10]}")
                elif isinstance(v, (int, float, str, bool)):
                    print(f"    {k}: {v}")
                else:
                    print(f"    {k}: ({type(v).__name__})")
    elif t == "hash":
        print(f"  {key}: (hash) fields={list(r.hgetall(key).keys())[:10]}")

# Check time range of info.enriched
print(f"\n{'=' * 80}")
print("info.enriched time range")
print("=" * 80)
first = r.xrange("info.enriched", count=1)
last = r.xrevrange("info.enriched", count=1)
if first:
    first_id = first[0][0]
    print(f"  First: {first_id}")
if last:
    last_id = last[0][0]
    print(f"  Last:  {last_id}")

# Check md.bars.1m
print(f"\n{'=' * 80}")
print("md.bars.1m — market data")
print("=" * 80)
bars_len = r.xlen("md.bars.1m") if r.exists("md.bars.1m") else 0
print(f"  Stream length: {bars_len}")
if bars_len > 0:
    sample = r.xrange("md.bars.1m", count=3)
    for entry_id, fields in sample:
        print(f"  {entry_id}: {dict(list(fields.items())[:8])}")
