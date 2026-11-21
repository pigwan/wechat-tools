# 微信工具｜消息 API 与发送消息文档

> 整理时间：2026-11-17
> 适用代码：当前工作区 `wechat-report-bridge/`、`local_wechat/`、`vendor/helper-main/plugins/`
> 令牌说明：本文所有令牌一律用 `<BRIDGE_TOKEN>`、`<ADMIN_TOKEN>` 占位。真实值在
> `wechat-report-bridge/config.local.json`、`frp/settings.local.json`，**不要写进公开文档或提交到仓库**。

---

## 1. 总览

### 1.1 两套对外 HTTP 服务

| 服务 | 代码 | 本机监听 | frp 映射 | 用途 | 鉴权 |
|---|---|---|---|---|---|
| 消息接收端 | `server/app.py` | `127.0.0.1:8080` | 服务器 `127.0.0.1:5980` | 消息/链接/公众号上报、查询、日报 | `Authorization: Bearer <BRIDGE_TOKEN>` |
| Web 管理台 | `admin/app.py` | `127.0.0.1:8088` | 服务器 `127.0.0.1:5988` | 页面 + `/api/*`（含发消息） | `Bearer <ADMIN_TOKEN>` 或账号密码登录后的会话令牌 |

公网访问由服务器 Nginx 按两个域名反代（见 `frp/nginx.conf.example`），例如
`https://wx-api.example.com/...` → 5980、`https://wx-admin.example.com/...` → 5988。
**必须两个域名**：两个服务都有 `/api/messages`、`/api/report`，同域名按 location 分流会串线。

### 1.2 不走端口的本地通道

| 通道 | 队列文件 | 写入方 | 执行方 |
|---|---|---|---|
| 文本消息发送 | `vendor/helper-main/.runtime/send_outbox.sqlite3` 的 `outbox` 表 | `send.py`、管理台 `/api/send`、提醒插件 | helper 中的 `report_bridge` 插件 |
| 本地动作中心 | `wechat-report-bridge/.runtime/local_api_tasks.sqlite3` 的 `local_tasks` 表 | `local_wechat.bat call` | helper 中的 `local_api` 插件 |

两条通道都是「写本地 SQLite → 插件领取 → 调 `ctx._service` 驱动本机微信 → 结果写回」。
不开端口、不联网、无需 Token。机器人没运行时任务留在队列里，启动后自动补做。

### 1.3 数据流

```
微信 PC 客户端
   │ helper hook（report_bridge 插件）
   ├─► 消息库 server/messages.sqlite3 ──► 接收端 8080 ──► frp 5980 ──► 外部系统
   │                                  └─► 管理台 8088 ──► frp 5988 ──► 浏览器/外部系统
   └─► 发送队列 send_outbox.sqlite3 ◄── POST /api/send 或 python send.py
              │ report_bridge 插件领取 → ctx.send_text → 微信发出
```

---

## 2. 鉴权与通用约定

### 2.1 接收端（8080 / 公网 5980）

- 除 `GET /health` 外，全部接口需要 `Authorization: Bearer <BRIDGE_TOKEN>`。
- `BRIDGE_TOKEN == "CHANGE_ME"` 时服务拒绝启动；返回 401 表示令牌错误。
- 请求体上限 2 MB，JSON 用 UTF-8。

### 2.2 管理台（8088 / 公网 5988）

- 程序化调用：`Authorization: Bearer <ADMIN_TOKEN>`（长期令牌，取自 `config.local.json` 的 `token`）。
- 网页登录：`POST /api/login` 用账号密码换 8 小时会话令牌；后续同样以 `Bearer <会话令牌>` 传。
- 公网加固模式（`ADMIN_PUBLIC=1`，当前配置已启用）：**禁止 URL `?token=` 传令牌**。
- 只读模式（`ADMIN_READONLY=1`）下 `/api/send`、`/api/report/send` 等写接口返回 403。
- 请求体上限 1 MB。未知路径 404，未授权 401，参数错误 400，服务端异常 500。

### 2.3 Nginx 注意

必须显式透传 `proxy_set_header Authorization $http_authorization;`，**绝不能开 `auth_basic`**——
Basic Auth 会覆盖 `Authorization` 头，导致所有 Bearer 请求 401。

---

## 3. 接收端消息 API（对外上报/查询，`server/app.py`）

