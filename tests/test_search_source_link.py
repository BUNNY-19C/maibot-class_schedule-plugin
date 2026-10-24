"""第一阶段验收：笔记修订与检索结果的关系（/找 的同源去重与来源标识）。

交接文档 v1.9.3 第一阶段的验收标准，全部用替身识别客户端跑真实链路（不联网）：
- 修订后按新词能找到该笔记；
- 旧识别命中若仍展示，必须带"笔记中已人工修订"的来源标识；
- 同一条来源（同一张图）的笔记命中与公式命中不重复展示；
- A 修订不影响共享同公式的 B；撤销后恢复原检索结果；
- 重识别（no_formula）不清掉人工修订；
- 来源关系（image_hash）在重启、课程移动、重复图片后保持一致。

等待条件一律看**笔记层终态**（公式已回填到笔记）：库计数在识别器落库后、
回调回填前就会满足，拿它当就绪信号会跟回调赛跑。
"""

from __future__ import annotations

import asyncio
import json

import _bootstrap  # noqa: F401  —— 注册插件包

import test_formula_flow as flow  # type: ignore  # 复用脚手架（FakeCtx/管道/替身客户端）
from class_schedule.formula import image_hash
from class_schedule.study_notes import StudyNoteStore

_IMAGE_A = b"\x89PNG-slide-alpha"
_IMAGE_B = b"\x89PNG-slide-beta"  # 内容不同、公式相同 → 指纹去重，共享库里同一条

_OLD = "sF(s)-f(0)"
_NEW = "sF(s)-f(0m)"

_REPLY = json.dumps(
    {"formulas": [{"latex": r"L[f'(t)]=sF(s)-f(0)", "name": "微分定理", "confidence": 0.95}]},
    ensure_ascii=False,
)


class SearchCase(flow._Case):
    """脚手架同 test_formula_flow._Case，识别回复固定为单条「微分定理」。"""

    def setup_single(self):
        plugin, db, store, _pipeline, make_recognizer, root = self.setup_case()
        plugin._recognizer = make_recognizer(flow.ScriptedVision(_REPLY))
        plugin._pipeline.set_recognizer(plugin._recognizer)
        return plugin, db, store, root

    async def wait_until(self, predicate, timeout: float = 5.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        self.fail("等待识别结果超时")

    @staticmethod
    def filled_notes(store) -> list:
        """已回填公式的笔记（跨课程）。"""
        return [
            note
            for course in store.courses()
            for note in store.recent(course, limit=20)
            if note.formula
        ]

    async def search(self, plugin, keyword: str) -> str:
        _ok, message, _count = await plugin.handle_note_search(
            matched_groups={"keyword": keyword}, stream_id="ps"
        )
        return message

    @staticmethod
    def marked(message: str, mark: str) -> list[str]:
        return [line for line in message.splitlines() if mark in line]

    async def recognize_one(self, plugin, db, store, image: bytes) -> None:
        """发一张图并等到识别结果已回填进笔记。"""
        await self.send_image(plugin, image=image)
        await self.wait_until(
            lambda: db.formula_count() == 1 and len(self.filled_notes(store)) >= 1
        )

    async def revise_note(self, plugin, db, store, image: bytes):
        """发图识别 → 人工修订公式，返回 (digest, note)。"""
        await self.recognize_one(plugin, db, store, image)
        digest = image_hash(image)
        note = store.notes_by_image_hash(digest)[0][1]
        store.revise_image_note(note.id, _OLD, _NEW)
        return digest, note


class TestRevisionSearchAndUndo(SearchCase):
    """单来源：修订 → 新词可搜、旧词只剩带标识的旧识别、撤销后复原。"""

    async def test_revise_search_annotate_undo(self):
        plugin, db, store, _root = self.setup_single()
        await self.recognize_one(plugin, db, store, _IMAGE_A)
        note = store.notes_by_image_hash(image_hash(_IMAGE_A))[0][1]
        self.assertIn(_OLD, note.formula)

        # 未修订：搜旧词 → 笔记命中，公式行同源去重，不重复展示
        message = await self.search(plugin, _OLD)
        self.assertIn("找到 1 条", message)
        self.assertIn(note.id, message)
        self.assertEqual(self.marked(message, "🧮"), [])

        # 修订后：新词直接找到笔记
        store.revise_image_note(note.id, _OLD, _NEW)
        message = await self.search(plugin, _NEW)
        self.assertIn("找到 1 条", message)
        self.assertIn(note.id, message)

        # 旧词：笔记不再命中；库里旧识别仍展示，但必须带来源标识
        message = await self.search(plugin, _OLD)
        self.assertIn("找到 1 条", message)
        self.assertEqual(len(self.marked(message, "🧮")), 1)
        self.assertIn("笔记中已人工修订", message)
        self.assertNotIn(note.id, message)

        # 撤销：旧内容恢复 → 旧词重新命中笔记，公式行回到同源去重
        store.undo_image_revision(note.id)
        message = await self.search(plugin, _OLD)
        self.assertIn("找到 1 条", message)
        self.assertIn(note.id, message)
        self.assertEqual(self.marked(message, "🧮"), [])
        self.assertNotIn("笔记中已人工修订", message)


class TestSharedFormulaTwoNotes(SearchCase):
    """A/B 两张图认出同一条公式：修订 A，B 的内容与检索都不受影响。"""

    async def test_a_revision_leaves_b_alone(self):
        plugin, db, store, _root = self.setup_single()
        await self.recognize_one(plugin, db, store, _IMAGE_A)
        await self.send_image(plugin, image=_IMAGE_B)
        await self.wait_until(lambda: len(self.filled_notes(store)) == 2)
        note_a = store.notes_by_image_hash(image_hash(_IMAGE_A))[0][1]
        note_b = store.notes_by_image_hash(image_hash(_IMAGE_B))[0][1]

        # 库里只有一条公式，但两张图都关联到它
        rows = db.search_formulas("微分定理")
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            set(db.image_hashes_for_formula(rows[0]["id"])),
            {image_hash(_IMAGE_A), image_hash(_IMAGE_B)},
        )

        store.revise_image_note(note_a.id, _OLD, _NEW)

        # 旧词：B 命中；公式行因 B 已展示而被同源去重（B 的旧识别仍是现状）
        message = await self.search(plugin, _OLD)
        self.assertIn("找到 1 条", message)
        self.assertIn(note_b.id, message)
        self.assertNotIn(note_a.id, message)
        self.assertEqual(self.marked(message, "🧮"), [])

        # 新词：只有 A 命中，B 不受影响
        message = await self.search(plugin, _NEW)
        self.assertIn(note_a.id, message)
        self.assertNotIn(note_b.id, message)
        self.assertEqual(self.marked(message, "🧮"), [])


