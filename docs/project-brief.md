# TGArchiveManager 项目简明大纲

本文件是后续新对话给 AI 的默认入口。修改代码前先读本文件，再按任务范围从 `docs/project-outline.md` 索引跳转到对应主题文档。

## 项目定位

TGArchiveManager 是本地 Windows 桌面工具，用于管理 Telegram 归档相关工作流。源码使用 Python 3.10+、PySide6、Telethon、sqlite3、openpyxl/pandas/Jinja2 和 PyInstaller。当前仓库没有 C++、`.cpp`、`.h` 或 Qt 5.9.9 工程文件。

当前 MVP 覆盖：

- Telegram 登录、验证码登录、二步验证、session 恢复和退出。
- 同步账号可访问的聊天列表，读取 Telegram 官方聊天文件夹/分组，维护本地标签。
- 通过“Bot 公开搜索”调用多个 Telegram Bot Provider，或通过独立“TG 频道搜索”在指定已加入频道/群聊内执行 TG 原生搜索；TG 频道搜索范围按官方分组显示为可折叠树，支持分组一键全选/取消。
- Bot 人机验证由用户在软件内手动选择按钮后继续搜索。
- Bot 搜索和 TG 频道搜索结果均可在对应搜索页和统一“搜索结果”页预览为转发卡片；TG 原生搜索结果可显示媒体大小或 Telegraph 图片数量，并下载账号可访问消息中的媒体。
- 软件界面中的本地记录支持删除：聊天列表、自建群登记、搜索结果、搜索任务、备份消息和本地搜索结果删除的都是 SQLite 本地记录及直接关联元数据，不会删除 Telegram 远端内容或已下载到磁盘的媒体文件。
- 公开搜索结果、TG 原生消息、Bot 按钮或链接预览中的 `telegra.ph` 链接会被识别为 `telegraph_page`，解析页面标题、发布时间、作者/来源、图片数量、正文图片和 Telegram 跳转链接；下载时按页面标题创建目录，并支持限制图片下载数量。
- 将搜索结果格式化为纯文本卡片，转发到已有目标聊天或自动建群后转发。
- 按指定条数预览并备份聊天消息到 SQLite，再从本地列表勾选下载账号可访问消息中的媒体，或转发为纯文本聊天记录；增量备份在本地缓存不足本次请求条数时会回补最近 N 条，避免只显示游标后的少量新消息；本地消息列表按图片/视频/文件等媒体显示大小，Telegraph 页面显示图片数量；图片/视频转发文本包含 `tgarchive://download` 内部下载链接，后续备份目标群文本后仍可一键下载原媒体。
- 本地搜索、CSV/Excel/JSON/HTML 导出。
- 应用日志、任务日志过滤、导出和错误详情复制。
- 设置页可编辑 Telegram、搜索、转发、备份下载、导出和日志配置，并写入本机 `config/config.yaml`。
- Bot 公开搜索、TG 频道搜索、媒体下载、转发和备份下载等长任务通过 Worker 持有的取消令牌协作取消；取消任务使用 `OP001`，已完成记录保留，未处理条目不标记为失败。
- 主要下拉选择框支持输入关键字模糊过滤；未输入过滤内容时仍显示全部选项。

## 必读和定位顺序

1. `AGENTS.md`：项目规则、分层边界、线程和安全约束。
2. `docs/project-brief.md`：快速判断入口和模块。
3. `docs/project-outline.md`：拆分后的项目大纲索引，按任务范围跳转到完整调用链、文件职责、数据库和测试索引主题文档。
4. 需要用户或发布语义时再读 `README.md`、`docs/configuration.md`、`docs/user-guide.md`、`docs/release-checklist.md`。

## 运行入口

- `main.py::project_root()`：源码运行返回仓库根目录；PyInstaller 打包后返回 exe 所在目录。
- `main.py::initialize_core()`：加载配置、配置日志、初始化 SQLite。
- `main.py::run_gui()`：创建 `QApplication` 和 `ui.main_window.MainWindow`。
- `main.py --check`：只初始化配置、日志和数据库，不启动 GUI。

## 分层速查

