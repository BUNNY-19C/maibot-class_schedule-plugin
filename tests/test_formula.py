"""阶段 3' 重建测试：LaTeX 归一化、指纹去重、识别结果解析、VLM 识别与降级。

全部离线：识别客户端是替身（``FakeVision``），数据库用临时目录。
"""

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  —— 注册插件包

from class_schedule.formula import (
    FORMULA_PROMPT,
    UNKNOWN_FORMULA_NAME,
    FormulaParseError,
    FormulaRecognizer,
    fingerprint,
    image_hash,
    is_low_confidence,
    normalize_latex,
    parse_formula_response,
)
from class_schedule.llm_client import CloudError
from class_schedule.notes_db import NotesDatabase
from class_schedule.pipeline import StudyPipeline


class NormalizeLatexTest(unittest.TestCase):
    """归一化是纯函数：同一公式的各种写法必须收敛到同一个字符串。"""

    def test_unicode_commands_converge(self):
        self.assertEqual(normalize_latex("e^{iπ}+1=0"), normalize_latex(r"e^{i\pi}+1=0"))
        self.assertEqual(normalize_latex("π/2"), normalize_latex(r"\frac{\pi}{2}"))
        self.assertEqual(normalize_latex(r"\alpha \times \beta"), normalize_latex(r"\alpha\cdot\beta"))

    def test_fraction_macros_and_slash_converge(self):
        for variant in (r"\frac{1}{2}", r"\dfrac{1}{2}", r"\tfrac{1}{2}", "1/2", r"\frac12"):
            self.assertEqual(normalize_latex(variant), normalize_latex(r"\frac{1}{2}"), variant)

    def test_layout_noise_removed(self):
        self.assertEqual(
            normalize_latex(r"\left( x \right) \quad + \quad y"),
            normalize_latex("(x)+y"),
        )
        self.assertEqual(normalize_latex("$$x = 1$$"), normalize_latex("x=1"))
        self.assertEqual(normalize_latex(r"\text{速度} v = \frac{s}{t}"), normalize_latex(r"速度v=\frac{s}{t}"))

    def test_superscript_forms_converge(self):
        self.assertEqual(normalize_latex("x^(2)+y^2"), normalize_latex(r"x^{2}+y^{2}"))

    def test_sqrt_forms_converge(self):
        self.assertEqual(normalize_latex("√2"), normalize_latex(r"\sqrt{2}"))
        self.assertEqual(normalize_latex("√2"), r"\sqrt2")

    def test_idempotent(self):
        """不幂等会让同一公式二次识别时算出新指纹、又存一条。"""
        samples = [
            r"\oint \frac{\pi}{2} \times \sqrt{2} + \left[ x \right]",
            "e^(iπ)+1=0",
            r"\dfrac{1}{2}",
            "1/2",
            r"\frac{\frac{a}{b}}{c}",
            "",
            "随便一段不是公式的中文",
        ]
        for sample in samples:
            once = normalize_latex(sample)
            self.assertEqual(normalize_latex(once), once, sample)

    def test_garbage_never_raises_and_keeps_content(self):
        """结构不完整的输入原样留着——不能静默吞掉用户/模型的字节。"""
        for sample in (r"\frac{1}", r"\frac", "{{{{", "a//b", r"\sqrt{", "$$"):
            self.assertIsInstance(normalize_latex(sample), str)
        self.assertIn("x", normalize_latex(r"\frac{x}"))

    def test_empty(self):
        self.assertEqual(normalize_latex(None), "")
        self.assertEqual(fingerprint(""), "")
        self.assertEqual(fingerprint(normalize_latex("  ")), "")


class FingerprintTest(unittest.TestCase):
    def test_same_formula_same_fingerprint(self):
        first = fingerprint(normalize_latex("e^{iπ}+1=0"))
        second = fingerprint(normalize_latex(r"e^{i \pi} + 1 = 0"))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)

    def test_different_formula_different_fingerprint(self):
        self.assertNotEqual(
            fingerprint(normalize_latex("a+b")), fingerprint(normalize_latex("a-b"))
        )

    def test_image_hash_depends_on_bytes(self):
        self.assertEqual(image_hash(b"abc"), image_hash(b"abc"))
        self.assertNotEqual(image_hash(b"abc"), image_hash(b"abd"))
        self.assertEqual(image_hash(b""), "")


