from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .common import GuardError


def chunks(text, limit=3000):
    # Telegram counts UTF-16 units; 3000 Python chars could contain 6000 units.
    part, size = "", 0
    for char in text:
        units = len(char.encode("utf-16-le")) // 2
        if size + units > limit:
            yield part
            part, size = "", 0
        part += char
        size += units
    if part:
        yield part


def api(method, payload, token=None):
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token or not token.strip() or any(x.isspace() for x in token):
        raise GuardError("缺少有效 TELEGRAM_BOT_TOKEN；不要在日志或聊天中粘贴密钥")
    # Never log the request URL: it contains the token. Never propagate urllib exception text.
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                result = json.loads(response.read())
            if not result.get("ok"):
                raise GuardError("Telegram 返回失败，请检查机器人权限和 Chat ID")
            return result["result"]
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 2:
                try:
                    delay = min(30, max(1, int(json.loads(exc.read()).get("parameters", {}).get("retry_after", 3))))
                except (ValueError, TypeError):
                    delay = 3
                time.sleep(delay)
                continue
            raise GuardError(f"Telegram HTTP {exc.code}；响应及 URL 已隐藏以保护 Token") from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            # Ambiguous timeout may have delivered the message: do not blindly resend.
            raise GuardError("Telegram 网络失败，未自动重发，避免重复消息；URL 已隐藏") from None
    raise GuardError("Telegram 限流，请稍后重试")


def send_report(text, parse_mode=None):
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        raise GuardError("缺少 TELEGRAM_CHAT_ID")
    for part in chunks(text):
        payload = {"chat_id": chat_id, "text": part, "link_preview_options": {"is_disabled": True}}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        api("sendMessage", payload)


def chat_ids():
    # Only print identifiers, never raw updates (which contain user messages).
    updates = api("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
    return sorted({str(u["message"]["chat"]["id"]) for u in updates if "message" in u})
