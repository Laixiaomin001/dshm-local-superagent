# 一个运行在本地，拥有独立人格，自我进化，不间断会话，永久记忆的 superagent

这是一套跑在单机上的 agent 骨架：会话可以换页，账本不会断；上下文有上限，记忆没有。它不依赖云端的会话存储，也不把判断交给外部服务。所有状态落在本地文件里，纯文本可读，派生数据可重建。

代码分两层。插件层挂在 DSH 的 cordis 总线上，管边界、上下文、派单和会话形态；工具层是十四个不依赖第三方库的 Python 脚本，管记账、检索、收料、自检和常驻页。两层之间只通过文件和环境变量耦合，任何一层都可以单独替换。

> 本仓按现役版本重新生成：四个插件、十四个工具脚本、队列协议层和回归测试。路径全部走环境变量，不含任何一台具体机器的地址、密钥或人名。

## 部件

**边界护栏 `plugins/guard`**：顶层会话不许直接改文件、跑命令，写和跑一律下发到子会话；记账脚本走白名单。这条规则把「决策」和「动手」拆开，主会话的上下文只用来判断。

**上下文闸门 `plugins/ctx-guard` + `tone.js`**：每轮结束算一次上下文占用，到窗口 60% 就插一条提醒，要求先落完整记忆再换页；顺带盯磁盘余量和当天新开的子会话数。轮末另做一次口吻自检：只看最后一条 assistant 正文，撞禁词、平均句长超 45 字、整句没数字没路径就算不合格，只记 `运行/口吻自检.jsonl`，不插消息。

**只读主会话 `plugins/readonly-preset`**：运行时注册一个只读 preset，文件工具关掉写和改，压缩阈值 0.7，工具结果超 8KB 先裁头尾。preset 定义写在 JS 里，绕开 home 层 YAML 的预加载检查。（v2 已下线，未加载；preset 方案由 guard 插件接管）

**本地派单 `plugins/delegate` + `tools/queue.py`**：`delegate` 把活写进 `agent-core/queue/`，插件轮询、起一个子会话执行、结果回到原会话。`queue.py` 是同一套目录协议的命令行版，多了认领、心跳超时回收、失败归档、验收打回和按任务号续做。插件和命令行共用一套队列目录，不再各写一份。`--key` 是幂等键，同一个 key 在 inbox/running 里只允许一张单。

**远端派单 `tools/dispatch.py`**：同一份工单格式，多两个字段（`target`、`cmd`），把命令丢到远端 nohup 跑，本机断了远端继续。远端主机、私钥、工作目录从 `DISPATCH_SSH_HOST`、`DISPATCH_SSH_KEY`、`DISPATCH_WORK` 读；没配主机时远端派单不启用，本机派单走 `delegate` 和 `queue.py`。

**账本 `tools/core.py`**：流水是唯一真相，一件一事，纯 markdown；SQLite FTS5 只是索引，随时可重建。检索有字节上限，一次最多回 5 条 2KB，绝不把整本账灌进上下文。`dream` 每天把十二步串成一轮：重建索引、收料、消费待记、出日报、挑教训、列未判域、抽事实候选、口吻统计、补向量、重建常驻页、体检、快照。

**事实层 `tools/fact.py`**：一句「到现在还成立的话」。同一主体加谓词只留一张有效卡，换值先失效旧的再写取代，被顶掉的卡不删。每张卡带置信度：来源类型定初值（抽取 0.4、工具 0.5、人给 0.8），被 `cite` 一次加 0.02，满 30 天没用乘 0.9。

**常驻层 `tools/recall.py`**：把续点、未结的活、用户事实卡、最近五条教训压成一页 `memory/常驻.md`，总字数硬顶 2000。每轮开局先读这一页；长的都留在流水里，按需 `search`。

**边界策略 `$CORE_HOME/policy.json` + `plugins/guard`**：插件启动时读 `guard` 节，读不到或读坏了退回内置默认，只会更保守；拦哪些工具、放行哪些文档目录、记账脚本白名单都在这里配。每次拒绝记 `运行/guard.jsonl`。命令判定在 `checkCommand`：只放行单条 `python3 bin/<白名单脚本>.py` 或 `bin/<白名单脚本>.py`，借道、管道、重定向、命令替换一律拒。

