# dshm-local-superagent

[English](README.en.md)

不依赖 GitHub、不依赖任何在线仓库的本地 superagent 骨架。

主会话派单 → 本地队列 → 进程内轮询 → 子会话 worker → 回报。链路里没有服务器、没有外网、没有常驻进程，只有本地 JSON 文件和一个跟随宿主启停的定时器。

## 它是什么

三个 DSH 插件加四个命令行脚本，把「派活给子会话、查记忆、定时跑脚本」放到本机：

1. 主会话调用 `delegate`，任务写成 `inbox/<id>.json`。
2. 插件每 10 秒扫一次 inbox，认领文件，起一个独立子会话当 worker。
3. worker 干完，结果写 `done/<id>.json`，再作为一条消息回到原会话。

队列就是目录，状态就是文件。能 `ls`、能手改、能备份，不需要数据库或消息中间件。

## 三个插件

| 插件 | 工具 | 作用 |
|---|---|---|
| `dshm-local-delegate` | `delegate` / `delegate_status` | 本地派单队列；进程内轮询，起子会话 worker，回报结果 |
| `dshm-local-memory` | `mem_search` / `mem_get` / `mem_store` / `mem_status` | 四个记忆入口；后端是短命 python 进程，单次有硬超时 |
| `dshm-local-jobs` | `jobs_status` / `jobs_run` | 进程内调度器；JSON 任务表、硬超时、补跑、抖动、退避 |

`delegate` 只看本地目录，不连仓库；`memory` 只调本地脚本，不访问网络；`jobs` 用 `spawn` 跑(argv 数组，不过 shell)，到点杀整个进程组。

## 安装

插件按 DSH 的 profile 布局放置：目录名即包名，宿主从 `profiles/ondevice/node_modules` 解析。

```bash
export DSH_HOME=<宿主的 home 目录>
export CORE=<DSH 安装目录>

for p in delegate memory jobs; do
  dest="$DSH_HOME/profiles/ondevice/node_modules/dshm-local-$p"
  cp -r "plugins/$p" "$dest"
  mkdir -p "$dest/node_modules/@deepseek-ai"
  ln -s "$CORE/node_modules/@deepseek-ai/dsh-tools" \
        "$dest/node_modules/@deepseek-ai/dsh-tools"
done
```

在 `$DSH_HOME/cordis.patch.yml` 里登记（home 层）：

```yaml
- insert:
    - id: dshm-local-delegate
      name: dshm-local-delegate
    - id: dshm-local-memory
      name: dshm-local-memory
    - id: dshm-local-jobs
      name: dshm-local-jobs
```

重启宿主，工具面出现 `delegate`、`mem_search`、`jobs_status` 即装好。装不上就先删这段 patch 再启动，宿主不受影响。

`memory` 插件只是适配层，后端是仓库里的 `tools/mem.py`：纯标准库，约定 `search|get|store|status|reindex` 子命令。默认从 `$LOCAL_SUPERAGENT_HOME/memory/mem.py` 加载，可用 `LOCAL_MEMORY_PY` 改路径。

## 配置

全部通过环境变量，默认值落在 `~/.local-superagent/`：

| 变量 | 默认 | 用途 |
|---|---|---|
| `LOCAL_SUPERAGENT_HOME` | `~/.local-superagent` | 数据根目录 |
| `LOCAL_SUPERAGENT_QUEUE` | `$HOME/delegate` | 派单队列 |
| `LOCAL_SUPERAGENT_WORKSPACE` | `$HOME` | worker / 任务的默认工作目录 |
| `LOCAL_SUPERAGENT_POLL_MS` | `10000` | 队列轮询间隔 |
| `LOCAL_SUPERAGENT_WORKER_TIMEOUT_MS` | `1800000` | 单个 worker 的超时 |
| `LOCAL_SUPERAGENT_JOBS_DIR` | `$HOME/jobs` | 任务表与运行流水 |
| `LOCAL_SUPERAGENT_TICK_MS` | `60000` | 调度器节拍 |
| `MEM_HOME` | 无（默认 `$HOME/memory`） | 记忆数据根目录，覆盖默认位置 |
| `LOCAL_MEMORY_PY` | `$HOME/memory/mem.py` | 记忆后端路径 |
| `LOCAL_SUPERAGENT_PYTHON` | `python3` | 解释器 |
| `LOCAL_SUPERAGENT_ROUTINE` | `$HOME/routine.json` | 日常步骤表 |
| `LOCAL_SUPERAGENT_SESSION_ID` | 无 | CLI 派单时的回报会话 |

