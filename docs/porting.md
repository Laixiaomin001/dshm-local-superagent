# 移植清单：换一台机器

目标：在新机器上把这套架构装起来，跑通一轮，之后还能把改动回填仓库。所有路径都从四个环境变量推出来，仓库里不带任何机器相关的绝对路径。

装完后的日常更新走 `tools/sync-repo.sh`：把实时版拷回仓库、脱敏、自检、本地提交。它不推送。

## 1. 前提

| 项 | 要求 | 怎么查 |
| --- | --- | --- |
| DSH | 0.2.2-rc.1 这一代（0.2.x 的 cordis 插件 API） | `dsh --version` |
| Node | 24+（DSH 0.2.2-rc.1 运行时自带 24.22） | `node -v` |
| Python | 3.10 以上，标准库带 SQLite FTS5 | `python3 -V`；`python3 -c "import sqlite3; sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)')"` |
| 系统 | Linux。`statfs`、`rename` 和 Android proot 的行为都按 Linux 假设 | `uname -s` |

DSH 版本不对时不要硬装：插件用的 `tools`、`tokenMeter`、`agents`、`subagents` 服务形状可能已经变了，先对照 `docs/architecture.md` 改插件。

profile 的目录结构：

```
$DSH_HOME/
├── AGENTS.md                    主会话运行规则，doctor 认「## 记忆与账本」这个标题
├── profiles/
│   └── <profile>/
│       ├── package.json         依赖和 dsh.profile.bundles
│       ├── cordis.patch.yml     这个 profile 的补丁层
│       └── node_modules/        插件依赖，装完才有
├── sessions/                    宿主写的会话日志，按工作目录分层
├── storages/
│   └── schedule.json            宿主管，不要手改
├── exports/                     本机快照
└── agent-core/                  账本和工具，默认 CORE_HOME
```

## 2. 环境变量

| 变量 | 指什么 | 默认 |
| --- | --- | --- |
| `DSH_HOME` | DSH 的根目录：`AGENTS.md`、`profiles/`、`sessions/`、`storages/` 都在它下面 | `$HOME/.dsh` |
| `CORE_HOME` | 账本根：`memory/`、`运行/`、`queue/`、`bin/`、`health.json` | `$DSH_HOME/agent-core` |
| `DSH_PLUGIN_DIR` | 插件安装根。`install.sh` 把三个插件放到 `$DSH_PLUGIN_DIR/<name>-plugin/`；`doctor` 第九项也从这里找 | `$DSH_HOME/plugins` |
| `DSH_WORKSPACE` | 主会话的工作目录；`guard` 拿它当白名单基准，`delegate` 拿它当子会话默认 cwd | 不设时：`guard` 取 `$HOME/workspace`，`delegate` 取进程 cwd |

安装时传：

```bash
export DSH_HOME="$HOME/.dsh"
export CORE_HOME="$DSH_HOME/agent-core"
export DSH_PLUGIN_DIR="$DSH_HOME/plugins"
export DSH_WORKSPACE="$HOME/workspace"

bash install.sh \
  --dsh-home "$DSH_HOME" \
  --core-home "$CORE_HOME" \
  --plugin-dir "$DSH_PLUGIN_DIR" \
  --workspace "$DSH_WORKSPACE"
```

`install.sh` 也认同名环境变量；命令行选项优先。变量只对当前 shell 和它拉起的进程有效，要长期生效就写进 shell 启动文件或 systemd 单元，别写进仓库。

`delegate` 插件内部把队列写死在 `$DSH_HOME/agent-core/queue`，`queue.py` 也按 `$DSH_HOME/agent-core/bin/queue.py` 找，所以 `CORE_HOME` 保持默认关系最省事。确实要拆开时，改 `plugins/delegate/index.js` 里的 `ROOT` 和 `QUEUE_PY`，并同时给 `queue.py`、`dispatch.py`、`ledger.py` 设同一个 `CORE_HOME`。

## 3. 要建的目录

`install.sh` 会建好下面这些；手工装就照这张表补。

