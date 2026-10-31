# 服务端与客户端

> 历史文档：本文描述 1.x 的 SQLite 服务端、令牌和客户端流程，不适用于当前 2.0 平台。当前部署请参阅 [Docker 服务端部署说明](DOCKER-SERVER.md) 和 [2.0 部署与开发说明](DEVELOPMENT-2.0.md)。

## 分工与入口

```text
Windows 微信（原始数据库只读）
    ↓ 本机解密、解析；仅处理加入工作台的会话
client_main.py / WechatHelper.exe
    ↓ HTTPS + upload_token：会话清单、结构化消息、上下文
server_main.py / Docker（SQLite 持久化）
    ↑ read_token：/mcp 或 /api/external/*
外部 AI（客户端离线后仍可读已上传历史）
```

服务端实现位于 `backend/`，独立于 `helper.wechat`、DPAPI、密钥提取、WebView 和本地模型。仅 Markdown 导出复用无平台依赖的 `helper.exporting`。客户端仍保留原有浏览、知识库、日历和本机 MCP 功能；知识整理任务仍在客户端执行，服务端保存最近上传的上下文快照。

## 1. 启动服务端

使用 Python 3.12+，推荐单进程运行。先安装独立依赖：

```powershell
python -m venv .venv-server
.venv-server/Scripts/python.exe -m pip install -r requirements-server.txt
```

服务端启动时必须提供账号配置。一个服务端可配置多个账号，每个账号的两种凭据必须不同，且所有账号之间也不能复用 token。账号名只是部署者分配的命名空间，不是微信 ID；第一次同步时会绑定客户端账号目录的 SHA-256 标识，不上传目录路径。更换微信账号或迁移其目录时使用另一个独立账号配置，防止历史混合。

在 PowerShell 中生成配置并启动：

```powershell
$uploadToken = .venv-server/Scripts/python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
$readToken = .venv-server/Scripts/python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
$env:WECHAT_SERVER_ACCOUNTS = @{default=@{upload_token=$uploadToken;read_token=$readToken}} | ConvertTo-Json -Compress
$env:WECHAT_SERVER_DATABASE = 'D:/WechatHelperServer/chats.sqlite3'
.venv-server/Scripts/python.exe server_main.py --host 127.0.0.1 --port 8787
```

将 `$uploadToken` 填入解析客户端，`$readToken` 交给外部 AI。请将生成后的账号配置保存到部署环境的秘密配置中；重新生成 token 并重启会撤销旧凭据。客户端配置中的 token 使用 Windows DPAPI 加密保存，界面和设置响应不会返回原值。

Linux 使用 `python3 -m venv .venv-server`、`.venv-server/bin/python -m pip install -r requirements-server.txt`，通过部署环境注入 `WECHAT_SERVER_ACCOUNTS` JSON 和 `WECHAT_SERVER_DATABASE` 后运行：

```bash
.venv-server/bin/python server_main.py --host 127.0.0.1 --port 8787
```

客户端支持 HTTP 和 HTTPS。局域网可使用 HTTP，例如 `http://192.168.1.10:8787`；HTTP 连接不会加密凭据和请求内容，请仅在可信网络中使用。公网部署建议配置 HTTPS：将 TLS 反向代理指向 `127.0.0.1:8787`，转发 `Authorization` 和正确的 Host / 协议信息，为 `/mcp` 保留流式响应并关闭代理缓冲。默认不开放 CORS，仅允许同源管理页或无 Origin 的原生客户端请求。`/healthz` 是无需凭据的健康检查。

### 服务端管理页

打开服务端首页（本机为 `http://127.0.0.1:8787/`），输入部署时的 `read_token` 登录。WSLC 本地部署可从 `.private/wslc-server/connection.json` 获取该凭据。上传 token 无法登录管理页。

管理页提供服务总览、当前账号的会话与消息统计、最近同步接收时间、会话/备注搜索、消息全文与日期筛选、历史分页、背景查看、导出已显示的 JSON 记录以及 MCP 接入配置。页面从服务端持久化数据库读取，客户端关闭后仍可查看；同步时间表示服务端最近接收到数据，不代表客户端当前在线。

