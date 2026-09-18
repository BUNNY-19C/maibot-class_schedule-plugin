"""阶段 2'/3' 重建测试：捕获 → SQLite 笔记库 → 公式识别 worker（端到端，防死锁教训）。"""

import asyncio
import base64
import json
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  —— 注册插件包

import test_plugin_smoke as smoke  # type: ignore  # 复用 FakeCtx/辅助
from class_schedule.formula import FormulaRecognizer, image_hash
from class_schedule.inbox import InboxDeduper, parse_message
from class_schedule.notes_db import NotesDatabase
from class_schedule.pipeline import StudyPipeline

#: 一张最小的 PNG（只用于"是图片字节"这件事，不参与解码）
_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"

_FORMULA_REPLY = json.dumps(
    {
        "latex": r"\frac{\pi}{2}",
        "name": "半角公式",
        "aliases": ["半角"],
        "category": "高等数学",
        "subcategory": "三角函数",
        "knowledge_points": ["半角公式"],
        "description": "半角公式",
        "confidence": 0.92,
    },
    ensure_ascii=False,
)


async def _wait_until(predicate, *, timeout: float = 5.0) -> bool:
    """轮询等待后台 worker 完成（真实线程 + 真实事件循环，防死锁靠超时暴露）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


class InboxLayerTest(unittest.TestCase):
    def test_parse_message_snowluma_image_and_links(self):
        raw = b"\x89PNGx"
        message = {
            "message_id": "m1",
            "processed_plain_text": "",
            "raw_message": [
                {"type": "text", "data": {"text": "看这个 https://a.test/x.pdf 还有图"}},
                {"type": "image", "data": "", "binary_data_base64": base64.b64encode(raw).decode()},
            ],
        }
        parsed = parse_message(message)
        self.assertIn("看这个", parsed.text)
        self.assertEqual(parsed.images[0][0], raw)
        self.assertIn("https://a.test/x.pdf", parsed.links)
        self.assertEqual(parsed.message_id, "m1")

    def test_dedup_forget(self):
        dedup = InboxDeduper()
        self.assertFalse(dedup.should_skip("同一句话"))
        self.assertTrue(dedup.should_skip("同一句话"))
        dedup.forget("")  # 空指纹不炸
        self.assertFalse(dedup.should_skip("另一句"))


class PersistEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def test_capture_persists_to_sqlite(self):
        """完整链路：after_process 捕获 → markdown 层 + SQLite 层都落（无队列死锁）。"""
        import shutil
        import tempfile as _tf

        from class_schedule.plugin import ClassSchedulePlugin
        from class_schedule.store import PluginState
        from class_schedule.study_notes import StudyNoteStore

        # LIFO 清理：先注册目录删除（最后执行），再注册关库——Windows 上
        # 文件被连接占用时 rmtree 会失败
        tmp_path = Path(_tf.mkdtemp(prefix="inbox2-"))
        self.addCleanup(shutil.rmtree, tmp_path, True)
        if True:
            plugin = ClassSchedulePlugin()
            plugin._set_context(smoke.FakeCtx(tmp_path))  # type: ignore[arg-type]
            plugin.set_plugin_config(
                {"plugin": {"config_version": "1.0.0", "enabled": True},
                 "reply": {"style": "fixed", "ack_style": "fixed"},
                 "access": {"chat_scope": "private"}}
            )
            plugin._data_dir = tmp_path
            plugin._notes = StudyNoteStore(tmp_path / "notes")
            plugin._state = PluginState()
            db = NotesDatabase(tmp_path / "notes.db")
            db.initialize()
            self.addCleanup(db.close)  # 后注册先跑：先关库再删目录
            plugin._notes_db = db
            plugin._pipeline = StudyPipeline(db=db, save_image=plugin._save_inbox_image)
            plugin._pipeline.start()

            async def capture(text: str) -> None:
                await plugin.handle_note_capture(
                    message={
                        "session_id": "ps",
                        "message_info": {"user_info": {"user_id": "654321"}},
                        "raw_message": [{"type": "text", "data": {"text": text}}],
                    },
                    stream_id="ps",
                )
                for task in list(plugin._note_tasks):
                    await task

            await capture("记一下 欧拉公式 e^iπ+1=0")
            rows = db.search_text("欧拉")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["course"], "未分类")
            tags = db.tags_for(rows[0]["id"])
            # 触发词「记一下」不含"公式"，kind=笔记 与 v1.4 行为一致
            self.assertIn("#笔记", tags)
            self.assertIn("#文字", tags)
            self.assertIn("#未分类", tags)
            # markdown 层同时存在（v1.4 行为没被管道破坏）
            self.assertEqual(plugin._notes.count("未分类"), 1)

            # 重复内容：SQLite 层去重，markdown 层维持原行为
            await capture("记一下 欧拉公式 e^iπ+1=0")
            self.assertEqual(len(db.search_text("欧拉")), 1)
            self.assertEqual(plugin._notes.count("未分类"), 2)

            # 落库异常不能打断主路径：pipeline 置 None 后再来一条照常归档
            plugin._pipeline = None
            await capture("记一下 牛顿第二定律")
            self.assertEqual(plugin._notes.count("未分类"), 3)


class RecognitionEndToEnd(unittest.IsolatedAsyncioTestCase):
    """图片笔记 → 落库 → worker 识别 → 公式库 + 回执（真队列，真线程）。"""

    async def _capture_one_image(self, reply: str = _FORMULA_REPLY, image: bytes = _PNG):
        """搭一套装好识别器的插件，发一张带「记一下」的图，返回 (plugin, db, fake)。"""
        import shutil
        import tempfile as _tf

        from class_schedule.plugin import ClassSchedulePlugin
        from class_schedule.store import PluginState
        from class_schedule.study_notes import StudyNoteStore

        tmp_path = Path(_tf.mkdtemp(prefix="inbox3-"))
        self.addCleanup(shutil.rmtree, tmp_path, True)

        plugin = ClassSchedulePlugin()
        plugin._set_context(smoke.FakeCtx(tmp_path))  # type: ignore[arg-type]
        plugin.set_plugin_config(
            {"plugin": {"config_version": "1.0.0", "enabled": True},
             "reply": {"style": "fixed", "ack_style": "fixed"},
             "access": {"chat_scope": "private"}}
        )
        plugin._data_dir = tmp_path
        plugin._notes = StudyNoteStore(tmp_path / "notes")
        plugin._state = PluginState()
        db = NotesDatabase(tmp_path / "notes.db")
        db.initialize()
        self.addCleanup(db.close)
        plugin._notes_db = db

        class FakeVision:
            def __init__(self):
                self.calls = 0

            async def vision(self, **kwargs):
                self.calls += 1
                return {"text": reply, "prompt_tokens": 0, "completion_tokens": 0}

        fake = FakeVision()
        plugin._recognizer = FormulaRecognizer(
            db=db, client=fake, model="vlm-primary", fallback_model="vlm-fallback"
        )
        plugin._pipeline = StudyPipeline(
            db=db,
            save_image=plugin._save_inbox_image,
            recognizer=plugin._recognizer,
            on_recognized=plugin._on_formula_recognized,
            queue_size=8,
        )
        plugin._pipeline.start()
        self.addAsyncCleanup(plugin._pipeline.stop)

        await plugin.handle_note_capture(
            message={
                "session_id": "ps",
                "message_info": {"user_info": {"user_id": "654321"}},
                "raw_message": [
                    {"type": "text", "data": {"text": "记一下 这张课件"}},
                    {"type": "image", "data": {},
                     "binary_data_base64": base64.b64encode(image).decode()},
                ],
            },
            stream_id="ps",
        )
        for task in list(plugin._note_tasks):
            await task
        return plugin, db, fake

    async def test_image_note_recognized_by_worker(self):
        import shutil
        import tempfile as _tf

        from class_schedule.plugin import ClassSchedulePlugin
        from class_schedule.store import PluginState
        from class_schedule.study_notes import StudyNoteStore

        tmp_path = Path(_tf.mkdtemp(prefix="inbox3-"))
        self.addCleanup(shutil.rmtree, tmp_path, True)

        plugin = ClassSchedulePlugin()
        plugin._set_context(smoke.FakeCtx(tmp_path))  # type: ignore[arg-type]
        plugin.set_plugin_config(
            {"plugin": {"config_version": "1.0.0", "enabled": True},
             "reply": {"style": "fixed", "ack_style": "fixed"},
             "access": {"chat_scope": "private"}}
        )
        plugin._data_dir = tmp_path
        plugin._notes = StudyNoteStore(tmp_path / "notes")
        plugin._state = PluginState()
        plugin._awaiting_note = {}
        db = NotesDatabase(tmp_path / "notes.db")
        db.initialize()
        self.addCleanup(db.close)
        plugin._notes_db = db

        class FakeVision:
            def __init__(self):
                self.calls = 0

            async def vision(self, **kwargs):
                self.calls += 1
                return {"text": _FORMULA_REPLY, "prompt_tokens": 0, "completion_tokens": 0}

        client = FakeVision()
        plugin._recognizer = FormulaRecognizer(
            db=db, client=client, model="vlm-primary", fallback_model="vlm-fallback"
        )
        plugin._pipeline = StudyPipeline(
            db=db,
            save_image=plugin._save_inbox_image,
            recognizer=plugin._recognizer,
            on_recognized=plugin._on_formula_recognized,
            queue_size=8,
        )
        plugin._pipeline.start()
        self.addAsyncCleanup(plugin._pipeline.stop)

        # 「记一下」只带图 → 等待内容 → 下一条图片消息被收纳并触发识别
        await plugin.handle_note_capture(
            message={
                "session_id": "ps",
                "message_info": {"user_info": {"user_id": "654321"}},
                "raw_message": [{"type": "text", "data": {"text": "记一下"}}],
            },
            stream_id="ps",
        )
        await plugin.handle_note_capture(
            message={
                "session_id": "ps",
                "message_info": {"user_info": {"user_id": "654321"}},
                "raw_message": [
                    {"type": "image", "data": {},
                     "binary_data_base64": base64.b64encode(_PNG).decode()}
                ],
            },
            stream_id="ps",
        )
        for task in list(plugin._note_tasks):
            await task

        pipeline = plugin._pipeline
        self.assertTrue(await _wait_until(lambda: pipeline.completed >= 1), "worker 未消费队列任务")
        self.assertTrue(
            await _wait_until(lambda: db.formula_count() == 1), "公式没落库"
        )
        self.assertEqual(client.calls, 1)

        row = db.formula_by_image_hash(image_hash(_PNG))
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "半角公式")
        self.assertEqual(row["latex_normalized"], "\\frac{\\pi}2")
        tags = db.formula_tags_for(int(row["id"]))
        self.assertIn("#公式", tags)
        self.assertNotIn("#未分类", tags)  # 「未分类」不是一门课，不进标签
        # 新公式要回执：用户得知道这张图被认出来了
        texts = [text for _stream, text in plugin.ctx.send.texts]  # type: ignore[attr-defined]
        self.assertTrue(any("半角公式" in text for text in texts), texts)

        # 同一张图再收一次 → 命中图片缓存，不再调模型、不重复建公式
        await plugin.handle_note_capture(
            message={
                "session_id": "ps",
                "message_info": {"user_info": {"user_id": "654321"}},
                "raw_message": [
                    {"type": "text", "data": {"text": "记一下 这张图再看一遍"}},
                    {"type": "image", "data": {},
                     "binary_data_base64": base64.b64encode(_PNG).decode()},
                ],
            },
            stream_id="ps",
        )
        for task in list(plugin._note_tasks):
            await task
        self.assertTrue(await _wait_until(lambda: plugin._recognizer.cached_count >= 1))
        self.assertEqual(client.calls, 1)
        self.assertEqual(db.formula_count(), 1)
        # 回归（线上实测）：命中缓存的图也必须回话——重发同一批课件图时一片安静，
        # 用户只会以为功能坏了（他回的原话是"不行"）
        self.assertTrue(
            await _wait_until(
                lambda: any(
                    "之前认过" in text and "半角公式" in text
                    for _stream, text in plugin.ctx.send.texts  # type: ignore[attr-defined]
                )
            ),
            [t for _s, t in plugin.ctx.send.texts],  # type: ignore[attr-defined]
        )

    async def test_formula_is_written_into_the_note(self):
        """公式要落到笔记里：/笔记、/找 与笔记文件看到的得是公式，不是图说。"""
        plugin, db, fake = await self._capture_one_image()
        notes = plugin._notes
        target = notes.recent("未分类", limit=1)[0]
        self.assertTrue(
            await _wait_until(lambda: bool(notes.recent("未分类", limit=1)[0].formula))
        )
        target = notes.recent("未分类", limit=1)[0]
        self.assertIn("半角公式", target.formula)
        self.assertIn("\\frac{\\pi}2", target.formula)
        # 列表里显示公式而不是"这是一张课件"的图说
        self.assertIn("半角公式", target.display)
        # 搜得到
        self.assertTrue(notes.search("半角公式"))
        self.assertTrue(notes.search("\\frac{\\pi}2"))
        # 笔记目录里有一份可读的 .md，正文就是公式
        body = (notes.course_dir("未分类") / f"{target.id}_{target.kind}.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("半角公式", body)
        self.assertIn("\\frac{\\pi}2", body)
        # /笔记 与 /找 展示的是用户真正收到的那条消息
        await plugin.handle_notes(**smoke.private_kwargs("ps"), matched_groups={"course": "未分类"})
        self.assertIn("半角公式", plugin.ctx.send.texts[-1][1])  # type: ignore[attr-defined]
        await plugin.handle_note_search(**smoke.private_kwargs("ps"), matched_groups={"keyword": "半角"})
        self.assertIn("半角公式", plugin.ctx.send.texts[-1][1])  # type: ignore[attr-defined]

    async def test_explicit_image_reports_failure_instead_of_silence(self):
        """用户主动发的图识别失败也要有回应：静默失败最难排查。"""
        import shutil
        import tempfile as _tf

        from class_schedule.llm_client import CloudError

        from class_schedule.plugin import ClassSchedulePlugin
        from class_schedule.store import PluginState
        from class_schedule.study_notes import StudyNoteStore

        tmp_path = Path(_tf.mkdtemp(prefix="inbox3f-"))
        self.addCleanup(shutil.rmtree, tmp_path, True)

        plugin = ClassSchedulePlugin()
        plugin._set_context(smoke.FakeCtx(tmp_path))  # type: ignore[arg-type]
        plugin.set_plugin_config(
            {"plugin": {"config_version": "1.0.0", "enabled": True},
             "reply": {"style": "fixed", "ack_style": "fixed"},
             "access": {"chat_scope": "private"}}
        )
        plugin._data_dir = tmp_path
        plugin._notes = StudyNoteStore(tmp_path / "notes")
        plugin._state = PluginState()
        db = NotesDatabase(tmp_path / "notes.db")
        db.initialize()
        self.addCleanup(db.close)
        plugin._notes_db = db

        class AlwaysFails:
            async def vision(self, **kwargs):
                raise CloudError("HTTP 503 模型繁忙")

        plugin._recognizer = FormulaRecognizer(
            db=db, client=AlwaysFails(), model="vlm", fallback_model=""
        )
        plugin._pipeline = StudyPipeline(
            db=db,
            save_image=plugin._save_inbox_image,
            recognizer=plugin._recognizer,
            on_recognized=plugin._on_formula_recognized,
            queue_size=4,
        )
        plugin._pipeline.start()
        self.addAsyncCleanup(plugin._pipeline.stop)

        await plugin.handle_note_capture(
            message={
                "session_id": "ps",
                "message_info": {"user_info": {"user_id": "654321"}},
                "raw_message": [
                    {"type": "text", "data": {"text": "记一下 这张没公式"}},
                    {"type": "image", "data": {},
                     "binary_data_base64": base64.b64encode(b"\x89PNGno-formula").decode()},
                ],
            },
            stream_id="ps",
        )
        for task in list(plugin._note_tasks):
            await task
        sent = list(plugin.ctx.send.texts)  # type: ignore[attr-defined]

        def said(message: str) -> bool:
            return any(message in text for _stream, text in plugin.ctx.send.texts)  # type: ignore[attr-defined]

        # 等的是"用户看得见的那句话"，不是内部计数：回执是 worker 在识别之后发的
        self.assertTrue(await _wait_until(lambda: said("没认出公式")), sent)
        self.assertGreaterEqual(plugin._recognizer.failed_count, 1)
        self.assertEqual(db.formula_count(), 0)
        # 失败按图片 hash 记了数，自动补识别才知道该不该再试
        self.assertEqual(db.formula_failure_count(), 1)


class ImageBytesRoundTrip(unittest.TestCase):
    """plain 图片消息（无触发词）也要能攒下来：before_process 抢存 → 收纳时才用得上。"""

    def test_parse_message_keeps_original_bytes(self):
        message = {
            "message_id": "m9",
            "raw_message": [
                {"type": "image", "data": "", "binary_data_base64": base64.b64encode(_PNG).decode()}
            ],
        }
        parsed = parse_message(message)
        self.assertEqual(parsed.images[0][0], _PNG)
        self.assertEqual(parsed.message_id, "m9")


if __name__ == "__main__":
    unittest.main()
