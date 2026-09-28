"""板书转录、修订与旧公式缓存升级的关键链路。"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import _bootstrap  # noqa: F401
import test_plugin_smoke as smoke  # type: ignore
import test_formula_flow as flow  # type: ignore

from class_schedule.formula import FormulaRecognizer, latex_syntax_issues, parse_formula_response, is_low_confidence
from class_schedule.note_draft import parse_note_draft
from class_schedule.notes_db import NotesDatabase
from class_schedule.plugin import ClassSchedulePlugin
from class_schedule.study_notes import StudyNoteStore


class DraftParserTest(unittest.TestCase):
    def test_latex_checker_flags_only_obvious_group_errors(self):
        self.assertEqual(latex_syntax_issues(r"\frac{a}{b}"), [])
        self.assertEqual(latex_syntax_issues(r"x\in[0,1)"), [])
        self.assertEqual(latex_syntax_issues(r"\left. x \right)"), [])
        self.assertIn("花括号未闭合", latex_syntax_issues(r"\frac{a}{b"))
        self.assertIn("环境起止不匹配", latex_syntax_issues(
            r"\begin{matrix}a&b\end{array}"))
        parsed = parse_formula_response(json.dumps({
            "formulas": [{"latex": r"\frac{a}{b", "name": "高置信错误式", "confidence": 0.99}]
        }))
        self.assertTrue(is_low_confidence(parsed[0]))

    def test_parses_and_limits_uncertainty_without_rejecting_legacy(self):
        self.assertIsNone(parse_note_draft('{"formulas": []}'))
        draft = parse_note_draft(json.dumps({
            "topic": "流体力学", "content_markdown": "马赫[?]数 M=v/c",
            "uncertain_items": [{"term": "赫?", "reason": "字迹模糊"}],
            "formulas": [],
        }, ensure_ascii=False))
        self.assertEqual(draft["topic"], "流体力学")
        self.assertEqual(draft["uncertain_items"][0]["term"], "赫?")


class DraftStoreTest(unittest.TestCase):
    def test_extraction_search_revision_undo_and_rerecognition(self):
        with TemporaryDirectory() as tmp:
            store = StudyNoteStore(Path(tmp) / "notes")
            note = store.add_image_note("未分类", "笔记", b"image")
            first = {"topic": "力学", "content_markdown": "F=ma，马赫树",
                     "uncertain_items": [{"term": "马赫树", "reason": "连笔"}]}
            store.update_extraction(note.id, first)
            self.assertEqual(store.search("马赫树")[0].id, note.id)
            store.revise_image_note(note.id, "马赫树", "马赫数")
            self.assertEqual(store.search("马赫数")[0].id, note.id)
            self.assertEqual(store.last_of("未分类").unresolved_items, [])
            self.assertEqual(store.last_of("未分类").content_raw, first["content_markdown"])
            store.update_extraction(note.id, {**first, "content_markdown": "F=ma，错误重识"})
            self.assertIn("马赫数", store.last_of("未分类").content)
            store.move_note("未分类", note.id, "流体力学")
            store.undo_image_revision(note.id)
            current = store.last_of("流体力学")
            self.assertEqual(current.content, first["content_markdown"])
            self.assertFalse(current.content_manual)
            self.assertEqual(current.unresolved_items[0]["term"], "马赫树")
            companion = store.course_dir("流体力学") / f"{note.id}_{note.kind}.md"
            self.assertIn("马赫树", companion.read_text(encoding="utf-8"))
            self.assertEqual(current.revisions[-1]["action"], "undo")

    def test_manual_formula_survives_recognition(self):
        with TemporaryDirectory() as tmp:
            store = StudyNoteStore(Path(tmp) / "notes")
            note = store.add_image_note("数学", "公式", b"image")
            store.update_extraction(note.id, {
                "topic": "复数", "content_markdown": "欧拉公式 e^{i\\pi}+1=0",
                "uncertain_items": [],
            })
            store.update_formula(note.id, "欧拉公式：e^{i\\pi}+1=0")
            store.revise_image_note(note.id, "i\\pi", "i\\theta")
            store.update_formula(note.id, "错误重识：x=0")
            self.assertIn("i\\theta", store.last_of("数学").formula)
            self.assertIn("i\\theta", store.last_of("数学").content)
            store.undo_image_revision(note.id)
            self.assertIn("i\\pi", store.last_of("数学").formula)
            self.assertIn("i\\pi", store.last_of("数学").content)


class DraftCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_text_survives_unusable_formula_reply(self):
        with TemporaryDirectory() as tmp:
            db = NotesDatabase(Path(tmp) / "notes.db")
            db.initialize()

            class Vision:
                async def vision(self, **_):
                    return {"text": json.dumps({
                        "topic": "力学", "content_markdown": "清楚可读的板书",
                        "uncertain_items": [], "formulas": [{"latex": ""}],
                    }, ensure_ascii=False)}

            result = await FormulaRecognizer(
                db=db, client=Vision(), model="test"
            ).recognize(b"bad-formula")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["draft"]["content_markdown"], "清楚可读的板书")
            db.close()

    async def test_legacy_cache_refreshes_once_and_structured_cache_replays(self):
        with TemporaryDirectory() as tmp:
            db = NotesDatabase(Path(tmp) / "notes.db")
            db.initialize()
            image = b"a-board"
            from class_schedule.formula import image_hash
            digest = image_hash(image)
            db.record_image_recognition(digest, "no_formula", [], "old")

            class Vision:
                calls = 0

                async def vision(self, **_):
                    self.calls += 1
                    return {"text": json.dumps({
                        "topic": "流体力学", "content_markdown": "连续性方程",
                        "uncertain_items": [], "formulas": [],
                    }, ensure_ascii=False)}

            client = Vision()
            recognizer = FormulaRecognizer(db=db, client=client, model="test")
            first = await recognizer.recognize(image)
            second = await recognizer.recognize(image)
            self.assertEqual(first["status"], "no_formula")
            self.assertEqual(second["status"], "cached")
            self.assertEqual(second["draft"]["content_markdown"], "连续性方程")
            self.assertEqual(client.calls, 1)
            db.close()


class NoteCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_detail_revision_and_undo_commands(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin = ClassSchedulePlugin()
            plugin._set_context(smoke.FakeCtx(root))  # type: ignore[arg-type]
            plugin.set_plugin_config(smoke.build_config(access={"chat_scope": "private"}))
            plugin._notes = StudyNoteStore(root / "notes")
            note = plugin._notes.add_image_note("力学", "笔记", b"image")
            plugin._notes.update_extraction(note.id, {
                "topic": "第一章", "content_markdown": "马赫树的定义",
                "uncertain_items": [{"term": "马赫树", "reason": "连笔"}],
            })
            scope = smoke.private_kwargs("ps")
            _, detail, _ = await plugin.handle_note_detail(
                **scope, matched_groups={"note_id": note.id}
            )
            self.assertIn("待核对", detail)
            _, reply, _ = await plugin.handle_note_revise(
                **scope, matched_groups={"note_id": note.id,
                                         "original": "马赫树", "replacement": "马赫数"}
            )
            self.assertIn("已修订", reply)
            self.assertEqual(plugin._notes.search("马赫数")[0].id, note.id)
            _, reply, _ = await plugin.handle_note_undo(
                **scope, matched_groups={"note_id": note.id}
            )
            self.assertIn("已撤销", reply)
            self.assertEqual(plugin._notes.search("马赫树")[0].id, note.id)


class DraftPipelineTest(flow._Case):
    async def test_one_vision_call_fills_formulas_and_text(self):
        plugin, db, store, pipeline, make_recognizer, _root = self.setup_case()
        reply = json.dumps({
            "topic": "工程流体力学", "content_markdown": "马赫数 M=v/c",
            "uncertain_items": [{"term": "M", "reason": "字迹模糊"}],
            "formulas": [{"latex": "M=v/c", "name": "马赫数", "confidence": 0.8}],
        }, ensure_ascii=False)
        fake = flow.ScriptedVision(reply)
        plugin._recognizer = make_recognizer(fake)
        pipeline.set_recognizer(plugin._recognizer)
        await self.send_image(plugin)
        await pipeline.drain()
        note = store.last_of("未分类")
        self.assertEqual(fake.calls, 1)
        self.assertIn("马赫数 M=v/c", note.content)
        self.assertIn("马赫数", note.formula)
        self.assertEqual(note.uncertain_items[0]["term"], "M")
        self.assertEqual(store.search("M=v/c")[0].id, note.id)
        self.assertEqual(db.formula_count(), 1)
