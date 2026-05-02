import asyncio
import sys
# Windows fix: use SelectorEventLoop for DNS resolution
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import aiohttp

async def test():
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.kucoin.com/api/v1/timestamp", timeout=aiohttp.ClientTimeout(total=10)) as r:
                print("aiohttp KuCoin (SelectorLoop):", r.status, await r.text())
    except Exception as e:
        print("aiohttp KuCoin (SelectorLoop) FAILED:", str(e)[:200])

asyncio.run(test())
