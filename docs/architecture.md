# Architecture

本地 superagent 的运行骨架：派单、调度、记忆三件事都不离开本机。

## 1. 目标与边界

目标是把「主会话派活 → 后台执行 → 结果回来」做成一个可审计的本地闭环。边界同样明确：

- 不依赖在线仓库、消息队列、数据库服务。
- 不引入常驻进程；所有定时行为挂在宿主进程内。
- 每个子进程调用都有硬超时，每个写入都可重放。
- 单机、单用户、文件权限即权限模型。

## 2. 三层结构

```
会话层        main session ── delegate tool / CLI ──┐
                                                  │ 写 ticket
队列层        $HOME/delegate/inbox/<id>.json      │
                    │  poll (in-process timer)     │
                    ▼                              │
执行层        child agent session (worker) ────────┘
                    │
                    ▼
回报层        done/<id>.json + followup message
```

三样东西各自独立：

- **delegate** 管「谁在什么时候干什么」，状态在文件里。
- **jobs** 管「隔多久跑一次脚本」，状态在 `jobs.json` 和 `runs.jsonl`。
- **memory** 管「记什么、怎么取」，状态在后端自己的库里。

插件之间不互相调用，只共享 `LOCAL_SUPERAGENT_HOME` 这个根目录。

## 3. Source → Ticket → Worker → Report 的本地映射

云端队列常见的四段式是 Source → Ticket → Worker → Report。本地版本逐段换成文件与进程内对象：

| 段 | 云端做法 | 本地做法 |
|---|---|---|
| Source | webhook / API 请求 | 会话里的 `delegate` 工具调用，或 `tools/delegate.py` |
| Ticket | 队列服务里的一条消息 | `inbox/<id>.json`，一个任务一个文件 |
| Worker | 独立容器 / 消费者 | `ctx.subagents.startActivation` 起的子会话 |
| Report | 回调 / 轮询结果接口 | `done/<id>.json` + `agent.followup()` 消息 |

### Ticket 字段

```json
{
  "id": "d-<base36 time>-<8 hex>",
  "title": "short title",
  "task": "full task text",
  "cwd": "/absolute/worker/dir",
  "sessionId": "session that should receive the report",
  "createdAt": "2026-01-01T12:00:00.000Z"
}
```

`task` 必须自包含：worker 看不到主会话的上下文，这是隔离的代价，也是不污染主上下文的收益。

### 状态迁移

```
inbox/<id>.json
   │ rename（认领，原子）
   ▼
inbox/<id>.json.running
   │ worker 结束
   ▼
done/<id>.json        结果全文，含 ok / reason / output / finishedAt
done/<id>.json.taken  认领占位，可清理
```

JSON 解析失败的文件改名成 `done/<id>.json.bad`，不阻塞后续任务。

认领用 `rename` 而不是加锁：同一文件系统内 rename 是原子的，两个轮询不可能同时拿到同一张单。

### Report 的可靠性

worker 结束后先写 `done/<id>.json`（`notified: false`），再尝试把摘要塞回原会话：

- 原会话还活着且 `status === 'idle'`：`followup` 成功，标记 `notified: true`。
- 会话忙或已结束：本轮不发，下一拍再试；记录一直保留。
- 结果全文留在 JSON 里，消息只带 400 字摘要和文件路径，避免把大结果灌回上下文。

## 4. 队列轮询

```
每 LOCAL_SUPERAGENT_POLL_MS（默认 10s）：
  ensure dirs
  for each inbox/*.json:
      parse; bad -> done/*.bad
      rename to .running          # 认领
      startActivation(...)         # 不 await，继续下一张
  notifyPending()                  # 扫 done，补发未通知的
```

要点：

- 轮询是 `ctx.setInterval`，作用域跟随插件，插件卸载即清。
- `startActivation` 带 `delivery: 'parent'` 和 `AbortSignal.timeout`，worker 有上限时长。
- 每张单独立跑，互不阻塞；并发上限由宿主的 `maxActiveSubagents` 决定。
- 宿主在 worker 跑一半时被杀，`.running` 文件会留在 inbox，重启后不会自动重认领。这是有意的取舍：宁可不重复执行，也不猜任务是否已经产生副作用。恢复方式是人工把文件改回 `.json`。

## 5. 进程内调度器

任务表是纯 JSON，人手可改，按 mtime 热重载：

```json
{
  "jobs": [
    {
      "id": "example",
      "title": "Example job",
      "enabled": false,
      "every": 3600,
      "timeout": 60,
      "cmd": ["python3", "/path/to/script.py"],
      "okExit": [0, 1],
      "cwd": "/path/to/workdir"
    }
  ]
}
```

