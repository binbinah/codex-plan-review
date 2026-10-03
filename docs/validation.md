# 验证记录

日期：2026-10-03。具体版本：Codex CLI `0.159.0-alpha.12.1`，Python `3.14.2`，macOS；Ruff `0.16.10`。

## 本地检查

- `python3 -m unittest discover -s tests -v`：53 个测试通过，退出码 0。
- `ruff check .`、`ruff format --check .` 与 Python 编译检查：退出码 0。
- 两个 skill 通过官方 skill-creator 的结构校验。
- 测试覆盖普通会话、直接正文提交、修订、异常输出、超时进程清理、预算、恢复、只读放行、并发、旧结果丢弃、最终正文变化、未跟踪文件变化和状态损坏。
- 凭证扫描在提交前执行；原始运行日志、配置和状态目录被 git 排除。

## 真实原生 Plan → 红队 → 子 agent

测试通过本机 `codex app-server`，使用私有临时配置、测试仓库和真实模型调用，不修改用户日常配置。插件通过 marketplace 实际安装并启用，6 个 hook 的信任只写入临时测试配置。

测试脚本：`tests/live_codex.py`。首次完整成功的实际结果：

| 验收项 | 证据 |
|---|---|
| 插件安装与加载 | installed/enabled；6 个 hook 被发现，无加载警告 |
| Plan 的实际权限 | read-only sandbox |
| 评审输入 | 主会话主动调用 submit --stdin，传入完整正文 |
| 独立红队 | 真实 Codex 进程返回 approve，1 轮 |
| 最终展示一致性 | 原生渲染 Plan 的 SHA-256 等于已评审正文的 SHA-256 |
| 评审前未实施 | 目标 result.json 不存在 |
| 原有执行流程 | 测试客户端正常切换到 Default 与 workspace-write，无额外批准口令 |
| 真实分派与回传 | 观察到 SubagentStop，插件记录 1 份回传 |
| 独立产物复验 | JSON 完整内容与求和断言通过，脚本退出码 0 |
| hook 异常 | failed_hooks 为空 |

产物的完整复验目标是 `{"task":"mechanical-probe","items":[1,2,3],"sum":6}`。测试成功不代表角色形成权限隔离，也不代表生产或复杂并行任务的可靠性。

原始证据在 git 排除的 `artifacts/live-codex.json`、`artifacts/live.log`、`artifacts/unit-tests.log`；公开仓库保留脚本与本脱敏记录。CI 另外验证 Linux/macOS、Python 3.11/3.13，真实供应商测试不在 CI 自动执行。

## 真实运行发现与设计取舍

1. 根 `plugin.json` 便携格式的 skill 可加载，但该客户端的 hooks/list 返回空清单。官方当前源码也有跳过 AgentPlugin hooks 的分支。本项目只使用 `.codex-plugin/plugin.json` 兼容入口，真实加载清单验证了全部 hooks。
2. 原生 Plan 最终被渲染为 plan item，Stop 输入的 last_assistant_message 为 null。permission_mode 反映执行权限，也不能可靠代表 Plan 协作模式。因此不按 Stop 正文或权限字符串判断完整 Plan。
3. 用户选择在最终 Plan 展示前主动传入完整正文。PreToolUse 接收字面 heredoc，在宿主运行红队，再把实际 shell 命令改写为输出 JSON；不解析 transcript，不要求主会话写计划文件或手填 session id。
4. Stop 如果能收到正文，会额外核对哈希；正文为空时无法独立核对最终文本，原样展示已评审正文属于 skill 契约。真实测试从 app-server 的 plan item 独立验证了这一契约。

## 未覆盖范围

- 用户日常桌面会话中的安装与 hook 信任交互。
- Windows、普通 Chat、云编排和其他 Codex 版本。
- 生产发布、数据库变更、跨供应商评审与复杂并行任务。
- 禁用、未信任 hooks 或模型跳过提交协议时，不能保证红队发生。

## 0.1.1 本机安装修复

在带有 node_repl、computer-use 和 chrome-devtools 的日常配置中，0.1.0 的红队子进程退出码为 1，错误为 `Error loading config.toml: invalid transport`。原因是 CLI `-c` 路径不按 TOML 语法去除段名的引号，原参数创建了带引号的新 MCP 名称。

0.1.1 改用 CLI 支持的直接段名路径；对不能安全表达的名称在启动前明确失败。修正参数的真实只读 Codex 调用返回 `OK`，退出码 0，并新增对应的配置协议回归测试。保留用户的模型、供应商配置；MCP 仍在红队进程中禁用。
