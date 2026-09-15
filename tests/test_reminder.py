"""提醒到期判定与文案渲染测试。

这里的用例直接对应需求里的核心行为：
「默认提前 20 分钟提醒」「提前量可自定义」「不重复提醒」。
"""

import unittest
from datetime import datetime, timedelta

import _bootstrap  # noqa: F401  —— 注册插件包

from class_schedule.ics_parser import CourseEvent
from class_schedule.reminder import (
    DEFAULT_TEMPLATE,
    collect_due,
    render_message,
    reminder_key,
    upcoming_events,
)

NOW = datetime(2026, 9, 1, 8, 0)


def course(minutes_from_now: float, *, name="高等数学", location="教三-201", uid="c1"):
    """构造一门在 ``NOW`` 之后 N 分钟开始的课。"""
    start = NOW + timedelta(minutes=minutes_from_now)
    return CourseEvent(
        uid=uid,
        summary=name,
        start=start,
        end=start + timedelta(minutes=100),
        location=location,
    )


def due_of(events, *, lead=20, grace=60, fired=None):
    return collect_due(
        events,
        now=NOW,
        lead_minutes=lead,
        late_grace_seconds=grace,
        fired=fired if fired is not None else set(),
    )


class TestCollectDue(unittest.TestCase):
    def test_exact_lead_time_is_due(self):
        """默认 20 分钟提前量：正好 20 分钟后上课要提醒。"""
        got = due_of([course(20)])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].minutes_until, 20)

    def test_outside_lead_window_is_not_due(self):
        self.assertEqual(due_of([course(25)]), [])

    def test_slightly_late_tick_rounds_up_to_configured_lead(self):
        """tick 落在 19 分 5 秒时显示 20 分钟，与用户设置一致。"""
        got = due_of([course(19 + 5 / 60)])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].minutes_until, 20)

    def test_catch_up_before_class_starts(self):
        """机器人重启后仍能在课开始前补发（提前量已过但课没开始）。"""
        got = due_of([course(3)])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].minutes_until, 3)

    def test_grace_allows_just_started_class(self):
        got = due_of([course(-0.5)], grace=60)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].minutes_until_display, 0)

    def test_started_beyond_grace_is_skipped(self):
        """课已开始超过宽限时间，不再补发（避免导入课表后轰炸历史课）。"""
        self.assertEqual(due_of([course(-5)], grace=60), [])

    def test_lead_zero_reminds_at_class_start(self):
        got = due_of([course(0)], lead=0)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].minutes_until_display, 0)

    def test_lead_zero_does_not_remind_early(self):
        self.assertEqual(due_of([course(30)], lead=0), [])

    def test_custom_lead_minutes_respected(self):
        """自定义提前量为 5 分钟时，20 分钟后的课还不到提醒时机。"""
        self.assertEqual(due_of([course(20)], lead=5), [])
        self.assertEqual(len(due_of([course(5)], lead=5)), 1)

    def test_already_fired_is_skipped(self):
        event = course(20)
        key = reminder_key(event, event.start, 20)
        self.assertEqual(due_of([event], fired={key}), [])
        self.assertEqual(len(due_of([event], fired={})), 1)

    def test_changing_lead_rearms_reminder(self):
        """改了提前量后按新提前量重新提醒一次（去重键含提前量）。"""
        event = course(10)
        old_key = reminder_key(event, event.start, 20)
        self.assertEqual(due_of([event], lead=20, fired={old_key}), [])
        self.assertEqual(len(due_of([event], lead=10, fired={old_key})), 1)

    def test_multiple_courses_sorted_by_start(self):
        got = due_of([course(15, name="B", uid="b"), course(8, name="A", uid="a")])
        self.assertEqual([item.event.summary for item in got], ["A", "B"])

    def test_duplicate_events_in_same_tick_dedupe(self):
        """回归：同一节课出现两次（同一份课表被导入两次）只提醒一次。

        跨文件重复是必然发生的：`_write_unique` 刻意不覆盖已有课表，
        所以重复导入会生成 cal-2.ics，里面是同 UID 同时间的课。
        """
        first = course(20, uid="dup")
        second = course(20, uid="dup")
        got = due_of([first, second])
        self.assertEqual(len(got), 1)

    def test_distinct_courses_same_time_both_remind(self):
        """不同课程即使同一时间开始也都要提醒。"""
        got = due_of([course(20, uid="a", name="数学"), course(20, uid="b", name="英语")])
        self.assertEqual(len(got), 2)

    def test_all_day_events_ignored(self):
        start = NOW.replace(hour=0, minute=0)
        all_day = CourseEvent(uid="d", summary="校庆", start=start, all_day=True)
        self.assertEqual(due_of([all_day]), [])

    def test_recurring_course_triggers_on_occurrence(self):
        """带重复规则的课：在每次课开始前都要提醒。"""
        # 每周二 08:20 的课，NOW 是 2026-09-01(周二) 08:00 -> 20 分钟后正是本次
        recurring = CourseEvent(
            uid="r",
            summary="英语",
            start=datetime(2026, 8, 25, 8, 20),
            end=datetime(2026, 8, 25, 10, 0),
            rrule="FREQ=WEEKLY;BYDAY=TU",
            location="外语楼",
        )
        got = due_of([recurring])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].start, datetime(2026, 9, 1, 8, 20))


