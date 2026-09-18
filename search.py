"""混合检索：查询解析 + 全文 + 语义召回 + 标签/时间过滤 + 可选精排。

设计决策：

- **查询解析是规则的**（"上周三高数作业"→ 时间/课程/类型/关键词），
  不依赖模型：解析错了好排查，也不会因模型故障而检索全挂；
- **语义召回是增强的不是必需的**：没配 Key、没向量时全文照常工作；
  有向量时按余弦补充召回（个人量级内存暴力扫，毫秒级）；
- **精排可选**：配了 rerank 模型就对候选做一次重排，失败退回原序——
  排序增强绝不能变成检索的故障点。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable

from .llm_client import CloudError
from .notes_db import cosine

#: 口语时间词 → (相对周偏移, 星期)。星期 0=周一
_WEEKDAY_RE = re.compile(
    r"([上这本下])?(?:星期|礼拜|周)([一二三四五六日天])"
)
_RELATIVE_DAYS = {"前天": -2, "昨天": -1, "今天": 0, "明天": 1, "后天": 2}
_WEEK_OFFSET = {"上": -1, "这": 0, "本": 0, "下": 1, "": 0}
_WEEKDAY_NUM = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_DATE_RE = re.compile(r"(\d{1,2})月(\d{1,2})日?")
_TYPE_WORDS = {
    "作业": "#作业",
    "ddl": "#作业",
    "公式": "#公式",
    "定理": "#定理",
    "概念": "#概念",
    "例题": "#例题",
    "证明": "#证明",
    "疑问": "#疑问",
    "重点": "#重点",
}


@dataclass
class ParsedQuery:
    keywords: str = ""
    courses: list[str] = field(default_factory=list)
    type_tag: str = ""
    day_start: datetime | None = None
    day_end: datetime | None = None


def parse_query(
    text: str, courses: Iterable[str] = (), *, now: datetime | None = None
) -> ParsedQuery:
    """把自然语言查询拆成 时间/课程/类型/关键词。识别不了的都归关键词。"""
    moment = now or datetime.now()
    raw = str(text or "").strip()
    parsed = ParsedQuery()
    remainder = raw

    match = _WEEKDAY_RE.search(remainder)
    if match:
        prefix = match.group(1) or "本"
        weekday = _WEEKDAY_NUM[match.group(2)]
        this_monday = moment - timedelta(days=moment.weekday())
        day = this_monday + timedelta(
            days=weekday + _WEEK_OFFSET.get(prefix, 0) * 7
        )
        parsed.day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        parsed.day_end = parsed.day_start + timedelta(days=1)
        remainder = (remainder[: match.start()] + remainder[match.end():]).strip()
    else:
        for word, offset in _RELATIVE_DAYS.items():
            if word in remainder:
                day = (moment + timedelta(days=offset)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                parsed.day_start = day
                parsed.day_end = day + timedelta(days=1)
                remainder = remainder.replace(word, "", 1).strip()
                break
        else:
            date_match = _DATE_RE.search(remainder)
            if date_match:
                month, day = (int(x) for x in date_match.groups())
                try:
                    start = moment.replace(
                        month=month, day=day, hour=0, minute=0, second=0, microsecond=0
                    )
                    parsed.day_start = start
                    parsed.day_end = start + timedelta(days=1)
                    remainder = (
                        remainder[: date_match.start()] + remainder[date_match.end():]
                    ).strip()
                except ValueError:
                    pass  # 不存在的日期当关键词

    for course in sorted(set(courses), key=len, reverse=True):
        if course and course in remainder:
            parsed.courses.append(course)
            remainder = remainder.replace(course, "", 1).strip()

    lowered = remainder.lower()
    for word, tag in _TYPE_WORDS.items():
        if word in lowered or word in remainder:
            parsed.type_tag = tag
            remainder = re.sub(re.escape(word), "", remainder, flags=re.I).strip()
            break

    # 剥掉时间词删完后的悬垂助词（「昨天的X」→「X」而不是「的X」）
    remainder = re.sub(r"^[的为了是在讲]+\s*", "", remainder)
    parsed.keywords = re.sub(r"\s+", " ", remainder).strip()
    return parsed


class HybridSearcher:
    """全文 + 语义 + 过滤的混合检索。db 是 NotesDatabase，client 可为 None。"""

    def __init__(
        self,
        db: Any,
        *,
        client: Any | None = None,
        embedding_model: str = "",
        rerank_model: str = "",
        semantic_limit: int = 8,
    ) -> None:
        self._db = db
        self._client = client
        self._embedding_model = embedding_model
        self._rerank_model = rerank_model
        self._semantic_limit = max(1, int(semantic_limit))

    async def search(
        self,
        query: str,
        *,
        courses: Iterable[str] = (),
        now: datetime | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """返回 {parsed, notes, formulas}。notes 每条带 matched_by 标注来源。"""
        parsed = parse_query(query, courses=courses, now=now)
        needle = parsed.keywords or str(query or "").strip()
        hits: dict[int, dict[str, Any]] = {}

        if needle:
            for row in self._db.search_text(needle, limit=limit):
                note = self._to_note_dict(row, matched_by="全文")
                if self._passes_filters(note, parsed):
                    hits[note["id"]] = note

        # 语义召回：有向量 + 有模型 + 有文本才做；失败只降级，不影响全文结果
        semantic_error = ""
        if needle and self._semantic_available():
            try:
                vectors = await self._client.embed(
                    model=self._embedding_model, texts=[needle]
                )
                if vectors:
                    scored = [
                        (cosine(vectors[0], vec), note_id)
                        for note_id, vec in self._db.all_embeddings()
                    ]
                    scored.sort(reverse=True)
                    for score, note_id in scored[: self._semantic_limit]:
                        if score <= 0.0:
                            continue
                        if note_id in hits:
                            hits[note_id]["matched_by"] = "全文+语义"
                            continue
                        row = self._db.get_note(note_id)
                        if row is None:
                            continue
                        note = self._to_note_dict(row, matched_by="语义")
                        note["score"] = round(score, 3)
                        if self._passes_filters(note, parsed):
                            hits[note_id] = note
            except CloudError as exc:
                semantic_error = str(exc)[:120]

        notes = sorted(
            hits.values(),
            key=lambda item: (item.get("matched_by") != "全文+语义", -item.get("id", 0)),
        )
        if self._rerank_model and self._semantic_available() and len(notes) > 1:
            notes = await self._rerank(needle or query, notes)

        formulas = [
            dict(row) for row in self._db.search_formulas(needle or query, limit=5)
        ] if (needle or query) else []
        return {
            "parsed": parsed,
            "notes": notes[:limit],
            "formulas": formulas,
            "semantic_error": semantic_error,
        }

    # ── 内部 ──────────────────────────────────────────────

    def _semantic_available(self) -> bool:
        return bool(self._client and self._embedding_model)

    def _to_note_dict(self, row: Any, *, matched_by: str) -> dict[str, Any]:
        note = dict(row)
        note["matched_by"] = matched_by
        note["tags"] = self._db.tags_for(int(note["id"]))
        return note

    def _passes_filters(self, note: dict[str, Any], parsed: ParsedQuery) -> bool:
        if parsed.courses and note.get("course") not in parsed.courses:
            return False
        if parsed.type_tag and parsed.type_tag not in (note.get("tags") or []):
            return False
        if parsed.day_start is not None:
            created = str(note.get("created_at") or "")
            try:
                moment = datetime.fromisoformat(created)
            except ValueError:
                return False
            if not (parsed.day_start <= moment < (parsed.day_end or parsed.day_start)):
                return False
        return True

    async def _rerank(self, query: str, notes: list[dict[str, Any]]) -> list[dict]:
        try:
            documents = [
                f"{n.get('course','')} {n.get('raw_content','')[:200]}" for n in notes
            ]
            ranked = await self._client.rerank(
                model=self._rerank_model,
                query=query,
                documents=documents,
                top_n=len(notes),
            )
        except CloudError:
            return notes  # 精排失败保持原序——增强功能不能变成故障点
        ordered: list[dict] = []
        used: set[int] = set()
        for item in ranked:
            index = int(item.get("index", -1))
            if 0 <= index < len(notes) and index not in used:
                used.add(index)
                note = dict(notes[index])
                note["rerank_score"] = item.get("relevance_score")
                ordered.append(note)
        ordered.extend(n for i, n in enumerate(notes) if i not in used)
        return ordered


def format_hits(result: dict[str, Any], *, max_items: int = 5) -> str:
    """检索结果的聊天展示：摘要、标签、课程/周次/节次、来源。"""
    lines: list[str] = []
    parsed: ParsedQuery = result.get("parsed") or ParsedQuery()
    scopes = []
    if parsed.courses:
        scopes.append("/".join(parsed.courses))
    if parsed.day_start:
        scopes.append(parsed.day_start.strftime("%m-%d"))
    if parsed.type_tag:
        scopes.append(parsed.type_tag)
    prefix = " ".join(scopes) or "全库"
    notes = result.get("notes") or []
    formulas = result.get("formulas") or []
    total = len(notes) + len(formulas)
    if not total:
        return f"🔍 没找到相关记录（{prefix}）"
    lines.append(f"🔍 {total} 条相关（{prefix}）")
    for note in notes[:max_items]:
        tags = " ".join((note.get("tags") or [])[:3])
        when = str(note.get("created_at") or "")[5:16].replace("T", " ")
        meta = "/".join(
            x for x in (
                str(note.get("course") or ""),
                f"W{note['week']}" if note.get("week") else "",
                note.get("period") or "",
            ) if x
        )
        snippet = (note.get("raw_content") or "")[:40] or note.get("image_path") or "（图）"
        lines.append(
            f"　#{note.get('id')} {snippet}"
            f" 〔{meta}｜{when}｜{note.get('matched_by')}｜{tags or '无标签'}｜{note.get('status')}〕"
        )
    for formula in formulas[:3]:
        lines.append(
            f"　🧮 {formula.get('name')} {formula.get('latex_normalized') or formula.get('latex_raw')}"
            f"（{formula.get('category') or '未分类'}，置信 {formula.get('confidence')}）"
        )
    if len(notes) > max_items:
        lines.append(f"　…还有 {len(notes) - max_items} 条")
    return "\n".join(lines)
