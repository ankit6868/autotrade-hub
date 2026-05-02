"""Quick test: hit /api/strategy/upload with bad OpenRouter key to check error format."""
import httpx
import asyncio

async def test():
    boundary = "----FormBoundary123"
    body = (
        f"--{boundary}\r\n"
        "Content-Disposition: form-data; name=\"text\"\r\n\r\n"
        "Buy when RSI below 30, sell when RSI above 70\r\n"
        f"--{boundary}\r\n"
        "Content-Disposition: form-data; name=\"name\"\r\n\r\n"
        "TestStrat\r\n"
        f"--{boundary}--\r\n"
    )
    # Use a fake bearer token to bypass auth guard in local dev
    # (in real app Clerk provides this)
    async with httpx.AsyncClient() as c:
        # First: test with no auth (expect 401)
        r = await c.post(
            "http://localhost:8000/api/strategy/upload",
            content=body.encode(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=10,
        )
        print(f"No auth -> Status: {r.status_code}, Body: {r.text[:200]}")

asyncio.run(test())
