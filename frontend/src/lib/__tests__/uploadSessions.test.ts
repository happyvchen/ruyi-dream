import { describe, expect, it } from 'vitest'
import type { UploadSession } from '../../api'
import { mergeUploadSessions, uploadSessionRefreshTargets } from '../../App'

function session(id: string, scope: string, updatedAt: string): UploadSession {
  return {
    id,
    scope,
    status: 'FINALIZED',
    totalCount: 1,
    uploadedCount: 1,
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
})
