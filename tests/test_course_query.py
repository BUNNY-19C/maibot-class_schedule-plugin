"""自然语言问课：相关性判定、注入文案、item 插入。"""

import unittest
from datetime import date, datetime, timedelta

import _bootstrap  # noqa: F401  —— 注册插件包

from class_schedule.course_query import (
    build_inject_text,
    build_system_item,
    collect_item_text,
    inject_into_items,
    looks_like_schedule_question,
)
from class_schedule.ics_parser import CourseEvent, expand_occurrences

NOW = datetime(2026, 9, 15, 14, 30)  # 周二下午


def course(start: datetime, *, name="高等数学", location="教三-201", minutes=100, uid="c1"):
    return CourseEvent(
        uid=uid,
        summary=name,
        start=start,
        end=start + timedelta(minutes=minutes),
        location=location,
    )


class TestLooksLikeScheduleQuestion(unittest.TestCase):
    def test_schedule_words_hit(self):
        for text in (
            "明天有课吗",
            "这周第几节有课",
            "我的课表呢",
            "下节课在哪个教室",
            "今天要上课吗",
            "几点下课",
            "老师点名了吗",
            "明天考试吗",
            "国庆放假吗",
        ):
            with self.subTest(text=text):
                self.assertTrue(looks_like_schedule_question(text))

    def test_session_ordinal_hit(self):
        for text in ("第3节是什么", "第 3-4 节在哪", "第12节课"):
            with self.subTest(text=text):
                self.assertTrue(looks_like_schedule_question(text))

    def test_generic_chat_not_hit(self):
        """通用寒暄不能触发注入，否则每条消息都多花 token。"""
        for text in (
            "今天天气怎么样",
            "我们去吃饭吧",
            "在吗",
            "晚安",
            "这个多少钱",
            "明天见",
            "几点睡觉好",
        ):
            with self.subTest(text=text):
                self.assertFalse(looks_like_schedule_question(text))

    def test_empty_is_false(self):
        self.assertFalse(looks_like_schedule_question(""))
        self.assertFalse(looks_like_schedule_question(None))  # type: ignore[arg-type]


class TestCollectItemText(unittest.TestCase):
    def test_collects_text_parts(self):
        items = [
            {"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "你是bot"}]},
            {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "明天有课吗"}]},
        ]
        text = collect_item_text(items)
        self.assertIn("明天有课吗", text)
        self.assertIn("你是bot", text)

    def test_tolerates_unknown_shapes(self):
        self.assertEqual(collect_item_text([None, "字符串", {"parts": "bad"}]), "")
        self.assertEqual(collect_item_text([]), "")

    def test_prefers_recent_items(self):
        items = [{"parts": [{"type": "text", "text": f"消息{i}"}]} for i in range(20)]
        text = collect_item_text(items, limit=3)
        self.assertIn("消息19", text)
        self.assertNotIn("消息0", text)


class TestInjectIntoItems(unittest.TestCase):
    def test_inserted_after_last_system_item(self):
        items = [
            {"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "s1"}]},
            {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "u1"}]},
            {"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "s2"}]},
            {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "u2"}]},
        ]
        out = inject_into_items(items, "课表内容")
        self.assertEqual(len(out), 5)
        self.assertEqual(out[2]["parts"][0]["text"], "s2")  # 原有顺序不乱
        self.assertEqual(out[3]["parts"][0]["text"], "课表内容")  # 插在 s2 之后
        self.assertEqual(out[4]["parts"][0]["text"], "u2")

    def test_prepended_when_no_system_item(self):
        out = inject_into_items([{"item_type": "UserMessageItem", "parts": []}], "课表")
        self.assertEqual(out[0]["parts"][0]["text"], "课表")

    def test_inserted_item_shape(self):
        item = build_system_item("课表", now=NOW)
        self.assertEqual(item["item_type"], "SystemMessageItem")
        self.assertEqual(item["parts"], [{"type": "text", "text": "课表"}])
        self.assertIn("item_id", item["meta"])
        self.assertIn("timestamp", item["meta"])

    def test_blank_text_is_noop(self):
        items = [{"item_type": "UserMessageItem"}]
        self.assertEqual(len(inject_into_items(items, "   ")), 1)

    def test_original_list_not_mutated(self):
        items = [{"item_type": "UserMessageItem"}]
        inject_into_items(items, "课表")
        self.assertEqual(len(items), 1)


