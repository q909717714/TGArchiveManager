# 配置说明

TGArchiveManager 启动时按以下顺序读取配置：

1. `config/config.yaml`
2. `config/config.yaml.example`

`config/config.yaml` 用于保存本机真实配置和 Telegram API 凭据，不应提交或分发给他人。

程序内“设置”页可编辑本文件中的稳定配置项，并写入本机 `config/config.yaml`。示例配置 `config/config.yaml.example` 只作为默认模板。

## Telegram

```yaml
telegram:
  api_id: ""
  api_hash: ""
  session_name: "user"
  session_dir: "sessions"
```

- `api_id` / `api_hash`：从 Telegram 官方开发者后台获取。也可以在登录页输入并保存。
- `session_name`：Telethon session 文件名前缀。
- `session_dir`：session 文件目录。打包后应位于 exe 同级目录下，保持可写。

## 搜索

```yaml
public_search:
  enabled: true
  default_max_results: 100
  duplicate_check: true
  require_preview_before_forward: true
  default_forward_mode: "card"
```

第一版公开搜索最多保存 100 条结果。结果会写入 SQLite，并在转发前以文本卡片发送。

搜索工具由 `search_engines` 配置控制：

```yaml
search_engines:
  jisou:
    enabled: true
    type: "telegram_bot"
    username: "@jisou"
  telegram_native:
    enabled: true
    type: "telegram_native"
```

- `telegram_bot`：通过指定 Bot username 搜索，“Bot 公开搜索”页也会把已同步聊天中 username 以 `bot` 结尾的 Bot 加入选项。
- `telegram_native`：不使用 Bot，由“TG 频道搜索”页使用；页面可从已同步频道/群聊中多选搜索范围，只在选中且账号可访问的频道/群聊内执行 Telegram 消息搜索。
- `rate_limit_seconds`：Bot 结果分页时，每次点击下一页按钮前等待的秒数。
- `duplicate_check`：为 true 时保存搜索结果会标记历史重复项；为 false 时不做历史重复标记。

## 转发

```yaml
forward:
  default_interval_seconds: 3
  max_per_task: 100
  skip_duplicates: true
```

- 默认发送间隔是 3 秒。
- `max_per_task` 限制单次转发最多处理的搜索结果或聊天记录数量；超过限制时任务不会启动。
- `create_group_before_forward` 控制转发页默认是否选择自动建群策略。
- `default_group_name_rule` 用于设置自动建群默认名称前缀。
- 第一版只转发结果卡片文本，不强制转发原始消息。
- 转发任务会生成 `logs/tasks/forward_*.log`。

## 备份和下载

```yaml
backup:
  default_limit: 1000
  enable_incremental: true

download:
  root_dir: "downloads"
  download_images: true
  download_videos: true
  download_documents: true
  download_audio: true
  max_file_size_mb: 500
  retry_count: 3
  skip_existing: true
```

备份写入 SQLite。媒体下载会按账号可访问的消息定位信息调用 Telegram 下载接口，并记录成功、失败或跳过状态。
下载前会按媒体类型开关、已知文件大小上限和本地已存在文件判断是否跳过；跳过同样会写入下载记录。Telegraph 图片下载遵守图片开关和大小上限。

## 导出

```yaml
export:
  root_dir: "exports"
  enable_csv: true
  enable_excel: true
  enable_json: true
  enable_html: false
```

当前支持 CSV、Excel、JSON。HTML 导出是可选增强。

## 数据库和日志

```yaml
database:
  path: "data/tg_archive.db"

logs:
  root_dir: "logs"
  level: "INFO"
  retention_days: 30
  save_public_search_log: true
  save_forward_log: true
  save_download_log: true
```

- `retention_days` 大于 0 时，启动日志服务会清理超过保留天数的 `.log` 文件。
- `save_public_search_log` / `save_forward_log` / `save_download_log` 控制是否额外创建对应模块独立日志；关闭后日志仍会进入应用主日志。

打包发布时必须保证以下目录可写：

- `config`
- `sessions`
- `logs`
- `logs/tasks`
- `downloads`
- `exports`
- `data`

可以执行以下命令验证：

```powershell
python scripts\preflight_check.py --root .
```
