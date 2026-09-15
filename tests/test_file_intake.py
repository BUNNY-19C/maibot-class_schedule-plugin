"""聊天文件导入测试：消息段提取、命名收敛、去重、类型识别。

这一层吃的是**适配器给的原始消息段**，字段名与嵌套层数都不统一
（实测 SnowLuma 会多包一层 ``{"type":"dict","data":{...}}``），
所以覆盖 dict / 嵌套 / 同名多文件 / 缺字段几种形态。
"""

import unittest

import _bootstrap  # noqa: F401  —— 注册插件包

from class_schedule.file_intake import (
    chat_import_filename,
    content_fingerprint,
    extract_file_candidates,
    is_schedule_filename,
)


def file_segment(
    *,
    name: str = "课表.ics",
    base64_data: str = "",
    url: str = "",
    file_id: str = "",
    mime_type: str = "",
    nested: bool = True,
) -> dict:
    """构造一个文件消息段（默认用 SnowLuma 的嵌套包法）。"""
    payload: dict = {"name": name, "size": "230"}
    if base64_data:
        payload["base64"] = base64_data
    if url:
        payload["url"] = url
    if file_id:
        payload["file_id"] = file_id
    if mime_type:
        payload["mime_type"] = mime_type
    segment = {"type": "file", "data": payload}
    return {"type": "dict", "data": segment} if nested else segment


def message_with(*segments: dict) -> dict:
    return {
        "message_id": "m-file-1",
        "session_id": "chat-stream",
        "raw_message": list(segments),
    }


class TestExtractFileCandidates(unittest.TestCase):
    def test_extracts_from_nested_segment(self):
        found = extract_file_candidates(message_with(file_segment()))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].name, "课表.ics")
        self.assertEqual(found[0].message_id, "m-file-1")

    def test_extracts_from_flat_segment(self):
        found = extract_file_candidates(
            message_with(file_segment(nested=False, url="https://x.test/a.ics"))
        )
        self.assertEqual([item.url for item in found], ["https://x.test/a.ics"])

    def test_non_file_segments_are_ignored(self):
        message = message_with(
            {"type": "text", "data": {"text": "你好"}},
            {"type": "image", "data": {"url": "https://x.test/a.png"}},
            file_segment(),
        )
        found = extract_file_candidates(message)
        self.assertEqual([item.name for item in found], ["课表.ics"])

    def test_same_file_twice_is_deduped(self):
        """同一条消息里同一份文件重复出现（适配器偶发）只留一个。"""
        segment = file_segment(base64_data="QUJD")
        found = extract_file_candidates(message_with(segment, dict(segment)))
        self.assertEqual(len(found), 1)

    def test_same_name_different_content_is_kept(self):
        """回归：同名但内容不同的两份课表曾被静默丢掉一份。

        适配器对多文件消息可能复用同一个文件名，只按名字去重会让用户
        以为两份都导入了，实际只导了第一份。
        """
        found = extract_file_candidates(
            message_with(
                file_segment(base64_data="QUJD"),        # "ABC"
                file_segment(base64_data="WFla"),        # "XYZ"
            )
        )
        self.assertEqual(len(found), 2)
        self.assertEqual([item.base64_data for item in found], ["QUJD", "WFla"])

    def test_same_name_different_url_is_kept(self):
        found = extract_file_candidates(
            message_with(
                file_segment(url="https://x.test/a.ics"),
                file_segment(url="https://x.test/b.ics"),
            )
        )
        self.assertEqual(len(found), 2)

    def test_mime_type_is_read(self):
        """麦麦的 FileComponent 带 mime_type，要读到才能认出没后缀的日历文件。"""
        found = extract_file_candidates(
            message_with(file_segment(name="课表", mime_type="text/calendar"))
        )
        self.assertEqual(found[0].mime_type, "text/calendar")

    def test_no_message_or_no_segments(self):
        self.assertEqual(extract_file_candidates(None), [])
        self.assertEqual(extract_file_candidates({}), [])
        self.assertEqual(extract_file_candidates(message_with()), [])

    def test_depth_limit_stops_runaway_nesting(self):
        """嵌套层数有上限，别被畸形结构拖死。"""
        node: dict = file_segment()
        for _ in range(12):
            node = {"type": "dict", "data": node}
        self.assertEqual(extract_file_candidates(message_with(node)), [])

    def test_segment_without_content_but_with_name_is_reported(self):
        """有名字没内容也要返回：回执才能告诉用户"这个文件我拿不到"。"""
        found = extract_file_candidates(message_with(file_segment(name="课表.ics")))
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].has_content)


class TestIsScheduleFilename(unittest.TestCase):
    def test_ics_suffixes(self):
        self.assertTrue(is_schedule_filename("课表.ics"))
        self.assertTrue(is_schedule_filename("课表.ICAL"))
        self.assertTrue(is_schedule_filename("  课表.ics  "))

    def test_calendar_mime(self):
        self.assertTrue(is_schedule_filename("课表", "text/calendar"))
        self.assertTrue(is_schedule_filename("课表", "text/calendar; charset=utf-8"))

    def test_other_files(self):
        self.assertFalse(is_schedule_filename("成绩单.zip"))
        self.assertFalse(is_schedule_filename("课表.pdf", "application/pdf"))
        self.assertFalse(is_schedule_filename(""))
        self.assertFalse(is_schedule_filename("课表ics"))


class TestChatImportFilename(unittest.TestCase):
    def test_stable_for_same_name(self):
        """同名重发必须落到同一个文件名，否则旧课表会留着继续提醒。"""
        self.assertEqual(
            chat_import_filename("我的课表.ics"), chat_import_filename("我的课表.ics")
        )

    def test_prefix_and_suffix(self):
        self.assertEqual(chat_import_filename("我的课表.ics"), "chat-我的课表.ics")

    def test_strips_directories(self):
        for raw in ("../../etc/passwd", "..\\..\\win.ini", "a/b/c.ics"):
            with self.subTest(raw=raw):
                name = chat_import_filename(raw)
                self.assertNotIn("/", name)
                self.assertNotIn("\\", name)
                self.assertTrue(name.startswith("chat-"))

    def test_blank_name_falls_back(self):
        self.assertEqual(chat_import_filename(""), "chat-schedule.ics")
        self.assertEqual(chat_import_filename("..."), "chat-schedule.ics")


class TestContentFingerprint(unittest.TestCase):
    def test_same_content_same_fingerprint(self):
        self.assertEqual(content_fingerprint("abc"), content_fingerprint("abc"))

    def test_different_content_differs(self):
        self.assertNotEqual(content_fingerprint("abc"), content_fingerprint("abd"))


if __name__ == "__main__":
    unittest.main()
