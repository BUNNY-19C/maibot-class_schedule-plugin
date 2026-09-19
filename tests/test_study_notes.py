"""学习笔记存储测试：按课目录、索引、原子写、检索与归类。"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import _bootstrap  # noqa: F401  —— 注册插件包

from class_schedule.study_notes import (
    StudyNoteStore,
    course_folder_name,
)

TEXT = "欧拉公式 e^iπ + 1 = 0"


class TestCourseFolderName(unittest.TestCase):
    def test_keeps_chinese_and_alnum(self):
        self.assertEqual(course_folder_name("高等数学"), "高等数学")
        self.assertEqual(course_folder_name("机械设计-2班"), "机械设计-2班")

    def test_strips_path_characters(self):
        for raw in ("../../etc", "a/b", "a\\b", "课:名"):
            with self.subTest(raw=raw):
                name = course_folder_name(raw)
                self.assertNotIn("/", name)
                self.assertNotIn("\\", name)

    def test_blank_falls_back(self):
        self.assertEqual(course_folder_name(""), "未分类")
        self.assertEqual(course_folder_name("///"), "未分类")

    def test_long_name_truncated(self):
        name = course_folder_name("超" * 100)
        self.assertLessEqual(len(name), 40)


class TestTextNotes(unittest.TestCase):
    def test_add_then_query(self):
        with TemporaryDirectory() as tmp:
            store = StudyNoteStore(Path(tmp) / "notes")
            note = store.add_text_note("高等数学", "公式", TEXT, source="同消息")

            self.assertEqual(note.course, "高等数学")
            self.assertTrue(note.file.endswith(".md"))
            # 文件真的在盘上、人可直接读
            saved = (Path(tmp) / "notes" / "高等数学" / note.file).read_text(
                encoding="utf-8"
            )
            self.assertIn("欧拉公式", saved)
            # 索引里有
            self.assertEqual(store.count("高等数学"), 1)
            self.assertEqual(store.recent("高等数学")[0].text, TEXT)

    def test_survives_new_store_instance(self):
        """索引落盘后，重建 store 对象仍能查到（跨重启可用）。"""
        with TemporaryDirectory() as tmp:
            StudyNoteStore(Path(tmp) / "notes").add_text_note(
                "高等数学", "重点", "第三章要考", source="测试"
            )
            store2 = StudyNoteStore(Path(tmp) / "notes")
            self.assertEqual(store2.courses(), ["高等数学"])
            self.assertEqual(store2.count("高等数学"), 1)

    def test_empty_content_rejected(self):
        with TemporaryDirectory() as tmp:
            store = StudyNoteStore(Path(tmp) / "notes")
            with self.assertRaises(ValueError):
                store.add_text_note("高等数学", "笔记", "   ")

    def test_overlong_content_truncated(self):
        with TemporaryDirectory() as tmp:
            store = StudyNoteStore(Path(tmp) / "notes")
            note = store.add_text_note("课", "笔记", "字" * 5000)
            self.assertLessEqual(len(note.text), 4000)

    def test_kind_in_filename(self):
        with TemporaryDirectory() as tmp:
            store = StudyNoteStore(Path(tmp) / "notes")
            note = store.add_text_note("课", "公式", "a=b")
            self.assertIn("公式", note.file)


class TestImageNotes(unittest.TestCase):
    def test_add_image_note(self):
        with TemporaryDirectory() as tmp:
            store = StudyNoteStore(Path(tmp) / "notes")
            note = store.add_image_note(
                "机械设计", "重点", b"\x89PNG-fake", suffix=".png", text="装配图"
            )
            saved = Path(tmp) / "notes" / "机械设计" / note.file
            self.assertTrue(saved.exists())
            self.assertEqual(saved.read_bytes(), b"\x89PNG-fake")
            self.assertEqual(note.text, "装配图")

    def test_empty_image_rejected(self):
        with TemporaryDirectory() as tmp:
            store = StudyNoteStore(Path(tmp) / "notes")
            with self.assertRaises(ValueError):
                store.add_image_note("课", "重点", b"")

    def test_bad_suffix_sanitized(self):
        with TemporaryDirectory() as tmp:
            store = StudyNoteStore(Path(tmp) / "notes")
            note = store.add_image_note("课", "重点", b"x", suffix="../../evil.exe")
            self.assertTrue(note.file.startswith("img/"))
            self.assertIn("img/", note.file)
            self.assertNotIn("..", note.file)


class TestSearchAndMove(unittest.TestCase):
    def _seed(self, root: Path) -> StudyNoteStore:
        store = StudyNoteStore(root / "notes")
        store.add_text_note("高等数学", "公式", "欧拉公式 e^iπ+1=0")
        store.add_text_note("高等数学", "重点", "第三章要考")
        store.add_text_note("机械设计", "重点", "公差配合")
        store.add_text_note("未分类", "笔记", "欧拉是谁")
        return store

    def test_courses_lists_all(self):
        with TemporaryDirectory() as tmp:
            store = self._seed(Path(tmp))
            # sorted() 按 unicode 码点排序：未(672A) < 机(673A) < 高(9AD8)
            self.assertEqual(
                store.courses(), ["未分类", "机械设计", "高等数学"]
            )

    def test_search_hits_across_courses(self):
        with TemporaryDirectory() as tmp:
            store = self._seed(Path(tmp))
            hits = store.search("欧拉")
            self.assertEqual(len(hits), 2)
            self.assertEqual({h.course for h in hits}, {"高等数学", "未分类"})

    def test_search_case_insensitive(self):
        with TemporaryDirectory() as tmp:
            store = StudyNoteStore(Path(tmp) / "notes")
            store.add_text_note("课", "笔记", "Taylor Expansion")
            self.assertEqual(len(store.search("taylor")), 1)

    def test_search_empty_keyword(self):
        with TemporaryDirectory() as tmp:
            store = self._seed(Path(tmp))
            self.assertEqual(store.search("  "), [])

    def test_move_latest_moves_file_and_index(self):
        with TemporaryDirectory() as tmp:
            store = self._seed(Path(tmp))
            before = store.count("未分类")
            self.assertEqual(before, 1)

            moved = store.move_latest("未分类", "高等数学")

            self.assertIsNotNone(moved)
            self.assertEqual(moved.course, "高等数学")
            self.assertEqual(store.count("未分类"), 0)
            self.assertEqual(store.count("高等数学"), 3)
            # 移动后的文件在新目录里
            self.assertTrue(
                (Path(tmp) / "notes" / "高等数学" / moved.file).exists()
            )
            self.assertFalse(
                (Path(tmp) / "notes" / "未分类" / moved.file).exists()
            )

    def test_move_from_empty_returns_none(self):
        with TemporaryDirectory() as tmp:
            store = StudyNoteStore(Path(tmp) / "notes")
            self.assertIsNone(store.move_latest("未分类", "高等数学"))

    def test_corrupt_index_tolerated(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp) / "notes"
            course = root / "高等数学"
            course.mkdir(parents=True)
            (course / "index.json").write_text("{ 不是 json", encoding="utf-8")

            store = StudyNoteStore(root)
            self.assertEqual(store.count("高等数学"), 0)
            store.add_text_note("高等数学", "笔记", "重建索引")
            self.assertEqual(store.count("高等数学"), 1)


class TestAttachFormula(unittest.TestCase):
    """识别结果回填笔记这一层的规矩。"""

    def test_writes_index_field_and_readable_file(self):
        with TemporaryDirectory() as tmp:
            store = StudyNoteStore(Path(tmp) / "notes")
            note = store.add_image_note(
                "高等数学", "笔记", b"\x89PNGx", text="这是一张课件幻灯片"
            )
            done = store.attach_formula("高等数学", note.id, "欧拉公式：e^{i\\pi}+1=0")
            self.assertIsNotNone(done)
            self.assertIn("欧拉公式", done.formula)
            self.assertIn("欧拉公式", done.display)  # 列表显示公式而不是图说
            body = (
                store.course_dir("高等数学") / f"{note.id}_{note.kind}.md"
            ).read_text(encoding="utf-8")
            self.assertIn("e^{i\\pi}+1=0", body)
            self.assertIn("这是一张课件幻灯片", body)  # 图说降级为"图片说明"
            self.assertIn(note.file, body)  # 还指向原图
            # 幂等：同一条公式重复回填不再写盘
            again = store.attach_formula("高等数学", note.id, "欧拉公式：e^{i\\pi}+1=0")
            self.assertEqual(again.formula, done.formula)
            # 搜索能命中公式；查不到笔记/空公式时返回 None
            self.assertTrue(store.search("欧拉公式"))
            self.assertIsNone(store.attach_formula("高等数学", "不存在", "x"))
            self.assertIsNone(store.attach_formula("高等数学", note.id, "  "))


class TestConcurrentWrites(unittest.TestCase):
    """识别回填与收纳会并发写同一份索引（后台线程 + 事件循环），不能互相覆盖。"""

    def test_threads_do_not_lose_notes_or_formulas(self):
        import threading

        with TemporaryDirectory() as tmp:
            store = StudyNoteStore(Path(tmp) / "notes")
            store.add_text_note("高等数学", "笔记", "第一条")
            errors: list[BaseException] = []

            def worker(index: int) -> None:
                try:
                    note = store.add_text_note("高等数学", "公式", f"第{index}条")
                    store.attach_formula("高等数学", note.id, f"公式{index}：x={index}")
                except BaseException as exc:  # noqa: BLE001  —— 线程里的异常要能断言
                    errors.append(exc)

            threads = [
                threading.Thread(target=worker, args=(index,)) for index in range(10)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            # 无锁的"读-改-写"会丢更新：这里必须 1 + 10 条都在，且公式一个不落
            self.assertEqual(store.count("高等数学"), 11)
            notes = store.recent("高等数学", limit=50)
            filled = [note for note in notes if note.formula.startswith("公式")]
            self.assertEqual(len(filled), 10)


if __name__ == "__main__":
    unittest.main()
