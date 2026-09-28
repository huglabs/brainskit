import assert from 'node:assert/strict'
import test from 'node:test'

import {
  BRAINSKIT_GUIDANCE,
  apply,
  createMutationGuard,
} from '../index.js'

function execution(name, argumentsValue = {}) {
  return { name, arguments: argumentsValue }
}

test('default guard permits retrieval and append-only capture', () => {
  const guard = createMutationGuard()
  assert.equal(guard(execution('mcp__brainskit__search', { consumer: 'local' })), undefined)
  assert.equal(guard(execution('mcp__brainskit__context', { consumer: 'cloud' })), undefined)
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

test('human consumer is denied with and without the mutation opt-in', () => {
  for (const guard of [createMutationGuard(), createMutationGuard(true)]) {
    for (const name of ['mcp__brainskit__search', 'mcp__brainskit__context']) {
      assert.match(guard(execution(name, { consumer: 'human' })), /local or cloud/)
      assert.match(guard(execution(name, {})), /local or cloud/)
    }
  }
})

test('explicit opt-in permits every Brainskit operation', () => {
  const guard = createMutationGuard(true)
  assert.equal(guard(execution('mcp__brainskit__apply')), undefined)
  assert.equal(
    guard(execution('mcp__brainskit__ask', { save: true })),
    undefined,
  )
})

test('plugin registers one prompt section and one monotonic guard', () => {
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
})
