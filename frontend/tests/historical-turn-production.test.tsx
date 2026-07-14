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
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      unobserve() {}
      disconnect() {}
    })
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
    const messages = Array.from({ length: 5 }, (_, index) => {
      const number = index + 1
      const response = number === 1
        ? assistant
        : makeAssistantMsg({ id: `a${number}`, content: `Finished ${number}`, isStreaming: false })
      return [makeUserMsg({ id: `u${number}`, content: `Investigate ${number}` }), response]
    }).flat()
    const view = (nextMessages = messages, nextSession = session) => (
      <Profiler id="historical-chat" onRender={() => { commits += 1 }}><Chat
        messages={nextMessages}
        pendingMessages={[]}
        runs={[]}
        streamingEvents={[]}
        isStreaming={false}
        isStopping={false}
        streamingLoadPhase={null}
        onSend={() => true}
        disabled={false}
        session={nextSession}
        draft=""
        onDraftChange={() => undefined}
        queuedPrompt={null}
        onPromoteQueued={() => undefined}
      /></Profiler>
    )
    const { container, rerender } = render(view())

    expect(childRequests()).toHaveLength(0)
    const rootIds = () => Array.from(container.querySelectorAll<HTMLElement>('.user-message-box')).map((element) => element.dataset.messageId)
    expect(rootIds()).toEqual(['u1', 'u2', 'u3', 'u4', 'u5'])
    expect(container.querySelectorAll('.message-box-header-main')).toHaveLength(5)
    const processControl = await screen.findByRole<HTMLButtonElement>('button', { name: 'Expand process' })
    const owner = processControl.closest<HTMLElement>('.turn-group')!
    const chevron = owner.querySelector<HTMLButtonElement>('.message-box-header-main')!
    expect(processControl.getAttribute('aria-label')).toBe('Expand process')
    expect(processControl.getAttribute('aria-controls')).toBe(owner.querySelector('.historical-work-region')?.id)
    expect(container.querySelector('.historical-turn-details')).toBeNull()
    await waitFor(() => expect(processControl.querySelector('.historical-work-hint')?.textContent).toBe('• • •'))
    expect(owner.contains(owner.querySelector('.historical-work-hint'))).toBe(true)
    fireEvent.click(chevron)
    expect(owner.querySelector('[data-testid="assistant-message"][data-message-id="a1"]')?.textContent).toContain('Finished')
    expect(rootIds()).toEqual(['u1', 'u2', 'u3', 'u4', 'u5'])
    expect(childRequests()).toHaveLength(0)
    expect(fetchMock.mock.calls.filter(([input]) => input.toString().includes('/events'))).toHaveLength(0)
    expect(fetchMock.mock.calls.filter(([input]) => input.toString().includes('cursor='))).toHaveLength(0)
    expect(container.querySelector('.historical-turn-details')).toBeNull()
    fireEvent.click(processControl)
    await screen.findByText('Historical worker')
    const historicalRegion = owner.querySelector<HTMLElement>('.historical-work-region')!
    const finalResult = owner.querySelector<HTMLElement>('[data-testid="assistant-answer-content"]')!
    expect(historicalRegion.compareDocumentPosition(finalResult) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0)
    expect(processControl.getAttribute('aria-label')).toBe('Collapse process')
    expect(owner.querySelector('.historical-work-hint')).toBeNull()
    expect(rootIds()).toEqual(['u1', 'u2', 'u3', 'u4', 'u5'])
    expect(container.querySelectorAll('.message-box-header-main')).toHaveLength(5)
    expect(container.querySelectorAll('[data-message-id^="__synth-"]')).toHaveLength(0)
    expect(container.querySelectorAll('.historical-work-region .message-row')).toHaveLength(0)
    expect(owner.querySelector('.historical-work-region')?.closest('.turn-group-children')).not.toBeNull()
    expect(owner.querySelectorAll('.historical-child-toggle')).toHaveLength(1)
    expect(owner.querySelectorAll('.raw-toggle')).toHaveLength(0)
    expect(screen.getByText('Unsupported historical payload')).toBeTruthy()
    expect(childRequests()).toHaveLength(1)
    expect(childRequests()[0][0].toString()).toContain('parent_id=root')
    expect(screen.queryByText('Nested result')).toBeNull()

    const pinnedControl = owner.querySelector<HTMLButtonElement>('.historical-process-toggle')!
    const controlForAssistant = () => container
      .querySelector<HTMLElement>('[data-testid="assistant-message"][data-message-id="a1"]')
      ?.closest('.turn-group')
      ?.querySelector<HTMLButtonElement>('.historical-process-toggle')
    const appended = [...messages, makeUserMsg({ id: 'u6', content: 'Live append' }), makeAssistantMsg({ id: 'a6', content: 'Live reply' })]
    rerender(view(appended))
    expect(controlForAssistant()).toBe(pinnedControl)
    expect(pinnedControl.getAttribute('aria-expanded')).toBe('true')
    expect(childRequests()).toHaveLength(1)

    const revised = appended.map((message) => message.id === 'a1' ? { ...message, content: 'Finished revision' } : message)
    rerender(view(revised))
    expect(controlForAssistant()).toBe(pinnedControl)
    expect(container.textContent).toContain('Finished revision')
    expect(childRequests()).toHaveLength(1)

    const prepended = [makeUserMsg({ id: 'u0', content: 'Older prompt' }), makeAssistantMsg({ id: 'a0', content: 'Older reply' }), ...revised]
    rerender(view(prepended))
    expect(controlForAssistant()).toBe(pinnedControl)
    expect(pinnedControl.getAttribute('aria-expanded')).toBe('true')
    expect(childRequests()).toHaveLength(1)

    fireEvent.click(screen.getByRole('button', { name: 'Tool event' }))
    await screen.findByText('Nested result')
    expect(childRequests()).toHaveLength(2)
    expect(childRequests()[1][0].toString()).toContain('parent_id=event')

    fireEvent.click(processControl)
    await waitFor(() => expect(container.querySelector('.historical-turn-details')).toBeNull())
    fireEvent.click(processControl)
    await screen.findByText('Historical worker')
    expect(childRequests()).toHaveLength(2)
    expect(commits).toBeLessThan(35)

    const switchedSession = { ...session, id: 'session-2' }
    rerender(view([
      makeUserMsg({ id: 'u1', content: 'New session prompt' }),
      Object.assign(makeAssistantMsg({ id: 'a1', content: 'New session reply' }), { historical_hydration_root: root }),
    ], switchedSession))
    const switchedControl = controlForAssistant()
    expect(switchedControl).not.toBe(pinnedControl)
    expect(switchedControl?.getAttribute('aria-expanded')).toBe('false')
  })
})
