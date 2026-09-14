# 落笔 · Noted

落笔 Noted 是一个**本地个人知识库工具**，自动收纳 opencode / codex / Claude Code 等 agent 对话产生的总结，通过浏览器阅读，零第三方依赖。

**重要声明：发布版源码绝不包含任何用户笔记、同步来源或个人信息。** 用户数据仅存在于用户本机的 `~/ai-notes/`（或 `NOTED_HOME` 指向的目录）中，由用户自行管理备份与隐私。

## 功能列表

- 阅读：浏览器端 Markdown 渲染，支持代码块、列表、引用
- 重命名：网页端重命名文件，可选联动更新正文 H1 标题、同步重命名源文件（软链接）
- 三态编辑：阅读、Word 富文本、Markdown 原文可切换；Word 支持格式、链接、引用、代码、列表、任务、表格、图片和 Markdown 往返保存
- Markdown 编辑：网页端编辑原文，双栏实时预览，支持表格、代码、任务列表、图片、快捷键和冲突保护
- 分组折叠：虚拟分组管理，支持右键/长按、拖拽建组/拖入组/批量勾选加入，组可折叠、重命名、解散（访达式交互，不动真实文件）
- 文件定位：阅读页查看文件路径、在 Finder 中显示、用编辑器打开
- 搜索：全文搜索标题、标签、正文
- 标签：读取 markdown 头部 `标签:` 行，支持网页端增删改
- 星标：标记重要总结
- 视图：保存自定义筛选视图（搜索 + 标签组合）
- 收藏区：独立收藏列表，快速访问
- 四种视图：平铺（按时间排序）、按来源分组、按标签分组、虚拟分组
- 相关笔记：阅读页推荐同标签/同来源笔记
- 移动端抽屉：侧栏抽屉导航，适配小屏
- 发现与同步：扫描 agent 会话目录和自定义同步目录，自动入库
- Doctor：`noted doctor` 健康检查
- 批量操作：批量改标签、批量删除

## 快速开始

### 源码安装（当前推荐）

```bash
git clone https://github.com/15717985905/noted.git
cd noted
pip install -e .
```

### pipx / pip（从 Git 安装）

```bash
pipx install git+https://github.com/15717985905/noted.git
```

## 命令行参考

```bash
noted              # 打开浏览器阅读页
noted list         # 列出所有总结
noted search 关键词   # 搜索总结
noted sync         # 手动同步软链接
noted discover     # 发现未入库总结
noted doctor       # 运行健康检查
noted add-path 目录  # 添加同步目录
noted serve        # 启动本地网页服务（开发用）
```

## 配置

- `NOTED_HOME`：笔记目录，默认 `~/ai-notes`
- `NOTED_PORT`：服务端口，默认 `8765`

## Install / Uninstall / LaunchAgent

### install.sh

```bash
bash scripts/install.sh [--notes-dir ~/ai-notes] [--port 8765]
```

可选参数：
- `--notes-dir`：笔记目录，默认 `~/ai-notes`
- `--port`：服务端口，默认 `8765`
- `--python`：Python 可执行文件路径，默认 `python3`

安装后服务会在登录时自动启动。

### uninstall.sh

```bash
bash scripts/uninstall.sh
```

停止并移除 LaunchAgent。

### 手动管理 LaunchAgent

```bash
# 加载
launchctl load ~/Library/LaunchAgents/com.noted.plist

# 卸载
launchctl unload ~/Library/LaunchAgents/com.noted.plist

# 查看状态
launchctl list | grep noted
```

## 安全模型

- **仅本地绑定**：服务监听 `127.0.0.1`，拒绝外网访问
- **Host 头严格校验**：仅允许 `localhost`、`127.0.0.1`、`[::1]`
- **Origin 校验**：POST 请求仅接受同源请求
- **安全响应头**：`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`
- **路径穿越防护**：禁止读取敏感文件
- **无遥测**：不收集任何使用数据
- **笔记不出本机**：所有数据仅存在于用户本机
- **为何按需路径是安全的**：文件路径仅通过 `/api/detail`、`/api/reveal`、`/api/open` 三个按需接口提供，且依赖 127.0.0.1 绑定、Host 校验防止 DNS 重绑定、响应永不带 `Access-Control-Allow-Origin`（跨源请求无法读取）。攻击者无法通过浏览器跨站获取绝对路径。