class TestBuildInjectText(unittest.TestCase):
    def _build(self, events, **kwargs):
        kwargs.setdefault("days", 3)
        kwargs.setdefault("max_lines", 12)
        return build_inject_text(events, now=NOW, expander=expand_occurrences, **kwargs)

    def test_lists_upcoming_with_day_labels(self):
        events = [
            course(NOW + timedelta(hours=2), name="工程创新训练", location="实训楼"),
            course(NOW + timedelta(days=1, hours=-4), name="机械设计", location="B207", uid="c2"),
        ]
        text = self._build(events)
        self.assertIn("今天", text)
        self.assertIn("明天", text)
        self.assertIn("工程创新训练", text)
        self.assertIn("@实训楼", text)
        self.assertIn("机械设计", text)
        self.assertIn("不要编造", text)  # 明确禁止编造

    def test_rrule_event_skipped_without_expander(self):
        """回归：没传 expander 时，带 RRULE 的课不能被当成"首次课时间"照搬。

        这类事件的 start 是首周那节课，照搬会印出早已过去的日期，
        随后又被"已上完"过滤掉，结果整门重复课从回答里静默消失。
        宁可少列，也不要给模型一个错的日期。
        """
        started = NOW - timedelta(days=21)  # 三周前开课，每周一次
        weekly = CourseEvent(
            uid="c1",
            summary="高等数学",
            start=started,
            end=started + timedelta(minutes=100),
            rrule="FREQ=WEEKLY;COUNT=16",
        )
        once = course(NOW + timedelta(hours=2), name="随堂测验", uid="c2")

        text = build_inject_text([weekly, once], now=NOW, days=3, expander=None)
        self.assertIn("随堂测验", text)
        self.assertNotIn("高等数学", text)
        self.assertNotIn("08-25", text)  # 首周那节课的日期也不能冒出来

        # 传了 expander 就必须列出来（这才是生产路径）
        with_expander = self._build([weekly, once])
        self.assertIn("高等数学", with_expander)

    def test_finished_classes_skipped(self):
        """已经上完的课不该出现在"接下来"里。"""
        event = course(NOW - timedelta(hours=5), name="早课")
        text = self._build([event])
        self.assertNotIn("早课", text)
        self.assertIn("没有排课", text)

    def test_no_courses_says_so(self):
        text = self._build([])
        self.assertIn("没有排课", text)

    def test_recurring_expanded(self):
        # 每周二 16:00 的课：NOW(周二 14:30) 时今天这次还没开始
        weekly = CourseEvent(
            uid="r1",
            summary="英语",
            start=datetime(2026, 9, 1, 16, 0),
            end=datetime(2026, 9, 1, 17, 40),
            rrule="FREQ=WEEKLY;BYDAY=TU",
            location="外语楼",
        )
        text = self._build([weekly])
        self.assertIn("英语", text)
        self.assertIn("16:00", text)  # 展开到了今天这次
        self.assertIn("今天", text)

    def test_max_lines_truncates(self):
        events = [course(NOW + timedelta(hours=i + 1), name=f"课{i}", uid=f"c{i}") for i in range(20)]
        text = self._build(events, max_lines=3)
        self.assertIn("仅列出前 3 条", text)

    def test_holiday_label_included(self):
        events = [course(NOW + timedelta(days=1, hours=-4), uid="c2")]
        text = self._build(events, holiday_label=lambda day: "中秋节放假" if day == date(2026, 9, 16) else "")
        self.assertIn("中秋节放假", text)

    def test_days_window_respected(self):
        far = course(NOW + timedelta(days=10), name="十天后的课")
        text = self._build([far], days=3)
        self.assertNotIn("十天后的课", text)

    def test_zero_days_returns_empty(self):
        self.assertEqual(self._build([course(NOW + timedelta(hours=1))], days=0), "")


if __name__ == "__main__":
    unittest.main()
