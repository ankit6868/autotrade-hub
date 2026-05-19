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

SYSTEM_PROMPT = """You are a Freqtrade IStrategy code generator for crypto futures backtesting.

YOUR OUTPUT IS EXEC'D DIRECTLY — if it's a stub, the backtest is meaningless.

═══════════════════════════════════════════════════════════════════════
ABSOLUTE RULES (any violation = unusable output):
═══════════════════════════════════════════════════════════════════════

1. Output ONLY valid Python that compiles. No markdown fences, no English
   pseudo-code, no "ON EVERY BAR: ..." instructions. Start with the imports.

2. Required class skeleton:
       class MyStrategy(IStrategy):
           timeframe   = "15m"
           stoploss    = -0.03
           minimal_roi = {"0": 0.06}
           can_short   = True            # MANDATORY — futures need shorts
           startup_candle_count = 50
           process_only_new_candles = True

3. ALL THREE METHODS MUST HAVE REAL BODIES (no `pass`, no docstring-only):
   - populate_indicators  → compute indicators referenced by the rules
   - populate_entry_trend → set enter_long AND enter_short per bar
   - populate_exit_trend  → set exit_long  AND exit_short  per bar

4. The class body MUST be at least 25 real lines of code. If your output is
   just attributes + a docstring, you have FAILED. A correct strategy has
   indicator calculations, entry condition logic, and exit condition logic.

5. Allowed imports ONLY:
       from freqtrade.strategy import IStrategy
       from pandas import DataFrame
       import pandas as pd
       import numpy as np
       import talib.abstract as ta
       from qtpylib import indicators as qtpylib

═══════════════════════════════════════════════════════════════════════
WORKED EXAMPLE — this is the QUALITY BAR you must hit:
═══════════════════════════════════════════════════════════════════════

User request: "RSI + EMA crossover. Long when RSI < 35 and 9 EMA crosses
above 21 EMA. Short when RSI > 65 and 9 EMA crosses below 21 EMA."

Correct output:

```python
from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta
from qtpylib import indicators as qtpylib


class RsiEmaCrossover(IStrategy):
    timeframe   = "15m"
    stoploss    = -0.03
    minimal_roi = {"0": 0.06}
    can_short   = True
    startup_candle_count = 50
    process_only_new_candles = True

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"]   = ta.RSI(dataframe, timeperiod=14)
        dataframe["ema9"]  = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"]  = 0
        dataframe["enter_short"] = 0

        dataframe.loc[
            (dataframe["rsi"] < 35) &
            qtpylib.crossed_above(dataframe["ema9"], dataframe["ema21"]),
            "enter_long",
        ] = 1

        dataframe.loc[
            (dataframe["rsi"] > 65) &
            qtpylib.crossed_below(dataframe["ema9"], dataframe["ema21"]),
            "enter_short",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"]  = 0
        dataframe["exit_short"] = 0
        return dataframe
```

═══════════════════════════════════════════════════════════════════════
COMMON FAILURES TO AVOID:
═══════════════════════════════════════════════════════════════════════

❌ Writing English pseudo-code like "IF rsi < 30 THEN enter_long = TRUE"
❌ Writing class methods like `macd_condition(self, ...)` that aren't
   populate_* hooks — these never run in Freqtrade's pipeline
❌ Class with only docstring + attributes, no methods → STUB
❌ `pd.Data dataframe:` (typo for `pd.DataFrame`) → SyntaxError
❌ `dataframe.loc[(), 'exit_long'] = 0` (empty tuple in .loc) → broken
❌ Forgetting `can_short = True` → engine ignores shorts silently
❌ Forgetting to set enter_short = 0 baseline → KeyError downstream

Translate ANY natural-language description into actual pandas conditions.
If the user describes a 7-step institutional strategy with HTF bias, OB,
FVG, etc., translate EACH step into pandas/numpy code, not English prose."""


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
    """Return (ok, missing_markers). A code passes only when ALL of these
    are true:

      1. Python syntax compiles cleanly (catches typos like
         `pd.Data dataframe:` which crashed real user strategies)
      2. All REQUIRED_MARKERS present (class, IStrategy, populate_*,
         enter_long, enter_short)
      3. `can_short = True` is set (futures need both directions; without
         this Freqtrade silently ignores enter_short → user thinks they
         have a bidirectional strategy but only longs ever fire)
      4. Code body has enough substance to be more than a docstring stub
         (≥ 30 non-comment, non-blank lines — stubs are typically <15)

    Returns (ok, list_of_issues). issues is empty when ok=True."""
    issues: list[str] = []
    if not code or not code.strip():
        return (False, ["empty code"])

    # 1. Syntax check — catches `pd.Data dataframe:`, indentation bugs,
    # unterminated strings, etc. without exec'ing dangerous code.
    try:
        compile(code, "<generated_strategy>", "exec")
    except SyntaxError as e:
        issues.append(f"syntax error: {e.msg} on line {e.lineno}")
        # Syntax-error code can't be safely scanned for markers below
        # (e.g. a missing colon could hide enter_long), so bail early.
        return (False, issues)

    # 2. Required-marker check (string-level, no exec).
    for m in REQUIRED_MARKERS:
        if m not in code:
            issues.append(f"missing: {m}")

    # 3. can_short must be True for futures. We accept any of:
    #    can_short = True / can_short=True / `can_short  =  True`
    if "can_short" not in code or "can_short = False" in code.replace(" ", ""):
        # Allow whitespace variations
        import re as _re
        if not _re.search(r"can_short\s*=\s*True", code):
            issues.append("missing: can_short = True (required for futures)")

    # 4. Substance check — count real code lines (not blank, not comment,
    # not pure-string lines like docstring content). A real strategy is
    # typically 40-100 lines; pure-stub strategies are 8-15.
    real_lines = 0
    in_docstring = False
    for raw in code.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Track triple-quoted strings; lines inside docstrings don't count.
        triple_count = line.count('"""') + line.count("'''")
        if triple_count == 1:
            in_docstring = not in_docstring
            continue
        if in_docstring:
            continue
        if line.startswith("#"):
            continue
        real_lines += 1
    if real_lines < 25:
        issues.append(
            f"code body too short ({real_lines} real lines) — looks like a "
            "stub. Real strategies need at least populate_indicators + "
            "populate_entry_trend bodies with actual indicator math."
        )

    return (len(issues) == 0, issues)


REPAIR_PROMPT = (
    "Your previous output was INCOMPLETE or BROKEN. Issues found: {missing}. "
    "Generate the FULL strategy code again — every required method body "
    "must be implemented in pandas/talib, not left as a stub. "
    "Both enter_long AND enter_short must be set on every bar. "
    "Set `can_short = True` as a class attribute (we trade futures). "
    "Output ONLY valid Python that compiles — no pseudo-code, no English, "
    "no `pd.Data dataframe` typos, no broken empty tuples."
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
    # enter_long/enter_short, syntax errors, etc.) try ONE repair
    # round-trip with the broken output attached + a stern follow-up.
    ok, issues = _validate_generated_code(code)
    if not ok:
        repair_messages = initial_messages + [
            {"role": "assistant", "content": code},
            {"role": "user",
             "content": REPAIR_PROMPT.format(missing="; ".join(issues))},
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

    final_ok, final_issues = _validate_generated_code(code)
    return {
        "code": code,
        "model_used": data.get("model", model),
        "tokens_used": data.get("usage", {}),
        "validation_passed": final_ok,
        "validation_missing": final_issues,   # legacy field name; carries full issues
        "validation_issues":  final_issues,
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
