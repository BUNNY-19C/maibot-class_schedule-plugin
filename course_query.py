"""自然语言问课：相关性判定与注入文案生成。

两条路径共用这里的格式化：

1. **规划器注入**（主路径）：在 Maisaka 向模型发起规划请求前，把课表作为一条
   system 上下文塞进去，模型就能用人设自然回答「明天几点有课」这类问题，
   不依赖它是否决定调用工具；
2. **LLM 工具**（备用）：显式查询。

注入不能无脑做——每条消息都多几百 token 既费钱又可能让模型跑题，所以默认
只在**聊到课表相关话题时**才注入（关键词判定，纯字符串匹配，毫秒级；
``maisaka.planner.before_request`` 的默认超时只有 6 秒，这里不能调 LLM）。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from uuid import uuid4
from typing import Callable, Iterable, Mapping

__all__ = [
    "DEFAULT_MAX_LINES",
    "build_inject_text",
    "build_system_item",
    "collect_item_text",
    "inject_into_items",
    "looks_like_schedule_question",
]

DEFAULT_MAX_LINES = 12

#: 只在出现这些词时才认为在聊课表。刻意**不含**「今天/明天/几点」这类通用词——
#: 它们几乎每条消息都可能出现，会把注入变成"每条都注入"
_STRONG_KEYWORDS = (
    "课表",
    "课程",
    "上课",
    "下课",
    "早课",
    "晚课",
    "第几节",
    "几节课",
    "有课",
    "没课",
    "什么课",
    "教室",
    "点名",
    "签到",
    "自习",
    "考试",
    "老师",
    "挂科",
    "调课",
    "补课",
    "放假",
    "调休",
)

#: 「第 3 节」「第3-4节」这类节次说法
_SESSION_RE = re.compile(r"第\s*\d+\s*(?:-\s*\d+\s*)?节")

_WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def looks_like_schedule_question(text: str) -> bool:
    """文本是否像在聊课表；用于决定要不要注入课表上下文。"""
    content = str(text or "")
    if not content:
        return False
    if any(keyword in content for keyword in _STRONG_KEYWORDS):
        return True
    return _SESSION_RE.search(content) is not None


def collect_item_text(items: Iterable[object], *, limit: int = 6, max_chars: int = 600) -> str:
    """从规划器的 Context Items 里取出最近几条的文本，用于相关性判定。

    只做宽松扫描：任何 dict 里的 ``text`` 字段都算，取不到就跳过——
    这里的目标是"别漏判"，不是精确解析结构。
    """
    texts: list[str] = []
    for item in list(items)[-int(limit) :][::-1]:
        if not isinstance(item, Mapping):
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            value = part.get("text")
            if isinstance(value, str) and value.strip():
                texts.append(value.strip())
    joined = " ".join(texts)
    return joined[-max_chars:] if len(joined) > max_chars else joined


def build_inject_text(
    events: Iterable[object],
    *,
    now: datetime,
    days: int = 3,
    max_lines: int = DEFAULT_MAX_LINES,
    holiday_label: Callable[[date], str] | None = None,
    expander: Callable[[object, datetime, datetime], list[object]] | None = None,
) -> str:
    """把未来若干天的课表渲染成一段紧凑的注入文本；没有课返回空串。

    Args:
        events: 已解析的课程事件。
        now: 当前本地时间。
        days: 往后看几天。
        max_lines: 最多列几行课（控制 token）。
        holiday_label: 给定日期返回放假说明（如 ``中秋节放假``），无则空串。
        expander: 展开重复规则的回调，签名 ``(event, start, end) -> [occurrence]``。
            生产代码必须传（否则带 RRULE 的课会被跳过）；只有直接喂
            "已经是一次性事件"的测试才省略。
    """
    if days <= 0:
        return ""

    horizon_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    horizon_end = horizon_start + timedelta(days=int(days))

    occurrences: list[object] = []
    for event in events:
        if getattr(event, "all_day", False):
            continue
        if expander is not None:
            occurrences.extend(expander(event, horizon_start, horizon_end))
        elif not getattr(event, "rrule", ""):
            # 没传 expander 时只能照搬 start，而带 RRULE 的事件的 start 是**首次**课
            # 的时间：照搬会印出早已过去的日期，随后又被下面的"已上完"过滤掉，
            # 结果整门重复课静默消失。宁可少列，也不要给模型一个错的日期。
            occurrences.append(event)
    occurrences.sort(key=lambda item: item.start)  # type: ignore[attr-defined]

    lines: list[str] = []
    truncated = False
    seen_dates: set[date] = set()
    for occurrence in occurrences:
        if len(lines) >= max_lines:
            truncated = True
            break
        start = occurrence.start  # type: ignore[attr-defined]
        # 已经上完的课不再列（回答"接下来有什么课"时更准）
        if getattr(occurrence, "end", None) is not None and occurrence.end < now:  # type: ignore[attr-defined]
            continue
        elif getattr(occurrence, "end", None) is None and start < now:
            continue
        if start.date() not in seen_dates:
            seen_dates.add(start.date())
            offset = (start.date() - horizon_start.date()).days
            label = "今天" if offset == 0 else ("明天" if offset == 1 else _WEEKDAY_LABELS[start.weekday()])
            holiday = holiday_label(start.date()) if holiday_label else ""
            header = f"{start.strftime('%m-%d')} {label}"
            if holiday:
                header += f"（{holiday}）"
        else:
            header = ""
        time_part = start.strftime("%H:%M")
        end = getattr(occurrence, "end", None)
        if end is not None and end.date() == start.date():
            time_part += f"-{end.strftime('%H:%M')}"
        location = str(getattr(occurrence, "location", "") or "").strip()
        name = str(getattr(occurrence, "display_name", "") or "").strip()
        line = f"- {header + ' ' if header else ''}{time_part} {name}"
        if location:
            line += f" @{location}"
        lines.append(line)

    if not lines:
        # 未来几天确实没课也要说明，否则模型可能编
        return (
            f"【课表】当前 {now.strftime('%Y-%m-%d %H:%M')}，"
            f"未来 {days} 天没有排课。用户若问上课安排，如实说没有课，不要编造。"
        )

    text = [
        f"【课表】当前 {now.strftime('%Y-%m-%d %H:%M')} {_WEEKDAY_LABELS[now.weekday()]}，"
        f"未来 {days} 天的课：",
        *lines,
    ]
    if truncated:
        text.append(f"（仅列出前 {max_lines} 条）")
    text.append("（以上是真实课表数据，回答与上课相关的问题时按此作答；没列出的时间段就是没课，不要编造）")
    return "\n".join(text)


def build_system_item(text: str, *, now: datetime | None = None) -> dict:
    """构造规划器能识别的 SystemMessageItem。

    结构取自麦麦 1.2 的 Context Item 约定（``item_type`` + ``meta`` + ``parts``），
    与社区插件 mai-life 的写法一致。
    """
    stamp = now or datetime.now()
    return {
        "item_type": "SystemMessageItem",
        "meta": {
            "item_id": uuid4().hex,
            "logical_turn_id": None,
            "timestamp": stamp.isoformat(),
        },
        "parts": [{"type": "text", "text": str(text)}],
    }


def inject_into_items(
    items: Iterable[object], text: str, *, now: datetime | None = None
) -> list:
    """把注入文本作为一条 system item 插到最后一条 system item 之后。

    插在 system 段末尾而不是开头：既靠近对话（模型更容易采信），
    又不会打乱已有的 system 提示顺序。
    """
    content = str(text or "").strip()
    if not content:
        return list(items)
    inserted = build_system_item(content, now=now)
    result = list(items)
    last_system = -1
    for index, item in enumerate(result):
        if isinstance(item, Mapping) and str(item.get("item_type") or "") == "SystemMessageItem":
            last_system = index
    if last_system >= 0:
        result.insert(last_system + 1, inserted)
    else:
        result.insert(0, inserted)
    return result
