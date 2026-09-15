"""法定节假日模块测试：数据解析、缓存、判定与 fail-open 行为。"""

import json
import os
import unittest
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import _bootstrap  # noqa: F401  —— 注册插件包

from class_schedule.holidays import (
    HolidayCalendar,
    HolidayNotPublishedError,
    cache_path,
    is_stale,
    render_url_template,
    write_cache,
)

#: 一份贴近真实结构的数据（含 调休上班 与 放假 两种条目）
PAYLOAD_2026 = {
    "year": 2026,
    "papers": ["https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm"],
    "days": [
        {"name": "元旦", "date": "2026-01-01", "isOffDay": True},
        {"name": "元旦", "date": "2026-01-02", "isOffDay": True},
        {"name": "元旦", "date": "2026-01-04", "isOffDay": False},  # 调休上班
        {"name": "春节", "date": "2026-02-16", "isOffDay": True},
        {"name": "国庆节", "date": "2026-09-20", "isOffDay": False},  # 调休上班
        {"name": "国庆节", "date": "2026-10-01", "isOffDay": True},
    ],
}


def payload_text(payload: dict | None = None) -> str:
    return json.dumps(payload if payload is not None else PAYLOAD_2026, ensure_ascii=False)


class TestLoadPayload(unittest.TestCase):
    def test_loads_all_entries(self):
        calendar = HolidayCalendar()
        self.assertEqual(calendar.load_payload(payload_text(), source="2026.json"), 6)
        self.assertEqual(calendar.years, [2026])
        self.assertEqual(calendar.day_count, 6)

    def test_accepts_dict_too(self):
        calendar = HolidayCalendar()
        self.assertEqual(calendar.load_payload(PAYLOAD_2026), 6)

    def test_is_off_day_only_for_off_days(self):
        """关键：调休上班日不能算假日，否则会漏掉当天的课。"""
        calendar = HolidayCalendar()
        calendar.load_payload(PAYLOAD_2026)
        self.assertTrue(calendar.is_off_day(date(2026, 1, 1)))
        self.assertFalse(calendar.is_off_day(date(2026, 1, 4)))  # 调休上班
        self.assertTrue(calendar.is_makeup_workday(date(2026, 1, 4)))
        self.assertFalse(calendar.is_makeup_workday(date(2026, 1, 1)))

    def test_name_and_label(self):
        calendar = HolidayCalendar()
        calendar.load_payload(PAYLOAD_2026)
        self.assertEqual(calendar.name_of(date(2026, 1, 1)), "元旦")
        self.assertEqual(calendar.label_of(date(2026, 1, 1)), "元旦（放假）")
        self.assertEqual(calendar.label_of(date(2026, 1, 4)), "元旦（调休上班）")
        self.assertEqual(calendar.label_of(date(2026, 3, 1)), "")

    def test_missing_isoffday_treated_as_not_holiday(self):
        """字段缺失时不能当假日，否则上课日会被跳过。"""
        calendar = HolidayCalendar()
        calendar.load_payload({"year": 2026, "days": [{"name": "可疑", "date": "2026-03-03"}]})
        self.assertFalse(calendar.is_off_day(date(2026, 3, 3)))

    def test_explicit_false_is_not_holiday(self):
        calendar = HolidayCalendar()
        calendar.load_payload(
            {"year": 2026, "days": [{"name": "调休", "date": "2026-03-04", "isOffDay": False}]}
        )
        self.assertFalse(calendar.is_off_day(date(2026, 3, 4)))

    def test_bad_entries_skipped_but_good_kept(self):
        calendar = HolidayCalendar()
        loaded = calendar.load_payload(
            {
                "year": 2026,
                "days": [
                    {"name": "好的", "date": "2026-05-01", "isOffDay": True},
                    {"name": "坏的", "date": "2026-13-45", "isOffDay": True},
                    {"name": "缺日期"},
                    "不是字典",
                    None,
                ],
            }
        )
        self.assertEqual(loaded, 1)
        self.assertTrue(calendar.is_off_day(date(2026, 5, 1)))

    def test_year_inferred_when_missing(self):
        """顶层没写 year 时按数据里的日期推断。"""
        calendar = HolidayCalendar()
        calendar.load_payload(
            {"days": [{"name": "x", "date": "2027-01-01", "isOffDay": True}]}
        )
        self.assertEqual(calendar.years, [2027])
        self.assertTrue(calendar.covers(date(2027, 1, 1)))

    def test_coverage_follows_actual_dates(self):
        """回归：覆盖年份按实际日期统计。

        声明年份与 days 不一致时（数据源异常或人工编辑），如果只信顶层字段，
        会出现「声称覆盖 2026、却按 2027 的日期判假」的矛盾。
        """
        calendar = HolidayCalendar()
        calendar.load_payload(
            {"year": 2027, "days": [{"name": "x", "date": "2026-01-01", "isOffDay": True}]}
        )
        self.assertIn(2026, calendar.years)  # 实际日期所在年份必须被登记
        self.assertTrue(calendar.covers(date(2026, 1, 1)))
        self.assertTrue(calendar.is_off_day(date(2026, 1, 1)))

    def test_rejects_broken_payloads(self):
        for payload in ("不是 json", json.dumps([1, 2]), json.dumps({"days": "x"}),
                        json.dumps({"days": [{"date": "坏"}]})):
            with self.subTest(payload=payload[:30]):
                with self.assertRaises(ValueError):
                    HolidayCalendar().load_payload(payload)

    def test_empty_days_means_not_published(self):
        """回归：空 days 是"尚未公布"，不是坏数据。

        实测发现：次年安排要到当年 11 月前后才公布，此前 days 就是空列表。
        原先一律当格式错误，导致每年大半时间都在报警并反复下载。
        """
        for payload in (
            {"year": 2027, "days": []},
            json.dumps({"year": 2027, "days": []}),
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(HolidayNotPublishedError):
                    HolidayCalendar().load_payload(payload)

    def test_not_published_is_still_a_value_error(self):
        """调用方按 ValueError 兜底时也不会漏掉这个分支。"""
        self.assertTrue(issubclass(HolidayNotPublishedError, ValueError))

    def test_empty_cache_file_is_skipped_quietly(self):
        """缓存里若有空数据文件，按"未公布"跳过，不该报成缓存损坏。"""
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "2027.json").write_text(
                json.dumps({"year": 2027, "days": []}), encoding="utf-8"
            )
            with self.assertNoLogs("class_schedule.holidays", level="WARNING"):
                loaded = HolidayCalendar().load_cache_dir(directory)
            self.assertEqual(loaded, 0)

    def test_covers_only_loads_years(self):
        calendar = HolidayCalendar()
        calendar.load_payload(PAYLOAD_2026)
        self.assertTrue(calendar.covers(date(2026, 6, 1)))
        self.assertFalse(calendar.covers(date(2027, 6, 1)))

    def test_uncovered_years_reported(self):
        calendar = HolidayCalendar()
        calendar.load_payload(PAYLOAD_2026)
        self.assertEqual(
            calendar.uncovered_years([date(2026, 3, 1), date(2027, 3, 1)]), [2027]
        )