- `ui/`：PySide6 页面。页面收集输入、创建 Worker、连接信号、刷新表格；`ui/searchable_combo_box.py` 提供通用可搜索下拉框。
- `workers/`：`QObject` 后台执行包装。调用 Service，发出进度、完成、失败信号。
- `services/`：业务编排。包含 Telegram、公开搜索、转发、建群、备份、下载、本地搜索、导出、日志、配置；`services/service_factory.py` 统一装配页面常用 Service、Repository、Logger 和配置。
- `database/`：sqlite3 schema、dataclass DTO、Repository。
- `providers/`：公开搜索 Provider 抽象、通用 Telegram Bot/Jisou-like 实现和 TG 原生搜索实现。
- `parsers/`：Bot 响应解析、Telegram 链接解析、搜索结果归一化。
- `scripts/`：预检和 Windows PyInstaller 打包；`.github/workflows/ci.yml` 在公开仓库的 Windows/Python 3.10 环境运行核心检查。
- `tests/test_stage1_services.py`：当前主要服务和页面静态逻辑回归测试。
- `tests/test_searchable_combo_box.py`：通用可搜索下拉框的候选过滤和选择恢复测试。
- `tests/test_telegram_native_search_page.py`：TG 频道搜索范围官方分组树、折叠和分组批量勾选测试。

## 核心调用链

- 登录：`LoginPage -> TelegramLoginWorker -> TelegramService -> AccountRepository`。
- 聊天同步：`ChatPage -> ChatSyncWorker -> TelegramService.sync_chats -> ChatRepository`。
- Bot 公开搜索：`PublicSearchPage -> PublicSearchWorker -> PublicSearchService -> JisouProvider -> TelegramService.query_search_bot -> BotResultParser -> ResultNormalizer -> PublicSearchRepository`。
- TG 频道搜索：`TelegramNativeSearchPage -> PublicSearchWorker -> PublicSearchService -> TelegramNativeSearchProvider -> TelegramService.search_joined_messages(target_chat_ids) -> PublicSearchRepository`。
- 人机验证：`PublicSearchPage -> VerificationClickWorker -> PublicSearchService.submit_verification -> JisouProvider.submit_verification -> TelegramService.click_bot_button_and_collect_responses`。
- 搜索结果管理：`SearchResultPage/PublicSearchPage/TelegramNativeSearchPage/ForwardPage -> PublicSearchRepository`，支持筛选、预览、下载媒体、删除搜索结果和删除搜索任务。
- 搜索结果媒体下载：`SearchResultPage/PublicSearchPage/TelegramNativeSearchPage -> SearchResultDownloadWorker -> DownloadService.download_search_results_media -> TelegramService.download_archived_message_media`；下载进度显示当前条目、累计 MB 和图片张数。
- 卡片转发：`ForwardPage -> ForwardWorker -> ForwardService -> TelegramService.send_text_messages -> ForwardRepository/TaskRepository/PublicSearchRepository`。
- 自动建群转发：`ForwardService.forward_search_result_cards_auto_group -> GroupService -> TelegramService.create_group -> ChatRepository/GroupRepository`。
- 备份预览：`BackupPage -> BackupWorker -> BackupService -> TelegramService.fetch_chat_messages -> MessageRepository/FileRepository`。
- 勾选消息媒体下载：`BackupPage -> MessageMediaDownloadWorker -> DownloadService.download_message_records_media -> TelegramService.download_archived_message_media`；也支持从转发文本中的 `tgarchive://download` 内部链接定位原媒体，并显示当前条目、累计 MB 和图片张数。
- 勾选聊天记录转发：`BackupPage -> ForwardWorker -> ForwardService.forward_message_records/forward_message_records_auto_group -> TelegramService.send_text_messages -> ForwardRepository/TaskRepository/MessageRepository`。
- 本地搜索导出：`LocalSearchPage/ExportPage -> LocalSearchService/ExportService -> MessageRepository`。
- 日志诊断：`LogPage -> LogService.read_entries/export_entries/latest_error_detail`。

## 重要边界

- 不提交真实 `config/config.yaml`、session、数据库、日志、下载、导出、编辑器/AI 本地元数据或 `build/dist` 构建产物；公开发布前先检查加密属性和凭据模式。
- 不实现自动大量加群、成员抓取、破解私密内容或访问账号无权限内容。
- UI 线程不得执行耗时 Telethon 操作；后台 Worker 不得直接操作 QWidget。
- 公开搜索 Provider 只负责搜索和解析，结果持久化由 `PublicSearchService` 和 Repository 处理。

## 常用验证

```powershell
python -m compileall . -q
python -m unittest discover -s tests
python main.py --check
python scripts\preflight_check.py --root .
```
