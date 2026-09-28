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
from unittest.mock import patch

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


class TestBackfillConsistency(_Case):
    async def test_note_moved_after_scan_is_still_recognized(self):
        plugin, db, store, pipeline, make_recognizer, _root = self.setup_case()
        fake = ScriptedVision(_REPLY_NEW)
        plugin._recognizer = make_recognizer(fake)
        pipeline.set_recognizer(plugin._recognizer)
        note = store.add_image_note("未分类", "笔记", _IMAGE)
        original_lookup = db.image_recognition

        def move_after_scan(digest):
            store.move_note("未分类", note.id, "高等数学")
            return original_lookup(digest)

        with patch.object(db, "image_recognition", side_effect=move_after_scan):
            await plugin._run_formula_backfill()

        self.assertEqual(fake.calls, 1)
        self.assertIn("新公式一", store.last_of("高等数学").formula)

    async def test_transient_failure_is_attempted_once_for_duplicate_notes(self):
        from class_schedule.llm_client import CloudError

        plugin, db, store, pipeline, make_recognizer, _root = self.setup_case()

        class BusyVision(ScriptedVision):
            async def vision(self, **kwargs):
                self.calls += 1
                raise CloudError("HTTP 503 模型繁忙")

        fake = BusyVision("")
        plugin._recognizer = make_recognizer(fake)
        pipeline.set_recognizer(plugin._recognizer)
        for course in ("高等数学", "机械设计"):
            note = store.add_image_note(course, "笔记", _IMAGE)
            store.attach_formula(course, note.id, "保留上次结果")

        await asyncio.wait_for(plugin._run_formula_backfill(), 5)

        self.assertEqual(fake.calls, 1)
        self.assertIsNone(db.formula_failure(image_hash(_IMAGE)))
        for course in ("高等数学", "机械设计"):
            self.assertEqual(store.last_of(course).formula, "保留上次结果")

    async def test_duplicate_images_fill_every_note_with_one_model_call(self):
        plugin, db, store, pipeline, make_recognizer, _root = self.setup_case()
        fake = ScriptedVision(_REPLY_NEW)
        plugin._recognizer = make_recognizer(fake)
        pipeline.set_recognizer(plugin._recognizer)
        for course in ("高等数学", "机械设计"):
            store.add_image_note(course, "笔记", _IMAGE)

        await plugin._run_formula_backfill()

        self.assertEqual(fake.calls, 1)
        for course in ("高等数学", "机械设计"):
            self.assertIn("新公式一", store.last_of(course).formula)

    async def test_cached_empty_result_clears_every_stale_note(self):
        plugin, db, store, pipeline, make_recognizer, _root = self.setup_case()
        fake = ScriptedVision(_REPLY_NEW)
        plugin._recognizer = make_recognizer(fake)
        pipeline.set_recognizer(plugin._recognizer)
        for course in ("高等数学", "机械设计"):
            note = store.add_image_note(course, "笔记", _IMAGE, text="保留图说")
            store.attach_formula(course, note.id, "过期公式：x=1")
        db.record_image_recognition(image_hash(_IMAGE), "no_formula", [], "test")
        db.record_image_draft(image_hash(_IMAGE), {})

        await plugin._run_formula_backfill()

        self.assertEqual(fake.calls, 0)
        for course in ("高等数学", "机械设计"):
            self.assertEqual(store.last_of(course).formula, "")
            self.assertEqual(store.last_of(course).text, "保留图说")

    async def test_smaller_successful_result_replaces_previous_formulas(self):
        plugin, db, store, pipeline, make_recognizer, _root = self.setup_case()
        note = store.add_image_note("高等数学", "笔记", _IMAGE, text="保留图说")
        store.attach_formula("高等数学", note.id, "公式一：x=1\n公式二：y=2")

        await plugin._on_formula_recognized(
            {"note_ref": note.id, "course": "高等数学", "stream_id": ""},
            {"status": "recognized", "formulas": [{"name": "公式一", "latex": "x=1"}]},
        )

        self.assertEqual(store.last_of("高等数学").formula, "公式一：x=1")
        body = (store.course_dir("高等数学") / f"{note.id}_{note.kind}.md").read_text(encoding="utf-8")
        self.assertNotIn("公式二", body)
        self.assertIn("保留图说", body)

    async def test_full_queue_does_not_abandon_historical_images(self):
        plugin, db, store, old_pipeline, make_recognizer, _root = self.setup_case()
        await old_pipeline.stop()
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowVision(ScriptedVision):
            async def vision(self, **kwargs):
                entered.set()
                await release.wait()
                return await super().vision(**kwargs)

        fake = SlowVision(_REPLY_NEW)
        plugin._recognizer = make_recognizer(fake)
        pipeline = StudyPipeline(
            db=db, queue_size=1, recognizer=plugin._recognizer,
            on_recognized=plugin._on_formula_recognized,
        )
        plugin._pipeline = pipeline
        pipeline.start()
        self.addAsyncCleanup(pipeline.stop)
        pipeline.enqueue({"image": b"busy"})
        await entered.wait()
        pipeline.enqueue({"image": b"waiting"})
        note = store.add_image_note("高等数学", "笔记", _IMAGE)
        scan_done = asyncio.Event()
        original_scan = plugin._note_image_files

        def scan():
            result = original_scan()
            loop.call_soon_threadsafe(scan_done.set)
            return result

        loop = asyncio.get_running_loop()
        plugin._note_image_files = scan
        task = asyncio.create_task(plugin._run_formula_backfill())
        try:
            await scan_done.wait()
            # Let the producer reach the full queue while the worker is held.
            await asyncio.sleep(0.05)
            release.set()
            await asyncio.wait_for(task, 5)
            await pipeline.drain()
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertIn("新公式一", store.last_of("高等数学").formula)
        self.assertEqual(pipeline.dropped, 0)


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
        with patch.object(plugin_module, "MAX_BACKFILL_PER_RUN", 2), patch.object(
            plugin, "_note_image_files", wraps=plugin._note_image_files
        ) as scan:
            await plugin._run_formula_backfill()
        self.assertEqual(scan.call_count, 1, "分批消费不应反复扫描历史目录")

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


