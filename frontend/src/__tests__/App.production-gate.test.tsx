/* @vitest-environment jsdom */
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from '../App'

const apiMock = vi.hoisted(() => ({
  session: vi.fn(),
  login: vi.fn(),
  logout: vi.fn(),
  listStylingGarments: vi.fn(),
  listTasks: vi.fn(),
  listRecords: vi.fn(),
  listModels: vi.fn(),
  listPromptConfigs: vi.fn(),
  previewPromptConfig: vi.fn(),
  listPromptInvocations: vi.fn(),
  listRecordWorkspace: vi.fn(),
  listRecordBatches: vi.fn(),
  getRecord: vi.fn(),
  getTaskDeleteImpact: vi.fn(),
  getTask: vi.fn(),
  updateTask: vi.fn(),
  createTask: vi.fn(),
  autoRunFinal: vi.fn(),
  cancelTask: vi.fn(),
  cancelAutoRun: vi.fn(),
  listBatchTasks: vi.fn(),
  autoRunBatch: vi.fn(),
  cancelBatch: vi.fn(),
  precheckFinalImageExport: vi.fn(),
  listTaskAttempts: vi.fn(),
  getTaskAiInvocation: vi.fn(),
  getStylingGarment: vi.fn(),
  getStylingGarmentDeleteImpact: vi.fn(),
  deleteStylingGarment: vi.fn(),
  deleteStylingGarments: vi.fn(),
  archiveTasks: vi.fn(),
  deleteTask: vi.fn(),
  deleteTasks: vi.fn(),
  restoreStylingGarment: vi.fn(),
  restoreStylingGarments: vi.fn(),
  restoreTasks: vi.fn(),
  getShootEnvironment: vi.fn(),
  addModelHeadReferences: vi.fn(),
  replaceModelHeadReference: vi.fn(),
  deleteModelHeadReference: vi.fn(),
  reorderModelHeadReference: vi.fn(),
  setModelHeadReferenceAngle: vi.fn(),
  createStylingImportSessions: vi.fn(),
  resumeUploadSession: vi.fn(),
  uploadPreparedSession: vi.fn(),
  finalizeStylingGarmentSession: vi.fn(),
  listUploadSessions: vi.fn(),
  abandonUploadSession: vi.fn(),
  getUploadSessionDeleteImpact: vi.fn(),
  archiveUploadSessions: vi.fn(),
  restoreUploadSessions: vi.fn(),
  deleteUploadSessions: vi.fn(),
  tagStylingGarments: vi.fn(),
}))

const directoryAccessMock = vi.hoisted(() => ({
  getDirectoryHandle: vi.fn(),
}))

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api')
  return {
    ...actual,
    API_BASE: 'http://test.local',
    api: apiMock,
    mediaUrl: (url?: string | null) => url ?? '',
    mediaDownloadUrl: (url?: string | null) => url ?? '',
    finalImageArchiveUrl: (ids: number[]) => `/archive?ids=${ids.join(',')}`,
    stylingGarmentArchiveUrl: (ids: number[]) => `/garment-archive?ids=${ids.join(',')}`,
  }
})

vi.mock('../lib/directoryAccess', async () => {
  const actual = await vi.importActual<typeof import('../lib/directoryAccess')>('../lib/directoryAccess')
  return {
    ...actual,
    getDirectoryHandle: directoryAccessMock.getDirectoryHandle,
  }
})

const emptyPage = {
  items: [],
  limit: 30,
  offset: 0,
  nextOffset: null,
  hasMore: false,
  total: 0,
  totalPages: 1,
}

const defaultModel = {
  id: 1,
  modelId: 'Face-01',
  name: '默认模特',
  profile: { headReferenceEntries: [] },
  isDefault: true,
  thumbUrl: null,
  previewUrl: null,
}

const promptList = {
  items: [
    {
      key: 'assembly.ai_styling',
      groupType: 'assembly',
      title: 'AI 选搭',
      description: 'AI Stylist 选搭 Prompt',
      content: '选择真实搭配服装',
      contentHash: 'hash',
      sortOrder: 110,
      usedSegments: [],
      missingPlaceholders: [],
      referencedPlaceholders: [],
      renderedPreview: '预览内容',
    },
  ],
  placeholders: [],
}

const task = {
  id: 1,
  name: 'LOOK-001',
  status: 'READY_FOR_STYLING',
  userRequirement: '',
  stageUserInputs: { aiStyling: '', storyboardPreview: '', finalImage: '' },
  currentInputVersionId: null,
  activeStylingId: null,
  activePreviewId: null,
  activeStoryboardPreviewId: null,
  activeResultSetId: null,
  batchId: null,
  autoRunTarget: null,
  autoRunRequestedAt: null,
  autoRunQueued: false,
  autoRunQueuePosition: null,
  archivedAt: null,
  archiveReason: null,
  createdAt: '2026-07-17T00:00:00Z',
  updatedAt: '2026-07-17T00:00:00Z',
  images: [
    {
      id: 11,
      sortOrder: 0,
      asset: {
        id: 101,
        objectKey: 'hero.jpg',
        filename: 'hero.jpg',
        mimeType: 'image/jpeg',
        thumbUrl: '/thumb.jpg',
        previewUrl: '/preview.jpg',
        originalUrl: '/original.jpg',
      },
    },
  ],
  inputVersion: null,
  styling: null,
  preview: null,
  storyboardPreview: null,
  resultSet: null,
  aiOperations: [],
}

const garment = {
  id: 1,
  code: 'CODX-UI-0718-B-8',
  status: 'READY',
  aiTags: {},
  manualTags: {},
  archivedAt: null,
  archiveReason: null,
  imageCount: 1,
  coverUrl: null,
  coverAsset: null,
  images: [],
  createdAt: '2026-07-17T00:00:00Z',
  updatedAt: '2026-07-17T00:00:00Z',
}

function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function configurePendingStylingUpload() {
  const upload = deferred<void>()
  apiMock.createStylingImportSessions.mockImplementation(async (_title: string, batches: { code: string }[], batchId: string) => {
    const session = {
      id: 'upload-session-1',
      scope: 'styling-garment',
      status: 'PENDING',
      businessCode: batches[0]!.code,
      batchId,
      totalCount: 1,
      uploadedCount: 0,
      items: [],
    }
    const prepared = { session, items: [] }
    apiMock.resumeUploadSession.mockResolvedValue(prepared)
    apiMock.finalizeStylingGarmentSession.mockResolvedValue({
      item: { ...garment, code: batches[0]!.code, status: 'DRAFT' },
      session: { ...session, status: 'FINALIZED', uploadedCount: 1, targetId: garment.id },
    })
    return { batchId, items: [prepared] }
  })
  apiMock.uploadPreparedSession.mockImplementation(() => upload.promise)
  apiMock.listUploadSessions.mockResolvedValue({ items: [] })
  apiMock.tagStylingGarments.mockResolvedValue({ items: [], startedIds: [], skipped: [] })
  return upload
}

