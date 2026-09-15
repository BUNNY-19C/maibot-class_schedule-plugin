"""ICS 解析测试：折行、转义、时间口径、RRULE/EXDATE、调课覆盖。"""

import unittest
from datetime import date, datetime, timedelta, timezone

import _bootstrap  # noqa: F401  —— 注册插件包，必须在导入插件模块之前

from class_schedule import ics_parser
from class_schedule.ics_parser import (
    MAX_OCCURRENCES_PER_WINDOW,
    IcsParseError,
    expand_occurrences,
    parse_ics,
    parse_ics_many,
    parse_ics_with_warnings,
)


def wrap(*vevent_fields: str) -> str:
    """拼一份最小的 VCALENDAR 文本。"""
    body = "\n".join(vevent_fields)
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//test//CN\r\n"
        "BEGIN:VEVENT\r\n"
        f"{body}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


BASIC = wrap(
    "UID:course-1",
    "SUMMARY:高等数学",
    "DTSTART;TZID=Asia/Shanghai:20260901T080000",
    "DTEND;TZID=Asia/Shanghai:20260901T094000",
    "LOCATION:教三-201",
    "DESCRIPTION:教师：张三",
)


class TestBasicParsing(unittest.TestCase):
    def test_single_event_fields(self):
        events = parse_ics(BASIC, source="a.ics")
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.uid, "course-1")
        self.assertEqual(event.summary, "高等数学")
        self.assertEqual(event.location, "教三-201")
        self.assertEqual(event.description, "教师：张三")
        self.assertEqual(event.source, "a.ics")
        self.assertEqual(event.start, datetime(2026, 9, 1, 8, 0))
        self.assertEqual(event.end, datetime(2026, 9, 1, 9, 40))
        self.assertEqual(event.duration, timedelta(minutes=100))
        self.assertFalse(event.all_day)

    def test_tzid_uses_wall_clock(self):
        """带 TZID 的时间按本地墙上时间取用（不做时区换算）。"""
        events = parse_ics(BASIC)
        self.assertEqual(events[0].start.hour, 8)

    def test_utc_value_is_converted_to_local(self):
        text = wrap(
            "UID:u1",
            "SUMMARY:线上课",
            "DTSTART:20260901T000000Z",
            "DTEND:20260901T010000Z",
        )
        expected = (
            datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
            .astimezone()
            .replace(tzinfo=None)
        )
        self.assertEqual(parse_ics(text)[0].start, expected)

    def test_all_day_event(self):
        text = wrap("UID:d1", "SUMMARY:校庆", "DTSTART;VALUE=DATE:20260901")
        event = parse_ics(text)[0]
        self.assertTrue(event.all_day)
        self.assertEqual(event.start, datetime(2026, 9, 1, 0, 0))

    def test_all_day_not_expanded(self):
        """全天事件不参与上课提醒，展开结果为空。"""
        event = parse_ics(wrap("UID:d1", "SUMMARY:校庆", "DTSTART;VALUE=DATE:20260901"))[0]
        window_start = datetime(2026, 8, 1)
        window_end = datetime(2026, 10, 1)
        self.assertEqual(expand_occurrences(event, window_start, window_end), [])

    def test_missing_summary_falls_back(self):
        text = wrap("UID:u1", "DTSTART:20260901T080000")
        self.assertEqual(parse_ics(text)[0].display_name, "未命名课程")

    def test_empty_content_raises(self):
        with self.assertRaises(IcsParseError):
            parse_ics("")
        with self.assertRaises(IcsParseError):
            parse_ics("BEGIN:VCALENDAR\nEND:VCALENDAR\n")

    def test_missing_dtstart_event_skipped(self):
        """单条坏事件被跳过，不影响同文件其他事件。"""
        text = (
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\nUID:bad\nSUMMARY:没有开始时间\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:ok\nSUMMARY:正常课\nDTSTART:20260901T080000\nEND:VEVENT\n"
            "END:VCALENDAR\n"
        )
        events = parse_ics(text)
        self.assertEqual([event.uid for event in events], ["ok"])

    def test_local_datetime_with_utc_offset(self):
        """RFC 5545 允许 20260901T080000+0800 写法，按墙上时间取用。"""
        text = wrap("UID:u1", "SUMMARY:带偏移", "DTSTART:20260901T080000+0800")
        self.assertEqual(parse_ics(text)[0].start, datetime(2026, 9, 1, 8, 0))

    def test_utc_offset_with_minutes(self):
        text = wrap("UID:u1", "SUMMARY:带偏移", "DTSTART:20260901T0800+0800")
        self.assertEqual(parse_ics(text)[0].start, datetime(2026, 9, 1, 8, 0))

    def test_malformed_line_does_not_kill_whole_file(self):
        """回归：混入一行非 ICS 文本不该让整份课表报废。"""
        text = (
            "BEGIN:VCALENDAR\n"
            "这是一行说明文字没有冒号\n"
            "BEGIN:VEVENT\n"
            "UID:ok1\nSUMMARY:第一节课\nDTSTART:20260901T080000\nEND:VEVENT\n"
            "BEGIN:VEVENT\n"
            "UID:ok2\nSUMMARY:第二节课\nDTSTART:20260901T100000\nEND:VEVENT\n"
            "END:VCALENDAR\n"
        )
        events = parse_ics(text)
        self.assertEqual([event.uid for event in events], ["ok1", "ok2"])

    def test_only_malformed_lines_still_raises(self):
        text = "BEGIN:VCALENDAR\n随便一行\nBEGIN:VEVENT\nEND:VEVENT\nEND:VCALENDAR\n"
        with self.assertRaises(IcsParseError):
            parse_ics(text)


