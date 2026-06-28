"""Inspect news_alpha example structure."""
import redis
import json

r = redis.Redis(host='localhost', port=6379, decode_responses=True)

keys = [k for k in r.keys('news_alpha:example:*')][:3]
for key in keys:
    print(f"\n{'=' * 80}")
    print(f"Key: {key}")
    print(f"{'=' * 80}")
    data = r.hgetall(key)
    for field, value in data.items():
        if field == 'frame':
            try:
                frame = json.loads(value)
                print(f"  frame:")
                print(f"    symbol: {frame.get('symbol')}")
                print(f"    ts_event: {frame.get('ts_event')}")
                print(f"    freq: {frame.get('freq')}")
                vals = frame.get('values', {})
                print(f"    values ({len(vals)} features):")
                for k, v in list(vals.items())[:25]:
                    print(f"      {k}: {v}")
                if len(vals) > 25:
                    print(f"      ... ({len(vals) - 25} more)")
                tags = frame.get('tags', {})
                print(f"    tags: {tags}")
            except Exception as e:
                print(f"  frame: (parse error: {e})")
        elif field.startswith('label:'):
            print(f"  {field}: {value}")
        elif field == 'start_price':
            print(f"  start_price: {value}")
        elif field == 'ts_event':
            print(f"  ts_event: {value}")
        else:
            print(f"  {field}: {value[:200]}")

# Count total and check label distribution
print(f"\n{'=' * 80}")
print(f"Label distribution across all 277 examples")
print(f"{'=' * 80}")
all_keys = [k for k in r.keys('news_alpha:example:*')]
label_5m_up = 0
label_5m_down = 0
label_5m_neutral = 0
label_30m_up = 0
label_30m_down = 0
label_30m_neutral = 0
label_4h_up = 0
label_4h_down = 0
label_4h_neutral = 0
symbols = set()
for key in all_keys:
    data = r.hgetall(key)
    for field, value in data.items():
        if field == 'label:5m:return':
            v = float(value)
            if v > 0.001: label_5m_up += 1
            elif v < -0.001: label_5m_down += 1
            else: label_5m_neutral += 1
        elif field == 'label:30m:return':
            v = float(value)
            if v > 0.001: label_30m_up += 1
            elif v < -0.001: label_30m_down += 1
            else: label_30m_neutral += 1
        elif field == 'label:4h:return':
            v = float(value)
            if v > 0.001: label_4h_up += 1
            elif v < -0.001: label_4h_down += 1
            else: label_4h_neutral += 1
    # Get symbol from frame
    frame_raw = data.get('frame', '')
    try:
        frame = json.loads(frame_raw)
        symbols.add(frame.get('symbol', 'UNKNOWN'))
    except Exception:
        pass

print(f"  Total examples: {len(all_keys)}")
print(f"  Unique symbols: {len(symbols)}")
print(f"  Symbols: {sorted(symbols)[:20]}")
print(f"\n  5m labels:  up={label_5m_up}  down={label_5m_down}  neutral={label_5m_neutral}")
print(f"  30m labels: up={label_30m_up}  down={label_30m_down}  neutral={label_30m_neutral}")
print(f"  4h labels:  up={label_4h_up}  down={label_4h_down}  neutral={label_4h_neutral}")
