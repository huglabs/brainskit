export const name = 'dsh-brainskit'

export const inject = ['systemPrompt', 'tools']

// Must equal `serverName` in cordis.patch.yml. DSH names MCP tools
// `mcp__<serverName>__<tool>` and a guard sees only that name, never which
// server registered it, so a renamed server is invisible to this guard.
export const SERVER_NAME = 'brainskit'
const TOOL_PREFIX = `mcp__${SERVER_NAME}__`

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

// Every tool result goes back to the model, so `human` is never a valid
// boundary for this bridge. Ordered narrowest first: `cloud` sees a subset of
// what `local` sees.
const MODEL_CONSUMERS = ['cloud', 'local']
export const DEFAULT_CONSUMER = 'cloud'

// Must stay the same expression as the `--consumer` argument in
// cordis.patch.yml, so the server's ceiling and this guard's cannot disagree.
export function declaredConsumer(env = process.env) {
  return env.BRAINSKIT_CONSUMER || DEFAULT_CONSUMER
}

function isModelConsumer(consumer) {
  return MODEL_CONSUMERS.includes(consumer)
}

export function brainskitGuidance(consumer = DEFAULT_CONSUMER) {
  const privacy = !isModelConsumer(consumer)
    ? `- This DSH process declared an invalid Brainskit consumer (${JSON.stringify(consumer)}), so the DSH bridge denies every Brainskit call until the operator sets BRAINSKIT_CONSUMER to cloud or local.`
    : consumer === 'cloud'
      ? '- This DSH process declared the Brainskit privacy consumer cloud: results reach a third-party model, so the server only returns cloud-eligible evidence. Omit the consumer argument or pass consumer: cloud. Never pass local or human; the DSH bridge denies both.'
      : '- This DSH process declared the Brainskit privacy consumer local: the operator runs a model on this machine. Omit the consumer argument or pass consumer: local, or cloud to narrow a result you will forward to a third-party service. Never pass human; the DSH bridge denies it.'
  return `Brainskit durable memory is available through tools named ${TOOL_PREFIX}*.

- When historical context may matter, call ${TOOL_PREFIX}search or ${TOOL_PREFIX}context before answering.
${privacy}
- Call ${TOOL_PREFIX}capture only when the user explicitly asks to remember something or supplies a durable source to retain. It accepts text and URLs; a file path is accepted only inside this project and never for a secret file such as .env.
- Never edit the vault directly. Compiled wiki changes must go through ${TOOL_PREFIX}apply so schema, provenance, citation, link and novelty checks remain active.
- Mutable wiki, filing, saved-answer, resurface and integration operations are denied by the DSH bridge unless the operator launched DSH with BRAINSKIT_ALLOW_MUTATIONS=1.`
}

export const BRAINSKIT_GUIDANCE = brainskitGuidance(DEFAULT_CONSUMER)

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

// Checked on every Brainskit tool, not only the ones that take a consumer
// today: the server treats its declared consumer as a ceiling for all of them.
function consumerDenial(tool, args, declared) {
  if (!isModelConsumer(declared)) {
    return `Brainskit ${tool} denied by the DSH bundle: BRAINSKIT_CONSUMER is ${JSON.stringify(declared)}, and only cloud or local can be declared for a model.`
  }
  if (!Object.hasOwn(args, 'consumer')) return undefined
  const requested = args.consumer
  if (isModelConsumer(requested) && MODEL_CONSUMERS.indexOf(requested) <= MODEL_CONSUMERS.indexOf(declared)) {
    return undefined
  }
  return `Brainskit ${tool} denied by the DSH bundle: consumer ${JSON.stringify(requested)} is wider than the declared ${declared}. Omit consumer or pass ${declared}${declared === 'local' ? ' or cloud' : ''}.`
}

export function createMutationGuard(allowMutations = false, consumer = DEFAULT_CONSUMER) {
  return (execution) => {
    if (typeof execution.name !== 'string' || !execution.name.startsWith(TOOL_PREFIX)) {
      return undefined
    }
    const tool = execution.name.slice(TOOL_PREFIX.length)
    const args = argumentsOf(execution)
    const privacy = consumerDenial(tool, args, consumer)
    if (privacy !== undefined) return privacy
    if (allowMutations) return undefined
    if (!DEFAULT_TOOLS.has(tool)) return MUTATION_DENIED
    if (tool === 'ask' && asksToSave(args)) return MUTATION_DENIED
    return undefined
  }
}

export function apply(ctx) {
  const consumer = declaredConsumer()
  ctx.systemPrompt.section({
    name: 'tool:brainskit',
    order: 145,
    text: brainskitGuidance(consumer),
  })
  ctx.tools.guard(createMutationGuard(process.env.BRAINSKIT_ALLOW_MUTATIONS === '1', consumer))
}
