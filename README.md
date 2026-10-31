# 知微 · Zhiwei

**让每段对话，成为有用的记忆。**

知微是一个面向个人自部署的开源微信对话记忆工具。Windows 客户端只读解析本机微信数据，服务端帮助你浏览消息、整理记忆，并通过 MCP 将授权内容提供给自己选择的 AI 工具。本项目与腾讯或微信官方无关联，也不会代替用户发送微信消息。

[官方网站](https://w89588.github.io/zhiwei/) · [在线演示](https://w89588.github.io/zhiwei/demo/) · [部署与使用](docs/QUICKSTART.md) · [开发指南](docs/DEVELOPMENT.md) · [数据与隐私](docs/PRIVACY.md) · [版本下载](https://github.com/w89588/zhiwei/releases)

> 在线演示只包含虚构数据，不提供注册、客户端上传或真实微信接入。开源版 v1.0.0 是独立版本，与此前私有版本的版本号不对应；已有部署请先备份、在隔离环境验证迁移，不要直接覆盖。

## 主要功能

- **本地解析与同步**：在 Windows 本机读取微信聊天记录；同步群聊和通讯录好友私聊，支持近期消息优先、历史补传与失败重试。
- **多媒体与链接**：支持图片、视频、语音、文件和分享链接的解析与展示；媒体内容可同步到服务端，实际可用性受微信版本及本机文件状态影响。
- **会话与记忆**：在服务端检索、浏览消息，选择监听对象，配置自己的模型生成总结、关系记忆及个人画像。
- **多模态分析**：根据模型能力处理图片、视频画面、音频和文件正文。音频需要支持对应输入的模型；分析会将相关内容发送至所配置的模型服务。
- **MCP 接入**：创建只读或全部权限的 Key，并限制会话授权范围；外部 AI 可以查询消息及可用的媒体内容。
- **个人部署**：服务端支持 Docker Compose；Windows 客户端支持系统托盘和开机启动。

## 快速开始

准备 Docker 和 Docker Compose，以及已登录 Windows 微信的 Windows 10/11 x64 电脑。需要自动分析时，自行准备兼容的模型服务。

```bash
git clone https://github.com/w89588/zhiwei.git
cd zhiwei
python tools/init_server_env.py
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d --build
```

打开 `http://127.0.0.1:8787/`，使用 `.env` 中生成的管理员账号和密码登录并修改密码；创建普通用户及客户端凭据后，从 [Releases](https://github.com/w89588/zhiwei/releases) 下载 Windows 客户端，填写服务端地址并选择微信账号。跨设备访问、局域网设置、图片密钥和故障排查详见 [部署与使用指南](docs/QUICKSTART.md)。

## 数据边界

服务端存储和消息批次使用加密，但服务端持有主密钥并需要解密内容以提供查询和模型分析，**并非服务端无法解密的端到端加密**。客户端会在本机生成解密缓存。HTTP 连接不加密凭据，应只在可信网络中使用；公网建议 HTTPS。AI 服务会收到所选的聊天和媒体内容。详见 [数据与隐私说明](docs/PRIVACY.md)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [部署与使用](docs/QUICKSTART.md) | 环境要求、首次部署、客户端、模型与 MCP |
| [开发指南](docs/DEVELOPMENT.md) | 仓库结构、本地运行、测试、构建和发布 |
| [数据与隐私](docs/PRIVACY.md) | 加密、密钥、备份、媒体和模型边界 |
| [演示站设计](docs/DEMO.md) | 静态示例、运行方式及无写入边界 |
| [历史技术文档](docs/DOCKER-SERVER.md) | 旧版本部署与备份资料；版本号和链接仅供参考 |
| [发行说明](RELEASE-NOTES.md) | v1.0.0 与兼容性说明 |
| [贡献指南](CONTRIBUTING.md) | 提交问题和变更 |

## 许可与致谢

本项目采用 [MIT License](LICENSE)。你可以使用、修改和发布派生作品；分发本软件或其重要部分时须保留原版权声明和许可声明。第三方组件及其许可见 [NOTICE.txt](NOTICE.txt) 和对应许可证文件。