| 目录 | 放什么 |
| --- | --- |
| `$CORE_HOME/bin/` | `tools/*.py` 和 `unzstd.mjs`，`chmod +x` 后由 guard 白名单调用 |
| `$CORE_HOME/memory/` | `常驻.md`（`recall.py` 生成）、`流水/`、`待记/`、`日报/`、`事实.jsonl`、`向量.jsonl`、`索引.db`、`状态.json` |
| `$CORE_HOME/运行/` | `guard.jsonl`、`派单台账/`、`待审/`、`手册/`、`计划/`、`报告/`、`图谱.md`、`教训.md`、`dream.log`、`口吻自检.jsonl` |
| `$CORE_HOME/运行/手册/` | 部署手册，`整理.md` 必须有 `## dream 十三步`，doctor 第九项会数步骤 |
| `$CORE_HOME/运行/待审/` | 每天整理出来的候选，等人判 |
| `$CORE_HOME/queue/` | `inbox/`、`running/`、`done/`、`stuck/` 四个目录，插件和命令行共用 |
| `$DSH_HOME/exports/` | 本机快照和导出；`core.py doctor` 第七项查这里 8 天内有没有 tar |
| `$DSH_HOME/plugins/` 或 `$DSH_PLUGIN_DIR/` | 三个插件，目录名 `<name>-plugin` |

```bash
mkdir -p \
  "$CORE_HOME/bin" \
  "$CORE_HOME/memory/流水" "$CORE_HOME/memory/待记" "$CORE_HOME/memory/日报" \
  "$CORE_HOME/运行/手册" "$CORE_HOME/运行/待审" "$CORE_HOME/运行/计划" \
  "$CORE_HOME/运行/报告" "$CORE_HOME/运行/派单台账" \
  "$CORE_HOME/queue/inbox" "$CORE_HOME/queue/running" \
  "$CORE_HOME/queue/done" "$CORE_HOME/queue/stuck" \
  "$DSH_HOME/exports" "$DSH_PLUGIN_DIR"
```

`charter.md`、`identity.md`、`policy.json` 放在 `$CORE_HOME/` 根下，是 doctor 第一项。它们写的是你自己的边界和身份，仓库不带，装完自己填。`policy.json` 里的保护路径和可写根也要改成新机器的实际路径。

运行规则追加到 `$DSH_HOME/AGENTS.md`：

```bash
cat docs/prompt-rules.md >> "$DSH_HOME/AGENTS.md"
```

已经追加过就别重复；`doctor` 第二项只认 `## 记忆与账本` 这个标题。

## 4. 插件怎么挂

三个插件：`guard`、`ctx-guard`、`delegate`。安装脚本只负责把文件放到 `$DSH_PLUGIN_DIR/<name>-plugin/`，profile 要手工改。改 profile 有把宿主弄挂的风险，脚本不自动动它。

先把依赖写进 `$DSH_HOME/profiles/<profile>/package.json`：

```bash
dsh plugin --profile <profile> add \
  file:"$DSH_PLUGIN_DIR/guard-plugin" \
  file:"$DSH_PLUGIN_DIR/ctx-guard-plugin" \
  file:"$DSH_PLUGIN_DIR/delegate-plugin"
```

等价的 JSON 片段：

```json
{
  "dependencies": {
    "dsh-guard": "file:/ABS/PATH/plugins/guard-plugin",
    "dsh-ctx-guard": "file:/ABS/PATH/plugins/ctx-guard-plugin",
    "dsh-delegate": "file:/ABS/PATH/plugins/delegate-plugin"
  }
}
```

### 方式 A：`dsh.profile.bundles`

`guard`、`ctx-guard` 的 `package.json` 里有 `dsh.bundle.patch`，写进 bundles 后它们的补丁会自己插入。`delegate` 没有 bundle patch，仍然要在 `cordis.patch.yml` 里 insert。

```json
{
  "dsh": {
    "profile": {
      "bundles": [
        "dsh-guard",
        "dsh-ctx-guard"
      ]
    }
  }
}
```

```yaml
# $DSH_HOME/profiles/<profile>/cordis.patch.yml
- insert:
    - id: delegate
      name: dsh-delegate
```

### 方式 B：`cordis.patch.yml` 的 insert

三个都写进补丁层，bundles 里一个都不加：

