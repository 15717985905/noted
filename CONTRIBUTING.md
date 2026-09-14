# Contributing

感谢你对落笔 Noted 的关注。

## 行为准则

- 尊重不同使用场景与隐私需求
- 提交 issue 或 PR 时请描述清楚复现步骤或改动动机

## 开发环境

```bash
# 克隆仓库后
pip install -e .

# 运行测试（必须含 ResourceWarning 检查）
python -W error::ResourceWarning -m unittest discover tests -v

# 语法检查
python -m py_compile src/noted/hub.py src/noted/cli.py
```

## 提交规范

- 保持零第三方依赖
- 不要引入任何硬编码的个人路径
- 不要修改用户数据目录外的逻辑时引入破坏性变更
- 测试覆盖新增的安全校验与边界条件
- 新增或修改 CLI 子命令时，同步补充函数级测试
- 确保 `python -W error::ResourceWarning -m unittest discover tests -v` 全绿后再提交

## 发布流程

- 更新 `CHANGELOG.md`
- 确保 `python -W error::ResourceWarning -m unittest discover tests -v` 全部通过
- 确保 `python -m py_compile` 全部通过
- 提交并打 tag，tag 格式为 `vMAJOR.MINOR.PATCH`，如 `v0.5.1`、`v0.5.2`
- 实际版本号以 Git tag 为准，无需修改 `pyproject.toml` 的 `version`
