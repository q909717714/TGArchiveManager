# 错误边界、构建发布、测试索引和 AI 定位指南

本文件拆分自 `docs/project-outline.md`，保留原大纲章节编号，供后续按主题定位。

## 12. 错误码和功能边界

文件：`utils/error_codes.py`。

主要错误码前缀：

- `SE`：公开搜索和 Provider。
- `PR`：解析。
- `TG`：Telegram 登录、权限、限流和服务操作。
- `FW`：转发。
- `GP`：建群。
- `DB`：数据库。
- `DL`：下载。
- `BK`：备份。
- `EX`：导出。
- `OP`：用户主动取消等跨模块操作状态。

功能边界写在 README 和发布清单中，代码也体现为：

- 只处理账号可访问内容。
- 公开搜索结果默认转为文本卡片转发，不强制转发原始消息。
- 媒体下载依赖 Telegram 返回结果，不访问账号无权限内容。
- 长任务取消使用 `OP001`，通过 Worker/Service 协作检查停止，不强制终止线程。

## 13. 构建、发布和运行数据

### 13.1 配置和运行目录

`config/config.yaml.example` 是示例配置。真实 `config/config.yaml` 可由登录页保存 Telegram API 凭据，也可由设置页保存搜索、转发、备份下载、导出和日志配置，属于本机数据。

运行时可写目录：

- `config`
- `sessions`
- `logs`
- `logs/tasks`
- `downloads`
- `exports`
- `data`

`.gitignore` 已忽略真实配置、数据库、日志、session、下载和导出产物，只保留 `.gitkeep`。

公开仓库元数据：

- `LICENSE`：MIT License。
- `CHANGELOG.md`：公开版本变化，首版为 `v0.1.0`。
- `CONTRIBUTING.md`、`SECURITY.md`：贡献流程、隐私边界和漏洞报告方式。
- `.github/workflows/ci.yml`：在 Windows/Python 3.10 环境运行 compileall、unittest、`main.py --check` 和 preflight。
- 公开提交前排除 `.vscode`、`.agents`、真实运行数据及 `build/dist`，并检查候选文件的加密属性和凭据模式。

### 13.2 打包

- `TGArchiveManager.spec`：PyInstaller 入口为 `main.py`，datas 包含 `config/config.yaml.example`。
- `scripts/build_windows.ps1`：依次执行 compileall、unittest、源码 preflight、PyInstaller、运行目录创建、拷贝示例配置、dist preflight、exe `--check`。
- `scripts/preflight_check.py`：加载配置、日志、数据库，检查运行目录可写，打印 `preflight-ok`。

## 14. 测试索引

主要测试文件：`tests/test_stage1_services.py`、`tests/test_searchable_combo_box.py`、`tests/test_telegram_native_search_page.py`。

覆盖范围：

- 配置加载、通用配置保存、运行目录创建、凭据保存、打包环境示例配置恢复。
- 日志初始化、敏感信息脱敏、日志过滤/导出/错误详情、任务日志、日志保留和模块独立日志开关。
- 账号、聊天、公开搜索、转发、群组、消息、文件、下载 Repository/Service。
- TelegramService 输入校验、dialog 转 Chat、频道/群聊 marked id entity fallback、Telegram 官方分组映射、Bot 响应按钮和 text entity 提取、按钮坐标查找。
- BotResultParser、TelegramLinkParser、ResultNormalizer。
- PublicSearchService 搜索、过滤、验证上下文恢复。
- PublicSearchService 取消时搜索任务状态标记为 `cancelled`。
- ForwardService 卡片格式、聊天记录格式、记录保存、已转发标记、单任务条数限制、自动建群分桶、聊天记录自动建群转发和取消后的通用任务状态。
- LocalSearchService 和 ExportService。
- BackupService 保存消息、增量缓存不足时回补最近 N 条、下载媒体；DownloadService 下载勾选本地消息媒体，遵守下载类型/大小/跳过已存在文件配置，支持协作取消，并可识别聊天记录转发文本中的 `tgarchive://download` 内部下载链接和本地消息中的 Telegraph 页面链接；BackupPage 消息类型、文件/媒体大小、Telegraph 页面图片数和下载进度展示格式。
- JisouProvider 人机验证、提交验证、分页拉取。
- TelegramNativeSearchProvider 结果映射、媒体大小摘要、指定频道/群聊范围传递和搜索结果媒体下载成功。
- TelegramNativeSearchPage 搜索范围官方分组树、分组折叠和分组批量勾选。
- SearchableComboBox 下拉候选过滤、提交首个匹配项、无匹配时恢复原选择。

