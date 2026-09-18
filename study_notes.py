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

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "file": self.file,
            "created_at": self.created_at,
            "source": self.source,
        }


@dataclass
class _CourseIndex:
    """一门课的索引（内存态，写盘时整体序列化）。"""

    notes: list[StudyNote] = field(default_factory=list)


class StudyNoteStore:
    """按课程目录管理学习笔记。

    线程模型：所有写操作都在调用方的线程/事件循环里串行发生
    （插件侧经 asyncio 锁串行化），本类内部不再加锁——与 CourseRepository
    不同，这里没有 to_thread 并发路径，避免过度设计。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

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
        index = self._load_index(course_dir)
        index.notes.append(note)
        self._write_index(course_dir, index)

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
        """跨课程按关键词搜笔记内容（大小写不敏感的包含匹配）。"""
        needle = str(keyword or "").strip().lower()
        if not needle:
            return []
        hits: list[StudyNote] = []
        for course in self.courses():
            for note in reversed(self._load_index(self.course_dir(course)).notes):
                if needle in note.text.lower() or needle in note.course.lower():
                    hits.append(note)
                    if len(hits) >= limit:
                        return hits
        return hits

    # ── 归类修正 ──────────────────────────────────────────

    def move_latest(self, from_course: str, to_course: str) -> StudyNote | None:
        """把 from 课目录里**最近一条**笔记挪到 to 课目录（人工纠正归属）。"""
        src_dir = self.course_dir(from_course)
        dst_dir = self.course_dir(to_course)
        index = self._load_index(src_dir)
        if not index.notes:
            return None
        note = index.notes.pop()
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
        self._append(dst_dir, note)
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


    def save_raw_image(self, course: str, data: bytes, suffix: str = ".png") -> str:
        """保存一张原图到 <课程>/img/ 并返回相对路径（管道用，不建索引）。"""
        import secrets
        from datetime import datetime as _dt

        if not data:
            raise ValueError("图片内容为空")
        folder = self._img_dir(self.course_dir(course))
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{_dt.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}{suffix}"
        _atomic_bytes(folder / name, data)
        return f"img/{name}"

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


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
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
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
