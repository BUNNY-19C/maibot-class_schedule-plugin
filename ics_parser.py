"""ICS（iCalendar / RFC 5545）课表解析。

只依赖标准库 + ``python-dateutil``：

- 手写文本层解析（折行展开、属性参数、转义还原），这部分简单且可控；
- 重复规则（RRULE）交给 ``dateutil.rrule`` 展开，避免手写 RFC 5545 的
  BYDAY/BYSETPOS/INTERVAL 等组合时出错。

时间口径（有意为之的简化，见 README）：

- ``DTSTART;VALUE=DATE`` → 全天事件（不作为上课提醒对象）；
- 以 ``Z`` 结尾的时间视为 UTC，转换成系统本地时间；
- ``TZID=...`` 与无时区的时间一律**按本地墙上时间（wall clock）原样使用**。

国内教务系统导出的课表基本都是「本校本地时间」，直接取墙上时间不会错，
同时免去了 Windows 上 ``zoneinfo`` 依赖 ``tzdata`` 数据包的问题。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from dateutil.rrule import rrulestr

from .constants import LOG_PREFIX

logger = logging.getLogger(__name__)

#: 单个事件在一个窗口内最多展开多少次。
#: 重复规则来自**不可信文件**，`FREQ=SECONDLY` 这类在 7 天窗口能展开出 60 万次
#: （实测 1.8 秒、数百 MB），而展开是在提醒循环与规划器注入的同步路径上跑的。
#: 正常课表一个窗口撑死几百次，2000 足够宽松。
MAX_OCCURRENCES_PER_WINDOW = 2000

#: 已告警过的键（有上限，避免"每次 tick 都刷一条"与无界增长）
_WARNED_KEYS: dict[str, None] = {}
_MAX_WARNED_KEYS = 200


def _warn_once(key: str, message: str, *args: object) -> None:
    """同一个键只告警一次。

    ``expand_occurrences`` 每轮 tick 都会对每个事件调用一次，
    一条坏规则若不限制就会每分钟刷一行日志。
    """
    if key in _WARNED_KEYS:
        return
    _WARNED_KEYS[key] = None
    while len(_WARNED_KEYS) > _MAX_WARNED_KEYS:
        _WARNED_KEYS.pop(next(iter(_WARNED_KEYS)))
    logger.warning(message, *args)


#: 比分钟更细的重复频率。课表不会有这种规则，出现即视为坏数据/恶意构造
_ABSURD_FREQ_RE = re.compile(r"FREQ=(SECONDLY|SECOND|MILLISECONDLY|MICROSECONDLY)\b", re.I)


def _is_absurd_rule(normalized_rule: str) -> bool:
    """重复频率是否细到不合常理（按秒/毫秒重复）。"""
    return bool(_ABSURD_FREQ_RE.search(str(normalized_rule or "")))


__all__ = [
    "CourseEvent",
    "IcsParseError",
    "decode_ics_bytes",
    "expand_occurrences",
    "parse_ics",
    "parse_ics_many",
    "parse_ics_with_warnings",
]


class IcsParseError(ValueError):
    """ICS 内容无法解析时抛出。"""


def decode_ics_bytes(data: bytes, charset: str = "") -> str:
    """把 ICS 字节流解码成文本。

    国内教务系统导出的课表有相当比例是 GBK/GB18030，直接按 UTF-8 读会
    整篇变成乱码（课程名全毁），所以做一次编码嗅探：优先用 HTTP 响应声明
    的编码，其次 UTF-8，再退到 GB18030，最后才用替换字符兜底。
    """
    candidates = [charset, "utf-8", "gb18030"] if charset else ["utf-8", "gb18030"]
    for encoding in candidates:
        if not encoding:
            continue
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


# ── 文本层 ────────────────────────────────────────────────


def _unfold(text: str) -> list[str]:
    """按 RFC 5545 展开折行：以空格/制表符开头的行是上一行的续行。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for raw in normalized.split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return [line for line in lines if line.strip()]


