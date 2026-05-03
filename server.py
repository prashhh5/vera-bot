import json
import os
import time
import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vera")

app = FastAPI(title="Vera Bot v3", version="3.0.0")

contexts = {}
conv_state = {}

CATEGORY_DATA = {}
SEED_MERCHANTS = {}
SEED_TRIGGERS = {}
SEED_CUSTOMERS = {}

CATEGORY_SLUGS = ["dentists", "gyms", "salons", "pharmacies", "restaurants"]


# -------------------------
# Utils
# -------------------------

def _load_json(p):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception as e:
        log.error(f"Failed loading {p}: {e}")
        return {}


def _find(fname):
    dirs = [
        os.getenv("SEED_DIR", ""),
        os.getenv("EXPANDED_DIR", ""),
        "dataset",
        "dataset/categories",
        "seeds",
        "expanded",
        "."
    ]
    for d in dirs:
        if not d:
            continue
        p = Path(d) / fname
        if p.exists():
            return p
    return None


# -------------------------
# STARTUP FIXED
# -------------------------

@app.on_event("startup")
async def startup():
    log.info("Startup loading...")

    # Categories
    for slug in CATEGORY_SLUGS:
        p = _find(f"{slug}.json")
        if p:
            d = _load_json(p)
            d["slug"] = slug
            CATEGORY_DATA[slug] = d

    # Merchants
    p = _find("merchants_seed.json")
    if p:
        raw = _load_json(p)
        items = raw.get("merchants", raw) if isinstance(raw, dict) else raw
        for m in items:
            mid = m.get("merchant_id") or m.get("id")
            if mid:
                SEED_MERCHANTS[mid] = m

    # Expanded merchants
    for d in ["expanded/merchants", "expanded"]:
        dp = Path(d)
        if dp.exists():
            for fp in list(dp.glob("m_*.json"))[:60]:
                try:
                    m = _load_json(fp)
                    mid = m.get("merchant_id") or fp.stem
                    SEED_MERCHANTS[mid] = m
                except:
                    pass
            break

    # Triggers
    p = _find("triggers_seed.json")
    if p:
        raw = _load_json(p)
        items = raw.get("triggers", raw) if isinstance(raw, dict) else raw
        for t in items:
            tid = t.get("id") or t.get("trigger_id")
            if tid:
                SEED_TRIGGERS[tid] = t

    # Expanded triggers
    for d in ["expanded/triggers", "expanded"]:
        dp = Path(d)
        if dp.exists():
            for fp in list(dp.glob("trg_*.json"))[:110]:
                try:
                    t = _load_json(fp)
                    tid = t.get("id") or fp.stem
                    SEED_TRIGGERS[tid] = t
                except:
                    pass
            break

    # Customers
    p = _find("customers_seed.json")
    if p:
        raw = _load_json(p)
        items = raw.get("customers", raw) if isinstance(raw, dict) else raw
        for c in items:
            cid = c.get("customer_id") or c.get("id")
            if cid:
                SEED_CUSTOMERS[cid] = c

    log.info(f"Startup done | categories={len(CATEGORY_DATA)} merchants={len(SEED_MERCHANTS)} triggers={len(SEED_TRIGGERS)}")


# -------------------------
# Health
# -------------------------

@app.get("/v1/healthz")
async def healthz():
    return JSONResponse({
        "status": "ok",
        "version": "3.0.0",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")
    })


# -------------------------
# CORE LOGIC (UNCHANGED SAFE)
# -------------------------

def _ctx(scope, cid):
    key = (scope, cid)
    if key in contexts:
        return contexts[key]["data"]
    if scope == "merchant":
        return SEED_MERCHANTS.get(cid)
    if scope == "trigger":
        return SEED_TRIGGERS.get(cid)
    if scope == "customer":
        return SEED_CUSTOMERS.get(cid)
    return None


def _stub_merchant(mid):
    return {
        "merchant_id": mid,
        "category_slug": "restaurants",
        "identity": {"name": mid, "city": "Delhi"},
        "performance": {"views": 1000, "calls": 10, "ctr": 0.02},
    }


def _stub_trigger(tid):
    return {
        "id": tid,
        "kind": "general",
        "scope": "merchant",
        "urgency": 3,
        "payload": {}
    }


@app.post("/v1/tick")
async def tick(request: Request):
    try:
        body = await request.json()
    except:
        return JSONResponse({"actions": []}, status_code=400)

    merchant_id = body.get("merchant_id", "")
    trigger_ids = body.get("available_triggers", [])

    merchant = _ctx("merchant", merchant_id) or _stub_merchant(merchant_id)

    actions = []

    for tid in trigger_ids:
        trigger = _ctx("trigger", tid) or _stub_trigger(tid)

        try:
            composed = bot.compose({}, merchant, trigger, None)
        except Exception as e:
            log.error(f"compose failed: {e}")
            composed = {"body": "Fallback message", "cta": "none"}

        actions.append({
            "trigger_id": tid,
            "action": "send",
            "body": composed.get("body", ""),
            "cta": composed.get("cta", "none")
        })

    return JSONResponse({"actions": actions})


# -------------------------
# ENTRYPOINT FIXED
# -------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("server:app", host="0.0.0.0", port=port)
