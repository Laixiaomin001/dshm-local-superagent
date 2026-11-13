/**
 * dsh-readonly-preset —— 运行时注册「只读主会话」preset。
 *
 * 为什么定义写在 JS 里：home 层 YAML 预检会扫插件行的 config，里面出现插件 name
 * 就整份 patch 被隔离（home/cordis.patch.yml.quarantine-* 就是这么来的）。
 * 所以这里只声明一条无 config 的行，真正的 rows 硬编码在下面。
 *
 * 注册失败不抛错：register() 的激活异常只落到 list() 的 broken 字段，
 * 因此 apply 之后延迟一拍自检一次（apply 处于宿主行激活中，不能就地 await list()）。
 */
export const name = 'dsh-readonly-preset'
export const inject = ['agentPresets']

const PRESET_DEF = {
  id: 'readonly',
  name: '只读主会话',
  description: '只查、只判、只派；写和跑交给子会话。',
  order: 10,
  plugins: [
    { id: 'time-context', name: '@deepseek-ai/dsh-time-context' },
    {
      id: 'agent-instructions',
      name: '@deepseek-ai/dsh-agent-instructions',
      config: { maxBytes: 65536 },
    },
    { id: 'tool-fs', name: '@deepseek-ai/dsh-tool-fs', config: { write: false, edit: false } },
    {
      id: 'tool-fs-search',
      name: '@deepseek-ai/dsh-tool-fs-search',
      config: { sampleOverCapGlobResults: false },
    },
    { id: 'tool-subagent-control', name: '@deepseek-ai/dsh-tool-subagent-control' },
    {
      id: 'tool-subagent',
      name: '@deepseek-ai/dsh-tool-subagent',
      config: { provider: 'spawn', toolName: 'subagent', agentPreset: 'standard' },
    },
    { id: 'tool-jobs', name: '@deepseek-ai/dsh-tool-jobs' },
    {
      id: 'compaction',
      name: 'cordis:group',
      group: true,
      isolate: { compaction: true, toolResultPruner: true },
      config: [
        {
          id: 'compaction-basic',
          name: '@deepseek-ai/dsh-compaction-basic',
          config: { thresholdRatio: 0.7 },
        },
        { id: 'command-compact', name: '@deepseek-ai/dsh-command-compact' },
        {
          id: 'tool-result-pruner',
          name: '@deepseek-ai/dsh-compaction-tool-result-pruner',
          config: { thresholdChars: 8192, headChars: 4096, tailChars: 1024 },
        },
      ],
    },
  ],
}

const PRESET_ID = PRESET_DEF.id
const TAG = '[dsh-readonly-preset]'
const SELF_CHECK_MS = 3000

async function selfCheck(service, ctx) {
  const log = ctx.logger ?? console
  const rows = await service.list()
  const mine = rows.find((r) => r.id === PRESET_ID)
  if (mine === undefined) {
    log.warn?.(`${TAG} list() 里没有 ${PRESET_ID}：注册没生效`)
  } else if (mine.broken !== undefined) {
    log.warn?.(`${TAG} ${PRESET_ID} 激活失败：${mine.broken}`)
  } else {
    log.info?.(`${TAG} ${PRESET_ID} 就绪，${PRESET_DEF.plugins.length} 行`)
  }
}

export async function apply(ctx) {
  const service = ctx.agentPresets
  if (service === undefined) throw new Error(`${TAG} 缺少 agentPresets 服务`)

  const dispose = await service.register(PRESET_DEF)
  ctx.effect(() => dispose)

  const timer = setTimeout(() => {
    selfCheck(service, ctx).catch(() => {
      /* 自检失败不惊动宿主 */
    })
  }, SELF_CHECK_MS)
  if (typeof timer?.unref === 'function') timer.unref()
  ctx.effect(() => () => clearTimeout(timer))
}
