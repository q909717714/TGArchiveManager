# TGArchiveManager 项目大纲索引

本索引面向后续 AI 维护使用。新建对话或修改代码前，先读 `AGENTS.md` 和 `docs/project-brief.md`，再按任务范围从下表跳转到对应主题文档。

## 阅读方式

- 只需要快速判断入口和边界时，先读 `docs/project-brief.md`。
- 需要完整调用链、文件职责、数据库、测试索引或修改原则时，从本索引进入对应主题文档。
- 主题文档保留原大纲章节编号，便于和旧引用、历史讨论中的章节号对应。

## 拆分目录

| 主题文档 | 覆盖章节 | 适用场景 |
| --- | --- | --- |
| [01-overview-architecture.md](project-outline/01-overview-architecture.md) | 1. 项目定位和技术栈；2. 总体架构 | 判断项目定位、依赖、分层规则和整体依赖方向。 |
| [02-runtime-data.md](project-outline/02-runtime-data.md) | 3. 启动、配置、日志和数据库；4. 数据模型和 Repository | 修改启动流程、配置、日志、SQLite schema、DTO 或 Repository。 |
| [03-ui-workers.md](project-outline/03-ui-workers.md) | 5. UI 入口和页面职责；6. Worker 层 | 修改 PySide6 页面、线程包装、信号槽和页面职责边界。 |
| [04-telegram-service.md](project-outline/04-telegram-service.md) | 7. TelegramService 详细职责 | 修改 Telethon 登录、聊天、Bot、消息读取、建群、发送或媒体下载底层能力。 |
| [05-public-search.md](project-outline/05-public-search.md) | 8. 公开搜索链路 | 修改 Bot 公开搜索、TG 原生搜索、Provider、Parser、Normalizer、人机验证或搜索结果媒体下载。 |
| [06-forwarding-groups.md](project-outline/06-forwarding-groups.md) | 9. 转发和建群链路；10. 聊天同步和目标群 | 修改搜索结果转发、聊天记录转发、自动建群、聊天同步、官方分组或本地标签。 |
| [07-backup-download-export.md](project-outline/07-backup-download-export.md) | 11. 备份、下载、本地搜索和导出 | 修改备份预览、增量备份、媒体下载、Telegraph 图片下载、本地搜索或导出。 |
| [08-operations-testing-ai-guide.md](project-outline/08-operations-testing-ai-guide.md) | 12. 错误码和功能边界；13. 构建、发布和运行数据；14. 测试索引；15. 后续 AI 定位指南 | 修改错误码、发布打包、运行目录、测试覆盖或按需求定位代码入口。 |

## 常用验证

```powershell
python -m compileall . -q
python -m unittest discover -s tests
python main.py --check
python scripts\preflight_check.py --root .
```
