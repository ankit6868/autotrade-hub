import asyncio
import sys
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import ccxt.async_support as ccxt

async def test_exchange(name):
    cls = getattr(ccxt, name)
    ex = cls({"enableRateLimit": True})
    try:
        markets = await ex.load_markets()
        pairs = [k for k in markets.keys() if "BTC/USDT" == k]
        print(f"{name}: OK, BTC/USDT={bool(pairs)}")
        return True
    except Exception as e:
        print(f"{name}: FAILED - {str(e)[:100]}")
        return False
    finally:
        await ex.close()

async def main():
    for ex in ["okx", "gateio", "mexc", "bitget", "bybit", "cryptocom"]:
        ok = await test_exchange(ex)
        if ok:
            print(f"\n✅ {ex} async works! Can use for backtesting.\n")
            break

asyncio.run(main())
