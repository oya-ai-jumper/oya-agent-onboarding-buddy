---
name: oya-messenger-sdr
display_name: "Oya Messenger SDR (Jumper Media)"
description: "Procedural Messenger SDR for Jumper Media local-SEO onboarding. Triggered by 'MAPS', looks up the lead's Google Business Profile via Places API, runs a hours/website/reviews/rating qualification check, detects current + returning customers via Xano, collects name/email/phone, and finishes onboarding by driving the local.jumpermedia.co form in the Oya browser."
category: sales
icon: message-square
skill_type: sandbox
catalog_type: addon
requirements: "httpx>=0.25"
# Procedural skill — the runtime bypasses the LLM tool-loop and dispatches
# every inbound message to this skill's `run()`. Skill returns
# `{reply, state, done}`; the harness persists `state` per thread via
# `agent_memories`. See `app/skills/procedural/`.
kind: procedural
relay_field: reply
done_field: done
state_field: state
trigger:
  keywords: ["MAPS"]
# Restricted to Jumper Media accounts. The skill list / detail / attach
# endpoints all hide it from users outside the allowlist (404, not 403).
restricted_to_emails:
  - jumpermedia.co
resource_requirements:
  - env_var: GOOGLE_PLACES_API_KEY
    name: "Google Places API Key"
    description: "API key with Places API (New) enabled. Used for business lookup + qualification (hours, website, review count, rating)."
  - env_var: XANO_API_GROUP_BASE_URL
    name: "Xano API Group Base URL"
    description: "Jumper Media's Xano API base URL — exposes the customer-lookup endpoints used to detect current + returning customers."
  - env_var: XANO_AUTH_TOKEN
    name: "Xano Auth Token"
    description: "Bearer token for the Xano API group (optional if endpoints are public)."
    optional: true
  - env_var: BROWSER_API_KEY
    name: "Oya Browser API Key"
    description: "From the Oya Browser gateway. Drives the jumpermedia.co onboarding form."
  - env_var: BROWSER_API_BASE
    name: "Oya Browser API Base URL"
    description: "Defaults to https://browser.oya.ai."
    optional: true
config_schema:
  properties:
    onboarding_url:
      type: string
      label: "Onboarding URL"
      description: "Where Oya drives the form on qualification."
      default: "https://local.jumpermedia.co/onboarding/utm=oya"
      group: defaults
    calendly_url:
      type: string
      label: "Calendly URL"
      description: "Booking link sent in the final confirmation message AND used as the reactivation link for returning customers."
      default: "https://calendly.com/jmpsales/google-ranking-increase-jumper-local"
      group: defaults
    login_url:
      type: string
      label: "Customer Login URL"
      description: "Sent to current (active-subscription) customers."
      default: "https://local.jumpermedia.co/"
      group: defaults
    support_email:
      type: string
      label: "Customer Support Email"
      description: "Surfaced to current customers as the fallback contact."
      default: "cs@jumpermedia.co"
      group: defaults
    minimum_reviews:
      type: number
      label: "Minimum Review Count"
      description: "Disqualifies leads with fewer reviews."
      default: 10
      group: qualification
    minimum_rating:
      type: number
      label: "Minimum Rating"
      description: "Disqualifies leads with rating at or below this value (PDF spec is 'above 3.0')."
      default: 3.0
      group: qualification
tool_schema:
  name: oya_messenger_sdr
  description: "Advance the Jumper Media Messenger SDR conversation by one turn. The procedural-skill harness calls this with `message` + `state` (auto-loaded from agent_memories) and persists the returned state. Returns the next reply, the new state, and a `done` flag."
  parameters:
    type: object
    properties:
      message:
        type: string
        description: "The latest inbound text from the lead. Multi-message names are debounced upstream by the gateway and arrive joined."
        default: ""
      state:
        type: object
        description: "Saved state from the previous turn — the harness round-trips this. `{}` on the very first call."
        default: {}
    required: [message]
---

# Oya Messenger SDR (Jumper Media)

Procedural Messenger SDR built to the Jumper Media spec (PDF: "Oya Messenger SDR — FLOW: OYA AI SDR Onboarding"). The agent persona's only job is to relay `reply` verbatim — every decision (Google Places lookup, Xano customer check, qualification, browser onboarding, Calendly handoff) is deterministic in Python.

