export const name = 'dsh-brainskit'

export const inject = ['systemPrompt', 'tools']

const TOOL_PREFIX = 'mcp__brainskit__'

// An allow-list, not a deny-list: a tool Brainskit adds later is denied until
// someone decides it is safe for an autonomous agent, instead of being allowed
// because nobody remembered to list it here.
const DEFAULT_TOOLS = new Set([
  'capture',
  'search',
  'context',
  'ask',
  'proposals',
  'status',
  'lint',
  'integration_status',
])

// Every tool result goes back to the model, so no call made through this
// bridge is "shown directly to a person and not relayed".
const MODEL_CONSUMERS = new Set(['local', 'cloud'])
const CONSUMER_TOOLS = new Set(['search', 'context'])

export const BRAINSKIT_GUIDANCE = `Brainskit durable memory is available through tools named mcp__brainskit__*.

- When historical context may matter, call mcp__brainskit__search or mcp__brainskit__context before answering.
- Every machine read must declare its privacy consumer. Use local only when the result stays on the operator's machine. Use cloud before forwarding a result to a third-party model or service. Never use human: every result you read is relayed through a model, and the DSH bridge denies it.
- Call mcp__brainskit__capture only when the user explicitly asks to remember something or supplies a durable source to retain.
- Never edit the vault directly. Compiled wiki changes must go through mcp__brainskit__apply so schema, provenance, citation, link and novelty checks remain active.
- Mutable wiki, filing, saved-answer, resurface and integration operations are denied by the DSH bridge unless the operator launched DSH with BRAINSKIT_ALLOW_MUTATIONS=1.`

const MUTATION_DENIED = 'Brainskit mutation denied by the DSH bundle. Restart DSH with BRAINSKIT_ALLOW_MUTATIONS=1 only after confirming the vault and requested operation.'

function argumentsOf(execution) {
  const value = execution.arguments
  return typeof value === 'object' && value !== null ? value : {}
}

// Brainskit reads `save` with Python's bool(), so "false", 1 and "no" all
// save. Only an absent or literal false value is a non-saving question.
function asksToSave(args) {
  return args.save !== undefined && args.save !== null && args.save !== false
}

function consumerDenial(tool, args) {
  if (!CONSUMER_TOOLS.has(tool) || MODEL_CONSUMERS.has(args.consumer)) return undefined
  return `Brainskit ${tool} denied by the DSH bundle: consumer must be local or cloud, never ${JSON.stringify(args.consumer ?? null)}, because the result is relayed through a model.`
}

export function createMutationGuard(allowMutations = false) {
  return (execution) => {
    if (typeof execution.name !== 'string' || !execution.name.startsWith(TOOL_PREFIX)) {
      return undefined
    }
    const tool = execution.name.slice(TOOL_PREFIX.length)
    const args = argumentsOf(execution)
    const privacy = consumerDenial(tool, args)
    if (privacy !== undefined) return privacy
    if (allowMutations) return undefined
    if (!DEFAULT_TOOLS.has(tool)) return MUTATION_DENIED
    if (tool === 'ask' && asksToSave(args)) return MUTATION_DENIED
    return undefined
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
