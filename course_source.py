"""课表来源：ics 目录扫描与导入落盘。

设计取舍：**所有课表都以 ics 文件的形式存在同一个目录里**。
本地放入的文件、URL 下载来的文件一视同仁 —— 好处是只有一个数据源，
不需要维护「文件 + 数据库」两份状态，用户也能直接看到/替换/删除文件。

目录结构（位于插件 data 目录下）::

    data/plugins/github.BUNNY-19C.class-schedule/
    ├── state.json          # 提醒目标、提前量覆盖、已提醒记录
    └── ics/
        ├── 2026秋-教务处.ics
        └── webcal-20260901-120000.ics
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .file_intake import content_fingerprint
from .ics_parser import (
    CourseEvent,
    decode_ics_bytes,
    parse_ics,
    parse_ics_many,
    parse_ics_with_warnings,
)

__all__ = [
    "CourseRepository",
    "ImportResult",
    "import_filename",
    "safe_filename",
]

_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")
MAX_FILENAME_LENGTH = 80
#: 记录「网址导入的文件名 → 来源网址 + 内容指纹」的文件。放在 ics 目录里、
#: 跟课表数据待在一起；自动刷新靠它知道去哪重新下载、内容有没有变化。
SOURCES_FILENAME = ".sources.json"


def safe_filename(name: str, fallback: str = "schedule") -> str:
    """把任意来源的字符串收敛成安全的文件名（不含路径分隔符）。"""
    cleaned = _SAFE_NAME_RE.sub("_", str(name or "").strip())
    cleaned = cleaned.strip("._") or fallback
    if not cleaned.lower().endswith(".ics"):
        cleaned += ".ics"
    stem = cleaned[:-4][:MAX_FILENAME_LENGTH]
    return f"{stem or fallback}.ics"


def import_filename(url: str) -> str:
    """由网址生成**稳定**的导入文件名。

    同一网址重复导入必须落到同一个文件名，否则旧课表会留在目录里：
    课程一旦调课，旧文件里的原时间仍会触发提醒。

    前缀 ``imported-`` 加上网址短哈希，保证不会撞上用户手动放进目录的文件。
    """
    path = urlsplit(str(url or "")).path
    base = Path(path).name or "schedule"
    digest = hashlib.sha256(str(url or "").strip().encode("utf-8")).hexdigest()[:8]
    stem = safe_filename(base, fallback="schedule")[:-4]
    return f"imported-{stem}-{digest}.ics"


class ImportResult:
    """一次导入的结果。"""

    def __init__(
        self,
        path: Path,
        event_count: int,
        warnings: list[str],
        *,
        replaced: bool = False,
    ) -> None:
        self.path = path
        self.event_count = event_count
        self.warnings = warnings
        self.replaced = replaced

    @property
    def filename(self) -> str:
        """落盘后的文件名。"""
        return self.path.name


class CourseRepository:
    """负责 ics 目录的扫描、解析与导入。

    解析结果带 TTL 缓存，避免每次 tick 都重新读盘；
    ``/课表重载`` 与导入操作会强制刷新。
    """

    def __init__(self, ics_dir: Path, *, cache_seconds: int = 300) -> None:
        self.ics_dir = Path(ics_dir)
        self.cache_seconds = max(0, int(cache_seconds))
        self._events: list[CourseEvent] = []
        self._errors: list[str] = []
        self._loaded_at: datetime | None = None
        self._fingerprint: tuple[tuple[str, int, int], ...] = ()
        # 文件名 → {"url": 来源网址, "fingerprint": 内容指纹, "updated_at": ...}
        # 自动刷新靠它重新下载并判断有没有变化
        self._url_sources: dict[str, dict[str, str]] = {}
        self._load_url_sources()
        # refresh 会被事件循环线程和 to_thread 的工作线程同时调用，
        # 而 asyncio 锁管不到线程池路径，所以这里用线程锁串行化
        self._lock = threading.Lock()

    # ── 扫描 ──────────────────────────────────────────────

    def ensure_dir(self) -> None:
        """确保 ics 目录存在。"""
        self.ics_dir.mkdir(parents=True, exist_ok=True)

    def _scan_fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        """目录指纹（文件名 + 大小 + mtime），用于判断是否需要重新解析。"""
        try:
            entries = sorted(self.ics_dir.glob("*.ics"))
        except OSError:
            return ()
        fingerprint = []
        for path in entries:
            try:
                stat = path.stat()
            except OSError:
                continue
            fingerprint.append((path.name, stat.st_size, int(stat.st_mtime)))
        return tuple(fingerprint)

    def refresh(self, *, force: bool = False) -> None:
        """按需重新扫描目录并解析。

        没有文件变动且缓存未过期时直接复用上次结果。
        """
        with self._lock:
            self._refresh_locked(force=force)

    def _refresh_locked(self, *, force: bool) -> None:
        """实际执行扫描；调用方必须已持有 ``self._lock``。"""
        now = datetime.now()
        fingerprint = self._scan_fingerprint()

        if not force and fingerprint == self._fingerprint and self._loaded_at is not None:
            if self.cache_seconds <= 0:
                return
            age = (now - self._loaded_at).total_seconds()
            if age < self.cache_seconds:
                return

        sources: dict[str, str] = {}
        errors: list[str] = []
        for path in sorted(self.ics_dir.glob("*.ics")):
            try:
                # 用 bytes 读再嗅探编码：教务导出的 ics 常见 GBK，按 UTF-8 读会乱码
                sources[path.name] = decode_ics_bytes(path.read_bytes())
            except OSError as exc:
                errors.append(f"{path.name}: 读取失败（{exc}）")

        # 复用解析层的"逐份解析、失败收集"，避免同一份逻辑两处维护
        events, parse_errors = parse_ics_many(sources)

        self._events = events
        self._errors = errors + parse_errors
        self._fingerprint = fingerprint
        self._loaded_at = now

    # ── 查询 ──────────────────────────────────────────────

    @property
    def events(self) -> list[CourseEvent]:
        """当前解析出的课程事件（未展开重复）。"""
        return list(self._events)

    @property
    def errors(self) -> list[str]:
        """解析失败的文件与原因。"""
        return list(self._errors)

    @property
    def file_count(self) -> int:
        """ics 目录下的文件数量。"""
        return len(self._fingerprint)

    @property
    def last_loaded_at(self) -> datetime | None:
        """上次实际重新解析的时间。"""
        return self._loaded_at

    # ── 网址来源映射（自动刷新用） ─────────────────────────

    def _sources_path(self) -> Path:
        return self.ics_dir / SOURCES_FILENAME

    def _load_url_sources(self) -> None:
        """读取来源映射；文件缺失或损坏时按空处理（自动刷新退化成不刷新）。"""
        try:
            raw = json.loads(self._sources_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        sources: dict[str, dict[str, str]] = {}
        for filename, info in raw.items():
            if not isinstance(filename, str) or not isinstance(info, dict):
                continue
            url = str(info.get("url") or "").strip()
            if url:
                sources[filename] = {
                    "url": url,
                    "fingerprint": str(info.get("fingerprint") or ""),
                    "updated_at": str(info.get("updated_at") or ""),
                }
        self._url_sources = sources

    def _save_url_sources(self) -> None:
        try:
            self.ics_dir.mkdir(parents=True, exist_ok=True)
            _write_atomic(
                self._sources_path(),
                json.dumps(self._url_sources, ensure_ascii=False, indent=2),
            )
        except OSError:
            # 映射写不出去只影响自动刷新，不该让导入失败
            pass

    def record_url_source(self, filename: str, url: str, fingerprint: str = "") -> None:
        """记录/更新一个网址导入文件的来源与内容指纹。"""
        name = Path(str(filename or "")).name
        if not name or not url:
            return
        self._url_sources[name] = {
            "url": str(url).strip(),
            "fingerprint": str(fingerprint or ""),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._save_url_sources()

    def url_sources(self) -> dict[str, dict[str, str]]:
        """全部网址来源映射（文件名 → 网址/指纹），供自动刷新使用。"""
        return dict(self._url_sources)

    # ── 导入 ──────────────────────────────────────────────

    def save_ics(
        self, text: str, filename: str, *, source_url: str | None = None
    ) -> ImportResult:
        """把 ICS 文本落盘，返回导入结果。

        - 先校验能解析出事件再写文件，避免把坏文件留在目录里让后续每次扫描都报错；
        - 同名文件会被**覆盖**：这是刻意的，因为调用方传的是由网址推导出的稳定
          文件名（见 :func:`import_filename`），重复导入同一网址就应当更新同一份
          课表。否则调课之后旧文件的原始时间还会继续触发提醒；
        - 传 ``source_url`` 时同步记录来源与内容指纹，供自动刷新判断变化
          （自动刷新走同一条路径，坏内容会在这里被拒绝，旧文件不受影响）。
        """
        self.ensure_dir()
        name = safe_filename(filename)
        # 校验能解析出事件再落盘；顺带拿到"有几行被跳过"这类非致命问题
        events, warnings = parse_ics_with_warnings(text, source=name)

        with self._lock:
            target = self.ics_dir / name
            replaced = target.exists()
            _write_atomic(target, text)
            self._refresh_locked(force=True)
            # 只报**这份文件**的问题：整个目录的错误列表里可能有别的坏文件，
            # 归到这次导入头上会让用户以为刚导入的课表有问题
            for item in self._errors:
                if item.startswith(f"{name}:"):
                    warnings.append(item)
            if source_url:
                self.record_url_source(name, source_url, content_fingerprint(text))
            return ImportResult(target, len(events), warnings, replaced=replaced)

    def delete_ics(self, filename: str) -> bool:
        """删除一个 ics 文件，返回是否删除成功。"""
        name = Path(str(filename or "")).name
        if not name or not name.lower().endswith(".ics"):
            return False
        path = self.ics_dir / name
        try:
            path.unlink()
        except OSError:
            return False
        with self._lock:
            self._refresh_locked(force=True)
        if self._url_sources.pop(name, None) is not None:
            self._save_url_sources()  # 文件没了，来源映射也该清掉
        return True


def _write_atomic(path: Path, text: str) -> None:
    """原子写入（先写临时文件再替换），避免导入中途失败留下半份课表。"""
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