## States

| State | Lead said... | Skill does | Next state |
| --- | --- | --- | --- |
| `awaiting_trigger` | (anything before MAPS) | (router only fires this skill on trigger) | `awaiting_business_name` |
| `awaiting_business_name` | "Anna Coffee and cookies" | Search Google Places. 1 → confirm; multiple → ask address; 0 → re-ask. Hardcode: "Jumper Media" → "Hey thats us, whats YOUR business name? :)" | `awaiting_gmb_confirmation` / `awaiting_address` |
| `awaiting_address` | "11659 Fox Rd, Indianapolis…" | Search `<name> + <address>`, confirm top hit | `awaiting_gmb_confirmation` |
| `awaiting_gmb_confirmation` | "yes" | Xano `clientByGmb` lookup → if active customer, send login + support email; else run qualification | `current_customer` / `disqualified_*` / `awaiting_lead_name` |
| `awaiting_lead_name` | "Maria Lopez" | "Whats your full name?" → "Perfect. I'll create your dashboard now. What's the best email for your login?" | `awaiting_lead_email` |
| `awaiting_lead_email` | "maria@…" | Xano `clientByEmail` lookup. If active → current_customer. Else ask for phone. | `awaiting_lead_phone` / `current_customer` |
| `awaiting_lead_phone` | "+1 …" | Drive jumpermedia.co form via Oya Browser, send Calendly confirmation | `complete` |

Disqualifications close warmly (see PDF "Disqualification Quick Reference"):
- `disqualified_hours` — recoverable: when the lead returns days later, re-run the **full** qualification (hours, website, reviews, rating).
- `disqualified_website` / `disqualified_reviews` / `disqualified_rating` — closes the conversation.

## Returning vs current customer

- **Current** (active subscription, detected by GMB place_id OR by email match in Xano) → "Looks like you already have active account with us! Please login {login_url} or contact customer support at {support_email}". State = `current_customer`, `done=true`.
- **Returning** (had a subscription, currently inactive) → "Welcome back! Please schedule a call with one of our representative to reactivate your account {calendly_url}". State = `returning_customer`, `done=true`.

Detection happens at TWO points (per PDF "IMPORTANT" callout):
1. After GMB confirmation, before asking for the lead's name (Xano lookup keyed on `place_id` + business name).
2. After email collection, before driving the browser (Xano lookup keyed on email).

Both endpoints expected to return `{exists: bool, status: "active"|"inactive"|""}`. Anything else is treated as "no match" so a missing endpoint never blocks a real lead.

## Multi-message name handling

Per PDF rules 10–11: leads on Messenger send the business name across multiple messages. The debounce belongs **upstream** — the agent's gateway should buffer inbound messages for a few seconds and pass the joined text. This skill is stateless across messages other than what's in `state`.

## Special case — "Jumper Media"

Per PDF rule 12: if the lead's GMB resolves to Jumper Media itself, reply `"Hey thats us, whats YOUR business name? :)"` and stay in `awaiting_business_name`.

## Browser onboarding

When name + email + phone are collected and the lead hasn't matched a current/returning customer, the skill drives the form:

1. `navigate` → `onboarding_url`
2. Step 1: enter the GMB name + address **EXACTLY as Google Places returned them** (per PDF rule 7 — never the lead's typed text).
3. Step 2: select the matching GMB from the autocomplete.
4. Step 3: enter the lead's name / email / phone.
5. Submit.

Browser failures surface as side-effects — the lead always gets the Calendly confirmation regardless, so a transient hiccup never loses a qualified lead.

## State shape

```json
{
  "step": "awaiting_lead_email",
  "gmb": {
    "place_id": "ChIJ…",
    "name": "The Groovy Cafe",
    "address": "11659 Fox Rd, Indianapolis, IN 46236",
    "website": "https://thegroovycafe.com",
    "rating": 4.6,
    "review_count": 142,
    "has_hours": true
  },
  "lead": {"name": "", "email": "", "phone": ""},
  "candidates": [],
  "disqualification_reason": ""
}
```

## Return shape

```json
{
  "reply": "Whats your full name?",
  "state": { ... },
  "done": false,
  "side_effects": ["places.text_search 1 hit", "xano.gmb_lookup miss", "qualified"]
}
```