class TestFailOpen(unittest.TestCase):
    """拿不到数据时一律不跳过——假期多提醒一次好过上课日漏提醒。"""

    def test_empty_calendar_never_skips(self):
        calendar = HolidayCalendar()
        self.assertTrue(calendar.is_empty())
        self.assertFalse(calendar.is_off_day(date(2026, 1, 1)))
        self.assertFalse(calendar.covers(date(2026, 1, 1)))

    def test_unknown_year_never_skips(self):
        calendar = HolidayCalendar()
        calendar.load_payload(PAYLOAD_2026)
        # 2027 年没有数据：即使是元旦也不能跳过
        self.assertFalse(calendar.is_off_day(date(2027, 1, 1)))

    def test_regular_day_not_skipped(self):
        calendar = HolidayCalendar()
        calendar.load_payload(PAYLOAD_2026)
        self.assertFalse(calendar.is_off_day(date(2026, 3, 10)))


class TestExtraDates(unittest.TestCase):
    def test_full_date(self):
        calendar = HolidayCalendar()
        added, rejected = calendar.add_extra_dates(["2026-11-15"])
        self.assertEqual((added, rejected), (1, []))
        self.assertTrue(calendar.is_off_day(date(2026, 11, 15)))
        self.assertFalse(calendar.is_off_day(date(2025, 11, 15)))

    def test_yearless_date_repeats_every_year(self):
        """校庆这类每年同一天的假日用 MM-DD。"""
        calendar = HolidayCalendar()
        added, _ = calendar.add_extra_dates(["05-20"])
        self.assertEqual(added, 1)
        self.assertTrue(calendar.is_off_day(date(2026, 5, 20)))
        self.assertTrue(calendar.is_off_day(date(2033, 5, 20)))

    def test_extra_beats_source_name(self):
        calendar = HolidayCalendar()
        calendar.load_payload(PAYLOAD_2026)
        calendar.add_extra_dates(["2026-01-01"])
        self.assertEqual(calendar.name_of(date(2026, 1, 1)), "自定假日")

    def test_invalid_values_reported(self):
        calendar = HolidayCalendar()
        added, rejected = calendar.add_extra_dates(["", "  ", "不是日期", "2026-13-01", "13-45"])
        self.assertEqual(added, 0)
        self.assertEqual(rejected, ["不是日期", "2026-13-01", "13-45"])

    def test_leap_day_allowed(self):
        calendar = HolidayCalendar()
        added, rejected = calendar.add_extra_dates(["02-29"])
        self.assertEqual((added, rejected), (1, []))

    def test_extra_makes_calendar_non_empty(self):
        calendar = HolidayCalendar()
        calendar.add_extra_dates(["2026-11-15"])
        self.assertFalse(calendar.is_empty())

    def test_none_input_is_safe(self):
        self.assertEqual(HolidayCalendar().add_extra_dates(None), (0, []))

    def test_bare_string_not_split_into_chars(self):
        """回归：直接传字符串会被逐字符拆开，报出一串 '2','0','2','6' 的警告。"""
        calendar = HolidayCalendar()
        added, rejected = calendar.add_extra_dates("2026-11-15")
        self.assertEqual((added, rejected), (1, []))
        self.assertTrue(calendar.is_off_day(date(2026, 11, 15)))

    def test_single_non_iterable_is_accepted(self):
        calendar = HolidayCalendar()
        added, _ = calendar.add_extra_dates(20261115)
        self.assertEqual(added, 0)  # 整数不是合法日期写法，应被拒绝而不是崩