基址：本机 `http://127.0.0.1:8080`，公网 `https://wx-api.example.com`（示例域名）。

### 3.1 `POST /api/messages` — 批量上报消息（幂等）

请求体：

```json
{
  "messages": [
    {
      "msg_key": "20261117-120000-xxx@chatroom-wxid_a-1",
      "chat_id": "12345678901@chatroom",
      "sender_id": "wxid_a",
      "sender_name": "张三",
      "is_group": 1,
      "text": "今天的日报好了",
      "received_at": "2026-11-17 12:00:00"
    }
  ],
  "links": [
    {
      "link_key": "唯一键",
      "chat_id": "12345678901@chatroom",
      "sender_id": "wxid_a", "sender_name": "张三", "is_group": 1,
      "kind": "card", "app_type": "", "title": "标题", "url": "https://...",
      "description": "", "source": "", "received_at": "2026-11-17 12:00:00"
    }
  ],
  "official": [
    {
      "official_key": "唯一键",
      "chat_id": "gh_xxx", "publisher_id": "gh_xxx", "publisher_name": "公众号名",
      "sender_id": "", "sender_name": "", "is_group": 0,
      "scene": "push", "title": "文章标题", "url": "https://...",
      "digest": "", "cover_url": "", "published_at": "",
      "received_at": "2026-11-17 12:00:00"
    }
  ]
}
```

- `msg_key` / `link_key` / `official_key` 必填且作为主键；重复投递被忽略（幂等）。
- `messages` 必填（数组）；`links`、`official` 可省略；三类各最多 500 条。
- `official.scene` 取值 `push`（默认）/ `shared` / `text`。

响应：

```json
{"accepted":1,"duplicates":0,"links_accepted":1,"links_duplicates":0,
 "official_accepted":1,"official_duplicates":0}
```

### 3.2 `GET /api/messages` — 查询原始消息

| 参数 | 默认 | 说明 |
|---|---|---|
| `day` | 今天 | `YYYY-MM-DD`，按 `received_at` 前 10 位过滤 |
| `chat_id` | 空 | 精确会话 ID |
| `limit` | 500 | 1–5000 |

返回 `{"day":"2026-11-17","count":N,"messages":[{msg_key,chat_id,sender_id,sender_name,is_group,text,received_at,ingested_at},...]}`。

### 3.3 `GET /api/links` — 查询链接

参数：`day`（支持 `all`）、`chat_id`、`q`（标题/URL/描述/来源模糊搜索）、`limit`（默认 200，1–1000）。
返回 `{"day":...,"count":N,"links":[...]}`。

### 3.4 `GET /api/official` — 查询公众号消息

参数：`day`（支持 `all`）、`chat_id`、`q`、`scene`（`push`/`shared`/`text`）、`limit`（默认 200，1–1000）。
返回 `{"day":...,"count":N,"official":[...]}`。

### 3.5 `GET /api/report` — 结构化日报 / Markdown

参数：`day`（默认今天）、`chat_id`、`top`（1–50，默认 5）、`format=md`（返回 `text/markdown`）。

JSON 字段：`day`、`chat_id`、`total`、`participants`、`groups`、`top_senders[{name,count}]`、
`top_groups[{chat_id,count}]`、`hourly[24]`、`keyword_hits`、
`link_count`/`links`/`link_domains`、`official_count`/`official_text_count`/`official`/`official_publishers`。

### 3.6 `GET /health` — 健康检查（免鉴权）

返回 `{"status":"ok"}`。

### 3.7 调用示例

```bash
# 上报
curl -X POST https://wx-api.example.com/api/messages \
  -H "Authorization: Bearer <BRIDGE_TOKEN>" -H "Content-Type: application/json" \
  -d '{"messages":[{"msg_key":"k-1","chat_id":"123@chatroom","sender_id":"wxid_a","text":"测试","received_at":"2026-11-17 12:00:00"}]}'

# 查日报 Markdown
curl -H "Authorization: Bearer <BRIDGE_TOKEN>" \
  'https://wx-api.example.com/api/report?day=2026-11-17&format=md'
```

---

## 4. 管理台消息 API（`admin/app.py`）

基址：本机 `http://127.0.0.1:8088`，公网 `https://wx-admin.example.com`。全部需要管理台鉴权。

### 4.1 只读查询接口

