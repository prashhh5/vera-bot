"""
server.py — Vera AI Challenge HTTP Bot
Run: uvicorn server:app --host 0.0.0.0 --port 8080

Implements all 5 endpoints required by challenge-testing-brief.md:
  GET  /v1/healthz
  GET  /v1/metadata
  POST /v1/context
  POST /v1/tick
  POST /v1/reply
"""

import json
import os
import time
import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import bot

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vera-bot")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Vera AI Challenge Bot", version="1.0.0")

# ── In-memory context store ───────────────────────────────────────────────────
# Structure: contexts[(scope, context_id)] = {"data": {...}, "version": int}
contexts: dict = {}

# conversation state per merchant: tracks auto-reply count and last body
conv_state: dict = {}

# Track composed messages for /v1/reply
last_composed: dict = {}

# ── Data loading ──────────────────────────────────────────────────────────────
SEED_DIR = Path(os.getenv("SEED_DIR", "./seeds"))
EXPANDED_DIR = Path(os.getenv("EXPANDED_DIR", "./expanded"))

CATEGORY_FILES = {
    "dentists":    "dentists.json",
    "gyms":        "gyms.json",
    "salons":      "salons.json",
    "pharmacies":  "pharmacies.json",
    "restaurants": "restaurants.json",
}

_category_cache: dict = {}

def load_category(slug: str) -> dict:
    if slug not in _category_cache:
        for search_dir in [SEED_DIR, Path(".")]:
            p = search_dir / CATEGORY_FILES.get(slug, f"{slug}.json")
            if p.exists():
                with open(p) as f:
                    data = json.load(f)
                    data["slug"] = slug
                    _category_cache[slug] = data
                    break
        else:
            _category_cache[slug] = {"slug": slug}
    return _category_cache[slug]


# ── Pydantic schemas ──────────────────────────────────────────────────────────
class ContextPayload(BaseModel):
    scope: str          # "merchant" | "customer" | "trigger" | "category"
    context_id: str
    version: int
    data: dict


class TickPayload(BaseModel):
    merchant_id: str
    trigger_id: str
    customer_id: Optional[str] = None


class ReplyPayload(BaseModel):
    merchant_id: str
    merchant_message: str
    turn: int


# ── Helper: resolve all 4 contexts for a tick ─────────────────────────────────
def _resolve_contexts(merchant_id: str, trigger_id: str,
                      customer_id: Optional[str]):
    merchant = None
    trigger = None
    customer = None

    # merchant context
    key_m = ("merchant", merchant_id)
    if key_m in contexts:
        merchant = contexts[key_m]["data"]

    # trigger context
    key_t = ("trigger", trigger_id)
    if key_t in contexts:
        trigger = contexts[key_t]["data"]

    # customer context
    if customer_id:
        key_c = ("customer", customer_id)
        if key_c in contexts:
            customer = contexts[key_c]["data"]

    # category context — derive from merchant
    cat_slug = None
    if merchant:
        cat_slug = merchant.get("category_slug", "")
    category = load_category(cat_slug) if cat_slug else {}

    return category, merchant, trigger, customer


# ── Idempotency check ─────────────────────────────────────────────────────────
def _store_context(payload: ContextPayload) -> tuple[int, str]:
    """
    Returns (http_status, message).
    409 if same version already stored, 201 for new, 200 for update.
    """
    key = (payload.scope, payload.context_id)
    existing = contexts.get(key)

    if existing and existing["version"] == payload.version:
        return 409, "Version already stored"

    is_new = existing is None
    contexts[key] = {"data": payload.data, "version": payload.version}
    return 201 if is_new else 200, "Stored"


# ── Auto-reply detection ───────────────────────────────────────────────────────
AUTO_REPLY_PATTERNS = {
    "ok", "okay", "haan", "ha", "theek hai", "thik hai", "sure",
    "accha", "sahi hai", "got it", "👍", "fine", "yes", "yeah",
}

