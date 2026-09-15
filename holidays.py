"""法定节假日：数据加载、缓存与判定。

数据源是**按年发布的 JSON**（默认取 holiday-cn 项目，它由国务院公告生成）::

    {
      "year": 2026,
      "papers": ["https://www.gov.cn/..."],
      "days": [
        {"name": "元旦", "date": "2026-01-01", "isOffDay": true},
        {"name": "元旦", "date": "2026-01-04", "isOffDay": false}
      ]
    }

注意 ``isOffDay`` 为 ``false`` 的条目是**调休上班日**（周末补班），不是放假。
因此「出现在表里」不等于假日，必须看这个字段——把调休上班日当假日会漏掉
当天的课。

关键取舍：**缺数据时一律不跳过提醒**（fail-open）。假日数据依赖网络，而
"假期里多提醒一次"只是烦人，"上课日漏提醒"会真的误事。缺哪一年会在启动
日志里告警，也可以通过 ``/课表假日`` 自查。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .constants import LOG_PREFIX

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SOURCE_URL_TEMPLATE",
    "HolidayCalendar",
    "HolidayDay",
    "HolidayNotPublishedError",
    "cache_path",
    "is_stale",
    "render_url_template",
    "write_cache",
]

#: 默认数据源（holiday-cn 项目，由国务院公告生成；`{year}` 会被替换成4位年份）
DEFAULT_SOURCE_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json"
)

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MONTH_DAY_RE = re.compile(r"^(\d{1,2})-(\d{1,2})$")
_YEAR_PLACEHOLDER_RE = re.compile(r"\{year\}")


class HolidayNotPublishedError(ValueError):
    """该年份的放假安排尚未公布（``days`` 是空列表）。

    这不是错误：国务院一般要到当年 11 月前后才公布次年安排，所以「明年没有
    数据」是常态。调用方应据此安静跳过，而不是当成格式问题反复告警重试。
    """


@dataclass(frozen=True)
class HolidayDay:
    """一天及其性质。"""

    date: date
    name: str
    #: ``True`` = 放假；``False`` = 调休上班（周末补班）
    is_off_day: bool

    @property
    def kind_label(self) -> str:
        """中文性质描述。"""
        return "放假" if self.is_off_day else "调休上班"


def _parse_date(raw: Any) -> date | None:
    """解析 ``YYYY-MM-DD``。"""
    match = _DATE_RE.match(str(raw or "").strip())
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _as_date_list(values: Iterable[Any] | str | None) -> list[Any]:
    """把配置值收敛成列表。

    直接 ``for item in "2026-01-01"`` 会把字符串拆成单个字符，
    于是"格式错误"变成一串 '2','0','2','6' 报警告，很难排查。
    """
    if values is None:
        return []
    if isinstance(values, str):
        return [values]
    try:
        return list(values)
    except TypeError:
        return [values]


def _parse_month_day(raw: str) -> tuple[int, int] | None:
    """解析 ``MM-DD``，非法返回 ``None``。"""
    match = _MONTH_DAY_RE.match(raw)
    if match is None:
        return None
    month, day_of_month = int(match.group(1)), int(match.group(2))
    try:
        # 用闰年校验合法性（02-29 只在闰年存在，仍允许配置）
        date(2024, month, day_of_month)
    except ValueError:
        return None
    return month, day_of_month


def cache_path(cache_dir: Path, year: int) -> Path:
    """某年份缓存文件路径。"""
    return Path(cache_dir) / f"{year}.json"


def write_cache(cache_dir: Path, year: int, text: str) -> Path:
    """原子写入年份缓存（先写临时文件再替换，避免留下半份 JSON）。"""
    target = cache_path(cache_dir, year)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


class HolidayCalendar:
    """多年度法定节假日表（含调休上班日）与用户自定假日。"""

    def __init__(self) -> None:
        #: 数据源给出的每一天（放假 + 调休上班）
        self._days: dict[date, HolidayDay] = {}
        #: 数据源实际提供了日期的年份（判定"这一年到底有没有数据"）
        self._years: set[int] = set()
        #: 用户自定假日（校历自定假日、寒暑假等），按具体日期
        self._extra: dict[date, str] = {}
        #: 用户自定假日里按"每年同一天"给出的（MM-DD）
        self._extra_yearly: dict[tuple[int, int], str] = {}
        #: 用户显式声明"这天要上课"的日期，压过法定假日（学校补课等）
        self._excluded: set[date] = set()
        self._excluded_yearly: set[tuple[int, int]] = set()
        #: 各年份数据来源，仅用于展示/排查
        self._sources: dict[int, str] = {}

    # ── 载入 ──────────────────────────────────────────────

    def load_payload(self, payload: dict[str, Any] | str, *, source: str = "") -> int:
        """载入一份年度数据，返回成功读入的天数。

        容错：单条日期非法就跳过那一条，不让整年数据报废；但若一条都读不出来
        会抛出 ``ValueError``，以便调用方不要把坏数据当成功。
        """
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError) as exc:
                raise ValueError(f"不是合法的 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("顶层结构不是对象")

        days_raw = payload.get("days")
        if not isinstance(days_raw, list):
            raise ValueError("缺少 days 列表")
        if not days_raw:
            # 空列表 = 该年安排尚未公布，不是坏数据
            raise HolidayNotPublishedError("days 为空，该年份放假安排尚未公布")

        loaded = 0
        touched_years: set[int] = set()
        for item in days_raw:
            if not isinstance(item, dict):
                continue
            day = _parse_date(item.get("date"))
            if day is None:
                continue
            # 只有显式 isOffDay=true 才算放假，缺失或 false 都按"不是假日"处理，
            # 避免因为字段异常而误把上课日当假日跳过
            is_off_day = item.get("isOffDay") is True
            name = str(item.get("name") or "").strip() or ("放假" if is_off_day else "调休上班")
            self._days[day] = HolidayDay(date=day, name=name, is_off_day=is_off_day)
            touched_years.add(day.year)
            loaded += 1

        if loaded == 0:
            raise ValueError("days 里没有一条可用的日期")

        # 只有真的带来了这一年的日期，才算"覆盖了这一年"。
        # 顶层 year 字段不能拿来撑大覆盖范围：声明与 days 不一致时（数据源
        # 异常或公告年 ≠ 条目年），会出现"被告知数据齐全、实际一天都判不出来"。
        self._years |= touched_years
        for value in touched_years:
            self._sources.setdefault(value, source or f"{value}.json")
        return loaded

    def load_cache_dir(self, cache_dir: Path) -> int:
        """载入缓存目录里所有年份文件，返回成功载入的文件数。

        单个文件损坏只记日志并跳过，其余文件照常载入。
        """
        directory = Path(cache_dir)
        if not directory.is_dir():
            return 0
        loaded = 0
        for path in sorted(directory.glob("*.json")):
            try:
                text = path.read_text(encoding="utf-8")
                self.load_payload(text, source=path.name)
                loaded += 1
            except HolidayNotPublishedError:
                continue  # 该年安排尚未公布，缓存里可能是空数据，安静跳过
            except (OSError, ValueError) as exc:
                logger.warning("%s 节假日缓存文件不可用已跳过：%s（%s）", LOG_PREFIX, path.name, exc)
        return loaded

    def add_extra_dates(self, values: Iterable[Any]) -> tuple[int, list[str]]:
        """加入用户自定假日，返回 ``(成功条数, 被拒绝的原始值列表)``。

        支持 ``YYYY-MM-DD``（一次性）与 ``MM-DD``（每年同一天，例如校庆）。
        """
        added = 0
        rejected: list[str] = []
        for item in _as_date_list(values):
            raw = str(item or "").strip()
            if not raw:
                continue
            day = _parse_date(raw)
            if day is not None:
                self._extra[day] = "自定假日"
                added += 1
                continue
            month_day = _parse_month_day(raw)
            if month_day is None:
                rejected.append(raw)
                continue
            self._extra_yearly[month_day] = "自定假日"
            added += 1
        return added, rejected

    def add_excluded_dates(self, values: Iterable[Any]) -> tuple[int, list[str]]:
        """加入"这天要上课、别当假日跳过"的日期，用法同 :meth:`add_extra_dates`。

        用于学校在法定假日补课这类情况：放假表说放假，但你们要上课。
        排除项优先于任何假日判定。
        """
        added = 0
        rejected: list[str] = []
        for item in _as_date_list(values):
            raw = str(item or "").strip()
            if not raw:
                continue
            day = _parse_date(raw)
            if day is not None:
                self._excluded.add(day)
                added += 1
                continue
            month_day = _parse_month_day(raw)
            if month_day is None:
                rejected.append(raw)
                continue
            self._excluded_yearly.add(month_day)
            added += 1
        return added, rejected

    # ── 查询 ──────────────────────────────────────────────

    @property
    def years(self) -> list[int]:
        """已有实际日期数据的年份，升序。"""
        return sorted(self._years)

    @property
    def day_count(self) -> int:
        """已载入的天数（含调休上班日）。"""
        return len(self._days)

    @property
    def extra_count(self) -> int:
        """用户自定假日条数。"""
        return len(self._extra) + len(self._extra_yearly)

    def source_of(self, year: int) -> str:
        """某年份的数据来源描述。"""
        return self._sources.get(year, "")

    def has_entries_for(self, year: int) -> bool:
        """数据里是否真的包含这一年的日期（用于校验下回来的内容）。"""
        return year in self._years

    def covers(self, day: date) -> bool:
        """是否已有该年份的数据（没有就无法判断，按"不是假日"处理）。"""
        return day.year in self._years

    def entry_of(self, day: date) -> HolidayDay | None:
        """返回该日在数据源里的条目（含调休上班日），没有则 ``None``。"""
        return self._days.get(day)

    def is_excluded(self, day: date) -> bool:
        """是否被用户显式声明为"这天要上课"（压过假日判定）。"""
        return day in self._excluded or (day.month, day.day) in self._excluded_yearly

    def is_user_extra(self, day: date) -> bool:
        """是否是用户自定假日（``extra_dates``，如寒暑假、校历假日）。

        与数据源里的法定节假日分开，因为两者的开关不同：法定节假日受
        ``holiday.skip_off_days`` 控制，而自定假日是用户逐条写下的名单，始终生效。
        """
        return day in self._extra or (day.month, day.day) in self._extra_yearly

    def is_off_day(self, day: date) -> bool:
        """是否放假（法定节假日或用户自定假日）。

        用户显式排除的日期优先返回 ``False``：放假表说放假、但你们学校要补课。
        """
        if self.is_excluded(day):
            return False
        if self.is_user_extra(day):
            return True
        entry = self._days.get(day)
        return bool(entry is not None and entry.is_off_day)

    def is_makeup_workday(self, day: date) -> bool:
        """是否是调休上班日（周末补班，**不是**假日）。

        自定假日优先：用户把某天列进 ``extra_dates`` 就是在说"这天不上课"，
        再同时把它报成"调休上班"会自相矛盾（展示成「自定假日（调休上班）」）。
        """
        if self.is_excluded(day) or self.is_user_extra(day):
            return False
        entry = self._days.get(day)
        return bool(entry is not None and not entry.is_off_day)

    def name_of(self, day: date) -> str:
        """该日的假日名称；自定假日优先，其次数据源。"""
        if self.is_excluded(day):
            return ""
        if day in self._extra:
            return self._extra[day]
        if (day.month, day.day) in self._extra_yearly:
            return self._extra_yearly[(day.month, day.day)]
        entry = self._days.get(day)
        return entry.name if entry is not None else ""

    def label_of(self, day: date) -> str:
        """展示用描述，例如 ``元旦（放假）``、``春节（调休上班）``；无信息返回空串。"""
        # 被显式排除的日子按"平日"展示，别再说它是假日
        if self.is_excluded(day):
            return ""
        name = self.name_of(day)
        # 自定假日优先：同一天在数据源里可能被标成"调休上班"，
        # 但用户把它列进 extra_dates 就是在说"这天不上课"，
        # 否则会展示成「自定假日（调休上班）」自相矛盾
        if self.is_user_extra(day):
            return f"{name}（放假）"
        entry = self._days.get(day)
        if entry is not None:
            return f"{name}（{entry.kind_label}）"
        if name:
            return f"{name}（自定假日）"
        return ""

    def uncovered_years(self, days: Iterable[date]) -> list[int]:
        """列出所给日期里、本地没有数据的年份（用于告警）。"""
        return sorted({day.year for day in days if not self.covers(day)})

    def is_empty(self) -> bool:
        """是否完全没有可用数据（既没有数据源也没有自定假日）。"""
        return not self._days and not self._extra and not self._extra_yearly


def render_url_template(template: str, year: int) -> str:
    """把数据源模板里的 ``{year}`` 替换成具体年份。"""
    text = str(template or "").strip()
    return _YEAR_PLACEHOLDER_RE.sub(str(year), text)


def is_stale(path: Path, *, now: datetime, max_age_hours: float) -> bool:
    """缓存文件是否缺失或过期。"""
    if max_age_hours <= 0:
        return not path.exists()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return True
    age_hours = (now.timestamp() - mtime) / 3600
    return age_hours >= max_age_hours
