# 发布检查清单

## 公开仓库隐私检查

- 候选文件不得带 Windows EFS `Encrypted` 属性，不得包含无法读取或需要密码解密的源码文件。
- 不提交 `config/config.yaml`、Telegram session、SQLite 数据库、日志、下载、导出、`.vscode`、`.agents`、`build` 或 `dist`。
- 提交前使用 `git status --short --ignored` 和 `git ls-files` 复核公开文件清单。
- 对 `git ls-files` 执行凭据模式扫描，只输出命中文件名，不在终端或 CI 中打印疑似密钥值。
- 首个公开 Release 只发布 GitHub 自动生成的源码归档；本地调试构建不得作为附件上传。
- 后续如需发布 Windows 二进制，必须从干净工作区重新构建，并单独检查包内配置、session、数据库、日志和用户内容。

## 构建环境

- Windows 10/11
- Python 3.10+
- `requirements-dev.txt` 已安装
- 可以执行 `python -m PyInstaller --version`

## 推荐打包命令

重复打包时在项目根目录执行：

```powershell
.\package.cmd
```

PowerShell 入口：

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

输出目录：

```text
dist\TGArchiveManager
```

## 发布包内容

发布目录应包含：

- `TGArchiveManager.exe`
- `config\config.yaml.example`
- `sessions`
- `logs`
- `logs\tasks`
- `downloads`
- `exports`
- `data`

## 干净 Windows 环境验收

在没有源码和虚拟环境的新目录中执行：

1. 拷贝 `dist\TGArchiveManager` 到目标机器。
2. 启动 `TGArchiveManager.exe`。
3. 确认自动创建或保留 `sessions/config/logs/downloads/exports/data`。
4. 登录 Telegram。
5. 重启程序，确认 session 自动恢复。
6. 同步聊天列表。
7. 执行一次公开搜索。
8. 保存搜索结果。
9. 选择前 1-3 条结果进行卡片转发。
10. 执行一次小范围备份，例如最近 10 条。
11. 执行一次本地搜索。
12. 导出 CSV 或 JSON。
13. 打开日志页，按 task_id 和错误码过滤。

## 发布前本地验证命令

```powershell
python -m compileall . -q
python -m unittest discover -s tests
python main.py --check
$env:QT_QPA_PLATFORM="offscreen"; python main.py --check-gui
python scripts\preflight_check.py --root .
```

构建后验证：

```powershell
python scripts\preflight_check.py --root dist\TGArchiveManager
dist\TGArchiveManager\TGArchiveManager.exe --check
$env:QT_QPA_PLATFORM="offscreen"; dist\TGArchiveManager\TGArchiveManager.exe --check-gui
```

## 功能边界

发布版本不得包含以下能力：

- 自动大量加群
- 自动拉人
- 破解私密频道
