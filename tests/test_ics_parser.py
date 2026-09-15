"""ICS 解析测试：折行、转义、时间口径、RRULE/EXDATE、调课覆盖。"""

import unittest
from datetime import datetime, timedelta, timezone

import _bootstrap  # noqa: F401  —— 注册插件包，必须在导入插件模块之前

from class_schedule.ics_parser import (
    IcsParseError,
    expand_occurrences,
    parse_ics,
    parse_ics_many,
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


if __name__ == "__main__":
    unittest.main()