function configureApi(role: 'admin' | 'user' = 'admin') {
  apiMock.session.mockResolvedValue({ authRequired: true, authenticated: true, role })
  apiMock.login.mockResolvedValue({ authRequired: true, authenticated: true, role })
  apiMock.logout.mockResolvedValue({ ok: true })
  apiMock.listStylingGarments.mockResolvedValue({ ...emptyPage, facets: {}, stableFacets: {} })
  apiMock.listTasks.mockResolvedValue(emptyPage)
  apiMock.listRecords.mockResolvedValue(emptyPage)
  apiMock.listModels.mockResolvedValue({ ...emptyPage, items: [defaultModel] })
  apiMock.getTask.mockResolvedValue({ item: task })
  apiMock.updateTask.mockResolvedValue({ item: task })
  apiMock.listPromptConfigs.mockResolvedValue(promptList)
  apiMock.previewPromptConfig.mockResolvedValue({ rendered: '预览内容', diagnostics: { usedSegments: [], missingPlaceholders: [], referencedPlaceholders: [] } })
  apiMock.listPromptInvocations.mockResolvedValue({ items: [] })
  apiMock.listRecordWorkspace.mockResolvedValue({ ...emptyPage, facets: {}, stableFacets: {} })
  apiMock.listRecordBatches.mockResolvedValue({ ...emptyPage, facets: {}, stableFacets: {} })
  apiMock.precheckFinalImageExport.mockResolvedValue({ items: [], downloadableTaskIds: [], completeTaskIds: [], totalImages: 0, totalBytes: 0 })
  apiMock.getShootEnvironment.mockResolvedValue({ asset: null })
  apiMock.listUploadSessions.mockResolvedValue({ items: [] })
  apiMock.abandonUploadSession.mockResolvedValue({ session: { id: 'abandoned', status: 'ABANDONED' } })
  apiMock.getUploadSessionDeleteImpact.mockResolvedValue({ impact: {
    uploadSessionCount: 1,
    uploadItemCount: 0,
    temporaryObjectCount: 0,
    linkedBusinessCount: 0,
  } })
  apiMock.archiveUploadSessions.mockResolvedValue({ archivedIds: [] })
  apiMock.restoreUploadSessions.mockResolvedValue({ restoredIds: [] })
  apiMock.deleteUploadSessions.mockResolvedValue({ deletedIds: [], deferredIds: [], objectCleanupErrors: [] })
}

