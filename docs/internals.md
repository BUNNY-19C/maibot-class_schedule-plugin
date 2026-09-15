# 内部实现

给想改代码或排查问题的人看:时间口径、数据文件布局、为什么这么写、怎么验证。

## 目录

- [时间口径](#时间口径)
- [数据文件](#数据文件)
- [为什么用 Hook 而不是 EventHandler](#为什么用-hook-而不是-eventhandler)
- [开发与测试](#开发与测试)
- [用真实的麦麦验证](#用真实的麦麦验证)
- [完整限制清单](#完整限制清单)
- [致谢](#致谢)

---

## 时间口径

为避免在 Windows 上依赖 `tzdata` 数据包,本插件对 ICS 时间做如下处理:

| ICS 里的写法 | 处理方式 |
| --- | --- |
| `DTSTART;VALUE=DATE:20260901` | 全天事件,**不参与上课提醒** |
| `DTSTART;TZID=Asia/Shanghai:20260901T080000` | 按本地墙上时间取用(即 08:00 就是 08:00) |
| `DTSTART:20260901T000000Z` | 视为 UTC,换算成系统本地时间 |

国内教务导出的课表时间基本都是「本校本地时间」,直接取墙上时间不会错。
如果课表用的是别的时区、且你需要跨时区提醒,请把 ICS 先转成本地时间再导入。

**重复规则**由 `python-dateutil` 展开,因此需要 `python-dateutil>=2.8.0`
(已在 `_manifest.json` 里声明,Runner 会自动安装)。

`EXDATE` 有两种写法,都支持:精确到时刻的按时刻匹配;只精确到天的
(`EXDATE;VALUE=DATE:20260915` 或裸 8 位 `20260915`)按"日"匹配——教务系统
常用后者标注停课,当时刻型课程的开始时间是 08:00 时,只按时刻比较永远匹配不上。

## 数据文件

```
data/plugins/local.class-schedule/
├── state.json      # 提醒会话(含各自提前量)、已提醒记录
├── holidays/       # 法定节假日数据缓存(按年)
│   ├── 2026.json
│   └── 2027.json
└── ics/            # 所有课表文件
    └── *.ics
```

课表永远以 ics 文件为唯一数据源,删掉 `ics/` 里的文件就等于删掉对应课程。
`holidays/` 可以整个删掉(会自动重新下载)。`state.json` 可直接删除(会重置
订阅、各会话提前量与去重记录,不影响课表)。

写入都是**原子**的(先写临时文件再 `os.replace`),中途断电不会留下半份文件。
状态文件读不出来时会告警并按空状态启动,不会静默归零。

`state.json` 里每个提醒会话长这样:

```json
{
  "stream_id": "会话 ID",
  "chat_type": "group",          // 或 private
  "group_id": "群号",
  "user_id": "用户号",
  "label": "群名/昵称",
  "lead_minutes": 30,            // null 表示跟随配置默认值
  "added_at": "2026-09-01T10:00:00"
}
```

> 从早期版本升级上来的旧状态文件(裸会话 ID 列表 + 一个全局提前量)会自动
> 迁移:旧会话全部保留,原全局提前量平摊给它们,之后就能各自调整。

## 为什么用 Hook 而不是 EventHandler

麦麦 1.2.0 里**消息类核心事件没有真正派发**——`event_bus.emit(EventType.ON_MESSAGE)`
以及 `POST_SEND` / `AFTER_SEND` 那几行在源码里都是注释状态,实际只有
`ON_START` / `ON_STOP` 会发出。所以「聊天里发文件」这个功能改用**命名 Hook**
`chat.receive.after_process`(`bot.py` 里是活代码,每条入站消息都会经过),
并选 `observe` 模式 + 后台任务,避免下载文件把入站链路阻塞到适配器超时。

如果哪天麦麦把事件派发接回来了,这段可以改回 `@EventHandler`;
`tools/verify_with_maibot.py` 里有一条检查会盯着 Hook 是否仍注册在预期名字上。

## 开发与测试

531 项单元测试,只用标准库 `unittest`,不需要额外安装依赖:

```bash
cd maibot_plugin_class_schedule
python -m unittest discover -s tests -v
```

(装了 pytest 也可以直接 `pytest tests/`,`tests/conftest.py` 已做兼容。)

测试覆盖:ICS 解析与重复规则展开(含 VALARM 子组件、日期型 EXDATE、病态规则
上限)、到期判定与去重、SSRF 防护、会话身份提取与名单判定、适用范围(私聊/群聊)、
法定节假日判定(含 fail-open)、状态持久化与旧版本迁移、聊天文件导入的段提取,
以及「写 ics → tick → 发送」的端到端链路。

> 测试**不联网**:`PluginSmokeTest.setUp` 会把抓取入口换成"立刻失败",
> 需要"下载成功"的用例显式替换(`fake_fetch` / `fake_holiday_text`)。
> 若日后新增联网代码,请沿用这个模式,否则测试会变慢且不稳定。

## 用真实的麦麦验证

单元测试用的是替身;下面这个脚本调用**麦麦自己的代码**做集成验证:

```bash
python tools/verify_with_maibot.py                 # 自动查找麦麦安装
python tools/verify_with_maibot.py --maibot "<麦麦的 modules/MaiBot 目录>"
```

它会在临时目录里搭一个假项目根(不动你的麦麦安装),然后:

| 阶段 | 验证内容 |
| --- | --- |
| 1 | 麦麦真实的 `PluginLoader`:清单校验、依赖解析、包导入、组件收集 |
| 2 | 麦麦的配置归一化:生成并读回 `config.toml`,再注回插件 |
| 3 | 真实 `PluginContext`:`on_load` → `/课表订阅` → `_tick` 真的走能力协议发消息 → 热更新 → `on_unload` |
| 4 | 主机字段契约:命令载荷能取出群号/用户号;Tool 载荷确实没有会话信息 |
| 5 | 节假日语义:放假跳过、调休上班不跳过、缺数据 fail-open |

**已验证环境**:麦麦 1.2.0 + **SnowLuma 适配器**,61 项检查全通过。

需要麦麦的依赖在线(`structlog`、`tomlkit`、`pydantic`、`packaging`、
`maibot-plugin-sdk`)。

跑这个脚本可以提前发现"单元测试测不出"的集成问题——本插件就有几处是靠它
才暴露的:清单缺 `author.url` 被真实校验拒绝;**工具调用拿不到会话信息**,
导致原先的"工具也过名单"设计根本不成立;以及 `message.get_recent` 的返回结构
(宿主返回 `{"success","messages"}`,SDK 客户端会拆包成列表)。

## 完整限制清单

- **网址导入是「拉一次」,不会自动定期更新**:课表变动后需要重新执行
  `/课表导入 <同一网址>`(会覆盖上一次的结果)。本地文件方式则改文件即生效。
- **导入文件与手动文件的命名空间**:见 [导入课表](import.md#文件命名与覆盖规则)。
- **仅识别 `.ics` / `.ical` 扩展名**(适配器给了 `mime_type = text/calendar`
  时例外):其他后缀请改名后再放入。
- **不接受校园内网地址**:见 [导入课表](import.md#方式-3网址导入)的安全说明。
- **调课覆盖**(`RECURRENCE-ID`)支持「改时间/改地点/改课程名」,
  但不支持把一次课拆成多次的复杂场景。
- **无法解析的重复规则不会静默失效**:这门课会按"只上一次"处理,并在日志里
  告警一次(否则一门每周重复的课会无声消失)。规则细到按秒/毫秒、或一个窗口内
  能展开出上千次的,同样会被拒绝或截断——那类规则基本只可能是坏数据或恶意文件,
  而展开跑在提醒循环与规划器注入的**同步路径**上(实测 `FREQ=SECONDLY` 在
  7 天窗口能展开出 60 万次、耗时 1.8 秒)。
- **全天事件**(如校历、节假日)不会触发提醒。
- **课程列表消息最多 30 行**(超出显示「仅显示前 30 行」),
  课程名与地点超长会被截断。
- **法定节假日数据依赖网络**:拿不到数据时不跳过提醒(照常提醒),并在日志与
  `/课表假日` 里标出缺哪些年份。数据由第三方项目(holiday-cn,取自国务院公告)
  提供,也可以换成你自己的数据源。
- **寒暑假、校历自定假日不在法定节假日数据里**:数据源只含国家法定节假日,
  寒暑假这类校历安排需要自己用 `holiday.extra_dates` 补。
- **调休上班日按上课日处理**:那天会照常提醒。若你们学校补班日不上课,把那天
  用 `extra_dates` 加进假日列表即可;反过来法定假日要补课的,用 `exclude_dates`。
- **节假日数据是后台下载的,不会阻塞提醒**:即使数据源很慢或超时,提醒判定
  照常进行(拿不到数据就不跳过)。下载失败会在日志里告警一次。
- **群号/用户号不一定拿得到**:接口能提供时才记录,取不到时用会话 ID 兜底
  (`/课表状态` 里能看到),名单匹配三种标识都支持。群名/昵称同理,只用于展示。
- **聊天文件走适配器给的地址下载**:若该地址在本机/内网,默认被安全策略拒绝,
  需要你在 `file_import.allowed_hosts` 里点名放行(日志会告诉你被拒的地址)。
- **`proactive` 模式的送达依赖麦麦的回复流程**:实测它可能只规划不发声且无报错,
  插件靠 `message.get_recent` 验证并在超时后兜底直发。默认的 `persona` 没有这个问题。
- **LLM 工具不受访问名单与适用范围限制**:麦麦调用工具时不传任何会话信息,
  这两层在这条路上都无法判定(已实测)。要防止课表被问出来,把
  `access.tool_query_enabled` 关掉。
- **提醒会话默认上限 20 个**:可调 `target.max_subscriptions`。
- **名单匹配大小写敏感**:会话 ID 若含字母,请从 `/课表状态` 原样复制。
- **时区按墙上时间处理**:宿主时区与课表时区不一致时会整体平移,
  见[时间口径](#时间口径)。容器化/UTC 部署前请自行确认。

## 致谢

自然语言问课的**注入机制**(`maisaka.planner.before_request` + 注入
`SystemMessageItem`)与热路径只做字符串判定、不调 LLM 的取舍,参考了:

- [tsuiraku9/mai-life](https://github.com/tsuiraku9/mai-life)(MIT)——注入时机、
  item 结构、插在 system 段末尾的做法;
- [xuqian13/autonomous_planning_plugin](https://github.com/xuqian13/autonomous_planning_plugin)(AGPL-3.0)——关键词加权判定意图、按需注入省 token 的思路;
- [Natural-Selectionn/maibot-auto-planning-plugin](https://github.com/Natural-Selectionn/maibot-auto-planning-plugin)(MIT)——同类注入与配置分层的写法。

注入用的 item 结构由麦麦自己的 Context Item 约定决定
(`src/llm_models/payload_content/context_item.py` 与 `request_snapshot.py`
的反序列化),任何插件写出来都是这个形状;参考的是**注入时机**(哪个 hook、
什么条件下注入)和"插到最后一条 system item 之后"这个位置选择。

`proactive` 的发言验证与"读宿主人设生成文案"这两个思路借鉴自
[DCY501/maibot-reminder-plugin](https://github.com/DCY501/maibot-reminder-plugin)
(MIT)——它用 `message.get_recent` 判断 bot 是否真的发过言,
解决了"交给 replyer 后可能静默失败"这个坑。
