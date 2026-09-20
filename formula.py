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
#: 一张图最多收几条公式。一页课件常常有 2~4 条；再高就多半是把推导步骤也当公式，
#: 而且条数越多模型耗时越长（实测 7 条要 285 秒），所以卡在 6 条。
MAX_FORMULAS_PER_IMAGE = 6
#: 指纹长度：16 位十六进制（64 bit）在个人笔记量级不会有实际碰撞
FINGERPRINT_LENGTH = 16
#: 同一张图自动重试的上限。自动补识别不能无限烧钱：图里根本没有公式、
#: 或模型反复答非所问时，试满这个次数就归为"这张图没救"，只留失败计数
#: （用户主动重发不受这个上限约束——那是他的明确意图）。
MAX_FAILED_ATTEMPTS = 2

FORMULA_PROMPT = """你是公式识别助手。看这张图片，把里面**所有**手写或印刷的数学公式逐条识别出来，按下面的 JSON 结构回答。

要求：
1. 只输出一个 JSON 对象。不要 Markdown 代码块，不要解释文字，不要注释。
2. 顶层是 ``{"formulas": [ ... ]}``，数组里每个元素是一条公式。图片里有几条就写几条
   （最多 6 条）。**不要只挑最主要的那一条**，也不要把两条公式合成一条。
3. **只收独立成行的式子**：定理、结论、定义式、例题的最终表达式。
   不要收推导的中间步骤、"令 x=y" 这类变量代换说明、图注或编号文字。
   图片里写"左式""右式"时，把它指代的**完整式子**写出来，不要输出"左="这种。
4. 逐字照抄图片里的式子：不化简、不改记号、不补推导步骤；上下标、分式、根号、
   希腊字母、微分号都按原样。实在看不清的局部用 ``?`` 占位并把 confidence 压低，
   **绝对不要**用你猜的公式填空。
5. 每条公式的字段：
   - latex：LaTeX 源码，不要加 $ 或 \\[ \\] 定界符
   - name：中文标准名称（如「欧拉公式」「带传动中心距计算公式」）；
     **没有把握就填「未知公式」**，绝不要编名字
   - aliases：常见别名数组，最多 5 个，没有给空数组
   - category / subcategory：学科大类 / 细分；不确定给空字符串
   - knowledge_points：这条公式涉及的知识点，数组，最多 8 个
   - description：一句话说明含义或用途
   - confidence：0 到 1 的小数，表示对 latex 与名称的把握；不确定就给低分
6. 只有图片里**确实一个公式都没有**时，才返回 ``{"formulas": []}``。
   有公式但看不清时照样给出能读出的 latex 并压低 confidence，不要返回空数组。

输出示例：
{"formulas": [
  {"latex": "e^{i\\\\pi}+1=0", "name": "欧拉公式", "aliases": ["欧拉恒等式"],
   "category": "高等数学", "subcategory": "复变函数",
   "knowledge_points": ["复数指数", "三角函数"],
   "description": "把五个基本数学常数联系在一起的恒等式", "confidence": 0.95},
  {"latex": "F=ma", "name": "牛顿第二定律", "aliases": [], "category": "力学",
   "subcategory": "", "knowledge_points": ["力", "加速度"],
   "description": "合外力等于质量乘以加速度", "confidence": 0.9}
]}
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
    # 3. 排版噪声与空白：LaTeX 里空格不参与语义，但宏名后的分界空格是例外
    text = _NOISE_MACROS.sub("", text)
    text = _strip_spacing(text)
    # 4. 分式与乘法的同义宏收敛。这里用 ``(?![A-Za-z])`` 而不是 ``\b``：
    #    ``\tfrac12`` 这种省花括号的写法里，``frac`` 后面跟的是数字，``\b`` 不成立，
    #    会漏掉不归一（同一公式两种写法各存一条）
    text = re.sub(r"\\(?:d|c|t)frac(?![A-Za-z])", r"\\frac", text)
    text = text.replace(r"\times", r"\cdot").replace(r"\ast", r"\cdot")
    # 5. 上下标统一成花括号形式：e^(i\pi) 与 e^{i\pi} 是同一个公式
    text = re.sub(r"\^\(([^()]*)\)", r"^{\1}", text)
    text = re.sub(r"_\(([^()]*)\)", r"_{\1}", text)
    text = re.sub(r"\^(\w|\\[A-Za-z]+)", r"^{\1}", text)
    text = re.sub(r"_(\w|\\[A-Za-z]+)", r"_{\1}", text)
    # 6. 去掉只包一个字符的括号（\sqrt{2}→\sqrt2）。^ 与 _ 的括号是语义，不动
    text = _strip_single_char_braces(text)
    # 7. √ 映射成 \sqrt{} 后把紧随的符号/括号组收进根号：√2→\sqrt{2}、√{ab}→\sqrt{ab}
    #    必须在剥单字符花括号之后：``√{2}`` 先变成 \sqrt{}2，这一步才收得到它；
    #    放前面的话 \sqrt{}{2} 会原样漏过去（同一公式两个指纹，线上踩过）
    text = _repair_sqrt(text)
    # 8. 分式归一：\frac 家族统一成 \frac{A}{B}（参数补花括号），裸除法也并进来
    text = _normalize_fracs(text)
    # 9. a/b → \frac{a}{b}：让 1/2 与 \frac{1}{2} 得到同一个指纹
    text = _fracs_from_slashes(text)
    # 10. 分式改写会把占位空格搬离它原本的宏名，先收拾干净（只有紧跟宏名的才是
    #     "必须保留的分界空格"，其余是残留，删掉）
    text = _drop_stray_placeholders(text)
    # 11. 最后再收一次单字符花括号：上一步补出来的 {2} 要变回 2，才与 \frac{1}{2}
    #     那条路合流。必须在清占位符之后做，否则残留占位符会把括号撑成"多字符"
    text = _strip_single_char_braces(text)
    return text.strip()


def _drop_stray_placeholders(text: str) -> str:
    """占位符只在紧跟宏名时保留为空格，其余位置一律删除（保证幂等）。"""
    marked = re.sub(r"(\\[A-Za-z]+)" + _PLACEHOLDER, lambda m: m.group(1) + "\x01", text)
    return marked.replace(_PLACEHOLDER, "").replace("\x01", " ")


#: 去空白时用来临时顶替"必须保留的分界空格"的占位符。
#: 公式文本里不会出现 NUL，用它顶一下最省事，最后再换回空格。
_PLACEHOLDER = "\x00"
#: 宏名 + 空白 + 字母：这个空白**不能删**。LaTeX 里 ``\cdot c`` 与 ``\cdotc``
#: 不是一回事（后者是未定义宏），全局去空白会把 ``\pi r^2`` 粘成 ``\pir^2``、
#: ``a/b\cdot c`` 粘成 ``a/b\cdotc``——后者会让除法的右操作数把 ``\cdotc``
#: 整个吃进去，算出 ``a/(b\cdot c)``，公式的意思就变了。
_MACRO_SPACE_BEFORE_LETTER = re.compile(r"(\\[A-Za-z]+)\s+(?=[A-Za-z])")


def _strip_spacing(text: str) -> str:
    """去掉排版空白，但把"宏名后必须保留的分界空格"留成占位符。

    占位符**不能在这里换回空格**：后面的分式改写会把操作数搬进花括号，
    那时它已经不在宏名后面了，留着就会被当成内容写进 ``\\frac{ b}c``（还会不幂等）。
    统一交给 ``_drop_stray_placeholders`` 在最后收拾。
    """
    text = _MACRO_SPACE_BEFORE_LETTER.sub(r"\1" + _PLACEHOLDER, text)
    return re.sub(r"\s+", "", text)


def _strip_single_char_braces(text: str) -> str:
    """去掉只包一个字符的花括号（``\\sqrt{2}``→``\\sqrt2``）。

    ``^`` 与 ``_`` 后面的花括号是语义（``e^{2}`` 不能写成 ``e^2`` 之后再被误解），
    所以那里不动。
    """
    return re.sub(r"(?<![\^_]){([^{}])}", r"\1", text)


def _repair_sqrt(text: str) -> str:
    """把 ``\\sqrt{}``（由 ``√`` 映射而来）与紧随的操作数合回 ``\\sqrt{操作数}``。

    用函数而不是正则：紧随的既可能是单字符（``√2``）、宏（``√\\pi``），也可能
    已经是一个括号组（``√{ab}``）。三种都要收成同一个形状，否则同一公式两个指纹。
    后面什么都没有时原样留着——宁可留一段没归一化的文本，也不吞掉内容。
    """
    marker = r"\sqrt{}"
    if marker not in text:
        return text
    out: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith(marker, index):
            index += len(marker)
            content, after = _take_group(text, index)
            if content is not None:
                out.append(r"\sqrt{" + content + "}")
                index = after
                continue
            out.append(marker)
            continue
        out.append(text[index])
        index += 1
    return "".join(out)


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


#: 分式归一的递归上限。纯为挡病态输入（几千层 \\frac 嵌套会让递归爆栈），
#: 正常公式远达不到；超限的分支原样保留，不归一也不报错。
MAX_FRACTION_DEPTH = 20

#: 会**断开操作数**的符号：除法两侧只吃紧邻的原子，遇到这些就停。
#: 为什么必须停：乘除同优先级、左结合，``a/b\cdot c`` 的意思是 ``(a/b)\cdot c``，
#: 把右边一路吃成 ``b\cdot c`` 就变成 ``a/(b\cdot c)``——**改掉了公式的意思**。
_BREAK_CHARS = frozenset("+-=<>,")
_BREAK_MACROS = (
    r"\cdot", r"\div", r"\pm", r"\mp", r"\cup", r"\cap", r"\circ", r"\times",
)
#: 与 ``/`` 同义的除号宏：一起改成分式，别让 ``a\div b`` 与 ``a/b`` 各算一个指纹
_DIV_MACRO = r"\div"
_MACRO_RE = re.compile(r"\\[A-Za-z]+")


def _atom_end(text: str, index: int) -> int:
    """读一个原子（宏 / 括号组 / 单字符）连同它的上下标，返回结束位置。"""
    if index >= len(text):
        return index
    char = text[index]
    if char == "\\":
        match = _MACRO_RE.match(text, index)
        end = match.end() if match else index + 1
    elif char in "{(":
        closer = "}" if char == "{" else ")"
        depth = 0
        end = index + 1  # 括号不闭合时当一个字符算，绝不吞掉后面的内容
        for position in range(index, len(text)):
            if text[position] == char:
                depth += 1
            elif text[position] == closer:
                depth -= 1
                if depth == 0:
                    end = position + 1
                    break
    else:
        end = index + 1
    # 连续上下标链（x^2^{3}_i）逐个往下吃，但设个硬上限：病态的长链会把递归
    # 变成栈溢出，上限之外原样截断（这串本来也不会是合法公式）
    for _ in range(16):
        if end < len(text) and text[end] in "^_":
            suffix = _atom_end(text, end + 1)
            if suffix <= end + 1:
                break  # ^ 后面什么都没有，别把它吃进来
            end = suffix
        else:
            break
    return end


def _starts_break_macro(text: str, index: int) -> bool:
    """``index`` 处是不是一个"断开操作数"的宏（二元运算符）。"""
    if index >= len(text) or text[index] != "\\":
        return False
    match = _MACRO_RE.match(text, index)
    return bool(match) and match.group(0) in _BREAK_MACROS


def _operand_end(text: str, index: int) -> int | None:
    """读一个操作数（一个或多个相邻原子）；读不出内容返回 ``None``。

    相邻原子算同一个操作数（``kT``、``\sigma_{\lim}``、``(a)(b)``），这在数学上
    是安全的：括起来只会更明确，不会改变结合关系。
    """
    cursor = index
    end: int | None = None
    while cursor < len(text):
        if text[cursor] == "/" or text[cursor] in _BREAK_CHARS:
            break
        if _starts_break_macro(text, cursor):
            break
        next_end = _atom_end(text, cursor)
        if next_end <= cursor:
            break
        cursor = next_end
        end = cursor
    return end


def _normalize_fracs(text: str, depth: int = 0) -> str:
    """``\\frac`` 家族统一成 ``\\frac{A}{B}``，参数一律补花括号。

    参数要**递归归一**：``\\frac{\\frac{a}{b}}{c}`` 的外层参数里还藏着一个
    ``\\frac``，只扫一遍外层就不再回头的话，内层会原样留着，于是"归一化再归一化"
    还会变一次（不幂等 → 同一公式存两条）。
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
                        numerator = _normalize_fracs(numerator, depth + 1)
                        denominator = _normalize_fracs(denominator, depth + 1)
                    out.append(f"\\frac{{{numerator}}}{{{denominator}}}")
                    index = after_den
                    continue
            # 结构不完整：原样留着，别把内容丢掉
            out.append(text[index: index + 5])
            index += 5
            continue
        out.append(text[index])
        index += 1
    return "".join(out)


