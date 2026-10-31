# 微信记忆工作台 v2.0.10 · WSLC 本机开发

更新：2026-10-30。命令按 v2.0.10 源码核对；本文描述独立的 WSLC 开发环境，不代表本次已执行部署。Docker/Compose、离线传输和通用升级流程见 [Docker 服务端部署说明](DOCKER-SERVER.md)，本次验证及历史验收边界见[验证记录](../VERIFICATION.md#v2010-docs)。

## 运行入口

- 管理页面：<http://127.0.0.1:8787/>
- 健康检查：`GET /healthz`；数据库就绪检查：`GET /readyz`。
- 个人 MCP：`http://127.0.0.1:8787/mcp`，请求头 `Authorization: Bearer <个人 Key>`。
- 初始管理员登录信息：`.private/wslc-v2/admin-login.txt`。首次登录必须修改密码。
- Windows 发布版：`release/WechatHelper-2.0.10-windows-x64-portable.zip`，解压后运行 `WechatHelper/WechatHelper.exe`。部署脚本优先使用当前 `VERSION` 对应便携包，没有时使用 `release/WechatHelper-2.0-dev.zip`；文件存在后登录“客户端管理”即可下载。已有容器更新下载配置需 `-Recreate`。
- 管理员、数据库及主加密密钥：`.private/wslc-v2/credentials.json`；这些文件已排除出 Git。

已有数据库应先阅读[升级与备份](DOCKER-SERVER.md#upgrade)，保留原 `.private/wslc-v2/credentials.json`、主密钥、环境文件和全部数据卷。下列命令会操作该脚本约定的开发容器；首次启动或 `-Recreate` 会执行迁移，应在配套备份和隔离演练完成后再运行。

```powershell
# 创建 / 启动本项目的 WSLC 数据库及服务端
.\deploy-wslc-dev.ps1

# 修改服务端代码后重建部署，数据库卷保持不变
.\deploy-wslc-dev.ps1 -Recreate

# 只启动数据库
.\deploy-wslc-dev.ps1 -InfrastructureOnly

# 启动 Windows 源码客户端，优先使用隔离的 Python 3.12 开发虚拟环境
.\start.ps1

# 打包开发客户端，保留 dist/ 中的旧版本
.\build-client-dev.ps1

# 发布构建（Python 3.12、requirements-dev-lock.txt、Inno Setup 6）
.\build-release.ps1 -Python .\.venv-dev\Scripts\python.exe

# 在短暂停止本项目服务端后，备份 MySQL、MongoDB 和加密恢复材料
.\backup-wslc-dev.ps1
```

`deploy-wslc-dev.ps1` 使用 PowerShell 7 和 `wslc`，镜像标签跟随仓库 `VERSION`（当前 `wechat-helper-server:2.0.10`）；发布构建也默认读取 `VERSION`。脚本不会删除数据库卷。已有服务端只有显式 `-Recreate` 才替换容器；默认再次运行保留已有容器镜像。所有发布端口仅绑定 `127.0.0.1`。`backup-wslc-dev.ps1` 在退出时尝试恢复应用原运行状态，升级维护窗口仍需单独停止全部写入。

| 组件 | WSLC 容器 | 用途 | 本机端口 |
| --- | --- | --- | --- |
| 服务端 + 持久化任务处理器 | `wechat-v2-server` | Web、REST、MCP、自动分析 | 8787 |
| MySQL 8.4 | `wechat-v2-mysql` | 用户、权限、加密消息、任务、版本指针、审计 | 13306 |
| MongoDB 7 | `wechat-v2-mongo` | 加密 Markdown 正文、证据与不可变版本 | 17017 |
| Redis 7 | `wechat-v2-redis` | 认证限流、任务唤醒通知 | 16379 |

独立网络为 `wechat-v2`，对应持久化卷为 `wechat-v2-mysql`、`wechat-v2-mongo`、`wechat-v2-redis`、`wechat-v2-server`。任务处理器以 MySQL 为队列事实来源并持续轮询；不依赖 Redis 保存任务。

## 现在可以使用的完整流程

1. 使用初始管理员登录并修改密码，在“用户管理”开通普通用户。管理员可停用用户、重置密码、查看平台统计和管理客户端、MCP Key；默认不能读取其他用户的通讯录、聊天和记忆。
2. 普通用户登录并修改初始密码，创建客户端，保存首次展示的 `appKey` / `appSecret`。
3. 启动客户端，在“连接设置”填写地址和客户端凭据，确认上传授权并通过连接检测。客户端自动发现微信账号；有多个账号时选择一个。当前源码仅上传群聊、通讯录好友私聊及对应联系人资料，排除陌生人私聊、已删除好友、公众号与系统推送；此筛选从 v2.0.2 开始生效。关闭窗口后可从系统托盘打开或退出。
4. 在网页按微信账号查看通讯录与会话，搜索已上传消息，将会话加入监听。上传、监听和 MCP 授权相互独立。停止监听保留消息与记忆，但停止分析并阻止未提交任务发布结果。
5. 配置自己的 Chat Completions 兼容模型 API 地址、模型标识和可选 API Key。无默认模型、无平台模型、无自动供应商回退。未配置时数据仍同步，已到期任务显示“等待模型配置”。测试连接只发送固定测试文本。
6. 五种周期默认全部开启。按上海时区自然日、周一开始的自然周、自然月、1—6 / 7—12 月、自然年计算，区间为 `[开始, 结束)`。完整周期结束后 2 分钟进入可执行状态；加入监听时补齐已有历史。迟到或更正消息会重排相关周期。
7. 周期 Markdown、关系记忆、综合个人画像自动保存，无人工证据确认门槛。网页可阅读、追溯证据、查看历史版本、修订和下载 `.md`。人工修订成为当前正文，后续自动结果保留为历史版本。
8. 创建只读或全部权限 MCP Key，可以限定会话、设置期限、轮换、禁用或撤销。默认范围为监听会话；综合个人画像需要额外授权。只读 Key 可查询消息、记忆和请求回复建议；全部权限 Key 包含这些查询能力，并可创建 / 更新记忆、调整已授权监听。旧版写 Key 在服务端启动时自动升级为全部权限，原密钥、授权范围和到期时间保留。API 权限值为 `read` / `full`，兼容旧请求中的 `write` 并规范化为 `full`。

回复建议支持自然回复和继续中断话题，不发送微信消息，也不声称能找回本机不存在的聊天记录。用户未配置模型时，MCP 返回授权聊天上下文，供接入的外部 AI 自行起草。

## 实现约定

- Docker 服务端的环境变量、数据库软件版本检查与应用结构迁移见 [Docker 服务端部署说明](DOCKER-SERVER.md)。

- 服务端入口仍为 `server_main.py`。`WECHAT_PLATFORM_VERSION=2` 选择新平台；没有此变量时保留旧版入口兼容。
- 业务实现：`backend/platform_db.py`、`platform_service.py`、`platform_jobs.py`、`platform_app.py`。
- Windows 客户端入口为 `main.py`，控制界面和生命周期分别在 `helper/collector_app.py`、`helper/collector.py`；上传实现为 `helper/platform_sync.py`。旧版本机日历、知识库等代码保留，但精简客户端不提供这些入口。
- 客户端凭据保存于 Windows DPAPI 加密文件；`appSecret` 不上传到模型，也不等同于微信数据库密钥。数据库路径和微信解密密钥不上传服务端。
- 注册凭据绑定设备；换电脑须解绑并轮换凭据。短期客户端令牌有效 24 小时，自动重新注册。重置密钥或停用客户端即时撤销令牌。
- 上传批次采用 AES-256-GCM，AAD 绑定 appKey 与批次 ID。服务端校验密文、时间与批次幂等；消息按稳定服务端 ID 去重，无服务端 ID 时使用客户端、分片和本地 ID 组合。
- MySQL 消息、联系人、会话正文与 MongoDB 记忆使用用户级 AES-GCM 加密；主密钥保存在数据库之外。为了云端检索和调用用户模型，服务端拥有受控解密能力，这不是服务器无法解密的端到端加密。
- Web 使用 HttpOnly / SameSite 会话 Cookie、同源写请求校验、Host 校验；客户端、Web 和 MCP 的凭据不可互换。关键操作有审计，正文和明文密钥不进入审计。
- MongoDB 先写不可变正文，MySQL 在事务内发布版本索引；MongoDB 写失败不能发布成功状态。SQL 回滚可能留下未引用的文档，不会出现已发布索引指向尚未写入正文的情况。
- 任务使用数据库租约、修订号、监听版本与模型配置版本检查；每次模型调用前以及最终提交时复查，重启后过期租约重新领取。历史同步完成门槛、跨设备分析协调见 [Docker 部署说明](DOCKER-SERVER.md)；模型失败与付费调用重试边界见[模型可靠性说明](MODEL-RELIABILITY.md)。
- 本机开发允许连接自建内网模型；公网模型仍要求 HTTPS，禁止链路本地元数据地址和跳转。远程部署需显式调整允许 Host、HTTPS 入口及模型网络策略。
- 搜索会解密用户指定范围后匹配，数据库中没有消息明文搜索索引。当前适合本机开发验证，尚未进行大规模历史性能验收。
- `requirements-server-lock.txt` 固定服务端依赖；`requirements-dev-lock.txt` 固定 Windows Python 3.12 的开发和打包依赖。使用独立 `.venv-dev` 安装开发依赖，避免覆盖其他环境。

## 验证与演练

```powershell
.\.venv-dev\Scripts\python.exe -m pytest -q
```

已有功能回归、新版权限与加密测试、真实 SQLite 解析到 HTTP 上传的客户端测试均纳入 `tests/`。新增覆盖：重复上传、密文篡改、跨用户访问、设备绑定、密钥轮换与停用、五类周期、未配置模型、停止监听时的提交检查、过期任务租约恢复、未来周期不得提前执行、Mongo 写失败不发布索引以及人工版本保护。

历史 WSLC 集成演练使用独立 QA 用户和合成聊天；相关工具包括 `tools/verify_wslc_v2.py` 和 `tools/verify_wslc_recovery.py`。`tools/verify_packaged_client.py` 针对旧版客户端接口，尚未适配精简客户端，不能作为新客户端验收依据。

验证报告保存在 `.private/wslc-v2/verification.json`、`recovery.json`、`packaged-client-verification.json`。网页实际检查了登录、用户管理、记忆列表及正文/证据页面；真实微信版本与用户自有模型的效果需在配置后继续验证。

## 升级与恢复边界

V2 自动迁移只处理其 MySQL/MongoDB 数据，不会自动迁移 1.x SQLite 或猜测“旧账号 → 新平台用户”的对应关系。若环境中保留了 1.x 的独立容器、原凭据和旧卷，应另行备份，不要删除或接入 V2 数据库冒充回滚。

v2.0.10 新增 SQL V6 逻辑会话池，MongoDB 保持 V2；不能直接让旧 V2 镜像连接已迁移的库。恢复应在隔离库/卷中成组还原同一停写点的 MySQL、MongoDB、必要文件及原主密钥，配合匹配的旧镜像验收后再切换；Redis 可重建。完整步骤及升级后新增数据的保留边界见[升级与备份](DOCKER-SERVER.md#upgrade)。

v2.0.10 已实现同一平台用户下按完全相同的非空微信 `self_id` 合并会话、按有效 `server_id` 去重，并保留设备原始消息与同步记录；不同账号或标识不明时不猜测合并。v2.0.9 客户端可继续使用，更旧客户端可能因缺少历史完成确认而阻塞分析。

仍需实际环境验收：真实旧库升级、历史数据建池、多设备与离线来源恢复、跨库备份恢复和大规模性能。附件原文件/多模态识别、配额与存储容量运营、永久删除及备份保留期清理、主密钥轮换仍需独立开发或验收；不自动迁移旧本机日历或已确认知识，也不提供找回丢失微信数据的承诺。
