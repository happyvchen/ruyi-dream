import { sha256File } from './lib/fileChecksum'

export const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000'

export type SessionRole = 'user' | 'admin'

export type AuthSession = {
  authRequired: boolean
  authenticated: boolean
  role?: SessionRole | null
}

export class ApiError extends Error {
  status: number
  errorCode?: string | null
  errorTitle?: string | null
  errorMessage?: string | null
  action?: string | null
  retryable?: boolean | null

  constructor(message: string, status: number, options?: { errorCode?: string | null; errorTitle?: string | null; errorMessage?: string | null; action?: string | null; retryable?: boolean | null }) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.errorCode = options?.errorCode
    this.errorTitle = options?.errorTitle
    this.errorMessage = options?.errorMessage
    this.action = options?.action
    this.retryable = options?.retryable
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

export type GenerationRecordFacets = {
  status?: FacetOption[]
}

export type GenerationRecordListResponse = Paginated<GenerationTask> & {
  facets?: GenerationRecordFacets
  stableFacets?: GenerationRecordFacets
}

export type GenerationRecordBatch = {
  id: string
  batchId: string
  name: string
  status: string
  taskCount: number
  activeTaskCount: number
  archivedTaskCount: number
  completedTaskCount: number
  failedTaskCount: number
  cancelledTaskCount: number
  runningTaskCount: number
  queuedTaskCount: number
  downloadableTaskCount: number
  statusCounts: Record<string, number>
  createdAt: string
  updatedAt: string
  coverTasks: GenerationTask[]
  sampleTaskNames: string[]
  firstTaskId: number
}

export type GenerationWorkspaceRecord =
  | { kind: 'batch'; id: string; status: string; name: string; createdAt: string; updatedAt: string; batch: GenerationRecordBatch }
  | { kind: 'task'; id: string; status: string; name: string; createdAt: string; updatedAt: string; task: GenerationTask }

export type GenerationRecordBatchListResponse = Paginated<GenerationRecordBatch> & {
  facets?: GenerationRecordFacets
  stableFacets?: GenerationRecordFacets
}

export type GenerationRecordWorkspaceResponse = Paginated<GenerationWorkspaceRecord> & {
  facets?: GenerationRecordFacets
  stableFacets?: GenerationRecordFacets
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
  taggingErrorCode?: string | null
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

export type DirectUploadItem = {
  objectKey: string
  uploadUrl: string
  method: 'PUT'
  headers?: Record<string, string>
  filename: string
  mimeType: string
  relativePath?: string
  lastModified?: number | null
  checksumSha256?: string | null
}

export type UploadProgressSnapshot = {
  phase: 'preparing' | 'uploading' | 'saving'
  completed: number
  total: number
  currentFile?: string
}

export type UploadSession = {
  id: string
  status: string
  scope?: string
  businessCode?: string | null
  sourceTitle?: string | null
  batchId?: string | null
  totalCount: number
  uploadedCount: number
  targetType?: string | null
  targetId?: number | null
  targetStatus?: string | null
  errorMessage?: string | null
  archivedAt?: string | null
  archiveReason?: string | null
  createdAt?: string
  updatedAt?: string
  items?: {
    id: number
    objectKey: string
    filename: string
    mimeType: string
    sizeBytes?: number | null
    relativePath?: string | null
    lastModified?: number | null
    checksumSha256?: string | null
    status: string
    mediaAssetId?: number | null
    errorMessage?: string | null
  }[]
}

const uploadExecutorStorageKey = 'ruyi-upload-executor-id'
let cachedUploadExecutorId = ''

function uploadExecutorId() {
  if (cachedUploadExecutorId) return cachedUploadExecutorId
  try {
    const existing = window.sessionStorage.getItem(uploadExecutorStorageKey)
    const navigation = window.performance?.getEntriesByType?.('navigation')?.[0] as PerformanceNavigationTiming | undefined
    if (existing && navigation?.type === 'reload') {
      cachedUploadExecutorId = existing
      return existing
    }
    const created = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`
    window.sessionStorage.setItem(uploadExecutorStorageKey, created)
    cachedUploadExecutorId = created
    return created
  } catch {
    cachedUploadExecutorId = `ephemeral-${Date.now()}-${Math.random().toString(36).slice(2)}`
    return cachedUploadExecutorId
  }
}

function uploadRelativePath(file: File) {
  return ((file as File & { webkitRelativePath?: string; relativePath?: string }).webkitRelativePath
    || (file as File & { relativePath?: string }).relativePath
    || file.name).replace(/^\/+/, '')
}

function uploadFileDescriptor(file: File) {
  return {
    filename: file.name,
    mimeType: file.type,
    sizeBytes: file.size,
    relativePath: uploadRelativePath(file),
    lastModified: file.lastModified,
  }
}

async function uploadFileDescriptorWithChecksum(file: File, onCompatibilityMode?: () => void) {
  const checksumSha256 = await sha256File(file, { onCompatibilityMode })
  return { ...uploadFileDescriptor(file), checksumSha256 }
}

async function uploadFileDescriptorsWithChecksums(files: File[], onCompatibilityMode?: () => void) {
  const descriptors: ReturnType<typeof uploadFileDescriptorWithChecksum> extends Promise<infer T> ? T[] : never = []
  for (const file of files) descriptors.push(await uploadFileDescriptorWithChecksum(file, onCompatibilityMode))
  return descriptors
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
  uploadSessionCount?: number
  uploadItemCount?: number
  temporaryObjectCount?: number
  linkedBusinessCount?: number
}

export type ObjectCleanupError = {
  prefix?: string
  key?: string
  objectKey?: string
  error: string
}

export type DeleteOperationResult = {
  ok: boolean
  archived?: boolean
  purged?: boolean
  ids?: number[]
  deletedGarmentIds?: number[]
  deletedTaskIds?: number[]
  objectCleanupErrors?: ObjectCleanupError[]
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
    const payload = await response.json().catch(() => ({ detail: response.statusText }))
    const detail = payload?.detail
    const structured = detail && typeof detail === 'object' && !Array.isArray(detail) ? detail as Record<string, unknown> : null
    const title = typeof structured?.title === 'string' ? structured.title : null
    const messageText = typeof structured?.message === 'string' ? structured.message : null
    const detailText = typeof structured?.detail === 'string' ? structured.detail : null
    const message = detailText || (title && messageText ? `${title}：${messageText}` : title || (typeof detail === 'string' ? detail : '请求失败'))
    if (typeof window !== 'undefined' && response.status === 401 && !path.startsWith('/api/auth/login') && !path.startsWith('/api/auth/session')) {
      window.dispatchEvent(new CustomEvent('ruyi-auth-expired', { detail: { message } }))
    }
    throw new ApiError(message, response.status, {
      errorCode: typeof structured?.code === 'string' ? structured.code : null,
      errorTitle: title,
      errorMessage: messageText,
      action: typeof structured?.action === 'string' ? structured.action : null,
      retryable: typeof structured?.retryable === 'boolean' ? structured.retryable : null,
    })
  }
  return response.json() as Promise<T>
}

async function runWithConcurrency<T>(items: T[], limit: number, worker: (item: T, index: number) => Promise<void>, onFailure?: () => void) {
  let cursor = 0
  let firstError: unknown = null
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (cursor < items.length && !firstError) {
      const index = cursor
      cursor += 1
      try {
        await worker(items[index]!, index)
      } catch (error) {
        if (!firstError) {
          firstError = error
          onFailure?.()
        }
      }
    }
  })
  await Promise.all(workers)
  if (firstError) throw firstError
}

async function uploadFilesViaSession(
  scope: string,
  files: File[],
  onProgress?: (progress: UploadProgressSnapshot) => void,
  metadata?: { businessCode?: string; sourceTitle?: string },
): Promise<{ session: UploadSession; items: DirectUploadItem[] }> {
  onProgress?.({ phase: 'preparing', completed: 0, total: files.length })
  const created = await request<{ session: UploadSession; items: DirectUploadItem[] }>('/api/upload-sessions', {
    method: 'POST',
    body: JSON.stringify({
      scope,
      businessCode: metadata?.businessCode,
      sourceTitle: metadata?.sourceTitle,
      executorId: uploadExecutorId(),
      files: files.map((file) => ({
        ...uploadFileDescriptor(file),
      })),
    }),
  })
  if (created.items.length !== files.length) throw new ApiError('上传任务准备失败，请重新选择图片后再试。', 500)
  let completed = 0
  const controller = new AbortController()
  await runWithConcurrency(created.items, 4, async (item, index) => {
    const file = files[index]!
    let response: Response
    try {
      response = await fetch(item.uploadUrl, {
        method: item.method,
        headers: item.headers ?? { 'Content-Type': item.mimeType || file.type },
        body: file,
        signal: controller.signal,
      })
    } catch {
      throw new ApiError('图片上传失败，请检查网络后重试。', 0)
    }
    if (!response.ok) throw new ApiError(`${file.name} 上传失败，请重新上传该图片。`, response.status)
    completed += 1
    await request<{ session: UploadSession }>(`/api/upload-sessions/${encodeURIComponent(created.session.id)}/complete-items`, {
      method: 'POST',
      body: JSON.stringify({ objectKeys: [item.objectKey], executorId: uploadExecutorId() }),
    })
    onProgress?.({ phase: 'uploading', completed, total: files.length })
  }, () => controller.abort())
  onProgress?.({ phase: 'saving', completed, total: files.length })
  return created
}

async function uploadPreparedSession(
  session: UploadSession,
  items: DirectUploadItem[],
  files: File[],
  onProgress?: (progress: UploadProgressSnapshot) => void,
) {
  const filesByPath = new Map(files.map((file) => [uploadRelativePath(file), file]))
  let completed = session.uploadedCount
  const controller = new AbortController()
  const executorId = uploadExecutorId()
  const heartbeat = window.setInterval(() => {
    void request(`/api/upload-sessions/${encodeURIComponent(session.id)}/lease`, {
      method: 'POST',
      body: JSON.stringify({ executorId }),
    }).catch(() => undefined)
  }, 30_000)
  onProgress?.({ phase: 'preparing', completed, total: session.totalCount })
  try {
    await runWithConcurrency(items, 4, async (item) => {
      const file = filesByPath.get(item.relativePath || item.filename)
      if (!file) throw new ApiError(`缺少原任务图片：${item.relativePath || item.filename}`, 409)
      let response: Response
      try {
        response = await fetch(item.uploadUrl, {
          method: item.method,
          headers: item.headers ?? { 'Content-Type': item.mimeType || file.type },
          body: file,
          signal: controller.signal,
        })
      } catch {
        throw new ApiError('图片上传失败，请检查网络后重试。', 0)
      }
      if (!response.ok) throw new ApiError(`${file.name} 上传失败，请稍后继续此任务。`, response.status)
      await request<{ session: UploadSession }>(`/api/upload-sessions/${encodeURIComponent(session.id)}/complete-items`, {
        method: 'POST',
        body: JSON.stringify({ objectKeys: [item.objectKey], executorId }),
      })
      completed += 1
      onProgress?.({ phase: 'uploading', completed, total: session.totalCount, currentFile: file.name })
    }, () => controller.abort())
    onProgress?.({ phase: 'saving', completed, total: session.totalCount })
  } finally {
    window.clearInterval(heartbeat)
  }
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

export function stylingGarmentArchiveUrl(ids: number[]) {
  return `${API_BASE}/api/styling-garment-exports/archive?ids=${ids.join(',')}`
}

export const api = {
  session: () => request<AuthSession>('/api/auth/session'),
  login: (username: string, password: string) =>
    request<AuthSession>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  listUploadSessions: (options?: { scope?: string; status?: string; batchId?: string; limit?: number; includeAbandoned?: boolean; includeArchived?: boolean }) =>
    request<{ items: UploadSession[] }>(params('/api/upload-sessions', options ?? {})),
  getUploadSession: (id: string) =>
    request<{ session: UploadSession }>(`/api/upload-sessions/${encodeURIComponent(id)}`),
  abandonUploadSession: (id: string) =>
    request<{ session: UploadSession }>(`/api/upload-sessions/${encodeURIComponent(id)}/abandon`, {
      method: 'POST',
      body: JSON.stringify({ executorId: uploadExecutorId() }),
    }),
  getUploadSessionDeleteImpact: (ids: string[]) =>
    request<{ impact: DeleteImpact }>('/api/upload-sessions/delete-impact', {
      method: 'POST',
      body: JSON.stringify({ ids }),
    }),
  archiveUploadSessions: (ids: string[]) =>
    request<{ archivedIds: string[] }>('/api/upload-sessions/archive', {
      method: 'POST',
      body: JSON.stringify({ ids }),
    }),
  restoreUploadSessions: (ids: string[]) =>
    request<{ restoredIds: string[] }>('/api/upload-sessions/restore', {
      method: 'POST',
      body: JSON.stringify({ ids }),
    }),
  deleteUploadSessions: (ids: string[], confirmation: { confirmName?: string; confirmPhrase?: string }) =>
    request<{ deletedIds: string[]; deferredIds: string[]; objectCleanupErrors: ObjectCleanupError[] }>('/api/upload-sessions', {
      method: 'DELETE',
      body: JSON.stringify({ ids, ...confirmation }),
    }),
  createStylingImportSessions: async (sourceTitle: string, batches: { code: string; files: File[] }[], batchId?: string) => {
    const items = batches.map((batch) => ({ businessCode: batch.code, files: batch.files.map(uploadFileDescriptor) }))
    return request<{ batchId: string; items: { session: UploadSession; items: DirectUploadItem[] }[] }>('/api/upload-sessions/styling-import-batch', {
      method: 'POST',
      body: JSON.stringify({
        sourceTitle,
        batchId,
        items,
      }),
    })
  },
  resumeUploadSession: async (id: string, files: File[], options?: { onChecksumCompatibilityMode?: () => void }) =>
    request<{ session: UploadSession; items: DirectUploadItem[] }>(`/api/upload-sessions/${encodeURIComponent(id)}/resume`, {
      method: 'POST',
      body: JSON.stringify({ files: await uploadFileDescriptorsWithChecksums(files, options?.onChecksumCompatibilityMode), executorId: uploadExecutorId() }),
    }),
  uploadPreparedSession,
  finalizeStylingGarmentSession: (sessionId: string, code: string) =>
    request<{ item: StylingGarment; session: UploadSession }>(`/api/upload-sessions/${encodeURIComponent(sessionId)}/finalize-styling-garment`, {
      method: 'POST',
      body: JSON.stringify({ code, executorId: uploadExecutorId() }),
    }),
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
  createStylingGarment: async (
    code: string,
    files: File[],
    options?: { onProgress?: (progress: UploadProgressSnapshot) => void; sourceTitle?: string },
  ) => {
    const upload = await uploadFilesViaSession('styling_garment', files, options?.onProgress, { businessCode: code, sourceTitle: options?.sourceTitle || code })
    return request<{ item: StylingGarment; session: UploadSession }>(`/api/upload-sessions/${encodeURIComponent(upload.session.id)}/finalize-styling-garment`, {
      method: 'POST',
      body: JSON.stringify({ code }),
    })
  },
  tagStylingGarment: (id: number) =>
    request<{ item: StylingGarment }>(`/api/styling-garments/${id}/save-and-tag`, { method: 'POST' }),
  tagStylingGarments: (ids: number[]) =>
    request<{ items: StylingGarment[]; startedIds: number[]; skipped: { id: number; code?: string; status?: string; reason?: string }[] }>(
      '/api/styling-garments/batch-save-and-tag',
      { method: 'POST', body: JSON.stringify({ ids }) },
    ),
  updateStylingGarment: (id: number, payload: { code?: string; manualTags?: Record<string, unknown> }) =>
    request<{ item: StylingGarment }>(`/api/styling-garments/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
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
    request<DeleteOperationResult>(
      params(`/api/styling-garments/${id}`, { purge: options?.purge, confirmCode: options?.confirmCode }),
      { method: 'DELETE' },
    ),
  deleteStylingGarments: (ids: number[], options?: { purge?: boolean; confirmPhrase?: string }) =>
    request<DeleteOperationResult>(
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
    request<DeleteOperationResult>(`/api/generation-tasks/${id}`, { method: 'DELETE', body: JSON.stringify({ purge: true, confirmName }) }),
  deleteTasks: (ids: number[], confirmPhrase: string) =>
    request<DeleteOperationResult>('/api/generation-tasks/batch-purge', { method: 'POST', body: JSON.stringify({ purge: true, ids, confirmPhrase }) }),
  getTaskDeleteImpact: (ids: number[]) =>
    request<{ impact: DeleteImpact }>('/api/generation-tasks/delete-impact', { method: 'POST', body: JSON.stringify({ ids }) }),
  createTask: async (
    name: string,
    files: File[],
    batchId?: string,
    userRequirement?: string,
    batchName?: string,
    options?: { onProgress?: (progress: UploadProgressSnapshot) => void },
  ) => {
    const upload = await uploadFilesViaSession('generation_task', files, options?.onProgress, { sourceTitle: name })
    return request<{ item: GenerationTask; session: UploadSession }>(`/api/upload-sessions/${encodeURIComponent(upload.session.id)}/finalize-generation-task`, {
      method: 'POST',
      body: JSON.stringify({ name, batchId, batchName, userRequirement }),
    })
  },
  listBatchTasks: (batchId: string) =>
    request<{ items: GenerationTask[] }>(`/api/generation-batches/${encodeURIComponent(batchId)}/tasks`),
  updateBatch: (batchId: string, name: string) =>
    request<{ item: GenerationRecordBatch }>(`/api/generation-batches/${encodeURIComponent(batchId)}`, { method: 'PATCH', body: JSON.stringify({ name }) }),
  autoRunBatch: (batchId: string, restart = false) =>
    request<{ startedTaskIds: number[]; items: GenerationTask[] }>(`/api/generation-batches/${encodeURIComponent(batchId)}/auto-run-final`, { method: 'POST', body: JSON.stringify({ restart }) }),
  cancelBatch: (batchId: string) =>
    request<{ cancelledTaskIds: number[]; items: GenerationTask[] }>(`/api/generation-batches/${encodeURIComponent(batchId)}/cancel`, { method: 'POST' }),
  updateTask: (id: number, payload: {
    name?: string
    userRequirement?: string
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
  listRecords: (options?: {
    limit?: number
    offset?: number
    keyword?: string
    includeArchived?: boolean
    status?: string[]
    createdFrom?: string
    createdTo?: string
    sortBy?: string
    sortDir?: string
  }) =>
    request<GenerationRecordListResponse>(params('/api/generation-records', options ?? {})),
  listRecordBatches: (options?: {
    limit?: number
    offset?: number
    keyword?: string
    includeArchived?: boolean
    status?: string[]
    createdFrom?: string
    createdTo?: string
    sortBy?: string
    sortDir?: string
  }) =>
    request<GenerationRecordBatchListResponse>(params('/api/generation-record-batches', options ?? {})),
  listRecordWorkspace: (options?: {
    limit?: number
    offset?: number
    keyword?: string
    includeArchived?: boolean
    status?: string[]
    createdFrom?: string
    createdTo?: string
    sortBy?: string
    sortDir?: string
  }) =>
    request<GenerationRecordWorkspaceResponse>(params('/api/generation-record-workspace', options ?? {})),
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
