export const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000'

export type SessionRole = 'user' | 'admin'

export type AuthSession = {
  authRequired: boolean
  authenticated: boolean
  role?: SessionRole | null
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

export type AIOperation = {
  id: number
  ownerType: string
  ownerId: number
  provider: string
  providerRequestId?: string | null
  status: string
  startedAt?: string | null
  completedAt?: string | null
  failedAt?: string | null
  errorMessage?: string | null
  outputAssetId?: number | null
  createdAt?: string | null
  updatedAt?: string | null
}

export type AICallRecord = AIOperation & {
  requestSnapshot?: Record<string, unknown>
  responseSnapshot?: Record<string, unknown>
}

export type GenerationTask = {
  id: number
  name: string
  status: string
  currentInputVersionId?: number | null
  activeStylingId?: number | null
  activeShootScriptId?: number | null
  activeStoryboardPreviewId?: number | null
  activePreviewId?: number | null
  activeResultSetId?: number | null
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
  } | null
  styling?: {
    id: number
    selectedMaterialIds?: number[]
    selectedMaterials: StylingGarment[]
    stylingPlan?: Record<string, unknown>
    heroUnderstanding?: Record<string, unknown>
    libraryAdequacy?: Record<string, unknown>
    outfitCandidates?: Record<string, unknown>[]
    criticScores?: Record<string, unknown>
    rejectedCandidates?: Record<string, unknown>[]
    qualitySignals?: Record<string, unknown>[]
    reasonSummary?: string
    warnings?: string[]
  } | null
  shootScript?: {
    id: number
    status: string
    script?: Record<string, unknown>
    summary?: Record<string, unknown>
    stylingRisks?: Record<string, unknown>[]
    accessoryAdjustments?: Record<string, unknown>[]
    resolvedAccessories?: Record<string, unknown>[]
    activeAttemptId?: number | null
    activeAttempt?: AIOperation | null
    createdAt?: string
    updatedAt?: string
  } | null
  shotPlans?: { id: number; sortOrder: number; shotType: string; plan: Record<string, unknown> }[]
  preview?: { id: number; status: string; asset?: MediaAsset | null; shootScriptId?: number | null; createdAt?: string; updatedAt?: string } | null
  storyboardPreview?: { id: number; status: string; asset?: MediaAsset | null; shootScriptId?: number | null; createdAt?: string; updatedAt?: string } | null
  resultSet?: {
    id: number
    status: string
    shootScriptId?: number | null
    previewId?: number | null
    images: {
      id: number
      sortOrder: number
      status: string
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

export type DeleteImpact = {
  garmentCount?: number
  garmentImageCount?: number
  dependentTaskIds?: number[]
  dependentTaskCount?: number
  taskCount?: number
  taskImageCount?: number
  inputVersionCount?: number
  stylingCount?: number
  shootScriptCount?: number
  shotPlanCount?: number
  previewCount?: number
  resultSetCount?: number
  resultImageCount?: number
  mediaAssetCount?: number
  estimatedBytes?: number
  dependentTaskImpact?: DeleteImpact
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

export type BrandInfo = {
  id: number
  name: string
  tagline: string
  brandCore: string
  styleDna: string
  stylingGuidelines: string
  shootingGuidelines: string
  negativeConstraints: string
  createdAt: string
  updatedAt: string
}

export type BrandInfoPayload = {
  name?: string
  tagline?: string
  brandCore?: string
  styleDna?: string
  stylingGuidelines?: string
  shootingGuidelines?: string
  negativeConstraints?: string
}

export type ShootConfigShotRule = {
  shotType: string
  shotRoleLabel: string
  framing: string
  pose: string
  cameraAngle: string
  mainFocus: string
}

export type ShootConfigGlobalRule = {
  id: string
  label: string
  value: string
  enabled: boolean
  sortOrder: number
}

export type ShootConfigCategoryOption = {
  id: number
  dimensionCode: string
  code: string
  labelZh: string
  isActive: boolean
  sortOrder: number
}

export type ShootConfigCategoryRule = {
  categoryDimensionCode: string
  categoryTagCode: string
  shots: ShootConfigShotRule[]
}

export type ShootConfig = {
  schemaVersion: string
  globalRules: ShootConfigGlobalRule[]
  defaultShotRule: { shots: ShootConfigShotRule[] }
  categoryShotRules: ShootConfigCategoryRule[]
  categoryOptions: ShootConfigCategoryOption[]
}

export type ShootConfigExportPayload = {
  schemaVersion: string
  config: ShootConfig
  categoryOptionsSnapshot: ShootConfigCategoryOption[]
}

export type TaxonomyTag = {
  id: number
  dimensionCode: string
  code: string
  labelZh: string
  descriptionZh?: string | null
  parentCode?: string | null
  level: number
  valueKind: string
  isSelectable: boolean
  isActive: boolean
  sortOrder: number
  metadata?: Record<string, unknown>
  usageCount: number
  children: TaxonomyTag[]
}

export type TaxonomyDimension = {
  id: number
  code: string
  labelZh: string
  descriptionZh?: string | null
  sortOrder: number
  isActive: boolean
  createdAt: string
  updatedAt: string
  tags: TaxonomyTag[]
}

export type TaxonomyRelation = {
  id: number
  fromDimensionCode: string
  fromTagCode: string
  relationType: string
  toDimensionCode: string
  toTagCode: string
  weight: number
  descriptionZh?: string | null
  isActive: boolean
  createdAt: string
  updatedAt: string
}

export type TaxonomyApplicabilityRule = {
  id: number
  categoryDimensionCode: string
  categoryTagCode: string
  targetDimensionCode: string
  targetTagCode: string
  mode: string
  appliesToDescendants: boolean
  note?: string | null
  isActive: boolean
  createdAt: string
  updatedAt: string
}

export type TaxonomyRules = {
  dimensions: TaxonomyDimension[]
  relations: TaxonomyRelation[]
  applicabilityRules: TaxonomyApplicabilityRule[]
  summary: {
    dimensionCount: number
    tagCount: number
    relationCount: number
    applicabilityRuleCount: number
    assignmentCount: number
    taggedGarmentCount: number
  }
}

export type TaxonomyDimensionPayload = {
  code?: string
  labelZh?: string
  descriptionZh?: string
  sortOrder?: number
  isActive?: boolean
}

export type TaxonomyTagPayload = {
  dimensionCode?: string
  code?: string
  labelZh?: string
  descriptionZh?: string
  parentCode?: string | null
  level?: number
  valueKind?: string
  isSelectable?: boolean
  isActive?: boolean
  sortOrder?: number
  metadata?: Record<string, unknown>
}

export type TaxonomyRelationPayload = {
  fromDimensionCode?: string
  fromTagCode?: string
  relationType?: string
  toDimensionCode?: string
  toTagCode?: string
  weight?: number
  descriptionZh?: string
  isActive?: boolean
}

export type TaxonomyApplicabilityPayload = {
  categoryDimensionCode?: string
  categoryTagCode?: string
  targetDimensionCode?: string
  targetTagCode?: string
  mode?: string
  appliesToDescendants?: boolean
  note?: string
  isActive?: boolean
}

export type TaxonomyExportPayload = {
  schemaVersion: string
  dimensions: TaxonomyDimension[]
  tags: TaxonomyTag[]
  relations: TaxonomyRelation[]
  applicabilityRules: TaxonomyApplicabilityRule[]
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
  diagnostics: PromptConfigDiagnostics
}

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
    throw new Error(typeof detail.detail === 'string' ? detail.detail : '请求失败')
  }
  return response.json() as Promise<T>
}

function params(path: string, values: Record<string, string | number | boolean | undefined | null>) {
  const search = new URLSearchParams()
  Object.entries(values).forEach(([key, value]) => {
    if (value === undefined || value === null || value === false || value === '') return
    search.set(key, String(value))
  })
  const query = search.toString()
  return query ? `${path}?${query}` : path
}

export function mediaUrl(url?: string | null) {
  if (!url) return ''
  return /^(https?:|blob:|data:)/i.test(url) ? url : `${API_BASE}${url}`
}

export const api = {
  session: () => request<AuthSession>('/api/auth/session'),
  login: (username: string, password: string) =>
    request<AuthSession>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  listStylingGarments: (options?: { limit?: number; offset?: number; keyword?: string; includeArchived?: boolean }) =>
    request<Paginated<StylingGarment>>(params('/api/styling-garments', options ?? {})),
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
  getStylingGarment: (id: number) =>
    request<{ item: StylingGarment }>(`/api/styling-garments/${id}`),
  addStylingGarmentImages: (id: number, files: File[]) => {
    const form = new FormData()
    files.forEach((file) => form.append('files', file))
    return request<{ item: StylingGarment }>(`/api/styling-garments/${id}/images`, { method: 'POST', body: form })
  },
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
  getStylingGarmentDeleteImpact: (ids: number[]) =>
    request<{ impact: DeleteImpact }>('/api/styling-garments/delete-impact', { method: 'POST', body: JSON.stringify({ ids }) }),
  listTasks: (options?: { limit?: number; offset?: number; includeArchived?: boolean }) =>
    request<Paginated<GenerationTask>>(params('/api/generation-tasks', options ?? {})),
  getTask: (id: number, includeDebug = false) =>
    request<{ item: GenerationTask }>(params(`/api/generation-tasks/${id}`, { includeDebug })),
  getTaskAiInvocations: (id: number) =>
    request<{ items: AICallRecord[] }>(`/api/generation-tasks/${id}/ai-invocations`),
  archiveTask: (id: number) =>
    request<{ ok: boolean; archived: boolean; item: GenerationTask }>(`/api/generation-tasks/${id}/archive`, { method: 'POST' }),
  archiveTasks: (ids: number[]) =>
    request<{ ok: boolean; archived: boolean; ids: number[] }>('/api/generation-tasks/batch-archive', { method: 'POST', body: JSON.stringify({ ids }) }),
  cancelTask: (id: number) =>
    request<{ ok: boolean; cancelled: boolean; item: GenerationTask }>(`/api/generation-tasks/${id}/cancel`, { method: 'POST' }),
  deleteTask: (id: number, confirmName: string) =>
    request<{ ok: boolean; purged: boolean; deletedTaskIds: number[] }>(`/api/generation-tasks/${id}`, { method: 'DELETE', body: JSON.stringify({ purge: true, confirmName }) }),
  deleteTasks: (ids: number[], confirmPhrase: string) =>
    request<{ ok: boolean; purged: boolean; deletedTaskIds: number[] }>('/api/generation-tasks/batch-purge', { method: 'POST', body: JSON.stringify({ purge: true, ids, confirmPhrase }) }),
  getTaskDeleteImpact: (ids: number[]) =>
    request<{ impact: DeleteImpact }>('/api/generation-tasks/delete-impact', { method: 'POST', body: JSON.stringify({ ids }) }),
  createTask: (name: string, files: File[]) => {
    const form = new FormData()
    if (name) form.append('name', name)
    files.forEach((file) => form.append('files', file))
    return request<{ item: GenerationTask }>('/api/generation-tasks', { method: 'POST', body: form })
  },
  updateTask: (id: number, payload: { name?: string }) =>
    request<{ item: GenerationTask }>(`/api/generation-tasks/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  addTaskImages: (id: number, files: File[]) => {
    const form = new FormData()
    files.forEach((file) => form.append('files', file))
    return request<{ item: GenerationTask }>(`/api/generation-tasks/${id}/images`, { method: 'POST', body: form })
  },
  replaceTaskImage: (id: number, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<{ item: GenerationTask }>(`/api/generation-task-images/${id}/replace`, { method: 'POST', body: form })
  },
  updateTaskImage: (id: number, sortOrder: number) =>
    request<{ item: GenerationTask }>(`/api/generation-task-images/${id}`, { method: 'PATCH', body: JSON.stringify({ sortOrder }) }),
  deleteTaskImage: (id: number) =>
    request<{ item: GenerationTask }>(`/api/generation-task-images/${id}`, { method: 'DELETE' }),
  readyTask: (id: number) =>
    request<{ item: GenerationTask }>(`/api/generation-tasks/${id}/ready`, { method: 'POST' }),
  selectStyling: (id: number) =>
    request<{ item: GenerationTask }>(`/api/generation-tasks/${id}/select-styling`, { method: 'POST' }),
  createShootScript: (id: number) =>
    request<{ item: GenerationTask }>(`/api/generation-tasks/${id}/shoot-script`, { method: 'POST' }),
  previewTask: (id: number) =>
    request<{ item: GenerationTask }>(`/api/generation-tasks/${id}/preview`, { method: 'POST' }),
  generateFinal: (id: number) =>
    request<{ item: GenerationTask }>(`/api/generation-tasks/${id}/generate-final`, { method: 'POST' }),
  retryResultImage: (id: number) =>
    request<{ item: GenerationTask }>(`/api/generation-result-images-new/${id}/retry`, { method: 'POST' }),
  listRecords: (options?: { limit?: number; offset?: number; includeArchived?: boolean }) =>
    request<Paginated<GenerationTask>>(params('/api/generation-records', options ?? {})),
  getRecord: (id: number, includeDebug = false) =>
    request<{ item: GenerationTask }>(params(`/api/generation-records/${id}`, { includeDebug })),
  listModels: () => request<Paginated<ModelProfile>>('/api/model-profiles?limit=30'),
  listPromptConfigs: () => request<PromptConfigList>('/api/prompt-configs'),
  updatePromptConfig: (key: string, content: string) =>
    request<{ item: PromptConfig; preview: PromptConfigPreview }>(`/api/prompt-configs/${encodeURIComponent(key)}`, { method: 'PATCH', body: JSON.stringify({ content }) }),
  previewPromptConfig: (key: string) =>
    request<PromptConfigPreview>(`/api/prompt-configs/${encodeURIComponent(key)}/preview`),
  getBrandInfo: () => request<{ item: BrandInfo | null }>('/api/brand-info'),
  createBrandInfo: (payload: BrandInfoPayload) =>
    request<{ item: BrandInfo }>('/api/brand-info', { method: 'POST', body: JSON.stringify(payload) }),
  updateBrandInfo: (payload: BrandInfoPayload) =>
    request<{ item: BrandInfo }>('/api/brand-info', { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteBrandInfo: () => request<{ ok: boolean }>('/api/brand-info', { method: 'DELETE' }),
  getShootConfig: () => request<{ item: ShootConfig }>('/api/shoot-config'),
  exportShootConfig: () => request<ShootConfigExportPayload>('/api/shoot-config/export'),
  importShootConfig: (config: unknown) =>
    request<{ item: ShootConfig }>('/api/shoot-config/import', { method: 'POST', body: JSON.stringify({ config }) }),
  updateShootConfig: (config: ShootConfig) =>
    request<{ item: ShootConfig }>('/api/shoot-config', { method: 'PATCH', body: JSON.stringify({ config }) }),
  getTaxonomyRules: () => request<TaxonomyRules>('/api/taxonomy/rules'),
  exportTaxonomyRules: () => request<TaxonomyExportPayload>('/api/taxonomy/export'),
  importTaxonomyRules: (payload: { dimensions: unknown[]; tags: unknown[]; relations: unknown[]; applicabilityRules: unknown[] }) =>
    request<TaxonomyRules>('/api/taxonomy/import', { method: 'POST', body: JSON.stringify(payload) }),
  createTaxonomyDimension: (payload: TaxonomyDimensionPayload & { code: string; labelZh: string }) =>
    request<TaxonomyRules>('/api/taxonomy/dimensions', { method: 'POST', body: JSON.stringify(payload) }),
  updateTaxonomyDimension: (code: string, payload: TaxonomyDimensionPayload) =>
    request<TaxonomyRules>(`/api/taxonomy/dimensions/${code}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteTaxonomyDimension: (code: string) =>
    request<TaxonomyRules>(`/api/taxonomy/dimensions/${code}`, { method: 'DELETE' }),
  createTaxonomyTag: (payload: TaxonomyTagPayload & { dimensionCode: string; code: string; labelZh: string }) =>
    request<TaxonomyRules>('/api/taxonomy/tags', { method: 'POST', body: JSON.stringify(payload) }),
  updateTaxonomyTag: (id: number, payload: TaxonomyTagPayload) =>
    request<TaxonomyRules>(`/api/taxonomy/tags/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteTaxonomyTag: (id: number) =>
    request<TaxonomyRules>(`/api/taxonomy/tags/${id}`, { method: 'DELETE' }),
  createTaxonomyRelation: (payload: TaxonomyRelationPayload & { fromDimensionCode: string; fromTagCode: string; relationType: string; toDimensionCode: string; toTagCode: string }) =>
    request<TaxonomyRules>('/api/taxonomy/relations', { method: 'POST', body: JSON.stringify(payload) }),
  updateTaxonomyRelation: (id: number, payload: TaxonomyRelationPayload) =>
    request<TaxonomyRules>(`/api/taxonomy/relations/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteTaxonomyRelation: (id: number) =>
    request<TaxonomyRules>(`/api/taxonomy/relations/${id}`, { method: 'DELETE' }),
  createTaxonomyApplicabilityRule: (payload: TaxonomyApplicabilityPayload & { categoryTagCode: string; targetDimensionCode: string; targetTagCode: string }) =>
    request<TaxonomyRules>('/api/taxonomy/applicability-rules', { method: 'POST', body: JSON.stringify(payload) }),
  updateTaxonomyApplicabilityRule: (id: number, payload: TaxonomyApplicabilityPayload) =>
    request<TaxonomyRules>(`/api/taxonomy/applicability-rules/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteTaxonomyApplicabilityRule: (id: number) =>
    request<TaxonomyRules>(`/api/taxonomy/applicability-rules/${id}`, { method: 'DELETE' }),
  backfillTaxonomyAssignments: () => request<{ ok: boolean; garmentCount: number }>('/api/taxonomy/backfill-assignments', { method: 'POST' }),
}
