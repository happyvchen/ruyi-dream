/* @vitest-environment jsdom */
import '@testing-library/jest-dom/vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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
