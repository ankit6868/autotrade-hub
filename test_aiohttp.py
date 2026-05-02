import asyncio
import aiohttp
import ssl

async def test():
    # Test 1: normal
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.kucoin.com/api/v1/timestamp", timeout=aiohttp.ClientTimeout(total=10)) as r:
                print("aiohttp KuCoin:", r.status, await r.text())
    except Exception as e:
        print("aiohttp KuCoin FAILED:", str(e)[:200])

    # Test 2: SSL disabled
    try:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.kucoin.com/api/v1/timestamp", ssl=ssl_ctx, timeout=aiohttp.ClientTimeout(total=10)) as r:
                print("aiohttp KuCoin (no SSL verify):", r.status, await r.text())
    except Exception as e:
        print("aiohttp KuCoin (no SSL verify) FAILED:", str(e)[:200])

asyncio.run(test())
