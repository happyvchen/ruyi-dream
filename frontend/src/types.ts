export type EventLog = {
  id: number
  entityType: string
  entityId?: number
  eventType: string
  payload: Record<string, unknown>
  createdAt: string
}

export type PaginatedResponse<T> = {
  items: T[]
  limit: number
  offset: number
  nextOffset: number | null
  hasMore: boolean
  total: number
  totalPages: number
}
