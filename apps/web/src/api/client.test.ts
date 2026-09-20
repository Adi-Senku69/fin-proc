import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from './client'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('api.get', () => {
  it('returns ok:true with the parsed body on a 200', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ a: 1 }), { status: 200 })),
    )
    const res = await api.get<{ a: number }>('/x')
    expect(res.ok).toBe(true)
    if (res.ok) expect(res.data).toEqual({ a: 1 })
  })

  it('surfaces the FastAPI `detail` string as the message on a non-2xx response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'boom' }), { status: 422 })),
    )
    const res = await api.get('/x')
    expect(res.ok).toBe(false)
    if (!res.ok) {
      expect(res.status).toBe(422)
      expect(res.message).toBe('boom')
    }
  })

  it('reports status 0 on a network failure, never throws', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockRejectedValue(new Error('offline')),
    )
    const res = await api.get('/x')
    expect(res.ok).toBe(false)
    if (!res.ok) {
      expect(res.status).toBe(0)
      expect(res.message).toContain('offline')
    }
  })
})
