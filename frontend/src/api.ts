export const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000'

export type SessionRole = 'user' | 'admin'

export type AuthSession = {
  authRequired: boolean
  authenticated: boolean
  role?: SessionRole | null
}

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export function isAuthExpiredError(error: unknown) {
  return error instanceof ApiError && error.status === 401
}

export type Paginated<T> = {
  items: T[]
  limit: number
  offset: number
  nextOffset: number | null
  hasMore: boolean
  total: number
  totalPages: number
}

export type FacetOption = {
  value: string
  label: string
  count: number
}

export type CategoryTreeOption = FacetOption & {
  children?: FacetOption[]
}

export type StylingGarmentFacets = {
  status?: FacetOption[]
  category?: FacetOption[]
  primaryCategory?: FacetOption[]
  secondaryCategory?: FacetOption[]
  categoryTree?: CategoryTreeOption[]
  color?: FacetOption[]
  season?: FacetOption[]
  series?: FacetOption[]
  channel?: FacetOption[]
  reviewStatus?: FacetOption[]
}

export type StylingGarmentListResponse = Paginated<StylingGarment> & {
  facets?: StylingGarmentFacets
  stableFacets?: StylingGarmentFacets
}

export type MediaAsset = {
  id: number
  objectKey: string
  filename: string
  mimeType: string
  width?: number | null
  height?: number | null
  sizeBytes?: number | null
  originalFilename?: string | null
  originalMimeType?: string | null
  originalWidth?: number | null
  originalHeight?: number | null
  originalSizeBytes?: number | null
  thumbSizeBytes?: number | null
  previewSizeBytes?: number | null
  url?: string | null
  thumbUrl?: string | null
  previewUrl?: string | null
  originalUrl?: string | null
}

export type StylingGarment = {
  id: number
  code: string
  status: string
  aiTags: Record<string, unknown>
  manualTags: Record<string, unknown>
  officialTitleTags?: Record<string, unknown>
  effectiveTags?: Record<string, unknown>
  tagMeta?: Record<string, unknown>
  tagAttempts?: {
    id: number
    provider: string
    status: string
    startedAt?: string | null
    completedAt?: string | null
    failedAt?: string | null
    errorCode?: string | null
    errorMessage?: string | null
    requestSnapshot?: Record<string, unknown>
    responseSnapshot?: Record<string, unknown>
  }[]
  tagSchemaVersion?: string | null
  tagCompletedAt?: string | null
  tagError?: string | null
  archivedAt?: string | null
  archiveReason?: string | null
  imageCount?: number | null
  coverUrl?: string | null
  coverAsset?: MediaAsset | null
  images?: { id: number; sortOrder: number; asset: MediaAsset }[]
  createdAt: string
  updatedAt: string
}

export type AttemptPhase = {
  phase: string
  startedAt?: string | null
  endedAt?: string | null
  detail?: Record<string, unknown>
}

export type AttemptProgress = {
  current?: string | null
  phases?: AttemptPhase[]
}

export type AttemptError = {
  code: string
  title: string
  message: string
  action: string
  retryable: boolean
}

export type AIOperation = {
  id: number
  taskId?: number | null
  ownerType: string
  ownerId: number
  provider: string
  providerRequestId?: string | null
  status: string
  startedAt?: string | null
  completedAt?: string | null
  failedAt?: string | null
  errorCode?: string | null
  errorMessage?: string | null
  error?: AttemptError | null
  progress?: AttemptProgress | null
  outputAssetId?: number | null
  retryOfAttemptId?: number | null
  retryMode?: string | null
  retryIndex?: number | null
  shotSortOrder?: number | null
  createdAt?: string | null
  updatedAt?: string | null
}

export type AICallRecord = AIOperation & {
  technicalMessage?: string | null
  requestFacts?: Record<string, unknown>
  responseFacts?: Record<string, unknown>
  modelInput?: Record<string, unknown>
  modelOutput?: Record<string, unknown>
}

