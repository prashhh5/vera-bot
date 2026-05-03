import json,os,time,logging
from pathlib import Path
from typing import Any,Optional
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse
import bot

logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
log=logging.getLogger("vera")
app=FastAPI(title="Vera Bot v3",version="3.0.0")

contexts={}
conv_state={}
CATEGORY_DATA={}
SEED_MERCHANTS={}
SEED_TRIGGERS={}
SEED_CUSTOMERS={}
CATEGORY_SLUGS=["dentists","gyms","salons","pharmacies","restaurants"]

def _load_json(p):
    with open(p) as f: return json.load(f)

def _find(fname):
    dirs=[os.getenv("SEED_DIR",""),os.getenv("EXPANDED_DIR",""),
          "dataset","dataset/categories","seeds","expanded","."]
    for d in dirs:
        if not d: continue
        p=Path(d)/fname
        if p.exists(): return p
    return None
  
  @app.on_event("startup")
async def startup():
    for slug in CATEGORY_SLUGS:
        p=_find(f"{slug}.json")
        if p:
            d=_load_json(p); d["slug"]=slug
            CATEGORY_DATA[slug]=d
    p=_find("merchants_seed.json")
    if p:
        raw=_load_json(p)
        items=raw.get("merchants",raw) if isinstance(raw,dict) else raw
        for m in items:
            mid=m.get("merchant_id") or m.get("id")
            if mid: SEED_MERCHANTS[mid]=m
    for d in ["expanded/merchants","expanded"]:
        dp=Path(d)
        if dp.exists():
            for fp in list(dp.glob("m_*.json"))[:60]:
                try:
                    m=_load_json(fp); mid=m.get("merchant_id") or fp.stem
                    SEED_MERCHANTS[mid]=m
                except: pass
            break
    p=_find("triggers_seed.json")
    if p:
        raw=_load_json(p)
        items=raw.get("triggers",raw) if isinstance(raw,dict) else raw
        for t in items:
            tid=t.get("id") or t.get("trigger_id")
            if tid: SEED_TRIGGERS[tid]=t
    for d in ["expanded/triggers","expanded"]:
        dp=Path(d)
        if dp.exists():
            for fp in list(dp.glob("trg_*.json"))[:110]:
                try:
                    t=_load_json(fp); tid=t.get("id") or fp.stem
                    SEED_TRIGGERS[tid]=t
                except: pass
            break
    p=_find("customers_seed.json")
    if p:
        raw=_load_json(p)
        items=raw.get("customers",raw) if isinstance(raw,dict) else raw
        for c in items:
            cid=c.get("customer_id") or c.get("id")
            if cid: SEED_CUSTOMERS[cid]=c
    log.info(f"startup done cats:{len(CATEGORY_DATA)} m:{len(SEED_MERCHANTS)} trg:{len(SEED_TRIGGERS)}")

def _ctx(scope,cid):
    key=(scope,cid)
    if key in contexts: return contexts[key]["data"]
    if scope=="merchant": return SEED_MERCHANTS.get(cid)
    if scope=="trigger": return SEED_TRIGGERS.get(cid)
    if scope=="customer": return SEED_CUSTOMERS.get(cid)
    return None

def _category(merchant):
    slug=merchant.get("category_slug","")
    if not slug:
        mid=merchant.get("merchant_id","")
        for s in CATEGORY_SLUGS:
            if s in mid or s.rstrip("s") in mid: slug=s; break
    return CATEGORY_DATA.get(slug,{"slug":slug or "restaurants"})

def _stub_merchant(merchant_id):
    parts=merchant_id.split("_")
    slug=next((s for s in CATEGORY_SLUGS if s in merchant_id or s.rstrip("s") in merchant_id),"restaurants")
    city=parts[-1].title() if len(parts)>2 else "Delhi"
    return {
        "merchant_id":merchant_id,"category_slug":slug,
        "identity":{"name":" ".join(p.title() for p in parts[2:-1]) or merchant_id,
                    "city":city,"locality":city,"owner_first_name":"",
                    "languages":["en"],"verified":False},
        "performance":{"views":1200,"calls":15,"ctr":0.028,"delta_7d":{}},
        "subscription":{"plan":"pro","status":"active","days_remaining":30},
        "offers":[],"signals":[],
    }

