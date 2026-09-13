# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-09-13

### Added

- Markdown 网页编辑工作台：原文编辑、桌面双栏预览、移动端编辑/预览切换
- 编辑工具栏：标题、强调、链接、引用、代码块、列表、任务列表、表格和图片
- 文本/AI Markdown 原样粘贴，剪贴板图片本地保存并自动插入 Markdown 引用
- 保存快捷键、未保存提示、revision 冲突保护和软链接源文件确认
- 编辑、预览、保存、图片上传和本地资源读取 API
- 图片类型、大小、文件魔数、路径穿越和 XSS 防护

## [0.3.0] - 2026-09-12

### Added

- 分组折叠管理（虚拟分组，数据存储在 `.index.json`）
- 三种创建/添加方式：拖拽文件新建组、拖入已有组、批量勾选加入组
- 分组视图（第 4 种视图模式）：组头可折叠/展开、重命名、解散
- 移动端降级：文件操作行「加入分组」按钮 + 分组选择器
- 组与 rename/delete 联动：重命名自动迁移组成员，删除后空组保留（需手动解散）

### API

- `GET /api/groups`：列出所有组
- `POST /api/group/create`：创建组
- `POST /api/group/add`：文件加入组（自动移出旧组）
- `POST /api/group/remove`：文件移出组（支持按组名或按文件名）
- `POST /api/group/rename`：重命名组
- `POST /api/group/disband`：解散组（文件回未分组）
- `POST /api/group/toggle`：切换折叠状态

### Security

- 组操作继承 Host/Origin 校验
- 单成员约束服务端强制（文件自动移出旧组）
- 组名校验防止路径穿越与内部键冲突
- 空组持久化（不自动解散，仅显式解散）

## [0.2.0] - 2026-09-12

### Added

- 阅读页文件信息区：显示文件类型（本地文件/软链接·来源目录）、绝对路径（点击复制）、"在 Finder 中显示"与"打开"按钮
- 列表平铺视图操作行新增 📂 按钮（data-action="reveal"），点击后在 Finder 中显示对应文件
- `GET /api/detail`：返回 `{file, kind, path, source_label, exists}`，symlink 返回源文件 realpath，local 返回 notes_dir 绝对路径
- `POST /api/reveal`：macOS 用 `open -R`，其他平台用 `xdg-open` 打开所在目录
- `POST /api/open`：支持 `NOTED_EDITOR` 环境变量（shlex.split + Popen），否则 macOS `open` / 其他 `xdg-open`
- 断链软链接在阅读页显示红色"断链"状态并禁用按钮

### Security

- 路径按需提供：仅通过 `/api/detail`、`/api/reveal`、`/api/open` 三个接口返回绝对路径
- 所有路径接口继承 Host/Origin 校验，响应不带 `Access-Control-Allow-Origin`
- 127.0.0.1 绑定 + Host 校验防止 DNS 重绑定 + 无 CORS 头 = 跨站无法读取路径

## [0.1.2] - 2026-09-12

### Added

- 网页端重命名：列表操作行与阅读页均提供"重命名"
- 重命名弹窗：预填当前文件名，可选"同时更新正文 H1 标题"（默认开）与"同步重命名源文件"（仅软链接显示，标注来源目录名）
- `POST /api/rename`：`{file, new_name, update_h1, propagate}`，响应不含绝对路径
- 重命名随迁 `.index.json` 中的标签、星标、备注与 `_note_` 键

### Security

- 重命名强校验：拒绝路径穿越、非法字符、隐藏文件名、保留名、同名冲突
- 联动源文件前检查 realpath 唯一性（多链接共享同一源文件时拒绝）
- 源文件位于笔记目录内时自动降级为直接改名（不再创建自指链接）

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