def _is_auto_reply(text: str) -> bool:
    cleaned = text.strip().lower().rstrip(".")
    return cleaned in AUTO_REPLY_PATTERNS or len(cleaned) < 6


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/v1/healthz")
async def healthz():
    """
    Required by challenge-testing-brief.md §3.1.
    Returns contexts_loaded counts and readiness status.
    Three consecutive failures = -10 penalty.
    """
    merchant_count = sum(1 for k in contexts if k[0] == "merchant")
    trigger_count = sum(1 for k in contexts if k[0] == "trigger")
    customer_count = sum(1 for k in contexts if k[0] == "customer")
    category_count = len(_category_cache)

    return JSONResponse({
        "status": "ok",
        "contexts_loaded": {
            "merchant": merchant_count,
            "trigger": trigger_count,
            "customer": customer_count,
            "category": category_count,
        },
        "version": "1.0.0",
        "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
    }, status_code=200)


@app.get("/v1/metadata")
async def metadata():
    """
    Required by challenge-testing-brief.md §3.2.
    Returns bot capability declaration.
    """
    return JSONResponse({
        "bot_name": "Vera-Submit",
        "version": "1.0.0",
        "author": "Challenge Submission",
        "categories_supported": list(CATEGORY_FILES.keys()),
        "trigger_kinds_supported": list(bot.FRAMING.keys()),
        "llm_backend": bot.MODEL,
        "max_message_length": 450,
        "supports_multilingual": True,
        "languages": ["en", "hi", "hi-en mix"],
        "context_ttl_seconds": 3600,
    }, status_code=200)


@app.post("/v1/context")
async def push_context(payload: ContextPayload):
    """
    Required by challenge-testing-brief.md §3.3.
    Stores a single context unit. Returns 409 on version collision.
    Supports batch: the judge pushes contexts one by one before each tick.
    """
    status_code, message = _store_context(payload)

    if status_code == 409:
        return JSONResponse({
            "accepted": False,
            "reason": message,
            "scope": payload.scope,
            "context_id": payload.context_id,
            "version": payload.version,
        }, status_code=409)

    log.info(f"Context stored: scope={payload.scope} id={payload.context_id} v={payload.version}")

    return JSONResponse({
        "accepted": True,
        "scope": payload.scope,
        "context_id": payload.context_id,
        "version": payload.version,
        "stored_at": time.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
    }, status_code=status_code)


@app.post("/v1/tick")
async def tick(payload: TickPayload):
    """
    Required by challenge-testing-brief.md §3.4.
    Core composition endpoint. 15-second timeout budget.
    Returns composed message + metadata, or action=skip if no trigger action needed.
    """
    t0 = time.time()

    category, merchant, trigger, customer = _resolve_contexts(
        payload.merchant_id, payload.trigger_id, payload.customer_id
    )

    # Guard: missing critical context
    if not merchant:
        log.warning(f"tick: no merchant context for {payload.merchant_id}")
        return JSONResponse({
            "action": "skip",
            "reason": f"Merchant context not loaded: {payload.merchant_id}",
            "latency_ms": int((time.time() - t0) * 1000),
        }, status_code=200)

    if not trigger:
        log.warning(f"tick: no trigger context for {payload.trigger_id}")
        return JSONResponse({
            "action": "skip",
            "reason": f"Trigger context not loaded: {payload.trigger_id}",
            "latency_ms": int((time.time() - t0) * 1000),
        }, status_code=200)

    # Check urgency — urgency 0 means suppressed
    if trigger.get("urgency", 1) == 0:
        return JSONResponse({
            "action": "skip",
            "reason": "Trigger suppressed (urgency=0)",
            "latency_ms": int((time.time() - t0) * 1000),
        }, status_code=200)

    try:
        composed = bot.compose(category, merchant, trigger, customer)
    except Exception as e:
        log.error(f"tick: compose failed: {e}")
        return JSONResponse({
            "action": "error",
            "reason": str(e),
            "latency_ms": int((time.time() - t0) * 1000),
        }, status_code=500)

    # Store for /v1/reply to reference
    last_composed[payload.merchant_id] = {
        "body": composed["body"],
        "trigger_kind": trigger.get("kind"),
        "trigger_id": payload.trigger_id,
    }

    # Reset auto-reply counter on new trigger
    conv_state[payload.merchant_id] = {"auto_reply_count": 0, "last_body": composed["body"]}

    latency = int((time.time() - t0) * 1000)
    log.info(f"tick: merchant={payload.merchant_id} trigger={trigger.get('kind')} latency={latency}ms")

    return JSONResponse({
        "action": "send",
        "message": {
            "body": composed["body"],
            "cta": composed["cta"],
            "send_as": composed["send_as"],
        },
        "suppression_key": composed["suppression_key"],
        "rationale": composed["rationale"],
        "latency_ms": latency,
    }, status_code=200)


