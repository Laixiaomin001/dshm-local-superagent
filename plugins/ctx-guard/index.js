/** xiaomin-ctx-guard: 每个会话在上下文压力达到 70% 时推一条提醒，只推一次。 */
import { randomUUID } from 'node:crypto'

export const name = 'xiaomin-ctx-guard'
export const inject = ['tools', 'agents', 'sessionProjections', 'timer']

const THRESHOLD = 0.6
const POLL_MS = 60 * 1000

// 进程内记账：sessionId -> 已经提醒过。插件重载才会清空。
const reminded = new Set()

/** 从会话投影读上下文压力；拿不到完整数字就返回 undefined。 */
function readPressure(ctx, session) {
  const state = ctx.sessionProjections.stateOf(session, 'contextPressure')
  if (!state || typeof state !== 'object') return undefined
  const pressure = state.pressureTokens
  const window = state.contextWindow
  if (typeof pressure !== 'number' || typeof window !== 'number') return undefined
  if (!(pressure >= 0) || !(window > 0)) return undefined
  return { pressure, window, ratio: pressure / window }
}

export function reminderText(pressure, window) {
  const pct = Math.floor((pressure / window) * 100)
  return `上下文已到 ${pct}%（${pressure}/${window}），该写续点、换页了。`
}

/**
 * 检查一个活着的 agent，必要时往它的会话推提醒。
 * 只有真的 followup 成功才记账；失败下个周期重试。
 */
export function checkAgent(ctx, agent) {
  const session = agent && agent.session
  if (!session || !session.id || reminded.has(session.id)) return false
  const p = readPressure(ctx, session)
  if (!p || p.ratio < THRESHOLD) return false
  if (agent.status !== 'idle') return false

  const text = reminderText(p.pressure, p.window)
  try {
    agent.followup({
      id: randomUUID(),
      role: 'user',
      content: [{ type: 'text', text }],
      source: { kind: 'user' },
    })
  } catch (error) {
    ctx.logger.warn(`xiaomin-ctx-guard: followup 失败（${session.id}）：${String((error && error.message) || error)}`)
    return false
  }
  reminded.add(session.id)
  ctx.logger.info(`xiaomin-ctx-guard: ${session.id} ${p.pressure}/${p.window} 已提醒`)
  return true
}

/** 扫一遍所有活会话；单个会话出错不拖垮整轮。 */
export function checkAll(ctx) {
  let agents
  try {
    agents = ctx.agents.list()
  } catch (error) {
    ctx.logger.warn(`xiaomin-ctx-guard: 列举 agent 失败：${String((error && error.message) || error)}`)
    return 0
  }
  let sent = 0
  for (const agent of agents) {
    try {
      if (checkAgent(ctx, agent)) sent += 1
    } catch (error) {
      ctx.logger.warn(`xiaomin-ctx-guard: 检查会话失败：${String((error && error.message) || error)}`)
    }
  }
  return sent
}

export function apply(ctx) {
  // turn 结束立刻看一次：压力通常在这轮 assistant 结算后已经落到投影里。
  ctx.on('session/event', (session, event) => {
    if (!event || event.type !== 'turn/end') return
    const agent = ctx.agents.get(session.id)
    if (!agent) return
    try {
      checkAgent(ctx, agent)
    } catch (error) {
      ctx.logger.warn(`xiaomin-ctx-guard: turn/end 检查失败：${String((error && error.message) || error)}`)
    }
  })

  // 低频兜底：覆盖已空闲但没再触发 turn/end 的会话（比如恢复出来的）。
  const setInt = typeof ctx.setInterval === 'function' ? ctx.setInterval.bind(ctx) : setInterval
  setInt(() => {
    try {
      checkAll(ctx)
    } catch (error) {
      ctx.logger.warn(`xiaomin-ctx-guard: 定时检查失败：${String((error && error.message) || error)}`)
    }
  }, POLL_MS)
}
