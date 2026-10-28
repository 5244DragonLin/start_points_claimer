# 贡献指南

欢迎参与本项目的开发！无论是修复 Bug、新增功能还是完善文档，都可以通过 Issue 和 Pull Request 参与进来。

## 贡献流程

1. **Fork** 本仓库到你的账户
2. **Clone** 你的 Fork 到本地：`git clone https://github.com/<your-name>/start_points_claimer.git`
3. 创建**特性分支**：`git checkout -b feature/your-feature`
4. **开发**并提交：保持提交粒度清晰，信息言简意赅
5. **推送**分支：`git push origin feature/your-feature`
6. 提交 **Pull Request** 到本仓库的 `main` 分支，并在 PR 描述中说明改动内容

## 开发约定

- 遵循本仓库已有的代码风格与目录结构（见 `README.md`）
- 本项目**零配置**：领取文案、入口路径等匹配规则统一写在 `main.py` 顶部的「内置参数」常量区，不引入配置文件、不新增第三方依赖
- 提交信息使用中文或英文均可，但需清晰描述改动意图
- 新增功能建议同步更新 `README.md` 的「亮点」「快速开始」与「更新日志」
- 若涉及内置参数变更，请同步更新 `README.md` 的「参数调整」

## 调试建议

- 页面改版导致定位失败时，先跑 `python main.py probe` 导出 `logs/dom_dump.txt`，对照新页面结构修改 `main.py` 常量区的文案/选择器
- 复现问题建议加 `--visible` 围观运行过程，并附上控制台日志（可把 `main.py` 的 `LOG_FILE` 设为 `"run.log"` 落成文件）

## 问题反馈

- 使用中发现问题，请先检索已有的 [Issues](../../issues)，避免重复提交
- 提交新 Issue 时请尽量包含：复现步骤、预期行为、实际行为、运行环境（系统 / Python 版本 / 依赖版本）
- 涉及页面定位问题的，请附上 `logs/dom_dump.txt`（**注意先删掉里面的个人昵称等信息**）

感谢你的贡献 ❤️
