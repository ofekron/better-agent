import { describe, expect, it } from 'vitest'

describe('production App module', () => {
  it('initializes the real startup module graph without a circular-import failure', async () => {
    const module = await import('../src/App')
    expect(typeof module.default).toBe('function')
  })
})
