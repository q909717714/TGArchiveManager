# Changelog

## [0.1.0] - 2026-07-22

首个公开预览版本。

### 功能

- Telegram 登录、聊天同步和官方分组读取。
- Bot 公开搜索与已加入频道/群聊的原生消息搜索。
- 搜索结果管理、文本卡片转发和自动建群转发。
- 消息备份、媒体下载、本地搜索与 CSV/Excel/JSON/HTML 导出。
- Telegraph 页面解析、图片统计和下载。
- Windows PyInstaller 打包、日志诊断和长任务协作取消。

### 质量与隐私

- 单元测试覆盖主要 Service、Repository、Parser、Worker/UI 静态逻辑。
- 真实配置、Telegram session、数据库、日志、下载、导出及本地构建产物不进入公开仓库。
- 明确禁止大量加群、成员采集和访问账号无权限内容。
