"""插件配置模型。

按 SDK 约定分节声明：``[plugin]`` 是必需的基础节（``config_version``
缺失时 Runner 会拒绝加载），其余节对应 WebUI 上的一个配置分组。
"""

from __future__ import annotations

from typing import Literal

from maibot_sdk import Field, PluginConfigBase

from .holidays import DEFAULT_SOURCE_URL_TEMPLATE
from .reminder import DEFAULT_TEMPLATE

__all__ = [
    "AccessSettingsConfig",
    "ClassScheduleConfig",
    "FileImportSettingsConfig",
    "HolidaySettingsConfig",
    "NaturalQuerySettingsConfig",
    "MessageSettingsConfig",
    "PluginBaseConfig",
    "ReminderSettingsConfig",
    "ReplySettingsConfig",
    "SourceSettingsConfig",
    "TargetSettingsConfig",
]


class PluginBaseConfig(PluginConfigBase):
    """插件基础配置（SDK 必需节）。"""

    __ui_label__ = "插件基础"

    config_version: str = Field(
        default="1.0.0",
        description="配置版本号，由 Runner 维护",
        json_schema_extra={"label": "配置版本", "disabled": True, "order": 0},
    )
    enabled: bool = Field(
        default=True,
        description="是否启用插件（关闭后不再提醒，命令仍可查看课表）",
        json_schema_extra={"label": "启用插件", "order": 1},
    )


class ReminderSettingsConfig(PluginConfigBase):
    """上课提醒配置。"""

    __ui_label__ = "提醒设置"
    __ui_icon__ = "alarm"

    enable_reminder: bool = Field(
        default=True,
        description="是否开启上课前主动提醒",
        json_schema_extra={"label": "开启上课提醒", "order": 0},
    )
    remind_before_minutes: int = Field(
        default=20,
        ge=0,
        le=1440,
        description="上课前多少分钟发送提醒。默认 20，设为 0 表示上课时提醒",
        json_schema_extra={"label": "提前分钟数", "order": 1, "step": 1},
    )
    check_interval_seconds: int = Field(
        default=60,
        ge=15,
        le=3600,
        description=(
            "多久检查一次课表，越小越准时。默认 60 秒。"
            "注意：间隔若比「提前量 + 补发宽限」还长，会让部分课程来不及提醒，"
            "插件会自动放宽补发窗口以保证不漏（代价是提醒可能迟到），并在启动时告警"
        ),
        json_schema_extra={"label": "检查间隔（秒）", "order": 2, "step": 5},
    )
    late_grace_seconds: int = Field(
        default=60,
        ge=0,
        le=1800,
        description=(
            "课已经开始多久之后就不再补发提醒。用于机器人重启后"
            "补发漏掉的提醒；设为 0 表示一旦开始就不再提醒。"
            "提前量设为 0 时此项会自动放宽到至少一个检查周期，否则会漏提醒"
        ),
        json_schema_extra={"label": "补发宽限（秒）", "order": 3, "step": 10},
    )


class TargetSettingsConfig(PluginConfigBase):
    """提醒对象配置。"""

    __ui_label__ = "提醒对象"
    __ui_icon__ = "users"

    target_streams: list[str] = Field(
        default_factory=list,
        description=(
            "额外固定推送的会话 ID（群聊与私聊都可以）。推荐直接在目标会话里"
            "发送 /课表订阅 自动登记，用 /课表状态 可查看各会话的 ID"
        ),
        json_schema_extra={"label": "固定提醒会话 ID", "order": 0, "rows": 4},
    )
    max_subscriptions: int = Field(
        default=20,
        ge=1,
        le=200,
        description="最多允许登记多少个提醒会话，防止被大量订阅撑爆",
        json_schema_extra={"label": "提醒会话数上限", "order": 1, "step": 1},
    )


