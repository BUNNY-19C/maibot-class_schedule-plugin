"""公式识别缓存的完整流程测试（评估项 1+3）。

用户要求的验证方式：与其堆单元测试，不如把"首次识别 → 重发同一张图 → 重启补
识别"这条真实链路整个跑一遍。这里全部使用替身视觉客户端，不联网。

另含 /重识图片 的行为测试：清识别缓存、连放弃记录一起清，然后重跑。
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  —— 注册插件包

import test_plugin_smoke as smoke  # type: ignore  # 复用 FakeCtx/辅助
from class_schedule.formula import FormulaRecognizer, image_hash
from class_schedule.notes_db import NotesDatabase
from class_schedule.pipeline import StudyPipeline
from class_schedule.store import PluginState
from class_schedule.study_notes import StudyNoteStore

_FAKE_KEY = "unit-test-only"  # 不是真实凭据，仅让客户端走到"已配置"分支

_IMAGE = b"\x89PNG-flow-slide"

_REPLY_TWO = json.dumps({"formulas": [
    {"latex": r"L[f'(t)]=sF(s)-f(0)", "name": "微分定理", "confidence": 0.95},
    {"latex": r"L[e^{at}]=\frac{1}{s-a}", "name": "指数变换", "confidence": 0.93},
]}, ensure_ascii=False)

_REPLY_NEW = json.dumps({"formulas": [
    {"latex": "a=b", "name": "新公式一", "confidence": 0.95},
    {"latex": "c=d", "name": "新公式二", "confidence": 0.9},
]}, ensure_ascii=False)


class ScriptedVision:
    """按脚本返回文本的识别客户端替身，并统计调用次数。"""

    def __init__(self, reply: str):
        self.calls = 0
        self._reply = reply

    async def vision(self, **kwargs):
        self.calls += 1
        return {"text": self._reply, "prompt_tokens": 0, "completion_tokens": 0}


class _Case(unittest.IsolatedAsyncioTestCase):
    """公共脚手架：插件 + 库 + 笔记存储 + 识别管道，装好即用。"""

    def setup_case(self) -> tuple:  # (plugin, db, store, pipeline, make_recognizer, root)
        root = Path(tempfile.mkdtemp(prefix="flow-"))
        self.addCleanup(shutil.rmtree, root, True)
        store = StudyNoteStore(root / "notes")
        plugin = smoke.ClassSchedulePlugin()  # type: ignore[attr-defined]
        plugin._set_context(smoke.FakeCtx(root))  # type: ignore[arg-type]
        plugin.set_plugin_config(smoke.build_config(study={"api_key": _FAKE_KEY}))
        plugin._data_dir = root
        plugin._notes = store
        plugin._state = PluginState()
        db = NotesDatabase(root / "notes.db")
        db.initialize()
        self.addCleanup(db.close)
        plugin._notes_db = db

        def make_recognizer(client):
            return FormulaRecognizer(db=db, client=client, model="vlm-test")

        pipeline = StudyPipeline(
            db=db,
            recognizer=make_recognizer(ScriptedVision(_REPLY_TWO)),
            on_recognized=plugin._on_formula_recognized,
        )
        plugin._pipeline = pipeline
        pipeline.start()
        self.addAsyncCleanup(pipeline.stop)
        return plugin, db, store, pipeline, make_recognizer, root

    async def send_image(self, plugin, image: bytes = _IMAGE) -> None:
        """发一条「记一下 + 图片」的消息并等后台收纳任务结束。"""
        await plugin.handle_note_capture(
            message={
                "session_id": "ps",
                "message_info": {"user_info": {"user_id": "654321"}},
                "raw_message": [
                    {"type": "text", "data": {"text": "记一下 这页课件"}},
                    {
                        "type": "image",
                        "data": {},
                        "binary_data_base64": base64.b64encode(image).decode(),
                    },
                ],
            },
            stream_id="ps",
        )
        for task in list(plugin._note_tasks):
            await task


class TestFirstSendResendAndRestart(_Case):
    """评估项要求的完整流程：首识多条 → 重发同一张图 → 重启后补识别。"""

    async def test_flow(self):
        plugin, db, store, _pipeline, make_recognizer, root = self.setup_case()
        fake = ScriptedVision(_REPLY_TWO)
        plugin._recognizer = make_recognizer(fake)
        plugin._pipeline.set_recognizer(plugin._recognizer)

        def said(fragment: str) -> bool:
            return any(
                fragment in text
                for _stream, text in plugin.ctx.send.texts  # type: ignore[attr-defined]
            )

        async def wait_for(predicate, *, timeout: float = 5.0) -> bool:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                if predicate():
                    return True
                await asyncio.sleep(0.01)
            return False

        # ① 首次识别：两条公式入库，回执列全
        await self.send_image(plugin)
        self.assertTrue(
            await wait_for(lambda: db.formula_count() == 2 and said("指数变换")),
            "首识没有把两条公式都落库/回执",
        )
        self.assertEqual(fake.calls, 1)

        # ② 重发同一张图：命中缓存，同样两条都回，模型一次不多调
        await self.send_image(plugin)
        self.assertTrue(
            await wait_for(
                lambda: store.count("未分类") == 2
                and fake.calls == 1
                and said("这张图之前认过")
                and said("微分定理")
                and said("指数变换"),
            ),
            "重发应当命中缓存并再次列出两条公式",
        )
        self.assertEqual(db.formula_count(), 2)

        # ③ 重启：新连接 + 补识别，公式不丢、不再付费、回填完整
        db.close()
        db2 = NotesDatabase(root / "notes.db")
        db2.initialize()
        self.addCleanup(db2.close)
        plugin._notes_db = db2
        fresh = ScriptedVision(_REPLY_TWO)
        plugin._recognizer = make_recognizer(fresh)
        plugin._pipeline.set_recognizer(plugin._recognizer)
        await plugin._run_formula_backfill()
        self.assertEqual(fresh.calls, 0, "重启补识别不该再付费")
        self.assertEqual(db2.formula_count(), 2)
        for note in store.recent("未分类", limit=5):
            self.assertIn("微分定理", note.formula)


class TestRerecognizeCommand(_Case):
    """/重识图片：主动重识入口（评估项 3）。"""

    async def test_clears_cache_and_reruns(self):
        plugin, db, store, _pipeline, make_recognizer, root = self.setup_case()
        image = b"\x89PNG-rerecognize"
        store.add_image_note("高等数学", "笔记", image, text="图说")
        old_id, _ = db.upsert_formula({
            "fingerprint": "fp-old", "name": "旧结果", "latex_normalized": "x=1",
        })
        digest = image_hash(image)
        db.record_image_recognition(digest, "recognized", [old_id], "vlm-old")
        # 别的图的放弃记录：不能被这次 /重识图片 误清
        db.record_formula_failure("another-image", "HTTP 400 模型不接受")
        fresh = ScriptedVision(_REPLY_NEW)
        plugin._recognizer = make_recognizer(fresh)
        plugin._pipeline.set_recognizer(plugin._recognizer)  # 管道也换到新替身

        ok, message, _ = await plugin.handle_rerecognize(**smoke.private_kwargs("ps"))
        self.assertTrue(ok)
        self.assertIn("1 张图", message)
        self.assertIsNone(db.image_recognition(digest), "识别缓存应当已清")
        self.assertIsNone(db.formula_failure(digest))
        self.assertIsNotNone(db.formula_failure("another-image"), "别图的放弃记录不该被误清")

        if plugin._backfill_task is not None:
            await plugin._backfill_task  # 命令自己调度的那次补识别扫描
        # 扫描只负责入队，识别由 worker 异步消费：等"识别记录已写回"而不是
        # 调用次数（calls 在响应返回时就 +1，落库可能还差一步）
        def rerun_done() -> bool:
            record = db.image_recognition(digest)
            if not (
                fresh.calls == 1
                and record is not None
                and str(record["status"]) == "recognized"
                and len(json.loads(str(record["formula_ids"] or "[]"))) == 2
            ):
                return False
            # 回执钩子在落库之后还会把公式写进笔记，等这一步也完成
            note = store.recent("高等数学", limit=1)[0]
            return "新公式一" in (note.formula or "")

        for _ in range(300):
            if rerun_done():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(rerun_done(), "重跑识别没有发生")
        record = db.image_recognition(digest)
        new_ids = json.loads(record["formula_ids"])
        self.assertEqual(len(new_ids), 2)
        self.assertNotIn(old_id, new_ids)
        refreshed = store.recent("高等数学", limit=1)[0]
        self.assertIn("新公式一", refreshed.formula)
        self.assertIn("新公式二", refreshed.formula)

    async def test_no_images_is_friendly(self):
        plugin, db, _store, _pipeline, make_recognizer, _root = self.setup_case()
        plugin._recognizer = make_recognizer(ScriptedVision(_REPLY_NEW))
        ok, message, _ = await plugin.handle_rerecognize(**smoke.private_kwargs("ps"))
        self.assertTrue(ok)
        self.assertIn("没有找到可重跑的图片", message)
        # 识别没启用时也要说清楚，而不是装作跑过
        plugin._recognizer = None
        ok, message, _ = await plugin.handle_rerecognize(**smoke.private_kwargs("ps"))
        self.assertIn("未启用", message)


class TestBackfillContinuation(_Case):
    """评估项 3：补识别超过单批上限时自动续跑，/重识图片 全量生效。"""

    async def test_backfill_continues_until_everything_is_done(self):
        from class_schedule import plugin as plugin_module

        plugin, db, store, _pipeline, make_recognizer, root = self.setup_case()
        fake = ScriptedVision(_REPLY_NEW)
        plugin._recognizer = make_recognizer(fake)
        plugin._pipeline.set_recognizer(plugin._recognizer)  # 管道同步换用新替身

        # 造 5 张待识别图，单批上限压到 2：必须跑 3 批才能全部覆盖
        for index in range(5):
            store.add_image_note("高等数学", "笔记", b"\x89PNG-backfill-%d" % index)
        original = plugin_module.MAX_BACKFILL_PER_RUN
        plugin_module.MAX_BACKFILL_PER_RUN = 2
        try:
            await plugin._run_formula_backfill()
        finally:
            plugin_module.MAX_BACKFILL_PER_RUN = original

        self.assertEqual(fake.calls, 5, "续跑没有覆盖到全部待识别图片")
        # 5 张图用同一段回复：指纹去重后库里只有 2 条不同的公式
        self.assertEqual(db.formula_count(), 2)
        for note in store.recent("高等数学", limit=10):
            self.assertIn("新公式一", note.formula)

        # 已全部有记录：再跑一轮不再调模型
        await plugin._run_formula_backfill()
        self.assertEqual(fake.calls, 5)

        # /重识图片 清掉全部缓存后，续跑同样要覆盖全部（而不是只跑前 2 张）
        ok, message, _ = await plugin.handle_rerecognize(**smoke.private_kwargs("ps"))
        self.assertIn("5 张图", message)
        for _ in range(600):
            if fake.calls == 10:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(fake.calls, 10, "/重识图片 只重跑了部分图片")
        self.assertEqual(db.formula_count(), 2)  # 指纹去重：还是那 2 条


if __name__ == "__main__":
    unittest.main()
