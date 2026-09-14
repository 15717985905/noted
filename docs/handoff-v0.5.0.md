# Noted 主管交接文档

> 版本：v0.5.x → v0.6.0 准备  
> 日期：2026-09-14  
> 仓库：https://github.com/15717985905/noted  
> 基线：`007427e`（tag-driven versioning）

---

## 1. 当前状态

- **发布版本**：v0.5.0 已发布，GitHub Release 已推送，CI 全绿。
- **工作树**：`main` 与 `origin/main` 同步，无未提交改动。
- **安装环境**：LaunchAgent `/Users/qym/Library/LaunchAgents/com.noted.plist` 使用 `/Users/qym/.venvs/noted` 虚拟环境，端口 `8765`。
- **用户数据**：`~/ai-notes`，默认笔记目录，**禁止纳入 Git 或测试修改**。

## 1.1 版本策略

- 实际发布版本以 Git tag `vMAJOR.MINOR.PATCH` 为准，如 `v0.5.1`、`v0.5.2`。
- `pyproject.toml` 的 `version` 仅作为包基础版本，不随每次补丁发布修改。
- 运行时网页标题与 `/api/list` 返回的 `version` 来自当前 tag；无 tag 时回退到 `pyproject.toml` 版本。

---

## 2. v0.5.0 已完成验收项

### 2.1 四视图分组交互
- 平铺 / 按来源 / 按标签 / 分组 四种视图模式统一卡片 `noteCardHtml`。
- 右键菜单、移动端长按菜单、拖拽建组/移入/移出、批量选择。
- 分组胶囊跳转、组头重命名/解散、折叠/展开。
- 单文件单组约束、rename/delete 联动、损坏 `.index.json` 防护。

### 2.2 三态阅读 / Word / Markdown
- 阅读页模式条：阅读 / Word / Markdown 三按钮切换。
- Word 富文本编辑器：`contenteditable` + `document.execCommand`，工具栏覆盖标题、粗斜体、行内代码、链接、引用、代码块、列表、任务列表、表格、图片。
- Markdown 双栏编辑：原文编辑 + 安全预览，移动端编辑/预览切换。

### 2.3 Word 序列化与保存
- `wordSerializeToMarkdown` 支持 h1-h6 / p / pre / 表格 / 嵌套列表 / 任务复选框 / 引用 / 代码块。
- 代码块保留连续空行，按最长连续反引号动态选择围栏。
- 表格单元格换行规整为空格，管道符写为 `\|`。
- `saveWord` 调用 `/api/save`，成功后更新 `wordState.revision`、清除 dirty、刷新只读内容和列表。
- `wordSaveInFlight` 防止重复提交，`beforeunload` 拦截 dirty 状态。

### 2.4 冲突与软链接保护
- `/api/save` revision 乐观锁，冲突返回 `{"conflict": true, "revision": ...}`。
- 软链接需 `propagate: true` 确认；共享 realpath 拒绝安全写。
- Word 冲突弹窗：重新加载 / 覆盖保存 / 取消。
- Markdown 保存后返回阅读会重新加载 `/api/read/html`。

### 2.5 跨模式一致性
- 每次从阅读/Markdown 进入 Word 都重新执行 `loadWordContent`。
- Word 任务复选框加载后移除 `disabled`、设置 `contenteditable=false`，使用 `.checked` 序列化。
- 粘贴净化结果为空时回退插入纯文本。

### 2.6 备注持久化修复
- 备注改为通过 `/api/tags` 扩展 `note` 字段保存，不再伪造 `_note_<file>` 视图。
- 服务端 `send_tags` 支持可选 `note`：字符串截断 500 字符，`null` 清除，省略保留。
- 旧 `_note_` 伪视图在 `/api/views` 中已过滤。

### 2.7 Word 图片闭环修复
- `/api/upload` 返回相对路径 `note/xxx.png`。
- `wordInsertImageNode` 自动转为浏览器可访问的 `/api/asset?file=note/xxx.png`。
- `wordImageMd` 检测 `/api/asset?file=` 前缀，还原为原始 Markdown 相对路径。
- 外链 `http/https` 保留，危险 URL 拒绝。

---

## 3. 技术架构与约束

### 3.1 单文件架构
- 核心实现集中于 `src/noted/hub.py`，服务端、HTML、CSS、前端 JS 均内嵌在单文件。
- 测试文件：`tests/test_hub.py`，当前 197 项测试。

### 3.2 零依赖
- Python 标准库 + 原生浏览器 API。
- `pyproject.toml` 无 `dependencies` / `install_requires`。
- 前端无 CDN、无 `import`/`require`、无第三方编辑器框架。

