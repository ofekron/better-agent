import { cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import 'src/i18n'
import { HistoricalTurnDetails } from 'src/components/HistoricalTurnDetails'
import { MessageBubble } from 'src/components/MessageBubble'
import type { WSEvent, WorkerPanel } from 'src/types'
import { makeAssistantMsg } from './fixtures'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const root = {
  id: 'root', type: 'turn', revision: 'root-r1', direct_child_count: 1,
  display_summary: 'Historical work',
}

async function renderHistorical(renderPayload: WSEvent | WorkerPanel) {
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({
    session_id: 's1', message_id: 'a1', parent_id: 'root', revision: 'root-r1', parent: root,
    children: [{
      id: 'node', type: 'event', revision: 'node-r1', direct_child_count: 1,
      display_summary: 'Expand historical child', render_payload: renderPayload,
    }],
    next_cursor: null, has_more: false,
  }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  const rendered = render(<HistoricalTurnDetails sessionId="s1" messageId="a1" manifest={root} active />)
  await waitFor(() => expect(rendered.container.querySelector('.canonical-row-core')).not.toBeNull())
  return rendered.container
}

function expectParity(liveCore: Element, historical: HTMLElement) {
  const historicalCore = historical.querySelector('.canonical-row-core')!
  expect(historicalCore.className).toBe('canonical-row-core')
  expect(historicalCore.innerHTML).toBe(liveCore.innerHTML)
  expect(liveCore.querySelectorAll('.historical-child-toggle')).toHaveLength(0)
  expect(historical.querySelectorAll('.historical-child-toggle')).toHaveLength(1)
  expect(historical.querySelectorAll('.raw-toggle')).toHaveLength(0)
}

describe('historical canonical row parity', () => {
  it.each([
    ['agent message', { type: 'agent_message', data: { type: 'assistant', message: { content: [{ type: 'text', text: 'same agent output' }] } } }],
    ['tool event', { type: 'tool_call', data: { tool: 'Read', args: { path: '/tmp/a' }, tool_use_id: 'tool-1' } }],
  ] as Array<[string, WSEvent]>)('matches the actual live MessageBubble %s row except its one child control', async (_name, event) => {
    const live = render(<MessageBubble message={makeAssistantMsg({ id: 'a1', content: '', events: [event] })} sessionId="s1" orchestrationMode="native" />).container
    const liveCore = live.querySelector('.message-content')!
    expect(liveCore).not.toBeNull()
    expectParity(liveCore, await renderHistorical(event))
  })

  it('matches the actual live manager WorkerPanel row except its one child control', async () => {
    const worker: WorkerPanel = {
      delegation_id: 'd1', worker_session_id: 'worker-s1', worker_description: 'same worker',
      is_new: false, instructions_preview: 'inspect', events: [],
    }
    const live = render(<MessageBubble message={makeAssistantMsg({ id: 'a1', content: '', workers: [worker] })} sessionId="s1" orchestrationMode="manager" />).container
    const liveCore = live.querySelector('.message-content')!
    expect(liveCore).not.toBeNull()
    expectParity(liveCore, await renderHistorical(worker))
  })
})
