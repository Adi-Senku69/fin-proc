import { describe, expect, it } from 'vitest'

import { categoryName, MARKED_PATHS, pathTerm, prettifyLabel, tagTerm } from './terms'

describe('categoryName', () => {
  it('maps known codes to plain names', () => {
    expect(categoryName('PERS')).toBe('Personnel costs')
  })
  it('falls back to the raw code for an unknown one', () => {
    expect(categoryName('XYZ')).toBe('XYZ')
  })
})

describe('pathTerm / MARKED_PATHS', () => {
  it('marks decided and ai_proposed, not cascaded or valorized', () => {
    expect(MARKED_PATHS.has('decided')).toBe(true)
    expect(MARKED_PATHS.has('ai_proposed')).toBe(true)
    expect(MARKED_PATHS.has('cascaded')).toBe(false)
  })
  it('gives every known path a plain label distinct from the raw code', () => {
    expect(pathTerm('cascaded').label).not.toBe('cascaded')
  })
})

describe('tagTerm', () => {
  it('classifies a provenance tag by its leading keyword', () => {
    expect(tagTerm('(stakeholder-verbal, Dana, 2026-01-01)').label).toBe('said by a stakeholder')
    expect(tagTerm('[ingestion/foo](../ingestion/foo)').label).toBe('synthesized record')
  })
  it('falls back to the raw tag when nothing matches', () => {
    expect(tagTerm('(something-unknown)').label).toBe('(something-unknown)')
  })
})

describe('prettifyLabel', () => {
  it('replaces category codes and path enum values with plain words', () => {
    expect(prettifyLabel('REV 2026 (base, valorized)')).toBe('Revenue 2026 (base, grown on its own trend)')
  })
})
