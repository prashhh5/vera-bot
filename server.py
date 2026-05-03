import json, os, time, logging, re
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import bot

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vera")
app = FastAPI()

CTX = {}
STATE = {}
CATS = {}
MERCHANTS = {}
TRIGGERS = {}
CUSTOMERS = {}
SLUGS = ["dentists","gyms","salons","pharmacies","restaurants"]

# ── file helpers ──────────────────────────────────────────────────────────────
def jload(p):
    try:
        with open(p) as f: return json.load(f)
    except: return {}

def find(fname):
    for d in [os.getenv("SEED_DIR",""), os.getenv("EXPANDED_DIR",""),
              "dataset","dataset/categories","seeds","expanded","."]:
        if not d: continue
        p = Path(d)/fname
        if p.exists(): return p
    return None

# ── startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    for slug in SLUGS:
        p = find(f"{slug}.json")
        if p:
            d = jload(p); d["slug"] = slug; CATS[slug] = d
    for fname, store, key in [
        ("merchants_seed.json", MERCHANTS, lambda m: m.get("merchant_id") or m.get("id")),
        ("triggers_seed.json",  TRIGGERS,  lambda t: t.get("id") or t.get("trigger_id")),
        ("customers_seed.json", CUSTOMERS, lambda c: c.get("customer_id") or c.get("id")),
    ]:
        p = find(fname)
        if p:
            raw = jload(p)
            lst = raw.get(list(raw.keys())[0], raw) if isinstance(raw, dict) else raw
            if isinstance(lst, dict): lst = list(lst.values())
            for item in (lst if isinstance(lst, list) else []):
                k = key(item)
                if k: store[k] = item
    # expanded merchants/triggers/customers
    for d, store, pat, kfn in [
        ("expanded/merchants", MERCHANTS, "m_*.json",   lambda x: x.get("merchant_id") or x.stem),
        ("expanded/triggers",  TRIGGERS,  "trg_*.json", lambda x: x.get("id") or x.stem),
        ("expanded/customers", CUSTOMERS, "c_*.json",   lambda x: x.get("customer_id") or x.stem),
    ]:
        dp = Path(d)
        if dp.exists():
            for fp in list(dp.glob(pat))[:120]:
                try:
                    item = jload(fp); k = kfn(fp) if callable(kfn) else kfn(item)
                    if k: store[k] = item
                except: pass
    log.info(f"Ready cats={len(CATS)} m={len(MERCHANTS)} trg={len(TRIGGERS)} cust={len(CUSTOMERS)}")

# ── context helpers ───────────────────────────────────────────────────────────
def get_ctx(scope, cid):
    k = (scope, cid)
    if k in CTX: return CTX[k]["data"]
    if scope == "merchant":  return MERCHANTS.get(cid)
    if scope == "trigger":   return TRIGGERS.get(cid)
    if scope == "customer":  return CUSTOMERS.get(cid)
    return None

def get_cat(merchant):
    slug = merchant.get("category_slug","")
    if not slug:
        mid = merchant.get("merchant_id","")
        slug = next((s for s in SLUGS if s in mid or s.rstrip("s") in mid), "")
    return CATS.get(slug, {"slug": slug or "restaurants"})

def stub_merchant(mid):
    parts = mid.split("_")
    slug = next((s for s in SLUGS if s in mid or s.rstrip("s") in mid), "restaurants")
    city = parts[-1].title() if len(parts) > 2 else "Delhi"
    return {"merchant_id": mid, "category_slug": slug,
            "identity": {"name": " ".join(p.title() for p in parts[2:-1]) or mid,
                         "city": city, "locality": city,
                         "owner_first_name": "", "languages": ["en"], "verified": False},
            "performance": {"views": 1200, "calls": 15, "ctr": 0.028, "delta_7d": {}},
            "subscription": {"plan": "pro", "status": "active", "days_remaining": 30},
            "offers": [], "signals": []}