class TestUpcomingEvents(unittest.TestCase):
    def test_window_filtering(self):
        events = [course(10, uid="a"), course(60 * 24 * 3, uid="b")]
        got = upcoming_events(events, start=NOW, end=NOW + timedelta(days=1))
        self.assertEqual([item.uid for item in got], ["a"])

    def test_limit_applied(self):
        events = [course(10 * index, uid=f"c{index}") for index in range(1, 6)]
        got = upcoming_events(
            events, start=NOW, end=NOW + timedelta(days=1), limit=2
        )
        self.assertEqual(len(got), 2)


class TestRenderMessage(unittest.TestCase):
    def test_default_template_contains_key_facts(self):
        reminder = due_of([course(20)])[0]
        text = render_message(reminder)
        self.assertIn("20 分钟后上课", text)
        self.assertIn("高等数学", text)
        self.assertIn("08:20-10:00", text)
        self.assertIn("教三-201", text)

    def test_missing_location_has_no_empty_icon_line(self):
        reminder = due_of([course(20, location="")])[0]
        text = render_message(reminder)
        self.assertNotIn("📍", text)
        self.assertNotIn("\n\n", text)

    def test_started_class_says_immediately(self):
        reminder = due_of([course(-0.5)], grace=60)[0]
        self.assertIn("马上上课", render_message(reminder))

    def test_custom_template(self):
        reminder = due_of([course(20)])[0]
        text = render_message(reminder, "【{weekday}】{start} {course}")
        self.assertIn("【星期二】", text)
        self.assertIn("08:20", text)

    def test_unknown_placeholder_is_left_alone(self):
        reminder = due_of([course(20)])[0]
        text = render_message(reminder, "{course} {nothing_here}")
        self.assertIn("高等数学", text)
        self.assertIn("{nothing_here}", text)

    def test_empty_template_falls_back_to_default(self):
        reminder = due_of([course(20)])[0]
        self.assertEqual(render_message(reminder, "   "), render_message(reminder, DEFAULT_TEMPLATE))

    def test_missing_end_renders_single_time(self):
        event = course(20)
        event.end = None
        reminder = due_of([event])[0]
        self.assertIn("08:20", render_message(reminder))

    def test_no_trailing_blank_lines(self):
        reminder = due_of([course(20)])[0]
        text = render_message(reminder)
        self.assertEqual(text, text.strip())

    def test_overlong_course_name_truncated(self):
        """课程名来自第三方 ics，超长时不能把消息撑爆。"""
        reminder = due_of([course(20, name="超长课程名" * 100)])[0]
        text = render_message(reminder)
        self.assertLess(len(text), 400)
        self.assertIn("…", text)

    def test_newlines_in_fields_collapsed(self):
        """ics 里的换行（\\n 转义）不能把提醒拆成乱七八糟的多行。"""
        event = course(20, name="数学\n第二讲", location="教三\n201")
        reminder = due_of([event])[0]
        text = render_message(reminder)
        self.assertNotIn("数学\n第二讲", text)
        self.assertIn("数学 第二讲", text)

    def test_whitespace_only_location_treated_as_empty(self):
        reminder = due_of([course(20, location="   ")])[0]
        self.assertNotIn("📍", render_message(reminder))


class TestReminderKey(unittest.TestCase):
    def test_key_includes_lead_minutes(self):
        event = course(20)
        self.assertNotEqual(
            reminder_key(event, event.start, 20), reminder_key(event, event.start, 10)
        )

    def test_key_stable_for_same_inputs(self):
        event = course(20)
        self.assertEqual(
            reminder_key(event, event.start, 20), reminder_key(event, event.start, 20)
        )

    def test_key_falls_back_to_name_without_uid(self):
        event = course(20, uid="")
        self.assertIn("高等数学", reminder_key(event, event.start, 20))


if __name__ == "__main__":
    unittest.main()
