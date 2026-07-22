# TelegramService 详细职责

本文件拆分自 `docs/project-outline.md`，保留原大纲章节编号，供后续按主题定位。

## 7. TelegramService 详细职责

文件：`services/telegram_service.py`。

公开数据结构：

- `TelegramAccountInfo`：UI 可展示账号信息。
- `CodeRequestResult`：验证码发送结果。
- `TelegramBotResponse`：Bot 响应文本、按钮 URL、按钮文本、text entity 链接、验证媒体路径。
- `TelegramTextLink`：文本实体链接。
- `TelegramOutgoingMessage` / `TelegramSendResult`：批量发送纯文本消息（搜索结果卡片或聊天记录）的输入和结果。
- `TelegramArchivedMessage`：备份和 TG 原生搜索用的消息元数据，包含聊天标题、媒体信息、source link、网页预览/text entity/按钮中的外部链接。
- `TelegramMediaDownloadResult`：媒体下载结果，包含本地路径、文件名、文件大小，以及 Telegraph 图片下载数量统计。

公开方法：

- `send_code()`：校验 api_id/api_hash/phone，调用 `_send_code_async()`，保存 pending 登录上下文。
- `sign_in_with_code()`：使用 pending phone/hash 和验证码登录；二步验证时抛 `TelegramPasswordRequired`。
- `sign_in_with_password()`：提交二步验证密码。
- `restore_session()`：使用已有 session 恢复账号。
- `logout()`：退出当前 session。
- `sync_chats()`：读取 dialogs 和 Telegram 官方聊天文件夹/分组，转换为 `Chat`，可写入 `ChatRepository`。
- `query_search_bot()`：向 Bot 发送关键词，收集响应；无响应时抛 `SE001`。
- `click_bot_button()` / `click_bot_button_and_collect_responses()`：点击 Bot 消息按钮并收集后续响应。
- `create_group()`：创建 Telegram supergroup，返回 `Chat`。
- `send_text_messages()`：向目标聊天发送一批纯文本消息，支持间隔和进度回调。
- `fetch_chat_messages()`：读取可访问聊天消息，支持 limit、最小 message id、日期过滤，不下载媒体。
- `search_joined_messages()`：使用 Telegram 原生搜索遍历当前账号已加入的频道/群聊；可按 `target_chat_ids` 限定到指定频道/群聊，返回可定位的 `TelegramArchivedMessage`。
- `download_archived_message_media()`：下载单条消息媒体，无媒体会 skipped，Telegram 下载异常会按失败结果记录；可接收 Telethon 字节进度回调用于 UI 显示已下载 MB。

关键私有逻辑：

- `_telethon_modules()`：延迟导入 Telethon，并把 `TelegramClient`、`functions`、`utils`、`FloodWaitError`、`SessionPasswordNeededError` 打包返回。
- `_run_async()`：当前线程无事件循环时 `asyncio.run(coroutine)`；如果已有事件循环则关闭 coroutine 并抛错，避免嵌套事件循环。
- `_collect_bot_responses()`：从 Bot 最近消息中收集具有链接候选或验证信息的响应。
- `_bot_response_from_message()`：提取 `raw_text`、按钮 URL、按钮文本、text entity 链接。
- `_download_message_media()`：把 Bot 验证附件下载到 `data/verification_media`。
- `_click_message_button()`：优先按按钮文本点击；失败后根据按钮坐标重试。
- `_entity_from_chat_id()`：按本地保存的 Telegram chat id 解析 Telethon entity；直接 `get_entity()` 失败时用 Telethon `utils.resolve_id()` 转成 Peer 后重试，覆盖频道/群聊 marked id。
- `_archived_message_from_message()`：将 Telethon message 转为 `TelegramArchivedMessage`，包含媒体类型、source link、网页预览链接、text entity 链接和按钮 URL；若检测到 `telegra.ph`，消息类型标记为 `telegraph_page`，不当作 Telegram 原生媒体消息处理。
- `_get_dialog_filters()` / `_dialog_filter_names_for_dialog()`：通过 Telethon `messages.GetDialogFiltersRequest` 读取 Telegram 官方聊天文件夹，按显式 include/pinned peers 和频道/群组等过滤规则映射到本地 `Chat.telegram_folder_names`；读取失败时只同步聊天列表，不清空已有分组。
- `_search_joined_messages_async()`：遍历 dialogs，只搜索群聊和频道，可用所选 chat id 白名单跳过非目标会话；单个无权限会话会跳过，遇到 FloodWait 以 `TG004` 返回。
- `_session_base_path()`：按配置生成安全 session 文件名。

