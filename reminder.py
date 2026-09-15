"""提醒到期判定与消息渲染。

判定思路：每 ``check_interval_seconds`` 跑一次 tick，把「目标触发时刻」
（``开始时间 - 提前分钟数``）已到、但课程还没开始的课挑出来。

- 提前 ``lead`` 分钟：只要 ``距离上课 <= lead``，且尚未提醒过，就发；
  这样即使机器人中途重启/离线，恢复后仍能在课开始前补发一次；
- 宽限 ``late_grace_seconds``：课已经开始超过这个秒数就放弃补发，
  避免导入一份新课表后把已经开始甚至上完的课全部轰炸一遍；
- 去重键包含提前分钟数，因此改了提前量会按新提前量重新提醒一次。
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .ics_parser import CourseEvent, expand_occurrences

__all__ = [
    "DEFAULT_TEMPLATE",
    "MAX_FIELD_LENGTH",
    "DueReminder",
    "collect_due",
    "one_line",
    "render_message",
    "reminder_key",
]

#: 提醒文案默认模板，可用占位符见 README
DEFAULT_TEMPLATE = "⏰ {minutes_label}：{course}\n🕐 {time_range}{location_part}"

#: 来自 ICS 的文本长度上限。课表内容由第三方文件决定（还能从网址导入），
#: 过长的课程名会把提醒消息撑爆，统一截断
MAX_FIELD_LENGTH = 120

_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


def one_line(text: str, limit: int = MAX_FIELD_LENGTH) -> str:
    """把 ICS 里取来的文本压成单行并截断。"""
    collapsed = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(collapsed) > limit:
        return collapsed[: limit - 1] + "…"
    return collapsed


@dataclass
class DueReminder:
    """一次待发送的提醒。"""

    key: str
    event: CourseEvent
    start: datetime
    minutes_until: int

    @property
    def minutes_until_display(self) -> int:
        """展示用分钟数（不足一分钟按 0）。"""
        return max(0, self.minutes_until)


def reminder_key(event: CourseEvent, start: datetime, lead_minutes: int) -> str:
    """构造去重键：课程 + 实际开始时间 + 提前量。

    带上 ``uid`` 是为了让同名但同时段的不同课不会互相顶掉；
    ``uid`` 缺失（部分导出会省略）时退化为用课程名。
    """
    identity = event.uid or event.display_name
    return f"{identity}|{start.isoformat(timespec='minutes')}|{lead_minutes}"


def collect_due(
    events: list[CourseEvent],
    *,
    now: datetime,
    lead_minutes: int,
    late_grace_seconds: int,
    fired: set[str] | dict[str, str],
    is_off_day: Callable[[date], bool] | None = None,
) -> list[DueReminder]:
    """挑出本轮需要提醒的课，按开始时间排序。

    Args:
        events: 已解析（未展开）的课程事件。
        now: 当前本地时间。
        lead_minutes: 提前多少分钟提醒。
        late_grace_seconds: 课已开始多久之后放弃补发。
        fired: 已提醒键集合（``dict`` 或 ``set`` 均可）。
        is_off_day: 判定某日是否放假（法定节假日等）。返回 ``True`` 时该日的课
            不提醒。传 ``None`` 表示不做节假日判断。
    """
    lead_seconds = max(0, int(lead_minutes)) * 60
    grace_seconds = max(0, int(late_grace_seconds))
    window_end = now + timedelta(seconds=lead_seconds)
    window_start = now - timedelta(seconds=grace_seconds)

    due: list[DueReminder] = []
    seen: set[str] = set()
    for event in events:
        if event.all_day:
            continue
        for occurrence in expand_occurrences(event, window_start, window_end):
            delta = (occurrence.start - now).total_seconds()
            if delta > lead_seconds:
                continue
            if delta < -grace_seconds:
                continue
            # 法定节假日不上课，直接跳过（按"这次课所在的那一天"判定）
            if is_off_day is not None and is_off_day(occurrence.start.date()):
                continue
            key = reminder_key(occurrence, occurrence.start, lead_minutes)
            # 同一轮内也要去重：同一份课表被导入两次会产生两个 ics 文件，
            # 里面是同 UID 同时间的课，只落库去重的话会当场发两条一样的提醒
            if key in fired or key in seen:
                continue
            seen.add(key)
            due.append(
                DueReminder(
                    key=key,
                    event=occurrence,
                    start=occurrence.start,
                    # 向上取整：提前量设 20 分钟时，tick 落在 19 分 5 秒
                    # 也显示"20 分钟后"，与用户设置一致
                    minutes_until=math.ceil(delta / 60),
                )
            )
    due.sort(key=lambda item: item.start)
    return due


def render_message(reminder: DueReminder, template: str = "") -> str:
    """按模板渲染提醒文案。

    未知占位符原样保留，用户模板写错也不会导致整条消息发不出去。
    """
    event = reminder.event
    start = reminder.start
    minutes = reminder.minutes_until_display

    if minutes <= 0:
        minutes_label = "马上上课"
    else:
        minutes_label = f"{minutes} 分钟后上课"

    location = one_line(event.location)
    description = one_line(event.description)

    placeholders = {
        "minutes": str(minutes),
        "minutes_label": minutes_label,
        "course": one_line(event.display_name),
        "location": location,
        "location_part": f"\n📍 {location}" if location else "",
        "description": description,
        "description_part": f"\n📝 {description}" if description else "",
        "start": start.strftime("%H:%M"),
        "end": event.end.strftime("%H:%M") if event.end else "",
        "time_range": _format_time_range(event, start),
        "date": start.strftime("%Y-%m-%d"),
        "weekday": _WEEKDAYS[start.weekday()],
    }

    body = template.strip() or DEFAULT_TEMPLATE
    rendered = _PLACEHOLDER_RE.sub(
        lambda match: placeholders.get(match.group(1), match.group(0)), body
    )
    # 某个可选片段为空时可能留下空行，折叠掉
    rendered = re.sub(r"\n{2,}", "\n", rendered)
    return "\n".join(line.rstrip() for line in rendered.splitlines()).strip()


def _format_time_range(event: CourseEvent, start: datetime) -> str:
    """``08:00-09:40``；缺 DTEND 时只给开始时间。"""
    if event.end is None:
        return start.strftime("%H:%M")
    if event.end.date() == start.date():
        return f"{start.strftime('%H:%M')}-{event.end.strftime('%H:%M')}"
    return f"{start.strftime('%H:%M')} 起"


def upcoming_events(
    events: list[CourseEvent],
    *,
    start: datetime,
    end: datetime,
    limit: int = 0,
) -> list[CourseEvent]:
    """查询 ``[start, end]`` 区间内的课程，按时间排序（供查询命令/Tool 用）。"""
    result: list[CourseEvent] = []
    for event in events:
        if event.all_day:
            continue
        result.extend(expand_occurrences(event, start, end))
    result.sort(key=lambda item: item.start)
    if limit > 0:
        return result[:limit]
    return result
