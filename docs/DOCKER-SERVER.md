> **历史文档**：本文记录旧版 v2.0.10 的环境和操作，不对应开源版 v1.0.0，旧版链接、镜像标签和命令不适用于首次部署。当前操作请使用 [开源版部署指南](QUICKSTART.md)。

# v2.0.10 服务端构建、部署与升级

服务端镜像默认启动 2.0 记忆平台。启动顺序为：读取环境配置 → 检查 MySQL/MongoDB 软件版本 → 执行应用迁移 → 初始化管理员 → 启动 HTTP/MCP 与后台任务。迁移失败时不提供服务。已有部署先阅读[升级与备份](#upgrade)，不要按首次部署步骤重新生成密钥。

## 版本与发布状态

2026-10-30 核对结果：

- [`v2.0.10` Tag](https://github.com/w89588/wechat-helper/tree/v2.0.10) 解析到提交 [`2c5f67413a594e78bd34512af600d6477bb89674`](https://github.com/w89588/wechat-helper/commit/2c5f67413a594e78bd34512af600d6477bb89674)，当时与 `main` 一致，`VERSION` 为 `2.0.10`。
- [v2.0.10 Release](https://github.com/w89588/wechat-helper/releases/tag/v2.0.10) 已发布，非 draft、非 prerelease；发布时间为 `2026-10-28T18:16:34Z`，发布说明使用日期 2026-10-29。
- Release 附件为 `WechatHelper-2.0.10-windows-x64-setup.exe`、`WechatHelper-2.0.10-windows-x64-portable.zip` 和 `SHA256SUMS.txt`，不包含服务端 Docker 镜像或离线镜像包。
- [`windows-release.yml`](../.github/workflows/windows-release.yml) 从指定 Tag 测试、构建并上传 Windows 客户端到已有 draft Release，不构建或推送 Docker 镜像，也不自动发布 Release。[该提交的 Windows 构建](https://github.com/w89588/wechat-helper/actions/runs/40742136378)已成功；这不能代替 Docker 或数据库升级验收。

服务端须从对应源码自行构建。仓库未配置服务端镜像发布流程，不要将下文的本地标签当成可直接 `docker pull` 的公共仓库镜像。`Dockerfile` 使用固定摘要的 Python 3.12 基础镜像，按 `requirements-server.txt` 和 `requirements-server-lock.txt` 安装依赖，并复制 `backend/`、必要的 `helper/` 文件、`server_main.py` 和 `VERSION`；`.env` 不进入镜像。

## 首次配置与在线构建

以下命令在仓库根目录执行，使用 Linux 容器模式的 Docker Engine 和 Docker Compose v2 或更新版本。配置脚本需要 Python 3.12；可将 `python` 替换为可用的 Python 3.12 命令。包含反斜杠续行的示例使用 POSIX Shell，Windows PowerShell 中应合为一行或改用反引号续行。

要构建发布时的源码，可使用独立目录检出 Tag：

```sh
git clone --branch v2.0.10 --single-branch https://github.com/w89588/wechat-helper.git
cd wechat-helper
git rev-parse HEAD
```

应得到上文的提交。已有工作区应先保护未提交改动，不要为切换版本覆盖原部署配置或数据。

仅首次部署生成配置：

```sh
python tools/init_server_env.py
```

脚本从 `.env.example` 创建 `.env`，生成数据库密码、初始管理员密码和主加密密钥，不覆盖已有文件。**已发布的 v2.0.10 Tag 中，Compose 与 `.env.example` 仍含 `2.0.4` 默认镜像标签；本次文档修正不改变该 Tag。** 无论从 Tag 还是当前分支部署，都应确认 `.env` 中以下值正确，已有配置只修改镜像项，不替换密钥：

```dotenv
WECHAT_IMAGE=wechat-helper-server:2.0.10
```

| 环境变量 | 用途 |
| --- | --- |
| `WECHAT_IMAGE` | Compose 构建和运行的本地镜像标签；必须与实际源码版本一致 |
| `WECHAT_DATABASE_URL` | MySQL SQLAlchemy 地址，如 `mysql+pymysql://user:password@host:3306/wechat?charset=utf8mb4` |
| `WECHAT_MONGO_URL` | MongoDB 地址，必须包含业务数据库名；可通过 `authSource` 指定认证库 |
| `WECHAT_REDIS_URL` | Redis 地址，包含密码和逻辑库，如 `redis://:password@host:6379/0` |
| `WECHAT_MASTER_KEY` | 原有或首次生成的 32 字节 base64url 主加密密钥；升级必须保留原值 |
| `WECHAT_ADMIN_USER` / `WECHAT_ADMIN_PASSWORD` | 新库的初始管理员；启动不会重置已有管理员 |
| `WECHAT_ALLOWED_HOSTS` | 对外访问使用的 Host，逗号分隔 |
| `WECHAT_HTTP_PORT` | Compose 绑定的宿主机管理端口，默认 8787 |
| `WECHAT_STARTUP_TIMEOUT` | 等待数据库连接就绪的秒数，默认 60，范围 1–600 |
| `WECHAT_MIGRATION_LOCK_TIMEOUT` | 等待其他实例释放迁移锁的秒数，默认 60，范围 1–600 |

连接 URI 中用户名、密码含 `@`、`:`、`/` 等字符时需使用百分号编码。连接字符串填写完整值，不依赖变量展开。Compose 会对未加引号或双引号值中的 `$` 插值，自定义配置应遵循 [Compose 环境文件语法](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)。源码入口读取 `.env` 时禁用插值，且不覆盖进程已有变量。

确认 Shell 中没有旧 `WECHAT_IMAGE` 等变量覆盖 `.env`，再检查解析后的镜像名、构建应用并取得数据库镜像：

```sh
docker compose --env-file .env config --quiet
docker compose --env-file .env config --images
docker compose --env-file .env build server
docker compose --env-file .env pull mysql mongodb redis
docker compose --env-file .env up -d --no-build --pull never
```

`config --images` 应列出 `wechat-helper-server:2.0.10`、`mysql:8.4`、`mongo:7` 和 `redis:7-alpine`。完整 `config` 可能显示密钥，不要将其输出贴到公开工单。`--pull never` 要求四个镜像均已在本地。

`compose.yaml` 提供四个持久化卷。数据库端口不对宿主机开放；管理端口默认仅绑定 `127.0.0.1:8787`。`.env` 的 `MYSQL_*`、`MONGO_INITDB_*`、`REDIS_PASSWORD` 用于这些容器；已有数据卷的数据库密码不会因修改环境变量而改变。远程客户端访问需要自行配置 HTTPS 和允许 Host。

### 使用外部数据库

将三个连接 URI 改为**容器内可访问**的外部地址；`mysql`、`mongodb`、`redis` 这些服务名只在相应 Compose 网络内有效。不要用 Compose 的 `up` 顺带启动不需要的数据库。

```sh
docker build -t wechat-helper-server:2.0.10 .
docker run -d --name wechat-server --env-file .env --pull=never \
  -p 127.0.0.1:8787:8787 \
  --mount type=volume,source=wechat-server-data,target=/data \
  wechat-helper-server:2.0.10
```

这是首次独立运行示例。已有独立容器升级时需保留原网络、端口、挂载、配置及数据库连接，先备份再替换应用容器。`/data` 卷不能代替外部 MySQL/MongoDB 备份。源码入口也可用 `python server_main.py --env-file /path/server.env` 指定配置文件。

## 离线传输与部署

在联网构建机上，先完成上面的源码版本核对和服务端构建。构建机需能获取基础镜像与 Python 依赖；离线包必须包含目标主机所需的 Linux CPU 架构，不能把 Windows 客户端 ZIP 或不匹配的架构镜像用于服务端。

首次使用附带的数据库服务时，一并导出四个镜像。以下为 POSIX Shell 示例，默认构建机与目标机架构相同：

```sh
docker pull mysql:8.4
docker pull mongo:7
docker pull redis:7-alpine
docker save -o wechat-helper-2.0.10-offline.tar \
  wechat-helper-server:2.0.10 mysql:8.4 mongo:7 redis:7-alpine
sha256sum wechat-helper-2.0.10-offline.tar > wechat-helper-2.0.10-offline.tar.sha256
```

同时保存源提交、镜像 ID/摘要和架构，随包传输 `compose.yaml` 及实际使用的 override 文件。数据库标签可能随时间变化；后续升级应保留已验收的数据库镜像，不要借应用升级更换数据库软件版本。已有外部数据库时只需 `docker save -o wechat-helper-server-2.0.10.tar wechat-helper-server:2.0.10`。

目标机校验并导入后，首次 Compose 部署执行：

```sh
sha256sum -c wechat-helper-2.0.10-offline.tar.sha256
docker load -i wechat-helper-2.0.10-offline.tar
docker compose --env-file .env config --quiet
docker compose --env-file .env config --images
docker compose --env-file .env up -d --no-build --pull never
```

单镜像包对应 `docker load -i wechat-helper-server-2.0.10.tar`，再按外部数据库方式启动。离线目标机需已安装 Docker 和 Compose。`.env` 与原主密钥通过受保护的独立渠道保存或传输，不放进公共镜像或发布附件；`docker save` 不备份数据库卷。Windows 可使用 `Get-FileHash -Algorithm SHA256` 校验归档，不要经旧版 PowerShell 的文本重定向传输二进制归档。已有环境应继续下面的升级流程，不能套用首次部署去创建另一组卷。

<a id="upgrade"></a>

## 升级与备份

### 1. 确定恢复边界并停止写入

升级前记录原镜像 ID、源码版本、Compose 项目名、实际数据库名、网络、卷名、bind mount 和配置来源。保持原 `COMPOSE_PROJECT_NAME` / `-p`、`-f` 与部署目录约定；换目录导致项目名变化会创建另一组空卷。本节命令假设使用默认 `compose.yaml`、`.env` 和数据库名 `wechat`；如有自定义参数，每条命令都须沿用原参数。

暂停客户端同步、限制入口写请求，并停止**所有连接这组数据库的应用实例和后台 worker**。默认 Compose 的 worker 在 `server` 进程内；只需停止应用，数据库保持运行：

```sh
docker compose --env-file .env stop server
```

备份期间禁止其他管理操作、模型任务、迁移和 DDL。单独的 MySQL 事务快照不能保证与 MongoDB 的跨库一致性；只有共同停写后取得的配套备份才可作为同一恢复点。

| 必须保留的材料 | 内容与边界 |
| --- | --- |
| MySQL 业务库全量备份 | 用户、设备、加密账号/联系人/消息、每设备同步游标所依赖的原始记录、监听、任务、记忆版本指针、权限、迁移记录与逻辑会话池；不能只导出新增表 |
| MongoDB 业务库全量备份 | `memory_versions` 中的密文正文、历史版本及 `_wechat_schema` 等元数据；与 MySQL 版本指针配套 |
| 原 `WECHAT_MASTER_KEY` 与原配置 | 必须保留应用实际使用的原值，包括环境覆盖或外部密钥管理系统中的值；仅备份 `.env` 不一定充分。重建主密钥会使原密文无法解密，重新生成管理员密码也无法修复 |
| Compose、override、镜像与文件挂载 | 原项目名和卷映射、可回用的旧镜像、`server-data:/data` 及自定义 bind mount 中的必要文件；数据库备份不包含这些文件 |

Redis 用于缓存、限流与唤醒，任务队列的事实来源是 MySQL；Redis 不是业务恢复来源。Windows 客户端的本机数据和 DPAPI 凭据也不包含在服务端备份中。1.x 的旧 SQLite/独立卷不在这套 V2 自动迁移范围内，应另行保留。

### 2. 制作并检查配套备份

以下 **POSIX Shell** 示例仅适用于默认 Compose 数据库容器、默认数据库名和现有有效密码。自建或托管数据库应使用其经过演练的备份方法；如数据库名不同，应同时调整 MySQL 和 `mongodump --db`。先确认容器中有相应备份工具和足够空间。

```sh
set -eu
umask 077
backup=".private/backups/pre-2.0.10-$(date +%Y%m%d-%H%M%S)"
mkdir -p .private/backups
mkdir "$backup"
cp .env "$backup/server.env"
cp compose.yaml "$backup/compose.yaml"
docker compose --env-file .env images server > "$backup/server-image.txt"
docker compose --env-file .env exec -T mysql sh -c \
  'MYSQL_PWD="$MYSQL_PASSWORD" mysqldump --user="$MYSQL_USER" --single-transaction --no-tablespaces --set-gtid-purged=OFF --databases "$MYSQL_DATABASE" > /tmp/wechat-upgrade.sql'
docker compose --env-file .env cp mysql:/tmp/wechat-upgrade.sql "$backup/mysql.sql"
docker compose --env-file .env exec -T mongodb sh -c \
  'mongodump --username="$MONGO_INITDB_ROOT_USERNAME" --password="$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase=admin --db=wechat --archive=/tmp/wechat-upgrade.archive.gz --gzip --quiet'
docker compose --env-file .env cp mongodb:/tmp/wechat-upgrade.archive.gz "$backup/mongo.archive.gz"
test -s "$backup/mysql.sql"
test -s "$backup/mongo.archive.gz"
(cd "$backup" && sha256sum mysql.sql mongo.archive.gz server.env compose.yaml > SHA256SUMS.txt)
(cd "$backup" && sha256sum -c SHA256SUMS.txt)
```

另外复制实际使用的 override、外部密钥恢复材料和所需文件卷快照，并保存原镜像。备份含密钥与私人数据，应限制访问并安全异地保留。导出失败时保持停写，先处理错误；文件非空和哈希正确只证明文件完整，**不能代替隔离恢复演练**。完成后按运维策略清理数据库容器 `/tmp/` 中的临时备份副本。

WSLC 开发环境可参考 [`backup-wslc-dev.ps1`](../backup-wslc-dev.ps1)：它只处理该脚本约定的 `wechat-v2-*` 容器与 `wechat` 库，并保存 `.private/wslc-v2/credentials.json`；会在退出时尝试恢复应用原运行状态，不能单独充当完整升级停写窗口。Compose 或其他部署不要直接套用这个脚本。

### 3. 只替换应用镜像并检查迁移

新镜像应已在独立源码目录构建或从离线包导入；在原部署目录保留原配置和所有数据库卷，只将 `.env` 的 `WECHAT_IMAGE` 改为 `wechat-helper-server:2.0.10`。首次启动即会迁移，所以必须先完成备份和隔离演练。

```sh
docker compose --env-file .env config --quiet
docker compose --env-file .env config --images
docker compose --env-file .env up -d --no-deps --no-build --pull never server
docker compose --env-file .env ps server
docker compose --env-file .env logs --tail=100 server
```

`--no-deps` 保持已运行的 MySQL/MongoDB/Redis 不变。构建镜像本身不会替换应用容器。日志应显示 SQL schema **6**、MongoDB schema **2**；`GET /healthz` 应返回应用版本 `2.0.10`，`GET /readyz` 应显示 SQL/MongoDB 为 `ok`、对应结构版本和 worker 状态。Redis 不可用时 `degraded=true`，即使 HTTP 200 也不等于全部组件正常。

保持客户端暂停，先检查已有消息和记忆可解密、权限正确、卷与原配置一致；随后分批恢复客户端同步并检查下文的合并和历史完成状态。就绪检查通过不代表所有历史会话池已建立或全量历史已同步。

### 4. 迁移失败与恢复

迁移锁只串行化迁移，不保证新旧应用混跑安全。不要让旧镜像连接已升到 schema 6 的数据库；旧程序会拒绝高于自身支持范围的结构版本。不要通过删除版本记录、手工填充历史完成状态、清空同步断点或 `docker compose down -v` 绕过检查。

MySQL DDL 可能隐式提交，SQL 与 MongoDB 之间也没有统一回滚事务。失败时保持应用停写，保存日志与升级后数据，按实际迁移状态排查；只有标记 `retry_safe` 的步骤支持自动重试，不能假定全部迁移都幂等。SQL V3 的账号来源改写未标记为可安全重试；V6 的建表步骤则支持重试。

需要恢复时，先在**新的隔离数据库/卷**中恢复升级前同一停写点的完整 MySQL、MongoDB、必要文件和原主密钥，使用匹配的旧镜像与数据库软件版本验证解密、权限和任务，再计划切换。保留原库以便诊断；不能只恢复其中一库、只改镜像标签或将两个时点的数据混用。Redis 使用与恢复环境隔离的可重建实例。恢复旧快照会丢失该恢复点之后的写入，切换前必须保留并规划补录这部分数据；仓库未提供自动无损降级或完整跨库恢复保证。

## v2.0.10 迁移、跨设备会话与客户端兼容

### 应用结构与按需归并

- [`backend/platform_db.py`](../backend/platform_db.py) 注册 SQL V1–V6：V2 调整 MCP 权限，V3 将账号来源限定到设备，V4 增加模型调用/检查点，V5 增加 `v2_history_sync` 并将 `queued`、`running`、`waiting_model` 任务置为 `waiting_history`，V6 新增 `v2_logical_sessions` 和 `v2_message_pool`。已完成步骤不会重跑；从更旧版本升级会执行所有缺失步骤。
- [`backend/platform_documents.py`](../backend/platform_documents.py) 仍为 MongoDB V1–V2：建立归属索引、补充 `format_version=1`，不改写原密文；v2.0.10 没有新增 MongoDB 迁移。Redis 不执行应用业务结构迁移。
- SQL 版本和执行状态分别保存在 `v2_schema`、`v2_migration_runs`，MongoDB 版本保存在 `_wechat_schema`。未来版本、断号或迁移失败会阻止启动。MySQL 使用按数据库隔离的命名锁；MongoDB 使用带过期时间和续期的迁移锁。
- V6 先建立映射表；已有消息池由 [`backend/platform_identity.py`](../backend/platform_identity.py) 在会话读取、上传或任务处理等路径按需建立/归并，不是启动时对所有历史消息执行一次批量删除或重写。首次访问大量历史会话的耗时仍需环境验收。

MySQL 软件兼容检查范围为 8.0 至 10.0（不含），MongoDB 为 6.0 至 9.0（不含）；不接受 MariaDB。Compose 默认使用 MySQL 8.4、MongoDB 7。旧文档记录的 MySQL 8.4.13 / MongoDB 7.0.45 演练属于历史证据，不代表 v2.0.10 已在所有版本完成升级验收。

新增迁移需在 `SQL_MIGRATIONS` 或 `MONGO_MIGRATIONS` 末尾追加连续版本和明确步骤，不修改已发布迁移或删除版本记录。系统不会根据模型差异猜测删除列、索引或数据；新增旧表列需显式 DDL。

### 合并规则与数据保留

- 只在**同一平台用户**下，解密后非空的微信 `self_id` **完全一致**且联系人/群聊本地标识相同的来源之间合并会话。不同用户、不同微信账号或缺失身份的来源保持分离，不按昵称、备注或相似文本猜测合并。稍后补齐可靠身份时可归并已有池。
- 统一会话池仅在该会话内按有效 `server_id` 选择同一消息的一份副本；空值或 `0` 时保留按来源区分的消息。因此界面消息数可能减少，但 `v2_messages` 中的设备原始记录及每台设备续传所需记录保留，不能为消除重复而删原始数据或断点。
- 旧监听周期合并到统一监听入口，原记忆及历史版本仍可访问。重复来源的监听被关闭，对应 `queued`、`running`、`waiting_history`、`waiting_model` 任务会被标为 `cancelled` 并释放租约；统一入口按需要补排周期。这里保留的是数据与分析连续性，**不是所有旧任务状态原样不变**。
- 自动分析读取合并后的消息池，需等待同一会话的**全部已知来源**确认历史完成，再按时间顺序处理周期。某台来源设备离线或仍在回填时，该会话可能一直显示 `waiting_history`。

### 客户端兼容范围

v2.0.10 服务端兼容现有 **v2.0.9 客户端**，不要求为接收端升级立即重装该版本客户端。需要安装或升级时可用 v2.0.10 Release 客户端。

更旧客户端可能仍能上传清单与消息，但不保证完整兼容，尤其可能不发送 `kind=history` / `history_complete` 确认。新建监听及尚缺确认的来源会阻塞自动分析，即使消息已经可浏览；不要把“上传成功”视为分析就绪。应升级各相关设备到 v2.0.9 或更新客户端、重新联网完成历史同步；不要伪造确认或清空服务端数据。多台设备应分别保留自己的客户端绑定与同步状态。

## 验证证据与仍需验收的内容

本次实际执行结果见 [v2.0.10 验证记录](../VERIFICATION.md#v2010-docs)。现有测试的边界如下：

| 证据 | 代码覆盖与限制 |
| --- | --- |
| [`tests/test_migrations.py`](../tests/test_migrations.py) | 数据库版本范围、SQLite 迁移顺序/只执行一次、失败不发布、未来版本拒绝、双启动串行、V3 账号 ID/密文保留、V5 历史等待、配置不覆盖。没有实际 MySQL DDL/命名锁或 MongoDB 迁移覆盖 |
| [`tests/test_logical_wechat.py`](../tests/test_logical_wechat.py) | 同账号多设备池去重、不同用户/账号及缺失身份隔离、无消息标识不猜测、各设备游标、等待所有历史、补齐身份、重复监听周期与记忆保留；使用隔离 SQLite、内存文档存储和合成数据 |
| [`tests/test_memory_strategy.py`](../tests/test_memory_strategy.py)、[`tests/test_incremental_sync.py`](../tests/test_incremental_sync.py) | 历史完成门槛、周期顺序、迟到数据与检查点保护、客户端回填和续传；使用测试模型与合成微信库，不等于真实微信多版本验收 |
| [`tools/verify_container_migrations.py`](../tools/verify_container_migrations.py) | 历史工具的 `check` 仍断言 SQL 版本 `[1, 2]`，`seed` 仅删版本 2，会与当前 V1–V6 记录形成断号；不能直接作为 v2.0.10 验收工具。它还会写入/重置 QA 数据，只能经适配后用于独立 `wechat_migration_qa` 环境 |

仍需单独安排：真实 V5 旧结构（缺少两张 V6 表）到 V6 的 MySQL/MongoDB 升级、旧加密数据按需建池、多实例与中断恢复、配套备份恢复及旧镜像恢复演练、离线导入和目标架构启动、大规模历史首次访问性能，以及真实 v2.0.9/较旧客户端混用、离线来源恢复和真实微信解析。现有迁移测试的“旧版”夹具仍使用当前 SQLAlchemy metadata 建表，不能证明真实旧结构到 V6 的完整路径。历史验收条目只适用于其注明的版本与日期，不据此宣称当前生产环境已升级。

命令语义参考 [Compose build](https://docs.docker.com/reference/cli/docker/compose/build/)、[Compose up](https://docs.docker.com/reference/cli/docker/compose/up/)、[镜像 save](https://docs.docker.com/reference/cli/docker/image/save/)、[mysqldump](https://dev.mysql.com/doc/refman/8.4/en/mysqldump.html) 和 [mongodump](https://www.mongodb.com/docs/database-tools/mongodump/) 官方文档。
