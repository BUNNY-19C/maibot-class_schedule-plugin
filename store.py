"""插件状态的持久化。

只存「随用户操作变化」的部分，课表本身永远从 ics 文件重新解析，
避免文件与缓存两份数据不一致：

- ``subscriptions``：提醒要发往哪些会话。每条记录除了 ``stream_id`` 还保存
  群号/用户号与群名昵称，用于访问名单匹配、状态展示，以及**该会话专属的
  提前量**（``lead_minutes`` 为 ``None`` 表示跟随配置默认值）；
- ``fired``：已提醒记录（``课程UID|开始时间|提前分钟数`` → 提醒时间），
  用于重启后不重复提醒。

提前量放在订阅记录上是刻意的：它天然是「按会话」的，且会话退订时
对应的提前量设置一并消失，不会留下无法清理的孤儿配置。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .access import ChatIdentity
from .constants import LOG_PREFIX

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_LEAD_MINUTES",
    "STATE_VERSION",
    "PluginState",
    "Subscription",
]

#: 状态文件结构版本。v1 = 裸 stream_id 列表 + 全局提前量；v2 = 带身份的订阅记录
STATE_VERSION = 2
FIRED_KEEP_DAYS = 3
FIRED_MAX_ENTRIES = 5000
#: 提前分钟数上限，与配置模型（le=1440）和 /课表提前 命令保持一致
MAX_LEAD_MINUTES = 1440


def _clean_lead(value: Any) -> int | None:
    """校验提前量：非 bool 的 0..MAX 整数，其余一律视为"未设置"。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 <= value <= MAX_LEAD_MINUTES:
        return None
    return value


