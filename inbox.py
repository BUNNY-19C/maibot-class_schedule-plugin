"""Note Inbox：多模态消息归一化 + 入队去重。

去重键用**原文**而不是整理结果：整理是异步的，若用整理结果，入队瞬间
所有文本的 hash 都是同一个空串，后续笔记会被无声吞掉(重建前踩过，写死在这)。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .file_intake import extract_file_candidates

MAX_RAW_CHARS = 8000
DEDUP_WINDOW = timedelta(hours=6)
DEDUP_KEEP = 10000

#: 宿主对纯图片消息的占位符（大小写/中英都在这）
_PLACEHOLDER_RE = re.compile(r"^[\[\{<（(]{0,3}(?:图片|图像|照片|image|img)[\]\}>）)]{0,3}$", re.I)


@dataclass
class ParsedMessage:
    text: str = ""
    images: list[tuple[bytes, str]] = field(default_factory=list)  # (bytes, .png/…)
    links: list[str] = field(default_factory=list)
    files: list[Any] = field(default_factory=list)
    message_id: str = ""
    timestamp: str = ""


def normalize_image_segment(node: dict) -> tuple[str, str]:
    """从 image/emoji 段提取 (base64, url)，认得 SnowLuma 的顶层 binary_data_base64。"""
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    b64 = ""
    for key in ("binary_data_base64", "base64", "base64_data", "data_base64"):
        value = node.get(key) or data.get(key)
        if value:
            b64 = str(value).strip()
            break
    url = ""
    for key in ("url", "image_url", "file_url"):
        value = node.get(key) or data.get(key)
        if value:
            url = str(value).strip()
            break
    return b64, url


def _walk(node: Any, visit, depth: int = 0) -> None:
    if depth > 5:
        return
    if isinstance(node, list):
        for item in node:
            _walk(item, visit, depth + 1)
        return
    if not isinstance(node, dict):
        return
    visit(node)
    data = node.get("data")
    if isinstance(data, (dict, list)):
        _walk(data, visit, depth + 1)


def parse_message(message: Any) -> ParsedMessage:
    """从入站消息提取文本(含引用/转发的文字)、图片、链接、文件候选。"""
    parsed = ParsedMessage()
    if isinstance(message, dict):
        parsed.message_id = str(message.get("message_id") or "").strip()
        parsed.timestamp = str(message.get("timestamp") or "").strip()
        segments = (
            message.get("raw_message")
            or message.get("message_segments")
            or message.get("segments")
            or []
        )
    else:
        parsed.message_id = str(getattr(message, "message_id", "") or "").strip()
        segments = getattr(message, "raw_message", None) or []

    texts: list[str] = []

    def visit(node: dict) -> None:
        seg_type = str(node.get("type") or "").strip().lower()
        data = node.get("data")
        if seg_type == "text" and isinstance(data, dict) and data.get("text"):
            texts.append(str(data["text"]))
            return
        if seg_type in ("image", "emoji"):
            b64, url = normalize_image_segment(node)
            if b64:
                import base64 as _b64
                import binascii

                try:
                    parsed.images.append((_b64.b64decode("".join(b64.split())), ".png"))
                except (binascii.Error, ValueError):
                    pass
            elif url:
                parsed.images.append((b"", ".url:" + url))
            return
        if seg_type == "forward":
            _walk(data.get("messages") if isinstance(data, dict) else data, visit)

    _walk(segments, visit)
    plain = ""
    if isinstance(message, dict):
        plain = str(message.get("processed_plain_text") or "").strip()
    else:
        plain = str(getattr(message, "processed_plain_text", "") or "").strip()
    body = (plain or " ".join(texts)).strip()
    body = re.sub(r"\n?\[\d{2}:\d{2}:\d{2}\]\n?", "\n", body)  # 转发消息自带时间戳行
    parsed.text = body[:MAX_RAW_CHARS]

    urls = set(re.findall(r"https?://[^\s<>\"']+", body))
    parsed.links = [u.rstrip(".,);") for u in urls][:5]
    parsed.files = extract_file_candidates(message)
    return parsed


class InboxDeduper:
    """按原文 hash 去重：同一窗口内重复消息只处理一次，重发可再触发。"""

    def __init__(self) -> None:
        self._seen: dict[str, datetime] = {}

    def should_skip(self, raw: str, *, fingerprint: str = "") -> bool:
        key = str(fingerprint or "") or hashlib.sha256(
            str(raw or "").encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        if not str(raw or "").strip() and not fingerprint:
            return False  # 空原文+无指纹（如纯图片解析失败）不去重，留排障线索
        now = datetime.now()
        recent = self._seen.get(key)
        if recent is not None and now - recent < DEDUP_WINDOW:
            return True
        self._seen[key] = now
        while len(self._seen) > DEDUP_KEEP:
            self._seen.pop(next(iter(self._seen)))
        return False

    def forget(self, fingerprint: str) -> None:
        """用户主动重做时清掉对应去重记录。"""
        if fingerprint:
            self._seen.pop(str(fingerprint), None)