常用命令：

```powershell
python -m compileall . -q
python -m unittest discover -s tests
python main.py --check
python scripts\preflight_check.py --root .
```

## 15. 后续 AI 定位指南

按需求类型定位：

- 改登录、验证码、二步验证、session：看 `ui/login_page.py`、`workers/telegram_worker.py`、`services/telegram_service.py`、`database.repositories.AccountRepository`。
- 改 Bot 公开搜索、验证码、人机验证、分页：看 `ui/public_search_page.py`、`workers/search_worker.py`、`services/public_search_service.py`、`providers/jisou_provider.py`、`services/telegram_service.py`、`parsers/*`。
- 改 TG 频道搜索、官方分组范围树、指定频道/群聊范围、原生搜索结果媒体下载或 Telegraph 页面卡片：看 `ui/telegram_native_search_page.py`、`workers/search_worker.py`、`services/public_search_service.py`、`services/telegraph_service.py`、`providers/telegram_native_provider.py`、`services/telegram_service.py`、`services/download_service.py`。
- 改搜索结果筛选或去重：看 `PublicSearchRepository.list_filtered_results()`、`save_results()`、`ResultNormalizer.normalize()`。
- 改搜索结果转发卡片或自动建群：看 `ui/forward_page.py`、`workers/forward_worker.py`、`services/forward_service.py`、`services/group_service.py`。
- 改已备份聊天记录转发：看 `ui/backup_page.py`、`workers/forward_worker.py`、`services/forward_service.py`、`services/group_service.py`、`MessageRepository.mark_forwarded()`。
- 改聊天同步、Telegram 官方分组和本地标签：看 `ui/chat_page.py`、`TelegramService.sync_chats()`、`TelegramService._get_dialog_filters()`、`ChatRepository`。
- 改备份预览、增量游标、本地消息外部链接、勾选消息下载或搜索结果媒体下载：看 `ui/backup_page.py`、`ui/public_search_page.py`、`ui/telegram_native_search_page.py`、`workers/backup_worker.py`、`workers/search_worker.py`、`services/backup_service.py`、`services/download_service.py`、`TelegramService.fetch_chat_messages()`、`TelegramService._entity_from_chat_id()`、`TelegramService.search_joined_messages()`、`download_archived_message_media()`。
- 改本地搜索或导出：看 `ui/local_search_page.py`、`ui/export_page.py`、`services/local_search_service.py`、`services/export_service.py`、`MessageRepository.search_messages()`。
- 改日志页或错误详情：看 `ui/log_page.py`、`services/log_service.py`、`utils/error_codes.py`。
- 改设置页或配置持久化：看 `ui/settings_page.py`、`services/config_service.py` 和 `config/config.yaml.example`。
- 改页面中的服务构造、Repository/Logger/配置装配：先看 `services/service_factory.py`，再看调用它的搜索、转发、备份和搜索结果页面。
- 改通用下拉选择和模糊过滤：看 `ui/searchable_combo_box.py`，再检查使用该控件的公开搜索、转发、备份、本地搜索、导出和日志页面。
- 改数据库字段：先改 `database/db.py` schema，再改 `models.py`、`repositories.py`、相关 Service/UI/导出列和测试。
- 改打包：看 `TGArchiveManager.spec`、`scripts/build_windows.ps1`、`scripts/preflight_check.py`、`docs/release-checklist.md`。

修改原则：

- 先沿调用链找到最靠近业务规则的层再改，不要在 UI 中复制 Service 逻辑。
- 新增页面后台任务时优先通过 `ServiceFactory` 复用现有服务装配；只有页面特有的 UI 状态和线程生命周期留在页面内。
- 新增耗时操作时必须走 Worker/QThread。
- 新增持久化字段时同步 schema、dataclass、Repository、相关表格/导出、测试。
- 新增 Provider 时实现 `BaseSearchProvider`，返回已归一化或可归一化的 `SearchResult`，并在 `search_engines` 配置和 `PublicSearchPage` 装配。
- 新增配置项时同步 `config/config.yaml.example`、`docs/configuration.md`、相关默认值读取和测试。

