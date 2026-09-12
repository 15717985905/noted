# 落笔 · Noted

落笔 Noted 是一个**本地个人知识库工具**，自动收纳 opencode / codex / Claude Code 等 agent 对话产生的总结，通过浏览器阅读，零第三方依赖。

**重要声明：发布版源码绝不包含任何用户笔记、同步来源或个人信息。** 用户数据仅存在于用户本机的 `~/ai-notes/`（或 `NOTED_HOME` 指向的目录）中，由用户自行管理备份与隐私。

## 功能列表

- 阅读：浏览器端 Markdown 渲染，支持代码块、列表、引用
- 搜索：全文搜索标题、标签、正文
- 标签：读取 markdown 头部 `标签:` 行，支持网页端增删改
- 星标：标记重要总结
- 视图：保存自定义筛选视图（搜索 + 标签组合）
- 收藏区：独立收藏列表，快速访问
- 三种视图：平铺（按时间排序）、按来源分组、按标签分组
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

# JS 语法检查（提取 HTML 内嵌 JS）
node --check <(sed -n '/<script>/,/<\/script>/p' src/noted/hub.py | sed 's/<script>//g; s/<\/script>//g')

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

## License

MIT
