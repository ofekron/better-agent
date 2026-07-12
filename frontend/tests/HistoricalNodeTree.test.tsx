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
})
