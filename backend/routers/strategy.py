import os
from pathlib import Path
from fastapi import APIRouter, Depends, UploadFile, File, Form, Request
from pydantic import BaseModel
from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from backend.models import get_db, Config, Strategy
from backend.utils.encryption import decrypt, DecryptError
from backend.utils.clerk_auth import get_user_id
from backend.utils.validators import validate_strategy_code
from backend.utils.rate_limit import limiter, AI_LIMIT
from backend.services.strategy_parser import parse_with_retry, ai_assist, DEFAULT_MODEL

router = APIRouter(prefix="/api/strategy", tags=["strategy"])


class ValidateRequest(BaseModel):
    code: str

TEMPLATES_DIR = Path("strategies/templates")
USER_STRATEGIES_DIR = Path("strategies/user_generated")


class ParseRequest(BaseModel):
    text: str
    model: str | None = None


class AiAssistRequest(BaseModel):
    prompt: str
    existing_code: str
    model: str | None = None


class UpdateStrategyRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    generated_code: str | None = None
    timeframe: str | None = None
    pairs: list[str] | None = None
    stoploss: float | None = None
    take_profit: float | None = None       # e.g. 0.015 = 1.5%
    default_leverage: int | None = None    # e.g. 10 = 10x for futures


def _get_openrouter_key(db: Session, user_id: str) -> str:
    config = db.execute(
        select(Config).where(Config.user_id == user_id).limit(1)
    ).scalar_one_or_none()
    if not config or not config.openrouter_key_enc:
        raise ValueError("OpenRouter key not configured. Visit /setup to add one.")
    try:
        return decrypt(config.openrouter_key_enc, user_id)
    except DecryptError:
        raise ValueError(
            "Your OpenRouter API key could not be decrypted (server secret changed). "
            "Please go to Setup and re-enter your OpenRouter key to continue."
        )


def _get_preferred_model(db: Session, user_id: str) -> str:
    config = db.execute(
        select(Config).where(Config.user_id == user_id).limit(1)
    ).scalar_one_or_none()
    # Return the user's stored preference if it's non-empty, else the default.
    # parse_with_retry will fall through to parse_with_fallback if the stored
    # model no longer exists on OpenRouter.
    stored = config.preferred_model if config else None
    return stored or DEFAULT_MODEL