class TestTextLayer(unittest.TestCase):
    def test_folded_line_is_unfolded(self):
        text = (
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            "UID:u1\n"
            "SUMMARY:高等数\n"
            " 学\n"
            "DTSTART:20260901T080000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        self.assertEqual(parse_ics(text)[0].summary, "高等数学")

    def test_escapes_are_restored(self):
        text = wrap(
            "UID:u1",
            r"SUMMARY:数学\, 上册\n第二讲",
            r"LOCATION:教三\;201",
            "DTSTART:20260901T080000",
        )
        event = parse_ics(text)[0]
        self.assertEqual(event.summary, "数学, 上册\n第二讲")
        self.assertEqual(event.location, "教三;201")

    def test_quoted_param_with_colon(self):
        """参数值里的冒号不能把属性名切错。"""
        text = wrap(
            'UID:u1',
            'ATTENDEE;CN="张三:老师":mailto:a@b.c',
            "SUMMARY:语文",
            "DTSTART:20260901T080000",
        )
        self.assertEqual(parse_ics(text)[0].summary, "语文")

    def test_lf_only_and_crlf_both_work(self):
        crlf = BASIC
        lf = BASIC.replace("\r\n", "\n")
        self.assertEqual(parse_ics(crlf)[0].start, parse_ics(lf)[0].start)


class TestRecurrence(unittest.TestCase):
    def test_weekly_byday_count(self):
        text = wrap(
            "UID:r1",
            "SUMMARY:英语",
            "DTSTART:20260901T080000",
            "RRULE:FREQ=WEEKLY;BYDAY=TU,TH;COUNT=4",
        )
        event = parse_ics(text)[0]
        got = expand_occurrences(
            event, datetime(2026, 8, 1), datetime(2026, 10, 1)
        )
        self.assertEqual(len(got), 4)
        self.assertEqual({item.start.weekday() for item in got}, {1, 3})

    def test_until_with_utc_z_does_not_crash(self):
        """回归：naive DTSTART + UTC UNTIL 曾被 dateutil 直接拒绝。"""
        text = wrap(
            "UID:r2",
            "SUMMARY:物理",
            "DTSTART:20260901T100000",
            "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261001T000000Z",
        )
        event = parse_ics(text)[0]
        got = expand_occurrences(event, datetime(2026, 8, 1), datetime(2027, 1, 1))
        self.assertGreaterEqual(len(got), 3)
        self.assertTrue(all(item.start <= datetime(2026, 10, 2) for item in got))

    def test_until_as_date_only(self):
        text = wrap(
            "UID:r3",
            "SUMMARY:化学",
            "DTSTART:20260903T140000",
            "RRULE:FREQ=WEEKLY;BYDAY=TH;UNTIL=20261001",
        )
        event = parse_ics(text)[0]
        got = expand_occurrences(event, datetime(2026, 8, 1), datetime(2027, 1, 1))
        self.assertEqual(len(got), 4)

    def test_biweekly_interval(self):
        text = wrap(
            "UID:r4",
            "SUMMARY:双周课",
            "DTSTART:20260901T100000",
            "RRULE:FREQ=WEEKLY;BYDAY=TU;INTERVAL=2",
        )
        event = parse_ics(text)[0]
        got = expand_occurrences(event, datetime(2026, 9, 1), datetime(2026, 10, 31))
        stamps = [item.start.strftime("%Y-%m-%d") for item in got]
        self.assertEqual(stamps, ["2026-09-01", "2026-09-15", "2026-09-29", "2026-10-13", "2026-10-27"])

    def test_infinite_rule_bounded_by_window(self):
        text = wrap(
            "UID:r5",
            "SUMMARY:无限重复",
            "DTSTART:20260901T080000",
            "RRULE:FREQ=WEEKLY;BYDAY=TU",
        )
        event = parse_ics(text)[0]
        got = expand_occurrences(event, datetime(2026, 9, 1), datetime(2026, 9, 30))
        self.assertEqual(len(got), 5)

    def test_exdate_excludes_occurrence(self):
        text = wrap(
            "UID:r6",
            "SUMMARY:国庆放假",
            "DTSTART:20260901T080000",
            "RRULE:FREQ=WEEKLY;BYDAY=TU",
            "EXDATE;TZID=Asia/Shanghai:20260915T080000",
        )
        event = parse_ics(text)[0]
        got = expand_occurrences(event, datetime(2026, 9, 1), datetime(2026, 9, 30))
        stamps = [item.start.strftime("%Y-%m-%d") for item in got]
        self.assertNotIn("2026-09-15", stamps)
        self.assertIn("2026-09-08", stamps)

    def test_multiple_exdate_lines_merge(self):
        text = (
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            "UID:r7\n"
            "SUMMARY:多行EXDATE\n"
            "DTSTART:20260901T080000\n"
            "RRULE:FREQ=WEEKLY;BYDAY=TU\n"
            "EXDATE:20260908T080000\n"
            "EXDATE:20260915T080000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        event = parse_ics(text)[0]
        got = expand_occurrences(event, datetime(2026, 9, 1), datetime(2026, 9, 30))
        stamps = {item.start.strftime("%Y-%m-%d") for item in got}
        self.assertNotIn("2026-09-08", stamps)
        self.assertNotIn("2026-09-15", stamps)
        self.assertIn("2026-09-22", stamps)

    def test_expanded_event_has_shifted_end(self):
        text = wrap(
            "UID:r8",
            "SUMMARY:两节连堂",
            "DTSTART:20260901T080000",
            "DTEND:20260901T094000",
            "RRULE:FREQ=WEEKLY;BYDAY=TU",
        )
        event = parse_ics(text)[0]
        got = expand_occurrences(event, datetime(2026, 9, 8), datetime(2026, 9, 9))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].start, datetime(2026, 9, 8, 8, 0))
        self.assertEqual(got[0].end, datetime(2026, 9, 8, 9, 40))


