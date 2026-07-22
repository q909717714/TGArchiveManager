# TGArchiveManager 项目规则

本仓库是 Python 3.10+ 的 Windows 桌面应用，技术栈为 PySide6、Telethon、sqlite3、PyInstaller。当前源码不是 Qt C++ 工程，也没有 `.cpp/.h` 文件；后续 AI 修改代码时应按本文件和 `docs/project-brief.md`、`docs/project-outline.md` 理解项目，不要套用 Qt C++ 或 C++17 规则。

## AI 必读顺序

1. 先读 `docs/project-brief.md`，确认项目定位、分层和常用入口。
2. 再读 `docs/project-outline.md` 索引，并按本次任务范围跳转到对应主题文档，先定位 UI、Worker、Service、Repository 或 Parser 的对应逻辑，再修改代码。
3. 涉及用户使用、发布或配置时，同时检查 `README.md`、`docs/configuration.md`、`docs/user-guide.md`、`docs/release-checklist.md`。

## 分层边界

- `main.py` 只负责定位运行根目录、初始化配置/日志/数据库、启动 GUI 或 `--check`。
- `ui/` 只负责 PySide6 页面、用户输入、表格展示、线程启动和信号槽连接，不应直接写复杂业务逻辑。
- `workers/` 是 `QObject` + `QThread` 包装层，只调用服务并发信号给 UI，不直接操作 QWidget。
- `services/` 承担业务编排、Telethon 调用、任务状态、日志、导出、下载和异常转换。
- `database/` 承担 SQLite schema、dataclass DTO 和 Repository，不应依赖 UI 或 PySide6。
- `providers/` 和 `parsers/` 承担公开搜索 Provider、Bot 响应解析、Telegram 链接归一化。
- `utils/error_codes.py` 是错误码集中入口，新增稳定错误码时同步更新说明和相关测试。

## 线程和生命周期

- 耗时操作、Telethon 网络调用、备份、转发、公开搜索必须通过 Worker 在线程中执行。
- Worker 只通过 Qt Signal 把结果传回 UI 线程；不得跨线程直接修改 QWidget。
- 页面中已有 `_thread` / `_worker` 判空防重入逻辑，新增任务时保持同样模式：创建 `QThread`，`worker.moveToThread()`，连接 `started/run`、结果信号、`failed`、`finished`、`deleteLater`。
- `TelegramService` 当前用同步公开方法包装内部异步 Telethon 调用，并通过 `_run_async()` 禁止在已有事件循环中直接调用。

## 数据、配置和安全边界

- 不要读取或提交真实 `config/config.yaml`、`sessions/*.session`、`data/*.db`、`logs/*.log`、`downloads/*`、`exports/*`。这些是本地运行数据，已在 `.gitignore` 中忽略。
- 示例配置只改 `config/config.yaml.example`，真实凭据只由登录页保存到本地 `config/config.yaml`。
- 日志中不得输出 `api_hash`、验证码、密码等敏感值；沿用 `LogService.RedactingFilter`。
- 严格遵守现有合规边界：不实现自动大量加群、成员抓取、破解私密内容或访问账号无权限内容。

## 代码风格

- 保持现有 Python 类型标注、dataclass DTO、Path 路径处理和清晰的异常转换风格。
- UI 文案以中文为主，内部日志和错误码保持稳定、可检索。
- 新增业务能力优先补 Service/Repository/Worker 测试，不把复杂逻辑塞进页面按钮回调。
- SQLite 访问沿用 `database/repositories.py` 的参数化 SQL；不要拼接用户输入到 SQL。
- 新增运行目录、发布文件、配置项时同步更新 README 和 docs。

## 验证命令

常规修改后至少运行相关子集；跨模块修改建议全量运行：

```powershell
python -m compileall . -q
python -m unittest discover -s tests
python main.py --check
python scripts\preflight_check.py --root .
```

发布打包相关修改再运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```
