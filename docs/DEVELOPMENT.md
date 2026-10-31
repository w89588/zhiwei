# 开发指南

## 结构

- `helper/`：Windows 客户端、本机解析、同步与兼容接口。
- `backend/`：FastAPI 服务端、数据库、任务、模型、MCP 和静态管理界面。
- `docs/`：官网、静态演示、使用和开发文档；`docs/demo/` 不依赖后端。
- `tests/`：自动化测试；`tools/`：构建、部署和验证工具；`packaging/`：Windows 安装配置。

## Windows 客户端

使用 Python 3.12，在 PowerShell 运行 `./start.ps1`，脚本会准备开发虚拟环境；浏览器调试可执行：

```powershell
.\.venv-dev\Scripts\python.exe main.py --browser --port 8765
```

需要真实微信解析时必须在 Windows 登录微信，测试请使用独立账号与隔离数据。请勿把个人数据库、截图、密钥或 `.env` 提交到仓库。

## 测试与构建

```powershell
.\.venv-dev\Scripts\python.exe -m pip install -r requirements-dev-lock.txt
.\.venv-dev\Scripts\python.exe -m pytest -q
.\build.ps1
```

安装 Inno Setup 6 后可运行 `./build-release.ps1` 生成安装版、便携版和 SHA-256 校验文件。发布版本读取 `VERSION`。GitHub Actions 的 Windows 发布工作流运行测试并向已存在的草稿 Release 上传构建产物。

## 服务端

参照 [部署指南](QUICKSTART.md) 使用 Compose 构建运行；涉及数据库、任务和模型的集成测试需要独立测试环境。保持应用主密钥及数据库备份配套，不要对真实库运行破坏性测试。

## 官网与演示

`docs/index.html`、`docs/site.css` 为静态官网；`docs/demo/` 使用代码内虚构数据，不请求 API，不接收上传。可运行 `python -m http.server 8000 --directory docs` 本地预览，再打开 `http://127.0.0.1:8000/`。公开部署只能使用独立静态站点，不要反向代理至生产 API。

## 贡献

见 [CONTRIBUTING.md](../CONTRIBUTING.md)。历史版本文档和验证记录只反映当时环境，不代表新版本已完成相同验证。
