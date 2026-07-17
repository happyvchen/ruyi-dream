import { describe, expect, it } from 'vitest'
import { routeToUrl } from '../../routes'

describe('workspace route serialization', () => {
  it('keeps core business object in the URL and leaves temporary UI state out', () => {
    expect(routeToUrl({ page: 'studio' })).toBe('/studio')
    expect(routeToUrl({ page: 'studio', taskId: 42 })).toBe('/studio?task=42')
    expect(routeToUrl({ page: 'records', recordId: 42 })).toBe('/records?task=42')
  })

  it('supports the five product pages defined by the 0.1.0 workflow', () => {
    expect(routeToUrl({ page: 'library' })).toBe('/library')
    expect(routeToUrl({ page: 'model' })).toBe('/model')
    expect(routeToUrl({ page: 'prompt-maintenance' })).toBe('/prompt-maintenance')
    expect(routeToUrl({ page: 'records' })).toBe('/records')
  })
})