@router.post("/upload")
@limiter.limit(AI_LIMIT)
async def upload_strategy(
    request: Request,
    file: UploadFile = File(None),
    text: str = Form(None),
    name: str = Form("My Strategy"),
    model: str = Form(None),
    skip_ai: str = Form("false"),
    auto_trade: str = Form("false"),
    auto_trade_mode: str = Form("paper"),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    try:
        return await _upload_strategy_impl(
            file=file,
            text=text,
            name=name,
            model=model,
            skip_ai=skip_ai,
            auto_trade=auto_trade,
            auto_trade_mode=auto_trade_mode,
            db=db,
            user_id=user_id,
        )
    except Exception as e:  # noqa: BLE001 - surface *anything* as JSON so the UI never sees plain-text 500s
        return {"error": f"{type(e).__name__}: {e}"}


async def _upload_strategy_impl(
    *,
    file: UploadFile | None,
    text: str | None,
    name: str,
    model: str | None,
    skip_ai: str,
    auto_trade: str,
    auto_trade_mode: str,
    db: Session,
    user_id: str,
):
    # Extract text from file or use direct text input
    strategy_text = ""
    if file:
        content = await file.read()
        filename = file.filename or ""
        if filename.endswith(".pdf"):
            from PyPDF2 import PdfReader
            import io
            reader = PdfReader(io.BytesIO(content))
            strategy_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        elif filename.endswith(".docx"):
            from docx import Document
            import io
            doc = Document(io.BytesIO(content))
            strategy_text = "\n".join(p.text for p in doc.paragraphs)
        else:
            strategy_text = content.decode("utf-8", errors="ignore")
    elif text:
        strategy_text = text
    else:
        return {"error": "No file or text provided"}

    if not strategy_text.strip():
        return {"error": "Empty strategy text"}

    # If skip_ai is set (e.g., "Use Template" saves known-good freqtrade code),
    # we skip the LLM round-trip entirely and store the text verbatim as code.
    use_ai = str(skip_ai).lower() not in ("true", "1", "yes")

    if use_ai:
        try:
            api_key = _get_openrouter_key(db, user_id)
        except ValueError as e:
            return {"error": str(e)}
        use_model = model or _get_preferred_model(db, user_id)
        try:
            result = await parse_with_retry(strategy_text, api_key, use_model)
        except Exception as e:
            return {"error": str(e)}
        code = result["code"]
        model_used = result["model_used"]
        tokens_used = result.get("tokens_used", {})
        description = f"Parsed from uploaded document using {model_used}"
        # Forward the new validation status from strategy_parser so the
        # frontend can warn loudly if the LLM still emitted a stub.
        ai_validation = {
            "passed":  result.get("validation_passed", True),
            "missing": result.get("validation_missing", []),
        }
    else:
        code = strategy_text
        model_used = None
        tokens_used = {}
        description = "Imported directly (no AI parsing)"
        ai_validation = {"passed": True, "missing": []}

    validation = validate_strategy_code(code)

    auto_flag = str(auto_trade).lower() in ("true", "1", "yes", "on")
    mode_flag = "live" if str(auto_trade_mode).lower() == "live" else "paper"

    # ── Extract trading params from generated code ──────────────────────────
    # Parse stoploss, minimal_roi (→ take_profit), and timeframe if present
    import re as _re
    def _extract_float(pattern: str, src: str, default: float) -> float:
        m = _re.search(pattern, src)
        return float(m.group(1)) if m else default

    sl_match   = _re.search(r'stoploss\s*=\s*(-?\d+\.?\d*)', code)
    roi_match  = _re.search(r'minimal_roi\s*=\s*\{[^}]*["\']0["\']\s*:\s*(\d+\.?\d*)', code)
    tf_match   = _re.search(r'timeframe\s*=\s*["\'](\w+)["\']', code)

    code_stoploss    = float(sl_match.group(1))  if sl_match  else -0.03
    code_take_profit = float(roi_match.group(1)) if roi_match else 0.015
    code_timeframe   = tf_match.group(1)          if tf_match  else "15m"

    strategy = Strategy(
        user_id=user_id,
        name=name,
        description=description,
        original_text=strategy_text,
        generated_code=code,
        model_used=model_used,
        auto_trade_enabled=auto_flag,
        auto_trade_mode=mode_flag,
        stoploss=code_stoploss,
        take_profit=code_take_profit,
        default_leverage=10,        # sensible futures default; user can change in editor
        timeframe=code_timeframe,
    )
    db.add(strategy)
    db.commit()
    db.refresh(strategy)

    user_dir = USER_STRATEGIES_DIR / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    filename = f"strategy_{strategy.id}.py"
    with open(user_dir / filename, "w") as f:
        f.write(code)

    return {
        "id": strategy.id,
        "code": code,
        "model_used": model_used,
        "tokens_used": tokens_used,
        "validation": validation,
        "ai_validation": ai_validation,
        "original_text": strategy_text[:500],
    }


@router.post("/validate")
def validate_strategy(req: ValidateRequest):
    """Validate strategy code locally (AST + safety checks). No AI, no API keys."""
    return validate_strategy_code(req.code)


@router.post("/parse")
@limiter.limit(AI_LIMIT)
async def reparse_strategy(
    req: ParseRequest,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    try:
        try:
            api_key = _get_openrouter_key(db, user_id)
        except ValueError as e:
            return {"error": str(e)}
        use_model = req.model or _get_preferred_model(db, user_id)
        result = await parse_with_retry(req.text, api_key, use_model)
        validation = validate_strategy_code(result["code"])
        return {
            "code": result["code"],
            "model_used": result["model_used"],
            "validation": validation,
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@router.post("/ai-assist")
@limiter.limit(AI_LIMIT)
async def strategy_ai_assist(
    req: AiAssistRequest,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    try:
        try:
            api_key = _get_openrouter_key(db, user_id)
        except ValueError as e:
            return {"error": str(e)}
        use_model = req.model or _get_preferred_model(db, user_id)
        code = await ai_assist(req.prompt, req.existing_code, api_key, use_model)
        validation = validate_strategy_code(code)
        return {"code": code, "validation": validation}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@router.get("/list")
def list_strategies(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    from sqlalchemy import or_
    try:
        result = db.execute(
            select(Strategy)
            .where(or_(Strategy.user_id == user_id, Strategy.is_template == True))  # noqa: E712
            .order_by(Strategy.is_template.desc(), Strategy.created_at.desc())
        )
        strategies = result.scalars().all()
    except Exception:
        # Fallback: skip template filter if new columns haven't migrated yet
        db.rollback()
        try:
            result = db.execute(
                select(Strategy).where(Strategy.user_id == user_id)
                .order_by(Strategy.created_at.desc())
            )
            strategies = result.scalars().all()
        except Exception:
            db.rollback()
            strategies = []

    return {
        "strategies": [
            {
                "id":                 s.id,
                "name":               s.name,
                "description":        s.description,
                "timeframe":          s.timeframe or "15m",
                "is_template":        getattr(s, "is_template", False),
                "auto_trade_enabled": bool(getattr(s, "auto_trade_enabled", False)),
                "auto_trade_mode":    getattr(s, "auto_trade_mode", "paper"),
                # Trading config — auto-fill stop-loss, take-profit, leverage on frontend
                "stoploss":           getattr(s, "stoploss", -0.03) or -0.03,
                "take_profit":        getattr(s, "take_profit", 0.015) or 0.015,
                "default_leverage":   getattr(s, "default_leverage", 1) or 1,
                "created_at":         str(s.created_at),
            }
            for s in strategies
        ]
    }


@router.post("/{strategy_id}/auto-trade")
def set_strategy_auto_trade(
    strategy_id: int,
    enabled: bool = True,
    mode: str = "paper",
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Toggle the auto-trade flag on a single strategy. The autotrade engine
    only considers strategies that have this flag set when picking which
    template to deploy from a scanner recommendation."""
    s = db.execute(
        select(Strategy).where(Strategy.id == strategy_id, or_(Strategy.user_id == user_id, Strategy.is_template == True))  # noqa: E712
    ).scalar_one_or_none()
    if not s:
        return {"error": "Strategy not found"}
    s.auto_trade_enabled = bool(enabled)
    s.auto_trade_mode = "live" if mode == "live" else "paper"
    db.commit()
    return {"id": s.id, "auto_trade_enabled": s.auto_trade_enabled, "auto_trade_mode": s.auto_trade_mode}


@router.get("/templates")
async def get_templates():
    templates = []
    for f in TEMPLATES_DIR.glob("*.py"):
        content = f.read_text()
        # Extract class name
        name = f.stem
        for line in content.split("\n"):
            if line.startswith("class ") and "IStrategy" in line:
                name = line.split("(")[0].replace("class ", "").strip()
                break
        templates.append({"file": f.name, "name": name, "code": content})
    return {"templates": templates}


@router.get("/{strategy_id}")
def get_strategy(
    strategy_id: int,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    result = db.execute(
        select(Strategy).where(Strategy.id == strategy_id, or_(Strategy.user_id == user_id, Strategy.is_template == True))  # noqa: E712
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        return {"error": "Strategy not found"}
    return {
        "id":               strategy.id,
        "name":             strategy.name,
        "description":      strategy.description,
        "original_text":    strategy.original_text,
        "generated_code":   strategy.generated_code,
        "model_used":       strategy.model_used,
        "indicators":       strategy.indicators,
        "timeframe":        strategy.timeframe,
        "pairs":            strategy.pairs,
        "stoploss":         strategy.stoploss,
        "take_profit":      getattr(strategy, "take_profit", 0.015) or 0.015,
        "default_leverage": getattr(strategy, "default_leverage", 10) or 10,
        "is_template":      strategy.is_template,
        "created_at":       str(strategy.created_at),
    }


@router.put("/{strategy_id}")
def update_strategy(
    strategy_id: int,
    req: UpdateStrategyRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    result = db.execute(
        select(Strategy).where(Strategy.id == strategy_id, or_(Strategy.user_id == user_id, Strategy.is_template == True))  # noqa: E712
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        return {"error": "Strategy not found"}

    for field, value in req.model_dump(exclude_none=True).items():
        setattr(strategy, field, value)

    db.commit()

    if req.generated_code:
        user_dir = USER_STRATEGIES_DIR / user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        with open(user_dir / f"strategy_{strategy_id}.py", "w") as f:
            f.write(req.generated_code)

    return {"status": "ok", "id": strategy_id}


@router.delete("/{strategy_id}")
def delete_strategy(
    strategy_id: int,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    result = db.execute(
        select(Strategy).where(Strategy.id == strategy_id, or_(Strategy.user_id == user_id, Strategy.is_template == True))  # noqa: E712
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        return {"error": "Strategy not found"}

    db.delete(strategy)
    db.commit()

    fpath = USER_STRATEGIES_DIR / user_id / f"strategy_{strategy_id}.py"
    if fpath.exists():
        fpath.unlink()

    return {"status": "deleted", "id": strategy_id}


@router.post("/dedupe")
def dedupe_strategies(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """
    Delete duplicate user-created strategies, keeping only the most recent
    instance of each unique name. Templates (is_template=True) are NEVER
    touched — only user-uploaded strategies with duplicate names are pruned.
    """
    user_strategies = db.execute(
        select(Strategy)
        .where(Strategy.user_id == user_id, Strategy.is_template == False)  # noqa: E712
        .order_by(Strategy.created_at.desc())
    ).scalars().all()

    seen_names: set[str] = set()
    deleted_ids: list[int] = []
    for s in user_strategies:
        if s.name in seen_names:
            # Older duplicate — delete it
            fpath = USER_STRATEGIES_DIR / user_id / f"strategy_{s.id}.py"
            if fpath.exists():
                try:
                    fpath.unlink()
                except Exception:
                    pass
            deleted_ids.append(s.id)
            db.delete(s)
        else:
            seen_names.add(s.name)

    if deleted_ids:
        db.commit()

    return {
        "status":     "ok",
        "deleted":    len(deleted_ids),
        "deleted_ids": deleted_ids,
        "kept":       len(seen_names),
    }
