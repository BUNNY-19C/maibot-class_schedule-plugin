"""Inbox 异步处理管道：队列 + worker，落库笔记（原始层）。

阶段 2' 的职责边界：解析归一化在 inbox，**这里只做持久化**（add_note + 标签 +
状态回写）；公式识别、结构化整理在阶段 3'/4' 接进同一个 worker。

状态回写是节流持久化：worker 改内存状态，只有 flush 点（tick/卸载）落盘，
避免每条笔记一次整文件重写。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from .constants import LOG_PREFIX
from .inbox import ParsedMessage, normalize_image_segment  # noqa: F401  (供上层判型)


class StudyPipeline:
    def __init__(
        self,
        *,
        db: Any,
        worker_count: int = 1,
        save_image: Callable[[str, bytes, str], str] | None = None,
        persist_interval_seconds: int = 10,
    ) -> None:
        self._db = db
        self._worker_count = max(1, int(worker_count))
        self._save_image = save_image
        self._started = False
        self._persist_interval = max(0, int(persist_interval_seconds))
        self._last_persist = 0.0

    # ── 生命周期 ──────────────────────────────────────────

    def start(self) -> None:
        # 阶段 2' 的落库是直连的（没有长任务），这里只置运行标记；
        # 真正的 worker 队列在阶段 3'（VLM 识别）引入。
        self._started = True

    async def stop(self) -> None:
        self._started = False

    @property
    def running(self) -> bool:
        return self._started

    # ── 入队与消费 ────────────────────────────────────────

    async def process(
        self,
        parsed: ParsedMessage,
        *,
        course: str,
        week: int | None = None,
        period: str = "",
        source_type: str = "文字",
    ) -> int:
        """同步落一条笔记到 notes_db（阶段 2' 无重活，直接处理即可）。"""
        raw = (parsed.text or "").strip()
        image_rel = ""
        if parsed.images and self._save_image is not None:
            data, suffix = parsed.images[0]
            if data:
                image_rel = self._save_image(course, data, suffix)
        note_id = await asyncio.to_thread(
            self._db.add_note,
            source_type=source_type,
            raw_content=raw[:8000],
            image_path=image_rel,
            course=course,
            week=week,
            period=period,
            message_id=parsed.message_id,
            timestamp=parsed.timestamp,
        )
        await asyncio.to_thread(self._db.attach_tag, "note", note_id, f"#{course}")
        await asyncio.to_thread(self._db.attach_tag, "note", note_id, f"#{source_type}")
        return note_id
