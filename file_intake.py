"""聊天里发来的课表文件：识别、取内容、落盘。

麦麦把文件消息序列化成消息段，形如::

    {"type": "file", "data": {"name": "课表.ics", "url": "https://…", "file_id": "…"}}

但不同适配器会多包一层（实测 SnowLuma 会包成
``{"type": "dict", "data": {"type": "file", "data": {…}}}``），字段名也不统一
（``file`` / ``name`` / ``file_name``），所以这里递归找、逐个字段兜底。

取内容的优先级：

1. 段里自带 ``base64``（WebUI 本地消息会给）——直接用，不联网；
2. 段里的 ``url``——走 :func:`netutil.fetch_text`，**默认只允许公网地址**。

第 2 条的例外由使用者显式配置 ``file_import.allowed_hosts`` 点名（例如适配器
自己的本地文件服务 ``127.0.0.1:3000``）。这不是"允许内网"的通用开关：只有被点名的
``host`` 或 ``host:port`` 才会放行，协议与端口合法性照旧受检。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .netutil import FetchError, UnsafeUrlError, fetch_text

logger = logging.getLogger(__name__)

__all__ = [
    "FileCandidate",
    "FileIntakeError",
    "chat_import_filename",
    "extract_file_candidates",
    "is_schedule_filename",
    "load_candidate_text",
]

#: 识别课表文件的扩展名与 MIME
_ICS_SUFFIXES = (".ics", ".ical")
_ICS_MIMES = ("text/calendar", "application/ics", "text/x-vcalendar")

#: 消息段嵌套层数上限（适配器可能包一层 "dict"，别无限递归）
_MAX_SEGMENT_DEPTH = 4

_NAME_KEYS = ("name", "file", "file_name", "filename")
_SIZE_KEYS = ("size", "file_size")
_URL_KEYS = ("url", "file_url", "download_url")
_ID_KEYS = ("file_id", "id", "fid")
_BASE64_KEYS = ("base64", "base64_data", "data_base64")
#: 麦麦的 FileComponent 带 mime_type（见 message_component_data_model.py），
#: 有它就能认出"文件名不叫 .ics 但类型是日历"的文件
_MIME_KEYS = ("mime_type", "mime", "content_type")


class FileIntakeError(RuntimeError):
    """文件内容取不到（既无内嵌内容，也没有可下载地址）。"""


@dataclass(frozen=True)
class FileCandidate:
    """消息里发现的一个文件。"""

    name: str = ""
    size: int = 0
    url: str = ""
    file_id: str = ""
    base64_data: str = ""
    mime_type: str = ""
    message_id: str = ""

    @property
    def display(self) -> str:
        """日志/回执里用的描述。"""
        label = self.name or self.file_id or "未命名文件"
        return f"{label}（{self.size} 字节）" if self.size else label

    @property
    def has_content(self) -> bool:
        """是否已经有可用的内容来源。"""
        return bool(self.base64_data or self.url)


def is_schedule_filename(name: str, mime_type: str = "") -> bool:
    """文件名或 MIME 看起来是不是课表文件。"""
    lowered = str(mime_type or "").strip().lower()
    if any(lowered.startswith(item) for item in _ICS_MIMES):
        return True
    text = str(name or "").strip().lower()
    return text.endswith(_ICS_SUFFIXES)


def chat_import_filename(name: str) -> str:
    """由聊天文件生成落盘名：同名重发即更新，不堆垃圾。

    刻意**不带内容哈希**：用户重新导出同名课表再发一次，就应该替换掉旧的，
    否则调课后的旧时间还会继续提醒。
    """
    # 前缀 "chat-" 是安全边界：聊天导入的文件绝不能盖掉用户自己放进目录的文件。
    # 名字被清理成空时退回 "schedule"，而不是让前缀重复成 "chat-chat"
    stem = Path(str(name or "").strip()).stem
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", stem).strip("._")
    return f"chat-{cleaned or 'schedule'}.ics"


def _as_int(value: Any) -> int:
    """尽量把大小字段转成整数，转不了当 0。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _first_text(data: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    """从若干候选键里取第一个非空字符串。"""
    for key in keys:
        value = data.get(key)
        if value is None or isinstance(value, (dict, list, tuple, set)):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _candidate_from_payload(
    data: Mapping[str, Any], *, message_id: str = ""
) -> FileCandidate:
    """按 FileComponent 的字段约定构造候选。"""
    return FileCandidate(
        name=_first_text(data, _NAME_KEYS),
        size=_as_int(_first_text(data, _SIZE_KEYS)),
        url=_first_text(data, _URL_KEYS),
        file_id=_first_text(data, _ID_KEYS),
        base64_data=_first_text(data, _BASE64_KEYS),
        mime_type=_first_text(data, _MIME_KEYS),
        message_id=message_id,
    )


def _walk_segments(
    node: Any,
    found: list[FileCandidate],
    *,
    message_id: str,
    depth: int,
) -> None:
    """递归收集文件段，兼容外层多包一层的写法。"""
    if depth > _MAX_SEGMENT_DEPTH:
        return

    if isinstance(node, list):
        for item in node:
            _walk_segments(item, found, message_id=message_id, depth=depth + 1)
        return
    if not isinstance(node, Mapping):
        return

    seg_type = str(node.get("type") or "").strip().lower()
    data = node.get("data")

    if seg_type == "file":
        payload = data if isinstance(data, Mapping) else {"name": data}
        candidate = _candidate_from_payload(payload, message_id=message_id)
        if candidate.has_content or candidate.name:
            found.append(candidate)
        return

    # 外层包装（如 SnowLuma 的 {"type": "dict", "data": {"type": "file", ...}}）
    if seg_type in ("dict", "segment", "message", "") and isinstance(data, Mapping):
        _walk_segments(data, found, message_id=message_id, depth=depth + 1)
        return

    if isinstance(data, list):
        _walk_segments(data, found, message_id=message_id, depth=depth + 1)


def extract_file_candidates(message: Any) -> list[FileCandidate]:
    """从消息（命令 kwargs 的 ``message`` 字典，或消息对象）里挑出文件候选。

    兼容三种载体：``raw_message`` 段列表、``message_segments``、以及直接就是
    段列表的形态。找不到就返回空列表。
    """
    message_id = ""
    roots: list[Any] = []

    if isinstance(message, Mapping):
        message_id = str(message.get("message_id") or "").strip()
        for key in ("raw_message", "message_segments", "segments"):
            if key in message:
                roots.append(message.get(key))
    else:
        message_id = str(getattr(message, "message_id", "") or "").strip()
        for attr in ("raw_message", "message_segments", "segments"):
            value = getattr(message, attr, None)
            if value is not None:
                roots.append(value)

    found: list[FileCandidate] = []
    for root in roots:
        _walk_segments(root, found, message_id=message_id, depth=0)

    # 同一条消息里重复出现的同一文件只留一个。
    # 键里带上内容片段：适配器对多文件消息可能复用同一个文件名，
    # 只按 (name, url, file_id) 去重会把第二份课表静默丢掉（用户以为都导入了）
    unique: list[FileCandidate] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in found:
        key = (
            item.name,
            item.url,
            item.file_id,
            f"{len(item.base64_data)}:{item.base64_data[:32]}",
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _decode_base64(data: str, *, max_bytes: int) -> str:
    """解码内嵌的文件内容，顺带做大小上限保护。"""
    text = "".join(str(data or "").split())
    if not text:
        raise FileIntakeError("内嵌内容为空")
    # base64 每 4 个字符约 3 字节，先按长度粗判，避免解出个巨型字符串
    if len(text) // 4 * 3 > max_bytes:
        raise FileIntakeError(f"内嵌文件超过大小上限 {max_bytes // 1024} KB")
    try:
        raw = base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise FileIntakeError(f"内嵌内容不是合法 base64：{exc}") from exc
    if len(raw) > max_bytes:
        raise FileIntakeError(f"内嵌文件超过大小上限 {max_bytes // 1024} KB")
    from .ics_parser import decode_ics_bytes

    return decode_ics_bytes(raw)


async def load_candidate_text(
    candidate: FileCandidate,
    *,
    timeout: int,
    max_bytes: int,
    allow_hosts: Iterable[str] = (),
) -> tuple[str, str]:
    """取到文件文本，返回 ``(内容, 来源描述)``。

    先看内嵌 base64（不联网），再走 URL 下载。URL 下载默认只允许公网地址，
    本地/内网地址需要使用者用 ``allow_hosts`` 点名放行。
    """
    if candidate.base64_data:
        return _decode_base64(candidate.base64_data, max_bytes=max_bytes), "内嵌内容"

    if not candidate.url:
        raise FileIntakeError("这个文件消息里既没有内嵌内容，也没有下载地址")

    try:
        text = await fetch_text(
            candidate.url,
            timeout=timeout,
            max_bytes=max_bytes,
            allow_hosts=allow_hosts,
        )
    except UnsafeUrlError as exc:
        # 平台给的是本地地址时最容易走到这里，提示怎么处理
        raise FileIntakeError(
            f"下载地址不被允许（{exc}）。如果这是适配器自己的本地文件服务，"
            "请在配置 file_import.allowed_hosts 里点名放行该主机"
        ) from exc
    except FetchError as exc:
        raise FileIntakeError(f"下载失败：{exc}") from exc
    return text, "下载地址"


def content_fingerprint(text: str) -> str:
    """内容指纹，用于"同一份文件重复发"的去重。"""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
