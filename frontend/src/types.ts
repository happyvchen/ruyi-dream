export type GarmentImage = {
  id: number
  filename: string
  role: string
  sortOrder: number
  mimeType: string
  url: string | null
  fileSize?: number | null
  createdAt: string
}

export type GarmentSku = {
  id: number
  sku: string
  title: string
  source: string
  status: string
  analysis?: GarmentAnalysis | null
  storagePath: string
  coverUrl: string | null
  coverFileSize?: number | null
  imageCount?: number | null
  images?: GarmentImage[]
  createdAt: string
  updatedAt: string
}

export type GarmentAnalysis = {
  category?: string
  summary?: string
  color?: string
  fabric?: string
  keyStructures?: string[]
  preserveStrictly?: string[]
}

export type ModelProfile = {
  id: number
  modelId: string
  name: string
  assetDir: string
  profile: Record<string, unknown>
  isDefault: boolean
  previewPath?: string | null
  previewUrl?: string | null
  createdAt: string
  updatedAt: string
}

export type Shot = {
  shotIndex: number
  englishName?: string
  title: string
  task?: string
  scene?: string
  cropRange?: string
  modelScale?: string
  headTreatment?: string
  imagePurpose?: string
  poseTone?: string
  bodyPosition?: string
}

export type ShotPlan = {
  id: number
  skuId: number
  modelProfileId: number
  status: string
  plannerProvider: string
  rawResponse?: string
  plan: {
    sku: string
    modelId: string
    category?: string
    garment: GarmentAnalysis
    brandStyle: {
      name: string
      summary: string
    }
    generationPlan?: {
      salesInfo?: string
      lilyTone?: string
      styling?: string
      background?: string
    }
    shots: Shot[]
  }
  createdAt: string
  updatedAt: string
}

export type GenerationImage = {
  id: number
  runId: number
  shotIndex: number
  title: string
  status: string
  url: string | null
  fileSize?: number | null
  taskPackage: Record<string, unknown>
  providerRequest?: Record<string, unknown> | null
  providerResponse?: Record<string, unknown> | null
  prompt?: string | null
  imageInputs?: Record<string, unknown>[]
  templateSnapshot?: Record<string, unknown> | null
  modelSnapshot?: Record<string, unknown> | null
  garmentSnapshot?: Record<string, unknown> | null
  attempts?: Record<string, unknown>[]
  currentAttemptIndex?: number
  providerRequestId?: string | null
  providerStatus?: string | null
  providerUrls?: Record<string, unknown>
  providerStatusResponse?: Record<string, unknown> | null
  submittedAt?: string | null
  completedAt?: string | null
  lastStatusCheckedAt?: string | null
  regenerateFromImageId?: number | null
  regenerateReason?: string | null
  isFinal: boolean
  createdAt: string
  updatedAt: string
}

export type GenerationRun = {
  id: number
  skuId: number
  sku?: string
  skuTitle?: string | null
  shotPlanId: number
  modelProfileId: number
  kind: string
  status: string
  outputDir: string
  archivedAt?: string | null
  archiveReason?: string | null
  summary?: {
    imageCount: number
    succeededCount: number
    finalCount: number
    regenerationCount: number
    previews: {
      id: number
      shotIndex: number
      title: string
      url: string | null
      status: string
      isFinal: boolean
      attemptCount: number
    }[]
  }
  images?: GenerationImage[]
  createdAt: string
  updatedAt: string
}

export type EventLog = {
  id: number
  entityType: string
  entityId?: number
  eventType: string
  payload: Record<string, unknown>
  createdAt: string
}
