# 启动配置、日志、数据库与 Repository

本文件拆分自 `docs/project-outline.md`，保留原大纲章节编号，供后续按主题定位。

## 3. 启动、配置、日志和数据库

### 3.1 `main.py`

- `project_root()`：源码运行时返回 `main.py` 所在目录；打包后返回 `sys.executable` 所在目录。
- `initialize_core(root)`：创建 `ConfigService`，加载配置；创建 `LogService` 并配置日志；创建 `DatabaseManager` 并初始化 schema。
- `run_gui(root, config_service, log_service, database)`：延迟导入 PySide6，创建 `QApplication`，实例化 `MainWindow` 并进入事件循环。
- `main()`：解析 `--check`。`--check` 只执行核心初始化，用于打包或部署预检。

### 3.2 `services/config_service.py`

- `ConfigService.load()`：优先读取 `config/config.yaml`，不存在则读取 `config/config.yaml.example`。读取后创建运行目录：`config`、`sessions`、`data`、`logs`、`logs/tasks`、`downloads`、`exports`。
- `save_telegram_api_credentials()`：校验 `api_id` 为数字、`api_hash` 非空，并写入本机 `config/config.yaml`。
- `save_config()`：保存设置页编辑后的完整配置到本机 `config/config.yaml`，并重新确保运行目录存在。
- `get(dotted_key)`：读取点号路径配置。
- `resolve_path(dotted_key, default)`：把相对路径解析到项目根目录。
- `_restore_example_config_if_available()`：打包环境中如果运行目录缺少示例配置，从 `sys._MEIPASS/config/config.yaml.example` 恢复。
- `search_engines` 配置支持 `telegram_bot` 和 `telegram_native`。`telegram_bot` 使用配置或 UI 输入的 Bot username；`telegram_native` 不使用 Bot，由独立“TG 频道搜索”页使用，可在当前账号已加入且可访问的频道/群聊中按 UI 多选范围搜索。

### 3.3 `services/log_service.py`

- `LogService.configure()`：创建 `app.log`、`error.log`，关闭旧 handler，设置 UTF-8 文件日志。
- `RedactingFilter`：日志中出现 `api_hash`、`password`、`verification_code`、`login_code` 等敏感键时整条脱敏。
- `get_logger(module_name)`：返回 `tg_archive_manager.<module>` 子 logger。
- `get_file_logger(module_name, filename)`：给指定模块增加独立文件日志，如 `public_search.log`、`forward.log`、`download.log`。
- `task_log_path()` / `get_task_logger()`：生成 `logs/tasks/<type>_<task_id>.log`。
- `read_entries(LogQuery)`：解析多行日志，支持文件、模块、等级、task_id、关键词过滤。
- `export_entries()`：导出当前筛选日志。
- `latest_error_detail()`：生成可复制的最新错误详情，并用 `utils/error_codes.py` 解释错误码。

### 3.4 `services/service_factory.py`

- `ApplicationContext` 保存页面构造服务所需的只读根依赖：`project_root`、`ConfigService`、`LogService` 和 `DatabaseManager`。
- `ServiceFactory` 统一构造 `TelegramService`、`DownloadService`、`ForwardService`、`BackupService`、`GroupService` 和 `PublicSearchService`。
- Bot 公开搜索、TG 频道搜索、搜索结果媒体下载、转发管理和备份下载页面通过该工厂装配服务；页面仍保留 UI 状态、Repository 查询和 Worker/QThread 生命周期管理。
- 工厂读取 `download`、`forward.max_per_task`、`public_search.duplicate_check` 等配置，并统一传入任务日志工厂、下载策略、Telegraph 仓库和模块日志。

### 3.5 `database/db.py`

`DatabaseManager.initialize()` 执行 `SCHEMA_STATEMENTS`，创建或确保以下表存在：

- `accounts`：Telegram 账号元数据。
- `chats`：本地聊天列表、本地标签、Telegram 官方分组、最后同步/备份消息。
- `public_search_tasks`：公开搜索任务。
- `public_search_results`：公开搜索归一化结果。
- `telegraph_pages` / `telegraph_page_images` / `telegraph_page_links`：保存 `telegra.ph` 页面卡片元数据、正文图片和正文 Telegram 跳转链接。
- `messages`：本地备份消息，包含正文/预览、Telegram 原文链接和网页预览、text entity、按钮等外部链接汇总字段 `external_urls`。
- `files`：消息媒体文件元数据。
- `links`：预留链接表。
- `groups`：工具创建或登记的目标群。
- `tasks`：通用任务摘要，供转发/备份等使用。
- `forward_records`：每条转发结果。
- `download_records`：每次下载尝试和结果。
- `schema_version`：schema 版本。

`connect()` 返回 `ClosingConnection`，上下文退出时自动关闭连接，并设置 `sqlite3.Row` 方便按列名取值。

## 4. 数据模型和 Repository

### 4.1 `database/models.py`

全部是 dataclass DTO：

- `Account`：账号、用户名、session 路径和登录时间。
- `Chat`：Telegram chat id、标题、类型、本地标签、Telegram 官方分组、最后消息和最后备份消息。
- `SearchResult`：公开搜索结果、归一化链接、Telegram 元数据、原消息 chat/message id、去重、访问标记和转发状态。
- `PublicSearchTask`：公开搜索任务摘要。
- `TaskSummary`：通用任务摘要。
- `ForwardRecord`：单条转发记录。
- `MessageRecord`：本地备份消息，包含正文、预览、媒体元数据、原文链接和外部链接汇总 `external_urls`。
- `FileRecord`：消息媒体文件元数据。
- `DownloadRecord`：下载任务记录。

### 4.2 `database/repositories.py`

- `AccountRepository`：`upsert_account()`、`get_by_phone()`、`latest_account()`。
- `ChatRepository`：`upsert_chat()`、`upsert_many()`、`list_chats()`、`get_by_tg_chat_id()`、`update_tag()`、`update_last_backup_message_id()`、`delete_chats_by_tg_chat_ids()`；`list_chats()` 支持按名称、username、本地标签或 Telegram 官方分组过滤。
- `PublicSearchRepository`：创建/完成公开搜索任务、保存结果、按任务/过滤条件读取结果、读取类型列表、按 id 保持顺序取结果、更新 `forward_status`、删除搜索结果、删除搜索任务及其结果。
- `GroupRepository`：登记工具目标群，`list_groups()` 返回 `Chat` 视图，并可删除本地群登记。
- `TaskRepository`：创建通用任务、更新进度、读取最近任务，并可删除任务摘要及对应转发/下载明细。
- `ForwardRepository`：写入、查询和删除 `forward_records`。
- `MessageRepository`：upsert 消息、标记下载、列出消息、统计单个聊天本地消息数、本地搜索、读取消息类型、删除消息及直接关联元数据；关键词搜索覆盖正文、预览、发送者、文件名、原文链接和 `external_urls`。
- `FileRepository`：upsert 文件、更新下载状态、按 id 或消息身份查询，并可删除文件元数据和对应下载记录。
- `DownloadRecordRepository`：写入、查询和删除下载记录。

Repository 统一使用参数化 SQL。涉及用户输入的过滤条件通过参数绑定传入，不应拼接原始输入。

