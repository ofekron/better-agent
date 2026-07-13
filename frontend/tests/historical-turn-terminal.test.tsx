import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import 'src/i18n'
import { HistoricalTurnDetails } from 'src/components/HistoricalTurnDetails'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const manifest = {
  id: 'root',
  type: 'turn',
  revision: 'revision-1',
  direct_child_count: 0,
  display_summary: 'No historical work',
}

describe('HistoricalTurnDetails terminal lifecycle', () => {
  it('commits an empty ready snapshot without owning layout completion', async () => {
    let resolveFetch!: (response: Response) => void
    vi.spyOn(globalThis, 'fetch').mockReturnValue(new Promise((resolve) => { resolveFetch = resolve }))
    render(<HistoricalTurnDetails sessionId="session-1" messageId="message-1" manifest={manifest} active />)
    resolveFetch(new Response(JSON.stringify({
      session_id: 'session-1', message_id: 'message-1', parent_id: 'root', revision: 'revision-1',
      parent: manifest, children: [], next_cursor: null, has_more: false,
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    expect((await screen.findByRole('status')).textContent).toContain('No historical work')
  })

  it('commits an error snapshot without owning layout completion', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('failure', { status: 500 }))
    render(<HistoricalTurnDetails sessionId="session-1" messageId="message-2" manifest={manifest} active />)
    expect((await screen.findByRole('alert')).textContent).toContain('Historical children request failed: 500')
  })
})
