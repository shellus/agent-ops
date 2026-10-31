# Codex 会话控制

通过 Codex app-server daemon 启动、复用、追加、打断和等待 Codex 会话。适用于主 Agent 把开发任务委派给本机或远程机器上的 Codex。不要再用 `codex exec` 加 `nohup`/`kill` 的组合：它无法在运行中追加或正常打断，会话来源记为 `exec`，在 `codex resume` 默认列表中不可见。

## 架构

- `codex app-server daemon` 常驻并持有会话和运行中的轮次；`scripts/codex_ctl.py` 的每条命令都是短连接客户端，退出或断线不影响正在执行的轮次。
- 客户端经 `codex app-server proxy` 连接 daemon 控制 socket，socket 上是 WebSocket 帧。Windows 的 Python 不支持 AF_UNIX，因此两个平台都走 proxy 子进程，只依赖 Python 标准库。
- daemon 未运行时脚本会执行一次 `codex app-server daemon start` 再连接。
- 新会话来源为 `vscode`，在 `codex resume` 默认列表中可见，用户可随时进入审阅或接手。

## 命令

```bash
python <skill-dir>/scripts/codex_ctl.py start --cwd <repo> --name <标题> --file prompt.md   # 新会话 + 第一轮
python <skill-dir>/scripts/codex_ctl.py send <thread> --file prompt.md                     # 空闲会话上开新一轮
python <skill-dir>/scripts/codex_ctl.py steer <thread> "补充要求"                           # 运行中追加，下一步采纳
python <skill-dir>/scripts/codex_ctl.py interrupt <thread>                                 # 打断当前轮
python <skill-dir>/scripts/codex_ctl.py wait <thread> --log run.log                         # 等到结束并输出最终回复
python <skill-dir>/scripts/codex_ctl.py status <thread>
python <skill-dir>/scripts/codex_ctl.py list --cwd <repo>
python <skill-dir>/scripts/codex_ctl.py archive <thread>
```

- prompt 按[团队编排](team-orchestration.md)「委派任务包」直接陈述任务；`steer` 和 `send` 也直接写补充或调整的内容，不加“用户叫你做：”“用户追加了命令：”等转述前缀。
- prompt 可用位置参数、`-`（stdin）或 `--file`，位置参数须紧跟在 thread 之后、选项之前；长任务包写入 UTF-8 文件再传 `--file`，避免 shell 引号和编码问题。
- `start`/`send` 默认使用 `approvalPolicy=never` 与 `danger-full-access`，与无人值守委派一致；需要沿用配置中的审批和沙箱时加 `--sandboxed`。
- `start`/`send` 加 `--wait` 等同于随后执行 `wait`。`--json` 输出机器可读结果。
- `send` 遇到运行中的轮次会拒绝；运行中补充信息用 `steer`。
- `-v` 把事件流打到 stderr；`--log` 追加带时间戳的事件日志，用于事后判断耗时卡在哪一步。

`wait` 的退出码：

| 退出码 | 含义 |
|---|---|
| 0 | completed |
| 1 | 连接或调用错误 |
| 2 | failed |
| 3 | interrupted |
| 4 | 上游过载，已自动打断 |
| 5 | `--timeout` 到期，轮次仍在运行 |
| 6 | `--idle` 秒内没有任何会话事件，轮次仍在运行 |

## 选择 Provider

`start --provider <name>` 将 `modelProvider` 传给 `thread/start`，不加时沿用本机 `~/.codex/config.toml` 的 `model_provider`。Provider 在 `config.toml` 的 `[model_providers.<name>]` 中定义，密钥通过 `env_key` 引用环境变量。会话保存自己的 provider，之后 `send`、`steer`、`wait` 直接恢复会话，无需重复选择。

```bash
python <skill-dir>/scripts/codex_ctl.py start --cwd <repo> --name <标题> --provider <name> --model <model> --file prompt.md
```

- 使用非默认 provider 时同时传 `--model`；多个会话共用同一号池或额度时，并行派发容易 429。
- 新增或修改 provider、密钥后，daemon 需要在无活动会话时重启才能读取；运行在该 daemon 内的 owner 不得自行重启，否则会中断自身轮次，由主 Agent 在其交付后重启。
- Codex 没有第三方模型的元数据时，上下文窗口与自动压缩阈值按兜底值处理；模型可能受 Codex 基础指令影响自称 GPT，实际模型以会话 rollout 的 `turn_context` 为准。
- `$CODEX_HOME/<name>.config.toml` 设置 `model_provider` 与 `model` 后，可用 `codex -p <name>` 手动使用，或用 `codex -p <name> resume` 审阅、接手会话。

## 远程机器

在远程机器上执行该机器自己的脚本副本：`ssh <host> python3 <远程 skill-dir>/scripts/codex_ctl.py ...`。远程 skill 加载目录以该机器实际安装位置为准，先用 `ls` 核实，不写死路径。cwd 和 prompt 文件都是远程路径。

## 等待

- 用当前平台的后台执行能力运行 `wait`，完成后读取输出，不要阻塞前台，也不要轮询 `status`。
- 长任务的后台 `wait` 加 `--idle 300 --log run.log` 作看门狗：退出码 6 时读日志末尾判断是卡住（断线重连、等待外部状态、方向不明）还是长命令静默运行，需要时 `steer` 推动，然后重新 `wait`。不用定时任务轮询日志代替。
- `wait` 断线（ssh 断开、会话重启）不影响 Codex；重新执行 `wait <thread>` 即可接上，已结束的轮次会立即返回结果。
- 收到结果后按 [团队编排](team-orchestration.md) 验收；若是停下汇报，按其中「停下汇报」作决定后用 `send` 在同一会话继续；owner 运行中需要补充授权或放宽约束时直接 `steer`，不等本轮结束。

## 上游过载

`wait` 收到 `serverOverloaded` 或消息含 overloaded 的错误时立即打断该轮并以退出码 4 返回。主 Agent 停止调度并向用户汇报，不自动重试，不换号池或模型。用户确认上游恢复后，用 `send` 在同一会话继续，说明上轮在哪一步中断。只有用户明确要求时才加 `--keep-on-overload`。

## 已知限制

- 打断只结束模型轮次，不终止它启动的命令；被打断的部署、构建或长命令会继续运行。打断后检查 daemon 子进程，确认是否需要手动处理。
- daemon 启动时读取 Provider 配置和密钥；更换 Key 或号池后需要 `codex app-server daemon restart`，否则会持续 401 或沿用旧上游。重启会中断运行中的轮次，先用 `list` 确认没有 `active` 会话。
- `thread/turns/list` 读取持久化记录，刚执行 `turn/start` 时可能把新轮次报成 interrupted，新会话可能报 not supported；脚本以会话实时状态判断是否运行中，只对已结束的轮次读取持久化结果。
- 由 `codex exec` 创建的旧会话仍可用 `send` 继续，但来源保持 `exec`，用户需用 `codex resume --include-non-interactive` 才能在列表中看到。需要用户审阅的长期任务，用 `start --name` 新建会话。
