import { describe, expect, it } from 'vitest'
import type { UploadSession } from '../../api'
import { compareCreatedAtDesc, mergeUploadSessions, mergeUploadSessionUpdates, uploadSessionRefreshTargets } from '../../App'

function session(id: string, scope: string, createdAt: string, updatedAt = createdAt): UploadSession {
  return {
    id,
    scope,
    status: 'FINALIZED',
    totalCount: 1,
    uploadedCount: 1,
    createdAt,
    updatedAt,
  }
}

describe('upload session refreshes', () => {
  it('loads both business scopes during session initialization', () => {
    expect(uploadSessionRefreshTargets()).toEqual([
      { scope: 'generation_task', limit: 20 },
      { scope: 'styling_garment', limit: 100 },
    ])
  })

  it('polls each active business scope with its own history window', () => {
    const stylingUpload = { ...session('styling-active', 'styling-garment', '2026-07-28T10:00:00Z'), status: 'UPLOADING' }
    const generationUpload = { ...session('generation-active', 'generation-task', '2026-07-28T10:01:00Z'), status: 'FINALIZING' }

    expect(uploadSessionRefreshTargets([stylingUpload, generationUpload])).toEqual([
      { scope: 'generation_task', limit: 20 },
      { scope: 'styling_garment', limit: 100 },
    ])
  })

  it('replaces only the requested scope during a scoped refresh', () => {
    const staleStylingUpload = session('styling-stale', 'styling-garment', '2026-07-20T10:00:00Z')
    const generationUpload = session('generation-current', 'generation-task', '2026-07-21T10:00:00Z')
    const currentStylingUpload = session('styling-current', 'styling-garment', '2026-07-28T10:00:00Z')

    expect(mergeUploadSessions(
      [staleStylingUpload, generationUpload],
      [currentStylingUpload],
      'styling_garment',
    )).toEqual([
      currentStylingUpload,
      generationUpload,
    ])
  })

  it('updates active sessions precisely without dropping older sessions outside the history window', () => {
    const olderActive = { ...session('older-active', 'generation-task', '2026-07-01T10:00:00Z'), status: 'UPLOADING' }
    const recentHistory = session('recent-history', 'generation-task', '2026-07-28T10:00:00Z')
    const refreshedActive = { ...olderActive, status: 'FINALIZING', updatedAt: '2026-07-29T10:00:00Z' }

    expect(mergeUploadSessionUpdates(
      [recentHistory, olderActive],
      [refreshedActive],
    )).toEqual([
      refreshedActive,
      recentHistory,
    ])
  })
})

describe('top-level business record ordering', () => {
  it('sorts by first creation time and uses the id as a stable descending tie-breaker', () => {
    const records = [
      { id: '2', createdAt: '2026-07-20T10:00:00Z', updatedAt: '2026-07-30T10:00:00Z' },
      { id: '1', createdAt: '2026-07-21T10:00:00Z', updatedAt: '2026-07-21T10:00:00Z' },
      { id: '3', createdAt: '2026-07-21T10:00:00Z', updatedAt: '2026-07-21T10:00:00Z' },
    ]

    expect(records.sort(compareCreatedAtDesc).map((record) => record.id)).toEqual(['3', '1', '2'])
  })
})
