"""从视觉模型的一次回答中提取可供人工核对的笔记草稿。"""

from __future__ import annotations

import json
import re
from typing import Any


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
MAX_CONTENT_CHARS = 8000
MAX_UNCERTAIN_ITEMS = 8


def parse_note_draft(raw: str) -> dict[str, Any] | None:
    """兼容旧版仅含公式的回答；坏的草稿不妨碍公式入库。"""
    text = _FENCE.sub("", str(raw or "").strip())
    start = text.find("{")
    if start < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "content_markdown" not in data:
        return None
    content = data.get("content_markdown")
    if not isinstance(content, str):
        return None
    content = content.strip()[:MAX_CONTENT_CHARS]
    topic = data.get("topic")
    topic = topic.strip()[:120] if isinstance(topic, str) else ""
    uncertain: list[dict[str, str]] = []
    if isinstance(data.get("uncertain_items"), list):
        for item in data["uncertain_items"][:MAX_UNCERTAIN_ITEMS]:
            if not isinstance(item, dict):
                continue
            term, reason = item.get("term"), item.get("reason")
            if isinstance(term, str) and term.strip():
                uncertain.append({
                    "term": term.strip()[:100],
                    "reason": reason.strip()[:200] if isinstance(reason, str) else "",
                })
    return {"topic": topic, "content_markdown": content, "uncertain_items": uncertain}
