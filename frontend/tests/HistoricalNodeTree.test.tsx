import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { HistoricalNodeTree, type HistoricalNodeRendererProps } from 'src/components/HistoricalNodeTree'
import { HistoricalHydrationStore, type HistoricalNodeManifest } from 'src/lib/historicalHydrationStore'

const node = (nodeId: string, childCount: number): HistoricalNodeManifest => ({
  sessionId: 'session',
  nodeId,
  revision: 'r1',
  childCount,
  summary: nodeId,
})

const renderNode = ({ manifest, expanded, expandable, toggleExpanded }: HistoricalNodeRendererProps) => (
  <div data-testid={`node-${manifest.nodeId}`}>
    <span>{manifest.summary}</span>
    {expandable && <button onClick={toggleExpanded}>{expanded ? '−' : '+'}</button>}
  </div>
)

describe('HistoricalNodeTree', () => {
  it('fetches only the explicitly expanded node and never its grandchildren', async () => {
    const client = vi.fn(async (manifest: HistoricalNodeManifest) => {
      if (manifest.nodeId === 'root') return { parent: manifest, children: [node('child', 1)] }
      return { parent: manifest, children: [node('grandchild', 0)] }
    })
    const store = new HistoricalHydrationStore(client)
    render(<HistoricalNodeTree store={store} manifest={node('root', 1)} renderNode={renderNode} />)

    expect(client).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button'))
    await screen.findByTestId('node-child')
    expect(client).toHaveBeenCalledTimes(1)
    expect(client.mock.calls[0][0].nodeId).toBe('root')
    expect(screen.queryByTestId('node-grandchild')).toBeNull()

    fireEvent.click(screen.getAllByRole('button')[1])
    await screen.findByTestId('node-grandchild')
    expect(client).toHaveBeenCalledTimes(2)
    expect(client.mock.calls[1][0].nodeId).toBe('child')

    fireEvent.click(screen.getAllByRole('button')[0])
    expect(screen.queryByTestId('node-child')).toBeNull()
    expect(screen.queryByTestId('node-grandchild')).toBeNull()
    expect(client).toHaveBeenCalledTimes(2)

    fireEvent.click(screen.getByRole('button'))
    await screen.findByTestId('node-child')
    expect(screen.queryByTestId('node-grandchild')).toBeNull()
    expect(client).toHaveBeenCalledTimes(2)
  })

  it('keeps five root turns in order while one turn renders only its direct payload', async () => {
    const client = vi.fn(async (manifest: HistoricalNodeManifest) => ({
      parent: manifest,
      children: [{ ...node(`${manifest.nodeId}-direct`, 1), renderPayload: { type: 'tool_call', data: { id: manifest.nodeId } } }],
    }))
    const store = new HistoricalHydrationStore(client)
    const roots = Array.from({ length: 5 }, (_, index) => node(`root-${index + 1}`, 1))
    render(<>{roots.map((root) => <HistoricalNodeTree key={root.nodeId} store={store} manifest={root} renderNode={renderNode} />)}</>)

    expect(screen.getAllByTestId(/^node-root-\d$/).map((element) => element.dataset.testid)).toEqual(
      roots.map((root) => `node-${root.nodeId}`),
    )
    fireEvent.click(screen.getAllByRole('button')[2])
    await screen.findByTestId('node-root-3-direct')
    expect(client).toHaveBeenCalledTimes(1)
    expect(client.mock.calls[0][0].nodeId).toBe('root-3')
    expect(screen.getAllByTestId(/^node-root-\d$/)).toHaveLength(5)
    expect(screen.queryByTestId('node-root-3-direct-direct')).toBeNull()
  })

  it('keeps the caller-rendered actionable node mounted while hydrating children', async () => {
    let resolve!: (response: { parent: HistoricalNodeManifest; children: HistoricalNodeManifest[] }) => void
    const request = new Promise<{ parent: HistoricalNodeManifest; children: HistoricalNodeManifest[] }>((done) => { resolve = done })
    const store = new HistoricalHydrationStore(() => request)
    const action = vi.fn()
    const actionableRenderer = (props: HistoricalNodeRendererProps) => (
      <div data-testid="action-card">
        <button onClick={action}>action</button>
        <button onClick={props.toggleExpanded}>expand</button>
      </div>
    )
    render(<HistoricalNodeTree store={store} manifest={node('actionable', 1)} renderNode={actionableRenderer} />)

    const card = screen.getByTestId('action-card')
    fireEvent.click(screen.getByText('expand'))
    fireEvent.click(screen.getByText('action'))
    expect(action).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('action-card')).toBe(card)
    resolve({ parent: node('actionable', 1), children: [node('child', 0)] })
    await waitFor(() => expect(screen.getAllByTestId('action-card')).toHaveLength(2))
    expect(screen.getAllByTestId('action-card')[0]).toBe(card)
  })

  it('aborts the node request when collapsed', () => {
    let signal: AbortSignal | undefined
    const store = new HistoricalHydrationStore((_manifest, requestSignal) => {
      signal = requestSignal
      return new Promise(() => undefined)
    })
    render(<HistoricalNodeTree store={store} manifest={node('root', 1)} renderNode={renderNode} />)

    fireEvent.click(screen.getByRole('button'))
    expect(signal?.aborted).toBe(false)
    fireEvent.click(screen.getByRole('button'))
    expect(signal?.aborted).toBe(true)
  })

  it('disables the load-more control while its cursor request is active', async () => {
    let resolvePage!: (response: { parent: HistoricalNodeManifest; children: HistoricalNodeManifest[]; hasMore: false }) => void
    const page = new Promise<{ parent: HistoricalNodeManifest; children: HistoricalNodeManifest[]; hasMore: false }>((done) => { resolvePage = done })
    const client = vi.fn()
      .mockResolvedValueOnce({ parent: node('root', 1), children: [node('child', 0)], nextCursor: 'next', hasMore: true })
      .mockImplementationOnce(() => page)
    const store = new HistoricalHydrationStore(client)
    render(<HistoricalNodeTree store={store} manifest={node('root', 1)} renderNode={renderNode} />)
    fireEvent.click(screen.getByRole('button'))
    const loadMore = await screen.findByRole('button', { name: /load/i })

    fireEvent.click(loadMore)
    expect((loadMore as HTMLButtonElement).disabled).toBe(true)
    expect(loadMore.getAttribute('aria-busy')).toBe('true')
    fireEvent.click(loadMore)
    expect(client).toHaveBeenCalledTimes(2)

    resolvePage({ parent: node('root', 1), children: [node('second', 0)], hasMore: false })
    await waitFor(() => expect(screen.queryByRole('button', { name: /load/i })).toBeNull())
  })

  it('keeps children visible and retries the failed page from its alert', async () => {
    const client = vi.fn()
      .mockResolvedValueOnce({ parent: node('root', 1), children: [node('first', 0)], nextCursor: 'next', hasMore: true })
      .mockRejectedValueOnce(new Error('page unavailable'))
      .mockResolvedValueOnce({ parent: node('root', 1), children: [node('second', 0)], hasMore: false })
    const store = new HistoricalHydrationStore(client)
    render(<HistoricalNodeTree store={store} manifest={node('root', 1)} renderNode={renderNode} />)
    fireEvent.click(screen.getByRole('button'))
    await screen.findByTestId('node-first')
    fireEvent.click(screen.getByRole('button', { name: /load/i }))

    const alert = await screen.findByRole('alert')
    expect(screen.getByTestId('node-first')).toBeTruthy()
    const retry = alert.querySelector<HTMLButtonElement>('.chat-load-error-retry')
    expect(retry).not.toBeNull()
    fireEvent.click(retry!)

    await screen.findByTestId('node-second')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(client.mock.calls[1][2]).toBe('next')
    expect(client.mock.calls[2][2]).toBe('next')
  })
})
