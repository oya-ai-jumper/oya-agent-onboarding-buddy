import os, json, re, httpx


def _md_to_slack(md: str) -> str:
    """Convert standard Markdown to Slack mrkdwn (bold, italic, lists, headers, links)."""
    lines = md.split("\n")
    result = []
    in_code = False
    for line in lines:
        if line.strip().startswith("```"):
            in_code = not in_code
            result.append(line)
            continue
        if in_code:
            result.append(line)
            continue
        # Headers → bold
        hm = re.match(r"^(#{1,6})\s+(.+)$", line)
        if hm:
            result.append(f"\n*{_inline(hm.group(2).strip())}*")
            continue
        # Bullet points: - or * → •
        bm = re.match(r"^(\s*)[-*]\s+(.+)$", line)
        if bm:
            result.append(f"{bm.group(1)}\u2022 {_inline(bm.group(2))}")
            continue
        # Numbered lists
        nm = re.match(r"^(\s*)(\d+\.)\s+(.+)$", line)
        if nm:
            result.append(f"{nm.group(1)}{nm.group(2)} {_inline(nm.group(3))}")
            continue
        # Horizontal rules
        if re.match(r"^[-*_]{3,}\s*$", line.strip()):
            result.append("\u2014\u2014\u2014")
            continue
        result.append(_inline(line))
    return "\n".join(result)


def _inline(text: str) -> str:
    """Convert inline Markdown formatting to Slack mrkdwn."""
    # Protect code spans
    codes = []

    def _save(m):
        codes.append(m.group(0))
        return f"\x00C{len(codes)-1}\x00"

    text = re.sub(r"`[^`]+`", _save, text)
    # Links
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)
    # Bold+italic
    text = re.sub(r"\*{3}(.+?)\*{3}", r"*_\1_*", text)
    # Bold → sentinel
    text = re.sub(r"\*{2}(.+?)\*{2}", "\x01\\1\x01", text)
    text = re.sub(r"__(.+?)__", "\x01\\1\x01", text)
    # Italic
    text = re.sub(r"(?<!\w)\*([^*]+?)\*(?!\w)", r"_\1_", text)
    # Restore bold
    text = text.replace("\x01", "*")
    # Strikethrough
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)
    # Restore code
    for i, c in enumerate(codes):
        text = text.replace(f"\x00C{i}\x00", c)
    return text


try:
    token = os.environ["SLACK_BOT_TOKEN"]
    inp = json.loads(os.environ.get("INPUT_JSON", "{}"))
    channel = inp.get("channel", "")
    thread_ts = inp.get("thread_ts", "")
    text = inp.get("text", "")
    if not channel or not text:
        print(json.dumps({"error": "channel and text are required"}))
    else:
        slack_text = _md_to_slack(text)
        payload = {"channel": channel, "text": slack_text}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        with httpx.Client(timeout=15) as c:
            r = c.post("https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=payload)
            data = r.json()
        # Fallback: if thread not found, post to channel directly
        if not data.get("ok") and data.get("error") == "thread_not_found" and thread_ts:
            payload.pop("thread_ts", None)
            with httpx.Client(timeout=15) as c:
                r = c.post("https://slack.com/api/chat.postMessage",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json=payload)
                data = r.json()
        if data.get("ok"):
            print(json.dumps({"ok": True, "channel": channel, "ts": data.get("ts")}))
        else:
            print(json.dumps({"error": data.get("error", "unknown")}))
except Exception as e:
    print(json.dumps({"error": str(e)}))
