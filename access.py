"""会话身份识别与访问名单（白/黑名单）判定。

为什么需要单独一层：SDK 只保证命令/工具会拿到 ``stream_id``，群号与用户号
藏在 ``message`` 对象里，而且**不同适配器给的结构不一样**——有时是 dict
（RPC 序列化后），有时是带属性的对象（``chat_info.group_info.group_id``、
``user_info.user_id``）。所以这里统一做「多路径防御式提取」，取不到就退回
用 ``stream_id`` 作标识，绝不因为拿不到群号就崩掉或误判。

名单匹配的原则：一个会话身上的**任一**标识命中名单条目即算命中，
因此群号、用户号、会话 ID 三种写法都能用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ACCESS_MODES",
    "CHAT_SCOPES",
    "AccessDecision",
    "ChatIdentity",
    "evaluate_access",
    "evaluate_chat_scope",
    "extract_session_id",
    "identity_from_kwargs",
    "normalize_entry",
    "parse_platform_target",
]

#: 名单模式：off 不限制 / whitelist 仅允许名单内 / blacklist 禁止名单内
ACCESS_MODES = ("off", "whitelist", "blacklist")

#: 适用范围：private 只走私聊 / group 只走群聊 / both 都行
CHAT_SCOPES = ("private", "group", "both")

# 群号候选路径（按可信度排序，取第一个拿到的非空值）
#
# `message_info.*` 是麦麦主机真实下发的形状（见
# src/plugin_runtime/host/message_utils.py 的 _session_message_to_dict /
# _message_info_to_dict）；其余几条是旧版与其它适配器的历史写法，留作兼容。
_GROUP_ID_PATHS = (
    "message_info.group_info.group_id",
    "chat_info.group_info.group_id",
    "group_info.group_id",
    "message_base_info.chat_info.group_info.group_id",
    "message_base_info.group_info.group_id",
    "message_base_info.group_id",
    "group_id",
)
_GROUP_NAME_PATHS = (
    "message_info.group_info.group_name",
    "chat_info.group_info.group_name",
    "group_info.group_name",
    "message_base_info.chat_info.group_info.group_name",
    "message_base_info.group_info.group_name",
    "message_base_info.group_name",
    "group_name",
)
_USER_ID_PATHS = (
    "message_info.user_info.user_id",
    "user_info.user_id",
    "message_base_info.user_info.user_id",
    "message_base_info.user_id",
    "sender.user_id",
    "user_id",
)
# 群名片优先于昵称：群里显示的是名片
_USER_NAME_PATHS = (
    "message_info.user_info.user_cardname",
    "user_info.user_cardname",
    "message_info.user_info.user_nickname",
    "user_info.user_nickname",
    "user_info.user_name",
    "message_base_info.user_info.user_nickname",
    "message_base_info.user_nickname",
    "user_nickname",
    "user_name",
)
_IS_GROUP_PATHS = (
    "is_group",
    "is_group_message",
    "message_base_info.is_group",
)
_STREAM_ID_PATHS = ("session_id", "stream_id", "message_base_info.stream_id")
#: 这些路径存在（不论内容）即说明是群聊消息
_GROUP_INFO_PRESENT_PATHS = (
    "message_info.group_info",
    "chat_info.group_info",
    "group_info",
)


def _dig_raw(obj: Any, path: str) -> Any:
    """按点分路径取原始值（可能是 dict/对象），取不到返回 ``None``。"""
    current = obj
    for key in path.split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
    return current


def _dig(obj: Any, path: str) -> str:
    """按点分路径取值并转成字符串；取不到或不是标量时返回空串。"""
    current = _dig_raw(obj, path)
    if current is None or isinstance(current, (dict, list, tuple, set)):
        return ""
    return str(current).strip()


def _present(obj: Any, paths: tuple[str, ...]) -> bool:
    """路径对应字段是否存在且非 None（允许是空字典——只看"有没有这一节"）。"""
    return any(_dig_raw(obj, path) is not None for path in paths)


def _first(obj: Any, paths: tuple[str, ...]) -> str:
    """返回第一个取到的非空值。"""
    for path in paths:
        value = _dig(obj, path)
        if value:
            return value
    return ""


@dataclass(frozen=True)
class ChatIdentity:
    """一个会话的可识别信息。"""

    stream_id: str = ""
    group_id: str = ""
    user_id: str = ""
    chat_type: str = ""
    label: str = ""

    @property
    def identifiers(self) -> list[str]:
        """可用于名单匹配的标识集合（去重且保序）。"""
        result: list[str] = []
        for value in (self.group_id, self.user_id, self.stream_id):
            value = str(value or "").strip()
            if value and value not in result:
                result.append(value)
        return result

    @property
    def display(self) -> str:
        """展示用描述，例如 ``群「高数三班」(123456)``。"""
        if self.label and self.group_id:
            return f"群「{self.label}」({self.group_id})"
        if self.label and self.user_id:
            return f"私聊「{self.label}」({self.user_id})"
        if self.group_id:
            return f"群 {self.group_id}"
        if self.user_id:
            return f"私聊 {self.user_id}"
        if self.stream_id:
            return f"会话 {self.stream_id}"
        return "未知会话"


def _build_identity(message: Any, stream_id: str) -> ChatIdentity:
    """从消息对象与 stream_id 组装身份。"""
    group_id = _first(message, _GROUP_ID_PATHS)
    user_id = _first(message, _USER_ID_PATHS)
    label = _first(message, _GROUP_NAME_PATHS) or _first(message, _USER_NAME_PATHS)

    is_group_flag = _first(message, _IS_GROUP_PATHS)
    if group_id:
        chat_type = "group"
    elif is_group_flag and is_group_flag.lower() in ("true", "1", "yes"):
        chat_type = "group"
    # 有些适配器带 group_info 节但不给 group_id：只要这一节存在就算群聊
    elif _present(message, _GROUP_INFO_PRESENT_PATHS):
        chat_type = "group"
    elif user_id:
        chat_type = "private"
    else:
        chat_type = ""

    # message 里自带 session_id 时优先用它（麦麦的 message 字典就是这么给的）
    resolved_stream = _first(message, _STREAM_ID_PATHS) or str(stream_id or "").strip()

    return ChatIdentity(
        stream_id=resolved_stream,
        group_id=group_id,
        user_id=user_id,
        chat_type=chat_type,
        label=label,
    )


def identity_from_kwargs(kwargs: dict[str, Any] | None) -> ChatIdentity:
    """从命令/Tool 的 ``kwargs`` 里提取会话身份。

    依次尝试 ``message`` / ``raw_message`` / ``event`` / kwargs 自身的顶层字段
    （部分适配器把 ``group_id``、``user_id`` 直接摊在参数顶层）。

    取舍：只要某个候选能给出**真正的群号或用户号**就立刻采用；否则记下第一个
    能给出 ``stream_id`` 的结果继续往后找，避免"第一个候选结构陌生就直接放弃
    回退"导致群号明明拿得到却用不上。
    """
    data = kwargs or {}
    stream_id = str(data.get("stream_id") or "")

    fallback: ChatIdentity | None = None
    for key in ("message", "raw_message", "event"):
        candidate = data.get(key)
        if candidate is None:
            continue
        identity = _build_identity(candidate, stream_id)
        if identity.group_id or identity.user_id:
            return identity
        if fallback is None:
            fallback = identity

    # kwargs 顶层字段（兼容旧式适配器把 group_id/user_id 直接摊平下发）
    top_level = _build_identity(data, stream_id)
    if top_level.group_id or top_level.user_id:
        return top_level
    if fallback is None:
        fallback = top_level

    return fallback or ChatIdentity(stream_id=stream_id)


def extract_session_id(stream: Any) -> str:
    """从 ``chat.get_stream_by_user_id`` 之类接口的返回值里取会话 ID。

    MaiBot 返回的是 dict（钥匙可能是 ``session_id`` 或 ``stream_id``），
    但不同版本/实现也可能是对象，所以两种都兼容——取错会静默发不出去。
    """
    if not stream:
        return ""
    if isinstance(stream, dict):
        return str(stream.get("session_id") or stream.get("stream_id") or "").strip()
    return str(
        getattr(stream, "session_id", "") or getattr(stream, "stream_id", "") or ""
    ).strip()


def parse_platform_target(target: Any, default_platform: str = "qq") -> tuple[str, str]:
    """把 ``platform:id`` 或裸 ID 拆成 ``(平台, 裸 ID)``。

    与名单条目同一套写法：``qq:123456`` 与 ``123456`` 等价。
    """
    text = str(target or "").strip()
    if not text:
        return str(default_platform or "qq"), ""
    if ":" in text:
        platform, _, bare = text.partition(":")
        return (str(platform).strip() or str(default_platform or "qq")), bare.strip()
    return str(default_platform or "qq"), text


def normalize_entry(entry: Any) -> list[str]:
    """把名单条目归一化成可匹配的候选集合。

    支持 ``123456``、``qq:123456``、``telegram:abc`` 三种写法：
    带平台前缀的条目同时用「原样」和「去掉前缀后的裸 ID」参与匹配。
    """
    text = str(entry or "").strip()
    if not text:
        return []
    candidates = [text]
    if ":" in text:
        _, _, bare = text.partition(":")
        bare = bare.strip()
        if bare:
            candidates.append(bare)
    return candidates


@dataclass(frozen=True)
class AccessDecision:
    """一次准入判定的结果。"""

    allowed: bool
    reason: str = ""
    matched: str = ""
    identifiers: list[str] = field(default_factory=list)

    @property
    def denied(self) -> bool:
        """是否被拒绝。"""
        return not self.allowed


def evaluate_access(
    identity: ChatIdentity,
    *,
    mode: str,
    entries: list[str] | tuple[str, ...] | None,
) -> AccessDecision:
    """按名单模式判定该会话是否被允许。

    - ``off``：全部允许；
    - ``whitelist``（允许名单）：仅名单内的会话通过，**默认拒绝**。
      认不出会话时也拒绝——课表属个人隐私，宁可少答一次也不要泄漏；
      开了白名单却没填内容等于谁都不许用，这是刻意的失败关闭；
    - ``blacklist``（拒绝名单）：名单内的会话被拒绝，**默认放行**。
      认不出会话时放行，因为黑名单是"点名禁止"：用户当初只能按群号、
      用户号或会话 ID 点名，认不出来的会话不可能是他要禁的那个。
    - 匹配是**大小写敏感**的精确比较（会话 ID 可能区分大小写，放宽会变成
      越权入口）；条目两侧空格会被去掉。

    重要提醒：**只有拿到了对应的标识，名单条目才可能命中**。如果适配器不下发
    群号/用户号，会话身上就只剩 ``stream_id``，此时用群号写的条目永远匹配不上
    （黑名单会静默失效）。所以插件启动时会检查这种情况并告警，
    ``/课表状态`` 也会打印本会话的全部可用标识供照抄。
    """
    normalized_mode = str(mode or "off").strip().lower()
    if normalized_mode not in ACCESS_MODES:
        normalized_mode = "off"

    identifiers = identity.identifiers
    if normalized_mode == "off":
        return AccessDecision(True, identifiers=identifiers)

    hits: list[str] = []
    for entry in entries or []:
        candidates = normalize_entry(entry)
        if not candidates:
            continue
        if any(ident in candidates for ident in identifiers):
            hits.append(str(entry).strip())

    if normalized_mode == "whitelist":
        if hits:
            return AccessDecision(True, matched=hits[0], identifiers=identifiers)
        reason = (
            "无法识别当前会话（消息里没有会话 ID）"
            if not identifiers
            else "当前会话不在白名单内"
        )
        return AccessDecision(False, reason=reason, identifiers=identifiers)

    # blacklist：命中才拒绝，否则放行
    if hits:
        return AccessDecision(
            False,
            reason=f"当前会话在黑名单内（命中 {hits[0]}）",
            matched=hits[0],
            identifiers=identifiers,
        )
    return AccessDecision(True, identifiers=identifiers)


def evaluate_chat_scope(
    identity: ChatIdentity,
    *,
    scope: str,
    unknown_allows: bool,
) -> AccessDecision:
    """按会话类型（私聊/群聊）判定是否在插件的适用范围内。

    与白/黑名单是**两个独立的维度**：名单管"是谁"，这里管"在什么类型的会话里"。
    课表是个人日程，发到群里等于公开，所以默认只走私聊。

    ``unknown_allows`` 决定认不出类型时怎么办，两个方向各有各的道理，
    所以由调用方按用途指定，而不是在这里一刀切：

    - **入站**（命令、文件导入、课表注入）传 ``False``（失败关闭）：认不出类型
      就把个人课表带进一个来源不明的会话，比拒绝一次更糟；
    - **出站**（提醒投递）传 ``True``（失败放行）：用户已经把某个会话登记成
      提醒对象了，因为认不出类型就**静默停掉提醒**是本插件最坏的失败方式
      （漏提醒没有任何人会发现），而且认不出的通常是用户自己在配置里钉死的
      裸会话 ID，那是明确的主动选择。

    ``scope`` 取 ``both`` 时不做任何限制（包括不校验类型）。
    """
    normalized = str(scope or "").strip().lower()
    if normalized not in CHAT_SCOPES or normalized == "both":
        return AccessDecision(True, identifiers=identity.identifiers)

    chat_type = str(identity.chat_type or "").strip().lower()
    if chat_type == normalized:
        return AccessDecision(True, identifiers=identity.identifiers)
    if chat_type in ("private", "group"):
        wanted = "私聊" if normalized == "private" else "群聊"
        return AccessDecision(
            False,
            reason=f"这个功能只在{wanted}里可用（当前是"
            f"{'群聊' if chat_type == 'group' else '私聊'}）",
            identifiers=identity.identifiers,
        )
    if unknown_allows:
        return AccessDecision(True, identifiers=identity.identifiers)
    wanted = "私聊" if normalized == "private" else "群聊"
    return AccessDecision(
        False,
        reason=f"无法识别当前会话是不是{wanted}",
        identifiers=identity.identifiers,
    )
