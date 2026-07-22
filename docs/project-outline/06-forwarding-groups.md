# 转发、建群和聊天同步链路

本文件拆分自 `docs/project-outline.md`，保留原大纲章节编号，供后续按主题定位。

## 9. 转发和建群链路

入口页面：`ui/forward_page.py`、`ui/backup_page.py`、`ui/group_page.py`。

搜索结果普通转发：

1. `ForwardPage` 读取 `PublicSearchRepository.latest_tasks()` 和 `list_filtered_results()`，展示结果。
2. 用户勾选结果并预览，预览调用 `ForwardService.preview_search_result_cards()`。
3. `ForwardWorker` 调 `ForwardService.forward_search_result_cards()`。
4. `ForwardService` 先按 `forward.max_per_task` 校验单任务条数，再创建通用 `tasks` 记录，跳过重复或已成功转发的结果。
5. 发送项转换为 `TelegramOutgoingMessage`，调用 `TelegramService.send_text_messages()`。
6. 每条发送结果通过 `_record_result()` 写 `forward_records`，更新 `public_search_results.forward_status`，更新 `tasks.progress`。
7. 最终任务状态为 `completed`、`completed_with_errors` 或用户取消时的 `cancelled`。

自动建群转发：

1. `ForwardPage` 会按 `forward.create_group_before_forward` 设置默认目标策略，并从 `forward.default_group_name_rule` 提取自动建群默认前缀。
2. `ForwardService.forward_search_result_cards_auto_group()` 按 `result_type` 或 `created_at` 日期分桶。
3. 每个分桶调用 `GroupService.create_target_group()`。
4. `GroupService` 调 `TelegramService.create_group()`，然后写 `chats` 和 `groups`。
5. 向新建群发送对应分桶卡片。

聊天记录转发：

1. `BackupPage` 从当前消息表格勾选行重新读取本地 `MessageRecord`。
2. 用户先通过 `ForwardService.preview_message_records()` 预览实际发送文本。
3. `ForwardWorker` 根据 `source_type=message_record` 调 `ForwardService.forward_message_records()`，或调 `forward_message_records_auto_group()` 先建一个新群。
4. `ForwardService` 按 `forward.max_per_task` 校验单任务条数，创建通用 `tasks` 记录，把聊天记录转换为 `TelegramOutgoingMessage`，调用 `TelegramService.send_text_messages()`。
5. 每条发送结果通过 `_record_message_result()` 写 `forward_records`，成功时更新 `messages.is_forwarded`，并更新 `tasks.progress`。

`ForwardWorker` 持有取消令牌；`ForwardService` 在分桶、建群前后、消息组装和发送回调之间检查取消。取消后已写入的 `forward_records` 和成功状态保留，通用 `tasks` 行标记为 `cancelled`。

文本格式：

- `ForwardService.format_card()` 输出纯文本，包含关键词、来源、类型、标题、媒体信息、摘要、链接；当结果带原消息定位时同时显示 chat_id/message_id。
- `ForwardService.format_message_record()` 输出纯文本，包含来源 chat_id/message_id、时间、发送者、消息类型、内容、媒体文件名/大小、下载状态和原始链接；图片/视频输出 `tgarchive://download?chat_id=...&message_id=...` 内部下载链接，供后续备份目标群文本后继续用“下载勾选媒体”定位原始媒体。
- 输出限制在 3500 字符以内，避免 Telegram 文本过长。

## 10. 聊天同步和目标群

`ui/chat_page.py`：

- `ChatPage._sync_chats()` 通过 `ChatSyncWorker` 调 `TelegramService.sync_chats()`。
- `TelegramService._chat_from_dialog()` 将 Telethon dialog 转成 `Chat`。
- `TelegramService._get_dialog_filters()` 读取 Telegram 官方聊天文件夹/分组，并用 `_dialog_filter_names_for_dialog()` 映射到本地聊天；无法读取分组时仍保留聊天同步能力。
- `ChatRepository.upsert_many()` 写入或更新本地 `chats`，官方分组写入 `telegram_folder_names`，本地标签不被同步覆盖。
- 本地标签通过 `ChatRepository.update_tag()` 即时保存。
- `ChatPage` 显示“官方分组”只读列，按官方分组排序和过滤；官方分组与本地标签分开维护。
- `target_chat_changed` 信号当前只在页面内部提供，`ForwardPage` 自己从 `ChatRepository` 读取目标聊天。

`ui/group_page.py`：

- 创建群时通过 `GroupCreateWorker -> GroupService -> TelegramService.create_group`。
- 成功后写入 `chats` 和 `groups`，再刷新本地自建群列表。

