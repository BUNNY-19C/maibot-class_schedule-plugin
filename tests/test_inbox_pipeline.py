"""阶段 2' 重建测试：捕获 → SQLite 笔记库（端到端，防死锁教训）。"""

import asyncio
import base64
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import _bootstrap  # noqa: F401  —— 注册插件包

import test_plugin_smoke as smoke  # type: ignore  # 复用 FakeCtx/辅助
from class_schedule.inbox import InboxDeduper, parse_message
from class_schedule.notes_db import NotesDatabase
from class_schedule.pipeline import StudyPipeline


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


if __name__ == "__main__":
    unittest.main()