def _split_property(line: str) -> tuple[str, dict[str, str], str]:
    """把一行拆成 ``(属性名, 参数, 值)``。

    冒号可能出现在带引号的参数值里（如 ``ATTENDEE;CN="A:B":mailto:x``），
    因此找冒号时必须跳过引号内的内容。
    """
    in_quote = False
    colon = -1
    for index, char in enumerate(line):
        if char == '"':
            in_quote = not in_quote
        elif char == ":" and not in_quote:
            colon = index
            break
    if colon < 0:
        raise IcsParseError(f"属性行缺少冒号: {line[:80]!r}")

    head, value = line[:colon], line[colon + 1 :]

    segments: list[str] = []
    current = ""
    in_quote = False
    for char in head:
        if char == '"':
            in_quote = not in_quote
            current += char
        elif char == ";" and not in_quote:
            segments.append(current)
            current = ""
        else:
            current += char
    segments.append(current)

    name = segments[0].strip().upper()
    params: dict[str, str] = {}
    for segment in segments[1:]:
        if "=" not in segment:
            continue
        key, _, val = segment.partition("=")
        params[key.strip().upper()] = val.strip().strip('"')
    return name, params, value


def _unescape(value: str) -> str:
    """还原 ICS 文本转义。"""
    out: list[str] = []
    index = 0
    length = len(value)
    while index < length:
        char = value[index]
        if char == "\\" and index + 1 < length:
            nxt = value[index + 1]
            if nxt in ("n", "N"):
                out.append("\n")
            elif nxt in ("\\", ";", ","):
                out.append(nxt)
            else:
                out.append(nxt)
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


# ── 时间解析 ──────────────────────────────────────────────

_DT_FORMATS = ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M")
_UTC_OFFSET_RE = re.compile(r"[+-]\d{4}$")


