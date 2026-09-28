"""学习笔记存储：按课程分目录收纳公式、重点与图片。

设计原则（与课表存储一致）：

- **人可直接读**：目录名就是课程名（做过文件名收敛），文本重点存 ``.md``，
  图片存原图；``index.json`` 只是检索用的元数据。整个 ``notes/`` 目录随时可以
  直接拷走。
- **原子写**：先写临时文件再 ``os.replace``，中途断电不会留下半份笔记。
- **软归属**：哪门课由调用方判定（当前正在上的课），本层只管"存到哪、怎么查"。

目录结构::

    notes/
    ├── 高等数学/
    │   ├── index.json        # 该课全部条目的元数据
    │   ├── 20260917_1030_a1b2_公式.md
    │   └── img/20260917_1030_c3d4.png
    ├── 机械设计/
    └── 未分类/
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = [
    "NoteKind",
    "StudyNote",
    "StudyNoteStore",
    "course_folder_name",
]

#: 非法文件名字符（课程名来自 ics，可能很随便）
_UNSAFE_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")
MAX_FOLDER_LENGTH = 40
#: 单条文本笔记的长度上限（防止把整篇聊天记录灌进来）
MAX_NOTE_CHARS = 4000
DEFAULT_FOLDER = "未分类"

NoteKind = str  # "公式" / "重点" / "笔记"


def course_folder_name(summary: str) -> str:
    """把课程名收敛成安全目录名；空名退回"未分类"。"""
    cleaned = _UNSAFE_RE.sub("_", str(summary or "").strip())
    cleaned = cleaned.strip("._")
    return (cleaned[:MAX_FOLDER_LENGTH] or DEFAULT_FOLDER)


@dataclass
class StudyNote:
    """一条学习笔记的元数据。"""

    id: str
    course: str
    kind: NoteKind
    text: str = ""
    file: str = ""  # 相对课程目录的文件路径（文本或图片）
    created_at: str = ""
    source: str = ""  # 来源说明（聊天消息/手动）
    #: 识别到的公式（"名称：LaTeX"），图片笔记在识别完成后回填。
    #: 写在索引里而不是只留在 SQLite：/笔记、/找 与笔记文件读的都是这一层。
    formula: str = ""
    topic: str = ""
    content_raw: str = ""  # 视觉模型首次转录；人工修订不会覆盖它
    content: str = ""  # 当前展示的转录
    uncertain_items: list[dict[str, Any]] = field(default_factory=list)
    revisions: list[dict[str, Any]] = field(default_factory=list)
    content_manual: bool = False
    formula_manual: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "file": self.file,
            "created_at": self.created_at,
            "source": self.source,
            "formula": self.formula,
            "topic": self.topic,
            "content_raw": self.content_raw,
            "content": self.content,
            "uncertain_items": self.uncertain_items,
            "revisions": self.revisions,
            "content_manual": self.content_manual,
            "formula_manual": self.formula_manual,
        }

    @property
    def display(self) -> str:
        """列表里给这一条显示什么：有公式就显示公式，其次才是说明文字。"""
        return self.content or self.formula or self.text

    @property
    def unresolved_items(self) -> list[dict[str, Any]]:
        """手工改掉原疑点后不再提示待核对，原疑点仍留在索引里。"""
        return [
            item for item in self.uncertain_items
            if isinstance(item, dict) and not item.get("resolved")
        ]


@dataclass
class _CourseIndex:
    """一门课的索引（内存态，写盘时整体序列化）。"""

    notes: list[StudyNote] = field(default_factory=list)


class StudyNoteStore:
    """按课程目录管理学习笔记。

    线程模型：这里会同时被事件循环（收纳/移动）与 ``asyncio.to_thread`` 的后台
    线程（识别完成回填公式、补识别）写入，索引又是"读-改-写"，所以加了
    ``threading.Lock`` 串行化——与 :class:`NotesDatabase` 同一纪律。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        # 可重入：move_latest 持锁期间还会调 _append（它也拿锁）
        self._lock = threading.RLock()

    # ── 路径 ──────────────────────────────────────────────

    def course_dir(self, course: str) -> Path:
        return self.root / course_folder_name(course)

    def _index_path(self, course_dir: Path) -> Path:
        return course_dir / "index.json"

    def _img_dir(self, course_dir: Path) -> Path:
        return course_dir / "img"

    # ── 索引读写 ──────────────────────────────────────────

    def _load_index(self, course_dir: Path) -> _CourseIndex:
        try:
            raw = json.loads(self._index_path(course_dir).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _CourseIndex()
        entries = raw.get("notes") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            return _CourseIndex()
        notes: list[StudyNote] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            notes.append(
                StudyNote(
                    id=str(item.get("id") or ""),
                    course=course_dir.name,
                    kind=str(item.get("kind") or "笔记"),
                    text=str(item.get("text") or ""),
                    file=str(item.get("file") or ""),
                    created_at=str(item.get("created_at") or ""),
                    source=str(item.get("source") or ""),
                    formula=str(item.get("formula") or ""),
                    topic=str(item.get("topic") or ""),
                    content_raw=str(item.get("content_raw") or ""),
                    content=str(item.get("content") or ""),
                    uncertain_items=(item.get("uncertain_items")
                                     if isinstance(item.get("uncertain_items"), list) else []),
                    revisions=(item.get("revisions")
                               if isinstance(item.get("revisions"), list) else []),
                    content_manual=bool(item.get("content_manual", False)),
                    formula_manual=bool(item.get("formula_manual", False)),
                )
            )
        return _CourseIndex(notes=notes)

    def _write_index(self, course_dir: Path, index: _CourseIndex) -> None:
        payload = {
            "course": course_dir.name,
            "notes": [note.to_dict() for note in index.notes],
        }
        course_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            self._index_path(course_dir),
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    # ── 写入 ──────────────────────────────────────────────

    def add_text_note(
        self, course: str, kind: NoteKind, text: str, *, source: str = ""
    ) -> StudyNote:
        """存一条文本笔记（.md 文件 + 索引）。"""
        text = str(text or "").strip()[:MAX_NOTE_CHARS]
        if not text:
            raise ValueError("笔记内容为空")
        course_dir = self.course_dir(course)
        now = datetime.now()
        note_id = _new_note_id(now)
        folder = course_folder_name(course)
        filename = f"{note_id}_{kind}.md"
        course_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(course_dir / filename, f"# {folder} · {kind}\n\n{text}\n")
        note = StudyNote(
            id=note_id,
            course=folder,
            kind=kind,
            text=text,
            file=filename,
            created_at=now.isoformat(timespec="seconds"),
            source=source,
        )
        self._append(course_dir, note)
        return note

    def add_image_note(
        self,
        course: str,
        kind: NoteKind,
        data: bytes,
        *,
        suffix: str = ".png",
        text: str = "",
        source: str = "",
    ) -> StudyNote:
        """存一条图片笔记（原图落盘 + 索引），可附一句说明。"""
        if not data:
            raise ValueError("图片内容为空")
        course_dir = self.course_dir(course)
        now = datetime.now()
        note_id = _new_note_id(now)
        self._img_dir(course_dir).mkdir(parents=True, exist_ok=True)
        rel_path = f"img/{note_id}{_safe_suffix(suffix)}"
        _atomic_bytes(course_dir / rel_path, data)
        note = StudyNote(
            id=note_id,
            course=course_folder_name(course),
            kind=kind,
            text=str(text or "").strip()[:MAX_NOTE_CHARS],
            file=rel_path,
            created_at=now.isoformat(timespec="seconds"),
            source=source,
        )
        self._append(course_dir, note)
        return note

    def _append(self, course_dir: Path, note: StudyNote) -> None:
        with self._lock:
            index = self._load_index(course_dir)
            index.notes.append(note)
            self._write_index(course_dir, index)

    def _write_image_companion(self, course_dir: Path, note: StudyNote) -> None:
        """图片笔记的可读版本；原图及原始转录留在索引中。"""
        if not note.file.startswith("img/"):
            return
        body = [f"# {course_dir.name} · {note.kind}"]
        if note.topic:
            body += ["", f"主题：{note.topic}"]
        if note.content:
            body += ["", "## 板书转录", "", note.content]
        if note.formula:
            body += ["", "## 公式", "", note.formula]
        if note.unresolved_items:
            body += ["", "## 待核对", ""]
            body += [
                f"- {item.get('term', '')}：{item.get('reason', '')}"
                for item in note.unresolved_items
            ]
        if note.text:
            body += ["", "## 图片说明", "", note.text]
        body += ["", f"原图：{note.file}"]
        _atomic_write(course_dir / f"{note.id}_{note.kind}.md", "\n".join(body) + "\n")

    def update_extraction(self, note_id: str, draft: dict) -> StudyNote | None:
        """保存一次图片转录；用户手工改过的正文不会被重识别覆盖。"""
        with self._lock:
            located = self.locate_note(note_id)
            if located is None:
                return None
            course, note = located
            if not note.file.startswith("img/"):
                return None
            incoming = str(draft.get("content_markdown") or "")[:8000]
            if not note.content_raw:
                note.content_raw = incoming
            if not note.content_manual:
                note.content = incoming
            note.topic = str(draft.get("topic") or "")[:120]
            resolved_terms = {
                str(item.get("term") or "") for item in note.uncertain_items
                if isinstance(item, dict) and item.get("resolved")
            }
            note.uncertain_items = [
                {**item, "resolved": str(item.get("term") or "") in resolved_terms}
                for item in list(draft.get("uncertain_items") or [])[:8]
                if isinstance(item, dict)
            ]
            course_dir = self.course_dir(course)
            self._save_changed_note(course_dir, note)
            return note

    def revise_image_note(self, note_id: str, original: str, replacement: str) -> StudyNote | None:
        """仅替换这张图片笔记的一段转录或公式，保留原稿和操作记录。"""
        original, replacement = original.strip(), replacement.strip()
        if not original or not replacement:
            raise ValueError("原文和修订内容均不能为空")
        if len(replacement) > MAX_NOTE_CHARS:
            raise ValueError("修订内容过长")
        with self._lock:
            located = self.locate_note(note_id)
            if located is None:
                return None
            course, note = located
            if not note.file.startswith("img/"):
                raise ValueError("只支持修订图片识别内容")
            fields = [
                name for name in ("content", "formula") if original in getattr(note, name)
            ]
            if not fields:
                raise ValueError("原文未出现在这条笔记的识别内容中")
            changes = []
            for field_name in fields:
                before = getattr(note, field_name)
                after = before.replace(original, replacement, 1)
                if len(after) > 8000:
                    raise ValueError("修订后的笔记过长")
                setattr(note, field_name, after)
                setattr(note, f"{field_name}_manual", True)
                changes.append({"field": field_name, "before": before, "after": after})
            uncertain_before = copy.deepcopy(note.uncertain_items)
            for item in note.uncertain_items:
                if isinstance(item, dict) and item.get("term") == original:
                    item["resolved"] = True
            note.revisions.append({
                "action": "edit", "changes": changes,
                "uncertain_before": uncertain_before,
                "at": datetime.now().isoformat(timespec="seconds"), "undone": False,
            })
            course_dir = self.course_dir(course)
            self._save_changed_note(course_dir, note)
            return note

    def undo_image_revision(self, note_id: str) -> StudyNote | None:
        """撤销最近一次尚未撤销的人工修订，不删除历史记录。"""
        with self._lock:
            located = self.locate_note(note_id)
            if located is None:
                return None
            course, note = located
            for index in range(len(note.revisions) - 1, -1, -1):
                edit = note.revisions[index]
                if edit.get("action") != "edit" or edit.get("undone"):
                    continue
                for change in edit["changes"]:
                    setattr(note, change["field"], change["before"])
                note.uncertain_items = edit.get("uncertain_before", note.uncertain_items)
                edit["undone"] = True
                note.revisions.append({
                    "action": "undo", "edit_index": index,
                    "at": datetime.now().isoformat(timespec="seconds"),
                })
                for change in edit["changes"]:
                    field_name = change["field"]
                    setattr(note, f"{field_name}_manual", any(
                        item.get("action") == "edit" and not item.get("undone")
                        and any(part.get("field") == field_name for part in item.get("changes", []))
                        for item in note.revisions
                    ))
                self._save_changed_note(self.course_dir(course), note)
                return note
            raise ValueError("这条笔记没有可撤销的人工修订")

    def _save_changed_note(self, course_dir: Path, note: StudyNote) -> None:
        index = self._load_index(course_dir)
        for index_note in index.notes:
            if index_note.id == note.id:
                index_note.__dict__.update(note.__dict__)
                break
        self._write_index(course_dir, index)
        self._write_image_companion(course_dir, note)

    def attach_formula(
        self, course: str, note_id: str, formula: str
    ) -> StudyNote | None:
        """把识别到的公式回填到某条笔记：索引 + 一个可读的 .md。

        图片笔记原本只有一张图（``file`` 指向 ``img/…``），没有任何可读正文，
        于是"翻笔记"翻到的只是一句图说。识别出公式后在这里补一份
        ``<id>_<kind>.md``，让公式本身成为这条笔记的内容。

        同一公式重复回填不重复追加（幂等）；笔记已被删掉则返回 ``None``。
        """
        text = str(formula or "").strip()[:MAX_NOTE_CHARS]
        if not text:
            return None
        course_dir = self.course_dir(course)
        with self._lock:
            index = self._load_index(course_dir)
            target = next((item for item in index.notes if item.id == note_id), None)
            if target is None:
                return None
            if target.formula_manual:
                return target
            if text == (target.formula or ""):
                return target  # 已经回填过，别重复写盘
            target.formula = text
            self._write_index(course_dir, index)
            self._write_image_companion(course_dir, target)
            return target

    def clear_formula(self, course: str, note_id: str) -> StudyNote | None:
        """清掉自动回填的公式（重识别判定"图里没有公式"时用）。

        只动自动生成的部分：formula 字段清空，配套 md 重写为只含图说与原图引用；
        用户的原文（text）与原图不动。公式库里那条公式记录也不删——它可能被
        其他图片的识别记录引用。幂等：本来就空就不再写盘。
        """
        course_dir = self.course_dir(course)
        with self._lock:
            index = self._load_index(course_dir)
            target = next((item for item in index.notes if item.id == note_id), None)
            if target is None:
                return None
            if target.formula_manual:
                return target
            if not target.formula:
                return target
            target.formula = ""
            self._write_index(course_dir, index)
            self._write_image_companion(course_dir, target)
            return target

    # ── 查询 ──────────────────────────────────────────────

    def courses(self) -> list[str]:
        """有哪些课程目录（含"未分类"），按名字排序。"""
        if not self.root.exists():
            return []
        return sorted(
            item.name
            for item in self.root.iterdir()
            if item.is_dir() and (item / "index.json").exists()
        )

    def recent(self, course: str, limit: int = 10) -> list[StudyNote]:
        """某课最近的笔记（新的在前）。"""
        index = self._load_index(self.course_dir(course))
        return list(reversed(index.notes[-limit:]))

    def count(self, course: str) -> int:
        return len(self._load_index(self.course_dir(course)).notes)

    def search(self, keyword: str, *, limit: int = 10) -> list[StudyNote]:
        """跨课程按关键词搜笔记内容（大小写不敏感的包含匹配）。

        公式也算内容：用户搜「许用应力」时，图片笔记要靠识别出的公式命中，
        而不是靠那句"这是一张课件幻灯片"的图说。
        """
        needle = str(keyword or "").strip().lower()
        if not needle:
            return []
        hits: list[StudyNote] = []
        for course in self.courses():
            for note in reversed(self._load_index(self.course_dir(course)).notes):
                haystack = (
                    f"{note.text}\n{note.formula}\n{note.content}\n"
                    f"{note.topic}\n{note.course}"
                ).lower()
                if needle in haystack:
                    hits.append(note)
                    if len(hits) >= limit:
                        return hits
        return hits

    # ── 归类修正 ──────────────────────────────────────────

    def move_latest(self, from_course: str, to_course: str) -> StudyNote | None:
        """把 from 课目录里**最近一条**笔记挪到 to 课目录（人工纠正归属）。"""
        with self._lock:
            index = self._load_index(self.course_dir(from_course))
            if not index.notes:
                return None
            return self.move_note(from_course, index.notes[-1].id, to_course)

    def locate_note(self, note_id: str) -> tuple[str, StudyNote] | None:
        """按 id 在所有课程目录里找一条笔记，返回 ``(课程名, 笔记)``。

        异步识别的回填用它定位笔记的**当前位置**：识别排队几秒到几分钟，
        期间用户可能已经 /归到 把笔记挪去了别的课程，任务创建时的课程名会过期。
        """
        with self._lock:
            for course in self.courses():
                for note in self._load_index(self.course_dir(course)).notes:
                    if note.id == note_id:
                        return course, note
        return None

    def update_formula(self, note_id: str, formula: str) -> StudyNote | None:
        """按笔记 id 替换自动识别结果；定位与写入共用归档锁。

        空结果清除自动公式。整个操作不可被 move_note 插入，避免定位后
        笔记又被移走；用户原文与图片保持原样。
        """
        with self._lock:
            located = self.locate_note(note_id)
            if located is None:
                return None
            course, _note = located
            if str(formula or "").strip():
                return self.attach_formula(course, note_id, formula)
            return self.clear_formula(course, note_id)

    def read_note_image(self, note_id: str) -> tuple[bytes, str] | None:
        """在归档锁内读取笔记当前位置的原图，供等待中的补识别任务使用。"""
        with self._lock:
            located = self.locate_note(note_id)
            if located is None:
                return None
            course, note = located
            if not note.file or not note.file.startswith("img/"):
                return None
            path = self.course_dir(course) / note.file
            return path.read_bytes(), path.suffix

    def move_note(
        self, course: str, note_id: str, to_course: str
    ) -> StudyNote | None:
        """把**指定 id** 的笔记从 course 挪到 to_course；找不到返回 ``None``。

        按 id 而不是"最近一条"：候选确认（/归到 1）从发起到执行中间可能隔着
        新笔记，重取"最近一条"就会归错对象。
        """
        src_dir = self.course_dir(course)
        dst_dir = self.course_dir(to_course)
        with self._lock:
            index = self._load_index(src_dir)
            note = next((item for item in index.notes if item.id == note_id), None)
            if note is None:
                return None
            index.notes.remove(note)
            self._write_index(src_dir, index)
            note.course = course_folder_name(to_course)
            dst_dir.mkdir(parents=True, exist_ok=True)
            if note.file:
                src_file = src_dir / note.file
                dst_file = dst_dir / note.file
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(src_file, dst_file)
                except OSError:
                    # 文件挪不动（被手动删了等）也保留索引记录，文本仍在
                    note.file = ""
            # 配套的可读 md（识别回填给图片笔记生成的公式文档）一起带走：
            # 留在原课程就成了孤儿文件，/笔记 与 /找 读的是新位置的索引
            companion = src_dir / f"{note.id}_{note.kind}.md"
            if companion.is_file():
                try:
                    os.replace(companion, dst_dir / companion.name)
                except OSError:
                    pass
            self._append(dst_dir, note)
            if note.file.startswith("img/") and (note.content or note.formula or companion.is_file()):
                self._write_image_companion(dst_dir, note)
            return note

    def last_of(self, course: str) -> StudyNote | None:
        notes = self._load_index(self.course_dir(course)).notes
        return notes[-1] if notes else None

    def notes_between(
        self, course: str, start: datetime, end: datetime
    ) -> list[StudyNote]:
        """某课程在 ``[start, end]`` 时间段内记的笔记（按时间正序）。

        边界比较前把两端截到整秒：created_at 落盘时就是秒精度，
        不截的话微秒残留会让"恰好等于窗口起点"的笔记被排除掉。
        """
        start = start.replace(microsecond=0)
        end = end.replace(microsecond=0)
        result: list[StudyNote] = []
        for note in self._load_index(self.course_dir(course)).notes:
            if not note.created_at:
                continue
            try:
                moment = datetime.fromisoformat(note.created_at)
            except ValueError:
                continue
            if start <= moment <= end:
                result.append(note)
        return result

    # ── 课后总结 ──────────────────────────────────────────

    def summaries_dir(self, course: str) -> Path:
        return self.course_dir(course) / "总结"

    def add_summary(self, course: str, day: str, markdown: str) -> Path:
        """保存某天该课的课后总结到 ``总结/<day>.md``，返回文件路径。

        总结由模型生成的 Markdown 组成（不是用户原始输入），直接原子落盘。
        """
        if not str(markdown or "").strip():
            raise ValueError("总结内容为空")
        target = self.summaries_dir(course) / f"{day}.md"
        _atomic_write(target, str(markdown))
        return target

    def summaries(self, course: str) -> list[str]:
        """该课已有哪些课后总结（按日期排序，新的在后）。"""
        folder = self.summaries_dir(course)
        if not folder.exists():
            return []
        return sorted(item.stem for item in folder.glob("*.md"))


# ── 工具 ──────────────────────────────────────────────────

def _new_note_id(now: datetime) -> str:
    """时间戳 + 4 位随机尾巴，同一秒内也不重名。"""
    import secrets

    return f"{now.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"


def _safe_suffix(suffix: str) -> str:
    cleaned = _UNSAFE_RE.sub("", str(suffix or "").lower())
    return cleaned if cleaned.startswith(".") and len(cleaned) <= 6 else ".bin"


def _replace_with_retry(tmp: Path, path: Path, *, attempts: int = 5) -> None:
    """``os.replace`` 遇到 Windows 瞬时占用（WinError 5）时短暂重试。

    杀毒/索引器会短暂握住刚写出的文件，一次 replace 就可能被拒；这不是内容
    问题，退避几十毫秒再试就能成功。重试耗尽才抛错（调用方按原语义处理）。
    """
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.02 * (attempt + 1))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        _replace_with_retry(Path(tmp), path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        _replace_with_retry(Path(tmp), path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
