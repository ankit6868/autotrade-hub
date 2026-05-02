import ccxt
try:
    ex = ccxt.kucoin({"enableRateLimit": True})
    markets = ex.load_markets()
    pairs = [k for k in markets.keys() if "USDT" in k][:3]
    print("KuCoin OK:", pairs)
except Exception as e:
    print("KuCoin FAILED:", str(e)[:300])