class TestDuplicateImageSource(SearchCase):
    """同一张图发两遍：两条笔记同源，公式行与两条笔记合并不重复。"""

    async def test_same_image_two_notes(self):
        plugin, db, store, _root = self.setup_single()
        await self.recognize_one(plugin, db, store, _IMAGE_A)
        await self.send_image(plugin, image=_IMAGE_A)
        digest = image_hash(_IMAGE_A)
        await self.wait_until(lambda: len(self.filled_notes(store)) == 2)
        self.assertEqual(len(store.notes_by_image_hash(digest)), 2)
        message = await self.search(plugin, _OLD)
        self.assertIn("找到 2 条", message)
        self.assertEqual(self.marked(message, "🧮"), [])


class TestSourceLinkPersistence(SearchCase):
    """重启（重开 store）、课程移动后，来源关系与修订状态保持一致。"""

    async def test_survives_restart(self):
        plugin, db, store, root = self.setup_single()
        digest, _note = await self.revise_note(plugin, db, store, _IMAGE_A)

        fresh = StudyNoteStore(root / "notes")  # 模拟重启：重开 store
        pairs = fresh.notes_by_image_hash(digest)
        self.assertEqual(len(pairs), 1)
        self.assertTrue(pairs[0][1].formula_manual)
        self.assertIn(_NEW, pairs[0][1].formula)

        plugin._notes = fresh
        message = await self.search(plugin, _OLD)
        self.assertIn("笔记中已人工修订", message)

    async def test_survives_course_move(self):
        plugin, db, store, _root = self.setup_single()
        digest, note = await self.revise_note(plugin, db, store, _IMAGE_A)

        self.assertIsNotNone(store.move_note("未分类", note.id, "高等数学"))
        pairs = store.notes_by_image_hash(digest)
        self.assertEqual([(c, n.id) for c, n in pairs], [("高等数学", note.id)])
        message = await self.search(plugin, _OLD)
        self.assertIn("笔记中已人工修订", message)


class TestRerecognizeProtectsManual(SearchCase):
    """重识别判定"图里没公式"时，人工修订过的公式原样保留。"""

    async def test_no_formula_keeps_manual_revision(self):
        plugin, db, store, _root = self.setup_single()
        digest, note = await self.revise_note(plugin, db, store, _IMAGE_A)

        await plugin._on_formula_recognized(
            {"note_ref": note.id, "course": "未分类", "stream_id": "", "image_hash": digest},
            {"status": "no_formula", "formulas": [], "degraded": False, "error": ""},
        )
        refreshed = store.locate_note(note.id)[1]
        self.assertIn(_NEW, refreshed.formula)
        self.assertTrue(refreshed.formula_manual)
        # 清除路径曾把 StudyNote 当索引进 _write_index，异常被吞后 hash 丢失
        self.assertEqual(store.notes_by_image_hash(digest)[0][1].id, note.id)
