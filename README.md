# 微信工具（wechat-tools）

一套跑在自己电脑上的微信群消息采集与日报工具。核心是一个接在 [helper 微信机器人框架](https://prometheus-first.github.io/helper/) 上的 `report_bridge` 插件：把群消息、分享链接、公众号文章批量上报到本机接收端，落库后自动生成日报，并提供一个网页管理台查看/检索。

> **重要前提**：本仓库只是「插件 + 接收端 + 管理台 + 拟人大脑」这一层，它**依赖一个外部框架 helper 才能跑起来**。仓库里**不含** helper 源码、Python 运行时、frp 客户端，也没有任何账号密钥。请先按下面的「准备依赖」把这三样补齐，否则脚本会报找不到文件。

---

## 目录结构

```
wechat-tools/
├── wechat-report-bridge/        # 本仓库主体
│   ├── server/                  # 消息接收端（零第三方依赖，Python 3.8+）
│   │   ├── app.py               # HTTP 服务：鉴权入库 + 日报查询
│   │   └── report.py            # 日报统计/渲染
│   ├── admin/                   # 网页管理台（零第三方依赖）
│   │   ├── app.py               # 管理台服务，默认 127.0.0.1:8088
│   │   ├── article_push.py      # 公众号文章推送（需本地配置）
│   │   └── index.html / app.js / style.css
│   ├── plugin/                  # report_bridge 插件（装到 helper 里）
│   ├── brain/                   # 拟人大脑：群内拟人回复（可选）
│   ├── local_wechat/            # 本地 CLI 工具集
│   ├── data/                    # 敏感词库等静态数据
│   ├── reports/                 # 历史日报示例
│   ├── launcher.py              # 单组件启动器
│   ├── run_all.py               # 统一启动台（一键拉起所有组件）
│   ├── wechat_guard.py          # 微信掉线守护
│   ├── health_monitor.py        # 全链路巡检 + 飞书告警
│   ├── daily_report.py          # 命令行生成日报
│   ├── send.py                  # 命令行发消息
│   └── *.bat                    # Windows 双击启动脚本
├── 消息API与发送文档.md          # 接收端 API 与发送方式说明
└── .gitignore                   # 排除密钥/运行时/缓存
```

---

## 一、准备依赖（必做）

仓库出于体积和安全考虑，故意没有提交以下内容，需要你自己准备：

| 依赖 | 说明 | 放置位置 |
|---|---|---|
| **Python 3.13（64 位）** | 所有 `.bat` 脚本硬编码用这个路径下的解释器 | `runtime/python313/python.exe` |
| **helper 框架** | 真正的微信机器人内核，插件要装进它 | `vendor/helper-main/` |
| **frp 客户端**（可选） | 需要公网访问接收端时才用 | `frp/frp_0.72.0_windows_amd64/frpc.exe` |

**Python**：任选一个 64 位 Python 3.13，放到 `runtime/python313/python.exe`。所有 `.bat` 都按这个相对路径找解释器；如果你用系统 Python，需要自行改脚本里的路径。接收端 `server/app.py` 只用标准库，Python 3.8+ 就能单独跑。

**helper 框架**：到 [helper 文档站](https://prometheus-first.github.io/helper/) 按其说明获取并放到 `vendor/helper-main/`。注意它的许可：**允许个人自用，禁止商业使用**，分享时请保留其协议与版权声明。

> helper 的 `requirements.txt`、`pip install` 等步骤以其官方文档为准，本仓库不重复维护。

---

## 二、配置（密钥都放本地，不会上传）

所有含密钥/令牌/群 ID 的文件都叫 `*.local.json`，已被 `.gitignore` 排除，**不会进 Git**。仓库里只提供 `*.example.json` 模板，复制后填写：

| 模板 | 复制成 | 用途 |
|---|---|---|
| `wechat-report-bridge/config.example.json` | 合并进 helper 的 `bot_config.json` | 插件采集参数、上报地址、令牌 |
| `wechat-report-bridge/alert.example.json` | `alert.local.json` | 飞书告警机器人 webhook |
| `wechat-report-bridge/brain/config.example.json` | `brain/config.local.json` | 拟人大脑总开关与群配置 |
| `vendor/helper-main/bot_config.example.json` | `bot_config.json` | helper 自身账号配置 |

关键点：

1. **接收端令牌**：接收端从环境变量 `BRIDGE_TOKEN` 读取，插件里的 `token` 必须和它一致。两边都用 `CHANGE_ME` 时插件只本地落库、不对外上报。
2. **模型 API Key**：只写进 `brain/llm.local.json` 或 helper 的 `llm_config.json`，均已排除。网页里设置只回显末 4 位。
3. **飞书 webhook**：写进 `alert.local.json`，已排除。不想要告警就在模板里设 `enabled: false`。
4. 不要新建任何 `*.local.json` 之外、却含密钥的文件；如果确实需要，先确认 `git status` 里看不到它再提交。

---

## 三、启动

### 方式 A：统一启动台（推荐）

双击 `wechat-report-bridge/start_all.bat`，一个窗口拉起接收端、管理台、微信守护、frp 隧道、拟人大脑等全部组件，日志汇总在同一窗口，Ctrl+C 一起关闭。

常用参数（改 `.bat` 或命令行传入）：

```
python run_all.py              # 接管模式：重启旧组件
python run_all.py --reuse      # 复用模式：只补启动缺失的
python run_all.py --dry-run    # 只打印计划，不实际启动
python run_all.py --no-alert   # 不拉起飞书告警
python run_all.py --no-brain   # 不拉拟人大脑
```

### 方式 B：单独启动

- 接收端：`server/app.py`，默认 `127.0.0.1:8080`
- 管理台：`admin.bat`，默认 `127.0.0.1:8088`，浏览器打开即用
- 生成日报：`report.bat [日期]`
- 发消息：`send.bat <会话ID> <文本>`
- 完整检查：`check.bat`
- 拟人大脑：`brain/启动大脑.bat` 或 `brain/大脑_静默启动.bat`

---

## 四、接收端 API

接收端提供以下接口（详见 `消息API与发送文档.md`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/messages` | 批量上报消息/链接/公众号，按 key 幂等 |
| GET | `/api/messages?day=YYYY-MM-DD` | 查原始消息 |
| GET | `/api/links?day=...` | 查采集到的链接 |
| GET | `/api/official?day=...` | 查公众号消息 |
| GET | `/api/report?day=...&format=md` | 结构化日报 / Markdown |
| GET | `/health` | 健康检查 |

鉴权：请求头 `Authorization: Bearer <BRIDGE_TOKEN>`。

> 默认只监听本机 `127.0.0.1`。要放到公网服务器，请设置 `BRIDGE_HOST=0.0.0.0` 并在前面用 Nginx/Caddy 终止 HTTPS，**不要把裸服务直接暴露到公网**。

---

## 五、拟人大脑（可选）

`brain/` 是一个独立旁路进程：只读消息库、只写发送队列，不开端口、不改机器人插件。关掉进程群聊立刻恢复原状。

安全用法（务必按顺序灰度）：

1. 保持 `enabled=true` + `dry_run=true`（演练），在网页「应用 → 拟人大脑」勾选**只放一个测试群**。
2. 配好大模型 Key，点「测试连通」。
3. 演练跑 1–2 天，看「决策日志」里每条回复是否合理。
4. 确认没问题后，再关掉演练模式、逐步加群。

更完整的参数说明见 `wechat-report-bridge/brain/README.md`。

---

## 六、安全与合规

- 仓库内所有 `*.local.json`、`*.sqlite3`、`*.log`、缓存目录均已被 `.gitignore` 排除。
- 密钥只存在本地，接口只回显末 4 位。
- 建议用**独立的微信小号**、不绑支付，遵守微信相关使用条款。
- helper 框架禁止商业使用，请勿用于任何商业用途。
- 群消息属于他人隐私，采集与存储请取得群成员知情同意，仅用于个人学习研究。

---

## 七、常见问题

**Q：双击 .bat 报找不到 python.exe？**
A：按「准备依赖」把 64 位 Python 3.13 放到 `runtime/python313/python.exe`，或修改 `.bat` 里的路径。

**Q：插件上报失败 / 接收端 401？**
A：检查插件 `token` 与接收端 `BRIDGE_TOKEN` 是否一致；接收端是否启动。

**Q：管理台打不开？**
A：确认 `admin/app.py` 在运行，浏览器访问 `http://127.0.0.1:8088`。

**Q：想彻底关掉某个功能？**
A：改对应的 `*.local.json`，总开关类配置支持热更新；或启动时用 `--no-xxx` 参数。

**Q：为什么没有 helper / Python / frp？**
A：体积大、且各自有独立许可与更新节奏，故不入库。按第一节自行准备。