beforeEach(() => {
  vi.clearAllMocks()
  window.history.replaceState(null, '', '/studio')
  const storage = new Map<string, string>()
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: {
      getItem: vi.fn((key: string) => storage.get(key) ?? null),
      setItem: vi.fn((key: string, value: string) => storage.set(key, value)),
      removeItem: vi.fn((key: string) => storage.delete(key)),
      clear: vi.fn(() => storage.clear()),
    },
  })
  window.localStorage.clear()
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  })
  configureApi()
  directoryAccessMock.getDirectoryHandle.mockResolvedValue(undefined)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('production gate app shell and page controls', () => {
  it('admin can reach every 0.1.0 page without exposing provider console as a top-level page', async () => {
    render(<App />)

    expect(await screen.findByRole('button', { name: '搭配服装库' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '模特与背景' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '生成记录' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Prompt 维护' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /provider|Image-2|Gemini|任务包/i })).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: '搭配服装库' }))
    expect(await screen.findByRole('button', { name: '上传搭配服装' })).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: '模特与背景' }))
    expect(await screen.findByText('默认模特')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: '生成记录' }))
    expect(await screen.findByText('全部记录')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Prompt 维护' }))
    await waitFor(() => expect(screen.getAllByText('AI 选搭').length).toBeGreaterThanOrEqual(1))
  })

  it('ordinary user keeps the shooting workflow but cannot open Prompt maintenance', async () => {
    configureApi('user')
    window.history.replaceState(null, '', '/prompt-maintenance')

    render(<App />)

    expect(await screen.findByRole('button', { name: '搭配服装库' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Prompt 维护' })).not.toBeInTheDocument()
    await waitFor(() => expect(window.location.pathname).toBe('/studio'))
  })

  it('studio summary exposes always-open business requirement modules and one-click final action', async () => {
    window.history.replaceState(null, '', '/studio?task=1')
    render(<App />)

    const summary = await screen.findByRole('tabpanel', { name: /任务摘要/ })
    expect(await within(summary).findByText('LOOK-001')).toBeInTheDocument()
    expect(within(summary).getByRole('button', { name: /一键成片/ })).toBeEnabled()
    expect(screen.getByRole('tab', { name: /任务摘要/ })).toHaveAttribute('aria-selected', 'true')
    expect(within(summary).getByRole('textbox', { name: '智能选搭要求' })).toBeInTheDocument()

    await userEvent.click(screen.getByRole('tab', { name: /最近任务/ }))
    expect(await screen.findByText('创建拍摄任务后会出现在这里。')).toBeInTheDocument()
  })

  it('renders every task chip in a batch instead of truncating the batch navigation', async () => {
    const batchTask = { ...task, batchId: 'batch-13' }
    const batchTasks = Array.from({ length: 13 }, (_, index) => ({
      ...batchTask,
      id: index + 1,
      name: `LOOK-${String(index + 1).padStart(3, '0')}`,
    }))
    apiMock.getTask.mockResolvedValue({ item: batchTask })
    apiMock.listBatchTasks.mockResolvedValue({ items: batchTasks })
    window.history.replaceState(null, '', '/studio?task=1')

    render(<App />)

    expect(await screen.findByRole('button', { name: /LOOK-013/ })).toBeInTheDocument()
    expect(document.querySelectorAll('.batch-bar-chips .batch-chip')).toHaveLength(13)
  })

  it('shows every task when a recent batch is expanded', async () => {
    const batchTask = { ...task, batchId: 'batch-13' }
    const batchTasks = Array.from({ length: 13 }, (_, index) => ({
      ...batchTask,
      id: index + 1,
      name: `LOOK-${String(index + 1).padStart(3, '0')}`,
    }))
    const batchRecord = {
      id: 'batch-13',
      batchId: 'batch-13',
      name: '五月回图',
      status: 'RUNNING',
      taskCount: 13,
      activeTaskCount: 13,
      archivedTaskCount: 0,
      completedTaskCount: 0,
      failedTaskCount: 0,
      cancelledTaskCount: 0,
      runningTaskCount: 13,
      queuedTaskCount: 0,
      downloadableTaskCount: 0,
      statusCounts: { READY_FOR_STYLING: 13 },
      createdAt: task.createdAt,
      updatedAt: task.updatedAt,
      coverTasks: batchTasks.slice(0, 4),
      sampleTaskNames: batchTasks.slice(0, 3).map((item) => item.name),
      firstTaskId: 1,
    }
    apiMock.getTask.mockResolvedValue({ item: batchTask })
    apiMock.listBatchTasks.mockResolvedValue({ items: batchTasks })
    apiMock.listRecordWorkspace.mockResolvedValue({
      ...emptyPage,
      items: [{
        kind: 'batch',
        id: 'batch:batch-13',
        status: 'RUNNING',
        name: batchRecord.name,
        createdAt: batchRecord.createdAt,
        updatedAt: batchRecord.updatedAt,
        batch: batchRecord,
      }],
      total: 1,
    })
    window.history.replaceState(null, '', '/studio?task=1')

    render(<App />)

    await userEvent.click(await screen.findByRole('tab', { name: /最近任务/ }))
    const recentPanel = screen.getByRole('tabpanel', { name: /最近任务/ })
    await userEvent.click(await within(recentPanel).findByRole('button', { name: /五月回图.*13 项/ }))

    expect(await within(recentPanel).findByRole('button', { name: /LOOK-013/ })).toBeInTheDocument()
    expect(within(recentPanel).queryByText(/还有 8 个任务/)).not.toBeInTheDocument()
  })

  it('library import and bulk controls are visible without hiding core filters', async () => {
    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: '搭配服装库' }))

    expect(screen.getByRole('button', { name: '上传搭配服装' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /批量管理/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /查看已归档/ })).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: '上传搭配服装' }))
    expect(await screen.findByText('拖入图片或文件夹')).toBeInTheDocument()
    expect(screen.getByText('选择图片')).toBeInTheDocument()
    expect(screen.getByText('选择文件夹')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: '返回服装库' }).length).toBeGreaterThanOrEqual(1)
  })

  it('allows returning to the garment library while an upload is still running', async () => {
    const upload = configurePendingStylingUpload()
    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: '搭配服装库' }))
    await userEvent.click(screen.getByRole('button', { name: '上传搭配服装' }))

    const imagePicker = screen.getByText('选择图片').querySelector('input[type="file"]') as HTMLInputElement
    await userEvent.upload(imagePicker, new File(['image'], 'LOOK-RETURN.jpg', { type: 'image/jpeg' }))
    await userEvent.click(await screen.findByRole('button', { name: '上传' }))

    expect(await screen.findByText('上传任务正在进行')).toBeInTheDocument()
    await waitFor(() => expect(apiMock.uploadPreparedSession).toHaveBeenCalledTimes(1))
    await userEvent.click(screen.getByRole('button', { name: '返回服装库' }))
    expect(await screen.findByRole('button', { name: /批量管理/ })).toBeInTheDocument()

    upload.resolve()
    await waitFor(() => expect(apiMock.finalizeStylingGarmentSession).toHaveBeenCalledWith('upload-session-1', expect.any(String)))
  })

  it('allows using the import panel return action while another upload is running', async () => {
    configurePendingStylingUpload()
    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: '搭配服装库' }))
    await userEvent.click(screen.getByRole('button', { name: '上传搭配服装' }))

    const imagePicker = screen.getByText('选择图片').querySelector('input[type="file"]') as HTMLInputElement
    await userEvent.upload(imagePicker, new File(['first'], 'LOOK-RETURN.jpg', { type: 'image/jpeg' }))
    await userEvent.click(await screen.findByRole('button', { name: '上传' }))
    await waitFor(() => expect(apiMock.uploadPreparedSession).toHaveBeenCalledTimes(1))

    await userEvent.upload(imagePicker, new File(['second'], 'LOOK-NEXT.jpg', { type: 'image/jpeg' }))
    const returnButtons = await screen.findAllByRole('button', { name: '返回服装库' })
    expect(returnButtons).toHaveLength(2)
    await userEvent.click(returnButtons[1]!)

    expect(await screen.findByRole('button', { name: /批量管理/ })).toBeInTheDocument()
  })

  it('shows a refreshed browser upload as paused even while the server session remains uploading', async () => {
    const session = {
      id: 'upload-session-refresh',
      scope: 'styling-garment',
      status: 'UPLOADING',
      businessCode: 'LOOK-REFRESH',
      sourceTitle: '刷新恢复批次',
      batchId: 'import-refresh',
      totalCount: 3,
      uploadedCount: 1,
      items: [],
      createdAt: '2026-07-28T08:00:00Z',
      updatedAt: '2026-07-28T08:01:00Z',
    }
    window.localStorage.setItem('ruyi-library-import-open', '1')
    window.localStorage.setItem('ruyi-library-import-jobs', JSON.stringify([{
      id: 'import-refresh',
      title: '刷新恢复批次',
      status: 'RUNNING',
      createdAt: session.createdAt,
      updatedAt: session.updatedAt,
      total: 1,
      uploaded: 0,
      skipped: 0,
      progress: null,
      operations: [],
      items: [{
        id: 'refresh-item',
        code: 'LOOK-REFRESH',
        imageCount: 3,
        uploadedCount: 1,
        status: 'UPLOADING',
        sessionId: session.id,
      }],
    }]))
    apiMock.listUploadSessions.mockImplementation(async (options: { scope?: string }) => ({
      items: options.scope === 'styling_garment' ? [session] : [],
    }))
    window.history.replaceState(null, '', '/library')

    render(<App />)

    expect(await screen.findByText('上传已暂停')).toBeInTheDocument()
    expect(screen.queryByText('上传中')).not.toBeInTheDocument()
  })

  it('keeps server-side saving and tagging visible beside a paused browser upload', async () => {
    const createdAt = '2026-07-28T08:00:00Z'
    const sessions = [
      { id: 'session-uploading', status: 'UPLOADING', businessCode: 'LOOK-UPLOAD', uploadedCount: 1 },
      { id: 'session-saving', status: 'FINALIZING', businessCode: 'LOOK-SAVE', uploadedCount: 3 },
      { id: 'session-tagging', status: 'FINALIZED', businessCode: 'LOOK-TAG', uploadedCount: 3, targetId: 99, targetStatus: 'TAGGING_RUNNING' },
    ].map((session) => ({
      scope: 'styling-garment',
      sourceTitle: '混合恢复批次',
      batchId: 'import-mixed-refresh',
      totalCount: 3,
      items: [],
      createdAt,
      updatedAt: createdAt,
      ...session,
    }))
    window.localStorage.setItem('ruyi-library-import-open', '1')
    window.localStorage.setItem('ruyi-library-import-jobs', JSON.stringify([{
      id: 'import-mixed-refresh',
      title: '混合恢复批次',
      status: 'RUNNING',
      createdAt,
      updatedAt: createdAt,
      total: 3,
      uploaded: 0,
      skipped: 0,
      progress: null,
      operations: [],
      items: [
        { id: 'uploading', code: 'LOOK-UPLOAD', imageCount: 3, uploadedCount: 1, status: 'UPLOADING', sessionId: 'session-uploading' },
        { id: 'saving', code: 'LOOK-SAVE', imageCount: 3, uploadedCount: 3, status: 'SAVING', sessionId: 'session-saving' },
        { id: 'tagging', code: 'LOOK-TAG', imageCount: 3, uploadedCount: 3, status: 'TAGGING', sessionId: 'session-tagging' },
      ],
    }]))
    apiMock.listUploadSessions.mockImplementation(async (options: { scope?: string }) => ({
      items: options.scope === 'styling_garment' ? sessions : [],
    }))
    window.history.replaceState(null, '', '/library')

    render(<App />)

    expect(await screen.findByText('部分上传已暂停')).toBeInTheDocument()
    expect(screen.getByText('1 已暂停')).toBeInTheDocument()
    expect(screen.getByText('1 整理中')).toBeInTheDocument()
    expect(screen.getByText('1 AI 打标中')).toBeInTheDocument()
  })

  it('only offers directory reselection when a paused upload has no saved directory permission', async () => {
    window.localStorage.setItem('ruyi-library-import-open', '1')
    window.localStorage.setItem('ruyi-library-import-jobs', JSON.stringify([{
      id: 'import-without-handle',
      title: '无目录授权批次',
      status: 'INTERRUPTED',
      total: 1,
      uploaded: 0,
      skipped: 0,
      progress: null,
      operations: [],
      items: [{ id: 'item-1', code: 'LOOK-NO-HANDLE', imageCount: 3, uploadedCount: 1, status: 'INTERRUPTED', sessionId: 'session-no-handle' }],
    }]))
    window.history.replaceState(null, '', '/library')

    render(<App />)

    expect(await screen.findByText('重新选择原目录')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '继续上传' })).not.toBeInTheDocument()
  })

  it('offers archive and deletion management for upload records in every status', async () => {
    const failedSession = {
      id: 'failed-upload-session',
      scope: 'styling-garment',
      status: 'FAILED',
      businessCode: 'LOOK-FAILED',
      sourceTitle: '失败上传批次',
      batchId: 'failed-upload-batch',
      totalCount: 3,
      uploadedCount: 1,
      items: [],
      errorMessage: '上传连接已中断',
      createdAt: '2026-07-28T08:00:00Z',
      updatedAt: '2026-07-28T08:01:00Z',
    }
    const finalizedSession = {
      ...failedSession,
      id: 'finalized-upload-session',
      status: 'FINALIZED',
      businessCode: 'LOOK-FINALIZED',
      sourceTitle: '已入库上传批次',
      batchId: 'finalized-upload-batch',
      uploadedCount: 3,
      targetId: 101,
      errorMessage: null,
      updatedAt: '2026-07-28T09:01:00Z',
    }
    apiMock.listUploadSessions.mockImplementation(async (options: { scope?: string }) => ({
      items: options.scope === 'styling_garment' ? [failedSession, finalizedSession] : [],
    }))
    window.localStorage.setItem('ruyi-library-import-open', '1')
    window.history.replaceState(null, '', '/library')

    render(<App />)

    await waitFor(() => expect(apiMock.listUploadSessions).toHaveBeenCalledWith(expect.objectContaining({ includeArchived: true })))
    const manageButtons = await screen.findAllByRole('button', { name: '归档' })
    expect(manageButtons).toHaveLength(2)
    manageButtons.forEach((button) => expect(button).toBeEnabled())

    const failedRecord = screen.getByText('失败上传批次').closest('section')
    expect(failedRecord).not.toBeNull()
    await userEvent.click(within(failedRecord!).getByRole('button', { name: '归档' }))
    expect(await screen.findByRole('dialog', { name: '归档或删除上传记录' })).toBeInTheDocument()
    await waitFor(() => expect(apiMock.getUploadSessionDeleteImpact).toHaveBeenCalled())
    await userEvent.type(screen.getByPlaceholderText('失败上传批次'), '失败上传批次')
    const purgeButton = screen.getByRole('button', { name: '彻底删除' })
    expect(purgeButton).toBeEnabled()
    apiMock.deleteUploadSessions.mockResolvedValue({ deletedIds: [], deferredIds: ['failed-upload-session'], objectCleanupErrors: [] })
    await userEvent.click(purgeButton)
    expect(await screen.findByText('上传已停止；相关临时文件将在上传地址安全失效后自动清理。')).toBeInTheDocument()
  })

  it('supports selecting multiple active upload records and only exposes batch archive', async () => {
    const archiveRequest = deferred<{ archivedIds: string[] }>()
    const sessions = [
      { id: 'batch-archive-session-1', businessCode: 'LOOK-BULK-1', sourceTitle: '批量归档一' },
      { id: 'batch-archive-session-2', businessCode: 'LOOK-BULK-2', sourceTitle: '批量归档二' },
    ].map((session, index) => ({
      scope: 'styling-garment',
      status: 'FAILED',
      batchId: null,
      totalCount: 1,
      uploadedCount: 0,
      items: [],
      createdAt: `2026-07-28T0${8 + index}:00:00Z`,
      updatedAt: `2026-07-28T0${8 + index}:01:00Z`,
      ...session,
    }))
    apiMock.listUploadSessions.mockImplementation(async (options: { scope?: string }) => ({
      items: options.scope === 'styling_garment' ? sessions : [],
    }))
    apiMock.archiveUploadSessions.mockImplementation(() => archiveRequest.promise)
    window.localStorage.setItem('ruyi-library-import-open', '1')
    window.history.replaceState(null, '', '/library')

    render(<App />)

    const workbench = (await screen.findByText('上传记录')).closest('.import-workbench') as HTMLElement
    expect(workbench).not.toBeNull()
    await userEvent.click(await within(workbench).findByRole('button', { name: '批量管理' }))
    const surface = workbench.querySelector('.import-record-list-body.marquee-selection-surface') as HTMLElement
    const selectableRecords = Array.from(surface.querySelectorAll<HTMLElement>('[data-select-id]'))
    surface.getBoundingClientRect = () => ({ left: 0, top: 0, right: 500, bottom: 500, width: 500, height: 500, x: 0, y: 0, toJSON: () => ({}) })
    selectableRecords[0]!.getBoundingClientRect = () => ({ left: 10, top: 10, right: 490, bottom: 80, width: 480, height: 70, x: 10, y: 10, toJSON: () => ({}) })
    selectableRecords[1]!.getBoundingClientRect = () => ({ left: 10, top: 90, right: 490, bottom: 160, width: 480, height: 70, x: 10, y: 90, toJSON: () => ({}) })
    fireEvent.pointerDown(surface, { button: 0, pointerType: 'mouse', clientX: 5, clientY: 5 })
    fireEvent.pointerMove(window, { pointerType: 'mouse', clientX: 495, clientY: 165 })
    fireEvent.pointerUp(window, { pointerType: 'mouse', clientX: 495, clientY: 165 })
    expect(await within(workbench).findByRole('button', { name: '归档 2 项' })).toBeEnabled()
    await userEvent.click(within(workbench).getByRole('button', { name: '全选本页' }))

    const firstRecord = within(workbench).getByText('批量归档一').closest('.import-record-row') as HTMLElement
    const firstExpandButton = within(firstRecord).getByRole('button', { name: '展开' })
    expect(firstExpandButton).toHaveAttribute('aria-expanded', 'false')
    await userEvent.click(within(workbench).getByRole('checkbox', { name: '选择 批量归档一' }))
    expect(firstExpandButton).toHaveAttribute('aria-expanded', 'false')
    await userEvent.keyboard('{Shift>}')
    await userEvent.click(within(workbench).getByRole('checkbox', { name: '选择 批量归档二' }))
    await userEvent.keyboard('{/Shift}')

    expect(within(workbench).getByRole('button', { name: '归档 2 项' })).toBeEnabled()
    expect(within(workbench).queryByRole('button', { name: /删除 2 项/ })).not.toBeInTheDocument()
    await userEvent.click(within(workbench).getByRole('button', { name: '归档 2 项' }))

    await waitFor(() => expect(apiMock.archiveUploadSessions).toHaveBeenCalledWith(expect.arrayContaining([
      'batch-archive-session-1',
      'batch-archive-session-2',
    ])))
    expect(within(workbench).getByRole('checkbox', { name: '选择 批量归档一' })).toBeDisabled()
    within(workbench).getAllByRole('button', { name: '归档' }).forEach((button) => expect(button).toBeDisabled())
    expect(apiMock.deleteUploadSessions).not.toHaveBeenCalled()
    archiveRequest.resolve({ archivedIds: sessions.map((item) => item.id) })
    await waitFor(() => expect(within(workbench).queryByRole('button', { name: '正在归档 2 项...' })).not.toBeInTheDocument())
  })

  it('chunks batch upload-record archive requests at the API limit', async () => {
    const sessions = Array.from({ length: 501 }, (_, index) => ({
      id: `large-batch-session-${index}`,
      scope: 'styling-garment',
      status: 'FAILED',
      businessCode: `LOOK-LARGE-${index}`,
      sourceTitle: '超大上传批次',
      batchId: 'large-upload-batch',
      totalCount: 1,
      uploadedCount: 0,
      items: [],
      createdAt: '2026-07-28T08:00:00Z',
      updatedAt: '2026-07-28T08:01:00Z',
    }))
    apiMock.listUploadSessions.mockImplementation(async (options: { scope?: string }) => ({
      items: options.scope === 'styling_garment' ? sessions : [],
    }))
    apiMock.archiveUploadSessions.mockImplementation(async (ids: string[]) => ({ archivedIds: ids }))
    window.localStorage.setItem('ruyi-library-import-open', '1')
    window.history.replaceState(null, '', '/library')

    render(<App />)

    const workbench = (await screen.findByText('上传记录')).closest('.import-workbench') as HTMLElement
    await userEvent.click(within(workbench).getByRole('button', { name: '批量管理' }))
    await userEvent.click(within(workbench).getByRole('checkbox', { name: '选择 超大上传批次' }))
    await userEvent.click(within(workbench).getByRole('button', { name: '归档 1 项' }))

    await waitFor(() => expect(apiMock.archiveUploadSessions).toHaveBeenCalledTimes(2))
    expect(apiMock.archiveUploadSessions.mock.calls[0]?.[0]).toHaveLength(500)
    expect(apiMock.archiveUploadSessions.mock.calls[1]?.[0]).toHaveLength(1)
  })

  it('keeps archived abandoned upload records recoverable and directly deletable', async () => {
    const archivedSession = {
      id: 'archived-abandoned-session',
      scope: 'styling-garment',
      status: 'ABANDONED',
      businessCode: 'LOOK-ARCHIVED',
      sourceTitle: '已归档放弃批次',
      batchId: 'archived-abandoned-batch',
      totalCount: 1,
      uploadedCount: 0,
      items: [],
      archivedAt: '2026-07-28T10:00:00Z',
      archiveReason: 'user_archived',
      createdAt: '2026-07-28T08:00:00Z',
      updatedAt: '2026-07-28T10:00:00Z',
    }
    const deletionPendingSession = {
      ...archivedSession,
      id: 'deletion-pending-session',
      businessCode: 'LOOK-DELETION-PENDING',
      sourceTitle: '等待清理批次',
      batchId: 'deletion-pending-batch',
      archiveReason: 'deletion_pending',
      updatedAt: '2026-07-28T11:00:00Z',
    }
    apiMock.listUploadSessions.mockImplementation(async (options: { scope?: string; includeArchived?: boolean }) => ({
      items: options.scope === 'styling_garment' && options.includeArchived ? [archivedSession, deletionPendingSession] : [],
    }))
    window.localStorage.setItem('ruyi-library-import-open', '1')
    window.history.replaceState(null, '', '/library')

    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: '查看已归档' }))

    expect(await screen.findByText('已归档放弃批次')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '恢复' })).toBeEnabled()
    const workbench = screen.getByText('上传记录').closest('.import-workbench') as HTMLElement
    await userEvent.click(within(workbench).getByRole('button', { name: '批量管理' }))
    expect(within(workbench).getByRole('checkbox', { name: '选择 已归档放弃批次' })).toBeDisabled()
    expect(within(workbench).getByRole('checkbox', { name: '选择 等待清理批次' })).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: '删除' }))
    expect(await screen.findByRole('dialog', { name: '归档或删除上传记录' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '归档' })).not.toBeInTheDocument()
  })

  it('blocks permanent upload-record deletion when impact precheck fails', async () => {
    const session = {
      id: 'precheck-failed-session', scope: 'styling-garment', status: 'FAILED', businessCode: 'LOOK-PRECHECK',
      sourceTitle: '预检失败批次', batchId: null, totalCount: 1, uploadedCount: 0, items: [],
      createdAt: '2026-07-28T08:00:00Z', updatedAt: '2026-07-28T08:01:00Z',
    }
    apiMock.listUploadSessions.mockImplementation(async (options: { scope?: string; includeArchived?: boolean }) => ({
      items: options.scope === 'styling_garment' && !options.includeArchived ? [session] : [],
    }))
    apiMock.getUploadSessionDeleteImpact.mockRejectedValue(new Error('precheck unavailable'))
    window.localStorage.setItem('ruyi-library-import-open', '1')
    window.history.replaceState(null, '', '/library')

    render(<App />)
    await waitFor(() => expect(apiMock.listUploadSessions).toHaveBeenCalledWith(expect.objectContaining({ includeArchived: true })))
    const record = (await screen.findByText('预检失败批次')).closest('section')
    expect(record).not.toBeNull()
    await userEvent.click(within(record!).getByRole('button', { name: '归档' }))
    expect(await screen.findByRole('dialog', { name: '归档或删除上传记录' })).toBeInTheDocument()
    await waitFor(() => expect(apiMock.getUploadSessionDeleteImpact).toHaveBeenCalledWith(['precheck-failed-session']))
    expect(await screen.findByText('删除影响预检失败，暂时不能彻底删除。')).toBeInTheDocument()
    await userEvent.type(screen.getByPlaceholderText('预检失败批次'), '预检失败批次')
    expect(screen.getByRole('button', { name: '彻底删除' })).toBeDisabled()
  })

  it('offers one-click continuation when a paused upload has a saved directory handle', async () => {
    directoryAccessMock.getDirectoryHandle.mockResolvedValue({
      kind: 'directory',
      name: '原任务目录',
      values: async function* () {},
      queryPermission: vi.fn().mockResolvedValue('granted'),
      requestPermission: vi.fn().mockResolvedValue('granted'),
    })
    window.localStorage.setItem('ruyi-library-import-open', '1')
    window.localStorage.setItem('ruyi-library-import-jobs', JSON.stringify([{
      id: 'import-with-handle',
      title: '已授权批次',
      status: 'INTERRUPTED',
      total: 1,
      uploaded: 0,
      skipped: 0,
      progress: null,
      operations: [],
      items: [{ id: 'item-1', code: 'LOOK-HANDLE', imageCount: 3, uploadedCount: 1, status: 'INTERRUPTED', sessionId: 'session-handle' }],
    }]))
    window.history.replaceState(null, '', '/library')

    render(<App />)

    expect(await screen.findByRole('button', { name: '继续上传' })).toBeInTheDocument()
    expect(screen.getByText('重新选择原目录')).toBeInTheDocument()
  })

  it('does not offer one-click continuation when the saved directory permission is denied', async () => {
    directoryAccessMock.getDirectoryHandle.mockResolvedValue({
      kind: 'directory',
      name: '已拒绝目录',
      values: async function* () {},
      queryPermission: vi.fn().mockResolvedValue('denied'),
      requestPermission: vi.fn().mockResolvedValue('denied'),
    })
    window.localStorage.setItem('ruyi-library-import-open', '1')
    window.localStorage.setItem('ruyi-library-import-jobs', JSON.stringify([{
      id: 'import-denied-handle',
      title: '拒绝授权批次',
      status: 'INTERRUPTED',
      total: 1,
      uploaded: 0,
      skipped: 0,
      progress: null,
      operations: [],
      items: [{ id: 'item-1', code: 'LOOK-DENIED', imageCount: 3, uploadedCount: 1, status: 'INTERRUPTED', sessionId: 'session-denied' }],
    }]))
    window.history.replaceState(null, '', '/library')

    render(<App />)

    expect(await screen.findByText('重新选择原目录')).toBeInTheDocument()
    await waitFor(() => expect(directoryAccessMock.getDirectoryHandle).toHaveBeenCalledWith('import-denied-handle'))
    expect(screen.queryByRole('button', { name: '继续上传' })).not.toBeInTheDocument()
  })

  it('does not recheck paused directory handles for unrelated upload progress', async () => {
    apiMock.createStylingImportSessions.mockImplementation(() => new Promise(() => undefined))
    window.localStorage.setItem('ruyi-library-import-open', '1')
    window.localStorage.setItem('ruyi-library-import-jobs', JSON.stringify([{
      id: 'stable-paused-job',
      title: '稳定暂停批次',
      status: 'INTERRUPTED',
      total: 1,
      uploaded: 0,
      skipped: 0,
      progress: null,
      operations: [],
      items: [{ id: 'paused-item', code: 'LOOK-PAUSED', imageCount: 3, uploadedCount: 1, status: 'INTERRUPTED', sessionId: 'paused-session' }],
    }]))
    window.history.replaceState(null, '', '/library')
    render(<App />)
    await waitFor(() => expect(directoryAccessMock.getDirectoryHandle).toHaveBeenCalledTimes(1))

    const imagePicker = screen.getByText('选择图片').querySelector('input[type="file"]') as HTMLInputElement
    await userEvent.upload(imagePicker, new File(['new'], 'LOOK-NEW.jpg', { type: 'image/jpeg' }))
    await userEvent.click(await screen.findByRole('button', { name: '上传' }))
    await waitFor(() => expect(apiMock.createStylingImportSessions).toHaveBeenCalledTimes(1))

    expect(directoryAccessMock.getDirectoryHandle).toHaveBeenCalledTimes(1)
  })

  it('keeps a newly opened tab passive until the user explicitly resumes an upload', async () => {
    const session = {
      id: 'passive-session',
      scope: 'styling-garment',
      status: 'UPLOADING',
      businessCode: 'LOOK-PASSIVE',
      sourceTitle: '被动查看批次',
      batchId: 'passive-job',
      totalCount: 2,
      uploadedCount: 1,
      items: [],
    }
    directoryAccessMock.getDirectoryHandle.mockResolvedValue({
      kind: 'directory',
      name: '原任务目录',
      values: async function* () {},
      queryPermission: vi.fn().mockResolvedValue('granted'),
      requestPermission: vi.fn().mockResolvedValue('granted'),
    })
    apiMock.resumeUploadSession.mockResolvedValue({ session, items: [] })
    apiMock.listUploadSessions.mockImplementation(async (options: { scope?: string }) => ({
      items: options.scope === 'styling_garment' ? [session] : [],
    }))
    window.localStorage.setItem('ruyi-library-import-open', '1')
    window.localStorage.setItem('ruyi-library-import-jobs', JSON.stringify([{
      id: 'passive-job',
      title: '被动查看批次',
      status: 'RUNNING',
      total: 1,
      uploaded: 0,
      skipped: 0,
      progress: null,
      operations: [],
      items: [{ id: 'passive-item', code: 'LOOK-PASSIVE', imageCount: 2, uploadedCount: 1, status: 'UPLOADING', sessionId: session.id }],
    }]))
    vi.mocked(window.localStorage.setItem).mockClear()
    window.history.replaceState(null, '', '/library')

    render(<StrictMode><App /></StrictMode>)

    expect(await screen.findByRole('button', { name: '继续上传' })).toBeInTheDocument()
    await new Promise((resolve) => window.setTimeout(resolve, 0))
    expect(apiMock.resumeUploadSession).not.toHaveBeenCalled()
    expect(window.localStorage.setItem).not.toHaveBeenCalledWith('ruyi-library-import-jobs', expect.any(String))

    await userEvent.click(screen.getByRole('button', { name: '继续上传' }))
    await waitFor(() => expect(apiMock.resumeUploadSession).toHaveBeenCalled())
    await waitFor(() => expect(window.localStorage.setItem).toHaveBeenCalledWith('ruyi-library-import-jobs', expect.any(String)))
  })

  it('does not persist batches discovered by passive server recovery', async () => {
    apiMock.listUploadSessions.mockImplementation(async (options: { scope?: string }) => ({
      items: options.scope === 'styling_garment' ? [{
        id: 'server-only-session',
        scope: 'styling-garment',
        status: 'UPLOADING',
        businessCode: 'LOOK-SERVER-ONLY',
        sourceTitle: '服务端恢复批次',
        batchId: 'server-only-batch',
        totalCount: 2,
        uploadedCount: 1,
        items: [],
        createdAt: '2026-07-28T08:00:00Z',
        updatedAt: '2026-07-28T08:01:00Z',
      }] : [],
    }))
    window.localStorage.setItem('ruyi-library-import-open', '1')
    vi.mocked(window.localStorage.setItem).mockClear()
    window.history.replaceState(null, '', '/library')

    render(<StrictMode><App /></StrictMode>)

    await waitFor(() => expect(directoryAccessMock.getDirectoryHandle).toHaveBeenCalledWith('server-only-batch'))
    expect(window.localStorage.setItem).not.toHaveBeenCalledWith('ruyi-library-import-jobs', expect.any(String))
  })

  it('downloads garment images from each card and from bulk selection', async () => {
    apiMock.listStylingGarments.mockResolvedValue({
      ...emptyPage,
      items: [garment, { ...garment, id: 2, code: 'TAGGING-LOOK', status: 'TAGGING_RUNNING' }],
      total: 2,
      facets: {},
      stableFacets: {},
    })
    const clickedUrls: string[] = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      clickedUrls.push(this.getAttribute('href') ?? '')
    })

    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: '搭配服装库' }))

    const firstCard = (await screen.findByText('CODX-UI-0718-B-8')).closest('.garment-card') as HTMLElement
    const actions = within(firstCard).getAllByRole('button').map((button) => button.textContent?.trim())
    expect(actions).toEqual(['详情', '归档', '下载', '重新识别'])
    await userEvent.click(within(firstCard).getByRole('button', { name: '下载' }))
    expect(clickedUrls).toEqual(['/garment-archive?ids=1'])

    await userEvent.click(screen.getByRole('button', { name: '批量管理' }))
    const checkboxes = screen.getAllByRole('checkbox', { name: '选择' })
    expect(checkboxes[1]).toBeEnabled()
    await userEvent.click(checkboxes[0])
    await userEvent.keyboard('{Shift>}')
    await userEvent.click(checkboxes[1])
    await userEvent.keyboard('{/Shift}')
    await userEvent.click(screen.getByRole('button', { name: '下载 2 项' }))
    expect(clickedUrls).toEqual(['/garment-archive?ids=1', '/garment-archive?ids=1,2'])
  })

  it('selects multiple garment cards with a drag marquee', async () => {
    apiMock.listStylingGarments.mockResolvedValue({
      ...emptyPage,
      items: [garment, { ...garment, id: 2, code: 'MARQUEE-LOOK-2' }],
      total: 2,
      facets: {},
      stableFacets: {},
    })
    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: '搭配服装库' }))
    expect(await screen.findByText('MARQUEE-LOOK-2')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '批量管理' }))
    const surface = document.querySelector('.library-results.marquee-selection-surface') as HTMLElement
    const cards = Array.from(surface.querySelectorAll<HTMLElement>('.garment-card'))
    surface.getBoundingClientRect = () => ({ left: 0, top: 0, right: 500, bottom: 500, width: 500, height: 500, x: 0, y: 0, toJSON: () => ({}) })
    cards[0].getBoundingClientRect = () => ({ left: 10, top: 10, right: 100, bottom: 120, width: 90, height: 110, x: 10, y: 10, toJSON: () => ({}) })
    cards[1].getBoundingClientRect = () => ({ left: 110, top: 10, right: 200, bottom: 120, width: 90, height: 110, x: 110, y: 10, toJSON: () => ({}) })

    fireEvent.pointerDown(surface, { button: 0, pointerType: 'mouse', clientX: 5, clientY: 5 })
    fireEvent.pointerMove(window, { pointerType: 'mouse', clientX: 205, clientY: 125 })
    fireEvent.pointerUp(window, { pointerType: 'mouse', clientX: 205, clientY: 125 })

    expect(await screen.findByRole('button', { name: '下载 2 项' })).toBeEnabled()
  })

  it('keeps all four failed-garment actions in a two-column grid', async () => {
    apiMock.listStylingGarments.mockResolvedValue({
      ...emptyPage,
      items: [{ ...garment, status: 'TAGGING_FAILED', tagError: '识别失败' }],
      total: 1,
      facets: {},
      stableFacets: {},
    })

    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: '搭配服装库' }))

    const card = (await screen.findByText('CODX-UI-0718-B-8')).closest('.garment-card') as HTMLElement
    const actions = card.querySelector('.card-actions') as HTMLElement
    expect(actions).toHaveAttribute('data-action-count', '4')
    expect(within(actions).getAllByRole('button').map((button) => button.textContent?.trim()))
      .toEqual(['详情', '归档', '下载', '识别标签'])
  })

  it('reassures users that a running garment recognition recovers in the background', async () => {
    apiMock.listStylingGarments.mockResolvedValue({
      ...emptyPage,
      items: [{ ...garment, id: 2, code: 'TAGGING-LOOK', status: 'TAGGING_RUNNING' }],
      total: 1,
      facets: {},
      stableFacets: {},
    })

    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: '搭配服装库' }))

    expect(await screen.findByText('正在识别服装信息；服务繁忙时系统会自动恢复，可以离开当前页面。')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('识别中')
  })

  it('only claims automatic recovery for a rate limit failure that exhausted retries', async () => {
    apiMock.listStylingGarments.mockResolvedValue({
      ...emptyPage,
      items: [
        {
          ...garment,
          id: 2,
          code: 'RATE-LIMITED-LOOK',
          status: 'TAGGING_FAILED',
          taggingErrorCode: 'RATE_LIMITED',
        },
        {
          ...garment,
          id: 3,
          code: 'GENERIC-FAILED-LOOK',
          status: 'TAGGING_FAILED',
          taggingErrorCode: 'MALFORMED_OUTPUT',
        },
        {
          ...garment,
          id: 4,
          code: 'UPLOAD-REJECTED-LOOK',
          status: 'TAGGING_FAILED',
          taggingErrorCode: 'UPLOAD_REQUIREMENT_NOT_MET',
        },
      ],
      total: 3,
      facets: {},
      stableFacets: {},
    })

    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: '搭配服装库' }))

    const rateLimitedCard = (await screen.findByText('RATE-LIMITED-LOOK')).closest('.garment-card') as HTMLElement
    const genericFailureCard = screen.getByText('GENERIC-FAILED-LOOK').closest('.garment-card') as HTMLElement
    const uploadRejectedCard = screen.getByText('UPLOAD-REJECTED-LOOK').closest('.garment-card') as HTMLElement
    expect(within(rateLimitedCard).getByText('识别失败')).toHaveAttribute('title', expect.stringContaining('系统已在安全范围内多次自动尝试'))
    expect(within(genericFailureCard).getByText('识别失败')).toHaveAttribute('title', expect.not.stringContaining('自动尝试'))
    expect(within(uploadRejectedCard).getByText('未通过上传要求')).toHaveAttribute('title', expect.stringContaining('未识别到符合上传要求的服装图'))
  })

  it('does not silently skip selected garments without images during bulk download', async () => {
    apiMock.listStylingGarments.mockResolvedValue({
      ...emptyPage,
      items: [garment, { ...garment, id: 2, code: 'EMPTY-LOOK', imageCount: 0 }],
      total: 2,
      facets: {},
      stableFacets: {},
    })
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => undefined)
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)

    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: '搭配服装库' }))
    expect(await screen.findByText('EMPTY-LOOK')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '批量管理' }))
    for (const checkbox of screen.getAllByRole('checkbox', { name: '选择' })) await userEvent.click(checkbox)
    await userEvent.click(screen.getByRole('button', { name: '下载 2 项' }))

    expect(alertSpy).toHaveBeenCalledWith('以下货号没有可下载的图片：EMPTY-LOOK')
    expect(clickSpy).not.toHaveBeenCalled()
  })

  it('uses stable 24, 48, and 96 item page sizes in the garment library', async () => {
    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: '搭配服装库' }))

    await waitFor(() => expect(apiMock.listStylingGarments).toHaveBeenCalledWith(expect.objectContaining({ limit: 24 })))
    const pageSizeSelect = screen.getByRole('combobox', { name: '每页' })
    expect(Array.from((pageSizeSelect as HTMLSelectElement).options).map((option) => option.textContent)).toEqual(['24 套', '48 套', '96 套'])

    await userEvent.selectOptions(pageSizeSelect, '48')
    await waitFor(() => expect(apiMock.listStylingGarments).toHaveBeenLastCalledWith(expect.objectContaining({ limit: 48, offset: 0 })))
  })

  it('uses stable 20, 50, and 100 row page sizes in generation records', async () => {
    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: '生成记录' }))

    await waitFor(() => expect(apiMock.listRecordWorkspace).toHaveBeenCalledWith(expect.objectContaining({ limit: 20 })))
    const pageSizeSelect = screen.getByRole('combobox', { name: '每页' })
    expect(Array.from((pageSizeSelect as HTMLSelectElement).options).map((option) => option.textContent)).toEqual(['20 行', '50 行', '100 行'])

    await userEvent.selectOptions(pageSizeSelect, '50')
    await waitFor(() => expect(apiMock.listRecordWorkspace).toHaveBeenLastCalledWith(expect.objectContaining({ limit: 50, offset: 0 })))
  })

  it('keeps purge checked when delete impact refreshes the archive dialog', async () => {
    let resolveImpact: (value: unknown) => void = () => undefined
    apiMock.listStylingGarments.mockResolvedValue({
      ...emptyPage,
      items: [garment],
      total: 1,
      facets: {},
      stableFacets: {},
    })
    apiMock.getStylingGarmentDeleteImpact.mockReturnValue(new Promise((resolve) => {
      resolveImpact = resolve
    }))

    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: '搭配服装库' }))
    expect(await screen.findByText('CODX-UI-0718-B-8')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /^归档$/ }))

    const purgeCheckbox = await screen.findByRole('checkbox', { name: /同时彻底删除关联记录和文件/ })
    await userEvent.click(purgeCheckbox)
    expect(purgeCheckbox).toBeChecked()

    resolveImpact({
      impact: {
        garmentCount: 1,
        garmentImageCount: 1,
        dependentTaskCount: 0,
        taskCount: 0,
        resultImageCount: 0,
        mediaAssetCount: 1,
        estimatedBytes: 1024,
      },
    })

    await waitFor(() => expect(screen.getByText(/彻底删除将移除/)).toBeInTheDocument())
    expect(screen.getByRole('checkbox', { name: /同时彻底删除关联记录和文件/ })).toBeChecked()
    expect(screen.getByPlaceholderText('CODX-UI-0718-B-8')).toBeInTheDocument()
  })

  it('keeps purge checked when record delete impact refreshes the archive dialog', async () => {
    const recordTask = { ...task, id: 21, name: 'LOOK-021', status: 'COMPLETED' }
    let resolveImpact: (value: unknown) => void = () => undefined
    apiMock.listRecordWorkspace.mockResolvedValue({
      ...emptyPage,
      items: [{
        kind: 'task',
        id: 'task-21',
        status: recordTask.status,
        name: recordTask.name,
        createdAt: recordTask.createdAt,
        updatedAt: recordTask.updatedAt,
        task: recordTask,
      }],
      total: 1,
      facets: {},
      stableFacets: {},
    })
    apiMock.getTaskDeleteImpact.mockReturnValue(new Promise((resolve) => {
      resolveImpact = resolve
    }))

    render(<App />)
    await userEvent.click(await screen.findByRole('button', { name: '生成记录' }))
    expect(await screen.findByText('LOOK-021')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /^归档$/ }))

    const purgeCheckbox = await screen.findByRole('checkbox', { name: /同时彻底删除关联记录和文件/ })
    await userEvent.click(purgeCheckbox)
    expect(purgeCheckbox).toBeChecked()

    resolveImpact({
      impact: {
        garmentCount: 0,
        garmentImageCount: 0,
        dependentTaskCount: 0,
        taskCount: 1,
        resultImageCount: 5,
        mediaAssetCount: 6,
        estimatedBytes: 2048,
      },
    })

    await waitFor(() => expect(screen.getByText(/彻底删除将移除/)).toBeInTheDocument())
    expect(screen.getByRole('checkbox', { name: /同时彻底删除关联记录和文件/ })).toBeChecked()
    expect(screen.getByPlaceholderText('LOOK-021')).toBeInTheDocument()
  })
})