export type GenerationTask = {
  id: number
  name: string
  status: string
  userRequirement?: string | null
  stageUserInputs?: {
    aiStyling?: string | null
    storyboardPreview?: string | null
    finalImage?: string | null
  }
  currentInputVersionId?: number | null
  activeStylingId?: number | null
  activeStoryboardPreviewId?: number | null
  activePreviewId?: number | null
  activeResultSetId?: number | null
  batchId?: string | null
  autoRunTarget?: string | null
  autoRunRequestedAt?: string | null
  autoRunQueued?: boolean
  autoRunQueuePosition?: number | null
  archivedAt?: string | null
  archiveReason?: string | null
  createdAt: string
  updatedAt: string
  images?: { id: number; sortOrder: number; asset: MediaAsset }[]
  inputVersion?: {
    id: number
    versionNumber: number
    imageSnapshot: { sortOrder: number; asset: MediaAsset }[]
    modelSnapshot?: Record<string, unknown>
    userRequirement?: Record<string, unknown>
  } | null
  styling?: {
    id: number
    selectedMaterialIds?: number[]
    selectedMaterials: StylingGarment[]
    userInstruction?: Record<string, unknown>
    userRequirement?: Record<string, unknown>
    userRequirementResolution?: Record<string, unknown>
    stylingPlan?: Record<string, unknown>
    heroUnderstanding?: Record<string, unknown>
    outfitCandidates?: Record<string, unknown>[]
    criticScores?: Record<string, unknown>
    rejectedCandidates?: Record<string, unknown>[]
    qualitySignals?: Record<string, unknown>[]
    reasonSummary?: string
    warnings?: string[]
  } | null
  preview?: { id: number; status: string; asset?: MediaAsset | null; userInstruction?: Record<string, unknown>; createdAt?: string; updatedAt?: string } | null
  storyboardPreview?: { id: number; status: string; asset?: MediaAsset | null; userInstruction?: Record<string, unknown>; createdAt?: string; updatedAt?: string } | null
  resultSet?: {
    id: number
    status: string
    previewId?: number | null
    userInstruction?: Record<string, unknown>
    images: {
      id: number
      sortOrder: number
      shotRole: string
      status: string
      userInstruction?: Record<string, unknown>
      asset?: MediaAsset | null
      activeAttemptId?: number | null
      activeAttempt?: AIOperation | null
      createdAt?: string
      updatedAt?: string
    }[]
  } | null
  summary?: {
    cover?: MediaAsset | null
    preview?: MediaAsset | null
    finalImages?: MediaAsset[]
    selectedGarments?: StylingGarment[]
  }
  aiOperations?: AIOperation[]
  attempts?: unknown[]
}

type DirectUploadItem = {
  objectKey: string
  uploadUrl: string
  method: 'PUT'
  headers?: Record<string, string>
  filename: string
  mimeType: string
}

type UploadedMedia = {
  objectKey: string
  filename: string
  mimeType: string
  sizeBytes: number
}

export type DeleteImpact = {
  garmentCount?: number
  garmentImageCount?: number
  dependentTaskIds?: number[]
  dependentTaskCount?: number
  taskCount?: number
  taskImageCount?: number
  inputVersionCount?: number
  stylingCount?: number
  previewCount?: number
  resultSetCount?: number
  resultImageCount?: number
  mediaAssetCount?: number
  estimatedBytes?: number
  dependentTaskImpact?: DeleteImpact
}

export type FinalImageExportIssue = {
  sortOrder: number
  shotLabel: string
  status: string
}

export type FinalImageExportTask = {
  taskId: number
  taskName: string
  taskStatus: string
  hasResultSet: boolean
  downloadableCount: number
  totalCount: number
  totalBytes: number
  issues: FinalImageExportIssue[]
}

export type FinalImageExportPrecheck = {
  items: FinalImageExportTask[]
  downloadableTaskIds: number[]
  completeTaskIds: number[]
  totalImages: number
  totalBytes: number
}

export type ModelProfile = {
  id: number
  modelId: string
  name: string
  profile: {
    height?: string
    weight?: string
    bodyShape?: string
    bust?: string
    waist?: string
    hips?: string
    notes?: string
    headReferenceEntries?: {
      id: string
      angle: string | null
      filename: string
      thumbUrl?: string | null
      previewUrl?: string | null
      originalUrl?: string | null
    }[]
  }
  isDefault: boolean
  thumbUrl?: string | null
  previewUrl?: string | null
}

export type PromptPlaceholder = {
  name: string
  scope: string
  description: string
}

export type PromptConfigDiagnostics = {
  usedSegments: string[]
  missingPlaceholders: string[]
  referencedPlaceholders: string[]
}

