"""插件端到端冒烟测试。

用假的 ``ctx`` 替换 SDK 运行时，验证真正的行为链路：
写入 ics 文件 → 解析 → tick 判定 → 调用 ``ctx.send``。

覆盖需求主路径：「上课前 20 分钟发消息提醒」「提前量可自定义」
「按会话单独设置提前量」「群聊与私聊都能作为提醒对象」「白/黑名单」。
"""

import asyncio
import base64
import json
import logging
import time
import unittest
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import _bootstrap  # noqa: F401  —— 注册插件包

from class_schedule import file_intake as intake_module
from class_schedule import plugin as plugin_module
from class_schedule.access import ChatIdentity
from class_schedule.course_source import CourseRepository
from class_schedule.holidays import write_cache
from class_schedule.netutil import FetchError
from class_schedule.plugin import MAX_LIST_LINES, ClassSchedulePlugin
from class_schedule.store import PluginState
from class_schedule.study_notes import StudyNoteStore


def ics_text_with(day: datetime, *, hour: int, summary: str, uid: str) -> str:
    """构造一份只有一个事件的 ics 文本（用于伪造网址下载结果）。"""
    start = day.replace(hour=hour, minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=100)
    return (
        "BEGIN:VCALENDAR\n"
        "BEGIN:VEVENT\n"
        f"UID:{uid}\n"
        f"SUMMARY:{summary}\n"
        f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}\n"
        f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )


class FakeSend:
    """记录发送内容，并可模拟发送失败。"""

    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.customs: list[tuple[str, dict]] = []
        self.text_fails = False
        self.custom_supported = False

    async def text(self, text: str, stream_id: str, **kwargs) -> bool:
        if self.text_fails:
            return False
        self.texts.append((stream_id, text))
        return True

    async def custom(self, custom_type: str, data: dict, stream_id: str, **kwargs) -> bool:
        if not self.custom_supported:
            return False
        self.customs.append((stream_id, data))
        return True

    def streams(self) -> list[str]:
        """收到过消息的会话 ID 列表。"""
        return [item[0] for item in self.texts]

    def last_text(self) -> str:
        """最后一条消息内容。"""
        return self.texts[-1][1] if self.texts else ""


class FakePaths:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.runtime_dir = data_dir / "temp"


class FakeMaisaka:
    """记录主动开口（replyer）调用，并可模拟被拒."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.result: Any = {"success": True}
        self.raise_error = False

        class _Proactive:
            def __init__(self, outer: "FakeMaisaka") -> None:
                self._outer = outer

            async def trigger(self, stream_id: str, intent: str, **kwargs: Any) -> Any:
                if self._outer.raise_error:
                    raise RuntimeError("测试：replyer 不可用")
                self._outer.calls.append(
                    (stream_id, intent, str(kwargs.get("reason") or ""))
                )
                return self._outer.result

        self.proactive = _Proactive(self)


class FakeLlm:
    """记录 LLM 调用；可模拟失败或变慢。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.response = "按人设生成的一句话"
        self.fail = False
        #: 模拟"模型很慢"（秒）；配合 persona_timeout_seconds 验证超时兜底
        self.delay_seconds = 0.0

    async def generate(self, prompt, model="", temperature=None, max_tokens=None, **kw):
        self.prompts.append(str(prompt))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.fail:
            return {"success": False, "error": "测试：模型不可用"}
        return {"success": True, "response": self.response}