```yaml
# $DSH_HOME/profiles/<profile>/cordis.patch.yml
- insert:
    - id: guard
      name: dsh-guard
    - id: ctx-guard
      name: dsh-ctx-guard
    - id: delegate
      name: dsh-delegate
```

两种方式只能选一种。`guard`、`ctx-guard` 同时出现在 bundles 和 insert 里会加载两次，第一个把它拦下来或第二个报重复 id。

`readonly-preset` 不在三个插件里，需要只读主会话时再单独加。它没有 `dsh.bundle.patch`，所以依赖和补丁层两样都要写：在仓库根执行

```bash
dsh plugin --profile <profile> add file:"$PWD/plugins/readonly-preset"
```

再在 `cordis.patch.yml` 里 insert：

```yaml
- insert:
    - id: readonly-preset
      name: dsh-readonly-preset
```

## 5. 必须改的机器相关项

| 位置 | 常量 | 默认/行为 | 换机时怎么改 |
| --- | --- | --- | --- |
| `plugins/guard/index.js` | `coreHome()`、`DOC_DIRS`、`SCRIPTS`、`ledgerPath()` | 可写目录是 `$CORE_HOME/运行` 和 `/tmp`；命令白名单是一串脚本名 | 运行目录不叫 `运行` 时改 `DOC_DIRS`；新增记账脚本要同时改 `SCRIPTS`，否则 guard 会拦 |
| `plugins/ctx-guard/index.js` | `DSH_HOME`、`CORE_HOME`、`SESSIONS_DIR`、`HEALTH_PATH`、`PROJCACHE_DIR`、`PURGE_ROOT` | 会话目录默认 `$DSH_HOME/sessions/--root-workspace--`，宿主按工作目录再加一层；health 在 `$CORE_HOME/health.json` | 工作目录变了就设 `DSH_SESSIONS_DIR` 指向新的 session 根；`PURGE_ROOT` 默认 `/tmp`，要换暂存盘就改它 |
| `plugins/delegate/index.js` | `DSH_HOME`、`ROOT`、`QUEUE_PY`、`DEFAULT_CWD` | 队列固定 `$DSH_HOME/agent-core/queue`；`QUEUE_PY` 固定 `$DSH_HOME/agent-core/bin/queue.py`；cwd 取 `DSH_WORKSPACE` 或进程 cwd | 拆开 `CORE_HOME` 时改 `ROOT` 和 `QUEUE_PY`，并让命令行工具用同一个 `CORE_HOME` |
| `tools/recall.py` | `HOME`、`MEM`、`RUN`、`OUT`、`USER_SUBJECTS` | 账本根取 `CORE_HOME`；读 `memory/事实.jsonl`、`运行/计划`、`运行/教训.md`，写 `memory/常驻.md` | 不用改路径。要认自己的主体名就设 `USER_SUBJECTS`，逗号分隔 |
| `tools/core.py` | `DSH_HOME`、`HOME`、`PLUGIN_HOME`、`EXPORT_ROOT` | 快照默认 `$DSH_HOME/exports`；doctor 读 `$DSH_HOME/AGENTS.md`、`storages/schedule.json` | 快照要放别处就设 `CORE_EXPORT`；插件不在 `$DSH_HOME/plugins` 就设 `DSH_PLUGIN_DIR` |
| `tools/ledger.py` | `HOME`、`LEDGER_DIR` | 派单台账在 `$CORE_HOME/运行/派单台账/YYYYMMDD.jsonl` | 跟着 `CORE_HOME` 走，一般不用改 |
| `tools/voice.py` | `DSH_HOME`、`HOST`、`KEY`、`OUT` | 用 SSH 到远端本机的 `127.0.0.1:3122` 取 TTS；`HOST` 读 `VOICE_SSH_HOST`，`KEY` 读 `VOICE_SSH_KEY`（不填走 ssh-agent） | 设 `VOICE_SSH_HOST=user@host`；私钥没进 ssh-agent 就设 `VOICE_SSH_KEY`；产物默认 `$DSH_HOME/exports/voice`，要换设 `VOICE_OUT` |

`core.py doctor` 第九项同时认 `<name>` 和 `<name>-plugin` 两种插件目录名，装到哪种都能查到。

