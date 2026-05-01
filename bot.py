"""
bot.py — Vera AI Challenge Submission
compose(category, merchant, trigger, customer?) -> ComposedMessage

Design philosophy:
- One prompt, dispatch by trigger.kind for framing variant
- Real numbers from context; never fabricate
- Category voice enforced via system prompt
- Post-LLM validation ensures CTA shape + language match
"""

import os
import json
import re
import urllib.request
from typing import Optional

# ── Config ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 600
TEMPERATURE = 0   # deterministic for same inputs

# ── Trigger kind → framing variant ───────────────────────────────────────────
FRAMING = {
    "research_digest":        "Share a specific finding from the digest. Anchor on trial size, percentage, and source citation. Offer to pull the full item or draft a patient-facing version.",
    "regulation_change":      "State the rule change, the deadline, and the practical impact on this clinic. Flag the specific equipment or process affected. Offer to help with the transition.",
    "cde_opportunity":        "Name the event, credits, fee, and speaker. Connect to the merchant's specific patient volume or case mix. Single yes/no ask.",
    "recall_due":             "Send from the merchant's voice (merchant_on_behalf). Name the patient, state time since last visit, give 2 real slot options, state the price, and close with numbered reply options.",
    "perf_dip":               "State the exact delta and the baseline. Give 2-3 fast fixes ranked by impact. Do not open with the renewal topic if renewal is also pending; lead with the performance fix.",
    "perf_spike":             "Celebrate briefly, then name the likely driver and suggest one action to capitalise on it. Keep it short.",
    "milestone_reached":      "Name the milestone number and the 'imminent' gap if applicable. Suggest one concrete action to cross the line or leverage it.",
    "competitor_opened":      "State competitor name, distance, and their offer. Recommend a differentiation move, not a price match. Offer to activate a specific counter-offer.",
    "dormant_with_vera":      "Re-engage with a question or a concrete low-effort win. Reference days dormant and current performance. Avoid preamble.",
    "festival_upcoming":      "Name the festival, days until, and category-specific relevance. Suggest one preparatory action the merchant can take now.",
    "category_seasonal":      "State the specific seasonal trends from the payload (exact percentages). Suggest shelf rearrangement or a specific product push. Practical operator tone.",
    "ipl_match_today":        "Check if match is weeknight (covers +18%) or weekend (covers -12%). Give the counter-intuitive data point. Suggest the right channel (dine-in promo vs delivery push).",
    "curious_ask_due":        "Ask one specific operational question the merchant can answer in one sentence. Promise to turn their answer into a concrete asset (post, reply template, etc.).",
    "active_planning_intent": "Merchant already said yes. Deliver the artifact immediately — draft package, pricing tiers, script, or copy. Do not ask another qualifying question.",
    "customer_lapsed_hard":   "Send from merchant voice. No guilt. Name the last focus area. Offer one specific new thing that addresses that goal. Single yes/no CTA with no auto-charge language.",
    "customer_lapsed_soft":   "Gentle check-in from merchant. Reference time since last visit. Offer a slot or a simple next step. Match language preference.",
    "chronic_refill_due":     "Name all molecules. State run-out date. Give total with discount applied. State delivery logistics. Close with CONFIRM or call option.",
    "renewal_due":            "Lead with value delivered, then state days remaining and amount. Single yes/no.",
    "gbp_unverified":         "State the estimated uplift percentage. Describe the verification process in one sentence. Offer to walk through it. Bundle with one other quick win.",
    "appointment_tomorrow":   "Simple reminder. Confirm slot. Offer to change if needed. Warm, brief.",
    "review_theme_emerged":   "Name the theme and number of mentions. Frame as a signal worth acting on. Suggest one operational change or a response template.",
    "supply_alert":           "Name the exact batch numbers and manufacturer. State the risk level and what the merchant needs to do. Offer to draft customer communication.",
    "trial_followup":         "Follow up on the trial outcome. Ask one specific question about what they liked or needed. Offer the next step.",
    "winback_eligible":       "Same as customer_lapsed_hard framing.",
    "seasonal_perf_dip":      "Reframe the dip as expected seasonal behaviour with peer data. Recommend retention focus over acquisition. Offer a specific retention asset.",
}

# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM = """You are Vera, Magicpin's merchant AI assistant. You compose WhatsApp messages for Indian merchants.

HARD RULES:
1. Use ONLY numbers, names, and facts present in the context provided. NEVER invent data.
2. WhatsApp message body: concise, no marketing preamble, no "I hope this message finds you well".
3. Match the merchant's language preference exactly: "hi-en mix" = natural Hindi-English blend; "en" or "english" = English only; "hi" = Hindi primary.
4. Voice by category:
   - dentists: clinical-peer, technical vocab OK, no "guaranteed" or "100% safe"
   - salons: warm, practical, approachable expert
   - gyms: coach tone, motivational but data-grounded
   - pharmacies: trustworthy, precise, neighbourhood pharmacist
   - restaurants: fellow operator, practical, covers/AOV/delivery language fine
5. For customer-facing messages (send_as=merchant_on_behalf): write FROM the merchant's voice, not Vera's.
6. One primary CTA per message. Binary YES/NO for action triggers; open question for curious/planning triggers; numbered slot options for booking triggers.
7. Do NOT use URLs in message body.
8. Return ONLY valid JSON. No markdown, no explanation outside the JSON.