def parse_datetime(value: str, params: dict[str, str]) -> tuple[datetime, bool]:
    """解析 ICS 时间值，返回 ``(naive 本地时间, 是否全天)``。"""
    raw = value.strip()
    if not raw:
        raise IcsParseError("时间值为空")

    if params.get("VALUE", "").upper() == "DATE" or (len(raw) == 8 and "T" not in raw):
        try:
            return datetime.strptime(raw, "%Y%m%d"), True
        except ValueError as exc:
            raise IcsParseError(f"无法解析全天日期: {raw!r}") from exc

    is_utc = raw.endswith("Z")
    core = raw[:-1] if is_utc else raw
    # RFC 5545 允许「本地时间+UTC 偏移」写法（如 20260901T080000+0800）。
    # 本模块统一按墙上时间工作，所以直接剥掉偏移量不做换算。
    core = _UTC_OFFSET_RE.sub("", core)
    parsed: datetime | None = None
    for fmt in _DT_FORMATS:
        try:
            parsed = datetime.strptime(core, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise IcsParseError(f"无法解析时间: {raw!r}")

    if is_utc:
        parsed = parsed.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return parsed, False


# ── RRULE ─────────────────────────────────────────────────

_UNTIL_RE = re.compile(r"UNTIL=([0-9TZ]+)", re.IGNORECASE)


def _normalize_rrule(rule: str) -> str:
    """把 RRULE 里的 UNTIL 归一化成 naive 本地时间。

    ``dateutil`` 拒绝「naive dtstart + 带 Z 的 UTC UNTIL」这种混用组合，
    而本模块统一以 naive 本地时间工作，所以这里把 UNTIL 也换算过去。
    学期末边界最多差一个时区偏移，对周课表的最后一次课没有实际影响。
    """
    match = _UNTIL_RE.search(rule)
    if match is None:
        return rule

    raw = match.group(1)
    if raw.upper().endswith("Z"):
        try:
            moment = datetime.strptime(raw[:-1], "%Y%m%dT%H%M%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return rule
        replacement = moment.astimezone().replace(tzinfo=None).strftime("%Y%m%dT%H%M%S")
    elif len(raw) == 8:
        replacement = raw + "T000000"
    else:
        return rule
    return rule[: match.start(1)] + replacement + rule[match.end(1) :]


# ── 事件模型 ──────────────────────────────────────────────


@dataclass
class CourseEvent:
    """一条课程事件（可能带重复规则）。"""

    uid: str
    summary: str
    start: datetime
    end: datetime | None = None
    location: str = ""
    description: str = ""
    all_day: bool = False
    rrule: str = ""
    exdates: list[datetime] = field(default_factory=list)
    #: 日期型 EXDATE（``VALUE=DATE``）：只精确到天，按"日"匹配（见 expand_occurrences）
    exdate_days: list[date] = field(default_factory=list)
    recurrence_id: datetime | None = None
    source: str = ""
    #: 被 RECURRENCE-ID 覆盖的单次课（原开始时间 → 覆盖事件）
    overrides: dict[datetime, "CourseEvent"] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        """用于消息展示的课程名。"""
        return self.summary.strip() or "未命名课程"

    @property
    def duration(self) -> timedelta | None:
        """课程时长；缺 DTEND 时返回 ``None``。"""
        if self.end is None:
            return None
        return self.end - self.start


def _make_event(props: dict[str, tuple[dict[str, str], str]], source: str) -> CourseEvent:
    """由属性字典构造一个 ``CourseEvent``。"""
    uid = _unescape(props.get("UID", ({}, ""))[1]).strip()

    summary_raw = props.get("SUMMARY")
    summary = _unescape(summary_raw[1]).strip() if summary_raw else ""

    location_raw = props.get("LOCATION")
    location = _unescape(location_raw[1]).strip() if location_raw else ""

    description_raw = props.get("DESCRIPTION")
    description = _unescape(description_raw[1]).strip() if description_raw else ""

    start_raw = props.get("DTSTART")
    if start_raw is None:
        raise IcsParseError("VEVENT 缺少 DTSTART")
    start, all_day = parse_datetime(start_raw[1], start_raw[0])

    end: datetime | None = None
    end_raw = props.get("DTEND")
    if end_raw is not None:
        try:
            end, _ = parse_datetime(end_raw[1], end_raw[0])
        except IcsParseError:
            end = None

    rrule_raw = props.get("RRULE")
    rrule = rrule_raw[1].strip() if rrule_raw else ""

    exdates: list[datetime] = []
    exdate_days: list[date] = []
    exdate_raw = props.get("EXDATE")
    if exdate_raw is not None:
        params, value = exdate_raw
        for piece in value.split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                exdate, date_only = parse_datetime(piece, params)
            except IcsParseError:
                continue
            exdates.append(exdate)
            # 只精确到天的那种（VALUE=DATE 或裸 8 位数字）单独记一份，
            # 定时课上要按"日"匹配才排得掉（见 expand_occurrences）
            if date_only:
                exdate_days.append(exdate.date())

    recurrence_id: datetime | None = None
    rid_raw = props.get("RECURRENCE-ID")
    if rid_raw is not None:
        try:
            recurrence_id, _ = parse_datetime(rid_raw[1], rid_raw[0])
        except IcsParseError:
            recurrence_id = None

    return CourseEvent(
        uid=uid,
        summary=summary,
        start=start,
        end=end,
        location=location,
        description=description,
        all_day=all_day,
        rrule=rrule,
        exdates=exdates,
        exdate_days=exdate_days,
        recurrence_id=recurrence_id,
        source=source,
    )


def _group_events(raw_events: list[CourseEvent]) -> list[CourseEvent]:
    """把 RECURRENCE-ID 覆盖合并进主事件。"""
    bases: dict[str, CourseEvent] = {}
    overrides: dict[str, list[CourseEvent]] = {}
    standalone: list[CourseEvent] = []

    for event in raw_events:
        if event.recurrence_id is not None and event.uid:
            overrides.setdefault(event.uid, []).append(event)
        elif event.uid:
            # 同 UID 重复出现：后出现的覆盖先出现的（等价于日历的更新语义）
            bases[event.uid] = event
        else:
            standalone.append(event)

    for uid, items in overrides.items():
        base = bases.get(uid)
        if base is None:
            # 孤立覆盖（主事件被单独导出）：当作单次课
            for item in items:
                item.rrule = ""
                item.recurrence_id = None
                standalone.append(item)
            continue
        for item in items:
            base.overrides[item.recurrence_id] = item

    return [*bases.values(), *standalone]


def parse_ics(text: str, source: str = "") -> list[CourseEvent]:
    """解析一份 ICS 文本，返回合并后的课程事件列表。"""
    return _parse_ics_impl(text, source)[0]


def _parse_ics_impl(
    text: str, source: str
) -> tuple[list[CourseEvent], list[str]]:
    """真正的解析实现，返回 ``(事件列表, 非致命问题列表)``。"""
    if not text or "BEGIN:VEVENT" not in text.upper():
        raise IcsParseError("内容中找不到 VEVENT，可能不是 iCalendar 文件")

    raw_events: list[CourseEvent] = []
    skipped = 0
    current: dict[str, tuple[dict[str, str], str]] | None = None
    # 嵌套组件深度：VEVENT 之内还可能有 VALARM / VTODO 等子组件，
    # 它们自己的 SUMMARY/DESCRIPTION 会覆盖外层同名属性（Google/Outlook 导出的
    # 闹钟就是这样），所以深度 > 0 时一律丢弃属性行
    nested_depth = 0

    for line in _unfold(text):
        try:
            name, params, value = _split_property(line)
        except IcsParseError:
            # 单行畸形（例如混入了非 ICS 的说明文字）不该让整份课表报废
            skipped += 1
            continue

        if name == "BEGIN":
            component = value.strip().upper()
            if component == "VEVENT":
                current = {}
                nested_depth = 0
            elif current is not None:
                nested_depth += 1
            continue
        if name == "END":
            component = value.strip().upper()
            if component == "VEVENT":
                # 只在最外层收尾：子组件的 END 不该把整个事件结掉
                if nested_depth == 0 and current is not None:
                    try:
                        raw_events.append(_make_event(current, source))
                    except IcsParseError:
                        # 单条事件缺关键字段时跳过，不让整份课表导入失败
                        skipped += 1
                    current = None
            elif current is not None and nested_depth > 0:
                nested_depth -= 1
            continue
        if current is None or nested_depth > 0:
            # 子组件（如 VALARM）里的属性不属于这节课，忽略
            continue

        # EXDATE 可能重复出现，用列表语义承载
        upper = name.upper()
        if upper == "EXDATE" and "EXDATE" in current:
            prev_params, prev_value = current["EXDATE"]
            current["EXDATE"] = (prev_params, f"{prev_value},{value}")
            continue
        current[upper] = (params, value)

    if not raw_events:
        raise IcsParseError("没有解析出任何有效课程事件")

    warnings: list[str] = []
    if skipped:
        # 用模块级 logger：SDK 说明非 plugin. 前缀的 logger 也会被转发到主进程
        logger.warning(
            "%s 有 %d 处内容无法解析已跳过（文件：%s）",
            LOG_PREFIX,
            skipped,
            source or "<未知来源>",
        )
        # 一并回给调用方：导入回执里要能告诉用户"有几行没读进去"，
        # 只写日志的话用户不知道自己的课表被啃掉了一部分
        warnings.append(f"{skipped} 处内容无法解析已跳过")

    return _group_events(raw_events), warnings


def parse_ics_with_warnings(
    text: str, source: str = ""
) -> tuple[list[CourseEvent], list[str]]:
    """同 :func:`parse_ics`，但把"有问题但没致命"的信息一并返回。

    目前只有一处：文件里有多少行因为格式问题被跳过。
    """
    return _parse_ics_impl(text, source)


def parse_ics_many(sources: dict[str, str]) -> tuple[list[CourseEvent], list[str]]:
    """解析多份 ICS，返回 ``(事件列表, 错误信息列表)``。

    单份文件失败不影响其他文件。
    """
    events: list[CourseEvent] = []
    errors: list[str] = []
    for source, text in sources.items():
        try:
            events.extend(parse_ics(text, source=source))
        except IcsParseError as exc:
            errors.append(f"{source}: {exc}")
    return events, errors


# ── 展开 ──────────────────────────────────────────────────


def expand_occurrences(
    event: CourseEvent,
    window_start: datetime,
    window_end: datetime,
) -> list[CourseEvent]:
    """把事件在 ``[window_start, window_end]`` 内的每次课展开成独立事件。

    返回的新事件 ``rrule`` 为空、``start`` 为该次课的开始时间，
    ``overrides`` 里的覆盖会替换掉对应的那一次。
    """
    if event.all_day:
        return []

    if not event.rrule:
        if window_start <= event.start <= window_end:
            return [_materialize(event, event.start)]
        return []

    normalized = _normalize_rrule(event.rrule)
    if _is_absurd_rule(normalized):
        # 课表不可能按秒/按分钟重复。这类规则要么是坏数据，要么是刻意构造的，
        # 一律按"单次课"处理并告警——比让它把同步路径算死要好
        _warn_once(
            f"absurd:{event.uid}:{event.rrule}",
            "%s 课程的重复规则过密（按秒/分钟重复），已按单次课处理：规则=%r；来源=%s",
            LOG_PREFIX,
            event.rrule,
            event.source or "<未知来源>",
        )
        if window_start <= event.start <= window_end:
            return [_materialize(event, event.start)]
        return []

    try:
        rule = rrulestr(normalized, dtstart=event.start)
    except (ValueError, TypeError) as exc:
        # 规则无法解析时退化为单次事件（避免整节课丢失），但**必须留痕**：
        # 静默退化会让一门每周重复的课只剩第一次，用户以为插件不再提醒了
        _warn_once(
            f"rrule:{event.uid}:{event.rrule}",
            "%s 课程的重复规则无法解析，已按单次课处理（只有这一次会提醒）："
            "%s；规则=%r；来源=%s",
            LOG_PREFIX,
            exc,
            event.rrule,
            event.source or "<未知来源>",
        )
        if window_start <= event.start <= window_end:
            return [_materialize(event, event.start)]
        return []

    # 用惰性的 xafter 而不是 between：between 会先把窗口内**全部**时间点物化出来
    # 再交给我们，规则由不可信文件决定时那可能是几十万个（实测 60 万个 / 1.8 秒）。
    # 这里边取边截断，时间和内存都是有界的。
    occurrences: list[datetime] = []
    truncated = False
    try:
        for moment in rule.xafter(
            window_start, count=MAX_OCCURRENCES_PER_WINDOW + 1, inc=True
        ):
            if moment > window_end:
                break
            if len(occurrences) >= MAX_OCCURRENCES_PER_WINDOW:
                truncated = True
                break
            occurrences.append(moment)
    except (ValueError, TypeError) as exc:
        _warn_once(
            f"between:{event.uid}:{exc}",
            "%s 课程按重复规则展开失败，本次不产生提醒：%s；规则=%r",
            LOG_PREFIX,
            exc,
            event.rrule,
        )
        occurrences = []

    if truncated:
        _warn_once(
            f"cap:{event.uid}",
            "%s 课程的重复规则过密（窗口内超过 %d 次），已只取前 %d 次："
            "规则=%r；来源=%s",
            LOG_PREFIX,
            MAX_OCCURRENCES_PER_WINDOW,
            MAX_OCCURRENCES_PER_WINDOW,
            event.rrule,
            event.source or "<未知来源>",
        )

    excluded = set(event.exdates)
    # 日期型 EXDATE（``EXDATE;VALUE=DATE:20260915``）在定时课上只给到"哪一天"，
    # 解析出来是当天 00:00，而这次课的开始时间在 08:00，精确比较永远匹配不上。
    # 教务系统常用这种写法标注停课/调课，所以按"日"再匹配一次。
    excluded_days = set(event.exdate_days)
    result: list[CourseEvent] = []
    consumed: set[datetime] = set()

    def _is_excluded(moment: datetime) -> bool:
        """该次课是否被 EXDATE 排除掉。"""
        return moment in excluded or moment.date() in excluded_days

    for moment in occurrences:
        if _is_excluded(moment):
            continue
        override = event.overrides.get(moment)
        if override is not None:
            consumed.add(moment)
            # 覆盖可能把课挪到别的时间，挪出窗口的这次课就不再提醒
            if window_start <= override.start <= window_end:
                result.append(_materialize(override, override.start))
            continue
        result.append(_materialize(event, moment))

    # 覆盖可能把课从窗口外挪进窗口内（如调课），这类要单独补上
    for recurrence_id, override in event.overrides.items():
        if recurrence_id in consumed or _is_excluded(recurrence_id):
            continue
        if window_start <= override.start <= window_end:
            result.append(_materialize(override, override.start))

    result.sort(key=lambda item: item.start)
    return result


def _materialize(event: CourseEvent, moment: datetime) -> CourseEvent:
    """生成一次具体课程（清空重复规则，按实际时间平移 DTEND）。"""
    duration = event.duration
    end = moment + duration if duration is not None else None
    return CourseEvent(
        uid=event.uid,
        summary=event.summary,
        start=moment,
        end=end,
        location=event.location,
        description=event.description,
        all_day=event.all_day,
        rrule="",
        exdates=[],
        recurrence_id=None,
        source=event.source,
    )