class TestExcludedDates(unittest.TestCase):
    """放假表说放假，但学校要补课——排除项优先。"""

    def test_excluded_date_no_longer_skipped(self):
        calendar = HolidayCalendar()
        calendar.load_payload(PAYLOAD_2026)
        self.assertTrue(calendar.is_off_day(date(2026, 10, 1)))

        added, rejected = calendar.add_excluded_dates(["2026-10-01"])
        self.assertEqual((added, rejected), (1, []))
        self.assertTrue(calendar.is_excluded(date(2026, 10, 1)))
        self.assertFalse(calendar.is_off_day(date(2026, 10, 1)))

    def test_exclusion_can_target_own_holiday(self):
        calendar = HolidayCalendar()
        calendar.add_extra_dates(["2026-11-15"])
        calendar.add_excluded_dates(["2026-11-15"])
        self.assertFalse(calendar.is_off_day(date(2026, 11, 15)))

    def test_exclusion_can_target_makeup_workday(self):
        calendar = HolidayCalendar()
        calendar.load_payload(PAYLOAD_2026)
        self.assertTrue(calendar.is_makeup_workday(date(2026, 1, 4)))
        calendar.add_excluded_dates(["2026-01-04"])
        self.assertFalse(calendar.is_makeup_workday(date(2026, 1, 4)))

    def test_yearless_exclusion_repeats(self):
        calendar = HolidayCalendar()
        calendar.add_excluded_dates(["10-01"])
        self.assertTrue(calendar.is_excluded(date(2027, 10, 1)))
        self.assertTrue(calendar.is_excluded(date(2030, 10, 1)))

    def test_excluded_has_no_name(self):
        calendar = HolidayCalendar()
        calendar.load_payload(PAYLOAD_2026)
        calendar.add_excluded_dates(["2026-10-01"])
        self.assertEqual(calendar.name_of(date(2026, 10, 1)), "")
        self.assertEqual(calendar.label_of(date(2026, 10, 1)), "")

    def test_invalid_values_reported(self):
        calendar = HolidayCalendar()
        added, rejected = calendar.add_excluded_dates(["瞎写的", "2026-13-01"])
        self.assertEqual(added, 0)
        self.assertEqual(rejected, ["瞎写的", "2026-13-01"])

    def test_none_input_is_safe(self):
        self.assertEqual(HolidayCalendar().add_excluded_dates(None), (0, []))


class TestEntryQueries(unittest.TestCase):
    def test_has_entries_for(self):
        calendar = HolidayCalendar()
        calendar.load_payload(PAYLOAD_2026)
        self.assertTrue(calendar.has_entries_for(2026))
        self.assertFalse(calendar.has_entries_for(2027))

    def test_declared_year_alone_does_not_claim_coverage(self):
        """回归：顶层 year 不能撑大覆盖范围，否则会谎报"数据齐全"。"""
        calendar = HolidayCalendar()
        calendar.load_payload(
            {"year": 2027, "days": [{"name": "x", "date": "2026-01-01", "isOffDay": True}]}
        )
        self.assertTrue(calendar.has_entries_for(2026))
        self.assertFalse(calendar.has_entries_for(2027))
        self.assertFalse(calendar.covers(date(2027, 1, 1)))
        self.assertEqual(calendar.uncovered_years([date(2027, 1, 1)]), [2027])

    def test_entry_of_returns_makeup_days_too(self):
        calendar = HolidayCalendar()
        calendar.load_payload(PAYLOAD_2026)
        entry = calendar.entry_of(date(2026, 1, 4))
        self.assertIsNotNone(entry)
        self.assertFalse(entry.is_off_day)
        self.assertEqual(entry.kind_label, "调休上班")
        self.assertIsNone(calendar.entry_of(date(2026, 3, 10)))

    def test_extra_count(self):
        calendar = HolidayCalendar()
        calendar.add_extra_dates(["2026-11-15", "05-20"])
        self.assertEqual(calendar.extra_count, 2)