class TestOverrides(unittest.TestCase):
    def test_recurrence_id_replaces_occurrence(self):
        text = (
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            "UID:o1\n"
            "SUMMARY:数据结构\n"
            "DTSTART:20260907T080000\n"
            "DTEND:20260907T094000\n"
            "RRULE:FREQ=WEEKLY;BYDAY=MO\n"
            "END:VEVENT\n"
            "BEGIN:VEVENT\n"
            "UID:o1\n"
            "RECURRENCE-ID;TZID=Asia/Shanghai:20260914T080000\n"
            "SUMMARY:数据结构（调课）\n"
            "DTSTART;TZID=Asia/Shanghai:20260914T100000\n"
            "DTEND;TZID=Asia/Shanghai:20260914T114000\n"
            "LOCATION:教五-101\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        events = parse_ics(text)
        self.assertEqual(len(events), 1)
        event = events[0]

        got = expand_occurrences(event, datetime(2026, 9, 14), datetime(2026, 9, 15))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].start, datetime(2026, 9, 14, 10, 0))
        self.assertEqual(got[0].summary, "数据结构（调课）")
        self.assertEqual(got[0].location, "教五-101")

    def test_override_moved_out_of_window_is_dropped(self):
        """调课挪到时间窗外的，这次课不再出现在结果里。"""
        text = (
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            "UID:o2\n"
            "SUMMARY:概率论\n"
            "DTSTART:20260907T080000\n"
            "RRULE:FREQ=WEEKLY;BYDAY=MO\n"
            "END:VEVENT\n"
            "BEGIN:VEVENT\n"
            "UID:o2\n"
            "RECURRENCE-ID;TZID=Asia/Shanghai:20260914T080000\n"
            "SUMMARY:概率论\n"
            "DTSTART;TZID=Asia/Shanghai:20260916T080000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        event = parse_ics(text)[0]
        just_the_14th = expand_occurrences(
            event, datetime(2026, 9, 14), datetime(2026, 9, 14, 23, 59)
        )
        self.assertEqual(just_the_14th, [])
        the_16th = expand_occurrences(
            event, datetime(2026, 9, 16), datetime(2026, 9, 16, 23, 59)
        )
        self.assertEqual([item.start for item in the_16th], [datetime(2026, 9, 16, 8, 0)])

    def test_duplicate_uid_without_recurrence_id_last_wins(self):
        text = (
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\nUID:dup\nSUMMARY:旧标题\nDTSTART:20260901T080000\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:dup\nSUMMARY:新标题\nDTSTART:20260901T080000\nEND:VEVENT\n"
            "END:VCALENDAR\n"
        )
        events = parse_ics(text)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].summary, "新标题")

    def test_orphan_override_becomes_single_event(self):
        text = (
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            "UID:orphan\n"
            "RECURRENCE-ID;TZID=Asia/Shanghai:20260914T080000\n"
            "SUMMARY:孤立调课\n"
            "DTSTART;TZID=Asia/Shanghai:20260914T080000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        events = parse_ics(text)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].summary, "孤立调课")
        self.assertEqual(events[0].rrule, "")


class TestParseMany(unittest.TestCase):
    def test_one_bad_file_does_not_break_others(self):
        events, errors = parse_ics_many(
            {"good.ics": BASIC, "bad.ics": "这不是课表", "empty.ics": ""}
        )
        self.assertEqual([event.uid for event in events], ["course-1"])
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("bad.ics" in item for item in errors))