class AccessSettingsConfig(PluginConfigBase):
    """访问名单与适用范围配置。"""

    __ui_label__ = "访问名单"
    __ui_icon__ = "shield"

    chat_scope: Literal["private", "group", "both"] = Field(
        default="private",
        description=(
            "允许在哪些类型的会话里工作。private = 只在私聊（默认）：群聊里的命令、"
            "文件导入、课表注入都不响应，群聊订阅也不投递；both = 私聊和群聊都行；"
            "group = 只在群聊。课表是个人日程，提醒发到群里等于公开，"
            "所以默认只在私聊工作"
        ),
        json_schema_extra={"label": "适用范围", "order": 5},
    )
    mode: Literal["off", "whitelist", "blacklist"] = Field(
        default="off",
        description=(
            "名单模式：off 不做限制；whitelist 只有名单内的会话能用；"
            "blacklist 名单内的会话被拒绝"
        ),
        json_schema_extra={"label": "名单模式", "order": 0},
    )
    entries: list[str] = Field(
        default_factory=list,
        description=(
            "名单内容，一行一个：可填群号、用户号或会话 ID，"
            "也支持 qq:123456 这种带平台前缀的写法。"
            "群号/用户号在部分适配器上取不到，此时请改用会话 ID"
        ),
        json_schema_extra={"label": "名单内容", "order": 1, "rows": 5},
    )
    apply_to_commands: bool = Field(
        default=True,
        description="名单是否限制命令（建议保持开启，否则名单外的会话仍能订阅和改设置）",
        json_schema_extra={"label": "限制命令", "order": 2},
    )
    apply_to_reminders: bool = Field(
        default=True,
        description="名单是否限制提醒投递（会话被拉黑后自动停止收到提醒）",
        json_schema_extra={"label": "限制提醒投递", "order": 3},
    )
    tool_query_enabled: bool = Field(
        default=True,
        description=(
            "是否允许麦麦用 LLM 工具查询课表。注意：工具调用拿不到会话信息"
            "（麦麦只把模型给的参数传下来，见 README「七」），所以名单管不住它。"
            "如果机器人也在你不放心的群里，请关掉这一项"
        ),
        json_schema_extra={"label": "允许 LLM 查询课表", "order": 4},
    )


class HolidaySettingsConfig(PluginConfigBase):
    """法定节假日检测配置。"""

    __ui_label__ = "法定节假日"
    __ui_icon__ = "calendar-off"

    skip_off_days: bool = Field(
        default=True,
        description=(
            "法定节假日（含春节、国庆等）不发送上课提醒。"
            "调休上班日不算假日，会照常提醒"
        ),
        json_schema_extra={"label": "节假日不提醒", "order": 0},
    )
    source_url_template: str = Field(
        default=DEFAULT_SOURCE_URL_TEMPLATE,
        description=(
            "节假日数据来源，{year} 会替换成四位年份。"
            "默认取由国务院公告生成的公开数据；只允许公网 http/https 地址"
        ),
        json_schema_extra={"label": "数据来源", "order": 1, "rows": 3},
    )
    extra_dates: list[str] = Field(
        default_factory=list,
        description=(
            "校历自定假日，一行一个。YYYY-MM-DD 表示只算这一年；"
            "MM-DD 表示每年同一天（如校庆、寒暑假）"
        ),
        json_schema_extra={"label": "自定假日", "order": 2, "rows": 4},
    )
    exclude_dates: list[str] = Field(
        default_factory=list,
        description=(
            "反向设置：这天要上课，即使放假表说是假日也照常提醒。"
            "用于学校在法定假日补课的情况，写法同上"
        ),
        json_schema_extra={"label": "照常上课日", "order": 3, "rows": 3},
    )
    refresh_hours: int = Field(
        default=12,
        ge=0,
        le=720,
        description=(
            "数据多久算过期并重新下载一次；0 表示只在插件启动时检查。"
            "次年放假安排未公布时也按这个间隔再试，不会反复下载刷日志"
        ),
        json_schema_extra={"label": "刷新间隔（小时）", "order": 4, "step": 1},
    )
    timeout_seconds: int = Field(
        default=15,
        ge=5,
        le=120,
        description="下载节假日数据的超时",
        json_schema_extra={"label": "下载超时（秒）", "order": 5, "step": 5},
    )


