import os
k = os.environ.get('FINCEPT_ALPACA_API_KEY', '')
s = os.environ.get('FINCEPT_ALPACA_API_SECRET', '')
print(f"FINCEPT_ALPACA_API_KEY: {'SET' if k else 'NOT SET'}")
print(f"FINCEPT_ALPACA_API_SECRET: {'SET' if s else 'NOT SET'}")