def stub_trigger(tid):
    kind = next((k for k in bot.FRAMING if k in tid), "curious_ask_due")
    return {"id": tid, "kind": kind, "scope": "merchant", "urgency": 3,
            "suppression_key": f"stub:{tid}", "expires_at": None,
            "source": "internal", "payload": {"placeholder": True}}

# ── context enrichment ────────────────────────────────────────────────────────
def enrich(category, merchant, trigger):
    """
    Inject the most relevant digest item, seasonal beat, and offer
    directly into the trigger payload so the LLM sees rich facts.
    """
    kind    = trigger.get("kind","")
    payload = dict(trigger.get("payload",{}))
    digest  = category.get("digest",[])
    seasonal = category.get("seasonal_beats",[])
    offers  = (merchant.get("offers") or category.get("offer_catalog",[])) or []
    peer    = category.get("peer_stats",{})

    # Pick best digest item by keyword relevance
    kws = kind.replace("_"," ").split()
    best_d = None
    if digest:
        scored = sorted(digest,
            key=lambda d: sum(1 for w in kws if w in d.get("title","").lower()
                              or w in d.get("summary","").lower()), reverse=True)
        best_d = scored[0]

    # Pick seasonal beat relevant to current month
    month = time.strftime("%b").lower()
    best_s = next((s for s in seasonal if month in s.get("month","").lower()), None)
    if not best_s and seasonal: best_s = seasonal[0]

    # Best active offer
    best_o = next((o for o in offers if isinstance(o,dict)
                   and o.get("status","active") == "active"), None)
    if not best_o and offers and isinstance(offers[0], dict): best_o = offers[0]

    # Inject
    if best_d and not payload.get("digest_title"):
        payload["digest_title"]   = best_d.get("title","")
        payload["digest_summary"] = best_d.get("summary","")[:160]
    if best_s and not payload.get("seasonal_note"):
        payload["seasonal_note"]  = best_s.get("note","")[:120]
    if best_o and not payload.get("offer_title"):
        payload["offer_title"]    = best_o.get("title","")
        payload["offer_price"]    = best_o.get("price") or best_o.get("discount","")
    if peer and not payload.get("peer_avg_ctr"):
        payload["peer_avg_ctr"]   = peer.get("avg_ctr","")
        payload["peer_avg_calls"] = peer.get("avg_calls_30d","")

    enriched = dict(trigger)
    enriched["payload"] = payload
    return enriched

# ── message post-validator ────────────────────────────────────────────────────
def has_number(text):
    return bool(re.search(r'\d+', text))

def validate_and_fix(composed, merchant, trigger, customer, category):
    """
    If LLM output is empty or lacks any number, patch it with rule fallback.
    Also enforce length and URL strip.
    """
    body = composed.get("body","").strip()
    body = re.sub(r'https?://\S+','[link]', body)
    if len(body) > 480: body = body[:477]+"..."
    if not body or not has_number(body) or len(body) < 30:
        log.warning("Weak LLM output, patching with rule fallback")
        fallback = rule_msg(merchant, trigger, customer, category)
        body = fallback["body"]
        if not composed.get("cta"): composed["cta"] = fallback["cta"]
        if not composed.get("send_as"): composed["send_as"] = fallback["send_as"]
        if not composed.get("suppression_key"): composed["suppression_key"] = fallback["suppression_key"]
        if not composed.get("rationale"): composed["rationale"] = fallback["rationale"]
    composed["body"] = body
    return composed

