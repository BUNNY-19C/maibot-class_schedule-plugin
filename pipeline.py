"""Inbox 异步处理管道：队列 + worker，落库笔记（原始层）并异步识别公式。

职责边界（与阶段 2' 一致，只多了一层）：

- **落库是同步的**（``process`` 里直接 add_note）——笔记原文必须立刻进库，
  不能等队列：用户发完消息就该能立刻检索到，worker 里失败也不该丢原文；
- **重活进队列**（公式识别要调 VLM，秒级到几十秒）：``process`` 落完库就
  把识别任务塞进 ``asyncio.Queue``，消息链路立刻返回。队列满了**丢弃并计数**，
  绝不阻塞入站消息——识别是增强层，丢一条识别远好过卡住聊天；
- **worker 不碰事件循环**：所有 sqlite/HTTP 调用都在 ``asyncio.to_thread`` 里，
  且**不在持锁处 await**（重建前踩过自调用死锁），所以 worker 内部可以安全地
  调回本管道/数据库；
- **停机丢的是识别、不是笔记**：``stop`` 取消 worker，队列里未处理的识别任务
  随卸载消失（下次同样内容会重新入队，指纹去重保证不会存两条）。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any, Awaitable, Callable

from .constants import LOG_PREFIX
from .inbox import ParsedMessage

logger = logging.getLogger(__name__)

#: 与 config.study.queue_size 默认值一致；外部没传时用这个
DEFAULT_QUEUE_SIZE = 200

#: 日志里给识别状态用的中文名（日志是异步链路唯一的现场）
_STATUS_LABELS = {
    "recognized": "完成",
    "cached": "命中图片缓存",
    "no_formula": "图里没有公式",
    "failed": "失败",
}


class StudyPipeline:
    def __init__(
        self,
        *,
        db: Any,
        worker_count: int = 1,
        recognizer: Any | None = None,
        on_recognized: Callable[[dict[str, Any], dict[str, Any]], Awaitable[None]] | None = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._db = db
        self._worker_count = max(1, int(worker_count))
        self._started = False
        self._recognizer = recognizer
        self._on_recognized = on_recognized
        self._queue_size = max(1, int(queue_size))
        self._queue: asyncio.Queue[dict[str, Any] | None] | None = None
        self._workers: list[asyncio.Task[None]] = []
        #: 计数：入队/识别完成/被丢弃。丢弃数是"队列太小"的唯一证据
        self.enqueued = 0
        self.completed = 0
        self.dropped = 0
        self.errors: list[str] = []

    # ── 生命周期 ──────────────────────────────────────────

    @property
    def recognizer(self) -> Any | None:
        return self._recognizer

    def set_recognizer(self, recognizer: Any | None) -> None:
        """换掉识别器（配置热更新用）。

        worker 是**每个任务现读** ``self._recognizer`` 的，所以这里换掉之后，
        队列里排着的与新来的任务都会用新的识别器；置 ``None`` 等于立刻停止识别。
        """
        self._recognizer = recognizer

    def start(self) -> None:
        """置运行标记并起 worker。必须在事件循环内调用（on_load 是 async）。"""
        self._started = True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # 没有事件循环（纯同步调用/单测直连落库）：不做识别，落库照常
            return
        self._spawn_workers()

    def _spawn_workers(self) -> None:
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self._queue_size)
        for index in range(self._worker_count - len(self._workers)):
            task = asyncio.create_task(
                self._worker(index), name=f"class-schedule-inbox-worker-{index}"
            )
            self._workers.append(task)

    async def stop(self) -> None:
        """停机：取消 worker 并等它们退出（未处理完的识别任务随之丢弃）。"""
        self._started = False
        workers, self._workers = self._workers, []
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._queue = None

    @property
    def running(self) -> bool:
        return self._started

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize() if self._queue is not None else 0

    # ── 入队与消费 ────────────────────────────────────────

    async def process(
        self,
        parsed: ParsedMessage,
        *,
        course: str,
        week: int | None = None,
        period: str = "",
        source_type: str = "文字",
        kind: str = "",
        stream_id: str = "",
        note_refs: list[str] | None = None,
        image_path: str = "",
    ) -> int:
        """落一条笔记（同步）+ 把公式识别排进队列（异步）。返回 note id。

        ``note_refs`` 是 markdown 层每条笔记的 id（与 ``parsed.images`` 同序）：
        识别完成后公式要回填到那一条，用户才在 /笔记、/找 里看得到公式本身。

        ``image_path`` 是原图在笔记目录里的相对路径——由 markdown 层提供。
        这里**不再自己存图**：同一张图存两份曾是重复构造，还会制造索引没引用的
        孤儿副本（线上清理过一次才知道心疼）。
        """
        raw = (parsed.text or "").strip()
        image_rel = str(image_path or "")
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
        if kind:
            await asyncio.to_thread(self._db.attach_tag, "note", note_id, f"#{kind}")
        # 识别在队列里做：每一张图一条任务（落库只留第一张的路径，识别要全覆盖）
        refs = list(note_refs or [])
        for index, (data, suffix) in enumerate(parsed.images):
            if data:
                self.enqueue(
                    {
                        "note_id": note_id,
                        "note_ref": refs[index] if index < len(refs) else "",
                        "image": data,
                        "suffix": suffix or ".png",
                        "course": course,
                        "week": week,
                        "period": period,
                        "message_id": parsed.message_id,
                        "stream_id": stream_id or parsed.message_id,
                        "kind": kind,
                    }
                )
        return note_id

    def enqueue(self, job: dict[str, Any]) -> bool:
        """非阻塞入队。返回是否入队成功；满则丢弃并计数（绝不阻塞消息链路）。"""
        queue = self._queue
        if not self._started or queue is None or self._recognizer is None:
            return False
        try:
            queue.put_nowait(job)
        except asyncio.QueueFull:
            self.dropped += 1
            return False
        self.enqueued += 1
        return True

    async def drain(self) -> None:
        """等队列里所有已入队任务被 worker 消费完（补识别分批续跑用）。

        worker 每处理完一条就 ``task_done``；取消本协程即可中断等待（停机时
        由调用方取消整个补识别任务）。
        """
        if self._queue is not None:
            await self._queue.join()

    async def _worker(self, index: int) -> None:
        """worker 主循环：一条任务失败不能带走整个 worker。"""
        queue = self._queue
        if queue is None:
            return
        while True:
            job = await queue.get()
            try:
                if job is None:  # 停机令牌（当前不主动投，保留给优雅停机）
                    return
                await self._run_inbox_item(job)
                self.completed += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 记下原因继续下一条：识别失败只是少一条公式，worker 必须活着
                self.errors.append(str(exc)[:200])
                if len(self.errors) > 50:
                    del self.errors[:-50]
            finally:
                with suppress(ValueError):
                    queue.task_done()

    async def _run_inbox_item(self, job: dict[str, Any]) -> None:
        """worker 的活：图片 → 公式识别 → 落公式库 → 回调通知用户。"""
        recognizer = self._recognizer
        if recognizer is None or not job.get("image"):
            return
        result = await recognizer.recognize(
            job["image"],
            suffix=str(job.get("suffix") or ".png"),
            course=str(job.get("course") or ""),
            week=job.get("week"),
            period=str(job.get("period") or ""),
            message_id=str(job.get("message_id") or ""),
        )
        # 每条结果都留一行日志：识别是异步的，没有日志就只剩"用户觉得没反应"
        # （线上实测：缓存命中的图被静默处理，排查时无从下手）
        names = "、".join(
            str(item.get("name") or "?") for item in result.get("formulas") or []
        )
        logger.info(
            f"{LOG_PREFIX} 识别{_STATUS_LABELS.get(str(result.get('status')), '结束')}："
            f"{names or '未认出'}｜课程 {job.get('course') or '未分类'}"
            f"｜note={job.get('note_id')}"
            + (f"｜{result.get('error')}" if result.get("status") == "failed" else "")
        )
        hook = self._on_recognized
        if hook is not None:
            # 失败也回调：用户主动发的图静默失败，比认错更让人摸不着头脑
            await hook(job, result)