class ReplySettingsConfig(PluginConfigBase):
    """回复风格配置。"""

    __ui_label__ = "回复风格"
    __ui_icon__ = "sparkles"

    style: Literal["persona", "fixed", "proactive"] = Field(
        default="persona",
        description=(
            "上课提醒的发送方式。persona = 让麦麦按宿主的人设生成一句话再直发"
            "（有风格、必定送达）；fixed = 插件模板原文直发（不走模型）；"
            "proactive = 交给主链路的 replyer 开口（最像本人，但可能延迟或不发声，"
            "插件会按 fallback_after_seconds 验证并兜底）"
        ),
        json_schema_extra={"label": "提醒发送方式", "order": 0},
    )
    ack_style: Literal["persona", "fixed", "proactive"] = Field(
        default="persona",
        description=(
            "提示语与导入回执的发送方式，取值同上。这类内容的作用是告诉用户"
            "下一步做什么，建议保持 persona 或 fixed，别用会静默的 proactive"
        ),
        json_schema_extra={"label": "回执发送方式", "order": 1},
    )
    fallback_after_seconds: int = Field(
        default=90,
        ge=0,
        le=600,
        description=(
            "仅 proactive 模式有效：交给 replyer 后等这么久，仍检测不到麦麦发言"
            "就兜底直发（0 表示不验证，完全信任主链路）"
        ),
        json_schema_extra={"label": "兜底等待（秒）", "order": 2, "step": 10},
    )
    persona_hint: str = Field(
        default="",
        description=(
            "给 replyer 的额外要求，会附在提醒任务里，例如「用简短的一句话，"
            "别加表情」。留空则不加额外约束"
        ),
        json_schema_extra={"label": "风格补充要求", "order": 3, "rows": 3},
    )
    fallback_to_fixed: bool = Field(
        default=True,
        description="replyer 明确拒绝接手时（会话不存在、任务被拒）自动退回直发",
        json_schema_extra={"label": "失败时回退直发", "order": 4},
    )
    persona_timeout_seconds: int = Field(
        default=12,
        ge=0,
        le=120,
        description=(
            "persona 模式生成文案的超时（秒）。提醒循环是同步等文案的，模型卡住会"
            "拖慢整轮检查、极端情况下错过某节课的提醒窗口；超时后自动改用模板文案"
            "直发。0 表示不限时（模型很慢又不想丢风格时再考虑）"
        ),
        json_schema_extra={"label": "文案生成超时（秒）", "order": 5, "step": 1},
    )


class FileImportSettingsConfig(PluginConfigBase):
    """聊天文件导入配置。"""

    __ui_label__ = "聊天文件导入"
    __ui_icon__ = "upload"

    enabled: bool = Field(
        default=True,
        description=(
            "允许在聊天里直接发 .ics 文件导入课表。"
            "同名文件重发即更新，不会堆积旧课表"
        ),
        json_schema_extra={"label": "允许发文件导入", "order": 0},
    )
    allowed_hosts: list[str] = Field(
        default_factory=list,
        description=(
            "显式放行哪些主机可以下载平台给出的文件地址，"
            "一行一个，形如 127.0.0.1:3000 或 127.0.0.1。"
            "默认留空 = 只允许公网地址；若适配器的文件服务在本机，"
            "需要在这里点名，插件日志会提示当前被拒的地址"
        ),
        json_schema_extra={"label": "文件地址放行主机", "order": 1, "rows": 3},
    )
    max_kb: int = Field(
        default=5120,
        ge=64,
        le=51200,
        description="允许导入的文件大小上限",
        json_schema_extra={"label": "大小上限（KB）", "order": 2, "step": 64},
    )
    timeout_seconds: int = Field(
        default=20,
        ge=5,
        le=120,
        description="下载聊天文件时的超时",
        json_schema_extra={"label": "下载超时（秒）", "order": 3, "step": 5},
    )
    arm_minutes: int = Field(
        default=10,
        ge=1,
        le=120,
        description=(
            "发送 /课程解析 后会等待这么久，期间你发的文件会被当成课表处理"
            "（发错文件会明确提示，而不是静默忽略）"
        ),
        json_schema_extra={"label": "等待文件时长（分钟）", "order": 4, "step": 1},
    )


class NaturalQuerySettingsConfig(PluginConfigBase):
    """自然语言问课配置。"""

    __ui_label__ = "自然语言问课"
    __ui_icon__ = "message-circle"

    enabled: bool = Field(
        default=True,
        description=(
            "把课表注入聊天上下文，让麦麦能用人设自然回答「明天几点有课」"
            "这类问题，不依赖它是否决定调用工具"
        ),
        json_schema_extra={"label": "开启自然语言问课", "order": 0},
    )
    mode: Literal["on_topic", "always"] = Field(
        default="on_topic",
        description=(
            "on_topic = 只在聊到课表相关话题时注入（省 token）；"
            "always = 每条消息都注入（更主动，但每条消息都多一些 token）"
        ),
        json_schema_extra={"label": "注入时机", "order": 1},
    )
    days: int = Field(
        default=3,
        ge=1,
        le=14,
        description="注入未来几天的课表",
        json_schema_extra={"label": "注入天数", "order": 2, "step": 1},
    )
    max_lines: int = Field(
        default=12,
        ge=1,
        le=60,
        description="注入文本最多列几条课，用于控制 token 开销",
        json_schema_extra={"label": "最多条数", "order": 3, "step": 1},
    )
    extra_keywords: list[str] = Field(
        default_factory=list,
        description=(
            "额外的触发词，一行一个。当你的课程名有特殊叫法"
            "（如「大物」「工训」）时，把这些词加上，聊到它们也会注入课表"
        ),
        json_schema_extra={"label": "额外触发词", "order": 4, "rows": 3},
    )


