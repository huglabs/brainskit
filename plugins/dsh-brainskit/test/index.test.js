import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  BRAINSKIT_GUIDANCE,
  SERVER_NAME,
  apply,
  brainskitGuidance,
  createMutationGuard,
  declaredConsumer,
} from '../index.js'

const PATCH = readFileSync(new URL('../cordis.patch.yml', import.meta.url), 'utf8')

function execution(name, argumentsValue = {}) {
  return { name, arguments: argumentsValue }
}

// The MCP client entry's `args:` list evaluated the way DSH does, `!!js`
// items against a stand-in `process`, so the file that ships is what is tested.
function spawnArgs(env) {
  const lines = PATCH.split('\n')
  const start = lines.findIndex((line) => line.trim() === 'args:')
  assert.notEqual(start, -1, 'cordis.patch.yml has no args list')
  const indent = lines[start].search(/\S/)
  const fakeProcess = { env, cwd: () => '/work' }
  const args = []
  for (const line of lines.slice(start + 1)) {
    const trimmed = line.trim()
    if (trimmed === '' || trimmed.startsWith('#')) continue
    if (line.search(/\S/) <= indent) break
    const item = trimmed.replace(/^- /, '')
    args.push(item.startsWith('!!js ')
      ? new Function('process', `return (${item.slice(5)})`)(fakeProcess)
      : item)
  }
  return args
}

function consumerArg(env) {
  const args = spawnArgs(env)
  const at = args.indexOf('--consumer')
  assert.notEqual(at, -1, `no --consumer in ${JSON.stringify(args)}`)
  return args[at + 1]
}

test('default guard permits retrieval and append-only capture', () => {
  const guard = createMutationGuard()
  assert.equal(guard(execution('mcp__brainskit__search', { consumer: 'cloud' })), undefined)
  assert.equal(guard(execution('mcp__brainskit__context', { query: 'q' })), undefined)
  assert.equal(guard(execution('mcp__brainskit__capture')), undefined)
  for (const name of ['status', 'lint', 'proposals', 'integration_status']) {
    assert.equal(guard(execution(`mcp__brainskit__${name}`)), undefined)
  }
  assert.equal(guard(execution('mcp__another-server__apply')), undefined)
})

test('default guard denies durable and lifecycle mutations', () => {
  const guard = createMutationGuard()
  for (const name of [
    'mcp__brainskit__apply',
    'mcp__brainskit__approve',
    'mcp__brainskit__file',
    'mcp__brainskit__integration_configure',
    'mcp__brainskit__integration_down',
    'mcp__brainskit__integration_sync',
    'mcp__brainskit__integration_up',
    'mcp__brainskit__reject',
  ]) {
    assert.match(guard(execution(name)), /BRAINSKIT_ALLOW_MUTATIONS=1/)
  }
  assert.match(
    guard(execution('mcp__brainskit__ask', { save: true })),
    /BRAINSKIT_ALLOW_MUTATIONS=1/,
  )
  assert.equal(
    guard(execution('mcp__brainskit__ask', { save: false })),
    undefined,
  )
  assert.equal(guard(execution('mcp__brainskit__ask', { question: 'q' })), undefined)
})

test('default guard reads save the way Brainskit does', () => {
  const guard = createMutationGuard()
  for (const save of ['true', 'false', 1, 'no', {}]) {
    assert.match(
      guard(execution('mcp__brainskit__ask', { save })),
      /BRAINSKIT_ALLOW_MUTATIONS=1/,
      `save: ${JSON.stringify(save)} must be denied`,
    )
  }
})

test('default guard denies tools it does not know and resurface', () => {
  const guard = createMutationGuard()
  for (const name of ['resurface', 'some_future_write']) {
    assert.match(guard(execution(`mcp__brainskit__${name}`)), /BRAINSKIT_ALLOW_MUTATIONS=1/)
  }
})

test('the server is spawned with --consumer cloud unless the operator opts into local', () => {
  assert.equal(consumerArg({}), 'cloud')
  assert.equal(consumerArg({ BRAINSKIT_CONSUMER: '' }), 'cloud')
  assert.equal(consumerArg({ BRAINSKIT_CONSUMER: 'local' }), 'local')
  assert.deepEqual(spawnArgs({}).slice(0, 2), ['--vault', '/work/.brainskit'])
})

test('the guard reads the same declaration the server is spawned with', () => {
  for (const env of [{}, { BRAINSKIT_CONSUMER: '' }, { BRAINSKIT_CONSUMER: 'local' }, { BRAINSKIT_CONSUMER: 'human' }]) {
    assert.equal(declaredConsumer(env), consumerArg(env), JSON.stringify(env))
  }
})

test('the guard prefix is the server name the patch registers', () => {
  const match = PATCH.match(/^\s*serverName:\s*(\S+)\s*$/m)
  assert.ok(match, 'cordis.patch.yml has no serverName')
  assert.equal(match[1], SERVER_NAME)
})

