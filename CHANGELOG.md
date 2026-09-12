# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-09-12

### Fixed

- 全文搜索现包含文件名（此前仅匹配标题/标签/摘要/正文，按文件名关键词搜不到）
- 自定义视图保存后可在 `/api/views` 正确列出（保存时补写 `view` 标志）
- `/api/views` 响应按视图名作键，不再暴露内部 `_view_` 前缀存储键
- `/api/delete` 响应不再返回回收站绝对路径，仅返回文件名
- CI 泄露检测器误报（跳过二进制文件、自身扫描、合法系统目录常量）

## [0.1.0] - 2026-09-12

### Added

- 本地网页服务，监听 127.0.0.1:8765
- 自动软链接同步（sync）
- 全文搜索（标题、标签、内容）
- 标签增删改、星标、备注
- 批量操作（批量改标签、批量删除）
- 自定义视图保存
- 发现未入库总结（discover）
- 离线 Markdown 兜底渲染
- 命令行工具 `noted`
- macOS LaunchAgent 开机自启配置（install.sh / uninstall.sh）
- `noted doctor` 健康检查子命令
- 收藏区、视图模式切换、相关笔记、移动端抽屉导航
- Host 头严格校验
- Origin 校验
- 安全响应头
- 路径穿越防护
- 发现候选 token 边界（不暴露绝对路径）
- Markdown 链接白名单（仅允许 http/https/mailto/安全相对路径）
- 前端事件委托（无内联 onclick）
- 支持 `NOTED_HOME`、`NOTED_PORT` 环境变量
- 支持 `--notes-dir`、`--port` 命令行参数
- 端口占用时给出明确提示

### Security

- GET 端点新增 Host 校验
- POST 端点新增 Host 与 Origin 校验
- 不暴露绝对路径，仅返回来源目录名
- 拒绝路径穿越请求
- discover 返回 opaque token 而非文件路径
- 服务端 token 缓存与过期清理
