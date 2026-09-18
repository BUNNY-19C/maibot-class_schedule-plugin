"""公式识别：LaTeX 归一化、指纹去重、VLM 识别（含降级）与图片 hash 缓存。

设计决策（都会影响"同一公式会不会存两条"，写清楚原因）：

- **归一化是纯规则、不调模型**：同一个公式在识别结果里有多种写法
  （``\\dfrac``/``\\frac``、``\\times``/``\\cdot``、``π``/``\\pi``、``√2``/``\\sqrt{2}``），
  不归一就会出现多条记录。规则表可单测、可解释、失败能定位；模型判断留在
  名称与分类上（那才是模型擅长的）；
- **指纹 = 归一化 LaTeX 的 sha256 前 16 位**，对应 ``formulas.fingerprint UNIQUE``，
  是"同一公式只存一条"的键。**图片 hash 缓存是另一层**：同一张图连发两次
  不该重复付费识别，两者不可互相替代；
- **识别失败不伪装成功**：模型返回的不是 JSON、LaTeX 为空、调用失败都算失败；
  先降级到 ``vlm_fallback_model`` 重试一次，仍失败就**放弃识别但不删笔记**——
  原图已经进笔记库，识别是增强层，不能变成数据源头；
- **没把握就说不确定**：名称为「未知公式」或置信度低于阈值时打 ``#待确认``，
  用户在检索结果里一眼能看出这条需要人工过一眼，而不是被编造的公式名骗过去；
- 本模块只依赖 :class:`NotesDatabase` 与客户端两个接口，不 import 插件其它部分。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from typing import Any

from .llm_client import CloudError

#: 名称兜底：模型对被问倒的公式必须报这个，而不是编一个像模像样的名字
UNKNOWN_FORMULA_NAME = "未知公式"
#: 低于这个置信度打 ``#待确认``（0.6 是"名称与 LaTeX 都可能不准"的经验分界）
LOW_CONFIDENCE_THRESHOLD = 0.6
#: 别名/知识点上限：识别结果偶尔会吐出十几条近义词，截断避免标签表被灌爆
MAX_ALIASES = 6
MAX_KNOWLEDGE_POINTS = 12
#: 指纹长度：16 位十六进制（64 bit）在个人笔记量级不会有实际碰撞
FINGERPRINT_LENGTH = 16

#: 识别结果里这些键在 SQLite 里是字符串列，统一成 str
_TEXT_FIELDS = ("latex", "name", "category", "subcategory", "description")

FORMULA_PROMPT = """你是公式识别助手。看这张图片，找出里面手写或印刷的数学公式，并按下面的 JSON 结构回答。

要求：
1. 只输出一个 JSON 对象。不要 Markdown 代码块，不要任何解释文字，不要注释。
2. latex 填公式的 LaTeX 源码，**不要**加 $ 或 \\[ \\] 定界符。图片里有多个公式时，
   只填最完整、最主要的那一个。
3. name 填中文标准名称（如「欧拉公式」「勾股定理」「分部积分公式」）。
   **没有把握就填「未知公式」**，绝对不要编造名称。
4. aliases 填常见别名，数组，最多 5 个，没有就给空数组。
5. category 填学科大类（如「高等数学」），subcategory 填细分（如「级数」）；
   不确定就填空字符串。
6. knowledge_points 填这条公式涉及的知识点，数组，最多 8 个。
7. description 用一句话说明公式的含义或用途。
8. confidence 是 0 到 1 之间的小数，表示你对 latex 与名称的把握；不确定就给低分。

输出示例：
{"latex": "e^{i\\pi}+1=0", "name": "欧拉公式", "aliases": ["欧拉恒等式"],
 "category": "高等数学", "subcategory": "复变函数",
 "knowledge_points": ["复数指数", "三角函数"],
 "description": "把五个基本数学常数联系在一起的恒等式", "confidence": 0.95}

