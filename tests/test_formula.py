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
    MAX_FAILED_ATTEMPTS,
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


class TestNormalizeLatex(unittest.TestCase):
    """归一化是纯函数：同一公式的各种写法必须收敛到同一个字符串。"""

    def test_unicode_commands_converge(self):
        self.assertEqual(normalize_latex("e^{iπ}+1=0"), normalize_latex(r"e^{i\pi}+1=0"))
        self.assertEqual(normalize_latex("π/2"), normalize_latex(r"\frac{\pi}{2}"))
        self.assertEqual(normalize_latex(r"\alpha \times \beta"), normalize_latex(r"\alpha\cdot\beta"))

    def test_fraction_macros_and_slash_converge(self):
        for variant in (
            r"\frac{1}{2}", r"\dfrac{1}{2}", r"\tfrac{1}{2}", r"\tfrac12",
            r"\frac12", "1/2",
        ):
            self.assertEqual(normalize_latex(variant), r"\frac12", variant)
        self.assertEqual(normalize_latex(r"\frac{a}{b}"), normalize_latex("a/b"))
        self.assertEqual(normalize_latex(r"a\div b"), normalize_latex("a/b"))

    def test_equation_with_fraction_is_not_reshaped(self):
        """回归（线上真实返回）：等式里带分式不能被"按斜杠切段补括号"重新结合。

        模型给的是 ``[\\sigma]=\\frac{\\sigma_{\\lim}}{S_{\\sigma}}=…``，
        旧实现按顶层斜杠切段补括号，产出
        ``([\\sigma]=(\\sigma_{\\lim}))/((S_{\\sigma})=…)``——两个等号被塞进分母，
        公式的意思就没了。最常见形状 ``x=\\frac{a}{b}`` 同样会被弄成 ``(x=(a))/(b)``。
        """
        source = (
            r"[\sigma]=\frac{\sigma_{\lim}}{S_{\sigma}}"
            r"=\frac{\sigma_{S}}{S_{\sigma}}"
        )
        self.assertEqual(normalize_latex(source), source)
        self.assertEqual(normalize_latex(r"x=\frac{a}{b}"), r"x=\fracab")
        self.assertEqual(normalize_latex("x=a/b"), r"x=\fracab")
        self.assertEqual(normalize_latex(r"v=\frac{s}{t}"), normalize_latex("v=s/t"))

    def test_multiplication_after_division_keeps_precedence(self):
        """``a/b\\cdot c`` 是 ``(a/b)\\cdot c``：右边不能被吃成 ``b\\cdot c``。

        根因是去空白把 ``\\cdot c`` 粘成了未定义宏 ``\\cdotc``，操作数扫描于是
        一路吃到 ``c``，算出 ``a/(b\\cdot c)``。
        """
        self.assertEqual(normalize_latex(r"a/b\cdot c"), r"\fracab\cdot c")
        self.assertEqual(normalize_latex(r"a \cdot b/c"), r"a\cdot\fracbc")
        self.assertEqual(normalize_latex(r"a/b/c"), r"\frac{\fracab}c")

    def test_macro_followed_by_space_is_not_fused(self):
        """``\\pi r^2`` 不能被去空白粘成 ``\\pir^2``（未定义宏）。"""
        self.assertEqual(normalize_latex(r"\pi r^2"), r"\pi r^{2}")
        self.assertEqual(normalize_latex(r"\sin x+\cos y"), r"\sin x+\cos y")
        self.assertEqual(normalize_latex(r"\int f(x) dx"), r"\int f(x)dx")

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

    def test_sqrt_with_braces_converges_too(self):
        """回归（构造审查）：``√{2}`` 与 ``√2`` 必须同一个指纹。

        旧顺序把"将符号收进 \\sqrt{}"排在剥单字符花括号之前，``√{2}`` 会先变成
        ``\\sqrt{}{2}``、再漏成 ``\\sqrt{}2``——同一公式两个指纹，正是要防的事故。
        """
        self.assertEqual(normalize_latex("√{2}"), normalize_latex("√2"))
        self.assertEqual(normalize_latex(r"\sqrt{2}"), normalize_latex("√{2}"))
        self.assertEqual(normalize_latex("√{ab}"), normalize_latex(r"\sqrt{ab}"))

    def test_idempotent(self):
        """不幂等会让同一公式二次识别时算出新指纹、又存一条。"""
        samples = [
            r"\oint \frac{\pi}{2} \times \sqrt{2} + \left[ x \right]",
            "e^(iπ)+1=0",
            r"\dfrac{1}{2}",
            "1/2",
            "√{2}",
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


class TestFingerprint(unittest.TestCase):
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


class TestParseFormulaResponse(unittest.TestCase):
    def test_plain_json(self):
        parsed = parse_formula_response(
            json.dumps(
                {
                    "formulas": [
                        {
                            "latex": r"\dfrac{\pi}{2}",
                            "name": "半角公式",
                            "aliases": ["α/2 公式"],
                            "category": "高等数学",
                            "subcategory": "三角函数",
                            "knowledge_points": ["半角", "三角函数"],
                            "description": "由 π/2 推出的公式",
                            "confidence": 0.9,
                        }
                    ]
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual(len(parsed), 1)
        entry = parsed[0]
        self.assertEqual(entry["name"], "半角公式")
        self.assertEqual(entry["latex"], r"\dfrac{\pi}{2}")  # 原文保留
        self.assertEqual(entry["latex_normalized"], normalize_latex(r"\frac{\pi}{2}"))
        self.assertEqual(entry["aliases"], ["α/2 公式"])
        self.assertEqual(entry["confidence"], 0.9)
        self.assertFalse(is_low_confidence(entry))

    def test_all_formulas_in_one_image_are_kept(self):
        """回归（准确率）：一页课件几条公式就收几条，不再只挑"最主要的那一条"。"""
        parsed = parse_formula_response(
            json.dumps(
                {
                    "formulas": [
                        {"latex": r"\sigma_{b}=\frac{F}{A}", "name": "应力公式", "confidence": 0.9},
                        {"latex": r"F=ma", "name": "牛顿第二定律", "confidence": 0.95},
                        {"latex": r"\tau=\frac{T}{W_{p}}", "name": "切应力公式", "confidence": 0.8},
                    ]
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual(
            [item["name"] for item in parsed], ["应力公式", "牛顿第二定律", "切应力公式"]
        )
        self.assertEqual(len({item["fingerprint"] for item in parsed}), 3)

    def test_duplicated_formula_in_answer_is_collapsed(self):
        parsed = parse_formula_response(
            json.dumps(
                {
                    "formulas": [
                        {"latex": r"\frac{1}{2}", "name": "半", "confidence": 0.9},
                        {"latex": "1/2", "name": "半(别名)", "confidence": 0.9},
                    ]
                },
                ensure_ascii=False,
            )
        )
        self.assertEqual(len(parsed), 1)

    def test_empty_array_means_no_formula(self):
        """模型明确说"图里没有公式"是合法结果，不是解析失败。"""
        self.assertEqual(parse_formula_response('{"formulas": []}'), [])

    def test_fenced_and_noisy_response(self):
        """模型经常套一层 ```json 和解释文字，必须能读出来。"""
        raw = "好的，这是识别结果：\n```json\n" + json.dumps(
            {"formulas": [{"latex": "a^2+b^2=c^2", "name": "勾股定理", "confidence": 0.95}]}
        ) + "\n```\n希望对你有帮助。"
        parsed = parse_formula_response(raw)
        self.assertEqual(parsed[0]["name"], "勾股定理")
        self.assertEqual(parsed[0]["latex_normalized"], "a^{2}+b^{2}=c^{2}")

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
        self.assertEqual(len(parsed), 1)  # 没按数组包的旧形状也能读
        self.assertEqual(parsed[0]["aliases"], ["牛顿二定律", "F=ma 定律"])
        self.assertEqual(parsed[0]["knowledge_points"], ["力", "加速度"])
        self.assertEqual(parsed[0]["confidence"], 0.9)

    def test_trailing_text_and_second_object_are_ignored(self):
        """模型在对象后面又补一段话（甚至再吐一个对象）时，取第一个完整值。"""
        raw = (
            '{"formulas": [{"latex": "a+b", "name": "加法", "confidence": 0.8}]}\n'
            "说明：这是加法公式（如果你还需要 {另一个} 结果，请告知）\n"
            '{"latex": "c+d", "name": "另一个", "confidence": 0.1}'
        )
        parsed = parse_formula_response(raw)
        self.assertEqual([item["name"] for item in parsed], ["加法"])

    def test_unknown_name_is_low_confidence(self):
        parsed = parse_formula_response(
            json.dumps({"formulas": [{"latex": "x=1", "name": UNKNOWN_FORMULA_NAME, "confidence": 0.9}]})
        )
        self.assertTrue(is_low_confidence(parsed[0]))
        # 名称缺失也按未知处理，不能编名字
        parsed = parse_formula_response(json.dumps({"formulas": [{"latex": "x=1", "confidence": 0.9}]}))
        self.assertEqual(parsed[0]["name"], UNKNOWN_FORMULA_NAME)
        self.assertTrue(is_low_confidence(parsed[0]))

    def test_unusable_responses_raise(self):
        for raw in ("", "   ", "我看不清这张图", '{"latex": "", "confidence": 0}'):
            with self.assertRaises(FormulaParseError, msg=raw):
                parse_formula_response(raw)

    def test_prompt_asks_for_the_contract_we_parse(self):
        """提示词与解析器是一份契约：键名与"要全部公式"的要求必须对齐。"""
        for key in (
            "formulas", "latex", "name", "aliases", "category", "subcategory",
            "knowledge_points", "description", "confidence",
        ):
            self.assertIn(key, FORMULA_PROMPT)
        self.assertIn(UNKNOWN_FORMULA_NAME, FORMULA_PROMPT)
        self.assertIn("不要只挑", FORMULA_PROMPT)  # 覆盖率的关键一句
        # 质量护栏：真实 A/B 里模型会把"令 x=y"、推导中间步骤也当公式收回来
        self.assertIn("只收独立成行的式子", FORMULA_PROMPT)
        self.assertIn("变量代换", FORMULA_PROMPT)
        self.assertIn("完整式子", FORMULA_PROMPT)  # "左式/右式"要展开写


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
        entry = result["formulas"][0]
        self.assertTrue(entry["created"])
        self.assertEqual(entry["name"], "半角公式")
        self.assertFalse(entry["low_confidence"])
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
        # 发给模型的是图片字节与提示词，不是本地路径；温度必须是 0（要可复现）
        self.assertEqual(self.client.calls[0]["model"], "vlm-primary")
        self.assertIn("公式识别", self.client.calls[0]["prompt"])
        self.assertEqual(self.client.calls[0]["temperature"], 0.0)

    async def test_several_formulas_in_one_image_all_get_stored(self):
        """回归（准确率/覆盖率）：一页课件几条就存几条，各自打标各自可查。"""
        reply = json.dumps(
            {
                "formulas": [
                    {"latex": r"\sigma_{b}=\frac{F}{A}", "name": "应力公式", "confidence": 0.9},
                    {"latex": "F=ma", "name": "牛顿第二定律", "confidence": 0.95},
                    {"latex": "?", "name": UNKNOWN_FORMULA_NAME, "confidence": 0.2},
                ]
            },
            ensure_ascii=False,
        )
        recognizer = self._recognizer([reply])
        result = await recognizer.recognize(b"\x89PNGthree", course="机械设计")
        self.assertEqual(len(result["formulas"]), 3)
        self.assertEqual(self.db.formula_count(), 3)
        self.assertTrue(result["formulas"][2]["low_confidence"])
        tagged = self.db.formula_tags_for(int(result["formulas"][2]["formula_id"]))
        self.assertIn("#待确认", tagged)
        # 前两条不该被标成待确认
        self.assertNotIn(
            "#待确认", self.db.formula_tags_for(int(result["formulas"][1]["formula_id"]))
        )

    async def test_same_formula_twice_is_not_duplicated(self):
        """不同图片、同一个公式 → 只有一条公式记录（指纹去重）。"""
        recognizer = self._recognizer([_FORMULA_JSON])
        await recognizer.recognize(b"image-one")
        second = await recognizer.recognize(b"image-two")
        self.assertFalse(second["formulas"][0]["created"])  # 第二个只是"又见到"
        self.assertEqual(self.db.formula_count(), 1)

    async def test_image_hash_cache_skips_model(self):
        recognizer = self._recognizer([_FORMULA_JSON])
        first = await recognizer.recognize(b"same-bytes")
        second = await recognizer.recognize(b"same-bytes")
        self.assertEqual(second["status"], "cached")
        self.assertEqual(
            second["formulas"][0]["formula_id"], first["formulas"][0]["formula_id"]
        )
        self.assertEqual(len(self.client.calls), 1)  # 第二次没有再调模型
        self.assertEqual(recognizer.cached_count, 1)

    async def test_no_formula_image_is_not_a_failure(self):
        """模型说"这张图没有公式"：不算失败、不吃自动重试预算，也不留公式行。"""
        recognizer = self._recognizer(['{"formulas": []}'])
        result = await recognizer.recognize(b"\x89PNGblank")
        self.assertEqual(result["status"], "no_formula")
        self.assertEqual(result["formulas"], [])
        self.assertEqual(recognizer.failed_count, 0)
        self.assertEqual(self.db.formula_failure_count(), 0)
        self.assertEqual(self.db.formula_count(), 0)

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
        self.assertEqual(result["formulas"], [])
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
        entry = result["formulas"][0]
        self.assertTrue(entry["low_confidence"])
        tags = self.db.formula_tags_for(int(entry["formula_id"]))
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

    async def test_failures_are_recorded_and_bounded(self):
        """失败按图片 hash 记数：自动补识别最多试 MAX_FAILED_ATTEMPTS 次，不再烧钱。"""
        recognizer = self._recognizer([CloudError("网络错误")])  # 永远失败
        digest = image_hash(b"retry-me")
        self.assertFalse(recognizer.has_given_up(digest))
        await recognizer.recognize(b"retry-me")
        self.assertFalse(recognizer.has_given_up(digest))
        await recognizer.recognize(b"retry-me")
        self.assertTrue(recognizer.has_given_up(digest))
        row = self.db.formula_failure(digest)
        self.assertEqual(int(row["attempts"]), MAX_FAILED_ATTEMPTS)
        self.assertIn("网络", row["last_error"])
        # 失败的图不算缓存命中，也不会建公式
        self.assertEqual(self.db.formula_count(), 0)
        self.assertEqual(self.db.formula_failure_count(), 1)
        # 后来成功了：公式进库，调用方以指纹/hash 缓存为准
        recognizer._client = FakeVision([_FORMULA_JSON])
        result = await recognizer.recognize(b"retry-me")
        self.assertEqual(result["status"], "recognized")
        self.assertEqual(self.db.formula_count(), 1)

    async def test_failure_recording_never_breaks_the_flow(self):
        """失败记录写不进去（DB 坏了）也不影响返回失败结果。"""
        import sqlite3

        class BrokenDb:
            def __getattr__(self, name):
                raise sqlite3.OperationalError(f"disk I/O error（{name}）")

        client = FakeVision([CloudError("网络错误")])
        recognizer = FormulaRecognizer(db=BrokenDb(), client=client, model="vlm")
        result = await recognizer.recognize(b"img")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(recognizer.has_given_up(b"img"))

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

    async def test_set_recognizer_takes_effect_on_queued_jobs(self):
        """配置热更新换识别器后，队列里排着的任务也要用新的那个（线上踩过的坑）。"""

        class Counting:
            def __init__(self):
                self.seen: list[bytes] = []

            async def recognize(self, image, **kwargs):
                self.seen.append(image)
                return {"status": "failed", "created": False}

        old = Counting()
        pipeline = StudyPipeline(db=self.db, recognizer=old, queue_size=8)
        pipeline.start()
        self.addAsyncCleanup(pipeline.stop)
        self.assertEqual(pipeline.recognizer, old)

        new = Counting()
        pipeline.set_recognizer(new)  # 旧识别器还没跑过任何任务就被换掉
        self.assertEqual(pipeline.recognizer, new)
        pipeline.enqueue({"image": b"after-swap"})
        for _ in range(200):
            if new.seen:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(new.seen, [b"after-swap"])
        self.assertEqual(old.seen, [])

        pipeline.set_recognizer(None)  # 关掉后不再收任务
        self.assertFalse(pipeline.enqueue({"image": b"nope"}))


class TestNotesDbFormulaLookups(unittest.TestCase):
    def test_lookup_helpers(self):
        db = _db_case(self)
        self.assertIsNone(db.formula_by_fingerprint(""))
        self.assertIsNone(db.formula_by_image_hash(""))
        self.assertEqual(db.formula_count(), 0)
        formula_id, created = db.upsert_formula(
            {"fingerprint": "fp-1", "name": "欧拉公式", "image_hash": "img-1"}
        )
        self.assertTrue(created)
        self.assertEqual(db.formula_count(), 1)
        self.assertEqual(db.formula_by_fingerprint("fp-1")["id"], formula_id)
        self.assertEqual(db.formula_by_image_hash("img-1")["id"], formula_id)
        # 同一张图再识别一次（新指纹）时返回最新那条，而不是最旧的
        newer, second_created = db.upsert_formula(
            {"fingerprint": "fp-2", "name": "欧拉公式(修正)", "image_hash": "img-1"}
        )
        self.assertTrue(second_created)
        self.assertEqual(db.formula_by_image_hash("img-1")["id"], newer)
        db.attach_tag("formula", formula_id, "#公式")
        self.assertEqual(db.formula_tags_for(formula_id), ["#公式"])

    def test_failure_table_counts_up(self):
        db = _db_case(self)
        self.assertIsNone(db.formula_failure(""))
        self.assertIsNone(db.formula_failure("nope"))
        self.assertEqual(db.formula_failure_count(), 0)
        db.record_formula_failure("img-1", "第一次失败")
        db.record_formula_failure("img-1", "第二次失败")
        db.record_formula_failure("", "")  # 空 hash 不记
        self.assertEqual(db.formula_failure_count(), 1)
        row = db.formula_failure("img-1")
        self.assertEqual(int(row["attempts"]), 2)
        self.assertEqual(row["last_error"], "第二次失败")


if __name__ == "__main__":
    unittest.main()
