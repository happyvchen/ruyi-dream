export type GarmentImage = {
  id: number
  filename: string
  role: string
  mimeType: string
  url: string | null
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
  profile: Record<string, unknown>
  isDefault: boolean
  previewPath?: string | null
}

export type Shot = {
  shotIndex: number
  title: string
  frame: string
  angle: string
  focus: string
  pose: string
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
  taskPackage: Record<string, unknown>
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
  shotPlanId: number
  modelProfileId: number
  kind: string
  status: string
  outputDir: string
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