def _fracs_from_slashes(text: str) -> str:
    """裸除法改成分式：``1/2`` → ``\\frac{1}{2}``，``a/b/c`` → ``\\frac{\\frac{a}{b}}{c}``。

    为什么不再"给两侧补括号"：补括号必须知道操作数边界，而边界一旦算错就会
    重新结合整个表达式。**线上真实返回暴露过这个错误**——模型给的是

        [\\sigma]=\\frac{\\sigma_{\\lim}}{S_{\\sigma}}=\\frac{\\sigma_{S}}{S_{\\sigma}}

    按顶层斜杠切段补括号会变成 ``([\\sigma]=(\\sigma_{\\lim}))/((S_{\\sigma})=…)``，
    等于把两个等号也塞进分母，公式的意思就没了。改成"斜杠变分式"以后边界靠
    操作数扫描决定，写到哪一段都只影响那一段，``x=\\frac{a}{b}`` 这类最常见形状
    也不会再被改动。
    """
    out: list[str] = []
    operand_at = 0  # out 里当前左操作数的起点
    index = 0
    while index < len(text):
        char = text[index]
        if char == "/" or text.startswith(_DIV_MACRO, index):
            operator_end = index + 1 if char == "/" else index + len(_DIV_MACRO)
            left = "".join(out[operand_at:])
            right_end = _operand_end(text, operator_end)
            if left and right_end is not None:
                right = text[operator_end: right_end]
                out[operand_at:] = [f"\\frac{{{left}}}{{{right}}}"]
                index = right_end
                continue
            # 除号两侧不完整：原样留着，别猜
            out.append(text[index: operator_end])
            index = operator_end
            continue
        start = index
        end = _atom_end(text, start)
        if end <= start:
            end = start + 1
        out.append(text[start:end])
        index = end
        if char in _BREAK_CHARS or _starts_break_macro(text, start):
            operand_at = len(out)  # 断开处之后是新的操作数
    return "".join(out)


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