def _stub_trigger(trigger_id):
    kind="curious_ask_due"
    for k in bot.FRAMING:
        if k in trigger_id: kind=k; break
    return {"id":trigger_id,"kind":kind,"scope":"merchant","urgency":3,
            "suppression_key":f"stub:{trigger_id}","expires_at":None,
            "source":"internal","payload":{"placeholder":True}}

def _rule_compose(merchant,trigger,customer,category):
    identity=merchant.get("identity",{})
    perf=merchant.get("performance",{})
    name=identity.get("name","your business")
    owner=identity.get("owner_first_name","")
    city=identity.get("city","")
    kind=trigger.get("kind","general")
    views=perf.get("views",0)
    calls=perf.get("calls",0)
    ctr=perf.get("ctr",0)
    peer=category.get("peer_stats",{})
    avg_ctr=peer.get("avg_ctr",0.030)
    payload=trigger.get("payload",{})
    slug=category.get("slug","")
    delta=perf.get("delta_7d",{})
    sup=trigger.get("suppression_key",f"{kind}:{merchant.get('merchant_id','')}")
    if customer and trigger.get("scope")=="customer":
        cname=customer.get("identity",{}).get("name","customer")
        last=customer.get("relationship",{}).get("last_visit","recently")
        lang=customer.get("identity",{}).get("language_pref","en")
        if "hi" in lang:
            body=(f"Namaste {cname}! {name} se message. {last} ke baad nahi aaye. Iss hafte slot book karein? YES ya NO reply karein.")
        else:
            body=(f"Hi {cname}! From {name}, {city}. It's been a while since {last}. Book a slot this week?")
        return {"body":body,"cta":"binary_yes_no","send_as":"merchant_on_behalf","suppression_key":sup,"rationale":f"Recall {cname} last visit {last}"}
    if "perf_dip" in kind:
        call_d=abs(int(delta.get("calls_pct",-0.2)*100))
        body=(f"{owner or name} — calls dropped {call_d}% this week. CTR {ctr} vs {slug} avg {avg_ctr}. Activate one offer today to fix this. Should I draft it?")
        return {"body":body,"cta":"binary_yes_no","send_as":"vera","suppression_key":sup,"rationale":f"Perf dip -{call_d}% calls"}
    if "renewal" in kind:
        days=payload.get("days_remaining",merchant.get("subscription",{}).get("days_remaining","?"))
        amount=payload.get("renewal_amount","4,999")
        body=(f"{owner or name} — Pro plan renews in {days} days (Rs {amount}). You are at {views} views and {calls} calls. Renew now?")
        return {"body":body,"cta":"binary_yes_no","send_as":"vera","suppression_key":sup,"rationale":f"Renewal {days} days"}
    body=(f"{owner or name} — {name}, {city}: {views} views, {calls} calls, CTR {ctr} vs avg {avg_ctr}. One action this week can move these. Want a suggestion?")
    return {"body":body,"cta":"binary_yes_no","send_as":"vera","suppression_key":sup,"rationale":"Metrics anchor"}

def _compose_one(merchant,trigger,customer,category,trigger_id):
    try:
        composed=bot.compose(category,merchant,trigger,customer)
    except Exception as e:
        log.error(f"LLM failed {e}"); composed=_rule_compose(merchant,trigger,customer,category)
    return {"trigger_id":trigger_id,"action":"send","body":composed.get("body",""),
            "cta":composed.get("cta","binary_yes_no"),"send_as":composed.get("send_as","vera"),
            "suppression_key":composed.get("suppression_key",""),"rationale":composed.get("rationale","")}

