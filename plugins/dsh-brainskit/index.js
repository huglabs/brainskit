export const name = 'dsh-brainskit'

export const inject = ['systemPrompt', 'tools']

const MUTATING_TOOLS = new Set([
  'mcp__brainskit__apply',
  'mcp__brainskit__approve',
  'mcp__brainskit__file',
  'mcp__brainskit__integration_configure',
  'mcp__brainskit__integration_down',
  'mcp__brainskit__integration_sync',
  'mcp__brainskit__integration_up',
  'mcp__brainskit__reject',
])

export const BRAINSKIT_GUIDANCE = `Brainskit durable memory is available through tools named mcp__brainskit__*.

- When historical context may matter, call mcp__brainskit__search or mcp__brainskit__context before answering.
- Every machine read must declare its privacy consumer. Use local only when the result stays on the operator's machine. Use cloud before forwarding a result to a third-party model or service. Never use human unless the result is shown directly to a person and is not relayed.
- Call mcp__brainskit__capture only when the user explicitly asks to remember something or supplies a durable source to retain.
- Never edit the vault directly. Compiled wiki changes must go through mcp__brainskit__apply so schema, provenance, citation, link and novelty checks remain active.
- Mutable wiki, filing and integration operations are denied by the DSH bridge unless the operator launched DSH with BRAINSKIT_ALLOW_MUTATIONS=1.`

function asksToSave(argumentsValue) {
  return typeof argumentsValue === 'object'
    && argumentsValue !== null
    && argumentsValue.save === true
}

export function createMutationGuard(allowMutations = false) {
  if (allowMutations) return () => undefined
  return (execution) => {
    const blocked = MUTATING_TOOLS.has(execution.name)
      || (execution.name === 'mcp__brainskit__ask' && asksToSave(execution.arguments))
    if (!blocked) return undefined
    return 'Brainskit mutation denied by the DSH bundle. Restart DSH with BRAINSKIT_ALLOW_MUTATIONS=1 only after confirming the vault and requested operation.'
  }
}

export function apply(ctx) {
  ctx.systemPrompt.section({
    name: 'tool:brainskit',
    order: 145,
    text: BRAINSKIT_GUIDANCE,
  })
  ctx.tools.guard(createMutationGuard(process.env.BRAINSKIT_ALLOW_MUTATIONS === '1'))
}
