import asyncio
import ccxt.async_support as ccxt

async def test():
    ex = ccxt.kucoin({"enableRateLimit": True, "timeout": 30000})
    try:
        markets = await ex.load_markets()
        pairs = [k for k in markets.keys() if "USDT" in k][:3]
        print("KuCoin async OK:", pairs)
    except Exception as e:
        print("KuCoin async FAILED:", str(e)[:300])
    finally:
        await ex.close()

asyncio.run(test())