export type PromptConfig = {
  key: string
  groupType: 'segment' | 'assembly'
  title: string
  description: string
  content: string
  contentHash: string
  sortOrder: number
  updatedAt?: string | null
  usedSegments: string[]
  missingPlaceholders: string[]
  referencedPlaceholders: string[]
  renderedPreview: string
}

export type PromptConfigList = {
  items: PromptConfig[]
  placeholders: PromptPlaceholder[]
}

export type PromptConfigPreview = {
  rendered: string
  historicalPrompt?: string | null
  diagnostics: PromptConfigDiagnostics
}

export type PromptInvocationOption = { id: number; ownerType: string; provider: string; status: string; createdAt?: string | null }

function headers(init?: RequestInit) {
  const h = new Headers(init?.headers)
  if (!(init?.body instanceof FormData) && !h.has('Content-Type')) h.set('Content-Type', 'application/json')
  return h
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: 'include',
    headers: headers(init),
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }))
    const message = typeof detail.detail === 'string' ? detail.detail : '请求失败'
    if (typeof window !== 'undefined' && response.status === 401 && !path.startsWith('/api/auth/login') && !path.startsWith('/api/auth/session')) {
      window.dispatchEvent(new CustomEvent('ruyi-auth-expired', { detail: { message } }))
    }
    throw new ApiError(message, response.status)
  }
  return response.json() as Promise<T>
}

async function uploadFilesDirect(scope: string, files: File[]): Promise<UploadedMedia[]> {
  const presigned = await request<{ items: DirectUploadItem[] }>('/api/uploads/presign', {
    method: 'POST',
    body: JSON.stringify({
      scope,
      files: files.map((file) => ({
        filename: file.name,
        mimeType: file.type,
        sizeBytes: file.size,
      })),
    }),
  })
  if (presigned.items.length !== files.length) throw new ApiError('上传地址数量不匹配', 500)
  await Promise.all(presigned.items.map(async (item, index) => {
    const file = files[index]!
    let response: Response
    try {
      response = await fetch(item.uploadUrl, {
        method: item.method,
        headers: item.headers ?? { 'Content-Type': item.mimeType || file.type },
        body: file,
      })
    } catch {
      throw new ApiError('浏览器直传对象存储失败，请检查对象存储 CORS 是否允许当前前端域名。', 0)
    }
    if (!response.ok) throw new ApiError(`${file.name} 上传失败`, response.status)
  }))
  return presigned.items.map((item, index) => ({
    objectKey: item.objectKey,
    filename: files[index]!.name,
    mimeType: item.mimeType || files[index]!.type,
    sizeBytes: files[index]!.size,
  }))
}

function params(path: string, values: Record<string, string | number | boolean | string[] | undefined | null>) {
  const search = new URLSearchParams()
  Object.entries(values).forEach(([key, value]) => {
    if (value === undefined || value === null || value === false || value === '') return
    if (Array.isArray(value)) {
      const filtered = value.map((item) => String(item).trim()).filter(Boolean)
      if (filtered.length) search.set(key, filtered.join(','))
      return
    }
    search.set(key, String(value))
  })
  const query = search.toString()
  return query ? `${path}?${query}` : path
}

export function mediaUrl(url?: string | null) {
  if (!url) return ''
  return /^(https?:|blob:|data:)/i.test(url) ? url : `${API_BASE}${url}`
}

export function mediaDownloadUrl(url?: string | null) {
  const resolved = mediaUrl(url)
  if (!resolved) return ''
  const downloadUrl = new URL(resolved, window.location.origin)
  downloadUrl.searchParams.set('variant', 'original')
  downloadUrl.searchParams.set('download', '1')
  return downloadUrl.toString()
}

export function finalImageArchiveUrl(ids: number[]) {
  return `${API_BASE}/api/generation-task-exports/archive?ids=${ids.join(',')}`
}

