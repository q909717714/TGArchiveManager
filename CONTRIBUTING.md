# 参与贡献

感谢关注 TGArchiveManager。提交修改前请先阅读 `AGENTS.md` 和 `docs/project-brief.md`。

## 开发流程

1. 从公开仓库创建分支。
2. 保持 UI、Worker、Service、Repository 和 Parser 的现有分层边界。
3. 为新增业务逻辑补充对应测试。
4. 提交前运行：

```powershell
python -m compileall . -q
python -m unittest discover -s tests
python main.py --check
python scripts\preflight_check.py --root .
```

## 隐私和合规

- 不要提交 `config/config.yaml`、Telegram session、数据库、日志、下载内容、导出文件或本地构建产物。
- 不要在 Issue、PR、测试或截图中公开 API Hash、验证码、密码、手机号或聊天内容。
- 不接受大量加群、成员采集、破解私密内容或访问账号无权限内容的功能。
- 安全问题请按 `SECURITY.md` 私下报告，不要先公开漏洞细节。