字段：

- `every`：间隔秒数；`at`：每天定点 `HH:MM`（本地时间），二选一。
- `timeout`：硬超时，到点对进程组发 `SIGKILL`。
- `cmd`：argv 数组，不经过 shell。
- `okExit`：哪些退出码算成功，默认 `[0]`。健康检查类任务把「有告警」编码成 1 时用得上。
- `cwd`：工作目录，缺省用 `LOCAL_SUPERAGENT_WORKSPACE`。

一拍只跑一条到期任务，规则对齐 systemd timer 的常用语义：

1. **补跑一次**：`next_run` 已过但没超过 2× 间隔，立即跑；超过就只顺延，不补一串陈年任务。
2. **抖动**：算下次时间加 ±10%，避免整点一起醒。
3. **防重叠**：同 id 在 `running` 集合里就跳过本拍。
4. **退避**：连续失败 3 次起，间隔 ×2、×4……上限 6 小时；成功一次清零。
5. **心跳**：每次运行追加 `runs.jsonl`（开始时间、id、reason、status、耗时、rc、输出尾部）。

`jobs_status` 把「超过 2× 间隔没成功」标成 `WARN: overdue`；`at` 任务按 24 小时判，不按小时任务的 2 倍判。

## 6. 记忆工具层

四个工具只是薄适配层，真正的存储在 `mem.py` 后端：

- `mem_search`：FTS5 检索，最多 5 条、2 KB。
- `mem_get`：取单件原文，最多 8 KB，超出截断并提示。
- `mem_store`：追加一条，先写临时文件再 rename，索引同事务更新。
- `mem_status`：件数、漂移、最后写入、库大小。

约束：

- `apply()` 只注册工具，不 spawn、不探端口、不写文件。
- 每次调用起一个短命进程，15 秒硬超时，到点 `SIGKILL`。
- 体积上限在协议里，不靠模型自觉。
- 后端只要求 Python 标准库；路径由 `LOCAL_MEMORY_PY` 指定。

## 7. 日常编排

`daily.py` 按 `routine.json` 顺序跑步骤，把「一串维护脚本」收敛成一个退出码：

```json
{
  "steps": [
    {"name": "check", "cmd": ["python3", "/path/check.py"], "timeout": 240, "okExit": [0, 1]},
    {"name": "report", "cmd": ["python3", "/path/report.py"], "timeout": 180, "at": "22:30"}
  ]
}
```

- 没有 `at` 的步骤每次都跑；有 `at` 的步骤只在当天该时间之后跑一次，标记写在 `state/daily.json`。
- `--dry-run` 跳过所有带 `at` 的步骤；`--force-daily [YYYYMMDD]` 强制跑一次。
- 退出码：0 全正常；1 有步骤返回了非 0 但属于 `okExit`（告警）；2 有失败或超时。
- 超时用 `Popen(start_new_session=True)` + `killpg`，连同孙进程一起杀。
- 每次调用追加 `state/daily.jsonl`。

## 8. 设计约束

1. 启动路径零副作用：插件 `apply()` 不 spawn、不探端口、不写文件（队列目录的创建发生在首次工具调用或首拍）。
2. 任何子进程都有超时，超时杀组。
3. 任何写入先写 `.tmp` 再 rename。
4. 任何读取都有上限（输出、结果、日志尾部）。
5. 不用 shell 拼命令，`cmd` 一律是数组。
6. 插件异常不向宿主抛：轮询和调度都包在 try/catch 里。
7. 状态文件坏了就改名隔离，用默认值继续，不阻断启动。

## 9. 与云端队列的差别

| 维度 | 云端队列 | 本项目 |
|---|---|---|
| 可用性 | 多副本、跨机器 | 单机，宿主停即停 |
| 投递语义 | at-least-once / exactly-once | 文件 rename，单次认领 |
| 扩缩容 | 消费者池 | 宿主 `maxActiveSubagents` |
| 权限 | IAM / token | 文件系统权限 |
| 观测 | 指标、追踪 | `runs.jsonl`、`done/*.json` |
| 成本 | 按量 | 零额外服务 |

## 10. 已知限制

- 没有跨机器同步，也没有远端备份。
- 没有多租户和配额。
- 没有权限层；能写队列目录就能派单。
- 派单失败不自动重试；调度任务有退避但没有死信队列。
- worker 串行深度受宿主限制，不能靠加机器扩。
- 宿主被杀时遗留的 `.running` 需要人工回收。
