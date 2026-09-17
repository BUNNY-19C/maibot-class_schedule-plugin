# 配置参考

配置文件由麦麦 Runner 首次启动时生成,位于插件目录下的 `config.toml`,
也可以直接在 WebUI 的插件配置页里改(**改完立即生效,不用重启**)。

> `config.toml` 不进版本库(见 `.gitignore`)——它通常含你的用户号/群号等个人信息。

## 目录

- [`[plugin]` 插件基础](#plugin-插件基础)
- [`[reminder]` 提醒设置](#reminder-提醒设置)
- [`[target]` 提醒对象](#target-提醒对象)
- [`[message]` 提醒文案](#message-提醒文案)
- [`[reply]` 回复风格](#reply-回复风格)
- [`[nl_query]` 自然语言问课](#nl_query-自然语言问课)
- [`[access]` 适用范围与访问名单](#access-适用范围与访问名单)
- [`[holiday]` 法定节假日](#holiday-法定节假日)
- [`[source]` 课表来源](#source-课表来源)
- [`[file_import]` 聊天文件导入](#file_import-聊天文件导入)

---

## `[plugin]` 插件基础

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `config_version` | `1.0.0` | 由 Runner 维护,不要删 |
| `enabled` | `true` | 关闭后不再提醒,命令仍可用 |

## `[reminder]` 提醒设置

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `enable_reminder` | `true` | 是否开启上课提醒 |
| `remind_before_minutes` | `20` | **默认提前多少分钟提醒**,0–1440。各会话可用 `/课表提前` 单独覆盖 |
| `check_interval_seconds` | `60` | 检查间隔,越小越准时(最小 15) |
| `late_grace_seconds` | `60` | 课已开始多久后放弃补发 |

> **检查间隔与提前量的关系**:提醒窗口是「课前 N 分钟」到「课后补发宽限」。
> 如果检查间隔比这个窗口还长,某一轮检查会整段跨过去、这节课就漏了。插件会
> 自动把窗口放宽到至少覆盖一个检查周期来**保证不漏**,代价是检查间隔设得很大时
> 提醒可能迟到(启动日志里会告警)。默认的 60 秒 + 20 分钟不会触发这种情况。

## `[target]` 提醒对象

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `target_streams` | `[]` | 固定推送的目标:会话 ID 或**纯数字用户号**(如 QQ 号)。一般用 `/课表订阅` 代替 |
| `max_subscriptions` | `20` | 最多允许登记多少个提醒会话 |

纯数字被当作**用户号**,自动解析成对方的私聊会话(也可写 `qq:123456789`);
32 位十六进制则按会话 ID 原样使用。

> 为什么不能直接把 QQ 号当会话 ID 用:麦麦里 `stream_id` 和用户号是两回事。
> 实测:同一个人,私聊会话 ID 是一串 32 位十六进制,QQ 号则是一串纯数字——
> 把 QQ 号当 `stream_id` 填进去会**静默发不出去**(日志里没有任何报错)。
> 所以两种写法插件都认。
>
> 解析需要对方**先给机器人发过消息**(否则机器人不知道那个会话),
> 解析不到时日志会明确告诉你,`/课表状态` 里也能看到。

## `[message]` 提醒文案

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `template` | 见下 | 提醒文案模板 |

默认模板:

```
⏰ {minutes_label}:{course}
🕐 {time_range}{location_part}
```

渲染结果:

```
⏰ 20 分钟后上课:高等数学
🕐 08:20-10:00
📍 教三-201
```

可用占位符:

| 占位符 | 含义 | 示例 |
| --- | --- | --- |
| `{minutes_label}` | 自然的提前量描述 | `20 分钟后上课` / `马上上课` |
| `{minutes}` | 提前量数字 | `20` |
| `{course}` | 课程名 | `高等数学` |
| `{time_range}` | 时间段 | `08:20-10:00` |
| `{start}` / `{end}` | 开始/结束时刻 | `08:20` |
| `{location}` | 地点 | `教三-201` |
| `{location_part}` | 带图标的地点行(无地点时为空) | `\n📍 教三-201` |
| `{description}` | 备注(常见为教师名) | `教师:张三` |
| `{description_part}` | 带图标的备注行 | `\n📝 教师:张三` |
| `{date}` / `{weekday}` | 日期 / 星期 | `2026-09-01` / `星期二` |

写错的占位符会被原样保留,不会导致整条消息发不出去。

> 提醒以**纯文本**发送。麦麦主机与内置的 NapCat / SnowLuma 适配器都不支持
> markdown 之类的自定义消息段(整个代码库里没有 `qq_markdown` 的实现),
> 所以不做"看起来能开、其实每次都在降级"的开关。

## `[reply]` 回复风格

见 [行为与隐私](behavior.md#回复风格)。

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `style` | `persona` | 上课提醒的发送方式(persona / fixed / proactive) |
| `ack_style` | `persona` | 提示语与导入回执的发送方式 |
| `fallback_after_seconds` | `90` | 仅 proactive:多久没确认发言就兜底 |
| `persona_hint` | `""` | 给模型/replyer 的额外要求,如"一句话说完,别加表情" |
| `fallback_to_fixed` | `true` | replyer 不接手时退回直发 |
| `persona_timeout_seconds` | `12` | persona 生成文案的超时,超时改用模板(0 = 不限时) |

## `[nl_query]` 自然语言问课

见 [行为与隐私](behavior.md#自然语言问课)。

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否把课表注入聊天上下文 |
| `mode` | `on_topic` | `on_topic` 只在聊到课表时注入;`always` 每条都注入 |
| `days` | `3` | 注入未来几天 |
| `max_lines` | `12` | 最多几条,控制 token |
| `extra_keywords` | `[]` | 额外触发词(课程名的特殊叫法,如 `["大物", "工训"]`) |

## `[access]` 适用范围与访问名单

见 [行为与隐私](behavior.md#适用范围默认只走私聊) 与
[访问白名单/黑名单](behavior.md#访问白名单与黑名单)。

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `chat_scope` | `private` | `private` 只在私聊 / `group` 只在群聊 / `both` 都行 |
| `mode` | `off` | `off` / `whitelist` / `blacklist` |
| `entries` | `[]` | 名单内容:群号、用户号或会话 ID |
| `apply_to_commands` | `true` | 名单是否限制命令 |
| `apply_to_reminders` | `true` | 名单是否限制提醒投递 |
| `tool_query_enabled` | `true` | 是否允许 LLM 查课表;**名单管不到工具**,只能靠这个开关 |

## `[holiday]` 法定节假日

见 [法定节假日](behavior.md#法定节假日自动识别)。

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `skip_off_days` | `true` | 法定节假日不提醒(调休上班日**不算**假日) |
| `source_url_template` | holiday-cn 地址 | 数据来源,`{year}` 替换成四位年份 |
| `extra_dates` | `[]` | 校历自定假日:`YYYY-MM-DD` 或 `MM-DD`,写法见下 |
| `exclude_dates` | `[]` | 照常上课日(压过假日判定),写法同上 |
| `refresh_hours` | `12` | 数据多久算过期并重下一次;`0` = 只在启动时检查 |
| `timeout_seconds` | `15` | 下载超时 |

`extra_dates` 与 `skip_off_days` **互相独立**:前者是你逐条写下的名单,始终生效;
后者只管"要不要因为法定节假日跳过"。所以关掉 `skip_off_days` 也不会让寒暑假失效。

`exclude_dates` 优先于任何假日判定(包括 `extra_dates`)。

## `[source]` 课表来源

见 [导入课表](import.md)。

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `ics_dir` | `ics` | 存放 ics 的子目录(相对插件数据目录) |
| `scan_interval_seconds` | `300` | 重新扫描目录的间隔 |
| `url_timeout_seconds` | `20` | 网址导入/自动刷新的单次下载超时 |
| `max_ics_kb` | `5120` | 网址导入/自动刷新的大小上限 |
| `url_refresh_hours` | `6` | 网址课表自动重新下载的间隔(小时):内容有变动才覆盖文件并通知提醒会话;下载失败保留旧课表,下个周期再试;`0` 关闭自动刷新 |

## `[file_import]` 聊天文件导入

见 [导入课表](import.md#方式-1命令引导导入推荐)。

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 允许在聊天里发 .ics 导入 |
| `allowed_hosts` | `[]` | 显式放行哪些主机可下载平台给出的文件地址(默认只允许公网) |
| `max_kb` | `5120` | 文件大小上限 |
| `timeout_seconds` | `20` | 下载超时 |
| `arm_minutes` | `10` | `/课程解析` 之后等待文件的时长 |
