import asyncio
import ccxt.async_support as ccxt

async def test():
    ex = ccxt.binance({"enableRateLimit": True})
    try:
        markets = await ex.load_markets()
        pairs = [k for k in markets.keys() if "USDT" in k][:3]
        print("Binance async OK:", pairs)
    except Exception as e:
        print("Binance async FAILED:", str(e)[:200])
    finally:
        await ex.close()

asyncio.run(test())
