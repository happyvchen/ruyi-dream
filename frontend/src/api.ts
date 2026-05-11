import type { EventLog, GarmentImage, GarmentSku, GenerationImage, GenerationRun, ModelProfile, ShotPlan } from './types'

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000'

function apiHeaders(init?: RequestInit): HeadersInit {
  const headers = new Headers(init?.headers)
  if (!(init?.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  return headers
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: 'include',
    headers: apiHeaders(init),
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(typeof detail.detail === 'string' ? detail.detail : '请求失败')
  }
  return response.json() as Promise<T>
}

export const mediaUrl = (url?: string | null) => {
  if (!url) return ''
  return /^https?:\/\//i.test(url) ? url : `${API_BASE}${url}`
}

export const api = {
  health: () => request<Record<string, unknown>>('/api/health'),
  session: () => request<{ authRequired: boolean; authenticated: boolean }>('/api/auth/session'),
  login: (password: string) =>
    request<{ authRequired: boolean; authenticated: boolean }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  logout: () => request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  listSkus: (options?: { includeArchived?: boolean }) =>
    request<{ items: GarmentSku[] }>(`/api/garment-skus${options?.includeArchived ? '?includeArchived=true' : ''}`),
  getSku: (id: number) => request<{ item: GarmentSku }>(`/api/garment-skus/${id}`),
  uploadSku: (sku: string, files: File[], title?: string) => {
    const form = new FormData()
    form.append('sku', sku)
    if (title) form.append('title', title)
    files.forEach((file) => form.append('files', file))
    return request<{ item: GarmentSku }>('/api/garment-skus', { method: 'POST', body: form })
  },
  updateSku: (id: number, payload: { sku?: string; title?: string; status?: string }) =>
    request<{ item: GarmentSku }>(`/api/garment-skus/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  deleteSku: (id: number, options?: { purge?: boolean; confirmSku?: string }) => {
    const params = new URLSearchParams()
    if (options?.purge) params.set('purge', 'true')
    if (options?.confirmSku) params.set('confirmSku', options.confirmSku)
    const suffix = params.size ? `?${params.toString()}` : ''
    return request<{ ok: boolean; archived?: boolean; purged?: boolean; objectCleanupErrors?: { prefix: string; error: string }[] }>(`/api/garment-skus/${id}${suffix}`, {
      method: 'DELETE',
    })
  },
  addSkuImages: (id: number, files: File[]) => {
    const form = new FormData()
    files.forEach((file) => form.append('files', file))
    return request<{ item: GarmentSku }>(`/api/garment-skus/${id}/images`, { method: 'POST', body: form })
  },
  updateGarmentImage: (id: number, payload: { role?: string; sortOrder?: number }) =>
    request<{ item: GarmentImage }>(`/api/garment-images/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  deleteGarmentImage: (id: number) =>
    request<{ ok: boolean }>(`/api/garment-images/${id}`, {
      method: 'DELETE',
    }),
  listModels: () => request<{ items: ModelProfile[] }>('/api/model-profiles'),
  createModel: (payload: { modelId: string; name: string; isDefault?: boolean; profile?: Record<string, unknown> }) =>
    request<{ item: ModelProfile }>('/api/model-profiles', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updateModel: (id: number, payload: { modelId?: string; name?: string; isDefault?: boolean; profile?: Record<string, unknown> }) =>
    request<{ item: ModelProfile }>(`/api/model-profiles/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  deleteModel: (id: number) =>
    request<{ ok: boolean }>(`/api/model-profiles/${id}`, {
      method: 'DELETE',
    }),
  addModelHeadReferences: (id: number, files: File[], angle?: string) => {
    const form = new FormData()
    if (angle) form.append('angle', angle)
    files.forEach((file) => form.append('files', file))
    return request<{ item: ModelProfile }>(`/api/model-profiles/${id}/head-references`, { method: 'POST', body: form })
  },
  updateModelHeadReference: (id: number, sourceAngle: string, payload: { angle?: string; orderIndex?: number }) =>
    request<{ item: ModelProfile }>(`/api/model-profiles/${id}/head-references/${encodeURIComponent(sourceAngle)}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  replaceModelHeadReference: (id: number, angle: string, file: File) => {
    const form = new FormData()
    form.append('angle', angle)
    form.append('file', file)
    return request<{ item: ModelProfile }>(`/api/model-profiles/${id}/head-references`, { method: 'POST', body: form })
  },
  deleteModelHeadReference: (id: number, angle: string) =>
    request<{ item: ModelProfile }>(`/api/model-profiles/${id}/head-references/${encodeURIComponent(angle)}`, {
      method: 'DELETE',
    }),
  createPlan: (skuId: number, modelProfileId?: number) =>
    request<{ item: ShotPlan }>('/api/shot-plans', {
      method: 'POST',
      body: JSON.stringify({ skuId, modelProfileId }),
    }),
  getDefaultPlan: (skuId: number) =>
    request<{ item: ShotPlan | null }>(`/api/garment-skus/${skuId}/default-shot-plan`),
  getPlan: (shotPlanId: number) => request<{ item: ShotPlan }>(`/api/shot-plans/${shotPlanId}`),
  updatePlan: (shotPlanId: number, plan: ShotPlan['plan']) =>
    request<{ item: ShotPlan }>(`/api/shot-plans/${shotPlanId}`, {
      method: 'PATCH',
      body: JSON.stringify({ plan }),
    }),
  previewGrid: (shotPlanId: number) =>
    request<{ item: GenerationRun }>('/api/preview-runs', {
      method: 'POST',
      body: JSON.stringify({ shotPlanId }),
    }),
  generateGroup: (shotPlanId: number, previewRunId?: number) =>
    request<{ item: GenerationRun }>('/api/generation-runs', {
      method: 'POST',
      body: JSON.stringify({ shotPlanId, previewRunId }),
    }),
  listRuns: (options?: { includeArchived?: boolean }) =>
    request<{ items: GenerationRun[] }>(`/api/generation-runs${options?.includeArchived ? '?includeArchived=true' : ''}`),
  getRun: (runId: number) => request<{ item: GenerationRun }>(`/api/generation-runs/${runId}`),
  deleteRun: (runId: number) =>
    request<{ ok: boolean }>(`/api/generation-runs/${runId}`, {
      method: 'DELETE',
    }),
  cancelRun: (runId: number) =>
    request<{ item: GenerationRun }>(`/api/generation-runs/${runId}/cancel`, {
      method: 'POST',
    }),
  regenerate: (imageId: number, reason: string, note?: string) =>
    request<{ item: GenerationRun }>(`/api/generation-images/${imageId}/regenerate`, {
      method: 'POST',
      body: JSON.stringify({ reason, note }),
    }),
  cancelImage: (imageId: number) =>
    request<{ item: GenerationImage }>(`/api/generation-images/${imageId}/cancel`, {
      method: 'POST',
    }),
  setFinal: (imageId: number, isFinal: boolean) =>
    request<{ item: GenerationImage }>(`/api/generation-images/${imageId}/final`, {
      method: 'PATCH',
      body: JSON.stringify({ isFinal }),
    }),
  setReviewStatus: (imageId: number, status: string, note?: string) =>
    request<{ item: GenerationImage }>(`/api/generation-images/${imageId}/review`, {
      method: 'PATCH',
      body: JSON.stringify({ status, note }),
    }),
  exportRun: async (runId: number) => {
    const response = await fetch(`${API_BASE}/api/exports/runs/${runId}`, { method: 'POST', credentials: 'include', headers: apiHeaders() })
    if (!response.ok) throw new Error('导出失败')
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `generation-run-${runId}.zip`
    anchor.click()
    URL.revokeObjectURL(url)
  },
  logs: () => request<{ items: EventLog[] }>('/api/logs?limit=30'),
}