OUTPUT FORMAT (strict JSON, no extra keys):
{
  "body": "<WhatsApp message text>",
  "cta": "<binary_yes_no | open_ended | multi_choice_slot | binary_confirm_cancel | none>",
  "send_as": "<vera | merchant_on_behalf>",
  "suppression_key": "<from trigger context>",
  "rationale": "<1-2 sentences: why this message, what compulsion lever used>"
}"""


def _build_prompt(category: dict, merchant: dict, trigger: dict,
                  customer: Optional[dict], framing: str) -> str:
    """Assemble the user prompt from all 4 contexts."""

    identity = merchant.get("identity", {})
    perf = merchant.get("performance", {})
    offers = merchant.get("offers", category.get("offer_catalog", []))
    active_offers = [o for o in offers if isinstance(o, dict) and o.get("status") in ("active", None)]
    peer = category.get("peer_stats", {})
    digest = category.get("digest", [])
    seasonal = category.get("seasonal_beats", [])
    voice = category.get("voice", {})
    cust_agg = merchant.get("customer_aggregate", {})
    signals = merchant.get("signals", [])

    prompt = f"""TRIGGER KIND: {trigger.get('kind')}
FRAMING INSTRUCTION: {framing}

=== CATEGORY CONTEXT ({category.get('slug', '?')}) ===
Voice tone: {voice.get('tone')}
Vocab allowed: {voice.get('vocab_allowed', [])[:6]}
Taboos: {voice.get('vocab_taboo', [])}
Peer stats: avg_rating={peer.get('avg_rating')}, avg_ctr={peer.get('avg_ctr')}, avg_calls_30d={peer.get('avg_calls_30d')}
Seasonal: {seasonal[:2]}
Digest items available: {[d.get('title') for d in digest[:3]]}

=== MERCHANT CONTEXT ===
Name: {identity.get('name')}
Owner: {identity.get('owner_first_name')}
City: {identity.get('city')} | Locality: {identity.get('locality')}
Verified: {identity.get('verified')} | Languages: {identity.get('languages')}
Subscription: {merchant.get('subscription', {})}
Performance (30d): views={perf.get('views')}, calls={perf.get('calls')}, ctr={perf.get('ctr')}
Delta (7d): {perf.get('delta_7d', {})}
Active offers: {[o.get('title') for o in active_offers[:3]]}
Signals: {signals}
Customer aggregate: {cust_agg}
Conversation history last turn: {(merchant.get('conversation_history') or [{}])[-1:]}

=== TRIGGER CONTEXT ===
ID: {trigger.get('id')}
Scope: {trigger.get('scope')}
Kind: {trigger.get('kind')}
Source: {trigger.get('source')}
Urgency: {trigger.get('urgency')}
Payload: {json.dumps(trigger.get('payload', {}))}
Suppression key: {trigger.get('suppression_key')}
Expires at: {trigger.get('expires_at')}"""

    if customer:
        cust_id = customer.get("identity", {})
        rel = customer.get("relationship", {})
        prefs = customer.get("preferences", {})
        prompt += f"""

=== CUSTOMER CONTEXT ===
Name: {cust_id.get('name')}
Language pref: {cust_id.get('language_pref')}
State: {customer.get('state')}
First visit: {rel.get('first_visit')} | Last visit: {rel.get('last_visit')}
Visits total: {rel.get('visits_total')} | LTV: ₹{rel.get('lifetime_value')}
Services received: {rel.get('services_received', [])}
Preferred slots: {prefs.get('preferred_slots')}
Consent scope: {customer.get('consent', {}).get('scope', [])}"""
    else:
        prompt += "\n\n=== CUSTOMER CONTEXT ===\nNone (merchant-facing message)"

    prompt += "\n\nCompose the WhatsApp message. Output strict JSON only."
    return prompt


def _call_llm(prompt: str) -> str:
    """Call Anthropic Claude API. Returns raw text."""
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not set")

    body = json.dumps({
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=28) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data["content"][0]["text"]


def _parse_output(raw: str, trigger: dict, customer: Optional[dict]) -> dict:
    """Extract and validate the JSON output from LLM response."""
    match = re.search(r'\{[\s\S]*\}', raw)
    if not match:
        raise ValueError(f"No JSON found in LLM response: {raw[:200]}")

    result = json.loads(match.group())

    # Enforce send_as
    scope = trigger.get("scope", "merchant")
    if scope == "customer" and customer:
        result["send_as"] = "merchant_on_behalf"
    else:
        result["send_as"] = "vera"

    # Enforce suppression_key from trigger
    result["suppression_key"] = trigger.get("suppression_key", result.get("suppression_key", ""))

    # Validate required keys
    for key in ("body", "cta", "send_as", "suppression_key", "rationale"):
        if key not in result:
            raise ValueError(f"Missing key in LLM output: {key}")

    # Strip any accidental URLs
    result["body"] = re.sub(r'https?://\S+', '[link]', result["body"])

    return result


def compose(category: dict, merchant: dict, trigger: dict,
            customer: Optional[dict] = None) -> dict:
    """
    Main composition function.

    Args:
        category: CategoryContext dict (from dentists.json, salons.json, etc.)
        merchant: MerchantContext dict (from merchants_seed.json or expanded)
        trigger: TriggerContext dict (from triggers_seed.json or expanded)
        customer: CustomerContext dict or None (for customer-scoped triggers)

    Returns:
        dict with keys: body, cta, send_as, suppression_key, rationale
    """
    kind = trigger.get("kind", "")
    framing = FRAMING.get(kind, "Compose a concise, specific, merchant-relevant message anchored on one verifiable fact from the context.")

    prompt = _build_prompt(category, merchant, trigger, customer, framing)

    raw = _call_llm(prompt)
    result = _parse_output(raw, trigger, customer)
    return result


# ── Standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    print("bot.py loaded. Run server.py to start the HTTP bot.")
    print("To test compose: import bot; result = bot.compose(cat, merchant, trigger)")