当前管理页是只读管理台。会话选择、备注编辑和上传开关仍在 Windows 客户端操作；服务端凭据仍通过部署环境配置。移出工作台的历史不会出现在管理页及统计中。

页面 HTML / CSS / JS 可公开加载，但任何账号数据都需要读取凭据。token 不进入 URL、Cookie 或浏览器存储，仅保留在当前页面内存中，刷新或退出后需要重新登录。复制 MCP 配置默认使用凭据占位符，显式勾选后才包含本次登录的 token。

### 本机 WSLC

安装提供 `wslc` 容器 CLI 的 WSL 后，在 Windows PowerShell 的项目目录运行：

```powershell
./deploy-wslc.ps1
```

脚本直接通过 `wslc build` 和 `wslc run` 创建服务端容器，不依赖 Debian 等发行版内的 Docker。服务端绑定 `127.0.0.1:8787`，使用 WSLC 命名卷 `wechat-helper-data` 保存数据库；容器默认以普通用户运行，并配置健康检查。

脚本首次生成随机凭据，之后保留同一份凭据。连接信息在忽略目录 `.private/wslc-server/connection.json`，HTTP MCP 配置在 `mcp-config.json`；不要提交或分享这些文件。Windows 客户端的服务端地址填写 `http://127.0.0.1:8787`，上传 token 取该文件的 `upload_token`。

已有镜像时可用 `./deploy-wslc.ps1 -SkipBuild` 启动已有容器。WSLC 当前命令未提供 Docker 的 `--restart` 选项，电脑重启后可手动运行该脚本或 `wslc start wechat-helper-server`。常用命令：

```powershell
wslc list
wslc logs --tail 30 wechat-helper-server
wslc stop wechat-helper-server
wslc start wechat-helper-server
```

重新构建镜像不会更新已有容器；更新代码时运行 `./deploy-wslc.ps1 -Recreate`，脚本先构建镜像，核对原容器的数据卷，再停止并重新创建容器，保留命名卷和凭据。已有镜像可加 `-SkipBuild`。默认不重新创建时，脚本不会更改已有容器创建时的镜像、端口与凭据。

### Docker

在项目根目录构建，镜像只复制服务端及必要的导出代码，不包含聊天数据、密钥、桌面依赖或虚拟环境：

```powershell
docker build -t wechat-helper-server .
docker run -d --name wechat-helper-server --restart unless-stopped -p 127.0.0.1:8787:8787 --env WECHAT_SERVER_ACCOUNTS --mount type=volume,source=wechat-helper-data,target=/data wechat-helper-server
```

运行前设置 `WECHAT_SERVER_ACCOUNTS`。容器默认以普通用户运行，数据库位于持久化卷 `/data/chats.sqlite3`。需要外部访问时由宿主机 TLS 代理转发。备份建议停止服务后复制数据库及仍存在的 `-wal` / `-shm` 文件，或使用 SQLite backup API 创建一致性备份。

## 2. 启动并配置客户端

Windows 安装完整客户端依赖后运行：

```powershell
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe client_main.py --browser --port 8765
```

也可运行 `start.ps1` 或构建后的桌面程序。`main.py` 保持兼容。

1. 在“本机连接”连接微信，主动添加需要同步的会话。
2. 在同一窗口下方“服务端存储与同步”填写 HTTPS 地址及此账号的 `upload_token`。
3. 勾选“允许向此服务端自动上传当前账号的工作台记录”，点击“保存并测试”。未勾选时可仅保存并测试，不会上传。
4. 查看上传进度和最近完成时间；可点击“立即增量同步”或“重新上传全部历史”。

首次同步遍历所选会话的全部本机历史。消息以数据库分片名和 `local_id` 组成稳定 ID，按账号和会话隔离，分批事务提交、去重更新。重启或失败后从服务端已提交的每分片最大 ID 续传，不依赖内存中的偏移。每批最多 200 条，按字节大小继续拆分；服务端每个请求最多 8 MiB、500 条。

自动上传每 15 秒检查一次；设置或会话清单变化会提前唤醒。新消息正常增量处理；每个分片额外重读最近 100 个本地 ID 范围，以更新近期撤回、修改和延迟出现的记录。更早记录的修改使用“重新上传全部历史”刷新。微信本机删除的消息不会自动从服务端删除；服务端承担独立历史存储，完整重传也不清除旧记录。尚未解析成功、未保存在本机的历史和附件无法上传。

