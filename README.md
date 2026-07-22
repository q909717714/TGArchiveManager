# TGArchiveManager

[![CI](https://github.com/q909717714/TGArchiveManager/actions/workflows/ci.yml/badge.svg)](https://github.com/q909717714/TGArchiveManager/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/q909717714/TGArchiveManager)](https://github.com/q909717714/TGArchiveManager/releases/latest)
[![License](https://img.shields.io/github/license/q909717714/TGArchiveManager)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)

TGArchiveManager 是一个本地 Windows 桌面工具，用于管理 Telegram 内容归档流程。
项目基于 Python 3.10+、PySide6、Telethon 和 SQLite 实现。

> 当前状态：`v0.1.0` 公开预览。项目仍在持续完善，建议先使用测试账号和小范围聊天验证工作流。

当前 MVP 已覆盖：Telegram 登录、聊天同步、公开搜索、搜索结果管理和转发、聊天记录转发、消息备份、媒体下载、本地搜索、数据导出、日志排查和 Windows 打包发布。

## 界面预览

截图由空配置、空数据库和未登录状态的本地演示环境生成，不包含真实账号或聊天数据。

### Bot 公开搜索

![Bot 公开搜索界面](docs/images/public-search.png)

<details>
<summary>查看更多界面</summary>

### 备份下载

![备份下载界面](docs/images/backup-download.png)

### 数据导出

![数据导出界面](docs/images/data-export.png)

</details>

## 功能范围

已支持的工作流：

- 使用 API ID/API Hash、手机验证码和可选二步验证密码登录 Telegram。
- 同步当前账号可访问的聊天，读取 Telegram 官方聊天文件夹/分组，并编辑本地标签。
- 通过多个 Telegram Bot Provider 执行 Bot 公开搜索，或在独立“TG 频道搜索”页指定已加入频道/群聊执行 TG 原生消息搜索，并保存标准化结果；TG 频道搜索结果可展示媒体大小或 Telegraph 图片数量。
- 当 Bot/TG 搜索结果、Telegram 消息文本、Bot 按钮或链接预览中出现 `telegra.ph` 链接时，识别为 Telegraph 图片页面卡片，解析标题、发布时间、作者/来源、图片数量和 Telegram 跳转链接；下载时按页面标题创建目录，并可限制图片下载数量。
- 在统一“搜索结果”页筛选、预览、下载或删除已保存搜索结果，也可删除当前搜索任务及其结果。
- 将已保存的搜索结果以文本卡片形式转发到已有群或新建整理群。
- 将本地已备份聊天记录以纯文本记录形式转发到已有目标群，或自动新建一个群组后转发；图片/视频记录会附带软件可识别的内部下载链接。
- 将聊天消息按最近 N 条或时间范围备份到 SQLite。
- 下载账号可访问消息中的媒体，显示当前任务、已下载 MB 和图片张数，并记录失败、重试和跳过状态。
- 对本地已备份消息执行关键词、聊天、时间、类型过滤。
- 导出 CSV、Excel、JSON，HTML 作为可选增强。
- 在日志页按日志文件、模块、等级、任务 ID 和关键词排查问题。
- 在设置页编辑 Telegram、搜索、转发、备份下载、导出和日志配置，并保存到本机 `config/config.yaml`。
- Bot 公开搜索、TG 频道搜索、搜索结果媒体下载、转发管理和备份下载页的长任务支持在界面中请求取消；取消会在安全检查点停止，不强制终止线程。
- 聊天列表、自建群登记、搜索结果、备份消息和本地搜索结果支持删除本地记录；删除不会移除 Telegram 远端内容或已下载到磁盘的媒体文件。
- 主要下拉框支持输入关键字模糊过滤，适合在大量聊天、目标群或任务选项中快速定位。

不支持也不会实现的能力：

- 自动大量加群。
- 自动拉人或成员采集。
- 破解或访问账号无权限的私密内容。

## 快速启动

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

只执行初始化检查，不启动界面：

```powershell
.\.venv\Scripts\python.exe main.py --check
```

运行单元测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

## 配置说明

程序优先读取 `config/config.yaml`，如果该文件不存在，则使用 `config/config.yaml.example`。

登录页保存的 Telegram API 凭据会写入 `config/config.yaml`。该文件只应保留在本机，不要提交或分发。

完整配置说明见：[configuration.md](docs/configuration.md)。

## 隐私与安全

- 真实配置、Telegram session、SQLite 数据库、日志、下载、导出和本地构建产物均被排除在版本控制之外。
- 发布 Issue、PR、测试记录或截图前，请移除手机号、聊天内容、API Hash、验证码和密码。
- 软件只处理当前账号有权访问的内容，不提供大量加群、成员采集或越权访问能力。
- 安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。

## 使用说明

完整使用说明见：[user-guide.md](docs/user-guide.md)。

主要流程包括：

- Telegram 登录
- 聊天列表同步
- Bot 公开搜索
- TG 频道搜索
- 搜索结果管理
- 搜索结果卡片转发
- 聊天记录转发
- 备份和媒体下载
- 本地搜索和导出
- 日志排查
- 设置

## Windows 打包

推荐重复打包命令：

```powershell
.\package.cmd
```

PowerShell 等价入口：

```powershell
powershell -ExecutionPolicy Bypass -File .\package.ps1
```

首次需要安装打包依赖时执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\package.ps1 -InstallDeps
```

指定 Python 解释器：

```powershell
powershell -ExecutionPolicy Bypass -File .\package.ps1 -Python .\.venv\Scripts\python.exe
```

底层构建脚本仍可直接调用：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

构建产物输出到：

```text
dist\TGArchiveManager
```

构建脚本会创建或验证 exe 同级的运行时可写目录：

- `config`
- `sessions`
- `logs`
- `logs\tasks`
- `downloads`
- `exports`
- `data`

干净 Windows 环境验收步骤见：[release-checklist.md](docs/release-checklist.md)。

## 日志排查

应用日志位于 `logs`，任务独立日志位于 `logs/tasks`。

程序内“日志”页支持：

- 按日志文件过滤
- 按模块过滤
- 按等级过滤
- 按任务 ID 过滤
- 按关键词或错误码过滤
- 打开日志目录
- 导出当前筛选日志
- 复制最新错误详情

## 开发检查

```powershell
python -m compileall . -q
python -m unittest discover -s tests
python main.py --check
$env:QT_QPA_PLATFORM="offscreen"; python main.py --check-gui
python scripts\preflight_check.py --root .
```

公开仓库会通过 GitHub Actions 在 Windows/Python 3.10 环境重复执行上述核心检查。完整使用流程见 [用户指南](docs/user-guide.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。

## 参与贡献与许可证

欢迎通过 [Issue](https://github.com/q909717714/TGArchiveManager/issues) 报告安装兼容性、导出格式和安全问题，也欢迎从 `good first issue` 或 `help wanted` 任务开始贡献。提交 Issue 或 PR 前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。项目采用 [MIT License](LICENSE)。
