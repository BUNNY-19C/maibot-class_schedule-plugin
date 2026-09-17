"""课程表提醒插件（MaiBot Plugin SDK）。

功能：
- 从本地 ics 目录 / http(s) 网址导入课表，支持 RRULE 重复与 EXDATE 调休；
- 每个提醒会话可以有自己的提前量（``/课表提前``），未设置时跟随配置默认值；
- 群聊与私聊都能登记为提醒对象（在目标会话里发 ``/课表订阅`` 即可）；
- 支持访问白/黑名单，限制哪些会话能用命令、哪些会话收得到提醒。

生命周期要点：提醒循环是 ``on_load`` 里创建的 asyncio 任务，``on_unload``
必须取消并等待它结束，否则重载插件会留下多个循环重复推送。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

from maibot_sdk import Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import (
    ErrorPolicy,
    HookMode,
    HookOrder,
    ToolParameterInfo,
    ToolParamType,
)

from .access import (
    AccessDecision,
    ChatIdentity,
    evaluate_access,
    evaluate_chat_scope,
    extract_session_id,
    identity_from_kwargs,
    parse_platform_target,
)
from .config_model import ClassScheduleConfig
from .constants import DEFAULT_ICS_DIR, LOG_PREFIX
from .course_query import (
    build_inject_text,
    collect_item_text,
    inject_into_items,
    looks_like_schedule_question,
)
from .course_source import CourseRepository, import_filename
from .file_intake import (
    FileIntakeError,
    chat_import_filename,
    content_fingerprint,
    extract_file_candidates,
    is_schedule_filename,
    load_candidate_text,
)
from .holidays import (
    HolidayCalendar,
    HolidayNotPublishedError,
    cache_path,
    is_stale,
    render_url_template,
    write_cache,
)
from .ics_parser import CourseEvent, expand_occurrences
from .netutil import FetchError, UnsafeUrlError, fetch_ics, fetch_text
from .reminder import collect_due, one_line, render_message, upcoming_events
from .store import MAX_LEAD_MINUTES, PluginState, Subscription

logger = logging.getLogger(__name__)

STATE_FILENAME = "state.json"
PRUNE_INTERVAL_SECONDS = 3600
MAX_LIST_LINES = 30
#: /课表状态 里最多列出多少个提醒会话
MAX_STATUS_SUBSCRIPTIONS = 10
#: 节假日数据的子目录（相对插件数据目录）
HOLIDAY_DIR = "holidays"
#: 需要提前准备好数据的年份跨度：今年 + 明年（跨年学期要用到）
HOLIDAY_YEAR_SPAN = 2
#: 配置里的纯数字目标按"用户号"处理（MaiBot 的会话 ID 是 32 位十六进制，
#: 不可能是纯数字），这样用户把 QQ 号填进 target_streams 也能正常工作
_USER_ID_RE = re.compile(r"^\d{5,}$")
#: 记住最近导入过的聊天文件指纹，避免同一份文件重复发送被反复导入
MAX_IMPORT_FINGERPRINTS = 50
#: 提醒类内容用的 reason 标识（决定走哪个发送方式）
REMINDER_REASON = "class_reminder"
#: proactive 兜底前查询"bot 是否已发言"的超时（秒）。
#: 这个查询在提醒循环里同步等待，卡住会拖慢整轮检查，所以必须有上限。
PROACTIVE_QUERY_TIMEOUT_SECONDS = 10.0
#: 记住多少个会话的私聊/群聊类型（给只能拿到 session_id 的注入 hook 用）。
#: 只是缓存，超出上限丢最旧的，丢了退化成"类型未知"。
MAX_REMEMBERED_STREAMS = 500
#: 节假日**下载失败**后的重试间隔（秒）。与 refresh_hours（数据过期周期）是两回事：
#: 失败可能下一分钟就好了，而数据本身 12 小时都不会变。
HOLIDAY_FAILURE_RETRY_SECONDS = 1800


@dataclass(frozen=True)
class OutgoingMessage:
    """一条要发出去的内容：用户文案与模型指令分开写。

    直发模式发 ``fixed``，persona 模式把 ``facts`` 交给 replyer —— 两者不能共用
    一段文字，否则模型指令（"请告诉 TA…"）会被原样发给用户。
    """

    fixed: str
    facts: str

_WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: 命令处理器签名：接收 kwargs，返回 SDK 要求的三元组
CommandResult = tuple[bool, str, int]
CommandHandler = Callable[..., Awaitable[CommandResult]]


def _requires_access(func: CommandHandler) -> CommandHandler:
    """命令级准入装饰器：名单外的会话直接拒绝，不执行命令体。

    用 ``functools.wraps`` 包一层，组件信息会随 ``__wrapped__`` 的 ``__dict__``
    一起复制到 wrapper 上，所以 ``@Command`` 放在外层能被 SDK 正常收集；
    同时 ``__name__`` 保持不变，Runner 按 ``handler_name`` 也能取到这个方法。
    """

    @functools.wraps(func)
    async def wrapper(self: "ClassSchedulePlugin", **kwargs: Any) -> CommandResult:
        denied = self._access_denial(kwargs)
        if denied is not None:
            await self._reply(str(kwargs.get("stream_id", "")), denied)
            return False, denied, 0
        return await func(self, **kwargs)

    return wrapper


class ClassSchedulePlugin(MaiBotPlugin):
    """课程表 ICS 导入 + 上课前提醒。"""

    config_model = ClassScheduleConfig

    #: 宿主人设变了要重新读（persona 文案依赖它）
    config_reload_subscriptions: ClassVar[Iterable[str]] = ("bot",)

    def __init__(self) -> None:
        super().__init__()
        self._data_dir: Path | None = None
        self._repo: CourseRepository | None = None
        self._state = PluginState()
        self._stop: asyncio.Event | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._tick_lock = asyncio.Lock()
        self._last_prune: datetime | None = None
        self._warned_no_target = False
        self._warned_all_filtered = False
        self._warned_skipped_groups = False
        self._warned_holiday_source = False
        self._holidays: HolidayCalendar | None = None
        self._holiday_task: asyncio.Task[None] | None = None
        self._holiday_lock = asyncio.Lock()
        #: 网址课表的自动刷新（单飞后台任务，绝不阻塞 tick）
        self._url_refresh_task: asyncio.Task[None] | None = None
        self._url_lock = asyncio.Lock()
        self._last_url_refresh: datetime | None = None
        self._url_refresh_warned: dict[str, None] = {}
        #: 已确认"放假安排尚未公布"的年份 → 记录时间，避免反复重试与刷屏
        self._holiday_unpublished: dict[int, datetime] = {}
        #: 下载失败的年份 → 记录时间，按短周期重试（见 HOLIDAY_FAILURE_RETRY_SECONDS）
        self._holiday_failed: dict[int, datetime] = {}
        #: 配置里的用户号 → 解析出的会话 ID
        self._resolved_targets: dict[str, str] = {}
        #: 会话 ID → "private"/"group"（供只能拿到 session_id 的 hook 判断适用范围）
        self._stream_types: dict[str, str] = {}
        #: 最近导入过的聊天文件内容指纹（进程内去重）
        self._imported_fingerprints: list[str] = []
        #: 正在进行的文件导入任务（卸载时要取消）
        self._intake_tasks: set[asyncio.Task[None]] = set()
        #: /课程解析 之后正在等课表文件的会话 → 截止时间
        self._awaiting_ics: dict[str, datetime] = {}
        #: 宿主人设（persona 模式用；插件不自带口吻）
        self._persona_nickname = ""
        self._persona_body = ""
        self._persona_reply_style = ""
        #: bot 自己的账号（platform → account），用于验证它是否真的开口了
        self._bot_accounts: dict[str, str] = {}
        #: proactive 模式已入队、等待验证的发言
        self._pending_proactive: list[dict[str, Any]] = []
        #: 人设是否已成功读取过（宿主配置可能晚于插件就绪）
        self._persona_loaded = False
        self._warned_unresolved_targets: set[str] = set()
        self._last_holiday_refresh: datetime | None = None

    # ── 配置访问 ──────────────────────────────────────────

    def _conf(self) -> ClassScheduleConfig:
        """取强类型配置；SDK 尚未注入时回退到默认值（便于单测/降级）。"""
        try:
            return self.config  # type: ignore[return-value]
        except RuntimeError:
            return ClassScheduleConfig()

    def _default_lead_minutes(self) -> int:
        """未单独设置的会话使用的提前分钟数。"""
        return max(0, int(self._conf().reminder.remind_before_minutes))

    def _state_path(self) -> Path:
        """状态文件路径（提醒会话、各会话提前量、已提醒记录）。"""
        base = self._data_dir or Path("data") / "plugins" / "github.BUNNY-19C.class-schedule"
        return base / STATE_FILENAME

    def _ics_dir_name(self) -> str:
        """把配置里的目录名收敛成单一目录名，避免 ``..`` 逃出数据目录。"""
        raw = str(self._conf().source.ics_dir or DEFAULT_ICS_DIR).replace("\\", "/")
        name = raw.strip("/").split("/")[-1].strip()
        if name in ("", ".", ".."):
            return DEFAULT_ICS_DIR
        return name

    # ── 访问名单 ──────────────────────────────────────────

    def _evaluate(self, identity: Any) -> AccessDecision:
        """按当前配置的名单模式判定某个会话身份。"""
        conf = self._conf()
        return evaluate_access(
            identity, mode=conf.access.mode, entries=conf.access.entries
        )

    def _scope_denial(self, identity: Any, *, unknown_allows: bool = False) -> str | None:
        """适用范围（私聊/群聊）检查：返回拒绝原因，允许时返回 ``None``。

        入站动作用默认的 ``unknown_allows=False``（认不出类型就拒绝），
        出站提醒传 ``True``（认不出就照发，别静默停掉提醒）。
        """
        decision = evaluate_chat_scope(
            identity,
            scope=self._conf().access.chat_scope,
            unknown_allows=unknown_allows,
        )
        return None if decision.allowed else decision.reason

    def _remember_chat_type(self, identity: ChatIdentity) -> None:
        """记住某个会话是私聊还是群聊。

        为什么需要这张表：``maisaka.planner.before_request`` 只给 ``session_id``、
        不给消息体，靠它自己是判断不出私聊/群聊的。而
        ``chat.receive.after_process`` 拿得到完整 message，且在规划器之前触发，
        所以在这里记下来给注入用。表有上限，只当缓存，丢了顶多退化成"类型未知"。
        """
        stream_id = str(identity.stream_id or "").strip()
        if not stream_id or identity.chat_type not in ("private", "group"):
            return
        if self._stream_types.get(stream_id) == identity.chat_type:
            return
        self._stream_types[stream_id] = identity.chat_type
        while len(self._stream_types) > MAX_REMEMBERED_STREAMS:
            self._stream_types.pop(next(iter(self._stream_types)))

    def _identity_for_stream(self, stream_id: str) -> ChatIdentity:
        """按已记住的类型构造身份；没记过则 ``chat_type`` 为空（类型未知）。"""
        return ChatIdentity(
            stream_id=stream_id,
            chat_type=self._stream_types.get(str(stream_id or "").strip(), ""),
        )

    def _access_denial(self, kwargs: dict[str, Any]) -> str | None:
        """命令准入检查：返回拒绝文案，允许时返回 ``None``。

        先看适用范围再看名单：范围是"这个插件在哪些类型的会话里工作"，
        与名单（"谁可以用"）是两件事，所以不受 ``apply_to_commands`` 影响。
        """
        conf = self._conf()
        identity = identity_from_kwargs(kwargs)
        scope_reason = self._scope_denial(identity)
        if scope_reason is not None:
            self.ctx.logger.info(
                f"{LOG_PREFIX} 拒绝命令：{scope_reason}；标识={identity.identifiers or '<无>'}"
            )
            return f"⛔ {scope_reason}。课表功能默认只在私聊可用。"
        if not conf.access.apply_to_commands:
            return None
        decision = self._evaluate(identity)
        if decision.allowed:
            return None
        self.ctx.logger.info(
            f"{LOG_PREFIX} 拒绝命令：{decision.reason}；"
            f"标识={decision.identifiers or '<无>'}"
        )
        return f"⛔ {decision.reason}，无法使用课表功能。"

    # ── 生命周期 ──────────────────────────────────────────

    async def on_load(self) -> None:
        conf = self._conf()

        # 防御：若 Runner 未先调 on_unload 就重新加载，先收拾掉旧循环，
        # 否则会留下两个循环各推一份提醒
        await self._stop_and_cancel_loop()

        self._data_dir = Path(self.ctx.paths.data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._state = PluginState.load(self._state_path())

        ics_dir = self._data_dir / self._ics_dir_name()
        self._repo = CourseRepository(
            ics_dir, cache_seconds=int(conf.source.scan_interval_seconds)
        )
        self._repo.ensure_dir()
        self._repo.refresh(force=True)

        # 读宿主人设与 bot 账号：persona 文案与 proactive 验证都依赖它
        self._bot_accounts = {}
        self._persona_loaded = False
        await self._load_host_persona()

        # 节假日数据要联网，放后台任务里做，不拖慢插件加载
        self._holidays = self._reload_holiday_cache()
        self._holiday_lock = asyncio.Lock()
        self._last_holiday_refresh = None
        if conf.holiday.skip_off_days or conf.holiday.extra_dates:
            self._schedule_holiday_refresh()

        self._stop = asyncio.Event()
        self._tick_lock = asyncio.Lock()
        self._loop_task = asyncio.create_task(
            self._run_loop(), name="class-schedule-reminder-loop"
        )

        self.ctx.logger.info(
            f"{LOG_PREFIX} 已加载：课表 {self._repo.file_count} 个文件 / "
            f"{len(self._repo.events)} 条课程，课表目录 {ics_dir}，"
            f"默认提前 {self._default_lead_minutes()} 分钟，"
            f"提醒会话 {len(self._subscriptions())} 个，"
            f"访问名单 {conf.access.mode}"
        )
        for error in self._repo.errors:
            self.ctx.logger.warning(f"{LOG_PREFIX} 课表解析失败 - {error}")
        self._warn_risky_settings()

    def _warn_risky_settings(self) -> None:
        """启动时把两类"看起来配好了、实际会失效"的组合说出来。

        这两处都不会报错，只会在用户毫无察觉时少干活，所以必须留痕。
        """
        conf = self._conf()
        interval = max(15, int(conf.reminder.check_interval_seconds))
        lead = self._default_lead_minutes()
        grace = int(conf.reminder.late_grace_seconds)
        if interval > lead * 60 + grace:
            self.ctx.logger.warning(
                f"{LOG_PREFIX} 检查间隔（{interval} 秒）比提前量窗口"
                f"（提前 {lead} 分钟 + 补发宽限 {grace} 秒）还长，"
                "为保证不漏提醒会自动放宽补发窗口，提醒可能迟到；"
                "建议把检查间隔调到 60 秒左右"
            )
        if conf.access.mode == "blacklist" and any(
            str(item).strip().isdigit() for item in (conf.access.entries or [])
        ):
            self.ctx.logger.warning(
                f"{LOG_PREFIX} 黑名单里有纯数字条目。这类条目需要群号或用户号才能匹配；"
                "若你的适配器不下发这些字段，条目不会命中（等于没配）。"
                "可在目标会话发 /课表状态 查看本会话可用标识，改用会话 ID"
            )
        if "proactive" in (conf.reply.style, conf.reply.ack_style):
            self.ctx.logger.warning(
                f"{LOG_PREFIX} 已启用 proactive 发送方式。实测（麦麦 1.2.0）交给 replyer "
                "的任务可能只被规划、不真的发言且无报错；插件会在 "
                f"{conf.reply.fallback_after_seconds} 秒后验证并兜底直发。"
                "若不想承担这段延迟，改用 persona（模型生成文案 + 直发）"
            )
        if (
            conf.access.mode != "off"
            and conf.access.tool_query_enabled
        ):
            self.ctx.logger.warning(
                f"{LOG_PREFIX} 访问名单管不住 LLM 工具：工具调用拿不到会话信息，"
                "模型只要被问到就能拿到课表。若机器人也在名单外的群里，"
                "建议把 access.tool_query_enabled 关掉"
            )
        if (
            conf.access.chat_scope == "private"
            and conf.access.tool_query_enabled
        ):
            # 与上一条同源（工具拿不到会话信息），但适用范围是另一个维度：
            # 只走私聊挡得住命令和注入，挡不住模型在群里主动调工具
            self.ctx.logger.warning(
                f"{LOG_PREFIX} access.chat_scope=private 挡不住 LLM 工具："
                "工具调用不带会话信息，模型在群里被问到课表仍能查到。"
                "若机器人也在群里，建议把 access.tool_query_enabled 关掉"
            )

    async def on_unload(self) -> None:
        await self._stop_and_cancel_loop()
        await self._cancel_intake_tasks()
        self._save_state()
        self.ctx.logger.info(f"{LOG_PREFIX} 已卸载")

    async def _cancel_intake_tasks(self) -> None:
        """取消尚未完成的文件导入任务，避免卸载后还在写盘/发消息。"""
        pending = [task for task in self._intake_tasks if not task.done()]
        self._intake_tasks.clear()
        for task in pending:
            task.cancel()
        for task in pending:
            with suppress(asyncio.CancelledError, Exception):
                await task

    async def _stop_and_cancel_loop(self) -> None:
        """通知循环退出并等待任务真正结束。

        等待是必要的：只 ``cancel()`` 而不 await 的话，任务可能还没被调度器
        处理完，紧接着的重新加载就会短暂出现两个循环同时推送。
        """
        if self._stop is not None:
            self._stop.set()

        task, self._loop_task = self._loop_task, None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

        holiday_task, self._holiday_task = self._holiday_task, None
        if holiday_task is not None and not holiday_task.done():
            holiday_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await holiday_task

        url_task, self._url_refresh_task = self._url_refresh_task, None
        if url_task is not None and not url_task.done():
            url_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await url_task

    async def on_config_update(
        self, scope: str, config_data: dict[str, Any], version: str
    ) -> None:
        if scope == "bot":
            # 宿主人设/账号变了：重读，否则 persona 文案会继续用旧人设
            self._bot_accounts = {}
            await self._load_host_persona()
            return
        if scope != "self":
            return
        # 主动应用新配置：提前量/名单/模板/开关都是每轮 tick 现读 self.config，
        # 只有把新配置灌进去热更新才会真正生效（Runner 若已设置过，这里幂等）
        try:
            self.set_plugin_config(dict(config_data or {}))
        except Exception as exc:
            self.ctx.logger.warning(
                f"{LOG_PREFIX} 配置热更新失败，继续沿用旧配置: {exc}"
            )
        if self._repo is not None:
            self._repo.cache_seconds = int(self._conf().source.scan_interval_seconds)
        # 重建成日历来让 extra_dates / exclude_dates 立刻生效，
        # 并清掉节流计时，好让数据源等改动能马上重新拉一次
        self._reload_holiday_cache()
        self._last_holiday_refresh = None
        self.ctx.logger.info(
            f"{LOG_PREFIX} 配置已更新（version={version}），"
            f"默认提前 {self._default_lead_minutes()} 分钟，"
            f"访问名单 {self._conf().access.mode}"
        )

    # ── 提醒主循环 ────────────────────────────────────────

    async def _run_loop(self) -> None:
        """按 ``check_interval_seconds`` 周期检查即将开始的课。"""
        while self._stop is not None and not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 单轮异常不能让循环退出
                self.ctx.logger.error(
                    f"{LOG_PREFIX} 提醒检查异常: {exc}", exc_info=True
                )

            interval = max(15, int(self._conf().reminder.check_interval_seconds))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        """单轮检查：刷新课表 → 按各会话的提前量找到期课程 → 推送。"""
        async with self._tick_lock:
            conf = self._conf()
            repo = self._repo
            if repo is None:
                return

            repo.cache_seconds = max(0, int(conf.source.scan_interval_seconds))
            # 缓存过期那一轮要读盘并解析全部 ics，放线程池里做，
            # 否则会阻塞事件循环上的命令响应与提醒发送
            await asyncio.to_thread(repo.refresh)

            # 节假日刷新只安排后台任务，绝不在这里 await 网络：
            # 一次慢下载会让两轮 tick 都错过到期窗口 -> 那节课永久漏提醒
            self._maybe_refresh_holidays()

            # 网址课表的自动刷新同理：只安排后台任务，不在 tick 里等网络
            self._maybe_url_refresh()

            # now 必须在所有 await 之后再取，否则窗口与"还有几分钟"都按旧时间算
            now = datetime.now()
            self._maybe_prune(now)
            # 验证 proactive 模式是否真的开口了（没开口就兜底，避免静默丢提醒）
            await self._check_pending_proactive(now)

            if not conf.plugin.enabled or not conf.reminder.enable_reminder:
                return

            # 每个会话可能用不同的提前量，所以按生效提前量分组后各判一轮；
            # 去重键里含提前量，因此不同提前量的会话互不干扰
            deliverable = [
                item
                for item in await self._resolve_user_id_targets(self._subscriptions())
                if self._should_deliver(item)
            ]
            by_lead: dict[int, list[Subscription]] = {}
            for item in deliverable:
                by_lead.setdefault(
                    item.effective_lead(self._default_lead_minutes()), []
                ).append(item)

            if not by_lead:
                self._warn_about_missing_targets()
                return
            # 有能投递的会话：清掉"没目标"类告警，并把被适用范围跳过的群聊报一次
            self._warned_no_target = self._warned_all_filtered = False
            self._warn_about_skipped_groups()

            interval = max(15, int(conf.reminder.check_interval_seconds))
            fired_any = False
            for lead in sorted(by_lead):
                fired_any |= await self._deliver_for_lead(by_lead[lead], lead, now, interval)

            if fired_any:
                self._save_state()

    async def _deliver_for_lead(
        self,
        subscriptions: list[Subscription],
        lead: int,
        now: datetime,
        interval_seconds: int,
    ) -> bool:
        """按某个提前量挑出到期的课并推送给对应会话，返回是否发过消息。"""
        conf = self._conf()
        repo = self._repo
        if repo is None:
            return False

        grace = int(conf.reminder.late_grace_seconds)
        # 到期窗口是 [start - lead, start + grace]，长度必须不短于检查周期，
        # 否则某一轮 tick 可能整段跨过窗口 -> 这节课永远等不到提醒。
        # 提前量设得越小（尤其 0）或检查周期设得越大，越容易踩到。
        grace = max(grace, interval_seconds - lead * 60)

        due = collect_due(
            repo.events,
            now=now,
            lead_minutes=lead,
            late_grace_seconds=grace,
            fired=self._state.fired,
            is_off_day=self._should_skip_off_day,
        )
        if not due:
            return False

        stream_ids = [item.stream_id for item in subscriptions]
        # 文案按"一节课"生成一次再发给所有会话：同一节课的事实完全相同，
        # 逐会话生成既浪费 token，又让 tick 串行等 N 次模型（N 个群就 N 倍延迟）
        persona_style = self._reply_style_for(REMINDER_REASON) == "persona"
        fired_any = False
        for item in due:
            facts = render_message(item, conf.message.template)
            persona_text = await self._persona_say(facts) if persona_style else ""
            sent, failed = 0, []
            for stream_id in stream_ids:
                if await self._deliver(
                    stream_id,
                    facts,
                    reason=REMINDER_REASON,
                    persona_text=persona_text,
                ):
                    sent += 1
                else:
                    failed.append(stream_id)
            if sent == 0:
                # 全部发送失败：不标记已提醒，下一轮在窗口内重试
                self.ctx.logger.warning(
                    f"{LOG_PREFIX} 提醒发送失败，将在下一轮重试: {item.event.display_name}"
                )
                continue
            self._state.mark_fired(item.key, now)
            fired_any = True
            self.ctx.logger.info(
                f"{LOG_PREFIX} 已提醒「{item.event.display_name}」"
                f"（{item.start.strftime('%m-%d %H:%M')}，提前 {lead} 分钟，"
                f"发送 {sent}/{len(stream_ids)}）"
            )
            if failed:
                # 去重键不含会话，所以这些会话这次提醒就丢了：至少要让用户看见
                self.ctx.logger.warning(
                    f"{LOG_PREFIX} 以下会话本次未送达（不会重发，课已临近）: "
                    f"{'、'.join(failed)}"
                )
        return fired_any

    def _warn_about_missing_targets(self) -> None:
        """没有可投递的会话时给出一次提示（区分"没订阅"和"被名单挡住"）。"""
        if not self._subscriptions():
            if not self._warned_no_target:
                repo = self._repo
                if repo is None or not repo.events:
                    # 别说"有课程到期"——课表都还是空的，那不是当前的问题
                    self.ctx.logger.warning(
                        f"{LOG_PREFIX} 还没有导入课表：发 /课程解析 或把 ics 文件"
                        "发给机器人，然后在需要收提醒的会话里发送 /课表订阅"
                    )
                else:
                    self.ctx.logger.warning(
                        f"{LOG_PREFIX} 课表已就绪但没有任何提醒会话，"
                        "请在需要收提醒的会话里发送 /课表订阅"
                    )
                self._warned_no_target = True
            return

        if not self._warned_all_filtered:
            # 分清是"范围"还是"名单"挡的：两者的改法完全不同
            skipped_groups = self._skipped_group_subscriptions()
            if skipped_groups and len(skipped_groups) == len(self._subscriptions()):
                self.ctx.logger.warning(
                    f"{LOG_PREFIX} 有 {len(skipped_groups)} 个提醒会话，但都是群聊，"
                    "而 access.chat_scope=private（只走私聊），本轮不发送。"
                    "在私聊里发 /课表订阅 才会收到提醒；想让群聊也收就把 "
                    "chat_scope 改成 both"
                )
            else:
                self.ctx.logger.warning(
                    f"{LOG_PREFIX} 所有提醒会话都被访问名单挡下了，本轮不发送"
                )
            self._warned_all_filtered = True

    def _maybe_prune(self, now: datetime) -> None:
        """定期清理过期的已提醒记录，以及没人再用的"等待课表文件"标记。"""
        if self._last_prune is not None:
            if (now - self._last_prune).total_seconds() < PRUNE_INTERVAL_SECONDS:
                return
        removed = self._state.prune_fired(now)
        # 等待标记只在"那个会话下一条消息"时才回收，所以任何发过 /课程解析
        # 却没发文件的会话都会留下一条永不清理的记录；这里按截止时间一起扫掉
        stale = [
            stream_id
            for stream_id, deadline in self._awaiting_ics.items()
            if deadline <= now
        ]
        for stream_id in stale:
            self._awaiting_ics.pop(stream_id, None)
        self._last_prune = now
        if removed:
            logger.debug(f"{LOG_PREFIX} 清理已提醒记录 {removed} 条")
        if stale:
            logger.debug(f"{LOG_PREFIX} 清理过期的等待课表标记 {len(stale)} 条")

    def _maybe_refresh_holidays(self, now: datetime | None = None) -> None:
        """按 ``holiday.refresh_hours`` 定期检查节假日数据是否过期。

        真正的判过期交给 :func:`holidays.is_stale`（看缓存文件时间），
        这里只控制"多久去检查一次"，避免每轮 tick 都碰磁盘。
        下载本身走后台任务，本方法不阻塞。

        另有一条独立通道：**下载失败**的年份按
        :data:`HOLIDAY_FAILURE_RETRY_SECONDS` 短周期重试——服务器实测出现过
        一次下载超时，若照常等满 refresh_hours，"假期跳过"会失效半天；
        而「尚未公布」与「地址不合法」不进这条通道（一个本来就该等，一个重试也没用）。
        """
        conf = self._conf()
        if not conf.holiday.skip_off_days and not conf.holiday.extra_dates:
            return
        moment = now or datetime.now()

        periodic_due = False
        refresh_hours = float(conf.holiday.refresh_hours)
        if refresh_hours > 0:
            if self._last_holiday_refresh is None:
                periodic_due = True
            else:
                elapsed = (moment - self._last_holiday_refresh).total_seconds() / 3600
                periodic_due = elapsed >= refresh_hours

        failure_due = any(
            (moment - failed_at).total_seconds() >= HOLIDAY_FAILURE_RETRY_SECONDS
            for failed_at in self._holiday_failed.values()
        )
        if not (periodic_due or failure_due):
            return
        self._last_holiday_refresh = moment
        self._schedule_holiday_refresh()

    # ── 提醒对象 ──────────────────────────────────────────

    def _config_stream_ids(self) -> list[str]:
        """配置里固定推送的会话 ID。"""
        result: list[str] = []
        for item in self._conf().target.target_streams or []:
            value = str(item or "").strip()
            if value and value not in result:
                result.append(value)
        return result

    def _subscriptions(self) -> list[Subscription]:
        """全部提醒会话 = 配置固定会话 + 运行时订阅。

        同一个会话两边都有时以运行时记录为准（它带着群号、昵称和专属提前量）。
        """
        result: list[Subscription] = []
        seen: set[str] = set()
        for item in self._state.subscriptions:
            if item.stream_id in seen:
                continue
            seen.add(item.stream_id)
            result.append(item)
        for stream_id in self._config_stream_ids():
            if stream_id in seen:
                continue
            seen.add(stream_id)
            result.append(Subscription(stream_id=stream_id))
        return result

    def _should_deliver(self, subscription: Subscription) -> bool:
        """投递前的双重过滤：适用范围（会话类型）+ 名单。"""
        if self._scope_denial(subscription.identity, unknown_allows=True) is not None:
            return False
        conf = self._conf()
        if not conf.access.apply_to_reminders:
            return True
        return self._evaluate(subscription.identity).allowed

    def _skipped_group_subscriptions(self) -> list[Subscription]:
        """按适用范围被跳过的群聊订阅（用于告警与 /课表状态）。"""
        if str(self._conf().access.chat_scope or "").strip().lower() != "private":
            return []
        return [
            item for item in self._subscriptions() if item.chat_type == "group"
        ]

    def _warn_about_skipped_groups(self) -> None:
        """有群聊订阅但只走私聊时告警一次：别让提醒静默停掉。"""
        if self._warned_skipped_groups:
            return
        skipped = self._skipped_group_subscriptions()
        if not skipped:
            return
        labels = "、".join(item.display for item in skipped[:5])
        self.ctx.logger.warning(
            f"{LOG_PREFIX} 当前 access.chat_scope=private（只走私聊），"
            f"以下 {len(skipped)} 个群聊订阅不会收到提醒：{labels}。"
            "想去掉它们请在各群发 /课表退订；想让群聊也收提醒就把 chat_scope 改成 both"
        )
        self._warned_skipped_groups = True

    # ── 配置目标的用户号解析 ──────────────────────────────

    async def _resolve_user_id_targets(
        self, subscriptions: list[Subscription]
    ) -> list[Subscription]:
        """把配置里"纯数字"的目标解析成真正的会话 ID。

        用户很自然会把自己的 QQ 号填进 ``target_streams``，但 MaiBot 的
        ``stream_id`` 是 32 位十六进制会话 ID（形如
        ``0123456789abcdef0123456789abcdef``），拿 QQ 号当 stream_id 永远发不出去。
        这里按"纯数字 = 用户号"处理，用 ``chat.get_stream_by_user_id`` 换成会话 ID，
        让那种写法也能正常工作；解析不到的（对方从没私聊过机器人）会明确告警。
        """
        resolved: list[Subscription] = []
        seen: set[str] = set()
        for item in subscriptions:
            target = item.stream_id
            platform, bare = parse_platform_target(target)
            if not _USER_ID_RE.match(bare):
                if target not in seen:
                    seen.add(target)
                    resolved.append(item)
                continue

            session_id = self._resolved_targets.get(target) or (
                await self._lookup_session_for_user(bare, platform)
            )
            if not session_id:
                self._warn_unresolved_target_once(bare)
                continue
            self._resolved_targets[target] = session_id
            if session_id in seen:
                continue  # 与另一个目标指向同一会话（如同时写了 QQ 号与会话 ID）
            seen.add(session_id)
            # 解析后必须把用户号一起带上：否则身份里只剩会话 ID，
            # 而名单里写的往往是 QQ 号，投递时会被自己的白名单挡掉（实测踩到过）
            resolved.append(
                replace(
                    item,
                    stream_id=session_id,
                    user_id=item.user_id or bare,
                    chat_type=item.chat_type or "private",
                )
            )
        return resolved

    async def _lookup_session_for_user(self, user_id: str, platform: str) -> str:
        """把用户号换成会话 ID；查不到返回空串。"""
        try:
            stream = await self.ctx.chat.get_stream_by_user_id(
                user_id, platform=platform
            )
        except Exception as exc:  # 能力不可用/查询失败都不该让整轮投递崩掉
            self.ctx.logger.debug(f"{LOG_PREFIX} 解析用户号 {user_id} 失败: {exc}")
            return ""
        session_id = extract_session_id(stream)
        if session_id:
            self.ctx.logger.info(
                f"{LOG_PREFIX} 配置目标「{user_id}」已解析为会话 {session_id}"
            )
        return session_id

    def _warn_unresolved_target_once(self, user_id: str) -> None:
        """用户号解析不到会话时告警一次，并给出可执行的建议。"""
        if user_id in self._warned_unresolved_targets:
            return
        self._warned_unresolved_targets.add(user_id)
        self.ctx.logger.warning(
            f"{LOG_PREFIX} 配置里的「{user_id}」看起来是用户号，但找不到对应会话"
            "（对方还没有私聊过机器人）。让 TA 先给机器人发一条消息，"
            "或直接在那个会话里发 /课表订阅"
        )

    def _is_config_pinned(self, stream_id: str) -> bool:
        """该会话是否被配置里的固定列表指定（命令退订退不掉，提醒照发）。"""
        if stream_id in self._config_stream_ids():
            return True
        # 配置里写的是用户号、订阅记录里存的是会话 ID 时也要认出来
        return any(
            self._resolved_targets.get(spec) == stream_id
            for spec in self._config_stream_ids()
        )

    def _subscription_limit(self) -> int:
        """允许登记的最大会话数。"""
        return max(1, int(self._conf().target.max_subscriptions))

    def _try_subscribe(self, identity: Any) -> tuple[Subscription | None, bool, str]:
        """登记提醒会话，返回 ``(记录, 是否新增, 拒绝原因)``。

        ``/课表订阅`` 与 ``/课表提前``（会顺手订阅）共用这一条路径，
        否则后者就成了绕过会话数上限的后门。
        """
        stream_id = str(identity.stream_id or "").strip()
        if not stream_id:
            return None, False, "无法识别当前会话"

        existing = self._state.find(stream_id)
        if existing is None and len(self._subscriptions()) >= self._subscription_limit():
            return (
                None,
                False,
                f"提醒会话数已达上限 {self._subscription_limit()} 个，"
                "请先在其他会话发 /课表退订，或调高配置里的上限",
            )

        record, created = self._state.add_subscription(identity)
        self._save_state()
        return record, created, ""

    def _auto_subscribe(self, identity: Any) -> bool:
        """导入成功后把当前会话登记为提醒对象，省掉再发一次 /课表订阅。

        走与命令同一条 :meth:`_try_subscribe` 路径：会话数上限照常生效，
        达到上限时只记日志、不影响导入本身；``file_import.auto_subscribe``
        关闭时完全不做（那是"有时只是帮别人看课表"用户的退路）。
        """
        conf = self._conf()
        if not conf.file_import.auto_subscribe:
            return False
        record, created, denied = self._try_subscribe(identity)
        if record is None:
            self.ctx.logger.info(
                f"{LOG_PREFIX} 自动订阅未生效：{denied}（{identity.display}）"
            )
            return False
        if created:
            self.ctx.logger.info(
                f"{LOG_PREFIX} 已自动订阅：{identity.display}"
                f"（{record.type_label}，导入即订阅）"
            )
        return True

    # ── 宿主人设与身份 ────────────────────────────────────

    async def _load_host_persona(self) -> None:
        """从宿主全局配置读取本体人设与账号。

        插件不自带任何口吻：``persona`` 模式生成文案要按宿主的人设来，
        ``proactive`` 模式的兜底验证也要知道 bot 自己的账号是什么。
        """
        self._persona_nickname = str(await self._host_cfg("bot.nickname", "") or "").strip()
        self._persona_body = str(
            await self._host_cfg("personality.personality", "") or ""
        ).strip()
        self._persona_reply_style = str(
            await self._host_cfg("personality.reply_style", "") or ""
        ).strip()

        qq = await self._host_cfg("bot.qq_account", "")
        if qq:
            self._bot_accounts["qq"] = str(qq)
        platforms = await self._host_cfg("bot.platforms", [])
        if isinstance(platforms, list):
            for item in platforms:
                if isinstance(item, str) and ":" in item:
                    name, _, account = item.partition(":")
                    if name.strip() and account.strip():
                        self._bot_accounts[name.strip()] = account.strip()
        self._persona_loaded = bool(
            self._persona_nickname or self._persona_body or self._bot_accounts
        )
        self.ctx.logger.info(
            f"{LOG_PREFIX} 已读取宿主人设：昵称={self._persona_nickname or '<无>'}，"
            f"人设长度={len(self._persona_body)}，bot 账号={self._bot_accounts or '<无>'}"
        )

    async def _ensure_persona(self) -> None:
        """第一次要用人设时补读一次。

        宿主配置不一定在插件 on_load 时就绪（实测插件会被先加载），
        读不到就一直用人设兜底文案，那人格就丢了。
        """
        if self._persona_loaded:
            return
        await self._load_host_persona()

    async def _host_cfg(self, key: str, default: Any) -> Any:
        """读一条宿主全局配置；失败返回默认值（插件不该因此崩）。"""
        try:
            value = await self.ctx.config.get(key, default)
        except Exception as exc:
            self.ctx.logger.debug(f"{LOG_PREFIX} 读取宿主配置 {key} 失败: {exc}")
            return default
        return value if value is not None else default

    def _persona_header(self) -> str:
        """拼出人设提示头；宿主没配人设时给一个中性的兜底。"""
        parts: list[str] = []
        if self._persona_nickname:
            parts.append(f"你的名字是{self._persona_nickname}。")
        if self._persona_body:
            parts.append(self._persona_body)
        if self._persona_reply_style:
            parts.append(f"说话风格：{self._persona_reply_style}")
        if not parts:
            return "你是一个正在聊天里说话的 bot，口吻自然简短。"
        return " ".join(parts)

    async def _persona_say(self, fact_prompt: str) -> str:
        """让模型按宿主人设说一句话；失败返回空串，由调用方退化。

        这条路径的价值：文案带宿主风格，但发送仍走 ``send.text``，
        **不依赖主链路是否开口**，所以既能拟人又必定送达。
        """
        conf = self._conf()
        await self._ensure_persona()
        hint = str(conf.reply.persona_hint or "").strip()
        prompt = (
            f"{self._persona_header()}\n{fact_prompt}\n"
            "只输出要发送的那一句话，不要解释、不要加引号。"
        )
        if hint:
            prompt += f"（额外要求：{hint}）"
        timeout = float(conf.reply.persona_timeout_seconds)
        try:
            call = self.ctx.llm.generate(prompt=prompt, temperature=0.7, max_tokens=120)
            if timeout > 0:
                # 提醒循环是同步等文案的：模型卡住会拖慢整轮检查，
                # 极端情况下（提前量设得小）会跨过某节课的提醒窗口
                result = await asyncio.wait_for(call, timeout=timeout)
            else:
                result = await call
        except asyncio.TimeoutError:
            self.ctx.logger.warning(
                f"{LOG_PREFIX} 生成拟人文案超时（{timeout:.0f}s），改用模板文案"
            )
            return ""
        except Exception as exc:
            self.ctx.logger.warning(f"{LOG_PREFIX} 生成拟人文案失败: {exc}")
            return ""
        if isinstance(result, dict):
            if not result.get("success", True):
                self.ctx.logger.debug(f"{LOG_PREFIX} LLM 拒绝生成: {result.get('error')}")
                return ""
            text = str(result.get("response") or "").strip()
        else:
            text = str(result or "").strip()
        # 去掉模型偶尔加上的引号与换行
        return text.strip().strip('"').strip("「」").replace("\n", " ").strip()

    # ── 主动发言的验证 ────────────────────────────────────

    async def _bot_spoke_since(self, stream_id: str, platform: str, since_ts: float) -> bool:
        """检查 bot 自己在 ``since_ts`` 之后是否发过言。

        判断不了时返回 ``True``（信任主链路）：宁可偶发不兜底，也不要因为拿不到
        账号信息就重复发一条。

        账号匹配用**全部自有账号**而不是只认 ``platform``：proactive 的待验证项
        是异步登记的，手上不一定有准确的平台名，而拿错账号比较会得出"没发言"，
        那就变成重复发送。只要发言人命中任一自有账号即视为已开口。
        """
        await self._ensure_persona()
        accounts = self._bot_accounts
        if not accounts:
            return True
        # 优先精确平台，缺失时接受任一自有账号（见上面注释里的取舍）
        primary = accounts.get(platform or "qq")
        accepted = {account for account in accounts.values() if account}
        if primary:
            accepted.add(primary)
        if not accepted:
            return True
        try:
            messages = await asyncio.wait_for(
                self.ctx.message.get_recent(chat_id=stream_id, limit=10),
                timeout=PROACTIVE_QUERY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            self.ctx.logger.warning(f"{LOG_PREFIX} 查询最近消息超时，本轮不做兜底判断")
            return True
        except Exception as exc:
            self.ctx.logger.debug(f"{LOG_PREFIX} 查询最近消息失败: {exc}")
            return True
        if not isinstance(messages, list):
            return True

        for item in messages:
            if not isinstance(item, dict):
                continue
            try:
                timestamp = float(item.get("timestamp") or 0)
            except (TypeError, ValueError):
                continue
            if timestamp <= since_ts:
                continue
            info = item.get("message_info") or {}
            user_id = str((info.get("user_info") or {}).get("user_id") or "")
            if user_id in accepted:
                return True
        return False

    def _schedule_proactive_check(
        self, stream_id: str, facts: str, fixed_text: str
    ) -> None:
        """登记一次待验证的主动发言（到点仍未发声就兜底）。"""
        delay = int(self._conf().reply.fallback_after_seconds)
        if delay <= 0:
            return  # 关掉验证：完全信任主链路
        queued_at = datetime.now()
        self._pending_proactive.append(
            {
                "stream_id": stream_id,
                "queued_at": queued_at,
                "due_at": queued_at + timedelta(seconds=delay),
                "facts": facts,
                "fixed_text": fixed_text,
            }
        )

    async def _check_pending_proactive(self, now: datetime) -> None:
        """到点验证待处理的主动发言；主链路没开口就兜底直发。"""
        if not self._pending_proactive:
            return
        remaining: list[dict[str, Any]] = []
        for item in self._pending_proactive:
            if item["due_at"] > now:
                remaining.append(item)
                continue
            spoken = await self._bot_spoke_since(
                item["stream_id"], "qq", item["queued_at"].timestamp()
            )
            if spoken:
                self.ctx.logger.info(
                    f"{LOG_PREFIX} replyer 已开口，无需兜底: {item['stream_id']}"
                )
                continue
            self.ctx.logger.warning(
                f"{LOG_PREFIX} replyer 超时未开口，兜底直发: {item['stream_id']}"
            )
            text = item["fixed_text"] or await self._persona_say(item["facts"])
            if text:
                await self._send_text(item["stream_id"], text)
        self._pending_proactive = remaining

    # ── 发送 ──────────────────────────────────────────────

    def _reply_style_for(self, reason: str) -> str:
        """按用途取生效的发送方式：提醒用 ``reply.style``，其余用 ``reply.ack_style``。"""
        conf = self._conf()
        return conf.reply.style if reason == REMINDER_REASON else conf.reply.ack_style

    async def _deliver(
        self,
        stream_id: str,
        facts: str,
        *,
        reason: str,
        fixed_text: str = "",
        persona_text: str | None = None,
    ) -> bool:
        """把一条内容送到某个会话，按用途选择交给 replyer 还是模板直发。

        Args:
            facts: **给模型**的内容（事实 + 要怎么说的要求），persona 模式用这段。
            fixed_text: **给用户看**的最终文案，直发模式用这段；留空则退回 ``facts``
                （提醒的模板文案两者合一，所以不必传）。
            reason: 决定用哪个风格设置：提醒走 ``reply.style``，其余走 ``reply.ack_style``。
            persona_text: 调用方预先算好的拟人文案。``None`` = 没预算，这里现算；
                空串 = 已经算过但失败了，别再算一次（多会话时避免重复请求模型）。

        默认两种模式都直发：实测发现 persona 只保证"任务已入队"，麦麦规划完
        **可能不真的开口**且不报错，而提示语、回执、提醒都要求必定送达。
        """
        conf = self._conf()
        style = self._reply_style_for(reason)

        if style == "persona":
            # 让模型按宿主人设说一句，再直发：有风格且必定送达
            text = persona_text if persona_text is not None else await self._persona_say(facts)
            if text:
                return await self._send_text(stream_id, text)
            self.ctx.logger.info(
                f"{LOG_PREFIX} 拟人文案生成失败，改用固定文案（reason={reason}）"
            )

        elif style == "proactive":
            # 交给主链路开口；随后按 fallback_after_seconds 验证是否真的说了
            if await self._trigger_replyer(stream_id, facts, reason=reason):
                self._schedule_proactive_check(stream_id, facts, fixed_text or facts)
                return True
            if not conf.reply.fallback_to_fixed:
                self.ctx.logger.warning(
                    f"{LOG_PREFIX} replyer 未接手，且已关闭回退直发：内容未送达"
                )
                return False
            self.ctx.logger.info(
                f"{LOG_PREFIX} replyer 未接手（reason={reason}），改用直发"
            )

        return await self._send_text(stream_id, fixed_text or facts)

    async def _trigger_replyer(self, stream_id: str, facts: str, *, reason: str) -> bool:
        """请求 Maisaka 基于该会话主动开口；返回是否已被接手。"""
        conf = self._conf()
        intent = (
            "请用你自己的语气在这个聊天里说一件事，不要提这是系统任务或定时任务，"
            "也不要复述本条指令。事实如下：\n"
            f"{facts}"
        )
        hint = str(conf.reply.persona_hint or "").strip()
        if hint:
            intent += f"\n\n额外要求：{hint}"
        try:
            # priority=high：提醒是时效性内容，让主链路优先处理
            result = await self.ctx.maisaka.proactive.trigger(
                stream_id,
                intent,
                reason=reason,
                priority="high",
                metadata={"plugin": "class-schedule", "kind": reason},
            )
        except Exception as exc:
            self.ctx.logger.warning(f"{LOG_PREFIX} 触发 replyer 失败: {exc}")
            return False

        if isinstance(result, dict) and not result.get("success", True):
            error = str(result.get("error") or "未知原因")
            # 会话不存在通常意味着目标从没和机器人说过话
            self.ctx.logger.info(f"{LOG_PREFIX} replyer 拒绝接手（{error}）")
            return False
        return True

    async def _send_text(self, stream_id: str, text: str) -> bool:
        """向指定会话发纯文本。

        只用 ``ctx.send.text``：提醒本身是 2-4 行短文本，而麦麦内置的
        NapCat / SnowLuma 适配器及主机都不支持 markdown 之类的自定义消息段
        （全仓搜不到 ``qq_markdown``），所以不走自定义段那条路。
        """
        return bool(await self.ctx.send.text(text, stream_id))

    async def _reply(self, stream_id: str, text: str) -> None:
        """回复当前会话（失败只记日志，不影响命令返回值）。"""
        if not stream_id:
            return
        try:
            await self.ctx.send.text(text, stream_id)
        except Exception as exc:
            self.ctx.logger.warning(f"{LOG_PREFIX} 回复消息失败: {exc}")

    def _save_state(self) -> None:
        """保存状态；失败只记日志（状态次要，不应影响主流程）。"""
        if self._data_dir is None:
            return
        try:
            self._state.save(self._state_path())
        except Exception as exc:
            self.ctx.logger.warning(f"{LOG_PREFIX} 保存状态失败: {exc}")

    # ── 课表刷新 / 导入 ───────────────────────────────────

    async def _reload(self, *, force: bool = True) -> CourseRepository | None:
        """在线程池里重新扫描课表目录，避免阻塞事件循环。"""
        repo = self._repo
        if repo is None:
            return None
        await asyncio.to_thread(repo.refresh, force=force)
        return repo

    async def _import_from_url(
        self, url: str, identity: Any = None
    ) -> tuple[bool, str]:
        """下载并保存一份 URL 课表，返回 ``(是否成功, 提示文案)``。"""
        conf = self._conf()
        try:
            text = await fetch_ics(
                url,
                timeout=int(conf.source.url_timeout_seconds),
                max_bytes=int(conf.source.max_ics_kb) * 1024,
            )
        except UnsafeUrlError as exc:
            return False, f"❌ 该地址不允许导入：{exc}"
        except FetchError as exc:
            return False, f"❌ 下载失败：{exc}"

        repo = self._repo
        if repo is None:
            return False, "❌ 插件尚未初始化完成，请稍后再试"

        try:
            result = await asyncio.to_thread(
                repo.save_ics, text, import_filename(url), source_url=url
            )
        except ValueError as exc:
            return False, f"❌ 这不是有效的课表文件：{exc}"
        except OSError as exc:
            return False, f"❌ 保存课表失败：{exc}"

        action = "已更新" if result.replaced else "导入成功"
        message = (
            f"✅ {action}：{result.filename}\n"
            f"📚 解析出 {result.event_count} 条课程，"
            f"当前共 {len(repo.events)} 条 / {repo.file_count} 个文件"
        )
        hours = float(self._conf().source.url_refresh_hours)
        if hours > 0:
            message += f"\n🔁 已加入自动刷新：每 {hours:g} 小时检查一次，有变动会通知你"
        # 与聊天文件一致：导入即订阅当前会话（可关）
        if self._auto_subscribe(identity):
            message += "\n🔔 本会话已自动订阅上课提醒，不想收了发 /课表退订。"
        if result.warnings:
            message += "\n⚠️ " + "；".join(result.warnings[:3])
        return True, message

    # ── 课表展示 ──────────────────────────────────────────

    def _events_between(
        self, start: datetime, end: datetime, limit: int = 0
    ) -> list[CourseEvent]:
        """返回 ``[start, end]`` 区间内的课程，``limit > 0`` 时截断。"""
        repo = self._repo
        if repo is None:
            return []
        return upcoming_events(repo.events, start=start, end=end, limit=limit)

    @staticmethod
    def _format_event(event: CourseEvent) -> str:
        """把一次课渲染成 ``08:20-10:00 高等数学 @教三-201`` 形式的一行。"""
        time_part = event.start.strftime("%H:%M")
        if event.end is not None and event.end.date() == event.start.date():
            time_part += f"-{event.end.strftime('%H:%M')}"
        # 课程名/地点来自第三方 ics，截断后再拼，避免单条占满整屏
        location = one_line(event.location)
        name = one_line(event.display_name)
        suffix = f" @{location}" if location else ""
        return f"{time_part} {name}{suffix}"

    def _day_lines(self, day: datetime) -> list[str]:
        """某一天的课程行（首行是日期标题），无课返回空列表。"""
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        events = self._events_between(day_start, day_start + timedelta(days=1))
        if not events:
            return []
        lines = [
            f"📅 {day_start.strftime('%m-%d')} {_WEEKDAY_LABELS[day_start.weekday()]}"
            f"{self._holiday_marks(day_start.date())}"
        ]
        for event in events:
            lines.append(f"　{self._format_event(event)}")
        return lines

    def _format_day(self, day: datetime) -> str:
        """格式化某一天的课程，无课返回空串。"""
        return "\n".join(self._day_lines(day))

    def _schedule_summary(self, days: int) -> str:
        """多天课程汇总，总行数受 ``MAX_LIST_LINES`` 限制。

        课表周数多、课程名长时消息会非常长，超出上限会发送失败或刷屏。
        """
        now = datetime.now()
        lines: list[str] = []
        truncated = False
        for offset in range(days):
            for line in self._day_lines(now + timedelta(days=offset)):
                if len(lines) >= MAX_LIST_LINES:
                    truncated = True
                    break
                lines.append(line)
            if truncated:
                break

        if not lines:
            return "这段时间没有课 🎉"
        text = "\n".join(lines)
        if truncated:
            text += f"\n…（课程较多，仅显示前 {MAX_LIST_LINES} 行）"
        return text

    # ── 命令 ──────────────────────────────────────────────

    @Command(
        "schedule_today",
        description="查看今天的课程",
        pattern=r"^/课表\s*$",
    )
    @_requires_access
    async def handle_today(self, **kwargs: Any) -> CommandResult:
        text = self._schedule_summary(1)
        await self._reply(str(kwargs.get("stream_id", "")), text)
        return True, "已返回今日课程", 1

    @Command(
        "schedule_tomorrow",
        description="查看明天的课程",
        pattern=r"^/课表明日\s*$",
    )
    @_requires_access
    async def handle_tomorrow(self, **kwargs: Any) -> CommandResult:
        tomorrow = datetime.now() + timedelta(days=1)
        block = self._format_day(tomorrow) or "明天没有课 🎉"
        await self._reply(str(kwargs.get("stream_id", "")), block)
        return True, "已返回明日课程", 1

    @Command(
        "schedule_week",
        description="查看今天起七天的课程",
        pattern=r"^/课表本周\s*$",
    )
    @_requires_access
    async def handle_week(self, **kwargs: Any) -> CommandResult:
        text = self._schedule_summary(7)
        await self._reply(str(kwargs.get("stream_id", "")), text)
        return True, "已返回本周课程", 1

    @Command(
        "schedule_import",
        description="从网址导入 ics 课表",
        pattern=r"^/课表导入\s+(?P<url>\S+)\s*$",
    )
    @_requires_access
    async def handle_import(self, **kwargs: Any) -> CommandResult:
        groups = kwargs.get("matched_groups") or {}
        url = str(groups.get("url", "")).strip()
        identity = identity_from_kwargs(kwargs)
        ok, message = await self._import_from_url(url, identity=identity)
        await self._reply(str(kwargs.get("stream_id", "")), message)
        return ok, message, 2 if ok else 1

    @Command(
        "schedule_parse",
        description="引导导入课表：回复提示后等待你发送 ics 文件",
        pattern=r"^/(?:课程解析|课表解析)\s*$",
    )
    @_requires_access
    async def handle_parse(self, **kwargs: Any) -> CommandResult:
        """进入「等待课表文件」状态，并让麦麦请用户把 ics 发过来。

        进入等待状态不只是为了提示：这样用户万一发错文件（zip、截图），
        插件能明确告诉他「这不是 ics」，而不是像平时那样静默忽略。
        """
        stream_id = str(kwargs.get("stream_id", "")).strip()
        if not stream_id:
            message = "❌ 无法识别当前会话"
            await self._reply(stream_id, message)
            return False, message, 0

        minutes = self._arm_awaiting_ics(stream_id)
        fixed_text = (
            f"📅 请把教务系统导出的 .ics 文件发到这个聊天里，我会自动解析导入。"
            f"（{minutes} 分钟内有效）"
        )
        facts = (
            "用户想导入课表。请告诉 TA：把教务系统导出的 .ics 文件（iCalendar 格式）"
            "直接发到这个聊天里就行，你会自动解析并导入。"
            f"接下来 {minutes} 分钟内收到的文件都会被当成课表处理，不需要让 TA 做别的操作。"
        )
        delivered = await self._deliver(
            stream_id, facts, reason="file_import_prompt", fixed_text=fixed_text
        )
        if not delivered:
            # 提示语就是这个命令的全部作用，交不出去就直接发，别让命令看起来没反应
            self.ctx.logger.warning(
                f"{LOG_PREFIX} 提示语未能交给 replyer，改为直接发送"
            )
            await self._reply(stream_id, fixed_text)
        self.ctx.logger.info(
            f"{LOG_PREFIX} 已进入等待课表文件状态（{minutes} 分钟）：{stream_id}"
        )
        return True, "已提示用户发送 ics 文件", 1

    @Command(
        "schedule_reload",
        description="重新扫描课表目录",
        pattern=r"^/课表重载\s*$",
    )
    @_requires_access
    async def handle_reload(self, **kwargs: Any) -> CommandResult:
        repo = await self._reload(force=True)
        if repo is None:
            message = "❌ 插件尚未初始化完成，请稍后再试"
        else:
            message = (
                f"🔄 已重新扫描：{repo.file_count} 个文件 / "
                f"{len(repo.events)} 条课程"
            )
            if repo.errors:
                message += "\n⚠️ " + "；".join(repo.errors[:3])
        await self._reply(str(kwargs.get("stream_id", "")), message)
        return repo is not None, message, 1

    @Command(
        "schedule_subscribe",
        description="把当前会话（群聊或私聊）设为提醒对象",
        pattern=r"^/课表订阅\s*$",
    )
    @_requires_access
    async def handle_subscribe(self, **kwargs: Any) -> CommandResult:
        stream_id = str(kwargs.get("stream_id", "")).strip()
        record, created, denied = self._try_subscribe(identity_from_kwargs(kwargs))
        if record is None:
            message = f"❌ {denied}"
        elif created:
            message = (
                f"✅ 已把本会话（{record.type_label}）设为提醒对象，"
                f"当前共 {len(self._subscriptions())} 个。\n"
                f"上课前 {record.effective_lead(self._default_lead_minutes())} "
                "分钟会在这里提醒你\n"
                "想改提前量可以发 /课表提前 30"
            )
            # 订阅成功不代表发得出去：投递侧还会按名单过一遍
            if not self._should_deliver(record):
                message += "\n⚠️ 但当前访问名单会挡住本会话，提醒不会送达"
        else:
            message = (
                f"ℹ️ 本会话已经是提醒对象"
                f"（{record.lead_display(self._default_lead_minutes())}）"
            )
        await self._reply(stream_id, message)
        return record is not None, message, 1

    @Command(
        "schedule_unsubscribe",
        description="取消当前会话的提醒",
        pattern=r"^/课表退订\s*$",
    )
    @_requires_access
    async def handle_unsubscribe(self, **kwargs: Any) -> CommandResult:
        stream_id = str(kwargs.get("stream_id", "")).strip()
        removed = self._state.remove_subscription(stream_id)
        if removed:
            self._save_state()

        # 配置固定推送的会话必须单独说清楚：光看"状态里有没有记录"会漏判——
        # 它在配置里被 /课表提前 升级成运行时记录后，退订会成功但提醒照发
        if self._is_config_pinned(stream_id):
            head = "✅ 已清掉本会话的单独设置（含提前量），" if removed else "ℹ️ "
            message = (
                f"{head}但本会话由配置文件的 target_streams 固定推送，"
                "请到插件配置里删除后重载插件"
            )
        elif removed:
            message = f"✅ 已取消本会话的提醒，剩余 {len(self._subscriptions())} 个提醒会话"
        else:
            message = "ℹ️ 本会话本来就不在提醒列表里"
        await self._reply(stream_id, message)
        return True, message, 1

    @Command(
        "schedule_lead",
        description="查看或设置本会话提前提醒的分钟数",
        pattern=r"^/课表提前(?:\s+(?P<value>\S+))?\s*$",
    )
    @_requires_access
    async def handle_lead(self, **kwargs: Any) -> CommandResult:
        stream_id = str(kwargs.get("stream_id", "")).strip()
        groups = kwargs.get("matched_groups") or {}
        raw = str(groups.get("value", "")).strip()
        default = self._default_lead_minutes()

        if not raw:
            record = self._state.find(stream_id)
            if record is None:
                message = (
                    f"⏰ 本会话还没订阅提醒，默认会提前 {default} 分钟提醒\n"
                    "发 /课表订阅 开始接收提醒\n"
                    "用法：/课表提前 30 单独设置本会话，/课表提前 重置 恢复默认"
                )
            else:
                message = (
                    f"⏰ 本会话：{record.lead_display(default)}\n"
                    f"（配置默认值 {default} 分钟）\n"
                    "用法：/课表提前 30 单独设置本会话，/课表提前 重置 恢复默认"
                )
        elif raw in ("重置", "默认", "reset", "default"):
            if self._state.set_lead(stream_id, None):
                self._save_state()
                message = f"✅ 本会话已恢复为跟随配置：提前 {default} 分钟"
            else:
                message = "ℹ️ 本会话还没订阅提醒，无需重置；发 /课表订阅 开始接收提醒"
        elif (minutes := self._parse_lead_value(raw)) is not None:
            # 顺手订阅：用户在本会话设提前量，意图显然是想在这里收提醒
            record, created, denied = self._try_subscribe(identity_from_kwargs(kwargs))
            if record is None:
                message = f"❌ {denied}"
            else:
                self._state.set_lead(record.stream_id, minutes)
                self._save_state()
                prefix = "✅ 已订阅本会话并设置" if created else "✅ 已设置"
                message = f"{prefix}：上课前 {minutes} 分钟提醒（仅本会话生效）"
                if not self._should_deliver(record):
                    message += "\n⚠️ 但当前访问名单会挡住本会话，提醒不会送达"
        else:
            message = (
                f"❌ 分钟数请填 0-{MAX_LEAD_MINUTES} 的整数，例如 /课表提前 30"
            )

        await self._reply(stream_id, message)
        return True, message, 1

    @Command(
        "schedule_holiday",
        description="查看法定节假日数据状态与今日性质",
        pattern=r"^/课表假日\s*$",
    )
    @_requires_access
    async def handle_holiday(self, **kwargs: Any) -> CommandResult:
        # 不用 await 等下载：最坏要等 30 秒以上，用户只会以为卡死了。
        # 立即用当前已有数据回状态，同时安排后台刷新
        self._schedule_holiday_refresh()
        conf = self._conf()
        lines = ["🗓️ 法定节假日", *self._holiday_status_lines()]

        calendar = self._holidays
        upcoming = self._upcoming_holidays(calendar, limit=3) if calendar else []
        if upcoming:
            lines.append("　最近假日：")
            for day, label in upcoming:
                lines.append(
                    f"　　{day.strftime('%m-%d')} {_WEEKDAY_LABELS[day.weekday()]} {label}"
                )
        lines.append(f"　数据来源：{conf.holiday.source_url_template}")

        message = "\n".join(lines)
        await self._reply(str(kwargs.get("stream_id", "")), message)
        return True, "已返回节假日状态", 1

    def _upcoming_holidays(
        self, calendar: HolidayCalendar, *, limit: int
    ) -> list[tuple[date, str]]:
        """从今天起往后列出最近的放假日期（跳过调休上班日）。"""
        today = datetime.now().date()
        result: list[tuple[date, str]] = []
        for offset in range(0, 400):
            day = today + timedelta(days=offset)
            if not calendar.covers(day):
                continue
            if calendar.is_off_day(day):
                # 同一次假期的连续日期只在首日列出名称
                previous = today + timedelta(days=offset - 1)
                if offset > 0 and calendar.is_off_day(previous) and calendar.name_of(previous) == calendar.name_of(day):
                    continue
                result.append((day, calendar.name_of(day)))
                if len(result) >= limit:
                    break
        return result

    @Command(
        "schedule_status",
        description="查看插件运行状态与提醒会话列表",
        pattern=r"^/课表状态\s*$",
    )
    @_requires_access
    async def handle_status(self, **kwargs: Any) -> CommandResult:
        conf = self._conf()
        repo = self._repo
        default = self._default_lead_minutes()

        lines = [
            "📊 课程表提醒状态",
            f"　插件：{'启用' if conf.plugin.enabled else '已停用'}",
            f"　提醒：{'开启' if conf.reminder.enable_reminder else '关闭'}，"
            f"每 {conf.reminder.check_interval_seconds} 秒检查一次",
            f"　课表：{repo.file_count if repo else 0} 个文件 / "
            f"{len(repo.events) if repo else 0} 条课程",
            f"　默认提前：{default} 分钟",
            f"　适用范围：{self._scope_summary()}",
            f"　访问名单：{self._access_summary(conf)}",
            *self._holiday_status_lines(),
        ]

        # 打印本会话的全部可用标识：名单条目要照抄这些值，尤其是适配器
        # 不下发群号/用户号时只能靠会话 ID
        identity = identity_from_kwargs(kwargs)
        labels = []
        if identity.group_id:
            labels.append(f"群号 {identity.group_id}")
        if identity.user_id:
            labels.append(f"用户号 {identity.user_id}")
        if identity.stream_id:
            labels.append(f"会话ID {identity.stream_id}")
        lines.append(f"　本会话标识：{'、'.join(labels) if labels else '无'}")

        subscriptions = self._subscriptions()
        lines.append(f"　提醒会话：{len(subscriptions)} 个")
        current_stream = str(kwargs.get("stream_id", "")).strip()
        for item in subscriptions[:MAX_STATUS_SUBSCRIPTIONS]:
            marks = []
            if item.stream_id == current_stream:
                marks.append("本会话")
            if self._is_config_pinned(item.stream_id):
                marks.append("来自配置")
            if self._scope_denial(item.identity, unknown_allows=True) is not None:
                # 说清"登记了但收不到"，否则用户只会觉得提醒坏了
                marks.append("群聊，按适用范围跳过")
            suffix = f" ← {'、'.join(marks)}" if marks else ""
            lines.append(
                f"　　[{item.type_label}] {item.display}"
                f" 提前 {item.lead_display(default)}{suffix}"
            )
        if len(subscriptions) > MAX_STATUS_SUBSCRIPTIONS:
            lines.append(f"　　…还有 {len(subscriptions) - MAX_STATUS_SUBSCRIPTIONS} 个")

        upcoming = self._events_between(
            datetime.now(), datetime.now() + timedelta(days=7), limit=1
        )
        if upcoming:
            lines.append(
                f"　下一节课：{self._format_event(upcoming[0])}"
                f"（{upcoming[0].start.strftime('%m-%d')}）"
            )
        if repo and repo.errors:
            lines.append(f"　⚠️ 解析失败：{len(repo.errors)} 个文件")

        message = "\n".join(lines)
        await self._reply(str(kwargs.get("stream_id", "")), message)
        return True, "已返回状态", 1

    @staticmethod
    def _parse_lead_value(raw: str) -> int | None:
        """解析提前量输入，返回合法分钟数；非数字或越界返回 ``None``。

        不用 ``str.isdigit()`` 判定：它接受 ``²``、``１２３`` 这类字符，
        而 ``int('²')`` 会抛 ``ValueError``，命令会因此无声地不回话。
        """
        try:
            minutes = int(str(raw).strip())
        except (TypeError, ValueError):
            return None
        if 0 <= minutes <= MAX_LEAD_MINUTES:
            return minutes
        return None

    # ── 法定节假日 ────────────────────────────────────────

    def _holiday_dir(self) -> Path:
        """节假日缓存目录。"""
        base = self._data_dir or Path("data") / "plugins" / "github.BUNNY-19C.class-schedule"
        return base / HOLIDAY_DIR

    def _reload_holiday_cache(self) -> HolidayCalendar:
        """从磁盘缓存重建日历，并叠加用户自定假日与"照常上课日"。"""
        conf = self._conf()
        calendar = HolidayCalendar()
        loaded = calendar.load_cache_dir(self._holiday_dir())
        added, rejected = calendar.add_extra_dates(conf.holiday.extra_dates or [])
        excluded, rejected_exclude = calendar.add_excluded_dates(
            conf.holiday.exclude_dates or []
        )
        for label, bad in (("自定假日", rejected), ("照常上课日", rejected_exclude)):
            if bad:
                self.ctx.logger.warning(
                    f"{LOG_PREFIX} {label}格式无法识别已忽略: {'、'.join(bad[:5])}"
                )
        logger.debug(
            "%s 节假日数据：%d 个年份文件 / %d 条自定假日 / %d 条照常上课日",
            LOG_PREFIX,
            loaded,
            added,
            excluded,
        )
        self._holidays = calendar
        return calendar

    def _schedule_holiday_refresh(self, *, force: bool = False) -> None:
        """把节假日刷新丢到后台任务，**绝不阻塞调用方**。

        为什么必须异步：下载最坏情况要花「年份数 × 超时」（默认 30 秒），
        而提醒判定要求"相邻两次 tick 的间隔不大于到期窗口"。若在 tick 里等网络，
        一次慢下载就会让两轮 tick 都落在窗口之外，那节课**永久**不再提醒。
        命令路径同理：等 30 秒才回包，用户只会以为插件卡死了。
        """
        if self._holiday_task is not None and not self._holiday_task.done():
            return  # 已有下载在进行，不叠加第二份（单飞）
        self._holiday_task = asyncio.create_task(
            self._ensure_holiday_data(force=force),
            name="class-schedule-holiday-refresh",
        )

    async def _ensure_holiday_data(self, *, force: bool = False) -> HolidayCalendar:
        """按需下载缺失/过期的节假日数据，然后重建日历。

        只在必要时联网；下载失败保留旧缓存继续用（拿不到数据就不跳过提醒，
        见 :meth:`_should_skip_off_day`）。调用方请用
        :meth:`_schedule_holiday_refresh`，不要直接在 tick/命令里 await。
        """
        async with self._holiday_lock:
            conf = self._conf()
            calendar = self._reload_holiday_cache()
            if not conf.holiday.skip_off_days and not conf.holiday.extra_dates:
                # 既不用数据源判假日、也没有自定假日，没必要联网
                return calendar

            now = datetime.now()
            warned = False
            retry_after = timedelta(
                hours=max(1.0, float(conf.holiday.refresh_hours) or 12.0)
            )
            for offset in range(HOLIDAY_YEAR_SPAN):
                year = now.year + offset
                # 已知尚未公布的年份：隔一段时间再试，别每次都下载 + 刷日志
                unpublished_at = self._holiday_unpublished.get(year)
                if unpublished_at is not None and now - unpublished_at < retry_after:
                    continue
                path = cache_path(self._holiday_dir(), year)
                if not force and not is_stale(
                    path, now=now, max_age_hours=float(conf.holiday.refresh_hours)
                ):
                    continue
                outcome = await self._download_holiday_year(year, path)
                if outcome == "ok":
                    # 成功即清掉失败标记；其余类别见 _download_holiday_year 的分类
                    self._holiday_failed.pop(year, None)
                elif outcome == "failed":
                    # 网络瞬断/超时这类可能自愈的失败：短周期重试，
                    # 别让一次断网把"假期跳过"废掉整个 refresh_hours
                    self._holiday_failed[year] = now
                    warned = True
                # "unpublished"（常态，长周期后自然重试）与
                # "bad_url"（重试也不会好，只靠 warn-once 提示）不进重试表

            self._holidays = self._reload_holiday_cache()
            # 只有整轮都没出问题才复位"已告警"标志，否则缺年份的告警
            # 会被另一年的成功静默清掉，变成每次都报一遍
            if not warned:
                self._warned_holiday_source = False
            return self._holidays

    async def _download_holiday_year(self, year: int, path: Path) -> str:
        """下载某一年的节假日数据并落盘。

        返回结果类别：``"ok"`` / ``"unpublished"``（公布前没有数据，常态）/
        ``"bad_url"``（地址本身不合法，重试也不会好）/ ``"failed"``（网络或
        数据问题，值得短周期重试）。失败保留旧缓存。
        """
        conf = self._conf()
        url = render_url_template(conf.holiday.source_url_template, year)
        if not url:
            return "bad_url"
        try:
            text = await fetch_text(
                url, timeout=int(conf.holiday.timeout_seconds)
            )
        except UnsafeUrlError as exc:
            # 地址不合法（含内网）只提示一次即可，不必每年重复报
            self._warn_holiday_source_once(f"数据来源地址不允许：{exc}")
            return "bad_url"
        except FetchError as exc:
            self._warn_holiday_source_once(f"{year} 年数据下载失败：{exc}")
            return "failed"

        try:
            probe = HolidayCalendar()
            probe.load_payload(text, source=f"{year}.json")
        except HolidayNotPublishedError:
            # 次年安排通常要到当年 11 月前后才公布，「目前没有」是常态：
            # 记下来安静跳过，既不当失败告警，也不反复下载
            self._holiday_unpublished[year] = datetime.now()
            self.ctx.logger.info(
                f"{LOG_PREFIX} {year} 年放假安排尚未公布，先跳过"
                "（不影响提醒，公布后会自动补上）"
            )
            return "unpublished"
        except ValueError as exc:
            self._warn_holiday_source_once(f"{year} 年数据格式不对：{exc}")
            return "failed"

        # 下回来的内容必须真的覆盖请求的那一年，否则按 <year>.json 落盘后
        # 会被当成"已有数据"，在 refresh_hours 内不再重试，而实际一天都判不出来
        if not probe.has_entries_for(year):
            self._warn_holiday_source_once(
                f"{year} 年数据里没有任何 {year} 年的日期（可能该年公告尚未发布）"
            )
            return "unpublished"

        try:
            await asyncio.to_thread(write_cache, self._holiday_dir(), year, text)
        except OSError as exc:
            self.ctx.logger.warning(f"{LOG_PREFIX} 节假日缓存写入失败: {exc}")
            return "failed"
        self.ctx.logger.info(f"{LOG_PREFIX} 已更新 {year} 年法定节假日数据")
        return "ok"

    def _warn_holiday_source_once(self, reason: str) -> None:
        """节假日数据拿不到时只告警一次，避免每轮 tick 刷屏。"""
        if self._warned_holiday_source:
            return
        self.ctx.logger.warning(
            f"{LOG_PREFIX} {reason}；在拿到数据前不会跳过节假日提醒"
            "（宁可假期多提醒一次，也不漏掉上课日）。可在 /课表假日 查看状态"
        )
        self._warned_holiday_source = True

    # ── 网址课表的自动刷新 ────────────────────────────────

    def _maybe_url_refresh(self, now: datetime | None = None) -> None:
        """按 ``source.url_refresh_hours`` 周期安排一次网址课表重新下载。"""
        conf = self._conf()
        hours = float(conf.source.url_refresh_hours)
        if hours <= 0:
            return  # 0 = 关闭自动刷新
        moment = now or datetime.now()
        if self._last_url_refresh is not None:
            if (moment - self._last_url_refresh).total_seconds() < hours * 3600:
                return
        self._last_url_refresh = moment
        self._schedule_url_refresh()

    def _schedule_url_refresh(self) -> None:
        """把自动刷新丢到后台任务，**绝不阻塞调用方**（理由同节假日刷新）。"""
        if self._url_refresh_task is not None and not self._url_refresh_task.done():
            return  # 单飞：已有刷新在进行
        self._url_refresh_task = asyncio.create_task(
            self._run_url_refresh(), name="class-schedule-url-refresh"
        )

    async def _run_url_refresh(self) -> None:
        try:
            changed = await self._ensure_url_refresh()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning(f"{LOG_PREFIX} 课表自动刷新异常: {exc}")
            return
        try:
            await self._notify_url_refresh(changed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning(f"{LOG_PREFIX} 课表更新通知失败: {exc}")

    async def _ensure_url_refresh(self) -> list[str]:
        """重新下载全部网址课表；有变动的覆盖文件并返回变动文件名列表。

        只在 ``/课表导入`` 时记录过来源的文件会被刷新（本地文件与聊天文件
        不联网）。没变化就什么都不动；下载失败保留旧课表，坏内容会被
        :meth:`save_ics <course_source.CourseRepository.save_ics>` 的解析校验
        挡在落盘之前，旧文件不受影响。调用方请用 :meth:`_schedule_url_refresh`。
        """
        repo = self._repo
        if repo is None:
            return []
        async with self._url_lock:
            conf = self._conf()
            sources = repo.url_sources()
            if not sources:
                return []
            changed: list[str] = []
            for filename, info in sources.items():
                url = str(info.get("url") or "").strip()
                if not url:
                    continue
                try:
                    text = await fetch_ics(
                        url,
                        timeout=int(conf.source.url_timeout_seconds),
                        max_bytes=int(conf.source.max_ics_kb) * 1024,
                    )
                except (UnsafeUrlError, FetchError) as exc:
                    self._warn_url_refresh_once(
                        filename, f"自动刷新 {filename} 失败：{exc}"
                    )
                    continue
                # 这次能连上了：清掉该文件的失败告警，免得修好之后还留着旧提示
                self._url_refresh_warned.pop(filename, None)
                fingerprint = content_fingerprint(text)
                if fingerprint == info.get("fingerprint"):
                    continue  # 内容没变，不动文件也不打扰
                try:
                    result = await asyncio.to_thread(repo.save_ics, text, filename)
                except ValueError as exc:
                    # 远端返回了坏内容（比如错误页）：旧课表原样保留，
                    # 指纹不更新，下个周期会再试
                    self._warn_url_refresh_once(
                        filename,
                        f"自动刷新 {filename} 拿到的内容不是有效课表：{exc}",
                    )
                    continue
                repo.record_url_source(filename, url, fingerprint)
                changed.append(filename)
                self.ctx.logger.info(
                    f"{LOG_PREFIX} 课表自动更新：{filename}"
                    f"（{result.event_count} 条课程，来源={url}）"
                )
            return changed

    async def _notify_url_refresh(self, changed: list[str]) -> None:
        """网址课表自动更新后告知提醒会话——静默更新等于没更新。"""
        if not changed:
            return
        repo = self._repo
        targets = [
            item
            for item in await self._resolve_user_id_targets(self._subscriptions())
            if self._should_deliver(item)
        ]
        if not targets:
            self.ctx.logger.info(
                f"{LOG_PREFIX} 课表已自动更新但没有任何提醒会话，不发送通知"
            )
            return
        count = len(repo.events) if repo else 0
        names = "、".join(changed)
        facts = (
            f"网址导入的课表刚刚自动更新了（{len(changed)} 个文件：{names}），"
            f"现在共有 {count} 条课程。用一两句话告诉用户课表有变动并已同步，"
            "上课提醒会按新课表执行。"
        )
        fixed_text = f"🔄 课表自动更新：{names}（当前共 {count} 条课程）"
        # 与提醒同一套纪律：文案只生成一次，发给全部会话
        style = self._reply_style_for("url_refresh")
        persona_text = await self._persona_say(facts) if style == "persona" else ""
        for item in targets:
            await self._deliver(
                item.stream_id,
                facts,
                reason="url_refresh",
                fixed_text=fixed_text,
                persona_text=persona_text,
            )

    def _warn_url_refresh_once(self, key: str, reason: str) -> None:
        """同一个文件的刷新失败只告警一次（成功后清除，可再次提示）。"""
        if key in self._url_refresh_warned:
            return
        self._url_refresh_warned[key] = None
        while len(self._url_refresh_warned) > 50:
            self._url_refresh_warned.pop(next(iter(self._url_refresh_warned)))
        self.ctx.logger.warning(
            f"{LOG_PREFIX} {reason}；保留旧课表继续提醒，下个周期再试"
        )

    def _should_skip_off_day(self, day: date) -> bool:
        """该日是否因为放假而不提醒。

        没有该年份数据时返回 ``False``（照常提醒）——fail-open，见模块文档。

        ``holiday.skip_off_days`` 只管**法定节假日**；``extra_dates`` 是用户逐条写下的
        名单（寒暑假、校历假日），始终生效——否则配了 extra_dates 却因为主开关是关的
        而静默失效，状态页还照旧显示"自定假日 N 条"，只有比对文案才发现没生效。
        """
        calendar = self._holidays
        if calendar is None or calendar.is_empty():
            return False
        if calendar.is_excluded(day):
            return False  # 用户显式声明这天要上课，优先于自定假日
        if calendar.is_user_extra(day):
            return True
        if not self._conf().holiday.skip_off_days:
            return False
        return calendar.is_off_day(day)

    def _holiday_marks(self, day: date) -> str:
        """课表列表里的假日标记，例如 `` 🎉元旦（放假）``。"""
        conf = self._conf()
        calendar = self._holidays
        if calendar is None or calendar.is_empty():
            return ""
        if self._should_skip_off_day(day):
            return f" 🎉{calendar.name_of(day)}放假"
        # 调休上班日只来自数据源，所以只在用数据源（skip_off_days）时才标
        if conf.holiday.skip_off_days and calendar.is_makeup_workday(day):
            return f" 🔁{calendar.name_of(day)}调休上班"
        return ""

    def _holiday_status_lines(self) -> list[str]:
        """``/课表假日`` 与 ``/课表状态`` 共用的数据状态说明。"""
        conf = self._conf()
        # 用空日历兜底，省掉一路 None 判断
        calendar = self._holidays or HolidayCalendar()
        now = datetime.now()
        lines = [
            f"　节假日不提醒：{'开启' if conf.holiday.skip_off_days else '关闭'}",
        ]
        days = calendar.day_count
        years = calendar.years
        if days:
            lines.append(
                f"　数据：{len(years)} 个年份（{'、'.join(str(y) for y in years)}），"
                f"共 {days} 天"
            )
        elif calendar.extra_count:
            lines.append("　数据：未载入数据源，仅使用自定假日")
        else:
            lines.append("　数据：尚未载入（此时不会跳过任何提醒）")
        if calendar.extra_count:
            lines.append(f"　自定假日：{calendar.extra_count} 条")

        # 缺哪一年是最要紧的信息，必须无条件报出来（不能因为"整体为空"就吞掉）：
        # 区分"尚未公布"（常态）与"真的没拿到"（需要注意）
        uncovered = calendar.uncovered_years(
            [date(now.year + offset, 1, 1) for offset in range(HOLIDAY_YEAR_SPAN)]
        )
        unpublished = [year for year in uncovered if year in self._holiday_unpublished]
        missing = [year for year in uncovered if year not in self._holiday_unpublished]
        if unpublished:
            lines.append(
                f"　{('、'.join(str(y) for y in unpublished))} 年放假安排尚未公布，"
                "不影响提醒（公布后会自动补上）"
            )
        if missing:
            lines.append(
                f"　⚠️ 缺少 {'、'.join(str(y) for y in missing)} 年数据，"
                "这几年不会跳过节假日提醒"
            )
        if days or calendar.extra_count:
            today = now.date()
            lines.append(f"　今天：{calendar.label_of(today) or '平日'}")
        return lines

    def _access_summary(self, conf: ClassScheduleConfig) -> str:
        """名单模式的展示文本。"""
        mode = conf.access.mode
        count = len(conf.access.entries or [])
        if mode == "off":
            return "关闭（所有会话可用）"
        if mode == "whitelist":
            return f"白名单 {count} 项（名单外的会话不可用）"
        if mode == "blacklist":
            return f"黑名单 {count} 项（名单内的会话被拒绝）"
        return mode

    def _scope_summary(self) -> str:
        """适用范围的展示文本（带被跳过的群聊数量）。"""
        scope = str(self._conf().access.chat_scope or "").strip().lower()
        text = {
            "private": "仅私聊",
            "group": "仅群聊",
            "both": "私聊 + 群聊",
        }.get(scope, scope or "未知")
        skipped = len(self._skipped_group_subscriptions())
        if skipped:
            text += f"（{skipped} 个群聊订阅被跳过）"
        return text

    # ── 等待课表文件（/课程解析 引导） ────────────────────

    def _arm_awaiting_ics(self, stream_id: str) -> int:
        """标记该会话正在等课表文件，返回等待分钟数。"""
        minutes = max(1, int(self._conf().file_import.arm_minutes))
        self._awaiting_ics[stream_id] = datetime.now() + timedelta(minutes=minutes)
        return minutes

    def _clear_awaiting_ics(self, stream_id: str) -> bool:
        """清除等待状态，返回清除前是否处于等待中（且未过期）。"""
        deadline = self._awaiting_ics.pop(stream_id, None)
        return bool(deadline and deadline > datetime.now())

    def _awaiting_stream_of(self, message: Any) -> str:
        """从消息里取会话 ID，用于判断是否处于等待状态。"""
        return identity_from_kwargs({"message": message}).stream_id

    def _is_awaiting_ics(self, message: Any) -> bool:
        """该会话是否正在等课表文件（过期即视为不在等待）。"""
        stream_id = self._awaiting_stream_of(message)
        if not stream_id:
            return False
        deadline = self._awaiting_ics.get(stream_id)
        if deadline is None:
            return False
        if deadline <= datetime.now():
            self._awaiting_ics.pop(stream_id, None)
            return False
        return True

    # ── 聊天文件导入 ──────────────────────────────────────

    @HookHandler(
        "chat.receive.after_process",
        name="schedule_chat_scope_probe",
        description="记住每个会话是私聊还是群聊，供适用范围（chat_scope）判定",
        mode=HookMode.OBSERVE,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def record_chat_type_handler(self, **kwargs: Any) -> None:
        """记下会话类型，给拿不到消息体的注入 hook 用。

        独立成一个 handler 而不是塞进文件导入：文件导入会因为
        ``file_import.enabled=false`` 或"这条消息没带文件"提前返回，
        而适用范围判定需要**每条消息**都记。纯内存一次字典写入，不阻塞链路。

        会话 ID 从整个 kwargs 取：麦麦真实下发时它在 ``message.session_id``，
        而命令/Hook 也可能把它放在顶层 ``stream_id``，两种都得认。
        """
        self._remember_chat_type(identity_from_kwargs(kwargs))

    @HookHandler(
        "chat.receive.after_process",
        name="schedule_file_intake",
        description="接收聊天里发来的 ics 课表文件并导入",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_file_message(self, **kwargs: Any) -> None:
        """聊天里发来 .ics 文件就自动导入，回执交给 replyer 决定说法。

        为什么用 Hook 而不是 EventHandler：麦麦 1.2.0 里消息类核心事件
        （``on_message`` / ``post_send`` 等）的派发全是**注释掉的**，只有
        ``ON_START`` / ``ON_STOP`` 真在发；而 ``chat.receive.*`` 这两个命名 Hook
        是活代码，每条入站消息都会经过。

        用 ``OBSERVE`` 模式：不阻塞消息链路，下载/落盘/回执全丢到后台任务，
        绝不让入站管线等人（下载超时会把适配器一起拖超时）。
        """
        conf = self._conf()
        if not conf.plugin.enabled or not conf.file_import.enabled:
            return

        message = kwargs.get("message")
        candidates = extract_file_candidates(message)
        if not candidates:
            return

        schedule_files = [
            item for item in candidates if is_schedule_filename(item.name, item.mime_type)
        ]
        if not schedule_files:
            # 只在"用户刚发过 /课程解析 正等着文件"时才出声：那种情况下发错文件
            # 是明确可纠正的，静默忽略会让人以为插件坏了；平时则保持安静不打扰
            if self._is_awaiting_ics(message):
                self.ctx.logger.info(
                    f"{LOG_PREFIX} 等待课表文件期间收到非 ics 文件，已提示用户"
                )
                self._spawn_intake([], message, wrong_files=candidates)
            else:
                self.ctx.logger.debug(
                    f"{LOG_PREFIX} 收到 {len(candidates)} 个文件但不是课表，忽略"
                )
            return

        self.ctx.logger.info(
            f"{LOG_PREFIX} 收到课表文件：{'、'.join(item.display for item in schedule_files)}"
        )
        self._spawn_intake(schedule_files, message)

    def _spawn_intake(
        self,
        candidates: list[Any],
        message: Any,
        *,
        wrong_files: list[Any] | None = None,
    ) -> None:
        """把文件导入丢到后台任务，并持有引用以便卸载时取消。"""
        task = asyncio.create_task(
            self._process_schedule_files(candidates, message, wrong_files=wrong_files),
            name="class-schedule-file-intake",
        )
        self._intake_tasks.add(task)
        task.add_done_callback(self._intake_tasks.discard)

    async def _process_schedule_files(
        self,
        candidates: list[Any],
        message: Any,
        *,
        wrong_files: list[Any] | None = None,
    ) -> None:
        """下载/解析/落盘/回执，全程在后台任务里跑。"""
        conf = self._conf()
        identity = identity_from_kwargs({"message": message})
        stream_id = identity.stream_id
        if not stream_id:
            self.ctx.logger.warning(f"{LOG_PREFIX} 文件消息缺少会话 ID，无法回执")
            return
        # 名单外的会话不当场处理（与命令一致），也不回执，避免打扰
        if conf.access.apply_to_commands and self._evaluate(identity).denied:
            self.ctx.logger.info(
                f"{LOG_PREFIX} 忽略名单外会话发来的课表文件（{identity.display}）"
            )
            return
        # 适用范围外的会话同样忽略：群里发来的课表不该被导入，
        # 这既避免打扰，也避免课表数据来自一个你不打算使用的会话
        scope_reason = self._scope_denial(identity)
        if scope_reason is not None:
            self.ctx.logger.info(
                f"{LOG_PREFIX} 忽略适用范围外的课表文件（{identity.display}）：{scope_reason}"
            )
            return

        awaiting = self._is_awaiting_ics(message)

        # 用户正等着导入、却发了别的文件：明确说清要发什么
        # （不清等待状态，TA 接着发对的课表仍然有效）
        if wrong_files:
            names = "、".join(item.name or "未命名文件" for item in wrong_files)
            await self._deliver(
                stream_id,
                f"用户刚发来的是「{names}」，但这不是课表文件。你刚才让 TA 发 .ics 课表，"
                "请说明需要教务系统导出的 .ics 文件（iCalendar 格式），"
                "直接把文件发到聊天里即可",
                reason="file_import",
                fixed_text=(
                    f"❌「{names}」不是课表文件。请发教务系统导出的 .ics 文件"
                    "（iCalendar 格式），直接发到聊天里就行"
                ),
            )
            return

        repo = self._repo
        if repo is None:
            return

        # 真正要导入了才清掉等待状态
        self._clear_awaiting_ics(stream_id)
        for candidate in candidates:
            outgoing = await self._import_chat_file(candidate, repo, conf, identity=identity)
            if outgoing is None:
                continue
            facts = outgoing.facts
            if awaiting:
                facts += "（这是 TA 刚用 /课程解析 请求的导入）"
            await self._deliver(
                stream_id, facts, reason="file_import", fixed_text=outgoing.fixed
            )

    async def _import_chat_file(
        self,
        candidate: Any,
        repo: CourseRepository,
        conf: ClassScheduleConfig,
        *,
        identity: Any = None,
    ) -> "OutgoingMessage | None":
        """导入一个聊天文件，返回要发出去的内容（失败也返回说明，便于告知用户）。

        ``identity`` 是发件会话的身份：给了且导入成功，就按
        ``file_import.auto_subscribe`` 自动把该会话登记为提醒对象，
        回执话术随之改变（不再引导去发 /课表订阅）。
        """
        name = candidate.display
        try:
            text, source = await load_candidate_text(
                candidate,
                timeout=int(conf.file_import.timeout_seconds),
                max_bytes=int(conf.file_import.max_kb) * 1024,
                allow_hosts=conf.file_import.allowed_hosts,
            )
        except FileIntakeError as exc:
            self.ctx.logger.warning(f"{LOG_PREFIX} 取课表文件失败：{exc}")
            return OutgoingMessage(
                fixed=f"❌ 课表文件「{name}」没能取到：{exc}",
                facts=f"用户刚发来的课表文件「{name}」没能读取：{exc}。请把原因告诉 TA。",
            )

        fingerprint = content_fingerprint(text)
        if fingerprint in self._imported_fingerprints:
            self.ctx.logger.info(f"{LOG_PREFIX} 同一份课表重复发送，已忽略")
            return OutgoingMessage(
                fixed=f"ℹ️「{name}」和上一次内容相同，已经导过了，没有重复导入。",
                facts=(
                    f"用户又发了一遍同一份课表「{name}」，内容没有变化，"
                    "已经导过了，不用重复处理。请告诉 TA 无需重复发送。"
                ),
            )

        try:
            result = await asyncio.to_thread(
                repo.save_ics, text, chat_import_filename(candidate.name)
            )
        except ValueError as exc:
            self.ctx.logger.warning(f"{LOG_PREFIX} 聊天文件不是有效课表：{exc}")
            return OutgoingMessage(
                fixed=f"❌「{name}」不是有效的课表文件：{exc}",
                facts=f"用户发来的「{name}」不是有效的课表文件：{exc}。请说明并让 TA 重新导出。",
            )
        except OSError as exc:
            self.ctx.logger.warning(f"{LOG_PREFIX} 保存聊天课表失败：{exc}")
            return OutgoingMessage(
                fixed=f"❌ 保存「{name}」失败：{exc}",
                facts=f"保存「{name}」失败：{exc}。请把故障告诉 TA。",
            )

        self._remember_import(fingerprint)
        self.ctx.logger.info(
            f"{LOG_PREFIX} 已从聊天文件导入课表：{result.filename}"
            f"（{result.event_count} 条课程，来源={source}）"
        )
        action = "更新" if result.replaced else "导入"
        subscribed = self._auto_subscribe(identity) if identity is not None else False
        if subscribed:
            subscribe_hint = (
                "本会话已自动订阅上课提醒，到点会在这里收到；"
                "想改提前量发 /课表提前 30，不想收了发 /课表退订。"
            )
        else:
            subscribe_hint = "想收上课提醒，在需要提醒的会话里发 /课表订阅。"
        return OutgoingMessage(
            fixed=(
                f"✅ 已{action}课表「{name}」：解析出 {result.event_count} 条课程，"
                f"当前共 {len(repo.events)} 条。\n{subscribe_hint}"
            ),
            facts=(
                f"用户刚发来课表文件「{name}」，已经成功{action}："
                f"解析出 {result.event_count} 条课程，目前共有 {len(repo.events)} 条课程。"
                f"告诉用户结果，并提醒 TA：{subscribe_hint}"
            ),
        )

    def _remember_import(self, fingerprint: str) -> None:
        """记住最近导入过的内容指纹，避免同一份文件反复导。"""
        self._imported_fingerprints.append(fingerprint)
        if len(self._imported_fingerprints) > MAX_IMPORT_FINGERPRINTS:
            self._imported_fingerprints.pop(0)

    # ── 自然语言问课：把课表注入规划器 ────────────────────

    @HookHandler(
        "maisaka.planner.before_request",
        name="schedule_nl_inject",
        description="在规划器请求前注入课表，让它能用人设自然回答上课相关提问",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_schedule_handler(self, **kwargs: Any) -> dict[str, Any]:
        """把课表作为一条 system 上下文注入规划器。

        为什么走注入而不是只靠工具：工具要模型自己决定调用，实测经常不调；
        注入之后课表就在上下文里，模型自然能答「明天几点有课」。

        为什么默认只在相关话题注入：这个 hook 每条消息都会触发，无脑注入等于
        每条消息都多几百 token，还可能让模型跑题。

        注意：hook 默认超时只有 6 秒，这里只做内存里的字符串拼接，不碰磁盘、
        不联网（课表由 on_load/tick 维护在内存里）。
        """
        conf = self._conf()
        if not conf.plugin.enabled or not conf.nl_query.enabled:
            return {"action": "continue"}

        items = kwargs.get("items")
        if not isinstance(items, list):
            return {"action": "continue"}

        session_id = str(kwargs.get("session_id") or "").strip()
        if not session_id:
            return {"action": "continue"}
        # 适用范围：只在私聊（默认）时注入。这里拿不到消息体，类型靠
        # record_chat_type_handler 记下的表来判断，所以要先记后用
        identity = self._identity_for_stream(session_id)
        scope_reason = self._scope_denial(identity)
        if scope_reason is not None:
            self.ctx.logger.debug(
                f"{LOG_PREFIX} 不注入课表（session={session_id}）：{scope_reason}"
            )
            return {"action": "continue"}
        # 名单外的会话不注入（与命令、文件导入一致）
        if conf.access.apply_to_commands and self._evaluate(identity).denied:
            return {"action": "continue"}

        if conf.nl_query.mode == "on_topic":
            recent = collect_item_text(items)
            if not looks_like_schedule_question(recent) and not self._matches_extra_keyword(
                recent, conf.nl_query.extra_keywords
            ):
                return {"action": "continue"}

        text = self._build_injection_text(conf)
        if not text:
            return {"action": "continue"}

        result = dict(kwargs)
        result["items"] = inject_into_items(items, text)
        self.ctx.logger.info(
            f"{LOG_PREFIX} 已注入课表上下文（session={session_id}，{len(text)} 字）"
        )
        return {"action": "continue", "modified_kwargs": result}

    @staticmethod
    def _matches_extra_keyword(text: str, keywords: Iterable[str]) -> bool:
        """是否命中用户自定义的触发词。"""
        content = str(text or "")
        return any(
            str(word).strip() and str(word).strip() in content for word in keywords or []
        )

    def _build_injection_text(self, conf: ClassScheduleConfig) -> str:
        """构造注入文本；课表为空时返回空串（没数据就别注入）。"""
        repo = self._repo
        if repo is None:
            return ""
        events = repo.events
        if not events:
            return ""
        return build_inject_text(
            events,
            now=datetime.now(),
            days=int(conf.nl_query.days),
            max_lines=int(conf.nl_query.max_lines),
            holiday_label=self._holiday_name_for,
            expander=expand_occurrences,
        )

    def _holiday_name_for(self, day: date) -> str:
        """给注入文本用的放假说明（如「中秋节放假」），无则空串。"""
        conf = self._conf()
        if not conf.holiday.skip_off_days:
            return ""
        calendar = self._holidays
        if calendar is None or calendar.is_empty():
            return ""
        if calendar.is_off_day(day):
            return f"{calendar.name_of(day)}放假"
        if calendar.is_makeup_workday(day):
            return f"{calendar.name_of(day)}调休上班"
        return ""

    # ── LLM 工具 ──────────────────────────────────────────

    @Tool(
        "query_class_schedule",
        brief_description="查询课程表",
        detailed_description=(
            "查询用户导入的课程表，返回课程名称、时间与地点。"
            "当用户询问「今天有什么课」「下节课是什么」「明天要上什么」等"
            "课表相关问题时调用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="scope",
                param_type=ToolParamType.STRING,
                description="查询范围：today 今天、tomorrow 明天、week 今天起七天",
                required=False,
                enum_values=["today", "tomorrow", "week"],
                default="today",
            ),
        ],
    )
    async def handle_query_schedule(self, **kwargs: Any) -> str:
        # 工具调用只拿到 LLM 给的参数，**没有任何会话信息**（麦麦的
        # component_query._build_tool_executor 只传 function_args），所以名单
        # 无法在这里判定。能提供的控制只有一个显式开关，见 README「七」。
        conf = self._conf()
        if not conf.access.tool_query_enabled:
            return "（课表查询功能已在插件配置中关闭）"

        scope = str(kwargs.get("scope") or "today").strip().lower()
        now = datetime.now()

        if scope == "tomorrow":
            block = self._format_day(now + timedelta(days=1))
            return block or "明天没有课。"
        if scope == "week":
            return self._schedule_summary(7)
        return self._schedule_summary(1)


def create_plugin() -> ClassSchedulePlugin:
    """SDK 入口：返回插件实例。"""
    return ClassSchedulePlugin()
