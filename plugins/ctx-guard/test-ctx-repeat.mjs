/**
 * ctx-guard 上下文提醒去重回归测试：node test-ctx-repeat.mjs
 * 用假 ctx 驱动 agent/turn-stopping，只数「上下文已用到」那一条。
 * 需要宿主提供 @deepseek-ai/dsh-llm，这里用 loader hook 塞一个假模块，不碰真依赖。
 */
import { register } from 'node:module'

const HOOKS = 'data:text/javascript,' + encodeURIComponent(`
export async function resolve(specifier, context, next) {
  if (specifier === '@deepseek-ai/dsh-llm') {
    return {
      url: 'data:text/javascript,' + encodeURIComponent('export const createUserMessage = (m) => m'),
      shortCircuit: true,
    }
  }
  return next(specifier, context)
}`)
register(HOOKS)

const { apply } = await import('./index.js')

const WINDOW = 100000
let tokens = 0
const injected = []
const session = { id: 'fake-session' }
const agent = { session, inject: async (msg) => { injected.push(msg) } }

let handler = null
const ctx = {
  tokenMeter: {
    contextPressure: () => ({ contextWindow: WINDOW }),
    measure: () => ({ totalTokens: tokens }),
  },
  on: (event, fn) => { if (event === 'agent/turn-stopping') handler = fn },
}

apply(ctx)
if (typeof handler !== 'function') {
  console.error('FAIL 没挂上 agent/turn-stopping')
  process.exit(1)
}

const isCtx = (msg) => {
  const parts = msg && msg.content
  return Array.isArray(parts) && parts.some((p) => typeof p.text === 'string' && p.text.includes('上下文到 '))
}

/** 跑到指定比例，返回这一轮插了几条 ctx 提醒。 */
async function turn(pct) {
  tokens = Math.round(WINDOW * pct)
  const before = injected.length
  await handler({ agent, turn: 1 })
  return injected.slice(before).filter(isCtx).length
}

const steps = []
steps.push(['62% 提醒', await turn(0.62), 1])
steps.push(['64% 不提醒', await turn(0.64), 0])
steps.push(['72% 再提醒', await turn(0.72), 1])
steps.push(['跌到 55% 不提醒', await turn(0.55), 0])
steps.push(['回到 62% 再提醒', await turn(0.62), 1])

let fail = 0
for (const [label, got, want] of steps) {
  const ok = got === want
  if (!ok) fail += 1
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}：期望 ${want} 条，实际 ${got} 条`)
}
console.log(`\n${steps.length - fail}/${steps.length} passed`)
process.exit(fail === 0 ? 0 : 1)
