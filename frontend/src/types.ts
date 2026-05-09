export type GarmentImage = {
  id: number
  filename: string
  role: string
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
  slotName?: string
  englishName?: string
  title: string
  task?: string
  unifiedRequirement?: string
  scene?: string
  frame: string
  cropRange?: string
  crop?: string
  modelScale?: string
  headTreatment?: string
  angle: string
  focus: string
  imagePurpose?: string
  pose: string
  poseTone?: string
  bodyPosition?: string
  headReference: string
  background: string
  styling: {
    shoes?: string
    bag?: string
    jewelry?: string
    forbidden?: string[]
  }
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
    salesInfo?: string
    evidenceLevel?: string
    lilyTone?: string
    stylingPlan?: string
    colorPalette?: string
    accessories?: string
    garment: GarmentAnalysis
    brandStyle: {
      name: string
      summary: string
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