**收料与整理 `tools/sentinel.py`、`style.py`、`review.py`、`vectors.py`、`eval.py`**：哨兵扫会话日志，按完整回合补进待记，偏移落盘不重复；口吻基线量「像不像同一个人」；日结只出草稿不下结论；向量做语义召回，`search --hybrid` 走向量加 FTS 的 RRF 融合，`dream` 第 9 步全量重嵌；评测脚本量 recall@5 和 MRR。`ledger.py` 把每次派单和结果按天记进 `运行/派单台账/`，`dream` 第 12 步做归档。

**队列协议 `protocol/`**：`queue.schema.json` 是字段、枚举和状态机的唯一出处；`validate.py` 只用标准库校验一张单合不合协议，也能单独用命令行跑；`INTEGRATION.md` 列出 `queue.py` 要接的四处。插件、命令行、远端派单共用这套目录，一张单长什么样只认 schema。

**回归测试 `tests/`**：`test_queue.py` 在临时 `CORE_HOME` 里跑认领、心跳、验收、打回、续做和并发认领；`test_recall.py` 造一份最小账本验常驻页的结构和字数上限。两个都不碰真账本。

## 怎么装

插件：把 `plugins/<name>/` 整个目录放进 DSH profile 的插件目录。每个包带 `package.json`；guard、ctx-guard 另带 `cordis.patch.yml`，`dsh.bundle.patch` 指向它。装完在 profile 的插件列表里能看到 `dsh-guard`、`dsh-ctx-guard`、`dsh-delegate`、`dsh-readonly-preset`。readonly-preset 的 preset 定义在 JS 里，不需要补丁文件。

工具：把 `tools/*.py` 和 `tools/unzstd.mjs` 放到 `$DSH_HOME/agent-core/bin/`，`tools/lib/` 放到 `$DSH_HOME/agent-core/bin/lib/`，`protocol/` 放到 `$DSH_HOME/agent-core/protocol/`，加执行位，然后跑一次 `core.py init`。脚本之间用同目录导入，`queue.py` 用 `../protocol/validate.py` 找校验器，目录层级别拆散。

宪章：把 `docs/prompt-rules.md` 里「记忆与账本」一节追加到 `$DSH_HOME/AGENTS.md`。`doctor` 靠 `## 记忆与账本` 这个标题判断挂钩是否生效。

测试：`tests/` 放在仓里直接跑就行，也可以装到 `$DSH_HOME/agent-core/tests/`；脚本位置用 `QUEUE_PY`、`RECALL_PY` 覆盖。

定时：`dream` 建议每天凌晨跑一次，用系统 cron 或 DSH 的定时任务都行。第 10、11 步是 `recall build` 和 `doctor`，常驻页由它生成，别手改。

## 依赖

- DSH，需要 cordis 插件 API 和 `tools`、`tokenMeter`、`agents`、`subagents` 这几个服务
- Node 20 以上，插件用到内置 `zlib.zstdDecompressSync`；`sentinel.py` 也借它解压会话日志
- Python 3.10 以上，只用标准库；SQLite 要带 FTS5
- 可选的 embeddings 接口，用于向量召回。不配也能跑，检索自动降级成纯 FTS

环境变量：`DSH_HOME`（默认 `~/.dsh`）、`CORE_HOME`（默认 `$DSH_HOME/agent-core`）、`EXPORT_ROOT`（默认 `$DSH_HOME/exports`）、`DSH_SESSIONS`（会话目录，不给就按 `DSH_WORKSPACE` 推）、`DSH_WORKSPACE`、`DSH_PLUGINS`（doctor 查插件目录，默认 `$DSH_HOME/plugins`）；远端派单的 `DISPATCH_SSH_HOST`、`DISPATCH_SSH_KEY`（默认 `~/.ssh/id_ed25519`）、`DISPATCH_WORK`（默认 `~/work`）；语音的 `VOICE_SSH_HOST`、`VOICE_SSH_KEY`；向量相关的 `EMBED_API_KEY_FILE`（默认 `~/.dsh/private/embed.key`，没有就降级成纯 FTS）。密钥只从环境变量或文件读，代码里没有任何凭证；远端和语音没配主机就不启用。

## 已知限制

记忆整理、索引、备份都靠 `dream` 单点串行，进程被杀就断在一半，下一轮从头补。向量是全量重嵌，件数上万会明显变慢。常驻页硬顶 2000 字，塞不下就截断最后一行。`guard` 只拦顶层会话，子会话有全部权限。`delegate` 的 worker 有 30 分钟硬超时，返回值截断在 400 字；插件不在线时单会留在 inbox。检索没有查询改写和重排，短查询退化成 `LIKE`。价格表是占位值，只用来演示算法。整套东西按单用户单机设计，没有鉴权，别把接口暴露到公网。