仅上传已解析的消息正文、原始消息内容及类型元数据，不上传微信数据库文件、解密/图片密钥、附件二进制或账号目录。备注、背景、最近总结、已确认知识和周期总结会随会话清单一起上传。AI 读取的是最近同步状态，`updated_at` 和 `generation` 可用于判断新鲜度。

每个微信账号分别保存连接设置。变更服务端地址时必须重新填写上传 token。关闭上传不删除已存数据，也不撤销服务端的读取 token；撤销读取需修改服务端配置并重启。移出会话后，下一次清单同步成功会立即隐藏服务端的会话、消息和上下文，同时拒绝旧清单版本的在途上传；网络中断期间远端权限尚未更新，可查看同步状态。重新加入后恢复已有历史。

## 3. 外部 AI 接入

### 直接使用 Streamable HTTP MCP

服务端地址为 `https://chat.example.com` 时，MCP URL 是 `https://chat.example.com/mcp`，请求头为 `Authorization: Bearer <read_token>`。支持 `url` / `headers` 配置的 MCP 客户端可使用：

```json
{
  "mcpServers": {
    "wechat-helper": {
      "url": "https://chat.example.com/mcp",
      "headers": {"Authorization": "Bearer <read_token>"}
    }
  }
}
```

提供六个只读工具：`wechat_status`、`list_sessions`、`read_messages`、`search_messages`、`get_chat_context`、`export_chat`，以及 `summarize_chat` 提示模板。工具的账号由读取 token 决定，不能通过消息参数切换其他账号；不提供发送微信消息、提取密钥或写文件工具。

### 使用原有 stdio MCP 入口连接远端

只支持 stdio 的 AI 客户端可运行 `mcp_main.py` 或发布包中的 `WechatHelperMCP.exe`，通过环境变量指定远程服务端。此模式无需运行 Windows 解析客户端，也不读取本机发现文件：

```json
{
  "mcpServers": {
    "wechat-helper": {
      "command": "D:\\work\\wechat-helper\\.venv\\Scripts\\python.exe",
      "args": ["D:\\work\\wechat-helper\\mcp_main.py"],
      "env": {
        "WECHAT_HELPER_SERVER_URL": "https://chat.example.com",
        "WECHAT_HELPER_READ_TOKEN": "<read_token>"
      }
    }
  }
}
```

远程桥接源码只需 `requirements-server.txt` 中的依赖；未设置这两个环境变量时，继续使用原有本机发现与授权流程。客户端“外部 AI 接入”中的自动配置向导仍用于本机兼容接入；远程接入使用此处配置。

### HTTP API

| 角色 | 方法与路径 | 用途 |
| --- | --- | --- |
| 无需认证 | `GET /healthz` | 健康检查 |
| 上传 token | `GET /api/v1/client/status` | 测试凭据，查询同步状态 |
| 上传 token | `POST /api/v1/client/workspace` | 原子替换所选会话清单，返回 revision |
| 上传 token | `GET /api/v1/client/cursors?user=...` | 已提交的每分片最大 local_id |
| 上传 token | `POST /api/v1/client/messages` | 带 revision 的幂等批量上传 |
| 读取 token | `GET /api/external/status` | 就绪状态、generation、updated_at |
| 读取 token | `GET /api/external/sessions` | q / offset / limit 搜索工作台会话 |
| 读取 token | `GET /api/external/messages` | user / offset / limit / q / start / end / format |
| 读取 token | `GET /api/external/context?user=...` | 已上传的上下文快照 |

消息页每页最多 500 条，`offset` 从最新消息计数，页内按时间升序；`next_offset` 指向更早一页。`start` / `end` 是包含边界的 Unix 秒，0 不限。同步期间 `generation` 改变时应按消息 ID 去重。`format=markdown` 返回内容，由调用者保存。

## 验证

```powershell
.venv/Scripts/python.exe -m pytest -q
```

新增测试包括：跨账号与读写权限隔离、持久化后离线读取、重复和中断上传、消息更新、分片分页、会话撤销、旧 revision 拒绝、原子回滚、请求大小限制、客户端凭据加密，以及真实 HTTP MCP 握手和六个工具调用。