| 接口 | 主要参数 | 返回要点 |
|---|---|---|
| `GET /api/state` | — | 消息库/接收端/worker/微信在线等状态灯 |
| `GET /api/messages` | `day`、`chat_id`、`q`、`limit`(1–200,默认50)、`offset` | `{day,count,total,messages[]}`，带真实群名和头像 |
| `GET /api/feed` | `day`、`chat_id`、`q`、`kind`、`source`、`limit`(1–200)、`offset` | 消息/链接/公众号混合消息流 |
| `GET /api/conversations` | `day`(默认 all)、`q`、`source`、`limit`(1–500) | 会话列表 |
| `GET /api/thread` | `chat_id`(必填)、`limit`(1–1000,默认120)、`before`、`before_key` | 单会话时间线（收到 + 己方已发合并，统一字段 + 游标翻页；详见 4.5） |
| `GET /api/links` | `day`、`chat_id`、`q`、`kind`、`limit`、`offset` | 链接列表 |
| `GET /api/official` | `day`、`chat_id`、`q`、`scene`、`limit`、`offset` | 公众号消息 |
| `GET /api/images` | `day`(默认 all)、`chat_id`、`q`、`limit`(1–100)、`offset` | 图片墙 |
| `GET /media/<文件名>?w=160\|320\|640` | — | 聊天图片（需鉴权，不裸露公网静态路径） |
| `GET /api/report` / `/api/report.md` | `day`、`chat_id` | 日报 JSON / Markdown |
| `GET /api/outbox` | — | 最近 20 条发送记录 |
| `GET /api/send?id=<队列ID>` | `id` | `{status,error}`，用于轮询发送结果 |
| `GET /api/contacts` | `q`、`kind`、`system=1`、`limit` | 联系人/群/公众号候选 |
| `GET /api/chats` | — | 全部会话（含真实群名） |
| `GET /api/reminders` | `status`、`kind`、`delivery`、`q`、`limit`、`offset` | 提醒任务列表 |

### 4.2 `POST /api/send` — 发送文本消息（对外推荐入口）

请求体：

```json
{"chat_id": "12345678901@chatroom", "text": "今天的日报已生成", "at_list": "wxid_a,wxid_b"}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `chat_id` | 是 | 群 `xxx@chatroom` / 好友 `wxid_xxx` / 公众号 `gh_xxx` / 自定义微信号（如 `pigwan`）/ 唯一昵称 / `filehelper` |
| `text` | 是 | 正文，去除首尾空白后不能为空；≤30000 字符；不能含 `\0` |
| `at_list` | 否 | 群内要 @ 的成员 wxid，逗号分隔 |

响应：

```json
{"id":123,"chat_id":"12345678901@chatroom","chat_name":"产品群",
 "resolver":{"from":"产品群","matched":"name","known":true}}
