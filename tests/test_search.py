"""混合检索引擎测试：查询解析、过滤、降级路径。"""

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import _bootstrap  # noqa: F401  —— 注册插件包

from class_schedule.notes_db import NotesDatabase
from class_schedule.search import HybridSearcher, format_hits, parse_query

NOW = datetime(2026, 9, 18, 10, 0)  # 周五


class TestParseQuery(unittest.TestCase):
    def test_last_weekday(self):
        # 解析器按字面匹配课程名（别名归一是打标层的事，管道重建后接入）
        parsed = parse_query("上周三高等数学作业", ["高等数学"], now=NOW)
        # 上周三 = 2026-09-09（本周一 09-14 往前 5 天）
        self.assertEqual(parsed.day_start, datetime(2026, 9, 9))
        self.assertEqual(parsed.courses, ["高等数学"])
        self.assertEqual(parsed.type_tag, "#作业")
        self.assertEqual(parsed.keywords, "")

    def test_relative_day_and_keyword(self):
        parsed = parse_query("昨天的泰勒展开", [], now=NOW)
        self.assertEqual(parsed.day_start, datetime(2026, 9, 17))
        self.assertEqual(parsed.keywords, "泰勒展开")

    def test_explicit_date(self):
        parsed = parse_query("9月1日 欧拉公式", [], now=NOW)
        self.assertEqual(parsed.day_start, datetime(2026, 9, 1))
        self.assertEqual(parsed.type_tag, "#公式")

    def test_bare_course_word_kept_when_unknown(self):
        parsed = parse_query("电路 作业", ["高等数学"], now=NOW)
        self.assertEqual(parsed.courses, [])
        self.assertIn("电路", parsed.keywords)
        self.assertEqual(parsed.type_tag, "#作业")

    def test_no_time_no_course(self):
        parsed = parse_query("质能方程", [], now=NOW)
        self.assertIsNone(parsed.day_start)
        self.assertEqual(parsed.keywords, "质能方程")


class TestHybridSearch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import shutil
        import tempfile

        self.root = Path(tempfile.mkdtemp(prefix="searchdb-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.db = NotesDatabase(self.root / "notes.db")
        self.db.initialize()
        self.addCleanup(self.db.close)

    async def test_keyword_only_without_client(self):
        self.db.add_note(source_type="文字", raw_content="欧拉公式 e^iπ+1=0")
        self.db.add_note(source_type="文字", raw_content="牛顿第二定律")
        searcher = HybridSearcher(self.db)
        result = await searcher.search("欧拉")
        self.assertEqual(len(result["notes"]), 1)
        self.assertEqual(result["notes"][0]["matched_by"], "全文")
        self.assertEqual(result["semantic_error"], "")

    async def test_course_and_time_filters(self):
        self.db.add_note(source_type="文字", raw_content="拉格朗日中值", course="高等数学")
        searcher = HybridSearcher(self.db)
        result = await searcher.search(
            "上周三 拉格朗日", courses=["高等数学"], now=NOW
        )
        # 上周三(09-09)之前没有记录：时间过滤生效
        self.assertEqual(result["notes"], [])
        result2 = await searcher.search("今天 拉格朗日", courses=["高等数学"], now=NOW)
        self.assertEqual(len(result2["notes"]), 1)

    async def test_tags_filter_and_format(self):
        note = self.db.add_note(source_type="文字", raw_content="第三章要考")
        self.db.attach_tag("note", note, "#作业")
        searcher = HybridSearcher(self.db)
        result = await searcher.search("第三章 作业", now=NOW)
        self.assertEqual(len(result["notes"]), 1)
        text = format_hits(result)
        self.assertIn("#作业", text)

    async def test_semantic_degrades_on_cloud_error(self):
        """Embedding 调用失败：全文结果照常返回，只报告降级原因。"""
        self.db.add_note(source_type="文字", raw_content="傅里叶级数")
        self.db.store_embedding(1, "m", [1.0, 0.0])

        class _BrokenClient:
            async def embed(self, **kwargs):
                from class_schedule.llm_client import CloudError

                raise CloudError("HTTP 500")

        searcher = HybridSearcher(
            self.db, client=_BrokenClient(), embedding_model="e"
        )
        result = await searcher.search("傅里叶")
        self.assertEqual(len(result["notes"]), 1)  # 全文没丢
        self.assertIn("HTTP 500", result["semantic_error"])


if __name__ == "__main__":
    unittest.main()