class TestNestedComponents(unittest.TestCase):
    """回归：VEVENT 里的子组件（VALARM）不能覆盖外层属性。

    Google / Apple / Outlook 导出的日历都带 VALARM，而 VALARM 自己也有
    SUMMARY、DESCRIPTION。以前这些行会被当成课程属性、把课程名整个换掉
    （实测课程名变成 "Alarm summary"）。
    """

    def test_valarm_properties_do_not_override_event(self):
        text = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:alarm-1\n"
            "SUMMARY:高等数学\nDESCRIPTION:教师-张三\n"
            "DTSTART:20260901T080000\nDTEND:20260901T094000\n"
            "LOCATION:教三-201\n"
            "BEGIN:VALARM\nACTION:EMAIL\nTRIGGER:-PT20M\n"
            "SUMMARY:Alarm summary\nDESCRIPTION:This is an event reminder\n"
            "END:VALARM\n"
            "END:VEVENT\nEND:VCALENDAR\n"
        )
        event = parse_ics(text)[0]
        self.assertEqual(event.summary, "高等数学")
        self.assertEqual(event.description, "教师-张三")
        self.assertEqual(event.location, "教三-201")
        self.assertEqual(event.start, datetime(2026, 9, 1, 8, 0))

    def test_alarm_inside_recurring_event_keeps_rule(self):
        """子组件的 END 不能把事件提前结掉，否则 RRULE 会丢。"""
        text = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:alarm-2\n"
            "SUMMARY:线性代数\nDTSTART:20260901T080000\nDTEND:20260901T094000\n"
            "RRULE:FREQ=WEEKLY;BYDAY=TU\n"
            "BEGIN:VALARM\nACTION:DISPLAY\nTRIGGER:-PT10M\n"
            "DESCRIPTION:提醒\nEND:VALARM\n"
            "END:VEVENT\nEND:VCALENDAR\n"
        )
        event = parse_ics(text)[0]
        self.assertEqual(event.rrule, "FREQ=WEEKLY;BYDAY=TU")
        occurrences = expand_occurrences(
            event, datetime(2026, 9, 6), datetime(2026, 9, 20)
        )
        self.assertEqual(
            [item.start.strftime("%m-%d") for item in occurrences], ["09-08", "09-15"]
        )

    def test_multiple_alarms_do_not_leak(self):
        text = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:alarm-3\n"
            "SUMMARY:大学物理\nDTSTART:20260901T080000\nDTEND:20260901T094000\n"
            "BEGIN:VALARM\nTRIGGER:-PT30M\nSUMMARY:提前半小时\nEND:VALARM\n"
            "BEGIN:VALARM\nTRIGGER:-PT5M\nSUMMARY:提前五分钟\nEND:VALARM\n"
            "END:VEVENT\nEND:VCALENDAR\n"
        )
        self.assertEqual(parse_ics(text)[0].summary, "大学物理")


