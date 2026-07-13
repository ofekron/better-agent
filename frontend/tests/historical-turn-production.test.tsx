import { Profiler } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import 'src/i18n'
import { Chat } from 'src/components/Chat'
import { makeAssistantMsg, makeSession, makeUserMsg } from './fixtures'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('historical turn production hydration', () => {
  it('uses the turn chevron as the only root expansion and reuses its exact-revision cache', async () => {
    const session = makeSession()
    const root = {
      id: 'root',
      type: 'turn',
      revision: 'revision-1',
      direct_child_count: 3,
      display_summary: 'Historical work',
    }
    const rootResponse = {
      session_id: session.id,
      message_id: 'a1',
      parent_id: 'root',
      revision: 'revision-1',
      parent: root,
      children: [
        {
          id: 'event', type: 'event', revision: 'event-r1', direct_child_count: 1, display_summary: 'Tool event',
          render_payload: { type: 'tool_call', data: { id: 'tool-1', name: 'Read', input: { path: '/tmp/example' } } },
        },
        {
          id: 'worker', type: 'worker', revision: 'worker-r1', direct_child_count: 0, display_summary: 'Worker summary',
          render_payload: {
            delegation_id: 'delegation-1', worker_session_id: 'worker-session', worker_description: 'Historical worker',
            is_new: false, instructions_preview: 'Inspect history', events: [],
          },
        },
        {
          id: 'diagnostic', type: 'future', revision: 'future-r1', direct_child_count: 0, display_summary: 'Unsupported historical payload',
          render_payload: { future_shape: true },
        },
      ],
    }
    const nestedResponse = {
      session_id: session.id,
      message_id: 'a1',
      parent_id: 'event',
      revision: 'event-r1',
      parent: rootResponse.children[0],
      children: [{
        id: 'nested', type: 'event', revision: 'nested-r1', direct_child_count: 0, display_summary: 'Nested result',
        render_payload: {
          type: 'agent_message',
          data: {
            type: 'assistant',
            message: { content: [{ type: 'text', text: 'Nested result' }] },
          },
        },
      }],
    }
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = input.toString()
      if (url.includes('/children?')) {
        const payload = url.includes('parent_id=event') ? nestedResponse : rootResponse
        return new Response(JSON.stringify(payload), { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } })
    })
    const childRequests = () => fetchMock.mock.calls.filter(([input]) => input.toString().includes('/children?'))
    let commits = 0
    const assistant = makeAssistantMsg({ id: 'a1', content: 'Finished', isStreaming: false })
    assistant.historical_hydration_root = root
    const { container } = render(
      <Profiler id="historical-chat" onRender={() => { commits += 1 }}><Chat
        messages={[makeUserMsg({ id: 'u1', content: 'Investigate' }), assistant]}
        pendingMessages={[]}
        runs={[]}
        streamingEvents={[]}
        isStreaming={false}
        isStopping={false}
        streamingLoadPhase={null}
        onSend={() => true}
        disabled={false}
        session={session}
        draft=""
        onDraftChange={() => undefined}
        queuedPrompt={null}
        onPromoteQueued={() => undefined}
      /></Profiler>,
    )

    expect(childRequests()).toHaveLength(0)
    const chevron = screen.getByRole('button', { name: /User/i })
    expect(container.querySelector('.historical-turn-details')).toBeNull()
    fireEvent.click(chevron)
    await screen.findByText('Historical worker')
    expect(screen.getByText('Unsupported historical payload')).toBeTruthy()
    expect(childRequests()).toHaveLength(1)
    expect(childRequests()[0][0].toString()).toContain('parent_id=root')
    expect(screen.queryByText('Nested result')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Tool event' }))
    await screen.findByText('Nested result')
    expect(childRequests()).toHaveLength(2)
    expect(childRequests()[1][0].toString()).toContain('parent_id=event')

    fireEvent.click(chevron)
    await waitFor(() => expect(container.querySelector('.historical-turn-details')).toBeNull())
    fireEvent.click(chevron)
    await screen.findByText('Historical worker')
    expect(childRequests()).toHaveLength(2)
    expect(commits).toBeLessThan(20)
  })
})
