import asyncio
import sys
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import aiohttp
import socket

async def test():
    # Try resolving DNS manually first
    try:
        ip = socket.gethostbyname("api.kucoin.com")
        print("DNS resolved:", ip)
    except Exception as e:
        print("DNS sync failed:", e)

    # Try with resolved IP
    try:
        ip = socket.gethostbyname("api.kucoin.com")
        conn = aiohttp.TCPConnector(ssl=False, family=socket.AF_INET)
        async with aiohttp.ClientSession(connector=conn) as s:
            url = f"https://{ip}/api/v1/timestamp"
            headers = {"Host": "api.kucoin.com"}
            async with s.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                print("aiohttp KuCoin (IP):", r.status, await r.text())
    except Exception as e:
        print("aiohttp KuCoin (IP) FAILED:", str(e)[:200])

asyncio.run(test())
