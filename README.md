# blivechat「发弹幕」插件

给 [blivechat](https://github.com/xfgryujk/blivechat) 加两个能力：

1. **手动发弹幕** —— 自带一个本地网页控制台，输入内容点发送，弹幕就从你的号发到直播间。
2. **自动直播弹幕回复** —— 观众发「/点歌」这类指令或包含关键词的弹幕时，自动用你的号回复一条。

发弹幕用的是 B 站直播的 `POST https://api.live.bilibili.com/msg/send` 接口，登录态直接复用
blivechat 已经保存的 cookie，不需要重复登录。

> ⚠️ **请务必用你自己的账号，遵守 B 站社区规则。** 发弹幕走的是真人账号，频率过快会被限流甚至
> 风控，插件默认已经做了限速，**不建议把 `min_send_interval` 调到 2 秒以下**。建议用小号。

---

## 目录

- [它是怎么工作的](#它是怎么工作的)
- [安装](#安装)
- [配置](#配置)
- [自动回复规则](#自动回复规则)
- [控制台](#控制台)
- [常见问题](#常见问题)
- [开发](#开发)

---

## 它是怎么工作的

blivechat 的插件是**独立进程**，由 blivechat 启动，通过 WebSocket 和主程序通信。官方对插件的
限制是：

- ✅ 可以监听后端收到的任何消息
- ✅ 可以生成文本消息并显示在前端
- ❌ 不能修改前端

所以这个插件的结构是：

```
blivechat ──WebSocket──► 插件进程 ──HTTPS──► api.live.bilibili.com/msg/send
    ▲                       │
    └── send_text 播报 ◄────┘
                            │
                     本地网页控制台 (127.0.0.1:12452)
```

- **收**：blivechat 把直播间弹幕通过 WebSocket 推给插件
- **判**：插件里的规则引擎判断要不要回、回什么
- **发**：插件用自己的 B 站 cookie 调 `msg/send` 真发出去
- **显示**：发出去的内容再通过 SDK 的 `send_text` 播报回 blivechat 弹幕栏，作者名显示成
  `[自动回复]`，和真实观众弹幕区分开

### 两个容易踩的坑，插件已经处理了

1. **自触发**：插件自己产生的消息会带 `isFromPlugin=true`，插件直接忽略。
2. **回声**：插件在 B 站真发出去的弹幕，会作为**普通观众弹幕**从弹幕流回来。如果不处理，就会
   自己回自己、无限循环。插件会把「自己刚发出去的内容」记下来，30 秒内看到同样内容就跳过。

---

## 安装

### 前置条件

1. **blivechat 1.9 或更高版本**（插件系统是 1.9 引入的）。
2. 在 blivechat 界面里**开启「通过服务器转发消息」选项**——不开的话插件收不到任何弹幕。
3. **B 站登录态**。二选一：
   - 装官方的 **「登录B站账号」插件**，登录一次，插件会自动读取它的 cookie（推荐）；
   - 或者自己从浏览器复制 `SESSDATA` 和 `bili_jct`，填到 `data/config.ini` 里。

### 安装步骤

1. 把整个 `send-danmu` 目录放进 blivechat 的 `data/plugins/` 下：

   ```
   blivechat/
     data/
       plugins/
         send-danmu/          ← 这个目录
           plugin.json
           main.py
           ...
       cookie_jar.pickle      ← 官方登录插件存的 cookie
   ```

2. 重启 blivechat。它会扫描 `data/plugins/`，发现有 `plugin.json` 就把这个目录当成插件并启动。

3. 看 blivechat 的日志，应该能看到插件启动成功，以及控制台地址：

   ```
   控制台已启动：http://127.0.0.1:12452/
   ```

4. 在 blivechat 插件列表里点这个插件的**「管理」**按钮，会自动打开控制台（可以在配置里关掉
   自动打开）。

### 运行环境怎么解决

插件的 `run.bat` 会按这个顺序找一个能用的 Python 环境：

1. **`uv`**（推荐）——`uv run` 会按 `pyproject.toml` 自动建虚拟环境、装依赖，什么都不用手动做。
   安装 uv：<https://docs.astral.sh/uv/>
2. 插件目录下的 **`.venv`** 虚拟环境
3. **系统 `python`** ——需要先手动装依赖：

   ```sh
   python -m pip install -r requirements.txt
   ```

`blcsdk`（blivechat 官方插件 SDK）已经随插件放在 `vendor/` 里，不需要额外安装。

Linux / macOS 用 `run.sh`，把 `plugin.json` 里的 `run` 改成 `run.sh` 即可。

---

## 配置

配置文件在插件目录的 `data/config.ini`。**第一次启动时会自动从 `data/config.example.ini`
复制一份**，里面每一项都有中文注释，直接照着改就行。

改完不用重启 blivechat，在控制台点「保存并重载」或「重载配置」就生效。

下面是每一项的说明。

### `[account]` 登录凭据

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `cookie_file` | 空 | cookie jar 路径。留空 = 自动探测 blivechat 的 `data/cookie_jar.pickle` |
| `room_id` | 0 | 固定发到哪个房间（真实房间ID）。0 = 跟随 blivechat 当前连接的房间 |
| `auth_code` | 空 | **身份码**。填写后自动发到该身份码对应的房间（需先在 blivechat 里用身份码添加该房间）。优先级高于 `room_id` |
| `SESSDATA` / `bili_jct` | 空 | 也可以直接写在这里，优先级高于 `cookie_file` |

> 一般不用改这一节，只要装了官方登录插件就行。

### `[danmaku]` 发送参数

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `min_send_interval` | 2 | 两条弹幕之间的最小间隔（秒）。**不建议低于 2** |
| `max_send_queue` | 50 | 排队上限，超出直接拒绝 |
| `max_length` | 20 | 弹幕最大长度（字符）。普通号 20，UL20 以上可以到 30 |
| `send_timeout` | 10 | 单次请求超时（秒） |
| `color` | 16777215 | 弹幕颜色（十进制 RGB），16777215 = 白色。B 站目前对颜色效果有限 |
| `fontsize` | 25 | 字号 |
| `mode` | 1 | 1=滚动，4=底部，5=顶部 |
| `dry_run` | false | **试运行**：只写日志，不真发。调试规则时很有用 |

### `[auto_reply]` 自动回复总控

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | true | 总开关 |
| `cooldown` | 5 | 全局冷却（秒）。任意一条自动回复之后至少等这么久 |
| `template` | `{content}` | 回复模板，可用变量见下 |
| `max_per_minute` | 0 | 每分钟最多回几条，0 = 不限 |
| `ignore_mirror` | true | 忽略跨房弹幕。建议保持 true |
| `ignore_medal_level_below` | 0 | 忽略勋章等级低于该值的弹幕 |
| `min_author_level` | 0 | 忽略用户等级低于该值的弹幕 |
| `block_users` | 空 | 黑名单，按用户名或 uid，逗号/换行分隔 |
| `allow_users` | 空 | 白名单，非空时只处理名单内用户 |

**模板可用变量**：

| 变量 | 含义 |
| --- | --- |
| `{content}` | 规则里配置的回复内容 |
| `{author}` | 发弹幕的用户名 |
| `{uid}` | 用户 ID |
| `{level}` | 用户等级 |
| `{medal_level}` | 当前房间的勋章等级（没戴是 0） |
| `{medal_name}` | 勋章名 |
| `{guard_name}` | 舰队名（总督/提督/舰长，非舰队为空） |
| `{keyword}` | 命中的关键词 |
| `{room_id}` | 房间 ID |
| `{time}` | 弹幕时间 `HH:MM:SS` |

例子：`template = 谢谢 {author} 的 {keyword}！{content}`

> 如果模板里没写 `{content}`，规则回复的内容会自动接在结果后面，不会丢。

### `[panel]` 控制台

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | true | 关掉就没有手动发弹幕的界面了 |
| `host` | 127.0.0.1 | **只建议用回环地址**，控制台能替你发弹幕，别暴露到公网 |
| `port` | 12452 | 端口，被占用就换一个 |
| `auto_open` | true | 点「管理」时是否自动打开浏览器 |

### `[sdk]` 与 blivechat 的交互

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `broadcast` | true | 自动回复的内容是否也显示在 blivechat 弹幕栏 |
| `broadcast_author` | `[自动回复]` | 播报时显示的作者名 |

---

## 自动回复规则

每条规则是一个 `[rule-xxx]` 段，段名随意。

```ini
[rule-greeting]
name = 打招呼
enabled = true
match_type = contains
keywords = 你好, 您好, 早上好
reply = 你好呀~, 欢迎欢迎！
reply_mode = random
cooldown = 10
per_user_cooldown = 60
```

### 规则字段

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `name` | 段名 | 规则名，只用于日志显示 |
| `enabled` | true | 是否启用 |
| `match_type` | contains | `contains` 包含 / `equals` 完全相等 / `prefix` 前缀 / `suffix` 后缀 / `regex` 正则 |
| `keywords` | — | 关键词。逗号或换行分隔。**正则是每行一个**（见下） |
| `reply` | — | 回复内容。写多条时按 `reply_mode` 选取 |
| `reply_mode` | first | `first` 只用第一条 / `random` 随机 / `sequence` 轮流 |
| `cooldown` | 0 | 本条规则的冷却（秒）。0 = 用全局 `cooldown` |
| `per_user_cooldown` | 0 | 同一用户触发本条规则的冷却（秒）。0 = 不限 |
| `priority` | 0 | 优先级，**越大越先匹配**。命中就停 |
| `command` | true | 是否响应弹幕指令 `/关键词` |
| `chat_trigger` | true | 是否响应普通弹幕内容 |
| `require_guard` | false | 是否只允许舰队用户触发 |
| `min_author_level` | 0 | 触发所需的最低用户等级 |
| `min_medal_level` | 0 | 触发所需的最低本房间勋章等级 |

### 弹幕指令

以 `/`、`!` 或 `！` 开头的弹幕会被当成指令。比如规则关键词是「点歌」：

- 观众发 `/点歌` → 触发
- 观众发 `/点歌 起风了` → 触发（带参数也能触发）
- 观众发 `我想点歌` → 只有 `chat_trigger = true` 时才触发

反过来，如果规则是 `chat_trigger = false`，那就只有指令能触发，普通聊天里提到关键词不会触发。
可以做「只在指令里生效」的功能型规则。

### 正则规则要注意

正则里逗号是有意义的（比如 `^6{3,}$`），所以**正则关键词只能一行一个**：

```ini
[rule-laugh]
match_type = regex
keywords = ^6{3,}$
           ^(23){2,}$
reply = 哈哈
```

写成 `^6{3,}$, ^(23){2,}$` 这种一行会被逗号错误地切开。

### 匹配顺序

1. 先按 `priority` 从大到小排
2. 同优先级按配置文件里的先后顺序
3. 命中的第一条规则生效，后面的不再尝试
4. 但规则自身的条件（`require_guard` / `min_medal_level` 等）不满足时，会**继续往下找**下一条规则

---

## 控制台

默认地址 <http://127.0.0.1:12452/>，只在回环地址监听。

| 区域 | 作用 |
| --- | --- |
| 手动发弹幕 | 选房间、输入内容、点发送（回车也能发） |
| 运行状态 | 登录态、房间、插件版本、发送统计、当前生效的规则 |
| 自动回复规则 | 直接编辑 `config.ini`，保存前会校验 INI 格式，保存后立即重载 |
| 日志 | 最近 500 条插件日志 |

顶部的两个按钮：

- **重载配置** —— 重新读 `config.ini`
- **重读Cookie** —— 重新读 cookie jar（比如你刚用登录插件重新登录过）

---

## 常见问题

### 插件收不到弹幕

99% 是没开 **「通过服务器转发消息」**。blivechat 的插件只有开了这个选项才能收到消息。

### 提示「账号未登录」（错误码 -101）

`SESSDATA` 失效或没读到。处理办法：

1. 确认装了官方的「登录B站账号」插件并且登录成功
2. 在控制台点 **「重读Cookie」**
3. 还不行就重启 blivechat

### 提示 csrf 校验失败（-111）

cookie 里的 `bili_jct` 缺失或不对。通常是登录插件的 cookie 没存全，重新登录一次。

### 提示发送频率过快 / 被风控（-412、10031、-509、-799）

调大 `min_send_interval` 和 `[auto_reply] cooldown`。风控一般是临时的，等几分钟再试。

- 建议 `min_send_interval` ≥ 2 秒
- 规则多的时候把各条规则的 `cooldown` 也调大
- 用 `max_per_minute` 设置一个每分钟硬上限

### 提示弹幕超出限制长度（1003212）

调小 `max_length`，或者把回复写短一点。插件会按**字符数**截断（不是字节），中文不会截半个字。

### 自动回复发不出去，但日志显示「已发送」

去 B 站直播间看一眼。如果确实没出现，看日志里 B 站返回的 `message`——注意 B 站有一种
「`code=0` 但其实没发出去」的情况（`message` 是 `msg in 1s` 或 `msg repeat`），插件已经把
这两种识别成发送失败并重试/报错，所以日志显示「已发送」就是真的发出去了。

### 自动回复把自己绕进去了（机器人自己回自己）

插件有两道防线（忽略 `isFromPlugin` 消息 + 识别自己的回声）。如果还是出现，请提 issue 并附上
`data/config.ini` 和日志。

### 控制台端口被占用

改 `[panel] port`，或者把 `[panel] enabled` 设成 false 只用配置文件。

### 想让弹幕栏干净一点

把 `[sdk] broadcast` 设成 `false`，自动回复就只在日志里看得到，不会出现在弹幕栏。

---

## 开发

### 目录结构

```
send-danmu/
  main.py                   入口：日志、SDK 初始化、信号处理
  app.py                    核心对象，串起 SDK / 发送 / 规则 / 控制台
  listener.py               blivechat 消息处理
  panel.py                  本地控制台（http.server + 内嵌网页）
  config.py                 配置加载与热重载
  rules.py                  自动回复规则引擎（纯决策，可单测）
  sender.py                 发送队列（跨线程桥接）
  rooms.py                  房间表
  watchdog.py               连接看门狗（blivechat 被强杀时兜底退出）
  logbuf.py                 日志缓冲与转发
  bilibili_live/
    credentials.py          读 cookie
    danmaku.py              调 msg/send 发送
    errors.py               错误码翻译
  vendor/blcsdk/            blivechat 官方插件 SDK（随插件分发）
  tests/                    测试
  data/config.example.ini   带注释的配置模板
```

### 跑测试

```sh
uv sync
uv run pytest                     # 全部（含真实网络测试和集成测试）
uv run pytest -m "not network"    # 跳过真实网络测试
uv run pytest -m "not integration" # 跳过集成测试
uv run pytest -m integration -v   # 只跑端到端集成测试
```

测试分三层：

- `tests/test_rules.py` —— 规则引擎的纯逻辑测试（匹配、冷却、去重、防回声、模板）
- `tests/test_credentials.py` —— cookie 读取，覆盖 aiohttp 新旧两种存储格式
- `tests/test_danmaku.py` —— 发送请求的构造与错误处理；标了 `network` 的会真的请求 B 站接口，
  验证端点契约（比如 `GET /msg/send` 返回 405，证明只能 POST）
- `tests/test_integration.py` —— 用 `tests/fake_blivechat.py` 起的假 blivechat 服务，跑真实插件
  代码，验证从收到弹幕到发出回复的完整链路

### 打包成 exe

```sh
uv run pyinstaller -y send-danmu.spec
```

产物在 `dist/send-danmu/`。把整个目录当插件目录放进 `data/plugins/`，并把 `plugin.json` 里的
`run` 改成 `send-danmu.exe`。

---

## 技术细节 / 参考来源

- 插件协议：[blivechat 插件系统 wiki](https://github.com/xfgryujk/blivechat/wiki/%E6%8F%92%E4%BB%B6%E7%B3%BB%E7%BB%9F)
- 插件 SDK：blivechat 仓库的 `blcsdk/` 目录（本插件的 `vendor/blcsdk/` 就是它）
- 发送弹幕接口：`POST https://api.live.bilibili.com/msg/send`
  - 必需参数：`csrf`（= cookie 里的 `bili_jct`）、`roomid`、`msg`、`rnd`（秒级时间戳）、`fontsize`、`color`
  - 参考实现：[BanqiJane/Bilibili_Danmuji](https://github.com/BanqiJane/Bilibili_Danmuji) 的 `HttpUserData.httpPostSendBarrage`
  - 参数表与错误码：[bilibili-API-collect 镜像](https://github.com/alittlehuaji/bilibili-api-collect-mirror/blob/master/docs/live/danmaku.md)
    （原仓库 `SocialSisterYi/bilibili-API-collect` 已于 2026-01 停止维护并删除文档）
- 弹幕接收（blivechat 内部用的是 [blivedm](https://github.com/xfgryujk/blivedm)）
- `room_id` 是**真实房间ID**；短号会被 `room/v1/Room/room_init` 解析成真实ID后使用

## 许可

MIT