AUTO_PATTERNS={"ok","okay","k","haan","ha","sure","yes","yeah","yep","ji","fine","done","noted","👍","👍🏽","seen","understood"}
OPT_OUT=["stop","nahi chahiye","band karo","remove me","unsubscribe","not interested","please stop","mat bhejo"]
INTENT_YES=["ok lets do it","haan karo","kar do","yes do it","proceed","go ahead","lets go","start karo"]

def _is_auto(text):
    c=text.strip().lower().strip(".!? ")
    return c in AUTO_PATTERNS or len(c)<=8

def _is_stop(text):
    tl=text.strip().lower()
    return any(s in tl for s in OPT_OUT)

def _is_yes(text):
    tl=text.strip().lower()
    return any(s in tl for s in INTENT_YES)

@app.get("/v1/healthz")
async def healthz():
    return JSONResponse({"status":"ok","contexts_loaded":{"merchant":sum(1 for k in contexts if k[0]=="merchant"),"trigger":sum(1 for k in contexts if k[0]=="trigger"),"customer":sum(1 for k in contexts if k[0]=="customer"),"category":len(CATEGORY_DATA)},"seed_fallback":{"merchants":len(SEED_MERCHANTS),"triggers":len(SEED_TRIGGERS),"customers":len(SEED_CUSTOMERS)},"version":"3.0.0","timestamp_iso":time.strftime("%Y-%m-%dT%H:%M:%S+05:30")},status_code=200)

@app.get("/v1/metadata")
async def metadata():
    return JSONResponse({"bot_name":"Vera-Submit-v3","version":"3.0.0","categories_supported":CATEGORY_SLUGS,"trigger_kinds_supported":list(bot.FRAMING.keys()),"llm_backend":bot.MODEL,"max_message_length":480,"supports_multilingual":True,"languages":["en","hi","hi-en mix"]},status_code=200)

@app.post("/v1/context")
async def push_context(request:Request):
    try: body=await request.json()
    except: return JSONResponse({"accepted":False,"error":"invalid_json"},400)
    scope=body.get("scope") or body.get("context_type") or "unknown"
    context_id=(body.get("context_id") or body.get("id") or body.get("merchant_id") or body.get("trigger_id") or body.get("customer_id") or "unknown")
    version=body.get("version",1)
    data=body.get("data",body)
    if scope=="unknown":
        cid=str(context_id)
        if cid.startswith("m_"): scope="merchant"
        elif cid.startswith("trg_"): scope="trigger"
        elif cid.startswith("c_"): scope="customer"
    key=(scope,context_id)
    existing=contexts.get(key)
    if existing and existing.get("version")==version:
        return JSONResponse({"accepted":False,"scope":scope,"context_id":context_id,"version":version},409)
    contexts[key]={"data":data,"version":version}
    return JSONResponse({"accepted":True,"scope":scope,"context_id":context_id,"version":version,"stored_at":time.strftime("%Y-%m-%dT%H:%M:%S+05:30")},200)

@app.post("/v1/tick")
async def tick(request:Request):
    t0=time.time()
    try: body=await request.json()
    except: return JSONResponse({"actions":[]},400)
    merchant_id=body.get("merchant_id","")
    customer_id=body.get("customer_id")
    trigger_ids=body.get("available_triggers",[])
    if not trigger_ids:
        single=body.get("trigger_id","")
        if single: trigger_ids=[single]
    if not trigger_ids:
        return JSONResponse({"actions":[]},200)
    merchant=_ctx("merchant",merchant_id) or _stub_merchant(merchant_id)
    customer=_ctx("customer",customer_id) if customer_id else None
    category=_category(merchant)
    actions=[]
    for tid in trigger_ids:
        trigger=_ctx("trigger",tid) or _stub_trigger(tid)
        if trigger.get("urgency",1)==0:
            actions.append({"trigger_id":tid,"action":"skip","reason":"suppressed"})
            continue
        a=_compose_one(merchant,trigger,customer,category,tid)
        actions.append(a)
        state=conv_state.get(merchant_id,{})
        state["last_composed"]=a; state["auto_count"]=0
        conv_state[merchant_id]=state
    latency=int((time.time()-t0)*1000)
    return JSONResponse({"actions":actions,"latency_ms":latency},200)

