# dshm-local-superagent

DSHM（DeepSeek Harness，跑在鸿蒙 PC 上）的端侧增强层。

它把一次对话拆成两层：主会话只负责查、判、派，改文件、跑命令、装东西交给子会话或本地派单队列。外层是七个 DSHM 插件，负责把记忆、调度、守卫、素材收集接进宿主进程；内层是四个 Python 工具，管记忆库、向量、人格和每日整理。全部跑在一台机器上，账本放在 `~/Documents/小敏`。

## 仓库内容

```
plugins/   七个 DSHM 插件，每个只有 index.js 和 package.json
tools/     四个 Python 工具：mem.py / embed.py / persona.py / dream.py
docs/      architecture.md / memory.md / prompt-rules.md / field-notes.md
examples/  派单队列的走查和示例（历史文档，保留原样）
CHANGES.md 这一版和上一版的差别
```

**目录名和插件名不一样。** `plugins/memory` 里加载的插件名仍然是 `xiaomin-memory`，其余六个同理。目录用短名是为了在 `node_modules/` 里一眼认出来；插件名是运行时身份，七个都以 `xiaomin-` 开头，`cordis.patch.yml` 里写的是插件名，不是目录名。

## 七个插件

| 目录 | 插件名 | 注入的服务 | 干什么 |
|---|---|---|---|
| `plugins/memory` | `xiaomin-memory` | `tools` | 注册 `mem_search` / `mem_get` / `mem_store` / `mem_status` 四个工具，每次调用起一个 15 秒硬超时的子进程去跑 `mem.py` |
| `plugins/jobs` | `xiaomin-jobs` | `tools`、`timer` | 进程内调度器，60 秒一拍，任务表在 `运行/jobs.json`；给工具 `jobs_status`、`jobs_run` |
| `plugins/delegate` | `xiaomin-delegate` | `tools`、`timer`、`agents`、`subagents` | 本地派单：任务落 `运行/派单/inbox`，插件每 10 秒轮一次，起一个子会话干活，干完把结果推回本会话；给工具 `delegate`、`delegate_status` |
| `plugins/readonly-preset` | `xiaomin-readonly-preset` | `agentPresets` | 在运行时注册「只读主会话」preset：写和编辑工具关掉，保留子会话工具和压缩 |
| `plugins/ctx-guard` | `xiaomin-ctx-guard` | `tools`、`agents`、`sessionProjections`、`timer` | 上下文压力到 60% 时往会话推一条提醒，每个会话只推一次 |
| `plugins/main-guard` | `xiaomin-main-guard` | `tools` | 顶层会话调七个动手工具直接拒；带 `parentSession` 或 `origin=subagent` 的子会话放行 |
| `plugins/sentinel` | `xiaomin-sentinel` | 无 | 每个回合把用户首句、助手结尾、工具调用和涉及文件攒成一行，追加到 `运行/待记/YYYYMMDD.jsonl` |

## 料流水线

```
对话 ──sentinel──▶ 运行/待记/YYYYMMDD.jsonl ──dream 04:10──▶ 日报 + 索引 + 向量 + 运行/health.json
```

哨兵每回合落一行素材，单文件超过 512 KB 滚到 `-2`，原始 jsonl 只读不删。每天 04:10 调度器跑 `dream.py`：体检、重建索引、消费待记（读到第几行记在 `.consumed.json`）、补向量、写健康表。日报落在 `运行/待记/日报-YYYYMMDD.md`，健康表是后台是否正常的唯一凭据，`mem_status` 末尾会带一行。

## 怎么装

前置：DSHM 0.2.x 端侧宿主，Node 由宿主内置，Python 3.12 用宿主自带的那份（`/data/storage/el2/base/haps/entry/files/bin/python3`）。

1. 把七个目录放进当前 profile 的 `node_modules`，目录名保留下面的短名：

   ```
   $DSH_HOME/profiles/ondevice/node_modules/memory
   $DSH_HOME/profiles/ondevice/node_modules/jobs
   $DSH_HOME/profiles/ondevice/node_modules/delegate
   $DSH_HOME/profiles/ondevice/node_modules/readonly-preset
   $DSH_HOME/profiles/ondevice/node_modules/ctx-guard
   $DSH_HOME/profiles/ondevice/node_modules/main-guard
   $DSH_HOME/profiles/ondevice/node_modules/sentinel
   ```

   目录名可以换，`cordis.patch.yml` 里的 `name` 必须是插件名。插件依赖宿主提供的 `@deepseek-ai/dsh-tools`，不用自己装。

