"""用本机安装的麦麦（MaiBot）本体验证本插件。

与 ``tests/`` 下的单元测试不同——那些使用替身上下文；本脚本调用麦麦自己的：

1. ``PluginLoader``：真实 Manifest 校验、依赖解析、包导入与组件收集；
2. 配置归一化代码：生成并读回 ``config.toml``；
3. ``maibot_sdk.PluginContext``：验证消息通过真实 SDK 能力协议发出；
4. 主机与 SnowLuma 适配器的字段契约：命令载荷能提取群号/用户号/会话 ID；
5. 节假日关键语义：放假跳过、调休上班不跳过、缺数据 fail-open。

用法::

    python tools/verify_with_maibot.py
    python tools/verify_with_maibot.py --maibot D:\\path\\to\\MaiBot

退出码 0 表示全部通过。脚本不会联网，也不会启动麦麦主程序；真实 QQ 投递仍需
在配置好 QQ 适配器与 LLM 的麦麦实例中进行。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PLUGIN_ID = "local.class-schedule"
EXPECTED_COMMANDS = {
    "schedule_holiday",
    "schedule_import",
    "schedule_lead",
    "schedule_parse",
    "schedule_reload",
    "schedule_status",
    "schedule_subscribe",
    "schedule_today",
    "schedule_tomorrow",
    "schedule_unsubscribe",
    "schedule_week",
}
EXPECTED_TOOLS = {"query_class_schedule"}
EXPECTED_HOOKS = {
    "schedule_file_intake",
    "schedule_nl_inject",
    "schedule_chat_scope_probe",
}
EXPECTED_HOOK_NAME = "chat.receive.after_process"
EXPECTED_CONFIG_SECTIONS = {
    "access",
    "file_import",
    "holiday",
    "nl_query",
    "message",
    "plugin",
    "reminder",
    "reply",
    "source",
    "target",
}

_MAIBOT_CANDIDATES = (
    Path(r"D:\Maibot\MaiBot OneKey\resources\modules\MaiBot"),
    Path(r"C:\Maibot\MaiBot OneKey\resources\modules\MaiBot"),
    Path.home() / "MaiBot",
    Path.cwd(),
)


class Check:
    """记录检查结果。"""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def ok(self, label: str, detail: str = "") -> None:
        self.passed += 1
        print(f"  [通过] {label}{f' — {detail}' if detail else ''}")

    def fail(self, label: str, detail: str) -> None:
        self.failures.append(f"{label}: {detail}")
        print(f"  [失败] {label} — {detail}")

    def expect(self, label: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.ok(label, detail)
        else:
            self.fail(label, detail or "条件不成立")


def find_maibot(explicit: str | None) -> Path:
    """定位麦麦本体目录（包含 ``src/plugin_runtime`` 的那一层）。"""
    if explicit:
        candidate = Path(explicit)
        if (candidate / "src" / "plugin_runtime").is_dir():
            return candidate
        raise SystemExit(f"指定目录中没有 src/plugin_runtime: {candidate}")
    for candidate in _MAIBOT_CANDIDATES:
        if (candidate / "src" / "plugin_runtime").is_dir():
            return candidate
    raise SystemExit("没找到麦麦本体，请用 --maibot 指定包含 src/plugin_runtime 的目录")


def prepare_workdir() -> Path:
    """创建临时项目根，不改动麦麦安装目录。"""
    workdir = Path(tempfile.mkdtemp(prefix="maibot-verify-"))
    target = workdir / "plugins" / PLUGIN_DIR.name
    shutil.copytree(
        PLUGIN_DIR,
        target,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "tests",
            "tools",
            ".mimosa",
            ".gitignore",
        ),
    )
    return workdir


def write_fixtures(workdir: Path) -> None:
    """预置一门 20 分钟后开始的课与两年假日缓存。"""
    data_dir = workdir / "data" / "plugins" / PLUGIN_ID
    ics_dir = data_dir / "ics"
    ics_dir.mkdir(parents=True, exist_ok=True)

    start = datetime.now() + timedelta(minutes=20)
    end = start + timedelta(minutes=100)
    (ics_dir / "fixture.ics").write_text(
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
        "UID:verify-1\nSUMMARY:验证课\n"
        f"DTSTART;TZID=Asia/Shanghai:{start.strftime('%Y%m%dT%H%M%S')}\n"
        f"DTEND;TZID=Asia/Shanghai:{end.strftime('%Y%m%dT%H%M%S')}\n"
        "LOCATION:教三-201\n"
        "END:VEVENT\nEND:VCALENDAR\n",
        encoding="utf-8",
    )

    year = datetime.now().year
    holiday_dir = data_dir / "holidays"
    holiday_dir.mkdir(parents=True, exist_ok=True)
    for target_year in (year, year + 1):
        (holiday_dir / f"{target_year}.json").write_text(
            json.dumps(
                {
                    "year": target_year,
                    "days": [
                        {
                            "name": "元旦",
                            "date": f"{target_year}-01-01",
                            "isOffDay": True,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


def check_loader(maibot: Path, workdir: Path, report: Check) -> str:
    """阶段一：用麦麦真实加载器加载插件，返回合成模块名。"""
    print("\n[1/5] 用麦麦真实加载器加载插件")
    sys.path.insert(0, str(maibot))
    os.chdir(maibot)

    from src.plugin_runtime import detect_host_application_version
    from src.plugin_runtime.runner.plugin_loader import PluginLoader

    host_version = detect_host_application_version()
    report.ok("读取麦麦 host 版本", host_version)

    loader = PluginLoader(host_version=host_version)
    candidates, conflicts = loader.discover_candidates([str(workdir / "plugins")])
    report.expect(
        "插件清单通过真实校验",
        PLUGIN_ID in candidates,
        f"失败原因: {loader.failed_plugins}" if PLUGIN_ID not in candidates else "",
    )
    if PLUGIN_ID not in candidates:
        return ""
    report.expect("插件 ID 无冲突", not conflicts, str(conflicts))

    order, failed = loader.resolve_dependencies(candidates)
    report.expect("依赖解析通过", not failed, str(failed))
    report.expect("插件进入加载顺序", PLUGIN_ID in order, str(order))

    meta = loader.load_candidate(PLUGIN_ID, candidates[PLUGIN_ID])
    report.expect("插件加载成功", meta is not None, str(loader.failed_plugins))
    if meta is None:
        return ""

    instance = meta.instance
    components = instance.get_components()
    commands = {item["name"] for item in components if item["type"] == "COMMAND"}
    tools = {item["name"] for item in components if item["type"] == "TOOL"}
    hooks = {item["name"] for item in components if item["type"] == "HOOK_HANDLER"}

    report.ok("模块名（加载器合成）", meta.module_name)
    report.ok("插件实例类", type(instance).__name__)
    report.expect("11 个命令全部注册", commands == EXPECTED_COMMANDS, str(sorted(commands)))
    report.expect("1 个 Tool 已注册", tools == EXPECTED_TOOLS, str(sorted(tools)))
    report.expect(
        "三个 Hook 都已注册（文件导入 + 课表注入 + 会话类型记录）",
        hooks == EXPECTED_HOOKS,
        str(sorted(hooks)),
    )
    hook_meta = {
        item["name"]: item["metadata"]
        for item in components
        if item["type"] == "HOOK_HANDLER"
    }
    probe_info = hook_meta.get("schedule_chat_scope_probe", {})
    report.expect(
        "类型记录 Hook 也订阅 chat.receive.after_process（拿得到消息体）",
        str(probe_info.get("hook", "")) == EXPECTED_HOOK_NAME,
        str(probe_info.get("hook")),
    )
    report.expect(
        "类型记录 Hook 是非阻塞 OBSERVE",
        str(probe_info.get("mode", "")).lower().endswith("observe"),
        str(probe_info.get("mode")),
    )
    hook_info = hook_meta.get("schedule_file_intake", {})
    report.expect(
        "Hook 订阅的是 chat.receive.after_process",
        str(hook_info.get("hook", "")) == EXPECTED_HOOK_NAME,
        str(hook_info.get("hook")),
    )
    report.expect(
        "Hook 用非阻塞 OBSERVE 模式（不拖慢消息链路）",
        str(hook_info.get("mode", "")).lower().endswith("observe"),
        str(hook_info.get("mode")),
    )

    missing_handlers = [
        item["name"]
        for item in components
        if not callable(
            getattr(instance, item["metadata"].get("handler_name", ""), None)
        )
    ]
    report.expect(
        "组件处理器可按 handler_name 取到",
        not missing_handlers,
        f"取不到: {missing_handlers}",
    )

    defaults = instance.build_default_config()
    schema_sections = set(instance.build_config_schema()["sections"])
    report.expect(
        "默认配置节完整",
        set(defaults) == EXPECTED_CONFIG_SECTIONS,
        str(sorted(defaults)),
    )
    report.expect(
        "WebUI Schema 节完整",
        schema_sections == EXPECTED_CONFIG_SECTIONS,
        str(sorted(schema_sections)),
    )

    lifecycle = [
        name
        for name in ("on_load", "on_unload", "on_config_update")
        if callable(getattr(instance, name, None))
    ]
    report.expect("三个生命周期方法齐全", len(lifecycle) == 3, str(lifecycle))
    return meta.module_name


def check_config_file(workdir: Path, module_name: str, report: Check) -> None:
    """阶段二：用麦麦配置重建逻辑生成并读回 config.toml。"""
    print("\n[2/5] 用麦麦的配置归一化生成 config.toml")
    import tomlkit

    from maibot_sdk.config import extract_plugin_config_version
    from src.plugin_runtime.runner.runner_main import rebuild_plugin_config_data

    module = __import__(module_name, fromlist=["create_plugin"])
    instance = module.create_plugin()
    default_config = instance.build_default_config()
    config_data = rebuild_plugin_config_data(default_config, {})

    version = extract_plugin_config_version(config_data)
    report.expect("config_version 可被 SDK 提取", bool(version), version)

    plugin_dir = workdir / "plugins" / PLUGIN_DIR.name
    config_path = plugin_dir / "config.toml"
    config_path.write_text(tomlkit.dumps(config_data), encoding="utf-8")
    written = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    report.expect(
        "config.toml 写入并读回",
        set(written) == EXPECTED_CONFIG_SECTIONS,
        str(sorted(written)),
    )

    instance.set_plugin_config(dict(written))
    report.expect(
        "配置注入插件成功",
        instance._conf().reminder.remind_before_minutes == 20,
        f"提前 {instance._conf().reminder.remind_before_minutes} 分钟",
    )
    # 适用范围必须默认只走私聊：课表是个人日程，发到群里等于公开
    report.expect(
        "默认适用范围是仅私聊",
        instance._conf().access.chat_scope == "private",
        f"chat_scope={instance._conf().access.chat_scope}",
    )
    report.ok("config.toml 位置", str(config_path.relative_to(workdir)))


def check_runtime(workdir: Path, module_name: str, report: Check) -> None:
    """阶段三：真实 PluginContext 下运行生命周期、命令、提醒与热更新。"""
    print("\n[3/5] 真实 SDK PluginContext 下运行插件")
    import tomlkit

    from maibot_sdk import PluginContext, PluginPaths

    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def rpc_call(
        method: str,
        plugin_id: str,
        payload: dict[str, Any],
        timeout_ms: int | None = None,
    ) -> Any:
        calls.append((method, plugin_id, dict(payload or {})))
        args = (payload or {}).get("args", {})
        capability = (payload or {}).get("capability")
        if capability in ("send.text", "send.custom"):
            return {"success": True, "sent": True, "message_id": "verify-1"}
        if capability == "llm.generate":
            return {"success": True, "response": "（模型按人设生成的一句话）"}
        if capability == "config.get":
            host_values = {
                "bot.nickname": "验证bot",
                "personality.personality": "测试人设",
                "personality.reply_style": "简短",
                "bot.qq_account": "10000",
            }
            return {"success": True, "value": host_values.get(str(args.get("key") or ""))}
        if capability == "message.get_recent":
            return {"success": True, "messages": []}
        return {"success": True}

    data_dir = workdir / "data" / "plugins" / PLUGIN_ID
    paths = PluginPaths(
        data_dir=data_dir,
        runtime_dir=workdir / "temp" / "plugins" / PLUGIN_ID,
    )
    ctx = PluginContext(plugin_id=PLUGIN_ID, rpc_call=rpc_call, paths=paths)

    module = __import__(module_name, fromlist=["create_plugin"])
    plugin = module.create_plugin()
    plugin._set_context(ctx)
    config_path = workdir / "plugins" / PLUGIN_DIR.name / "config.toml"
    plugin.set_plugin_config(dict(tomlkit.parse(config_path.read_text(encoding="utf-8"))))

    async def scenario() -> None:
        await plugin.on_load()
        try:
            report.expect("on_load 后提醒循环已启动", plugin._loop_task is not None)
            report.expect(
                "课表已解析",
                plugin._repo is not None and len(plugin._repo.events) == 1,
                f"{len(plugin._repo.events) if plugin._repo else 0} 条课程",
            )
            report.expect(
                "节假日缓存已载入（未联网）",
                plugin._holidays is not None and not plugin._holidays.is_empty(),
                f"年份 {plugin._holidays.years if plugin._holidays else []}",
            )

            # 真实日历导出的形态：带 VALARM，且用日期型 EXDATE 标停课。
            # 两者以前都会出错——VALARM 的 SUMMARY 会顶掉课程名，
            # 日期型 EXDATE 匹配不上导致停课的周照旧提醒
            gcal_start = datetime.now() + timedelta(days=1)
            (plugin._data_dir / "ics" / "gcal.ics").write_text(
                "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
                "UID:gcal-1\nSUMMARY:真实日历课\nDESCRIPTION:教师-李四\n"
                f"DTSTART:{gcal_start.strftime('%Y%m%dT%H%M%S')}\n"
                f"DTEND:{(gcal_start + timedelta(minutes=100)).strftime('%Y%m%dT%H%M%S')}\n"
                "RRULE:FREQ=WEEKLY;COUNT=4\n"
                f"EXDATE;VALUE=DATE:{gcal_start.strftime('%Y%m%d')}\n"
                "BEGIN:VALARM\nACTION:EMAIL\nTRIGGER:-PT20M\n"
                "SUMMARY:Alarm summary\nDESCRIPTION:This is an event reminder\n"
                "END:VALARM\n"
                "END:VEVENT\nEND:VCALENDAR\n",
                encoding="utf-8",
            )
            plugin._repo.refresh(force=True)
            imported = [
                item for item in plugin._repo.events if item.uid == "gcal-1"
            ]
            report.expect(
                "带 VALARM 的日历导入后课程名不被提醒器文案顶掉",
                len(imported) == 1 and imported[0].summary == "真实日历课",
                imported[0].summary if imported else "<没解析出来>",
            )
            report.expect(
                "日期型 EXDATE 被识别为当天停课",
                bool(imported)
                and list(imported[0].exdate_days) == [gcal_start.date()],
                str(list(imported[0].exdate_days)) if imported else "<无>",
            )
            # 清理这份临时课表，别影响后面的提醒判定
            (plugin._data_dir / "ics" / "gcal.ics").unlink()
            plugin._repo.refresh(force=True)

            # 形状与麦麦命令执行器真实 invoke_args 一致。
            # 用**私聊**消息：插件默认 access.chat_scope=private，只在私聊工作，
            # 这也正是这个插件的目标场景
            message = {
                "session_id": "verify-stream",
                "message_info": {
                    "user_info": {
                        "user_id": "654321",
                        "user_nickname": "验证者",
                        # 私聊没有群名片，主机会给空值
                        "user_cardname": "",
                    },
                },
            }
            ok, reply, _ = await plugin.handle_subscribe(
                stream_id="verify-stream",
                user_id="654321",
                message=message,
            )
            report.expect("命令 /课表订阅 执行成功", bool(ok), str(reply))
            record = plugin._state.find("verify-stream")
            report.expect(
                "订阅记录用户号与昵称",
                record is not None
                and record.user_id == "654321"
                and record.label == "验证者"
                and record.chat_type == "private",
                f"user={getattr(record, 'user_id', None)} "
                f"label={getattr(record, 'label', None)} "
                f"type={getattr(record, 'chat_type', None)}",
            )

            # 适用范围：同一条命令在群里必须被拒绝（默认只走私聊）
            group_message = {
                "session_id": "verify-group",
                "message_info": {
                    "group_info": {"group_id": "123456", "group_name": "验证群"},
                    "user_info": {"user_id": "654321"},
                },
            }
            group_denied, group_reply, _ = await plugin.handle_subscribe(
                stream_id="verify-group",
                group_id="123456",
                user_id="654321",
                message=group_message,
            )
            report.expect(
                "群聊里同样的命令被拒绝（chat_scope=private）",
                not group_denied and "私聊" in group_reply,
                str(group_reply),
            )
            report.expect(
                "被拒绝的群聊没有留下订阅记录",
                plugin._state.find("verify-group") is None,
                str(plugin._state.find("verify-group")),
            )

            # 跑一轮提醒判定：先清空调用记录，只看这一轮发了什么
            calls.clear()
            await plugin._tick()

            # 默认是 persona：文案由模型按宿主人设生成，再由插件直发（必达）
            llm_calls = [c for c in calls if c[2].get("capability") == "llm.generate"]
            send_calls = [c for c in calls if c[2].get("capability") == "send.text"]
            report.expect(
                "persona 模式先让模型生成文案",
                len(llm_calls) == 1,
                str([c[2].get("capability") for c in calls]),
            )
            report.expect(
                "persona 模式随后直发（不依赖主链路开口）",
                len(send_calls) == 1,
                str([c[2].get("capability") for c in calls]),
            )
            if send_calls:
                text = str(send_calls[0][2].get("args", {}).get("text", ""))
                report.expect(
                    "发出去的是模型生成的文案",
                    "模型按人设生成的一句话" in text,
                    text[:60].replace(chr(10), " | "),
                )
            # 自然语言问课：注入课表到规划器上下文
            # 真实链路里 chat.receive.after_process 先于规划器触发，
            # 注入用的会话类型就来自那一步记下的表，这里复刻同一顺序
            await plugin.record_chat_type_handler(
                stream_id="verify-stream", message=message
            )
            await plugin.record_chat_type_handler(
                stream_id="verify-group", message=group_message
            )
            report.expect(
                "会话类型已记录（私聊/群聊各自认得）",
                plugin._stream_types.get("verify-stream") == "private"
                and plugin._stream_types.get("verify-group") == "group",
                str(plugin._stream_types),
            )
            planner_items = [
                {
                    "item_type": "SystemMessageItem",
                    "parts": [{"type": "text", "text": "你是验证bot"}],
                },
                {
                    "item_type": "UserMessageItem",
                    "parts": [{"type": "text", "text": "明天有课吗"}],
                },
            ]
            inject_result = await plugin.inject_schedule_handler(
                items=list(planner_items),
                item_schema_version=1,
                tool_definitions=[],
                selected_history_count=2,
                built_message_count=2,
                selection_reason="verify",
                session_id="verify-stream",
            )
            injected_items = (inject_result.get("modified_kwargs") or {}).get("items")
            report.expect(
                "聊到课表时注入课表上下文",
                isinstance(injected_items, list) and len(injected_items) == len(planner_items) + 1,
                f"items={len(injected_items) if isinstance(injected_items, list) else injected_items}",
            )
            injected_text = ""
            if isinstance(injected_items, list) and len(injected_items) > 1:
                parts = injected_items[1].get("parts") or []
                injected_text = str((parts[0] or {}).get("text", "")) if parts else ""
            report.expect(
                "注入内容含课表事实",
                "验证课" in injected_text and "【课表】" in injected_text,
                injected_text[:80].replace(chr(10), " | "),
            )
            unrelated = await plugin.inject_schedule_handler(
                items=[
                    {
                        "item_type": "UserMessageItem",
                        "parts": [{"type": "text", "text": "今天天气怎么样"}],
                    }
                ],
                item_schema_version=1,
                tool_definitions=[],
                selected_history_count=1,
                built_message_count=1,
                selection_reason="verify",
                session_id="verify-stream",
            )
            report.expect(
                "闲聊时不注入（省 token）",
                "modified_kwargs" not in unrelated,
                str(list(unrelated.keys())),
            )

            # 群聊：既不注入课表，这是最直接的隐私保护
            group_inject = await plugin.inject_schedule_handler(
                items=[dict(item) for item in planner_items],
                item_schema_version=1,
                tool_definitions=[],
                selected_history_count=2,
                built_message_count=2,
                selection_reason="verify",
                session_id="verify-group",
            )
            report.expect(
                "群聊里不注入课表（chat_scope=private）",
                "modified_kwargs" not in group_inject,
                str(list(group_inject.keys())),
            )
            # 对照：类型未知时同样不放行（入站失败关闭）
            unknown_inject = await plugin.inject_schedule_handler(
                items=[dict(item) for item in planner_items],
                item_schema_version=1,
                tool_definitions=[],
                selected_history_count=2,
                built_message_count=2,
                selection_reason="verify",
                session_id="never-seen-stream",
            )
            report.expect(
                "没记录过类型的会话也不注入（入站失败关闭）",
                "modified_kwargs" not in unknown_inject,
                str(list(unknown_inject.keys())),
            )

            report.expect(
                "persona 模式不使用 proactive",
                not [c for c in calls if c[2].get("capability") == "maisaka.proactive.trigger"],
                str([c[2].get("capability") for c in calls]),
            )

            # proactive：交给主链路，并登记一次发言验证
            proactive_config = dict(plugin.get_plugin_config_data())
            proactive_config["reply"] = {
                **proactive_config.get("reply", {}),
                "style": "proactive",
            }
            plugin.set_plugin_config(proactive_config)
            plugin._state.fired.clear()
            calls.clear()
            await plugin._tick()
            trigger_calls = [
                c for c in calls if c[2].get("capability") == "maisaka.proactive.trigger"
            ]
            report.expect(
                "proactive 模式交给 replyer（maisaka.proactive.trigger）",
                len(trigger_calls) == 1,
                str([c[2].get("capability") for c in calls]),
            )
            if trigger_calls:
                args = trigger_calls[0][2].get("args", {})
                intent = str(args.get("intent", ""))
                report.expect(
                    "触发目标会话正确",
                    args.get("stream_id") == "verify-stream",
                    str(args.get("stream_id")),
                )
                report.expect(
                    "意图里带上完整事实",
                    "验证课" in intent and "20 分钟后" in intent,
                    intent[:70].replace(chr(10), " | "),
                )
            report.expect(
                "已登记发言验证（超时未开口会兜底）",
                len(plugin._pending_proactive) == 1,
                f"{len(plugin._pending_proactive)} 条待验证",
            )

            # fixed：模板原文直发
            fixed_config = dict(plugin.get_plugin_config_data())
            fixed_config["reply"] = {**fixed_config.get("reply", {}), "style": "fixed"}
            plugin.set_plugin_config(fixed_config)
            plugin._state.fired.clear()
            calls.clear()
            await plugin._tick()
            send_calls = [c for c in calls if c[2].get("capability") == "send.text"]
            report.expect(
                "fixed 模式直接发模板文本",
                len(send_calls) == 1
                and not [c for c in calls if c[2].get("capability") == "llm.generate"],
                str([c[2].get("capability") for c in calls]),
            )
            if send_calls:
                text = str(send_calls[0][2].get("args", {}).get("text", ""))
                report.expect(
                    "模板文案含课程与提前量",
                    "验证课" in text and "20 分钟后" in text,
                    text[:70].replace(chr(10), " | "),
                )

            updated = dict(plugin.get_plugin_config_data())
            updated["reminder"] = {
                **updated.get("reminder", {}),
                "remind_before_minutes": 45,
            }
            await plugin.on_config_update("self", updated, "1.0.0")
            report.expect(
                "热更新立即生效",
                plugin._default_lead_minutes() == 45,
                f"默认提前 {plugin._default_lead_minutes()} 分钟",
            )
        finally:
            await plugin.on_unload()

        report.expect("on_unload 后提醒循环已停止", plugin._loop_task is None)
        report.expect("on_unload 后假日任务已停止", plugin._holiday_task is None)

    asyncio.run(scenario())


def check_host_contract(module_name: str, report: Check) -> None:
    """阶段四：核对主机与 SnowLuma 适配器真实下发的字段契约。

    麦麦 1.2.0 的命令执行器会补上 ``stream_id``/``group_id``/``user_id``；
    SnowLuma 适配器（``plugins/snowluma-adapter``）构造的 ``message`` 里，
    群号在 ``message_info.group_info`` 下且**群聊才有这个键**，``platform`` 是
    ``"qq"``，它自己填的 ``session_id`` 是空串（真会话 ID 由主机给）。
    Tool 则只传 LLM 给出的参数，没有会话信息。
    """
    print("\n[4/5] 主机与 SnowLuma 适配器的字段契约（对照真实源码结构）")
    access = __import__(f"{module_name}.access", fromlist=["identity_from_kwargs"])
    identity_from_kwargs = access.identity_from_kwargs

    snowluma_group_message = {
        "message_id": "12345",
        "platform": "qq",
        "message_info": {
            "user_info": {
                "user_id": "987654321",
                "user_nickname": "小明",
                "user_cardname": "小明（高数三班）",
            },
            "additional_config": {"snowluma_message_type": "group"},
            "group_info": {"group_id": "123456789", "group_name": "高数三班"},
        },
        "raw_message": [{"type": "text", "data": "/课表"}],
        "is_command": True,
        "session_id": "",
        "processed_plain_text": "/课表",
    }
    group_kwargs = {
        "stream_id": "session-abc123",
        "group_id": "123456789",
        "platform": "qq",
        "user_id": "987654321",
        "message": snowluma_group_message,
    }

    identity = identity_from_kwargs(group_kwargs)
    report.expect(
        "命令载荷能取出群号/用户号/会话 ID",
        identity.group_id == "123456789"
        and identity.user_id == "987654321"
        and identity.stream_id == "session-abc123",
        f"group={identity.group_id} user={identity.user_id} stream={identity.stream_id}",
    )
    report.expect("群聊类型判定", identity.chat_type == "group", identity.chat_type)
    report.expect("群聊展示名取群名", identity.label == "高数三班", identity.label)

    message_only = identity_from_kwargs({"message": snowluma_group_message})
    report.expect(
        "仅凭 SnowLuma 的 message 也能取出群号与用户号",
        message_only.group_id == "123456789" and message_only.user_id == "987654321",
        f"group={message_only.group_id} user={message_only.user_id}",
    )

    private_message = {
        "message_id": "12346",
        "platform": "qq",
        "message_info": {
            "user_info": {
                "user_id": "987654321",
                "user_nickname": "小明",
                "user_cardname": "",
            },
            "additional_config": {"snowluma_message_type": "private"},
        },
        "session_id": "",
        "processed_plain_text": "/课表订阅",
    }
    private_identity = identity_from_kwargs(
        {"stream_id": "session-private", "user_id": "987654321", "message": private_message}
    )
    report.expect(
        "私聊（没有 group_info 键）判为私聊且展示名退回昵称",
        private_identity.chat_type == "private" and private_identity.label == "小明",
        f"type={private_identity.chat_type} label={private_identity.label}",
    )

    tool_identity = identity_from_kwargs({"scope": "today"})
    report.expect(
        "Tool 载荷没有会话信息（名单管不住 Tool）",
        tool_identity.identifiers == [],
        str(tool_identity.identifiers),
    )


def check_holiday_data(module_name: str, report: Check) -> None:
    """阶段五：核对节假日关键语义。"""
    print("\n[5/5] 节假日判定语义")
    holidays = __import__(f"{module_name}.holidays", fromlist=["HolidayCalendar"])
    calendar = holidays.HolidayCalendar()
    calendar.load_payload(
        {
            "year": 2026,
            "days": [
                {"name": "元旦", "date": "2026-01-01", "isOffDay": True},
                {"name": "元旦", "date": "2026-01-04", "isOffDay": False},
            ],
        }
    )
    report.expect("放假当天判为假日", calendar.is_off_day(date(2026, 1, 1)))
    report.expect(
        "调休上班日不算假日",
        not calendar.is_off_day(date(2026, 1, 4)),
    )
    report.expect(
        "无数据年份 fail-open",
        not calendar.is_off_day(date(2030, 1, 1)),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="用本机麦麦验证课程表提醒插件")
    parser.add_argument("--maibot", help="麦麦本体目录（包含 src/plugin_runtime）")
    args = parser.parse_args()

    maibot = find_maibot(args.maibot)
    print(f"麦麦目录: {maibot}")
    print(f"插件目录: {PLUGIN_DIR}")

    report = Check()
    workdir = prepare_workdir()
    print(f"临时项目根: {workdir}")
    write_fixtures(workdir)

    original_cwd = Path.cwd()
    try:
        module_name = check_loader(maibot, workdir, report)
        if module_name:
            check_config_file(workdir, module_name, report)
            check_runtime(workdir, module_name, report)
            check_host_contract(module_name, report)
            check_holiday_data(module_name, report)
    except Exception as exc:
        report.fail("验证脚本异常", f"{type(exc).__name__}: {exc}")
        raise
    finally:
        os.chdir(original_cwd)

    print(f"\n{'=' * 60}")
    if report.failures:
        print(f"未通过 {len(report.failures)} 项（通过 {report.passed} 项）：")
        for item in report.failures:
            print(f"  - {item}")
        print(f"\n临时目录保留以便排查: {workdir}")
        return 1

    shutil.rmtree(workdir, ignore_errors=True)
    print(f"全部通过（共 {report.passed} 项检查），临时目录已清理")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