class TestDateOnlyExdate(unittest.TestCase):
    """回归：``EXDATE;VALUE=DATE`` 对定时重复事件要能排掉那次课。

    教务系统常用日期型 EXDATE 标注停课，解析出来是当天 00:00，
    与 08:00 的那次课精确比较永远匹配不上，于是照旧提醒。
    """

    def _weekly(self, exdate_line: str) -> str:
        return (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:ex-1\nSUMMARY:线性代数\n"
            "DTSTART:20260901T080000\nDTEND:20260901T094000\n"
            "RRULE:FREQ=WEEKLY;BYDAY=TU\n"
            f"{exdate_line}\n"
            "END:VEVENT\nEND:VCALENDAR\n"
        )

    def _occurrences(self, text: str, start: datetime, days: int) -> list[str]:
        event = parse_ics(text)[0]
        return [
            item.start.strftime("%m-%d")
            for item in expand_occurrences(event, start, start + timedelta(days=days))
        ]

    def test_value_date_excludes_that_occurrence(self):
        text = self._weekly("EXDATE;VALUE=DATE:20260915")
        self.assertEqual(self._occurrences(text, datetime(2026, 9, 14), 4), [])

    def test_bare_eight_digit_exdate_also_excludes(self):
        """裸 8 位数字写法（很多教务系统这么导）同样按日期处理。"""
        text = self._weekly("EXDATE:20260915")
        self.assertEqual(self._occurrences(text, datetime(2026, 9, 14), 4), [])

    def test_timed_exdate_still_works(self):
        text = self._weekly("EXDATE:20260915T080000")
        self.assertEqual(self._occurrences(text, datetime(2026, 9, 14), 4), [])

    def test_other_weeks_still_remind(self):
        """只排掉被排除的那一次，别误伤后面几周。"""
        text = self._weekly("EXDATE;VALUE=DATE:20260915")
        self.assertEqual(
            self._occurrences(text, datetime(2026, 9, 20), 15), ["09-22", "09-29"]
        )

    def test_multiple_date_exdates(self):
        text = self._weekly("EXDATE;VALUE=DATE:20260915,20260922")
        self.assertEqual(self._occurrences(text, datetime(2026, 9, 14), 12), [])

    def test_date_only_exdate_recorded_separately(self):
        event = parse_ics(self._weekly("EXDATE;VALUE=DATE:20260915"))[0]
        self.assertEqual(list(event.exdate_days), [date(2026, 9, 15)])
        # 精确时间型不该进 date 列表，免得把当天别的课也一起排掉
        timed = parse_ics(self._weekly("EXDATE:20260915T080000"))[0]
        self.assertEqual(timed.exdate_days, [])