test('a cloud declaration denies a per-call local or human consumer', () => {
  for (const guard of [createMutationGuard(), createMutationGuard(true, 'cloud')]) {
    for (const name of ['search', 'context', 'ask', 'capture', 'some_future_read']) {
      for (const consumer of ['local', 'human', null, 'CLOUD', 1]) {
        assert.match(
          guard(execution(`mcp__brainskit__${name}`, { consumer })),
          /wider than the declared cloud/,
          `${name} with consumer ${JSON.stringify(consumer)}`,
        )
      }
    }
    assert.equal(guard(execution('mcp__brainskit__search', { consumer: 'cloud' })), undefined)
    assert.equal(guard(execution('mcp__brainskit__search', {})), undefined)
  }
})

test('a local declaration permits narrowing to cloud and still denies human', () => {
  for (const guard of [createMutationGuard(false, 'local'), createMutationGuard(true, 'local')]) {
    for (const name of ['mcp__brainskit__search', 'mcp__brainskit__context']) {
      assert.equal(guard(execution(name, { consumer: 'local' })), undefined)
      assert.equal(guard(execution(name, { consumer: 'cloud' })), undefined)
      assert.equal(guard(execution(name, {})), undefined)
      assert.match(guard(execution(name, { consumer: 'human' })), /wider than the declared local/)
    }
  }
})

test('an invalid declaration denies every Brainskit call, even with the opt-in', () => {
  for (const declared of ['human', 'Cloud', 'none']) {
    for (const guard of [createMutationGuard(false, declared), createMutationGuard(true, declared)]) {
      for (const name of ['status', 'search', 'capture', 'apply']) {
        assert.match(guard(execution(`mcp__brainskit__${name}`)), /only cloud or local/)
      }
      assert.equal(guard(execution('mcp__another-server__search')), undefined)
    }
  }
})

test('guidance names the declared consumer and never steers a cloud model to local', () => {
  const cloud = brainskitGuidance('cloud')
  assert.equal(cloud, BRAINSKIT_GUIDANCE)
  assert.match(cloud, /declared the Brainskit privacy consumer cloud/)
  assert.doesNotMatch(cloud, /pass consumer: local/)
  assert.match(brainskitGuidance('local'), /pass consumer: local/)
  assert.match(brainskitGuidance('human'), /denies every Brainskit call/)
  for (const consumer of ['cloud', 'local']) {
    assert.match(brainskitGuidance(consumer), /accepts text and URLs; a file path is accepted only inside this project/)
  }
})

test('guidance ties integration lifecycle to a local consumer, not the opt-in alone', () => {
  const cloud = brainskitGuidance('cloud')
  assert.match(cloud, /Integration configuration, startup, shutdown and sync require BRAINSKIT_CONSUMER=local and BRAINSKIT_ALLOW_MUTATIONS=1/)
  assert.match(cloud, /bk integration <verb>/)
  assert.doesNotMatch(cloud, /resurface and integration operations/)
  const local = brainskitGuidance('local')
  assert.match(local, /Integration configuration, startup, shutdown and sync are also denied by the DSH bridge unless the operator launched DSH with BRAINSKIT_ALLOW_MUTATIONS=1/)
  assert.doesNotMatch(local, /BRAINSKIT_CONSUMER=local/)
  assert.match(brainskitGuidance('human'), /require BRAINSKIT_CONSUMER=local/)
})

test('explicit opt-in permits every Brainskit operation', () => {
  const guard = createMutationGuard(true)
  assert.equal(guard(execution('mcp__brainskit__apply')), undefined)
  assert.equal(
    guard(execution('mcp__brainskit__ask', { save: true })),
    undefined,
  )
})

test('plugin registers one prompt section and one monotonic guard', (t) => {
  const saved = process.env.BRAINSKIT_CONSUMER
  delete process.env.BRAINSKIT_CONSUMER
  t.after(() => {
    if (saved === undefined) delete process.env.BRAINSKIT_CONSUMER
    else process.env.BRAINSKIT_CONSUMER = saved
  })
  const registrations = {}
  apply({
    systemPrompt: {
      section(section) {
        registrations.section = section
      },
    },
    tools: {
      guard(guard) {
        registrations.guard = guard
      },
    },
  })

  assert.deepEqual(registrations.section, {
    name: 'tool:brainskit',
    order: 145,
    text: BRAINSKIT_GUIDANCE,
  })
  assert.equal(typeof registrations.guard, 'function')
  assert.match(
    registrations.guard(execution('mcp__brainskit__search', { consumer: 'local' })),
    /wider than the declared cloud/,
  )
})

test('plugin registers the local declaration when the operator opts in', (t) => {
  const saved = process.env.BRAINSKIT_CONSUMER
  process.env.BRAINSKIT_CONSUMER = 'local'
  t.after(() => {
    if (saved === undefined) delete process.env.BRAINSKIT_CONSUMER
    else process.env.BRAINSKIT_CONSUMER = saved
  })
  const registrations = {}
  apply({
    systemPrompt: { section: (section) => { registrations.section = section } },
    tools: { guard: (guard) => { registrations.guard = guard } },
  })
  assert.equal(registrations.section.text, brainskitGuidance('local'))
  assert.equal(registrations.guard(execution('mcp__brainskit__search', { consumer: 'local' })), undefined)
})
