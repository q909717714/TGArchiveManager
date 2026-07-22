# UI 入口、页面职责与 Worker 层

本文件拆分自 `docs/project-outline.md`，保留原大纲章节编号，供后续按主题定位。

## 5. UI 入口和页面职责

### 5.1 `ui/main_window.py`

`MainWindow` 负责：

- 创建 `RuntimeState` 保存当前进程内 API ID、API Hash、手机号和账号名。
- 创建左侧 `QListWidget` 导航和右侧 `QStackedWidget`。
- 装配页面顺序：账号登录、Bot 公开搜索、TG 频道搜索、搜索结果、转发管理、自建群管理、聊天列表、备份下载、本地搜索、导出、日志、设置。
- 连接 `LoginPage.login_status_changed` 和 `account_changed` 到状态栏。

Bot 公开搜索、TG 频道搜索、搜索结果、转发管理和备份下载页面持有 `ServiceFactory(ApplicationContext)`，用于集中创建后台 Worker 需要的 Service；页面仍直接维护表格状态、筛选条件、Repository 读取和 QThread 生命周期。

### 5.2 页面文件职责

| 文件 | 主要类 | 作用 |
| --- | --- | --- |
| `ui/login_page.py` | `LoginPage` | Telegram API 配置、验证码、二步验证、session 恢复、退出登录。创建 `TelegramLoginWorker`。 |
| `ui/public_search_page.py` | `PublicSearchPage` | Bot 公开搜索条件、配置/已同步/自定义 Bot 选项、结果表格、卡片预览、搜索结果媒体下载、删除选中搜索结果、人机验证面板、验证媒体预览。创建 `PublicSearchWorker`、`VerificationClickWorker` 和 `SearchResultDownloadWorker`。 |
| `ui/telegram_native_search_page.py` | `TelegramNativeSearchPage` | 独立 TG 频道搜索页面；从本地已同步频道/群聊中按 Telegram 官方分组展示可折叠搜索范围树，支持分组一键全选/取消，按关键词执行 TG 原生消息搜索，结果表格显示媒体大小或 Telegraph 图片数量，卡片预览也包含媒体信息，并可下载或删除带原消息定位的结果记录。创建 `PublicSearchWorker` 和 `SearchResultDownloadWorker`。 |
| `ui/search_result_page.py` | `SearchResultPage` | 统一管理已保存搜索结果；支持按任务、关键词、类型和时间筛选，勾选预览卡片、下载选中媒体、删除选中结果或删除当前搜索任务。创建 `SearchResultDownloadWorker`。 |
| `ui/forward_page.py` | `ForwardPage` | 搜索结果筛选、全选/预览、删除选中结果、已有目标转发、按类型/日期自动建群转发、进度显示和运行中取消。创建 `ForwardWorker`。 |
| `ui/group_page.py` | `GroupPage` | 新建目标群、分类、列出并删除本地登记的工具群。创建 `GroupCreateWorker`。 |
| `ui/chat_page.py` | `ChatPage` | 同步聊天、展示 Telegram 官方分组、按名称/username/标签/官方分组过滤、编辑本地标签、选择转发目标、删除本地聊天记录。创建 `ChatSyncWorker`。 |
| `ui/backup_page.py` | `BackupPage` | 选择聊天、条数、日期范围和增量参数，先预览/备份消息元数据；列表显示类型、大小/图片数、下载状态和转发状态，支持勾选消息后下载媒体、转发为纯文本聊天记录，运行中取消任务，或删除本地消息记录。创建 `BackupWorker`、`MessageMediaDownloadWorker` 和 `ForwardWorker`。 |
| `ui/local_search_page.py` | `LocalSearchPage` | 搜索本地消息，显示可勾选结果，并可导出或删除当前勾选结果。直接使用 `LocalSearchService` 和 `ExportService`。 |
| `ui/export_page.py` | `ExportPage` | 按过滤条件导出本地消息，支持 CSV、Excel、JSON、HTML。 |
| `ui/log_page.py` | `LogPage` | 日志文件、模块、等级、task_id、关键词过滤；导出日志；复制最新错误详情；定时刷新。 |
| `ui/telegram_credentials.py` | `telegram_credentials_or_warn()` | 从 `RuntimeState` 读取 API 凭据，缺失时弹窗提示先登录页配置。 |
| `ui/searchable_combo_box.py` | `SearchableComboBox` | 通用可搜索下拉框；用于公开搜索、转发、备份、本地搜索、导出和日志页的下拉选择。 |
| `ui/placeholder_page.py` | `PlaceholderPage` | 简单占位页基类。 |
| `ui/settings_page.py` | `SettingsPage` | 编辑 Telegram、搜索、转发、备份下载、导出和日志配置；通过 `ConfigService.save_config()` 写入本机 `config/config.yaml`。 |

