"""/归到 课程名解析与候选确认的端到端测试（评估清单第十三节）。

关键场景：歧义候选必须绑定发起时的那条笔记——确认期间未分类又进了新笔记，
/归到 1 也只能归档原来那条（归错对象的代价是笔记永久进错课程）。
"""

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import _bootstrap  # noqa: F401  —— 注册插件包

import test_plugin_smoke as smoke  # type: ignore  # 复用 FakeCtx/辅助
from class_schedule.course_source import CourseRepository
from class_schedule.plugin import ClassSchedulePlugin
from class_schedule.store import PluginState
from class_schedule.study_notes import StudyNoteStore


def _make(root: Path) -> ClassSchedulePlugin:
    """与 prepare_bare 相同的骨架（不预置课表），课程名全部来自笔记目录。"""
    plugin = ClassSchedulePlugin()
    plugin._set_context(smoke.FakeCtx(root))  # type: ignore[arg-type]
    plugin.set_plugin_config(
        smoke.build_config(
            access={"chat_scope": "private"}, study={"summary_enabled": False}
        )
    )
    plugin._data_dir = root
    plugin._repo = CourseRepository(root / "ics")
    plugin._repo.ensure_dir()
    plugin._repo.refresh(force=True)
    plugin._state = PluginState()
    plugin._notes = StudyNoteStore(root / "notes")
    return plugin


async def _move(plugin: ClassSchedulePlugin, arg: str) -> str:
    ok, message, _ = await plugin.handle_note_move(
        **smoke.private_kwargs("ps"), matched_groups={"course": arg}
    )
    return message


class TestCourseMoveResolution(unittest.IsolatedAsyncioTestCase):
    """Commit 3：解析接入后的归档行为。"""

    async def test_unique_contains_hits(self):
        with TemporaryDirectory() as tmp:
            plugin = _make(Path(tmp))
            plugin._notes.add_text_note("未分类", "公式", "欧拉公式")
            plugin._notes.add_text_note("航空自动控制基础", "笔记", "已有笔记")
            message = await _move(plugin, "自动控制")
            self.assertIn("已把", message)
            self.assertIn("航空自动控制基础", message)
            moved = plugin._notes.recent("航空自动控制基础", limit=1)[0]
            self.assertIn("欧拉公式", moved.text)
            self.assertEqual(plugin._notes.count("未分类"), 0)

    async def test_not_found_creates_literal_course(self):
        with TemporaryDirectory() as tmp:
            plugin = _make(Path(tmp))
            plugin._notes.add_text_note("未分类", "笔记", "一条与已知课程无关的记录")
            message = await _move(plugin, "材料力学")
            self.assertIn("新课程名", message)
            self.assertEqual(plugin._notes.count("材料力学"), 1)


