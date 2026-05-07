import type { EventLog, GarmentSku, GenerationImage, GenerationRun, ModelProfile, ShotPlan } from './types'

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: init?.body instanceof FormData ? init.headers : { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(typeof detail.detail === 'string' ? detail.detail : '请求失败')
  }
  return response.json() as Promise<T>
}

export const mediaUrl = (url?: string | null) => (url ? `${API_BASE}${url}` : '')

export const api = {
  health: () => request<Record<string, unknown>>('/api/health'),
  listSkus: () => request<{ items: GarmentSku[] }>('/api/garment-skus'),
  getSku: (id: number) => request<{ item: GarmentSku }>(`/api/garment-skus/${id}`),
  uploadSku: (sku: string, files: File[]) => {
    const form = new FormData()
    form.append('sku', sku)
    files.forEach((file) => form.append('files', file))
    return request<{ item: GarmentSku }>('/api/garment-skus', { method: 'POST', body: form })
  },
  listModels: () => request<{ items: ModelProfile[] }>('/api/model-profiles'),
  createPlan: (skuId: number, modelProfileId?: number) =>
    request<{ item: ShotPlan }>('/api/shot-plans', {
      method: 'POST',
      body: JSON.stringify({ skuId, modelProfileId }),
    }),
  generateGroup: (shotPlanId: number) =>
    request<{ item: GenerationRun }>('/api/generation-runs', {
      method: 'POST',
      body: JSON.stringify({ shotPlanId }),
    }),
  regenerate: (imageId: number, reason: string, note?: string) =>
    request<{ item: GenerationRun }>(`/api/generation-images/${imageId}/regenerate`, {
      method: 'POST',
      body: JSON.stringify({ reason, note }),
    }),
  setFinal: (imageId: number, isFinal: boolean) =>
    request<{ item: GenerationImage }>(`/api/generation-images/${imageId}/final`, {
      method: 'PATCH',
      body: JSON.stringify({ isFinal }),
    }),
  exportRun: async (runId: number) => {
    const response = await fetch(`${API_BASE}/api/exports/runs/${runId}`, { method: 'POST' })
    if (!response.ok) throw new Error('导出失败')
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `lily-run-${runId}.zip`
    anchor.click()
    URL.revokeObjectURL(url)
  },
  logs: () => request<{ items: EventLog[] }>('/api/logs?limit=30'),
}
