import asyncio
import httpx

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"

# Model priority list — verified against the live OpenRouter catalogue.
# parse_with_fallback tries them in order and moves on after any failure or
# rate-limit. Update this list when models are added / retired.
FREE_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",        # Nemotron Super 120B — primary
    "openai/gpt-oss-120b:free",                       # GPT-OSS 120B
    "openai/gpt-oss-20b:free",                        # GPT-OSS 20B (fast)
    "google/gemma-4-31b-it:free",                     # Gemma 4 31B
    "z-ai/glm-4.5-air:free",                          # GLM-4.5 Air
    "qwen/qwen3-coder:free",                          # Qwen3 Coder (best for code, may rate-limit)
    "meta-llama/llama-3.3-70b-instruct:free",         # Llama 3.3 70B
    "nousresearch/hermes-3-llama-3.1-405b:free",      # Hermes 3 405B
    "google/gemma-3-12b-it:free",                     # Gemma 3 12B
    "minimax/minimax-m2.5:free",                      # Minimax M2.5
]

DEFAULT_MODEL = FREE_MODELS[0]

SYSTEM_PROMPT = """You are a Freqtrade strategy code generator.
Read the user's trading strategy description and generate a valid
Freqtrade strategy Python class.

Include:
- populate_indicators() with all required indicators using ta-lib
- populate_entry_trend() with entry signals
- populate_exit_trend() with exit signals
- Proper stoploss, minimal_roi, timeframe settings
- Class must inherit from IStrategy
- Use only indicators available in ta-lib or pandas-ta

Output ONLY valid Python code. No markdown, no explanation, no backticks.
Start directly with 'from freqtrade.strategy import IStrategy'"""


def _clean_code(code: str) -> str:
    code = code.strip()
    # Strip markdown code fences
    for fence in ("```python", "```py", "```"):
        if code.startswith(fence):
            code = code[len(fence):]
            break
    if code.endswith("```"):
        code = code[:-3]
    return code.strip()


async def parse_strategy_with_openrouter(
    strategy_text: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> dict:
    async with httpx.AsyncClient(timeout=90.0) as client:
        response = await client.post(
            OPENROUTER_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://autotrade-hub.local",
                "X-Title": "AutoTrade Hub",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Convert this trading strategy to Freqtrade code:\n\n{strategy_text}"
                        ),
                    },
                ],
                "temperature": 0.2,
                "max_tokens": 4000,
            },
        )
        response.raise_for_status()

    data = response.json()

    # Some models/providers return an error body with HTTP 200
    if "error" in data:
        raise Exception(
            f"Model {model} returned error: {data['error'].get('message', data['error'])}"
        )

    choices = data.get("choices")
    if not choices:
        raise Exception(f"Model {model} returned no choices. Response: {data}")

    content = choices[0].get("message", {}).get("content", "")
    if not content:
        raise Exception(f"Model {model} returned empty content")

    code = _clean_code(content)
    return {
        "code": code,
        "model_used": data.get("model", model),
        "tokens_used": data.get("usage", {}),
    }


async def parse_with_fallback(strategy_text: str, api_key: str) -> dict:
    """Try every model in FREE_MODELS until one succeeds."""
    last_error = "All free models failed"
    for model in FREE_MODELS:
        try:
            result = await parse_strategy_with_openrouter(strategy_text, api_key, model)
            if result["code"] and "IStrategy" in result["code"]:
                print(f"Strategy parsed successfully with model: {model}")
                return result
            print(f"Model {model} produced code without IStrategy, trying next...")
        except httpx.HTTPStatusError as e:
            last_error = f"{model}: HTTP {e.response.status_code} — {e.response.text[:200]}"
            print(f"Model {model} HTTP error: {last_error}")
            if e.response.status_code == 429:
                # Rate limited — wait before trying next model
                await asyncio.sleep(5)
        except Exception as e:
            last_error = f"{model}: {e}"
            print(f"Model {model} failed: {e}, trying next...")
    raise Exception(
        f"All free models tried and failed. Last error: {last_error}. "
        "Check your OpenRouter API key or try again later."
    )


async def parse_with_retry(
    strategy_text: str,
    api_key: str,
    model: str | None = None,
    max_retries: int = 2,
) -> dict:
    """If a specific model is requested, try it with retries on 429.
    Otherwise run the full fallback chain."""
    if not model:
        return await parse_with_fallback(strategy_text, api_key)

    for attempt in range(max_retries + 1):
        try:
            return await parse_strategy_with_openrouter(strategy_text, api_key, model)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                wait = (attempt + 1) * 15
                print(f"Rate limited on {model}. Waiting {wait}s (attempt {attempt + 1})...")
                await asyncio.sleep(wait)
            elif attempt == 0:
                # First failure on a non-429 — try fallback chain instead
                print(f"Requested model {model} failed ({e.response.status_code}), falling back...")
                return await parse_with_fallback(strategy_text, api_key)
            else:
                raise
        except Exception as e:
            if attempt == 0:
                print(f"Requested model {model} failed ({e}), falling back to chain...")
                return await parse_with_fallback(strategy_text, api_key)
            raise

    # Exhausted retries on rate-limited model → try fallback chain
    return await parse_with_fallback(strategy_text, api_key)


async def ai_assist(
    prompt: str,
    existing_code: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> str:
    system = (
        "You are a Freqtrade strategy code assistant. The user has existing strategy code "
        "and wants modifications. Return ONLY the modified Python code. No explanations."
    )
    last_error = ""
    models_to_try = [model] + [m for m in FREE_MODELS if m != model]
    for m in models_to_try:
        try:
            async with httpx.AsyncClient(timeout=90.0) as client:
                response = await client.post(
                    OPENROUTER_BASE_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://autotrade-hub.local",
                        "X-Title": "AutoTrade Hub",
                    },
                    json={
                        "model": m,
                        "messages": [
                            {"role": "system", "content": system},
                            {
                                "role": "user",
                                "content": (
                                    f"Existing code:\n```python\n{existing_code}\n```\n\nRequest: {prompt}"
                                ),
                            },
                        ],
                        "temperature": 0.2,
                        "max_tokens": 4000,
                    },
                )
                response.raise_for_status()
            data = response.json()
            if "error" in data:
                last_error = str(data["error"])
                continue
            choices = data.get("choices", [])
            if choices:
                return _clean_code(choices[0]["message"]["content"])
        except Exception as e:
            last_error = str(e)
            print(f"AI assist model {m} failed: {e}, trying next...")
            continue
    raise Exception(f"AI assist failed with all models. Last: {last_error}")