@dataclass
class Subscription:
    """一个提醒对象（群聊或私聊会话）。"""

    stream_id: str
    chat_type: str = ""
    group_id: str = ""
    user_id: str = ""
    label: str = ""
    #: 该会话专属的提前分钟数；``None`` 表示用配置里的默认值
    lead_minutes: int | None = None
    added_at: str = ""

    # ── 展示 ──────────────────────────────────────────────

    @property
    def identity(self) -> ChatIdentity:
        """转成访问名单判定用的会话身份。"""
        return ChatIdentity(
            stream_id=self.stream_id,
            group_id=self.group_id,
            user_id=self.user_id,
            chat_type=self.chat_type,
            label=self.label,
        )

    @property
    def display(self) -> str:
        """展示用描述（含类型与名称）。"""
        return self.identity.display

    @property
    def type_label(self) -> str:
        """中文类型名，仅在已知时返回。"""
        if self.chat_type == "group":
            return "群聊"
        if self.chat_type == "private":
            return "私聊"
        return "会话"

    def effective_lead(self, default_minutes: int) -> int:
        """该会话实际生效的提前分钟数。"""
        if self.lead_minutes is not None:
            return self.lead_minutes
        return max(0, int(default_minutes))

    def lead_display(self, default_minutes: int) -> str:
        """提前量说明，标明是会话设置还是配置默认。"""
        if self.lead_minutes is not None:
            return f"{self.lead_minutes} 分钟（本会话单独设置）"
        return f"{self.effective_lead(default_minutes)} 分钟（跟随配置）"

    # ── 序列化 ────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典。"""
        return {
            "stream_id": self.stream_id,
            "chat_type": self.chat_type,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "label": self.label,
            "lead_minutes": self.lead_minutes,
            "added_at": self.added_at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Subscription | None:
        """从字典还原；``stream_id`` 缺失时返回 ``None``（该条丢弃）。"""
        if not isinstance(raw, dict):
            return None
        stream_id = str(raw.get("stream_id") or "").strip()
        if not stream_id:
            return None

        def text(key: str) -> str:
            value = raw.get(key)
            return str(value).strip() if value is not None else ""

        chat_type = text("chat_type")
        if chat_type not in ("group", "private"):
            chat_type = ""

        return cls(
            stream_id=stream_id,
            chat_type=chat_type,
            group_id=text("group_id"),
            user_id=text("user_id"),
            label=text("label"),
            lead_minutes=_clean_lead(raw.get("lead_minutes")),
            added_at=text("added_at"),
        )


@dataclass
class PluginState:
    """需要跨重启保留的插件状态。"""

    subscriptions: list[Subscription] = field(default_factory=list)
    fired: dict[str, str] = field(default_factory=dict)
    #: 按会话记录的送达：提醒键 → {会话ID → 送达时间}。
    #: 一节课发给多个会话时，部分失败不能再整节标记"已提醒"——那会让失败的
    #: 会话永远等不到补发（线上取舍过，用户指出这是漏提醒）。
    fired_sessions: dict[str, dict[str, str]] = field(default_factory=dict)
    #: 已生成的课后总结："<课次key>" -> "<ISO时间>"（key 形如 202609170800:uid）
    summaries: dict[str, str] = field(default_factory=dict)

    # ── 读写 ──────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> PluginState:
        """从磁盘读取状态；文件缺失或损坏时返回空状态（不抛异常）。"""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            # 文件存在却读不出来（截断、手工编辑写坏等）必须留痕：
            # 静默归零会让用户只看到"没有提醒对象"，排查不到根因
            logger.warning("%s 状态文件读取失败，已按空状态启动：%s", LOG_PREFIX, exc)
            return cls()
        if not isinstance(raw, dict):
            logger.warning("%s 状态文件结构异常（非对象），已按空状态启动", LOG_PREFIX)
            return cls()

        subscriptions = cls._load_subscriptions(raw)
        fired = cls._load_fired(raw.get("fired"))
        fired_sessions = cls._load_fired_sessions(raw.get("fired_sessions"))
        summaries = cls._load_summaries(raw.get("summaries"))
        return cls(
            subscriptions=subscriptions,
            fired=fired,
            fired_sessions=fired_sessions,
            summaries=summaries,
        )

    @staticmethod
    def _load_fired(raw: Any) -> dict[str, str]:
        """读取已提醒记录。"""
        fired: dict[str, str] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(key, str) and isinstance(value, str):
                    fired[key] = value
        return fired

    @staticmethod
    def _load_fired_sessions(raw: Any) -> dict[str, dict[str, str]]:
        """读取按会话记录的送达；结构异常的条目整条跳过。"""
        result: dict[str, dict[str, str]] = {}
        if not isinstance(raw, dict):
            return result
        for key, sessions in raw.items():
            if not isinstance(key, str) or not isinstance(sessions, dict):
                continue
            per: dict[str, str] = {}
            for stream_id, moment in sessions.items():
                if isinstance(stream_id, str) and stream_id and isinstance(moment, str):
                    per[stream_id] = moment
            if per:
                result[key] = per
        return result

    @staticmethod
    def _load_summaries(raw: Any) -> dict[str, str]:
        """读取已生成总结的记录；结构异常的条目跳过。"""
        result: dict[str, str] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(key, str) and key and isinstance(value, str):
                    result[key] = value
        return result

    @classmethod
    def _load_subscriptions(cls, raw: dict[str, Any]) -> list[Subscription]:
        """读取订阅列表，并把 v1 结构迁移过来。"""
        items = raw.get("subscriptions")
        if isinstance(items, list):
            result: list[Subscription] = []
            for item in items:
                subscription = Subscription.from_dict(item)
                if subscription is None:
                    continue
                if subscription.stream_id in {s.stream_id for s in result}:
                    continue
                result.append(subscription)
            return result

        # ── v1 迁移：裸 stream_id 列表 + 一个全局提前量 ──
        legacy_targets = raw.get("targets")
        if not isinstance(legacy_targets, list):
            # 键**存在**却不是列表（手改/外部工具写坏，如 null）才告警：
            # 键不存在是正常的 v1 结构或首次启动，不该刷日志
            if "subscriptions" in raw:
                logger.warning(
                    "%s 状态文件的 subscriptions 字段不是列表（%s），已忽略；"
                    "该字段里原有的订阅不会恢复",
                    LOG_PREFIX,
                    type(items).__name__,
                )
            return []

        # 旧的全局提前量体现了用户意图，迁移时平摊给已有订阅，
        # 之后每个会话就能各自调整了
        legacy_lead = _clean_lead(raw.get("remind_before_minutes"))

        migrated: list[Subscription] = []
        for item in legacy_targets:
            if item is None or isinstance(item, bool):
                continue
            stream_id = str(item).strip()
            if not stream_id or stream_id in {s.stream_id for s in migrated}:
                continue
            migrated.append(Subscription(stream_id=stream_id, lead_minutes=legacy_lead))

        if migrated:
            logger.info(
                "%s 已把 %d 个旧版提醒对象迁移到新结构%s",
                LOG_PREFIX,
                len(migrated),
                f"（沿用原全局提前量 {legacy_lead} 分钟）" if legacy_lead is not None else "",
            )
        return migrated

    def save(self, path: Path) -> None:
        """原子写入磁盘（先写临时文件再替换，避免中途崩溃损坏）。"""
        payload: dict[str, Any] = {
            "version": STATE_VERSION,
            "subscriptions": [item.to_dict() for item in self.subscriptions],
            "fired": self.fired,
            "fired_sessions": self.fired_sessions,
            "summaries": self.summaries,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ── 订阅 ──────────────────────────────────────────────

    def find(self, stream_id: str) -> Subscription | None:
        """按会话 ID 查找订阅记录。"""
        target = str(stream_id or "").strip()
        if not target:
            return None
        for item in self.subscriptions:
            if item.stream_id == target:
                return item
        return None

    def add_subscription(
        self,
        identity: ChatIdentity,
        *,
        now: datetime | None = None,
    ) -> tuple[Subscription, bool]:
        """登记提醒对象，返回 ``(记录, 是否新增)``。

        已存在时刷新身份信息（群名可能改过），但**不覆盖**用户设置的提前量。
        """
        stream_id = str(identity.stream_id or "").strip()
        if not stream_id:
            raise ValueError("stream_id 不能为空")

        existing = self.find(stream_id)
        if existing is not None:
            existing.chat_type = identity.chat_type or existing.chat_type
            existing.group_id = identity.group_id or existing.group_id
            existing.user_id = identity.user_id or existing.user_id
            existing.label = identity.label or existing.label
            return existing, False

        record = Subscription(
            stream_id=stream_id,
            chat_type=identity.chat_type,
            group_id=identity.group_id,
            user_id=identity.user_id,
            label=identity.label,
            added_at=(now or datetime.now()).isoformat(timespec="seconds"),
        )
        self.subscriptions.append(record)
        return record, True

    def remove_subscription(self, stream_id: str) -> bool:
        """移除订阅（连带该会话的专属提前量），返回是否存在过。"""
        record = self.find(stream_id)
        if record is None:
            return False
        self.subscriptions.remove(record)
        return True

    def set_lead(self, stream_id: str, minutes: int | None) -> bool:
        """设置某会话的专属提前量；``None`` 表示恢复跟随配置。"""
        record = self.find(stream_id)
        if record is None:
            return False
        record.lead_minutes = _clean_lead(minutes) if minutes is not None else None
        return True

    # ── 已提醒记录 ────────────────────────────────────────

    def mark_summarized(self, key: str, moment: datetime) -> None:
        """标记某节课的总结已生成（无论成败，避免重复生成）。"""
        self.summaries[key] = moment.isoformat(timespec="seconds")

    def was_summarized(self, key: str) -> bool:
        return key in self.summaries

    def mark_fired(self, key: str, moment: datetime) -> None:
        """记录一次已提醒（全部目标会话都送达后调用）。"""
        self.fired[key] = moment.isoformat()

    def mark_session_delivered(self, key: str, stream_id: str, moment: datetime) -> None:
        """记录"这节课的提醒已送到这个会话"（部分送达时逐会话记录）。"""
        per = self.fired_sessions.setdefault(str(key), {})
        per[str(stream_id)] = moment.isoformat()

    def delivered_sessions(self, key: str) -> dict[str, str]:
        """这节课已经成功送达过的会话 → 送达时间；没记录过返回空表。"""
        return self.fired_sessions.get(str(key), {})

    def prune_fired(self, now: datetime, keep_days: int = FIRED_KEEP_DAYS) -> int:
        """清理过期记录，返回清理条数。

        记录里的时间既用于过期判断，也在超量时作为淘汰依据。
        """
        cutoff = now - timedelta(days=keep_days)
        removed = 0
        for key, value in list(self.fired.items()):
            try:
                moment = datetime.fromisoformat(value)
            except (ValueError, TypeError):
                del self.fired[key]
                removed += 1
                continue
            if moment < cutoff:
                del self.fired[key]
                removed += 1

        for key, sessions in list(self.fired_sessions.items()):
            if not sessions:
                del self.fired_sessions[key]
                removed += 1
                continue
            try:
                latest = max(datetime.fromisoformat(v) for v in sessions.values())
            except (ValueError, TypeError):
                del self.fired_sessions[key]
                removed += 1
                continue
            if latest < cutoff:
                del self.fired_sessions[key]
                removed += 1

        if len(self.fired) > FIRED_MAX_ENTRIES:
            ordered = sorted(self.fired.items(), key=lambda item: item[1])
            for key, _ in ordered[: len(self.fired) - FIRED_MAX_ENTRIES]:
                del self.fired[key]
                removed += 1
        return removed
