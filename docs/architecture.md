# 架构

## 一句话

会话是易失的，账本是持久的。所有设计都围绕这一条：任何一个时刻进程被杀，下次开机能从落盘的文件里接着干。

## 三层

```
宿主（会话、工具调用、模型）
  │
插件层（cordis）    guard / ctx-guard（含 tone.js） / readonly-preset / delegate
  │                 只做拦截、提醒、注册、调度，不碰账本内容
  │
工具层（Python）    core / fact / recall / queue / dispatch / guard / sentinel /
  │                 vectors / style / review / usage / ctx / eval
  │                 纯标准库，命令行走 stdin/stdout，退出码表达成败
  │
文件层（纯文本）    $DSH_HOME/agent-core/
```

层与层之间只有两种耦合：文件路径和环境变量。工具层不知道插件存在，插件也不解析账本内容。要替换任意一层，另外两层不用改。

## 目录

```
$DSH_HOME/
├── AGENTS.md                 宪章，插件和工具都认里面的标记
├── sessions/                 宿主写的会话日志，只读
├── exports/                  人可读导出与快照
└── agent-core/
    ├── bin/                  十三个脚本 + unzstd.mjs
    ├── memory/
    │   ├── 常驻.md           每轮先读的一页，recall.py 生成，别手改
    │   ├── 流水/             正本，一件一事
    │   ├── 待记/             每回合原料，只增不删
    │   ├── 日报/             整理产物
    │   ├── 事实.jsonl        事实卡
    │   ├── 向量.jsonl        向量索引
    │   ├── 索引.db           FTS5，派生物
    │   └── 状态.json         件数、最后写入、续点
    ├── queue/                inbox / running / done / stuck，插件和命令行共用
    ├── 运行/                 哨兵偏移、口吻自检、教训、待审、报告
    ├── policy.json           guard.py 的边界策略
    └── health.json           后台是否正常的唯一凭据
```

## 模块清单

| 模块 | 管什么 | 落在哪 |
| --- | --- | --- |
| `core.py` | 流水、索引、检索、整理、体检、备份 | `memory/`、`health.json` |
| `fact.py` | 一句话事实，带置信度和取代链 | `memory/事实.jsonl` |
| `recall.py` | 常驻层，把续点/待办/用户事实/教训压成一页 | `memory/常驻.md` |
| `queue.py` | 本地派单队列：认领、心跳、验收、打回、续做 | `queue/` |
| `dispatch.py` | 投远端执行，收日志和退出码 | `queue/` + 远端 |
| `guard.py` | 路径和命令的边界判定 | `运行/guard.jsonl` |
| `sentinel.py` | 扫会话日志，按回合补进待记 | `memory/待记/`、`运行/sentinel.json` |
| `vectors.py` | 嵌入、余弦召回、RRF 的一路候选 | `memory/向量.jsonl` |
| `style.py` | 口吻基线 | `运行/口吻基线.json` |
| `review.py` | 日结草稿 | `memory/日报/` |
| `usage.py` | 会话日志的 token 与花费估算 | 只看不写 |
| `ctx.py` | 量上下文占用，给闸门用 | 只看不写 |
| `eval.py` | recall@5 / MRR 回归 | 只看不写 |
| `plugins/ctx-guard/tone.js` | 口吻自检：禁词、句长、空泛 | `运行/口吻自检.jsonl` |

## 部件怎么咬合

一次普通回合：主会话收到请求 → `guard` 判断要不要拦（顶层不许直接写文件）→ 需要动手就 `delegate` 或 `subagent` 下发，单落 `queue/inbox` → 子会话干完回消息，插件把结果写进 `queue/done` 并回注原会话 → 回合结束时 `ctx-guard` 算占用，到 60% 插提醒，同时跑一次口吻自检只记日志。整个过程 `sentinel` 在背后扫会话日志，把完整回合补进待记。

一次派单：`delegate` 工具或 `queue.py add --key` 落单 → 插件每 10 秒扫一次 inbox，认领时把单 rename 进 running 并每 60 秒写心跳 → 完成后进 done，带 `ok`、结果和 `accepted: false` → 验收走 `queue.py accept`；不满意走 `queue.py reject --retry`，单回 inbox，`attempts` 加一，两次到顶转人工；要补做走 `queue.py resume <id> --task "..."`，新单记 `parent` 指向原单。同一个 `key` 在 inbox/running 里只允许一张，重复投递的两张里后到的那张进 stuck 记 `duplicate_of`。

一次换页：占用到 60% → 写续点（卡在哪、下一步、必须知道的参数）→ 把这一页的判断落进流水和事实卡 → 开新页，新页开局先读 `memory/常驻.md`，再读状态里的续点。

一天一次 `dream`，十二步，顺序固定：

1. `reindex` 重建 FTS5 索引
2. `sentinel` 收料
3. `consume` 把待记新行按天补进日报
4. `digest` 数日报份数
5. `lessons` 挑教训候选
6. `undecided` 列未判域
7. `factcand` 抽事实候选
8. `tone` 读口吻自检日志，出当天合规率和最常犯的三条
9. `vectors` 全量重嵌
10. `recall` 重建常驻页
11. `doctor` 体检
12. `backup` 快照

前面几步任何一步失败都记进 `steps`，不裸崩；子进程非 0 退出也算失败，不看 stdout 说没事。健康表整体标红，出问题能定位到具体一步。

## 为什么是这套结构

**正本用 markdown，索引用 SQLite。** 正本要能在任何编辑器里读，要能直接 `git diff`，要能被人手改。索引是派生物，坏了重跑一条命令就回来。反过来把 SQLite 当正本，迁移和备份都会变成麻烦。

**检索有字节预算。** `search` 一次最多 5 条、2KB，`get` 最多 8KB。上下文是稀缺资源，账本可以很大，进上下文的必须很小。这条约束写在代码里，不靠自觉。常驻页同理：每轮都要读，所以硬顶 2000 字。

**写入走临时文件加 rename。** 宿主随时会杀进程，写到一半的文件不能留在正本目录里。索引和正本在同一个事务里更新，宁可整条失败，不留半条。队列也用 rename 认领：`rename` 成功才算拿到单，两个进程同时扫也不会抢同一张。

**事实和流水分开。** 流水记「发生了什么」，带时间、带原话、带当时的判断。事实记「什么还成立」，一句话，可被取代，失效不删除。事实卡还带置信度：抽取出来的默认 0.4，工具验过的 0.5，人给的 0.8，被引用越多越高，久了没用会衰减。前者回答「怎么走到今天」，后者回答「现在是什么情况」。

**整理只出候选，不自动下结论。** 判域清单、事实候选、教训候选都写进 `运行/待审/`，等人或主会话第二天判。自动判错一次，账本就被污染一次，修起来比手判贵。

## 降级

向量接口不通、没有 key、返回维度不对，检索静默退回纯 FTS，`dream` 里对应那一步记成 `degraded`，不拖红整轮。口吻自检写不进日志就当没这回事，不拖垮回合。远端派单没配主机时只当本机队列。磁盘不足、子会话过多只提醒不阻断。会话日志解不开就跳过那个会话，下一轮再试。`health.json` 里每步都有 `ok` 和 `note`，出问题能定位到具体一步。