def _parse_item(data: dict[str, Any]) -> dict[str, Any] | None:
    """规范化单条公式；LaTeX 为空时返回 ``None``（不落空公式）。"""
    latex_raw = str(data.get("latex") or "").strip()
    normalized = normalize_latex(latex_raw)
    if not normalized:
        return None
    return {
        "latex": latex_raw,
        "latex_normalized": normalized,
        "fingerprint": fingerprint(normalized),
        "name": str(data.get("name") or "").strip() or UNKNOWN_FORMULA_NAME,
        "aliases": _as_text_list(data.get("aliases"), limit=MAX_ALIASES),
        "category": str(data.get("category") or "").strip(),
        "subcategory": str(data.get("subcategory") or "").strip(),
        "knowledge_points": _as_text_list(
            data.get("knowledge_points"), limit=MAX_KNOWLEDGE_POINTS
        ),
        "description": str(data.get("description") or "").strip(),
        "confidence": _as_confidence(data.get("confidence")),
    }


def parse_formula_response(raw: str) -> list[dict[str, Any]]:
    """把模型的回答解析成规范化公式**列表**；结构不可用时抛 :class:`FormulaParseError`。

    返回空列表是合法结果，意思是"这张图里确实没有公式"（提示词要求模型只有在这种
    情况下才返回 ``{"formulas": []}``）。但 ``{"latex": ""}`` 这种旧 bail-out 形状
    含义模糊（更可能是"放弃了"），按解析失败处理，让降级模型再试一次。

    这里**只做解析不做网络**：解析规则单测得到，出问题一眼能看出是模型乱答还是我们
    读错。取的是**第一个完整 JSON 值**（``raw_decode``）而不是"第一个 { 到最后一个 }"：
    模型常在对象后面补一句带花括号的话，贪心匹配会连它一起吃进来、解析失败。
    """
    text = _CODE_FENCE.sub("", str(raw or "").strip())
    if not text:
        raise FormulaParseError("模型返回为空")
    starts = [position for position in (text.find("{"), text.find("[")) if position >= 0]
    if not starts:
        raise FormulaParseError("模型返回里找不到 JSON 结构")
    start = min(starts)
    try:
        data, _end = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise FormulaParseError(f"JSON 解析失败：{exc}") from exc

    if isinstance(data, dict) and isinstance(data.get("formulas"), list):
        items: list[Any] = data["formulas"]
    elif isinstance(data, list):
        items = list(data)
    elif isinstance(data, dict):
        items = [data]  # 模型没按数组包：当成一条处理
    else:
        raise FormulaParseError("模型返回的 JSON 结构无法理解")

    parsed: list[dict[str, Any]] = []
    gave_up = False  # 出现过 {"latex": ""} 这种"我放弃了"的形状
    for item in items:
        if not isinstance(item, dict):
            continue
        entry = _parse_item(item)
        if entry is None:
            if "latex" in item:
                gave_up = True
            continue
        if any(existing["fingerprint"] == entry["fingerprint"] for existing in parsed):
            continue  # 同一条公式被写了两遍
        parsed.append(entry)
        if len(parsed) >= MAX_FORMULAS_PER_IMAGE:
            break
    if not parsed and gave_up and isinstance(data, dict):
        raise FormulaParseError("识别结果没有 LaTeX（模型放弃了这一张）")
    return parsed


