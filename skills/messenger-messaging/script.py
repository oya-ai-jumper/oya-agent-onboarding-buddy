import os
import json
import httpx

UNIPILE_DSN = os.environ.get("UNIPILE_DSN", "").rstrip("/")
UNIPILE_API_KEY = os.environ.get("UNIPILE_API_KEY", "")
UNIPILE_ACCOUNT_ID = os.environ.get("UNIPILE_ACCOUNT_ID", "")


def _headers():
    return {
        "X-API-KEY": UNIPILE_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _params(extra=None):
    p = {"account_id": UNIPILE_ACCOUNT_ID}
    if extra:
        p.update(extra)
    return p


def api_get(path, params=None, timeout=15):
    with httpx.Client(timeout=timeout) as c:
        r = c.get(f"{UNIPILE_DSN}/{path}", headers=_headers(), params=_params(params))
        r.raise_for_status()
        return r.json()


def api_post(path, body=None, params=None, timeout=15):
    with httpx.Client(timeout=timeout) as c:
        r = c.post(f"{UNIPILE_DSN}/{path}", headers=_headers(), json=body or {}, params=_params(params))
        r.raise_for_status()
        return r.json() if r.content else {}


# --- Actions ---


def do_list_chats(limit=20):
    data = api_get("api/v1/chats", {"limit": str(limit)})
    chats = data.get("items") or data.get("chats") or (data if isinstance(data, list) else [])
    results = []
    for chat in chats:
        results.append({
            "chat_id": chat.get("id") or chat.get("chat_id") or "",
            "name": chat.get("name") or chat.get("title") or "",
            "last_message": (chat.get("last_message") or {}).get("text", ""),
            "updated_at": chat.get("updated_at") or chat.get("timestamp") or "",
            "attendees_count": len(chat.get("attendees") or []),
        })
    return {"chats": results, "count": len(results)}


def do_get_chat(chat_id):
    if not chat_id:
        return {"error": "chat_id is required"}
    data = api_get(f"api/v1/chats/{chat_id}")
    return {
        "chat_id": data.get("id") or data.get("chat_id") or chat_id,
        "name": data.get("name") or data.get("title") or "",
        "attendees": data.get("attendees") or [],
        "updated_at": data.get("updated_at") or "",
    }


def do_read_messages(chat_id, limit=20):
    if not chat_id:
        return {"error": "chat_id is required"}
    data = api_get(f"api/v1/chats/{chat_id}/messages", {"limit": str(limit)})
    msgs = data.get("items") or data.get("messages") or (data if isinstance(data, list) else [])
    results = []
    for msg in msgs:
        sender = msg.get("sender") or msg.get("from") or {}
        results.append({
            "message_id": msg.get("id") or msg.get("message_id") or "",
            "text": msg.get("text") or msg.get("body") or msg.get("content") or "",
            "sender_name": sender.get("name") or sender.get("display_name") or "" if isinstance(sender, dict) else str(sender),
            "is_from_me": msg.get("is_from_me") or msg.get("from_me") or False,
            "timestamp": msg.get("timestamp") or msg.get("created_at") or "",
        })
    return {"messages": results, "count": len(results)}


def do_send_message(chat_id, text):
    if not chat_id:
        return {"error": "chat_id is required"}
    if not text or not text.strip():
        return {"error": "text is required"}
    body = {
        "account_id": UNIPILE_ACCOUNT_ID,
        "text": text.strip(),
    }
    data = api_post(f"api/v1/chats/{chat_id}/messages", body)
    return {
        "sent": True,
        "message_id": data.get("id") or data.get("message_id") or "",
    }


def do_start_chat(attendees_ids, text):
    if not attendees_ids:
        return {"error": "attendees_ids is required (comma-separated user IDs or phone numbers)"}
    if not text or not text.strip():
        return {"error": "text is required"}
    ids = [x.strip() for x in attendees_ids.split(",") if x.strip()]
    body = {
        "account_id": UNIPILE_ACCOUNT_ID,
        "attendees_ids": ids,
        "text": text.strip(),
    }
    data = api_post("api/v1/chats", body)
    return {
        "started": True,
        "chat_id": data.get("id") or data.get("chat_id") or "",
    }


# --- Dispatcher ---


def run(action, **kwargs):
    if not UNIPILE_DSN or not UNIPILE_API_KEY or not UNIPILE_ACCOUNT_ID:
        return {"error": "Messenger not connected. Connect Messenger in the Channels section to use this skill."}

    actions = {
        "list_chats": lambda: do_list_chats(kwargs.get("limit", 20)),
        "get_chat": lambda: do_get_chat(kwargs.get("chat_id", "")),
        "read_messages": lambda: do_read_messages(kwargs.get("chat_id", ""), kwargs.get("limit", 20)),
        "send_message": lambda: do_send_message(kwargs.get("chat_id", ""), kwargs.get("text", "")),
        "start_chat": lambda: do_start_chat(kwargs.get("attendees_ids", ""), kwargs.get("text", "")),
    }

    if action not in actions:
        return {"error": f"Unknown action '{action}'. Available: {', '.join(actions.keys())}"}

    try:
        return actions[action]()
    except httpx.HTTPStatusError as e:
        return {"error": f"API error {e.response.status_code}: {e.response.text[:500]}"}
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    import sys
    input_data = json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}
    action = input_data.pop("action", "list_chats")
    result = run(action, **input_data)
    print(json.dumps(result, indent=2, default=str))