## 6. 装完怎么验

先初始化，再体检：

```bash
python3 "$CORE_HOME/bin/core.py" init
python3 "$CORE_HOME/bin/core.py" doctor
```

九项固定是：宪章/身份/规则、AGENTS 挂钩、健康表 <36h、待记积压 <200、卡住工单 =0、账本对账、本机快照、心声断档、图谱一致性。前七项不过算失败，第八、九项只亮黄。新 home 上前三项和第七项会红，因为 charter、AGENTS、health、快照还没建；这不是装坏了。

重建常驻页：

```bash
python3 "$CORE_HOME/bin/recall.py" build
python3 "$CORE_HOME/bin/recall.py" show | head -40
```

手工跑一次整理：

```bash
python3 "$CORE_HOME/bin/core.py" dream
python3 -c 'import json,os,pathlib; h=json.loads((pathlib.Path(os.environ["CORE_HOME"])/"health.json").read_text()); print(h["at"], h["ok"])'
```

`dream` 可能因为 doctor 那一项返回非 0，看 `health.json` 的 `steps` 哪一步红，不要只看退出码。`at` 是今天，才说明这一轮真跑了。

确认插件已加载，CLI：

```bash
dsh --profile <profile> --dump-config | grep -E 'dsh-(guard|ctx-guard|delegate)'
```

应该看到三个包名。App 或 Web 界面里打开插件面板，同样能看到它们；再开一个新会话，让它改一个 `运行/` 之外的文件，主会话应该拒绝并改走派单。

最后把 `运行/图谱.md`、`运行/手册/整理.md` 补上，再建至少两条 `target=new` 的定时任务（到 `$DSH_HOME/storages/schedule.json` 里成 active），跑一次 `doctor`，第九项才会转绿。

## 7. 换机要带走什么

带：

- `$CORE_HOME/memory/`：流水、事实卡、待记、日报、`常驻.md`。`索引.db` 和 `向量.jsonl` 是派生物，可以一起带，丢了也能重建。
- `$CORE_HOME/运行/`：手册、图谱、计划、教训、派单台账。
- `$DSH_HOME/AGENTS.md`：主会话规则正本。
- `$CORE_HOME/queue/` 里还没跑完的单，按需带。

不带：

- 任何凭证、私钥、API key、`.credentials.yaml`。
- `$DSH_HOME/private/`、`profiles/*/node_modules/`、宿主自己管的 `storages/schedule.json`。
- `sessions/` 原文。需要复盘就单独加密带走，不要混进账本包。

打包：

```bash
tar -czf /tmp/agent-core-move.tgz \
  -C "$CORE_HOME" memory 运行 \
  -C "$DSH_HOME" AGENTS.md
```

到新机器解开，设好四个环境变量，跑 `install.sh`，再按上一节验一遍。

## 8. 已知坑

- 定时任务一律 `target=new`。绑固定会话的，那个会话归档或删除，任务会跟着消失。到点没提醒先查这一条。
- 引擎没活动时，定时任务到点也不会真跑；实际是当天第一次有活动时补跑。跑没跑只看 `health.json` 的 `at`，不是今天就是没跑。
- `doctor` 第九项在没图谱、没手册、没有两条 active 定时任务的新装环境是黄灯，不阻断。补上 `运行/图谱.md`、`运行/手册/整理.md` 和定时任务再跑一次；插件目录和 `tone.js` 也在这一项里查。
- `dream` 内部的 doctor 步骤失败会让整轮返回非 0，但前面的步骤已经落盘；看 `health.json` 的 `steps`，别只看退出码。
- `guard` 只拦主会话，子会话有完整权限。它是分工线，不是沙箱。
- `delegate` 的队列路径不读 `CORE_HOME`；保持 `CORE_HOME=$DSH_HOME/agent-core`，或者同时改插件和命令行。
- 同一台机器上换工作目录，session 根会多一层；给 `ctx-guard` 设 `DSH_SESSIONS_DIR`，否则子会话计数、轮转和 dream 提醒都会看错目录。
- 插件依赖用 `file:` 指向绝对路径，搬目录后要重新 `dsh plugin add` 或改 `package.json`，`node_modules` 里的软链不会自己跟着走。
