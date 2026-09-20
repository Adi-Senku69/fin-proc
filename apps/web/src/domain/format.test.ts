import { describe, expect, it } from 'vitest'

import { fmtDate, fmtMoney, fmtNum, fmtParam } from './format'

describe('fmtNum', () => {
  it('renders null/undefined/empty as a dash', () => {
    expect(fmtNum(null)).toBe('-')
    expect(fmtNum(undefined)).toBe('-')
    expect(fmtNum('')).toBe('-')
  })

  it('uses 1 decimal for magnitudes >= 100, 4 otherwise', () => {
    expect(fmtNum(1234.5)).toBe('1,234.5')
    expect(fmtNum(0.123456)).toBe('0.1235')
  })

  it('passes non-numbers through as strings', () => {
    expect(fmtNum('abc')).toBe('abc')
  })
})

describe('fmtMoney', () => {
  it('appends the unit', () => {
    expect(fmtMoney(12.3)).toBe('12.3 k EUR')
    expect(fmtMoney(null)).toBe('-')
  })
})

describe('fmtDate', () => {
  it('falls back to the raw string for an unparseable date', () => {
    expect(fmtDate(null)).toBe('-')
    expect(fmtDate('not-a-date')).toBe('not-a-date')
  })
})

describe('fmtParam', () => {
  it('renders year-like keys as bare integers, no thousands separator', () => {
    expect(fmtParam('year', 2027)).toBe('2027')
    expect(fmtParam('window_from', 2020)).toBe('2020')
  })

  it('renders a genuine quantity with thousands separators when it is a whole number', () => {
    expect(fmtParam('n', 1234)).toBe('1,234')
  })

  it('keeps fractional figures through fmtNum', () => {
    expect(fmtParam('alpha', 12.5)).toBe(fmtNum(12.5))
  })
})