```

若发送 worker 未加载，额外带 `warning`（消息仍已入队，插件加载后自动补发）。
解析失败返回 400（`{"error":"..."}`）；只读模式返回 403。

### 4.3 `POST /api/report/send` — 把某日日报发到指定会话

请求体 `{"chat_id":"...","day":"2026-11-17"}`（ day 省略为今天）。服务端生成 Markdown 后入队，响应同 `/api/send`。

### 4.4 登录 / 其它写接口

- `POST /api/login`：`{"username":"...","password":"..."}` → `{"token":"<会话令牌>","expires_in":28800,"mode":"session"}`；连续失败 5 次锁 30 秒。
- `POST /api/logout`：注销当前会话令牌。
- 其它：`/api/reminders/action`、`/api/article-push/*`、`/api/social/sync`、`/api/config`、`/api/chat-meta` 等，详见源码 `admin/app.py`。

### 4.5 `GET /api/thread` — 群聊长期记忆用法（字段对齐 + 游标翻页）

> 本节描述的是本次补充后的行为：**需重启 8088 管理台进程**才生效；前端静态文件刷新即可。

**参数**

| 参数 | 说明 |
|---|---|
| `chat_id` | 必填，会话 ID / 微信号 / 唯一昵称 |
| `limit` | 1–1000，默认 120（原上限 200 已放宽） |
| `before` | 游标，`YYYY-MM-DD HH:MM:SS`，返回严格早于该时间的消息 |
| `before_key` | 可选，配合 `before` 处理同一秒内的并列项；直接用上一页返回的 `next_before_key` |

**每个 item 的统一字段（收到 / 己方已发完全对齐）**

| 字段 | 说明 |
|---|---|
| `msg_key` | 去重键。收到的是消息表原始 `msg_key`（链接/公众号为各自 key、图片为文件名）；己方为 `outbox:<队列ID>`，稳定不复用 |
| `text` | 正文（与 `body` 同值） |
| `received_at` | `YYYY-MM-DD HH:MM:SS`（与 `ts` 同值），可直接用于排序 |
| `sender_id` | 收到的为原发送者 wxid/Alias；己方为机器人自己的 wxid（取自守护进程的登录账号，如 `wxid_你的助手号`；取不到则为空串） |
| `self` / `is_self` | 布尔值，`true` = 己方发出 |
| `direction` | `"in"`（收到）/ `"out"`（己方发出）——**推荐用它判方向，不用再拿 outbox 做排除集** |
| `key` | 时间线内部键：`feed:kind:item_key` 或 `out:<id>`，仅用于游标，不要当去重键 |

其它保留字段：`kind`、`kind_label`、`sender`、`time`、`avatar_url`、`title`、`url`、`description`、
`cover_url`、`image_filename`、`image_width/height`、`status`（仅己方消息有，0待发/1发送中/2成功/3失败）、
`error`、`rid`。

**翻页示例**

```bash
# 第一页（最新 500 条，按时间升序返回）
curl -H "Authorization: Bearer <ADMIN_TOKEN>" \
  'https://wx-admin.example.com/api/thread?chat_id=123%40chatroom&limit=500'

# 响应含：has_more、next_before、next_before_key
# 继续往更早翻：把两个值原样带回
curl -H "Authorization: Bearer <ADMIN_TOKEN>" \
  'https://wx-admin.example.com/api/thread?chat_id=123%40chatroom&limit=500&before=2026-11-13%2013%3A51%3A40&before_key=feed%3Aimage%3Aimage-230.jpg'
```

- `items` 始终按时间**升序**；下一页取更早的时间段，循环到 `has_more=false`。
- `total` = 当前游标之后的剩余条数（翻页时会逐页递减），不是全量会话总数。
- 排序与游标都是 `(秒级时间, key)` 严格比较，同一秒多条不会重复或漏掉；已发消息同一秒按队列 ID 数字序。

---

## 5. 发送消息文档

### 5.1 方式 A：HTTP API（外部系统集成首选）

```bash
# 1) 入队
curl -X POST https://wx-admin.example.com/api/send \
  -H "Authorization: Bearer <ADMIN_TOKEN>" -H "Content-Type: application/json" \
  -d '{"chat_id":"12345678901@chatroom","text":"大家好","at_list":"wxid_a"}'
# => {"id":123, ...}

# 2) 轮询结果（status: 0待处理 / 1发送中 / 2成功 / 3失败 / -1不存在）
curl -H "Authorization: Bearer <ADMIN_TOKEN>" \
  'https://wx-admin.example.com/api/send?id=123'
# => {"status":2,"error":""}
```

注意：`/api/send` 只保证**已入队**，HTTP 200 ≠ 微信已发出。务必轮询到 status 2/3 再判定；
插件配置为 `include_self=false`，已发消息不会回流到 `messages` 表，只在 `outbox` 和 `/api/thread` 里出现。

### 5.2 方式 B：命令行 `send.py`（本机脚本）

位置：`wechat-report-bridge/send.py`（配套 `send.bat`）。

```bash
python send.py 你的群ID@chatroom "今天的日报已生成"
python send.py wxid_xxxxxxxx "请查收" --at wxid_a,wxid_b
python send.py pigwan "发给我自己"
python send.py 婷婷 "晚上好"                       # 昵称必须唯一
python send.py 你的群ID@chatroom --file reports/2026-11-06.md
python send.py filehelper "自测" --wait 30
```

| 参数 | 说明 |
|---|---|
| `chat_id` | 必填，目标会话；自动按 `admin/chats.auto.json` 解析微信号/昵称 |
| `text` | 正文（含空格用引号）；也可 `--file` 从文件读取（相对路径基于 `wechat-report-bridge/`） |
| `--at` | 要 @ 的 wxid，逗号分隔 |
| `--wait` | 等待发送结果秒数，默认 20 |

退出码：`0` 成功、`1` 失败/参数非法、`2` 超时（消息仍在队列，勿重复提交）。

### 5.3 方式 C：本地能力中心 `local_wechat`（发图片/文件/语音等）

`local_wechat.bat` 提供 54 个本地动作，其中消息类动作如下（纯本机、无 Token）：

```bash
local_wechat.bat list message              # 列出消息类动作
local_wechat.bat info message.postText     # 查看参数
local_wechat.bat call message.postText toWxid=wxid_xxx content=你好
local_wechat.bat call message.postAtText room=xxx@chatroom content=开会了 ats=wxid_a,wxid_b
local_wechat.bat call message.postImage toWxid=wxid_xxx path=C:\img\a.jpg
local_wechat.bat status                    # 队列状态
```

| 动作 | 参数 | 说明 |
|---|---|---|
| `message.postText` | `toWxid,content` | 发送文本 |
| `message.postAtText` | `room,content,ats` | 群内 @；`ats` 逗号分隔，`notify@all` 可 @ 全体 |
| `message.postImage` | `toWxid,path` | 发送图片（本机绝对路径） |
| `message.postFile` | `toWxid,path` | 发送文件（本机绝对路径） |
| `message.postVoice` | `toWxid,path` | 发送语音（silk / wav） |
| `message.postVideo` | `toWxid,path` | 以「文件」形式发送视频，非原生视频消息 |
| `message.postEmoji` | `toWxid,path` | 发送本地 GIF 表情 |
| `message.postLink` | `toWxid,content` | 以纯文本发 URL，不渲染卡片 |
| `message.postLocation` | `toWxid,content` | 以文本描述位置 |
| `message.forwardFile` / `forwardImage` / `forwardVideo` / `forwardUrl` | `toWxid,path` 或 `content` | 重发本地文件/URL，不能按原消息 ID 转发 |
| `message.downloadImage` / `downloadFile` / `downloadVoice` / `downloadVideo` / `downloadEmoji` / `downloadCdn` | `kind,limit` | 检索本机已落盘媒体 |
| `message.exportEmoticons` | `convert` | 导出本地表情 |

参数用 `key=value` 传入；JSON/布尔/`null` 会自动解析。任务状态：`0 pending / 1 running / 2 done / 3 failed`。
首次使用需 `local_wechat.bat enable` 并在 helper 控制台 `reload`（或重启机器人）。底层未开放的视频号、收藏夹、
好友删除/改备注、群管理写入等**明确不做假实现**，可用 `local_wechat.bat coverage --detail` 查看逐项原因。

### 5.4 可靠性与限制

- 目标解析：精确 ID → 自定义微信号 → 唯一昵称；匹配多个会拒绝发送，避免误发。格式合法的未知 ID 放行。
- 群 @ 人数上限 8 人（提醒插件侧）；正文 30000 字符上限。
- 发送 worker 心跳文件：`vendor/helper-main/.runtime/outbox_worker.json`，超过 15 秒视为未加载。
- 未加载时在 helper 控制台输入 `reload report_bridge`（**不要动不动重启 helper**，可能触发微信退登需扫码）。
- 微信客户端掉线时消息会留在队列，恢复后补发。

---

## 6. 常见错误

| 现象 | 原因 / 处理 |
|---|---|
| 401 unauthorized | 令牌错误、令牌过期；公网模式用 URL `?token=` 也会被拒，改用 `Authorization: Bearer` |
| 403 只读模式 | `ADMIN_READONLY=1`，需改环境变量重启管理台 |
| Web 502 | frp 隧道或本机 8080/8088 未运行；检查 `frpc.log` 与端口 |
| `warning` 已入队未发送 | `report_bridge` 插件发送模块未加载，控制台 `reload report_bridge` |
| status 长时间 0 | helper 未运行或微信掉线；检查心跳文件与 `/api/state` 状态灯 |
| 昵称发不出去 | 昵称不唯一或未在 `chats.auto.json` 缓存中，改用精确会话 ID |
| 登录后仍 401 | Nginx 开了 `auth_basic` 或没透传 `Authorization` 头 |

---

## 7. 安全提示

1. 8080/8088 只绑 `127.0.0.1`；不要改成 `0.0.0.0`。
2. 公网只放行 443 与 frps `bindPort 15443`；frps 映射端口用 `proxyBindAddr="127.0.0.1"`，避免绕过 HTTPS 直连 5980/5988。
3. 令牌只在服务端与调用方保存；不要用带令牌的 URL 分享链接。
4. 两个服务必须两个域名/两个端口，不能同域名按路径分流。