class SourceSettingsConfig(PluginConfigBase):
    """课表来源配置。"""

    __ui_label__ = "课表来源"
    __ui_icon__ = "calendar"

    ics_dir: str = Field(
        default="ics",
        description=(
            "存放 .ics 课表的子目录（相对插件数据目录）。"
            "把教务系统导出的 ics 放进这个目录即可被识别"
        ),
        json_schema_extra={"label": "课表目录", "order": 0},
    )
    scan_interval_seconds: int = Field(
        default=300,
        ge=30,
        le=86400,
        description="多久重新扫描一次课表目录，用于识别手动放入的新文件",
        json_schema_extra={"label": "扫描间隔（秒）", "order": 1, "step": 30},
    )
    url_timeout_seconds: int = Field(
        default=20,
        ge=5,
        le=120,
        description="从网址导入课表时的请求超时",
        json_schema_extra={"label": "导入超时（秒）", "order": 2, "step": 5},
    )
    max_ics_kb: int = Field(
        default=5120,
        ge=64,
        le=51200,
        description="从网址导入时允许的最大文件大小",
        json_schema_extra={"label": "导入大小上限（KB）", "order": 3, "step": 64},
    )
    url_refresh_hours: int = Field(
        default=6,
        ge=0,
        le=168,
        description=(
            "网址导入的课表每隔几小时自动重新下载一次：内容有变动才覆盖文件并通知"
            "提醒会话，没变动不动作；下载失败保留旧课表，下个周期再试。"
            "0 表示关闭自动刷新（课表变动后需要重新 /课表导入）"
        ),
        json_schema_extra={"label": "网址自动刷新（小时）", "order": 4, "step": 1},
    )


class MessageSettingsConfig(PluginConfigBase):
    """提醒消息配置。"""

    __ui_label__ = "提醒消息"
    __ui_icon__ = "message"

    template: str = Field(
        default=DEFAULT_TEMPLATE,
        description=(
            "提醒文案模板。可用占位符：{minutes_label} {minutes} {course} "
            "{location_part} {location} {time_range} {start} {end} "
            "{description_part} {date} {weekday}"
        ),
        json_schema_extra={"label": "提醒文案模板", "order": 0, "rows": 5},
    )


class ClassScheduleConfig(PluginConfigBase):
    """课程表提醒插件完整配置。"""

    __ui_label__ = "课程表提醒"

    plugin: PluginBaseConfig = Field(
        default_factory=PluginBaseConfig, description="插件基础配置"
    )
    reminder: ReminderSettingsConfig = Field(
        default_factory=ReminderSettingsConfig, description="提醒设置"
    )
    target: TargetSettingsConfig = Field(
        default_factory=TargetSettingsConfig, description="提醒对象"
    )
    access: AccessSettingsConfig = Field(
        default_factory=AccessSettingsConfig, description="访问名单"
    )
    holiday: HolidaySettingsConfig = Field(
        default_factory=HolidaySettingsConfig, description="法定节假日"
    )
    reply: ReplySettingsConfig = Field(
        default_factory=ReplySettingsConfig, description="回复风格"
    )
    file_import: FileImportSettingsConfig = Field(
        default_factory=FileImportSettingsConfig, description="聊天文件导入"
    )
    nl_query: NaturalQuerySettingsConfig = Field(
        default_factory=NaturalQuerySettingsConfig, description="自然语言问课"
    )
    source: SourceSettingsConfig = Field(
        default_factory=SourceSettingsConfig, description="课表来源"
    )
    message: MessageSettingsConfig = Field(
        default_factory=MessageSettingsConfig, description="提醒消息"
    )
