from fastapi import FastAPI, Request

app = FastAPI()

# ---------- INTENT DETECTION ----------
def detect_intent(msg: str):
    msg = msg.lower().strip()

    if any(x in msg for x in ["yes", "do it", "go ahead"]):
        return "confirm"
    if any(x in msg for x in ["no", "not now", "later"]):
        return "reject"
    if msg in ["ok", "okay", "hmm"]:
        return "neutral"
    if msg in ["stop", "unsubscribe", "end"]:
        return "stop"

    return "unknown"


# ---------- MESSAGE GENERATION ----------
def generate_message(trigger: str, merchant_id: str):
    base = {
        "perf_dip": "Your performance dropped this week.",
        "recall": "Customers are not returning as expected.",
        "festival": "Upcoming festival is a big opportunity.",
        "milestone": "You're close to an important milestone.",
        "competitor": "Competitors are gaining traction."
    }

    line = base.get(trigger, "We found something important.")

    # simple context awareness
    if merchant_id and "restaurant" in merchant_id:
        line += " Try boosting your menu visibility or offers."
    else:
        line += " Try improving your listing or promotions."

    return line + " Want help?"


# ---------- MAIN ENDPOINT ----------
@app.post("/v1/tick")
async def tick(request: Request):
    data = await request.json()
    actions = []

    merchant_id = data.get("merchant_id", "")

    # ----- MESSAGE FLOW -----
    if "message" in data:
        intent = detect_intent(data["message"])

        if intent == "stop":
            return {
                "actions": [{
                    "trigger_id": "stop",
                    "action": "end",
                    "body": "",
                    "cta": "",
                    "send_as": "system",
                    "suppression_key": "s:stop",
                    "rationale": "user_opt_out"
                }]
            }

        if intent == "confirm":
            body = "Great, setting this up for you now."
        elif intent == "reject":
            body = "No problem, we’ll pause this for now."
        elif intent == "neutral":
            body = "Got it. Let me know when you're ready."
        else:
            body = "Let me know how you'd like to proceed."

        actions.append({
            "trigger_id": "reply",
            "action": "send",
            "body": body,
            "cta": "none",
            "send_as": "vera",
            "suppression_key": "s:reply",
            "rationale": "intent_based"
        })

    # ----- TRIGGER FLOW -----
    elif "available_triggers" in data:
        triggers = data.get("available_triggers", [])

        for t in triggers:
            body = generate_message(t, merchant_id)

            actions.append({
                "trigger_id": t,
                "action": "send",
                "body": body,
                "cta": "binary_yes_no",
                "send_as": "vera",
                "suppression_key": f"s:{t}",
                "rationale": "dynamic"
            })

    return {"actions": actions}


# ---------- HEALTH CHECK ----------
@app.get("/v1/healthz")
def health():
    return {"status": "ok", "version": "final-optimized"}