如果图片里没有公式、或者字迹看不清，就返回：
{"latex": "", "name": "未知公式", "confidence": 0.0}
"""


class FormulaParseError(ValueError):
    """识别结果不可用（不是 JSON、LaTeX 为空、结构对不上）。"""


# ── LaTeX 归一化 ──────────────────────────────────────────

#: Unicode 数学符号 → LaTeX 命令。识别模型时而输出 ``π`` 时而输出 ``\\pi``，
#: 这是同一公式最常见的"两种写法"，必须收敛。
_UNICODE_TO_LATEX = {
    "π": r"\pi", "α": r"\alpha", "β": r"\beta", "γ": r"\gamma",
    "δ": r"\delta", "ε": r"\epsilon", "ζ": r"\zeta", "η": r"\eta",
    "θ": r"\theta", "ι": r"\iota", "κ": r"\kappa", "λ": r"\lambda",
    "μ": r"\mu", "ν": r"\nu", "ξ": r"\xi", "ρ": r"\rho",
    "σ": r"\sigma", "τ": r"\tau", "υ": r"\upsilon", "φ": r"\phi",
    "χ": r"\chi", "ψ": r"\psi", "ω": r"\omega",
    "Γ": r"\Gamma", "Δ": r"\Delta", "Θ": r"\Theta", "Λ": r"\Lambda",
    "Ξ": r"\Xi", "Π": r"\Pi", "Σ": r"\Sigma", "Φ": r"\Phi",
    "Ψ": r"\Psi", "Ω": r"\Omega",
    # 运算符与关系
    "×": r"\cdot", "·": r"\cdot", "⋅": r"\cdot", "÷": r"\div",
    "±": r"\pm", "∓": r"\mp", "−": "-", "–": "-", "—": "-",
    "≤": r"\le", "≥": r"\ge", "≠": r"\ne", "≈": r"\approx",
    "≡": r"\equiv", "∼": r"\sim", "∞": r"\infty", "∝": r"\propto",
    "∂": r"\partial", "∇": r"\nabla", "∈": r"\in", "∉": r"\notin",
    "⊂": r"\subset", "⊆": r"\subseteq", "∪": r"\cup", "∩": r"\cap",
    "∅": r"\emptyset", "∀": r"\forall", "∃": r"\exists",
    "→": r"\to", "←": r"\leftarrow", "⇒": r"\Rightarrow",
    "⇔": r"\Leftrightarrow", "∑": r"\sum", "∏": r"\prod",
    "∫": r"\int", "∮": r"\oint", "√": r"\sqrt{}", "∘": r"\circ",
    "∠": r"\angle", "⊥": r"\perp", "∥": r"\parallel", "△": r"\triangle",
    "∴": r"\therefore", "∵": r"\because",
    # 上下标数字（模型有时直接抄图片里的排版字符）
    "⁰": "^0", "¹": "^1", "²": "^2", "³": "^3", "⁴": "^4",
    "⁵": "^5", "⁶": "^6", "⁷": "^7", "⁸": "^8", "⁹": "^9",
    "₀": "_0", "₁": "_1", "₂": "_2", "₃": "_3", "₄": "_4",
    "₅": "_5", "₆": "_6", "₇": "_7", "₈": "_8", "₉": "_9",
    # 全角标点（中文输入法下抄公式很常见）
    "（": "(", "）": ")", "＝": "=", "＋": "+", "－": "-",
    "，": ",", "：": ":", "；": ";", "　": " ",
}

#: 这些宏只是排版噪声，语义上与去掉同义
_NOISE_MACROS = re.compile(
    r"\\(?:left|right|big|Big|bigg|Bigg)\b|\\[!,;:]|\\quad|\\qquad"
)
#: ``\text{...}``/``\mathrm{...}`` 之类只包字面量的宏：去壳留内容
_TEXT_WRAPPERS = re.compile(r"\\(?:text|mathrm|operatorname)\{([^{}]*)\}")


def normalize_latex(raw: str) -> str:
    """把公式的各种写法收敛成一个规范形（纯函数，幂等）。

    幂等是硬要求：``normalize(normalize(x)) == normalize(x)``。指纹是拿它的
    输出算的，若不幂等，二次识别同一公式就会算出新指纹、又存一条。
    """
    text = str(raw or "")
    # 1. 去数学定界符：$…$、$$…$$、\[…\]、\(…\)
    text = text.replace("$$", "").replace("$", "")
    text = re.sub(r"\\\[|\\\]|\\\(|\\\)", "", text)
    text = _TEXT_WRAPPERS.sub(r"\1", text)
    # 2. Unicode → LaTeX。必须先做：后面所有规则只认 ASCII 形式
    text = "".join(_UNICODE_TO_LATEX.get(ch, ch) for ch in text)
    # 3. 排版噪声与空白：LaTeX 里空格不参与语义，一律去掉
    text = _NOISE_MACROS.sub("", text)
    text = re.sub(r"\s+", "", text)
    # 4. 分式与乘法的同义宏收敛
    text = re.sub(r"\\(?:d|c|t)frac\b", r"\\frac", text)
    text = text.replace(r"\times", r"\cdot").replace(r"\ast", r"\cdot")
    # 5. 上下标统一成花括号形式：e^(i\pi) 与 e^{i\pi} 是同一个公式
    text = re.sub(r"\^\(([^()]*)\)", r"^{\1}", text)
    text = re.sub(r"_\(([^()]*)\)", r"_{\1}", text)
    text = re.sub(r"\^(\w|\\[A-Za-z]+)", r"^{\1}", text)
    text = re.sub(r"_(\w|\\[A-Za-z]+)", r"_{\1}", text)
    # 6. √ 映射成 \sqrt{} 后把紧随的单个符号收进括号：√2 → \sqrt{2}
    text = re.sub(r"\\sqrt\{\}(\\[A-Za-z]+|[A-Za-z0-9])", r"\\sqrt{\1}", text)
    # 7. 去掉只包一个字符的括号（\sqrt{2}→\sqrt2）。^ 与 _ 的括号是语义，不动
    text = re.sub(r"(?<![\^_]){([^{}])}", r"\1", text)
    # 8. \frac{A}{B} → (A)/(B)：分式写法与 1/2 这类斜杠写法在这里合流
    text = _expand_fractions(text)
    # 9. a/b → (a)/(b)：让 1/2 与 \frac{1}{2} 得到同一个指纹
    text = _parenthesize_divisions(text)
    return text.strip()


def _single_token(text: str, index: int) -> str | None:
    """从 ``index`` 取一个"宏或单字符"记号；取不到返回 ``None``。"""
    if index >= len(text):
        return None
    if text[index] == "\\":
        match = re.match(r"\\[A-Za-z]+", text[index:])
        if match:
            return match.group(0)
        return text[index]  # 转义单字符，如 \{ \%
    if text[index] in "{}()[]":
        return None
    return text[index]


def _take_group(text: str, index: int) -> tuple[str | None, int]:
    """取 ``{...}`` 或单个记号作为参数，返回 (内容, 新位置)。

    结构不完整（括号不闭合、参数缺失）时返回 ``None``——调用方原样保留，
    宁可留一段没归一化的文本，也不能静默吞掉内容。
    """
    if index < len(text) and text[index] == "[":
        end = text.find("]", index)
        if end != -1:
            index = end + 1  # \frac[a]{b}{c} 的可选参数，直接跳过
    if index >= len(text):
        return None, index
    if text[index] != "{":
        token = _single_token(text, index)
        if token is None:
            return None, index
        return token, index + len(token)
    depth = 0
    for position in range(index, len(text)):
        if text[position] == "{":
            depth += 1
        elif text[position] == "}":
            depth -= 1
            if depth == 0:
                return text[index + 1: position], position + 1
    return None, index


#: 分式展开的递归上限。纯为挡病态输入（几千层 \\frac 嵌套会让递归爆栈），
#: 正常公式远达不到；超限的分支原样保留，不展开也不报错。
MAX_FRACTION_DEPTH = 20


def _expand_fractions(text: str, depth: int = 0) -> str:
    """把 ``\\frac{A}{B}``（含 ``\\frac AB`` 写法）展开成 ``(A)/(B)``。

    分子分母要**递归展开**：``\\frac{\\frac{a}{b}}{c}`` 的外层参数里还藏着
    一个 ``\\frac``。若只从外往里扫一遍、扫过参数就不再回头，内层那个会被
    原样留在括号里，于是"归一化再归一化"还会变一次（不幂等 → 同一公式存两条）。
    """
    out: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith(r"\frac", index):
            numerator, after_num = _take_group(text, index + 5)
            if numerator is not None:
                denominator, after_den = _take_group(text, after_num)
                if denominator is not None:
                    if depth < MAX_FRACTION_DEPTH:
                        numerator = _expand_fractions(numerator, depth + 1)
                        denominator = _expand_fractions(denominator, depth + 1)
                    out.append(f"({numerator})/({denominator})")
                    index = after_den
                    continue
            # 结构不完整：原样留着，别把内容丢掉
            out.append(text[index: index + 5])
            index += 5
            continue
        out.append(text[index])
        index += 1
    return "".join(out)


def _split_top_level(text: str, separator: str) -> list[str]:
    """按分隔符切分，但跳过 ``{}`` 与 ``()`` 内部（结构内的斜杠不算除法）。"""
    parts: list[str] = []
    current: list[str] = []
    brace = paren = 0
    for char in text:
        if char == "{":
            brace += 1
        elif char == "}":
            brace = max(0, brace - 1)
        elif char == "(":
            paren += 1
        elif char == ")":
            paren = max(0, paren - 1)
        if char == separator and brace == 0 and paren == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def _is_wrapped(part: str) -> bool:
    """整段是否已被一对括号包住（``(a)`` 是，``(a)(b)`` 不是）。"""
    if len(part) < 2 or part[0] != "(" or part[-1] != ")":
        return False
    depth = 0
    for position, char in enumerate(part):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return position == len(part) - 1
    return False


def _parenthesize_divisions(text: str) -> str:
    """给裸除法两侧补括号：``1/2`` → ``(1)/(2)``，与 ``\\frac`` 展开式一致。"""
    parts = _split_top_level(text, "/")
    if len(parts) < 2:
        return text
    normalized: list[str] = []
    for part in parts:
        stripped = part.strip()
        if not stripped:
            return text  # 空段（a//b 之类）不猜，原样放行
        normalized.append(stripped if _is_wrapped(stripped) else f"({stripped})")
    return "/".join(normalized)


def fingerprint(normalized_latex: str) -> str:
    """归一化 LaTeX 的 sha256 前 16 位；空文本返回空串（调用方跳过落库）。"""
    text = str(normalized_latex or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def image_hash(image: bytes) -> str:
    """图片内容 hash（识别缓存键）。与指纹一样取 sha256 前 16 位。"""
    if not image:
        return ""
    return hashlib.sha256(image).hexdigest()[:FINGERPRINT_LENGTH]


# ── 识别结果解析 ──────────────────────────────────────────

_CODE_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def _as_text_list(value: Any, *, limit: int) -> list[str]:
    """容忍模型给字符串/数组/None 三种形态，去重保序并截断。"""
    items: list[Any]
    if isinstance(value, str):
        items = re.split(r"[、,，;/|]", value)
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _as_confidence(value: Any) -> float:
    """置信度容错：``"0.9"``、``90``（百分数）、``None`` 都能收下。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if 1.0 < number <= 100.0:
        number = number / 100.0  # 模型偶尔按百分制给分
    return max(0.0, min(1.0, number))