@app.post("/v1/reply")
async def reply(payload: ReplyPayload):
    """
    Required by challenge-testing-brief.md §3.5.
    Handles merchant's reply during an active conversation.
    Returns action=end after 3 auto-replies (anti-auto-reply-hell).
    """
    t0 = time.time()
    mid = payload.merchant_id
    msg = payload.merchant_message.strip()
    turn = payload.turn

    # --- Hostile / opt-out detection ---
    OPT_OUT_SIGNALS = [
        "stop", "nahi chahiye", "band karo", "don't contact",
        "remove me", "unsubscribe", "block", "not interested",
        "leave me alone", "nahin", "nahi"
    ]
    if any(sig in msg.lower() for sig in OPT_OUT_SIGNALS):
        log.info(f"reply: opt-out detected from {mid}")
        return JSONResponse({
            "action": "end",
            "message": {
                "body": "Understood. I won't reach out again. You can always type 'Hi Vera' whenever you want to reconnect.",
                "cta": "none",
                "send_as": "vera",
            },
            "reason": "merchant_opted_out",
            "latency_ms": int((time.time() - t0) * 1000),
        }, status_code=200)

    # --- Auto-reply hell detection ---
    state = conv_state.get(mid, {"auto_reply_count": 0, "last_body": ""})

    if _is_auto_reply(msg):
        state["auto_reply_count"] = state.get("auto_reply_count", 0) + 1
    else:
        state["auto_reply_count"] = 0

    conv_state[mid] = state

    if state["auto_reply_count"] >= 3:
        log.info(f"reply: auto-reply hell detected for {mid}, ending")
        return JSONResponse({
            "action": "end",
            "message": {
                "body": "Got it! I'll check back in later when there's something specific to act on.",
                "cta": "none",
                "send_as": "vera",
            },
            "reason": "auto_reply_loop_exit",
            "latency_ms": int((time.time() - t0) * 1000),
        }, status_code=200)

    # --- Intent transition: merchant says yes to something ---
    INTENT_YES = ["ok lets do it", "okay let's do it", "haan karo", "kar do", "yes do it", "proceed", "go ahead"]
    if any(sig in msg.lower() for sig in INTENT_YES):
        last = last_composed.get(mid, {})
        followup = f"Great! I'll set that up now. Give me a moment. You'll have the draft ready before end of day."
        return JSONResponse({
            "action": "continue",
            "message": {
                "body": followup,
                "cta": "none",
                "send_as": "vera",
            },
            "reason": "intent_transition_confirmed",
            "latency_ms": int((time.time() - t0) * 1000),
        }, status_code=200)

    # --- General reply: pass back to compose with conversation context ---
    # Resolve last known contexts for this merchant
    key_m = ("merchant", mid)
    merchant = contexts.get(key_m, {}).get("data", {})
    cat_slug = merchant.get("category_slug", "")
    category = load_category(cat_slug) if cat_slug else {}

    # Build a lightweight "conversation follow-up" trigger
    followup_trigger = {
        "id": f"reply_turn_{turn}_{mid}",
        "scope": "merchant",
        "kind": "active_planning_intent",
        "source": "merchant_reply",
        "urgency": 4,
        "suppression_key": f"reply:{mid}:turn:{turn}",
        "expires_at": None,
        "payload": {
            "intent_topic": "merchant_reply_followup",
            "merchant_last_message": msg,
        },
    }

    try:
        composed = bot.compose(category, merchant, followup_trigger, None)
    except Exception as e:
        log.error(f"reply: compose failed: {e}")
        composed = {
            "body": "Could you share a bit more detail? I want to make sure I get this right.",
            "cta": "open_ended",
            "send_as": "vera",
            "rationale": "Fallback on compose error",
            "suppression_key": followup_trigger["suppression_key"],
        }

    latency = int((time.time() - t0) * 1000)
    log.info(f"reply: merchant={mid} turn={turn} latency={latency}ms")

    return JSONResponse({
        "action": "continue",
        "message": {
            "body": composed["body"],
            "cta": composed["cta"],
            "send_as": composed["send_as"],
        },
        "latency_ms": latency,
    }, status_code=200)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    log.info(f"Starting Vera bot on port {port}")
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
