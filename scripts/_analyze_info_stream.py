"""Analyze info.enriched stream for training data extraction."""
import redis
import json
from collections import Counter
from datetime import datetime, timezone

r = redis.Redis(host='localhost', port=6379, decode_responses=True)

# Get all info.enriched entries
entries = r.xrange('info.enriched')
print(f"Total info.enriched entries: {len(entries)}")

# Parse all entries
events = []
for entry_id, fields in entries:
    payload_raw = fields.get('payload', '')
    if isinstance(payload_raw, bytes):
        payload_raw = payload_raw.decode()
    try:
        event = json.loads(payload_raw)
        p = event.get('payload', event)
        events.append(p)
    except Exception:
        continue

print(f"Parsed events: {len(events)}")

# Time range
ts_values = [e.get('ts_event', 0) for e in events if e.get('ts_event')]
if ts_values:
    min_ts = min(ts_values)
    max_ts = max(ts_values)
    min_dt = datetime.fromtimestamp(min_ts / 1e9, tz=timezone.utc)
    max_dt = datetime.fromtimestamp(max_ts / 1e9, tz=timezone.utc)
    print(f"Time range: {min_dt} to {max_dt}")
    print(f"  Span: {(max_dt - min_dt).total_seconds() / 3600:.1f} hours")

# Symbol coverage
symbol_counts = Counter()
for e in events:
    for sym in e.get('symbols', []):
        symbol_counts[sym] += 1

print(f"\nSymbol coverage ({len(symbol_counts)} unique symbols):")
for sym, count in symbol_counts.most_common(30):
    print(f"  {sym:10s} {count:5d} events")

# Event categories
cat_counts = Counter(e.get('event_category', 'unknown') for e in events)
print(f"\nEvent categories:")
for cat, count in cat_counts.most_common():
    print(f"  {cat:20s} {count:5d}")

# Source types
src_counts = Counter(e.get('source_type', 'unknown') for e in events)
print(f"\nSource types:")
for src, count in src_counts.most_common():
    print(f"  {src:20s} {count:5d}")

# Source names
source_names = Counter(e.get('source', 'unknown') for e in events)
print(f"\nSource names:")
for src, count in source_names.most_common(10):
    print(f"  {src:20s} {count:5d}")

# Check which of our 20 training symbols have events
training_symbols = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'JPM',
                    'V', 'JNJ', 'WMT', 'PG', 'UNH', 'HD', 'MA', 'DIS', 'BAC', 'XOM',
                    'KO', 'PEP']
print(f"\nTraining symbol coverage in info.enriched:")
for sym in training_symbols:
    count = symbol_counts.get(sym, 0)
    print(f"  {sym:6s} {count:5d} events")