class TestNoFormulaReplacesOldContent(_Case):
    """评估项 4：重识别判定"没有公式"时，笔记里的旧自动公式要清掉。"""

    async def test_no_formula_clears_but_failure_keeps(self):
        plugin, db, store, _pipeline, make_recognizer, _root = self.setup_case()
        image = b"\x89PNG-recycled-slide"
        note = store.add_image_note("高等数学", "笔记", image, text="用户的课件图说")
        # 旧识别留下的公式
        await plugin._on_formula_recognized(
            {"note_ref": note.id, "course": "高等数学", "stream_id": ""},
            {
                "status": "recognized",
                "degraded": False,
                "error": "",
                "formulas": [{
                    "name": "旧公式", "latex": "x=1",
                    "confidence": 0.9, "low_confidence": False, "created": True,
                }],
            },
        )
        self.assertIn("旧公式", store.recent("高等数学", limit=1)[0].formula)
        formulas_before = db.formula_count()

        # 新模型判定"这张图里没有公式"：笔记里的旧自动公式要被清掉
        await plugin._on_formula_recognized(
            {"note_ref": note.id, "course": "高等数学", "stream_id": ""},
            {"status": "no_formula", "formulas": [], "degraded": False, "error": ""},
        )
        refreshed = store.recent("高等数学", limit=1)[0]
        self.assertEqual(refreshed.formula, "")
        self.assertIn("用户的课件图说", refreshed.text)  # 用户/宿主的文字保留
        self.assertEqual(db.formula_count(), formulas_before)  # 公式库记录不删
        body = (
            store.course_dir("高等数学") / f"{note.id}_{note.kind}.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn("旧公式", body)
        self.assertIn("用户的课件图说", body)  # md 只剩图说与原图引用

        # 识别失败则保留旧内容不动
        await plugin._on_formula_recognized(
            {"note_ref": note.id, "course": "高等数学", "stream_id": ""},
            {"status": "failed", "error": "读超时", "transient": True, "formulas": []},
        )
        self.assertEqual(store.recent("高等数学", limit=1)[0].formula, "")

        # 缓存的 no_formula（重发）同样不复活公式
        await plugin._on_formula_recognized(
            {"note_ref": note.id, "course": "高等数学", "stream_id": ""},
            {"status": "cached", "cached_no_formula": True, "formulas": []},
        )
        self.assertEqual(store.recent("高等数学", limit=1)[0].formula, "")


if __name__ == "__main__":
    unittest.main()