class TestCache(unittest.TestCase):
    def test_write_then_load(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            write_cache(directory, 2026, payload_text())
            calendar = HolidayCalendar()
            self.assertEqual(calendar.load_cache_dir(directory), 1)
            self.assertTrue(calendar.is_off_day(date(2026, 1, 1)))

    def test_cache_path_name(self):
        self.assertEqual(cache_path(Path("x"), 2026).name, "2026.json")

    def test_multiple_years(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            write_cache(directory, 2026, payload_text())
            write_cache(
                directory,
                2027,
                payload_text({"year": 2027, "days": [{"name": "元旦", "date": "2027-01-01", "isOffDay": True}]}),
            )
            calendar = HolidayCalendar()
            self.assertEqual(calendar.load_cache_dir(directory), 2)
            self.assertEqual(calendar.years, [2026, 2027])

    def test_broken_file_skipped_others_kept(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            write_cache(directory, 2026, payload_text())
            (directory / "2027.json").write_text("{ 坏文件", encoding="utf-8")
            calendar = HolidayCalendar()
            self.assertEqual(calendar.load_cache_dir(directory), 1)
            self.assertTrue(calendar.is_off_day(date(2026, 1, 1)))

    def test_missing_directory_is_safe(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(HolidayCalendar().load_cache_dir(Path(tmp) / "nope"), 0)

    def test_no_temp_file_left(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            write_cache(directory, 2026, payload_text())
            self.assertEqual([p.name for p in directory.iterdir()], ["2026.json"])

    def test_write_creates_parent(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "deep" / "holidays"
            write_cache(target, 2026, payload_text())
            self.assertTrue((target / "2026.json").exists())


class TestStaleness(unittest.TestCase):
    """显式设置文件时间，让边界判定确定。

    否则"写文件"与"取 now"之间的微秒差会让 12 小时的边界用例时灵时不灵。
    """

    def _aged_cache(self, tmp: str, hours: float) -> Path:
        path = write_cache(Path(tmp), 2026, payload_text())
        mtime = datetime.now().timestamp() - hours * 3600
        os.utime(path, (mtime, mtime))
        return path

    def test_missing_file_is_stale(self):
        with TemporaryDirectory() as tmp:
            self.assertTrue(
                is_stale(Path(tmp) / "nope.json", now=datetime.now(), max_age_hours=12)
            )

    def test_fresh_file_is_not_stale(self):
        with TemporaryDirectory() as tmp:
            path = self._aged_cache(tmp, 0)
            self.assertFalse(is_stale(path, now=datetime.now(), max_age_hours=12))

    def test_just_under_limit_is_not_stale(self):
        with TemporaryDirectory() as tmp:
            path = self._aged_cache(tmp, 11.5)
            self.assertFalse(is_stale(path, now=datetime.now(), max_age_hours=12))

    def test_just_over_limit_is_stale(self):
        with TemporaryDirectory() as tmp:
            path = self._aged_cache(tmp, 12.5)
            self.assertTrue(is_stale(path, now=datetime.now(), max_age_hours=12))

    def test_boundary_counts_as_stale(self):
        with TemporaryDirectory() as tmp:
            path = self._aged_cache(tmp, 12)
            self.assertTrue(is_stale(path, now=datetime.now(), max_age_hours=12))

    def test_zero_hours_means_present_is_enough(self):
        with TemporaryDirectory() as tmp:
            path = self._aged_cache(tmp, 100)
            self.assertFalse(is_stale(path, now=datetime.now(), max_age_hours=0))

    def test_zero_hours_still_stale_when_missing(self):
        with TemporaryDirectory() as tmp:
            self.assertTrue(
                is_stale(Path(tmp) / "nope.json", now=datetime.now(), max_age_hours=0)
            )


class TestUrlTemplate(unittest.TestCase):
    def test_year_placeholder_replaced(self):
        self.assertEqual(
            render_url_template("https://x.test/{year}.json", 2026),
            "https://x.test/2026.json",
        )

    def test_multiple_placeholders(self):
        self.assertEqual(render_url_template("/{year}/{year}.json", 2030), "/2030/2030.json")

    def test_without_placeholder_unchanged(self):
        self.assertEqual(render_url_template("https://x.test/cal.json", 2026),
                         "https://x.test/cal.json")

    def test_empty(self):
        self.assertEqual(render_url_template("", 2026), "")


if __name__ == "__main__":
    unittest.main()