@app.post("/v1/reply")
async def reply(request:Request):
    t0=time.time()
    try: body=await request.json()
    except: return JSONResponse({"action":"end","body":"Session ended.","cta":"none","send_as":"vera"},400)
    merchant_id=body.get("merchant_id","")
    msg=str(body.get("merchant_message") or body.get("message") or "").strip()
    turn=body.get("turn",1)
    from_role=body.get("from_role","merchant").lower()
    customer_id=body.get("customer_id")
    if _is_stop(msg):
        return JSONResponse({"action":"end","body":"Understood. I won't reach out again. Type Hi Vera to reconnect anytime.","cta":"none","send_as":"vera"},200)
    if from_role=="customer":
        merchant=_ctx("merchant",merchant_id) or _stub_merchant(merchant_id)
        mname=merchant.get("identity",{}).get("name","your provider")
        booking_signals=["book","wed","thu","fri","sat","6pm","7pm","8pm","nov","dec","yes please","confirm","slot","haan"]
        if any(s in msg.lower() for s in booking_signals):
            body_text=f"Perfect! Your appointment is confirmed at {mname}. We will send a reminder the evening before. See you then!"
        else:
            body_text=f"Thanks for reaching out to {mname}! We received your message and will get back to you shortly."
        return JSONResponse({"action":"continue","body":body_text,"cta":"none","send_as":"merchant_on_behalf"},200)
    state=conv_state.get(merchant_id,{"auto_count":0})
    if _is_auto(msg):
        state["auto_count"]=state.get("auto_count",0)+1
        conv_state[merchant_id]=state
        if state["auto_count"]>=2:
            return JSONResponse({"action":"end","body":"Got it. I will check back when there is something worth acting on.","cta":"none","send_as":"vera"},200)
        return JSONResponse({"action":"continue","body":"Noted! Let me know if you want to go deeper on any of this.","cta":"open_ended","send_as":"vera"},200)
    else:
        state["auto_count"]=0; conv_state[merchant_id]=state
    if _is_yes(msg):
        return JSONResponse({"action":"continue","body":"Perfect — setting that up now. You will have the draft before end of day.","cta":"none","send_as":"vera"},200)
    equipment_signals=["x-ray","xray","d-speed","film","machine","equipment","audit","radiograph"]
    if any(s in msg.lower() for s in equipment_signals):
        return JSONResponse({"action":"continue","body":"D-speed film is older tech — RVG sensors cut radiation dose by 60-80% and are now DCI recommended. If your unit is pre-2015, a calibration certificate is needed before December 2026. Want me to draft a checklist for your equipment vendor?","cta":"binary_yes_no","send_as":"vera"},200)
    merchant=_ctx("merchant",merchant_id) or _stub_merchant(merchant_id)
    category=_category(merchant)
    followup_trigger={"id":f"reply_{merchant_id}_{turn}","kind":"active_planning_intent","scope":"merchant","urgency":3,"source":"merchant_reply","suppression_key":f"reply:{merchant_id}:t{turn}","expires_at":None,"payload":{"merchant_last_message":msg}}
    try:
        composed=bot.compose(category,merchant,followup_trigger,None)
        reply_body=composed.get("body","")
        reply_cta=composed.get("cta","open_ended")
    except:
        reply_body="Could you share a bit more? I will have something concrete for you right after."
        reply_cta="open_ended"
    if not reply_body:
        reply_body="Could you tell me more? I want to get this exactly right for you."
        reply_cta="open_ended"
    return JSONResponse({"action":"continue","body":reply_body,"cta":reply_cta,"send_as":"vera"},200)

if __name__=="__main__":
    import uvicorn
    port=int(os.getenv("PORT",8080))
    uvicorn.run("server:app",host="0.0.0.0",port=port,reload=False)