### 5.3 通用下拉框行为

- `SearchableComboBox` 继承 `QComboBox`，内部使用未过滤源模型和过滤代理模型维护完整选项集合。
- 用户直接展开下拉框时显示全部选项；在输入框中输入关键字时，候选列表按显示文本和 `Qt.UserRole` 数据进行不区分大小写的模糊过滤。
- 选择候选或回车提交首个匹配项后恢复完整选项集合，页面原有 `currentData()`、`findData()`、`addItem()` 和 `addItems()` 用法保持不变。
- 无匹配结果时不写入临时文本，不破坏提交前的已选项。

## 6. Worker 层

Worker 都继承 `QObject`，通过 `@Slot()` 的 `run()` 被 `QThread.started` 调用。

| 文件 | Worker | 调用服务 | 关键信号 |
| --- | --- | --- | --- |
| `workers/telegram_worker.py` | `TelegramLoginWorker` | `TelegramService` | `code_sent`、`account_ready`、`password_required`、`logout_completed`、`failed`、`finished` |
| `workers/chat_worker.py` | `ChatSyncWorker` | `TelegramService.sync_chats` | `chats_synced`、`failed`、`finished` |
| `workers/search_worker.py` | `PublicSearchWorker` | `PublicSearchService.search` | `search_completed`、`verification_required`、`cancelled`、`failed`、`finished` |
| `workers/search_worker.py` | `VerificationClickWorker` | `PublicSearchService.submit_verification` | `verification_completed`、`verification_required`、`cancelled`、`failed`、`finished` |
| `workers/search_worker.py` | `SearchResultDownloadWorker` | `DownloadService.download_search_results_media` | `progress_changed`、`download_completed`、`cancelled`、`failed`、`finished` |
| `workers/forward_worker.py` | `ForwardWorker` | `ForwardService` | `progress_changed`、`forward_completed`、`cancelled`、`failed`、`finished` |
| `workers/forward_worker.py` | `GroupCreateWorker` | `GroupService.create_target_group` | `group_created`、`failed`、`finished` |
| `workers/backup_worker.py` | `BackupWorker` | `BackupService.backup_chat` | `progress_changed`、`backup_completed`、`cancelled`、`failed`、`finished` |
| `workers/backup_worker.py` | `MessageMediaDownloadWorker` | `DownloadService.download_message_records_media` | `progress_changed`、`download_completed`、`cancelled`、`failed`、`finished` |

`ForwardWorker` 通过 payload 区分搜索结果卡片和本地聊天记录：搜索结果来自 `ForwardPage`，聊天记录来自 `BackupPage`。

`PublicSearchWorker`、`VerificationClickWorker`、`SearchResultDownloadWorker`、`ForwardWorker`、`BackupWorker` 和 `MessageMediaDownloadWorker` 持有 `CancellationToken`，页面点击取消时调用 Worker 的 `cancel()`。Service、Provider、TelegramService 和下载循环在安全检查点抛 `OperationCancelled(OP001)`，Worker 发 `cancelled(error_code, message)`，UI 只更新状态，不弹失败框。

错误传播方式：业务服务抛出带 `error_code` 的异常，Worker 捕获后发 `failed(error_code, message)`；未知异常记录 logger.exception 后使用兜底错误码。