class FakeMessage:
    """记录最近消息查询，可用来模拟"bot 是否发言过"。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.recent: list[dict] = []
        self.raise_error = False
        #: 模拟查询卡住（秒）
        self.delay_seconds = 0.0

    async def get_recent(self, chat_id: str, limit: int = 10):
        self.calls.append((chat_id, limit))
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.raise_error:
            raise RuntimeError("测试：查询失败")
        return self.recent


class FakeConfig:
    """模拟宿主全局配置（人设、bot 账号）。"""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {
            "bot.nickname": "卡斯",
            "personality.personality": "嘴硬但贴心的猫娘",
            "personality.reply_style": "简短、口语",
            "bot.qq_account": "10000",
        }

    async def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


class FakeCtx:
    def __init__(self, data_dir: Path) -> None:
        self.paths = FakePaths(data_dir)
        self.send = FakeSend()
        self.maisaka = FakeMaisaka()
        self.llm = FakeLlm()
        self.message = FakeMessage()
        self.config = FakeConfig()
        self.logger = logging.getLogger("test.class-schedule")


def write_ics(
    path: Path, start: datetime, *, summary="高等数学", location="教三-201", uid="c1"
) -> None:
    end = start + timedelta(minutes=100)
    text = (
        "BEGIN:VCALENDAR\n"
        "BEGIN:VEVENT\n"
        f"UID:{uid}\n"
        f"SUMMARY:{summary}\n"
        f"DTSTART;TZID=Asia/Shanghai:{start.strftime('%Y%m%dT%H%M%S')}\n"
        f"DTEND;TZID=Asia/Shanghai:{end.strftime('%Y%m%dT%H%M%S')}\n"
        f"LOCATION:{location}\n"
        "END:VEVENT\n"
        "END:VCALENDAR\n"
    )
    path.write_text(text, encoding="utf-8")


def build_config(**sections) -> dict:
    """构造一份配置，自动带上 SDK 必需的 ``[plugin]`` 节。

    ``plugin.config_version`` 缺失时 SDK 会直接拒绝加载配置，
    所以任何测试配置都必须包含它（真实配置由 Runner 生成，同样带这一节）。
    """
    config: dict = {
        "plugin": {"config_version": "1.0.0", "enabled": True},
        # 默认用 fixed 投递：绝大多数用例关心的是"送达/去重/窗口"等机制，
        # 把回复风格固定住才不会让断言随它漂移；persona 有专项用例
        "reply": {"style": "fixed", "ack_style": "fixed"},
        # 默认放开适用范围：多数用例在测别的机制，不该被"只走私聊"挡住；
        # 适用范围本身（含插件的真实默认值 = private）有专项用例
        "access": {"chat_scope": "both"},
    }
    for name, values in sections.items():
        config.setdefault(name, {}).update(values)
    return config


def group_kwargs(
    stream_id: str = "group-stream",
    *,
    group_id: str = "123456",
    group_name: str = "高数三班",
    user_id: str = "654321",
    nickname: str = "小明",
) -> dict:
    """伪造一条群消息的命令 kwargs（含群号与用户号）。"""
    return {
        "stream_id": stream_id,
        "message": {
            "chat_info": {
                "group_info": {"group_id": group_id, "group_name": group_name}
            },
            "user_info": {"user_id": user_id, "user_nickname": nickname},
        },
    }


def private_kwargs(
    stream_id: str = "private-stream",
    *,
    user_id: str = "654321",
    nickname: str = "小明",
) -> dict:
    """伪造一条私聊消息的命令 kwargs（只有用户号，没有群号）。"""
    return {
        "stream_id": stream_id,
        "message": {"user_info": {"user_id": user_id, "user_nickname": nickname}},
    }


class _AttrPatch:
    """替换模块属性的上下文管理器，退出时还原。"""

    def __init__(self, module, name: str) -> None:
        self._module = module
        self._name = name
        self._original = getattr(module, name)

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc_info) -> bool:
        setattr(self._module, self._name, self._original)
        return False


async def _no_network(url: str, *, timeout: int = 20, max_bytes: int = 0) -> str:
    """测试环境的兜底：任何远端抓取直接失败，避免用例偷偷联网。"""
    raise FetchError("测试环境不联网")


class PluginSmokeTest(unittest.IsolatedAsyncioTestCase):
    """构造一个只差提醒循环的插件实例，直接驱动 ``_tick``。

    测试**绝不联网**：``setUp`` 把抓取入口换成"立刻失败"，需要"下载成功"的
    用例用 :meth:`fake_fetch` / :meth:`fake_holiday_text` 显式替换。
    否则 ``on_load``/``_tick`` 里的节假日刷新会真的去请求公网，测试又慢又不稳。
    """

    def setUp(self) -> None:
        # 真实实现留在手边：地址校验发生在联网之前，所以"拒绝非法地址"的用例
        # 可以安全地用真实函数（见 _use_real_fetch_validation）
        self._real_fetch_ics = plugin_module.fetch_ics
        self._real_fetch_text = plugin_module.fetch_text
        self._guards = [
            _AttrPatch(plugin_module, "fetch_ics"),
            _AttrPatch(plugin_module, "fetch_text"),
            # file_intake 在导入时就绑定了 fetch_text，补丁要单独打一份
            _AttrPatch(intake_module, "fetch_text"),
        ]
        for guard in self._guards:
            guard.__enter__()
        plugin_module.fetch_ics = _no_network  # type: ignore[assignment]
        plugin_module.fetch_text = _no_network  # type: ignore[assignment]
        intake_module.fetch_text = _no_network  # type: ignore[assignment]

    def tearDown(self) -> None:
        for guard in reversed(self._guards):
            guard.__exit__(None, None, None)

    def fake_ics_text(self, text: str) -> None:
        """把聊天文件下载换成返回固定内容，并记录 allow_hosts。"""
        calls: list[str] = []
        allows: list[tuple[str, ...]] = []
        self.ics_fetch_calls = calls
        self.ics_fetch_allow_hosts = allows

        async def _fetch(url: str, *, timeout: int = 20, max_bytes: int = 0, allow_hosts=()):
            calls.append(url)
            allows.append(tuple(allow_hosts or ()))
            return text

        intake_module.fetch_text = _fetch  # type: ignore[assignment]

    def _use_real_fetch_validation(self) -> None:
        """改用真实的抓取实现。

        只对"URL 本身非法"的用例安全：``validate_url`` 会在发起请求**之前**抛出，
        因此不会真的联网。
        """
        plugin_module.fetch_ics = self._real_fetch_ics  # type: ignore[assignment]
        plugin_module.fetch_text = self._real_fetch_text  # type: ignore[assignment]
        intake_module.fetch_text = self._real_fetch_text  # type: ignore[assignment]

    def make_plugin(self, data_dir: Path, config: dict | None = None) -> ClassSchedulePlugin:
        plugin = ClassSchedulePlugin()
        plugin._set_context(FakeCtx(data_dir))  # type: ignore[arg-type]  # 模拟 Runner 注入
        plugin.set_plugin_config(config if config is not None else build_config())
        return plugin

    def fake_fetch(self, *responses: str):
        """把 ``fetch_ics`` 换成按序返回的打桩（用于网址导入测试）。"""
        queue = list(responses)

        async def _fetch(url: str, *, timeout: int = 20, max_bytes: int = 0) -> str:
            return queue.pop(0)

        plugin_module.fetch_ics = _fetch  # type: ignore[assignment]
        return _AttrPatch(plugin_module, "fetch_ics")

    def fake_holiday_text(self, text: str = "", *, fail: bool = False) -> None:
        """把 ``fetch_text`` 换成返回固定节假日 JSON 的打桩（默认失败）。"""
        calls: list[str] = []

        async def _fetch(url: str, *, timeout: int = 20, max_bytes: int = 0) -> str:
            calls.append(url)
            if fail:
                raise FetchError("测试：下载失败")
            return text

        plugin_module.fetch_text = _fetch  # type: ignore[assignment]
        self.holiday_fetch_calls = calls

    def prepare_bare(
        self,
        data_dir: Path,
        *,
        config: dict | None = None,
        targets: tuple[str, ...] = (),
    ) -> ClassSchedulePlugin:
        """只装好仓库与状态，不预置任何课表文件（导入类测试用）。"""
        plugin = self.make_plugin(data_dir, config)
        plugin._data_dir = data_dir
        plugin._repo = CourseRepository(data_dir / "ics")
        plugin._repo.ensure_dir()
        plugin._repo.refresh(force=True)
        plugin._state = PluginState()
        # 与 on_load 一致：学习笔记存储挂在数据目录的 notes/ 下
        plugin._notes = StudyNoteStore(data_dir / "notes")
        for stream_id in targets:
            plugin._state.add_subscription(ChatIdentity(stream_id=stream_id))
        return plugin

    def prepare(
        self,
        data_dir: Path,
        *,
        offset_minutes: float = 20,
        config: dict | None = None,
        targets: tuple[str, ...] = ("stream-1",),
        summary: str = "高等数学",
    ) -> ClassSchedulePlugin:
        plugin = self.prepare_bare(data_dir, config=config, targets=targets)
        write_ics(
            data_dir / "ics" / "a.ics",
            datetime.now() + timedelta(minutes=offset_minutes),
            summary=summary,
        )
        plugin._repo.refresh(force=True)  # type: ignore[union-attr]
        return plugin

    # ── 需求主路径 ────────────────────────────────────────

    async def test_reminds_20_minutes_before_by_default(self):
        """默认配置下，20 分钟后开始的课应当立刻被提醒。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20)
            await plugin._tick()

            send: FakeSend = plugin.ctx.send  # type: ignore[assignment]
            self.assertEqual(len(send.texts), 1)
            stream_id, text = send.texts[0]
            self.assertEqual(stream_id, "stream-1")
            self.assertIn("高等数学", text)
            self.assertIn("20 分钟后上课", text)
            self.assertIn("教三-201", text)

    async def test_no_reminder_for_course_far_away(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=120)
            await plugin._tick()
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]

    async def test_reminder_sent_only_once(self):
        """同一个 tick 反复执行（等价于每分钟检查）不会重复轰炸。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20)
            await plugin._tick()
            await plugin._tick()
            await plugin._tick()
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]

    async def test_duplicate_ics_files_send_one_reminder(self):
        """回归：同一份课表被放了两份，不能发两条一样的提醒。

        跨文件重复是必然情况——用户可能重复导入同一个网址，或手动复制了一份。
        """
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20)
            ics_dir = Path(tmp) / "ics"
            write_ics(
                ics_dir / "a-2.ics",
                datetime.now() + timedelta(minutes=20),
                summary="高等数学",
                uid="c1",
            )
            plugin._repo.refresh(force=True)  # type: ignore[union-attr]
            self.assertEqual(plugin._repo.file_count, 2)  # type: ignore[union-attr]

            await plugin._tick()
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]

    # ── 提前量：配置默认 + 按会话覆盖 ─────────────────────

    async def test_custom_lead_from_config(self):
        """配置改成提前 60 分钟：50 分钟后开始的课也要提醒。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=50,
                config=build_config(reminder={"remind_before_minutes": 60}),
            )
            await plugin._tick()
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]
            self.assertIn("50 分钟后上课", plugin.ctx.send.texts[0][1])  # type: ignore[attr-defined]

    async def test_session_lead_overrides_config(self):
        """会话单独设置的提前量优先于配置默认值。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=30,
                config=build_config(reminder={"remind_before_minutes": 60}),
            )
            # 配置说提前 60 分钟，但本会话被单独设成 5 分钟
            plugin._state.set_lead("stream-1", 5)
            await plugin._tick()
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]

    async def test_session_lead_can_enable_reminder(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=5)
            plugin._state.set_lead("stream-1", 10)
            await plugin._tick()
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]

    async def test_each_session_uses_its_own_lead(self):
        """两个会话设不同提前量时，各自在自己的时间点收到提醒且互不吞掉。

        提前 60 分钟的会话在 40 分钟后那节课上就该响，提前 10 分钟的不该响。
        """
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=40,
                targets=("stream-long", "stream-short"),
            )
            plugin._state.set_lead("stream-long", 60)
            plugin._state.set_lead("stream-short", 10)

            await plugin._tick()
            self.assertEqual(plugin.ctx.send.streams(), ["stream-long"])  # type: ignore[attr-defined]

    async def test_two_leads_both_fire_at_their_own_moment(self):
        """同一个会话不能同时用两个提前量；不同会话在各自时点各响一次。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp), offset_minutes=40, targets=("s60", "s10")
            )
            plugin._state.set_lead("s60", 60)
            plugin._state.set_lead("s10", 10)
            await plugin._tick()
            self.assertEqual(plugin.ctx.send.streams(), ["s60"])  # type: ignore[attr-defined]

            fired = set(plugin._state.fired)
            self.assertEqual(len(fired), 1)
            self.assertTrue(fired.pop().endswith("|60"))

    async def test_same_lead_sessions_share_one_reminder(self):
        """两个会话提前量相同时只判定一次、一起发，不重复计算。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20, targets=("s1", "s2"))
            await plugin._tick()
            self.assertEqual(sorted(plugin.ctx.send.streams()), ["s1", "s2"])  # type: ignore[attr-defined]
            self.assertEqual(len(plugin._state.fired), 1)

    async def test_reset_session_lead_falls_back_to_config(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=50,
                config=build_config(reminder={"remind_before_minutes": 60}),
            )
            plugin._state.set_lead("stream-1", 5)
            await plugin._tick()
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]

            plugin._state.set_lead("stream-1", None)  # 恢复跟随配置
            await plugin._tick()
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]

    async def test_short_lead_still_delivers_with_large_check_interval(self):
        """回归：提前量小 + 检查周期大时，整节课曾被静默漏掉。

        到期窗口是「课程开始前 lead 分钟到开始后 grace 秒」，长度必须不短于
        检查周期，否则某一轮 tick 会整段跨过去、这节课永远等不到提醒。
        这里让课程落在"看起来没有 tick 覆盖"的位置，验证后一轮仍能补上。
        """
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=("s1",),
                config=build_config(
                    reminder={"check_interval_seconds": 300, "late_grace_seconds": 60}
                ),
            )
            plugin._state.set_lead("s1", 1)  # 只提前 1 分钟

            start = datetime.now() + timedelta(seconds=150)
            write_ics(Path(tmp) / "ics" / "a.ics", start, uid="c1")
            plugin._repo.refresh(force=True)  # type: ignore[union-attr]

            await plugin._tick()  # 此刻距上课 150 秒 > 60 秒，还不到提醒时机
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]

            # 模拟 300 秒后的那一轮：窗口被放宽到覆盖整个检查周期，必须补发
            later = datetime.now() + timedelta(seconds=300)
            await plugin._deliver_for_lead(plugin._subscriptions(), 1, later, 300)
            self.assertEqual(plugin.ctx.send.streams(), ["s1"])  # type: ignore[attr-defined]

    async def test_no_target_warning_distinguishes_empty_schedule(self):
        """回归：课表为空时不能说"有课程到期"——那不是当前的问题。"""
        with TemporaryDirectory() as tmp:
            # 空课表 + 无订阅：应提示"先导入课表"
            plugin = self.prepare_bare(Path(tmp), targets=())
            plugin._warned_no_target = False
            with self.assertLogs("test.class-schedule", level="WARNING") as captured:
                plugin._warn_about_missing_targets()
            joined = "\n".join(captured.output)
            self.assertIn("还没有导入课表", joined)
            self.assertNotIn("有课程到期", joined)

            # 有课表 + 无订阅：应提示"去订阅"，也不再提"有课程到期"
            with TemporaryDirectory() as tmp2:
                plugin2 = self.prepare(Path(tmp2), offset_minutes=20, targets=())
                plugin2._warned_no_target = False
                with self.assertLogs("test.class-schedule", level="WARNING") as cap2:
                    plugin2._warn_about_missing_targets()
                joined2 = "\n".join(cap2.output)
                self.assertIn("课表已就绪但没有任何提醒会话", joined2)
                self.assertNotIn("有课程到期", joined2)

    async def test_risky_settings_are_logged_at_load(self):
        """配置组合会导致迟到提醒时必须告警，不能悄悄放宽了事。"""
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(
                Path(tmp),
                build_config(
                    reminder={"check_interval_seconds": 3600, "remind_before_minutes": 20}
                ),
            )
            with self.assertLogs("test.class-schedule", level="WARNING") as captured:
                await plugin.on_load()
            try:
                self.assertTrue(
                    any("检查间隔" in line for line in captured.output), captured.output
                )
            finally:
                await plugin.on_unload()

    async def test_numeric_blacklist_entries_are_logged_at_load(self):
        """黑名单写群号但适配器可能不给群号，这条坑必须留痕。"""
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(
                Path(tmp),
                build_config(access={"mode": "blacklist", "entries": ["999999"]}),
            )
            with self.assertLogs("test.class-schedule", level="WARNING") as captured:
                await plugin.on_load()
            try:
                self.assertTrue(
                    any("黑名单" in line for line in captured.output), captured.output
                )
            finally:
                await plugin.on_unload()

    # ── 法定节假日 ────────────────────────────────────────

    def _install_holiday_data(
        self, data_dir: Path, *, dates: dict[str, bool], year: int | None = None
    ) -> None:
        """往缓存目录写一份节假日数据（不联网）。

        ``dates`` 形如 ``{"2026-10-01": True}``，``True`` = 放假、
        ``False`` = 调休上班。
        """
        year = year or datetime.now().year
        payload = {
            "year": year,
            "days": [
                {
                    "name": "测试假日",
                    "date": day,
                    "isOffDay": is_off,
                }
                for day, is_off in dates.items()
            ],
        }
        write_cache(
            data_dir / "holidays", year, json.dumps(payload, ensure_ascii=False)
        )

    def _prepare_on_holiday(self, tmp: str, *, offset_minutes: float = 20):
        """造一个"今天就是法定节假日"的场景，课程在 offset 分钟后开始。"""
        data_dir = Path(tmp)
        now = datetime.now()
        plugin = self.prepare(data_dir, offset_minutes=offset_minutes)
        self._install_holiday_data(data_dir, dates={now.strftime("%Y-%m-%d"): True})
        plugin._reload_holiday_cache()
        return plugin

    async def test_no_reminder_on_statutory_holiday(self):
        """核心行为：法定节假日不提醒上课。"""
        with TemporaryDirectory() as tmp:
            plugin = self._prepare_on_holiday(tmp)
            self.assertTrue(plugin._should_skip_off_day(datetime.now().date()))

            await plugin._tick()
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]
            # 跳过不等于"已提醒"：假期结束后同一天的课不该被永久吞掉
            self.assertEqual(plugin._state.fired, {})

    async def test_reminder_still_sent_on_makeup_workday(self):
        """调休上班日（周末补班）不是假日，照常提醒。"""
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            now = datetime.now()
            plugin = self.prepare(data_dir, offset_minutes=20)
            self._install_holiday_data(
                data_dir, dates={now.strftime("%Y-%m-%d"): False}
            )
            plugin._reload_holiday_cache()

            self.assertFalse(plugin._should_skip_off_day(now.date()))
            await plugin._tick()
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]

    async def test_extra_dates_also_skip(self):
        """校历自定假日（如校庆）同样跳过。"""
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            today = datetime.now().strftime("%Y-%m-%d")
            plugin = self.prepare(
                data_dir,
                offset_minutes=20,
                config=build_config(holiday={"extra_dates": [today]}),
            )
            plugin._reload_holiday_cache()

            await plugin._tick()
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]

    async def test_holiday_skip_can_be_disabled(self):
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            now = datetime.now()
            plugin = self.prepare(
                data_dir,
                offset_minutes=20,
                config=build_config(holiday={"skip_off_days": False}),
            )
            self._install_holiday_data(data_dir, dates={now.strftime("%Y-%m-%d"): True})
            plugin._reload_holiday_cache()

            self.assertFalse(plugin._should_skip_off_day(now.date()))
            await plugin._tick()
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]

    async def test_extra_dates_apply_even_when_skip_off_days_off(self):
        """回归：extra_dates 曾经在主开关关闭时静默失效。

        自定假日（寒暑假、校历假日）是用户逐条写下的名单，与"法定节假日是否
        跳过"是两件事——以前配了 extra_dates 却一天都不生效，状态页还照旧
        显示"自定假日 N 条"，只有比对文案才发现。
        """
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            today = datetime.now().strftime("%Y-%m-%d")
            plugin = self.prepare(
                data_dir,
                offset_minutes=20,
                config=build_config(
                    holiday={"skip_off_days": False, "extra_dates": [today]}
                ),
            )
            plugin._reload_holiday_cache()

            self.assertTrue(plugin._should_skip_off_day(datetime.now().date()))
            await plugin._tick()
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]

    async def test_extra_dates_marked_even_when_skip_off_days_off(self):
        """标记也要跟上，否则列表里看不出这天为什么不提醒。"""
        with TemporaryDirectory() as tmp:
            today = datetime.now().strftime("%Y-%m-%d")
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                config=build_config(
                    holiday={"skip_off_days": False, "extra_dates": [today]}
                ),
            )
            plugin._reload_holiday_cache()

            self.assertIn("放假", plugin._holiday_marks(datetime.now().date()))

    async def test_exclude_dates_beat_extra_dates(self):
        """同一天既在 extra 又在 exclude 时，以"这天要上课"为准。"""
        with TemporaryDirectory() as tmp:
            today = datetime.now().strftime("%Y-%m-%d")
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                config=build_config(
                    holiday={"extra_dates": [today], "exclude_dates": [today]}
                ),
            )
            plugin._reload_holiday_cache()

            self.assertFalse(plugin._should_skip_off_day(datetime.now().date()))
            await plugin._tick()
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]

    async def test_statutory_holiday_still_respects_main_switch(self):
        """主开关仍然管法定节假日：关掉了就照常提醒。"""
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            now = datetime.now()
            plugin = self.prepare(
                data_dir,
                offset_minutes=20,
                config=build_config(holiday={"skip_off_days": False}),
            )
            self._install_holiday_data(data_dir, dates={now.strftime("%Y-%m-%d"): True})
            plugin._reload_holiday_cache()

            self.assertFalse(plugin._should_skip_off_day(now.date()))

    async def test_prune_clears_expired_awaiting_ics(self):
        """回归：等待课表文件的标记以前只在该会话下次说话时才回收。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=())
            plugin._awaiting_ics["ghost"] = datetime.now() - timedelta(minutes=1)
            plugin._awaiting_ics["alive"] = datetime.now() + timedelta(minutes=10)
            plugin._last_prune = None  # 让这一轮真的执行清理

            plugin._maybe_prune(datetime.now())

            self.assertNotIn("ghost", plugin._awaiting_ics)
            self.assertIn("alive", plugin._awaiting_ics)

    async def test_missing_data_does_not_skip(self):
        """拿不到数据时必须照常提醒（fail-open）——漏掉上课日比假期多提醒更糟。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20)
            reloaded = plugin._reload_holiday_cache()
            self.assertTrue(reloaded.is_empty())

            await plugin._tick()
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]

    async def test_unknown_year_does_not_skip(self):
        """缓存里只有别的年份时，今年不能按假日处理。"""
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            plugin = self.prepare(data_dir, offset_minutes=20)
            other_year = datetime.now().year + 3
            self._install_holiday_data(
                data_dir,
                dates={f"{other_year}-01-01": True},
                year=other_year,
            )
            plugin._reload_holiday_cache()
            await plugin._tick()
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]

    async def test_holiday_download_failure_keeps_working(self):
        """下载失败要用旧缓存继续工作，并告警一次（不静默跳过）。"""
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            today = datetime.now()
            self._install_holiday_data(
                data_dir, dates={today.strftime("%Y-%m-%d"): True}
            )
            plugin = self.prepare(data_dir, offset_minutes=20)
            plugin.set_plugin_config(
                build_config(holiday={"refresh_hours": 0})  # 0 = 不按时间过期
            )
            self.fake_holiday_text(fail=True)

            with self.assertLogs("test.class-schedule", level="WARNING") as captured:
                calendar = await plugin._ensure_holiday_data(force=True)
            self.assertTrue(calendar.is_off_day(today.date()))
            self.assertTrue(any("不会跳过" in line for line in captured.output), captured.output)

    async def test_holiday_data_downloaded_and_cached(self):
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            plugin = self.prepare(data_dir, offset_minutes=20)
            year = datetime.now().year
            self.fake_holiday_text(
                json.dumps(
                    {
                        "year": year,
                        "days": [{"name": "元旦", "date": f"{year}-01-01", "isOffDay": True}],
                    },
                    ensure_ascii=False,
                )
            )

            await plugin._ensure_holiday_data(force=True)
            self.assertTrue((data_dir / "holidays" / f"{year}.json").exists())
            self.assertTrue(
                plugin._should_skip_off_day(datetime(year, 1, 1).date())
            )
            self.assertTrue(plugin.ctx.send.texts == [])  # type: ignore[attr-defined]

    async def test_holiday_download_rejects_private_url(self):
        """节假日数据源同样受 SSRF 约束：内网地址不得被请求。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            plugin.set_plugin_config(
                build_config(holiday={"source_url_template": "http://127.0.0.1/{year}.json"})
            )
            self._use_real_fetch_validation()
            with self.assertLogs("test.class-schedule", level="WARNING") as captured:
                await plugin._ensure_holiday_data(force=True)
            self.assertTrue(
                any("不允许" in line for line in captured.output), captured.output
            )

    async def test_holiday_download_rejects_bad_payload(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            self.fake_holiday_text("这不是节假日数据")
            with self.assertLogs("test.class-schedule", level="WARNING") as captured:
                await plugin._ensure_holiday_data(force=True)
            self.assertTrue(
                any("格式不对" in line for line in captured.output), captured.output
            )

    async def test_cached_data_not_refetched_when_fresh(self):
        """缓存新鲜时不该重复下载（含"次年已备好"的两年循环）。"""
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            year = datetime.now().year
            plugin = self.prepare_bare(data_dir)
            # 一份数据覆盖当年与次年，两边的缓存文件都能写成
            self.fake_holiday_text(
                json.dumps(
                    {
                        "year": year,
                        "days": [
                            {"name": "元旦", "date": f"{year}-01-01", "isOffDay": True},
                            {"name": "元旦", "date": f"{year + 1}-01-01", "isOffDay": True},
                        ],
                    },
                    ensure_ascii=False,
                )
            )

            await plugin._ensure_holiday_data(force=True)
            self.assertEqual(len(self.holiday_fetch_calls), 2)  # 当年 + 次年

            await plugin._ensure_holiday_data()  # 两份缓存都还新鲜
            self.assertEqual(len(self.holiday_fetch_calls), 2)

    async def test_year_payload_for_other_year_is_rejected(self):
        """下回来的内容不含请求年份时不能落盘，否则会被当成"已有数据"不再重试。"""
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            year = datetime.now().year
            plugin = self.prepare_bare(data_dir)
            self.fake_holiday_text(
                json.dumps(
                    {
                        "year": year + 5,
                        "days": [{"name": "元旦", "date": f"{year + 5}-01-01", "isOffDay": True}],
                    },
                    ensure_ascii=False,
                )
            )

            with self.assertLogs("test.class-schedule", level="WARNING") as captured:
                await plugin._ensure_holiday_data(force=True)
            self.assertFalse((data_dir / "holidays" / f"{year}.json").exists())
            self.assertTrue(
                any("没有任何" in line for line in captured.output), captured.output
            )

    async def test_holiday_command_reports_status(self):
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            year = datetime.now().year
            plugin = self.prepare_bare(data_dir, targets=("g1",))
            self._install_holiday_data(data_dir, dates={f"{year}-01-01": True})
            plugin._reload_holiday_cache()

            await plugin.handle_holiday(**group_kwargs("g1"))
            text = plugin.ctx.send.last_text()  # type: ignore[attr-defined]
            self.assertIn("节假日不提醒：开启", text)
            self.assertIn(str(year), text)
            self.assertIn("数据来源", text)

    async def test_holiday_command_warns_about_missing_years(self):
        """下载失败（真问题）时要标出缺哪一年并打上警告标记。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("g1",))
            self.fake_holiday_text(fail=True)
            await plugin.handle_holiday(**group_kwargs("g1"))
            text = plugin.ctx.send.last_text()  # type: ignore[attr-defined]
            self.assertIn("尚未载入", text)
            self.assertIn("⚠️ 缺少", text)
            self.assertIn("不会跳过节假日提醒", text)

    async def test_week_view_marks_holiday(self):
        """课表列表要标出假日与调休，用户一眼能看出为什么那天没提醒。"""
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            now = datetime.now()
            plugin = self.prepare(data_dir, offset_minutes=20)
            # 明天也放一节课，才能在列表里看到调休标记
            write_ics(
                data_dir / "ics" / "b.ics",
                now + timedelta(days=1),
                summary="大学物理",
                uid="c2",
            )
            self._install_holiday_data(
                data_dir,
                dates={
                    now.strftime("%Y-%m-%d"): True,
                    (now + timedelta(days=1)).strftime("%Y-%m-%d"): False,
                },
            )
            plugin._repo.refresh(force=True)  # type: ignore[union-attr]
            plugin._reload_holiday_cache()

            text = plugin._schedule_summary(2)
            self.assertIn("🎉测试假日放假", text)
            self.assertIn("🔁测试假日调休上班", text)

    async def test_holiday_marks_empty_when_disabled(self):
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            now = datetime.now()
            plugin = self.prepare(
                data_dir,
                offset_minutes=20,
                config=build_config(holiday={"skip_off_days": False}),
            )
            self._install_holiday_data(data_dir, dates={now.strftime("%Y-%m-%d"): True})
            plugin._reload_holiday_cache()
            self.assertEqual(plugin._holiday_marks(now.date()), "")

    async def test_config_update_applies_extra_dates(self):
        """热更新改了自定假日要立刻生效，否则用户会以为功能坏了。"""
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            today = datetime.now().strftime("%Y-%m-%d")
            plugin = self.prepare(data_dir, offset_minutes=20)
            self.assertFalse(plugin._should_skip_off_day(datetime.now().date()))

            await plugin.on_config_update(
                "self", build_config(holiday={"extra_dates": [today]}), "1.0.0"
            )
            self.assertTrue(plugin._should_skip_off_day(datetime.now().date()))

            await plugin.on_config_update(
                "self", build_config(holiday={"extra_dates": []}), "1.0.0"
            )
            self.assertFalse(plugin._should_skip_off_day(datetime.now().date()))

    async def test_config_update_applies_exclude_dates(self):
        """放假表说放假、学校要补课：排除项能把它翻回来。"""
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            now = datetime.now()
            plugin = self.prepare(data_dir, offset_minutes=20)
            self._install_holiday_data(data_dir, dates={now.strftime("%Y-%m-%d"): True})
            plugin._reload_holiday_cache()
            self.assertTrue(plugin._should_skip_off_day(now.date()))

            await plugin.on_config_update(
                "self",
                build_config(holiday={"exclude_dates": [now.strftime("%Y-%m-%d")]}),
                "1.0.0",
            )
            self.assertFalse(plugin._should_skip_off_day(now.date()))

            await plugin._tick()
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]

    async def test_bad_extra_dates_logged(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                config=build_config(holiday={"extra_dates": ["瞎写的"]}),
            )
            with self.assertLogs("test.class-schedule", level="WARNING") as captured:
                plugin._reload_holiday_cache()
            self.assertTrue(
                any("自定假日格式" in line for line in captured.output), captured.output
            )

    async def test_on_load_does_not_block_on_holiday_download(self):
        """节假日要联网，不能拖慢插件加载。"""
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(Path(tmp))
            self.fake_holiday_text(
                json.dumps({"year": datetime.now().year, "days": [
                    {"name": "x", "date": f"{datetime.now().year}-01-01", "isOffDay": True}]})
            )
            await plugin.on_load()
            try:
                self.assertIsNotNone(plugin._holiday_task)
            finally:
                await plugin.on_unload()
            self.assertIsNone(plugin._holiday_task)

    async def test_holiday_task_cancelled_on_unload(self):
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(Path(tmp))
            # 让下载悬挂，验证 on_unload 能把它取消掉
            async def _hang(url: str, *, timeout: int = 20, max_bytes: int = 0) -> str:
                await asyncio.sleep(30)
                return "{}"

            plugin_module.fetch_text = _hang  # type: ignore[assignment]
            await plugin.on_load()
            task = plugin._holiday_task
            self.assertIsNotNone(task)
            await asyncio.wait_for(plugin.on_unload(), timeout=5)
            self.assertTrue(task.done())  # type: ignore[union-attr]

    async def test_slow_holiday_download_does_not_delay_tick(self):
        """回归：节假日下载曾阻塞在 tick 里，一次慢下载就能整段吃掉到期窗口，
        导致那节课**永久**漏提醒。

        这里把下载打桩成"卡住 30 秒"，然后断言 `_tick` 立刻返回并照常发出提醒。
        """
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            plugin = self.prepare(data_dir, offset_minutes=20)
            # 缓存失效（refresh_hours=0 + 文件不存在），确保 tick 会想去下载
            plugin.set_plugin_config(build_config(holiday={"refresh_hours": 1}))
            plugin._last_holiday_refresh = None

            started: list[str] = []

            async def _hang(url: str, *, timeout: int = 20, max_bytes: int = 0) -> str:
                started.append(url)
                await asyncio.sleep(30)  # 远超一个 tick 周期
                return "{}"

            plugin_module.fetch_text = _hang  # type: ignore[assignment]

            await asyncio.wait_for(plugin._tick(), timeout=3)
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]

    async def test_holiday_refresh_is_single_flight(self):
        """启动任务、tick、命令三方同时想刷新时不该重复下载。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            calls: list[str] = []

            async def _slow(url: str, *, timeout: int = 20, max_bytes: int = 0) -> str:
                calls.append(url)
                await asyncio.sleep(0.2)
                return json.dumps({"year": datetime.now().year, "days": [
                    {"name": "x", "date": f"{datetime.now().year}-01-01", "isOffDay": True}]})

            plugin_module.fetch_text = _slow  # type: ignore[assignment]

            # 连续三次请求刷新：只有第一次真正起任务
            plugin._schedule_holiday_refresh(force=True)
            plugin._schedule_holiday_refresh(force=True)
            plugin._schedule_holiday_refresh(force=True)
            await asyncio.sleep(0.6)
            self.assertEqual(len(calls), 2)  # 当年 + 次年，各一次

    async def test_tick_does_not_wait_for_holiday_refresh(self):
        """tick 里对假日刷新只做"安排"，不等待。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            await plugin._ensure_holiday_data(force=True)  # 先备好数据，避免真下载
            plugin._last_holiday_refresh = None

            hang = asyncio.Event()

            async def _hang(url: str, *, timeout: int = 20, max_bytes: int = 0) -> str:
                await hang.wait()
                return "{}"

            plugin_module.fetch_text = _hang  # type: ignore[assignment]
            try:
                await asyncio.wait_for(plugin._tick(), timeout=2)
            finally:
                hang.set()
                if plugin._holiday_task is not None:
                    plugin._holiday_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await plugin._holiday_task

    async def test_unpublished_year_is_quiet_and_not_retried(self):
        """回归：次年放假安排未公布时，不该报警也不该反复下载。

        实测（真实数据源）：次年 `days` 是空列表，而公布要到当年 11 月前后，
        所以"明年没数据"是大半年的常态。
        """
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            year = datetime.now().year
            self.fake_holiday_text(
                json.dumps({"year": year, "days": []}, ensure_ascii=False)
            )

            with self.assertNoLogs("test.class-schedule", level="WARNING"):
                await plugin._ensure_holiday_data(force=True)

            self.assertIn(year, plugin._holiday_unpublished)
            self.assertIn(year + 1, plugin._holiday_unpublished)

            first = len(self.holiday_fetch_calls)
            await plugin._ensure_holiday_data(force=True)  # 已知未公布，直接跳过
            self.assertEqual(len(self.holiday_fetch_calls), first)

    async def test_unpublished_year_does_not_skip_reminders(self):
        """没数据就照常提醒（fail-open），这条在真实数据缺年份时同样成立。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20)
            year = datetime.now().year
            self.fake_holiday_text(
                json.dumps({"year": year, "days": []}, ensure_ascii=False)
            )
            await plugin._ensure_holiday_data(force=True)

            self.assertFalse(plugin._should_skip_off_day(datetime.now().date()))
            await plugin._tick()
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]

    async def test_holiday_command_reports_unpublished(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("g1",))
            self.fake_holiday_text(
                json.dumps({"year": datetime.now().year, "days": []})
            )
            await plugin._ensure_holiday_data(force=True)
            await plugin.handle_holiday(**group_kwargs("g1"))
            text = plugin.ctx.send.last_text()  # type: ignore[attr-defined]
            # 未公布要说成"尚未公布"，不能吓唬成"数据缺失"
            self.assertIn("尚未公布", text)
            self.assertIn("不影响提醒", text)
            self.assertNotIn("⚠️", text)

    # ── 配置里的用户号目标 ────────────────────────────────

    def fake_stream_lookup(
        self, mapping: dict[str, str], *, raise_error: bool = False
    ) -> None:
        """把 ``ctx.chat.get_stream_by_user_id`` 换成按映射返回的打桩。"""
        calls: list[tuple[str, str]] = []
        self.stream_lookup_calls = calls

        class _Chat:
            async def get_stream_by_user_id(self, user_id: str, platform: str = "qq"):
                calls.append((user_id, platform))
                if raise_error:
                    raise RuntimeError("测试：能力查询失败")
                session = mapping.get(user_id)
                return {"session_id": session} if session else None

        self._pending_chat = _Chat()

    def _install_chat(self, plugin: ClassSchedulePlugin) -> None:
        if getattr(self, "_pending_chat", None) is not None:
            plugin.ctx.chat = self._pending_chat  # type: ignore[union-attr]
            self._pending_chat = None

    async def test_numeric_config_target_resolved_to_session(self):
        """回归：把 QQ 号填进 target_streams 是自然写法，必须能用。

        实测（真机）：MaiBot 的 stream_id 是 32 位十六进制会话 ID，
        不是 QQ 号；拿 QQ 号当 stream_id 会永远发不出去。
        """
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=(),
                config=build_config(target={"target_streams": ["1234567890"]}),
            )
            self.fake_stream_lookup({"1234567890": "0123456789abcdef0123456789abcdef"})
            self._install_chat(plugin)

            await plugin._tick()
            self.assertEqual(
                plugin.ctx.send.streams(),  # type: ignore[attr-defined]
                ["0123456789abcdef0123456789abcdef"],
            )

    async def test_platform_prefixed_config_target(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=(),
                config=build_config(target={"target_streams": ["qq:1234567890"]}),
            )
            self.fake_stream_lookup({"1234567890": "session-1"})
            self._install_chat(plugin)

            await plugin._tick()
            self.assertEqual(plugin.ctx.send.streams(), ["session-1"])  # type: ignore[attr-defined]
            self.assertEqual(self.stream_lookup_calls, [("1234567890", "qq")])

    async def test_hex_session_id_used_as_is(self):
        """32 位十六进制会话 ID 不能被误当成用户号。"""
        with TemporaryDirectory() as tmp:
            session = "0123456789abcdef0123456789abcdef"
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=(),
                config=build_config(target={"target_streams": [session]}),
            )
            self.fake_stream_lookup({})
            self._install_chat(plugin)

            await plugin._tick()
            self.assertEqual(plugin.ctx.send.streams(), [session])  # type: ignore[attr-defined]
            self.assertEqual(self.stream_lookup_calls, [])  # 没去查用户号

    async def test_unresolvable_user_id_warns_once_and_is_skipped(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=(),
                config=build_config(target={"target_streams": ["1234567890"]}),
            )
            self.fake_stream_lookup({})  # 查不到
            self._install_chat(plugin)

            with self.assertLogs("test.class-schedule", level="WARNING") as captured:
                await plugin._tick()
                await plugin._tick()  # 第二次不该重复告警
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]
            warnings = [line for line in captured.output if "找不到对应会话" in line]
            self.assertEqual(len(warnings), 1, captured.output)

    async def test_resolved_target_deduped_against_runtime_subscription(self):
        """配置写 QQ 号、同时又在那个会话订阅过 → 只发一条。"""
        with TemporaryDirectory() as tmp:
            session = "0123456789abcdef0123456789abcdef"
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=(session,),
                config=build_config(target={"target_streams": ["1234567890"]}),
            )
            self.fake_stream_lookup({"1234567890": session})
            self._install_chat(plugin)

            await plugin._tick()
            self.assertEqual(plugin.ctx.send.streams(), [session])  # type: ignore[attr-defined]

    async def test_resolution_cached_across_ticks(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=(),
                config=build_config(target={"target_streams": ["1234567890"]}),
            )
            self.fake_stream_lookup({"1234567890": "session-1"})
            self._install_chat(plugin)

            await plugin._tick()
            await plugin._tick()
            self.assertEqual(len(self.stream_lookup_calls), 1)

    async def test_lookup_failure_does_not_break_tick(self):
        """能力查询抛异常时不能让整轮 tick 崩掉。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=("good",),
                config=build_config(target={"target_streams": ["1234567890"]}),
            )
            self.fake_stream_lookup({}, raise_error=True)
            self._install_chat(plugin)

            await plugin._tick()
            self.assertEqual(plugin.ctx.send.streams(), ["good"])  # type: ignore[attr-defined]

    async def test_config_pinned_recognises_resolved_user_id(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                config=build_config(target={"target_streams": ["1234567890"]}),
            )
            self.fake_stream_lookup({"1234567890": "session-1"})
            self._install_chat(plugin)

            await plugin._resolve_user_id_targets(plugin._subscriptions())
            self.assertTrue(plugin._is_config_pinned("session-1"))

    async def test_resolved_target_still_passes_whitelist(self):
        """回归（实机踩到）：配置写 QQ 号 + 白名单也写 QQ 号时必须能发出去。

        解析成会话 ID 后如果丢掉 user_id，身份里就只剩会话 ID，
        而白名单里写的是 QQ 号 —— 会被自己的白名单挡掉，提醒静默不发。
        """
        with TemporaryDirectory() as tmp:
            session = "0123456789abcdef0123456789abcdef"
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=(),
                config=build_config(
                    target={"target_streams": ["1234567890"]},
                    access={"mode": "whitelist", "entries": ["1234567890"]},
                ),
            )
            self.fake_stream_lookup({"1234567890": session})
            self._install_chat(plugin)

            await plugin._tick()
            self.assertEqual(plugin.ctx.send.streams(), [session])  # type: ignore[attr-defined]

    async def test_resolved_target_blacklist_by_user_id(self):
        """反过来：黑名单写了 QQ 号，解析后也要能挡住。"""
        with TemporaryDirectory() as tmp:
            session = "0123456789abcdef0123456789abcdef"
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=(),
                config=build_config(
                    target={"target_streams": ["1234567890"]},
                    access={"mode": "blacklist", "entries": ["1234567890"]},
                ),
            )
            self.fake_stream_lookup({"1234567890": session})
            self._install_chat(plugin)

            await plugin._tick()
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]

    async def test_resolved_target_identity_keeps_user_id(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), config=build_config(target={"target_streams": ["1234567890"]})
            )
            self.fake_stream_lookup({"1234567890": "session-1"})
            self._install_chat(plugin)

            resolved = await plugin._resolve_user_id_targets(plugin._subscriptions())
            self.assertEqual(resolved[0].stream_id, "session-1")
            self.assertEqual(resolved[0].user_id, "1234567890")
            self.assertEqual(resolved[0].chat_type, "private")
            self.assertEqual(resolved[0].identity.identifiers, ["1234567890", "session-1"])

    # ── 交给 replyer 决定回复风格 ─────────────────────────

    async def test_persona_mode_generates_then_sends_directly(self):
        """persona：让模型按宿主人设生成一句话，再直发（有风格且必达）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp), offset_minutes=20, config=build_config(reply={"style": "persona"})
            )
            llm: FakeLlm = plugin.ctx.llm  # type: ignore[assignment]

            await plugin._tick()
            # 用 LLM 生成，但发送走 send.text —— 不依赖主链路是否开口
            self.assertEqual(len(llm.prompts), 1)
            prompt = llm.prompts[0]
            self.assertIn("高等数学", prompt)
            self.assertIn("20 分钟后上课", prompt)
            self.assertIn("卡斯", prompt)  # 提示里带上了宿主人设
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]
            self.assertIn(
                "按人设生成的一句话", plugin.ctx.send.last_text()  # type: ignore[attr-defined]
            )
            maisaka: FakeMaisaka = plugin.ctx.maisaka  # type: ignore[assignment]
            self.assertEqual(maisaka.calls, [])  # 没走主链路

    async def test_persona_hint_appended(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                config=build_config(
                    reply={"style": "persona", "persona_hint": "简短一点，别加表情"}
                ),
            )
            await plugin._tick()
            llm: FakeLlm = plugin.ctx.llm  # type: ignore[assignment]
            self.assertIn("简短一点，别加表情", llm.prompts[0])

    async def test_shipped_defaults_use_persona_but_stay_reliable(self):
        """出厂默认 persona：文案由模型按宿主人设生成，但发送走直发所以必达。

        这是两种极端之间的取舍：fixed 太机器味，proactive（真正交给 replyer 开口）
        实测可能只规划不发声。persona 兼顾了风格与送达。
        """
        plugin = ClassSchedulePlugin()
        conf = plugin._conf()
        self.assertEqual(conf.reply.style, "persona")
        self.assertEqual(conf.reply.ack_style, "persona")
        self.assertNotEqual(conf.reply.style, "proactive")  # 会静默的那个不当默认
        self.assertGreater(conf.reply.fallback_after_seconds, 0)

    async def test_ack_uses_ack_style_not_reminder_style(self):
        """回执走 ack_style：提醒设为 fixed 也不影响回执用 persona。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=("chat-stream",),
                config=build_config(reply={"style": "fixed", "ack_style": "persona"}),
            )
            raw = self._ics_bytes()
            message = self._file_message(base64_data=base64.b64encode(raw).decode())
            await self._intake(plugin, message)
            llm: FakeLlm = plugin.ctx.llm  # type: ignore[assignment]
            self.assertEqual(len(llm.prompts), 1)
            self.assertIn("课表", llm.prompts[0])
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]

    async def test_ack_can_be_fixed_too(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=("chat-stream",),
                config=build_config(reply={"ack_style": "fixed"}),
            )
            raw = self._ics_bytes()
            message = self._file_message(base64_data=base64.b64encode(raw).decode())
            await self._intake(plugin, message)
            maisaka: FakeMaisaka = plugin.ctx.maisaka  # type: ignore[assignment]
            self.assertEqual(maisaka.calls, [])
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]

    async def test_fixed_mode_never_uses_replyer(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp), offset_minutes=20, config=build_config(reply={"style": "fixed"})
            )
            await plugin._tick()
            maisaka: FakeMaisaka = plugin.ctx.maisaka  # type: ignore[assignment]
            self.assertEqual(maisaka.calls, [])
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]
            self.assertIn("高等数学", plugin.ctx.send.last_text())  # type: ignore[attr-defined]

    async def test_persona_falls_back_to_fixed_when_llm_fails(self):
        """模型生成失败时必须退回固定文案，别把提醒弄丢。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp), offset_minutes=20, config=build_config(reply={"style": "persona"})
            )
            llm: FakeLlm = plugin.ctx.llm  # type: ignore[assignment]
            llm.fail = True

            await plugin._tick()
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]
            self.assertIn("高等数学", plugin.ctx.send.last_text())  # type: ignore[attr-defined]
            self.assertTrue(plugin._state.fired)

    async def test_persona_falls_back_when_llm_returns_blank(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp), offset_minutes=20, config=build_config(reply={"style": "persona"})
            )
            llm: FakeLlm = plugin.ctx.llm  # type: ignore[assignment]
            llm.response = "   "

            await plugin._tick()
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]
            self.assertIn("高等数学", plugin.ctx.send.last_text())  # type: ignore[attr-defined]

    async def test_persona_generated_once_for_all_sessions(self):
        """回归：一节课发给 N 个会话只该请求模型一次。

        逐会话生成会让 tick 串行等 N 次模型（N 个群就 N 倍延迟），
        文案本身却完全相同。
        """
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=("s1", "s2", "s3"),
                config=build_config(reply={"style": "persona"}),
            )
            llm: FakeLlm = plugin.ctx.llm  # type: ignore[assignment]

            await plugin._tick()

            self.assertEqual(sorted(plugin.ctx.send.streams()), ["s1", "s2", "s3"])  # type: ignore[attr-defined]
            self.assertEqual(len(llm.prompts), 1)
            self.assertEqual(len(plugin.ctx.send.texts), 3)  # type: ignore[attr-defined]

    async def test_persona_text_failure_not_retried_per_session(self):
        """生成失败时也不该每个会话各失败一次。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=("s1", "s2"),
                config=build_config(reply={"style": "persona"}),
            )
            llm: FakeLlm = plugin.ctx.llm  # type: ignore[assignment]
            llm.fail = True

            await plugin._tick()

            self.assertEqual(len(llm.prompts), 1)
            self.assertEqual(sorted(plugin.ctx.send.streams()), ["s1", "s2"])  # type: ignore[attr-defined]

    async def test_slow_llm_falls_back_to_template(self):
        """模型卡住不能拖垮提醒：超时后退回模板文案，仍要送达。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                config=build_config(
                    reply={"style": "persona", "persona_timeout_seconds": 1}
                ),
            )
            llm: FakeLlm = plugin.ctx.llm  # type: ignore[assignment]
            llm.delay_seconds = 5

            started = time.perf_counter()
            await plugin._tick()
            elapsed = time.perf_counter() - started

            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]
            self.assertIn("高等数学", plugin.ctx.send.last_text())  # type: ignore[attr-defined]
            # 超时生效：没有一直等到模型返回
            self.assertLess(elapsed, 5)
            self.assertTrue(plugin._state.fired)

    async def test_slow_llm_does_not_delay_other_reminders(self):
        """一节慢生成不能连带拖住另一节：超时后各自照常送达。"""
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            plugin = self.prepare_bare(
                data_dir,
                targets=("s1",),
                config=build_config(
                    reply={"style": "persona", "persona_timeout_seconds": 1}
                ),
            )
            now = datetime.now()
            write_ics(data_dir / "ics" / "a.ics", now + timedelta(minutes=18), uid="c1", summary="高等数学")
            write_ics(data_dir / "ics" / "b.ics", now + timedelta(minutes=20), uid="c2", summary="线性代数")
            plugin._repo.refresh(force=True)  # type: ignore[union-attr]
            llm: FakeLlm = plugin.ctx.llm  # type: ignore[assignment]
            llm.delay_seconds = 5

            await plugin._tick()

            texts = [text for _stream, text in plugin.ctx.send.texts]  # type: ignore[attr-defined]
            self.assertEqual(len(texts), 2)
            self.assertTrue(any("高等数学" in text for text in texts))
            self.assertTrue(any("线性代数" in text for text in texts))

    async def test_proactive_mode_enqueues_and_schedules_verification(self):
        """proactive：交给主链路开口，并登记一次"到点验证"。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                config=build_config(reply={"style": "proactive"}),
            )
            maisaka: FakeMaisaka = plugin.ctx.maisaka  # type: ignore[assignment]

            await plugin._tick()
            self.assertEqual(len(maisaka.calls), 1)
            self.assertEqual(maisaka.calls[0][2], "class_reminder")
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]
            # 已登记待验证：到点仍检测不到发声就兜底直发
            self.assertEqual(len(plugin._pending_proactive), 1)
            self.assertTrue(plugin._state.fired)

    async def test_proactive_rejected_without_fallback_keeps_retrying(self):
        """replyer 不接手且关掉回退时不能假装送达：不标记已提醒，下轮重试。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                config=build_config(
                    reply={"style": "proactive", "fallback_to_fixed": False}
                ),
            )
            maisaka: FakeMaisaka = plugin.ctx.maisaka  # type: ignore[assignment]
            maisaka.result = {"success": False, "error": "会话不存在"}

            await plugin._tick()
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]
            self.assertEqual(plugin._state.fired, {})

    # ── 聊天里发 ics 文件 ─────────────────────────────────

    @staticmethod
    def _ics_bytes(offset_minutes: int = 20, summary: str = "文件导入的课") -> bytes:
        from datetime import datetime as _dt

        start = _dt.now() + timedelta(minutes=offset_minutes)
        text = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:from-chat\n"
            f"SUMMARY:{summary}\n"
            f"DTSTART;TZID=Asia/Shanghai:{start.strftime('%Y%m%dT%H%M%S')}\n"
            "END:VEVENT\nEND:VCALENDAR\n"
        )
        return text.encode("utf-8")

    @staticmethod
    def _file_message(
        *,
        name: str = "我的课表.ics",
        base64_data: str = "",
        url: str = "",
        nested: bool = True,
        stream_id: str = "chat-stream",
    ) -> dict:
        """构造一条带文件段的入站消息（默认用 SnowLuma 的嵌套包法）。"""
        payload: dict[str, Any] = {"name": name, "size": "230"}
        if base64_data:
            payload["base64"] = base64_data
        if url:
            payload["url"] = url
        segment = {"type": "file", "data": payload}
        if nested:
            segment = {"type": "dict", "data": segment}
        return {
            "message_id": "m-file-1",
            "session_id": stream_id,
            "platform": "qq",
            "message_info": {
                "user_info": {"user_id": "654321", "user_nickname": "小明"},
                "group_info": {"group_id": "123456", "group_name": "高数三班"},
            },
            "raw_message": [segment],
            "processed_plain_text": "[file]",
        }

    async def _intake(self, plugin, message: dict, **kwargs):
        """触发 Hook 并等待它派生的后台导入任务结束（测试里要确定性）。"""
        await plugin.handle_file_message(message=message, **kwargs)
        # hook 只是安排后台任务，测试需要等它跑完
        for task in list(plugin._intake_tasks):
            await task

    async def test_chat_file_imported_from_base64(self):
        """内嵌内容的文件直接导入，不联网。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=("chat-stream",),
                config=build_config(reply={"ack_style": "persona"}),
            )
            raw = self._ics_bytes()
            message = self._file_message(base64_data=base64.b64encode(raw).decode())

            await self._intake(plugin, message)
            events = plugin._repo.events  # type: ignore[union-attr]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].summary, "文件导入的课")
            # 回执由模型按人设生成，再直发
            llm: FakeLlm = plugin.ctx.llm  # type: ignore[assignment]
            self.assertEqual(len(llm.prompts), 1)
            self.assertIn("成功导入", llm.prompts[0])
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]

    async def test_chat_file_imported_from_url(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("chat-stream",))
            text = self._ics_bytes().decode()
            self.fake_ics_text(text)
            message = self._file_message(
                url="https://example.com/cal.ics", base64_data=""
            )

            await self._intake(plugin, message)
            self.assertEqual(len(plugin._repo.events), 1)  # type: ignore[union-attr]
            self.assertEqual(self.ics_fetch_calls, ["https://example.com/cal.ics"])

    async def test_chat_file_local_url_needs_allowlist(self):
        """平台给的本地地址默认被拒，且提示怎么放行。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("chat-stream",))
            self._use_real_fetch_validation()
            message = self._file_message(url="http://127.0.0.1:3000/cal.ics")

            with self.assertLogs("test.class-schedule", level="WARNING") as captured:
                await self._intake(plugin, message)
            self.assertEqual(plugin._repo.events, [])  # type: ignore[union-attr]
            self.assertTrue(
                any("allowed_hosts" in line for line in captured.output), captured.output
            )

    async def test_chat_file_local_url_allowed_when_listed(self):
        """点名放行后，本地地址也能下载（内容用打桩给出）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=("chat-stream",),
                config=build_config(file_import={"allowed_hosts": ["127.0.0.1:3000"]}),
            )
            self.fake_ics_text(self._ics_bytes().decode())
            message = self._file_message(url="http://127.0.0.1:3000/cal.ics")

            await self._intake(plugin, message)
            self.assertEqual(len(plugin._repo.events), 1)  # type: ignore[union-attr]
            self.assertEqual(self.ics_fetch_allow_hosts, [("127.0.0.1:3000",)])

    async def test_non_ics_file_ignored(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("chat-stream",))
            message = self._file_message(name="笔记.txt", base64_data="aGVsbG8=")

            await self._intake(plugin, message)
            maisaka: FakeMaisaka = plugin.ctx.maisaka  # type: ignore[assignment]
            self.assertEqual(maisaka.calls, [])
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]

    async def test_duplicate_chat_file_not_reimported(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=("chat-stream",),
                config=build_config(reply={"ack_style": "persona"}),
            )
            raw = self._ics_bytes()
            message = self._file_message(base64_data=base64.b64encode(raw).decode())

            await self._intake(plugin, message)
            first = list(plugin.ctx.send.texts)  # type: ignore[attr-defined]
            await self._intake(plugin, message)

            llm: FakeLlm = plugin.ctx.llm  # type: ignore[assignment]
            self.assertEqual(len(llm.prompts), 2)
            self.assertIn("内容没有变化", llm.prompts[1])
            self.assertEqual(len(first), 1)  # 第一次只回执一次
            self.assertEqual(len(plugin.ctx.send.texts), 2)  # type: ignore[attr-defined]

    async def test_resending_same_filename_replaces(self):
        """同名文件重发 = 更新，不堆旧课表（调课后旧时间不能继续提醒）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("chat-stream",))
            first = base64.b64encode(self._ics_bytes(summary="旧课表")).decode()
            second = base64.b64encode(self._ics_bytes(summary="新课表")).decode()

            await self._intake(plugin, self._file_message(base64_data=first))
            await self._intake(plugin, self._file_message(base64_data=second))

            repo = plugin._repo
            self.assertEqual(repo.file_count, 1)  # type: ignore[union-attr]
            self.assertEqual(len(repo.events), 1)  # type: ignore[union-attr]
            self.assertEqual(repo.events[0].summary, "新课表")  # type: ignore[union-attr]

    async def test_chat_file_ignored_when_disabled(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=("chat-stream",),
                config=build_config(file_import={"enabled": False}),
            )
            raw = self._ics_bytes()
            message = self._file_message(base64_data=base64.b64encode(raw).decode())
            await self._intake(plugin, message)
            self.assertEqual(plugin._repo.file_count, 0)  # type: ignore[union-attr]

    async def test_chat_file_ignored_for_blacklisted_chat(self):
        """名单外的会话发文件也不处理（与命令准入一致）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=("chat-stream",),
                config=build_config(access={"mode": "blacklist", "entries": ["654321"]}),
            )
            raw = self._ics_bytes()
            message = self._file_message(base64_data=base64.b64encode(raw).decode())
            await self._intake(plugin, message)
            self.assertEqual(plugin._repo.file_count, 0)  # type: ignore[union-attr]

    async def test_chat_file_invalid_content_reported(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=("chat-stream",),
                config=build_config(reply={"ack_style": "persona"}),
            )
            message = self._file_message(
                base64_data=base64.b64encode("这不是课表".encode()).decode()
            )
            await self._intake(plugin, message)
            llm: FakeLlm = plugin.ctx.llm  # type: ignore[assignment]
            self.assertIn("不是有效的课表文件", llm.prompts[0])

    async def test_message_without_file_segment_is_noop(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("chat-stream",))
            await self._intake(
                plugin,
                {"message_id": "m1", "session_id": "s", "raw_message": [
                    {"type": "text", "data": "今天有什么课"}
                ]},
            )
            maisaka: FakeMaisaka = plugin.ctx.maisaka  # type: ignore[assignment]
            self.assertEqual(maisaka.calls, [])

    # ── /课程解析 引导流程 ────────────────────────────────

    async def test_parse_command_prompts_and_arms(self):
        """发 /课程解析 → 提示发文件 + 进入等待状态。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=("chat-stream",),
                config=build_config(reply={"ack_style": "persona"}),
            )
            ok, message, _ = await plugin.handle_parse(**group_kwargs("chat-stream"))
            self.assertTrue(ok)
            # 提示语经模型按人设生成，再用直发送达
            llm: FakeLlm = plugin.ctx.llm  # type: ignore[assignment]
            self.assertEqual(len(llm.prompts), 1)
            self.assertIn(".ics", llm.prompts[0])
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]
            self.assertTrue(
                plugin._is_awaiting_ics({"session_id": "chat-stream", "message_info": {}})
            )

    async def test_file_sent_after_parse_command_is_imported(self):
        """命令引导后的文件正常解析，并且等待状态被清掉。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=("chat-stream",),
                config=build_config(reply={"ack_style": "persona"}),
            )
            await plugin.handle_parse(**group_kwargs("chat-stream"))

            raw = self._ics_bytes()
            message = self._file_message(base64_data=base64.b64encode(raw).decode())
            await self._intake(plugin, message)

            self.assertEqual(len(plugin._repo.events), 1)  # type: ignore[union-attr]
            self.assertFalse(plugin._is_awaiting_ics(message))
            # 回执里要说明这是用户刚请求的导入，模型才知道怎么接话
            llm: FakeLlm = plugin.ctx.llm  # type: ignore[assignment]
            self.assertIn("刚用 /课程解析 请求", llm.prompts[-1])
            self.assertEqual(len(plugin.ctx.send.texts), 2)  # 提示 + 回执

    async def test_wrong_file_while_awaiting_gets_told(self):
        """等待期间发错文件（zip/截图）要明确提示，而不是静默忽略。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=("chat-stream",),
                config=build_config(reply={"ack_style": "persona"}),
            )
            await plugin.handle_parse(**group_kwargs("chat-stream"))

            message = self._file_message(name="成绩单.zip", base64_data="aGVsbG8=")
            await self._intake(plugin, message)

            llm: FakeLlm = plugin.ctx.llm  # type: ignore[assignment]
            self.assertEqual(len(llm.prompts), 2)  # 提示 + 纠正
            self.assertIn("不是课表文件", llm.prompts[-1])
            self.assertIn("成绩单.zip", llm.prompts[-1])
            self.assertEqual(len(plugin.ctx.send.texts), 2)  # type: ignore[attr-defined]
            # 发错不该清掉等待状态，用户可以接着发对的
            self.assertTrue(plugin._is_awaiting_ics(message))

    async def test_wrong_file_without_awaiting_stays_silent(self):
        """没发过命令时发错文件保持安静，不打扰正常聊天。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("chat-stream",))
            message = self._file_message(name="成绩单.zip", base64_data="aGVsbG8=")
            await self._intake(plugin, message)

            maisaka: FakeMaisaka = plugin.ctx.maisaka  # type: ignore[assignment]
            self.assertEqual(maisaka.calls, [])
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]

    async def test_awaiting_expires(self):
        """等待窗口过期后不再把文件当课表。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=("chat-stream",),
                config=build_config(file_import={"arm_minutes": 1}),
            )
            await plugin.handle_parse(**group_kwargs("chat-stream"))
            # 手动把截止时间拨到过去，模拟过期
            plugin._awaiting_ics["chat-stream"] = datetime.now() - timedelta(seconds=1)

            payload = {"session_id": "chat-stream", "message_info": {}}
            self.assertFalse(plugin._is_awaiting_ics(payload))
            self.assertNotIn("chat-stream", plugin._awaiting_ics)  # 顺带清理

    async def test_parse_command_sends_fixed_text_when_llm_fails(self):
        """模型生成失败时提示语必须退回固定文案，别让命令看起来没反应。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=("chat-stream",),
                config=build_config(reply={"ack_style": "persona"}),
            )
            llm: FakeLlm = plugin.ctx.llm  # type: ignore[assignment]
            llm.fail = True

            ok, _, _ = await plugin.handle_parse(**group_kwargs("chat-stream"))
            self.assertTrue(ok)
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]
            self.assertIn(".ics", plugin.ctx.send.texts[0][1])  # type: ignore[attr-defined]

    async def test_parse_command_respects_access_list(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                config=build_config(access={"mode": "whitelist", "entries": ["123456"]}),
            )
            ok, message, _ = await plugin.handle_parse(
                **group_kwargs("other", group_id="999999")
            )
            self.assertFalse(ok)
            self.assertIn("白名单", message)
            self.assertNotIn("999999", plugin._awaiting_ics)

    async def test_parse_command_accepts_both_spellings(self):
        """`/课程解析` 与 `/课表解析` 是同一个命令，只注册一个组件。"""
        import re

        plugin = ClassSchedulePlugin()
        patterns = [
            item["metadata"]["command_pattern"]
            for item in plugin.get_components()
            if item["name"] == "schedule_parse"
        ]
        self.assertEqual(len(patterns), 1)
        for text in ("/课程解析", "/课表解析"):
            with self.subTest(text=text):
                self.assertIsNotNone(re.fullmatch(patterns[0], text))

    # ── 宿主人设 ──────────────────────────────────────────

    async def test_host_persona_loaded_from_config(self):
        """人设从宿主全局配置读取——插件不自带口吻。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            await plugin._load_host_persona()

            self.assertEqual(plugin._persona_nickname, "卡斯")
            self.assertIn("猫娘", plugin._persona_body)
            self.assertEqual(plugin._bot_accounts.get("qq"), "10000")
            header = plugin._persona_header()
            self.assertIn("卡斯", header)
            self.assertIn("猫娘", header)

    async def test_persona_header_falls_back_when_host_has_none(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            plugin.ctx.config.values = {}  # type: ignore[attr-defined]
            await plugin._load_host_persona()
            self.assertIn("bot", plugin._persona_header())

    async def test_host_config_failure_is_tolerated(self):
        """宿主配置读不到不能让插件崩。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))

            class _Boom:
                async def get(self, key: str, default: Any = None):
                    raise RuntimeError("测试：配置不可用")

            plugin.ctx.config = _Boom()  # type: ignore[assignment]
            await plugin._load_host_persona()  # 不应抛异常
            self.assertEqual(plugin._persona_nickname, "")

    async def test_persona_loaded_lazily_when_missing_at_load(self):
        """宿主配置可能晚于插件就绪：第一次要用时补读一次。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            self.assertFalse(plugin._persona_loaded)

            await plugin._persona_say("随便说一句")
            self.assertTrue(plugin._persona_loaded)
            llm: FakeLlm = plugin.ctx.llm  # type: ignore[assignment]
            self.assertIn("卡斯", llm.prompts[0])

    # ── proactive 的发言验证与兜底 ────────────────────────

    @staticmethod
    def _bot_message(timestamp: float, user_id: str = "10000") -> dict:
        return {
            "timestamp": str(timestamp),
            "message_info": {"user_info": {"user_id": user_id}},
        }

    def _queue_proactive(self, plugin) -> None:
        """登记一条已到验证时间的待处理主动发言。"""
        plugin._pending_proactive.append(
            {
                "stream_id": "s1",
                "queued_at": datetime.now() - timedelta(seconds=200),
                "due_at": datetime.now() - timedelta(seconds=1),
                "facts": "提醒事实",
                "fixed_text": "固定兜底文案",
            }
        )

    async def test_no_fallback_when_bot_already_spoke(self):
        """主链路确实开口了就不该再兜底，否则用户收到两条。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("s1",))
            await plugin._load_host_persona()
            self._queue_proactive(plugin)
            message: FakeMessage = plugin.ctx.message  # type: ignore[assignment]
            message.recent = [self._bot_message(datetime.now().timestamp())]

            await plugin._check_pending_proactive(datetime.now())
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]
            self.assertEqual(plugin._pending_proactive, [])

    async def test_fallback_sent_when_bot_never_spoke(self):
        """主链路没开口时必须兜底直发——这正是之前静默丢消息的坑。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("s1",))
            await plugin._load_host_persona()
            self._queue_proactive(plugin)
            message: FakeMessage = plugin.ctx.message  # type: ignore[assignment]
            # 只有用户自己的消息，bot 没说话
            message.recent = [self._bot_message(datetime.now().timestamp(), "654321")]

            await plugin._check_pending_proactive(datetime.now())
            self.assertEqual(plugin.ctx.send.texts, [("s1", "固定兜底文案")])  # type: ignore[attr-defined]
            self.assertEqual(plugin._pending_proactive, [])

    async def test_old_messages_do_not_count_as_spoken(self):
        """早于入队时间的 bot 发言不算（那是上一轮的事）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("s1",))
            await plugin._load_host_persona()
            self._queue_proactive(plugin)
            message: FakeMessage = plugin.ctx.message  # type: ignore[assignment]
            message.recent = [self._bot_message(datetime.now().timestamp() - 5000)]

            await plugin._check_pending_proactive(datetime.now())
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]

    async def test_verification_trusts_chain_without_bot_account(self):
        """拿不到 bot 账号就无法判断，此时信任主链路，避免重复发。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("s1",))
            plugin.ctx.config.values = {}  # type: ignore[attr-defined]  # 宿主没给账号
            self.assertTrue(await plugin._bot_spoke_since("s1", "qq", 0))

    async def test_verification_trusts_chain_on_query_failure(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("s1",))
            await plugin._load_host_persona()
            message: FakeMessage = plugin.ctx.message  # type: ignore[assignment]
            message.raise_error = True
            self.assertTrue(await plugin._bot_spoke_since("s1", "qq", 0))

    async def test_verification_matches_any_own_account(self):
        """平台名对不上时按"任一自有账号"匹配。

        待验证项是异步登记的，手上不一定有准确的平台名；只认单一平台会得出
        "没发言"，那就变成重复发送。
        """
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("s1",))
            plugin.ctx.config.values["bot.qq_account"] = ""  # type: ignore[attr-defined]
            plugin.ctx.config.values["bot.platforms"] = [  # type: ignore[attr-defined]
                "telegram:777",
            ]
            await plugin._load_host_persona()
            message: FakeMessage = plugin.ctx.message  # type: ignore[assignment]
            message.recent = [self._bot_message(datetime.now().timestamp(), "777")]

            # 问的是 qq，实际账号在 telegram：仍应认出发言，不重复发
            self.assertTrue(await plugin._bot_spoke_since("s1", "qq", 0))

    async def test_verification_query_timeout_trusts_chain(self):
        """查询卡住时不能拖着提醒循环等：超时按"信任主链路"处理。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("s1",))
            await plugin._load_host_persona()
            message: FakeMessage = plugin.ctx.message  # type: ignore[assignment]
            message.delay_seconds = 5

            with _AttrPatch(plugin_module, "PROACTIVE_QUERY_TIMEOUT_SECONDS"):
                plugin_module.PROACTIVE_QUERY_TIMEOUT_SECONDS = 0.1
                started = time.perf_counter()
                spoken = await plugin._bot_spoke_since("s1", "qq", 0)
                elapsed = time.perf_counter() - started

            self.assertTrue(spoken)
            self.assertLess(elapsed, 5)

    async def test_verification_disabled_when_delay_zero(self):
        """fallback_after_seconds=0 表示不验证，完全信任主链路。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=("s1",),
                config=build_config(reply={"style": "proactive", "fallback_after_seconds": 0}),
            )
            plugin._schedule_proactive_check("s1", "f", "x")
            self.assertEqual(plugin._pending_proactive, [])

    # ── 自然语言问课（注入规划器） ────────────────────────

    @staticmethod
    def _planner_kwargs(*texts: str, session_id: str = "chat-stream") -> dict:
        """构造规划器 hook 的入参（items 里带若干条消息文本）。"""
        items = [
            {"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "你是bot"}]}
        ]
        items += [
            {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": text}]}
            for text in texts
        ]
        return {
            "items": items,
            "item_schema_version": 1,
            "tool_definitions": [],
            "selected_history_count": len(items),
            "built_message_count": len(items),
            "selection_reason": "测试",
            "session_id": session_id,
        }

    async def test_injects_schedule_when_topic_related(self):
        """聊到课表时把课表注入上下文，模型就能自然回答。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20, targets=("chat-stream",))

            result = await plugin.inject_schedule_handler(
                **self._planner_kwargs("明天有课吗")
            )
            self.assertEqual(result.get("action"), "continue")
            items = result["modified_kwargs"]["items"]
            self.assertEqual(len(items), 3)  # 原 2 条 + 注入 1 条
            injected = items[1]["parts"][0]["text"]  # 插在 system 之后
            self.assertIn("高等数学", injected)
            self.assertIn("【课表】", injected)

    async def test_no_injection_for_unrelated_chat(self):
        """闲聊不注入，避免每条消息都多花 token。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20, targets=("chat-stream",))

            result = await plugin.inject_schedule_handler(
                **self._planner_kwargs("今天天气怎么样")
            )
            self.assertNotIn("modified_kwargs", result)

    async def test_always_mode_injects_even_unrelated(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=("chat-stream",),
                config=build_config(nl_query={"mode": "always"}),
            )
            result = await plugin.inject_schedule_handler(**self._planner_kwargs("在吗"))
            self.assertIn("modified_kwargs", result)

    async def test_extra_keywords_trigger_injection(self):
        """课程名有特殊叫法时，用户加的词也能触发注入。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=("chat-stream",),
                config=build_config(nl_query={"extra_keywords": ["大物"]}),
            )
            result = await plugin.inject_schedule_handler(**self._planner_kwargs("大物难吗"))
            self.assertIn("modified_kwargs", result)

    async def test_injection_disabled(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=("chat-stream",),
                config=build_config(nl_query={"enabled": False}),
            )
            result = await plugin.inject_schedule_handler(
                **self._planner_kwargs("明天有课吗")
            )
            self.assertNotIn("modified_kwargs", result)

    async def test_injection_skipped_without_courses(self):
        """没有课表就别注入（否则会喂给模型一段空数据）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("chat-stream",))
            result = await plugin.inject_schedule_handler(
                **self._planner_kwargs("明天有课吗")
            )
            self.assertNotIn("modified_kwargs", result)

    async def test_injection_skipped_for_blacklisted_chat(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=("chat-stream",),
                config=build_config(access={"mode": "blacklist", "entries": ["chat-stream"]}),
            )
            result = await plugin.inject_schedule_handler(
                **self._planner_kwargs("明天有课吗")
            )
            self.assertNotIn("modified_kwargs", result)

    async def test_injection_skipped_without_session_id(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20, targets=("chat-stream",))
            kwargs = self._planner_kwargs("明天有课吗")
            kwargs["session_id"] = ""
            result = await plugin.inject_schedule_handler(**kwargs)
            self.assertNotIn("modified_kwargs", result)

    async def test_injection_tolerates_missing_items(self):
        """items 结构不符合预期时安静跳过，不能抛异常打断聊天。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20, targets=("chat-stream",))
            result = await plugin.inject_schedule_handler(session_id="chat-stream")
            self.assertEqual(result.get("action"), "continue")

    async def test_injection_includes_holiday_note(self):
        """注入文本要带放假说明，模型才知道那天没课。"""
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            plugin = self.prepare(data_dir, offset_minutes=20, targets=("chat-stream",))
            today = datetime.now().strftime("%Y-%m-%d")
            self._install_holiday_data(data_dir, dates={today: True})
            plugin._reload_holiday_cache()

            result = await plugin.inject_schedule_handler(
                **self._planner_kwargs("今天有课吗")
            )
            injected = result["modified_kwargs"]["items"][1]["parts"][0]["text"]
            self.assertIn("测试假日放假", injected)

    async def test_nl_inject_hook_registered(self):
        """注入 Hook 必须注册为 BLOCKING —— 它要改写 items。"""
        plugin = ClassSchedulePlugin()
        hooks = {
            item["name"]: item["metadata"]
            for item in plugin.get_components()
            if item["type"] == "HOOK_HANDLER"
        }
        self.assertIn("schedule_nl_inject", hooks)
        self.assertEqual(hooks["schedule_nl_inject"]["hook"], "maisaka.planner.before_request")
        self.assertTrue(str(hooks["schedule_nl_inject"]["mode"]).lower().endswith("blocking"))

    # ── 开关 ──────────────────────────────────────────────
    async def test_plugin_disabled_sends_nothing(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp), offset_minutes=20, config=build_config(plugin={"enabled": False})
            )
            await plugin._tick()
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]

    async def test_reminder_disabled_sends_nothing(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                config=build_config(reminder={"enable_reminder": False}),
            )
            await plugin._tick()
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]

    # ── 提醒对象 ──────────────────────────────────────────

    async def test_no_target_skips_without_burning_reminder(self):
        """没有提醒对象时不能把这次课标记成已提醒，否则订阅后就收不到了。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20, targets=())
            await plugin._tick()
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]
            self.assertEqual(plugin._state.fired, {})

            plugin._state.add_subscription(ChatIdentity(stream_id="stream-late"))
            await plugin._tick()
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]
            self.assertEqual(plugin.ctx.send.texts[0][0], "stream-late")  # type: ignore[attr-defined]

    async def test_config_target_streams_used(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=(),
                config=build_config(target={"target_streams": ["cfg-stream"]}),
            )
            await plugin._tick()
            self.assertEqual(plugin.ctx.send.texts[0][0], "cfg-stream")  # type: ignore[attr-defined]

    async def test_config_target_can_get_its_own_lead(self):
        """配置里的固定会话也能被 /课表提前 单独设置（升级为运行时记录）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=40,
                targets=(),
                config=build_config(target={"target_streams": ["cfg-stream"]}),
            )
            _, message, _ = await plugin.handle_lead(
                **group_kwargs("cfg-stream"), matched_groups={"value": "60"}
            )
            self.assertIn("60 分钟", message)
            # 清掉命令自身的回复，只看提醒是否发出、发了几条
            plugin.ctx.send.texts.clear()  # type: ignore[attr-defined]

            await plugin._tick()
            self.assertEqual(plugin.ctx.send.streams(), ["cfg-stream"])  # type: ignore[attr-defined]
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]

    async def test_broadcast_to_multiple_targets(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20, targets=("s1", "s2"))
            await plugin._tick()
            self.assertEqual(
                sorted(item[0] for item in plugin.ctx.send.texts), ["s1", "s2"]  # type: ignore[attr-defined]
            )

    async def test_send_failure_is_retried_next_tick(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20)
            send: FakeSend = plugin.ctx.send  # type: ignore[assignment]
            send.text_fails = True
            await plugin._tick()
            self.assertEqual(send.texts, [])
            self.assertEqual(plugin._state.fired, {})

            send.text_fails = False
            await plugin._tick()
            self.assertEqual(len(send.texts), 1)

    async def test_send_uses_plain_text_only(self):
        """提醒只走 send.text。

        麦麦主机与内置的 NapCat / SnowLuma 适配器都不支持 markdown 类自定义
        消息段（全仓搜不到 qq_markdown），所以不该再去尝试自定义段。
        """
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20)
            await plugin._tick()
            send: FakeSend = plugin.ctx.send  # type: ignore[assignment]
            self.assertEqual(len(send.texts), 1)
            self.assertEqual(send.customs, [])

    async def test_send_failure_still_reported_per_stream(self):
        """部分会话发送失败时其余会话照常收到，失败列表记进日志。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20, targets=("s1",))
            send: FakeSend = plugin.ctx.send  # type: ignore[assignment]

            original = send.text

            async def flaky(text: str, stream_id: str, **kwargs) -> bool:
                if stream_id == "s1":
                    return False
                return await original(text, stream_id, **kwargs)

            send.text = flaky  # type: ignore[assignment]
            plugin._state.add_subscription(ChatIdentity(stream_id="s2"))

            await plugin._tick()
            # s1 失败、s2 成功：发出去过就不重试，避免给 s2 重复提醒
            self.assertEqual(send.streams(), ["s2"])
            self.assertTrue(plugin._state.fired)

    async def test_custom_template_applied(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                config=build_config(message={"template": "【{weekday}】{course} 于 {start}"}),
            )
            await plugin._tick()
            text = plugin.ctx.send.texts[0][1]  # type: ignore[attr-defined]
            self.assertTrue(text.startswith("【星期"))
            self.assertIn("高等数学 于", text)

    # ── 生命周期 ──────────────────────────────────────────

    async def test_on_load_starts_and_on_unload_stops_loop(self):
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(Path(tmp))
            await plugin.on_load()

            task = plugin._loop_task
            self.assertIsNotNone(task)
            self.assertFalse(task.done())  # type: ignore[union-attr]

            # ics 目录被自动创建
            self.assertTrue((Path(tmp) / "ics").is_dir())

            await plugin.on_unload()
            self.assertIsNone(plugin._loop_task)
            self.assertTrue(task.done())  # type: ignore[union-attr]
            self.assertTrue(task.cancelled())  # type: ignore[union-attr]

    async def test_on_unload_is_idempotent(self):
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(Path(tmp))
            await plugin.on_load()
            await plugin.on_unload()
            await plugin.on_unload()  # 不应抛异常

    async def test_on_load_twice_leaves_single_loop(self):
        """回归：重复 on_load 不能留下两个循环各推一份提醒。"""
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(Path(tmp))
            await plugin.on_load()
            first = plugin._loop_task
            await plugin.on_load()
            second = plugin._loop_task
            try:
                self.assertIsNotNone(second)
                self.assertIsNot(first, second)
                self.assertTrue(first.done())  # type: ignore[union-attr]
            finally:
                await plugin.on_unload()

    async def test_on_load_reads_ics_placed_by_hand(self):
        """用户手动把 ics 丢进目录，重载后应能识别。"""
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            ics_dir = data_dir / "ics"
            ics_dir.mkdir(parents=True)
            write_ics(ics_dir / "manual.ics", datetime.now() + timedelta(days=1))

            plugin = self.make_plugin(data_dir)
            await plugin.on_load()
            try:
                self.assertEqual(plugin._repo.file_count, 1)  # type: ignore[union-attr]
                self.assertEqual(len(plugin._repo.events), 1)  # type: ignore[union-attr]
            finally:
                await plugin.on_unload()

    async def test_config_update_resyncs_scan_interval(self):
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(Path(tmp))
            await plugin.on_load()
            try:
                await plugin.on_config_update(
                    "self", build_config(source={"scan_interval_seconds": 60}), "1.0.0"
                )
                self.assertEqual(plugin._repo.cache_seconds, 60)  # type: ignore[union-attr]
            finally:
                await plugin.on_unload()

    async def test_config_update_ignores_non_self_scope(self):
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(Path(tmp))
            await plugin.on_load()
            try:
                before = plugin._repo.cache_seconds  # type: ignore[union-attr]
                await plugin.on_config_update("bot", {}, "1.0.0")
                self.assertEqual(plugin._repo.cache_seconds, before)  # type: ignore[union-attr]
            finally:
                await plugin.on_unload()

    async def test_config_update_applies_new_reminder_settings(self):
        """热更新必须真的生效（否则 WebUI 改配置像没反应）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(Path(tmp))
            await plugin.on_load()
            try:
                self.assertEqual(plugin._default_lead_minutes(), 20)
                await plugin.on_config_update(
                    "self", build_config(reminder={"remind_before_minutes": 45}), "1.0.0"
                )
                self.assertEqual(plugin._default_lead_minutes(), 45)
            finally:
                await plugin.on_unload()

    # ── 订阅命令：群聊 + 私聊 ─────────────────────────────

    async def test_subscribe_registers_group(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            ok, message, _ = await plugin.handle_subscribe(**group_kwargs("group-42"))

            self.assertTrue(ok)
            record = plugin._state.find("group-42")
            self.assertIsNotNone(record)
            self.assertEqual(record.chat_type, "group")
            self.assertEqual(record.group_id, "123456")
            self.assertEqual(record.label, "高数三班")
            self.assertIn("群聊", message)

            # 状态已落盘，且用的是 v2 结构
            saved = json.loads((Path(tmp) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], 2)
            self.assertEqual(saved["subscriptions"][0]["stream_id"], "group-42")

    async def test_subscribe_registers_private_chat(self):
        """私聊同样能作为提醒对象，并记下用户号。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            ok, message, _ = await plugin.handle_subscribe(**private_kwargs("private-7"))

            self.assertTrue(ok)
            record = plugin._state.find("private-7")
            self.assertIsNotNone(record)
            self.assertEqual(record.chat_type, "private")
            self.assertEqual(record.user_id, "654321")
            self.assertIn("私聊", message)
            self.assertIn("私聊「小明」", record.display)

    async def test_private_and_group_can_both_receive(self):
        """群和私聊各订阅一次，提醒两边都要到。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20, targets=())
            await plugin.handle_subscribe(**group_kwargs("group-1"))
            await plugin.handle_subscribe(**private_kwargs("private-1"))
            plugin.ctx.send.texts.clear()  # type: ignore[attr-defined]

            await plugin._tick()
            self.assertEqual(
                sorted(plugin.ctx.send.streams()), ["group-1", "private-1"]  # type: ignore[attr-defined]
            )

    async def test_subscribe_is_idempotent(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("group-42",))
            _, message, _ = await plugin.handle_subscribe(**group_kwargs("group-42"))
            self.assertIn("已经是提醒对象", message)
            self.assertEqual(len(plugin._state.subscriptions), 1)

    async def test_subscribe_refreshes_identity_but_keeps_lead(self):
        """重复订阅会刷新群名，但不会覆盖用户设过的提前量。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            await plugin.handle_subscribe(**group_kwargs("g1"))
            plugin._state.set_lead("g1", 33)

            await plugin.handle_subscribe(
                **group_kwargs("g1", group_name="改过名的群")
            )
            record = plugin._state.find("g1")
            self.assertEqual(record.lead_minutes, 33)
            self.assertEqual(record.label, "改过名的群")

    async def test_unsubscribe_removes_stream(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("group-42",))
            ok, message, _ = await plugin.handle_unsubscribe(**group_kwargs("group-42"))
            self.assertTrue(ok)
            self.assertEqual(plugin._state.subscriptions, [])
            self.assertIn("已取消", message)

    async def test_resubscribe_after_unsubscribe_starts_clean(self):
        """退订要把该会话的提前量一并带走：重新订阅应是干净的默认值。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            await plugin.handle_subscribe(**group_kwargs("g1"))
            plugin._state.set_lead("g1", 45)

            await plugin.handle_unsubscribe(**group_kwargs("g1"))
            self.assertIsNone(plugin._state.find("g1"))

            await plugin.handle_subscribe(**group_kwargs("g1"))
            self.assertIsNone(plugin._state.find("g1").lead_minutes)

    async def test_lead_command_respects_subscription_limit(self):
        """/课表提前 会顺手订阅，所以同样要受会话数上限约束，不能成为后门。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), config=build_config(target={"max_subscriptions": 1})
            )
            await plugin.handle_subscribe(**group_kwargs("g1"))
            _, message, _ = await plugin.handle_lead(
                **group_kwargs("g2"), matched_groups={"value": "30"}
            )
            self.assertIn("上限", message)
            self.assertIsNone(plugin._state.find("g2"))

    async def test_unsubscribe_reports_config_managed_target(self):
        """配置里指定的会话不该只回一句"不在列表里"就完事。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), config=build_config(target={"target_streams": ["cfg-stream"]})
            )
            _, message, _ = await plugin.handle_unsubscribe(**group_kwargs("cfg-stream"))
            self.assertIn("配置", message)

    async def test_unsubscribe_config_pinned_session_with_own_lead(self):
        """回归：配置固定的会话被 /课表提前 升级成运行时记录后，
        退订会成功（设置确实清掉了）但提醒仍会发，回执必须说清这一点。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), config=build_config(target={"target_streams": ["cfg"]})
            )
            await plugin.handle_lead(**group_kwargs("cfg"), matched_groups={"value": "30"})
            self.assertIsNotNone(plugin._state.find("cfg"))  # 已升级为运行时记录

            _, message, _ = await plugin.handle_unsubscribe(**group_kwargs("cfg"))
            self.assertIn("配置", message)
            self.assertNotIn("已取消本会话的提醒", message)
            # 提醒确实还在发：配置把它固定住了
            self.assertIn("cfg", [item.stream_id for item in plugin._subscriptions()])
            # 但单独设置的提前量已经清掉
            self.assertIsNone(plugin._state.find("cfg"))

    async def test_unsubscribe_plain_session_says_cancelled(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("g1",))
            _, message, _ = await plugin.handle_unsubscribe(**group_kwargs("g1"))
            self.assertIn("已取消本会话的提醒", message)

    async def test_subscription_limit_enforced(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), config=build_config(target={"max_subscriptions": 1})
            )
            await plugin.handle_subscribe(**group_kwargs("g1"))
            _, message, _ = await plugin.handle_subscribe(**group_kwargs("g2"))
            self.assertIn("上限", message)
            self.assertIsNone(plugin._state.find("g2"))

    # ── 提前量命令 ────────────────────────────────────────

    async def test_lead_command_sets_current_session_only(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("g1", "g2"))
            _, message, _ = await plugin.handle_lead(
                **group_kwargs("g1"), matched_groups={"value": "35"}
            )
            self.assertIn("35 分钟", message)
            self.assertEqual(plugin._state.find("g1").lead_minutes, 35)
            self.assertIsNone(plugin._state.find("g2").lead_minutes)  # 别的会话不受影响

    async def test_lead_command_auto_subscribes(self):
        """在没订阅的会话设提前量，顺手订阅它才符合直觉。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            _, message, _ = await plugin.handle_lead(
                **group_kwargs("g9"), matched_groups={"value": "15"}
            )
            self.assertIn("已订阅本会话", message)
            record = plugin._state.find("g9")
            self.assertIsNotNone(record)
            self.assertEqual(record.lead_minutes, 15)

    async def test_lead_command_reset_clears_session_override(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=("g1",),
                config=build_config(reminder={"remind_before_minutes": 15}),
            )
            plugin._state.set_lead("g1", 99)
            _, message, _ = await plugin.handle_lead(
                **group_kwargs("g1"), matched_groups={"value": "重置"}
            )
            self.assertIsNone(plugin._state.find("g1").lead_minutes)
            self.assertIn("15 分钟", message)

    async def test_lead_command_rejects_garbage(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("g1",))
            _, message, _ = await plugin.handle_lead(
                **group_kwargs("g1"), matched_groups={"value": "abc"}
            )
            self.assertIsNone(plugin._state.find("g1").lead_minutes)
            self.assertIn("❌", message)

    async def test_lead_command_rejects_out_of_range(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("g1",))
            await plugin.handle_lead(**group_kwargs("g1"), matched_groups={"value": "99999"})
            self.assertIsNone(plugin._state.find("g1").lead_minutes)

    async def test_lead_command_survives_superscript_digits(self):
        """回归：'²'.isdigit() 为真但 int('²') 会抛异常，命令会静默不回话。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("g1",))
            _, message, _ = await plugin.handle_lead(
                **group_kwargs("g1"), matched_groups={"value": "²"}
            )
            self.assertIn("❌", message)
            self.assertIsNone(plugin._state.find("g1").lead_minutes)

    async def test_lead_value_parser(self):
        parse = ClassSchedulePlugin._parse_lead_value
        self.assertEqual(parse("30"), 30)
        self.assertEqual(parse(" 0 "), 0)
        self.assertEqual(parse("+5"), 5)  # int() 接受加号
        self.assertEqual(parse("1440"), 1440)
        self.assertIsNone(parse("1441"))
        self.assertIsNone(parse("-1"))
        self.assertIsNone(parse("²"))
        self.assertIsNone(parse("abc"))
        self.assertIsNone(parse(""))
        self.assertIsNone(parse("3.5"))

    async def test_lead_command_reports_current(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("g1",))
            _, message, _ = await plugin.handle_lead(
                **group_kwargs("g1"), matched_groups={}
            )
            self.assertIn("跟随配置", message)
            self.assertIn("20 分钟", message)

    # ── 访问名单 ──────────────────────────────────────────

    async def test_whitelist_blocks_unlisted_session_commands(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                config=build_config(
                    access={"mode": "whitelist", "entries": ["123456"]}
                ),
            )
            ok, message, _ = await plugin.handle_subscribe(
                **group_kwargs("other", group_id="999999")
            )
            self.assertFalse(ok)
            self.assertIn("白名单", message)
            self.assertIsNone(plugin._state.find("other"))

    async def test_whitelist_allows_listed_session(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                config=build_config(
                    access={"mode": "whitelist", "entries": ["123456"]}
                ),
            )
            ok, _, _ = await plugin.handle_subscribe(**group_kwargs("g1"))
            self.assertTrue(ok)
            self.assertIsNotNone(plugin._state.find("g1"))

    async def test_blacklist_blocks_listed_session(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                config=build_config(access={"mode": "blacklist", "entries": ["999999"]}),
            )
            ok, message, _ = await plugin.handle_subscribe(
                **group_kwargs("bad", group_id="999999")
            )
            self.assertFalse(ok)
            self.assertIn("黑名单", message)

    async def test_blacklist_allows_others(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                config=build_config(access={"mode": "blacklist", "entries": ["999999"]}),
            )
            ok, _, _ = await plugin.handle_subscribe(**group_kwargs("g1"))
            self.assertTrue(ok)

    async def test_access_matches_user_id_for_private_chat(self):
        """私聊没有群号，名单可以写用户号来命中。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                config=build_config(access={"mode": "whitelist", "entries": ["654321"]}),
            )
            ok, _, _ = await plugin.handle_subscribe(**private_kwargs("p1"))
            self.assertTrue(ok)

    async def test_access_matches_platform_prefixed_entry(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                config=build_config(
                    access={"mode": "whitelist", "entries": ["qq:123456"]}
                ),
            )
            ok, _, _ = await plugin.handle_subscribe(**group_kwargs("g1"))
            self.assertTrue(ok)

    async def test_access_matches_stream_id_entry(self):
        """群号取不到时可以用会话 ID（/课表状态 里能看到）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                config=build_config(
                    access={"mode": "whitelist", "entries": ["my-stream"]}
                ),
            )
            ok, _, _ = await plugin.handle_subscribe(stream_id="my-stream")
            self.assertTrue(ok)

    async def test_tool_query_not_gated_by_access_list(self):
        """工具调用拿不到会话信息（麦麦只传 LLM 给的参数），名单管不住它。

        因此这里断言**名单不影响工具**——这是实测过的真实行为，不是"应该如此"。
        真正的控制是 access.tool_query_enabled 开关。
        """
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20, targets=())
            plugin.set_plugin_config(
                build_config(access={"mode": "whitelist", "entries": ["123456"]})
            )
            # 即便带上名单外的会话信息，工具也照常回答（它本来也不该看到这些）
            answer = await plugin.handle_query_schedule(**group_kwargs("other", group_id="999999"))
            self.assertIn("高等数学", answer)
            # 真实调用形态：只有 LLM 给的参数，没有 stream_id / message
            answer = await plugin.handle_query_schedule(scope="today")
            self.assertIn("高等数学", answer)

    async def test_tool_query_can_be_disabled(self):
        """不给名单兜底时，至少提供一个明确的开关。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20, targets=())
            plugin.set_plugin_config(build_config(access={"tool_query_enabled": False}))
            answer = await plugin.handle_query_schedule(scope="today")
            self.assertIn("已在插件配置中关闭", answer)
            self.assertNotIn("高等数学", answer)

    async def test_startup_warns_that_list_cannot_gate_tool(self):
        """开了名单又留着工具时必须告警，否则用户以为名单挡住了。"""
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(
                Path(tmp),
                build_config(
                    access={"mode": "whitelist", "entries": ["123456"], "tool_query_enabled": True}
                ),
            )
            with self.assertLogs("test.class-schedule", level="WARNING") as captured:
                await plugin.on_load()
            try:
                self.assertTrue(
                    any("管不住 LLM 工具" in line for line in captured.output),
                    captured.output,
                )
            finally:
                await plugin.on_unload()

    async def test_no_warning_when_tool_disabled(self):
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(
                Path(tmp),
                build_config(
                    access={
                        "mode": "whitelist",
                        "entries": ["123456"],
                        "tool_query_enabled": False,
                    }
                ),
            )
            # 关掉工具后不该再出现这条告警（没有告警时 assertLogs 会直接失败）
            with self.assertNoLogs("test.class-schedule", level="WARNING"):
                await plugin.on_load()
            await plugin.on_unload()

    async def test_whitelist_allows_tool_query_in_listed_chat(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20, targets=())
            plugin.set_plugin_config(
                build_config(access={"mode": "whitelist", "entries": ["123456"]})
            )
            answer = await plugin.handle_query_schedule(**group_kwargs("g1"))
            self.assertIn("高等数学", answer)

    async def test_blacklist_tool_allows_without_identity(self):
        """黑名单是点名禁止：认不出来就放行，与命令路径语义一致。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20, targets=())
            plugin.set_plugin_config(
                build_config(access={"mode": "blacklist", "entries": ["999999"]})
            )
            answer = await plugin.handle_query_schedule(scope="today")
            self.assertIn("高等数学", answer)

    async def test_reminder_delivery_skips_blacklisted_session(self):
        """已订阅的会话被拉黑后应自动停止收到提醒。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20, targets=("bad", "good"))
            plugin.set_plugin_config(
                build_config(access={"mode": "blacklist", "entries": ["bad"]})
            )
            await plugin._tick()
            self.assertEqual(plugin.ctx.send.streams(), ["good"])  # type: ignore[attr-defined]

    async def test_reminder_delivery_can_ignore_access_list(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20, targets=("bad", "good"))
            plugin.set_plugin_config(
                build_config(
                    access={
                        "mode": "blacklist",
                        "entries": ["bad"],
                        "apply_to_reminders": False,
                    }
                )
            )
            await plugin._tick()
            self.assertEqual(sorted(plugin.ctx.send.streams()), ["bad", "good"])  # type: ignore[attr-defined]

    async def test_commands_can_ignore_access_list(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                config=build_config(
                    access={
                        "mode": "whitelist",
                        "entries": ["123456"],
                        "apply_to_commands": False,
                    }
                ),
            )
            ok, _, _ = await plugin.handle_subscribe(
                **group_kwargs("other", group_id="999999")
            )
            self.assertTrue(ok)

    async def test_subscribe_warns_when_delivery_will_be_blocked(self):
        """订阅成功不等于发得出去，回执要说清，否则用户白等。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                config=build_config(
                    access={
                        "mode": "whitelist",
                        "entries": ["123456"],
                        "apply_to_commands": False,  # 命令放行，投递仍受限
                    }
                ),
            )
            ok, message, _ = await plugin.handle_subscribe(
                **group_kwargs("other", group_id="999999")
            )
            self.assertTrue(ok)
            self.assertIn("访问名单会挡住", message)

    async def test_lead_command_warns_when_delivery_will_be_blocked(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                config=build_config(
                    access={
                        "mode": "whitelist",
                        "entries": ["123456"],
                        "apply_to_commands": False,
                    }
                ),
            )
            _, message, _ = await plugin.handle_lead(
                **group_kwargs("other", group_id="999999"),
                matched_groups={"value": "30"},
            )
            self.assertIn("访问名单会挡住", message)

    async def test_whitelist_fails_closed_without_any_identity(self):
        """连会话 ID 都拿不到时白名单必须拒绝（宁可不答也不泄漏）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                config=build_config(access={"mode": "whitelist", "entries": ["123456"]}),
            )
            ok, message, _ = await plugin.handle_subscribe()
            self.assertFalse(ok)
            self.assertIn("无法识别", message)

    async def test_all_filtered_sessions_produce_no_reminder(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20, targets=("bad",))
            plugin.set_plugin_config(
                build_config(access={"mode": "blacklist", "entries": ["bad"]})
            )
            await plugin._tick()
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]
            # 没发出去就不该标记已提醒
            self.assertEqual(plugin._state.fired, {})

    # ── 课表展示命令 ──────────────────────────────────────

    async def test_today_command_lists_course(self):
        with TemporaryDirectory() as tmp:
            # 20 分钟后开课，"今天"这个范围内必然包含它
            plugin = self.prepare(Path(tmp), offset_minutes=20)
            await plugin.handle_today(stream_id="s")
            send: FakeSend = plugin.ctx.send  # type: ignore[assignment]
            self.assertEqual(len(send.texts), 1)
            self.assertIn("高等数学", send.texts[0][1])
            self.assertEqual(send.texts[0][0], "s")

    async def test_today_command_when_empty(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            await plugin.handle_today(stream_id="s")
            self.assertIn("没有课", plugin.ctx.send.last_text())  # type: ignore[attr-defined]

    async def test_status_command_shows_key_numbers(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20, targets=("g1",))
            await plugin.handle_status(**group_kwargs("g1"))
            text = plugin.ctx.send.last_text()  # type: ignore[attr-defined]
            self.assertIn("默认提前：20 分钟", text)
            self.assertIn("1 个文件", text)
            self.assertIn("提醒会话：1 个", text)
            self.assertIn("本会话", text)
            self.assertIn("访问名单：关闭", text)

    async def test_status_command_shows_per_session_lead(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("g1",))
            plugin._state.set_lead("g1", 45)
            await plugin.handle_status(**group_kwargs("g1"))
            text = plugin.ctx.send.last_text()  # type: ignore[attr-defined]
            self.assertIn("45 分钟（本会话单独设置）", text)

    async def test_status_command_shows_access_mode(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=("g1",),
                config=build_config(access={"mode": "whitelist", "entries": ["123456"]}),
            )
            await plugin.handle_status(**group_kwargs("g1"))
            self.assertIn("白名单 1 项", plugin.ctx.send.last_text())  # type: ignore[attr-defined]

    async def test_status_command_shows_current_session_identifiers(self):
        """名单要照抄标识，所以状态里必须把本会话的可用标识打出来。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("g1",))
            await plugin.handle_status(**group_kwargs("g1"))
            text = plugin.ctx.send.last_text()  # type: ignore[attr-defined]
            self.assertIn("群号 123456", text)
            self.assertIn("用户号 654321", text)
            self.assertIn("会话ID g1", text)

    async def test_status_shows_stream_id_when_message_info_missing(self):
        """适配器只给会话 ID 时（拿不到群号/用户号）也要能照抄出来。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            await plugin.handle_status(stream_id="raw-stream")
            text = plugin.ctx.send.last_text()  # type: ignore[attr-defined]
            self.assertIn("会话ID raw-stream", text)
            self.assertNotIn("群号", text)

    async def test_long_schedule_is_truncated(self):
        """课程很多时列表要截断，否则消息会超长发不出去。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=60)
            ics_dir = Path(tmp) / "ics"
            for index in range(40):
                write_ics(
                    ics_dir / f"c{index}.ics",
                    datetime.now() + timedelta(hours=index + 1),
                    summary=f"课程{index}",
                    uid=f"u{index}",
                )
            plugin._repo.refresh(force=True)  # type: ignore[union-attr]

            text = plugin._schedule_summary(7)
            self.assertIn("仅显示前", text)
            self.assertLessEqual(len(text.splitlines()), MAX_LIST_LINES + 2)

    async def test_reload_command_rescans(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20)
            write_ics(
                Path(tmp) / "ics" / "b.ics",
                datetime.now() + timedelta(days=2),
                summary="大学物理",
                uid="c2",
            )
            ok, message, _ = await plugin.handle_reload(stream_id="s")
            self.assertTrue(ok)
            self.assertIn("2 个文件", message)

    async def test_import_rejects_private_url(self):
        """导入命令不得被用作访问内网的跳板。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            self._use_real_fetch_validation()
            ok, message, _ = await plugin.handle_import(
                stream_id="s", matched_groups={"url": "http://127.0.0.1/cal.ics"}
            )
            self.assertFalse(ok)
            self.assertIn("不允许导入", message)

    async def test_import_rejects_non_http_scheme(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            self._use_real_fetch_validation()
            ok, message, _ = await plugin.handle_import(
                stream_id="s", matched_groups={"url": "file:///etc/passwd"}
            )
            self.assertFalse(ok)
            self.assertIn("不允许导入", message)

    async def test_reimporting_same_url_updates_instead_of_duplicating(self):
        """回归：调课后重新导入同一网址，旧时间的课不能残留。

        否则旧文件里的原时间会继续触发提醒，用户会在错误的时间被叫去上课。
        """
        first = ics_text_with(
            datetime.now() + timedelta(days=1), hour=9, summary="高等数学", uid="u1"
        )
        second = ics_text_with(
            datetime.now() + timedelta(days=1), hour=14, summary="高等数学（调课）", uid="u1"
        )

        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))

            with self.fake_fetch(first, second):
                url = "https://example.com/my-calendar.ics"
                ok1, message1, _ = await plugin.handle_import(
                    stream_id="s", matched_groups={"url": url}
                )
                ok2, message2, _ = await plugin.handle_import(
                    stream_id="s", matched_groups={"url": url}
                )

            self.assertTrue(ok1 and ok2)
            self.assertIn("导入成功", message1)
            self.assertIn("已更新", message2)

            # 只留一份文件、一节课，且是调课后的时间
            self.assertEqual(plugin._repo.file_count, 1)  # type: ignore[union-attr]
            events = plugin._repo.events  # type: ignore[union-attr]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].summary, "高等数学（调课）")
            self.assertEqual(events[0].start.hour, 14)

    async def test_import_two_different_urls_kept_separately(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            first = ics_text_with(
                datetime.now() + timedelta(days=1), hour=9, summary="A课", uid="a"
            )
            second = ics_text_with(
                datetime.now() + timedelta(days=2), hour=10, summary="B课", uid="b"
            )

            with self.fake_fetch(first, second):
                await plugin.handle_import(
                    stream_id="s", matched_groups={"url": "https://a.example.com/cal.ics"}
                )
                await plugin.handle_import(
                    stream_id="s", matched_groups={"url": "https://b.example.com/cal.ics"}
                )

            self.assertEqual(plugin._repo.file_count, 2)  # type: ignore[union-attr]
            self.assertEqual(len(plugin._repo.events), 2)  # type: ignore[union-attr]

    async def test_url_import_records_source_for_refresh(self):
        """网址导入要记录来源与指纹，自动刷新才有依据。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            content = ics_text_with(
                datetime.now() + timedelta(days=1), hour=9, summary="A课", uid="a"
            )

            with self.fake_fetch(content):
                ok, message, _ = await plugin.handle_import(
                    stream_id="s", matched_groups={"url": "https://a.example.com/cal.ics"}
                )

            self.assertTrue(ok, message)
            self.assertIn("自动刷新", message)
            sources = plugin._repo.url_sources()  # type: ignore[union-attr]
            self.assertEqual(len(sources), 1)
            info = next(iter(sources.values()))
            self.assertEqual(info["url"], "https://a.example.com/cal.ics")
            self.assertTrue(info["fingerprint"])

    async def test_url_refresh_skips_unchanged_content(self):
        """内容没变就不覆盖文件、不通知，安静跳过。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("s1",))
            content = ics_text_with(
                datetime.now() + timedelta(days=1), hour=9, summary="A课", uid="a"
            )
            with self.fake_fetch(content, content):
                await plugin.handle_import(
                    stream_id="s", matched_groups={"url": "https://a.example.com/cal.ics"}
                )
                before = list(plugin._repo.events)  # type: ignore[union-attr]
                changed = await plugin._ensure_url_refresh()

            self.assertEqual(changed, [])
            self.assertEqual(list(plugin._repo.events), before)  # type: ignore[union-attr]

    async def test_url_refresh_updates_and_notifies_on_change(self):
        """内容变了要覆盖文件并通知提醒会话——静默更新等于没更新。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("s1",))
            old = ics_text_with(
                datetime.now() + timedelta(days=1), hour=9, summary="旧课", uid="a"
            )
            new = ics_text_with(
                datetime.now() + timedelta(days=1), hour=9, summary="新课表", uid="a"
            )
            with self.fake_fetch(old, new):
                await plugin.handle_import(
                    stream_id="s", matched_groups={"url": "https://a.example.com/cal.ics"}
                )
                sent_before = len(plugin.ctx.send.texts)  # type: ignore[attr-defined]
                changed = await plugin._ensure_url_refresh()
                await plugin._notify_url_refresh(changed)

            self.assertEqual(len(changed), 1)
            self.assertTrue(any("新课表" in e.summary for e in plugin._repo.events))  # type: ignore[union-attr]
            new_sends = plugin.ctx.send.texts[sent_before:]  # type: ignore[attr-defined]
            # 网址导入会自动订阅请求会话 s，加上预置的 s1 共两个提醒对象
            self.assertEqual(len(new_sends), 2)
            self.assertEqual(sorted(s for s, _t in new_sends), ["s", "s1"])
            self.assertTrue(all("课表自动更新" in t for _s, t in new_sends))

    async def test_url_refresh_failure_keeps_old_and_warns_once(self):
        """下载失败保留旧课表，且同一文件只告警一次。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("s1",))
            old = ics_text_with(
                datetime.now() + timedelta(days=1), hour=9, summary="旧课", uid="a"
            )
            with self.fake_fetch(old):
                await plugin.handle_import(
                    stream_id="s", matched_groups={"url": "https://a.example.com/cal.ics"}
                )
            events_before = list(plugin._repo.events)  # type: ignore[union-attr]

            async def _boom(url, *, timeout=20, max_bytes=0):
                raise FetchError("网络断了")

            with _AttrPatch(plugin_module, "fetch_ics"):
                plugin_module.fetch_ics = _boom  # type: ignore[assignment]
                with self.assertLogs("test.class-schedule", level="WARNING") as captured:
                    await plugin._ensure_url_refresh()
                    await plugin._ensure_url_refresh()  # 第二轮：不该再告警

            self.assertEqual(list(plugin._repo.events), events_before)  # type: ignore[union-attr]
            hits = [line for line in captured.output if "自动刷新" in line]
            self.assertEqual(len(hits), 1)

    async def test_url_refresh_rejects_bad_content_keeps_old(self):
        """远端返回坏内容（如错误页）时，旧课表必须原样保留。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=("s1",))
            old = ics_text_with(
                datetime.now() + timedelta(days=1), hour=9, summary="旧课", uid="a"
            )
            with self.fake_fetch(old, "<html>502 Bad Gateway</html>"):
                await plugin.handle_import(
                    stream_id="s", matched_groups={"url": "https://a.example.com/cal.ics"}
                )
                events_before = list(plugin._repo.events)  # type: ignore[union-attr]
                changed = await plugin._ensure_url_refresh()

            self.assertEqual(changed, [])
            self.assertEqual(list(plugin._repo.events), events_before)  # type: ignore[union-attr]

    async def test_url_refresh_disabled_by_config(self):
        """url_refresh_hours=0 表示关闭自动刷新。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), config=build_config(source={"url_refresh_hours": 0})
            )
            plugin._last_url_refresh = None
            plugin._maybe_url_refresh()
            self.assertIsNone(plugin._url_refresh_task)

    async def test_no_targets_skip_refresh_notification(self):
        """没有提醒会话时只更新文件，不通知（也不报错）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=())
            await plugin._notify_url_refresh(["x.ics"])  # 不应抛异常
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]

    async def test_unload_cancels_url_refresh_task(self):
        """卸载时自动刷新任务必须被取消，别留下孤儿任务。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            plugin._url_refresh_task = asyncio.create_task(asyncio.sleep(30))
            await plugin.on_unload()
            # 取消后引用会被置 None（与提醒循环同一套清理纪律）
            task = plugin._url_refresh_task
            self.assertTrue(task is None or task.cancelled() or task.done())

    async def test_holiday_failure_retries_soon(self):
        """回归：下载失败曾要等满 refresh_hours（12h）才重试。

        服务器实测出现过一次下载超时——短暂断网不该让"假期跳过"失效半天。
        """
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            plugin._last_holiday_refresh = datetime.now()  # 周期未到
            plugin._holiday_failed[2026] = datetime.now() - timedelta(seconds=10)
            plugin._holiday_task = None

            plugin._maybe_refresh_holidays()
            self.assertIsNone(plugin._holiday_task)  # 10 秒 < 30 分钟，不重试

            plugin._holiday_failed[2026] = datetime.now() - timedelta(minutes=31)
            plugin._maybe_refresh_holidays()
            self.assertIsNotNone(plugin._holiday_task)

    async def test_holiday_failure_retries_even_when_periodic_disabled(self):
        """refresh_hours=0（只在启动时检查）也挡不住失败重试通道。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), config=build_config(holiday={"refresh_hours": 0})
            )
            plugin._holiday_failed[2026] = datetime.now() - timedelta(minutes=31)
            plugin._holiday_task = None

            plugin._maybe_refresh_holidays()
            self.assertIsNotNone(plugin._holiday_task)

    async def test_holiday_unpublished_not_marked_failed(self):
        """「尚未公布」是常态，走长周期，不进失败重试表。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            year = datetime.now().year + 1
            self.fake_holiday_text(json.dumps({"year": year, "days": []}))

            outcome = await plugin._download_holiday_year(
                year, plugin._holiday_dir() / f"{year}.json"
            )

            self.assertEqual(outcome, "unpublished")
            self.assertNotIn(year, plugin._holiday_failed)

    async def test_holiday_success_clears_failure_mark(self):
        """下载成功要清掉失败标记（清除逻辑在 _ensure_holiday_data 的调度层）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            plugin._holiday_failed[2026] = datetime.now()
            plugin._holiday_dir().mkdir(parents=True, exist_ok=True)
            self.fake_holiday_text(
                json.dumps(
                    {
                        "year": 2026,
                        "days": [{"name": "元旦", "date": "2026-01-01", "isOffDay": True}],
                    }
                )
            )

            await plugin._ensure_holiday_data(force=True)

            self.assertNotIn(2026, plugin._holiday_failed)

    async def test_import_failure_does_not_write_file(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))

            with self.fake_fetch("这不是课表"):
                ok, message, _ = await plugin.handle_import(
                    stream_id="s", matched_groups={"url": "https://example.com/cal.ics"}
                )

            self.assertFalse(ok)
            self.assertIn("不是有效的课表", message)
            # 坏文件不该落盘
            self.assertEqual(plugin._repo.file_count, 0)  # type: ignore[union-attr]

    # ── Tool ──────────────────────────────────────────────

    async def test_query_tool_returns_schedule_text(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(Path(tmp), offset_minutes=20)
            text = await plugin.handle_query_schedule(scope="today")
            self.assertIn("高等数学", text)
            # Tool 只回文本给 LLM，不直接发消息
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]

    async def test_query_tool_handles_empty_schedule(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp))
            self.assertIn("没有课", await plugin.handle_query_schedule(scope="week"))

    # ── 注册声明 ──────────────────────────────────────────

    async def test_components_declared(self):
        """同时也守住了装饰器顺序：准入装饰器若包在 @Command 外面，命令会丢。"""
        plugin = ClassSchedulePlugin()
        components = plugin.get_components()
        by_name = {item["name"]: item for item in components}

        for name in (
            "schedule_today",
            "schedule_tomorrow",
            "schedule_week",
            "schedule_import",
            "schedule_reload",
            "schedule_subscribe",
            "schedule_unsubscribe",
            "schedule_lead",
            "schedule_status",
        ):
            with self.subTest(command=name):
                self.assertIn(name, by_name)
                self.assertEqual(by_name[name]["type"], "COMMAND")
                self.assertTrue(by_name[name]["metadata"]["command_pattern"])

        self.assertIn("query_class_schedule", by_name)
        self.assertEqual(by_name["query_class_schedule"]["type"], "TOOL")

    async def test_command_handlers_remain_callable(self):
        """准入装饰器必须用 functools.wraps，否则按 handler_name 取不到方法。"""
        plugin = ClassSchedulePlugin()
        for item in plugin.get_components():
            handler_name = item["metadata"].get("handler_name", "")
            with self.subTest(component=item["name"]):
                handler = getattr(plugin, handler_name, None)
                self.assertTrue(callable(handler))
                self.assertEqual(handler.__name__, handler_name)

    async def test_command_patterns_are_anchored(self):
        """命令正则必须首尾锚定，否则 /课表中途包含也会误触发。"""
        plugin = ClassSchedulePlugin()
        for item in plugin.get_components():
            if item["type"] != "COMMAND":
                continue
            pattern = item["metadata"]["command_pattern"]
            with self.subTest(command=item["name"]):
                self.assertTrue(pattern.startswith("^"))
                self.assertTrue(pattern.endswith("$"))

    async def test_create_plugin_factory(self):
        from class_schedule.plugin import create_plugin

        self.assertIsInstance(create_plugin(), ClassSchedulePlugin)

    # ── 学习陪伴：自然语言收纳 ────────────────────────────

    @staticmethod
    def _text_message(stream_id: str, text: str, *, private: bool = True) -> dict:
        """构造一条带文本段的入站消息（receive hook 的形态）。"""
        message = {
            "message_id": f"m-{abs(hash(text)) % 99999}",
            "session_id": stream_id,
            "message_info": {"user_info": {"user_id": "654321"}},
            "raw_message": [{"type": "text", "data": {"text": text}}],
        }
        if not private:
            message["message_info"]["group_info"] = {"group_id": "123456"}
        return message

    async def _capture(self, plugin, message: dict):
        """触发收纳 hook 并等后台任务结束（测试要确定性）。"""
        await plugin.handle_note_capture(message=message, stream_id=message.get("session_id"))
        for task in list(plugin._note_tasks):
            try:
                await task
            except Exception:
                pass

    async def test_capture_same_message_content(self):
        """「记一下 欧拉公式」：触发词后面的就是内容，直接归档。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), targets=(), config=build_config(access={"chat_scope": "private"})
            )
            await self._capture(
                plugin, self._text_message("ps", "记一下 欧拉公式 e^iπ+1=0")
            )

            notes = plugin._notes
            self.assertEqual(notes.count("未分类"), 1)
            note = notes.recent("未分类")[0]
            self.assertEqual(note.text, "欧拉公式 e^iπ+1=0")
            self.assertEqual(note.kind, "笔记")
            # 回执说明归到了哪门课
            self.assertIn("已记入", plugin.ctx.send.last_text())  # type: ignore[attr-defined]

    async def test_capture_kind_from_trigger(self):
        """「这个是公式」「这个是重点」决定笔记类型。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), targets=(), config=build_config(access={"chat_scope": "private"})
            )
            plugin._recent_texts["ps"] = ("质点在光滑平面上运动", datetime.now())
            await self._capture(plugin, self._text_message("ps", "这个是公式"))
            await self._capture(plugin, self._text_message("ps", "这个是重点"))

            notes = plugin._notes
            self.assertEqual(notes.count("未分类"), 2)
            kinds = [n.kind for n in notes.recent("未分类", limit=2)]
            self.assertEqual(sorted(kinds), ["公式", "重点"])

    async def test_capture_arm_then_next_message(self):
        """只发「记一下」→ 进入等待；下一条消息成为内容。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), targets=(), config=build_config(access={"chat_scope": "private"})
            )
            await self._capture(plugin, self._text_message("ps", "记一下"))
            self.assertIn("ps", plugin._awaiting_note)
            self.assertIn("接下来这条消息", plugin.ctx.send.last_text())  # type: ignore[attr-defined]

            await self._capture(plugin, self._text_message("ps", "第三节是重点考试章节"))

            notes = plugin._notes
            self.assertEqual(notes.count("未分类"), 1)
            self.assertEqual(notes.recent("未分类")[0].text, "第三节是重点考试章节")
            self.assertNotIn("ps", plugin._awaiting_note)

    async def test_capture_expired_arm_ignored(self):
        """等待窗口过期后，普通消息不再被当作内容。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), targets=(), config=build_config(access={"chat_scope": "private"})
            )
            plugin._awaiting_note["ps"] = {
                "deadline": datetime.now() - timedelta(minutes=1),
                "kind": "笔记",
            }
            await self._capture(plugin, self._text_message("ps", "这只是一句闲聊"))

            self.assertEqual(plugin._notes.count("未分类"), 0)  # type: ignore[union-attr]

    async def test_capture_attribution_to_ongoing_course(self):
        """正在上课时段的笔记默认归当节课（软信号）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=-10,  # 10 分钟前开始的课，此刻进行中
                targets=(),
                config=build_config(access={"chat_scope": "private"}),
            )
            await self._capture(plugin, self._text_message("ps", "记一下 老师划了第二章"))

            notes = plugin._notes
            self.assertIn("高等数学", notes.courses())
            self.assertEqual(notes.count("高等数学"), 1)
            self.assertEqual(notes.count("未分类"), 0)

    async def test_capture_attribution_off_goes_uncategorized(self):
        """auto_attribution=false 时全部进未分类。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=-10,
                targets=(),
                config=build_config(
                    access={"chat_scope": "private"},
                    study={"auto_attribution": False},
                ),
            )
            await self._capture(plugin, self._text_message("ps", "记一下 随便什么"))

            self.assertEqual(plugin._notes.count("未分类"), 1)  # type: ignore[union-attr]

    async def test_capture_group_message_ignored_in_private_scope(self):
        """默认只走私聊：群里发「记一下」不收纳。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), targets=(), config=build_config(access={"chat_scope": "private"})
            )
            await self._capture(
                plugin, self._text_message("gs", "记一下 群里的东西", private=False)
            )

            self.assertEqual(plugin._notes.courses(), [])  # type: ignore[union-attr]
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]

    async def test_capture_disabled_by_config(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=(),
                config=build_config(
                    access={"chat_scope": "private"}, study={"enabled": False}
                ),
            )
            await self._capture(plugin, self._text_message("ps", "记一下 什么"))

            self.assertEqual(plugin._notes.courses(), [])  # type: ignore[union-attr]

    async def test_capture_skips_commands_and_non_triggers(self):
        """命令不进收纳；普通句子不触发但会被记为回指对象。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), targets=(), config=build_config(access={"chat_scope": "private"})
            )
            await self._capture(plugin, self._text_message("ps", "/课表"))
            self.assertEqual(plugin._notes.courses(), [])  # type: ignore[union-attr]

            await self._capture(plugin, self._text_message("ps", "我今天有点累"))
            self.assertEqual(plugin._notes.courses(), [])  # type: ignore[union-attr]
            # 但它是回指对象
            self.assertIn("ps", plugin._recent_texts)

    async def test_capture_image_note_real_snowluma_shape(self):
        """回归：图片收纳必须认得 SnowLuma 的真实形态。

        实测发现原图 base64 在段**顶层**的 ``binary_data_base64`` 键里、
        ``data`` 是空串——以前只找 data 内的键，结果原图拿不到，
        只存了视觉管道生成的描述文本（用户指出：那是麦麦的理解，不是笔记）。
        """
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), targets=(), config=build_config(access={"chat_scope": "private"})
            )
            raw = b"\x89PNG-fake-formula-image"
            message = {
                "session_id": "ps",
                "message_info": {"user_info": {"user_id": "654321"}},
                "raw_message": [
                    {"type": "text", "data": {"text": "记一下 这张图重要"}},
                    {
                        "type": "image",
                        "data": "",
                        "hash": "abc",
                        "binary_data_base64": base64.b64encode(raw).decode(),
                    },
                ],
            }
            await self._capture(plugin, message)

            notes = plugin._notes
            self.assertEqual(notes.count("未分类"), 1)
            note = notes.recent("未分类")[0]
            self.assertTrue(note.file.startswith("img/"))
            saved = Path(tmp) / "notes" / "未分类" / note.file
            self.assertEqual(saved.read_bytes(), raw)
            self.assertEqual(saved.suffix, ".png")  # 魔数识别
            # 视觉描述作为说明文字保留（可检索），但本体是原图
            self.assertIn("这张图重要", note.text)

    async def test_capture_multiple_images_each_gets_a_note(self):
        """一次发多张图：每张各存一条，描述只挂在第一张上。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), targets=(), config=build_config(access={"chat_scope": "private"})
            )
            raw_a = b"\x89PNG-image-A"
            raw_b = b"\x89PNG-image-B"
            message = {
                "session_id": "ps",
                "message_info": {"user_info": {"user_id": "654321"}},
                "raw_message": [
                    {"type": "text", "data": {"text": "记一下 两张课件"}},
                    {
                        "type": "image",
                        "data": "",
                        "binary_data_base64": base64.b64encode(raw_a).decode(),
                    },
                    {
                        "type": "image",
                        "data": "",
                        "binary_data_base64": base64.b64encode(raw_b).decode(),
                    },
                ],
            }
            await self._capture(plugin, message)

            notes = plugin._notes
            self.assertEqual(notes.count("未分类"), 2)
            recent = notes.recent("未分类", limit=2)
            saved_bytes = {
                (Path(tmp) / "notes" / "未分类" / n.file).read_bytes() for n in recent
            }
            self.assertEqual(saved_bytes, {raw_a, raw_b})
            captions = [n.text for n in recent]
            # 触发词「记一下」被剥掉，余下的「两张课件」是第一张图的说明
            self.assertEqual(sorted(captions), ["", "两张课件"])

    async def test_image_grabbed_before_host_discards_bytes(self):
        """回归（核心）：原图必须在 before_process 抢下。

        宿主在 process() 阶段生成视觉描述后**立即清空原图字节**
        （``component.binary_data = b""``），after_process 时只剩
        「{图片}」占位符和描述文本——v1.3.1 因此只存出无效笔记。
        """
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), targets=(), config=build_config(access={"chat_scope": "private"})
            )
            # 1) 触发词武装等待状态（走 after_process，与真实链路一致）
            await self._capture(plugin, self._text_message("ps", "记一下"))
            self.assertIn("ps", plugin._awaiting_note)

            # 2) 用户发图片：这条消息先过 before_process（原图还在），
            #    再过 process（字节被清空、换成占位符），
            #    最后到 after_process（我们的收纳点）
            raw = b"\x89PNG-real-formula-slide"
            img_msg = {
                "session_id": "ps",
                "message_info": {"user_info": {"user_id": "654321"}},
                "raw_message": [
                    {
                        "type": "image",
                        "data": "",
                        "binary_data_base64": base64.b64encode(raw).decode(),
                    }
                ],
            }
            await plugin.handle_note_image_grab(
                message=img_msg, stream_id="ps"
            )
            self.assertIn("ps", plugin._note_images)  # 抢到了

            consumed_msg = {
                **img_msg,
                "processed_plain_text": "{图片}",  # 宿主处理后的形态
                "raw_message": [{"type": "text", "data": {"text": "{图片}"}}],
            }
            await self._capture(plugin, consumed_msg)

            notes = plugin._notes
            self.assertEqual(notes.count("未分类"), 1)
            note = notes.recent("未分类")[0]
            self.assertTrue(note.file.startswith("img/"))
            saved = Path(tmp) / "notes" / "未分类" / note.file
            self.assertEqual(saved.read_bytes(), raw)  # 原图！
            self.assertEqual(note.text, "")  # 占位符不能当说明

    async def test_image_grab_skips_when_not_armed_or_triggered(self):
        """没触发收纳时，普通图片消息不暂存（别为无关图片占内存）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), targets=(), config=build_config(access={"chat_scope": "private"})
            )
            img_msg = {
                "session_id": "ps",
                "message_info": {"user_info": {"user_id": "654321"}},
                "raw_message": [
                    {
                        "type": "image",
                        "data": "",
                        "binary_data_base64": base64.b64encode(b"x").decode(),
                    }
                ],
            }
            await plugin.handle_note_image_grab(message=img_msg, stream_id="ps")
            self.assertEqual(plugin._note_images, {})

    async def test_placeholder_only_without_image_asks_again(self):
        """只有占位符、又没有抢到图：明确说内容为空，别存下「{图片}」。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), targets=(), config=build_config(access={"chat_scope": "private"})
            )
            plugin._awaiting_note["ps"] = {
                "deadline": datetime.now() + timedelta(minutes=5),
                "kind": "笔记",
            }
            placeholder_msg = {
                "session_id": "ps",
                "message_info": {"user_info": {"user_id": "654321"}},
                "raw_message": [{"type": "text", "data": {"text": "{图片}"}}],
                "processed_plain_text": "{图片}",
            }
            await self._capture(plugin, placeholder_msg)

            self.assertEqual(plugin._notes.count("未分类"), 0)  # type: ignore[union-attr]
            texts = plugin.ctx.send.texts  # type: ignore[attr-defined]
            self.assertTrue(any("内容是空的" in t for _s, t in texts))

    async def test_course_summary_generated_after_class(self):
        """下课后聚合该课随手记录，模型整理成结构化总结并落盘。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), targets=("ps",), config=build_config(access={"chat_scope": "private"})
            )
            start = datetime.now() - timedelta(minutes=105)  # 100 分钟的课，5 分钟前结束
            write_ics(Path(tmp) / "ics" / "a.ics", start, summary="高等数学", uid="s1")
            plugin._repo.refresh(force=True)  # type: ignore[union-attr]
            # 这节课期间记的两条随手笔记（store 用 index 里的 created_at 判断归属窗口，
            # 所以加完后直接改 index.json 的时间字段来回溯到课中）
            store = plugin._notes
            store.add_text_note("高等数学", "重点", "第三章要考", source="测试")
            store.add_text_note("高等数学", "公式", "e^iπ+1=0", source="测试")
            index_path = store.course_dir("高等数学") / "index.json"
            import json as _json
            data = _json.loads(index_path.read_text(encoding="utf-8"))
            data["notes"][0]["created_at"] = (
                datetime.now() - timedelta(minutes=80)
            ).isoformat(timespec="seconds")
            data["notes"][1]["created_at"] = (
                datetime.now() - timedelta(minutes=60)
            ).isoformat(timespec="seconds")
            index_path.write_text(
                _json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )

            llm: FakeLlm = plugin.ctx.llm  # type: ignore[assignment]
            llm.response = "## 核心考点\n- 泰勒展开\n\n## 思维导图\n```mermaid\nmindmap\n 高数\n```"

            await plugin._generate_course_summary(
                "高等数学",
                (start, start + timedelta(minutes=100)),
                "testkey",
            )

            summary_path = (
                Path(tmp) / "notes" / "高等数学" / "总结" / f"{start.strftime('%Y-%m-%d')}.md"
            )
            self.assertTrue(summary_path.exists())
            content = summary_path.read_text(encoding="utf-8")
            self.assertIn("核心考点", content)
            self.assertIn("mermaid", content)
            self.assertTrue(plugin._state.was_summarized("testkey"))
            # 通知发送给提醒会话
            self.assertTrue(any("课堂总结" in t for _s, t in plugin.ctx.send.texts))  # type: ignore[attr-defined]

    async def test_course_summary_skipped_without_notes(self):
        """该节课没有任何随手记录：不生成文件，但也标记成已处理。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), targets=(), config=build_config(access={"chat_scope": "private"})
            )
            start = datetime.now() - timedelta(minutes=105)

            await plugin._generate_course_summary(
                "高等数学",
                (start, start + timedelta(minutes=100)),
                "emptykey",
            )

            self.assertFalse(
                (Path(tmp) / "notes" / "高等数学" / "总结").exists()
            )
            # 标记由调度器（_maybe_course_summaries）负责，直调只负责跳过
            self.assertFalse(plugin._state.was_summarized("emptykey"))

    async def test_course_summary_failure_retries_then_gives_up(self):
        """模型失败按次数重试，超限后放弃并留痕（不无限循环）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), targets=(), config=build_config(access={"chat_scope": "private"})
            )
            llm: FakeLlm = plugin.ctx.llm  # type: ignore[assignment]
            llm.fail = True
            window = (datetime.now() - timedelta(minutes=105),
                      datetime.now() - timedelta(minutes=5))
            plugin._notes.add_text_note("高等数学", "重点", "要考", source="测试")
            # 回溯到课中，否则 notes_between 为空走不到模型调用
            import json as _json
            index_path = plugin._notes.course_dir("高等数学") / "index.json"
            data = _json.loads(index_path.read_text(encoding="utf-8"))
            data["notes"][0]["created_at"] = window[0].isoformat(timespec="seconds")
            index_path.write_text(
                _json.dumps(data, ensure_ascii=False), encoding="utf-8"
            )

            await plugin._generate_course_summary("高等数学", window, "failkey")
            self.assertFalse(plugin._state.was_summarized("failkey"))  # 还会重试
            self.assertEqual(plugin._summary_attempts.get("failkey"), 1)
            self.assertIn("failkey", plugin._summary_retries)  # 已排入重试队列

            await plugin._generate_course_summary("高等数学", window, "failkey")
            await plugin._generate_course_summary("高等数学", window, "failkey")
            self.assertTrue(plugin._state.was_summarized("failkey"))  # 放弃后留痕
            self.assertNotIn("failkey", plugin._summary_retries)  # 队列清掉
            self.assertNotIn("failkey", plugin._summary_attempts)

    async def test_summary_disabled_by_config(self):
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=(),
                config=build_config(
                    access={"chat_scope": "private"}, study={"summary_enabled": False}
                ),
            )
            plugin._maybe_course_summaries(datetime.now())
            self.assertEqual(plugin._summary_attempts, {})

    async def test_note_commands(self):
        """/笔记 概览与明细、/归到 纠正、/找 检索。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp), targets=(), config=build_config(access={"chat_scope": "private"})
            )
            plugin._notes.add_text_note("未分类", "公式", "欧拉公式 e^iπ+1=0")

            await plugin.handle_notes(**private_kwargs("ps"))
            overview = plugin.ctx.send.texts[-1][1]  # type: ignore[attr-defined]
            self.assertIn("未分类", overview)

            ok, message, _ = await plugin.handle_note_move(
                **private_kwargs("ps"), matched_groups={"course": "高等数学"}
            )
            self.assertIn("已把", message)
            self.assertEqual(plugin._notes.count("高等数学"), 1)  # type: ignore[union-attr]

            await plugin.handle_note_search(
                **private_kwargs("ps"), matched_groups={"keyword": "欧拉"}
            )
            search = plugin.ctx.send.texts[-1][1]  # type: ignore[attr-defined]
            self.assertIn("高等数学", search)

            await plugin.handle_notes(**private_kwargs("ps"), matched_groups={"course": "高等数学"})
            detail = plugin.ctx.send.texts[-1][1]  # type: ignore[attr-defined]
            self.assertIn("欧拉公式", detail)

    async def test_injection_includes_current_course(self):
        """注入文本带「当前正在上课」行，模型才知道语境。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=-10,  # 课正在进行
                targets=("ps",),
                config=build_config(access={"chat_scope": "private"}),
            )
            text = plugin._build_injection_text(plugin.config)  # type: ignore[union-attr]
            self.assertIn("【当前】正在上课：高等数学", text)
            self.assertIn("高等数学", text)  # 课表本体仍在

    # ── 适用范围：默认只走私聊 ────────────────────────────

    async def test_config_default_scope_is_private(self):
        """配置模型里的真实默认值必须就是"只走私聊"。

        这里直接看模型默认值：其它用例为了测别的机制会把适用范围放开，
        所以真实的默认值必须单独钉住，否则改坏了没人发现。
        """
        from class_schedule.config_model import ClassScheduleConfig

        self.assertEqual(ClassScheduleConfig().access.chat_scope, "private")

    async def test_private_command_allowed_group_command_denied(self):
        """私聊能用命令，群聊被明确拒绝（默认只走私聊）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=(),
                config=build_config(access={"chat_scope": "private"}),
            )

            ok, message, _ = await plugin.handle_subscribe(**private_kwargs("ps"))
            self.assertTrue(ok, message)
            self.assertTrue(plugin._state.find("ps"))

            ok, message, _ = await plugin.handle_subscribe(**group_kwargs("gs"))
            self.assertFalse(ok)
            self.assertIn("私聊", message)
            self.assertIsNone(plugin._state.find("gs"))

    async def test_command_denied_when_chat_type_unknown(self):
        """认不出会话类型时拒绝：宁可少答一次，也不要把课表带进陌生会话。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=(),
                config=build_config(access={"chat_scope": "private"}),
            )

            ok, message, _ = await plugin.handle_subscribe(stream_id="bare-stream")
            self.assertFalse(ok)
            self.assertIn("无法识别", message)

    async def test_scope_gate_ignores_apply_to_commands_flag(self):
        """适用范围与名单是两个维度：关掉名单限制不该顺手放开群聊。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=(),
                config=build_config(
                    access={"chat_scope": "private", "apply_to_commands": False}
                ),
            )

            ok, _, _ = await plugin.handle_subscribe(**group_kwargs("gs"))
            self.assertFalse(ok)

    async def test_group_scope_only_blocks_private(self):
        """chat_scope=group 时反过来：群聊能用，私聊被拒。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=(),
                config=build_config(access={"chat_scope": "group"}),
            )

            ok, _, _ = await plugin.handle_subscribe(**group_kwargs("gs"))
            self.assertTrue(ok)
            ok, message, _ = await plugin.handle_subscribe(**private_kwargs("ps"))
            self.assertFalse(ok)
            self.assertIn("群聊", message)

    async def test_group_subscription_skipped_with_warning(self):
        """群聊订阅在只走私聊时不再投递，并且必须说出原因。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=("gs",),
                config=build_config(access={"chat_scope": "both"}),
            )
            # 先用放开范围登记成"群聊订阅"，再收紧成默认值
            ok, _, _ = await plugin.handle_subscribe(**group_kwargs("gs"))
            self.assertTrue(ok)
            self.assertEqual(plugin._state.find("gs").chat_type, "group")
            plugin.set_plugin_config(build_config(access={"chat_scope": "private"}))
            sent_before = len(plugin.ctx.send.texts)  # type: ignore[attr-defined]

            await plugin._tick()

            # 订阅回执不算，这里要的是"没有再收到提醒"
            self.assertEqual(
                len(plugin.ctx.send.texts), sent_before  # type: ignore[attr-defined]
            )
            self.assertEqual(len(plugin._skipped_group_subscriptions()), 1)

    async def test_private_subscription_still_delivered(self):
        """私聊订阅在默认范围下照常收提醒（核心功能）."""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=("ps",),
                config=build_config(access={"chat_scope": "private"}),
            )
            ok, _, _ = await plugin.handle_subscribe(**private_kwargs("ps"))
            self.assertTrue(ok)
            sent_before = len(plugin.ctx.send.texts)  # type: ignore[attr-defined]

            await plugin._tick()

            new_sends = plugin.ctx.send.streams()[sent_before:]  # type: ignore[attr-defined]
            self.assertEqual(new_sends, ["ps"])

    async def test_bare_stream_target_still_delivered(self):
        """配置里钉死的裸会话 ID 照发：认不出类型就静默停提醒是最坏结果。

        这是刻意的取舍——钉裸 ID 是用户在配置里的主动选择，而提醒漏发
        没有任何人会发现。README 里写明了想严格只走私聊就别钉裸 ID。
        """
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=("a" * 32,),
                config=build_config(access={"chat_scope": "private"}),
            )
            write_ics(Path(tmp) / "ics" / "a.ics", datetime.now() + timedelta(minutes=20))
            plugin._repo.refresh(force=True)  # type: ignore[union-attr]

            await plugin._tick()

            self.assertEqual(plugin.ctx.send.streams(), ["a" * 32])  # type: ignore[attr-defined]

    async def test_group_file_message_ignored(self):
        """群里发来的课表文件不导入（也不回执）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=(),
                config=build_config(access={"chat_scope": "private"}),
            )
            # _file_message 默认就带 group_info，即一条群消息
            message = self._file_message(
                base64_data=base64.b64encode(self._ics_bytes()).decode()
            )

            await self._intake(plugin, message)

            self.assertEqual(plugin._repo.events, [])  # type: ignore[union-attr]
            self.assertEqual(plugin.ctx.send.texts, [])  # type: ignore[attr-defined]

    async def test_private_file_message_imported(self):
        """私聊里发来的课表文件照常导入（对照组）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=(),
                config=build_config(access={"chat_scope": "private"}),
            )
            message = self._file_message(
                base64_data=base64.b64encode(self._ics_bytes()).decode(),
                stream_id="dm-1",
            )
            # 去掉群聊标识 -> 私聊
            message["message_info"].pop("group_info")

            await self._intake(plugin, message)

            self.assertEqual(len(plugin._repo.events), 1)  # type: ignore[union-attr]

    async def test_private_file_import_auto_subscribes(self):
        """回归：私聊里识别 ics 即自动订阅本会话，不再要求发 /课表订阅。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=(),
                config=build_config(access={"chat_scope": "private"}),
            )
            message = self._file_message(
                base64_data=base64.b64encode(self._ics_bytes()).decode(),
                stream_id="dm-1",
            )
            message["message_info"].pop("group_info")

            await self._intake(plugin, message)

            record = plugin._state.find("dm-1")
            self.assertIsNotNone(record)
            self.assertEqual(record.chat_type, "private")
            self.assertEqual(record.user_id, "654321")
            # 回执不再引导去发 /课表订阅，而是说明已自动订阅
            texts = plugin.ctx.send.texts  # type: ignore[attr-defined]
            self.assertEqual(len(texts), 1)
            self.assertIn("已自动订阅", texts[0][1])
            self.assertNotIn("/课表订阅", texts[0][1])

    async def test_auto_subscribe_can_be_disabled(self):
        """auto_subscribe=false 是"帮别人看课表"用户的退路。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=(),
                config=build_config(
                    access={"chat_scope": "private"},
                    file_import={"auto_subscribe": False},
                ),
            )
            message = self._file_message(
                base64_data=base64.b64encode(self._ics_bytes()).decode(),
                stream_id="dm-1",
            )
            message["message_info"].pop("group_info")

            await self._intake(plugin, message)

            self.assertIsNone(plugin._state.find("dm-1"))
            texts = plugin.ctx.send.texts  # type: ignore[attr-defined]
            self.assertEqual(len(texts), 1)
            self.assertIn("/课表订阅", texts[0][1])

    async def test_url_import_auto_subscribes_requesting_session(self):
        """网址导入同样导入即订阅（行为一致）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=())
            content = ics_text_with(
                datetime.now() + timedelta(days=1), hour=9, summary="A课", uid="a"
            )
            with self.fake_fetch(content):
                ok, message, _ = await plugin.handle_import(
                    stream_id="ps", matched_groups={"url": "https://a.example.com/cal.ics"}
                )

            self.assertTrue(ok, message)
            self.assertIsNotNone(plugin._state.find("ps"))
            self.assertIn("自动订阅", message)

    async def test_auto_subscribe_respects_session_limit(self):
        """达到会话上限时：导入照常成功、只是不自动订阅（记日志）。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=("full1", "full2"),
                config=build_config(access={"chat_scope": "private"}),
            )
            plugin.set_plugin_config(
                build_config(
                    access={"chat_scope": "private"},
                    target={"max_subscriptions": 2},
                    file_import={"auto_subscribe": True},
                )
            )
            message = self._file_message(
                base64_data=base64.b64encode(self._ics_bytes()).decode(),
                stream_id="dm-1",
            )
            message["message_info"].pop("group_info")

            await self._intake(plugin, message)

            # 导入成功
            self.assertEqual(len(plugin._repo.events), 1)  # type: ignore[union-attr]
            # 但没有自动订阅（上限 2 已满）
            self.assertIsNone(plugin._state.find("dm-1"))

    async def test_group_chat_gets_no_schedule_injection(self):
        """群聊里不注入课表——这是最直接的隐私保护。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=("gs",),
                config=build_config(access={"chat_scope": "private"}),
            )
            # 先让 receive hook 记下这个会话是群聊（真实链路里它就是先触发的）
            await plugin.record_chat_type_handler(**group_kwargs("gs"))
            self.assertEqual(plugin._stream_types.get("gs"), "group")

            result = await plugin.inject_schedule_handler(
                **self._planner_kwargs("明天有课吗", session_id="gs")
            )
            self.assertEqual(result, {"action": "continue"})

    async def test_private_chat_gets_schedule_injection(self):
        """私聊里照常注入：类型来自 receive hook 记下的表。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=("ps",),
                config=build_config(access={"chat_scope": "private"}),
            )
            await plugin.record_chat_type_handler(**private_kwargs("ps"))
            self.assertEqual(plugin._stream_types.get("ps"), "private")

            result = await plugin.inject_schedule_handler(
                **self._planner_kwargs("明天有课吗", session_id="ps")
            )
            items = result["modified_kwargs"]["items"]
            self.assertIn("高等数学", items[1]["parts"][0]["text"])

    async def test_injection_denied_for_unrecorded_stream(self):
        """没记过类型的会话说不出是私聊还是群聊：不注入。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare(
                Path(tmp),
                offset_minutes=20,
                targets=("chat-stream",),
                config=build_config(access={"chat_scope": "private"}),
            )

            result = await plugin.inject_schedule_handler(
                **self._planner_kwargs("明天有课吗", session_id="chat-stream")
            )
            self.assertEqual(result, {"action": "continue"})

    async def test_stream_type_table_is_bounded(self):
        """类型表是缓存，必须有上限，别被大量会话撑爆。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=())
            for index in range(plugin_module.MAX_REMEMBERED_STREAMS + 20):
                await plugin.record_chat_type_handler(**private_kwargs(f"s{index}"))

            self.assertEqual(
                len(plugin._stream_types), plugin_module.MAX_REMEMBERED_STREAMS
            )

    async def test_chat_type_recorded_from_real_message_shape(self):
        """类型表要认得麦麦真实下发的 message_info 形状（SnowLuma 契约）。

        主机把 session_id 放在 message 里，所以这里只传 message 也必须认得。
        """
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(Path(tmp), targets=())
            await plugin.record_chat_type_handler(
                message={
                    "session_id": "grp-real",
                    "message_info": {
                        "group_info": {"group_id": "123456789", "group_name": "高数三班"},
                        "user_info": {"user_id": "987654321"},
                    },
                }
            )
            self.assertEqual(plugin._stream_types.get("grp-real"), "group")

            await plugin.record_chat_type_handler(
                message={
                    "session_id": "dm-real",
                    "message_info": {
                        "user_info": {"user_id": "987654321", "user_nickname": "小明"}
                    },
                }
            )
            self.assertEqual(plugin._stream_types.get("dm-real"), "private")

    async def test_status_shows_scope_and_marks_skipped_groups(self):
        """/课表状态 要说清适用范围与"登记了但收不到"的群聊订阅。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=(),
                config=build_config(access={"chat_scope": "both"}),
            )
            await plugin.handle_subscribe(**group_kwargs("gs"))
            plugin.set_plugin_config(build_config(access={"chat_scope": "private"}))

            ok, _, _ = await plugin.handle_status(**private_kwargs("ps"))

            self.assertTrue(ok)
            text = plugin.ctx.send.texts[-1][1]  # type: ignore[attr-defined]
            self.assertIn("适用范围：仅私聊", text)
            self.assertIn("按适用范围跳过", text)

    async def test_group_only_targets_warn_about_scope_not_blacklist(self):
        """所有提醒对象都是群聊时，要说清是"适用范围"挡的，别赖到名单头上。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=(),
                config=build_config(access={"chat_scope": "both"}),
            )
            await plugin.handle_subscribe(**group_kwargs("gs"))
            plugin.set_plugin_config(build_config(access={"chat_scope": "private"}))
            write_ics(Path(tmp) / "ics" / "a.ics", datetime.now() + timedelta(minutes=20))
            plugin._repo.refresh(force=True)  # type: ignore[union-attr]

            with self.assertLogs("test.class-schedule", level="WARNING") as captured:
                await plugin._tick()

            joined = "\n".join(captured.output)
            self.assertIn("都是群聊", joined)
            self.assertIn("chat_scope", joined)
            self.assertNotIn("访问名单挡下", joined)

    async def test_skipped_group_warning_reported_once(self):
        """群聊订阅的告警只报一次，别每分钟刷屏。"""
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=(),
                config=build_config(access={"chat_scope": "both"}),
            )
            await plugin.handle_subscribe(**group_kwargs("gs"))
            await plugin.handle_subscribe(**private_kwargs("ps"))
            plugin.set_plugin_config(build_config(access={"chat_scope": "private"}))

            with self.assertLogs("test.class-schedule", level="WARNING") as captured:
                plugin._warn_about_skipped_groups()
                plugin._warn_about_skipped_groups()

            hits = [line for line in captured.output if "不会收到提醒" in line]
            self.assertEqual(len(hits), 1)

    # ── 阶段 3'：云端识别接线（配置 → 客户端 → 识别器 → 管道）与降级路径 ──

    async def test_recognizer_wired_from_config(self):
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(
                Path(tmp),
                build_config(
                    study={
                        "api_key": "sk-test-key",
                        "vlm_model": "Qwen/custom-vlm",
                        "vlm_fallback_model": "Qwen/small-vlm",
                    }
                ),
            )
            with self.assertLogs("test.class-schedule", level="INFO") as captured:
                await plugin.on_load()
            try:
                # 正向也要留痕：出问题时"到底开没开"是第一件要确认的事
                self.assertTrue(
                    any("公式识别已启用" in line for line in captured.output),
                    captured.output,
                )
                self.assertIsNotNone(plugin._recognizer)
                self.assertIsNotNone(plugin._cloud_client)
                pipeline = plugin._pipeline
                self.assertIs(pipeline.recognizer, plugin._recognizer)
                self.assertTrue(pipeline.running)
                message = (await plugin.handle_notes_db(**private_kwargs("ps")))[1]
                self.assertIn("公式识别", message)
            finally:
                await plugin.on_unload()
            self.assertIsNone(plugin._pipeline)  # 卸载必须收掉 worker

    async def test_missing_key_degrades_quietly(self):
        """没配 Key 是预期状态：不识别、不告警，但 /笔记库 要看得出原因。"""
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(Path(tmp), build_config(study={"api_key": ""}))
            with self.assertNoLogs("test.class-schedule", level="WARNING"):
                await plugin.on_load()
            try:
                self.assertIsNone(plugin._recognizer)
                message = (await plugin.handle_notes_db(**private_kwargs("ps")))[1]
                self.assertIn("未启用", message)
            finally:
                await plugin.on_unload()

    async def test_cloud_disabled_says_so(self):
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(
                Path(tmp), build_config(study={"cloud_enabled": False})
            )
            await plugin.on_load()
            try:
                self.assertIsNone(plugin._recognizer)
                message = (await plugin.handle_notes_db(**private_kwargs("ps")))[1]
                self.assertIn("已关闭", message)
                # 管道照常，图片仍然入库（只是不识别）
                self.assertIsNotNone(plugin._pipeline)
            finally:
                await plugin.on_unload()

    async def test_non_https_api_base_is_rejected_with_warning(self):
        """填了 Key 却把地址配成 http：必须告警，且不装配识别器。"""
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(
                Path(tmp),
                build_config(
                    study={"api_key": "sk-test-key", "api_base_url": "http://api.example.com/v1"}
                ),
            )
            with self.assertLogs("test.class-schedule", level="WARNING") as captured:
                await plugin.on_load()
            try:
                self.assertIsNone(plugin._recognizer)
                self.assertTrue(
                    any("不是 https 地址" in line for line in captured.output), captured.output
                )
                self.assertIsNone(plugin._cloud_client)  # Key 不会留在内存里备用
            finally:
                await plugin.on_unload()

    async def test_recognition_timeout_has_a_floor(self):
        """识别这条链路的超时有 90 秒下限：真实 A/B 里收 7 条公式要了 285 秒。

        多公式的返回不是流式的，模型算完之前 socket 上一个字节都没有，所以配置的
        30 秒会变成"整次调用的上限"并把识别整条打死。
        """
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(
                Path(tmp),
                build_config(study={"api_key": "sk-test-key", "cloud_timeout_seconds": 30}),
            )
            await plugin.on_load()
            try:
                self.assertIsNotNone(plugin._cloud_client)
                # 客户端私有字段：测试里直接读，避免为一个下限再开一层公开 API
                self.assertGreaterEqual(plugin._cloud_client._timeout, 90)
            finally:
                await plugin.on_unload()

            # 用户配得更长时要尊重他的值
            with TemporaryDirectory() as tmp2:
                plugin2 = self.make_plugin(
                    Path(tmp2),
                    build_config(study={"api_key": "sk-test-key", "cloud_timeout_seconds": 240}),
                )
                await plugin2.on_load()
                try:
                    self.assertEqual(plugin2._cloud_client._timeout, 240)
                finally:
                    await plugin2.on_unload()

    async def test_notes_db_reports_dropped_jobs(self):
        """队列满丢弃必须有出口：/笔记库 里要能看到丢弃数。"""
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(
                Path(tmp), build_config(study={"api_key": "sk-test-key"})
            )
            await plugin.on_load()
            try:
                plugin._pipeline.dropped = 7
                self.assertIn("队列满丢弃 7", await plugin._recognition_status_line())
            finally:
                await plugin.on_unload()

    async def test_history_images_recognized_without_user_action(self):
        """回归（用户要求）：识别是自动能力——库里已有的图片要被自己补齐，不用重发。

        场景就是线上那次：图片早进库了，Key 是后来配的。插件在装载/识别刚可用时
        扫一遍笔记目录，把"有图没公式"的排进队列；用户什么都不用做。
        """
        from class_schedule.formula import FormulaRecognizer, image_hash

        class FakeVision:
            def __init__(self, reply: str):
                self.calls: list[str] = []
                self._reply = reply

            async def vision(self, **kwargs):
                self.calls.append(kwargs["model"])
                return {"text": self._reply, "prompt_tokens": 0, "completion_tokens": 0}

        reply = (
            '{"latex": "\\\\frac{\\\\pi}{2}", "name": "半角公式", '
            '"aliases": [], "category": "高等数学", "subcategory": "三角函数", '
            '"knowledge_points": [], "description": "d", "confidence": 0.9}'
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 历史图片：装载前就躺在笔记目录里（模拟 Key 配好之前收下的图）
            image_dir = root / "notes" / "未分类" / "img"
            image_dir.mkdir(parents=True)
            (image_dir / "20260917_214707_79f7.png").write_bytes(b"\x89PNG-history-image")

            plugin = self.make_plugin(root, build_config(study={"api_key": "sk-test-key"}))
            fake = FakeVision(reply)
            plugin._build_recognizer = lambda conf, db: FormulaRecognizer(  # type: ignore[assignment]
                db=db, client=fake, model="vlm-history"
            )
            await plugin.on_load()
            try:
                db = plugin._notes_db
                backfill = plugin._backfill_task
                self.assertIsNotNone(backfill, "装载时就该自动排补识别")
                await backfill
                for _ in range(200):
                    if db.formula_count() == 1:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(db.formula_count(), 1, "历史图片没被自动识别")
                self.assertEqual(len(fake.calls), 1)
                row = db.formula_by_image_hash(image_hash(b"\x89PNG-history-image"))
                self.assertEqual(row["course"], "未分类")
                self.assertEqual(row["latex_normalized"], "\\frac{\\pi}2")
                # 补识别是补历史，不该往会话里回执刷屏
                texts = [text for _stream, text in plugin.ctx.send.texts]  # type: ignore[attr-defined]
                self.assertFalse(any("认出公式" in text for text in texts), texts)
                self.assertIn("历史图片补识别 1", await plugin._recognition_status_line())

                # 再触发一轮：认过的图靠 hash 缓存跳过，不再调用模型
                await plugin._run_formula_backfill()
                self.assertEqual(db.formula_count(), 1)
                self.assertEqual(len(fake.calls), 1)
            finally:
                await plugin.on_unload()

    async def test_backfill_writes_formula_into_existing_notes(self):
        """回归（线上实测）：补识别要把"已认过、但笔记里没公式"的图补写进笔记。

        用户的库正是这个状态：公式在 SQLite 里，`/笔记` 与 `笔记文件` 里只有一句
        图说，所以他回"不是完整的公式，是文字描述"。
        """
        import shutil
        import tempfile as _tf

        from class_schedule.formula import FormulaRecognizer, image_hash
        from class_schedule.notes_db import NotesDatabase
        from class_schedule.pipeline import StudyPipeline

        # LIFO 清理：先注册目录删除（最后执行），再注册关库——Windows 上
        # 文件被连接占用时 rmtree 会失败
        root = Path(_tf.mkdtemp(prefix="backfill-"))
        self.addCleanup(shutil.rmtree, root, True)
        store = StudyNoteStore(root / "notes")
        image = b"\x89PNG-old-slide"
        note = store.add_image_note("未分类", "笔记", image, text="这是一张课件幻灯片")
        plugin = self.make_plugin(root, build_config(study={"api_key": "sk-test-key"}))
        plugin._data_dir = root
        plugin._notes = store
        plugin._state = PluginState()
        db = NotesDatabase(root / "notes.db")
        db.initialize()
        self.addCleanup(db.close)
        plugin._notes_db = db
        # 模拟"之前已经认过、只是没写进笔记"
        db.upsert_formula({
            "fingerprint": "old-fp",
            "name": "许用应力公式",
            "latex_raw": r"\frac{\sigma_{\lim}}{S_{\sigma}}",
            "latex_normalized": r"\frac{\sigma_{\lim}}{S_{\sigma}}",
            "image_hash": image_hash(image),
        })

        class NoCall:
            async def vision(self, **kwargs):
                raise AssertionError("已认过的图不该再调模型")

        plugin._recognizer = FormulaRecognizer(db=db, client=NoCall(), model="vlm")
        plugin._pipeline = StudyPipeline(
            db=db,
            recognizer=plugin._recognizer,
            on_recognized=plugin._on_formula_recognized,
        )
        plugin._pipeline.start()
        self.addAsyncCleanup(plugin._pipeline.stop)

        await plugin._run_formula_backfill()
        refreshed = store.recent("未分类", limit=1)[0]
        self.assertIn("许用应力公式", refreshed.formula)
        self.assertIn(r"\frac{\sigma_{\lim}}{S_{\sigma}}", refreshed.formula)
        self.assertIn("许用应力公式", refreshed.display)
        self.assertTrue(store.search("许用应力"))
        body = (
            store.course_dir("未分类") / f"{note.id}_{note.kind}.md"
        ).read_text(encoding="utf-8")
        self.assertIn("许用应力公式", body)

    async def test_no_backfill_without_recognizer(self):
        """没配 Key 就不该有补识别任务（也不能报错）。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "notes" / "未分类" / "img"
            image_dir.mkdir(parents=True)
            (image_dir / "a.png").write_bytes(b"\x89PNGx")
            plugin = self.make_plugin(root, build_config(study={"api_key": ""}))
            await plugin.on_load()
            try:
                self.assertIsNone(plugin._recognizer)
                self.assertIsNone(plugin._backfill_task)
                self.assertEqual(plugin._notes_db.formula_count(), 0)
            finally:
                await plugin.on_unload()

    async def test_formula_receipt_bypasses_model_in_persona_mode(self):
        """回归（线上实测）：公式回执完全不经过模型。

        两个坑都是真人真事：① persona 把公式概括成"公式没错，和之前一样"，LaTeX
        一个字都到不了用户手上；② 插件现编拟人句时模型**没看到那张图**，会凭空
        描述图片内容（"那个矩形波例题喵…上课别摸鱼"），和主链路对同一条消息的
        回复撞车，用户看到的就是重复发言。所以带数据的回执一律直发原文。
        """
        with TemporaryDirectory() as tmp:
            plugin = self.prepare_bare(
                Path(tmp),
                targets=(),
                config=build_config(
                    access={"chat_scope": "private"},
                    reply={"style": "fixed", "ack_style": "persona"},
                    study={"summary_enabled": False},
                ),
            )

            async def must_not_be_called(facts: str) -> str:
                raise AssertionError("带原文数据的回执不该再问模型")

            plugin._persona_say = must_not_be_called  # type: ignore[assignment]

            await plugin._on_formula_recognized(
                {"stream_id": "ps", "course": "材料力学", "note_ref": ""},
                {
                    "status": "recognized",
                    "degraded": False,
                    "error": "",
                    "formulas": [
                        {
                            "name": "许用应力公式",
                            "latex": r"[\sigma]=\frac{\sigma_{\lim}}{S_{\sigma}}",
                            "confidence": 0.98,
                            "low_confidence": False,
                            "created": False,
                        },
                        {
                            "name": "交变应力参数关系公式",
                            "latex": r"\sigma_{m}=\frac{\sigma_{max}+\sigma_{min}}2",
                            "confidence": 0.4,
                            "low_confidence": True,
                            "created": True,
                        },
                    ],
                },
            )
            self.assertEqual(len(plugin.ctx.send.texts), 1)  # type: ignore[attr-defined]
            sent = plugin.ctx.send.texts[-1][1]  # type: ignore[attr-defined]
            self.assertIn(r"\frac{\sigma_{\lim}}{S_{\sigma}}", sent)
            self.assertIn(r"\frac{\sigma_{max}+\sigma_{min}}2", sent)  # 两条都列出
            self.assertIn("2 条", sent)
            self.assertIn("材料力学", sent)
            self.assertIn("待确认", sent)  # 没把握的那条要标出来
            self.assertNotIn("和之前一样", sent)  # 没有任何模型编的话

            # 失败回执同理：原因直发，不问模型
            await plugin._on_formula_recognized(
                {"stream_id": "ps", "course": "", "note_ref": ""},
                {"status": "failed", "error": "HTTP 503 模型繁忙", "formulas": []},
            )
            sent = plugin.ctx.send.texts[-1][1]  # type: ignore[attr-defined]
            self.assertIn("HTTP 503 模型繁忙", sent)

            # 图里没有公式也要说一声（原来会静默，像坏了）
            await plugin._on_formula_recognized(
                {"stream_id": "ps", "course": "", "note_ref": ""},
                {"status": "no_formula", "error": "", "formulas": []},
            )
            self.assertIn("没有公式", plugin.ctx.send.texts[-1][1])  # type: ignore[attr-defined]

    async def test_formula_receipt_bypasses_replyer_in_proactive_mode(self):
        """proactive 下同样直发：那条路把措辞交给主链路，数据根本没人发。"""
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(
                Path(tmp),
                build_config(
                    access={"chat_scope": "private"},
                    reply={"style": "fixed", "ack_style": "proactive"},
                ),
            )
            await plugin._on_formula_recognized(
                {"stream_id": "ps", "course": "未分类", "note_ref": ""},
                {
                    "status": "recognized",
                    "degraded": False,
                    "error": "",
                    "formulas": [{
                        "name": "欧拉公式",
                        "latex": "e^{i\\pi}+1=0",
                        "confidence": 0.95,
                        "low_confidence": False,
                        "created": True,
                    }],
                },
            )
            self.assertEqual(plugin.ctx.maisaka.calls, [])  # 没交给 replyer
            self.assertEqual(plugin._pending_proactive, [])  # 也没登记发言验证
            sent = plugin.ctx.send.texts[-1][1]  # type: ignore[attr-defined]
            self.assertIn("e^{i\\pi}+1=0", sent)  # 公式原样送达

    async def test_config_update_enables_recognition_without_restart(self):
        """回归（线上实测）：Key 是后来在 WebUI 填的，热更新必须重建识别器。

        只更新配置对象不重建识别器的话，填完 Key 发图也不会识别，日志里还停在
        启动时那句"没配 Key"。
        """
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(Path(tmp), build_config(study={"api_key": ""}))
            await plugin.on_load()
            try:
                self.assertIsNone(plugin._recognizer)
                with self.assertLogs("test.class-schedule", level="INFO") as captured:
                    await plugin.on_config_update(
                        "self", build_config(study={"api_key": "sk-later-key"}), "1.0.1"
                    )
                self.assertIsNotNone(plugin._recognizer)
                self.assertIs(plugin._pipeline.recognizer, plugin._recognizer)
                self.assertTrue(
                    any("公式识别已启用" in line for line in captured.output), captured.output
                )
                # /笔记库 也要跟着改口：不能再报"未启用"
                self.assertNotIn("未启用", await plugin._recognition_status_line())

                # 反向：关掉云端开关后立刻停止识别，且管道不再收任务
                await plugin.on_config_update(
                    "self", build_config(study={"api_key": "sk-later-key", "cloud_enabled": False}), "1.0.2"
                )
                self.assertIsNone(plugin._recognizer)
                self.assertIsNone(plugin._pipeline.recognizer)
                self.assertFalse(plugin._pipeline.enqueue({"image": b"x"}))
            finally:
                await plugin.on_unload()

    async def test_irrelevant_config_update_does_not_claim_recognition_change(self):
        with TemporaryDirectory() as tmp:
            plugin = self.make_plugin(Path(tmp), build_config(study={"api_key": ""}))
            await plugin.on_load()
            try:
                with self.assertLogs("test.class-schedule", level="INFO") as captured:
                    await plugin.on_config_update(
                        "self", build_config(study={"api_key": "", "arm_minutes": 30}), "1.0.3"
                    )
                self.assertFalse(
                    any("公式识别已启用" in line for line in captured.output), captured.output
                )
            finally:
                await plugin.on_unload()

    async def test_image_note_survives_caption_dedup(self):
        """回归（线上实测）：同一张课件图重发要能再进一次识别，不能被文案去重挡掉。

        图片的身份由内容 hash 判定（识别缓存），说明文字相同不代表是重复内容——
        用户重发图想再识别一次时，若被文案去重挡住，SQLite 与识别都不会发生。
        """
        import shutil
        import tempfile as _tf

        from class_schedule.notes_db import NotesDatabase
        from class_schedule.pipeline import StudyPipeline

        # LIFO 清理：先注册目录删除（最后执行），再注册关库——Windows 上
        # 文件被连接占用时 rmtree 会失败
        tmp_path = Path(_tf.mkdtemp(prefix="cappedup-"))
        self.addCleanup(shutil.rmtree, tmp_path, True)
        plugin = self.prepare_bare(
            tmp_path,
            config=build_config(
                access={"chat_scope": "private"}, study={"summary_enabled": False}
            ),
        )
        db = NotesDatabase(tmp_path / "notes.db")
        db.initialize()
        self.addCleanup(db.close)  # 后注册先跑：先关库再删目录
        plugin._notes_db = db
        plugin._pipeline = StudyPipeline(db=db)
        plugin._pipeline.start()
        self.addAsyncCleanup(plugin._pipeline.stop)

        payload = base64.b64encode(b"\x89PNGx-caption-dedup").decode()
        message = {
            "session_id": "ps",
            "message_info": {"user_info": {"user_id": "654321"}},
            "raw_message": [
                {"type": "text", "data": {"text": "记一下 这张课件"}},
                {"type": "image", "data": {}, "binary_data_base64": payload},
            ],
        }
        await plugin.handle_note_capture(message=message, stream_id="ps")
        for task in list(plugin._note_tasks):
            await task
        await plugin.handle_note_capture(message=message, stream_id="ps")
        for task in list(plugin._note_tasks):
            await task
        self.assertEqual(len(db.search_text("课件")), 2)

        # 纯文本笔记仍然去重（老行为不变）
        text_message = {
            "session_id": "ps",
            "message_info": {"user_info": {"user_id": "654321"}},
            "raw_message": [{"type": "text", "data": {"text": "记一下 牛顿第二定律"}}],
        }
        await plugin.handle_note_capture(message=text_message, stream_id="ps")
        for task in list(plugin._note_tasks):
            await task
        await plugin.handle_note_capture(message=text_message, stream_id="ps")
        for task in list(plugin._note_tasks):
            await task
        self.assertEqual(len(db.search_text("牛顿第二定律")), 1)


if __name__ == "__main__":
    unittest.main()