def parse_formula_response(raw: str) -> dict[str, Any]:
    """把模型的回答解析成规范化字典；不可用时抛 :class:`FormulaParseError`。

    这里**只做解析不做网络**：解析规则单测得到，出问题一眼能看出是模型乱答
    还是我们读错。LaTeX 为空按失败处理——"看不清"必须是失败，不能落一条空公式。

    取的是**第一个完整 JSON 对象**（``raw_decode``），而不是"第一个 { 到最后一个 }"：
    模型常在对象后面再补一句带花括号的话，贪心匹配会把这些一起吃进来、解析失败，
    白降级一次模型。
    """
    text = _CODE_FENCE.sub("", str(raw or "").strip())
    if not text:
        raise FormulaParseError("模型返回为空")
    start = text.find("{")
    if start < 0:
        raise FormulaParseError("模型返回里找不到 JSON 对象")
    try:
        data, _end = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise FormulaParseError(f"JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise FormulaParseError("模型返回的 JSON 不是对象")

    latex_raw = str(data.get("latex") or "").strip()
    normalized = normalize_latex(latex_raw)
    if not normalized:
        raise FormulaParseError("识别结果没有 LaTeX（可能图片里没有公式）")

    name = str(data.get("name") or "").strip() or UNKNOWN_FORMULA_NAME
    return {
        "latex": latex_raw,
        "latex_normalized": normalized,
        "fingerprint": fingerprint(normalized),
        "name": name,
        "aliases": _as_text_list(data.get("aliases"), limit=MAX_ALIASES),
        "category": str(data.get("category") or "").strip(),
        "subcategory": str(data.get("subcategory") or "").strip(),
        "knowledge_points": _as_text_list(
            data.get("knowledge_points"), limit=MAX_KNOWLEDGE_POINTS
        ),
        "description": str(data.get("description") or "").strip(),
        "confidence": _as_confidence(data.get("confidence")),
    }


def is_low_confidence(parsed: dict[str, Any]) -> bool:
    """名称未知或置信度不达标 → 需要人工确认。"""
    return (
        str(parsed.get("name") or "") == UNKNOWN_FORMULA_NAME
        or float(parsed.get("confidence") or 0.0) < LOW_CONFIDENCE_THRESHOLD
    )


# ── 识别器 ────────────────────────────────────────────────


class FormulaRecognizer:
    """图片 → 公式记录。缓存优先、失败降级、低置信打标。

    ``db`` 需要 ``formula_by_image_hash`` / ``formula_by_fingerprint`` /
    ``upsert_formula`` / ``attach_tag``；``client`` 需要 ``vision``。
    """

    def __init__(
        self,
        *,
        db: Any,
        client: Any,
        model: str,
        fallback_model: str = "",
        image_cache_enabled: bool = True,
        low_confidence_threshold: float = LOW_CONFIDENCE_THRESHOLD,
        prompt: str = FORMULA_PROMPT,
        max_tokens: int = 1200,
    ) -> None:
        self._db = db
        self._client = client
        self._model = str(model or "").strip()
        fallback = str(fallback_model or "").strip()
        self._fallback_model = fallback if fallback != self._model else ""
        self._cache_enabled = bool(image_cache_enabled)
        self._threshold = float(low_confidence_threshold)
        self._prompt = prompt
        self._max_tokens = max(256, int(max_tokens))
        #: 累计计数（/笔记库 展示用，也是"到底有没有在跑"的证据）
        self.recognized_count = 0
        self.cached_count = 0
        self.failed_count = 0
        self.last_error = ""

    async def recognize(
        self,
        image: bytes,
        *,
        suffix: str = ".png",
        course: str = "",
        week: int | None = None,
        period: str = "",
        message_id: str = "",
    ) -> dict[str, Any]:
        """识别一张图；**不抛异常**——失败以 ``status="failed"`` 返回。

        识别是增强层，抛异常会让上层 worker 反复重启任务，用户却看不到任何
        有用信息。失败原因放进 ``error`` 供日志与 /笔记库 排障。
        """
        if not image:
            return self._failure("空图片")
        digest = image_hash(image)
        if self._cache_enabled and digest:
            cached = await self._lookup_image(digest)
            if cached is not None:
                self.cached_count += 1
                return cached

        parsed: dict[str, Any] | None = None
        errors: list[str] = []
        degraded = False
        for index, model in enumerate(self._models()):
            try:
                response = await self._client.vision(
                    model=model,
                    image_base64=base64.b64encode(image).decode("ascii"),
                    prompt=self._prompt,
                    image_suffix=suffix or ".png",
                    max_tokens=self._max_tokens,
                )
                parsed = parse_formula_response(response.get("text") or "")
                degraded = index > 0
                break
            except (CloudError, FormulaParseError) as exc:
                errors.append(f"{model}: {exc}")
            except Exception as exc:  # 客户端之外意外也要落成失败，别炸 worker
                errors.append(f"{model}: {exc!r}")
        if parsed is None:
            return self._failure("；".join(errors)[:300] or "识别失败")

        low = is_low_confidence(parsed) or parsed["confidence"] < self._threshold
        try:
            return await self._store(
                parsed,
                digest=digest,
                course=course,
                week=week,
                period=period,
                message_id=message_id,
                low_confidence=low,
                degraded=degraded,
            )
        except Exception as exc:  # 落库失败也是"这条识别没成"，返失败而不是抛
            return self._failure(f"公式落库失败：{exc}")

    # ── 内部 ──────────────────────────────────────────────

    @property
    def signature(self) -> tuple[str, str, bool]:
        """识别能力标识 ``(主模型, 降级模型, 图片缓存)``。

        配置热更新后调用方拿它比一比，就知道"识别能力变没变"（只改了提前量这类
        无关配置时不该刷日志），不必把 API Key 暴露出来比较。
        """
        return (self._model, self._fallback_model, self._cache_enabled)

    def _models(self) -> list[str]:
        models = [self._model] if self._model else []
        if self._fallback_model:
            models.append(self._fallback_model)
        return models or [""]

    def _failure(self, reason: str) -> dict[str, Any]:
        self.failed_count += 1
        self.last_error = reason
        return {
            "status": "failed",
            "formula_id": 0,
            "name": "",
            "latex": "",
            "latex_normalized": "",
            "confidence": 0.0,
            "low_confidence": False,
            "degraded": False,
            "created": False,
            "error": reason,
        }

    async def _lookup_image(self, digest: str) -> dict[str, Any] | None:
        try:
            row = await asyncio.to_thread(self._db.formula_by_image_hash, digest)
        except Exception:
            return None  # 缓存查询失败就当没缓存，绝不让它挡住识别
        if row is None:
            return None
        return {
            "status": "cached",
            "formula_id": int(row["id"]),
            "name": str(row["name"] or ""),
            "latex": str(row["latex_normalized"] or row["latex_raw"] or ""),
            "latex_normalized": str(row["latex_normalized"] or ""),
            "confidence": float(row["confidence"] or 0.0),
            "low_confidence": str(row["name"] or "") == UNKNOWN_FORMULA_NAME,
            "degraded": False,
            "created": False,
            "error": "",
        }

    async def _store(
        self,
        parsed: dict[str, Any],
        *,
        digest: str,
        course: str,
        week: int | None,
        period: str,
        message_id: str,
        low_confidence: bool,
        degraded: bool,
    ) -> dict[str, Any]:
        fingerprint_value = parsed["fingerprint"]
        existing = None
        try:
            existing = await asyncio.to_thread(
                self._db.formula_by_fingerprint, fingerprint_value
            )
        except Exception:
            pass
        formula_id = await asyncio.to_thread(
            self._db.upsert_formula,
            {
                "fingerprint": fingerprint_value,
                "latex_raw": parsed["latex"],
                "latex_normalized": parsed["latex_normalized"],
                "name": parsed["name"],
                "aliases": "、".join(parsed["aliases"]),
                "category": parsed["category"],
                "subcategory": parsed["subcategory"],
                "description": parsed["description"],
                "confidence": parsed["confidence"],
                "image_hash": digest,
                "course": course,
                "week": week,
                "period": period,
                "source_message_id": message_id,
            },
        )
        for tag, source in self._tags(course, low_confidence):
            try:
                await asyncio.to_thread(
                    self._db.attach_tag, "formula", formula_id, tag, source=source
                )
            except Exception:
                continue  # 标签失败不影响公式本身
        self.recognized_count += 1
        return {
            "status": "recognized",
            "formula_id": formula_id,
            "name": parsed["name"],
            "latex": parsed["latex"],
            "latex_normalized": parsed["latex_normalized"],
            "confidence": parsed["confidence"],
            "low_confidence": low_confidence,
            "degraded": degraded,
            "created": existing is None,
            "error": "",
        }

    @staticmethod
    def _tags(course: str, low_confidence: bool) -> list[tuple[str, str]]:
        """公式标签：内容类型 + 课程 + 待确认（都是规则给的，标 source=规则）。"""
        tags: list[tuple[str, str]] = [("#公式", "规则")]
        if course and course != "未分类":
            tags.append((f"#{course}", "规则"))
        if low_confidence:
            tags.append(("#待确认", "规则"))
        return tags
