export type Page = 'generate' | 'garments' | 'models' | 'history'

export type AppRoute = {
  page: Page
  sku?: string
  modelId?: string
  runId?: number
  generateSku?: string
  generateRunId?: number
  generatePreviewRunId?: number
}

export function parseRoute(): AppRoute {
  const path = window.location.pathname.replace(/\/+$/, '') || '/'
  const parts = path.split('/').filter(Boolean).map(decodeURIComponent)
  const params = new URLSearchParams(window.location.search)
  if (parts[0] === 'garment-skus') return { page: 'garments', sku: parts[1] }
  if (parts[0] === 'model-profiles') return { page: 'models', modelId: parts[1] }
  if (parts[0] === 'generation-runs') {
    const runId = Number(parts[1])
    return { page: 'history', runId: Number.isFinite(runId) ? runId : undefined }
  }
  if (parts[0] === 'generate') {
    const runId = Number(params.get('run'))
    const previewRunId = Number(params.get('preview'))
    return {
      page: 'generate',
      generateSku: params.get('sku') || undefined,
      generateRunId: Number.isFinite(runId) ? runId : undefined,
      generatePreviewRunId: Number.isFinite(previewRunId) ? previewRunId : undefined,
    }
  }
  return { page: 'generate' }
}

export function routeToUrl(route: AppRoute) {
  if (route.page === 'garments') return route.sku ? `/garment-skus/${encodeURIComponent(route.sku)}` : '/garment-skus'
  if (route.page === 'models') return route.modelId ? `/model-profiles/${encodeURIComponent(route.modelId)}` : '/model-profiles'
  if (route.page === 'history') return route.runId ? `/generation-runs/${route.runId}` : '/generation-runs'
  if (route.page === 'generate') {
    const params = new URLSearchParams()
    if (route.generateRunId) params.set('run', String(route.generateRunId))
    else {
      if (route.generateSku) params.set('sku', route.generateSku)
      if (route.generatePreviewRunId) params.set('preview', String(route.generatePreviewRunId))
    }
    const query = params.toString()
    return query ? `/generate?${query}` : '/generate'
  }
  return '/generate'
}