### 3.3 安全模型
- Host 头严格校验 + Origin 校验。
- 路径穿越防护：`is_safe_note_ref` / `is_valid_note_name` + `realpath` 前缀校验。
- 软链接安全模型：propagate 确认、共享 realpath 拒绝。
- 软错误 JSON：服务端异常不抛堆栈，返回 `{"ok": false, "error": "..."}`。

### 3.4 前端事件约定
- **禁止 `onclick=`**：全部事件委托。
- 无 `marked` / CDN / 第三方库。

---

## 4. 验证记录

| 检查项 | 结果 |
|--------|------|
| `python -m py_compile` | OK |
| `python -W error::SyntaxWarning -m py_compile` | OK |
| `python -W error::ResourceWarning -m unittest discover tests` | 197 tests OK |
| `bash scripts/smoke.sh` | PASS 7/7 |
| 运行时 HTML 内嵌 JS `node --check` | OK |
| `git diff --check` | OK |
| Chrome/CDP 浏览器验收 | 全流程通过 |
| GitHub CI | success |

---

## 5. 已知风险与后续 TODO

### 5.1 低风险
- Word 图片闭环已修复，但未覆盖拖拽上传到 Word 的端到端浏览器自动化测试（当前 CDP 覆盖了点击上传和序列化）。
- 移动端长按菜单的浏览器级自动化未完全覆盖（当前有静态断言和部分 CDP 验证）。

### 5.2 待办
- [ ] v0.6.0 需求评审：图片拖拽上传、批量备注、视图导出、主题切换等。
- [ ] 浏览器测试脚本整理到 `scripts/`，纳入 CI。
- [ ] 考虑为嵌入 JS 增加 lint 规则（当前仅 `node --check`）。
- [ ] 文档更新：README 中的 Word 编辑说明、分组使用说明。

---

## 6. v0.6.0 更新规划（草案）

### 6.1 建议方向
1. **图片拖拽与粘贴增强**
   - Word 和 Markdown 模式支持拖拽图片到编辑器。
   - 剪贴板图片粘贴自动上传并插入 Markdown 引用。
   - 图片库管理：查看、删除、替换当前笔记图片。

2. **批量备注与元数据**
   - 批量选择后统一添加/清除备注。
   - 备注支持 Markdown 格式（当前纯文本）。
   - 备注搜索与过滤。

3. **视图增强**
   - 视图导出为 JSON / Markdown 列表。
   - 视图共享（导出配置到剪贴板）。
   - 最近打开记录。

4. **编辑器体验**
   - Markdown 编辑器支持快捷键提示面板。
   - 代码块语言选择器。
   - 表格编辑器增强（添加/删除行列）。

5. **移动端优化**
   - 移动端分组管理交互优化。
   - 触屏拖拽排序。
   - PWA 离线支持（localStorage 缓存最近阅读）。

### 6.2 技术约束保持
- 零第三方依赖不变。
- 单文件架构不变（除非性能瓶颈）。
- 安全模型不变（Host/Origin + 路径校验 + 软链接保护）。
- 无 `onclick=`，事件委托不变。

### 6.3 发布计划
- 迭代周期：2 周。
- 里程碑：
  1. 需求冻结 + 技术设计
  2. 核心功能开发 + 单元测试
  3. 浏览器验收 + 文档更新
  4. 发布 v0.6.0

---

## 7. 交接清单

- [x] 代码已提交并推送到 `origin/main`
- [x] GitHub Release `v0.5.0` 已发布
- [x] CI 配置已更新（运行时 JS 检查）
- [x] 浏览器验收通过
- [x] 用户环境已更新到 v0.5.0
- [ ] 下一版本需求确认（待用户/产品决策）
- [ ] v0.6.0 开发启动

---

## 8. 关键命令速查

```bash
# 开发环境
cd /Users/qym/Projects/noted
git pull origin main
pip install -e .

# 运行测试
python -W error::ResourceWarning -m unittest discover tests -v
bash scripts/smoke.sh

# JS 语法检查（运行时 HTML）
PYTHONPATH=src python -W error::SyntaxWarning -c \
  'from noted.hub import HTML; s = HTML.index("<script>") + len("<script>"); e = HTML.index("</script>", s); print(HTML[s:e], end="")' | node --check

# 启动服务
noted
# 或
python -m noted.hub

# 浏览器验收（临时目录）
NOTED_HOME=/tmp/noted-home NOTED_PORT=8766 PYTHONPATH=src python -m noted.hub
# Chrome headless + CDP 脚本在 /var/folders/.../T/opencode/browser_cdp_check.py
```

---

## 9. 联系方式

- 仓库：https://github.com/15717985905/noted
- Issues：https://github.com/15717985905/noted/issues
- 当前主管：opencode agent（本次会话）

---

*文档结束*
