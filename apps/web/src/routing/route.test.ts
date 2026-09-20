import { describe, expect, it } from 'vitest'

import { buildHash, parseHash } from './route'

describe('parseHash', () => {
  it('defaults to the plan view when the hash is empty', () => {
    expect(parseHash('').view).toBe('plan')
    expect(parseHash('#').view).toBe('plan')
  })

  it('reads a known view and its params', () => {
    const { view, params } = parseHash('#view=trace&kind=plan-value&id=42')
    expect(view).toBe('trace')
    expect(params.get('kind')).toBe('plan-value')
    expect(params.get('id')).toBe('42')
  })

  it('falls back to plan for an unrecognised view', () => {
    expect(parseHash('#view=nonsense').view).toBe('plan')
  })
})

describe('buildHash', () => {
  it('omits undefined/null/empty values', () => {
    expect(buildHash({ view: 'plan', trace: '' , scenario: undefined, claim: null })).toBe('view=plan')
  })

  it('round-trips through parseHash', () => {
    const hash = buildHash({ view: 'brain', claim: 7 })
    const { view, params } = parseHash(hash)
    expect(view).toBe('brain')
    expect(params.get('claim')).toBe('7')
  })
})
