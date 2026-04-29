"""Jumper Media Messenger SDR — procedural skill.

Implements the entire Oya Messenger SDR onboarding flow per the PDF spec
("Oya Messenger SDR — FLOW: OYA AI SDR Onboarding"). Every conversational
decision is deterministic; the agent's only job is to relay `reply` verbatim.

Inputs (from the procedural-skill harness):
    message: str   — the latest inbound text (multi-message names already joined upstream)
    state:   dict  — round-tripped opaque state; `{}` on first call

Returns:
    {
        "reply": str,
        "state": dict,
        "done":  bool,
        "side_effects": list[str],
    }

Env:
    GOOGLE_PLACES_API_KEY       (required)
    XANO_API_GROUP_BASE_URL     (optional — when missing, customer-detection is skipped)
    XANO_AUTH_TOKEN             (optional)
    BROWSER_API_KEY             (optional — when missing, browser step is skipped, lead still gets Calendly)
    BROWSER_API_BASE            (optional, default https://browser.oya.ai)

    INPUT_*                     — the harness sets these for each kwarg
    INPUT_JSON                  — fallback when kwargs were complex
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx


# ---------------------------------------------------------------------------
# Configurable constants (overridable via the skill's per-agent config field)
# ---------------------------------------------------------------------------

DEFAULT_ONBOARDING_URL = "https://local.jumpermedia.co/onboarding/utm=oya"
DEFAULT_CALENDLY_URL = "https://calendly.com/jmpsales/google-ranking-increase-jumper-local"
DEFAULT_LOGIN_URL = "https://local.jumpermedia.co/"
DEFAULT_SUPPORT_EMAIL = "cs@jumpermedia.co"
DEFAULT_MIN_REVIEWS = 10
DEFAULT_MIN_RATING = 3.0  # PDF: "Rating above 3.0" — disqualified at <= 3.0

JUMPER_MEDIA_NAME_LC = "jumper media"


# ---------------------------------------------------------------------------
# Conversation copy (verbatim from PDF "Conversation Script")
# ---------------------------------------------------------------------------

WELCOME = "Hey {first_name}! I'm Hannah \U0001f44b Give me your business name. Going to look you up to see if we can help"
JUMPER_HARDCODE = "Hey thats us, whats YOUR business name? :)"
ASK_ADDRESS = "Sorry! Couldn't find your profile. What's your business address?"
NO_RESULTS = (
    "Hmm, I wasn't able to find that listing on Google. Could you double-check the name? "
    "It should appear exactly as it does when you search your business on Google Maps."
)
CONFIRM_GMB = "Is this your business?\n\n{name}\n{address}"
CURRENT_CUSTOMER_MSG = (
    "Looks like you already have active account with us! Please login {login_url} "
    "or contact customer support at {support_email}"
)
RETURNING_CUSTOMER_MSG = (
    "Welcome back! Please schedule a call with one of our representative to reactive your account {calendly_url}"
)

DISQUAL_NO_HOURS = (
    "Looks like your Google Business Profile doesn't meet all of our requirements. "
    "Please add business hours to your profile and try again."
)
DISQUAL_NO_WEBSITE = (
    "Looks like your Google Business Profile doesn't meet all of our requirements. "
    "Please add a website to your profile and try again."
)
DISQUAL_FEW_REVIEWS = (
    "Looks like your Google Business Profile doesn't meet all of our requirements. "
    "We need to see at least {min_reviews} reviews on your profile."
)
DISQUAL_LOW_RATING = (
    "Looks like your Google Business Profile doesn't meet all of our requirements. "
    "We need to see at least a {min_rating} or higher rating on your Google Business Profile."
)

ASK_FULL_NAME = "Whats your full name?"
ASK_EMAIL = "Perfect. I'll create your dashboard now. What's the best email for your login?"
ASK_PHONE = "And what phone number can I text your login details to?"
COMPLETE_MSG = (
    "Awesome! Your free trial of Jumper Local has been initiated. You should see improved rankings in less than a week. "
    "The last step is to schedule with a specialist to go over your results. Choose a time that works best for you here: "
    "{calendly_url}"
)


# ---------------------------------------------------------------------------
# State machine — step labels (mirror SKILL.md table)
# ---------------------------------------------------------------------------

STEP_AWAITING_BUSINESS_NAME = "awaiting_business_name"
STEP_AWAITING_ADDRESS = "awaiting_address"
STEP_AWAITING_GMB_CONFIRMATION = "awaiting_gmb_confirmation"
STEP_AWAITING_LEAD_NAME = "awaiting_lead_name"
STEP_AWAITING_LEAD_EMAIL = "awaiting_lead_email"
STEP_AWAITING_LEAD_PHONE = "awaiting_lead_phone"
STEP_DISQUAL_HOURS = "disqualified_hours"
STEP_DISQUAL_WEBSITE = "disqualified_website"
STEP_DISQUAL_REVIEWS = "disqualified_reviews"
STEP_DISQUAL_RATING = "disqualified_rating"
STEP_COMPLETE = "complete"
STEP_CURRENT_CUSTOMER = "current_customer"
STEP_RETURNING_CUSTOMER = "returning_customer"


_YES_RE = re.compile(r"^\s*(y|yes|yeah|yep|yup|correct|that's it|thats it|sure|right|ok|okay)\b", re.I)
_NO_RE = re.compile(r"^\s*(n|no|nope|nah|wrong|not that|that's not|thats not)\b", re.I)


# ---------------------------------------------------------------------------
# External integrations (Google Places, Xano, Browser)
# ---------------------------------------------------------------------------


def _places_search(query: str, api_key: str) -> tuple[list[dict], list[str]]:
    """Return (candidates, side_effects). Each candidate has place_id, name, address."""
    side: list[str] = []
    if not api_key:
        side.append("places.skipped no-key")
        return [], side
    try:
        with httpx.Client(timeout=15) as c:
            r = c.post(
                "https://places.googleapis.com/v1/places:searchText",
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": api_key,
                    "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress",
                },
                json={"textQuery": query, "maxResultCount": 5},
            )
            r.raise_for_status()
            data = r.json() or {}
    except Exception as exc:
        side.append(f"places.search_failed {exc.__class__.__name__}")
        return [], side
    out: list[dict] = []
    for p in data.get("places") or []:
        out.append({
            "place_id": p.get("id", ""),
            "name": (p.get("displayName") or {}).get("text", "") if isinstance(p.get("displayName"), dict) else (p.get("displayName") or ""),
            "address": p.get("formattedAddress", ""),
        })
    side.append(f"places.text_search {len(out)} hits for {query[:40]!r}")
    return out, side


def _places_details(place_id: str, api_key: str) -> tuple[dict, list[str]]:
    """Fetch hours/website/rating/review_count for a place. Returns (gmb_dict, side_effects)."""
    side: list[str] = []
    if not api_key or not place_id:
        return {}, ["places.details_skipped"]
    try:
        with httpx.Client(timeout=15) as c:
            r = c.get(
                f"https://places.googleapis.com/v1/places/{place_id}",
                headers={
                    "X-Goog-Api-Key": api_key,
                    "X-Goog-FieldMask": "id,displayName,formattedAddress,websiteUri,rating,userRatingCount,regularOpeningHours,currentOpeningHours",
                },
            )
            r.raise_for_status()
            d = r.json() or {}
    except Exception as exc:
        side.append(f"places.details_failed {exc.__class__.__name__}")
        return {}, side
    name = (d.get("displayName") or {}).get("text", "") if isinstance(d.get("displayName"), dict) else (d.get("displayName") or "")
    has_hours = bool((d.get("regularOpeningHours") or {}).get("periods")) or bool((d.get("currentOpeningHours") or {}).get("periods"))
    side.append(f"places.details {'ok' if d else 'empty'}")
    return {
        "place_id": d.get("id", place_id),
        "name": name,
        "address": d.get("formattedAddress", ""),
        "website": d.get("websiteUri", ""),
        "rating": d.get("rating", 0.0) or 0.0,
        "review_count": d.get("userRatingCount", 0) or 0,
        "has_hours": has_hours,
    }, side


def _xano_lookup(path_suffix: str, body: dict) -> tuple[dict, list[str]]:
    """Generic Xano POST helper. Returns (parsed_response, side_effects)."""
    base = os.getenv("XANO_API_GROUP_BASE_URL", "").strip().rstrip("/")
    if not base:
        return {}, ["xano.skipped no-base-url"]
    url = f"{base}{path_suffix}"
    headers = {"Content-Type": "application/json"}
    token = os.getenv("XANO_AUTH_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=10) as c:
            r = c.post(url, headers=headers, json=body)
            if r.status_code == 404:
                return {}, [f"xano.{path_suffix} 404"]
            r.raise_for_status()
            data = r.json() or {}
            if not isinstance(data, dict):
                return {}, [f"xano.{path_suffix} non-dict"]
            return data, [f"xano.{path_suffix} {'hit' if data.get('exists') else 'miss'}"]
    except Exception as exc:
        return {}, [f"xano.{path_suffix}_failed {exc.__class__.__name__}"]


def _xano_check_by_gmb(place_id: str, name: str) -> tuple[str, list[str]]:
    """Return ("active"|"inactive"|"" , side_effects). Empty string == not a customer."""
    data, side = _xano_lookup("/clientByGmb", {"place_id": place_id, "business_name": name})
    if not data.get("exists"):
        return "", side
    status = str(data.get("status") or "").strip().lower()
    return ("active" if status == "active" else "inactive" if status == "inactive" else ""), side


def _xano_check_by_email(email: str) -> tuple[str, list[str]]:
    data, side = _xano_lookup("/clientByEmail", {"email": email})
    if not data.get("exists"):
        return "", side
    status = str(data.get("status") or "").strip().lower()
    return ("active" if status == "active" else "inactive" if status == "inactive" else ""), side


def _drive_browser_onboarding(
    *,
    gmb: dict,
    lead: dict,
    onboarding_url: str,
    playbook_name: str = "jumper_onboarding",
) -> list[str]:
    """Drive the onboarding form via a NAMED PLAYBOOK from the browser gateway.

    The playbook lives on `agent_gateways.config["playbooks"]` for the browser
    gateway and is delivered to this sandbox script as `BROWSER_PLAYBOOKS_JSON`
    (one JSON-encoded list). The runner substitutes `{{gmb_name}}` /
    `{{gmb_address}}` / `{{lead_name}}` / `{{lead_email}}` / `{{lead_phone}}`
    into each step's string fields and dispatches to the Oya Browser API.

    Best-effort — failures surface as side-effects so the lead still gets the
    Calendly confirmation regardless of a transient browser hiccup (PDF rule).
    """
    api_key = os.getenv("BROWSER_API_KEY", "").strip()
    api_base = os.getenv("BROWSER_API_BASE", "https://browser.oya.ai").strip()
    raw_pb = os.getenv("BROWSER_PLAYBOOKS_JSON", "").strip()
    if not api_key:
        return ["browser.skipped no-api-key"]
    if not raw_pb:
        return ["browser.skipped no-playbooks"]

    try:
        playbooks = json.loads(raw_pb)
    except Exception:
        return ["browser.failed playbooks_json_parse"]

    # Find the named playbook (list-of-dicts preferred; legacy dict-keyed
    # shape also accepted via find_playbook helper).
    playbook = None
    if isinstance(playbooks, list):
        for p in playbooks:
            if isinstance(p, dict) and p.get("name") == playbook_name:
                playbook = p
                break
    elif isinstance(playbooks, dict):
        v = playbooks.get(playbook_name)
        if isinstance(v, dict):
            playbook = {"name": playbook_name, **v} if "name" not in v else v
    if not playbook:
        return [f"browser.skipped no-playbook:{playbook_name}"]

    params = {
        "gmb_name": gmb.get("name", ""),
        "gmb_address": gmb.get("address", ""),
        "lead_name": lead.get("name", ""),
        "lead_email": lead.get("email", ""),
        "lead_phone": lead.get("phone", ""),
        "onboarding_url": onboarding_url,
    }

    # Inline minimal runner — duplicates a slice of app/browser/playbook_runner
    # because the sandbox can't import backend modules. Kept narrow on purpose.
    try:
        with httpx.Client(timeout=60) as c:
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            base = api_base.rstrip("/")
            # Resolve a browser id from the pool.
            r = c.get(f"{base}/api/pool", headers=headers)
            if r.status_code >= 400:
                return [f"browser.failed pool_http_{r.status_code}"]
            pool = (r.json() or {}).get("browsers") or []
            if not pool:
                return ["browser.failed no_browser_in_pool"]
            browser_id = pool[0].get("browser_id") if isinstance(pool[0], dict) else pool[0]
            if not browser_id:
                return ["browser.failed no_browser_id"]
            steps = playbook.get("steps") or []
            for i, step in enumerate(steps):
                rendered = {}
                for k, v in step.items():
                    if isinstance(v, str):
                        for pk, pv in params.items():
                            v = v.replace("{{" + pk + "}}", str(pv))
                    rendered[k] = v
                action = rendered.get("action")
                if action == "wait":
                    import time as _t
                    _t.sleep(min(int(rendered.get("ms", 0) or 0), 30000) / 1000.0)
                    continue
                cmd_params: dict[str, Any] = {}
                if action == "navigate":
                    cmd_params["url"] = rendered.get("url", "")
                elif action == "type":
                    cmd_params = {"selector": rendered.get("selector", ""), "text": rendered.get("value", "")}
                elif action == "click":
                    cmd_params["selector"] = rendered.get("selector", "")
                elif action == "select_option":
                    cmd_params = {"selector": rendered.get("selector", ""), "value": rendered.get("value", "")}
                elif action == "assert_url":
                    cmd_params["contains"] = rendered.get("contains", "")
                else:
                    return [f"browser.failed unsupported_action_step_{i}_{action}"]
                resp = c.post(f"{base}/api/browsers/{browser_id}/command", headers=headers,
                              json={"action": action, "params": cmd_params})
                if resp.status_code >= 400:
                    return [f"browser.failed step_{i}_http_{resp.status_code}"]
                body = resp.json() if resp.content else {}
                if isinstance(body, dict) and body.get("ok") is False:
                    return [f"browser.failed step_{i}_{body.get('error', 'unknown')}"]
        return [f"browser.playbook_completed:{playbook_name}"]
    except Exception as exc:
        return [f"browser.failed {exc.__class__.__name__}"]


# ---------------------------------------------------------------------------
# Conversational helpers
# ---------------------------------------------------------------------------


def _is_yes(text: str) -> bool:
    return bool(_YES_RE.match(text or ""))


def _is_no(text: str) -> bool:
    return bool(_NO_RE.match(text or ""))


_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+")
_PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{6,}\d")


def _extract_email(text: str) -> str:
    m = _EMAIL_RE.search(text or "")
    return m.group(0).strip() if m else ""


def _extract_phone(text: str) -> str:
    m = _PHONE_RE.search(text or "")
    return m.group(0).strip() if m else ""


def _qualification_failure(gmb: dict, *, min_reviews: int, min_rating: float) -> str | None:
    """Return the first failing-criterion key, or None if qualified.

    Order matches the disqualification message ordering in the PDF.
    """
    if not gmb.get("has_hours"):
        return STEP_DISQUAL_HOURS
    if not gmb.get("website"):
        return STEP_DISQUAL_WEBSITE
    if int(gmb.get("review_count", 0) or 0) < int(min_reviews):
        return STEP_DISQUAL_REVIEWS
    # PDF "above 3.0" -> rating must be > 3.0
    if float(gmb.get("rating", 0) or 0) <= float(min_rating):
        return STEP_DISQUAL_RATING
    return None


def _normalize_state(state: dict | None) -> dict:
    s = dict(state or {})
    s.setdefault("step", "")
    s.setdefault("gmb", {})
    s.setdefault("lead", {"name": "", "email": "", "phone": ""})
    s.setdefault("candidates", [])
    s.setdefault("disqualification_reason", "")
    return s


# ---------------------------------------------------------------------------
# Main entry point — called by the procedural-skill harness once per turn
# ---------------------------------------------------------------------------


def run(
    *,
    message: str = "",
    state: dict | None = None,
    config: dict | None = None,
    first_name: str = "",
) -> dict[str, Any]:
    """Advance one turn of the SDR conversation. See module docstring."""
    api_key = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()
    cfg = config or {}
    onboarding_url = cfg.get("onboarding_url") or DEFAULT_ONBOARDING_URL
    calendly_url = cfg.get("calendly_url") or DEFAULT_CALENDLY_URL
    login_url = cfg.get("login_url") or DEFAULT_LOGIN_URL
    support_email = cfg.get("support_email") or DEFAULT_SUPPORT_EMAIL
    min_reviews = int(cfg.get("minimum_reviews") or DEFAULT_MIN_REVIEWS)
    min_rating = float(cfg.get("minimum_rating") or DEFAULT_MIN_RATING)

    s = _normalize_state(state)
    text = (message or "").strip()
    side: list[str] = []

    # Recovery path: a lead disqualified for hours returns later. PDF rule:
    # "If a lead is disqualified for hours and later returns, re-run the FULL
    #  qualification check, not just hours." We restart the lookup from the
    #  business name they provide.
    if s["step"] == STEP_DISQUAL_HOURS and text:
        s["step"] = STEP_AWAITING_BUSINESS_NAME

    # First call — no prior step. Show welcome and prime for business name.
    if not s["step"]:
        s["step"] = STEP_AWAITING_BUSINESS_NAME
        return {
            "reply": WELCOME.format(first_name=(first_name or "there").strip() or "there"),
            "state": s,
            "done": False,
            "side_effects": ["welcome_sent"],
        }

    # ---- Branches ---------------------------------------------------------

    if s["step"] == STEP_AWAITING_BUSINESS_NAME:
        if not text:
            return {"reply": WELCOME.format(first_name=first_name or "there"), "state": s, "done": False, "side_effects": []}
        # PDF rule 12: hardcode "Jumper Media"
        if text.strip().lower() == JUMPER_MEDIA_NAME_LC:
            return {"reply": JUMPER_HARDCODE, "state": s, "done": False, "side_effects": ["jumper_hardcode"]}
        candidates, ps_side = _places_search(text, api_key)
        side.extend(ps_side)
        if not candidates:
            return {"reply": NO_RESULTS, "state": s, "done": False, "side_effects": side}
        if len(candidates) == 1:
            s["candidates"] = candidates
            s["step"] = STEP_AWAITING_GMB_CONFIRMATION
            top = candidates[0]
            return {
                "reply": CONFIRM_GMB.format(name=top["name"], address=top["address"]),
                "state": s, "done": False, "side_effects": side,
            }
        s["candidates"] = candidates
        s["step"] = STEP_AWAITING_ADDRESS
        return {"reply": ASK_ADDRESS, "state": s, "done": False, "side_effects": side}

    if s["step"] == STEP_AWAITING_ADDRESS:
        if not text:
            return {"reply": ASK_ADDRESS, "state": s, "done": False, "side_effects": []}
        # Re-run search with name + address. We don't have the original name
        # text here, so use the top candidate's name as the seed (per PDF
        # spec: candidates were already returned in step 2; we narrow with
        # the address).
        seed_name = (s["candidates"][0]["name"] if s["candidates"] else "")
        query = f"{seed_name} {text}".strip() if seed_name else text
        candidates, ps_side = _places_search(query, api_key)
        side.extend(ps_side)
        if not candidates:
            return {"reply": NO_RESULTS, "state": s, "done": False, "side_effects": side}
        s["candidates"] = candidates
        s["step"] = STEP_AWAITING_GMB_CONFIRMATION
        top = candidates[0]
        return {
            "reply": CONFIRM_GMB.format(name=top["name"], address=top["address"]),
            "state": s, "done": False, "side_effects": side,
        }

    if s["step"] == STEP_AWAITING_GMB_CONFIRMATION:
        if _is_no(text):
            # User rejected the candidate — go back to the name step.
            s["step"] = STEP_AWAITING_BUSINESS_NAME
            s["candidates"] = []
            return {"reply": "No problem — what's the exact business name as it appears on Google Maps?", "state": s, "done": False, "side_effects": ["gmb_rejected"]}
        if not _is_yes(text):
            # Anything other than a clear yes/no — re-prompt softly.
            top = s["candidates"][0] if s["candidates"] else {"name": "", "address": ""}
            return {"reply": CONFIRM_GMB.format(name=top["name"], address=top["address"]), "state": s, "done": False, "side_effects": []}

        top = s["candidates"][0] if s["candidates"] else {}
        details, det_side = _places_details(top.get("place_id", ""), api_key)
        side.extend(det_side)
        s["gmb"] = details or top  # if details lookup fails, fall back to whatever we have

        # PDF "IMPORTANT": check current/returning customer BEFORE asking for name.
        status, x_side = _xano_check_by_gmb(s["gmb"].get("place_id", ""), s["gmb"].get("name", ""))
        side.extend(x_side)
        if status == "active":
            s["step"] = STEP_CURRENT_CUSTOMER
            return {
                "reply": CURRENT_CUSTOMER_MSG.format(login_url=login_url, support_email=support_email),
                "state": s, "done": True, "side_effects": side + ["current_customer_by_gmb"],
            }
        if status == "inactive":
            s["step"] = STEP_RETURNING_CUSTOMER
            return {
                "reply": RETURNING_CUSTOMER_MSG.format(calendly_url=calendly_url),
                "state": s, "done": True, "side_effects": side + ["returning_customer_by_gmb"],
            }

        # Run the silent qualification check.
        fail = _qualification_failure(s["gmb"], min_reviews=min_reviews, min_rating=min_rating)
        if fail == STEP_DISQUAL_HOURS:
            s["step"] = STEP_DISQUAL_HOURS
            s["disqualification_reason"] = "hours"
            # NOT done=True — we want the recovery path on a future return.
            return {"reply": DISQUAL_NO_HOURS, "state": s, "done": False, "side_effects": side + ["disqualified_hours"]}
        if fail == STEP_DISQUAL_WEBSITE:
            s["step"] = STEP_DISQUAL_WEBSITE
            s["disqualification_reason"] = "website"
            return {"reply": DISQUAL_NO_WEBSITE, "state": s, "done": True, "side_effects": side + ["disqualified_website"]}
        if fail == STEP_DISQUAL_REVIEWS:
            s["step"] = STEP_DISQUAL_REVIEWS
            s["disqualification_reason"] = "reviews"
            return {"reply": DISQUAL_FEW_REVIEWS.format(min_reviews=min_reviews), "state": s, "done": True, "side_effects": side + ["disqualified_reviews"]}
        if fail == STEP_DISQUAL_RATING:
            s["step"] = STEP_DISQUAL_RATING
            s["disqualification_reason"] = "rating"
            return {"reply": DISQUAL_LOW_RATING.format(min_rating=min_rating), "state": s, "done": True, "side_effects": side + ["disqualified_rating"]}

        # Qualified. Ask for full name.
        s["step"] = STEP_AWAITING_LEAD_NAME
        return {"reply": ASK_FULL_NAME, "state": s, "done": False, "side_effects": side + ["qualified"]}

    if s["step"] == STEP_AWAITING_LEAD_NAME:
        if not text:
            return {"reply": ASK_FULL_NAME, "state": s, "done": False, "side_effects": []}
        s["lead"]["name"] = text.strip()
        s["step"] = STEP_AWAITING_LEAD_EMAIL
        return {"reply": ASK_EMAIL, "state": s, "done": False, "side_effects": []}

    if s["step"] == STEP_AWAITING_LEAD_EMAIL:
        email = _extract_email(text)
        if not email:
            return {"reply": ASK_EMAIL, "state": s, "done": False, "side_effects": []}
        s["lead"]["email"] = email
        # PDF "IMPORTANT" callout: check email exists BEFORE starting in-browser onboarding.
        status, x_side = _xano_check_by_email(email)
        side.extend(x_side)
        if status == "active":
            s["step"] = STEP_CURRENT_CUSTOMER
            return {
                "reply": CURRENT_CUSTOMER_MSG.format(login_url=login_url, support_email=support_email),
                "state": s, "done": True, "side_effects": side + ["current_customer_by_email"],
            }
        if status == "inactive":
            s["step"] = STEP_RETURNING_CUSTOMER
            return {
                "reply": RETURNING_CUSTOMER_MSG.format(calendly_url=calendly_url),
                "state": s, "done": True, "side_effects": side + ["returning_customer_by_email"],
            }
        s["step"] = STEP_AWAITING_LEAD_PHONE
        return {"reply": ASK_PHONE, "state": s, "done": False, "side_effects": side}

    if s["step"] == STEP_AWAITING_LEAD_PHONE:
        phone = _extract_phone(text)
        if not phone:
            return {"reply": ASK_PHONE, "state": s, "done": False, "side_effects": []}
        s["lead"]["phone"] = phone
        # Drive the browser onboarding form (best-effort; lead always gets Calendly).
        br_side = _drive_browser_onboarding(gmb=s["gmb"], lead=s["lead"], onboarding_url=onboarding_url)
        side.extend(br_side)
        s["step"] = STEP_COMPLETE
        return {
            "reply": COMPLETE_MSG.format(calendly_url=calendly_url),
            "state": s, "done": True, "side_effects": side + ["onboarding_complete"],
        }

    # Disqualified-but-recoverable branches: only `disqualified_hours` is
    # explicitly recoverable per the PDF. The branch was already handled at
    # the top of run() — if we end up here, the lead is in a terminal state.
    if s["step"] in (STEP_DISQUAL_WEBSITE, STEP_DISQUAL_REVIEWS, STEP_DISQUAL_RATING, STEP_COMPLETE,
                     STEP_CURRENT_CUSTOMER, STEP_RETURNING_CUSTOMER):
        # No further action — re-emit the closing message so the harness still
        # has a reply to push, but mark done=True (clears state).
        closing = {
            STEP_DISQUAL_WEBSITE: DISQUAL_NO_WEBSITE,
            STEP_DISQUAL_REVIEWS: DISQUAL_FEW_REVIEWS.format(min_reviews=min_reviews),
            STEP_DISQUAL_RATING: DISQUAL_LOW_RATING.format(min_rating=min_rating),
            STEP_COMPLETE: COMPLETE_MSG.format(calendly_url=calendly_url),
            STEP_CURRENT_CUSTOMER: CURRENT_CUSTOMER_MSG.format(login_url=login_url, support_email=support_email),
            STEP_RETURNING_CUSTOMER: RETURNING_CUSTOMER_MSG.format(calendly_url=calendly_url),
        }[s["step"]]
        return {"reply": closing, "state": s, "done": True, "side_effects": ["terminal_replay"]}

    # Unknown state — defensive reset.
    s["step"] = STEP_AWAITING_BUSINESS_NAME
    return {
        "reply": WELCOME.format(first_name=first_name or "there"),
        "state": s, "done": False, "side_effects": ["state_reset"],
    }


# ---------------------------------------------------------------------------
# Sandbox entry — the procedural-skill harness invokes the skill via the
# standard sandbox executor. INPUT_* env vars carry the kwargs; INPUT_JSON
# is the same data as a single dict. We emit the result as JSON on stdout,
# which the harness parses.
# ---------------------------------------------------------------------------


def _load_inputs() -> dict[str, Any]:
    raw = os.environ.get("INPUT_JSON", "")
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    out: dict[str, Any] = {}
    for k, v in os.environ.items():
        if not k.startswith("INPUT_") or k == "INPUT_JSON":
            continue
        out[k[len("INPUT_"):].lower()] = v
    return out


if __name__ == "__main__":  # pragma: no cover - sandbox entry only
    inputs = _load_inputs()
    state_in = inputs.get("state") or {}
    if isinstance(state_in, str):
        try:
            state_in = json.loads(state_in)
        except Exception:
            state_in = {}
    config_in = inputs.get("config") or {}
    if isinstance(config_in, str):
        try:
            config_in = json.loads(config_in)
        except Exception:
            config_in = {}
    result = run(
        message=str(inputs.get("message", "") or ""),
        state=state_in if isinstance(state_in, dict) else {},
        config=config_in if isinstance(config_in, dict) else {},
        first_name=str(inputs.get("first_name", "") or ""),
    )
    print(json.dumps(result))