class TestPendingCourseChoice(unittest.IsolatedAsyncioTestCase):
    """Commit 4：歧义候选绑定原笔记 + TTL。"""

    async def test_ambiguous_binds_note_and_number_choice_archives_it(self):
        """评估第十三节的关键流程：A 触发歧义，B 插队，/归到 1 归档的是 A。"""
        with TemporaryDirectory() as tmp:
            plugin = _make(Path(tmp))
            notes = plugin._notes
            # 两个候选课程目录先存在
            notes.add_text_note("航空自动控制基础", "笔记", "已有笔记一")
            notes.add_text_note("自动控制原理", "笔记", "已有笔记二")
            # 笔记 A：将触发歧义的那条（未分类里的最新一条）
            note_a = notes.add_text_note("未分类", "公式", "我是笔记A")

            message = await _move(plugin, "自动控制")
            self.assertIn("可能对应", message)
            # 候选按分数排序，顺序不固定；关键是一门不少、都有编号
            self.assertIn("自动控制原理", message)
            self.assertIn("航空自动控制基础", message)
            self.assertIn("1. ", message)
            self.assertIn("2. ", message)
            pending = plugin._pending_course_choices["ps"]
            self.assertEqual(pending.note_id, note_a.id)

            # 确认期间笔记 B 进了未分类（比 A 新）
            note_b = notes.add_text_note("未分类", "公式", "我是笔记B")

            message = await _move(plugin, "1")
            self.assertIn("已把", message)
            # 1 号候选按分数是「自动控制原理」：A 应该归到那里
            moved = notes.recent("自动控制原理", limit=1)[0]
            self.assertEqual(moved.id, note_a.id, "归档的必须是 A，不是 B")
            self.assertIn("我是笔记A", moved.text)
            # B 仍然留在未分类；确认用掉后 pending 消失
            self.assertEqual(notes.count("未分类"), 1)
            self.assertEqual(notes.recent("未分类", limit=1)[0].id, note_b.id)
            self.assertNotIn("ps", plugin._pending_course_choices)

    async def test_full_name_in_pending_binds_the_same_note(self):
        """回归：待确认状态下输入候选的完整名称，也作用于绑定的那条笔记。

        旧实现只有编号分支绑定笔记，完整名称走 move_latest——确认期间 B 插队
        时就会把 B 归档，A 滞留在未分类。
        """
        with TemporaryDirectory() as tmp:
            plugin = _make(Path(tmp))
            notes = plugin._notes
            notes.add_text_note("航空自动控制基础", "笔记", "已有笔记一")
            notes.add_text_note("自动控制原理", "笔记", "已有笔记二")
            note_a = notes.add_text_note("未分类", "公式", "我是笔记A")

            await _move(plugin, "自动控制")  # 歧义，候选绑定 A
            note_b = notes.add_text_note("未分类", "公式", "我是笔记B")

            message = await _move(plugin, "航空自动控制基础")  # 完整名称确认
            self.assertIn("已把", message)
            moved = notes.recent("航空自动控制基础", limit=1)[0]
            self.assertEqual(moved.id, note_a.id, "归档的必须是 A，不是 B")
            self.assertIn("我是笔记A", moved.text)
            self.assertEqual(notes.count("未分类"), 1)
            self.assertEqual(notes.recent("未分类", limit=1)[0].id, note_b.id)
            self.assertNotIn("ps", plugin._pending_course_choices)

    async def test_full_name_in_pending_binds_the_same_note(self):
        """回归：待确认状态下输入候选的完整名称，也作用于绑定的那条笔记。

        旧实现只有编号分支绑定笔记，完整名称走 move_latest——确认期间 B 插队
        时就会把 B 归档，A 滞留在未分类。
        """
        with TemporaryDirectory() as tmp:
            plugin = _make(Path(tmp))
            notes = plugin._notes
            notes.add_text_note("航空自动控制基础", "笔记", "已有笔记一")
            notes.add_text_note("自动控制原理", "笔记", "已有笔记二")
            note_a = notes.add_text_note("未分类", "公式", "我是笔记A")

            await _move(plugin, "自动控制")  # 歧义，候选绑定 A
            note_b = notes.add_text_note("未分类", "公式", "我是笔记B")

            message = await _move(plugin, "航空自动控制基础")  # 完整名称确认
            self.assertIn("已把", message)
            moved = notes.recent("航空自动控制基础", limit=1)[0]
            self.assertEqual(moved.id, note_a.id, "归档的必须是 A，不是 B")
            self.assertIn("我是笔记A", moved.text)
            self.assertEqual(notes.count("未分类"), 1)
            self.assertEqual(notes.recent("未分类", limit=1)[0].id, note_b.id)
            self.assertNotIn("ps", plugin._pending_course_choices)

    async def test_move_carries_companion_formula_md(self):
        """回归：图片笔记带公式 md 移动课程时，文档必须一起走。

        旧实现只挪 note.file（原图），attach_formula 生成的公式 md 留在原课程
        成孤儿——新课程下翻笔记没有公式内容。
        """
        with TemporaryDirectory() as tmp:
            plugin = _make(Path(tmp))
            notes = plugin._notes
            image = b"\x89PNG-slide-with-formula"
            note = notes.add_image_note("未分类", "笔记", image, text="课件图说")
            notes.attach_formula("未分类", note.id, "欧拉公式：e^{i\\pi}+1=0")
            md_name = f"{note.id}_{note.kind}.md"

            await _move(plugin, "航空自动控制基础")

            new_dir = notes.course_dir("航空自动控制基础")
            old_dir = notes.course_dir("未分类")
            self.assertTrue((new_dir / md_name).is_file())
            self.assertTrue((new_dir / note.file).is_file())
            self.assertFalse((old_dir / md_name).exists())
            self.assertFalse((old_dir / note.file).exists())
            body = (new_dir / md_name).read_text(encoding="utf-8")
            self.assertIn("欧拉公式", body)

    async def test_out_of_range_number_gets_range_hint(self):
        with TemporaryDirectory() as tmp:
            plugin = _make(Path(tmp))
            notes = plugin._notes
            notes.add_text_note("航空自动控制基础", "笔记", "x")
            notes.add_text_note("自动控制原理", "笔记", "y")  # 两个候选才构成歧义
            notes.add_text_note("未分类", "公式", "笔记A")
            await _move(plugin, "自动控制")
            message = await _move(plugin, "9")
            self.assertIn("1 到 2 之间", message)
            self.assertIn("ps", plugin._pending_course_choices)  # 仍然待选

    async def test_expired_pending_is_dropped(self):
        with TemporaryDirectory() as tmp:
            plugin = _make(Path(tmp))
            notes = plugin._notes
            notes.add_text_note("航空自动控制基础", "笔记", "x")
            notes.add_text_note("自动控制原理", "笔记", "y")  # 两个候选才构成歧义
            notes.add_text_note("未分类", "公式", "笔记A")
            await _move(plugin, "自动控制")
            # 把待选改成已过期
            pending = plugin._pending_course_choices["ps"]
            plugin._pending_course_choices["ps"] = pending.__class__(
                note_id=pending.note_id,
                original_query=pending.original_query,
                candidates=pending.candidates,
                expires_at=datetime.now() - timedelta(seconds=1),
            )
            message = await _move(plugin, "1")
            self.assertIn("没有待选择", message)
            self.assertNotIn("ps", plugin._pending_course_choices)

    async def test_digit_without_pending_gets_hint(self):
        with TemporaryDirectory() as tmp:
            plugin = _make(Path(tmp))
            notes = plugin._notes
            notes.add_text_note("未分类", "公式", "笔记A")
            message = await _move(plugin, "1")
            self.assertIn("没有待选择", message)
            self.assertEqual(notes.count("未分类"), 1)  # 什么也没归档


if __name__ == "__main__":
    unittest.main()