class TestPathologicalRules(unittest.TestCase):
    """回归：不可信文件里的重复规则不能拖垮同步路径。

    ``FREQ=SECONDLY`` 在 7 天窗口能展开出 60 万次（实测 1.8 秒、数百 MB），
    而展开跑在提醒循环和规划器注入的同步路径上。
    """

    def setUp(self) -> None:
        # 告警去重是模块级状态（生产上正是要"只提醒一次"），
        # 测试之间必须清掉，否则后面的用例会因为前一个用例报过而什么都收不到
        ics_parser._WARNED_KEYS.clear()

    def _event(self, rule: str):
        return parse_ics(
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:path-1\nSUMMARY:病态\n"
            "DTSTART:20260901T000000\nDTEND:20260901T000100\n"
            f"RRULE:{rule}\n"
            "END:VEVENT\nEND:VCALENDAR\n"
        )[0]

    def test_per_second_rule_degrades_to_single_event(self):
        event = self._event("FREQ=SECONDLY")
        with self.assertLogs("class_schedule.ics_parser", level="WARNING") as captured:
            occurrences = expand_occurrences(
                event, datetime(2026, 9, 1), datetime(2026, 9, 8)
            )
        self.assertEqual(len(occurrences), 1)
        self.assertTrue(any("过密" in line for line in captured.output))

    def test_dense_rule_is_capped(self):
        event = self._event("FREQ=MINUTELY")
        with self.assertLogs("class_schedule.ics_parser", level="WARNING") as captured:
            occurrences = expand_occurrences(
                event, datetime(2026, 9, 1), datetime(2026, 9, 8)
            )
        self.assertEqual(len(occurrences), MAX_OCCURRENCES_PER_WINDOW)
        self.assertTrue(any("过密" in line for line in captured.output))

    def test_normal_weekly_rule_is_not_capped(self):
        event = self._event("FREQ=WEEKLY;BYDAY=TU")
        self.assertLess(
            len(expand_occurrences(event, datetime(2026, 9, 1), datetime(2026, 10, 1))),
            MAX_OCCURRENCES_PER_WINDOW,
        )

    def test_unparseable_rule_is_reported_not_silent(self):
        """回归：坏规则以前静默退化成单次课，重复课会无声消失。"""
        event = self._event("FREQ=WEEKLY;BYDAY=XX")
        with self.assertLogs("class_schedule.ics_parser", level="WARNING") as captured:
            occurrences = expand_occurrences(
                event, datetime(2026, 9, 1), datetime(2026, 9, 8)
            )
        self.assertEqual(len(occurrences), 1)
        self.assertTrue(any("无法解析" in line for line in captured.output))

    def test_same_bad_rule_warns_only_once(self):
        """坏规则每轮 tick 都会展开一次，不能每分钟刷一行日志。"""
        event = self._event("FREQ=WEEKLY;BYDAY=XX")
        with self.assertLogs("class_schedule.ics_parser", level="WARNING") as captured:
            for _ in range(5):
                expand_occurrences(event, datetime(2026, 9, 1), datetime(2026, 9, 8))
        self.assertEqual(len(captured.output), 1)


class TestParseWithWarnings(unittest.TestCase):
    def test_skipped_lines_are_reported(self):
        """有几行没读进去要能告诉用户，不能只写日志。"""
        text = wrap(
            "UID:warn-1",
            "SUMMARY:高等数学",
            "DTSTART:20260901T080000",
            "这一行不是合法的 ICS 属性",
        )
        events, warnings = parse_ics_with_warnings(text, source="warn.ics")
        self.assertEqual(len(events), 1)
        self.assertTrue(any("1 处内容无法解析" in item for item in warnings))

    def test_clean_file_has_no_warnings(self):
        events, warnings = parse_ics_with_warnings(BASIC, source="ok.ics")
        self.assertEqual(len(events), 1)
        self.assertEqual(warnings, [])

    def test_parse_ics_still_returns_events_only(self):
        events = parse_ics(BASIC, source="ok.ics")
        self.assertEqual([event.uid for event in events], ["course-1"])


if __name__ == "__main__":
    unittest.main()
