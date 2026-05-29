export type Page = 'studio' | 'library' | 'model' | 'brand' | 'shoot-config' | 'tag-rules' | 'records'

export type AppRoute = {
  page: Page
  taskId?: number
  recordId?: number
}

function positiveId(value: string | null) {
  const id = Number(value)
  return Number.isInteger(id) && id > 0 ? id : undefined
}

export function parseRoute(): AppRoute {
  const path = window.location.pathname.replace(/\/+$/, '') || '/'
  const params = new URLSearchParams(window.location.search)
  if (path === '/library') return { page: 'library' }
  if (path === '/model') return { page: 'model' }
  if (path === '/brand') return { page: 'brand' }
  if (path === '/shoot-config') return { page: 'shoot-config' }
  if (path === '/tag-rules') return { page: 'tag-rules' }
  if (path === '/records') return { page: 'records', recordId: positiveId(params.get('task')) }
  return { page: 'studio', taskId: positiveId(params.get('task')) }
}

export function routeToUrl(route: AppRoute) {
  if (route.page === 'library') return '/library'
  if (route.page === 'model') return '/model'
  if (route.page === 'brand') return '/brand'
  if (route.page === 'shoot-config') return '/shoot-config'
  if (route.page === 'tag-rules') return '/tag-rules'
  if (route.page === 'records') return route.recordId ? `/records?task=${route.recordId}` : '/records'
  return route.taskId ? `/studio?task=${route.taskId}` : '/studio'
}
