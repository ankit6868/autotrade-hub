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

SYSTEM_PROMPT = """You are a Freqtrade IStrategy code generator for crypto futures.

CRITICAL RULES — your output MUST satisfy ALL of these or it is unusable:

1. The class MUST inherit from IStrategy:
       class MyStrategy(IStrategy):

2. The class MUST set can_short = True (futures supports BOTH long AND short).

3. The class MUST define ALL THREE method bodies — NOT stubs, NOT pass:

       def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
           # Compute every indicator the entry/exit rules below reference.
           # Use pandas (.rolling, .ewm, .diff) or talib.RSI / talib.EMA etc.
           # ALWAYS return dataframe.
           return dataframe

       def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
           # Set BOTH enter_long AND enter_short to 0 or 1 per bar.
           # Use vectorized pandas: dataframe.loc[CONDITION, "enter_long"] = 1
           # If the strategy is long-only, still set enter_short = 0 explicitly.
           dataframe["enter_long"]  = 0
           dataframe["enter_short"] = 0
           # ... actual conditions here ...
           return dataframe

       def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
           dataframe["exit_long"]  = 0
           dataframe["exit_short"] = 0
           # ... actual conditions here ...
           return dataframe

4. NEVER emit a class with only attributes (timeframe, stoploss, minimal_roi)
   and no methods. That is a stub and will produce zero trades.

5. Implement the user's described logic IN PYTHON — translate any natural
   language ("buy when RSI < 30") into actual pandas conditions:
       dataframe["rsi"] = talib.RSI(dataframe["close"], 14)
       dataframe.loc[dataframe["rsi"] < 30, "enter_long"] = 1

6. Mandatory class attributes (set sensible defaults if user didn't specify):
       timeframe   = "15m"
       stoploss    = -0.03
       minimal_roi = {"0": 0.06}
       can_short   = True
       startup_candle_count = 50      # large enough for longest indicator
       process_only_new_candles = True

7. Allowed imports ONLY (these are stubbed in the runner sandbox):
       from freqtrade.strategy import IStrategy
       from pandas import DataFrame
       import pandas as pd
       import numpy as np
       import talib            # SMA, EMA, RSI, MACD, BBANDS, ATR, ADX, STOCH stubbed
       import pandas_ta        # also routed to the talib stub
       from qtpylib import indicators as qtpylib    # crossed_above / crossed_below

   Do NOT import: os, sys, subprocess, requests, urllib, asyncio, freqtrade.persistence.

8. Output ONLY valid executable Python. No markdown fences, no commentary,
   no docstring describing "what it should do" without implementing it.
   Start directly with `from freqtrade.strategy import IStrategy`."""


# Minimum signals the generated code must contain to be considered complete.
# Used by _validate_generated_code() below — if any are missing we retry the
# LLM with a stricter follow-up prompt rather than persisting a stub.
REQUIRED_MARKERS = [
    "class ",
    "IStrategy",
    "def populate_indicators",
    "def populate_entry_trend",
    "def populate_exit_trend",
    "enter_long",
    "enter_short",
]


def _validate_generated_code(code: str) -> tuple[bool, list[str]]:
    """Return (ok, missing_markers). A code passes only when it has ALL the
    elements an IStrategy needs to actually drive entries/exits in our
    backtester. Catches the common LLM failure mode of emitting a stub
    with just class attributes and a docstring."""
    missing = [m for m in REQUIRED_MARKERS if m not in code]
    return (not missing, missing)


REPAIR_PROMPT = (
    "Your previous output was INCOMPLETE. The strategy class you wrote is "
    "missing the following required elements: {missing}. "
    "Generate the FULL strategy code again — every required method body "
    "must be implemented in pandas/talib, not left as a stub. "
    "Both enter_long and enter_short must be set on every bar."
)


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


async def _call_openrouter(messages: list, api_key: str, model: str) -> dict:
    """Single round-trip to OpenRouter — used by both initial gen and repair."""
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
                "messages": messages,
                "temperature": 0.2,
                "max_tokens": 4000,
            },
        )
        response.raise_for_status()
    return response.json()


async def parse_strategy_with_openrouter(
    strategy_text: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> dict:
    """Generate a strategy; if the first attempt is a stub, send the
    REPAIR_PROMPT to the same model with the broken output attached and
    ask for the missing pieces. Up to one repair pass per model."""

    initial_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",
         "content": f"Convert this trading strategy to a complete Freqtrade IStrategy:\n\n{strategy_text}"},
    ]
    data = await _call_openrouter(initial_messages, api_key, model)

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

    # ── Validate. If the model emitted a stub (no populate_* hooks, no
    # enter_long/enter_short, etc.) try ONE repair round-trip with the
    # broken output attached + a stern follow-up message.
    ok, missing = _validate_generated_code(code)
    if not ok:
        repair_messages = initial_messages + [
            {"role": "assistant", "content": code},
            {"role": "user",
             "content": REPAIR_PROMPT.format(missing=", ".join(missing))},
        ]
        try:
            repair_data = await _call_openrouter(repair_messages, api_key, model)
            if "error" not in repair_data and repair_data.get("choices"):
                repaired = _clean_code(
                    repair_data["choices"][0].get("message", {}).get("content", "")
                )
                ok2, _ = _validate_generated_code(repaired)
                if ok2:
                    code = repaired
                    data = repair_data
        except Exception as e:
            # Repair attempt failed — surface the original stub but with a
            # warning marker in the model_used field so the caller knows.
            print(f"Repair attempt with {model} failed: {e}")

    return {
        "code": code,
        "model_used": data.get("model", model),
        "tokens_used": data.get("usage", {}),
        "validation_passed": _validate_generated_code(code)[0],
        "validation_missing": _validate_generated_code(code)[1],
    }


async def parse_with_fallback(strategy_text: str, api_key: str) -> dict:
    """Try every model in FREE_MODELS until one returns COMPLETE strategy
    code (not just a stub). The validation in parse_strategy_with_openrouter
    already does a single repair round-trip per model; this layer then
    moves to the next model if even the repaired output is incomplete.

    Returns the best result we got — if all models failed validation, we
    still return the most-complete attempt so the user sees something
    rather than a blanket error.
    """
    last_error = "All free models failed"
    best_partial: dict | None = None
    for model in FREE_MODELS:
        try:
            result = await parse_strategy_with_openrouter(strategy_text, api_key, model)
            code = result["code"]
            if code and result.get("validation_passed"):
                print(f"Strategy parsed (+validated) with model: {model}")
                return result
            # Remember the most-complete-looking attempt as a fallback.
            if code and "IStrategy" in code:
                if best_partial is None or len(code) > len(best_partial.get("code", "")):
                    best_partial = result
                print(f"Model {model} produced incomplete code (missing: {result.get('validation_missing')}), trying next...")
            else:
                print(f"Model {model} produced no IStrategy class, trying next...")
        except httpx.HTTPStatusError as e:
            last_error = f"{model}: HTTP {e.response.status_code} — {e.response.text[:200]}"
            print(f"Model {model} HTTP error: {last_error}")
            if e.response.status_code == 429:
                await asyncio.sleep(5)
        except Exception as e:
            last_error = f"{model}: {e}"
            print(f"Model {model} failed: {e}, trying next...")
    if best_partial:
        print(f"All models produced incomplete code — returning best partial from {best_partial.get('model_used')}")
        return best_partial
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
