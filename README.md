# 课程表提醒(麦麦插件)

> 🤖 **本项目由 AI 生成**(代码、文档与测试),经人工实测与真实麦麦环境验证。

导入教务系统导出的 `.ics` 课表,在上课**前 N 分钟**(默认 20)主动私聊提醒你。
文案不是插件写死的——它把事实交给**你自己的麦麦**,按宿主的人设来说。

## 特性

- **导入方式多**:`/课程解析` 引导发文件、直接发 `.ics`、放进本地目录、公网网址导入。
  支持 `RRULE` 重复、`EXDATE` 调休、`RECURRENCE-ID` 调课。
- **提前量按会话独立**:同一节课,会话 A 提前 30 分钟、会话 B 提前 10 分钟,互不干扰。
- **默认只在私聊工作**:课表是个人日程,群聊里的命令、文件、注入、投递一律不响应。
  想让群聊也能用,把 `access.chat_scope` 改成 `"both"`。
- **自动识别法定节假日**:放假当天不提醒;**调休上班日照常提醒**;拿不到数据就照常
  提醒(宁可假期多提醒一次,也不漏掉上课日)。
- **自然语言问课**:直接问「明天有课吗」,麦麦按课表用自己的语气回答。
- **不丢提醒**:重启不重复提醒;机器人短暂离线错过的,只要课还没开始就补发。
- 改设置不用重启;支持访问白/黑名单。

## 快速开始

1. 把整个目录放进麦麦的插件目录,名字保持 `maibot_plugin_class_schedule`:

   ```
   MaiBot/plugins/maibot_plugin_class_schedule/
   ```

2. 重启麦麦。首次启动会自动安装依赖(`python-dateutil`)、生成 `config.toml`、
   建好课表目录。

3. 在**私聊**里发 `/课程解析`,然后把 `.ics` 文件发给机器人。
   (也可以跳过命令,直接把文件发给它。)

```
/课程解析          → 麦麦:请把 ics 文件发给我
(发 .ics 文件)     → 麦麦:已导入,解析出 N 条课程
/课表订阅          → 这个会话开始收上课提醒
```

`/课表状态` 随时能看运行状况,`/课表` / `/课表明日` / `/课表本周` 查课表。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/课表` / `/课表明日` / `/课表本周` | 查看今天 / 明天 / 今天起七天的课 |
| `/课程解析` | 引导导入:回你一句提示,然后等你发 ics 文件 |
| (把 .ics 文件发给机器人) | 直接导入课表 |
| `/课表导入 <网址>` | 从公网 http/https 网址导入 |
| `/课表重载` | 重新扫描课表目录 |
| `/课表订阅` / `/课表退订` | 当前会话开始 / 停止收提醒 |
| `/课表提前 [分钟]` | 查看或设置**本会话**的提前量,`重置` 回到配置默认 |
| `/课表假日` | 节假日数据状态与今日性质 |
| `/课表状态` | 运行状态、本会话可用标识、提醒会话列表 |

另外注册了 LLM 工具 `query_class_schedule`,所以「今天有什么课」这类问题麦麦也能答。

## 常用配置

配置在 WebUI 的插件配置页里改,或直接编辑插件目录下的 `config.toml`,**改完立即生效**。

```toml
[reminder]
remind_before_minutes = 20    # 默认提前多少分钟
check_interval_seconds = 60   # 检查间隔

[access]
chat_scope = "private"        # 默认只在私聊工作
tool_query_enabled = true     # LLM 能否查课表(名单管不到它)

[holiday]
skip_off_days = true          # 法定节假日不提醒
extra_dates = []              # 校历自定假日,如 ["2026-11-15", "05-20"]
```

完整字段(含提醒文案模板的占位符表)见 **[配置参考](docs/config.md)**。

## 文档

| 文档 | 内容 |
| --- | --- |
| [导入课表](docs/import.md) | 四种导入方式的细节、文件命名与覆盖规则、内网地址怎么办 |
| [行为与隐私](docs/behavior.md) | 提前量、回复风格、自然语言问课、适用范围、访问名单、节假日 |
| [配置参考](docs/config.md) | 全部配置项 |
| [内部实现](docs/internals.md) | 时间口径、数据文件、开发与测试、集成验证、完整限制清单 |

## 需要注意

- **默认只走私聊**。群聊里发命令、发课表文件、问课都不会有反应——这是有意的默认值,
  改 `access.chat_scope` 可以放宽。
- **LLM 工具不受访问名单与适用范围限制**。麦麦调用工具时不传会话信息(已实测),
  所以如果机器人也在你不放心的群里,请把 `access.tool_query_enabled` 关掉。
- **网址导入只接受公网 http/https**(SSRF 防护,内网地址会被拒绝)。
  校园内网的教务系统请用聊天发文件或放进本地目录。
- **网址导入是「拉一次」**,不会自动定期更新:课表变了要重新 `/课表导入 <同一网址>`。
- 其余限制(时区口径、全天事件、调课覆盖范围等)见
  [完整限制清单](docs/internals.md#完整限制清单)。

## 开发

531 项单元测试,只用标准库,不需要额外依赖:

```bash
python -m unittest discover -s tests -v
```

`tools/verify_with_maibot.py` 会用**麦麦自己的代码**做一次集成验证(61 项检查,
在临时目录里搭假项目根,不动你的安装)。详见
[内部实现](docs/internals.md#用真实的麦麦验证)。

## 致谢

注入机制参考了 [mai-life](https://github.com/tsuiraku9/mai-life)、
[autonomous_planning_plugin](https://github.com/xuqian13/autonomous_planning_plugin)、
[maibot-auto-planning-plugin](https://github.com/Natural-Selectionn/maibot-auto-planning-plugin);
`proactive` 的发言验证思路来自
[maibot-reminder-plugin](https://github.com/DCY501/maibot-reminder-plugin)。

## 许可

[MIT](LICENSE)