## 隐私声明

落笔 Noted 是一个纯本地工具。你的笔记、标签、星标等所有数据都存储在 `NOTED_HOME` 目录下，由你自行管理。服务不会向任何外部服务器发送数据。

## FAQ

**Q: 笔记存在哪里？**
A: 默认在 `~/ai-notes/`，可通过 `NOTED_HOME` 环境变量修改。

**Q: 如何备份？**
A: 直接备份 `NOTED_HOME` 目录即可，包含 markdown 文件、`.index.json` 和 `.sync-paths`。

**Q: 支持多人使用吗？**
A: 每个用户需要独立安装和运行，服务仅监听本地回环地址。

**Q: 为什么需要 python3？**
A: 落笔 Noted 使用 Python 3.10+ 编写，零第三方依赖，macOS 自带 Python 即可运行。

## 开发与测试

```bash
# 安装开发依赖
pip install -e .

# 运行测试（含 ResourceWarning 检查）
python -W error::ResourceWarning -m unittest discover tests -v

# 语法检查
python -m py_compile src/noted/hub.py src/noted/cli.py

# JS 语法检查（提取运行时 HTML 内嵌 JS）
PYTHONPATH=src python -W error::SyntaxWarning -c 'from noted.hub import HTML; start = HTML.index("<script>") + len("<script>"); end = HTML.index("</script>", start); print(HTML[start:end], end="")' | node --check

# 冒烟测试
bash scripts/smoke.sh
```

## 停止服务

```bash
# 前台启动直接 Ctrl+C
# 或找到进程后终止
pkill -f "python3 -m noted.hub"
```

## 标签存储

- 默认读取 markdown 文件头部的 `标签:` 行
- 网页端修改标签后写入 `.index.json`，优先采用用户手动修正的标签
- 原始 markdown 文件不被修改

## 分组使用说明

### 三种创建/添加方式

1. **拖拽建组**：在分组视图下，直接拖入文件到「+ 新建分组」区域，输入组名即可创建并加入。
2. **拖入已有组**：将文件拖到已有组头，自动加入该组（若文件已在其他组，会自动移出旧组）。
3. **批量加入**：勾选文件后点击批量栏「加入分组」，输入组名（已有组直接加入，不存在的组自动创建）。
4. **移动端加入**：文件操作行的「加入分组」按钮，输入组名即可。

### 组管理

- **折叠/展开**：点击组头的箭头图标
- **重命名组**：组头「重命名」按钮
- **解散组**：组头「解散」按钮（文件回到未分组，不删除文件）
- **移出文件**：组内文件旁的「移出」按钮

### 数据安全

- **虚拟分组**：分组关系仅保存在 `.index.json` 中，不移动、不复制任何真实文件
- **单文件单组**：一个文件同一时刻最多属于一个组；拖入新组自动从旧组移出
- **删除组不删文件**：解散组仅删除 `.index.json` 中的组记录，文件完好保留在原始位置
- **rename 联动**：重命名文件时，分组记录自动迁移（旧名→新名）
- **delete 联动**：删除文件时，自动从所有分组中移除；空组保留（需手动解散）

## Word 编辑

阅读页支持「阅读 / Word / Markdown」三态切换。Word 使用本地富文本编辑器，支持标题、粗斜体、行内代码、链接、引用、代码块、列表、任务列表、表格和图片；保存时会转换为 Markdown。图片仍保存到当前笔记的本地资源目录，重新打开 Word 或切换 Markdown 后保持同一资源引用。支持 Cmd/Ctrl+S、未保存提醒、revision 冲突处理和软链接源文件确认。

## Markdown 编辑

在阅读页切换到「Markdown」进入工作台。桌面端为 Markdown 原文与安全预览双栏布局，移动端可切换「编辑/预览」。工具栏可插入标题、强调、链接、引用、代码块、列表、任务列表、表格和图片；普通文本或 AI 生成的 Markdown 粘贴会保留原文，剪贴板图片会保存到当前笔记的本地资源目录并自动插入引用。支持 Cmd/Ctrl+S 保存。

保存使用 revision 乐观并发保护：文件被其他程序修改时不会覆盖，需重新加载或明确选择覆盖。软链接笔记默认不写源文件，只有明确确认后才同步修改源文件；图片和 Markdown 资源仅保存在本机笔记目录，不上传外部服务。

## License

MIT
