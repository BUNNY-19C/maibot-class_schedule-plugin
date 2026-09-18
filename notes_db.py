"""笔记数据库（SQLite）：Note Inbox、公式库、标签、知识点、向量。

设计决策（与 README 的"原始层不动"原则一致）：

- **schema 版本记在 meta 表**，启动时 ``CREATE TABLE IF NOT EXISTS``。不做全量
  迁移脚本：个人笔记库没有运维场景，字段演进用 ``ALTER`` + 兜底即可；
- **FTS5 运行时探测**：部分 Python 的 sqlite3 编译不带 FTS5。缺失时全文检索
  自动降级为 LIKE——弱一点，但功能不坏；
- **向量存 BLOB**：个人量级（几千条 × 4KB ≈ 十几 MB）下，Python 余弦暴力扫描
  毫秒级完成，不引入向量数据库依赖；
- 原始层（notes.raw_content / note_sources / formulas.latex_raw / 图片文件）
  只增不改；整理层（structured_json、标签、状态）可重新生成。
"""

from __future__ import annotations

import math
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  course TEXT NOT NULL DEFAULT '未分类',
  week INTEGER,
  period TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL DEFAULT '文字',
  raw_content TEXT NOT NULL DEFAULT '',
  image_path TEXT NOT NULL DEFAULT '',
  structured_json TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT '待整理',
  confidence REAL NOT NULL DEFAULT 1.0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS note_sources(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  message_id TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL DEFAULT '',
  timestamp TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tags(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  parent_id INTEGER REFERENCES tags(id),
  aliases TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS note_tags(
  note_id INTEGER NOT NULL,
  tag_id INTEGER NOT NULL,
  confidence REAL NOT NULL DEFAULT 1.0,
  source TEXT NOT NULL DEFAULT '规则',
  PRIMARY KEY (note_id, tag_id)
);
CREATE TABLE IF NOT EXISTS formulas(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  latex_raw TEXT NOT NULL DEFAULT '',
  latex_normalized TEXT NOT NULL DEFAULT '',
  fingerprint TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL DEFAULT '未知公式',
  aliases TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '',
  subcategory TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  confidence REAL NOT NULL DEFAULT 0.0,
  image_hash TEXT NOT NULL DEFAULT '',
  course TEXT NOT NULL DEFAULT '',
  week INTEGER,
  period TEXT NOT NULL DEFAULT '',
  source_message_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_formulas_image_hash ON formulas(image_hash);
CREATE TABLE IF NOT EXISTS formula_tags(
  formula_id INTEGER NOT NULL,
  tag_id INTEGER NOT NULL,
  confidence REAL NOT NULL DEFAULT 1.0,
  source TEXT NOT NULL DEFAULT 'LLM',
  PRIMARY KEY (formula_id, tag_id)
);
CREATE TABLE IF NOT EXISTS knowledge_points(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  course TEXT NOT NULL DEFAULT '',
  aliases TEXT NOT NULL DEFAULT '',
  UNIQUE (name, course)
);
CREATE TABLE IF NOT EXISTS note_knowledge_points(
  note_id INTEGER NOT NULL,
  knowledge_point_id INTEGER NOT NULL,
  confidence REAL NOT NULL DEFAULT 1.0,
  PRIMARY KEY (note_id, knowledge_point_id)
);
CREATE TABLE IF NOT EXISTS embeddings(
  note_id INTEGER PRIMARY KEY,
  model TEXT NOT NULL DEFAULT '',
  vector BLOB NOT NULL,
  dims INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
"""

#: FTS5 虚拟表（standalone 模式，由本层手动同步插入，避免触发器的兼容性风险）
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts
  USING fts5(note_id UNINDEXED, content, tags, formula_names);
CREATE VIRTUAL TABLE IF NOT EXISTS formula_fts
  USING fts5(formula_id UNINDEXED, name, aliases, description, latex_raw);
"""


class NotesDatabase:
    """插件笔记库的薄封装：连接、建表、基础读写、两种检索。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()  # 写入可能来自事件循环与 to_thread 两条线
        self._conn: sqlite3.Connection | None = None
        self.fts_enabled = False

    # ── 连接与建表 ────────────────────────────────────────

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 本类用自己的 threading.Lock 串行化所有读写，允许跨线程使用连接
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def initialize(self) -> None:
        """建表 + FTS 探测。幂等，可在每次加载时调用。"""
        with self._lock:
            conn = self._connection()
            conn.executescript(_SCHEMA)
            try:
                conn.executescript(_FTS_SCHEMA)
                self.fts_enabled = True
            except sqlite3.OperationalError:
                # 该 Python 的 sqlite3 没编 FTS5：全文检索降级 LIKE，不报错
                self.fts_enabled = False
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ── 写入 ──────────────────────────────────────────────

    def add_note(
        self,
        *,
        source_type: str,
        raw_content: str = "",
        image_path: str = "",
        course: str = "未分类",
        week: int | None = None,
        period: str = "",
        message_id: str = "",
        timestamp: str = "",
    ) -> int:
        """Inbox 落一条笔记（原始层）。返回 note id。"""
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            conn = self._connection()
            cursor = conn.execute(
                "INSERT INTO notes(course, week, period, source_type, raw_content,"
                " image_path, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (course, week, period, source_type, raw_content, image_path, now, now),
            )
            note_id = int(cursor.lastrowid or 0)
            conn.execute(
                "INSERT INTO note_sources(note_id, message_id, source_type, timestamp)"
                " VALUES(?,?,?,?)",
                (note_id, message_id, source_type, timestamp or now),
            )
            if self.fts_enabled:
                conn.execute(
                    "INSERT INTO notes_fts(note_id, content, tags, formula_names)"
                    " VALUES(?,?,?,?)",
                    (note_id, f"{raw_content} {image_path}", "", ""),
                )
            conn.commit()
            return note_id

    def upsert_formula(self, fields: dict[str, Any]) -> int:
        """按 fingerprint 落公式：同指纹不重复建（hash 缓存的落库形态）。"""
        fingerprint = str(fields.get("fingerprint") or "").strip()
        if not fingerprint:
            raise ValueError("公式缺少 fingerprint")
        now = datetime.now().isoformat(timespec="seconds")
        columns = (
            "latex_raw", "latex_normalized", "name", "aliases", "category",
            "subcategory", "description", "confidence", "image_hash", "course",
            "week", "period", "source_message_id",
        )
        with self._lock:
            conn = self._connection()
            existing = conn.execute(
                "SELECT id FROM formulas WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if existing is not None:
                return int(existing["id"])
            values = [fields.get(col, None if col == "week" else "") for col in columns]
            cursor = conn.execute(
                f"INSERT INTO formulas(fingerprint, created_at, {', '.join(columns)})"
                f" VALUES(?, ?, {', '.join('?' for _ in columns)})",
                (fingerprint, now, *values),
            )
            formula_id = int(cursor.lastrowid or 0)
            if self.fts_enabled:
                conn.execute(
                    "INSERT INTO formula_fts(formula_id, name, aliases, description, latex_raw)"
                    " VALUES(?,?,?,?,?)",
                    (
                        formula_id,
                        str(fields.get("name") or ""),
                        str(fields.get("aliases") or ""),
                        str(fields.get("description") or ""),
                        str(fields.get("latex_raw") or ""),
                    ),
                )
            conn.commit()
            return formula_id

    def formula_by_fingerprint(self, fingerprint: str) -> sqlite3.Row | None:
        """按指纹取公式（判断"这条是不是新公式"，决定要不要回执给用户）。"""
        value = str(fingerprint or "").strip()
        if not value:
            return None
        with self._lock:
            return self._connection().execute(
                "SELECT * FROM formulas WHERE fingerprint = ?", (value,)
            ).fetchone()

    def formula_by_image_hash(self, digest: str) -> sqlite3.Row | None:
        """按图片 hash 取公式——识别缓存：同一张图不再调模型、不再计费。

        取最近一条：同一张图理论上对应同一指纹，但用户可能先用降级模型识别过、
        后来配置好了又识别一次，此时返回新的那条更符合预期。
        """
        value = str(digest or "").strip()
        if not value:
            return None
        with self._lock:
            return self._connection().execute(
                "SELECT * FROM formulas WHERE image_hash = ? ORDER BY id DESC LIMIT 1",
                (value,),
            ).fetchone()

    def formula_count(self) -> int:
        """公式总数（/笔记库 展示用）。"""
        with self._lock:
            row = self._connection().execute(
                "SELECT COUNT(*) c FROM formulas"
            ).fetchone()
        return int(row["c"]) if row is not None else 0

    def formula_tags_for(self, formula_id: int) -> list[str]:
        """某个公式的标签名列表（按置信度降序）。"""
        with self._lock:
            rows = self._connection().execute(
                "SELECT t.name FROM formula_tags ft JOIN tags t ON t.id = ft.tag_id"
                " WHERE ft.formula_id = ? ORDER BY ft.confidence DESC",
                (int(formula_id),),
            ).fetchall()
        return [str(row["name"]) for row in rows]

    def attach_tag(
        self,
        table: str,
        owner_id: int,
        name: str,
        *,
        confidence: float = 1.0,
        source: str = "规则",
    ) -> None:
        """给笔记/公式打标签；标签不存在则创建。别名合并由上层维护表驱动。"""
        if table not in ("note", "formula"):
            raise ValueError(f"未知标签宿主类型: {table}")
        name = str(name or "").strip()
        if not name:
            return
        with self._lock:
            conn = self._connection()
            row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
            if row is None:
                cursor = conn.execute("INSERT INTO tags(name) VALUES(?)", (name,))
                tag_id = int(cursor.lastrowid or 0)
            else:
                tag_id = int(row["id"])
            link = "note_tags" if table == "note" else "formula_tags"
            key = "note_id" if table == "note" else "formula_id"
            conn.execute(
                f"INSERT OR REPLACE INTO {link}({key}, tag_id, confidence, source)"
                " VALUES(?,?,?,?)",
                (owner_id, tag_id, confidence, source),
            )
            conn.commit()

    def store_embedding(self, note_id: int, model: str, vector: list[float]) -> None:
        """向量存 BLOB：个人量级下检索直接内存余弦，不需要向量数据库。"""
        import array

        blob = array.array("f", [float(x) for x in vector])
        with self._lock:
            conn = self._connection()
            conn.execute(
                "INSERT OR REPLACE INTO embeddings(note_id, model, vector, dims, created_at)"
                " VALUES(?,?,?,?,?)",
                (
                    note_id,
                    model,
                    blob.tobytes(),
                    len(vector),
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            conn.commit()

    # ── 检索 ──────────────────────────────────────────────

    def search_text(
        self,
        query: str,
        *,
        course: str = "",
        source_type: str = "",
        limit: int = 10,
    ) -> list[sqlite3.Row]:
        """全文/关键词检索笔记。FTS5 可用走 MATCH，否则 LIKE 降级。"""
        query = str(query or "").strip()
        if not query:
            return []
        filters: list[str] = []
        args: list[Any] = []
        if course:
            filters.append("n.course = ?")
            args.append(course)
        if source_type:
            filters.append("n.source_type = ?")
            args.append(source_type)
        where = (" AND " + " AND ".join(filters)) if filters else ""
        with self._lock:
            conn = self._connection()
            if self.fts_enabled:
                try:
                    rows = list(
                        conn.execute(
                            "SELECT n.* FROM notes_fts f JOIN notes n ON n.id = f.note_id"
                            " WHERE notes_fts MATCH ?" + where
                            + " ORDER BY rank LIMIT ?",
                            (f'"{query}"', *args, limit),
                        )
                    )
                    if rows:
                        return rows
                    # FTS5 的 unicode61 分词对中文不友好（整段 CJK 是一个 token，
                    # 短查询匹配不上）：空结果时回退 LIKE，而不是让用户搜不到
                except sqlite3.OperationalError:
                    pass  # 查询语法不被接受时退回 LIKE，不让检索整体失败
            pattern = f"%{query}%"
            return list(
                conn.execute(
                    "SELECT * FROM notes n WHERE (raw_content LIKE ? OR image_path LIKE ?)"
                    + where + " ORDER BY id DESC LIMIT ?",
                    (pattern, pattern, *args, limit),
                )
            )

    def search_formulas(self, query: str, *, limit: int = 10) -> list[sqlite3.Row]:
        """公式检索：名称/别名/描述/LaTeX 原文。"""
        query = str(query or "").strip()
        if not query:
            return []
        with self._lock:
            conn = self._connection()
            if self.fts_enabled:
                try:
                    rows = list(
                        conn.execute(
                            "SELECT f.* FROM formula_fts x JOIN formulas f ON f.id = x.formula_id"
                            " WHERE formula_fts MATCH ? ORDER BY rank LIMIT ?",
                            (f'"{query}"', limit),
                        )
                    )
                    if rows:
                        return rows  # 中文分词问题同 search_text：空结果退回 LIKE
                except sqlite3.OperationalError:
                    pass
            pattern = f"%{query}%"
            return list(
                conn.execute(
                    "SELECT * FROM formulas WHERE name LIKE ? OR aliases LIKE ?"
                    " OR description LIKE ? OR latex_raw LIKE ? ORDER BY id DESC LIMIT ?",
                    (pattern, pattern, pattern, pattern, limit),
                )
            )

    def get_note(self, note_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._connection().execute(
                "SELECT * FROM notes WHERE id = ?", (int(note_id),)
            ).fetchone()

    def tags_for(self, note_id: int) -> list[str]:
        """某条笔记的标签名列表（按置信度降序）。"""
        with self._lock:
            rows = self._connection().execute(
                "SELECT t.name FROM note_tags nt JOIN tags t ON t.id = nt.tag_id"
                " WHERE nt.note_id = ? ORDER BY nt.confidence DESC",
                (int(note_id),),
            ).fetchall()
        return [str(row["name"]) for row in rows]

    def status_counts(self) -> dict[str, int]:
        """按状态统计笔记条数（/笔记库 用）。"""
        with self._lock:
            rows = self._connection().execute(
                "SELECT status, COUNT(*) c FROM notes GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["c"]) for row in rows}

    def all_embeddings(self) -> list[tuple[int, list[float]]]:
        """全部向量（语义召回用；几千条量级直接内存算）。"""
        import array

        with self._lock:
            rows = self._connection().execute(
                "SELECT note_id, vector FROM embeddings"
            ).fetchall()
        result: list[tuple[int, list[float]]] = []
        for row in rows:
            data = array.array("f")
            data.frombytes(row["vector"])
            result.append((int(row["note_id"]), list(data)))
        return result


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度。维度不等（换了 embedding 模型）按重叠部分算并容忍。"""
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    dot = sum(a[i] * b[i] for i in range(n))
    norm_a = math.sqrt(sum(x * x for x in a[:n]))
    norm_b = math.sqrt(sum(x * x for x in b[:n]))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