class ParseFormulaResponseTest(unittest.TestCase):
    def test_plain_json(self):
        parsed = parse_formula_response(
            json.dumps(
                {
                    "latex": r"\dfrac{\pi}{2}",
                    "name": "半角公式",
                    "aliases": ["α/2 公式"],
                    "category": "高等数学",
                    "subcategory": "三角函数",
                    "knowledge_points": ["半角", "三角函数"],
                    "description": "由 π/2 推出的公式",
                    "confidence": 0.9,
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual(parsed["name"], "半角公式")
        self.assertEqual(parsed["latex"], r"\dfrac{\pi}{2}")  # 原文保留
        self.assertEqual(parsed["latex_normalized"], normalize_latex(r"\frac{\pi}{2}"))
        self.assertEqual(parsed["aliases"], ["α/2 公式"])
        self.assertEqual(parsed["confidence"], 0.9)
        self.assertFalse(is_low_confidence(parsed))

    def test_fenced_and_noisy_response(self):
        """模型经常套一层 ```json 和解释文字，必须能读出来。"""
        raw = "好的，这是识别结果：\n```json\n" + json.dumps(
            {"latex": "a^2+b^2=c^2", "name": "勾股定理", "confidence": 0.95}
        ) + "\n```\n希望对你有帮助。"
        parsed = parse_formula_response(raw)
        self.assertEqual(parsed["name"], "勾股定理")
        self.assertEqual(parsed["latex_normalized"], "a^{2}+b^{2}=c^{2}")

    def test_tolerates_string_lists_and_percent_confidence(self):
        parsed = parse_formula_response(
            json.dumps(
                {
                    "latex": "F=ma",
                    "name": "牛顿第二定律",
                    "aliases": "牛顿二定律、F=ma 定律",
                    "knowledge_points": "力、加速度",
                    "confidence": 90,
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual(parsed["aliases"], ["牛顿二定律", "F=ma 定律"])
        self.assertEqual(parsed["knowledge_points"], ["力", "加速度"])
        self.assertEqual(parsed["confidence"], 0.9)

    def test_unknown_name_is_low_confidence(self):
        parsed = parse_formula_response(
            json.dumps({"latex": "x=1", "name": UNKNOWN_FORMULA_NAME, "confidence": 0.9})
        )
        self.assertTrue(is_low_confidence(parsed))
        # 名称缺失也按未知处理，不能编名字
        parsed = parse_formula_response(json.dumps({"latex": "x=1", "confidence": 0.9}))
        self.assertEqual(parsed["name"], UNKNOWN_FORMULA_NAME)
        self.assertTrue(is_low_confidence(parsed))

    def test_unusable_responses_raise(self):
        for raw in ("", "   ", "我看不清这张图", json.dumps({"name": "欧拉公式"}), "[1,2]"):
            with self.assertRaises(FormulaParseError, msg=raw):
                parse_formula_response(raw)

    def test_prompt_asks_for_the_contract_we_parse(self):
        """提示词与解析器是一份契约：键名必须对齐，否则模型答对了也白搭。"""
        for key in (
            "latex", "name", "aliases", "category", "subcategory",
            "knowledge_points", "description", "confidence",
        ):
            self.assertIn(key, FORMULA_PROMPT)
        self.assertIn(UNKNOWN_FORMULA_NAME, FORMULA_PROMPT)


class FakeVision:
    """识别客户端替身：按脚本返回文本或抛错，并记录调用参数。"""

    def __init__(self, responses):
        self.calls = []
        self._responses = list(responses)

    async def vision(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        item = self._responses[index]
        if isinstance(item, Exception):
            raise item
        return {"text": item, "prompt_tokens": 1, "completion_tokens": 1}


def _db_case(case: unittest.TestCase) -> NotesDatabase:
    """临时目录经 addCleanup 删除；cleanup 是 LIFO——先关库再删目录。"""
    root = Path(tempfile.mkdtemp(prefix="formula-"))
    case.addCleanup(shutil.rmtree, root, True)
    db = NotesDatabase(root / "notes.db")
    db.initialize()
    case.addCleanup(db.close)
    return db


_FORMULA_JSON = json.dumps(
    {
        "latex": r"\dfrac{\pi}{2}",
        "name": "半角公式",
        "aliases": ["半角"],
        "category": "高等数学",
        "subcategory": "三角函数",
        "knowledge_points": ["半角公式"],
        "description": "由半角推出的公式",
        "confidence": 0.9,
    },
    ensure_ascii=False,
)


class TestFormulaRecognizer(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = _db_case(self)

    def _recognizer(self, responses, **kwargs) -> FormulaRecognizer:
        client = FakeVision(responses)
        self.client = client
        return FormulaRecognizer(
            db=self.db,
            client=client,
            model=kwargs.pop("model", "vlm-primary"),
            fallback_model=kwargs.pop("fallback_model", "vlm-fallback"),
            **kwargs,
        )

    async def test_success_stores_formula_and_tags(self):
        recognizer = self._recognizer([_FORMULA_JSON])
        result = await recognizer.recognize(
            b"\x89PNGx", course="高等数学", week=3, period="第3节", message_id="m1"
        )
        self.assertEqual(result["status"], "recognized")
        self.assertTrue(result["created"])
        self.assertEqual(result["name"], "半角公式")
        self.assertFalse(result["low_confidence"])
        self.assertFalse(result["degraded"])
        row = self.db.formula_by_fingerprint(fingerprint(normalize_latex(r"\frac{\pi}{2}")))
        self.assertIsNotNone(row)
        self.assertEqual(row["course"], "高等数学")
        self.assertEqual(row["period"], "第3节")
        self.assertEqual(row["source_message_id"], "m1")
        tags = self.db.formula_tags_for(int(row["id"]))
        self.assertIn("#公式", tags)
        self.assertIn("#高等数学", tags)
        self.assertNotIn("#待确认", tags)
        # 发给模型的是图片字节与提示词，不是本地路径
        self.assertEqual(self.client.calls[0]["model"], "vlm-primary")
        self.assertIn("公式识别", self.client.calls[0]["prompt"])

    async def test_same_formula_twice_is_not_duplicated(self):
        """不同图片、同一个公式 → 只有一条公式记录（指纹去重）。"""
        recognizer = self._recognizer([_FORMULA_JSON])
        await recognizer.recognize(b"image-one")
        second = await recognizer.recognize(b"image-two")
        self.assertFalse(second["created"])  # 第二个只是"又见到"
        self.assertEqual(self.db.formula_count(), 1)

    async def test_image_hash_cache_skips_model(self):
        recognizer = self._recognizer([_FORMULA_JSON])
        first = await recognizer.recognize(b"same-bytes")
        second = await recognizer.recognize(b"same-bytes")
        self.assertEqual(second["status"], "cached")
        self.assertEqual(second["formula_id"], first["formula_id"])
        self.assertEqual(len(self.client.calls), 1)  # 第二次没有再调模型
        self.assertEqual(recognizer.cached_count, 1)

    async def test_cache_can_be_disabled(self):
        recognizer = self._recognizer([_FORMULA_JSON], image_cache_enabled=False)
        await recognizer.recognize(b"same-bytes")
        await recognizer.recognize(b"same-bytes")
        self.assertEqual(len(self.client.calls), 2)

    async def test_primary_failure_falls_back_once(self):
        recognizer = self._recognizer(
            [CloudError("HTTP 503 模型繁忙"), _FORMULA_JSON]
        )
        result = await recognizer.recognize(b"img")
        self.assertEqual(result["status"], "recognized")
        self.assertTrue(result["degraded"])
        models = [call["model"] for call in self.client.calls]
        self.assertEqual(models, ["vlm-primary", "vlm-fallback"])

    async def test_unparseable_answer_triggers_fallback(self):
        recognizer = self._recognizer(["这张图里没有公式", _FORMULA_JSON])
        result = await recognizer.recognize(b"img")
        self.assertTrue(result["degraded"])
        self.assertEqual(len(self.client.calls), 2)

    async def test_all_failures_report_failure_without_row(self):
        recognizer = self._recognizer([CloudError("网络错误"), CloudError("网络错误")])
        result = await recognizer.recognize(b"img")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["formula_id"], 0)
        self.assertIn("vlm-primary", result["error"])
        self.assertIn("vlm-fallback", result["error"])
        self.assertEqual(self.db.formula_count(), 0)
        self.assertEqual(recognizer.failed_count, 1)

    async def test_empty_image_is_failure(self):
        recognizer = self._recognizer([_FORMULA_JSON])
        result = await recognizer.recognize(b"")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.client.calls, [])

    async def test_low_confidence_marked_for_review(self):
        low = json.dumps(
            {"latex": "x=1", "name": UNKNOWN_FORMULA_NAME, "confidence": 0.2}
        )
        recognizer = self._recognizer([low])
        result = await recognizer.recognize(b"img", course="未分类")
        self.assertTrue(result["low_confidence"])
        tags = self.db.formula_tags_for(result["formula_id"])
        self.assertIn("#待确认", tags)
        self.assertNotIn("#未分类", tags)  # 未分类不加课程标签

    async def test_failure_never_raises_even_if_db_is_broken(self):
        """识别是增强层：DB 坏了也要返失败，不能把异常抛进 worker。"""
        import sqlite3

        class BrokenDb:
            def __getattr__(self, name):
                raise sqlite3.OperationalError(f"disk I/O error（{name}）")

        client = FakeVision([_FORMULA_JSON])
        recognizer = FormulaRecognizer(db=BrokenDb(), client=client, model="vlm")
        result = await recognizer.recognize(b"img")
        self.assertEqual(result["status"], "failed")
        self.assertIn("落库失败", result["error"])

    async def test_deep_fraction_nesting_never_blows_up(self):
        """病态输入（五百层 \\frac 嵌套）不能把归一化炸掉。"""
        deep = r"\frac{" * 500 + "1" + "}" * 500
        self.assertIsInstance(normalize_latex(deep), str)

    async def test_internal_api_address_fails_at_call_time(self):
        """内网 https 地址过得了装配（装配只看协议），真正拦它的是每次调用前的校验。

        这条钉住降级形态：识别失败、不落公式、原因留在 error 里——笔记本身照收。
        """
        from class_schedule.llm_client import SiliconFlowClient

        client = SiliconFlowClient("k", base_url="https://127.0.0.1:9/v1", max_retries=0)
        self.assertTrue(client.configured)  # 装配期看不出来
        recognizer = FormulaRecognizer(db=self.db, client=client, model="vlm")
        result = await recognizer.recognize(b"img")
        self.assertEqual(result["status"], "failed")
        self.assertIn("不被允许", result["error"])
        self.assertEqual(self.db.formula_count(), 0)

    async def test_recognizer_does_not_block_event_loop(self):
        import time

        recognizer = self._recognizer([_FORMULA_JSON])
        start = time.perf_counter()
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(ticker())
        await recognizer.recognize(b"img")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertGreater(ticks, 0)
        self.assertGreater(time.perf_counter() - start, 0)  # 只是别卡死


class TestPipelineQueue(unittest.IsolatedAsyncioTestCase):
    """worker 队列：消费、满则丢、停机取消——都很容易写成死锁，逐条钉住。"""

    def setUp(self):
        self.db = _db_case(self)

    async def test_queue_full_drops_without_blocking(self):
        class BlockingRecognizer:
            def __init__(self):
                self.gate = asyncio.Event()
                self.started = asyncio.Event()

            async def recognize(self, *args, **kwargs):
                self.started.set()
                await self.gate.wait()
                return {"status": "failed", "created": False}

        recognizer = BlockingRecognizer()
        pipeline = StudyPipeline(db=self.db, recognizer=recognizer, queue_size=4)
        pipeline.start()
        self.addAsyncCleanup(pipeline.stop)
        accepted = [pipeline.enqueue({"image": b"x"}) for _ in range(10)]
        # 入队是纯同步的，worker 还没机会跑：队列只能是 4 个位置
        self.assertEqual(sum(accepted), 4)
        self.assertEqual(pipeline.dropped, 6)
        self.assertEqual(pipeline.queue_depth, 4)
        recognizer.gate.set()
        await asyncio.wait_for(recognizer.started.wait(), timeout=2)

    async def test_process_returns_note_id_before_recognition(self):
        """落库必须同步完成：用户发完消息就该能搜到，不等识别。"""
        class NeverCalled:
            async def recognize(self, *args, **kwargs):
                raise AssertionError("识别不该在落库路径里同步执行")

        pipeline = StudyPipeline(
            db=self.db, recognizer=NeverCalled(), queue_size=4,
        )
        pipeline.start()
        self.addAsyncCleanup(pipeline.stop)
        from class_schedule.inbox import ParsedMessage

        note_id = await pipeline.process(
            ParsedMessage(text="欧拉公式", images=[(b"\x89PNGx", ".png")], message_id="m1"),
            course="未分类",
            kind="公式",
        )
        self.assertGreater(note_id, 0)
        self.assertEqual(len(self.db.search_text("欧拉")), 1)
        self.assertIn("#公式", self.db.tags_for(note_id))
        self.assertEqual(pipeline.enqueued, 1)
        await asyncio.sleep(0)  # worker 起来消费掉，别留悬挂任务

    async def test_worker_survives_bad_job(self):
        """一条任务炸了不能带走 worker：后面那条还得被处理。"""
        class HalfBroken:
            def __init__(self):
                self.calls = []

            async def recognize(self, image, **kwargs):
                self.calls.append(image)
                if image == b"bad":
                    raise RuntimeError("模拟识别器内部异常")
                return {"status": "failed", "created": False}

        recognizer = HalfBroken()
        pipeline = StudyPipeline(db=self.db, recognizer=recognizer, queue_size=8)
        pipeline.start()
        self.addAsyncCleanup(pipeline.stop)
        pipeline.enqueue({"image": b"bad"})
        pipeline.enqueue({"image": b"good"})
        for _ in range(200):
            if recognizer.calls == [b"bad", b"good"]:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(recognizer.calls, [b"bad", b"good"])
        self.assertTrue(pipeline.errors)
        self.assertTrue(pipeline.running)

    async def test_stop_cancels_workers_and_stops_accepting(self):
        class BlockingRecognizer:
            async def recognize(self, *args, **kwargs):
                await asyncio.Event().wait()

        pipeline = StudyPipeline(db=self.db, recognizer=BlockingRecognizer())
        pipeline.start()
        pipeline.enqueue({"image": b"x"})
        await asyncio.sleep(0)
        await pipeline.stop()
        self.assertFalse(pipeline.running)
        self.assertEqual(pipeline._workers, [])
        self.assertFalse(pipeline.enqueue({"image": b"y"}))

    async def test_no_recognizer_means_no_queue_traffic(self):
        """没配 Key（recognizer=None）时，图片照常落库、不产生任何队列任务。"""
        from class_schedule.inbox import ParsedMessage

        pipeline = StudyPipeline(db=self.db)
        pipeline.start()
        self.addAsyncCleanup(pipeline.stop)
        note_id = await pipeline.process(
            ParsedMessage(text="", images=[(b"\x89PNGx", ".png")]), course="未分类"
        )
        self.assertGreater(note_id, 0)
        self.assertEqual(pipeline.enqueued, 0)
        self.assertEqual(pipeline.queue_depth, 0)


class TestNotesDbFormulaLookups(unittest.TestCase):
    def test_lookup_helpers(self):
        db = _db_case(self)
        self.assertIsNone(db.formula_by_fingerprint(""))
        self.assertIsNone(db.formula_by_image_hash(""))
        self.assertEqual(db.formula_count(), 0)
        formula_id = db.upsert_formula(
            {"fingerprint": "fp-1", "name": "欧拉公式", "image_hash": "img-1"}
        )
        self.assertEqual(db.formula_count(), 1)
        self.assertEqual(db.formula_by_fingerprint("fp-1")["id"], formula_id)
        self.assertEqual(db.formula_by_image_hash("img-1")["id"], formula_id)
        # 同一张图再识别一次（新指纹）时返回最新那条，而不是最旧的
        newer = db.upsert_formula(
            {"fingerprint": "fp-2", "name": "欧拉公式(修正)", "image_hash": "img-1"}
        )
        self.assertEqual(db.formula_by_image_hash("img-1")["id"], newer)
        db.attach_tag("formula", formula_id, "#公式")
        self.assertEqual(db.formula_tags_for(formula_id), ["#公式"])


if __name__ == "__main__":
    unittest.main()