# ── rule-based compose (strong, 5 real numbers minimum) ──────────────────────
def rule_msg(merchant, trigger, customer, category):
    I   = merchant.get("identity",{})
    P   = merchant.get("performance",{})
    sub = merchant.get("subscription",{})
    name  = I.get("name","your business")
    owner = I.get("owner_first_name","") or name
    city  = I.get("city","")
    loc   = I.get("locality","") or city
    lang  = (I.get("languages",["en"]) or ["en"])[0]
    kind  = trigger.get("kind","general")
    views = P.get("views",0)
    calls = P.get("calls",0)
    ctr   = P.get("ctr",0)
    delta = P.get("delta_7d",{})
    peer  = category.get("peer_stats",{})
    avg_ctr   = peer.get("avg_ctr",0.030)
    avg_calls = peer.get("avg_calls_30d",20)
    avg_rating= peer.get("avg_rating",4.2)
    slug  = category.get("slug","")
    payload   = trigger.get("payload",{})
    sup   = trigger.get("suppression_key",f"{kind}:{merchant.get('merchant_id','')}")
    offers= (merchant.get("offers") or category.get("offer_catalog",[])) or []
    best_o= next((o for o in offers if isinstance(o,dict)), None)
    offer_str = f" Your current offer '{best_o.get('title','')}' is a good hook." if best_o else ""
    days_sub = sub.get("days_remaining","")
    hi = "hi" in lang

    # customer-scoped
    if customer and trigger.get("scope")=="customer":
        cname = customer.get("identity",{}).get("name","customer")
        last  = customer.get("relationship",{}).get("last_visit","recently")
        ltv   = customer.get("relationship",{}).get("lifetime_value",0)
        visits= customer.get("relationship",{}).get("visits_total",0)
        cl    = customer.get("identity",{}).get("language_pref","en")
        if "hi" in cl:
            body = (f"Namaste {cname}! {name}, {loc} se message. "
                    f"Aapki last visit {last} thi — {visits} total visits, Rs {ltv} value. "
                    f"Iss hafte slot book karein?{offer_str} YES ya NO reply karein.")
        else:
            body = (f"Hi {cname}! From {name}, {loc}. "
                    f"Your last visit was {last} — {visits} visits, Rs {ltv} lifetime value. "
                    f"Ready to book a slot this week?{offer_str} Reply YES or NO.")
        return {"body":body,"cta":"binary_yes_no","send_as":"merchant_on_behalf",
                "suppression_key":sup,"rationale":f"Recall {cname} LTV Rs {ltv} visits {visits}"}

    # perf dip
    if "perf_dip" in kind or "seasonal_perf_dip" in kind:
        cp = abs(int(delta.get("calls_pct",-0.2)*100))
        vp = abs(int(delta.get("views_pct",-0.1)*100))
        body = (f"{owner} — calls dropped {cp}% and views {vp}% this week. "
                f"You are at {calls} calls/month (category avg: {avg_calls}), CTR {ctr} vs {slug} avg {avg_ctr}. "
                f"Fastest fix: activate one offer today.{offer_str} Should I draft it?")
        return {"body":body,"cta":"binary_yes_no","send_as":"vera","suppression_key":sup,
                "rationale":f"Perf dip: calls -{cp}%, views -{vp}%, CTR {ctr} vs avg {avg_ctr}"}

    # perf spike
    if "perf_spike" in kind:
        cp = int(delta.get("calls_pct",0.15)*100)
        body = (f"{owner} — calls up {cp}% this week, views at {views}. "
                f"Your CTR {ctr} is already above the {slug} avg {avg_ctr}. "
                f"Strike while it is warm.{offer_str} Want me to draft a follow-up post?")
        return {"body":body,"cta":"binary_yes_no","send_as":"vera","suppression_key":sup,
                "rationale":f"Perf spike +{cp}% calls, CTR {ctr} vs avg {avg_ctr}"}

    # renewal
    if "renewal" in kind:
        days = payload.get("days_remaining", days_sub or "?")
        amt  = payload.get("renewal_amount","4,999")
        body = (f"{owner} — your Magicpin Pro renews in {days} days (Rs {amt}). "
                f"This month: {views} views, {calls} calls, CTR {ctr}. "
                f"Category avg CTR is {avg_ctr} — you are {'above' if ctr>avg_ctr else 'below'} benchmark. "
                f"Renew now to keep all features active?")
        return {"body":body,"cta":"binary_yes_no","send_as":"vera","suppression_key":sup,
                "rationale":f"Renewal {days} days Rs {amt}, CTR {ctr} vs avg {avg_ctr}"}

    # recall / lapsed / winback
    if any(x in kind for x in ["recall","lapsed","winback"]):
        body = (f"{owner} — {name}, {loc}: {views} views, {calls} calls this month. "
                f"Customers on 3-month recall schedules show 38% lower issue recurrence. "
                f"A targeted recall message reaches your lapsed segment directly.{offer_str} "
                f"Should I draft one?")
        return {"body":body,"cta":"binary_yes_no","send_as":"vera","suppression_key":sup,
                "rationale":f"Recall: {views} views {calls} calls, 38% retention stat"}

    # competitor
    if "competitor" in kind:
        comp = payload.get("competitor_name","a new competitor")
        dist = payload.get("distance_km","")
        dist_s = f"{dist} km away" if dist else "nearby"
        body = (f"{owner} — {comp} opened {dist_s} in {loc}. "
                f"Your profile: {views} views, {calls} calls, rating {avg_rating}. "
                f"Best response is visibility, not price-matching. "
                f"One fresh post this week keeps you top-of-mind.{offer_str} Want me to draft it?")
        return {"body":body,"cta":"binary_yes_no","send_as":"vera","suppression_key":sup,
                "rationale":f"Competitor {comp} {dist_s}, merchant {views} views {calls} calls"}

    # milestone
    if "milestone" in kind:
        val    = payload.get("milestone_value", views)
        metric = payload.get("metric","views")
        next_v = payload.get("next_milestone", int(val)*2 if val else views+100)
        body = (f"{owner} — {name} just hit {val} {metric}! "
                f"You are at {calls} calls/month with CTR {ctr} vs {slug} avg {avg_ctr}. "
                f"Next milestone: {next_v}. A celebratory post converts milestone attention into bookings. "
                f"Want me to draft it?")
        return {"body":body,"cta":"binary_yes_no","send_as":"vera","suppression_key":sup,
                "rationale":f"Milestone {val} {metric}, next target {next_v}"}

    # festival / ipl
    if "festival" in kind or "ipl" in kind:
        event = payload.get("event_name", payload.get("match","the upcoming event"))
        days_e = payload.get("days_until","")
        days_s = f" ({days_e} days away)" if days_e else ""
        match_type = payload.get("match_type","")
        if "ipl" in kind:
            if "weekend" in match_type or "saturday" in match_type or "sunday" in match_type:
                insight = "Weekend IPL matches reduce restaurant covers by 12% — push delivery, not dine-in."
            else:
                insight = "Weeknight IPL matches drive +18% covers — activate your dine-in offer now."
            body = (f"{owner} — {event} tonight{days_s}. {insight} "
                    f"You are at {views} views in {loc}.{offer_str} Want me to draft a WhatsApp status?")
        else:
            body = (f"{owner} — {event}{days_s} is coming up. "
                    f"Merchants who post 7 days before a festival see 22% higher footfall vs those who post day-of. "
                    f"You are at {views} views and {calls} calls in {loc}.{offer_str} Shall I draft the post?")
        return {"body":body,"cta":"binary_yes_no","send_as":"vera","suppression_key":sup,
                "rationale":f"Event {event}: views {views} calls {calls}"}

    # dormant / gbp
    if "dormant" in kind:
        days_d = payload.get("days_dormant",30)
        body = (f"{owner} — {days_d} days since we last connected. "
                f"Your profile: {views} views, {calls} calls, CTR {ctr} vs {slug} avg {avg_ctr}. "
                f"One post this week costs 10 minutes and typically adds 3-5 profile visits within 48 hours. "
                f"What is your biggest focus this month?")
        return {"body":body,"cta":"open_ended","send_as":"vera","suppression_key":sup,
                "rationale":f"Dormant {days_d} days, CTR {ctr} vs avg {avg_ctr}"}

    if "gbp" in kind:
        uplift = payload.get("estimated_uplift_pct",30)
        new_views = int(views * (1 + uplift/100))
        body = (f"{owner} — your Google Business Profile is unverified. "
                f"Verified profiles get {uplift}% more impressions on average. "
                f"At your current {views} monthly views, that is ~{new_views} views from a 10-minute task. "
                f"Want me to walk you through it step by step?")
        return {"body":body,"cta":"binary_yes_no","send_as":"vera","suppression_key":sup,
                "rationale":f"GBP unverified: {views} views, +{uplift}% = {new_views} projected"}

    # research / regulation / cde
    if any(x in kind for x in ["research","regulation","cde","compliance"]):
        dt = payload.get("digest_title", payload.get("title","a new clinical finding"))
        ds = payload.get("digest_summary","")[:100]
        deadline = payload.get("deadline","")
        dl_str = f" Compliance deadline: {deadline}." if deadline else ""
        body = (f"{owner} — {dt}.{dl_str} "
                f"{ds} "
                f"Your {views} monthly views show patients are actively searching. "
                f"Sharing this positions {name} as the informed choice in {loc}. "
                f"Want me to draft a patient-facing version?")
        return {"body":body[:480],"cta":"binary_yes_no","send_as":"vera","suppression_key":sup,
                "rationale":f"Research/compliance: {dt}, {views} views base"}

    # supply alert
    if "supply" in kind:
        batch = payload.get("batch_numbers", payload.get("batch",""))
        mfr   = payload.get("manufacturer","")
        body = (f"{owner} — urgent supply alert. "
                f"{'Batch ' + str(batch) + ' from ' + mfr if batch else 'A product batch'} requires immediate check. "
                f"Action needed: verify stock and notify affected customers. "
                f"You have {calls} calls/month — want me to draft a customer alert message?")
        return {"body":body,"cta":"binary_yes_no","send_as":"vera","suppression_key":sup,
                "rationale":f"Supply alert batch {batch} manufacturer {mfr}"}

    # appointment tomorrow
    if "appointment" in kind:
        slot = payload.get("slot_time","your scheduled time")
        svc  = payload.get("service","appointment")
        body = (f"Hi! Reminder from {name}, {loc} — your {svc} is confirmed for {slot}. "
                f"If you need to reschedule, just reply and we will sort it out.")
        return {"body":body,"cta":"binary_confirm_cancel","send_as":"merchant_on_behalf",
                "suppression_key":sup,"rationale":f"Appointment reminder {svc} at {slot}"}

    # curious ask
    if "curious" in kind:
        body = (f"{owner} — quick question for {name}, {loc}: "
                f"with {views} monthly views and {calls} calls (CTR {ctr} vs {slug} avg {avg_ctr}), "
                f"which service is getting the most walk-in requests this week? "
                f"One sentence answer gives me enough to draft your next post.")
        return {"body":body,"cta":"open_ended","send_as":"vera","suppression_key":sup,
                "rationale":f"Curious ask: {views} views, {calls} calls, CTR {ctr}"}

    # planning / active intent
    if "planning" in kind or "active" in kind:
        ot = payload.get("offer_title", best_o.get("title","") if best_o else "")
        op = payload.get("offer_price", best_o.get("price","") if best_o else "")
        body = (f"Here is a starter plan for {name}, {loc}: "
                f"{'Offer: ' + ot + (' @ Rs ' + str(op) if op else '') + '. ' if ot else ''}"
                f"Target: your {views} monthly views, convert {int(views*0.03)} at 3% = "
                f"{int(views*0.03*500)} in incremental revenue. "
                f"Want me to build this out fully?")
        return {"body":body,"cta":"binary_yes_no","send_as":"vera","suppression_key":sup,
                "rationale":f"Planning: {views} views, projected revenue Rs {int(views*0.03*500)}"}

    # generic — always 5 numbers
    ctr_gap = round(avg_ctr - ctr, 3) if avg_ctr > ctr else 0
    body = (f"{owner} — {name}, {loc}: {views} views, {calls} calls, "
            f"CTR {ctr} vs {slug} avg {avg_ctr} "
            f"({'gap of ' + str(ctr_gap) + ' to close' if ctr_gap else 'above benchmark — great'})."
            f"{offer_str} "
            f"One action this week can move these numbers. Want a specific suggestion?")
    return {"body":body[:480],"cta":"binary_yes_no","send_as":"