2. 在 `$DSH_HOME/cordis.patch.yml` 里插入七行：

   ```yaml
   - insert:
       - id: xiaomin-memory
         name: xiaomin-memory
       - id: xiaomin-jobs
         name: xiaomin-jobs
       - id: xiaomin-delegate
         name: xiaomin-delegate
       - id: xiaomin-readonly-preset
         name: xiaomin-readonly-preset
       - id: xiaomin-ctx-guard
         name: xiaomin-ctx-guard
       - id: xiaomin-main-guard
         name: xiaomin-main-guard
       - id: xiaomin-sentinel
         name: xiaomin-sentinel
   ```

3. 重启宿主。插件启动路径没有副作用：不 spawn、不探端口、不写文件，起不来只影响自己。

4. 把 `tools/` 放到 `$XIAOMIN_MEM/工具/`（默认 `~/Documents/小敏/记忆机/工具/`），确保 `mem.py` 可执行。`plugins/memory/index.js` 里写死了 `mem.py` 和 `python3` 的绝对路径，换机器要改那两行。

5. 第一次跑 `python3 tools/mem.py verify`，再跑 `python3 tools/dream.py`，看 `运行/health.json` 的 `ok`。

## 配置

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `XIAOMIN_MEM` | 调度器和插件认的记忆机根目录 | `~/Documents/小敏/记忆机` |
| `XIAOMIN_TICK_MS` | 调度器 tick 间隔（毫秒） | `60000` |
| `MEM_HOME` | `mem.py` 的根目录 | 同上 |
| `MEM_TIMEOUT` | 单个记忆动作的硬超时（秒） | `8` |
| `MEM_VERIFY_TIMEOUT` | `verify` 的超时（秒） | `120` |
| `MEM_BACKUP_TIMEOUT` | 备份的超时（秒） | `300` |
| `DSH_HOME` | 守卫找 `main-guard.off` 的目录，也是会话目录 | `/data/storage/el2/base/haps/entry/files/dsh/home` |
| `DSHM_BAILIAN_KEY` | 百炼 key 文件路径 | `~/Documents/小敏/凭证/百炼.key` |
| `DSHM_BAILIAN_URL` | 百炼 embeddings 接口 | `https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings` |
| `DSHM_BAILIAN_MODEL` | 嵌入模型 | `text-embedding-v3` |
| `DSHM_CA_FILE` | 端侧 CPython 的 CA 证书文件 | `/etc/ssl/certs/cacert.pem` |

## 已知限制

单机。所有路径按 `~/Documents/小敏` 写死，没有多租户，没有按用户隔离，换机器要改路径。

git 端侧不可用。要推仓库只能走 GitHub API 或 `gh` CLI，仓库操作别指望本地 git。

依赖百炼。检索的向量只有百炼一条通道，断网、没 key、超时都退纯 FTS，返回 `mode=fts` 和 `degraded_reason`，能查但语义召回会差一截。

记忆库不是无状态服务。`mem.py` 用 sqlite + flock，同一台机器上并发写靠文件锁串行化，跨机器共享目录会坏。

急救包里有凭证。`急救.py pack` 会把 `凭证/`、`.credentials.yaml`、pem 一起打进去，那个包不能进仓库、不能外发。

插件里写死的路径和本地约定（`运行/jobs.json`、`运行/派单`、`运行/待记`）是配套的，只装插件不铺目录，调度器会自己建目录，但任务表只有默认两条（体检、日备份），要 12 条得自己写 `jobs.json`。

`ctx-guard` 的 `package.json` 描述还写着 70%，实际阈值是 60%，以代码为准。

没有测试，没有 CI。改完只能在本机跑一遍 `verify` 和 `dream.py` 看 `health.json`。

## 来源

代码从正在运行的那份 `profiles/ondevice/node_modules` 直接拷出，只保留 `index.js` 和 `package.json`，逻辑没动。`tools/` 四个文件按原样收进来，没有改写。