`tools/delegate.py` 是同一队列的命令行入口，`tools/daily.py` 按 `routine.json` 顺序跑步骤。两者的参数、配置格式和退出码写进 [docs/architecture.md](docs/architecture.md)。

## 记忆系统

本地 SQLite + FTS5：索引在 `index.db`，条目是两层 markdown（`flow/` 流水、`canon/` 正本）。中文按三字切片检索，两字走 `LIKE` 兜底；断网照查，不需要模型，也不需要向量库。

四个工具和派单是一套：worker 看不到主会话的上下文，任务里要它查旧账，就让它调 `mem_search`；干完用 `mem_store` 写一条续点。worker 和主会话读同一个库，不经过网络。

装的时候把后端放到插件默认位置，再建一次索引：

```bash
SUPERAGENT="${LOCAL_SUPERAGENT_HOME:-$HOME/.local-superagent}"
mkdir -p "$SUPERAGENT/memory"
cp tools/mem.py "$SUPERAGENT/memory/mem.py"
python3 "$SUPERAGENT/memory/mem.py" reindex
```

不想放默认位置，安装时用 `LOCAL_MEMORY_PY=/path/to/mem.py` 指定后端。数据默认在 `$SUPERAGENT/memory/`，可用 `MEM_HOME` 覆盖。设计细节见 [docs/memory.md](docs/memory.md)。

## 为什么本地

断网可用。没有 API、没有仓库、没有 webhook，拔了网线照跑。

数据不出机器。任务原文、worker 结果、记忆、运行流水都是本地文件。

随宿主启停。没有 systemd、没有守护进程；宿主停了，队列留在磁盘，下次起来接着跑。

可审计。每次运行追加 `runs.jsonl`，任务原文可以直接 `grep`。

## 提示词

只装插件不够用：没有提示词，主会话会自己动手，`python`、`terminal` 也还能绕过只读限制。要让「主会话只查、判、派」成立，得把这段规矩写进 `AGENTS.md`。原文和实测边界见 [docs/prompt-rules.md](docs/prompt-rules.md)。

## 已知限制

单机。队列是本地目录，多台机器不共享，也没有远端副本。

无多租户。一个宿主一个队列，不区分调用者。

无权限层。能读写队列目录就能改任务，靠文件权限兜底。

串行。调度器一拍只跑一条；delegate 的 worker 深度受宿主 `maxActiveSubagents` 限制。

不自动重试派单。失败只记录原因，重派要人再发一次。

记忆是单机单文件。跨机器不同步；手改或批量导入文件后，要跑一次 `reindex` 才对齐。

## 目录

```
dshm-local-superagent/
├── README.md         中文
├── README.en.md      英文
├── LICENSE           MIT
├── examples/
│   ├── e2e.md        端到端验证步骤
│   └── inbox.example.json
├── plugins/
│   ├── delegate/    index.js + package.json
│   ├── memory/      index.js + package.json
│   └── jobs/        index.js + package.json
├── tools/
│   ├── delegate.py
│   ├── daily.py
│   ├── mem.py
│   └── persona.py
├── docs/
│   ├── architecture.md
│   ├── memory.md
│   └── prompt-rules.md
└── .gitignore
```

## 许可

MIT，见 [LICENSE](LICENSE)。
