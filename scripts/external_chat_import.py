"""Shared helpers for importing JSON produced by external chat exporters."""

from import_store import save_import_bundle
from message_normalizer import normalize_payload


NUMERIC_TYPE_MAP = {
    1: "text",
    3: "image",
    34: "voice",
    42: "card",
    43: "video",
    47: "emoji",
    48: "location",
    49: "link",
    50: "call",
    10000: "system",
    10002: "quote",
}

TEXT_TYPE_MAP = {
    "text": "text", "文本": "text", "文本消息": "text",
    "image": "image", "图片": "image", "图片消息": "image",
    "voice": "voice", "语音": "voice", "语音消息": "voice",
    "video": "video", "视频": "video", "视频消息": "video",
    "emoji": "emoji", "表情": "emoji", "动画表情": "emoji",
    "link": "link", "链接": "link", "链接消息": "link",
    "file": "file", "文件": "file", "文件消息": "file",
    "system": "system", "系统": "system", "系统消息": "system",
    "quote": "quote", "引用": "quote", "引用消息": "quote",
    "card": "card", "名片": "card", "location": "location", "位置": "location",
}

PLACEHOLDERS = {
    "image": "[图片]",
    "voice": "[语音消息]",
    "video": "[视频]",
    "emoji": "[表情]",
    "file": "[文件]",
    "card": "[名片]",
    "location": "[位置]",
    "call": "[通话]",
}


def load_message_array(data, source_name):
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        return data["messages"]
    raise ValueError(f"不是有效的 {source_name} JSON：顶层需要是消息数组或包含 messages 数组")


def normalize_external_type(value):
    if isinstance(value, bool):
        return "other"
    if isinstance(value, (int, float)):
        return NUMERIC_TYPE_MAP.get(int(value), "other")
    text = str(value or "").strip().lower()
    if text.isdigit():
        return NUMERIC_TYPE_MAP.get(int(text), "other")
    return TEXT_TYPE_MAP.get(text, "other")


def content_or_placeholder(content, message_type):
    text = str(content or "")
    return text or PLACEHOLDERS.get(message_type, "")


def observed_owner_usernames(data):
    """Keep explicit outgoing sender identities without choosing an account."""
    messages = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(messages, list):
        return []
    return sorted({raw["senderUsername"] for raw in messages
                   if isinstance(raw, dict) and raw.get("isSend") in (1, True, "1")
                   and isinstance(raw.get("senderUsername"), str) and raw["senderUsername"]})


def write_contact_bundle(payload, contact, contact_id, output_dir, *, identity_context=None):
    normalized = normalize_payload(payload, drop_invalid=True)
    if identity_context is None:
        identity_context = {
            "source": payload.get("source"),
            "request": {"contact": contact, "contact_id": contact_id},
            "source_identity": {key: payload.get(key) for key in (
                "contact_username", "contact_display", "own_wxid",
            )},
        }
    result = save_import_bundle(
        normalized, contact=contact, contact_id=contact_id, output_dir=output_dir,
        identity_context=identity_context,
    )
    return result["payload"], {**result["bundle"], "created": result["created"], "reused": result["reused"]}