export const api = {
  session: () => request<AuthSession>('/api/auth/session'),
  login: (username: string, password: string) =>
    request<AuthSession>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  listStylingGarments: (options?: {
    limit?: number
    offset?: number
    keyword?: string
    includeArchived?: boolean
    status?: string[]
    category?: string[]
    primaryCategory?: string[]
    secondaryCategory?: string[]
    color?: string[]
    season?: string[]
    series?: string[]
    channel?: string[]
    reviewStatus?: string[]
    uploadedFrom?: string
    uploadedTo?: string
    sortBy?: string
    sortDir?: string
  }) =>
    request<StylingGarmentListResponse>(params('/api/styling-garments', options ?? {})),
  createStylingGarment: (code: string, files: File[]) => {
    const form = new FormData()
    form.append('code', code)
    files.forEach((file) => form.append('files', file))
    return request<{ item: StylingGarment }>('/api/styling-garments', { method: 'POST', body: form })
  },
  tagStylingGarment: (id: number) =>
    request<{ item: StylingGarment }>(`/api/styling-garments/${id}/save-and-tag`, { method: 'POST' }),
  tagStylingGarments: (ids: number[]) =>
    request<{ items: StylingGarment[] }>('/api/styling-garments/batch-save-and-tag', { method: 'POST', body: JSON.stringify({ ids }) }),
  updateStylingGarment: (id: number, payload: { code?: string; manualTags?: Record<string, unknown> }) =>
    request<{ item: StylingGarment }>(`/api/styling-garments/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  updateStylingGarmentManualTags: (ids: number[], manualTags: Record<string, unknown>) =>
    request<{ items: StylingGarment[] }>('/api/styling-garments/batch-manual-tags', { method: 'POST', body: JSON.stringify({ ids, manualTags }) }),
  getStylingGarment: (id: number) =>
    request<{ item: StylingGarment }>(`/api/styling-garments/${id}`),
  replaceStylingGarmentImage: (id: number, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<{ item: StylingGarment }>(`/api/styling-garment-images/${id}/replace`, { method: 'POST', body: form })
  },
  updateStylingGarmentImage: (id: number, payload: { sortOrder?: number }) =>
    request<{ item: StylingGarment }>(`/api/styling-garment-images/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteStylingGarmentImage: (id: number) =>
    request<{ item: StylingGarment }>(`/api/styling-garment-images/${id}`, { method: 'DELETE' }),
  deleteStylingGarment: (id: number, options?: { purge?: boolean; confirmCode?: string }) =>
    request<{ ok: boolean; archived?: boolean; purged?: boolean }>(
      params(`/api/styling-garments/${id}`, { purge: options?.purge, confirmCode: options?.confirmCode }),
      { method: 'DELETE' },
    ),
  deleteStylingGarments: (ids: number[], options?: { purge?: boolean; confirmPhrase?: string }) =>
    request<{ ok: boolean; archived?: boolean; purged?: boolean; ids?: number[]; deletedGarmentIds?: number[]; deletedTaskIds?: number[] }>(
      '/api/styling-garments/batch-delete',
      { method: 'POST', body: JSON.stringify({ ids, purge: options?.purge, confirmPhrase: options?.confirmPhrase }) },
    ),
  restoreStylingGarment: (id: number) =>
    request<{ ok: boolean; restored: boolean; item: StylingGarment }>(`/api/styling-garments/${id}/restore`, { method: 'POST' }),
  restoreStylingGarments: (ids: number[]) =>
    request<{ ok: boolean; restored: boolean; ids: number[] }>('/api/styling-garments/batch-restore', { method: 'POST', body: JSON.stringify({ ids }) }),
  getStylingGarmentDeleteImpact: (ids: number[]) =>
    request<{ impact: DeleteImpact }>('/api/styling-garments/delete-impact', { method: 'POST', body: JSON.stringify({ ids }) }),
  listTasks: (options?: { limit?: number; offset?: number; includeArchived?: boolean }) =>
    request<Paginated<GenerationTask>>(params('/api/generation-tasks', options ?? {})),
  getTask: (id: number, includeDebug = false) =>
    request<{ item: GenerationTask }>(params(`/api/generation-tasks/${id}`, { includeDebug })),
  listTaskAttempts: (id: number, ownerType: string, options?: { limit?: number; beforeCreatedAt?: string; beforeId?: number }) =>
    request<{ items: AIOperation[]; total: number; limit: number; hasMore: boolean; nextCursor: { beforeCreatedAt: string; beforeId: number } | null }>(
      params(`/api/generation-tasks/${id}/attempts`, { ownerType, ...(options ?? {}) }),
    ),
  getTaskAiInvocation: (id: number, attemptId: number) =>
    request<{ item: AICallRecord }>(`/api/generation-tasks/${id}/ai-invocations/${attemptId}`),
  archiveTask: (id: number) =>
    request<{ ok: boolean; archived: boolean; item: GenerationTask }>(`/api/generation-tasks/${id}/archive`, { method: 'POST' }),
  archiveTasks: (ids: number[]) =>
    request<{ ok: boolean; archived: boolean; ids: number[] }>('/api/generation-tasks/batch-archive', { method: 'POST', body: JSON.stringify({ ids }) }),
  restoreTask: (id: number) =>
    request<{ ok: boolean; restored: boolean; item: GenerationTask }>(`/api/generation-tasks/${id}/restore`, { method: 'POST' }),
  restoreTasks: (ids: number[]) =>
    request<{ ok: boolean; restored: boolean; ids: number[] }>('/api/generation-tasks/batch-restore', { method: 'POST', body: JSON.stringify({ ids }) }),
  cancelTask: (id: number) =>
    request<{ ok: boolean; cancelled: boolean; item: GenerationTask }>(`/api/generation-tasks/${id}/cancel`, { method: 'POST' }),
  deleteTask: (id: number, confirmName: string) =>
    request<{ ok: boolean; purged: boolean; deletedTaskIds: number[] }>(`/api/generation-tasks/${id}`, { method: 'DELETE', body: JSON.stringify({ purge: true, confirmName }) }),
  deleteTasks: (ids: number[], confirmPhrase: string) =>
    request<{ ok: boolean; purged: boolean; deletedTaskIds: number[] }>('/api/generation-tasks/batch-purge', { method: 'POST', body: JSON.stringify({ purge: true, ids, confirmPhrase }) }),
  getTaskDeleteImpact: (ids: number[]) =>
    request<{ impact: DeleteImpact }>('/api/generation-tasks/delete-impact', { method: 'POST', body: JSON.stringify({ ids }) }),
  createTask: async (name: string, files: File[], batchId?: string) => {
    const uploads = await uploadFilesDirect('generation_task', files)
    return request<{ item: GenerationTask }>('/api/generation-tasks/from-uploads', {
      method: 'POST',
      body: JSON.stringify({ name, batchId, uploads }),
    })
  },
  listBatchTasks: (batchId: string) =>
    request<{ items: GenerationTask[] }>(`/api/generation-batches/${encodeURIComponent(batchId)}/tasks`),
  autoRunBatch: (batchId: string, restart = false) =>
    request<{ startedTaskIds: number[]; items: GenerationTask[] }>(`/api/generation-batches/${encodeURIComponent(batchId)}/auto-run-final`, { method: 'POST', body: JSON.stringify({ restart }) }),
  cancelBatch: (batchId: string) =>
    request<{ cancelledTaskIds: number[]; items: GenerationTask[] }>(`/api/generation-batches/${encodeURIComponent(batchId)}/cancel`, { method: 'POST' }),
  updateTask: (id: number, payload: {
    name?: string
    aiStylingUserInstruction?: string
    storyboardPreviewUserInstruction?: string
    finalImageUserInstruction?: string
  }) =>
    request<{ item: GenerationTask }>(`/api/generation-tasks/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  replaceTaskImage: (id: number, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<{ item: GenerationTask }>(`/api/generation-task-images/${id}/replace`, { method: 'POST', body: form })
  },
  updateTaskImage: (id: number, sortOrder: number) =>
    request<{ item: GenerationTask }>(`/api/generation-task-images/${id}`, { method: 'PATCH', body: JSON.stringify({ sortOrder }) }),
  deleteTaskImage: (id: number) =>
    request<{ item: GenerationTask }>(`/api/generation-task-images/${id}`, { method: 'DELETE' }),
  selectStyling: (id: number, userInstruction?: string) =>
    request<{ item: GenerationTask }>(`/api/generation-tasks/${id}/select-styling`, { method: 'POST', body: JSON.stringify({ userInstruction }) }),
  previewTask: (id: number, userInstruction?: string) =>
    request<{ item: GenerationTask }>(`/api/generation-tasks/${id}/preview`, { method: 'POST', body: JSON.stringify({ userInstruction }) }),
  generateFinal: (id: number, userInstruction?: string) =>
    request<{ item: GenerationTask }>(`/api/generation-tasks/${id}/generate-final`, { method: 'POST', body: JSON.stringify({ userInstruction }) }),
  autoRunFinal: (id: number, restart = false) =>
    request<{ item: GenerationTask }>(`/api/generation-tasks/${id}/auto-run-final`, { method: 'POST', body: JSON.stringify({ restart }) }),
  cancelAutoRun: (id: number) =>
    request<{ item: GenerationTask }>(`/api/generation-tasks/${id}/cancel-auto-run`, { method: 'POST' }),
  retryResultImage: (id: number, userInstruction?: string) =>
    request<{ item: GenerationTask }>(`/api/generation-result-images-new/${id}/retry`, { method: 'POST', body: JSON.stringify({ userInstruction }) }),
  redeliverResultImage: (id: number) =>
    request<{ item: GenerationTask }>(`/api/generation-result-images-new/${id}/redeliver`, { method: 'POST' }),
  precheckFinalImageExport: (ids: number[]) =>
    request<FinalImageExportPrecheck>('/api/generation-task-exports/precheck', { method: 'POST', body: JSON.stringify({ ids }) }),
  listRecords: (options?: { limit?: number; offset?: number; includeArchived?: boolean }) =>
    request<Paginated<GenerationTask>>(params('/api/generation-records', options ?? {})),
  getRecord: (id: number, includeDebug = false) =>
    request<{ item: GenerationTask }>(params(`/api/generation-records/${id}`, { includeDebug })),
  listModels: () => request<Paginated<ModelProfile>>('/api/model-profiles?limit=30'),
  listPromptConfigs: () => request<PromptConfigList>('/api/prompt-configs'),
  updatePromptConfig: (key: string, content: string) =>
    request<{ item: PromptConfig; preview: PromptConfigPreview }>(`/api/prompt-configs/${encodeURIComponent(key)}`, { method: 'PATCH', body: JSON.stringify({ content }) }),
  previewPromptConfig: (key: string, content?: string, attemptId?: number) =>
    content === undefined
      ? request<PromptConfigPreview>(`/api/prompt-configs/${encodeURIComponent(key)}/preview`)
      : request<PromptConfigPreview>(`/api/prompt-configs/${encodeURIComponent(key)}/preview`, { method: 'POST', body: JSON.stringify({ content, attemptId }) }),
  listPromptInvocations: (key: string) =>
    request<{ items: PromptInvocationOption[] }>(`/api/prompt-configs/${encodeURIComponent(key)}/invocations`),
  addModelHeadReferences: (modelId: number, files: File[]) => {
    const form = new FormData()
    files.forEach((file) => form.append('files', file))
    return request<{ item: ModelProfile }>(`/api/model-profiles/${modelId}/head-references`, { method: 'POST', body: form })
  },
  replaceModelHeadReference: (modelId: number, referenceId: string, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<{ item: ModelProfile }>(`/api/model-profiles/${modelId}/head-references/${encodeURIComponent(referenceId)}/replace`, { method: 'POST', body: form })
  },
  deleteModelHeadReference: (modelId: number, referenceId: string) =>
    request<{ item: ModelProfile }>(`/api/model-profiles/${modelId}/head-references/${encodeURIComponent(referenceId)}`, { method: 'DELETE' }),
  reorderModelHeadReference: (modelId: number, referenceId: string, orderIndex: number) =>
    request<{ item: ModelProfile }>(`/api/model-profiles/${modelId}/head-references/${encodeURIComponent(referenceId)}`, { method: 'PATCH', body: JSON.stringify({ orderIndex }) }),
  setModelHeadReferenceAngle: (modelId: number, referenceId: string, angle: string) =>
    request<{ item: ModelProfile }>(`/api/model-profiles/${modelId}/head-references/${encodeURIComponent(referenceId)}`, { method: 'PATCH', body: JSON.stringify({ angle }) }),
  getShootEnvironment: () => request<{ asset: MediaAsset | null }>('/api/shoot-environment'),
  setShootEnvironment: (file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<{ asset: MediaAsset | null }>('/api/shoot-environment', { method: 'POST', body: form })
  },
  clearShootEnvironment: () => request<{ asset: MediaAsset | null }>('/api/shoot-environment', { method: 'DELETE' }),
}
