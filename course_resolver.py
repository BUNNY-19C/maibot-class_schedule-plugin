"""课程名称解析：用户可以输入简称，但库里永远保存标准课程名。

设计要点（为什么长这样）：

- **解析顺序固定**：精确 → 别名 → 唯一包含 → 模糊，前一步命中就不再往后走。
  "自动控制" 命中唯一包含时根本不需要 fuzzy，更不需要 LLM；
- **唯一包含是第一版的重点**：``"自动控制" in "航空自动控制基础"`` 且只命中
  一门课时直接归档；但输入长度不足 4 个字符时不自动决定——未来可能同时存在
  自动控制原理 / 飞行控制系统 / 现代控制理论，短输入含糊不得；
- **多候选绝不强行选第一名**：不按字符串长短、课表顺序、数据库顺序或"第一个
  结果"自动决定，返回 AMBIGUOUS 让用户挑。这是整个功能最重要的防误归档规则；
- **fuzzy 只负责错字和残缺输入**（``航空自动控制基`` 这类），用标准库
  ``difflib.SequenceMatcher`` 就够；而且必须同时看第一名和第二名——两个分数
  很接近（0.91 vs 0.90）时明显有歧义，不能自动选；
- **名称归一是纯函数**：全角/半角、大小写、空白、连接符、部分标点都收掉，
  但绝不删除 基础/原理/实验/设计 这类词——它们可能正是区分课程的关键。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from enum import Enum

#: 唯一包含匹配允许自动执行的最短输入（normalize 后的字符数）。
#: 低于它宁可交给模糊/候选，也不拿两三个字去猜课程。
CONTAINS_MIN_QUERY_LENGTH = 4

#: fuzzy 自动执行的最低分数：低于它最多只作为候选列出来
FUZZY_AUTO_THRESHOLD = 0.86
#: fuzzy 进入候选列表的最低分数：再低就当作完全无关
FUZZY_SUGGEST_THRESHOLD = 0.70
#: 第一名必须领先第二名的幅度：差距不足就是"明显有歧义"
FUZZY_MIN_MARGIN = 0.12

#: 候选最多列几条。超过这个数用户也挑不过来，宁可提示换更完整的名称
MAX_CANDIDATES = 5

#: 名称归一时要剔除的字符：空白、连接符、常见标点（NFKC 会把全角形态收成这些
#: ASCII/半角形态，所以一张表就够）。全部从匹配语义里去掉，但不会碰任何
#: 汉字/字母/数字。
_STRIP_CHARS = (
    " \t\r\n\f\v\u3000"
    "-‐‑‒–—―＿_~～·・.。,，、;；:：!！?？"
    "'\"“”‘’«»()（）【】〔〕［］[]{}｛｝《》〈〉<>「」『』"
    "+*=|\\/˄§°·"
)
_STRIP_TABLE = {ord(ch): None for ch in _STRIP_CHARS}


class MatchKind(str, Enum):
    """一次解析是怎么得出结果的（决定回执措辞与是否需要用户确认）。"""

    EXACT = "exact"
    ALIAS = "alias"
    CONTAINS = "contains"
    FUZZY = "fuzzy"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class CourseCandidate:
    """一个可能的课程：分数只用于候选的展示排序，绝不用来替代用户确认。"""

    name: str
    score: float
    method: MatchKind


@dataclass(frozen=True)
class CourseResolution:
    query: str
    kind: MatchKind
    selected: str | None
    candidates: tuple[CourseCandidate, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.selected is not None


@dataclass(frozen=True)
class PendingCourseChoice:
    """一次歧义候选的待确认状态：**绑定到具体那条笔记**。

    为什么必须绑定：候选列出到用户回复 /归到 1 之间，未分类里可能又进了新笔记；
    执行时若重取"最近一条未分类"就会归错对象（评估清单第十一节的场景）。
    note_id 是 markdown 层的笔记 id（字符串，见 StudyNote.id）。
    """

    note_id: str
    original_query: str
    candidates: tuple[str, ...]
    expires_at: datetime

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


def normalize_course_name(text: str) -> str:
    """把课程名的各种写法收敛成同一个比较形态（纯函数，幂等）。

    处理：前后与中间的空白、全角/半角（NFKC）、英文大小写、普通连接符、
    部分标点。例如 " 航空 自动控制基础 "、"航空-自动控制基础"、
    "ＦＵＬＬWIDTH"，都能和 "航空自动控制基础" 一样参与比较。

    **不删除** 基础/原理/实验/设计 这类词——它们可能正是区分课程的信息；
    也不对汉字做任何改写。
    """
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return normalized.translate(_STRIP_TABLE)


def similarity(a: str, b: str) -> float:
    """两个（已归一的）名称的相似度，0 到 1。fuzzy 只用它兜底错字。"""
    return SequenceMatcher(None, a, b).ratio()


def _candidates_from(
    query_norm: str,
    names: list[str],
    *,
    method: MatchKind,
    threshold: float | None,
) -> tuple[CourseCandidate, ...]:
    """把一批课程名整理成候选（按分数降序；分数相同保持原有顺序）。

    ``threshold`` 不为 None 时只保留达到阈值的候选（fuzzy 用）；
    contains 场景传 None 表示全部保留（能包含就算强信号）。
    """
    scored = [
        (name, similarity(query_norm, normalize_course_name(name)))
        for name in names
    ]
    if threshold is not None:
        scored = [(name, score) for name, score in scored if score >= threshold]
    scored.sort(key=lambda item: item[1], reverse=True)
    return tuple(
        CourseCandidate(name=name, score=round(score, 4), method=method)
        for name, score in scored[:MAX_CANDIDATES]
    )


class CourseResolver:
    """把用户输入的课程名解析成标准课程名。外部只需要调 :meth:`resolve`。

    阈值可在构造时覆盖（测试用），默认值来自模块常量；解析本身无状态、
    不碰任何 IO，课程名列表由调用方（plugin）给出。
    """

    def __init__(
        self,
        *,
        fuzzy_auto_threshold: float = FUZZY_AUTO_THRESHOLD,
        fuzzy_suggest_threshold: float = FUZZY_SUGGEST_THRESHOLD,
        fuzzy_min_margin: float = FUZZY_MIN_MARGIN,
        contains_min_query_length: int = CONTAINS_MIN_QUERY_LENGTH,
        max_candidates: int = MAX_CANDIDATES,
    ) -> None:
        self.fuzzy_auto_threshold = float(fuzzy_auto_threshold)
        self.fuzzy_suggest_threshold = float(fuzzy_suggest_threshold)
        self.fuzzy_min_margin = float(fuzzy_min_margin)
        self.contains_min_query_length = int(contains_min_query_length)
        self.max_candidates = max(1, int(max_candidates))

    def resolve(
        self,
        query: str,
        course_names: list[str],
        aliases: dict[str, str] | None = None,
    ) -> CourseResolution:
        """按 固定顺序 解析：精确 → 别名 → 唯一包含 → 模糊 → 候选/未找到。

        ``course_names`` 是当前所有标准课程名（重复与顺序由调用方负责清理，
        这里会按归一化去重）；``aliases`` 是"确认过的别名 → 标准名"映射，
        第一版可以不传。
        """
        query_norm = normalize_course_name(query)
        if not query_norm:
            return CourseResolution(
                query=str(query or ""), kind=MatchKind.NOT_FOUND, selected=None
            )

        # 去重 + 保序：同名（归一化后）的课程只留第一个
        unique: list[str] = []
        seen: set[str] = set()
        for name in course_names:
            key = normalize_course_name(name)
            if key and key not in seen:
                seen.add(key)
                unique.append(name)

        # 1. 精确匹配（归一化后的全等）
        exact = [
            name for name in unique if normalize_course_name(name) == query_norm
        ]
        if len(exact) == 1:
            return CourseResolution(
                query=str(query), kind=MatchKind.EXACT, selected=exact[0]
            )

        # 2. 已确认别名（第一版通常不传；映射的值必须在标准课程里才可信）
        if aliases:
            target = aliases.get(query_norm)
            if target is not None and target in unique:
                return CourseResolution(
                    query=str(query), kind=MatchKind.ALIAS, selected=target
                )

        # 3. 唯一包含：输入是某门课名的子串，且只有一门命中、输入足够长
        contained = [
            name for name in unique if query_norm in normalize_course_name(name)
        ]
        if len(contained) > 1:
            return CourseResolution(
                query=str(query),
                kind=MatchKind.AMBIGUOUS,
                selected=None,
                candidates=_candidates_from(
                    query_norm, contained, method=MatchKind.CONTAINS, threshold=None
                )[: self.max_candidates],
            )
        if len(contained) == 1 and len(query_norm) >= self.contains_min_query_length:
            return CourseResolution(
                query=str(query), kind=MatchKind.CONTAINS, selected=contained[0]
            )

        # 4. fuzzy：只兜错字和残缺输入，且必须同时看第一名和第二名
        return self._resolve_fuzzy(str(query), query_norm, unique)

    def _resolve_fuzzy(
        self, query: str, query_norm: str, course_names: list[str]
    ) -> CourseResolution:
        scored = sorted(
            (
                (name, similarity(query_norm, normalize_course_name(name)))
                for name in course_names
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        candidates = [
            CourseCandidate(name=name, score=round(score, 4), method=MatchKind.FUZZY)
            for name, score in scored
            if score >= self.fuzzy_suggest_threshold
        ][: self.max_candidates]
        if not candidates:
            return CourseResolution(
                query=query, kind=MatchKind.NOT_FOUND, selected=None
            )
        top1 = candidates[0].score
        top2 = candidates[1].score if len(candidates) > 1 else 0.0
        if top1 >= self.fuzzy_auto_threshold and top1 - top2 >= self.fuzzy_min_margin:
            return CourseResolution(
                query=query, kind=MatchKind.FUZZY, selected=candidates[0].name
            )
        # 分数不够高，或前两名咬得太近：把候选交给用户，绝不强行选第一名
        return CourseResolution(
            query=query, kind=MatchKind.AMBIGUOUS, selected=None, candidates=candidates
        )