def is_low_confidence(parsed: dict[str, Any]) -> bool:
    """名称未知或置信度不达标 → 需要人工确认。"""
    return (
        str(parsed.get("name") or "") == UNKNOWN_FORMULA_NAME
        or float(parsed.get("confidence") or 0.0) < LOW_CONFIDENCE_THRESHOLD
    )


# ── 识别器 ────────────────────────────────────────────────


class FormulaRecognizer:
    """图片 → 公式记录。缓存优先、失败降级、低置信打标、**一张图认全部公式**。

    ``db`` 需要 ``formula_by_image_hash`` / ``upsert_formula`` / ``attach_tag`` /
    ``record_formula_failure``；``client`` 需要 ``vision``。
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
        max_tokens: int = 2000,
        temperature: float = 0.0,
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
        # 识别要可复现：同一张图问两次不该给出两种式子，所以默认温度为 0
        self._temperature = max(0.0, min(1.0, float(temperature)))
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
        """识别一张图里的全部公式；**不抛异常**，失败以 ``status="failed"`` 返回。

        返回 ``{"status", "formulas": [...], "degraded", "error"}``，status 四种：

        - ``recognized``：认出了公式并已落库（``formulas`` 里是每一条，含 created 标记）；
        - ``cached``：这张图之前认过，直接给回已有公式（不再调模型、不再花钱）；
        - ``no_formula``：模型明确说图里没有公式（不算失败，不吃自动重试预算）；
        - ``failed``：调用失败或返回读不出来（已按顺序试过降级模型）。

        识别是增强层，抛异常会让上层 worker 反复重启任务、用户什么也看不到；
        失败原因放进 ``error`` 供日志与 /笔记库 排障。
        """
        if not image:
            return self._failure("空图片")
        digest = image_hash(image)
        if self._cache_enabled and digest:
            cached = await self._lookup_image(digest)
            if cached is not None:
                self.cached_count += 1
                return cached

        parsed: list[dict[str, Any]] | None = None
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
                    temperature=self._temperature,
                )
                parsed = parse_formula_response(response.get("text") or "")
                degraded = index > 0
                break
            except (CloudError, FormulaParseError) as exc:
                errors.append(f"{model}: {exc}")
            except Exception as exc:  # 客户端之外意外也要落成失败，别炸 worker
                errors.append(f"{model}: {exc!r}")
        if parsed is None:
            result = self._failure("；".join(errors)[:300] or "识别失败")
            await self._note_failure(digest, result["error"])
            return result
        if not parsed:
            # 模型说"这张图确实没有公式"：不记失败（免得把重试预算吃光），
            # 但也不留结果——所以同一条图再发还会问一次模型，这是有意的取舍
            return {
                "status": "no_formula",
                "formulas": [],
                "degraded": degraded,
                "error": "",
            }

        try:
            return await self._store(
                parsed,
                digest=digest,
                course=course,
                week=week,
                period=period,
                message_id=message_id,
                degraded=degraded,
            )
        except Exception as exc:  # 落库失败也是"这条识别没成"，返失败而不是抛
            await self._note_failure(digest, f"公式落库失败：{exc}")
            return self._failure(f"公式落库失败：{exc}")

    async def _note_failure(self, digest: str, error: str) -> None:
        """把失败记进库（尽力而为）：自动补识别靠它判断"这张图别再试了"。"""
        if not digest:
            return
        try:
            await asyncio.to_thread(self._db.record_formula_failure, digest, error)
        except Exception:
            pass  # 记不上只是失去重试上限，不能因此把失败上报流程也弄断

    def has_given_up(self, digest: str) -> bool:
        """这张图是否已经自动试满次数（调用方据此跳过，别重复付费）。"""
        if not digest:
            return False
        try:
            row = self._db.formula_failure(digest)
        except Exception:
            return False
        return row is not None and int(row["attempts"] or 0) >= MAX_FAILED_ATTEMPTS

    # ── 内部 ──────────────────────────────────────────────

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
            "formulas": [],
            "degraded": False,
            "error": reason,
        }

    def _entry(self, row: Any, *, created: bool = False) -> dict[str, Any]:
        """把库里一行（或解析结果）整理成回执/写笔记要用的统一结构。"""
        return {
            "formula_id": int(row["id"]) if "id" in row.keys() else 0,
            "name": str(row["name"] or ""),
            "latex": str(row["latex_normalized"] or row["latex_raw"] or ""),
            "confidence": float(row["confidence"] or 0.0),
            "low_confidence": (
                str(row["name"] or "") == UNKNOWN_FORMULA_NAME
                or float(row["confidence"] or 0.0) < self._threshold
            ),
            "created": created,
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
            "formulas": [self._entry(row)],
            "degraded": False,
            "error": "",
        }

    async def _store(
        self,
        parsed: list[dict[str, Any]],
        *,
        digest: str,
        course: str,
        week: int | None,
        period: str,
        message_id: str,
        degraded: bool,
    ) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        for item in parsed:
            # created 直接由 SQL 层带出来：先查一遍再插是把同一判断做了两次，
            # 而且查失败（吞异常）时会误报"新公式"
            formula_id, created = await asyncio.to_thread(
                self._db.upsert_formula,
                {
                    "fingerprint": item["fingerprint"],
                    "latex_raw": item["latex"],
                    "latex_normalized": item["latex_normalized"],
                    "name": item["name"],
                    "aliases": "、".join(item["aliases"]),
                    "category": item["category"],
                    "subcategory": item["subcategory"],
                    "description": item["description"],
                    "confidence": item["confidence"],
                    "image_hash": digest,
                    "course": course,
                    "week": week,
                    "period": period,
                    "source_message_id": message_id,
                },
            )
            low = is_low_confidence(item) or item["confidence"] < self._threshold
            for tag, source in self._tags(course, low):
                try:
                    await asyncio.to_thread(
                        self._db.attach_tag, "formula", formula_id, tag, source=source
                    )
                except Exception:
                    continue  # 标签失败不影响公式本身
            self.recognized_count += 1
            entries.append({
                "formula_id": formula_id,
                "name": item["name"],
                "latex": item["latex_normalized"],
                "confidence": item["confidence"],
                "low_confidence": low,
                "created": created,
            })
        return {
            "status": "recognized",
            "formulas": entries,
            "degraded": degraded,
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
