import { act, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const extensionIds = vi.hoisted(() => ({
  loadBuiltinExtensionIds: vi.fn<() => Promise<boolean>>(),
}))
const logger = vi.hoisted(() => ({ installFrontendLogger: vi.fn(), logFailure: vi.fn(), logTiming: vi.fn() }))

vi.mock('../src/extensionIds', () => extensionIds)
vi.mock('../src/App', () => ({ default: () => <main data-testid="app-shell">Better Agent</main> }))
vi.mock('../src/components/ScreenWakeLock', () => ({ ScreenWakeLock: () => null }))
vi.mock('../src/lib/frontendLogger', () => logger)
vi.mock('@capacitor/core', () => ({ Capacitor: { isNativePlatform: () => false } }))

describe('production bootstrap', () => {
  beforeEach(() => {
    vi.resetModules()
    extensionIds.loadBuiltinExtensionIds.mockReset()
    extensionIds.loadBuiltinExtensionIds.mockImplementation(() => new Promise<boolean>(() => {}))
    logger.logFailure.mockReset()
    document.body.innerHTML = '<div id="root"></div>'
  })

  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('renders the app shell while extension metadata remains pending', async () => {
    await act(async () => { await import('../src/main') })
    expect(extensionIds.loadBuiltinExtensionIds).toHaveBeenCalledOnce()
    await waitFor(() => expect(document.querySelector('[data-testid="app-shell"]')?.textContent).toBe('Better Agent'))
  })

  it('keeps the shell mounted and reports extension bootstrap rejection', async () => {
    extensionIds.loadBuiltinExtensionIds.mockRejectedValueOnce(new Error('backend restarting'))
    await act(async () => { await import('../src/main') })
    await waitFor(() => expect(document.querySelector('[data-testid="app-shell"]')?.textContent).toBe('Better Agent'))
    await waitFor(() => expect(logger.logFailure).toHaveBeenCalledWith('boot', 'builtin_extension_ids_failed', expect.any(Error)))
    expect(extensionIds.loadBuiltinExtensionIds).toHaveBeenCalledOnce()
  })
})
