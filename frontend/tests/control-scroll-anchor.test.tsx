import { useLayoutEffect, useRef, useState } from 'react'
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useControlScrollAnchor } from 'src/hooks/useControlScrollAnchor'

type ObserverHarness = { callback: ResizeObserverCallback; disconnect: ReturnType<typeof vi.fn> }

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function Harness({ scrollEl, reducedMotion = false }: { scrollEl: HTMLElement; reducedMotion?: boolean }) {
  const ownerRef = useRef<HTMLDivElement>(null)
  const regionRef = useRef<HTMLDivElement>(null)
  const [expanded, setExpanded] = useState(false)
  const [hydrated, setHydrated] = useState(false)
  const { capture, contentCommitted, layoutAnimationCompleted, layoutAnimationStarted, stabilize } =
    useControlScrollAnchor(scrollEl, ownerRef, () => scrollEl)

  useLayoutEffect(() => {
    stabilize(regionRef.current ?? ownerRef.current)
    if (expanded && !hydrated) return
    contentCommitted()
    if (reducedMotion) layoutAnimationCompleted()
  }, [contentCommitted, expanded, hydrated, layoutAnimationCompleted, reducedMotion, stabilize])

  return (
    <div ref={ownerRef}>
      <button
        type="button"
        data-testid="toggle"
        onClick={(event) => {
          capture(event.currentTarget)
          setExpanded((value) => !value)
        }}
      >toggle</button>
      <button type="button" data-testid="hydrate" onClick={() => setHydrated(true)}>hydrate</button>
      <button type="button" data-testid="layout-start" onClick={layoutAnimationStarted}>start</button>
      <button type="button" data-testid="layout-complete" onClick={layoutAnimationCompleted}>complete</button>
      {expanded && <div ref={regionRef} data-testid="region">{hydrated ? 'content' : 'loading'}</div>}
    </div>
  )
}

function createScroll() {
  const scroll = document.createElement('div')
  document.body.appendChild(scroll)
  Object.defineProperties(scroll, {
    scrollTop: { configurable: true, value: 120, writable: true },
    scrollHeight: { configurable: true, value: 1200 },
    clientHeight: { configurable: true, value: 300 },
  })
  return scroll
}

function installResizeObserver() {
  const observers: ObserverHarness[] = []
  vi.stubGlobal('ResizeObserver', class {
    callback: ResizeObserverCallback
    disconnect = vi.fn()
    constructor(callback: ResizeObserverCallback) {
      this.callback = callback
      observers.push(this)
    }
    observe() {}
    unobserve() {}
  })
  return observers
}

describe('useControlScrollAnchor', () => {
  it('pins the control through delayed hydration, collapse, and cached reopen with exact 78px shifts', () => {
    const observers = installResizeObserver()
    const scroll = createScroll()
    const { getByTestId } = render(<Harness scrollEl={scroll} />, { container: scroll })
    const control = getByTestId('toggle')
    let layoutTop = 220
    control.getBoundingClientRect = () => ({
      top: layoutTop - scroll.scrollTop, bottom: layoutTop - scroll.scrollTop + 44,
      left: 0, right: 44, width: 44, height: 44, x: 0, y: layoutTop - scroll.scrollTop,
      toJSON: () => ({}),
    })

    fireEvent.click(control)
    fireEvent.click(getByTestId('layout-start'))
    fireEvent.click(getByTestId('layout-complete'))
    expect(observers.at(-1)!.disconnect).not.toHaveBeenCalled()

    layoutTop += 78
    act(() => observers.at(-1)!.callback([], observers.at(-1) as unknown as ResizeObserver))
    expect(scroll.scrollTop).toBe(198)
    expect(control.getBoundingClientRect().top).toBe(100)

    fireEvent.click(getByTestId('hydrate'))
    fireEvent.click(getByTestId('layout-start'))
    fireEvent.click(getByTestId('layout-complete'))
    expect(observers.at(-1)!.disconnect).toHaveBeenCalled()
    expect(control.getBoundingClientRect().top).toBe(100)

    fireEvent.click(control)
    fireEvent.click(getByTestId('layout-start'))
    layoutTop -= 78
    scroll.scrollTop -= 78
    act(() => observers.at(-1)!.callback([], observers.at(-1) as unknown as ResizeObserver))
    fireEvent.click(getByTestId('layout-complete'))
    expect(scroll.scrollTop).toBe(120)
    expect(control.getBoundingClientRect().top).toBe(100)

    fireEvent.click(control)
    fireEvent.click(getByTestId('layout-start'))
    layoutTop += 78
    act(() => observers.at(-1)!.callback([], observers.at(-1) as unknown as ResizeObserver))
    fireEvent.click(getByTestId('layout-complete'))
    expect(scroll.scrollTop).toBe(198)
    expect(control.getBoundingClientRect().top).toBe(100)
    scroll.remove()
  })

  it('keeps correcting until both terminal content and final layout completion arrive', () => {
    const observers = installResizeObserver()
    const scroll = createScroll()
    const { getByTestId } = render(<Harness scrollEl={scroll} />, { container: scroll })
    const control = getByTestId('toggle')
    let layoutTop = 220
    control.getBoundingClientRect = () => ({
      top: layoutTop - scroll.scrollTop, bottom: layoutTop - scroll.scrollTop + 44,
      left: 0, right: 44, width: 44, height: 44, x: 0, y: layoutTop - scroll.scrollTop,
      toJSON: () => ({}),
    })

    fireEvent.click(control)
    fireEvent.click(getByTestId('layout-start'))
    fireEvent.click(getByTestId('layout-complete'))
    fireEvent.click(getByTestId('hydrate'))
    layoutTop += 78
    act(() => observers.at(-1)!.callback([], observers.at(-1) as unknown as ResizeObserver))
    expect(scroll.scrollTop).toBe(198)
    expect(observers.at(-1)!.disconnect).not.toHaveBeenCalled()

    fireEvent.click(getByTestId('layout-start'))
    layoutTop += 20
    act(() => observers.at(-1)!.callback([], observers.at(-1) as unknown as ResizeObserver))
    fireEvent.click(getByTestId('layout-complete'))
    expect(scroll.scrollTop).toBe(218)
    expect(observers.at(-1)!.disconnect).toHaveBeenCalled()
    scroll.remove()
  })

  it('settles immediately from committed layout when motion is reduced', () => {
    const observers = installResizeObserver()
    const scroll = createScroll()
    const { getByTestId } = render(<Harness scrollEl={scroll} reducedMotion />, { container: scroll })
    fireEvent.click(getByTestId('toggle'))
    expect(observers.at(-1)!.disconnect).not.toHaveBeenCalled()
    fireEvent.click(getByTestId('hydrate'))
    expect(observers.at(-1)!.disconnect).toHaveBeenCalled()
    scroll.remove()
  })
})
