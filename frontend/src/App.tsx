import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { CSSProperties, Dispatch, DragEvent, FormEvent, KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent, PointerEvent as ReactPointerEvent, ReactNode, SetStateAction, UIEvent } from 'react'
import {
  Bot,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleStop,
  Clock3,
  Download,
  Eye,
  FileImage,
  FileText,
  FolderOpen,
  GalleryVerticalEnd,
  History,
  ImagePlus,
  Images,
  Library,
  Layers3,
  Loader2,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Shield,
  Sparkles,
  Trash2,
  Upload,
  User,
  UserRound,
  X,
  WandSparkles,
  ZoomIn,
  ZoomOut,
} from 'lucide-react'
import { ApiError, api, finalImageArchiveUrl, isAuthExpiredError, mediaDownloadUrl, mediaUrl, stylingGarmentArchiveUrl } from './api'
import type { AICallRecord, AIOperation, AttemptPhase, CategoryTreeOption, DeleteImpact, DeleteOperationResult, DirectUploadItem, FacetOption, FinalImageExportPrecheck, GenerationRecordBatch, GenerationRecordFacets, GenerationWorkspaceRecord, GenerationTask, MediaAsset, ModelProfile, ObjectCleanupError, PromptConfig, PromptInvocationOption, PromptPlaceholder, SessionRole, StylingGarment, StylingGarmentFacets, StylingGarmentListResponse, UploadProgressSnapshot, UploadSession } from './api'
import { formalAssetFiles, groupImportFiles, importPath, isSupportedImageFile, sanitizeClientName, SUPPORTED_IMAGE_ACCEPT, timestampName } from './lib/importGrouping'
import type { ImportBatch, ImportIssue } from './lib/importGrouping'
import { chooseDirectory, filesFromDirectory, getDirectoryHandle, storeDirectoryHandle, supportsDirectoryAccess } from './lib/directoryAccess'
import type { RuyiFileSystemDirectoryHandle } from './lib/directoryAccess'
import { friendlyErrorText } from './lib/errorMessages'
import { CHECKSUM_COMPATIBILITY_NOTICE } from './lib/fileChecksum'
import { parseRoute, routeToUrl } from './routes'
import type { Page } from './routes'

type StudioStep = 0 | 1 | 2 | 3
type FileSystemEntry = {
  name: string
  isFile: boolean
  isDirectory: boolean
}
type FileSystemFileEntry = FileSystemEntry & {
  file: (success: (file: File) => void, error?: () => void) => void
}
type FileSystemDirectoryEntry = FileSystemEntry & {
  createReader: () => { readEntries: (success: (entries: FileSystemEntry[]) => void, error?: () => void) => void }
}
type LightboxImage = {
  src: string
  thumbSrc?: string
  title: string
  meta?: string
  downloadUrl?: string
  details?: Record<string, ReactNode>
  items?: LightboxImageItem[]
  initialIndex?: number
}

type LightboxImageItem = Omit<LightboxImage, 'items' | 'initialIndex'>
type DeleteDialogTarget = Pick<GenerationTask, 'id' | 'name'> & { batchId?: string | null }
type RunOptions = { refresh?: boolean | 'background' }
type PageSizeOption = number | { value: number; label: string; key?: string }

const steps = [
  { title: '上传主推款', icon: Upload },
  { title: 'AI 智能选搭', icon: WandSparkles },
  { title: '生成分镜预览', icon: GalleryVerticalEnd },
  { title: '生成成片', icon: Sparkles },
] as const
const adminPages = new Set<Page>(['prompt-maintenance'])

const defaultPageSizeOptions = [20, 50, 100] as const
const libraryPageSizeOptions = [24, 48, 96] as const
const recentTaskRowHeight = 62
const recentTaskMaxPageSize = 60
const authSeenStorageKey = 'ruyi-auth-seen'
const sidebarCollapsedStorageKey = 'ruyi-sidebar-collapsed'
const libraryImportOpenStorageKey = 'ruyi-library-import-open'
const libraryImportJobsStorageKey = 'ruyi-library-import-jobs'
const sidebarCompactQuery = '(max-width: 900px)'
const authExpiredMessage = '登录状态已过期，请重新登录后继续。'
const STYLING_GARMENT_MAX_IMAGES = 15
const HERO_PRODUCT_MAX_IMAGES = 15
const STYLING_GARMENT_SELECTED_IMAGES = 6
const HERO_PRODUCT_SELECTED_IMAGES = 8
const PRODUCT_CODE_MAX_CHARS = 20
const BATCH_NAME_MAX_CHARS = 80
const STYLING_GARMENT_EXPORT_MAX_ITEMS = 50
const BULK_SEARCH_THRESHOLD = 5
const IMPORT_SESSION_GROUP_WINDOW_MS = 10 * 60 * 1000
const UPLOAD_SESSION_ARCHIVE_BATCH_SIZE = 500
const uploadSessionScopeRefreshes = [
  { scope: 'generation_task', limit: 20 },
  { scope: 'styling_garment', limit: 100 },
] as const
const TAGGABLE_GARMENT_STATUSES = new Set(['DRAFT', 'TAGGING_FAILED', 'TAGGING_READY'])
const RETAGGABLE_GARMENT_STATUSES = new Set(['READY'])
const ANY_TAGGABLE_GARMENT_STATUSES = new Set([...TAGGABLE_GARMENT_STATUSES, ...RETAGGABLE_GARMENT_STATUSES])

type SelectableId = string | number
type MarqueeBox = { left: number; top: number; width: number; height: number } | null

function useMarqueeSelection<T extends SelectableId>({ enabled, selected, onChange }: {
  enabled: boolean
  selected: ReadonlySet<T>
  onChange: (ids: Set<T>) => void
}) {
  const containerRef = useRef<HTMLDivElement | null>(null)
  const [box, setBox] = useState<MarqueeBox>(null)
  const selectedRef = useRef(selected)
  const suppressClickRef = useRef(false)
  const cleanupRef = useRef<(() => void) | null>(null)

  useEffect(() => {
    selectedRef.current = selected
  }, [selected])

  useEffect(() => () => cleanupRef.current?.(), [])

  function onPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (!enabled || event.button !== 0 || event.pointerType === 'touch') return
    const target = event.target as HTMLElement
    if (target.closest('a, input, label, button:not(.record-open), [data-no-marquee]')) return
    const container = containerRef.current
    if (!container) return
    const start = { x: event.clientX, y: event.clientY }
    const additive = event.metaKey || event.ctrlKey || event.shiftKey
    const base = additive ? new Set(selectedRef.current) : new Set<T>()
    let dragging = false

    const move = (pointerEvent: PointerEvent) => {
      const distance = Math.hypot(pointerEvent.clientX - start.x, pointerEvent.clientY - start.y)
      if (!dragging && distance < 5) return
      dragging = true
      suppressClickRef.current = true
      document.body.classList.add('is-marquee-selecting')
      const bounds = container.getBoundingClientRect()
      const left = Math.min(start.x, pointerEvent.clientX)
      const top = Math.min(start.y, pointerEvent.clientY)
      const right = Math.max(start.x, pointerEvent.clientX)
      const bottom = Math.max(start.y, pointerEvent.clientY)
      setBox({
        left: left - bounds.left + container.scrollLeft,
        top: top - bounds.top + container.scrollTop,
        width: right - left,
        height: bottom - top,
      })
      const next = new Set(base)
      container.querySelectorAll<HTMLElement>('[data-select-id]').forEach((item) => {
        if (item.dataset.selectDisabled === 'true') return
        const rect = item.getBoundingClientRect()
        const intersects = rect.left < right && rect.right > left && rect.top < bottom && rect.bottom > top
        const rawId = item.dataset.selectId
        if (!intersects || rawId === undefined) return
        const id = (item.dataset.selectType === 'number' ? Number(rawId) : rawId) as T
        next.add(id)
      })
      onChange(next)
    }
    const finish = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', finish)
      window.removeEventListener('pointercancel', finish)
      document.body.classList.remove('is-marquee-selecting')
      setBox(null)
      cleanupRef.current = null
      if (dragging) window.setTimeout(() => { suppressClickRef.current = false }, 0)
    }
    cleanupRef.current?.()
    cleanupRef.current = finish
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', finish)
    window.addEventListener('pointercancel', finish)
  }

  function onClickCapture(event: ReactMouseEvent<HTMLDivElement>) {
    if (!suppressClickRef.current) return
    event.preventDefault()
    event.stopPropagation()
  }

  return { containerRef, box, onPointerDown, onClickCapture }
}

function MarqueeSelectionBox({ box }: { box: MarqueeBox }) {
  if (!box) return null
  return <span className="marquee-selection-box" style={box} aria-hidden="true" />
}

function splitSearchTerms(value: string) {
  const seen = new Set<string>()
  return value
    .split(/[\s,，.。、]+/)
    .map((part) => part.trim())
    .filter((part) => {
      if (!part) return false
      const key = part.toLocaleLowerCase()
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
}

function defaultSidebarCollapsed() {
  return typeof window !== 'undefined' && window.matchMedia(sidebarCompactQuery).matches
}

function storedSidebarCollapsed() {
  if (typeof window === 'undefined') return null
  let value: string | null = null
  try {
    value = window.localStorage.getItem(sidebarCollapsedStorageKey)
  } catch {
    return null
  }
  if (value === 'collapsed') return true
  if (value === 'expanded') return false
  return null
}

function storeSidebarCollapsed(value: boolean) {
  try {
    window.localStorage.setItem(sidebarCollapsedStorageKey, value ? 'collapsed' : 'expanded')
  } catch {
    // Preference persistence is nice to have; keep the UI usable if storage is unavailable.
  }
}

function formatImageCount(count: number, limit: number) {
  return `${count}/${limit} 张`
}

export function App() {
  const [checked, setChecked] = useState(false)
  const [authenticated, setAuthenticated] = useState(false)
  const [role, setRole] = useState<SessionRole | null>(null)
  const [page, setPage] = useState<Page>(() => parseRoute().page)
  const [collapsed, setCollapsed] = useState(() => storedSidebarCollapsed() ?? defaultSidebarCollapsed())
  const [sidebarPreferenceSet, setSidebarPreferenceSet] = useState(() => storedSidebarCollapsed() !== null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [loginNotice, setLoginNotice] = useState('')
  const [task, setTask] = useState<GenerationTask | null>(null)
  const [tasks, setTasks] = useState<GenerationTask[]>([])
  const [records, setRecords] = useState<GenerationTask[]>([])
  const [garments, setGarments] = useState<StylingGarment[]>([])
  const [models, setModels] = useState<ModelProfile[]>([])
  const [recordDetail, setRecordDetail] = useState<GenerationTask | null>(null)
  const [localOperations, setLocalOperations] = useState<AIOperation[]>([])
  const [uploadSessions, setUploadSessions] = useState<UploadSession[]>([])
  const [browserUploadingCount, setBrowserUploadingCount] = useState(0)
  const suppressStudioAutoRestoreRef = useRef(false)
  const [libraryImportOpen, setLibraryImportOpen] = useState(() => {
    if (typeof window === 'undefined') return false
    return window.localStorage.getItem(libraryImportOpenStorageKey) === '1'
  })
  const [libraryImportJobs, setLibraryImportJobs] = useState<ImportJob[]>(() => storedLibraryImportJobs())
  const persistLibraryImportJobs = useCallback<Dispatch<SetStateAction<ImportJob[]>>>((action) => {
    setLibraryImportJobs((current) => {
      const next = typeof action === 'function' ? action(current) : action
      try {
        window.localStorage.setItem(libraryImportJobsStorageKey, JSON.stringify(next.slice(0, 5)))
      } catch {
        // Keep upload execution usable when local persistence is unavailable.
      }
      return next
    })
  }, [])

  useEffect(() => {
    void api.session()
      .then((session) => {
        setAuthenticated(session.authenticated)
        setRole(session.role ?? null)
        if (session.authenticated) {
          window.localStorage.setItem(authSeenStorageKey, '1')
        } else if (session.authRequired && window.localStorage.getItem(authSeenStorageKey)) {
          setLoginNotice(authExpiredMessage)
        }
        if (session.role !== 'admin' && adminPages.has(parseRoute().page)) {
          setPage('studio')
          window.history.replaceState(null, '', routeToUrl({ page: 'studio' }))
        }
        setChecked(true)
      })
      .catch(() => setChecked(true))
  }, [])

  useEffect(() => {
    const handleAuthExpired = () => {
      markAuthExpired()
    }
    window.addEventListener('ruyi-auth-expired', handleAuthExpired)
    return () => window.removeEventListener('ruyi-auth-expired', handleAuthExpired)
  }, [])

  useEffect(() => {
    if (sidebarPreferenceSet) return
    const media = window.matchMedia(sidebarCompactQuery)
    const syncSidebarDefault = () => setCollapsed(media.matches)
    syncSidebarDefault()
    media.addEventListener('change', syncSidebarDefault)
    return () => media.removeEventListener('change', syncSidebarDefault)
  }, [sidebarPreferenceSet])

  useEffect(() => {
    if (!authenticated) return
    void refreshAll().catch((err) => {
      if (isAuthExpiredError(err)) markAuthExpired()
    })
  }, [authenticated, role])

  useEffect(() => {
    if (!authenticated) return
    void refreshUploadSessionScopes().catch(() => undefined)
  }, [authenticated])

  useEffect(() => {
    try {
      window.localStorage.setItem(libraryImportOpenStorageKey, libraryImportOpen ? '1' : '0')
    } catch {
      // Restoring the import page is a convenience; routing remains usable without storage.
    }
  }, [libraryImportOpen])

  useEffect(() => {
    const pollingSessions = pollingUploadSessions(uploadSessions)
    if (!authenticated || !pollingSessions.length) return
    const timer = window.setInterval(() => {
      void refreshUploadSessionScopes(pollingSessions).catch(() => undefined)
    }, 3000)
    return () => window.clearInterval(timer)
  }, [authenticated, uploadSessions])

  useEffect(() => {
    if (browserUploadingCount <= 0) return
    const warnUploading = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = '图片正在上传；刷新或关闭会暂停任务，之后可从断点继续。'
    }
    window.addEventListener('beforeunload', warnUploading)
    return () => window.removeEventListener('beforeunload', warnUploading)
  }, [browserUploadingCount])

  useEffect(() => {
    if (!authenticated || role === 'admin' || !adminPages.has(page)) return
    setPage('studio')
    window.history.replaceState(null, '', routeToUrl({ page: 'studio' }))
  }, [authenticated, role, page])

  useEffect(() => {
    const handlePopState = () => {
      const route = parseRoute()
      setPage(route.page)
      if (route.page === 'studio' && !route.taskId) setTask(null)
      if (route.page === 'studio' && route.taskId) {
        void api.getTask(route.taskId, role === 'admin').then((result) => setTask(result.item)).catch(() => undefined)
      }
      if (route.page === 'records' && !route.recordId) setRecordDetail(null)
      if (route.page === 'records' && route.recordId) {
        void api.getRecord(route.recordId, false).then((result) => setRecordDetail(result.item)).catch(() => undefined)
      }
    }
    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [role])

  useEffect(() => {
    if (page === 'records' && recordDetail?.id) window.history.replaceState(null, '', routeToUrl({ page, recordId: recordDetail.id }))
  }, [page, recordDetail?.id])

  const runningTaggingIds = useMemo(() => garments.filter((item) => item.status === 'TAGGING_RUNNING').map((item) => item.id), [garments])
  const runningTaggingKey = runningTaggingIds.join(',')
  const hasRunningTagging = runningTaggingIds.length > 0
  useEffect(() => {
    if (!authenticated || !hasRunningTagging) return
    const timer = window.setInterval(() => {
      void api.listStylingGarments({ limit: 80 }).then((response) => setGarments(response.items)).catch(() => undefined)
    }, 3000)
    return () => window.clearInterval(timer)
  }, [authenticated, hasRunningTagging, runningTaggingKey])

  async function refreshAll() {
    const [garmentResult, taskResult, recordResult, modelResult] = await Promise.all([
      api.listStylingGarments({ limit: 80 }),
      api.listTasks({ limit: 30 }),
      api.listRecords({ limit: 30 }),
      api.listModels(),
    ])
    setGarments(garmentResult.items)
    setTasks(taskResult.items)
    setRecords(recordResult.items)
    setModels(modelResult.items)
    const route = parseRoute()
    const requestedTaskId = route.page === 'studio' ? route.taskId : undefined
    if (requestedTaskId && (!task || task.id !== requestedTaskId)) {
      const detail = await api.getTask(requestedTaskId, role === 'admin').then((r) => r.item).catch(() => null)
      if (detail) setTask(detail)
    } else if (route.page === 'studio' && !requestedTaskId && !task && !suppressStudioAutoRestoreRef.current) {
      const latestTask = recordResult.items[0] ?? taskResult.items[0] ?? null
      if (latestTask) {
        setTask(latestTask)
        window.history.replaceState(null, '', routeToUrl({ page: 'studio', taskId: latestTask.id }))
        void api.getTask(latestTask.id, role === 'admin').then((r) => setTask(r.item)).catch(() => undefined)
      }
    }
    if (route.page === 'records' && route.recordId && !recordDetail) {
      const detail = await api.getRecord(route.recordId, false).then((r) => r.item).catch(() => null)
      if (detail) setRecordDetail(detail)
    }
  }

  async function refreshUploadSessions(scope: string, limit = 20) {
    const result = await api.listUploadSessions({ scope, limit })
    setUploadSessions((current) => mergeUploadSessions(current, result.items, scope))
    return result.items
  }

  async function refreshUploadSessionScopes(items?: UploadSession[]) {
    await Promise.all(uploadSessionRefreshTargets(items).map(({ scope, limit }) => refreshUploadSessions(scope, limit)))
  }

  function navigate(next: Page) {
    if (next !== page && document.body.dataset.promptDirty === 'true' && !window.confirm('Prompt 有未保存修改，确定离开并放弃这些修改吗？')) return
    suppressStudioAutoRestoreRef.current = false
    setPage(next)
    if (next === 'studio') {
      const latestTask = records[0] ?? tasks[0] ?? null
      setTask(latestTask)
      window.history.pushState(null, '', routeToUrl({ page: 'studio', taskId: latestTask?.id }))
      if (latestTask) void api.getTask(latestTask.id, role === 'admin').then((result) => setTask(result.item)).catch(() => undefined)
      return
    }
    if (next === 'records') setRecordDetail(null)
    window.history.pushState(null, '', routeToUrl({ page: next }))
  }

  function markBrowserUploading(delta: 1 | -1) {
    setBrowserUploadingCount((value) => Math.max(0, value + delta))
  }

  function openTaskInStudio(item: GenerationTask) {
    setPage('studio')
    setTask(item)
    window.history.pushState(null, '', routeToUrl({ page: 'studio', taskId: item.id }))
    void api.getTask(item.id, role === 'admin').then((result) => setTask(result.item)).catch(() => undefined)
  }

  function toggleSidebarCollapsed() {
    setCollapsed((value) => {
      const next = !value
      storeSidebarCollapsed(next)
      return next
    })
    setSidebarPreferenceSet(true)
  }

  async function run<T>(label: string, action: () => Promise<T>, after?: (value: T) => void, options: RunOptions = {}) {
    setBusy(label)
    setError('')
    try {
      const value = await action()
      after?.(value)
      if (options.refresh === 'background') void refreshAll().catch(() => undefined)
      else if (options.refresh !== false) await refreshAll().catch(() => undefined)
      return value
    } catch (err) {
      setLocalOperations((items) => items.filter((item) => !isAttemptRunning(item)))
      if (isAuthExpiredError(err)) {
        markAuthExpired()
        return undefined
      }
      if (options.refresh === 'background') void refreshAll().catch(() => undefined)
      else if (options.refresh !== false) await refreshAll().catch(() => undefined)
      setError(err instanceof ApiError ? friendlyErrorText(err.errorCode, err.errorMessage || err.message) : err instanceof Error ? friendlyErrorText(undefined, err.message) : '操作失败')
      throw err
    } finally {
      setBusy('')
    }
  }

  function noteDeleteOperationResult(result: DeleteOperationResult) {
    const cleanupErrors = result.objectCleanupErrors ?? []
    if (!cleanupErrors.length) return
    setError(`记录已删除，但有 ${cleanupErrors.length} 处对象存储文件未清理完成。请稍后重试清理或交由管理员处理。`)
  }

  function markAuthExpired() {
    window.localStorage.setItem(authSeenStorageKey, '1')
    setLoginNotice(authExpiredMessage)
    setAuthenticated(false)
    setRole(null)
    setBusy('')
    setError('')
  }

  function startLocalOperation(operation: Omit<AIOperation, 'id' | 'status' | 'startedAt'> & { status?: string; startedAt?: string | null }) {
    const next: AIOperation = {
      ...operation,
      id: -Date.now() - localOperations.length,
      status: operation.status ?? 'RUNNING',
      startedAt: operation.startedAt ?? new Date().toISOString(),
    }
    setLocalOperations((items) => [next, ...items.filter((item) => !(item.ownerType === next.ownerType && item.ownerId === next.ownerId))].slice(0, 20))
    return next
  }

  function clearLocalOperationsFor(ownerTypes: string[], taskId?: number | null) {
    setLocalOperations((items) => items.filter((item) => {
      if (!ownerTypes.includes(item.ownerType)) return true
      return taskId != null && item.taskId !== taskId
    }))
  }

  if (!checked) return <BootScreen />
  if (!authenticated) return <LoginPage notice={loginNotice} onAuthed={(session) => {
    window.localStorage.setItem(authSeenStorageKey, '1')
    setAuthenticated(true)
    setRole(session.role ?? null)
    setLoginNotice('')
  }} />

  const nav = [
    { id: 'studio' as const, label: 'AI 摄影棚', icon: Bot },
    { id: 'library' as const, label: '搭配服装库', icon: Library },
    { id: 'model' as const, label: '模特与背景', icon: UserRound },
    { id: 'records' as const, label: '生成记录', icon: History },
    ...(role === 'admin' ? [
      { id: 'prompt-maintenance' as const, label: 'Prompt 维护', icon: FileText },
    ] : []),
  ]

  return (
    <div className="app-shell">
      <aside className={`sidebar ${collapsed ? 'is-collapsed' : ''}`}>
        <div className="brand-mark">
          <div className="brand-orbit">R</div>
          {!collapsed && <div><strong>Ruyi-Dream</strong><span>AI 数字化摄影棚</span></div>}
        </div>
        <nav className="side-nav">
          {nav.map((item) => {
            const Icon = item.icon
            return (
              <button key={item.id} className={page === item.id ? 'active' : ''} onClick={() => navigate(item.id)} title={item.label}>
                <Icon size={18} />
                {!collapsed && <span>{item.label}</span>}
              </button>
            )
          })}
        </nav>
        <div className="sidebar-bottom">
          <button className="collapse-btn" onClick={toggleSidebarCollapsed} title={collapsed ? '展开侧边栏' : '收起侧边栏'}>
            {collapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
            {!collapsed && <span>{collapsed ? '展开' : '收起'}</span>}
          </button>
          <div className="user-block">
            <div className="avatar"><User size={16} /></div>
            {!collapsed && <div><span>用户</span><strong>{role ?? 'user'}</strong></div>}
            {!collapsed && <button className="ghost-icon" onClick={() => void api.logout().then(() => {
              window.localStorage.removeItem(authSeenStorageKey)
              setLoginNotice('')
              setAuthenticated(false)
              setRole(null)
            })}><LogOut size={16} /></button>}
          </div>
        </div>
      </aside>
      <main className="workspace">
        {error && <div className="toast">{error.length > 300 ? `${error.slice(0, 300)}…` : error}</div>}
        {busy && <BusyPill label={busy} />}
        {page === 'studio' && <StudioPage task={task} setTask={setTask} role={role} run={run} localOperations={localOperations} startLocalOperation={startLocalOperation} clearLocalOperationsFor={clearLocalOperationsFor} uploadSessions={uploadSessions.filter((item) => item.scope === 'generation-task')} refreshUploadSessions={() => refreshUploadSessions('generation_task')} markBrowserUploading={markBrowserUploading} onStartNewTask={() => { suppressStudioAutoRestoreRef.current = true }} />}
        {page === 'library' && <LibraryPage garments={garments} run={run} role={role} onDeleteResult={noteDeleteOperationResult} uploadSessions={uploadSessions.filter((item) => item.scope === 'styling-garment')} refreshUploadSessions={() => refreshUploadSessions('styling_garment', 100)} markBrowserUploading={markBrowserUploading} showImport={libraryImportOpen} setShowImport={setLibraryImportOpen} importJobs={libraryImportJobs} setImportJobs={persistLibraryImportJobs} syncImportJobs={setLibraryImportJobs} />}
        {page === 'model' && <ModelPage models={models} role={role} run={run} />}
        {page === 'prompt-maintenance' && role === 'admin' && <PromptMaintenancePage run={run} />}
        {page === 'records' && <RecordsPage records={records} detail={recordDetail} setDetail={setRecordDetail} openTaskInStudio={openTaskInStudio} run={run} onDeleteResult={noteDeleteOperationResult} />}
      </main>
    </div>
  )
}

function BootScreen() {
  return <div className="login-screen"><div className="login-card"><Loader2 className="spin" /> 启动中...</div></div>
}

function LoginPage({ notice, onAuthed }: { notice?: string; onAuthed: (session: { role?: SessionRole | null }) => void }) {
  const showDemoLogin = import.meta.env.DEV || import.meta.env.VITE_SHOW_DEMO_LOGIN === 'true'
  const [username, setUsername] = useState(showDemoLogin ? 'user' : '')
  const [password, setPassword] = useState(showDemoLogin ? 'password' : '')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const session = await api.login(username, password)
      onAuthed(session)
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-screen">
      <div className="login-ambient" />
      <form className="login-card" onSubmit={submit}>
        <div className="login-logo"><Sparkles size={22} /> Ruyi-Dream</div>
        <h1>AI 数字化摄影棚</h1>
        {notice && <div className="login-notice">{notice}</div>}
        <label>用户名<input autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} /></label>
        <label>密码<input autoComplete="current-password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
        {error && <div className="form-error">{error}</div>}
        <button className="task-action wide" disabled={busy}>{busy ? <Loader2 className="spin" size={16} /> : <Shield size={16} />} 登录</button>
      </form>
    </div>
  )
}

const LONG_AI_OPERATION_PATTERN = /选搭|智能选择|打标|分镜|成片|预览/

function formatElapsedSeconds(totalSeconds: number): string {
  if (totalSeconds < 60) return `${totalSeconds} 秒`
  return `${Math.floor(totalSeconds / 60)} 分 ${totalSeconds % 60} 秒`
}

function BusyPill({ label }: { label: string }) {
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    setElapsed(0)
    const timer = window.setInterval(() => setElapsed((value) => value + 1), 1000)
    return () => window.clearInterval(timer)
  }, [label])
  const isLongAIOperation = LONG_AI_OPERATION_PATTERN.test(label)
  return (
    <div className="busy">
      <Loader2 size={16} className="spin" />
      <span>
        {label}
        {elapsed >= 3 ? `(已等待 ${formatElapsedSeconds(elapsed)})` : ''}
      </span>
      {isLongAIOperation && elapsed >= 5 && <span className="busy-hint">AI 生成通常需要 1-3 分钟,请保持页面打开</span>}
    </div>
  )
}

function pollingUploadSessions(items: UploadSession[]) {
  return items.filter((item) => ['PENDING', 'UPLOADING', 'UPLOADED', 'FINALIZING'].includes(item.status))
}

export function uploadSessionRefreshTargets(items?: UploadSession[]) {
  if (!items) return [...uploadSessionScopeRefreshes]
  const activeScopes = new Set(items.map((item) => item.scope))
  return uploadSessionScopeRefreshes.filter(({ scope }) => activeScopes.has(scope.replaceAll('_', '-')))
}

export function mergeUploadSessions(current: UploadSession[], incoming: UploadSession[], refreshedScope: string) {
  const normalizedScope = refreshedScope.replaceAll('_', '-')
  const retained = current.filter((item) => item.scope !== normalizedScope)
  const byId = new Map<string, UploadSession>()
  retained.forEach((item) => byId.set(item.id, item))
  incoming.forEach((item) => byId.set(item.id, item))
  return Array.from(byId.values()).sort((a, b) => String(b.updatedAt ?? '').localeCompare(String(a.updatedAt ?? '')))
}

function uploadSessionStatusLabel(status: string) {
  if (status === 'PENDING') return '等待上传'
  if (status === 'UPLOADING') return '上传图片中'
  if (status === 'UPLOADED') return '等待保存'
  if (status === 'FINALIZING') return '保存中'
  if (status === 'FINALIZED') return '已保存'
  if (status === 'FAILED') return '失败'
  if (status === 'ABANDONED') return '已放弃'
  return status
}

function StudioPage({ task, setTask, role, run, localOperations, startLocalOperation, clearLocalOperationsFor, uploadSessions, refreshUploadSessions, markBrowserUploading, onStartNewTask }: {
  task: GenerationTask | null
  setTask: (task: GenerationTask | null) => void
  role: SessionRole | null
  run: <T>(label: string, action: () => Promise<T>, after?: (value: T) => void, options?: RunOptions) => Promise<T | undefined>
  localOperations: AIOperation[]
  startLocalOperation: (operation: Omit<AIOperation, 'id' | 'status' | 'startedAt'> & { status?: string; startedAt?: string | null }) => AIOperation
  clearLocalOperationsFor: (ownerTypes: string[], taskId?: number | null) => void
  uploadSessions: UploadSession[]
  refreshUploadSessions: () => Promise<UploadSession[]>
  markBrowserUploading: (delta: 1 | -1) => void
  onStartNewTask: () => void
}) {
  const [step, setStep] = useState<StudioStep>(0)
  const [taskRailTab, setTaskRailTab] = useState<'summary' | 'recent'>('summary')
  const [recentTaskPageSize, setRecentTaskPageSize] = useState(() => preferredRecentTaskPageSize())
  const [recentItems, setRecentItems] = useState<GenerationWorkspaceRecord[]>([])
  const [recentTaskTotal, setRecentTaskTotal] = useState(0)
  const [recentTaskHasMore, setRecentTaskHasMore] = useState(false)
  const [recentTaskNextOffset, setRecentTaskNextOffset] = useState<number | null>(null)
  const [recentTaskLoading, setRecentTaskLoading] = useState(false)
  const [expandedRecentBatchIds, setExpandedRecentBatchIds] = useState<Set<string>>(() => new Set())
  const [recentBatchTasks, setRecentBatchTasks] = useState<Record<string, GenerationTask[]>>({})
  const [recentBatchLoading, setRecentBatchLoading] = useState<Record<string, boolean>>({})
  const recentBatchRequestRef = useRef<Record<string, number>>({})
  const [batchItems, setBatchItems] = useState<GenerationTask[]>([])
  const [batchItemsLoading, setBatchItemsLoading] = useState(false)
  const [batchDownloadPrecheck, setBatchDownloadPrecheck] = useState<FinalImageExportPrecheck | null>(null)
  const [batchDownloadChecking, setBatchDownloadChecking] = useState(false)
  const activeBatchIdRef = useRef<string | null>(null)
  const batchAutoRunRequestRef = useRef(0)
  const batchDownloadRequestRef = useRef(0)
  const technicalDetailTriggerRef = useRef<HTMLElement | null>(null)
  const technicalDetailRequestRef = useRef(0)
  const recentTaskRequestRef = useRef(0)
  const [aiLogDialog, setAiLogDialog] = useState<{ title: string; ownerTypes: string[]; attemptId: number; records: AICallRecord[]; loading: boolean; error?: string } | null>(null)
  const [aiStylingInstruction, setAiStylingInstruction] = useState('')
  const [storyboardInstruction, setStoryboardInstruction] = useState('')
  const [finalImageInstruction, setFinalImageInstruction] = useState('')
  const currentStep = getTaskStep(task)
  const visibleStep = Math.min(Math.max(step, 0), currentStep) as StudioStep
  const summaryInputStep = (visibleStep > 0 ? visibleStep : currentStep) as StudioStep
  const studioView = task ? studioTaskPresentation(task) : null
  const batchContextMatches = !!task?.batchId && activeBatchIdRef.current === task.batchId
  const visibleBatchItems = batchContextMatches ? batchItems : []
  const batchContextLoading = !!task?.batchId && (!batchContextMatches || batchItemsLoading)
  const isAdmin = role === 'admin'
  function applyTaskUpdate(nextTask: GenerationTask) {
    setTask(nextTask)
    setRecentItems((current) => upsertRecentRecordItem(current, nextTask))
    setRecentBatchTasks((current) => nextTask.batchId && current[nextTask.batchId]
      ? { ...current, [nextTask.batchId]: upsertRecentTaskItem(current[nextTask.batchId], nextTask) }
      : current)
  }
  function openRecentTask(item: GenerationTask) {
    setTaskRailTab('summary')
    applyTaskUpdate(item)
    window.history.pushState(null, '', routeToUrl({ page: 'studio', taskId: item.id }))
    void api.getRecord(item.id, isAdmin).then((r) => applyTaskUpdate(r.item)).catch(() => undefined)
  }
  function openBatchTask(item: GenerationTask) {
    setTaskRailTab('summary')
    applyTaskUpdate(item)
    window.history.pushState(null, '', routeToUrl({ page: 'studio', taskId: item.id }))
    void api.getTask(item.id, isAdmin).then((r) => applyTaskUpdate(r.item)).catch(() => undefined)
  }
  function loadRecentTasks(offset = 0, append = false) {
    if (recentTaskLoading) return
    const requestId = recentTaskRequestRef.current + 1
    recentTaskRequestRef.current = requestId
    setRecentTaskLoading(true)
    void api.listRecordWorkspace({ limit: recentTaskPageSize, offset }).then((result) => {
      if (recentTaskRequestRef.current !== requestId) return
      setRecentTaskTotal(result.total)
      setRecentTaskHasMore(result.hasMore)
      setRecentTaskNextOffset(result.nextOffset)
      setRecentItems((current) => {
        if (!append) return result.items
        return mergeRecentRecordItems(current, result.items)
      })
    }).catch(() => undefined).finally(() => {
      if (recentTaskRequestRef.current === requestId) setRecentTaskLoading(false)
    })
  }
  function loadRecentBatchTasks(batchId: string) {
    const requestId = (recentBatchRequestRef.current[batchId] ?? 0) + 1
    recentBatchRequestRef.current = { ...recentBatchRequestRef.current, [batchId]: requestId }
    setRecentBatchLoading((current) => ({ ...current, [batchId]: true }))
    void api.listBatchTasks(batchId)
      .then((result) => {
        if (recentBatchRequestRef.current[batchId] === requestId) {
          setRecentBatchTasks((current) => ({ ...current, [batchId]: result.items }))
        }
      })
      .catch(() => {
        if (recentBatchRequestRef.current[batchId] === requestId) {
          setRecentBatchTasks((current) => ({ ...current, [batchId]: [] }))
        }
      })
      .finally(() => {
        if (recentBatchRequestRef.current[batchId] === requestId) {
          setRecentBatchLoading((current) => ({ ...current, [batchId]: false }))
        }
      })
  }
  function toggleRecentBatch(batch: GenerationRecordBatch) {
    const willExpand = !expandedRecentBatchIds.has(batch.batchId)
    setExpandedRecentBatchIds((current) => {
      const next = new Set(current)
      if (willExpand) next.add(batch.batchId)
      else next.delete(batch.batchId)
      return next
    })
    if (willExpand && !recentBatchTasks[batch.batchId]) loadRecentBatchTasks(batch.batchId)
  }
  function handleRecentTaskScroll(event: UIEvent<HTMLDivElement>) {
    const element = event.currentTarget
    if (!recentTaskHasMore || recentTaskLoading || recentTaskNextOffset == null) return
    if (element.scrollHeight - element.scrollTop - element.clientHeight < 80) {
      loadRecentTasks(recentTaskNextOffset, true)
    }
  }
  function openAICallLogs(title: string, ownerTypes: string[], attemptId: number) {
    if (!task || !isAdmin) return
    const taskId = task.id
    const requestId = technicalDetailRequestRef.current + 1
    technicalDetailRequestRef.current = requestId
    technicalDetailTriggerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    setAiLogDialog({ title, ownerTypes, attemptId, records: [], loading: true })
    void api.getTaskAiInvocation(taskId, attemptId)
      .then((result) => {
        if (technicalDetailRequestRef.current === requestId && task?.id === taskId) setAiLogDialog({ title, ownerTypes, attemptId, records: [result.item], loading: false })
      })
      .catch((error) => {
        if (technicalDetailRequestRef.current === requestId && task?.id === taskId) setAiLogDialog({ title, ownerTypes, attemptId, records: [], loading: false, error: error instanceof Error ? error.message : '调用记录读取失败。' })
      })
  }
  function closeAICallLogs() {
    technicalDetailRequestRef.current += 1
    setAiLogDialog(null)
    window.setTimeout(() => technicalDetailTriggerRef.current?.focus(), 0)
  }
  function startNewTask() {
    setTaskRailTab('summary')
    onStartNewTask()
    setTask(null)
    window.history.pushState(null, '', routeToUrl({ page: 'studio' }))
  }
  function cancelCurrentTask() {
    if (!task || !isTaskRunning(task)) return
    if (!window.confirm('确定取消本次生成吗？已提交给生成服务的图片可能仍会完成并产生费用；系统会停止后续生成，并保留已完成结果。')) return
    void run('取消生成中...', () => api.cancelTask(task.id), (r: { item: GenerationTask }) => {
      clearLocalOperationsFor(['generation_styling', 'generation_preview', 'generation_result_image'], task.id)
      applyTaskUpdate(r.item)
    }, { refresh: 'background' })
  }
  function cancelAutoRunQueue(item: GenerationTask) {
    if (!window.confirm('确定取消排队吗？任务会退出自动成片队列，已完成的步骤和结果保留，之后可随时再次一键成片。')) return
    void run('取消排队中...', () => api.cancelAutoRun(item.id), (r: { item: GenerationTask }) => {
      if (task?.id === item.id) applyTaskUpdate(r.item)
      setRecentItems((current) => upsertRecentRecordItem(current, r.item))
    }, { refresh: 'background' })
  }
  function startOneClickFinal() {
    if (!task) return
    const restart = canRestartOneClickFinal(task)
    if (!restart && !canStartOneClickFinal(task)) return
    const confirmText = restart
      ? '将重新走完整流程：重新智能选搭、分镜预览和成片，产出一套全新方案。已有成片会保留在历史记录里可追溯。确定重新一键成片吗？'
      : '将自动完成后续步骤，并复用已有选搭和分镜预览。生成过程可能产生多次 AI 调用。'
    if (!window.confirm(confirmText)) return
    void run(restart ? '重新一键成片启动中...' : '启动一键成片中...', () => api.autoRunFinal(task.id, restart), (r: { item: GenerationTask }) => {
      applyTaskUpdate(r.item)
    }, { refresh: 'background' })
  }
  function startBatchAutoRun(restart = false) {
    const batchId = task?.batchId
    if (!batchId || activeBatchIdRef.current !== batchId) return
    const eligibleCount = restart
      ? batchItems.filter((item) => !item.archivedAt && item.status === 'COMPLETED').length
      : batchItems.filter((item) => canStartOneClickFinal(item)).length
    if (!eligibleCount) return
    const confirmText = restart
      ? `将为本批次 ${eligibleCount} 个已完成任务重新执行智能选搭、分镜预览和成片；已有成果保留在历史记录中。确定重新生成吗？`
      : `将为 ${eligibleCount} 个任务自动完成后续步骤；已成功成果保持不变，同时只有少量任务并发生成，其余排队等待。确定开始吗？`
    if (!window.confirm(confirmText)) return
    const requestId = batchAutoRunRequestRef.current + 1
    batchAutoRunRequestRef.current = requestId
    void run(restart ? '重新启动批量成片中...' : '启动批量成片中...', () => api.autoRunBatch(batchId, restart), (result: { items: GenerationTask[] }) => {
      if (activeBatchIdRef.current === batchId && batchAutoRunRequestRef.current === requestId) setBatchItems(result.items)
    }, { refresh: 'background' })
  }
  function cancelBatchAutoRun() {
    const batchId = task?.batchId
    if (!batchId || activeBatchIdRef.current !== batchId) return
    const activeCount = batchItems.filter((item) => isTaskRunning(item) || !!item.autoRunTarget).length
    if (!activeCount) return
    if (!window.confirm(`确定取消本批次 ${activeCount} 个生成中或排队中的任务吗？已完成成果会保留，之后可继续一键成片。`)) return
    const requestId = batchAutoRunRequestRef.current + 1
    batchAutoRunRequestRef.current = requestId
    void run('取消批量成片中...', () => api.cancelBatch(batchId), (result: { cancelledTaskIds: number[]; items: GenerationTask[] }) => {
      if (activeBatchIdRef.current !== batchId || batchAutoRunRequestRef.current !== requestId) return
      setBatchItems(result.items)
      result.cancelledTaskIds.forEach((taskId) => clearLocalOperationsFor(['generation_styling', 'generation_preview', 'generation_result_image'], taskId))
    }, { refresh: 'background' })
  }
  function handleBatchPrimaryAction(action: BatchPrimaryAction) {
    if (action === 'cancel') cancelBatchAutoRun()
    else if (action === 'restart') startBatchAutoRun(true)
    else if (action === 'start' || action === 'continue') startBatchAutoRun(false)
  }
  function triggerBatchDownload(ids: number[]) {
    if (!ids.length) return
    const anchor = document.createElement('a')
    anchor.href = finalImageArchiveUrl(ids)
    anchor.rel = 'noreferrer'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    setBatchDownloadPrecheck(null)
  }
  function startBatchDownload() {
    const batchId = task?.batchId
    if (!batchId || activeBatchIdRef.current !== batchId) return
    const downloadableCount = batchItems.filter((item) => ['COMPLETED', 'FINAL_PARTIAL'].includes(item.status)).length
    if (!downloadableCount || batchDownloadChecking) return
    const ids = batchItems.filter((item) => !isTaskRunning(item)).map((item) => item.id)
    const requestId = batchDownloadRequestRef.current + 1
    batchDownloadRequestRef.current = requestId
    setBatchDownloadChecking(true)
    void api.precheckFinalImageExport(ids)
      .then((result) => {
        if (activeBatchIdRef.current !== batchId || batchDownloadRequestRef.current !== requestId) return
        const allComplete = result.downloadableTaskIds.length > 0
          && result.items.every((item) => item.downloadableCount > 0 && !item.issues.length)
        if (allComplete) triggerBatchDownload(result.downloadableTaskIds)
        else setBatchDownloadPrecheck(result)
      })
      .catch(() => {
        if (activeBatchIdRef.current === batchId && batchDownloadRequestRef.current === requestId) setBatchDownloadPrecheck(null)
      })
      .finally(() => {
        if (activeBatchIdRef.current === batchId && batchDownloadRequestRef.current === requestId) setBatchDownloadChecking(false)
      })
  }
  useEffect(() => setStep(currentStep), [task?.id, task?.status])
  useEffect(() => {
    technicalDetailRequestRef.current += 1
    setAiLogDialog(null)
  }, [task?.id])
  useEffect(() => setAiStylingInstruction(task?.stageUserInputs?.aiStyling ?? stageInstructionText(task?.styling?.userInstruction)), [task?.id, task?.stageUserInputs?.aiStyling, task?.styling?.id])
  useEffect(() => setStoryboardInstruction(task?.stageUserInputs?.storyboardPreview ?? stageInstructionText((task?.storyboardPreview ?? task?.preview)?.userInstruction)), [task?.id, task?.stageUserInputs?.storyboardPreview, (task?.storyboardPreview ?? task?.preview)?.id])
  useEffect(() => setFinalImageInstruction(task?.stageUserInputs?.finalImage ?? stageInstructionText(task?.resultSet?.userInstruction)), [task?.id, task?.stageUserInputs?.finalImage, task?.resultSet?.id])
  useEffect(() => {
    loadRecentTasks(0)
  }, [recentTaskPageSize])
  useEffect(() => {
    if (!task || (!['STYLING_RUNNING', 'PREVIEW_RUNNING', 'FINAL_RUNNING'].includes(task.status) && !task.autoRunTarget)) return
    const timer = window.setInterval(() => {
      void api.getTask(task.id, isAdmin).then((result) => applyTaskUpdate(result.item)).catch(() => undefined)
    }, 1500)
    return () => window.clearInterval(timer)
  }, [task?.id, task?.status, task?.autoRunTarget, setTask])
  useEffect(() => {
    if (!task || isTaskLockedForGeneration(task)) return
    clearLocalOperationsFor(['generation_styling', 'generation_preview', 'generation_result_image'], task.id)
  }, [task?.id, task?.status, task?.autoRunTarget, task?.resultSet?.status])
  const hasActiveRecentTasks = recentRecordTasks(recentItems).some((item) => isTaskRunning(item) || isTaskQueuedForAutoRun(item))
  useEffect(() => {
    if (!hasActiveRecentTasks) return
    const timer = window.setInterval(() => {
      void api.listRecordWorkspace({ limit: recentTaskPageSize, offset: 0 }).then((result) => {
        setRecentTaskTotal(result.total)
        setRecentItems((current) => {
          const byId = new Map(result.items.map((item) => [item.id, item] as const))
          const known = new Set(current.map((item) => item.id))
          const merged = current.map((item) => {
            const next = byId.get(item.id)
            return next ? { ...item, ...next } : item
          })
          const fresh = result.items.filter((item) => !known.has(item.id))
          return fresh.length ? [...fresh, ...merged] : merged
        })
      }).catch(() => undefined)
    }, 4000)
    return () => window.clearInterval(timer)
  }, [hasActiveRecentTasks, recentTaskPageSize])
  useEffect(() => {
    const batchId = task?.batchId
    activeBatchIdRef.current = batchId ?? null
    batchAutoRunRequestRef.current += 1
    batchDownloadRequestRef.current += 1
    setBatchItems([])
    setBatchDownloadPrecheck(null)
    setBatchDownloadChecking(false)
    if (!batchId) {
      setBatchItemsLoading(false)
      return
    }
    setBatchItemsLoading(true)
    let cancelled = false
    function refresh() {
      void api.listBatchTasks(batchId as string).then((result) => {
        if (!cancelled && activeBatchIdRef.current === batchId) setBatchItems(result.items)
      }).catch(() => undefined).finally(() => {
        if (!cancelled && activeBatchIdRef.current === batchId) setBatchItemsLoading(false)
      })
    }
    refresh()
    const timer = window.setInterval(refresh, 4000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [task?.batchId])
  const handleTaskRailTabKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const nextTab = event.key === 'Home'
      ? 'summary'
      : event.key === 'End'
        ? 'recent'
        : taskRailTab === 'summary' ? 'recent' : 'summary'
    setTaskRailTab(nextTab)
    window.requestAnimationFrame(() => document.getElementById(`task-rail-${nextTab}-tab`)?.focus())
  }
  return (
    <section className="page studio-page">
      <PageTitle
        title="AI 摄影棚"
        subtitle="上传主推款，让系统完成智能选搭、分镜预览并生成电商棚拍成片。"
        actions={task ? (
          <div className="studio-title-actions">
            <button className="management-action" onClick={startNewTask}><ImagePlus size={16} />新建拍摄任务</button>
          </div>
        ) : undefined}
      />
      <div className="studio-grid">
        <div className="studio-work-area">
          {task?.batchId && (
            <BatchBar
              items={visibleBatchItems}
              loading={batchContextLoading}
              downloadChecking={batchDownloadChecking}
              currentTaskId={task.id}
              onOpenTask={openBatchTask}
              onPrimaryAction={handleBatchPrimaryAction}
              onDownload={startBatchDownload}
            />
          )}
          <div className="panel studio-main">
            <div className="studio-main-top">
              <StepRail active={visibleStep} executing={studioView?.executingStage ?? null} max={currentStep} onSelect={setStep} />
            </div>
            <div className="studio-stage-viewport">
              {visibleStep === 0 && <UploadHeroStep task={task} setTask={applyTaskUpdate} run={run} uploadSessions={uploadSessions} refreshUploadSessions={refreshUploadSessions} markBrowserUploading={markBrowserUploading} />}
              {visibleStep === 1 && <StylingStep task={task} setTask={applyTaskUpdate} run={run} userInstruction={aiStylingInstruction} localOperations={localOperations} startLocalOperation={startLocalOperation} clearLocalOperationsFor={clearLocalOperationsFor} isAdmin={isAdmin} onOpenAICallLogs={openAICallLogs} />}
              {visibleStep === 2 && <PreviewStep task={task} setTask={applyTaskUpdate} run={run} userInstruction={storyboardInstruction} localOperations={localOperations} startLocalOperation={startLocalOperation} clearLocalOperationsFor={clearLocalOperationsFor} isAdmin={isAdmin} onOpenAICallLogs={openAICallLogs} />}
              {visibleStep === 3 && <FinalStep task={task} setTask={applyTaskUpdate} run={run} userInstruction={finalImageInstruction} localOperations={localOperations} startLocalOperation={startLocalOperation} clearLocalOperationsFor={clearLocalOperationsFor} isAdmin={isAdmin} onOpenAICallLogs={openAICallLogs} />}
            </div>
          </div>
        </div>
        <aside className="panel task-rail">
          <div className="task-rail-tabs" role="tablist" aria-label="任务侧栏">
            <button id="task-rail-summary-tab" role="tab" aria-selected={taskRailTab === 'summary'} aria-controls="task-summary-panel" tabIndex={taskRailTab === 'summary' ? 0 : -1} className={taskRailTab === 'summary' ? 'active' : ''} onClick={() => setTaskRailTab('summary')} onKeyDown={handleTaskRailTabKeyDown}>任务摘要</button>
            <button id="task-rail-recent-tab" role="tab" aria-selected={taskRailTab === 'recent'} aria-controls="recent-tasks-panel" tabIndex={taskRailTab === 'recent' ? 0 : -1} className={taskRailTab === 'recent' ? 'active' : ''} onClick={() => setTaskRailTab('recent')} onKeyDown={handleTaskRailTabKeyDown}>最近任务</button>
          </div>
          <div id="task-summary-panel" role="tabpanel" aria-labelledby="task-rail-summary-tab" className="task-rail-panel task-summary-panel" hidden={taskRailTab !== 'summary'}>
            {task ? (
              <>
                <div className="task-summary-identity">
                  <AssetThumb asset={taskCoverAsset(task)} interactive={false} />
                  <div><TruncatedText as="strong" text={task.name} /><span>{studioView?.statusLabel ?? statusText(task.status)}</span><em>主推款 {task.images?.length ?? 0} 张</em></div>
                </div>
                <div className="task-summary-progress">
                  <Meta label="当前步骤" value={studioView?.currentStageLabel ?? '上传主推款'} />
                  <Meta label="总体进度" value={studioView?.progressLabel ?? '等待开始'} />
                  <Meta label="最终成片" value={`${task.resultSet?.images?.filter((i) => i.status === 'SUCCEEDED').length ?? 0}/5`} />
                  {!!task.styling?.selectedMaterials?.length && <Meta label="搭配服装" value={`${task.styling.selectedMaterials.length} 件`} />}
                </div>
                {summaryInputStep > 0 && (
                  <StageUserInputModule
                    step={summaryInputStep}
                    task={task}
                    value={stageInputValue(summaryInputStep, { aiStylingInstruction, storyboardInstruction, finalImageInstruction })}
                    savedValue={savedStageInputValue(summaryInputStep, task)}
                    onChange={(value) => updateStageInput(summaryInputStep, value, { setAiStylingInstruction, setStoryboardInstruction, setFinalImageInstruction })}
                    onSave={() => void run(`${stageInputSaveLabel(summaryInputStep)}中...`, () => api.updateTask(task.id, stageInputSavePayload(summaryInputStep, stageInputValue(summaryInputStep, { aiStylingInstruction, storyboardInstruction, finalImageInstruction }))), (r: { item: GenerationTask }) => applyTaskUpdate(r.item), { refresh: 'background' })}
                    disabled={isTaskRunning(task) || !!task.autoRunTarget}
                  />
                )}
                <div className="task-action-row">
                  {isTaskRunning(task) ? (
                    <button className="management-action" onClick={cancelCurrentTask}><CircleStop size={15} />取消生成</button>
                  ) : isTaskQueuedForAutoRun(task) ? (
                    <button className="management-action" onClick={() => cancelAutoRunQueue(task)}><CircleStop size={15} />取消排队</button>
                  ) : (canStartOneClickFinal(task) || canRestartOneClickFinal(task)) ? (
                    <button className="task-action" onClick={startOneClickFinal}><Sparkles size={15} />{oneClickFinalLabel(task)}</button>
                  ) : null}
                </div>
              </>
            ) : (
              <EmptyState title="暂无任务" text="上传主推款后会在这里形成拍摄任务。" />
            )}
          </div>
          <div id="recent-tasks-panel" role="tabpanel" aria-labelledby="task-rail-recent-tab" className="task-rail-panel recent-tasks-panel" hidden={taskRailTab !== 'recent'}>
            <div className="mini-list recent-task-list" onScroll={handleRecentTaskScroll}>
              {recentItems.map((entry) => entry.kind === 'batch' ? (
                <RecentBatchTaskRow
                  key={entry.id}
                  item={entry.batch}
                  activeTaskId={task?.id ?? null}
                  activeBatchId={task?.batchId ?? null}
                  expanded={expandedRecentBatchIds.has(entry.batch.batchId)}
                  tasks={recentBatchTasks[entry.batch.batchId] ?? []}
                  loading={!!recentBatchLoading[entry.batch.batchId]}
                  onToggle={toggleRecentBatch}
                  onOpenTask={openBatchTask}
                />
              ) : (
                <RecentSingleTaskRow key={entry.id} item={entry.task} active={task?.id === entry.task.id} onOpen={openRecentTask} />
              ))}
              {!recentItems.length && <EmptyState title="暂无任务" text="创建拍摄任务后会出现在这里。" />}
              {!!recentItems.length && (
                <div className="recent-task-footer" role="status" aria-live="polite">
                  <span>{recentTaskLoading ? '正在加载更多...' : recentTaskHasMore ? `已显示 ${recentItems.length} / 共 ${recentTaskTotal} 条` : `已触底，共 ${recentTaskTotal} 条`}</span>
                </div>
              )}
            </div>
          </div>
        </aside>
      </div>
      {aiLogDialog && (
        <AICallLogDialog
          dialog={aiLogDialog}
          onClose={closeAICallLogs}
        />
      )}
      {batchDownloadPrecheck && (
        <FinalImageDownloadDialog
          precheck={batchDownloadPrecheck}
          onClose={() => setBatchDownloadPrecheck(null)}
          onDownload={triggerBatchDownload}
        />
      )}
    </section>
  )
}

function preferredRecentTaskPageSize() {
  if (typeof window === 'undefined') return 16
  const availableHeight = Math.max(360, window.innerHeight - 260)
  const visibleRows = Math.ceil(availableHeight / recentTaskRowHeight)
  const bufferedRows = visibleRows * 2
  return Math.min(recentTaskMaxPageSize, Math.max(12, bufferedRows))
}

function StepRail({ active, executing, max, onSelect }: { active: StudioStep; executing: StudioStep | null; max: StudioStep; onSelect: (step: StudioStep) => void }) {
  return (
    <div className="step-rail">
      {steps.map((step, index) => {
        const enabled = index <= max
        const isExecuting = executing === index
        const isDone = index < max
        return (
          <button
            key={step.title}
            className={`${active === index ? 'active' : ''} ${index < max ? 'done' : ''} ${isExecuting ? 'executing' : ''}`}
            disabled={!enabled}
            onClick={() => onSelect(index as StudioStep)}
            aria-current={active === index ? 'step' : undefined}
            aria-label={`${step.title}${isExecuting ? '，正在执行' : ''}`}
          >
            <strong>{isExecuting ? <Loader2 size={14} className="spin" /> : isDone ? <Check size={14} /> : index + 1}</strong>
            <span>{step.title}</span>
          </button>
        )
      })}
    </div>
  )
}

function upsertRecentTaskItem(items: GenerationTask[], task: GenerationTask) {
  const next = items.map((item) => item.id === task.id ? { ...item, ...task } : item)
  if (next.some((item) => item.id === task.id)) return next
  return [task, ...next]
}

function recentRecordTasks(items: GenerationWorkspaceRecord[]) {
  return items.flatMap((item) => item.kind === 'task' ? [item.task] : item.batch.coverTasks)
}

function upsertRecentRecordItem(items: GenerationWorkspaceRecord[], task: GenerationTask) {
  let touched = false
  const next = items.map((item) => {
    if (item.kind === 'task' && item.task.id === task.id) {
      touched = true
      return {
        ...item,
        status: task.status,
        name: task.name,
        updatedAt: task.updatedAt,
        task: { ...item.task, ...task },
      }
    }
    if (item.kind === 'batch' && task.batchId === item.batch.batchId) {
      touched = true
      return {
        ...item,
        updatedAt: task.updatedAt,
        batch: {
          ...item.batch,
          updatedAt: task.updatedAt,
          coverTasks: upsertRecentTaskItem(item.batch.coverTasks, task).slice(0, 4),
        },
      }
    }
    return item
  })
  if (touched) return next
  return [{
    kind: 'task' as const,
    id: `task-${task.id}`,
    status: task.status,
    name: task.name,
    createdAt: task.createdAt,
    updatedAt: task.updatedAt,
    task,
  }, ...next]
}

function mergeRecentRecordItems(current: GenerationWorkspaceRecord[], incoming: GenerationWorkspaceRecord[]) {
  const seen = new Set<string>()
  return [...current, ...incoming].filter((item) => {
    if (seen.has(item.id)) return false
    seen.add(item.id)
    return true
  })
}

function taskCoverAsset(task: GenerationTask) {
  return task.summary?.cover ?? task.images?.[0]?.asset ?? null
}

function RecentSingleTaskRow({ item, active, onOpen, showKind = true }: { item: GenerationTask; active: boolean; onOpen: (item: GenerationTask) => void; showKind?: boolean }) {
  const statusLabel = item.archivedAt ? archiveStateText(item.archiveReason) : taskListStatusLabel(item)
  const dateLabel = dateText(item.createdAt)
  const tooltip = `${item.name} · ${statusLabel} · ${dateLabel}`
  return (
    <div className={`mini-row view-only recent-task-row is-single ${active ? 'active' : ''} ${item.archivedAt ? 'is-archived' : ''}`}>
      <button onClick={() => onOpen(item)} title={tooltip} aria-label={tooltip}>
        <AssetThumb asset={taskCoverAsset(item)} interactive={false} />
        <span className="recent-task-copy">
          <span className="recent-task-title-line">
            <TruncatedText as="strong" text={item.name} />
            {showKind && <em className="recent-kind-chip">单次</em>}
          </span>
          <span className="recent-task-meta">
            <TruncatedText as="em" text={statusLabel} />
            <time dateTime={item.createdAt}>{dateLabel}</time>
          </span>
        </span>
      </button>
    </div>
  )
}

function RecentBatchTaskRow({ item, activeTaskId, activeBatchId, expanded, tasks, loading, onToggle, onOpenTask }: {
  item: GenerationRecordBatch
  activeTaskId: number | null
  activeBatchId: string | null
  expanded: boolean
  tasks: GenerationTask[]
  loading: boolean
  onToggle: (item: GenerationRecordBatch) => void
  onOpenTask: (item: GenerationTask) => void
}) {
  const active = activeBatchId === item.batchId
  const statusLabel = `${recordStatusText(item.status)} · ${item.taskCount} 项`
  const dateLabel = dateText(item.updatedAt)
  const tooltip = `${item.name} · ${statusLabel} · ${dateLabel}`
  return (
    <div className={`recent-batch-shell ${expanded ? 'is-expanded' : ''}`}>
      <div className={`mini-row view-only recent-task-row is-batch ${active ? 'active' : ''} ${item.status === 'ARCHIVED' ? 'is-archived' : ''}`}>
        <button aria-expanded={expanded} onClick={() => onToggle(item)} title={tooltip} aria-label={tooltip}>
          <div className="recent-batch-cover-stack" aria-hidden="true">
            {item.coverTasks.slice(0, 4).map((task) => <AssetThumb key={task.id} asset={taskCoverAsset(task)} interactive={false} />)}
            {!item.coverTasks.length && <AssetThumb asset={null} interactive={false} />}
          </div>
          <span className="recent-task-copy">
            <span className="recent-task-title-line">
              <TruncatedText as="strong" text={item.name} />
              <em className="recent-kind-chip is-batch">批次</em>
            </span>
            <span className="recent-task-meta">
              <TruncatedText as="em" text={statusLabel} />
              <time dateTime={item.updatedAt}>{dateLabel}</time>
            </span>
          </span>
        </button>
      </div>
      {expanded && (
        <div className="recent-batch-children">
          {loading && <span className="recent-batch-loading"><Loader2 className="spin" size={14} />读取批次任务...</span>}
          {!loading && tasks.map((task) => (
            <RecentSingleTaskRow
              key={task.id}
              item={task}
              active={activeTaskId === task.id}
              onOpen={onOpenTask}
              showKind={false}
            />
          ))}
          {!loading && !tasks.length && <span className="recent-batch-more">暂无可显示的批次任务</span>}
        </div>
      )}
    </div>
  )
}

function TaskResumeActionContent({ task }: { task: GenerationTask }) {
  if (task.archivedAt) return <><Eye size={14} />查看任务</>
  if (isTaskRunning(task)) return <><Loader2 className="spin" size={14} />查看进度</>
  if (task.status === 'COMPLETED') return <><RotateCcw size={14} />再次生成</>
  return <><Sparkles size={14} />继续生成</>
}

function batchChipStatus(item: GenerationTask) {
  if (item.status === 'COMPLETED') return '完成'
  if (item.status === 'FINAL_RUNNING') return `成片 ${item.summary?.finalImages?.length ?? 0}/5`
  if (isTaskQueuedForAutoRun(item)) {
    return item.autoRunQueuePosition && item.autoRunQueuePosition > 0 ? `排队第 ${item.autoRunQueuePosition} 位` : '排队中'
  }
  return statusText(item.status)
}

function batchChipTone(item: GenerationTask) {
  if (item.status === 'COMPLETED') return 'is-done'
  if (item.status === 'FAILED') return 'is-failed'
  if (isTaskRunning(item)) return 'is-running'
  if (isTaskQueuedForAutoRun(item)) return 'is-queued'
  return ''
}

function BatchBar({ items, loading, downloadChecking, currentTaskId, onOpenTask, onPrimaryAction, onDownload }: {
  items: GenerationTask[]
  loading: boolean
  downloadChecking: boolean
  currentTaskId: number | null
  onOpenTask: (task: GenerationTask) => void
  onPrimaryAction: (action: BatchPrimaryAction) => void
  onDownload: () => void
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  useOutsideClose(menuOpen, wrapRef, () => setMenuOpen(false))
  const openTask = (item: GenerationTask) => {
    onOpenTask(item)
    setMenuOpen(false)
  }
  return (
    <div className={`panel batch-bar ${menuOpen ? 'is-menu-open' : ''}`} ref={wrapRef}>
      <div className="batch-bar-main">
        <span className="recent-kind-chip is-batch">{loading ? '读取中' : '批次'}</span>
        <div className="batch-bar-chips">
          {items.map((item) => (
            <button
              key={item.id}
              className={`batch-chip ${batchChipTone(item)} ${item.id === currentTaskId ? 'active' : ''}`}
              onClick={() => openTask(item)}
              title={`切换到 ${item.name}`}
            >
              <TruncatedText as="strong" text={item.name} />
              <span>{batchChipStatus(item)}</span>
            </button>
          ))}
        </div>
        {items.length > 1 && (
          <div className="batch-task-select">
            <button className="batch-expand-button" type="button" onClick={() => setMenuOpen((value) => !value)} title={menuOpen ? '收起批次任务' : '查看全部批次任务'} aria-label={menuOpen ? '收起批次任务' : '查看全部批次任务'} aria-expanded={menuOpen} aria-haspopup="menu" aria-controls="batch-task-popover">
              <ChevronDown size={16} />
            </button>
            {menuOpen && (
              <div id="batch-task-popover" className="filter-popover sort-popover batch-task-popover" role="menu">
                {items.map((item, index) => (
                  <button
                    className={item.id === currentTaskId ? 'is-selected' : ''}
                    type="button"
                    role="menuitem"
                    key={item.id}
                    onClick={() => openTask(item)}
                    title={`切换到 ${item.name}`}
                  >
                    <span>
                      <TruncatedText as="strong" text={item.name} />
                      <TruncatedText as="small" text={`第 ${index + 1}/${items.length} 个任务 · ${batchChipStatus(item)}`} />
                    </span>
                    {item.id === currentTaskId && <Check size={15} />}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
      <BatchTaskSummary currentTaskId={currentTaskId} items={items} loading={loading} downloadChecking={downloadChecking} onPrimaryAction={onPrimaryAction} onDownload={onDownload} />
    </div>
  )
}

type BatchPrimaryAction = 'start' | 'continue' | 'cancel' | 'restart' | 'none'

function batchPrimaryAction(items: GenerationTask[]): BatchPrimaryAction {
  const activeItems = items.filter((item) => !item.archivedAt && item.status !== 'ABANDONED')
  if (activeItems.some((item) => isTaskRunning(item) || !!item.autoRunTarget)) return 'cancel'
  if (activeItems.length > 0 && activeItems.every((item) => item.status === 'COMPLETED')) return 'restart'
  const eligibleItems = activeItems.filter((item) => canStartOneClickFinal(item))
  if (!eligibleItems.length) return 'none'
  const isFreshBatch = activeItems.every((item) => ['DRAFT', 'READY_FOR_STYLING'].includes(item.status))
  return isFreshBatch ? 'start' : 'continue'
}

function BatchTaskSummary({ currentTaskId, items, loading, downloadChecking, onPrimaryAction, onDownload }: {
  currentTaskId: number | null
  items: GenerationTask[]
  loading: boolean
  downloadChecking: boolean
  onPrimaryAction: (action: BatchPrimaryAction) => void
  onDownload: () => void
}) {
  const currentIndex = items.findIndex((item) => item.id === currentTaskId)
  const primaryAction = batchPrimaryAction(items)
  const downloadableCount = items.filter((item) => ['COMPLETED', 'FINAL_PARTIAL'].includes(item.status)).length
  const actionPresentation = {
    start: { label: '全部一键成片', icon: Sparkles, className: 'task-action compact-action' },
    continue: { label: '继续一键成片', icon: Sparkles, className: 'task-action compact-action' },
    cancel: { label: '全部取消', icon: CircleStop, className: 'management-action compact-control' },
    restart: { label: '重新一键成片', icon: RotateCcw, className: 'task-action compact-action' },
    none: { label: '全部一键成片', icon: Sparkles, className: 'task-action compact-action' },
  }[primaryAction]
  const ActionIcon = actionPresentation.icon
  return (
    <section className="task-batch-summary" aria-label="批次操作">
      <span className="task-batch-label">
        {loading ? '读取中' : currentIndex >= 0 ? `第 ${currentIndex + 1}/${items.length} 个任务` : `共 ${items.length} 个任务`}
      </span>
      <div className="task-batch-actions" aria-live="polite">
        <button className={actionPresentation.className} type="button" disabled={loading || primaryAction === 'none'} onClick={() => onPrimaryAction(primaryAction)}><ActionIcon size={14} />{actionPresentation.label}</button>
        <button className="management-action compact-control" type="button" disabled={loading || !downloadableCount || downloadChecking} onClick={onDownload}><Download size={14} />{downloadChecking ? '检查成片中...' : '下载本批次成片'}</button>
      </div>
    </section>
  )
}

type PendingBatchGroup = {
  id: string
  name: string
  userRequirement?: string
  files: File[]
  issues?: ImportIssue[]
  error?: string
}

function UploadHeroStep({ task, setTask, run, uploadSessions, refreshUploadSessions, markBrowserUploading }: {
  task: GenerationTask | null
  setTask: (task: GenerationTask) => void
  run: any
  uploadSessions: UploadSession[]
  refreshUploadSessions: () => Promise<UploadSession[]>
  markBrowserUploading: (delta: 1 | -1) => void
}) {
  const [files, setFiles] = useState<File[]>([])
  const [name, setName] = useState('')
  const [userRequirement, setUserRequirement] = useState('')
  const [singleIssues, setSingleIssues] = useState<ImportIssue[]>([])
  const [batchGroups, setBatchGroups] = useState<PendingBatchGroup[]>([])
  const [batchName, setBatchName] = useState('')
  const [batchCreating, setBatchCreating] = useState<{ current: number; total: number } | null>(null)
  const [uploadProgress, setUploadProgress] = useState<UploadProgressSnapshot | null>(null)
  const [batchError, setBatchError] = useState('')
  const [existingTaskNames, setExistingTaskNames] = useState<Set<string> | null>(null)
  const batchIdRef = useRef('')
  const firstCreatedTaskRef = useRef<GenerationTask | null>(null)
  const uploadWarningActiveRef = useRef(false)

  function trackUploadProgress(progress: UploadProgressSnapshot) {
    const shouldWarn = progress.phase === 'preparing' || progress.phase === 'uploading'
    if (shouldWarn !== uploadWarningActiveRef.current) {
      markBrowserUploading(shouldWarn ? 1 : -1)
      uploadWarningActiveRef.current = shouldWarn
    }
    setUploadProgress(progress)
  }

  function clearUploadWarning() {
    if (!uploadWarningActiveRef.current) return
    markBrowserUploading(-1)
    uploadWarningActiveRef.current = false
  }

  useEffect(() => () => clearUploadWarning(), [])

  useEffect(() => {
    if (!batchGroups.length || existingTaskNames) return
    void api.listTasks({ limit: 100, includeArchived: true })
      .then((result) => setExistingTaskNames(new Set(result.items.map((item) => item.name))))
      .catch(() => setExistingTaskNames(new Set()))
  }, [batchGroups.length, existingTaskNames])

  function applySingleFiles(imageFiles: File[], defaultName?: string, issues: ImportIssue[] = []) {
    setFiles(imageFiles)
    const firstPath = imageFiles[0] ? importPath(imageFiles[0]) : ''
    setName(defaultName || (firstPath.includes('/') ? sanitizeClientName(firstPath.split('/')[0]) : timestampName()))
    setUserRequirement('')
    setSingleIssues(issues)
  }

  function pendingGroupFromSingle(): PendingBatchGroup | null {
    if (!files.length) return null
    return {
      id: `single-${Date.now()}`,
      name: sanitizeClientName(name) || timestampName(),
      userRequirement,
      files,
      issues: singleIssues,
    }
  }

  function pendingGroupsFromImports(groups: ImportBatch[]): PendingBatchGroup[] {
    return groups.map((group, index) => ({
      id: `${group.id}-${Date.now()}-${index}`,
      name: group.code,
      userRequirement: '',
      files: group.files,
      issues: group.issues,
    }))
  }

  function inferredBatchName(nextFiles: File[]) {
    const folderNames = nextFiles
      .map((file) => importPath(file).split('/').filter(Boolean)[0])
      .filter(Boolean)
    const uniqueNames = Array.from(new Set(folderNames))
    return uniqueNames.length === 1 ? uniqueNames[0].slice(0, BATCH_NAME_MAX_CHARS) : timestampName()
  }

  function addHeroFiles(nextFiles: File[]) {
    if (uploadProgress) return
    const imageFiles = nextFiles
      .filter(isSupportedImageFile)
      .sort((a, b) => importPath(a).localeCompare(importPath(b), 'zh-CN', { numeric: true }))
    const folderFiles = nextFiles.filter((file) => importPath(file).includes('/'))
    const grouping = groupImportFiles(folderFiles.length ? folderFiles : nextFiles)
    const groups = grouping.batches
    if (!groups.length) {
      const ignoredCount = grouping.ignoredFiles.length
      if (ignoredCount) {
        const ignoredIssue: ImportIssue = {
          code: 'ignored_non_image_files',
          count: ignoredCount,
          message: `已忽略 ${ignoredCount} 个非图片文件`,
        }
        if (batchGroups.length) {
          setBatchError(ignoredIssue.message)
        } else if (files.length) {
          setSingleIssues((current) => [...current, ignoredIssue])
        } else {
          setBatchError(ignoredIssue.message)
        }
      }
      return
    }
    if (groups.length > 1 || batchGroups.length > 0 || files.length > 0) {
      const existingSingle = pendingGroupFromSingle()
      setBatchGroups((current) => [
        ...current,
        ...(current.length ? [] : existingSingle ? [existingSingle] : []),
        ...pendingGroupsFromImports(groups),
      ])
      setBatchError('')
      setFiles([])
      setName('')
      setUserRequirement('')
      setSingleIssues([])
      batchIdRef.current = ''
      firstCreatedTaskRef.current = null
      if (!batchGroups.length) setBatchName(inferredBatchName(nextFiles))
      return
    }
    setBatchGroups([])
    setBatchName('')
    setBatchError('')
    const group = groups[0]
    if (group && folderFiles.length) {
      applySingleFiles(group.files, group.code, group.issues)
    } else {
      applySingleFiles(imageFiles, undefined, group?.issues)
    }
  }

  function updateBatchGroup(id: string, patch: Partial<PendingBatchGroup>) {
    setBatchGroups((items) => items.map((item) => item.id === id ? { ...item, ...patch, error: undefined } : item))
  }

  async function createBatchTasks() {
    if (!batchGroups.length || batchCreating) return
    const seen = new Set<string>()
    const ready: PendingBatchGroup[] = []
    const blocked: PendingBatchGroup[] = []
    batchGroups.forEach((group) => {
      const groupName = sanitizeClientName(group.name)
      if (!groupName) {
        blocked.push({ ...group, error: '每个任务都需要一个有效名称' })
        return
      }
      if (!group.files.length) {
        blocked.push({ ...group, name: groupName, error: '没有可用图片' })
        return
      }
      if (seen.has(groupName)) {
        blocked.push({ ...group, name: groupName, error: '本批次任务名重复' })
        return
      }
      seen.add(groupName)
      ready.push({ ...group, name: groupName, error: undefined })
    })
    if (!ready.length) {
      setBatchGroups(blocked)
      setBatchError('没有可创建的任务，请先处理标红项')
      return
    }
    setBatchGroups([...blocked, ...ready])
    setBatchError(blocked.length ? '标红项不会创建，处理后可再次提交' : '')
    if (!batchIdRef.current) batchIdRef.current = `b-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
    const effectiveBatchName = batchName.trim().slice(0, BATCH_NAME_MAX_CHARS) || timestampName()
    const created: GenerationTask[] = []
    let lastError = ''
    let failedCount = 0
    for (let index = 0; index < ready.length; index += 1) {
      const group = ready[index]
      setBatchCreating({ current: index + 1, total: ready.length })
      try {
        const uploadFiles = formalAssetFiles(group.files, HERO_PRODUCT_MAX_IMAGES)
        const result = await api.createTask(group.name, uploadFiles, batchIdRef.current, group.userRequirement ?? '', effectiveBatchName, { onProgress: trackUploadProgress })
        clearUploadWarning()
        void refreshUploadSessions().catch(() => undefined)
        created.push(result.item)
        if (!firstCreatedTaskRef.current) firstCreatedTaskRef.current = result.item
        setBatchGroups((current) => current.filter((item) => item.id !== group.id))
      } catch (err) {
        clearUploadWarning()
        void refreshUploadSessions().catch(() => undefined)
        lastError = err instanceof Error ? err.message : '创建任务失败'
        failedCount += 1
        setBatchGroups((current) => current.map((item) => item.id === group.id ? { ...item, error: lastError } : item))
      }
    }
    setBatchCreating(null)
    setUploadProgress(null)
    if (blocked.length || failedCount) {
      const parts = []
      if (created.length) parts.push(`已创建 ${created.length} 个任务`)
      if (failedCount) parts.push(`${failedCount} 个创建失败${lastError ? `：${lastError}` : ''}`)
      if (blocked.length) parts.push('标红项处理后可再次提交，会并入同一批次')
      setBatchError(parts.join('；'))
      return
    }
    const firstTask = firstCreatedTaskRef.current
    if (firstTask) {
      setTask(firstTask)
      window.history.pushState(null, '', routeToUrl({ page: 'studio', taskId: firstTask.id }))
    }
  }

  async function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    if (uploadProgress) return
    const droppedFiles = await filesFromDataTransfer(event.dataTransfer)
    addHeroFiles(droppedFiles)
  }

  return (
    <div className="step-panel">
      <div className="step-scroll-content">
        <div className="step-copy"><HeadingWithTooltip as="h2" tooltip="支持多张图片、一个货号文件夹或一个上级文件夹。每套最多 15 张进入候选池，AI 智能选搭后保留挑选排序好的 8 张主推款参考图。">上传主推款</HeadingWithTooltip></div>
        {task?.status === 'DRAFT' && <HeroDraftEditor task={task} setTask={setTask} run={run} />}
        {!task && (
          <>
            <div className={`hero-upload ${uploadProgress ? 'is-disabled' : ''}`} onDragOver={(event) => event.preventDefault()} onDrop={(event) => void handleDrop(event)}>
              <div>
                <Images size={22} />
                <strong>拖入主推款图片或文件夹</strong>
                <span>{files.length ? `当前待创建 ${files.length} 张；顶部继续选择会新增一款` : '支持多张图片、一个文件夹，或多个货号文件夹（批量成片）'}</span>
              </div>
              <div className="import-actions">
                <label className="management-action">
                  <ImagePlus size={16} /> 选择图片
                  <input type="file" accept={SUPPORTED_IMAGE_ACCEPT} multiple disabled={!!uploadProgress} onChange={(event) => addHeroFiles(Array.from(event.target.files ?? []))} />
                </label>
                <label className="management-action">
                  <FolderOpen size={16} /> 选择文件夹
                  <input type="file" multiple disabled={!!uploadProgress} {...{ webkitdirectory: '', directory: '' }} onChange={(event) => addHeroFiles(Array.from(event.target.files ?? []))} />
                </label>
              </div>
            </div>
            {batchGroups.length > 0 && (() => {
              const nameCounts = new Map<string, number>()
              batchGroups.forEach((group) => {
                const groupName = sanitizeClientName(group.name)
                if (groupName) nameCounts.set(groupName, (nameCounts.get(groupName) ?? 0) + 1)
              })
              return (
                <div className="upload-review">
                  <div className="batch-card-head">
                    <div className="batch-icon"><FolderOpen size={18} /></div>
                    <strong className="batch-detect-title">待创建 {batchGroups.length} 个拍摄任务</strong>
                    <span className="count-pill">{batchGroups.reduce((sum, group) => sum + group.files.length, 0)} 张图</span>
                  </div>
                  <div className="batch-card-form">
                    <div className="batch-icon"><Layers3 size={18} /></div>
                    <label className="batch-field batch-info-field">
                      <span>批次名</span>
                      <input
                        value={batchName}
                        maxLength={BATCH_NAME_MAX_CHARS}
                        onChange={(event) => setBatchName(event.target.value.slice(0, BATCH_NAME_MAX_CHARS))}
                        placeholder="本批拍摄任务名称"
                        aria-label="批次名"
                        disabled={!!batchCreating}
                      />
                    </label>
                    <span className="batch-form-spacer" />
                  </div>
                  <div className="batch-note">
                    <span>顶部选择的散图或文件夹会新增为待创建任务；补充当前货号图片请使用卡片内的新增图片。创建后可从顶部批次条一键批量成片。</span>
                  </div>
                  <div className="batch-list">
                    {batchGroups.map((group) => {
                      const groupName = sanitizeClientName(group.name)
                      const dupInBatch = !!groupName && (nameCounts.get(groupName) ?? 0) > 1
                      const overLimit = group.files.length > HERO_PRODUCT_MAX_IMAGES
                      const hasBlockingError = !group.files.length || dupInBatch || !!group.error
                      const hasError = hasBlockingError
                      const existsHint = !!groupName && !dupInBatch && !!existingTaskNames?.has(groupName)
                      return (
                        <div className={`batch-card ${hasError ? 'has-error' : ''}`} key={group.id}>
                          <div className="batch-card-form">
                            <div className="batch-icon"><Layers3 size={18} /></div>
                            <label className="batch-field batch-code-field">
                              <span>货号</span>
                              <input
                                value={group.name}
                                maxLength={PRODUCT_CODE_MAX_CHARS}
                                onChange={(event) => updateBatchGroup(group.id, { name: event.target.value.slice(0, PRODUCT_CODE_MAX_CHARS) })}
                                aria-label="主推款货号"
                                disabled={!!batchCreating || !!uploadProgress}
                              />
                            </label>
                            <label className="batch-field batch-info-field">
                              <span>服装信息</span>
                              <input
                                value={group.userRequirement ?? ''}
                                maxLength={800}
                                onChange={(event) => updateBatchGroup(group.id, { userRequirement: event.target.value.slice(0, 800) })}
                                placeholder="品类 / 颜色 / 面料 / 卖点 / 注意事项"
                                aria-label="主推款服装信息"
                                disabled={!!batchCreating || !!uploadProgress}
                              />
                            </label>
                            <span className={`count-pill ${overLimit ? 'is-over' : ''}`}>{formatImageCount(group.files.length, HERO_PRODUCT_MAX_IMAGES)}</span>
                            <button className="management-action icon-button" onClick={() => setBatchGroups((current) => current.filter((item) => item.id !== group.id))} title="移除该任务" disabled={!!batchCreating || !!uploadProgress}><Trash2 size={16} /></button>
                          </div>
                            <div className="batch-note">
                              <span>推荐 3-5 张，排序越靠前越重要。{existsHint ? ' 与已有任务重名，将共存（任务名不要求唯一）。' : ''}</span>
                              {hasBlockingError && <strong>{group.error ?? (!group.files.length ? '没有可用图片' : '本批次任务名重复')}</strong>}
                              {overLimit && <strong>{`超出 ${HERO_PRODUCT_MAX_IMAGES} 张，红框图片不会进入后续识别`}</strong>}
                              {group.issues?.map((issue) => <strong key={`${issue.code}-${issue.path ?? issue.count ?? issue.message}`}>{issue.message}</strong>)}
                          </div>
                          <FileThumbList files={group.files} onChange={(nextFiles) => updateBatchGroup(group.id, { files: nextFiles })} limit={HERO_PRODUCT_MAX_IMAGES} disabled={!!uploadProgress} />
                        </div>
                      )
                    })}
                  </div>
                  <div className="studio-upload-sticky-actions">
                    {batchError && <span className="inline-error">{batchError}</span>}
                    {uploadProgress && <UploadProgressPanel progress={uploadProgress} prefix={batchCreating ? `第 ${batchCreating.current}/${batchCreating.total} 个任务` : undefined} />}
                    {batchCreating ? (
                      <p className="edit-hint">正在创建第 {batchCreating.current}/{batchCreating.total} 个任务，请勿刷新或离开页面...</p>
                    ) : (
                      <div className="inline-form sticky-action-buttons">
                        <button className="management-action" disabled={!!uploadProgress} onClick={() => void createBatchTasks()}>
                          <ImagePlus size={16} /> 批量创建 {batchGroups.length} 个任务
                        </button>
                        <button className="management-action" disabled={!!uploadProgress} onClick={() => { setBatchGroups([]); setBatchName('') }}>取消</button>
                      </div>
                    )}
                  </div>
                </div>
              )
            })()}
            {files.length > 0 && (
              <div className="upload-review">
                <div className="batch-card-form">
                  <div className="batch-icon"><Layers3 size={18} /></div>
                  <label className="batch-field batch-code-field">
                    <span>货号</span>
                    <input value={name} maxLength={PRODUCT_CODE_MAX_CHARS} disabled={!!uploadProgress} onChange={(e) => setName(e.target.value.slice(0, PRODUCT_CODE_MAX_CHARS))} placeholder="主推款货号" aria-label="主推款货号" />
                  </label>
                  <label className="batch-field batch-info-field">
                    <span>服装信息</span>
                    <input value={userRequirement} maxLength={800} disabled={!!uploadProgress} onChange={(event) => setUserRequirement(event.target.value.slice(0, 800))} placeholder="品类 / 颜色 / 面料 / 卖点 / 注意事项" aria-label="主推款服装信息" />
                  </label>
                  <span className={`count-pill ${files.length > HERO_PRODUCT_MAX_IMAGES ? 'is-over' : ''}`}>{formatImageCount(files.length, HERO_PRODUCT_MAX_IMAGES)}</span>
                </div>
                  <div className="batch-note">
                    <span>{`最多 ${HERO_PRODUCT_MAX_IMAGES} 张进入候选池，AI 智能选搭后保留 ${HERO_PRODUCT_SELECTED_IMAGES} 张参考图。`}</span>
                    {files.length > HERO_PRODUCT_MAX_IMAGES && <strong>{`当前超过 ${HERO_PRODUCT_MAX_IMAGES} 张，红框图片不会进入后续识别`}</strong>}
                    {singleIssues.map((issue) => <strong key={`${issue.code}-${issue.path ?? issue.count ?? issue.message}`}>{issue.message}</strong>)}
                  </div>
                <FileThumbList files={files} onChange={setFiles} limit={HERO_PRODUCT_MAX_IMAGES} disabled={!!uploadProgress} />
                <div className="inline-form">
                    <button className="management-action" disabled={!files.length || !sanitizeClientName(name) || !!uploadProgress} onClick={() => {
                      void run('创建拍摄任务中...', () => api.createTask(name, formalAssetFiles(files, HERO_PRODUCT_MAX_IMAGES), undefined, userRequirement, undefined, { onProgress: trackUploadProgress }), (r: { item: GenerationTask }) => {
                    setTask(r.item)
                    setFiles([])
                    setName('')
                    setUserRequirement('')
                    setUploadProgress(null)
                    window.history.pushState(null, '', routeToUrl({ page: 'studio', taskId: r.item.id }))
                    void refreshUploadSessions().catch(() => undefined)
                  }, { refresh: 'background' }).catch(() => setUploadProgress(null)).finally(() => {
                    clearUploadWarning()
                    void refreshUploadSessions().catch(() => undefined)
                  })
                    }}>
                    <ImagePlus size={16} /> 创建任务
                  </button>
                </div>
                {uploadProgress && <UploadProgressPanel progress={uploadProgress} />}
              </div>
            )}
          </>
        )}
        {task && task.status !== 'DRAFT' && <HeroDraftEditor task={task} setTask={setTask} run={run} />}
      </div>
    </div>
  )
}

function UploadProgressPanel({ progress, prefix, savingLabel = '保存任务', savingDetail = '正在检查图片并创建拍摄任务' }: {
  progress: UploadProgressSnapshot
  prefix?: string
  savingLabel?: string
  savingDetail?: string
}) {
  const percent = progress.total > 0 ? Math.round((progress.completed / progress.total) * 100) : 0
  const phaseLabel = progress.phase === 'preparing' ? '准备上传' : progress.phase === 'saving' ? savingLabel : '上传图片'
  const detail = progress.phase === 'saving'
    ? savingDetail
    : progress.phase === 'uploading'
      ? `已上传 ${progress.completed}/${progress.total} 张`
      : `${progress.completed}/${progress.total} 张`
  return (
    <div className="upload-progress-panel" role="status" aria-live="polite">
      <div className="upload-progress-head">
        <strong>{prefix ? `${prefix} · ${phaseLabel}` : phaseLabel}</strong>
        <span>{progress.completed}/{progress.total}</span>
      </div>
      <div className="upload-progress-bar" aria-hidden="true"><span style={{ width: `${percent}%` }} /></div>
      <p>{detail}</p>
    </div>
  )
}

type HeroDraftImage = {
  id: number
  asset: MediaAsset
  replacement?: File
}

function HeroDraftEditor({ task, setTask, run }: { task: GenerationTask; setTask: (task: GenerationTask) => void; run: any }) {
  const [name, setName] = useState(task.name)
  const [userRequirement, setUserRequirement] = useState(task.userRequirement ?? '')
  const [draftImages, setDraftImages] = useState<HeroDraftImage[]>(() => (task.images ?? []).map((image) => ({ id: image.id, asset: image.asset })))
  const [viewer, setViewer] = useState<LightboxImage | null>(null)
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const imageCount = draftImages.length
  const imageLimit = task.status === 'DRAFT' ? HERO_PRODUCT_MAX_IMAGES : HERO_PRODUCT_SELECTED_IMAGES
  const originalImages = task.images ?? []
  const draftIds = draftImages.map((image) => image.id).join(',')
  const originalIds = originalImages.map((image) => image.id).join(',')
  const hasImageDraftChange = draftIds !== originalIds || draftImages.some((image) => image.replacement)
  const hasInfoDraftChange = userRequirement.trim() !== (task.userRequirement ?? '').trim()
  const hasPendingDraft = hasImageDraftChange || hasInfoDraftChange

  useEffect(() => {
    setName(task.name)
    setUserRequirement(task.userRequirement ?? '')
    setDraftImages((task.images ?? []).map((image) => ({ id: image.id, asset: image.asset })))
  }, [task.id])

  function resetDraft(nextTask = task) {
    setUserRequirement(nextTask.userRequirement ?? '')
    setDraftImages((nextTask.images ?? []).map((image) => ({ id: image.id, asset: image.asset })))
  }

  function moveTo(from: number, to: number) {
    if (from === to || from < 0 || to < 0 || from >= draftImages.length || to >= draftImages.length) return
    const next = [...draftImages]
    const [item] = next.splice(from, 1)
    next.splice(to, 0, item)
    setDraftImages(next)
  }

  function replaceDraftImage(id: number, file: File) {
    setDraftImages((items) => items.map((item) => item.id === id ? { ...item, replacement: file } : item))
  }

  function removeDraftImage(id: number) {
    setDraftImages((items) => items.filter((item) => item.id !== id))
  }

  async function confirmDraftChanges() {
    if (!hasPendingDraft) return
    if (draftImages.length < 1) return
    if (task.status !== 'DRAFT' && !window.confirm('确认修改主推款输入吗？确认后当前选搭、分镜预览和成片会失效，但历史记录会保留。')) return
    await run(task.status === 'DRAFT' ? '保存主推款修改中...' : '确认主推款修改中...', async () => {
      let result: { item: GenerationTask } = { item: task }
      if (hasInfoDraftChange) {
        result = await api.updateTask(task.id, { userRequirement })
      }
      for (const image of draftImages) {
        if (image.replacement) result = await api.replaceTaskImage(image.id, image.replacement)
      }
      const keptIds = new Set(draftImages.map((image) => image.id))
      for (const image of originalImages) {
        if (!keptIds.has(image.id)) result = await api.deleteTaskImage(image.id)
      }
      for (let index = 0; index < draftImages.length; index += 1) {
        const image = draftImages[index]
        const originalIndex = originalImages.findIndex((item) => item.id === image.id)
        if (originalIndex !== index || image.replacement) {
          result = await api.updateTaskImage(image.id, index)
        }
      }
      return result
    }, (result: { item: GenerationTask }) => {
      setTask(result.item)
      resetDraft(result.item)
    }, { refresh: 'background' })
  }

  return (
    <div className="hero-draft">
      <div className="section-head"><strong>主推款图片</strong><span className={`count-pill ${imageCount > imageLimit ? 'is-over' : ''}`}>{formatImageCount(imageCount, imageLimit)}</span><span className="status draft">{hasPendingDraft ? '待确认修改' : task.status === 'DRAFT' ? '待选搭' : '当前生效'}</span></div>
      <div className="batch-card-form hero-task-form">
        <div className="batch-icon"><Layers3 size={18} /></div>
        <label className="batch-field batch-code-field">
          <span>货号</span>
          <input value={name} maxLength={PRODUCT_CODE_MAX_CHARS} onChange={(event) => setName(event.target.value.slice(0, PRODUCT_CODE_MAX_CHARS))} placeholder="主推款货号" />
        </label>
        <label className="batch-field batch-info-field">
          <span>服装信息</span>
          <input value={userRequirement} maxLength={800} onChange={(event) => setUserRequirement(event.target.value.slice(0, 800))} placeholder="品类 / 颜色 / 面料 / 卖点 / 注意事项" />
        </label>
        <button className="management-action" disabled={name.trim() === task.name} onClick={() => void run('保存货号中...', () => api.updateTask(task.id, { name }), (r: { item: GenerationTask }) => setTask(r.item), { refresh: 'background' })}>保存货号</button>
      </div>
      {hasPendingDraft && <div className="batch-note"><span>当前生效素材和已有结果仍保留；下方为待确认草稿。</span></div>}
      <div className="draft-image-grid">
        {draftImages.map((image, index) => (
          <HeroDraftImageCard
            key={image.id}
            image={image}
            draggable={draftImages.length > 1}
            canRemove={draftImages.length > 1}
            onOpen={setViewer}
            onDragStart={() => setDragIndex(index)}
            onDragOver={(event) => event.preventDefault()}
            onDrop={() => {
              if (dragIndex !== null) moveTo(dragIndex, index)
              setDragIndex(null)
            }}
            onReplace={(file) => replaceDraftImage(image.id, file)}
            onRemove={() => removeDraftImage(image.id)}
          />
        ))}
      </div>
      <p className="edit-hint">{task.status === 'DRAFT' ? `最多 ${HERO_PRODUCT_MAX_IMAGES} 张进入候选池；AI 智能选搭后保留 ${HERO_PRODUCT_SELECTED_IMAGES} 张主推款参考图。` : hasPendingDraft ? '修改尚未生效；确认前不会影响当前选搭、分镜预览和成片。' : `当前展示 AI 选搭后保留的主推款参考图，最多 ${HERO_PRODUCT_SELECTED_IMAGES} 张。`}</p>
      <div className="inline-form">
        <button className="management-action" disabled={!hasPendingDraft || draftImages.length < 1} onClick={() => void confirmDraftChanges()}><Check size={16} />确认修改</button>
        <button className="management-action" disabled={!hasPendingDraft} onClick={() => resetDraft()}><RotateCcw size={16} />放弃修改</button>
      </div>
      {viewer && <ImageLightbox image={viewer} onClose={() => setViewer(null)} />}
    </div>
  )
}

function HeroDraftImageCard({ image, draggable, canRemove, onOpen, onDragStart, onDragOver, onDrop, onReplace, onRemove }: {
  image: HeroDraftImage
  draggable: boolean
  canRemove: boolean
  onOpen: (image: LightboxImage) => void
  onDragStart: () => void
  onDragOver: (event: DragEvent<HTMLDivElement>) => void
  onDrop: () => void
  onReplace: (file: File) => void
  onRemove: () => void
}) {
  const [replacementUrl, setReplacementUrl] = useState('')
  const [assetSrcIndex, setAssetSrcIndex] = useState(0)
  useEffect(() => {
    if (!image.replacement) {
      setReplacementUrl('')
      return
    }
    const nextUrl = URL.createObjectURL(image.replacement)
    setReplacementUrl(nextUrl)
    return () => URL.revokeObjectURL(nextUrl)
  }, [image.replacement])
  const assetSources = assetDisplaySources(image.asset, 'thumb')
  const assetSrc = assetSources[assetSrcIndex] ?? ''
  useEffect(() => setAssetSrcIndex(0), [image.asset.id, assetSources.join('|')])
  const filename = image.replacement?.name ?? image.asset.filename
  const meta = image.replacement ? fileMeta(image.replacement) : assetMeta(image.asset)
  const openImage = () => {
    if (image.replacement && replacementUrl) {
      onOpen(fileLightboxImage(image.replacement, replacementUrl))
      return
    }
    onOpen(assetLightboxImage(image.asset))
  }
  return (
    <div
      className={`saved-image-card ${image.replacement ? 'is-pending' : ''}`}
      draggable={draggable}
      onDragStart={onDragStart}
      onDragOver={onDragOver}
      onDrop={onDrop}
    >
      <button className="image-open" onClick={openImage} title="放大查看">
        <img
          src={replacementUrl || mediaUrl(assetSrc)}
          alt={filename}
          loading="lazy"
          onError={() => {
            if (!replacementUrl && assetSrcIndex + 1 < assetSources.length) {
              setAssetSrcIndex((index) => index + 1)
            }
          }}
        />
      </button>
      {image.replacement && <em className="thumb-pending-badge">待替换</em>}
      <TruncatedText text={filename}>
        {filename}<TruncatedText as="small" text={meta} />
      </TruncatedText>
      <div className="thumb-actions">
        <label title="替换图片">
          <RefreshCw size={14} />
          <input type="file" accept={SUPPORTED_IMAGE_ACCEPT} aria-label="替换主推款图片" onChange={(event) => {
            const replacement = event.target.files?.[0]
            if (replacement) onReplace(replacement)
          }} />
        </label>
        <button disabled={!canRemove} onClick={onRemove} title="移除图片"><Trash2 size={14} /></button>
      </div>
    </div>
  )
}

function RequirementEditor({ value, onChange, title, placeholder }: { value: string; onChange: (value: string) => void; title: string; placeholder: string }) {
  const chips = ['突出腰线', '更显腿长', '减少配饰', '避免外套', '更通勤', '尽量单穿']
  function appendChip(chip: string) {
    const next = value.trim() ? `${value.trim()}；${chip}` : chip
    onChange(next)
  }
  return (
    <div className="requirement-editor">
      <label>
        <span>{title}</span>
        <textarea value={value} maxLength={800} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} />
      </label>
      <div className="requirement-chips">
        {chips.map((chip) => <button key={chip} type="button" className="management-action" onClick={() => appendChip(chip)}>{chip}</button>)}
      </div>
      <em>{value.length}/800</em>
    </div>
  )
}

type StudioStageInputState = {
  aiStylingInstruction: string
  storyboardInstruction: string
  finalImageInstruction: string
}

type StudioStageInputSetters = {
  setAiStylingInstruction: (value: string) => void
  setStoryboardInstruction: (value: string) => void
  setFinalImageInstruction: (value: string) => void
}

function stageInputValue(step: StudioStep, state: StudioStageInputState) {
  if (step === 1) return state.aiStylingInstruction
  if (step === 2) return state.storyboardInstruction
  return state.finalImageInstruction
}

function updateStageInput(step: StudioStep, value: string, setters: StudioStageInputSetters) {
  if (step === 1) setters.setAiStylingInstruction(value)
  else if (step === 2) setters.setStoryboardInstruction(value)
  else setters.setFinalImageInstruction(value)
}

function savedStageInputValue(step: StudioStep, task: GenerationTask | null) {
  if (!task) return ''
  if (step === 1) return task.stageUserInputs?.aiStyling ?? ''
  if (step === 2) return task.stageUserInputs?.storyboardPreview ?? ''
  return task.stageUserInputs?.finalImage ?? ''
}

function stageInputSavePayload(step: StudioStep, value: string) {
  if (step === 1) return { aiStylingUserInstruction: value }
  if (step === 2) return { storyboardPreviewUserInstruction: value }
  return { finalImageUserInstruction: value }
}

function stageInputSaveLabel(step: StudioStep) {
  if (step === 1) return '保存选搭要求'
  if (step === 2) return '保存分镜预览要求'
  return '保存成片要求'
}

function stageInstructionText(value?: Record<string, unknown> | null) {
  return stringValue(value?.instruction)
}

function StageUserInputModule({ step, task, value, savedValue = '', onChange, onSave, disabled = false }: {
  step: StudioStep
  task: GenerationTask | null
  value: string
  savedValue?: string
  onChange: (value: string) => void
  onSave?: () => void
  disabled?: boolean
}) {
  const labels = [
    { title: '', placeholder: '' },
    { title: '智能选搭要求', placeholder: '例如：不要厚重外套，改成更轻盈的通勤搭配。' },
    { title: '分镜预览要求', placeholder: '例如：分镜姿态更舒展，背景保持简洁。' },
    { title: '成片要求', placeholder: '例如：整体更干净，减少配饰存在感，保持棚拍电商质感。' },
  ][step]
  const saveDisabled = disabled || !task || value.trim() === savedValue.trim()
  const hasValue = Boolean(value.trim())
  return (
    <section className="side-input-module" aria-label={labels.title}>
      <header><span>{labels.title}</span><em>{hasValue ? '已填写' : '选填'}</em></header>
      <div className="side-input-control">
        <textarea aria-label={labels.title} value={value} maxLength={800} disabled={disabled} onChange={(event) => onChange(event.target.value)} placeholder={labels.placeholder} />
        {task && <p>保存后用于下一次执行，不会改变当前结果。</p>}
        <div className="side-input-actions">
          <em>{value.length}/800</em>
          {task && <button className="management-action" type="button" disabled={saveDisabled} onClick={onSave}>{stageInputSaveLabel(step)}</button>}
        </div>
      </div>
    </section>
  )
}

function StylingStep({ task, setTask, run, userInstruction, localOperations, startLocalOperation, clearLocalOperationsFor, isAdmin, onOpenAICallLogs }: {
  task: GenerationTask | null
  setTask: (task: GenerationTask) => void
  run: any
  userInstruction: string
  localOperations: AIOperation[]
  startLocalOperation: (operation: Omit<AIOperation, 'id' | 'status' | 'startedAt'> & { status?: string; startedAt?: string | null }) => AIOperation
  clearLocalOperationsFor: (ownerTypes: string[], taskId?: number | null) => void
  isAdmin: boolean
  onOpenAICallLogs: (title: string, ownerTypes: string[], attemptId: number) => void
}) {
  const locked = !!task && (isTaskRunning(task) || !!task.autoRunTarget)
  const hasHeroImages = (task?.images?.length ?? 0) > 0
  const canSelect = !!task && !locked && (['READY_FOR_STYLING', 'STYLING_READY', 'PREVIEW_READY', 'COMPLETED', 'FINAL_PARTIAL', 'FAILED', 'CANCELLED'].includes(task.status) || (task.status === 'DRAFT' && hasHeroImages))
  const hasStyling = !!task?.styling
  const operations = task ? operationFallbacks(task, localOperations, 'generation_styling') : []
  return (
    <div className="step-panel">
      <div className="step-scroll-content">
        <div className="step-copy"><HeadingWithTooltip as="h2" tooltip="理解主推款与商品卖点，评估多套造型方向，并选择最能突出主推款的搭配方案。">AI 智能选搭</HeadingWithTooltip></div>
        <div className="stage-priority-content">{['result', 'status'].map((section) => section === 'status' ? (
          <div className="stage-operation-status" key="status"><AIOperationList operations={operations} taskId={task?.id} ownerType="generation_styling" onOpenTechnicalDetails={isAdmin ? (operation) => onOpenAICallLogs(`AI 智能选搭 · Attempt #${operation.id}`, ['generation_styling'], operation.id) : undefined} /></div>
        ) : (
          <div className="stage-business-result" key="result">{task?.styling ? (
            <StylingPlanSummary task={task} />
          ) : <EmptyState title="等待智能选搭" text="点击按钮后会冻结当前主推款输入版本。" />}</div>
        ))}</div>
      </div>
      <div className="stage-action-bar">
        <button className="task-action" disabled={!canSelect} onClick={() => task && void run(hasStyling ? '重新选搭中...' : '智能选搭中...', () => {
          startLocalOperation({ taskId: task.id, ownerType: 'generation_styling', ownerId: task.id, provider: 'gemini' })
          const previousTask = task
          setTask({
            ...task,
            status: 'STYLING_RUNNING',
            preview: null,
            storyboardPreview: null,
            resultSet: null,
            activeStoryboardPreviewId: null,
            activePreviewId: null,
            activeResultSetId: null,
          })
          return api.selectStyling(task.id, userInstruction).catch((error) => {
            setTask(previousTask)
            throw error
          })
        }, (r: { item: GenerationTask }) => {
          clearLocalOperationsFor(['generation_styling'], task.id)
          setTask(r.item)
        }, { refresh: false })}>
          <WandSparkles size={16} /> {hasStyling ? '重新选搭' : 'AI 智能选搭'}
        </button>
      </div>
    </div>
  )
}

function StylingPlanSummary({ task }: { task: GenerationTask }) {
  const styling = task.styling
  if (!styling) return null
  const plan = styling.stylingPlan ?? {}
  const hero = (styling.heroUnderstanding ?? planValue<Record<string, unknown>>(plan, 'hero_understanding') ?? {}) as Record<string, unknown>
  const candidates = styling.outfitCandidates?.length ? styling.outfitCandidates : planValue<Record<string, unknown>[]>(plan, 'outfit_candidates') ?? []
  const selectedOutfitKey = stringValue(planValue(plan, 'selected_outfit_key')) || stringValue(planValue(plan, 'selected_outfit_id'))
  const selected = candidates.find((item) => (stringValue(item.outfit_key) || stringValue(item.id)) === selectedOutfitKey) ?? candidates[0]
  const scores = (styling.criticScores && Object.keys(styling.criticScores).length ? styling.criticScores : planValue<Record<string, unknown>>(selected ?? {}, 'scores') ?? {}) as Record<string, unknown>
  const generatedAccessories = stringList(planValue(selected ?? {}, 'generated_accessories'))
  const heroFocus = stringList(planValue(selected ?? {}, 'hero_focus'))
  const aestheticDiagnosis = diagnosisItems(planValue<Record<string, unknown>>(selected ?? {}, 'aesthetic_diagnosis') ?? {})
  const rejected = styling.rejectedCandidates?.length ? styling.rejectedCandidates : planValue<Record<string, unknown>[]>(plan, 'rejected_candidates') ?? []
  const materialCodesById = materialCodeMap(styling, candidates, plan)
  const requirement = (styling.userInstruction ?? styling.userRequirement ?? {}) as Record<string, unknown>
  const requirementText = stringValue(requirement.instruction ?? requirement.upload_requirement ?? requirement.combined_requirement)
  const requirementResolution = (styling.userRequirementResolution && Object.keys(styling.userRequirementResolution).length ? styling.userRequirementResolution : planValue<Record<string, unknown>>(plan, 'user_requirement_resolution') ?? {}) as Record<string, unknown>
  const requirementItems = [
    ...stringList(requirementResolution.satisfied).map((item) => `已满足：${item}`),
    ...stringList(requirementResolution.not_satisfied).map((item) => `未满足：${item}`),
    ...stringList(requirementResolution.conflicts).map((item) => `冲突：${item}`),
    ...stringList(requirementResolution.alternative_handling).map((item) => `替代：${item}`),
  ]
  return (
    <div className="styling-plan">
      <div className="styling-plan-head">
        <div>
          <span className="result-verdict"><Check size={14} />推荐造型方案</span>
          <h3>{stringValue(planValue(selected ?? {}, 'title')) || stringValue(planValue(selected ?? {}, 'mode')) || '主推款优先'}</h3>
        </div>
        <div className="score-strip">
          <ScorePill label="审美" value={scores.aesthetic_score} />
          <ScorePill label="品牌" value={scores.brand_fit_score} />
          <ScorePill label="主推可见" value={scores.hero_visibility_score} />
          <ScorePill label="商业可用" value={scores.commercial_readiness_score} />
        </div>
      </div>
      <p>{styling.reasonSummary || stringValue(planValue(plan, 'reason_summary'))}</p>
      <div className="selected-strip">
        {styling.selectedMaterials.length ? styling.selectedMaterials.map((item) => <GarmentCard key={item.id} item={item} />) : <EmptyState title="无需库内搭配服装" text="智能选搭判断本次以主推款单穿或生成配饰辅助更合适。" />}
      </div>
      {requirementText && (
        <div className="requirement-summary">
          <span>用户介入要求</span>
          <strong>{requirementText}</strong>
          {requirementItems.length > 0 && requirementItems.slice(0, 5).map((item, index) => <em key={`${item}-${index}`}>{item}</em>)}
        </div>
      )}
      <div className="styling-plan-grid primary-review-grid">
        <InfoBlock title="主推款理解" items={[stringValue(hero.hero_visual_summary), ...stringList(hero.hero_selling_points)].filter(Boolean)} />
        <InfoBlock title="图位重点" items={heroFocus.length ? heroFocus : stringList(hero.hero_selling_points)} />
        <InfoBlock title="风险提示" items={[...stringList(hero.hero_visibility_risks), ...(styling.warnings ?? [])].filter(Boolean)} empty="暂无明显风险" />
      </div>
      <details className="result-details">
        <summary>查看审美诊断、生成配饰与未采用方案</summary>
        <div className="styling-plan-grid secondary-review-grid">
          <InfoBlock title="审美诊断" items={aestheticDiagnosis} empty="暂无审美诊断" />
          <InfoBlock title="生成配饰" items={generatedAccessories.length ? generatedAccessories : ['低存在感鞋包饰品']} />
        </div>
        {rejected.length > 0 && <div className="rejected-list"><strong>未采用方案</strong>{rejected.slice(0, 3).map((item, index) => {
          const reason = userFacingMaterialText(stringValue(item.reason), materialCodesById)
          const codes = rejectedMaterialCodes(item, materialCodesById)
          const prefix = codes.length && !codes.some((code) => reason.includes(code)) ? codes.join('、') : ''
          return <span key={`${stringValue(item.outfit_key) || stringValue(item.id)}-${index}`}>{[prefix, reason].filter(Boolean).join(' · ')}</span>
        })}</div>}
      </details>
    </div>
  )
}

function InfoBlock({ title, items, empty = '暂无' }: { title: string; items: string[]; empty?: string }) {
  const displayItems = items.filter(Boolean)
  return <div className="info-block"><span>{title}</span>{displayItems.length ? displayItems.slice(0, 4).map((item, index) => <strong key={`${item}-${index}`}>{item}</strong>) : <em>{empty}</em>}</div>
}

function ScorePill({ label, value }: { label: string; value: unknown }) {
  const number = typeof value === 'number' ? value : Number(value)
  const verdict = scoreVerdict(number)
  return <span><b>{verdict}</b>{label}</span>
}

function scoreVerdict(value: number) {
  if (!Number.isFinite(value)) return '待看'
  if (value >= 0.82) return '成立'
  if (value >= 0.68) return '可用'
  return '复核'
}

function planValue<T = unknown>(source: Record<string, unknown>, key: string): T | undefined {
  return source[key] as T | undefined
}

function stringValue(value: unknown) {
  if (typeof value === 'string') return value
  if (typeof value === 'number') return String(value)
  return ''
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : []
}

function materialCodeMap(styling: NonNullable<GenerationTask['styling']>, candidates: Record<string, unknown>[], plan: Record<string, unknown>) {
  const codesById = new Map<string, string>()
  const add = (id: unknown, code: unknown) => {
    const idText = stringValue(id)
    const codeText = stringValue(code)
    if (idText && codeText) codesById.set(idText, codeText)
  }
  styling.selectedMaterials.forEach((item) => add(item.id, item.code))
  arrayValue<Record<string, unknown>>(plan, 'selected_materials').forEach((item) => add(item.material_id ?? item.id, item.material_code ?? item.code))
  candidates.forEach((candidate) => {
    arrayValue<Record<string, unknown>>(candidate, 'selected_materials').forEach((item) => add(item.material_id ?? item.id, item.material_code ?? item.code))
    const ids = Array.isArray(candidate.selected_material_ids) ? candidate.selected_material_ids : []
    const codes = Array.isArray(candidate.selected_material_codes) ? candidate.selected_material_codes : []
    ids.forEach((id, index) => add(id, codes[index]))
  })
  return codesById
}

function rejectedMaterialCodes(item: Record<string, unknown>, codesById: Map<string, string>) {
  const explicitCodes = stringList(item.material_codes ?? item.selected_material_codes)
  const materialCodes = arrayValue<Record<string, unknown>>(item, 'selected_materials')
    .map((ref) => stringValue(ref.material_code ?? ref.code))
    .filter(Boolean)
  const ids = Array.isArray(item.selected_material_ids) ? item.selected_material_ids : []
  const mappedCodes = ids.map((id) => codesById.get(String(id))).filter((code): code is string => Boolean(code))
  return Array.from(new Set([...explicitCodes, ...materialCodes, ...mappedCodes]))
}

function userFacingMaterialText(text: string, codesById: Map<string, string>) {
  let result = text
  for (const [id, code] of codesById.entries()) {
    const escapedId = escapeRegExp(id)
    result = result.replace(new RegExp(`\\b(?:material[_\\s-]*id|服饰ID|商品ID|ID)\\s*[:：#-]?\\s*${escapedId}(?![A-Za-z0-9])`, 'gi'), code)
    result = result.replace(new RegExp(`(库内(?:针织衫|衬衫|外套|西装|上衣|下装|裤装|半裙|连衣裙|搭配服装|服饰|单品)|皮革马甲)\\s*${escapedId}(?![A-Za-z0-9])`, 'g'), `$1 ${code} `)
  }
  result = result.replace(/\b(?:material[_\s-]*id|服饰ID|商品ID|ID)\s*[:：#-]?\s*\d+(?![A-Za-z0-9])/gi, '该服装')
  result = result.replace(/(库内(?:针织衫|衬衫|外套|西装|上衣|下装|裤装|半裙|连衣裙|搭配服装|服饰|单品)|皮革马甲)\s*\d+(?![A-Za-z0-9])/g, '$1')
  return result
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function arrayValue<T = unknown>(source: unknown, key: string): T[] {
  if (!source || typeof source !== 'object') return []
  const value = (source as Record<string, unknown>)[key]
  return Array.isArray(value) ? value.filter((item): item is T => typeof item === 'object' && item !== null) : []
}

function numberValue(value: unknown) {
  const number = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(number) ? number : 0
}

function diagnosisItems(value: Record<string, unknown>): string[] {
  const labels: Record<string, string> = {
    color_weight: '色彩',
    silhouette_balance: '廓形',
    waistline_proportion: '比例',
    season_material_fit: '材质',
    template_risk: '模板',
    brand_read: '品牌',
  }
  return Object.entries(labels).map(([key, label]) => {
    const text = stringValue(value[key])
    return text ? `${label}: ${text}` : ''
  }).filter(Boolean)
}

 function PreviewStep({ task, setTask, run, userInstruction, localOperations, startLocalOperation, clearLocalOperationsFor, isAdmin, onOpenAICallLogs }: {
  task: GenerationTask | null
  setTask: (task: GenerationTask) => void
  run: any
  userInstruction: string
  localOperations: AIOperation[]
  startLocalOperation: (operation: Omit<AIOperation, 'id' | 'status' | 'startedAt'> & { status?: string; startedAt?: string | null }) => AIOperation
  clearLocalOperationsFor: (ownerTypes: string[], taskId?: number | null) => void
  isAdmin: boolean
  onOpenAICallLogs: (title: string, ownerTypes: string[], attemptId: number) => void
}) {
  const [viewer, setViewer] = useState<LightboxImage | null>(null)
  const preview = task?.storyboardPreview ?? task?.preview ?? null
  const canPreview = (
    !!task?.styling && !isTaskLockedForGeneration(task) && ['STYLING_READY', 'PREVIEW_READY', 'COMPLETED', 'FINAL_PARTIAL', 'CANCELLED'].includes(task?.status ?? '')
  ) || (!!task && !isTaskLockedForGeneration(task) && task.status === 'FAILED' && !!task.styling && !preview?.asset)
  const hasPreview = !!preview?.asset
  const operations = task ? operationFallbacks(task, localOperations, 'generation_preview') : []
  return (
    <div className="step-panel">
      <div className="step-scroll-content">
        <div className="step-copy"><HeadingWithTooltip as="h2" tooltip="按主推款类型与选搭方案生成 3:2 六格分镜；前 5 格用于生成成片，第 6 格仅作备用参考。">生成分镜预览</HeadingWithTooltip></div>
        <div className="stage-priority-content">{['result', 'status'].map((section) => section === 'status' ? (
          <div className="stage-operation-status" key="status"><AIOperationList operations={operations} taskId={task?.id} ownerType="generation_preview" onOpenTechnicalDetails={isAdmin ? (operation) => onOpenAICallLogs(`生成分镜预览 · Attempt #${operation.id}`, ['generation_preview'], operation.id) : undefined} /></div>
        ) : (
          <div className="stage-business-result" key="result">{preview?.asset ? (
            <div className="storyboard-review"><button className="preview-board-open" onClick={() => preview.asset && setViewer(assetLightboxImage(preview.asset))} title="放大查看"><img className="preview-board" src={mediaUrl(assetPreviewSrc(preview.asset))} loading="lazy" alt="分镜预览" /></button><div className="storyboard-legend"><span><strong>1–5</strong>生成成片</span><span><strong>6</strong>备用参考，不生成</span><em>点击图片放大检查姿势、构图与光线</em></div></div>
          ) : <EmptyState title="尚未生成分镜预览" text="点击生成分镜预览后，系统会根据主推款品类与选搭方案输出一张 3:2 横版分镜预览图。" />}</div>
        ))}</div>
      </div>
      <div className="stage-action-bar">
        <button className="task-action" disabled={!canPreview} onClick={() => task && void run(hasPreview ? '重新生成分镜预览中...' : '生成分镜预览中...', () => {
          startLocalOperation({ taskId: task.id, ownerType: 'generation_preview', ownerId: task.id, provider: 'image-2' })
          setTask({ ...task, status: 'PREVIEW_RUNNING', resultSet: null, activeResultSetId: null })
          return api.previewTask(task.id, userInstruction)
        }, (r: { item: GenerationTask }) => {
          clearLocalOperationsFor(['generation_preview'], task.id)
          setTask(r.item)
        }, { refresh: false })}>
          <GalleryVerticalEnd size={16} /> {hasPreview ? '重新生成分镜预览' : '生成分镜预览'}
        </button>
      </div>
      {viewer && <ImageLightbox image={viewer} onClose={() => setViewer(null)} />}
    </div>
  )
}

function FinalStep({ task, setTask, run, userInstruction, localOperations, startLocalOperation, clearLocalOperationsFor, isAdmin, onOpenAICallLogs }: {
  task: GenerationTask | null
  setTask: (task: GenerationTask) => void
  run: any
  userInstruction: string
  localOperations: AIOperation[]
  startLocalOperation: (operation: Omit<AIOperation, 'id' | 'status' | 'startedAt'> & { status?: string; startedAt?: string | null }) => AIOperation
  clearLocalOperationsFor: (ownerTypes: string[], taskId?: number | null) => void
  isAdmin: boolean
  onOpenAICallLogs: (title: string, ownerTypes: string[], attemptId: number) => void
}) {
  const [viewer, setViewer] = useState<LightboxImage | null>(null)
  const images = task?.resultSet?.images ?? []
  const finalAssets = images.map((image) => image.asset).filter((asset): asset is MediaAsset => !!asset)
  const preview = task?.storyboardPreview ?? task?.preview ?? null
  const canGenerate = !!preview?.asset && !!task && !isTaskLockedForGeneration(task) && ['PREVIEW_READY', 'COMPLETED', 'FINAL_PARTIAL', 'CANCELLED'].includes(task?.status ?? '')
  const hasFinalImages = images.some((image) => !!image.asset)
  const finalHistoryOperations = task ? operationFallbacks(task, localOperations, 'generation_result_image') : []
  const finalSlots = Array.from({ length: 5 }, (_, index) => images.find((image) => image.sortOrder === index + 1) ?? null)
  return (
    <div className="step-panel">
      <div className="step-scroll-content">
        <div className="step-copy"><HeadingWithTooltip as="h2" tooltip="并发生成分镜前 5 个镜头；可逐张检查、下载或重新生成，也可打包下载全部成片。">生成成片</HeadingWithTooltip></div>
        <div className="stage-priority-content">{['result', 'status'].map((section) => section === 'status' ? (
          <div className="stage-operation-status" key="status">
            <AIOperationList operations={finalHistoryOperations} taskId={task?.id} ownerType="generation_result_image" onOpenTechnicalDetails={isAdmin ? (operation) => onOpenAICallLogs(`生成成片 · Attempt #${operation.id}`, ['generation_result_image'], operation.id) : undefined} />
          </div>
        ) : (
          <div className="stage-business-result" key="result"><div className="result-grid">
          {finalSlots.map((image, index) => (
            <div className="result-card" key={image?.id ?? `slot-${index + 1}`}>
              {image?.asset ? (
                <button className="result-image-open" onClick={() => image.asset && setViewer(assetLightboxImage(image.asset, finalAssets))} title="放大查看">
                  <img src={mediaUrl(assetPreviewSrc(image.asset))} loading="lazy" alt={`最终成片 ${image.sortOrder}`} />
                </button>
              ) : (
                <div className="image-placeholder">
                  {image ? (image.activeAttempt ? attemptStatusText(image.activeAttempt) : statusText(image.status)) : '等待生成'}
                  {image?.activeAttempt && isAttemptRunning(image.activeAttempt) && (
                    <small>
                      {(() => {
                        const phase = currentAttemptPhase(image.activeAttempt)
                        const total = phase
                          ? formatDuration(attemptPhaseDurationSeconds(phase, image.activeAttempt))
                          : formatDuration(elapsedSeconds(image.activeAttempt.startedAt || image.updatedAt))
                        return phase ? `${attemptPhaseText(phase, image.activeAttempt)} · ${total}` : total
                      })()}
                    </small>
                  )}
                  {image?.activeAttempt && !isAttemptRunning(image.activeAttempt) && (() => {
                    const note = attemptFriendlyFailure(image.activeAttempt)
                    return note ? <small className="image-placeholder-failure">{note}</small> : null
                  })()}
                </div>
              )}
              <div><TruncatedText text={`#${index + 1} ${image?.shotRole || '镜头'}`}><b>#{index + 1}</b>{image?.shotRole || '镜头'}</TruncatedText>{image?.asset && <a className="management-icon-action" href={mediaDownloadUrl(assetOriginalSrc(image.asset))} aria-label={`下载镜头 ${index + 1}`}><Download size={15} /></a>}{image?.status === 'FAILED' && image.activeAttempt?.errorCode === 'DELIVERY_FAILED' && <button className="management-icon-action" disabled={!!task && isTaskLockedForGeneration(task)} onClick={() => void run('重新回传成片中...', () => api.redeliverResultImage(image.id), (r: { item: GenerationTask }) => setTask(r.item), { refresh: 'background' })} title="重新回传（图已生成，仅重新拉取，不消耗生成额度）" aria-label={`重新回传镜头 ${index + 1}`}><RefreshCw size={14} /></button>}{image && !['QUEUED', 'RUNNING'].includes(image.status) && <button className="task-icon-action" disabled={!!task && isTaskLockedForGeneration(task)} onClick={() => void run('单张重新生成中...', () => api.retryResultImage(image.id, userInstruction), (r: { item: GenerationTask }) => setTask(r.item), { refresh: 'background' })} title="重新生成" aria-label={`重新生成镜头 ${index + 1}`}><RotateCcw size={14} /></button>}</div>
            </div>
          ))}
          </div></div>
        ))}</div>
      </div>
      <div className="stage-action-bar">
        {hasFinalImages && task && (
          <a className="management-action button-link" href={finalImageArchiveUrl([task.id])} rel="noreferrer" title="打包下载本任务全部成片，未完成项记录在清单.txt">
            <Download size={16} /> 下载全部成片
          </a>
        )}
        <button className="task-action" disabled={!canGenerate} onClick={() => task && void run(hasFinalImages ? '重新生成最终成片中...' : '生成最终成片中...', () => {
          for (let index = 1; index <= 5; index += 1) {
            startLocalOperation({ taskId: task.id, ownerType: 'generation_result_image', ownerId: index, provider: 'image-2' })
          }
          setTask({ ...task, status: 'FINAL_RUNNING' })
          return api.generateFinal(task.id, userInstruction)
        }, (r: { item: GenerationTask }) => {
          clearLocalOperationsFor(['generation_result_image'], task.id)
          setTask(r.item)
        }, { refresh: false })}>
          <Sparkles size={16} /> {hasFinalImages ? '重新生成成片' : '生成成片'}
        </button>
      </div>
      {viewer && <ImageLightbox image={viewer} onClose={() => setViewer(null)} />}
    </div>
  )
}

function LibraryPage({ garments, run, role, onDeleteResult, uploadSessions, refreshUploadSessions, markBrowserUploading, showImport, setShowImport, importJobs, setImportJobs, syncImportJobs }: {
  garments: StylingGarment[]
  run: any
  role: SessionRole | null
  onDeleteResult: (result: DeleteOperationResult) => void
  uploadSessions: UploadSession[]
  refreshUploadSessions: () => Promise<UploadSession[]>
  markBrowserUploading: (delta: 1 | -1) => void
  showImport: boolean
  setShowImport: Dispatch<SetStateAction<boolean>>
  importJobs: ImportJob[]
  setImportJobs: Dispatch<SetStateAction<ImportJob[]>>
  syncImportJobs: Dispatch<SetStateAction<ImportJob[]>>
}) {
  const [query, setQuery] = useState('')
  const [detail, setDetail] = useState<StylingGarment | null>(null)
  const [items, setItems] = useState<StylingGarment[]>([])
  const [importPanelMounted, setImportPanelMounted] = useState(showImport)
  const [offset, setOffset] = useState(0)
  const [total, setTotal] = useState(0)
  const [totalPages, setTotalPages] = useState(0)
  const [facets, setFacets] = useState<StylingGarmentFacets>({})
  const [stableFacets, setStableFacets] = useState<StylingGarmentFacets>({})
  const [statusFilter, setStatusFilter] = useState('')
  const [primaryCategoryFilter, setPrimaryCategoryFilter] = useState<string[]>([])
  const [secondaryCategoryFilter, setSecondaryCategoryFilter] = useState<string[]>([])
  const [uploadedFrom, setUploadedFrom] = useState('')
  const [uploadedTo, setUploadedTo] = useState('')
  const [sortBy, setSortBy] = useState('createdAt')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [openFilter, setOpenFilter] = useState<'category' | 'date' | 'sort' | 'status' | 'importJob' | null>(null)
  const [importJobFilter, setImportJobFilter] = useState('')
  const [focusedGarmentCode, setFocusedGarmentCode] = useState('')
  const [focusedGarmentId, setFocusedGarmentId] = useState<number | null>(null)
  const [pageSize, setPageSize] = useState<number>(libraryPageSizeOptions[0])
  const [searching, setSearching] = useState(true)
  const [showSearchBusy, setShowSearchBusy] = useState(false)
  const [includeArchived, setIncludeArchived] = useState(false)
  const [bulkMode, setBulkMode] = useState(false)
  const [selectedGarmentIds, setSelectedGarmentIds] = useState<Set<number>>(() => new Set())
  const [deleteGarmentDialog, setDeleteGarmentDialog] = useState<StylingGarment[] | null>(null)
  const [deleteGarmentImpact, setDeleteGarmentImpact] = useState<DeleteImpact | null>(null)
  const [deleteGarmentImpactLoading, setDeleteGarmentImpactLoading] = useState(false)
  const refreshUploadSessionsRef = useRef(refreshUploadSessions)
  const selectableGarmentIds = items.map((item) => item.id)
  const selectedVisibleGarments = items.filter((item) => selectedGarmentIds.has(item.id) && selectableGarmentIds.includes(item.id))
  const selectedArchivedGarments = selectedVisibleGarments.filter((item) => item.archivedAt)
  const selectedActiveGarments = selectedVisibleGarments.filter((item) => !item.archivedAt)
  const selectedTaggingGarments = selectedActiveGarments.filter((item) => item.status === 'TAGGING_RUNNING')
  const selectedTaggableGarments = selectedActiveGarments.filter((item) => TAGGABLE_GARMENT_STATUSES.has(item.status))
  const selectedRetaggableGarments = selectedActiveGarments.filter((item) => RETAGGABLE_GARMENT_STATUSES.has(item.status))
  const allVisibleGarmentsSelected = selectableGarmentIds.length > 0 && selectableGarmentIds.every((id) => selectedGarmentIds.has(id))
  const garmentMarquee = useMarqueeSelection<number>({
    enabled: bulkMode,
    selected: selectedGarmentIds,
    onChange: setSelectedGarmentIds,
  })
  const lastSelectedGarmentIdRef = useRef<number | null>(null)
  const importBatchOptions = useMemo(() => importJobs
    .map((job) => {
      const codes = Array.from(new Set(job.items.map((item) => item.code).filter(Boolean)))
      return {
        value: job.id,
        label: importJobDisplayTitle(job, []),
        count: codes.length,
        codes,
        updatedAt: job.updatedAt ?? job.createdAt,
      }
    })
    .filter((option) => option.codes.length > 0)
    .sort((a, b) => new Date(parseRuyiTime(b.updatedAt)).getTime() - new Date(parseRuyiTime(a.updatedAt)).getTime()), [importJobs])
  const selectedImportBatch = importBatchOptions.find((option) => option.value === importJobFilter)
  const selectedImportCodes = useMemo(() => new Set(selectedImportBatch?.codes ?? []), [selectedImportBatch])

  useEffect(() => {
    refreshUploadSessionsRef.current = refreshUploadSessions
  }, [refreshUploadSessions])

  useEffect(() => {
    if (!showImport) return
    void refreshUploadSessionsRef.current().catch(() => undefined)
  }, [showImport])

  async function openDetail(item: StylingGarment) {
    const result = await api.getStylingGarment(item.id)
    updateGarmentInView(result.item)
    setDetail(result.item)
  }

  function libraryQueryOptions(targetOffset = offset) {
    const keyword = query.trim()
    const importKeyword = selectedImportBatch?.codes.join(' ')
    const exactCodeKeyword = focusedGarmentCode.trim()
    return {
      limit: selectedImportBatch ? Math.max(100, selectedImportBatch.codes.length, pageSize) : focusedGarmentCode ? Math.max(100, pageSize) : pageSize,
      offset: selectedImportBatch || focusedGarmentCode || focusedGarmentId ? 0 : targetOffset,
      keyword: selectedImportBatch ? importKeyword : exactCodeKeyword || keyword || undefined,
      includeArchived,
      status: statusFilter ? [statusFilter] : undefined,
      primaryCategory: primaryCategoryFilter.length ? primaryCategoryFilter : undefined,
      secondaryCategory: secondaryCategoryFilter.length ? secondaryCategoryFilter : undefined,
      uploadedFrom: uploadedFrom || undefined,
      uploadedTo: uploadedTo || undefined,
      sortBy,
      sortDir,
    }
  }

  function refreshLibraryPage(targetOffset = offset) {
    return api.listStylingGarments(libraryQueryOptions(targetOffset)).then((result) => {
      if (!selectedImportBatch && !focusedGarmentCode && !focusedGarmentId && !result.items.length && result.total > 0 && targetOffset > 0) {
        const fallbackOffset = Math.max(0, Math.floor((result.total - 1) / pageSize) * pageSize)
        setOffset(fallbackOffset)
        return api.listStylingGarments(libraryQueryOptions(fallbackOffset)).then((nextResult) => applyLibraryResult(nextResult, fallbackOffset))
      }
      applyLibraryResult(result, targetOffset)
    })
  }

  function applyLibraryResult(result: StylingGarmentListResponse, targetOffset = offset) {
    if (selectedImportBatch) {
      const queryTerms = splitSearchTerms(query).map((term) => term.toLocaleLowerCase())
      const filtered = result.items.filter((item) => selectedImportCodes.has(item.code) && garmentMatchesLibraryQuery(item, queryTerms))
      setItems(filtered.slice(targetOffset, targetOffset + pageSize))
      setTotal(filtered.length)
      setTotalPages(filtered.length ? Math.ceil(filtered.length / pageSize) : 0)
    } else if (focusedGarmentCode) {
      const filtered = result.items.filter((item) => item.code === focusedGarmentCode)
      setItems(filtered.slice(targetOffset, targetOffset + pageSize))
      setTotal(filtered.length)
      setTotalPages(filtered.length ? Math.ceil(filtered.length / pageSize) : 0)
    } else {
      setItems(result.items)
      setTotal(result.total)
      setTotalPages(result.totalPages)
    }
    setFacets(result.facets ?? {})
    setStableFacets(result.stableFacets ?? result.facets ?? {})
  }

  function refreshFirstLibraryPage() {
    setOffset(0)
    return refreshLibraryPage(0)
  }

  useEffect(() => {
    setOffset(0)
  }, [query, includeArchived, statusFilter, primaryCategoryFilter, secondaryCategoryFilter, uploadedFrom, uploadedTo, sortBy, sortDir, importJobFilter, focusedGarmentCode, focusedGarmentId])

  useEffect(() => {
    if (!searching) {
      setShowSearchBusy(false)
      return
    }
    const timer = window.setTimeout(() => setShowSearchBusy(true), 350)
    return () => window.clearTimeout(timer)
  }, [searching])

  useEffect(() => {
    let cancelled = false
    setSearching(true)
    const timer = window.setTimeout(() => {
      if (focusedGarmentId) {
        void api.getStylingGarment(focusedGarmentId).then((result) => {
          if (!cancelled) {
            setItems([result.item])
            setTotal(1)
            setTotalPages(1)
          }
        }).catch(() => api.listStylingGarments({
          ...libraryQueryOptions(0),
          limit: Math.max(100, pageSize),
          offset: 0,
          keyword: focusedGarmentCode || query.trim() || undefined,
          includeArchived: true,
        }).then((result) => {
          if (cancelled) return
          const filtered = focusedGarmentCode ? result.items.filter((item) => item.code === focusedGarmentCode) : result.items
          setItems(filtered.slice(0, pageSize))
          setTotal(filtered.length)
          setTotalPages(filtered.length ? Math.ceil(filtered.length / pageSize) : 0)
        })).catch(() => {
          if (!cancelled) {
            setItems([])
            setTotal(0)
            setTotalPages(0)
          }
        }).finally(() => {
          if (!cancelled) setSearching(false)
        })
        return
      }
      void api.listStylingGarments(libraryQueryOptions(offset)).then((result) => {
        if (!cancelled) {
          applyLibraryResult(result, offset)
        }
      }).catch(() => undefined).finally(() => {
        if (!cancelled) setSearching(false)
      })
    }, 250)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [offset, query, includeArchived, statusFilter, primaryCategoryFilter, secondaryCategoryFilter, uploadedFrom, uploadedTo, sortBy, sortDir, pageSize, importJobFilter, selectedImportBatch, focusedGarmentCode, focusedGarmentId])

  useEffect(() => {
    setItems((current) => current.map((item) => garments.find((latest) => latest.id === item.id) ?? item))
  }, [garments])

  useEffect(() => setSelectedGarmentIds(new Set()), [offset, includeArchived, query, statusFilter, primaryCategoryFilter, secondaryCategoryFilter, uploadedFrom, uploadedTo, sortBy, sortDir, pageSize, importJobFilter, focusedGarmentCode, focusedGarmentId])

  useEffect(() => {
    if (!detail) return
    const latest = garments.find((item) => item.id === detail.id)
    if (latest && (latest.status !== detail.status || latest.code !== detail.code || latest.updatedAt !== detail.updatedAt)) void openDetail(latest)
  }, [garments, detail?.id, detail?.status, detail?.code, detail?.updatedAt])

  function toggleGarmentSelection(id: number, checked: boolean, extend = false) {
    setSelectedGarmentIds((current) => {
      const next = new Set(current)
      if (extend && lastSelectedGarmentIdRef.current !== null) {
        const start = selectableGarmentIds.indexOf(lastSelectedGarmentIdRef.current)
        const end = selectableGarmentIds.indexOf(id)
        if (start >= 0 && end >= 0) {
          selectableGarmentIds.slice(Math.min(start, end), Math.max(start, end) + 1).forEach((rangeId) => {
            if (checked) next.add(rangeId)
            else next.delete(rangeId)
          })
          lastSelectedGarmentIdRef.current = id
          return next
        }
      }
      if (checked) next.add(id)
      else next.delete(id)
      lastSelectedGarmentIdRef.current = id
      return next
    })
  }

  function toggleVisibleGarmentSelection(checked: boolean) {
    setSelectedGarmentIds((current) => {
      const next = new Set(current)
      selectableGarmentIds.forEach((id) => {
        if (checked) next.add(id)
        else next.delete(id)
      })
      return next
    })
  }

  function confirmGarmentDelete(targets: DeleteDialogTarget[], purge: boolean, confirmText: string) {
    const ids = targets.map((item) => item.id)
    const action = purge
      ? () => ids.length === 1 ? api.deleteStylingGarment(ids[0], { purge: true, confirmCode: confirmText }) : api.deleteStylingGarments(ids, { purge: true, confirmPhrase: confirmText })
      : () => api.deleteStylingGarments(ids)
    setDeleteGarmentDialog(null)
    setDeleteGarmentImpact(null)
    setItems((current) => current.filter((item) => !ids.includes(item.id)))
    void run(purge ? '彻底删除搭配服装中...' : '归档搭配服装中...', action, (result: DeleteOperationResult) => {
      onDeleteResult(result)
      if (detail && ids.includes(detail.id)) setDetail(null)
      setSelectedGarmentIds(new Set())
      void refreshLibraryPage().catch(() => undefined)
    }, { refresh: 'background' })
  }

  function restoreGarments(targets: StylingGarment[]) {
    const ids = targets.map((item) => item.id)
    void run(ids.length === 1 ? '恢复搭配服装中...' : `恢复 ${ids.length} 套搭配服装中...`, () => api.restoreStylingGarments(ids), () => {
      setDetail((current) => current && ids.includes(current.id) ? { ...current, archivedAt: null, archiveReason: null } : current)
      setSelectedGarmentIds(new Set())
      void refreshLibraryPage().catch(() => undefined)
    }, { refresh: 'background' })
  }

  function tagGarments(targets: StylingGarment[], action: 'tag' | 'retag' = 'tag') {
    const ids = targets.map((item) => item.id)
    if (!ids.length) return
    const label = action === 'retag' ? '重新识别' : '识别'
    void run(ids.length === 1 ? `提交${label}中...` : `提交 ${ids.length} 套搭配服装${label}中...`, () => api.tagStylingGarments(ids), (result: { items: StylingGarment[] }) => {
      result.items.forEach(updateGarmentInView)
      setSelectedGarmentIds(new Set())
    }, { refresh: 'background' })
  }

  function downloadGarments(targets: StylingGarment[]) {
    const unavailable = targets.filter((item) => Number(item.imageCount ?? 0) < 1)
    if (unavailable.length) {
      window.alert(`以下货号没有可下载的图片：${unavailable.map((item) => item.code).join('、')}`)
      return
    }
    const ids = targets.map((item) => item.id)
    if (ids.length > STYLING_GARMENT_EXPORT_MAX_ITEMS) {
      window.alert(`单次最多下载 ${STYLING_GARMENT_EXPORT_MAX_ITEMS} 个货号，请减少选择后重试。`)
      return
    }
    const anchor = document.createElement('a')
    anchor.href = stylingGarmentArchiveUrl(ids)
    anchor.rel = 'noreferrer'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  }

  function updateGarmentInView(next: StylingGarment) {
    setItems((current) => current.map((item) => item.id === next.id ? next : item))
    if (detail?.id === next.id) setDetail(next)
  }

  function openGarmentDeleteDialog(targets: StylingGarment[]) {
    const ids = targets.map((item) => item.id)
    setDeleteGarmentDialog(targets)
    setDeleteGarmentImpact(null)
    setDeleteGarmentImpactLoading(true)
    void api.getStylingGarmentDeleteImpact(ids)
      .then((response) => setDeleteGarmentImpact(response.impact))
      .catch(() => setDeleteGarmentImpact(null))
      .finally(() => setDeleteGarmentImpactLoading(false))
  }

  function clearLibraryFilters() {
    setQuery('')
    setStatusFilter('')
    setPrimaryCategoryFilter([])
    setSecondaryCategoryFilter([])
    setUploadedFrom('')
    setUploadedTo('')
    setSortBy('createdAt')
    setSortDir('desc')
    setIncludeArchived(false)
    setImportJobFilter('')
    setFocusedGarmentCode('')
    setFocusedGarmentId(null)
  }

  function showGarmentFromImport(code: string, garmentId?: number) {
    setQuery(code)
    setStatusFilter('')
    setPrimaryCategoryFilter([])
    setSecondaryCategoryFilter([])
    setUploadedFrom('')
    setUploadedTo('')
    setSortBy('createdAt')
    setSortDir('desc')
    setIncludeArchived(true)
    setImportJobFilter('')
    setFocusedGarmentCode(code)
    setFocusedGarmentId(garmentId ?? null)
    setOffset(0)
    setDetail(null)
    setShowImport(false)
  }

  function showImportJobInLibrary(jobId: string) {
    setQuery('')
    setStatusFilter('')
    setPrimaryCategoryFilter([])
    setSecondaryCategoryFilter([])
    setUploadedFrom('')
    setUploadedTo('')
    setSortBy('createdAt')
    setSortDir('desc')
    setIncludeArchived(true)
    setImportJobFilter(jobId)
    setFocusedGarmentCode('')
    setFocusedGarmentId(null)
    setOffset(0)
    setDetail(null)
    setShowImport(false)
  }

  const categoryFilterCount = primaryCategoryFilter.length + secondaryCategoryFilter.length
  const hasActiveFilters = Boolean(query.trim() || statusFilter || categoryFilterCount || uploadedFrom || uploadedTo || includeArchived || sortBy !== 'createdAt' || sortDir !== 'desc' || importJobFilter)
  const stableCategoryTree = stableFacets.categoryTree ?? facets.categoryTree ?? []
  const importGarments = useMemo(() => {
    const byId = new Map<number, StylingGarment>()
    garments.forEach((item) => byId.set(item.id, item))
    items.forEach((item) => byId.set(item.id, item))
    return Array.from(byId.values())
  }, [garments, items])

  return (
    <section className="page workbench-page library-page">
      <div className="library-title-row">
        <PageTitle
          title="搭配服装库"
          subtitle="管理真实会进入智能选搭的服装资产，优先看商品、品类和可用状态。"
        />
        <button
          className="management-action"
          onClick={() => {
            if (!showImport) setImportPanelMounted(true)
            setShowImport((value) => !value)
          }}
        >
          {showImport ? <ChevronLeft size={16} /> : <ImagePlus size={16} />}
          {showImport ? '返回服装库' : '上传搭配服装'}
        </button>
      </div>
      <div className={`library-content-scroll ${showImport ? 'is-import' : 'is-list'}`}>
        {importPanelMounted && (
          <div hidden={!showImport}>
            <BatchImportPanel
              onClose={() => setShowImport(false)}
              onImported={() => void refreshFirstLibraryPage().catch(() => undefined)}
              uploadSessions={uploadSessions}
              refreshUploadSessions={refreshUploadSessions}
              markBrowserUploading={markBrowserUploading}
              garments={importGarments}
              importJobs={importJobs}
              setImportJobs={setImportJobs}
              syncImportJobs={syncImportJobs}
              onFindGarment={showGarmentFromImport}
              onFindJob={showImportJobInLibrary}
            />
          </div>
        )}
        {!showImport && (
        <div className="panel library-panel">
          <div className="library-commandbar">
            <SearchBox
              value={query}
              onChange={(value) => {
                setQuery(value)
                setFocusedGarmentCode('')
                setFocusedGarmentId(null)
              }}
              placeholder="搜索货号 / 标签，支持粘贴多个"
              panelPlaceholder="每行一个货号，或用空格、逗号、句号、顿号分隔"
              busy={showSearchBusy}
            />
            <div className="library-filter-pills">
              <CategoryTreeFilter
                open={openFilter === 'category'}
                onOpen={(open) => setOpenFilter(open ? 'category' : null)}
                options={stableCategoryTree}
                selectedPrimary={primaryCategoryFilter}
                selectedSecondary={secondaryCategoryFilter}
                onPrimary={setPrimaryCategoryFilter}
                onSecondary={setSecondaryCategoryFilter}
              />
              <ImportBatchFilter
                open={openFilter === 'importJob'}
                onOpen={(open) => setOpenFilter(open ? 'importJob' : null)}
                value={importJobFilter}
                options={importBatchOptions}
                onChange={(value) => {
                  setImportJobFilter(value)
                  setFocusedGarmentCode('')
                  setFocusedGarmentId(null)
                }}
              />
              <DateRangeFilter
                open={openFilter === 'date'}
                onOpen={(open) => setOpenFilter(open ? 'date' : null)}
                from={uploadedFrom}
                to={uploadedTo}
                onFrom={setUploadedFrom}
                onTo={setUploadedTo}
              />
              <SortFilter
                open={openFilter === 'sort'}
                onOpen={(open) => setOpenFilter(open ? 'sort' : null)}
                sortBy={sortBy}
                sortDir={sortDir}
                options={librarySortOptions}
                onChange={(nextSortBy, nextSortDir) => {
                  setSortBy(nextSortBy)
                  setSortDir(nextSortDir)
                }}
              />
              <StatusFilter
                open={openFilter === 'status'}
                onOpen={(open) => setOpenFilter(open ? 'status' : null)}
                value={statusFilter}
                options={stylingGarmentStatusOptions(facets.status, stableFacets.status, statusFilter)}
                onChange={setStatusFilter}
              />
              <button className={`filter-pill checkbox-pill ${includeArchived ? 'is-active' : ''}`} type="button" onClick={() => {
                setOffset(0)
                setIncludeArchived(!includeArchived)
              }}><Check size={15} /><span>查看已归档</span></button>
              {hasActiveFilters && <button className="filter-pill reset-filter-pill" type="button" onClick={clearLibraryFilters}><span>清空筛选</span></button>}
            </div>
            <div className="library-management-tools">
              {selectedVisibleGarments.length > 0 && (
                selectedArchivedGarments.length === selectedVisibleGarments.length
                  ? <button className="management-action" onClick={() => restoreGarments(selectedArchivedGarments)}>恢复 {selectedArchivedGarments.length} 项</button>
                  : <button className="management-action" disabled={selectedArchivedGarments.length > 0 || selectedTaggingGarments.length > 0} title={selectedArchivedGarments.length ? '请分别选择已归档和未归档服装' : selectedTaggingGarments.length ? '识别中的服装可以下载，但不能归档' : undefined} onClick={() => openGarmentDeleteDialog(selectedActiveGarments)}>归档 {selectedActiveGarments.length} 项</button>
              )}
              {bulkMode && selectedVisibleGarments.length > 0 && (
                <>
                  <button
                    className="management-action"
                    disabled={!selectedVisibleGarments.some((item) => Number(item.imageCount ?? 0) > 0)}
                    onClick={() => downloadGarments(selectedVisibleGarments)}
                  >
                    <Download size={15} /> 下载 {selectedVisibleGarments.length} 项
                  </button>
                  {selectedTaggableGarments.length > 0 && (
                    <button
                      className="management-action"
                      title="提交待识别、待复核或识别失败的服装进行 AI 识别"
                      onClick={() => tagGarments(selectedTaggableGarments, 'tag')}
                    >
                      <WandSparkles size={15} /> 识别 {selectedTaggableGarments.length} 项
                    </button>
                  )}
                  {selectedRetaggableGarments.length > 0 && (
                    <button
                      className="management-action"
                      title="对已可用服装重新进行 AI 识别"
                      onClick={() => tagGarments(selectedRetaggableGarments, 'retag')}
                    >
                      <RotateCcw size={15} /> 重新识别 {selectedRetaggableGarments.length} 项
                    </button>
                  )}
                </>
              )}
              {bulkMode && <button className={`management-action management-check-action ${allVisibleGarmentsSelected ? 'is-active' : ''}`} disabled={!selectableGarmentIds.length} onClick={() => toggleVisibleGarmentSelection(!allVisibleGarmentsSelected)}><Check size={15} /> 全选本页</button>}
              {bulkMode && <span className="bulk-selection-hint">拖动框选 · Shift 连选</span>}
              <button className="management-action compact-control" onClick={() => { setBulkMode((value) => !value); setSelectedGarmentIds(new Set()) }}>{bulkMode ? '退出批量' : '批量管理'}</button>
            </div>
          </div>
          <div
            ref={garmentMarquee.containerRef}
            className={`library-results marquee-selection-surface ${bulkMode ? 'is-bulk-selecting' : ''} ${showSearchBusy ? 'is-loading' : ''}`}
            onPointerDown={garmentMarquee.onPointerDown}
            onClickCapture={garmentMarquee.onClickCapture}
          >
            <MarqueeSelectionBox box={garmentMarquee.box} />
            {items.length ? (
              <div className="garment-grid">{items.map((item) => (
                <GarmentCard
                  key={item.id}
                  item={item}
                  onOpen={() => void openDetail(item)}
                  onDownload={() => downloadGarments([item])}
                  onTag={ANY_TAGGABLE_GARMENT_STATUSES.has(item.status) ? () => void run(item.status === 'READY' ? '重新识别标签中...' : '识别标签中...', () => api.tagStylingGarment(item.id), (r: { item: StylingGarment }) => updateGarmentInView(r.item), { refresh: 'background' }) : undefined}
                  tagLabel={item.status === 'READY' ? '重新识别' : '识别标签'}
                  selected={bulkMode && selectedGarmentIds.has(item.id)}
                  onSelect={bulkMode ? (checked, extend) => toggleGarmentSelection(item.id, checked, extend) : undefined}
                  onDelete={() => openGarmentDeleteDialog([item])}
                  onRestore={() => restoreGarments([item])}
                />
              ))}</div>
            ) : searching ? null : <EmptyState title={hasActiveFilters ? '没有匹配的搭配服装' : '暂无搭配服装'} text={hasActiveFilters ? '减少筛选条件，或粘贴多个货号、标签再试。' : '点击右上角上传搭配服装。'} />}
          </div>
          <div className="library-pagination-stick">
            <PaginationControls
              disabled={searching}
              offset={offset}
              pageSize={pageSize}
              pageSizeOptions={libraryPageSizeOptions}
              pageSizeUnit="套"
              total={total}
              totalPages={totalPages}
              onOffsetChange={setOffset}
              onPageSizeChange={(value) => {
                setPageSize(value)
                setOffset(0)
              }}
            />
          </div>
        </div>
        )}
      </div>
      {detail && <GarmentDetailDrawer item={detail} run={run} isAdmin={role === 'admin'} onClose={() => setDetail(null)} onChange={updateGarmentInView} onDelete={() => openGarmentDeleteDialog([detail])} onRestore={() => restoreGarments([detail])} />}
      {deleteGarmentDialog && (
        <DeleteConfirmDialog
          title={deleteGarmentDialog.length === 1 ? '归档搭配服装' : `归档 ${deleteGarmentDialog.length} 套搭配服装`}
          items={deleteGarmentDialog.map((item) => ({ id: item.id, name: item.code }))}
          onClose={() => setDeleteGarmentDialog(null)}
          onConfirm={confirmGarmentDelete}
          impact={deleteGarmentImpact}
          impactLoading={deleteGarmentImpactLoading}
        />
      )}
    </section>
  )
}

function CategoryTreeFilter({ open, onOpen, options, selectedPrimary, selectedSecondary, onPrimary, onSecondary }: {
  open: boolean
  onOpen: (open: boolean) => void
  options: CategoryTreeOption[]
  selectedPrimary: string[]
  selectedSecondary: string[]
  onPrimary: (value: string[]) => void
  onSecondary: (value: string[]) => void
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const [activePrimary, setActivePrimary] = useState('')
  const [categoryQuery, setCategoryQuery] = useState('')
  const normalizedCategoryQuery = categoryQuery.trim().toLocaleLowerCase()
  const visibleOptions = useMemo(() => {
    if (!normalizedCategoryQuery) return options
    return options.reduce<CategoryTreeOption[]>((result, option) => {
      const parentMatches = option.label.toLocaleLowerCase().includes(normalizedCategoryQuery)
      const children = option.children ?? []
      const visibleChildren = parentMatches
        ? children
        : children.filter((child) => child.label.toLocaleLowerCase().includes(normalizedCategoryQuery))
      if (parentMatches || visibleChildren.length) result.push({ ...option, children: visibleChildren })
      return result
    }, [])
  }, [normalizedCategoryQuery, options])
  const primary = visibleOptions.find((item) => item.value === activePrimary) ?? visibleOptions[0]
  const selectedLabels = categorySelectionLabels(options, selectedPrimary, selectedSecondary)
  const selectedCount = selectedLabels.length
  const selectedLabel = selectedCount
    ? selectedCount === 1 ? selectedLabels[0] : `${selectedLabels[0]} 等 ${selectedCount} 项`
    : '品类'
  useOutsideClose(open, wrapRef, () => onOpen(false))

  useEffect(() => {
    if (!open) {
      setCategoryQuery('')
      return
    }
    if (!visibleOptions.length) return
    if (!activePrimary || !visibleOptions.some((item) => item.value === activePrimary)) setActivePrimary(visibleOptions[0].value)
  }, [activePrimary, open, visibleOptions])

  function childValues(option?: CategoryTreeOption) {
    return (option?.children ?? []).map((item) => item.value)
  }

  function clearSecondaryValues(values: string[]) {
    return selectedSecondary.filter((item) => !values.includes(item))
  }

  function optionState(option: CategoryTreeOption) {
    const children = childValues(option)
    const parentSelected = selectedPrimary.includes(option.value)
    const selectedChildren = children.filter((value) => selectedSecondary.includes(value))
    const checked = parentSelected || (children.length > 0 && selectedChildren.length === children.length)
    const indeterminate = !checked && selectedChildren.length > 0
    return { checked, indeterminate, children, selectedChildren }
  }

  function togglePrimary(option: CategoryTreeOption, checked: boolean) {
    const children = childValues(option)
    if (checked) {
      onPrimary(Array.from(new Set([...selectedPrimary, option.value])))
      onSecondary(clearSecondaryValues(children))
      return
    }
    onPrimary(selectedPrimary.filter((item) => item !== option.value))
    onSecondary(clearSecondaryValues(children))
  }

  function toggleSecondary(value: string, checked: boolean) {
    if (!primary) return
    const children = childValues(primary)
    const siblingValues = children.filter((item) => item !== value)
    const nextPrimary = selectedPrimary.filter((item) => item !== primary.value)
    let nextSecondary = selectedPrimary.includes(primary.value)
      ? Array.from(new Set([...selectedSecondary, ...siblingValues]))
      : selectedSecondary
    nextSecondary = checked
      ? Array.from(new Set([...nextSecondary, value]))
      : nextSecondary.filter((item) => item !== value)
    const allChildrenSelected = children.length > 0 && children.every((item) => nextSecondary.includes(item))
    onPrimary(allChildrenSelected ? Array.from(new Set([...nextPrimary, primary.value])) : nextPrimary)
    onSecondary(allChildrenSelected ? nextSecondary.filter((item) => !children.includes(item)) : nextSecondary)
  }

  function toggleAllPrimary(checked: boolean) {
    if (checked) {
      const allPrimary = visibleOptions.map((item) => item.value)
      const allChildValues = visibleOptions.flatMap((item) => childValues(item))
      onPrimary(allPrimary)
      onSecondary(selectedSecondary.filter((item) => !allChildValues.includes(item)))
      return
    }
    onPrimary([])
    onSecondary([])
  }

  function toggleActiveChildren(checked: boolean) {
    if (!primary) return
    togglePrimary(primary, checked)
  }

  const primaryStates = visibleOptions.map((option) => optionState(option))
  const allPrimarySelected = visibleOptions.length > 0 && primaryStates.every((state) => state.checked)
  const anyPrimarySelected = primaryStates.some((state) => state.checked || state.indeterminate)
  const activeState = primary ? optionState(primary) : { checked: false, indeterminate: false, children: [], selectedChildren: [] }

  return (
    <div className="filter-popover-wrap" ref={wrapRef}>
      <button className={`filter-pill ${selectedCount ? 'is-active' : ''}`} type="button" onClick={() => onOpen(!open)}>
        <TruncatedText text={selectedLabel} />
        <ChevronDown size={15} />
      </button>
      {open && (
        <div className="filter-popover category-popover">
          <label className="popover-search">
            <Search size={15} />
            <input value={categoryQuery} onChange={(event) => setCategoryQuery(event.target.value)} placeholder="搜索品类" autoFocus />
          </label>
          <div className="category-tree">
            <div className="category-column">
              <button className="tree-option" type="button" disabled={!visibleOptions.length} onClick={() => toggleAllPrimary(!allPrimarySelected)}>
                <CascadeCheckbox checked={allPrimarySelected} indeterminate={!allPrimarySelected && anyPrimarySelected} />
                <span>选择全部</span>
              </button>
              {visibleOptions.length ? visibleOptions.map((option) => (
                <div className={`tree-row ${primary?.value === option.value ? 'is-current' : ''}`} role="button" tabIndex={0} key={option.value} onClick={() => setActivePrimary(option.value)} onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    setActivePrimary(option.value)
                  }
                }}>
                  <button className="cascade-check-hit" type="button" onClick={(event) => {
                    event.stopPropagation()
                    const state = optionState(option)
                    togglePrimary(option, !state.checked)
                  }}>
                    <CascadeCheckbox checked={optionState(option).checked} indeterminate={optionState(option).indeterminate} />
                  </button>
                  <TruncatedText text={option.label} />
                  <ChevronRight size={15} />
                </div>
              )) : <span className="facet-empty">没有匹配品类</span>}
            </div>
            <div className="category-column">
              <button className="tree-option" type="button" disabled={!primary?.children?.length} onClick={() => toggleActiveChildren(!activeState.checked)}>
                <CascadeCheckbox checked={activeState.checked} indeterminate={activeState.indeterminate} />
                <span>选择全部</span>
              </button>
              {primary?.children?.length ? primary.children.map((child) => (
                <button className="tree-option" type="button" key={child.value} onClick={() => toggleSecondary(child.value, !(selectedPrimary.includes(primary.value) || selectedSecondary.includes(child.value)))}>
                  <CascadeCheckbox checked={selectedPrimary.includes(primary.value) || selectedSecondary.includes(child.value)} />
                  <TruncatedText text={child.label} />
                </button>
              )) : <span className="facet-empty">暂无二级品类</span>}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function CascadeCheckbox({ checked, indeterminate = false }: { checked: boolean; indeterminate?: boolean }) {
  return (
    <span className={`cascade-checkbox ${checked ? 'is-checked' : ''} ${indeterminate ? 'is-indeterminate' : ''}`} aria-hidden="true">
      {checked ? <Check size={12} strokeWidth={3} /> : indeterminate ? <span /> : null}
    </span>
  )
}

function categorySelectionLabels(options: CategoryTreeOption[], selectedPrimary: string[], selectedSecondary: string[]) {
  const labels: string[] = []
  const primaryLabels = new Map(options.map((item) => [item.value, item.label]))
  const secondaryLabels = new Map(options.flatMap((item) => (item.children ?? []).map((child) => [child.value, child.label] as const)))
  selectedPrimary.forEach((value) => labels.push(primaryLabels.get(value) ?? value))
  selectedSecondary.forEach((value) => {
    if (!selectedPrimary.some((primary) => (options.find((item) => item.value === primary)?.children ?? []).some((child) => child.value === value))) {
      labels.push(secondaryLabels.get(value) ?? value)
    }
  })
  return Array.from(new Set(labels))
}

function DateRangeFilter({ open, onOpen, label: emptyLabel = '上传时间', from, to, onFrom, onTo }: {
  open: boolean
  onOpen: (open: boolean) => void
  label?: string
  from: string
  to: string
  onFrom: (value: string) => void
  onTo: (value: string) => void
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const [calendarMonth, setCalendarMonth] = useState(() => monthStart(from ? parseDateInput(from) ?? new Date() : new Date()))
  const [draftFrom, setDraftFrom] = useState('')
  const [draftTo, setDraftTo] = useState('')
  const pendingRange = draftFrom && !draftTo ? `${formatDateLabel(draftFrom)} 至 ...` : ''
  const label = dateRangeLabel(emptyLabel, from, to)
  useOutsideClose(open, wrapRef, () => onOpen(false))

  useEffect(() => {
    if (!open) return
    setDraftFrom(from)
    setDraftTo(to)
    if (from) setCalendarMonth(monthStart(parseDateInput(from) ?? new Date()))
  }, [from, open, to])

  function selectDate(value: string) {
    if (!draftFrom || (draftFrom && draftTo)) {
      setDraftFrom(value)
      setDraftTo('')
      return
    }
    const nextFrom = value < draftFrom ? value : draftFrom
    const nextTo = value < draftFrom ? draftFrom : value
    setDraftFrom(nextFrom)
    setDraftTo(nextTo)
    onFrom(nextFrom)
    onTo(nextTo)
    onOpen(false)
  }

  function applyRange(nextFrom: Date, nextTo = new Date()) {
    onFrom(dateInputValue(nextFrom))
    onTo(dateInputValue(nextTo))
    onOpen(false)
  }

  function applyWeekRange() {
    const today = new Date()
    const mondayOffset = (today.getDay() + 6) % 7
    const nextFrom = new Date(today)
    nextFrom.setDate(today.getDate() - mondayOffset)
    applyRange(nextFrom, today)
  }

  function applyMonthRange() {
    const today = new Date()
    applyRange(new Date(today.getFullYear(), today.getMonth(), 1), today)
  }

  function applyYearRange() {
    const today = new Date()
    applyRange(new Date(today.getFullYear(), 0, 1), today)
  }

  function toggleOpen() {
    if (open) {
      onOpen(false)
      return
    }
    setDraftFrom(from)
    setDraftTo(to)
    onOpen(true)
  }

  return (
    <div className="filter-popover-wrap" ref={wrapRef}>
      <button className={`filter-pill date-filter-pill ${from || to ? 'is-active' : ''}`} type="button" onClick={toggleOpen}>
        <TruncatedText text={pendingRange || label} />
        <ChevronDown size={15} />
      </button>
      {open && (
        <div className="filter-popover date-popover">
          <button type="button" onClick={applyWeekRange}>本周</button>
          <button type="button" onClick={applyMonthRange}>本月</button>
          <button type="button" onClick={applyYearRange}>本年</button>
          <CalendarRangePicker month={calendarMonth} from={draftFrom} to={draftTo} onMonth={setCalendarMonth} onSelect={selectDate} />
        </div>
      )}
    </div>
  )
}

function SortFilter({ open, onOpen, sortBy, sortDir, options = librarySortOptions, onChange }: {
  open: boolean
  onOpen: (open: boolean) => void
  sortBy: string
  sortDir: 'asc' | 'desc'
  options?: { value: string; label: string }[]
  onChange: (sortBy: string, sortDir: 'asc' | 'desc') => void
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const current = `${sortBy}:${sortDir}`
  const currentLabel = options.find((option) => option.value === current)?.label ?? '排序'
  useOutsideClose(open, wrapRef, () => onOpen(false))
  return (
    <div className="filter-popover-wrap" ref={wrapRef}>
      <button className={`filter-pill ${current !== 'createdAt:desc' ? 'is-active' : ''}`} type="button" onClick={() => onOpen(!open)}>
        <TruncatedText text={currentLabel} />
        <ChevronDown size={15} />
      </button>
      {open && (
        <div className="filter-popover sort-popover">
          {options.map((option) => (
            <button className={option.value === current ? 'is-selected' : ''} type="button" key={option.value} onClick={() => {
              const [nextSortBy, nextSortDir] = option.value.split(':')
              onChange(nextSortBy, nextSortDir === 'asc' ? 'asc' : 'desc')
              onOpen(false)
            }}>
              {option.label}
              {option.value === current && <Check size={15} />}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

const librarySortOptions = [
  { value: 'createdAt:desc', label: '最新上传' },
  { value: 'createdAt:asc', label: '最早上传' },
  { value: 'updatedAt:desc', label: '最近更新' },
  { value: 'updatedAt:asc', label: '最早更新' },
  { value: 'code:asc', label: '货号 A-Z' },
  { value: 'code:desc', label: '货号 Z-A' },
]

const recordSortOptions = [
  { value: 'createdAt:desc', label: '最新创建' },
  { value: 'createdAt:asc', label: '最早创建' },
  { value: 'updatedAt:desc', label: '最近更新' },
  { value: 'updatedAt:asc', label: '最早更新' },
  { value: 'name:asc', label: '任务名 A-Z' },
  { value: 'name:desc', label: '任务名 Z-A' },
]

const stylingGarmentStatusOrder = ['DRAFT', 'TAGGING_RUNNING', 'TAGGING_READY', 'TAGGING_FAILED', 'READY']

function stylingGarmentStatusOptions(options: FacetOption[] | undefined, stableOptions: FacetOption[] | undefined, selected: string) {
  const byValue = new Map((stableOptions ?? []).map((option) => [option.value, { ...option, count: 0 }]))
  for (const option of options ?? []) byValue.set(option.value, option)
  if (selected && !byValue.has(selected)) byValue.set(selected, { value: selected, label: selected, count: 0 })
  return [
    ...stylingGarmentStatusOrder.map((status) => byValue.get(status) ?? { value: status, label: status, count: 0 }),
    ...Array.from(byValue.values()).filter((option) => !stylingGarmentStatusOrder.includes(option.value)),
  ]
}

const generationRecordStatuses = [
  'DRAFT',
  'READY_FOR_STYLING',
  'STYLING_RUNNING',
  'STYLING_READY',
  'PREVIEW_RUNNING',
  'PREVIEW_READY',
  'FINAL_RUNNING',
  'FINAL_PARTIAL',
  'COMPLETED',
  'FAILED',
  'CANCELLED',
  'ABANDONED',
]

const generationBatchRecordStatuses = ['ARCHIVED', 'RUNNING', 'FAILED', 'PARTIAL', 'CANCELLED', 'COMPLETED', 'PENDING']
const workspaceRecordStatuses = [...generationBatchRecordStatuses, ...generationRecordStatuses.filter((status) => !generationBatchRecordStatuses.includes(status))]

function generationRecordStatusOptions(options: FacetOption[] | undefined, stableOptions: FacetOption[] | undefined, selected: string) {
  return orderedStatusOptions(generationRecordStatuses, options, stableOptions, selected)
}

function generationBatchStatusOptions(options: FacetOption[] | undefined, stableOptions: FacetOption[] | undefined, selected: string) {
  return orderedStatusOptions(generationBatchRecordStatuses, options, stableOptions, selected)
}

function generationWorkspaceStatusOptions(options: FacetOption[] | undefined, stableOptions: FacetOption[] | undefined, selected: string) {
  return orderedStatusOptions(workspaceRecordStatuses, options, stableOptions, selected)
}

function orderedStatusOptions(order: string[], options: FacetOption[] | undefined, stableOptions: FacetOption[] | undefined, selected: string) {
  const byValue = new Map((stableOptions ?? []).map((option) => [option.value, { ...option, count: 0 }]))
  for (const option of options ?? []) byValue.set(option.value, option)
  if (selected && !byValue.has(selected)) byValue.set(selected, { value: selected, label: selected, count: 0 })
  return [
    ...order.map((status) => byValue.get(status) ?? { value: status, label: status, count: 0 }),
    ...Array.from(byValue.values()).filter((option) => !order.includes(option.value)),
  ]
}

function StatusFilter({ open, onOpen, value, options, showCounts = true, formatLabel = garmentStatus, onChange }: {
  open: boolean
  onOpen: (open: boolean) => void
  value: string
  options: FacetOption[]
  showCounts?: boolean
  formatLabel?: (value: string) => string
  onChange: (value: string) => void
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  useOutsideClose(open, wrapRef, () => onOpen(false))
  return (
    <div className="filter-popover-wrap" ref={wrapRef}>
      <button className={`filter-pill ${value ? 'is-active' : ''}`} type="button" onClick={() => onOpen(!open)}>
        <TruncatedText text={value ? formatLabel(value) : '全部状态'} />
        <ChevronDown size={15} />
      </button>
      {open && (
        <div className="filter-popover sort-popover">
          <button className={!value ? 'is-selected' : ''} type="button" onClick={() => { onChange(''); onOpen(false) }}>
            全部状态
            {!value && <Check size={15} />}
          </button>
          {options.map((option) => (
            <button className={option.value === value ? 'is-selected' : ''} type="button" key={option.value} onClick={() => {
              onChange(option.value)
              onOpen(false)
            }}>
              {formatLabel(option.value)}
              {showCounts && <small>{option.count}</small>}
              {option.value === value && <Check size={15} />}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function ImportBatchFilter({ open, onOpen, value, options, onChange }: {
  open: boolean
  onOpen: (open: boolean) => void
  value: string
  options: ImportBatchFilterOption[]
  onChange: (value: string) => void
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const selected = options.find((option) => option.value === value)
  useOutsideClose(open, wrapRef, () => onOpen(false))
  return (
    <div className="filter-popover-wrap" ref={wrapRef}>
      <button className={`filter-pill import-batch-filter-pill ${value ? 'is-active' : ''}`} type="button" onClick={() => onOpen(!open)}>
        <TruncatedText text={selected ? selected.label : '全部上传批次'} />
        <ChevronDown size={15} />
      </button>
      {open && (
        <div className="filter-popover sort-popover import-batch-popover">
          <button className={!value ? 'is-selected' : ''} type="button" onClick={() => { onChange(''); onOpen(false) }}>
            全部上传批次
            {!value && <Check size={15} />}
          </button>
          {options.map((option) => (
            <button className={option.value === value ? 'is-selected' : ''} type="button" key={option.value} onClick={() => {
              onChange(option.value)
              onOpen(false)
            }}>
              <TruncatedText text={option.label} />
              <small>{option.count} 个货号</small>
              {option.value === value && <Check size={15} />}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function CalendarRangePicker({ month, from, to, onMonth, onSelect }: {
  month: Date
  from: string
  to: string
  onMonth: (value: Date) => void
  onSelect: (value: string) => void
}) {
  const days = calendarDays(month)
  const todayValue = dateInputValue(new Date())
  const monthLabel = `${month.getFullYear()} 年 ${month.getMonth() + 1} 月`
  const year = month.getFullYear()
  const years = Array.from({ length: 13 }, (_, index) => year - 6 + index)
  return (
    <div className="calendar-range">
      <div className="calendar-head">
        <button type="button" onClick={() => onMonth(addMonths(month, -1))}><ChevronLeft size={15} /></button>
        <div className="calendar-month-selects" aria-label={monthLabel}>
          <select value={month.getFullYear()} onChange={(event) => onMonth(new Date(Number(event.target.value), month.getMonth(), 1))}>
            {years.map((item) => <option value={item} key={item}>{item} 年</option>)}
          </select>
          <select value={month.getMonth()} onChange={(event) => onMonth(new Date(month.getFullYear(), Number(event.target.value), 1))}>
            {Array.from({ length: 12 }, (_, index) => <option value={index} key={index}>{index + 1} 月</option>)}
          </select>
        </div>
        <button type="button" onClick={() => onMonth(addMonths(month, 1))}><ChevronRight size={15} /></button>
      </div>
      <div className="calendar-weekdays">{['一', '二', '三', '四', '五', '六', '日'].map((item) => <span key={item}>{item}</span>)}</div>
      <div className="calendar-grid">
        {days.map((day) => {
          const value = dateInputValue(day.date)
          const inRange = !!from && !!to && value >= from && value <= to
          const isEdge = value === from || value === to
          return (
            <button
              className={`${day.inMonth ? '' : 'is-muted'} ${value === todayValue ? 'is-today' : ''} ${inRange ? 'is-in-range' : ''} ${isEdge ? 'is-selected' : ''}`}
              type="button"
              key={value}
              onClick={() => onSelect(value)}
            >
              {day.date.getDate()}
            </button>
          )
        })}
      </div>
    </div>
  )
}

function ManualTagFields({
  launchBatch,
  onLaunchBatch,
  series,
  onSeries,
  retailChannel,
  onRetailChannel,
  internalCollection,
  onInternalCollection,
  stylingPriority,
  onStylingPriority,
  notes,
  onNotes,
  disabled = false,
}: {
  launchBatch: string
  onLaunchBatch: (value: string) => void
  series: string
  onSeries: (value: string) => void
  retailChannel: string
  onRetailChannel: (value: string) => void
  internalCollection: string
  onInternalCollection: (value: string) => void
  stylingPriority: string
  onStylingPriority: (value: string) => void
  notes: string
  onNotes: (value: string) => void
  disabled?: boolean
}) {
  return (
    <div className="manual-tags-form">
      <label>上新批次<input value={launchBatch} disabled={disabled} onChange={(event) => onLaunchBatch(event.target.value)} /></label>
      <label>系列<input value={series} disabled={disabled} onChange={(event) => onSeries(event.target.value)} placeholder="多个值用逗号分隔" /></label>
      <label>渠道<input value={retailChannel} disabled={disabled} onChange={(event) => onRetailChannel(event.target.value)} placeholder="多个值用逗号分隔" /></label>
      <label>内部分组<input value={internalCollection} disabled={disabled} onChange={(event) => onInternalCollection(event.target.value)} placeholder="多个值用逗号分隔" /></label>
      <label>选搭优先级
        <select value={stylingPriority} disabled={disabled} onChange={(event) => onStylingPriority(event.target.value)}>
          <option value="">不调整</option>
          <option value="preferred">优先</option>
          <option value="normal">正常</option>
          <option value="deprioritized">降权</option>
        </select>
      </label>
      <label className="manual-tags-notes">备注<textarea value={notes} disabled={disabled} onChange={(event) => onNotes(event.target.value)} rows={3} /></label>
    </div>
  )
}

function parseTagListInput(value: string) {
  return value.split(/[,，、/]/).map((item) => item.trim()).filter(Boolean)
}

function mergedFacetOptions(options: FacetOption[] | undefined, selected: string[]) {
  const byValue = new Map<string, FacetOption>()
  ;(options ?? []).forEach((option) => byValue.set(option.value, option))
  selected.forEach((value) => {
    if (!byValue.has(value)) byValue.set(value, { value, label: value, count: 0 })
  })
  return Array.from(byValue.values())
}

function useOutsideClose(open: boolean, ref: { current: HTMLElement | null }, onClose: () => void) {
  useEffect(() => {
    if (!open) return
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target
      if (target instanceof Node && ref.current?.contains(target)) return
      onClose()
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('pointerdown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [open, onClose, ref])
}

function parseDateInput(value: string) {
  if (!value) return null
  const [year, month, day] = value.split('-').map(Number)
  if (!year || !month || !day) return null
  return new Date(year, month - 1, day)
}

function formatDateLabel(value: string) {
  if (!value) return ''
  return value.replace(/-/g, '.')
}

function dateRangeLabel(emptyLabel: string, from: string, to: string) {
  if (from && to) return from === to ? formatDateLabel(from) : `${formatDateLabel(from)} - ${formatDateLabel(to)}`
  if (from) return `${formatDateLabel(from)} 起`
  if (to) return `截至 ${formatDateLabel(to)}`
  return emptyLabel
}

function monthStart(value: Date) {
  return new Date(value.getFullYear(), value.getMonth(), 1)
}

function addMonths(value: Date, offset: number) {
  return new Date(value.getFullYear(), value.getMonth() + offset, 1)
}

function calendarDays(month: Date) {
  const first = monthStart(month)
  const mondayOffset = (first.getDay() + 6) % 7
  const start = new Date(first)
  start.setDate(first.getDate() - mondayOffset)
  return Array.from({ length: 42 }, (_, index) => {
    const date = new Date(start)
    date.setDate(start.getDate() + index)
    return { date, inMonth: date.getMonth() === month.getMonth() }
  })
}

function dateInputValue(value: Date) {
  const year = value.getFullYear()
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

type StylingImportBatch = ImportBatch & { error?: string }

function inferredImportJobTitle(files: File[], batches: StylingImportBatch[] = []) {
  const folderNames = files
    .map((file) => importPath(file).split('/').filter(Boolean)[0])
    .filter(Boolean)
  const uniqueFolders = Array.from(new Set(folderNames))
  if (uniqueFolders.length === 1) return uniqueFolders[0].slice(0, BATCH_NAME_MAX_CHARS)
  if (uniqueFolders.length > 1) return `${uniqueFolders[0]} 等 ${uniqueFolders.length} 个文件夹`.slice(0, BATCH_NAME_MAX_CHARS)
  if (batches.length === 1) return sanitizeClientName(batches[0]?.code ?? '') || timestampName()
  if (batches.length > 1) return `${batches[0]?.code ?? '搭配服装'} 等 ${batches.length} 个货号`.slice(0, BATCH_NAME_MAX_CHARS)
  return timestampName()
}

type ImportJobItem = {
  id: string
  code: string
  imageCount: number
  uploadedCount: number
  status: 'WAITING' | 'UPLOADING' | 'SAVING' | 'SAVED' | 'TAGGING' | 'SUCCEEDED' | 'INTERRUPTED' | 'FAILED'
  sessionId?: string
  garmentId?: number
  previewUrl?: string
  error?: string
}

type ImportJob = {
  id: string
  title: string
  status: 'RUNNING' | 'INTERRUPTED' | 'SUCCEEDED' | 'FAILED'
  createdAt?: string
  updatedAt?: string
  archivedAt?: string | null
  archiveReason?: string | null
  total: number
  uploaded: number
  skipped: number
  currentCode?: string
  progress?: UploadProgressSnapshot | null
  operations: AIOperation[]
  items: ImportJobItem[]
}

function importJobItemStatusLabel(status: ImportJobItem['status']) {
  if (status === 'WAITING') return '等待上传'
  if (status === 'UPLOADING') return '上传图片'
  if (status === 'SAVING') return '保存服装'
  if (status === 'SAVED') return '已保存，等待识别'
  if (status === 'TAGGING') return 'AI 识别中'
  if (status === 'SUCCEEDED') return '已提交识别'
  if (status === 'INTERRUPTED') return '上传已暂停'
  if (status === 'FAILED') return '需要处理'
  return status
}

function importJobItemProgress(item: ImportJobItem) {
  const uploadTargetCount = Math.min(Math.max(item.imageCount, item.uploadedCount, 1), STYLING_GARMENT_MAX_IMAGES)
  if (item.status === 'SUCCEEDED') return 100
  if (item.status === 'TAGGING') return 92
  if (item.status === 'SAVED') return 82
  if (item.status === 'SAVING') return 72
  if (item.status === 'FAILED') return item.uploadedCount > 0 ? Math.max(12, Math.round((item.uploadedCount / uploadTargetCount) * 70)) : 0
  return Math.round((item.uploadedCount / uploadTargetCount) * 70)
}

function storedLibraryImportJobs(): ImportJob[] {
  if (typeof window === 'undefined') return []
  try {
    const parsed = JSON.parse(window.localStorage.getItem(libraryImportJobsStorageKey) || '[]') as ImportJob[]
    if (!Array.isArray(parsed)) return []
    return parsed.slice(0, 5).map((job) => ({
      ...job,
      status: job.status === 'RUNNING' ? 'INTERRUPTED' : job.status,
      items: (job.items ?? []).map((item) => item.status === 'WAITING' || item.status === 'UPLOADING'
        ? { ...item, status: 'INTERRUPTED', error: item.error ?? '页面刷新后上传已暂停，可从已完成位置继续。' }
        : item),
    }))
  } catch {
    return []
  }
}

type ImportTaskFilter = 'all' | 'running' | 'pending' | 'attention' | 'saved'

type ImportBatchFilterOption = {
  value: string
  label: string
  count: number
  codes: string[]
  updatedAt?: string
}

type ImportTaskRow = {
  id: string
  source: 'job' | 'session'
  batchId?: string
  serverBatchId?: string | null
  sessionId?: string
  garmentId?: number
  scope?: string | null
  sourceTitle?: string | null
  code: string
  importedCount: number
  uploadedCount: number
  retainedCount: number | null
  status: string
  statusLabel: string
  statusTone: 'running' | 'success' | 'warning' | 'danger' | 'muted'
  previewUrl?: string | null
  createdAt?: string
  updatedAt?: string
  error?: string | null
  archivedAt?: string | null
  archiveReason?: string | null
}

type RecoveredImportSessionGroup = {
  id: string
  title: string
  rows: ImportTaskRow[]
  total: number
  createdAt?: string
  updatedAt?: string
}

type UploadRecordTarget = {
  id: string
  title: string
  rows: ImportTaskRow[]
  jobId?: string
  archived?: boolean
}

function importTaskStatusTone(status: string): ImportTaskRow['statusTone'] {
  if (['WAITING', 'UPLOADING', 'SAVING', 'PENDING', 'FINALIZING', 'TAGGING', 'TAGGING_RUNNING'].includes(status)) return 'running'
  if (['SAVED', 'SUCCEEDED', 'UPLOADED', 'DRAFT', 'TAGGING_READY', 'INTERRUPTED'].includes(status)) return 'warning'
  if (['FINALIZED', 'READY'].includes(status)) return 'success'
  if (['FAILED', 'TAGGING_FAILED'].includes(status)) return 'danger'
  if (status === 'ABANDONED') return 'muted'
  return 'warning'
}

function isImportTaskRunning(status: string) {
  return ['WAITING', 'UPLOADING', 'SAVING', 'RUNNING', 'PENDING', 'FINALIZING', 'TAGGING', 'TAGGING_RUNNING'].includes(status)
}

function isImportTaskAttention(status: string) {
  return ['FAILED', 'TAGGING_FAILED', 'INTERRUPTED'].includes(status)
}

function isImportTaskDone(status: string) {
  return ['FINALIZED', 'READY'].includes(status)
}

function isImportTaskPendingRecognition(status: string) {
  return ['SAVED', 'SUCCEEDED', 'DRAFT', 'TAGGING_READY'].includes(status)
}

function uploadSessionDrivesImportItem(status: ImportJobItem['status']) {
  return ['WAITING', 'UPLOADING', 'SAVING'].includes(status)
}

function importSessionCode(session: UploadSession, garment?: StylingGarment) {
  if (session.businessCode) return session.businessCode
  if (garment?.code) return garment.code
  const filename = session.items?.[0]?.filename
  if (filename) return filename.replace(/\.[^.]+$/, '')
  return `上传任务 ${session.id.slice(0, 8)}`
}

function importSessionTitle(session: UploadSession, row: ImportTaskRow) {
  return session.sourceTitle || row.code
}

function importTaskStatusLabel(status: string) {
  if (status === 'TAGGING') return 'AI 识别中'
  if (['PENDING', 'UPLOADING', 'UPLOADED', 'FINALIZING', 'FINALIZED', 'FAILED', 'ABANDONED'].includes(status)) return uploadSessionStatusLabel(status)
  if (['WAITING', 'SAVING', 'SAVED', 'SUCCEEDED', 'INTERRUPTED'].includes(status)) return importJobItemStatusLabel(status as ImportJobItem['status'])
  return garmentStatus(status)
}

function importRowBusinessStatus(session: UploadSession | undefined, garment: StylingGarment | undefined, fallbackStatus: string) {
  if (garment?.status) return garment.status
  return session?.status ?? fallbackStatus
}

function importSessionGroupTitle(rows: ImportTaskRow[], sessionsById: Map<string, UploadSession>) {
  const first = rows[0]
  if (!first) return '上传任务'
  const session = first.sessionId ? sessionsById.get(first.sessionId) : undefined
  if (session) return importSessionTitle(session, first)
  return first.sourceTitle || first.code
}

function importRowTime(row: ImportTaskRow) {
  return new Date(parseRuyiTime(row.createdAt ?? row.updatedAt)).getTime()
}

function importRowsLatestTime(rows: ImportTaskRow[]) {
  return rows
    .map((row) => row.updatedAt ?? row.createdAt)
    .filter(Boolean)
    .sort()
    .at(-1)
}

function importRowsEarliestTime(rows: ImportTaskRow[]) {
  return rows
    .map((row) => row.createdAt ?? row.updatedAt)
    .filter(Boolean)
    .sort()
    .at(0)
}

function recoveredImportSessionGroups(rows: ImportTaskRow[], sessionsById: Map<string, UploadSession>): RecoveredImportSessionGroup[] {
  const groups: RecoveredImportSessionGroup[] = []
  const buckets = new Map<string, ImportTaskRow[]>()

  rows.forEach((row) => {
    const session = row.sessionId ? sessionsById.get(row.sessionId) : undefined
    const title = session?.sourceTitle?.trim() || row.sourceTitle?.trim()
    if (!title) {
      groups.push({
        id: row.id,
        title: session ? importSessionTitle(session, row) : row.code,
        rows: [row],
        total: 1,
        createdAt: row.createdAt,
        updatedAt: row.updatedAt ?? row.createdAt,
      })
      return
    }
    const key = session?.batchId || row.serverBatchId || `${session?.scope ?? row.scope ?? 'upload'}::${title}`
    buckets.set(key, [...(buckets.get(key) ?? []), row])
  })

  buckets.forEach((bucket, key) => {
    const sorted = [...bucket].sort((a, b) => importRowTime(a) - importRowTime(b))
    let cluster: ImportTaskRow[] = []
    let previousTime = 0
    const pushCluster = () => {
      if (!cluster.length) return
      const rowsByLatest = [...cluster].sort((a, b) => importRowTime(b) - importRowTime(a))
      const title = importSessionGroupTitle(rowsByLatest, sessionsById)
      const createdAt = importRowsEarliestTime(rowsByLatest)
      groups.push({
        id: rowsByLatest.length === 1 ? rowsByLatest[0]!.id : `session-group-${key}-${createdAt ?? rowsByLatest[0]!.id}`,
        title,
        rows: rowsByLatest,
        total: rowsByLatest.length,
        createdAt,
        updatedAt: importRowsLatestTime(rowsByLatest) ?? createdAt,
      })
    }
    sorted.forEach((row) => {
      const time = importRowTime(row)
      if (cluster.length && Number.isFinite(time) && Number.isFinite(previousTime) && time - previousTime > IMPORT_SESSION_GROUP_WINDOW_MS) {
        pushCluster()
        cluster = []
      }
      cluster.push(row)
      previousTime = time
    })
    pushCluster()
  })

  return groups
}

function retainedImageCount(garment?: StylingGarment) {
  if (!garment) return null
  if (typeof garment.imageCount === 'number') return garment.imageCount
  if (Array.isArray(garment.images)) return garment.images.length
  return null
}

function imageCountText(imported: number, uploaded: number, retained: number | null) {
  return `${imported} / ${uploaded} / ${retained === null ? '-' : retained}`
}

function importImageCountText(imported: number, uploaded: number, retained: number | null) {
  return `已选 ${imported} / 已上传 ${uploaded} / 已入库 ${retained === null ? '-' : retained}`
}

function importTaskMetaText(row: ImportTaskRow) {
  if (row.error) return row.error
  if (row.garmentId) return `服装记录 #${row.garmentId}`
  if (row.source === 'session' && row.sessionId) return `上传批次 ${row.sessionId.slice(0, 8)}`
  return '本次上传'
}

function importTaskProgress(row: ImportTaskRow) {
  if (['SUCCEEDED', 'SAVED', 'FINALIZED'].includes(row.status)) return 100
  if (row.status === 'TAGGING') return 92
  if (row.status === 'FINALIZING') return 88
  if (row.status === 'SAVING' || row.status === 'UPLOADED') return 78
  if (['FAILED', 'INTERRUPTED'].includes(row.status)) return row.uploadedCount > 0 ? Math.max(12, Math.round((row.uploadedCount / Math.max(1, row.importedCount)) * 70)) : 0
  return Math.round((row.uploadedCount / Math.max(1, row.importedCount)) * 70)
}

function importJobDisplayTitle(job: ImportJob, rows: ImportTaskRow[]) {
  const cleanTitle = job.title?.trim()
  if (cleanTitle && !/^\d+\s*套搭配服装$/.test(cleanTitle)) return cleanTitle
  const firstCode = rows.find((row) => row.code)?.code
  if (firstCode && job.total > 1) return `${firstCode} 等 ${job.total} 个货号`
  if (firstCode) return firstCode
  return cleanTitle || `${job.total} 个货号`
}

function garmentMatchesLibraryQuery(item: StylingGarment, queryTerms: string[]) {
  if (!queryTerms.length) return true
  const searchableText = [
    item.code,
    item.status,
    garmentStatus(item.status),
    JSON.stringify(item.aiTags ?? {}),
    JSON.stringify(item.manualTags ?? {}),
    JSON.stringify(item.officialTitleTags ?? {}),
    JSON.stringify(item.effectiveTags ?? {}),
  ].join(' ').toLocaleLowerCase()
  return queryTerms.some((term) => searchableText.includes(term))
}

function BatchImportPanel({
  onClose,
  onImported,
  uploadSessions,
  refreshUploadSessions,
  markBrowserUploading,
  garments,
  importJobs,
  setImportJobs,
  syncImportJobs,
  onFindGarment,
  onFindJob,
}: {
  onClose: () => void
  onImported: () => void
  uploadSessions: UploadSession[]
  refreshUploadSessions: () => Promise<UploadSession[]>
  markBrowserUploading: (delta: 1 | -1) => void
  garments: StylingGarment[]
  importJobs: ImportJob[]
  setImportJobs: Dispatch<SetStateAction<ImportJob[]>>
  syncImportJobs: Dispatch<SetStateAction<ImportJob[]>>
  onFindGarment: (code: string, garmentId?: number) => void
  onFindJob: (jobId: string) => void
}) {
  const [batches, setBatches] = useState<StylingImportBatch[]>([])
  const [importSourceTitle, setImportSourceTitle] = useState('')
  const [localError, setLocalError] = useState('')
  const [selectedDirectoryHandle, setSelectedDirectoryHandle] = useState<RuyiFileSystemDirectoryHandle | null>(null)
  const [resumingJobId, setResumingJobId] = useState<string | null>(null)
  const [savedDirectoryJobIds, setSavedDirectoryJobIds] = useState<Set<string>>(() => new Set())
  const [checksumCompatibilityMode, setChecksumCompatibilityMode] = useState(false)
  const runningImportJobCount = importJobs.filter((job) => job.status === 'RUNNING').length
  const hasRunningImportJobs = runningImportJobCount > 0
  const uploadWarningActiveRef = useRef(false)
  const interruptedJobIdsKey = JSON.stringify(importJobs.filter((job) => job.status === 'INTERRUPTED').map((job) => job.id).sort())

  useEffect(() => {
    let cancelled = false
    const interruptedJobIds = JSON.parse(interruptedJobIdsKey) as string[]
    void Promise.all(interruptedJobIds.map(async (jobId) => {
      const handle = await getDirectoryHandle(jobId).catch(() => undefined)
      const permission = handle ? await handle.queryPermission({ mode: 'read' }).catch(() => 'denied' as const) : 'denied' as const
      return { jobId, available: Boolean(handle) && permission !== 'denied' }
    })).then((entries) => {
      if (cancelled) return
      setSavedDirectoryJobIds(new Set(entries.filter((entry) => entry.available).map((entry) => entry.jobId)))
    })
    return () => {
      cancelled = true
    }
  }, [interruptedJobIdsKey])

  useEffect(() => {
    const grouped = new Map<string, UploadSession[]>()
    uploadSessions.forEach((session) => {
      if (session.scope !== 'styling-garment' || !session.batchId) return
      grouped.set(session.batchId, [...(grouped.get(session.batchId) ?? []), session])
    })
    if (!grouped.size) return
    syncImportJobs((current) => {
      const existingIds = new Set(current.map((job) => job.id))
      const recovered = [...grouped.entries()].filter(([batchId]) => !existingIds.has(batchId)).map(([batchId, sessions]) => {
        const allFinalized = sessions.every((session) => session.status === 'FINALIZED')
        const needsRecognition = sessions.some((session) => session.status === 'FINALIZED' && ['DRAFT', 'TAGGING_FAILED'].includes(session.targetStatus || ''))
        const hasUploadFailure = sessions.some((session) => session.status === 'FAILED' && session.uploadedCount < session.totalCount)
        const createdAt = sessions.map((session) => session.createdAt).filter(Boolean).sort().at(0)
        const updatedAt = sessions.map((session) => session.updatedAt).filter(Boolean).sort().at(-1)
        return {
          id: batchId,
          title: sessions[0]?.sourceTitle || `${sessions.length} 个货号`,
          status: allFinalized && !needsRecognition ? 'SUCCEEDED' as const : hasUploadFailure ? 'FAILED' as const : 'INTERRUPTED' as const,
          createdAt,
          updatedAt,
          total: sessions.length,
          uploaded: sessions.filter((session) => session.status === 'FINALIZED').length,
          skipped: sessions.filter((session) => session.status === 'FAILED').length,
          progress: null,
          operations: [],
          items: sessions.map((session) => ({
            id: session.id,
            code: session.businessCode || `上传任务 ${session.id.slice(0, 8)}`,
            imageCount: session.totalCount,
            uploadedCount: session.uploadedCount,
            status: session.status === 'FINALIZED' ? 'SAVED' as const
              : session.status === 'UPLOADED' || session.status === 'FINALIZING' ? 'SAVING' as const
                : session.status === 'FAILED' ? 'FAILED' as const : 'INTERRUPTED' as const,
            sessionId: session.id,
            garmentId: session.targetId ?? undefined,
            error: session.errorMessage ?? undefined,
          })),
        }
      })
      return recovered.length ? [...recovered, ...current] : current
    })
  }, [syncImportJobs, uploadSessions])

  function addFiles(files: File[]) {
    const next = groupImportFiles(files).batches
    setLocalError('')
    setBatches((current) => {
      const existingCodes = new Set(current.map((item) => item.code))
      return [
        ...current,
        ...next.map((item) => {
          if (!existingCodes.has(item.code)) {
            existingCodes.add(item.code)
            return { ...item, error: undefined }
          }
          const code = `${item.code}-${current.length + 1}`
          existingCodes.add(code)
          return { ...item, id: `${item.id}-${current.length}`, code, error: undefined }
        }),
      ]
    })
    setImportSourceTitle((current) => current || inferredImportJobTitle(files, next))
  }

  async function selectDirectory() {
    try {
      const handle = await chooseDirectory()
      if (!handle) return
      const files = await filesFromDirectory(handle)
      setSelectedDirectoryHandle(handle)
      addFiles(files)
    } catch (err) {
      if ((err as DOMException)?.name === 'AbortError') return
      setLocalError(err instanceof Error ? err.message : '无法读取所选文件夹')
    }
  }

  function trackImportProgress(jobId: string, code: string, progress: UploadProgressSnapshot) {
    updateJob(jobId, (job) => ({
      ...job,
      currentCode: code,
      progress,
      items: job.items.map((item) => item.code === code ? {
        ...item,
        status: progress.phase === 'saving' ? 'SAVING' : 'UPLOADING',
        uploadedCount: progress.completed,
      } : item),
    }))
  }

  function clearImportUploadWarning() {
    if (!uploadWarningActiveRef.current) return
    markBrowserUploading(-1)
    uploadWarningActiveRef.current = false
  }

  async function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    const files = await filesFromDataTransfer(event.dataTransfer)
    addFiles(files)
  }

  function updateCode(id: string, code: string) {
    setBatches((items) => items.map((item) => item.id === id ? { ...item, code, error: undefined } : item))
  }

  function updateFiles(id: string, files: File[]) {
    setBatches((items) => items.map((item) => item.id === id ? { ...item, files, error: undefined } : item))
  }

  function removeBatch(id: string) {
    let shouldClearTitle = false
    setBatches((items) => {
      const next = items.filter((item) => item.id !== id)
      shouldClearTitle = !next.length
      return next
    })
    if (shouldClearTitle) setImportSourceTitle('')
  }

  function saveDrafts() {
    const seen = new Set<string>()
    const ready: StylingImportBatch[] = []
    const editBlocked: StylingImportBatch[] = []
    const recordBlocked: StylingImportBatch[] = []
    batches.forEach((batch) => {
      const code = sanitizeClientName(batch.code)
      if (!code) {
        editBlocked.push({ ...batch, error: '每套搭配服装都需要一个有效货号' })
        return
      }
      if (!batch.files.length) {
        editBlocked.push({ ...batch, code, error: '没有可用图片' })
        return
      }
      if (seen.has(code)) {
        recordBlocked.push({ ...batch, code, error: '本批次货号重复，本次未重复上传' })
        return
      }
      seen.add(code)
      ready.push({ ...batch, code, error: undefined })
    })
    if (editBlocked.length) {
      setBatches(batches.map((batch) => {
        const blocked = editBlocked.find((item) => item.id === batch.id)
        const duplicated = recordBlocked.find((item) => item.id === batch.id)
        return blocked ?? duplicated ?? { ...batch, code: sanitizeClientName(batch.code), error: undefined }
      }))
      setLocalError('请先处理空货号或无图片的服装，再开始上传')
      return
    }
    if (!ready.length) {
      setBatches(recordBlocked)
      setLocalError(recordBlocked.length ? '没有可上传的新服装，请先处理标红项' : '请先选择图片或文件夹')
      return
    }
    setBatches([])
    setLocalError('')
    void startImportJob(ready, recordBlocked, importSourceTitle || inferredImportJobTitle([], [...ready, ...recordBlocked]), selectedDirectoryHandle)
    setImportSourceTitle('')
    setSelectedDirectoryHandle(null)
  }

  function updateJob(jobId: string, updater: (job: ImportJob) => ImportJob) {
    setImportJobs((jobs) => jobs.map((job) => job.id === jobId ? { ...updater(job), updatedAt: new Date().toISOString() } : job))
  }

  function updateJobItem(jobId: string, code: string, patch: Partial<ImportJobItem>) {
    updateJob(jobId, (job) => ({
      ...job,
      items: job.items.map((item) => item.code === code ? { ...item, ...patch } : item),
    }))
  }

  async function startImportJob(jobBatches: StylingImportBatch[], initiallyFailedBatches: StylingImportBatch[] = [], sourceTitle = '', directoryHandle: RuyiFileSystemDirectoryHandle | null = null) {
    const jobId = `import-${Date.now()}-${Math.random().toString(36).slice(2)}`
    const startedAt = new Date().toISOString()
    const allJobBatches = [...jobBatches, ...initiallyFailedBatches]
    const initialOperations: AIOperation[] = allJobBatches.map((batch, index) => ({
      id: -(index + 1),
      ownerType: 'styling_garment_upload',
      ownerId: index + 1,
      provider: 'browser',
      status: initiallyFailedBatches.some((item) => item.id === batch.id) ? 'FAILED' : 'SUBMITTED',
      startedAt,
      providerRequestId: batch.code,
      errorMessage: batch.error,
    }))
    const nextJob: ImportJob = {
      id: jobId,
      title: sourceTitle || `${allJobBatches.length} 套搭配服装`,
      status: 'RUNNING',
      createdAt: startedAt,
      updatedAt: startedAt,
      total: allJobBatches.length,
      uploaded: 0,
      skipped: initiallyFailedBatches.length,
      progress: null,
      operations: initialOperations,
      items: allJobBatches.map((batch) => ({
        id: batch.id,
        code: batch.code,
        imageCount: batch.files.length,
        uploadedCount: 0,
        status: batch.error ? 'FAILED' : 'WAITING',
        previewUrl: batch.files[0] ? URL.createObjectURL(batch.files[0]) : undefined,
        error: batch.error,
      })),
    }
    setImportJobs((jobs) => [nextJob, ...jobs].slice(0, 5))
    if (!uploadWarningActiveRef.current) {
      markBrowserUploading(1)
      uploadWarningActiveRef.current = true
    }
    try {
      const prepared = await api.createStylingImportSessions(nextJob.title, jobBatches.map((batch) => ({
        code: batch.code,
        files: formalAssetFiles(batch.files, STYLING_GARMENT_MAX_IMAGES),
      })), jobId)
      const preparedByCode = new Map(prepared.items.map((entry) => [entry.session.businessCode, entry]))
      updateJob(jobId, (job) => ({
        ...job,
        items: job.items.map((item) => {
          const entry = preparedByCode.get(item.code)
          return entry ? { ...item, sessionId: entry.session.id } : item
        }),
      }))
      if (directoryHandle) void storeDirectoryHandle(jobId, directoryHandle)
        .then(() => setSavedDirectoryJobIds((current) => new Set(current).add(jobId)))
        .catch(() => undefined)
      await runImportJob(jobId, jobBatches, nextJob.title, preparedByCode)
    } catch (err) {
      const message = err instanceof Error ? err.message : '上传任务创建失败'
      updateJob(jobId, (job) => ({
        ...job,
        status: 'FAILED',
        items: job.items.map((item) => item.status === 'WAITING' ? { ...item, status: 'FAILED', error: message } : item),
      }))
      clearImportUploadWarning()
    }
  }

  async function runImportJob(
    jobId: string,
    jobBatches: StylingImportBatch[],
    sourceTitle: string,
    preparedByCode?: Map<string | null | undefined, { session: UploadSession; items: DirectUploadItem[] }>,
  ) {
    const createdItems: StylingGarment[] = []
    let interruptedCount = 0
    if (!uploadWarningActiveRef.current) {
      markBrowserUploading(1)
      uploadWarningActiveRef.current = true
    }
    updateJob(jobId, (job) => ({ ...job, status: 'RUNNING' }))
    for (const batch of jobBatches) {
      updateJob(jobId, (job) => ({
        ...job,
        items: job.items.map((item) => item.code === batch.code ? { ...item, status: 'UPLOADING', error: undefined } : item),
        operations: job.operations.map((item) => item.providerRequestId === batch.code ? { ...item, status: 'RUNNING', startedAt: item.startedAt || new Date().toISOString() } : item),
      }))
      try {
        const uploadFiles = formalAssetFiles(batch.files, STYLING_GARMENT_MAX_IMAGES)
        let prepared = preparedByCode?.get(batch.code)
        if (prepared) {
          prepared = await api.resumeUploadSession(prepared.session.id, uploadFiles, {
            onChecksumCompatibilityMode: () => setChecksumCompatibilityMode(true),
          })
        } else {
          const item = importJobs.find((job) => job.id === jobId)?.items.find((candidate) => candidate.code === batch.code)
          if (!item?.sessionId) throw new Error(`${batch.code} 缺少可恢复的上传记录`)
          prepared = await api.resumeUploadSession(item.sessionId, uploadFiles, {
            onChecksumCompatibilityMode: () => setChecksumCompatibilityMode(true),
          })
        }
        let created: { item: StylingGarment; session: UploadSession }
        if (prepared.session.status === 'FINALIZED' && prepared.session.targetId) {
          created = { item: (await api.getStylingGarment(prepared.session.targetId)).item, session: prepared.session }
        } else if (prepared.session.status === 'FINALIZING') {
          let persisted = prepared.session
          for (let attempt = 0; attempt < 60 && persisted.status === 'FINALIZING'; attempt += 1) {
            await new Promise((resolve) => window.setTimeout(resolve, 1_000))
            persisted = (await api.getUploadSession(prepared.session.id)).session
          }
          if (persisted.status !== 'FINALIZED' || !persisted.targetId) {
            throw new Error(`${batch.code} 仍在保存中，请稍后继续任务，系统不会重复上传图片。`)
          }
          created = { item: (await api.getStylingGarment(persisted.targetId)).item, session: persisted }
        } else {
          await api.uploadPreparedSession(prepared.session, prepared.items, uploadFiles, (progress) => trackImportProgress(jobId, batch.code, progress))
          created = await api.finalizeStylingGarmentSession(prepared.session.id, batch.code)
        }
        void refreshUploadSessions().catch(() => undefined)
        createdItems.push(created.item)
        updateJob(jobId, (job) => ({
          ...job,
          uploaded: createdItems.length,
          currentCode: undefined,
          progress: null,
          items: job.items.map((item) => item.code === batch.code ? {
            ...item,
            status: 'SAVED',
            uploadedCount: Math.min(batch.files.length, STYLING_GARMENT_MAX_IMAGES),
            sessionId: created.session.id,
            garmentId: created.item.id,
          } : item),
          operations: job.operations.map((item) => item.providerRequestId === batch.code ? { ...item, status: 'SUCCEEDED', completedAt: new Date().toISOString() } : item),
        }))
      } catch (err) {
        void refreshUploadSessions().catch(() => undefined)
        const message = err instanceof Error ? err.message : '上传失败'
        const displayMessage = ['搭配服装代码已存在', '搭配服装货号已存在'].includes(message) ? '货号已存在，本次未重复上传' : message
        const interrupted = !/已存在|重复/.test(displayMessage)
        if (interrupted) interruptedCount += 1
        updateJob(jobId, (job) => ({
          ...job,
          skipped: job.skipped + 1,
          currentCode: undefined,
          progress: null,
          items: job.items.map((item) => item.code === batch.code ? { ...item, status: interrupted ? 'INTERRUPTED' : 'FAILED', error: displayMessage } : item),
          operations: job.operations.map((item) => item.providerRequestId === batch.code ? { ...item, status: 'FAILED', failedAt: new Date().toISOString(), errorMessage: displayMessage } : item),
        }))
      }
    }
    clearImportUploadWarning()
    const batchSessions = (await api.listUploadSessions({ scope: 'styling_garment', batchId: jobId, limit: 500 })).items
    const finalizedSessions = batchSessions.filter((session) => session.status === 'FINALIZED' && session.targetId)
    const batchGarments = await Promise.all(finalizedSessions.map((session) => api.getStylingGarment(session.targetId!).then((result) => result.item)))
    const tagTargets = batchGarments.filter((item) => ['DRAFT', 'TAGGING_FAILED'].includes(item.status))
    if (!batchGarments.length) {
      updateJob(jobId, (job) => ({ ...job, status: interruptedCount ? 'INTERRUPTED' : 'FAILED' }))
      return
    }
    updateJob(jobId, (job) => ({ ...job, uploaded: finalizedSessions.length }))
    if (!tagTargets.length) {
      updateJob(jobId, (job) => ({ ...job, status: interruptedCount ? 'INTERRUPTED' : job.skipped ? 'FAILED' : 'SUCCEEDED' }))
      onImported()
      void refreshUploadSessions().catch(() => undefined)
      return
    }
    updateJob(jobId, (job) => ({
      ...job,
      items: job.items.map((item) => tagTargets.some((target) => target.code === item.code) ? { ...item, status: 'TAGGING' } : item),
      operations: [
        ...batchGarments.map((item, index) => ({
          id: -(index + 1 + job.total),
          ownerType: 'styling_garment_upload',
          ownerId: item.id,
          provider: 'browser',
          status: 'SUCCEEDED',
          completedAt: new Date().toISOString(),
          providerRequestId: item.code,
        })),
        ...tagTargets.map((item, index) => ({
          id: -(index + 1 + job.total + batchGarments.length),
          ownerType: 'styling_garment_tagging',
          ownerId: item.id,
          provider: 'gemini',
          status: 'RUNNING',
          startedAt: new Date().toISOString(),
          providerRequestId: item.code,
        })),
        ...job.operations.filter((operation) => operation.status === 'FAILED'),
      ],
    }))
    try {
      const tagging = await api.tagStylingGarments(tagTargets.map((item) => item.id))
      const started = new Set(tagging.startedIds)
      tagTargets.forEach((item) => updateJobItem(jobId, item.code, started.has(item.id)
        ? { status: 'SUCCEEDED', error: undefined }
        : { status: 'FAILED', error: tagging.skipped.find((entry) => entry.id === item.id)?.reason || '未能提交识别' }))
      updateJob(jobId, (job) => ({
        ...job,
        status: interruptedCount ? 'INTERRUPTED' : job.skipped || tagging.skipped.length ? 'FAILED' : 'SUCCEEDED',
        operations: job.operations.map((operation) => operation.ownerType === 'styling_garment_tagging' ? { ...operation, status: 'SUBMITTED' } : operation),
      }))
    } catch {
      updateJob(jobId, (job) => ({ ...job, status: 'FAILED' }))
    } finally {
      onImported()
      void refreshUploadSessions().catch(() => undefined)
    }
  }

  async function resumeImportJob(jobId: string, selectedFiles: File[]) {
    if (resumingJobId) return
    const job = importJobs.find((item) => item.id === jobId)
    if (!job) return
    setResumingJobId(jobId)
    setLocalError('')
    try {
      const sessions = await Promise.all(job.items
        .filter((item) => item.sessionId)
        .map(async (item) => uploadSessions.find((session) => session.id === item.sessionId) ?? (await api.getUploadSession(item.sessionId!)).session))
      const sessionsById = new Map(sessions.map((session) => [session.id, session]))
      const expectedPaths = new Set(sessions.flatMap((session) => session.items?.map((item) => item.relativePath || item.filename) ?? []))
      const supportedFiles = selectedFiles.filter(isSupportedImageFile)
      const selectedByPath = new Map(supportedFiles.map((file) => [importPath(file), file]))
      const missing = [...expectedPaths].filter((path) => !selectedByPath.has(path))
      if (missing.length) {
        throw new Error(`所选文件夹与原上传任务不一致（缺少 ${missing.length} 张），已拒绝断点继续。`)
      }

      const resumableItems = job.items.filter((item) => {
        const session = item.sessionId ? sessionsById.get(item.sessionId) : undefined
        return session && !['FINALIZED', 'ABANDONED'].includes(session.status)
      })
      if (!resumableItems.length) {
        await runImportJob(jobId, [], job.title)
        return
      }
      const resumeBatches: StylingImportBatch[] = []
      const preparedByCode = new Map<string, { session: UploadSession; items: DirectUploadItem[] }>()
      for (const item of resumableItems) {
        const session = sessionsById.get(item.sessionId!)!
        const files = (session.items ?? []).map((sessionItem) => selectedByPath.get(sessionItem.relativePath || sessionItem.filename)).filter((file): file is File => Boolean(file))
        const prepared = await api.resumeUploadSession(session.id, files, {
          onChecksumCompatibilityMode: () => setChecksumCompatibilityMode(true),
        })
        preparedByCode.set(item.code, prepared)
        resumeBatches.push({ id: item.id, code: item.code, files })
      }
      await runImportJob(jobId, resumeBatches, job.title, preparedByCode)
    } catch (err) {
      const message = err instanceof Error ? err.message : '无法继续上传'
      setLocalError(message)
      updateJob(jobId, (job) => ({ ...job, status: 'INTERRUPTED' }))
    } finally {
      setResumingJobId(null)
    }
  }

  async function resumeFromSavedDirectory(jobId: string) {
    try {
      const handle = await getDirectoryHandle(jobId)
      if (!handle) throw new Error('未找到原文件夹授权，请重新选择原文件夹。')
      let permission = await handle.queryPermission({ mode: 'read' })
      if (permission === 'prompt') permission = await handle.requestPermission({ mode: 'read' })
      if (permission !== 'granted') throw new Error('需要重新授权原文件夹后才能继续上传。')
      await resumeImportJob(jobId, await filesFromDirectory(handle))
    } catch (err) {
      setLocalError(err instanceof Error ? err.message : '无法读取原文件夹')
    }
  }

  return (
    <div className="import-page-stack">
      <div className={`panel import-panel ${batches.length > 0 ? 'has-batches' : ''}`}>
        <div className="import-head">
          <div>
            <HeadingWithTooltip as="h3" tooltip={`散图会合并为一套；单货号文件夹是一套；上级文件夹会递归识别最底层图片目录。最多 ${STYLING_GARMENT_MAX_IMAGES} 张进入候选池，AI 识别后只保留挑选排序好的 ${STYLING_GARMENT_SELECTED_IMAGES} 张纯服装参考图。`}>上传搭配服装</HeadingWithTooltip>
          </div>
        </div>
        <div className="import-drop" onDragOver={(event) => event.preventDefault()} onDrop={(event) => void handleDrop(event)}>
          <Images size={22} />
          <strong>拖入图片或文件夹</strong>
          <span>支持多张图片、一个货号文件夹、一个上级文件夹，或拖入多个货号文件夹</span>
          <div className="import-actions">
            <label className="management-action">
              <ImagePlus size={16} /> 选择图片
              <input type="file" accept={SUPPORTED_IMAGE_ACCEPT} multiple onChange={(event) => addFiles(Array.from(event.target.files ?? []))} />
            </label>
            {supportsDirectoryAccess() ? (
              <button className="management-action" type="button" onClick={() => void selectDirectory()}><FolderOpen size={16} /> 选择文件夹</button>
            ) : (
              <label className="management-action">
                <FolderOpen size={16} /> 选择文件夹
                <input type="file" multiple {...{ webkitdirectory: '', directory: '' }} onChange={(event) => addFiles(Array.from(event.target.files ?? []))} />
              </label>
            )}
          </div>
        </div>
        {localError && <span className="inline-error">{localError}</span>}
        {checksumCompatibilityMode && <div className="import-notice is-warning" role="status">{CHECKSUM_COMPATIBILITY_NOTICE}</div>}
        {batches.length > 0 && (
          <div className="batch-list">
            {batches.map((batch) => (
              <ImportBatchCard
                key={batch.id}
                batch={batch}
                onCode={(code) => updateCode(batch.id, code)}
                onFiles={(files) => updateFiles(batch.id, files)}
                onRemove={() => removeBatch(batch.id)}
              />
            ))}
          </div>
        )}
        {batches.length > 0 && (
          <div className="import-footer-actions">
            <button
              className="management-action"
              type="button"
              onClick={onClose}
            >
              <ChevronLeft size={16} />返回服装库
            </button>
            <button className="management-action" type="button" onClick={saveDrafts}><Upload size={16} />上传</button>
          </div>
        )}
      </div>
      {hasRunningImportJobs && (
        <div className="import-upload-notice" role="status">
          <span className="import-upload-notice-icon" aria-hidden="true"><Loader2 className="spin" size={14} /></span>
          <span className="import-upload-notice-copy">
            <strong>上传任务正在进行</strong>
            <span>可以切换站内页面或浏览器 Tab；刷新或关闭页面会暂停任务，返回后可从断点继续。</span>
          </span>
          <span className="import-upload-notice-badge">{runningImportJobCount} 个进行中</span>
        </div>
      )}
      {batches.length === 0 && (
        <ImportTaskWorkbench
          jobs={importJobs}
          sessions={uploadSessions}
          garments={garments}
          onAbandon={(id) => api.abandonUploadSession(id).then(() => refreshUploadSessions())}
          onRemoveJob={(jobId) => setImportJobs((current) => current.filter((job) => job.id !== jobId))}
          onSetJobArchived={(jobId, archived) => setImportJobs((current) => current.map((job) => job.id === jobId ? {
            ...job,
            archivedAt: archived ? new Date().toISOString() : null,
            archiveReason: archived ? 'user_archived' : null,
          } : job))}
          onRefresh={refreshUploadSessions}
          onFindGarment={onFindGarment}
          onFindJob={onFindJob}
          resumingJobId={resumingJobId}
          savedDirectoryJobIds={savedDirectoryJobIds}
          onResumeSaved={(jobId) => void resumeFromSavedDirectory(jobId)}
          onResumeFiles={(jobId, files) => void resumeImportJob(jobId, files)}
        />
      )}
    </div>
  )
}

function ImportTaskWorkbench({
  jobs,
  sessions,
  garments,
  onAbandon,
  onRemoveJob,
  onSetJobArchived,
  onRefresh,
  onFindGarment,
  onFindJob,
  resumingJobId,
  savedDirectoryJobIds,
  onResumeSaved,
  onResumeFiles,
}: {
  jobs: ImportJob[]
  sessions: UploadSession[]
  garments: StylingGarment[]
  onAbandon: (id: string) => Promise<unknown>
  onRemoveJob: (jobId: string) => void
  onSetJobArchived: (jobId: string, archived: boolean) => void
  onRefresh: () => Promise<UploadSession[]>
  onFindGarment: (code: string, garmentId?: number) => void
  onFindJob: (jobId: string) => void
  resumingJobId: string | null
  savedDirectoryJobIds: ReadonlySet<string>
  onResumeSaved: (jobId: string) => void
  onResumeFiles: (jobId: string, files: File[]) => void
}) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<ImportTaskFilter>('all')
  const [uploadedFrom, setUploadedFrom] = useState('')
  const [uploadedTo, setUploadedTo] = useState('')
  const [openFilter, setOpenFilter] = useState<'date' | 'status' | null>(null)
  const [abandoningId, setAbandoningId] = useState<string | null>(null)
  const [dismissedUploadSessionIds, setDismissedUploadSessionIds] = useState<Set<string>>(() => new Set())
  const [includeArchived, setIncludeArchived] = useState(false)
  const [archivedSessionSnapshot, setArchivedSessionSnapshot] = useState<UploadSession[]>([])
  const [loadingArchived, setLoadingArchived] = useState(false)
  const [manageRecord, setManageRecord] = useState<UploadRecordTarget | null>(null)
  const [manageImpact, setManageImpact] = useState<DeleteImpact | null>(null)
  const [manageImpactLoading, setManageImpactLoading] = useState(false)
  const [manageImpactError, setManageImpactError] = useState<string | null>(null)
  const [uploadRecordOperationId, setUploadRecordOperationId] = useState<string | null>(null)
  const [uploadRecordError, setUploadRecordError] = useState<string | null>(null)
  const [bulkMode, setBulkMode] = useState(false)
  const [selectedUploadRecordIds, setSelectedUploadRecordIds] = useState<Set<string>>(() => new Set())
  const [batchArchiving, setBatchArchiving] = useState(false)
  const [batchArchiveCount, setBatchArchiveCount] = useState(0)
  const lastSelectedUploadRecordIdRef = useRef<string | null>(null)
  const manageImpactRequestRef = useRef(0)
  const [expandedTasks, setExpandedTasks] = useState<Set<string>>(() => new Set())
  const [offset, setOffset] = useState(0)
  const [pageSize, setPageSize] = useState(8)
  const garmentsById = useMemo(() => new Map(garments.map((item) => [item.id, item])), [garments])
  const garmentsByCode = useMemo(() => new Map(garments.map((item) => [item.code, item])), [garments])
  const mergedUploadSessions = useMemo(() => {
    const merged = new Map(archivedSessionSnapshot.map((item) => [item.id, item]))
    sessions.forEach((session) => {
      const snapshot = merged.get(session.id)
      merged.set(session.id, snapshot?.archivedAt
        ? { ...snapshot, ...session, archivedAt: snapshot.archivedAt, archiveReason: snapshot.archiveReason }
        : session)
    })
    return Array.from(merged.values())
  }, [archivedSessionSnapshot, sessions])
  const displayedSessions = useMemo(() => mergedUploadSessions
    .filter((item) => includeArchived || !item.archivedAt), [includeArchived, mergedUploadSessions])
  const sessionsById = useMemo(() => new Map(displayedSessions.map((item) => [item.id, item])), [displayedSessions])
  const allSessionsById = useMemo(() => new Map(mergedUploadSessions.map((item) => [item.id, item])), [mergedUploadSessions])
  const jobSessionIds = useMemo(() => new Set(jobs.flatMap((job) => job.items.map((item) => item.sessionId).filter(Boolean) as string[])), [jobs])

  const reconciledJobs = useMemo(() => jobs.map((job) => {
    const linked = job.items.map((item) => item.sessionId ? allSessionsById.get(item.sessionId) : undefined).filter(Boolean) as UploadSession[]
    if (!linked.length) return job
    const archivedAt = linked.every((session) => session.archivedAt) ? linked.find((session) => session.archivedAt)?.archivedAt ?? job.archivedAt : null
    return { ...job, archivedAt, archiveReason: archivedAt ? linked.find((session) => session.archiveReason)?.archiveReason ?? 'user_archived' : null }
  }), [allSessionsById, jobs])
  const displayedJobs = useMemo(() => reconciledJobs.filter((job) => includeArchived || !job.archivedAt), [includeArchived, reconciledJobs])
  const jobRows = useMemo<ImportTaskRow[]>(() => displayedJobs.flatMap((job) => {
    const createdAt = job.createdAt ?? job.operations.find((item) => item.startedAt)?.startedAt ?? job.operations.find((item) => item.completedAt)?.completedAt ?? undefined
    const updatedAt = job.updatedAt ?? [...job.operations]
      .map((item) => item.completedAt ?? item.failedAt ?? item.startedAt)
      .filter(Boolean)
      .sort()
      .at(-1) ?? createdAt ?? undefined
    return job.items.map((item) => {
      const session = item.sessionId ? sessionsById.get(item.sessionId) : undefined
      const targetId = session?.targetId ?? item.garmentId
      const garment = targetId ? garmentsById.get(targetId) : garmentsByCode.get(item.code)
      const sessionDrivesStatus = Boolean(session && uploadSessionDrivesImportItem(item.status))
      const status = garment?.status ?? (sessionDrivesStatus && session ? session.status : item.status)
      return {
        id: `job-${job.id}-${item.id}`,
        source: 'job',
        batchId: job.id,
        serverBatchId: session?.batchId,
        sessionId: item.sessionId,
        garmentId: targetId ?? undefined,
        scope: session?.scope ?? 'styling-garment',
        sourceTitle: session?.sourceTitle ?? job.title,
        code: item.code,
        importedCount: item.imageCount,
        uploadedCount: session?.uploadedCount ?? item.uploadedCount,
        retainedCount: retainedImageCount(garment),
        status,
        statusLabel: importTaskStatusLabel(status),
        statusTone: importTaskStatusTone(status),
        previewUrl: garment?.coverUrl ?? item.previewUrl ?? null,
        createdAt,
        updatedAt: garment?.updatedAt ?? (sessionDrivesStatus ? session?.updatedAt ?? updatedAt : updatedAt),
        error: sessionDrivesStatus ? session?.errorMessage ?? item.error : item.error,
        archivedAt: session?.archivedAt ?? job.archivedAt,
        archiveReason: session?.archiveReason ?? job.archiveReason,
      }
    })
  }), [displayedJobs, garmentsByCode, garmentsById, sessionsById])

  const sessionRows = useMemo<ImportTaskRow[]>(() => displayedSessions
    .filter((session) => session.scope === 'styling-garment' && !jobSessionIds.has(session.id) && !dismissedUploadSessionIds.has(session.id))
    .map((session) => {
      const garment = session.targetId ? garmentsById.get(session.targetId) : undefined
      const status = importRowBusinessStatus(session, garment, session.status)
      return {
        id: `session-${session.id}`,
        source: 'session',
        sessionId: session.id,
        serverBatchId: session.batchId,
        garmentId: session.targetId ?? undefined,
        scope: session.scope,
        sourceTitle: session.sourceTitle,
        code: importSessionCode(session, garment),
        importedCount: session.totalCount,
        uploadedCount: session.uploadedCount,
        retainedCount: retainedImageCount(garment),
        status,
        statusLabel: importTaskStatusLabel(status),
        statusTone: importTaskStatusTone(status),
        previewUrl: garment?.coverUrl ?? null,
        createdAt: session.createdAt,
        updatedAt: garment?.updatedAt ?? session.updatedAt ?? session.createdAt,
        error: session.errorMessage,
        archivedAt: session.archivedAt,
        archiveReason: session.archiveReason,
      }
    }), [dismissedUploadSessionIds, displayedSessions, garmentsById, jobSessionIds])

  const allRows = useMemo(() => [...jobRows, ...sessionRows], [jobRows, sessionRows])
  const queryTerms = splitSearchTerms(query).map((term) => term.toLocaleLowerCase())
  const titleMatchedJobIds = new Set(displayedJobs
    .filter((job) => {
      if (!queryTerms.length) return false
      const rows = jobRows.filter((row) => row.batchId === job.id)
      const title = importJobDisplayTitle(job, rows).toLocaleLowerCase()
      return queryTerms.some((term) => title.includes(term))
    })
    .map((job) => job.id))
  const sortedRows = useMemo(() => [...allRows].sort((a, b) => {
    const av = new Date(parseRuyiTime(a.createdAt ?? a.updatedAt)).getTime()
    const bv = new Date(parseRuyiTime(b.createdAt ?? b.updatedAt)).getTime()
    return bv - av
  }), [allRows])
  const filteredRows = sortedRows.filter((row) => {
    const searchableText = [
      row.code,
      row.sessionId,
      row.statusLabel,
      row.error,
    ].filter(Boolean).join(' ').toLocaleLowerCase()
    const matchesKeyword = !queryTerms.length
      || queryTerms.some((term) => searchableText.includes(term))
      || (row.batchId ? titleMatchedJobIds.has(row.batchId) : false)
    if (!matchesKeyword) return false
    const uploadedAt = new Date(parseRuyiTime(row.createdAt ?? row.updatedAt)).getTime()
    if (uploadedFrom) {
      const from = new Date(`${uploadedFrom}T00:00:00`).getTime()
      if (uploadedAt < from) return false
    }
    if (uploadedTo) {
      const to = new Date(`${uploadedTo}T23:59:59`).getTime()
      if (uploadedAt > to) return false
    }
    if (filter === 'running') return isImportTaskRunning(row.status)
    if (filter === 'pending') return isImportTaskPendingRecognition(row.status)
    if (filter === 'attention') return isImportTaskAttention(row.status)
    if (filter === 'saved') return isImportTaskDone(row.status)
    return true
  })
  const filteredRowIds = new Set(filteredRows.map((row) => row.id))
  const rowsByBatch = new Map<string, ImportTaskRow[]>()
  jobRows.forEach((row) => {
    if (!row.batchId || !filteredRowIds.has(row.id)) return
    const list = rowsByBatch.get(row.batchId) ?? []
    list.push(row)
    rowsByBatch.set(row.batchId, list)
  })
  const visibleSessionRows = sessionRows.filter((row) => filteredRowIds.has(row.id))
  const visibleSessionGroups = recoveredImportSessionGroups(visibleSessionRows, sessionsById)
  const runningCount = allRows.filter((row) => isImportTaskRunning(row.status)).length
  const pendingCount = allRows.filter((row) => isImportTaskPendingRecognition(row.status)).length
  const attentionCount = allRows.filter((row) => isImportTaskAttention(row.status)).length
  const statusOptions = [
    { value: 'running', label: '进行中', count: runningCount },
    { value: 'pending', label: '待识别', count: pendingCount },
    { value: 'attention', label: '需处理', count: attentionCount },
    { value: 'saved', label: '可用', count: allRows.filter((row) => isImportTaskDone(row.status)).length },
  ]
  const hasActiveFilters = Boolean(query.trim() || filter !== 'all' || uploadedFrom || uploadedTo)
  const visibleJobs = [...displayedJobs].sort((a, b) => {
    const av = new Date(parseRuyiTime(a.createdAt ?? a.updatedAt)).getTime()
    const bv = new Date(parseRuyiTime(b.createdAt ?? b.updatedAt)).getTime()
    return bv - av
  })
  const visibleJobIds = new Set(visibleJobs
    .filter((job) => jobRows.some((row) => row.batchId === job.id && filteredRowIds.has(row.id)))
    .map((job) => job.id))
  const recoveredRecords = visibleSessionGroups.map((group) => ({
    kind: 'session' as const,
    id: group.id,
    group,
  }))
  const importRecords = [
    ...visibleJobs.filter((job) => visibleJobIds.has(job.id)).map((job) => ({ kind: 'job' as const, id: job.id, job })),
    ...recoveredRecords,
  ].sort((a, b) => {
    const aTime = a.kind === 'job' ? a.job.updatedAt ?? a.job.createdAt : a.group.updatedAt ?? a.group.createdAt
    const bTime = b.kind === 'job' ? b.job.updatedAt ?? b.job.createdAt : b.group.updatedAt ?? b.group.createdAt
    return new Date(parseRuyiTime(bTime)).getTime() - new Date(parseRuyiTime(aTime)).getTime()
  })
  const totalRecords = importRecords.length
  const totalPages = totalRecords > 0 ? Math.ceil(totalRecords / pageSize) : 0
  const pagedRecords = importRecords.slice(offset, offset + pageSize)
  const selectableUploadRecords = pagedRecords.filter((record) => isUploadRecordSelectable(uploadRecordTarget(record)))
  const selectableUploadRecordIds = selectableUploadRecords.map(uploadRecordSelectionId)
  const selectedUploadRecords = selectableUploadRecords.filter((record) => selectedUploadRecordIds.has(uploadRecordSelectionId(record)))
  const allVisibleUploadRecordsSelected = selectableUploadRecordIds.length > 0 && selectableUploadRecordIds.every((id) => selectedUploadRecordIds.has(id))
  const uploadRecordMarquee = useMarqueeSelection<string>({
    enabled: bulkMode && !batchArchiving,
    selected: selectedUploadRecordIds,
    onChange: setSelectedUploadRecordIds,
  })

  useEffect(() => {
    setOffset(0)
  }, [query, filter, uploadedFrom, uploadedTo])

  useEffect(() => {
    setSelectedUploadRecordIds(new Set())
    lastSelectedUploadRecordIdRef.current = null
  }, [offset, pageSize, query, filter, uploadedFrom, uploadedTo, includeArchived])

  useEffect(() => {
    if (offset < totalRecords || offset === 0) return
    setOffset(Math.max(0, (Math.max(1, totalPages) - 1) * pageSize))
  }, [offset, pageSize, totalPages, totalRecords])

  function toggleTask(id: string) {
    setExpandedTasks((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function summarizeRows(rows: ImportTaskRow[]) {
    const imported = rows.reduce((sum, row) => sum + row.importedCount, 0)
    const uploaded = rows.reduce((sum, row) => sum + row.uploadedCount, 0)
    const retainedValues = rows.map((row) => row.retainedCount).filter((value): value is number => typeof value === 'number')
    const retained = retainedValues.length ? retainedValues.reduce((sum, value) => sum + value, 0) : null
    const failed = rows.filter((row) => row.statusTone === 'danger').length
    const interrupted = rows.filter((row) => row.status === 'INTERRUPTED').length
    const uploading = rows.filter((row) => ['WAITING', 'UPLOADING', 'PENDING'].includes(row.status)).length
    const organizing = rows.filter((row) => ['SAVING', 'UPLOADED', 'FINALIZING'].includes(row.status)).length
    const tagging = rows.filter((row) => ['TAGGING', 'TAGGING_RUNNING'].includes(row.status)).length
    const running = rows.filter((row) => isImportTaskRunning(row.status)).length
    const review = rows.filter((row) => row.status === 'TAGGING_READY').length
    const pendingRecognition = rows.filter((row) => isImportTaskPendingRecognition(row.status)).length
    const ready = rows.filter((row) => row.status === 'READY').length
    const finalized = rows.filter((row) => row.garmentId || isImportTaskDone(row.status) || ['DRAFT', 'TAGGING_RUNNING', 'TAGGING_READY', 'TAGGING_FAILED'].includes(row.status)).length
    const saved = finalized
    const hasActiveWork = uploading > 0 || organizing > 0 || tagging > 0 || running > 0
    const tone: ImportTaskRow['statusTone'] = failed ? 'danger' : interrupted ? 'warning' : uploading || tagging || running ? 'running' : ready === rows.length ? 'success' : pendingRecognition || review ? 'warning' : 'warning'
    const label = failed ? '部分失败' : interrupted && hasActiveWork ? '部分上传已暂停' : interrupted ? '上传已暂停' : uploading ? '上传中' : tagging ? 'AI 打标中' : organizing ? '整理入库中' : ready === rows.length ? '可用' : review ? '待复核' : pendingRecognition ? '待识别' : saved === rows.length ? '已入库' : '需处理'
    const updatedAt = rows.map((row) => row.updatedAt ?? row.createdAt).filter(Boolean).sort().at(-1)
    return { imported, uploaded, retained, failed, interrupted, running, uploading, organizing, tagging, saved, tone, label, updatedAt }
  }

  function clearImportFilters() {
    setQuery('')
    setFilter('all')
    setUploadedFrom('')
    setUploadedTo('')
    setOpenFilter(null)
  }

  async function abandon(sessionId: string) {
    if (abandoningId) return
    setAbandoningId(sessionId)
    try {
      await onAbandon(sessionId)
    } finally {
      setAbandoningId(null)
    }
  }

  function uploadRecordSessionIds(rows: ImportTaskRow[]) {
    return Array.from(new Set(rows.map((row) => row.sessionId).filter(Boolean) as string[]))
  }

  function uploadRecordSelectionId(record: (typeof importRecords)[number]) {
    return `${record.kind}:${record.id}`
  }

  function uploadRecordTarget(record: (typeof importRecords)[number]): UploadRecordTarget {
    if (record.kind === 'session') {
      return {
        id: record.group.id,
        title: record.group.title,
        rows: record.group.rows,
        archived: record.group.rows.every((row) => Boolean(row.archivedAt)),
      }
    }
    const rows = jobRows.filter((row) => row.batchId === record.job.id)
    return {
      id: record.job.id,
      title: importJobDisplayTitle(record.job, rows),
      rows,
      jobId: record.job.id,
      archived: Boolean(record.job.archivedAt || rows.every((row) => row.archivedAt)),
    }
  }

  function isUploadRecordSelectable(target: UploadRecordTarget) {
    return !target.archived && !target.rows.some((row) => row.archiveReason === 'deletion_pending')
  }

  function toggleUploadRecordSelection(id: string, checked: boolean, extend = false) {
    setSelectedUploadRecordIds((current) => {
      const next = new Set(current)
      if (extend && lastSelectedUploadRecordIdRef.current !== null) {
        const start = selectableUploadRecordIds.indexOf(lastSelectedUploadRecordIdRef.current)
        const end = selectableUploadRecordIds.indexOf(id)
        if (start >= 0 && end >= 0) {
          selectableUploadRecordIds.slice(Math.min(start, end), Math.max(start, end) + 1).forEach((rangeId) => {
            if (checked) next.add(rangeId)
            else next.delete(rangeId)
          })
          lastSelectedUploadRecordIdRef.current = id
          return next
        }
      }
      if (checked) next.add(id)
      else next.delete(id)
      lastSelectedUploadRecordIdRef.current = id
      return next
    })
  }

  function toggleVisibleUploadRecordSelection(checked: boolean) {
    setSelectedUploadRecordIds((current) => {
      const next = new Set(current)
      selectableUploadRecordIds.forEach((id) => {
        if (checked) next.add(id)
        else next.delete(id)
      })
      return next
    })
  }

  async function loadArchivedSessions() {
    setLoadingArchived(true)
    try {
      const result = await api.listUploadSessions({ scope: 'styling_garment', includeArchived: true, includeAbandoned: true, limit: 500 })
      setArchivedSessionSnapshot(result.items)
      setUploadRecordError(null)
    } finally {
      setLoadingArchived(false)
    }
  }

  useEffect(() => {
    void loadArchivedSessions().catch((error) => setUploadRecordError(error instanceof Error ? error.message : '无法读取上传记录'))
  }, [])

  useEffect(() => {
    if (loadingArchived || uploadRecordOperationId || batchArchiving) return
    reconciledJobs.forEach((job) => {
      const original = jobs.find((item) => item.id === job.id)
      if (original && Boolean(original.archivedAt) !== Boolean(job.archivedAt)) onSetJobArchived(job.id, Boolean(job.archivedAt))
    })
  }, [batchArchiving, jobs, loadingArchived, reconciledJobs, uploadRecordOperationId])

  function openUploadRecordManage(target: UploadRecordTarget) {
    if (batchArchiving) return
    setManageRecord(target)
    setManageImpact(null)
    setManageImpactError(null)
    const sessionIds = uploadRecordSessionIds(target.rows)
    if (!sessionIds.length) {
      setManageImpact({ uploadSessionCount: 0, uploadItemCount: 0, temporaryObjectCount: 0, linkedBusinessCount: 0, estimatedBytes: 0 })
      setManageImpactLoading(false)
      return
    }
    const requestId = ++manageImpactRequestRef.current
    setManageImpactLoading(true)
    void api.getUploadSessionDeleteImpact(sessionIds)
      .then((result) => {
        if (manageImpactRequestRef.current === requestId) setManageImpact(result.impact)
      })
      .catch((error) => {
        if (manageImpactRequestRef.current !== requestId) return
        setManageImpact(null)
        setManageImpactError(error instanceof Error ? error.message : '影响预检失败')
      })
      .finally(() => {
        if (manageImpactRequestRef.current === requestId) setManageImpactLoading(false)
      })
  }

  function closeUploadRecordManage() {
    if (uploadRecordOperationId) return
    manageImpactRequestRef.current += 1
    setManageRecord(null)
  }

  async function refreshManagedUploadRecords() {
    await onRefresh().catch(() => [])
    await loadArchivedSessions().catch(() => undefined)
  }

  async function archiveUploadRecord(target: UploadRecordTarget) {
    if (batchArchiving || uploadRecordOperationId) return
    setUploadRecordOperationId(target.id)
    setUploadRecordError(null)
    const sessionIds = uploadRecordSessionIds(target.rows)
    try {
      for (let index = 0; index < sessionIds.length; index += UPLOAD_SESSION_ARCHIVE_BATCH_SIZE) {
        await api.archiveUploadSessions(sessionIds.slice(index, index + UPLOAD_SESSION_ARCHIVE_BATCH_SIZE))
      }
      const archivedAt = new Date().toISOString()
      setArchivedSessionSnapshot((current) => current.map((session) => sessionIds.includes(session.id) ? { ...session, archivedAt, archiveReason: 'user_archived' } : session))
      if (target.jobId) onSetJobArchived(target.jobId, true)
      if (!includeArchived) setDismissedUploadSessionIds((current) => new Set([...current, ...sessionIds]))
      setManageRecord(null)
      await refreshManagedUploadRecords()
    } catch (error) {
      await refreshManagedUploadRecords()
      throw error
    } finally {
      setUploadRecordOperationId(null)
    }
  }

  async function archiveSelectedUploadRecords() {
    if (batchArchiving || !selectedUploadRecords.length) return
    const targets = selectedUploadRecords.map(uploadRecordTarget)
    const sessionIds = Array.from(new Set(targets.flatMap((target) => uploadRecordSessionIds(target.rows))))
    setBatchArchiving(true)
    setBatchArchiveCount(targets.length)
    setUploadRecordError(null)
    try {
      for (let index = 0; index < sessionIds.length; index += UPLOAD_SESSION_ARCHIVE_BATCH_SIZE) {
        await api.archiveUploadSessions(sessionIds.slice(index, index + UPLOAD_SESSION_ARCHIVE_BATCH_SIZE))
      }
      const archivedAt = new Date().toISOString()
      setArchivedSessionSnapshot((current) => current.map((session) => sessionIds.includes(session.id) ? { ...session, archivedAt, archiveReason: 'user_archived' } : session))
      targets.forEach((target) => {
        if (target.jobId) onSetJobArchived(target.jobId, true)
      })
      if (!includeArchived) setDismissedUploadSessionIds((current) => new Set([...current, ...sessionIds]))
      setSelectedUploadRecordIds(new Set())
      lastSelectedUploadRecordIdRef.current = null
      await refreshManagedUploadRecords()
    } catch (error) {
      await refreshManagedUploadRecords()
      setUploadRecordError(error instanceof Error ? error.message : '批量归档上传记录失败')
    } finally {
      setBatchArchiving(false)
      setBatchArchiveCount(0)
    }
  }

  async function restoreUploadRecord(target: UploadRecordTarget) {
    if (batchArchiving || uploadRecordOperationId) return
    setUploadRecordOperationId(target.id)
    setUploadRecordError(null)
    const sessionIds = uploadRecordSessionIds(target.rows)
    try {
      if (sessionIds.length) await api.restoreUploadSessions(sessionIds)
      setArchivedSessionSnapshot((current) => current.map((session) => sessionIds.includes(session.id) ? { ...session, archivedAt: null, archiveReason: null } : session))
      if (target.jobId) onSetJobArchived(target.jobId, false)
      setDismissedUploadSessionIds((current) => {
        const next = new Set(current)
        sessionIds.forEach((id) => next.delete(id))
        return next
      })
      await refreshManagedUploadRecords()
    } catch (error) {
      setUploadRecordError(error instanceof Error ? error.message : '恢复上传记录失败')
    } finally {
      setUploadRecordOperationId(null)
    }
  }

  async function purgeUploadRecord(target: UploadRecordTarget, confirmText: string) {
    if (batchArchiving || uploadRecordOperationId) return
    setUploadRecordOperationId(target.id)
    setUploadRecordError(null)
    try {
      const sessionIds = uploadRecordSessionIds(target.rows)
      let cleanupErrors: ObjectCleanupError[] = []
      let deferredIds: string[] = []
      if (sessionIds.length) {
        const result = await api.deleteUploadSessions(sessionIds, sessionIds.length === 1 ? { confirmName: confirmText } : { confirmPhrase: confirmText })
        cleanupErrors = result.objectCleanupErrors
        deferredIds = result.deferredIds
      }
      setArchivedSessionSnapshot((current) => current
        .filter((session) => !sessionIds.includes(session.id) || deferredIds.includes(session.id))
        .map((session) => deferredIds.includes(session.id) ? { ...session, archivedAt: new Date().toISOString(), archiveReason: 'deletion_pending' } : session))
      setDismissedUploadSessionIds((current) => new Set([...current, ...sessionIds]))
      if (target.jobId) onRemoveJob(target.jobId)
      await refreshManagedUploadRecords()
      setManageRecord(null)
      if (cleanupErrors.length) setUploadRecordError(`上传记录已删除，但有 ${cleanupErrors.length} 个临时文件清理失败，系统已记录并会自动重试。`)
      else if (deferredIds.length) setUploadRecordError('上传已停止；相关临时文件将在上传地址安全失效后自动清理。')
    } finally {
      setUploadRecordOperationId(null)
    }
  }

  return (
    <div className="panel import-workbench">
      <div className="import-workbench-head">
        <div>
          <strong>上传记录</strong>
        </div>
      </div>
      <div className="import-commandbar">
        <SearchBox
          value={query}
          onChange={setQuery}
          placeholder="搜索货号 / 上传任务"
          panelPlaceholder="每行一个货号、上传任务名、状态或任务编号，也可用空格、逗号、句号、顿号分隔"
        />
        <div className="import-filter-pills">
          <DateRangeFilter
            open={openFilter === 'date'}
            onOpen={(open) => setOpenFilter(open ? 'date' : null)}
            label="上传时间"
            from={uploadedFrom}
            to={uploadedTo}
            onFrom={setUploadedFrom}
            onTo={setUploadedTo}
          />
          <StatusFilter
            open={openFilter === 'status'}
            onOpen={(open) => setOpenFilter(open ? 'status' : null)}
            value={filter === 'all' ? '' : filter}
            options={statusOptions}
            formatLabel={(value) => statusOptions.find((item) => item.value === value)?.label ?? value}
            onChange={(value) => setFilter((value || 'all') as ImportTaskFilter)}
          />
          <button className={`filter-pill checkbox-pill ${includeArchived ? 'is-active' : ''}`} type="button" disabled={loadingArchived} onClick={() => {
            const next = !includeArchived
            setIncludeArchived(next)
            if (next) void loadArchivedSessions().catch((error) => setUploadRecordError(error instanceof Error ? error.message : '无法读取已归档记录'))
          }}>
            <Check size={15} /><span>{loadingArchived ? '读取中' : '查看已归档'}</span>
          </button>
          {hasActiveFilters && <button className="filter-pill reset-filter-pill" type="button" onClick={clearImportFilters}><span>清空筛选</span></button>}
        </div>
        <div className="library-management-tools import-management-tools">
          {bulkMode && (selectedUploadRecords.length > 0 || batchArchiving) && (
            <button className="management-action" type="button" disabled={batchArchiving} onClick={() => void archiveSelectedUploadRecords()}>
              <Trash2 size={15} />{batchArchiving ? `正在归档 ${batchArchiveCount} 项...` : `归档 ${selectedUploadRecords.length} 项`}
            </button>
          )}
          {bulkMode && <button className={`management-action management-check-action ${allVisibleUploadRecordsSelected ? 'is-active' : ''}`} type="button" disabled={batchArchiving || !selectableUploadRecordIds.length} onClick={() => toggleVisibleUploadRecordSelection(!allVisibleUploadRecordsSelected)}><Check size={15} /> 全选本页</button>}
          {bulkMode && <span className="bulk-selection-hint">拖动框选 · Shift 连选</span>}
          <button className="management-action compact-control" type="button" disabled={batchArchiving} onClick={() => {
            setBulkMode((value) => !value)
            setSelectedUploadRecordIds(new Set())
            lastSelectedUploadRecordIdRef.current = null
          }}>{bulkMode ? '退出批量' : '批量管理'}</button>
        </div>
      </div>
      {uploadRecordError && <div className="inline-error" role="alert">{uploadRecordError}</div>}

      <div
        ref={uploadRecordMarquee.containerRef}
        className={`record-list-body import-record-list-body marquee-selection-surface ${bulkMode ? 'is-bulk-selecting' : ''}`}
        onPointerDown={uploadRecordMarquee.onPointerDown}
        onClickCapture={uploadRecordMarquee.onClickCapture}
      >
        <MarqueeSelectionBox box={uploadRecordMarquee.box} />
        {pagedRecords.map((record) => {
          const selectionId = uploadRecordSelectionId(record)
          const selectable = isUploadRecordSelectable(uploadRecordTarget(record))
          const selected = selectedUploadRecordIds.has(selectionId)
          if (record.kind === 'session') {
            const group = record.group
            const expanded = expandedTasks.has(group.id)
            const summary = summarizeRows(group.rows)
            const title = group.title
            const archived = group.rows.every((row) => row.archivedAt)
            const deleting = group.rows.some((row) => row.archiveReason === 'deletion_pending')
            const manageTarget = { id: group.id, title, rows: group.rows, archived }
            return (
              <section
                className={`batch-record-shell import-record-shell ${expanded ? 'is-expanded' : ''}`}
                key={group.id}
                data-select-id={bulkMode && selectable ? selectionId : undefined}
                data-select-disabled={bulkMode && !selectable ? 'true' : undefined}
              >
                <div
                  className={`record-row batch-record-row import-record-row ${bulkMode ? 'has-selection' : ''} ${selected ? 'is-selected' : ''}`}
                  role="group"
                  aria-label={title}
                  title={expanded ? '点击收起上传任务' : '点击展开上传任务'}
                  onClick={(event) => {
                    if ((event.target as HTMLElement).closest('input, button, label, a')) return
                    toggleTask(group.id)
                  }}
                >
                  {bulkMode && <BulkCheckbox checked={selected} disabled={batchArchiving || !selectable} label={`选择 ${title}`} onChange={(checked, extend) => toggleUploadRecordSelection(selectionId, checked, extend)} />}
                  <div className="import-record-main">
                    <ImportThumbStack rows={group.rows} />
                    <div>
                      <TruncatedText as="strong" text={title} />
                      <TruncatedText text={`${summary.label} · ${group.total} 个货号 · 上传 ${dateText(group.createdAt)} · 更新 ${dateText(summary.updatedAt)}`} />
                    </div>
                  </div>
                  <div className="batch-progress-strip import-progress-strip">
                    <span>{summary.saved}/{group.total} 已整理</span>
                    {summary.interrupted > 0 && <span>{summary.interrupted} 已暂停</span>}
                    {summary.uploading > 0 && <span>{summary.uploading} 上传中</span>}
                    {summary.organizing > 0 && <span>{summary.organizing} 整理中</span>}
                    {summary.tagging > 0 && <span>{summary.tagging} AI 打标中</span>}
                    {summary.failed > 0 && <span>{summary.failed} 需处理</span>}
                    <span>{importImageCountText(summary.imported, summary.uploaded, summary.retained)} 张</span>
                  </div>
                  <span className={`import-status-badge is-${summary.tone}`}>{summary.label}</span>
                  <div className="import-record-actions" onClick={(event) => event.stopPropagation()}>
                    {deleting
                      ? <button className="management-action compact-control" type="button" disabled><Loader2 className="spin" size={14} />等待清理</button>
                      : <>{archived && <button className="management-action compact-control" type="button" disabled={batchArchiving || uploadRecordOperationId === group.id} onClick={() => void restoreUploadRecord(manageTarget)}><RefreshCw size={14} />恢复</button>}
                        <button className="management-action compact-control" type="button" disabled={batchArchiving || uploadRecordOperationId === group.id} onClick={() => openUploadRecordManage(manageTarget)}><Trash2 size={14} />{archived ? '删除' : '归档'}</button></>}
                  </div>
                  <button className="management-action record-action-button" aria-expanded={expanded} title={expanded ? '收起上传任务' : '展开上传任务'} onClick={(event) => {
                    event.stopPropagation()
                    toggleTask(group.id)
                  }}>
                    <ChevronDown size={14} className={expanded ? 'is-rotated' : ''} />{expanded ? '收起' : '展开'}
                  </button>
                </div>
                {expanded && (
                  <div className="batch-expanded-list import-expanded-list">
                    <ImportTaskRows rows={group.rows} abandoningId={abandoningId} onAbandon={(id) => void abandon(id)} onFindGarment={onFindGarment} />
                  </div>
                )}
              </section>
            )
          }
          const job = record.job
          const rows = rowsByBatch.get(job.id) ?? []
          if (!rows.length) return null
          const allJobRows = jobRows.filter((row) => row.batchId === job.id)
          const summary = summarizeRows(allJobRows)
          const expanded = expandedTasks.has(job.id)
          const title = importJobDisplayTitle(job, allJobRows)
          const archived = Boolean(job.archivedAt || allJobRows.every((row) => row.archivedAt))
          const deleting = allJobRows.some((row) => row.archiveReason === 'deletion_pending')
          const manageTarget = { id: job.id, title, rows: allJobRows, jobId: job.id, archived }
          const canResume = !archived && job.status === 'INTERRUPTED'
          const canResumeSavedDirectory = canResume && savedDirectoryJobIds.has(job.id)
          return (
            <section
              className={`batch-record-shell import-record-shell ${expanded ? 'is-expanded' : ''}`}
              key={job.id}
              data-select-id={bulkMode && selectable ? selectionId : undefined}
              data-select-disabled={bulkMode && !selectable ? 'true' : undefined}
            >
              <div
                className={`record-row batch-record-row import-record-row ${bulkMode ? 'has-selection' : ''} ${selected ? 'is-selected' : ''}`}
                role="group"
                aria-label={title}
                title={expanded ? '点击收起上传任务' : '点击展开上传任务'}
                onClick={(event) => {
                  if ((event.target as HTMLElement).closest('input, button, label, a')) return
                  toggleTask(job.id)
                }}
              >
                {bulkMode && <BulkCheckbox checked={selected} disabled={batchArchiving || !selectable} label={`选择 ${title}`} onChange={(checked, extend) => toggleUploadRecordSelection(selectionId, checked, extend)} />}
                <div className="import-record-main">
                  <ImportThumbStack rows={allJobRows} />
                  <div>
                    <TruncatedText as="strong" text={title} />
                    <TruncatedText text={`${summary.label} · ${job.total} 个货号 · 上传 ${dateText(job.createdAt)} · 更新 ${dateText(summary.updatedAt)}`} />
                  </div>
                </div>
                <div className="batch-progress-strip import-progress-strip">
                  <span>{summary.saved}/{job.total} 已整理</span>
                  {summary.interrupted > 0 && <span>{summary.interrupted} 已暂停</span>}
                  {summary.uploading > 0 && <span>{summary.uploading} 上传中</span>}
                  {summary.organizing > 0 && <span>{summary.organizing} 整理中</span>}
                  {summary.tagging > 0 && <span>{summary.tagging} AI 打标中</span>}
                  {summary.failed > 0 && <span>{summary.failed} 需处理</span>}
                  <span>{importImageCountText(summary.imported, summary.uploaded, summary.retained)} 张</span>
                </div>
                <span className={`import-status-badge is-${summary.tone}`}>{summary.label}</span>
                <div className="import-record-actions" onClick={(event) => event.stopPropagation()}>
                  {canResume && (
                    <div className="import-resume-actions">
                      {canResumeSavedDirectory && (
                        <button className="management-action compact-control" type="button" disabled={batchArchiving || resumingJobId === job.id} onClick={() => onResumeSaved(job.id)}>
                          <RefreshCw size={14} />{resumingJobId === job.id ? '恢复中' : '继续上传'}
                        </button>
                      )}
                      <label className={`management-action compact-control ${batchArchiving ? 'is-disabled' : ''}`} aria-disabled={batchArchiving} title="重新选择原任务文件夹；系统会严格核对后从断点继续">
                        <FolderOpen size={14} />重新选择原目录
                        <input type="file" multiple {...{ webkitdirectory: '', directory: '' }} disabled={batchArchiving || resumingJobId === job.id} onChange={(event) => {
                          const files = Array.from(event.target.files ?? [])
                          event.target.value = ''
                          if (files.length) onResumeFiles(job.id, files)
                        }} />
                      </label>
                    </div>
                  )}
                  {!canResume && !archived && <button className="management-action compact-control import-batch-view-button" type="button" title="回到服装库查看本次上传的服装" onClick={() => onFindJob(job.id)}>
                    查看本批次
                  </button>}
                  {deleting
                    ? <button className="management-action compact-control" type="button" disabled><Loader2 className="spin" size={14} />等待清理</button>
                    : <>{archived && <button className="management-action compact-control" type="button" disabled={batchArchiving || uploadRecordOperationId === job.id} onClick={() => void restoreUploadRecord(manageTarget)}><RefreshCw size={14} />恢复</button>}
                      <button className="management-action compact-control" type="button" disabled={batchArchiving || uploadRecordOperationId === job.id} onClick={() => openUploadRecordManage(manageTarget)}><Trash2 size={14} />{archived ? '删除' : '归档'}</button></>}
                </div>
                <button className="management-action record-action-button" aria-expanded={expanded} title={expanded ? '收起上传任务' : '展开上传任务'} onClick={(event) => {
                  event.stopPropagation()
                  toggleTask(job.id)
                }}>
                  <ChevronDown size={14} className={expanded ? 'is-rotated' : ''} />{expanded ? '收起' : '展开'}
                </button>
              </div>
              {expanded && (
                <div className="batch-expanded-list import-expanded-list">
                  <ImportTaskRows rows={rows} abandoningId={abandoningId} onAbandon={(id) => void abandon(id)} onFindGarment={onFindGarment} />
                </div>
              )}
            </section>
          )
        })}
        {!filteredRows.length && (
          <div className="import-empty-state">
            <Search size={18} />
            <strong>没有匹配的上传记录</strong>
            <span>换一个货号关键词或状态筛选再试。</span>
          </div>
        )}
      </div>
      <div className="record-pagination-stick import-pagination-stick">
        <PaginationControls
          offset={offset}
          pageSize={pageSize}
          pageSizeOptions={[8, 16, 32]}
          pageSizeUnit="行"
          total={totalRecords}
          totalPages={totalPages}
          onOffsetChange={setOffset}
          onPageSizeChange={(value) => {
            setPageSize(value)
            setOffset(0)
          }}
        />
      </div>
      {manageRecord && <UploadRecordManageDialog
        target={manageRecord}
        impact={manageImpact}
        impactLoading={manageImpactLoading}
        impactError={manageImpactError}
        operationBusy={uploadRecordOperationId === manageRecord.id}
        onRetryImpact={() => openUploadRecordManage(manageRecord)}
        onClose={closeUploadRecordManage}
        onArchive={() => archiveUploadRecord(manageRecord)}
        onPurge={(confirmText) => purgeUploadRecord(manageRecord, confirmText)}
      />}
    </div>
  )
}

function ImportTaskRows({ rows, abandoningId, onAbandon, onFindGarment }: {
  rows: ImportTaskRow[]
  abandoningId: string | null
  onAbandon: (sessionId: string) => void
  onFindGarment: (code: string, garmentId?: number) => void
}) {
  return (
    <div className="import-task-table">
      <div className="import-task-table-head" aria-hidden="true">
        <span>货号</span>
        <span>已选 / 已上传 / 已入库</span>
        <span>状态</span>
        <span>更新时间</span>
        <span>操作</span>
      </div>
      {rows.map((row) => {
        const canAbandon = row.sessionId && isImportTaskRunning(row.status) && !['FINALIZED', 'FINALIZING', 'ABANDONED'].includes(row.status)
        const isDuplicateFailure = row.statusTone === 'danger' && /已存在|重复/.test(row.error ?? '')
        const needsManualReimport = row.status === 'FAILED' && !isDuplicateFailure && !row.garmentId
        const canOpenGarment = Boolean(row.garmentId) && !needsManualReimport
        return (
          <div className={`record-row import-task-row is-${row.statusTone}`} key={row.id}>
            <div className="import-task-code">
              <ImportPreviewThumb src={row.previewUrl} />
              <TruncatedText as="strong" text={row.code} />
              <TruncatedText text={importTaskMetaText(row)} />
            </div>
            <span className="import-task-counts">{importImageCountText(row.importedCount, row.uploadedCount, row.retainedCount)} 张</span>
            <span className={`import-status-badge is-${row.statusTone}`}>{row.statusLabel}</span>
            <span className="import-task-time">{dateText(row.updatedAt ?? row.createdAt)}</span>
            <div className="import-task-actions">
              {canAbandon ? (
                <button className="management-action compact-control" type="button" disabled={abandoningId === row.sessionId} onClick={() => row.sessionId && onAbandon(row.sessionId)}>
                  {abandoningId === row.sessionId ? '处理中' : '放弃'}
                </button>
              ) : isDuplicateFailure ? (
                <button className="management-action compact-control" type="button" title="回到服装库查看已存在的货号" onClick={() => onFindGarment(row.code, row.garmentId)}>
                  查看已有
                </button>
              ) : canOpenGarment ? (
                <button className="management-action compact-control" type="button" title="回到服装库查看这件服装" onClick={() => onFindGarment(row.code, row.garmentId)}>
                  查看服装
                </button>
              ) : needsManualReimport ? (
                <span className="import-action-hint is-danger" title="当前页面已无法自动恢复原始本地文件，请重新选择图片或文件夹上传该货号">重新上传</span>
              ) : row.status === 'ABANDONED' ? <span>已放弃</span> : <span>无需操作</span>}
            </div>
            <div className="upload-progress-bar import-task-progress" aria-hidden="true"><span style={{ width: `${importTaskProgress(row)}%` }} /></div>
          </div>
        )
      })}
    </div>
  )
}

function UploadRecordManageDialog({ target, impact, impactLoading, impactError, operationBusy, onClose, onRetryImpact, onArchive, onPurge }: {
  target: UploadRecordTarget
  impact: DeleteImpact | null
  impactLoading: boolean
  impactError: string | null
  operationBusy: boolean
  onClose: () => void
  onRetryImpact: () => void
  onArchive: () => Promise<void>
  onPurge: (confirmText: string) => Promise<void>
}) {
  const sessionCount = uploadRecordSessionCount(target.rows)
  const expectedText = sessionCount <= 1 ? target.title : '全部删除'
  const [confirmText, setConfirmText] = useState('')
  const [submitting, setSubmitting] = useState<'archive' | 'purge' | null>(null)
  const confirmInputRef = useRef<HTMLInputElement | null>(null)
  const running = target.rows.some((row) => isImportTaskRunning(row.status))

  useEffect(() => {
    confirmInputRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !submitting && !operationBusy) onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose, operationBusy, submitting])

  async function submit(action: 'archive' | 'purge') {
    if (submitting || operationBusy || (action === 'purge' && (confirmText !== expectedText || !impact))) return
    setSubmitting(action)
    try {
      if (action === 'archive') await onArchive()
      else await onPurge(confirmText)
    } catch (error) {
      window.alert(error instanceof ApiError
        ? friendlyErrorText(error.errorCode, error.errorMessage || error.message)
        : error instanceof Error
          ? friendlyErrorText(undefined, error.message)
          : '操作失败')
      setSubmitting(null)
    }
  }

  return createPortal(
    <div className="modal-backdrop" role="presentation" onClick={() => { if (!submitting && !operationBusy) onClose() }}>
      <div className="confirm-modal" role="dialog" aria-modal="true" aria-label="归档或删除上传记录" onClick={(event) => event.stopPropagation()}>
        <div className="confirm-head">
          <h2>归档或删除上传记录</h2>
          <button type="button" className="ghost-icon" onClick={onClose} title="关闭"><X size={18} /></button>
        </div>
        <p className="confirm-copy">归档后仅从日常上传记录中隐藏，可通过“查看已归档”恢复；不会中断任务，也不会影响已入库服装。</p>
        {running && <p className="confirm-warning" role="alert">该任务仍在进行。归档不会中断上传；彻底删除会立即阻止继续确认和入库，并在上传地址失效后清理临时文件。</p>}
        <div className="confirm-items"><span>{target.title}</span></div>
        <div className="confirm-copy">
          {impactLoading ? '正在计算删除影响范围...' : impactError ? <><span>删除影响预检失败，暂时不能彻底删除。</span> <button type="button" className="text-button" onClick={onRetryImpact}>重试</button></> : uploadRecordDeleteImpactText(impact, sessionCount)}
        </div>
        <label className="confirm-field">
          <span>{sessionCount <= 1 ? '输入完整任务名确认彻底删除' : '输入“全部删除”确认彻底删除'}</span>
          <input ref={confirmInputRef} value={confirmText} onChange={(event) => setConfirmText(event.target.value)} placeholder={expectedText} />
        </label>
        <div className="confirm-actions upload-record-confirm-actions">
          <button type="button" className="management-action" disabled={!!submitting || operationBusy} onClick={onClose}>取消</button>
          {!target.archived && <button type="button" className="management-action" disabled={!!submitting || operationBusy} onClick={() => void submit('archive')}>{submitting === 'archive' ? '归档中...' : '归档'}</button>}
          <button type="button" className="danger-action" disabled={!!submitting || operationBusy || impactLoading || !impact || !!impactError || confirmText !== expectedText} onClick={() => void submit('purge')}>{submitting === 'purge' ? '删除中...' : '彻底删除'}</button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

function uploadRecordSessionCount(rows: ImportTaskRow[]) {
  return new Set(rows.map((row) => row.sessionId).filter(Boolean)).size
}

function uploadRecordDeleteImpactText(impact: DeleteImpact | null, fallbackSessionCount: number) {
  if (!impact) return '彻底删除会移除上传记录元数据和未入库的临时文件；已经入库的服装与媒体保留。该操作不可恢复。'
  const sessionCount = impact.uploadSessionCount ?? fallbackSessionCount
  const temporaryCount = impact.temporaryObjectCount ?? 0
  const linkedCount = impact.linkedBusinessCount ?? 0
  const linkedText = linkedCount ? `；${linkedCount} 个已入库业务对象及其媒体会保留` : ''
  return `彻底删除将移除 ${sessionCount || 1} 条上传会话记录${temporaryCount ? `和 ${temporaryCount} 个未入库临时文件（约 ${formatBytes(impact.estimatedBytes)}）` : ''}${linkedText}。该操作不可恢复。`
}

function ImportThumbStack({ rows }: { rows: ImportTaskRow[] }) {
  const previewRows = rows.filter((row) => row.previewUrl).slice(0, 4)
  const fallbackCount = Math.max(0, Math.min(4, rows.length) - previewRows.length)
  return (
    <div className="batch-cover-stack import-cover-stack" aria-hidden="true">
      {previewRows.map((row) => <ImportPreviewThumb key={row.id} src={row.previewUrl} />)}
      {Array.from({ length: fallbackCount }).map((_, index) => <ImportPreviewThumb key={`empty-${index}`} />)}
    </div>
  )
}

function ImportPreviewThumb({ src }: { src?: string | null }) {
  const [failed, setFailed] = useState(false)
  useEffect(() => setFailed(false), [src])
  const resolvedSrc = src ? mediaUrl(src) : ''
  return (
    <span className="import-preview-thumb">
      {resolvedSrc && !failed ? <img src={resolvedSrc} alt="" loading="lazy" onError={() => setFailed(true)} /> : <Images size={14} />}
    </span>
  )
}

function ImportBatchCard({ batch, onCode, onFiles, onRemove }: {
  batch: StylingImportBatch
  onCode: (code: string) => void
  onFiles: (files: File[]) => void
  onRemove: () => void
}) {
  return (
    <div className={`batch-card ${!batch.files.length || batch.error ? 'has-error' : ''}`}>
      <div className="batch-card-form">
        <div className="batch-icon"><Layers3 size={18} /></div>
        <label className="batch-field batch-code-field">
          <span>货号</span>
          <input
            value={batch.code}
            maxLength={PRODUCT_CODE_MAX_CHARS}
            onChange={(event) => onCode(event.target.value.slice(0, PRODUCT_CODE_MAX_CHARS))}
            aria-label="搭配服装货号"
          />
        </label>
        <span className="batch-form-spacer" />
        <span className={`count-pill ${batch.files.length > STYLING_GARMENT_MAX_IMAGES ? 'is-over' : ''}`}>{formatImageCount(batch.files.length, STYLING_GARMENT_MAX_IMAGES)}</span>
        <button className="management-action icon-button" onClick={onRemove} title="移除整套"><Trash2 size={16} /></button>
      </div>
      <div className="batch-note">
        <span>推荐 3-5 张，排序越靠前越重要。</span>
        {batch.error && <strong>{batch.error}</strong>}
        {!batch.files.length && !batch.error && <strong>没有可用图片</strong>}
        {batch.files.length > STYLING_GARMENT_MAX_IMAGES && <strong>{`超出 ${STYLING_GARMENT_MAX_IMAGES} 张，红框图片不会进入后续识别`}</strong>}
        {batch.issues?.map((issue) => <strong key={`${issue.code}-${issue.path ?? issue.count ?? issue.message}`}>{issue.message}</strong>)}
      </div>
      <FileThumbList files={batch.files} onChange={onFiles} limit={STYLING_GARMENT_MAX_IMAGES} />
    </div>
  )
}

function FileThumbList({ files, onChange, limit, disabled = false }: { files: File[]; onChange: (files: File[]) => void; limit?: number; disabled?: boolean }) {
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const [viewer, setViewer] = useState<LightboxImage | null>(null)
  const [dimensions, setDimensions] = useState<Record<string, { width: number; height: number }>>({})
  function move(from: number, to: number) {
    if (disabled) return
    if (to < 0 || to >= files.length) return
    const next = [...files]
    const [item] = next.splice(from, 1)
    next.splice(to, 0, item)
    onChange(next)
  }
  return (
    <div className={`file-thumb-grid ${disabled ? 'is-disabled' : ''}`}>
      {files.map((file, index) => {
        const overLimit = typeof limit === 'number' && index >= limit
        return (
          <div
            className={`file-thumb ${overLimit ? 'is-over-limit' : ''}`}
            key={`${file.name}-${file.size}-${index}`}
            draggable={!disabled}
            onDragStart={() => { if (!disabled) setDragIndex(index) }}
            onDragOver={(event) => event.preventDefault()}
            onDrop={() => {
              if (disabled) return
              if (dragIndex !== null) move(dragIndex, index)
              setDragIndex(null)
            }}
          >
            {overLimit && <em className="thumb-overflow-badge">超出</em>}
            <FilePreviewButton
              file={file}
              dimensions={dimensions[fileKey(file)]}
              onDimensions={(value) => setDimensions((current) => ({ ...current, [fileKey(file)]: value }))}
              onOpen={(image) => setViewer(image)}
            />
            <TruncatedText text={file.name}>
              {file.name}<TruncatedText as="small" text={fileMeta(file, dimensions[fileKey(file)])} />
            </TruncatedText>
            <div className="thumb-actions">
              <label title="替换图片">
                <RefreshCw size={14} />
                <input type="file" accept={SUPPORTED_IMAGE_ACCEPT} disabled={disabled} onChange={(event) => {
                  const replacement = event.target.files?.[0]
                  if (!replacement || disabled) return
                  const next = [...files]
                  next[index] = replacement
                  onChange(next)
                }} />
              </label>
              <button disabled={disabled} onClick={() => onChange(files.filter((_, itemIndex) => itemIndex !== index))} title="删除图片"><X size={14} /></button>
            </div>
          </div>
        )
      })}
      <label className="file-thumb file-thumb-add" title="新增图片">
        <div className="add-icon-wrapper">
          <ImagePlus size={24} />
        </div>
        <span>新增图片</span>
        <input type="file" accept={SUPPORTED_IMAGE_ACCEPT} multiple disabled={disabled} onChange={(event) => {
          const added = Array.from(event.target.files ?? [])
          if (added.length && !disabled) {
            onChange([...files, ...added])
          }
          event.target.value = ''
        }} />
      </label>
      {viewer && <ImageLightbox image={viewer} onClose={() => setViewer(null)} />}
    </div>
  )
}

function FilePreviewButton({ file, dimensions, onDimensions, onOpen }: {
  file: File
  dimensions?: { width: number; height: number }
  onDimensions: (dimensions: { width: number; height: number }) => void
  onOpen: (image: LightboxImage) => void
}) {
  const url = useObjectUrl(file)
  return (
    <button className="image-open" onClick={() => onOpen(fileLightboxImage(file, url, dimensions))} title="放大查看">
      <img
        src={url}
        alt={file.name}
        onLoad={(event) => onDimensions({
          width: event.currentTarget.naturalWidth,
          height: event.currentTarget.naturalHeight,
        })}
      />
    </button>
  )
}

function ModelPage({ models, role, run }: { models: ModelProfile[]; role: SessionRole | null; run: any }) {
  const isAdmin = role === 'admin'
  const model = models.find((item) => item.isDefault) ?? models[0]
  const editable = isAdmin && !!model
  const refs = model?.profile?.headReferenceEntries ?? []
  const coverThumb = refs.find((ref) => ref.angle === 'front')?.previewUrl ?? refs[0]?.previewUrl ?? null
  const displayRefs = refs.length || editable ? refs : fallbackHeadRefs()
  const takenAngles = new Set(refs.map((ref) => ref.angle).filter((angle): angle is string => !!angle))
  const [env, setEnv] = useState<MediaAsset | null | undefined>(undefined)
  const [dragIndex, setDragIndex] = useState<number | null>(null)

  useEffect(() => {
    void api.getShootEnvironment().then((r) => setEnv(r.asset)).catch(() => setEnv(null))
  }, [])

  return (
    <section className="page workbench-page model-shell-page">
      <PageTitle
        title="模特与背景"
        subtitle="维护全局模特身份和拍摄空间参考，保证每次成片稳定一致。"
      />
      <div className="panel model-page">
        <div className="model-meta">
          <div className="model-cover">
            {coverThumb ? <img src={mediaUrl(coverThumb)} loading="lazy" alt={model?.name ?? '模特与背景'} /> : <div className="image-placeholder"><UserRound size={40} /></div>}
          </div>
          <div className="model-meta-head">
            <h2>{model?.name ?? 'Face-01 默认模特'}</h2>
            <span className={`chip ${isAdmin ? 'chip-admin' : ''}`}>{isAdmin ? '可维护' : '只读'}</span>
          </div>
          <Meta label="发型 / 气质" value={model?.profile?.notes ?? '短发、自然淡妆、克制通勤气质'} />
          <Meta label="身型参数" value={`${model?.profile?.height ?? '170cm'} · ${model?.profile?.bodyShape ?? 'H型'}`} />
          <Meta label="头部参考图" value={`${refs.length} 张`} />
        </div>

        <div className="model-main">
          <section className="model-block">
            <div className="section-head">
              <strong>模特头部参考图</strong>
              {editable && (
                <label className="management-action">
                  <ImagePlus size={16} /> 追加图片
                  <input type="file" accept={SUPPORTED_IMAGE_ACCEPT} multiple onChange={(event) => {
                    const next = Array.from(event.target.files ?? [])
                    event.currentTarget.value = ''
                    if (next.length) void run('追加模特图片中...', () => api.addModelHeadReferences(model.id, next), undefined, { refresh: 'background' })
                  }} />
                </label>
              )}
            </div>
            <p className="edit-hint">{editable ? '拖拽卡片调整顺序，靠前的图片优先用于生成（当前取前 2 张）。身份以这些图为准，表情与角度可变。' : '多角度头部参考图，身份以这些图为准。'}</p>
            <div className="saved-image-grid">
              {displayRefs.map((ref, index) => {
                const thumb = ('thumbUrl' in ref ? ref.thumbUrl : null) ?? ref.previewUrl ?? null
                return (
                  <div
                    className="saved-image-card"
                    key={ref.id}
                    draggable={editable && refs.length > 1}
                    onDragStart={editable ? () => setDragIndex(index) : undefined}
                    onDragOver={editable ? (event) => event.preventDefault() : undefined}
                    onDrop={editable ? () => {
                      if (dragIndex !== null && dragIndex !== index) void run('调整模特图片顺序中...', () => api.reorderModelHeadReference(model.id, refs[dragIndex].id, index), undefined, { refresh: 'background' })
                      setDragIndex(null)
                    } : undefined}
                  >
                    {thumb ? <img src={mediaUrl(thumb)} loading="lazy" alt={angleLabel(ref.angle ?? ref.id)} /> : <div className="image-placeholder"><UserRound size={28} /></div>}
                    {editable ? (
                      <span className="has-angle" title={angleLabel(ref.angle ?? ref.id)}>
                        <select className="angle-select" value={ref.angle ?? ''} onChange={(event) => {
                          const angle = event.target.value
                          if (model) void run('设置模特图片角度中...', () => api.setModelHeadReferenceAngle(model.id, ref.id, angle), undefined, { refresh: 'background' })
                        }}>
                          <option value="">未设置</option>
                          {HEAD_ANGLE_OPTIONS.map((option) => (
                            <option key={option.value} value={option.value} disabled={takenAngles.has(option.value) && option.value !== ref.angle}>{option.label}</option>
                          ))}
                        </select>
                        <small>{`第 ${index + 1} 张`}</small>
                      </span>
                    ) : (
                      <TruncatedText text={angleLabel(ref.angle ?? ref.id)} />
                    )}
                    {editable && (
                      <div className="thumb-actions">
                        <label title="替换图片">
                          <RefreshCw size={14} />
                          <input type="file" accept={SUPPORTED_IMAGE_ACCEPT} onChange={(event) => {
                            const replacement = event.target.files?.[0]
                            event.currentTarget.value = ''
                            if (replacement) void run('替换模特图片中...', () => api.replaceModelHeadReference(model.id, ref.id, replacement), undefined, { refresh: 'background' })
                          }} />
                        </label>
                        <button disabled={refs.length <= 1} onClick={() => void run('删除模特图片中...', () => api.deleteModelHeadReference(model.id, ref.id), undefined, { refresh: 'background' })} title="删除图片"><Trash2 size={14} /></button>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </section>

          <section className="model-block">
            <div className="section-head">
              <strong>拍摄背景参考图</strong>
              {isAdmin && (
                <label className="management-action">
                  <ImagePlus size={16} /> {env ? '更换' : '上传'}
                  <input type="file" accept={SUPPORTED_IMAGE_ACCEPT} onChange={(event) => {
                    const file = event.target.files?.[0]
                    event.currentTarget.value = ''
                    if (file) void run('上传拍摄背景参考图中...', () => api.setShootEnvironment(file), (r: { asset: MediaAsset | null }) => setEnv(r.asset), { refresh: false })
                  }} />
                </label>
              )}
            </div>
            <p className="edit-hint">全局只保留 0 或 1 张。生成分镜预览时只参考它的背景、空间与光线；模特和服装以各自参考图为准，为空则不引用背景参考图。</p>
            {env === undefined ? (
              <div className="model-empty">加载中…</div>
            ) : env ? (
              <div className="saved-image-grid">
                <div className="saved-image-card">
                  <img src={mediaUrl(assetPreviewSrc(env))} loading="lazy" alt="拍摄背景参考图" />
                  <TruncatedText text="拍摄背景参考图">
                    拍摄背景参考图<TruncatedText as="small" text={assetMeta(env)} />
                  </TruncatedText>
                  {isAdmin && (
                    <div className="thumb-actions">
                      <label title="更换图片">
                        <RefreshCw size={14} />
                        <input type="file" accept={SUPPORTED_IMAGE_ACCEPT} onChange={(event) => {
                          const file = event.target.files?.[0]
                          event.currentTarget.value = ''
                          if (file) void run('更换拍摄背景参考图中...', () => api.setShootEnvironment(file), (r: { asset: MediaAsset | null }) => setEnv(r.asset), { refresh: false })
                        }} />
                      </label>
                      <button onClick={() => void run('移除拍摄背景参考图中...', () => api.clearShootEnvironment(), (r: { asset: MediaAsset | null }) => setEnv(r.asset), { refresh: false })} title="移除图片"><Trash2 size={14} /></button>
                    </div>
                  )}
                </div>
              </div>
            ) : (
              <div className="model-empty">尚未设置拍摄背景参考图{isAdmin ? '，点击上方「上传」添加一张。' : '。'}</div>
            )}
          </section>
        </div>
      </div>
    </section>
  )
}

function PromptMaintenancePage({ run }: { run: any }) {
  const [items, setItems] = useState<PromptConfig[]>([])
  const [placeholders, setPlaceholders] = useState<PromptPlaceholder[]>([])
  const [activeKey, setActiveKey] = useState('')
  const [draft, setDraft] = useState('')
  const [rendered, setRendered] = useState('')
  const [missing, setMissing] = useState<string[]>([])
  const [usedSegments, setUsedSegments] = useState<string[]>([])
  const [invocations, setInvocations] = useState<PromptInvocationOption[]>([])
  const [attemptId, setAttemptId] = useState<number | undefined>()
  const [historicalPrompt, setHistoricalPrompt] = useState('')

  function loadConfigs(selectKey?: string) {
    return api.listPromptConfigs().then((result) => {
      setItems(result.items)
      setPlaceholders(result.placeholders)
      const nextKey = selectKey || activeKey || result.items[0]?.key || ''
      const nextItem = result.items.find((item) => item.key === nextKey) || result.items[0]
      setActiveKey(nextItem?.key ?? '')
      setDraft(nextItem?.content ?? '')
      setRendered(nextItem?.renderedPreview ?? '')
      setMissing(nextItem?.missingPlaceholders ?? [])
      setUsedSegments(nextItem?.usedSegments ?? [])
    })
  }

  useEffect(() => {
    void loadConfigs().catch(() => undefined)
  }, [])

  useEffect(() => {
    if (!activeKey) return
    void api.listPromptInvocations(activeKey).then((result) => setInvocations(result.items)).catch(() => setInvocations([]))
  }, [activeKey])

  const activeItem = items.find((item) => item.key === activeKey) || items[0]
  const dirty = Boolean(activeItem && draft !== activeItem.content)
  const segments = items.filter((item) => item.groupType === 'segment')
  const assemblies = items.filter((item) => item.groupType === 'assembly')

  useEffect(() => {
    document.body.dataset.promptDirty = dirty ? 'true' : 'false'
    if (!dirty) return () => { delete document.body.dataset.promptDirty }
    const warnUnsaved = (event: BeforeUnloadEvent) => event.preventDefault()
    window.addEventListener('beforeunload', warnUnsaved)
    return () => {
      window.removeEventListener('beforeunload', warnUnsaved)
      delete document.body.dataset.promptDirty
    }
  }, [dirty])

  function selectItem(key: string) {
    const next = items.find((item) => item.key === key)
    if (!next) return
    if (dirty && !window.confirm('当前 Prompt 有未保存修改，确定切换并放弃这些修改吗？')) return
    setActiveKey(key)
    setDraft(next.content)
    setRendered(next.renderedPreview)
    setMissing(next.missingPlaceholders)
    setUsedSegments(next.usedSegments)
    setAttemptId(undefined)
    setHistoricalPrompt('')
    void api.listPromptInvocations(key).then((result) => setInvocations(result.items)).catch(() => setInvocations([]))
  }

  function refreshPreview() {
    if (!activeItem) return
    void run('渲染 Prompt 预览中...', () => api.previewPromptConfig(activeItem.key, draft, attemptId), (result: { rendered: string; historicalPrompt?: string | null; diagnostics: { missingPlaceholders: string[]; usedSegments: string[] } }) => {
      setRendered(result.rendered)
      setHistoricalPrompt(result.historicalPrompt ?? '')
      setMissing(result.diagnostics.missingPlaceholders)
      setUsedSegments(result.diagnostics.usedSegments)
    }, { refresh: false })
  }

  function save() {
    if (!activeItem) return
    void run('保存 Prompt 配置中...', () => api.updatePromptConfig(activeItem.key, draft), (result: { item: PromptConfig; preview: { rendered: string; diagnostics: { missingPlaceholders: string[]; usedSegments: string[] } } }) => {
      setRendered(result.preview.rendered)
      setMissing(result.preview.diagnostics.missingPlaceholders)
      setUsedSegments(result.preview.diagnostics.usedSegments)
      void loadConfigs(result.item.key)
    }, { refresh: false })
  }

  function insertPlaceholder(name: string) {
    setDraft((current) => `${current}${current.endsWith('\n') || !current ? '' : '\n'}{{${name}}}`)
  }

  return (
    <section className="page workbench-page prompt-page">
      <PageTitle
        title="Prompt 维护"
        subtitle="维护固定分镜和组装模板，普通用户无需接触这些底层配置。"
        actions={<div className="toolbar"><span className="chip">Admin</span><button className="management-action" onClick={refreshPreview} disabled={!activeItem}><RefreshCw size={16} />刷新预览</button><button className="management-action" onClick={save} disabled={!activeItem || !dirty}><Save size={16} />保存</button></div>}
      />
      <div className="prompt-maintenance-layout">
        <aside className="panel prompt-nav-panel">
          <PromptNavGroup title="分段 Prompt" items={segments} activeKey={activeKey} onSelect={selectItem} />
          <PromptNavGroup title="组装 Prompt" items={assemblies} activeKey={activeKey} onSelect={selectItem} />
        </aside>
        <section className="panel prompt-editor-panel">
          {activeItem ? (
            <>
              <div className="prompt-editor-head">
                <div>
                  <span className="chip">{activeItem.groupType === 'segment' ? '分段 Prompt' : '组装 Prompt'}</span>
                  <HeadingWithTooltip as="h2" tooltip={activeItem.description}>{activeItem.title}</HeadingWithTooltip>
                </div>
                <div className="prompt-status">
                  <span className={dirty ? 'status-badge warning' : 'status-badge ok'}>{dirty ? '未保存' : '已保存'}</span>
                  {activeItem.updatedAt && <small>更新于 {dateText(activeItem.updatedAt)}</small>}
                  <small>版本 {activeItem.contentHash.slice(0, 12)}</small>
                </div>
              </div>
              <textarea className="prompt-editor-textarea" value={draft} onChange={(event) => setDraft(event.target.value)} spellCheck={false} />
            </>
          ) : <EmptyState title="暂无 Prompt 配置" text="后端还没有返回可维护的 Prompt 配置。" />}
        </section>
        <aside className="panel prompt-preview-panel">
          <div className="prompt-side-section">
            <h3>历史调用上下文</h3>
            <select value={attemptId ?? ''} onChange={(event) => setAttemptId(event.target.value ? Number(event.target.value) : undefined)}>
              <option value="">样例数据</option>
              {invocations.map((item) => <option key={item.id} value={item.id}>#{item.id} · {item.ownerType} · {dateText(item.createdAt)}</option>)}
            </select>
          </div>
          <div className="prompt-side-section">
            <h3>{attemptId ? '当前草稿按历史事实重新渲染' : '预览'}</h3>
            <div className="prompt-rendered-section"><pre>{rendered}</pre></div>
          </div>
          {historicalPrompt && <div className="prompt-side-section"><h3>当时实际发送</h3><div className="prompt-rendered-section"><pre>{historicalPrompt}</pre></div></div>}
          <div className="prompt-side-section">
            <h3>校验</h3>
            <Meta label="引用分段" value={usedSegments.length ? usedSegments.join('、') : '无'} />
            <Meta label="缺失占位符" value={missing.length ? missing.join('、') : '无'} />
          </div>
          <div className="prompt-side-section">
            <h3>占位符</h3>
            <div className="prompt-placeholder-list">
              {placeholders.map((placeholder) => (
                <button key={placeholder.name} type="button" onClick={() => insertPlaceholder(placeholder.name)}>
                  <code>{`{{${placeholder.name}}}`}</code>
                  <span>{placeholder.description}</span>
                </button>
              ))}
            </div>
          </div>
        </aside>
      </div>
    </section>
  )
}

function PromptNavGroup({ title, items, activeKey, onSelect }: { title: string; items: PromptConfig[]; activeKey: string; onSelect: (key: string) => void }) {
  return (
    <div className="prompt-nav-group">
      <h3>{title}</h3>
      {items.map((item) => (
        <button key={item.key} className={activeKey === item.key ? 'active' : ''} onClick={() => onSelect(item.key)}>
          <TruncatedText as="strong" text={item.title} />
          <span>{item.missingPlaceholders.length ? `缺失 ${item.missingPlaceholders.length}` : '已保存'}</span>
        </button>
      ))}
    </div>
  )
}

type RecordViewMode = 'all' | 'batches' | 'tasks'

function RecordsPage({ records, detail, setDetail, openTaskInStudio, run, onDeleteResult }: {
  records: GenerationTask[]
  detail: GenerationTask | null
  setDetail: Dispatch<SetStateAction<GenerationTask | null>>
  openTaskInStudio: (task: GenerationTask) => void
  run: any
  onDeleteResult: (result: DeleteOperationResult) => void
}) {
  const [viewMode, setViewMode] = useState<RecordViewMode>('all')
  const [workspaceItems, setWorkspaceItems] = useState<GenerationWorkspaceRecord[]>([])
  const [batchRecords, setBatchRecords] = useState<GenerationRecordBatch[]>([])
  const [batchDetail, setBatchDetail] = useState<GenerationRecordBatch | null>(null)
  const [batchDetailTasks, setBatchDetailTasks] = useState<GenerationTask[]>([])
  const [batchDetailLoading, setBatchDetailLoading] = useState(false)
  const batchDetailRequestRef = useRef('')
  const [expandedBatchIds, setExpandedBatchIds] = useState<Set<string>>(() => new Set())
  const [expandedBatchTasks, setExpandedBatchTasks] = useState<Record<string, GenerationTask[]>>({})
  const [expandedBatchLoading, setExpandedBatchLoading] = useState<Record<string, boolean>>({})
  const expandedBatchRequestRef = useRef<Record<string, number>>({})
  const batchBulkRequestRef = useRef(0)
  const [items, setItems] = useState<GenerationTask[]>(records)
  const [query, setQuery] = useState('')
  const [offset, setOffset] = useState(0)
  const [total, setTotal] = useState(0)
  const [totalPages, setTotalPages] = useState(0)
  const [facets, setFacets] = useState<GenerationRecordFacets>({})
  const [stableFacets, setStableFacets] = useState<GenerationRecordFacets>({})
  const [loading, setLoading] = useState(false)
  const [includeArchived, setIncludeArchived] = useState(false)
  const [statusFilter, setStatusFilter] = useState('')
  const [createdFrom, setCreatedFrom] = useState('')
  const [createdTo, setCreatedTo] = useState('')
  const [sortBy, setSortBy] = useState('createdAt')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [openFilter, setOpenFilter] = useState<'status' | 'date' | 'sort' | null>(null)
  const [selectedRecordIds, setSelectedRecordIds] = useState<Set<number>>(() => new Set())
  const [selectedRecordCache, setSelectedRecordCache] = useState<Record<number, GenerationTask>>({})
  const [deleteRecordDialog, setDeleteRecordDialog] = useState<GenerationTask[] | null>(null)
  const [deleteRecordImpact, setDeleteRecordImpact] = useState<DeleteImpact | null>(null)
  const [deleteRecordImpactLoading, setDeleteRecordImpactLoading] = useState(false)
  const [downloadPrecheck, setDownloadPrecheck] = useState<FinalImageExportPrecheck | null>(null)
  const [downloadChecking, setDownloadChecking] = useState(false)
  const [pageSize, setPageSize] = useState<number>(defaultPageSizeOptions[0])
  const [bulkMode, setBulkMode] = useState(false)
  const [batchBulkLoading, setBatchBulkLoading] = useState(false)
  const recordMarquee = useMarqueeSelection<number>({
    enabled: bulkMode && !batchBulkLoading && viewMode !== 'batches',
    selected: selectedRecordIds,
    onChange: setSelectedRecordIds,
  })
  const lastSelectedRecordIdRef = useRef<number | null>(null)
  const taskListItems = viewMode === 'tasks'
    ? items
    : viewMode === 'all'
      ? workspaceItems.filter((entry) => entry.kind === 'task').map((entry) => entry.task)
      : []
  const batchListItems = viewMode === 'batches'
    ? batchRecords
    : viewMode === 'all'
      ? workspaceItems.filter((entry) => entry.kind === 'batch').map((entry) => entry.batch)
      : []
  const cachedVisibleBatchTasks = batchListItems.flatMap((batch) => expandedBatchTasks[batch.batchId] ?? [])
  const visibleTaskItems = [...taskListItems, ...cachedVisibleBatchTasks]
  const selectableRecordIds = visibleTaskItems.filter((item) => !isTaskRunning(item)).map((item) => item.id)
  const selectedVisibleRecords = Array.from(selectedRecordIds)
    .map((id) => selectedRecordCache[id])
    .filter((item): item is GenerationTask => !!item && !isTaskRunning(item))
  const selectedArchivedRecords = selectedVisibleRecords.filter((item) => item.archivedAt)
  const selectedActiveRecords = selectedVisibleRecords.filter((item) => !item.archivedAt)
  const allVisibleRecordsSelected = selectableRecordIds.length > 0 && selectableRecordIds.every((id) => selectedRecordIds.has(id))

  function recordQueryOptions(targetOffset = offset) {
    const keyword = query.trim()
    return {
      limit: pageSize,
      offset: targetOffset,
      keyword: keyword || undefined,
      includeArchived,
      status: statusFilter ? [statusFilter] : undefined,
      createdFrom: createdFrom || undefined,
      createdTo: createdTo || undefined,
      sortBy,
      sortDir,
    }
  }

  function cacheSelectedRecords(nextItems: GenerationTask[]) {
    if (!nextItems.length) return
    setSelectedRecordCache((current) => {
      const next = { ...current }
      nextItems.forEach((item) => {
        next[item.id] = item
      })
      return next
    })
  }

  function applyRecordListResult(result: { items: GenerationTask[]; total: number; totalPages: number; facets?: GenerationRecordFacets; stableFacets?: GenerationRecordFacets }, targetOffset: number) {
    setItems(result.items)
    cacheSelectedRecords(result.items)
    setWorkspaceItems([])
    setBatchRecords([])
    setTotal(result.total)
    setTotalPages(result.totalPages)
    setFacets(result.facets ?? {})
    setStableFacets(result.stableFacets ?? result.facets ?? {})
    if (!result.items.length && targetOffset > 0) setOffset(Math.max(0, targetOffset - pageSize))
  }

  function applyWorkspaceResult(result: { items: GenerationWorkspaceRecord[]; total: number; totalPages: number; facets?: GenerationRecordFacets; stableFacets?: GenerationRecordFacets }, targetOffset: number) {
    setWorkspaceItems(result.items)
    cacheSelectedRecords(result.items.flatMap((entry) => entry.kind === 'task' ? [entry.task] : entry.batch.coverTasks))
    setItems([])
    setBatchRecords([])
    setTotal(result.total)
    setTotalPages(result.totalPages)
    setFacets(result.facets ?? {})
    setStableFacets(result.stableFacets ?? result.facets ?? {})
    if (!result.items.length && targetOffset > 0) setOffset(Math.max(0, targetOffset - pageSize))
  }

  function applyBatchListResult(result: { items: GenerationRecordBatch[]; total: number; totalPages: number; facets?: GenerationRecordFacets; stableFacets?: GenerationRecordFacets }, targetOffset: number) {
    setBatchRecords(result.items)
    cacheSelectedRecords(result.items.flatMap((item) => item.coverTasks))
    setWorkspaceItems([])
    setItems([])
    setTotal(result.total)
    setTotalPages(result.totalPages)
    setFacets(result.facets ?? {})
    setStableFacets(result.stableFacets ?? result.facets ?? {})
    if (!result.items.length && targetOffset > 0) setOffset(Math.max(0, targetOffset - pageSize))
  }

  function refreshRecordPage(targetOffset = offset) {
    if (viewMode === 'all') return api.listRecordWorkspace(recordQueryOptions(targetOffset)).then((result) => applyWorkspaceResult(result, targetOffset))
    if (viewMode === 'batches') return api.listRecordBatches(recordQueryOptions(targetOffset)).then((result) => applyBatchListResult(result, targetOffset))
    return api.listRecords(recordQueryOptions(targetOffset)).then((result) => applyRecordListResult(result, targetOffset))
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    const timer = window.setTimeout(() => {
      const request = viewMode === 'all'
        ? api.listRecordWorkspace(recordQueryOptions(offset)).then((result) => {
          if (!cancelled) applyWorkspaceResult(result, offset)
        })
        : viewMode === 'batches'
          ? api.listRecordBatches(recordQueryOptions(offset)).then((result) => {
            if (!cancelled) applyBatchListResult(result, offset)
          })
          : api.listRecords(recordQueryOptions(offset)).then((result) => {
            if (!cancelled) applyRecordListResult(result, offset)
          })
      void request.catch(() => undefined).finally(() => {
        if (!cancelled) setLoading(false)
      })
    }, 250)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [records, viewMode, offset, query, includeArchived, statusFilter, createdFrom, createdTo, sortBy, sortDir, pageSize])

  useEffect(() => {
    setOffset(0)
  }, [viewMode, query, includeArchived, statusFilter, createdFrom, createdTo, sortBy, sortDir])

  useEffect(() => {
    batchBulkRequestRef.current += 1
    setSelectedRecordIds(new Set())
    setBatchBulkLoading(false)
  }, [viewMode, offset, includeArchived, query, statusFilter, createdFrom, createdTo, sortBy, sortDir])

  useEffect(() => {
    setBulkMode(false)
  }, [viewMode])

  function openRecord(item: GenerationTask) {
    batchDetailRequestRef.current = ''
    setBatchDetail(null)
    setDetail(item)
    window.history.replaceState(null, '', routeToUrl({ page: 'records', recordId: item.id }))
    void api.getRecord(item.id, false)
      .then((r) => setDetail(r.item))
      .catch(() => undefined)
  }

  function openBatchRecord(item: GenerationRecordBatch) {
    batchDetailRequestRef.current = item.batchId
    setDetail(null)
    setBatchDetail(item)
    setBatchDetailTasks([])
    setBatchDetailLoading(true)
    void api.listBatchTasks(item.batchId)
      .then((result) => {
        if (batchDetailRequestRef.current === item.batchId) {
          setBatchDetailTasks(result.items)
          cacheSelectedRecords(result.items)
        }
      })
      .catch(() => {
        if (batchDetailRequestRef.current === item.batchId) setBatchDetailTasks([])
      })
      .finally(() => {
        if (batchDetailRequestRef.current === item.batchId) setBatchDetailLoading(false)
      })
  }

  function loadExpandedBatchTasks(batchId: string) {
    const requestId = (expandedBatchRequestRef.current[batchId] ?? 0) + 1
    expandedBatchRequestRef.current = { ...expandedBatchRequestRef.current, [batchId]: requestId }
    setExpandedBatchLoading((current) => ({ ...current, [batchId]: true }))
    void api.listBatchTasks(batchId)
      .then((result) => {
        if (expandedBatchRequestRef.current[batchId] === requestId) {
          if (result.items.length) {
            setExpandedBatchTasks((current) => ({ ...current, [batchId]: result.items }))
            cacheSelectedRecords(result.items)
          } else {
            setExpandedBatchIds((current) => {
              const next = new Set(current)
              next.delete(batchId)
              return next
            })
            setExpandedBatchTasks((current) => {
              const next = { ...current }
              delete next[batchId]
              return next
            })
          }
        }
      })
      .catch(() => {
        if (expandedBatchRequestRef.current[batchId] === requestId) {
          setExpandedBatchTasks((current) => ({ ...current, [batchId]: [] }))
        }
      })
      .finally(() => {
        if (expandedBatchRequestRef.current[batchId] === requestId) {
          setExpandedBatchLoading((current) => ({ ...current, [batchId]: false }))
        }
      })
  }

  function toggleBatchExpansion(item: GenerationRecordBatch) {
    const willExpand = !expandedBatchIds.has(item.batchId)
    setExpandedBatchIds((current) => {
      const next = new Set(current)
      if (next.has(item.batchId)) next.delete(item.batchId)
      else next.add(item.batchId)
      return next
    })
    if (willExpand && !expandedBatchTasks[item.batchId]) loadExpandedBatchTasks(item.batchId)
  }

  function refreshExpandedBatchTaskLists(targets: Array<{ batchId?: string | null }>) {
    const batchIds = Array.from(new Set(targets.map((item) => item.batchId).filter(Boolean) as string[]))
    batchIds.forEach((batchId) => {
      if (expandedBatchIds.has(batchId) || expandedBatchTasks[batchId]) loadExpandedBatchTasks(batchId)
    })
  }

  function refreshActiveBatchDetail(targets: Array<{ batchId?: string | null }>) {
    const activeBatchId = batchDetail?.batchId
    if (!activeBatchId || !targets.some((item) => item.batchId === activeBatchId)) return
    const requestId = activeBatchId
    batchDetailRequestRef.current = requestId
    setBatchDetailLoading(true)
    void api.listBatchTasks(activeBatchId)
      .then((result) => {
        if (batchDetailRequestRef.current !== requestId) return
        if (!result.items.length) {
          setBatchDetailTasks([])
          setBatchDetail((current) => current?.batchId === activeBatchId ? null : current)
          setExpandedBatchIds((current) => {
            const next = new Set(current)
            next.delete(activeBatchId)
            return next
          })
          setExpandedBatchTasks((current) => {
            const next = { ...current }
            delete next[activeBatchId]
            return next
          })
          return
        }
        setBatchDetailTasks(result.items)
        cacheSelectedRecords(result.items)
        setBatchDetail((current) => current?.batchId === activeBatchId ? summarizeBatchRecordFromTasks(current, result.items) : current)
      })
      .catch(() => {
        if (batchDetailRequestRef.current === requestId) setBatchDetailTasks([])
      })
      .finally(() => {
        if (batchDetailRequestRef.current === requestId) setBatchDetailLoading(false)
      })
  }

  function refreshAffectedBatchViews(targets: Array<{ batchId?: string | null }>) {
    refreshExpandedBatchTaskLists(targets)
    refreshActiveBatchDetail(targets)
  }

  function renameBatchRecord(batchId: string, name: string) {
    const cleanName = name.trim().slice(0, BATCH_NAME_MAX_CHARS)
    if (!cleanName) return Promise.resolve(undefined)
    return run('保存批次名中...', () => api.updateBatch(batchId, cleanName), (result: { item: GenerationRecordBatch }) => {
      const nextBatch = result.item
      setBatchDetail((current) => current?.batchId === batchId ? { ...nextBatch, coverTasks: current.coverTasks } : current)
      setBatchRecords((current) => current.map((item) => item.batchId === batchId ? nextBatch : item))
      setWorkspaceItems((current) => current.map((entry) => entry.kind === 'batch' && entry.batch.batchId === batchId
        ? { ...entry, name: nextBatch.name, updatedAt: nextBatch.updatedAt, batch: nextBatch }
        : entry))
    }, { refresh: 'background' })
  }

  function toggleRecordSelection(id: number, checked: boolean, extend = false) {
    setSelectedRecordIds((current) => {
      const next = new Set(current)
      if (extend && lastSelectedRecordIdRef.current !== null && recordMarquee.containerRef.current) {
        const orderedIds = Array.from(recordMarquee.containerRef.current.querySelectorAll<HTMLElement>('[data-select-id][data-select-type="number"]'))
          .filter((item) => item.dataset.selectDisabled !== 'true')
          .map((item) => Number(item.dataset.selectId))
        const start = orderedIds.indexOf(lastSelectedRecordIdRef.current)
        const end = orderedIds.indexOf(id)
        if (start >= 0 && end >= 0) {
          orderedIds.slice(Math.min(start, end), Math.max(start, end) + 1).forEach((rangeId) => {
            if (checked) next.add(rangeId)
            else next.delete(rangeId)
          })
          lastSelectedRecordIdRef.current = id
          return next
        }
      }
      if (checked) next.add(id)
      else next.delete(id)
      lastSelectedRecordIdRef.current = id
      return next
    })
  }

  async function loadBatchTasksForSelection(batchId: string) {
    const cached = expandedBatchTasks[batchId]
    if (cached) return cached
    const result = await api.listBatchTasks(batchId)
    setExpandedBatchTasks((current) => ({ ...current, [batchId]: result.items }))
    cacheSelectedRecords(result.items)
    return result.items
  }

  async function toggleBatchTaskSelection(batch: GenerationRecordBatch, checked: boolean) {
    if (batchBulkLoading) return
    const requestId = batchBulkRequestRef.current + 1
    batchBulkRequestRef.current = requestId
    setBatchBulkLoading(true)
    try {
      const tasks = await loadBatchTasksForSelection(batch.batchId)
      if (batchBulkRequestRef.current !== requestId) return
      const selectableIds = tasks.filter((item) => !isTaskRunning(item)).map((item) => item.id)
      setSelectedRecordIds((current) => {
        const next = new Set(current)
        selectableIds.forEach((id) => {
          if (checked) next.add(id)
          else next.delete(id)
        })
        return next
      })
    } finally {
      if (batchBulkRequestRef.current === requestId) setBatchBulkLoading(false)
    }
  }

  async function toggleVisibleRecordSelection(checked: boolean) {
    if (batchBulkLoading) return
    const requestId = batchBulkRequestRef.current + 1
    batchBulkRequestRef.current = requestId
    setBatchBulkLoading(true)
    try {
      const batchTasks = await Promise.all(batchListItems.map((batch) => loadBatchTasksForSelection(batch.batchId).catch(() => [])))
      if (batchBulkRequestRef.current !== requestId) return
      const pageTasks = [...taskListItems, ...batchTasks.flat()]
      cacheSelectedRecords(pageTasks)
      const ids = pageTasks.filter((item) => !isTaskRunning(item)).map((item) => item.id)
      setSelectedRecordIds((current) => {
        const next = new Set(current)
        ids.forEach((id) => {
          if (checked) next.add(id)
          else next.delete(id)
        })
        return next
      })
    } finally {
      if (batchBulkRequestRef.current === requestId) setBatchBulkLoading(false)
    }
  }

  function batchSelectionState(batch: GenerationRecordBatch) {
    const tasks = expandedBatchTasks[batch.batchId] ?? batch.coverTasks
    const selectableTasks = tasks.filter((item) => !isTaskRunning(item))
    const selectedCount = selectableTasks.filter((item) => selectedRecordIds.has(item.id)).length
    return {
      selected: selectableTasks.length > 0 && selectedCount === selectableTasks.length,
      indeterminate: selectedCount > 0 && selectedCount < selectableTasks.length,
      selectable: batch.taskCount > 0,
    }
  }

  function clearRecordDetailIfAffected(ids: number[]) {
    if (detail && ids.includes(detail.id)) {
      setDetail(null)
      window.history.replaceState(null, '', routeToUrl({ page: 'records' }))
    }
  }

  function openRecordDeleteDialog(item: GenerationTask) {
    openRecordDeleteDialogForItems([item])
  }

  function runRecordBatchAction() {
    if (!selectedVisibleRecords.length) return
    openRecordDeleteDialogForItems(selectedVisibleRecords)
  }

  function openRecordDeleteDialogForItems(targets: GenerationTask[]) {
    const ids = targets.map((item) => item.id)
    setDeleteRecordDialog(targets)
    setDeleteRecordImpact(null)
    setDeleteRecordImpactLoading(true)
    void api.getTaskDeleteImpact(ids)
      .then((response) => setDeleteRecordImpact(response.impact))
      .catch(() => setDeleteRecordImpact(null))
      .finally(() => setDeleteRecordImpactLoading(false))
  }

  function confirmRecordDelete(items: DeleteDialogTarget[], purge: boolean, confirmText: string) {
    const ids = items.map((item) => item.id)
    const action = purge
      ? () => ids.length === 1 ? api.deleteTask(ids[0], confirmText) : api.deleteTasks(ids, confirmText)
      : () => api.archiveTasks(ids)
    setDeleteRecordDialog(null)
    setDeleteRecordImpact(null)
    setItems((current) => current.filter((item) => !ids.includes(item.id)))
    void run(purge ? '彻底删除记录中...' : '归档记录中...', action, (result: DeleteOperationResult) => {
      onDeleteResult(result)
      clearRecordDetailIfAffected(ids)
      setSelectedRecordIds(new Set())
      refreshAffectedBatchViews(items)
      void refreshRecordPage().catch(() => undefined)
    }, { refresh: 'background' })
  }

  function restoreRecords(targets: GenerationTask[]) {
    const ids = targets.map((item) => item.id)
    void run(ids.length === 1 ? '恢复生成记录中...' : `恢复 ${ids.length} 条生成记录中...`, () => api.restoreTasks(ids), () => {
      setDetail((current) => current && ids.includes(current.id) ? { ...current, archivedAt: null, archiveReason: null } : current)
      setSelectedRecordIds(new Set())
      refreshAffectedBatchViews(targets)
      void refreshRecordPage().catch(() => undefined)
    }, { refresh: 'background' })
  }

  function triggerFinalImageDownload(ids: number[]) {
    if (!ids.length) return
    const anchor = document.createElement('a')
    anchor.href = finalImageArchiveUrl(ids)
    anchor.rel = 'noreferrer'
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
    setDownloadPrecheck(null)
    setSelectedRecordIds(new Set())
  }

  function startBatchDownload() {
    if (!selectedVisibleRecords.length || downloadChecking) return
    const ids = selectedVisibleRecords.map((item) => item.id)
    setDownloadChecking(true)
    void api.precheckFinalImageExport(ids)
      .then((result) => {
        const allComplete = result.downloadableTaskIds.length > 0
          && result.items.every((item) => item.downloadableCount > 0 && !item.issues.length)
        if (allComplete) triggerFinalImageDownload(result.downloadableTaskIds)
        else setDownloadPrecheck(result)
      })
      .catch(() => setDownloadPrecheck(null))
      .finally(() => setDownloadChecking(false))
  }

  function startSingleRecordDownload(item: GenerationTask) {
    if (downloadChecking) return
    setDownloadChecking(true)
    void api.precheckFinalImageExport([item.id])
      .then((result) => {
        const allComplete = result.downloadableTaskIds.length > 0
          && result.items.every((entry) => entry.downloadableCount > 0 && !entry.issues.length)
        if (allComplete) triggerFinalImageDownload(result.downloadableTaskIds)
        else setDownloadPrecheck(result)
      })
      .catch(() => setDownloadPrecheck(null))
      .finally(() => setDownloadChecking(false))
  }

  function startBatchDetailDownload(ids: number[]) {
    if (!ids.length || downloadChecking) return
    setDownloadChecking(true)
    void api.precheckFinalImageExport(ids)
      .then((result) => {
        const allComplete = result.downloadableTaskIds.length > 0
          && result.items.every((entry) => entry.downloadableCount > 0 && !entry.issues.length)
        if (allComplete) triggerFinalImageDownload(result.downloadableTaskIds)
        else setDownloadPrecheck(result)
      })
      .catch(() => setDownloadPrecheck(null))
      .finally(() => setDownloadChecking(false))
  }

  function cancelRecordTask(item: GenerationTask) {
    if (!window.confirm('确定取消本次生成吗？已提交给生成服务的图片可能仍会完成并产生费用；系统会停止后续生成，并保留已完成结果。')) return
    void run('取消生成中...', () => api.cancelTask(item.id), (r: { item: GenerationTask }) => {
      setItems((current) => current.map((entry) => entry.id === item.id ? r.item : entry))
      if (detail?.id === item.id) setDetail(r.item)
      refreshAffectedBatchViews([r.item])
    }, { refresh: 'background' })
  }

  function clearRecordFilters() {
    setQuery('')
    setStatusFilter('')
    setCreatedFrom('')
    setCreatedTo('')
    setSortBy('createdAt')
    setSortDir('desc')
    setIncludeArchived(false)
  }

  const hasActiveFilters = Boolean(query.trim() || statusFilter || createdFrom || createdTo || includeArchived || sortBy !== 'createdAt' || sortDir !== 'desc')
  const statusOptions = viewMode === 'tasks'
    ? generationRecordStatusOptions(facets.status, stableFacets.status, statusFilter)
    : viewMode === 'batches'
      ? generationBatchStatusOptions(facets.status, stableFacets.status, statusFilter)
      : generationWorkspaceStatusOptions(facets.status, stableFacets.status, statusFilter)
  const formatRecordStatus = (value: string) => viewMode === 'tasks' ? statusText(value) : recordStatusText(value)

  return (
    <section className="page workbench-page records-page">
      <PageTitle
        title="生成记录"
        subtitle="按批次或单次任务复盘拍摄流程、分镜预览和最终成片。"
      />
      <div className="records-layout">
        <div className="panel record-list">
          <div className="record-list-head">
            <div className="record-view-tabs" role="tablist" aria-label="记录视角">
              {[
                ['all', '全部记录'],
                ['batches', '批次任务'],
                ['tasks', '单次任务'],
              ].map(([value, label]) => (
                <button
                  key={value}
                  className={viewMode === value ? 'active' : ''}
                  type="button"
                  onClick={() => {
                    setViewMode(value as RecordViewMode)
                    batchDetailRequestRef.current = ''
                    setExpandedBatchIds(new Set())
                    setBatchDetail(null)
                    setDetail(null)
                    setStatusFilter('')
                  }}
                >{label}</button>
              ))}
            </div>
            <SearchBox
              value={query}
              onChange={setQuery}
              placeholder="搜索任务名 / 货号 / 批次，支持粘贴多个"
              panelPlaceholder="每行一个任务名、货号或批次，也可用空格、逗号、句号、顿号分隔"
              busy={loading}
            />
            <div className="record-filter-pills">
              <DateRangeFilter
                open={openFilter === 'date'}
                onOpen={(open) => setOpenFilter(open ? 'date' : null)}
                label="创建时间"
                from={createdFrom}
                to={createdTo}
                onFrom={setCreatedFrom}
                onTo={setCreatedTo}
              />
              <SortFilter
                open={openFilter === 'sort'}
                onOpen={(open) => setOpenFilter(open ? 'sort' : null)}
                sortBy={sortBy}
                sortDir={sortDir}
                options={recordSortOptions}
                onChange={(nextSortBy, nextSortDir) => {
                  setSortBy(nextSortBy)
                  setSortDir(nextSortDir)
                }}
              />
              <StatusFilter
                open={openFilter === 'status'}
                onOpen={(open) => setOpenFilter(open ? 'status' : null)}
                value={statusFilter}
                options={statusOptions}
                formatLabel={formatRecordStatus}
                onChange={setStatusFilter}
              />
              <button className={`filter-pill checkbox-pill ${includeArchived ? 'is-active' : ''}`} type="button" onClick={() => {
                setOffset(0)
                setIncludeArchived(!includeArchived)
                setSelectedRecordIds(new Set())
              }}><Check size={15} /><span>查看已归档</span></button>
              {hasActiveFilters && <button className="filter-pill reset-filter-pill" type="button" onClick={clearRecordFilters}><span>清空筛选</span></button>}
            </div>
            <div className="record-list-meta">
              {selectedVisibleRecords.length > 0 && (
                <button className="management-action" disabled={downloadChecking} onClick={startBatchDownload} title="打包下载所选任务的最终成片，按任务名分文件夹">
                  <Download size={14} /> {downloadChecking ? '检查成片中...' : `下载成片 ${selectedVisibleRecords.length} 项`}
                </button>
              )}
              {selectedVisibleRecords.length > 0 && (
                selectedArchivedRecords.length === selectedVisibleRecords.length
                  ? <button className="management-action" onClick={() => restoreRecords(selectedArchivedRecords)}>恢复 {selectedArchivedRecords.length} 项</button>
                  : <button className="management-action" disabled={selectedArchivedRecords.length > 0} title={selectedArchivedRecords.length ? '请分别选择已归档和未归档记录' : undefined} onClick={runRecordBatchAction}>归档 {selectedActiveRecords.length} 项</button>
              )}
              {bulkMode && <button className={`management-action management-check-action ${allVisibleRecordsSelected ? 'is-active' : ''}`} disabled={batchBulkLoading || (!selectableRecordIds.length && !batchListItems.length)} onClick={() => void toggleVisibleRecordSelection(!allVisibleRecordsSelected)}><Check size={15} /> {batchBulkLoading ? '解析中...' : '全选本页'}</button>}
              {bulkMode && viewMode !== 'batches' && <span className="bulk-selection-hint">拖动框选单条记录 · Shift 连选</span>}
              <button className="management-action compact-control" disabled={batchBulkLoading} onClick={() => { setBulkMode((value) => !value); setSelectedRecordIds(new Set()) }}>{bulkMode ? '退出批量' : '批量管理'}</button>
            </div>
          </div>
          <div
            ref={recordMarquee.containerRef}
            className={`record-list-body marquee-selection-surface ${bulkMode && viewMode !== 'batches' ? 'is-bulk-selecting' : ''}`}
            onPointerDown={recordMarquee.onPointerDown}
            onClickCapture={recordMarquee.onClickCapture}
          >
            <MarqueeSelectionBox box={recordMarquee.box} />
            {viewMode === 'all' && workspaceItems.map((entry) => entry.kind === 'batch' ? (
              (() => {
                const selection = batchSelectionState(entry.batch)
                return (
                  <BatchRecordRow
                    key={entry.id}
                    item={entry.batch}
                    active={batchDetail?.batchId === entry.batch.batchId}
                    expanded={expandedBatchIds.has(entry.batch.batchId)}
                    tasks={expandedBatchTasks[entry.batch.batchId] ?? []}
                    loading={!!expandedBatchLoading[entry.batch.batchId]}
                    bulkMode={bulkMode}
                    selected={selection.selected}
                    indeterminate={selection.indeterminate}
                    selectable={selection.selectable && !batchBulkLoading}
                    onSelect={(batchId, checked) => void toggleBatchTaskSelection(entry.batch, checked)}
                    selectedTaskIds={selectedRecordIds}
                    onTaskSelect={toggleRecordSelection}
                    onOpen={openBatchRecord}
                    onToggle={toggleBatchExpansion}
                    onOpenTask={openRecord}
                    onOpenStudio={openTaskInStudio}
                    onManageTask={(item) => isTaskRunning(item) ? cancelRecordTask(item) : item.archivedAt ? restoreRecords([item]) : openRecordDeleteDialog(item)}
                    activeTaskId={detail?.id ?? null}
                  />
                )
              })()
            ) : (
              <TaskRecordRow
                key={entry.id}
                item={entry.task}
                active={detail?.id === entry.task.id}
                bulkMode={bulkMode}
                selected={selectedRecordIds.has(entry.task.id)}
                onSelect={toggleRecordSelection}
                onOpen={openRecord}
                onOpenStudio={openTaskInStudio}
                onManage={(item) => isTaskRunning(item) ? cancelRecordTask(item) : item.archivedAt ? restoreRecords([item]) : openRecordDeleteDialog(item)}
              />
            ))}
            {viewMode === 'batches' && batchRecords.map((item) => {
              const selection = batchSelectionState(item)
              return (
                <BatchRecordRow
                  key={item.batchId}
                  item={item}
                  active={batchDetail?.batchId === item.batchId}
                  expanded={expandedBatchIds.has(item.batchId)}
                  tasks={expandedBatchTasks[item.batchId] ?? []}
                  loading={!!expandedBatchLoading[item.batchId]}
                  bulkMode={bulkMode}
                  selected={selection.selected}
                  indeterminate={selection.indeterminate}
                  selectable={selection.selectable && !batchBulkLoading}
                  onSelect={(batchId, checked) => void toggleBatchTaskSelection(item, checked)}
                  selectedTaskIds={selectedRecordIds}
                  onTaskSelect={toggleRecordSelection}
                  onOpen={openBatchRecord}
                  onToggle={toggleBatchExpansion}
                  onOpenTask={openRecord}
                  onOpenStudio={openTaskInStudio}
                  onManageTask={(item) => isTaskRunning(item) ? cancelRecordTask(item) : item.archivedAt ? restoreRecords([item]) : openRecordDeleteDialog(item)}
                  activeTaskId={detail?.id ?? null}
                />
              )
            })}
            {viewMode === 'tasks' && items.map((item) => (
              <TaskRecordRow
                key={item.id}
                item={item}
                active={detail?.id === item.id}
                bulkMode={bulkMode}
                selected={selectedRecordIds.has(item.id)}
                onSelect={toggleRecordSelection}
                onOpen={openRecord}
                onOpenStudio={openTaskInStudio}
                onManage={(item) => isTaskRunning(item) ? cancelRecordTask(item) : item.archivedAt ? restoreRecords([item]) : openRecordDeleteDialog(item)}
              />
            ))}
            {((viewMode === 'all' && !workspaceItems.length) || (viewMode === 'batches' && !batchRecords.length) || (viewMode === 'tasks' && !items.length)) && <EmptyState title="暂无生成记录" text="创建拍摄任务后会出现在这里。" />}
          </div>
          <div className="record-pagination-stick">
            <PaginationControls
              disabled={loading}
              offset={offset}
              pageSize={pageSize}
              pageSizeOptions={defaultPageSizeOptions}
              pageSizeUnit="行"
              total={total}
              totalPages={totalPages}
              onOffsetChange={setOffset}
              onPageSizeChange={(value) => {
                setPageSize(value)
                setOffset(0)
                setSelectedRecordIds(new Set())
              }}
            />
          </div>
        </div>
        <div className="panel record-detail">
          {batchDetail ? (
            <BatchRecordDetail item={batchDetail} tasks={batchDetailTasks} loading={batchDetailLoading} downloadChecking={downloadChecking} onDownload={startBatchDetailDownload} onRename={renameBatchRecord} />
          ) : detail ? (
            <>
              <h3>{detail.name}</h3>
              <Meta label="状态" value={statusText(detail.status)} />
              <Meta label="选中搭配" value={`${detail.styling?.selectedMaterials?.length ?? 0} 件`} />
              <RecordImagePreview task={detail} />
              <button className="management-action" type="button" disabled={downloadChecking} onClick={() => startSingleRecordDownload(detail)}>
                <Download size={14} /> {downloadChecking ? '检查成片中...' : '下载成片'}
              </button>
              {detail.archivedAt && <button className="management-action" disabled={detail.archiveReason === 'deletion_pending'} onClick={() => restoreRecords([detail])}>{archivedRestoreText(detail.archiveReason)}</button>}
            </>
          ) : <EmptyState title="选择一条记录" text="查看分镜预览、搭配服装和最终成片。" />}
        </div>
      </div>
      {deleteRecordDialog && (
        <DeleteConfirmDialog
          title={deleteRecordDialog.length === 1 ? '归档记录' : `归档 ${deleteRecordDialog.length} 条记录`}
          items={deleteRecordDialog}
          onClose={() => setDeleteRecordDialog(null)}
          onConfirm={confirmRecordDelete}
          impact={deleteRecordImpact}
          impactLoading={deleteRecordImpactLoading}
        />
      )}
      {downloadPrecheck && (
        <FinalImageDownloadDialog
          precheck={downloadPrecheck}
          onClose={() => setDownloadPrecheck(null)}
          onDownload={triggerFinalImageDownload}
        />
      )}
    </section>
  )
}

function BulkCheckbox({ checked, indeterminate = false, disabled = false, label, onChange }: {
  checked: boolean
  indeterminate?: boolean
  disabled?: boolean
  label: string
  onChange: (checked: boolean, extend: boolean) => void
}) {
  const ref = useRef<HTMLInputElement | null>(null)
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate
  }, [indeterminate])

  return (
    <input
      ref={ref}
      type="checkbox"
      checked={checked}
      disabled={disabled}
      aria-checked={indeterminate ? 'mixed' : checked}
      onChange={(event) => onChange(event.target.checked, event.nativeEvent instanceof MouseEvent && event.nativeEvent.shiftKey)}
      aria-label={label}
    />
  )
}

function TaskRecordRow({ item, active, bulkMode, selected, onSelect, onOpen, onOpenStudio, onManage }: {
  item: GenerationTask
  active: boolean
  bulkMode: boolean
  selected: boolean
  onSelect: (id: number, checked: boolean, extend?: boolean) => void
  onOpen: (item: GenerationTask) => void
  onOpenStudio: (task: GenerationTask) => void
  onManage: (item: GenerationTask) => void
}) {
  return (
    <div
      className={`record-row ${selected ? 'is-selected' : ''} ${active ? 'active' : ''} ${item.archivedAt ? 'is-archived' : ''}`}
      data-select-id={bulkMode ? item.id : undefined}
      data-select-type={bulkMode ? 'number' : undefined}
      data-select-disabled={bulkMode && isTaskRunning(item) ? 'true' : undefined}
    >
      {bulkMode && <BulkCheckbox checked={selected} disabled={isTaskRunning(item)} label={`选择 ${item.name}`} onChange={(checked, extend) => onSelect(item.id, checked, extend)} />}
      <button className="record-open" aria-current={active ? 'true' : undefined} title="查看记录详情" onClick={() => onOpen(item)}>
        <AssetThumb asset={taskCoverAsset(item)} interactive={false} />
        <div><TruncatedText as="strong" text={item.name} /><TruncatedText text={`${item.archivedAt ? archiveStateText(item.archiveReason) : statusText(item.status)} · ${dateText(item.createdAt)}`} /></div>
        <div className="strip">{(item.summary?.finalImages ?? []).slice(0, 5).map((asset) => <AssetThumb key={asset.id} asset={asset} interactive={false} />)}</div>
      </button>
      <button className="task-action record-action-button" onClick={() => onOpenStudio(item)} title="回到 AI 摄影棚恢复该任务">
        <TaskResumeActionContent task={item} />
      </button>
      <button className="management-action record-action-button" disabled={item.archiveReason === 'deletion_pending'} onClick={() => onManage(item)}>
        {isTaskRunning(item) ? '取消生成' : item.archivedAt ? archivedRestoreText(item.archiveReason) : '归档'}
      </button>
    </div>
  )
}

function BatchRecordRow({ item, active, expanded, tasks, loading, activeTaskId, bulkMode = false, selected = false, indeterminate = false, selectable = true, selectedTaskIds, onSelect, onTaskSelect, onOpen, onToggle, onOpenTask, onOpenStudio, onManageTask }: {
  item: GenerationRecordBatch
  active: boolean
  expanded: boolean
  tasks: GenerationTask[]
  loading: boolean
  activeTaskId: number | null
  bulkMode?: boolean
  selected?: boolean
  indeterminate?: boolean
  selectable?: boolean
  selectedTaskIds?: Set<number>
  onSelect?: (batchId: string, checked: boolean) => void
  onTaskSelect?: (id: number, checked: boolean) => void
  onOpen: (item: GenerationRecordBatch) => void
  onToggle: (item: GenerationRecordBatch) => void
  onOpenTask: (item: GenerationTask) => void
  onOpenStudio: (task: GenerationTask) => void
  onManageTask: (item: GenerationTask) => void
}) {
  const panelId = `batch-record-${item.batchId}-tasks`
  return (
    <div className={`batch-record-shell ${expanded ? 'is-expanded' : ''}`}>
      <div className={`record-row batch-record-row ${bulkMode ? 'has-selection' : ''} ${active ? 'active' : ''} ${item.status === 'ARCHIVED' ? 'is-archived' : ''}`}>
        {bulkMode && <BulkCheckbox checked={selected} indeterminate={indeterminate} disabled={!selectable} label={`选择 ${item.name}`} onChange={(checked) => onSelect?.(item.batchId, checked)} />}
        <button className="record-open batch-record-open" aria-current={active ? 'true' : undefined} aria-expanded={expanded} aria-controls={panelId} title={expanded ? '查看并收起批次' : '查看并展开批次'} onClick={() => {
          onOpen(item)
          onToggle(item)
        }}>
          <div className="batch-cover-stack">
            {item.coverTasks.slice(0, 4).map((task) => <AssetThumb key={task.id} asset={taskCoverAsset(task)} interactive={false} />)}
          </div>
          <div>
            <TruncatedText as="strong" text={item.name} />
            <TruncatedText text={`${recordStatusText(item.status)} · ${item.taskCount} 个任务 · ${dateText(item.createdAt)}`} />
          </div>
          <div className="batch-progress-strip">
            <span>{item.completedTaskCount} 完成</span>
            {item.failedTaskCount > 0 && <span>{item.failedTaskCount} 失败</span>}
            {(item.runningTaskCount + item.queuedTaskCount) > 0 && <span>{item.runningTaskCount + item.queuedTaskCount} 进行中</span>}
            {item.downloadableTaskCount > 0 && <span>{item.downloadableTaskCount} 可下载</span>}
          </div>
        </button>
        <button className="management-action record-action-button" aria-expanded={expanded} aria-controls={panelId} onClick={() => {
          onOpen(item)
          onToggle(item)
        }}><ChevronDown size={14} className={expanded ? 'is-rotated' : ''} />{expanded ? '收起' : '展开'}</button>
      </div>
      {expanded && (
        <div className="batch-expanded-list" id={panelId}>
          {loading && <EmptyState title="正在读取批次" text="正在加载批次内任务。" />}
          {!loading && tasks.map((task) => (
            <div className="batch-child-record" key={task.id}>
              <span className="batch-child-badge">批次内任务</span>
              <TaskRecordRow
                item={task}
                active={activeTaskId === task.id}
                bulkMode={bulkMode}
                selected={!!selectedTaskIds?.has(task.id)}
                onSelect={onTaskSelect ?? (() => undefined)}
                onOpen={onOpenTask}
                onOpenStudio={onOpenStudio}
                onManage={onManageTask}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function BatchRecordDetail({ item, tasks, loading, downloadChecking, onDownload, onRename }: {
  item: GenerationRecordBatch
  tasks: GenerationTask[]
  loading: boolean
  downloadChecking: boolean
  onDownload: (ids: number[]) => void
  onRename: (batchId: string, name: string) => Promise<unknown>
}) {
  const [draftName, setDraftName] = useState(item.name)
  useEffect(() => setDraftName(item.name), [item.batchId, item.name])
  const downloadableIds = tasks.filter((task) => ['COMPLETED', 'FINAL_PARTIAL'].includes(task.status)).map((task) => task.id)
  const cleanDraftName = draftName.trim().slice(0, BATCH_NAME_MAX_CHARS)
  const canSaveName = !!cleanDraftName && cleanDraftName !== item.name
  return (
    <>
      <div className="batch-detail-head">
        <span className="status-badge">{recordStatusText(item.status)}</span>
        <label className="batch-field batch-info-field">
          <span>批次名</span>
          <input
            value={draftName}
            maxLength={BATCH_NAME_MAX_CHARS}
            onChange={(event) => setDraftName(event.target.value.slice(0, BATCH_NAME_MAX_CHARS))}
            aria-label="批次名"
          />
        </label>
        <p>{item.taskCount} 个任务 · {item.completedTaskCount} 完成 · {item.failedTaskCount} 失败 · {item.downloadableTaskCount} 可下载</p>
      </div>
      <div className="batch-detail-stats">
        <Meta label="创建时间" value={dateText(item.createdAt)} />
        <Meta label="更新时间" value={dateText(item.updatedAt)} />
        <Meta label="进行中/排队" value={`${item.runningTaskCount + item.queuedTaskCount} 个`} />
      </div>
      <button className="management-action" type="button" disabled={!canSaveName} onClick={() => void onRename(item.batchId, cleanDraftName)}>
        <Save size={14} /> 保存批次名
      </button>
      <button className="management-action" type="button" disabled={loading || downloadChecking || !downloadableIds.length} onClick={() => onDownload(downloadableIds)}>
        <Download size={14} /> {loading || downloadChecking ? '检查成片中...' : '下载成片'}
      </button>
    </>
  )
}

function summarizeBatchRecordFromTasks(item: GenerationRecordBatch, tasks: GenerationTask[]): GenerationRecordBatch {
  const statusCounts = tasks.reduce<Record<string, number>>((counts, task) => {
    counts[task.status] = (counts[task.status] ?? 0) + 1
    return counts
  }, {})
  const archivedTaskCount = tasks.filter((task) => task.archivedAt || task.status === 'ABANDONED').length
  const runningTaskCount = tasks.filter((task) => isTaskRunning(task)).length
  const queuedTaskCount = tasks.filter((task) => task.autoRunTarget).length
  const completedTaskCount = tasks.filter((task) => task.status === 'COMPLETED').length
  const failedTaskCount = tasks.filter((task) => task.status === 'FAILED').length
  const cancelledTaskCount = tasks.filter((task) => task.status === 'CANCELLED').length
  const downloadableTaskCount = tasks.filter((task) => ['COMPLETED', 'FINAL_PARTIAL'].includes(task.status)).length
  const statuses = tasks.map((task) => task.status)
  const status = tasks.some((task) => task.archivedAt || task.status === 'ABANDONED')
    ? 'ARCHIVED'
    : tasks.some((task) => isTaskRunning(task) || task.autoRunTarget)
      ? 'RUNNING'
      : statuses.includes('FAILED')
        ? 'FAILED'
        : statuses.includes('FINAL_PARTIAL') || (statuses.includes('COMPLETED') && statuses.some((value) => value !== 'COMPLETED'))
          ? 'PARTIAL'
          : statuses.includes('CANCELLED')
            ? 'CANCELLED'
            : statuses.length && statuses.every((value) => value === 'COMPLETED')
              ? 'COMPLETED'
              : 'PENDING'
  const createdValues = tasks.map((task) => task.createdAt)
  const updatedValues = tasks.map((task) => task.updatedAt)
  return {
    ...item,
    status,
    taskCount: tasks.length,
    activeTaskCount: tasks.length - archivedTaskCount,
    archivedTaskCount,
    completedTaskCount,
    failedTaskCount,
    cancelledTaskCount,
    runningTaskCount,
    queuedTaskCount,
    downloadableTaskCount,
    statusCounts,
    createdAt: createdValues.length ? createdValues.reduce((min, value) => value < min ? value : min) : item.createdAt,
    updatedAt: updatedValues.length ? updatedValues.reduce((max, value) => value > max ? value : max) : item.updatedAt,
    coverTasks: tasks.slice(0, 4),
    sampleTaskNames: tasks.slice(0, 3).map((task) => task.name),
    firstTaskId: tasks[0]?.id ?? item.firstTaskId,
  }
}

const finalImageIssueStatusText: Record<string, string> = {
  FAILED: '生成失败',
  RUNNING: '生成中',
  QUEUED: '排队中',
  CANCELLED: '已取消',
  MISSING_ASSET: '缺少成片文件',
}

function FinalImageDownloadDialog({ precheck, onClose, onDownload }: {
  precheck: FinalImageExportPrecheck
  onClose: () => void
  onDownload: (ids: number[]) => void
}) {
  const incompleteItems = precheck.items.filter((item) => item.downloadableCount === 0 || item.issues.length > 0)
  const hasDownloadable = precheck.downloadableTaskIds.length > 0
  const hasComplete = precheck.completeTaskIds.length > 0
  return createPortal(
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div className="confirm-modal" role="dialog" aria-modal="true" aria-label="批量下载成片" onClick={(event) => event.stopPropagation()}>
        <div className="confirm-head">
          <h2>批量下载成片</h2>
          <button type="button" className="ghost-icon" onClick={onClose} title="关闭"><X size={18} /></button>
        </div>
        <p className="confirm-copy">
          {hasDownloadable
            ? `可下载 ${precheck.totalImages} 张成片（约 ${formatBytes(precheck.totalBytes)}），以下任务的部分成片未完成或失败：`
            : '所选任务当前没有可下载的成片。'}
        </p>
        <div className="confirm-items download-issue-list">
          {incompleteItems.map((item) => (
            <div key={item.taskId} className="download-issue-item">
              <TruncatedText as="strong" text={item.taskName} />
              {!item.hasResultSet && <span>尚未生成成片</span>}
              {item.hasResultSet && item.downloadableCount === 0 && !item.issues.length && <span>没有可下载的成片</span>}
              {item.issues.map((issue) => (
                <span key={`${item.taskId}-${issue.sortOrder}`}>
                  #{issue.sortOrder} {issue.shotLabel || '镜头'}：{finalImageIssueStatusText[issue.status] ?? issue.status}
                </span>
              ))}
            </div>
          ))}
        </div>
        <p className="confirm-copy">继续下载时，未完成项会记录在压缩包内的“清单.txt”。</p>
        <div className="confirm-actions">
          <button type="button" className="management-action" onClick={onClose}>取消</button>
          <button
            type="button"
            className="management-action"
            disabled={!hasDownloadable}
            onClick={() => onDownload(precheck.downloadableTaskIds)}
          >
            全部下载
          </button>
          <button
            type="button"
            className="management-action"
            disabled={!hasComplete}
            onClick={() => onDownload(precheck.completeTaskIds)}
          >
            仅下载完整任务{hasComplete ? ` ${precheck.completeTaskIds.length} 项` : ''}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

function DeleteConfirmDialog({ title, items, onClose, onConfirm, impact = null, impactLoading = false }: {
  title: string
  items: DeleteDialogTarget[]
  onClose: () => void
  onConfirm: (items: DeleteDialogTarget[], purge: boolean, confirmText: string) => void
  impact?: DeleteImpact | null
  impactLoading?: boolean
}) {
  const [purge, setPurge] = useState(false)
  const [confirmText, setConfirmText] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const expectedText = items.length === 1 ? items[0].name : '全部删除'
  const itemResetKey = JSON.stringify(items.map((item) => [item.id, item.name, item.batchId ?? null]))
  const canConfirm = !submitting && (!purge || confirmText === expectedText)

  useEffect(() => {
    setPurge(false)
    setConfirmText('')
    setSubmitting(false)
  }, [itemResetKey])

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!canConfirm) return
    setSubmitting(true)
    onConfirm(items, purge, confirmText)
  }

  return createPortal(
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <form className="confirm-modal" role="dialog" aria-modal="true" aria-label={title} onSubmit={submit} onClick={(event) => event.stopPropagation()}>
        <div className="confirm-head">
          <h2>{title}</h2>
          <button type="button" className="ghost-icon" onClick={onClose} title="关闭"><X size={18} /></button>
        </div>
        <p className="confirm-copy">
          确认后会归档并从日常列表移除，可通过“查看已归档”找回。
        </p>
        <div className="confirm-items">
          {items.slice(0, 4).map((item) => <span key={item.id}>{item.name}</span>)}
          {items.length > 4 && <span>另 {items.length - 4} 项</span>}
        </div>
        <label className="danger-check">
          <input type="checkbox" checked={purge} onChange={(event) => {
            setPurge(event.target.checked)
            setConfirmText('')
          }} />
          同时彻底删除关联记录和文件
        </label>
        {purge && (
          <>
            <div className="confirm-copy">
              {impactLoading ? '正在计算影响范围...' : deleteImpactText(impact)}
            </div>
            <label className="confirm-field">
              <span>{items.length === 1 ? '输入完整名称确认彻底删除' : '输入“全部删除”确认彻底删除'}</span>
              <input
                autoFocus
                value={confirmText}
                onChange={(event) => setConfirmText(event.target.value)}
                placeholder={expectedText}
              />
            </label>
          </>
        )}
        <div className="confirm-actions">
          <button type="button" className="management-action" onClick={onClose}>取消</button>
          <button className={purge ? 'danger-action' : 'management-action'} disabled={!canConfirm}>
            {submitting ? '处理中...' : '确认'}
          </button>
        </div>
      </form>
    </div>,
    document.body,
  )
}

function PaginationControls({ offset, pageSize, pageSizeOptions = defaultPageSizeOptions, pageSizeOptionKey, pageSizeUnit = '行', total, totalPages, disabled, compact = false, onOffsetChange, onPageSizeChange }: {
  offset: number
  pageSize: number
  pageSizeOptions?: readonly PageSizeOption[]
  pageSizeOptionKey?: string
  pageSizeUnit?: string
  total: number
  totalPages: number
  disabled?: boolean
  compact?: boolean
  onOffsetChange: (offset: number) => void
  onPageSizeChange: (pageSize: number, optionKey?: string) => void
}) {
  const normalizedPageSizeOptions = normalizePageSizeOptions(pageSizeOptions, pageSizeUnit)
  const selectedPageSizeKey = pageSizeOptionKey ?? String(pageSize)
  const currentPage = total > 0 ? Math.floor(offset / pageSize) + 1 : 0
  const firstItem = total > 0 ? offset + 1 : 0
  const lastItem = Math.min(offset + pageSize, total)
  const lastOffset = totalPages > 0 ? (totalPages - 1) * pageSize : 0
  const canPrev = currentPage > 1 && !disabled
  const canNext = currentPage < totalPages && !disabled
  const [pageInput, setPageInput] = useState(String(currentPage))

  useEffect(() => {
    setPageInput(String(currentPage))
  }, [currentPage])

  function goToPage(value: number) {
    if (!totalPages || disabled) return
    const page = Math.min(Math.max(value, 1), totalPages)
    onOffsetChange((page - 1) * pageSize)
  }

  function commitPageInput() {
    const page = Number(pageInput)
    if (!Number.isFinite(page) || page < 1) {
      setPageInput(String(currentPage))
      return
    }
    goToPage(page)
  }

  return (
    <div className={`pagination-bar ${compact ? 'compact-pagination' : ''}`}>
      <div className="pagination-pages">
        <button className="management-action" disabled={!canPrev} onClick={() => goToPage(1)}>首页</button>
        <button className="management-action" disabled={!canPrev} onClick={() => goToPage(currentPage - 1)}><ChevronLeft size={16} />上一页</button>
        <label className="page-jump">第
          <input
            type="number"
            min={totalPages ? 1 : 0}
            max={Math.max(totalPages, 1)}
            value={pageInput}
            disabled={disabled || totalPages === 0}
            onBlur={commitPageInput}
            onChange={(event) => setPageInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') commitPageInput()
            }}
          />
          页
        </label>
        <span>共 {totalPages} 页</span>
        <button className="management-action" disabled={!canNext} onClick={() => goToPage(currentPage + 1)}>下一页<ChevronRight size={16} /></button>
        <button className="management-action" disabled={!canNext} onClick={() => onOffsetChange(lastOffset)}>尾页</button>
      </div>
      <div className="pagination-meta">
        <span>共 {total} {pageSizeUnit}</span>
        <span>{total > 0 ? `${firstItem}-${lastItem} ${pageSizeUnit}` : `当前 0 ${pageSizeUnit}`}</span>
        <label>每页
          <select
            value={selectedPageSizeKey}
            disabled={disabled}
            onChange={(event) => {
              const selected = normalizedPageSizeOptions.find((option) => option.key === event.target.value)
              if (selected) onPageSizeChange(selected.value, selected.key)
            }}
          >
            {normalizedPageSizeOptions.map((option) => <option key={option.key} value={option.key}>{option.label}</option>)}
          </select>
        </label>
      </div>
    </div>
  )
}

function normalizePageSizeOptions(options: readonly PageSizeOption[], unit: string) {
  const seen = new Set<string>()
  return options.map((option) => {
    if (typeof option === 'number') return { value: option, label: `${option} ${unit}`, key: String(option) }
    return { value: option.value, label: option.label, key: option.key ?? String(option.value) }
  }).filter((option) => {
    if (seen.has(option.key)) return false
    seen.add(option.key)
    return true
  })
}

function PageTitle({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  const tooltipId = useId()
  return (
    <div className="page-title">
      <div className="page-title-copy">
        <div className="page-title-heading">
          <h1
            className={subtitle ? 'has-page-title-tooltip' : undefined}
            tabIndex={subtitle ? 0 : undefined}
            aria-describedby={subtitle ? tooltipId : undefined}
          >
            {title}
          </h1>
          {subtitle && <span id={tooltipId} className="page-title-tooltip" role="tooltip">{subtitle}</span>}
        </div>
      </div>
      {actions}
    </div>
  )
}

function HeadingWithTooltip({ as: Tag = 'h3', children, tooltip }: { as?: 'h2' | 'h3'; children: ReactNode; tooltip?: string }) {
  const tooltipId = useId()
  if (!tooltip) return <Tag>{children}</Tag>
  return (
    <div className="heading-tooltip-wrap">
      <Tag
        className="has-heading-tooltip"
        tabIndex={0}
        aria-describedby={tooltipId}
      >
        {children}
      </Tag>
      <span id={tooltipId} className="heading-tooltip" role="tooltip">{tooltip}</span>
    </div>
  )
}

function UploadBox({ files, setFiles, max, compact = false }: { files: File[]; setFiles: (files: File[]) => void; max: number; compact?: boolean }) {
  return (
    <label className={`upload-box ${compact ? 'compact' : ''}`}>
      <input type="file" accept={SUPPORTED_IMAGE_ACCEPT} multiple onChange={(e) => setFiles(Array.from(e.target.files ?? []).filter(isSupportedImageFile).slice(0, max))} />
      <Upload size={22} />
      <strong>上传图片</strong>
      <span>已选择 {files.length}/{max} 张</span>
    </label>
  )
}

function GarmentCard({ item, onTag, tagLabel = '识别标签', onOpen, onDownload, onDelete, onRestore, selected, onSelect }: {
  item: StylingGarment
  onTag?: () => void
  tagLabel?: string
  onOpen?: () => void
  onDownload?: () => void
  onDelete?: () => void
  onRestore?: () => void
  selected?: boolean
  onSelect?: (checked: boolean, extend: boolean) => void
}) {
  const tags = item.aiTags ?? {}
  const effective = item.effectiveTags ?? {}
  const meta = item.tagMeta ?? {}
  const compatibility = typeof tags.styling_compatibility === 'object' && tags.styling_compatibility ? tags.styling_compatibility as Record<string, unknown> : {}
  const brief = garmentBrief(item, compatibility)
  const tagging = item.status === 'TAGGING_RUNNING'
  const failed = item.status === 'TAGGING_FAILED'
  const showTagResult = !tagging && !failed
  const chips = garmentTagChips(item)
  const failedTagAttempt = failed ? latestFailedTagAttempt(item) : null
  const taggingIssue = failed ? garmentTaggingIssue(item, failedTagAttempt) : null
  const cardBrief = tagging ? '正在识别服装信息；服务繁忙时系统会自动恢复，可以离开当前页面。' : failed ? '识别未完成，可重新识别。' : brief || '待识别搭配信息'
  const failureHint = taggingIssue ? `${taggingIssue.title}：${taggingIssue.description}` : undefined
  const statusLabel = taggingIssue?.badge ?? garmentStatus(item.status)
  const actionCount = [onOpen, item.archivedAt ? onRestore : onDelete, onDownload, onTag].filter(Boolean).length
  return (
    <div
      className={`garment-card ${selected ? 'is-selected' : ''} ${tagging ? 'is-tagging' : ''} ${failed ? 'is-failed' : ''} ${item.archivedAt ? 'is-archived' : ''}`}
      data-select-id={onSelect ? item.id : undefined}
      data-select-type={onSelect ? 'number' : undefined}
    >
      {onSelect && (
        <label className="card-select" title="选择">
          <input type="checkbox" aria-label="选择" checked={!!selected} onChange={(event) => onSelect(event.target.checked, event.nativeEvent instanceof MouseEvent && event.nativeEvent.shiftKey)} />
        </label>
      )}
      <div className="garment-cover">
        <AssetThumb asset={item.images?.[0]?.asset ?? item.coverAsset ?? (item.coverUrl ? { id: item.id, objectKey: '', filename: item.code, mimeType: '', thumbUrl: item.coverUrl } : null)} large />
        {tagging && <span className="cover-badge">识别中</span>}
        {item.archivedAt && <span className="cover-badge">{archiveStateText(item.archiveReason)}</span>}
      </div>
      <div className="garment-card-body">
        <div className="garment-card-head"><TruncatedText as="strong" text={item.code} /><span className={`status ${item.status.toLowerCase()}`} title={failureHint ?? statusLabel} role={tagging ? 'status' : undefined} aria-live={tagging ? 'polite' : undefined}>{statusLabel}</span></div>
        <p className="garment-card-brief" title={failed ? failureHint : cardBrief}>{cardBrief}</p>
        <div className="tag-summary">
          {showTagResult && chips.slice(0, 3).map((chip) => <TruncatedText key={chip} text={chip} />)}
          {(!showTagResult || !chips.length) && <span>{tagging ? '识别中' : failed ? '待重新识别' : '待识别'}</span>}
        </div>
      </div>
      <div className="card-actions" data-action-count={actionCount}>
        {onOpen && <button className="management-action" onClick={onOpen}><Eye size={16} /> 详情</button>}
        {item.archivedAt ? onRestore && <button className="management-action" disabled={tagging || item.archiveReason === 'deletion_pending'} onClick={onRestore}><RefreshCw size={16} /> {archivedRestoreText(item.archiveReason)}</button> : onDelete && <button className="management-action" disabled={tagging} onClick={onDelete}><Trash2 size={16} /> 归档</button>}
        {onDownload && <button className="management-action" disabled={Number(item.imageCount ?? 0) < 1} onClick={onDownload}><Download size={16} /> 下载</button>}
        {onTag && <button className="management-action" disabled={tagging} onClick={onTag}><RotateCcw size={16} /> {tagLabel}</button>}
      </div>
    </div>
  )
}

function GarmentDetailDrawer({ item, run, isAdmin, onClose, onChange, onDelete, onRestore }: {
  item: StylingGarment
  run: any
  isAdmin: boolean
  onClose: () => void
  onChange: (item: StylingGarment) => void
  onDelete: () => void
  onRestore: () => void
}) {
  const [working, setWorking] = useState(item.images ?? [])
  const [replacementFiles, setReplacementFiles] = useState<Record<number, File>>({})
  const [removedIds, setRemovedIds] = useState<number[]>([])
  const [dirty, setDirty] = useState(false)
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const [viewer, setViewer] = useState<LightboxImage | null>(null)
  const [replacementDimensions, setReplacementDimensions] = useState<Record<number, { width: number; height: number }>>({})
  const tagging = item.status === 'TAGGING_RUNNING'
  const runningTagAttempt = latestRunningTagAttempt(item)
  const failedTagAttempt = latestFailedTagAttempt(item)
  const hasCurrentTaggingFailure = item.status === 'TAGGING_FAILED' || Boolean(item.tagError)
  const taggingIssue = hasCurrentTaggingFailure ? garmentTaggingIssue(item, failedTagAttempt) : null
  const selectableText = item.status === 'READY' && !item.archivedAt ? '可进入智能选搭' : '不进入智能选搭'

  useEffect(() => {
    setWorking(item.images ?? [])
    setReplacementFiles({})
    setReplacementDimensions({})
    setRemovedIds([])
    setDirty(false)
  }, [item.id, item.updatedAt])

  useEffect(() => {
    if (item.status !== 'TAGGING_RUNNING') return
    const timer = window.setInterval(() => {
      void api.getStylingGarment(item.id).then((result) => onChange(result.item)).catch(() => undefined)
    }, 2000)
    return () => window.clearInterval(timer)
  }, [item.id, item.status, onChange])

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !viewer) onClose()
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [onClose, viewer])

  function move(from: number, to: number) {
    if (to < 0 || to >= working.length) return
    const next = [...working]
    const [image] = next.splice(from, 1)
    next.splice(to, 0, image)
    setWorking(next)
    setDirty(true)
  }

  async function save(retag: boolean) {
    if (!working.length) throw new Error('搭配服装至少需要保留一张图片')
    await run(retag ? '保存并重新识别标签中...' : '保存搭配服装中...', async () => {
      await Promise.all(Object.entries(replacementFiles).map(([id, file]) => api.replaceStylingGarmentImage(Number(id), file)))
      await Promise.all(removedIds.map((id) => api.deleteStylingGarmentImage(id)))
      await Promise.all(working.map((image, index) => api.updateStylingGarmentImage(image.id, { sortOrder: index })))
      return retag ? (await api.tagStylingGarment(item.id)).item : (await api.getStylingGarment(item.id)).item
    }, (latest: StylingGarment) => onChange(latest), { refresh: 'background' })
  }

  function retag() {
    if (dirty && !window.confirm('当前有未保存的图片修改，本次重新识别将基于已保存的图片。是否继续？')) return
    void run('重新识别中...', () => api.tagStylingGarment(item.id), (result: { item: StylingGarment }) => onChange(result.item), { refresh: 'background' })
  }

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="detail-drawer" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-head">
          <div>
            <h2>{item.code}</h2>
            <span className={`status ${item.status.toLowerCase()}`}>{taggingIssue?.badge ?? garmentStatus(item.status)}</span>
          </div>
          <button className="management-action icon-button" onClick={onClose} title="关闭"><X size={16} /></button>
        </div>
        <div className="drawer-section">
          <div className="section-head"><strong>基础信息</strong></div>
          <Meta label="上传时间" value={dateText(item.createdAt)} />
          <Meta label="更新时间" value={dateText(item.updatedAt)} />
          <Meta label="识别时间" value={dateText(item.tagCompletedAt)} />
          <Meta label="选搭状态" value={selectableText} />
          <Meta label="图片" value={`${working.length}/${STYLING_GARMENT_MAX_IMAGES} 张`} />
          {taggingIssue && (
            <TagObjectPanel
              title={taggingIssue.panelTitle}
              data={taggingErrorDisplay(item, failedTagAttempt)}
              defaultOpen
            />
          )}
          {tagging && runningTagAttempt && <AIOperationList operations={[runningTagAttempt]} />}
          <CodeEditor item={item} run={run} disabled={tagging} />
        </div>
        <div className="drawer-section">
          <div className="section-head">
            <strong>图片</strong>
          </div>
          <p className="edit-hint">此次修改会影响标签识别。排序第一张会作为封面和主要参考图；如需补充图片，请重新上传一套素材。</p>
          {tagging && <p className="edit-hint" role="status" aria-live="polite">正在识别标签；服务繁忙时系统会自动恢复，可以离开当前页面。</p>}
          <div className="saved-image-grid">
            {working.map((image, index) => {
              const replacement = replacementFiles[image.id]
              return (
                <div
                  className="saved-image-card"
                  key={image.id}
                  draggable={!tagging}
                  onDragStart={() => setDragIndex(index)}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={() => {
                    if (dragIndex !== null) move(dragIndex, index)
                    setDragIndex(null)
                  }}
                >
                  {replacement ? (
                    <FilePreviewButton
                      file={replacement}
                      dimensions={replacementDimensions[image.id]}
                      onDimensions={(value) => setReplacementDimensions((current) => ({ ...current, [image.id]: value }))}
                      onOpen={(nextImage) => setViewer(nextImage)}
                    />
                  ) : (
                    <button className="image-open" onClick={() => setViewer(assetLightboxImage(image.asset, working.map((itemImage) => itemImage.asset)))} title="放大查看">
                      <img src={mediaUrl(assetThumbSrc(image.asset))} alt={image.asset.filename} loading="lazy" />
                    </button>
                  )}
                  <TruncatedText text={replacement?.name ?? image.asset.filename}>
                    {replacement?.name ?? image.asset.filename}<TruncatedText as="small" text={replacement ? fileMeta(replacement, replacementDimensions[image.id]) : assetMeta(image.asset)} />
                  </TruncatedText>
                  <div className="thumb-actions">
                    <label className={tagging ? 'is-disabled' : ''} title="替换图片">
                      <RefreshCw size={14} />
                      <input disabled={tagging} type="file" accept={SUPPORTED_IMAGE_ACCEPT} onChange={(event) => {
                        const nextReplacement = event.target.files?.[0]
                        if (!nextReplacement) return
                        setReplacementFiles((current) => ({ ...current, [image.id]: nextReplacement }))
                        setDirty(true)
                      }} />
                    </label>
                    <button disabled={tagging} onClick={() => {
                      setWorking((current) => current.filter((itemImage) => itemImage.id !== image.id))
                      setRemovedIds((current) => [...current, image.id])
                      setDirty(true)
                    }} title="删除图片"><Trash2 size={14} /></button>
                  </div>
                </div>
              )
            })}
          </div>
          {!working.length && <span className="inline-error">搭配服装至少需要保留一张图片</span>}
        </div>
        <div className="drawer-section">
          <div className="section-head"><strong>AI 识别标签</strong></div>
          <TagObjectPanel title={hasCurrentTaggingFailure ? '上次识别返回' : 'AI 识别标签'} data={item.aiTags ?? {}} defaultOpen />
          <TagObjectPanel title="生效标签" data={item.effectiveTags ?? {}} />
        </div>
        <div className="drawer-section">
          <div className="section-head"><strong>人工业务标签</strong></div>
          <ManualTagsEditor item={item} run={run} disabled={tagging} onChange={onChange} />
        </div>
        <div className="drawer-section">
          <div className="section-head"><strong>官方标题信息</strong></div>
          <TagObjectPanel title="官方标题信息" data={item.officialTitleTags ?? {}} defaultOpen={Boolean(Object.keys(item.officialTitleTags ?? {}).length)} />
        </div>
        {isAdmin && (
          <div className="drawer-section">
            <div className="section-head"><strong>技术详情</strong></div>
            <TagObjectPanel title="标签元信息" data={item.tagMeta ?? {}} />
            {(item.tagAttempts ?? []).length > 0 && (
              <JsonOutputPanel title="Gemini 调用过程事实" data={item.tagAttempts} />
            )}
          </div>
        )}
        <div className="drawer-actions">
          <button className="management-action" disabled={tagging} onClick={retag}>重新识别</button>
          <button className="management-action" disabled={tagging || !dirty || !working.length} onClick={() => void save(false)}>仅保存</button>
          <button className="management-action" disabled={tagging || !dirty || !working.length} onClick={() => void save(true)}>保存并重新识别标签</button>
        </div>
        <div className="danger-zone">
          <strong>{item.archivedAt ? archivedRestoreText(item.archiveReason) : '归档'}</strong>
          <button className="management-action" disabled={tagging || item.archiveReason === 'deletion_pending'} onClick={() => {
            if (item.archivedAt) {
              onRestore()
            } else {
              onDelete()
              onClose()
            }
          }}>{item.archivedAt ? archivedRestoreText(item.archiveReason) : '归档'}</button>
        </div>
      </aside>
      {viewer && <ImageLightbox image={viewer} onClose={() => setViewer(null)} />}
    </div>
  )
}

function ManualTagsEditor({ item, run, disabled = false, onChange }: { item: StylingGarment; run: any; disabled?: boolean; onChange: (item: StylingGarment) => void }) {
  const [launchBatch, setLaunchBatch] = useState('')
  const [series, setSeries] = useState('')
  const [retailChannel, setRetailChannel] = useState('')
  const [internalCollection, setInternalCollection] = useState('')
  const [stylingPriority, setStylingPriority] = useState('')
  const [notes, setNotes] = useState('')
  const [doNotUse, setDoNotUse] = useState(false)

  useEffect(() => {
    const tags = item.manualTags ?? {}
    setLaunchBatch(String(tags.launch_batch ?? ''))
    setSeries(tagListInputValue(tags.series))
    setRetailChannel(tagListInputValue(tags.retail_channel))
    setInternalCollection(tagListInputValue(tags.internal_collection))
    setStylingPriority(String(tags.styling_priority ?? ''))
    setNotes(String(tags.notes ?? ''))
    setDoNotUse(Boolean(tags.do_not_use_for_styling))
  }, [item.id, item.manualTags])

  function currentPayload() {
    return {
      launch_batch: launchBatch.trim(),
      series: parseTagListInput(series),
      retail_channel: parseTagListInput(retailChannel),
      internal_collection: parseTagListInput(internalCollection),
      styling_priority: stylingPriority,
      notes: notes.trim(),
      do_not_use_for_styling: doNotUse,
    }
  }

  const changed = JSON.stringify(currentPayload()) !== JSON.stringify({
    launch_batch: String(item.manualTags?.launch_batch ?? ''),
    series: Array.isArray(item.manualTags?.series) ? item.manualTags?.series : parseTagListInput(String(item.manualTags?.series ?? '')),
    retail_channel: Array.isArray(item.manualTags?.retail_channel) ? item.manualTags?.retail_channel : parseTagListInput(String(item.manualTags?.retail_channel ?? '')),
    internal_collection: Array.isArray(item.manualTags?.internal_collection) ? item.manualTags?.internal_collection : parseTagListInput(String(item.manualTags?.internal_collection ?? '')),
    styling_priority: String(item.manualTags?.styling_priority ?? ''),
    notes: String(item.manualTags?.notes ?? ''),
    do_not_use_for_styling: Boolean(item.manualTags?.do_not_use_for_styling),
  })

  function save() {
    const manualTags = { ...(item.manualTags ?? {}), ...currentPayload() }
    void run('保存人工业务标签中...', () => api.updateStylingGarment(item.id, { manualTags }), (result: { item: StylingGarment }) => onChange(result.item), { refresh: 'background' })
  }

  return (
    <div className="manual-editor">
      <ManualTagFields
        launchBatch={launchBatch}
        onLaunchBatch={setLaunchBatch}
        series={series}
        onSeries={setSeries}
        retailChannel={retailChannel}
        onRetailChannel={setRetailChannel}
        internalCollection={internalCollection}
        onInternalCollection={setInternalCollection}
        stylingPriority={stylingPriority}
        onStylingPriority={setStylingPriority}
        notes={notes}
        onNotes={setNotes}
        disabled={disabled}
      />
      <label className="manual-checkbox">
        <input type="checkbox" checked={doNotUse} disabled={disabled} onChange={(event) => setDoNotUse(event.target.checked)} />
        禁止进入智能选搭
      </label>
      <button className="management-action" disabled={disabled || !changed} onClick={save}>保存人工标签</button>
    </div>
  )
}

function tagListInputValue(value: unknown) {
  if (Array.isArray(value)) return value.map((item) => String(item)).filter(Boolean).join('，')
  return String(value ?? '')
}

function CodeEditor({ item, run, disabled = false }: { item: StylingGarment; run: any; disabled?: boolean }) {
  const [value, setValue] = useState(item.code)
  const changed = value.trim() !== item.code

  useEffect(() => {
    setValue(item.code)
  }, [item.id, item.code])

  return (
    <div className="code-editor">
      <label>货号</label>
      <div>
        <input disabled={disabled} value={value} onChange={(event) => setValue(event.target.value)} />
        <button className="management-action" disabled={disabled || !changed} onClick={() => void run('保存货号中...', () => api.updateStylingGarment(item.id, { code: value }), undefined, { refresh: 'background' })}>保存</button>
      </div>
    </div>
  )
}

function latestFailedTagAttempt(item: StylingGarment) {
  return (item.tagAttempts ?? []).find((entry) => entry.status === 'FAILED') ?? null
}

type GarmentTaggingIssue = {
  badge: string
  panelTitle: string
  title: string
  description: string
  action: string
}

function rawTaggingErrorText(item: StylingGarment, attempt: NonNullable<StylingGarment['tagAttempts']>[number] | null) {
  return [item.tagError, attempt?.errorMessage, attempt?.errorCode].filter(Boolean).join(' ')
}

function isUploadRequirementMiss(item: StylingGarment, attempt: NonNullable<StylingGarment['tagAttempts']>[number] | null) {
  if (item.taggingErrorCode === 'UPLOAD_REQUIREMENT_NOT_MET' || attempt?.errorCode === 'UPLOAD_REQUIREMENT_NOT_MET') return true
  const text = rawTaggingErrorText(item, attempt).toLowerCase()
  return text.includes('selected_files') && (
    text.includes('非空') ||
    text.includes('empty') ||
    text.includes('no selected') ||
    text.includes('至少') ||
    text.includes('must not be empty')
  )
}

function garmentTaggingIssue(item: StylingGarment, attempt: NonNullable<StylingGarment['tagAttempts']>[number] | null): GarmentTaggingIssue {
  if (isUploadRequirementMiss(item, attempt)) {
    return {
      badge: '未通过上传要求',
      panelTitle: '上传要求检查',
      title: '未识别到符合上传要求的服装图',
      description: '这组图片已完成识别，但没有适合作为搭配服装入库的纯服装参考图。',
      action: '请更换为清晰、同一件服装、以商品为主体的图片后重新识别。',
    }
  }
  if (item.taggingErrorCode === 'RATE_LIMITED' || attempt?.errorCode === 'RATE_LIMITED') {
    return {
      badge: '识别失败',
      panelTitle: '识别状态',
      title: 'AI 服务暂时繁忙',
      description: '系统已在安全范围内多次自动尝试，但本次仍未完成识别。',
      action: '请稍后点击“识别标签”重试。',
    }
  }
  return {
    badge: '识别失败',
    panelTitle: '识别状态',
    title: '识别暂时未完成',
    description: '识别调用或返回处理未完成。',
    action: '请重新识别；多次失败请联系开发人员。',
  }
}

function taggingErrorDisplay(item: StylingGarment, attempt: NonNullable<StylingGarment['tagAttempts']>[number] | null): Record<string, unknown> {
  const issue = garmentTaggingIssue(item, attempt)
  const data: Record<string, unknown> = {
    当前状态: issue.badge,
    说明: issue.description,
    建议操作: issue.action,
  }
  if (attempt?.startedAt) data.开始时间 = dateText(attempt.startedAt)
  if (attempt?.failedAt) data.失败时间 = dateText(attempt.failedAt)
  return data
}

function TagObjectPanel({ title, data, defaultOpen = false }: { title: string; data: Record<string, unknown>; defaultOpen?: boolean }) {
  const entries = Object.entries(data ?? {}).filter(([, value]) => value !== undefined && value !== null && value !== '')
  return (
    <details className="tag-details" open={defaultOpen}>
      <summary>{title}</summary>
      {entries.length ? (
        <>
          <div className="tag-field-grid">
            {entries.map(([key, value]) => <TagField key={key} label={key} value={value} />)}
          </div>
        </>
      ) : <p className="empty-inline">暂无</p>}
    </details>
  )
}

function JsonOutputPanel({ title, data, defaultOpen = false }: { title: string; data: unknown; defaultOpen?: boolean }) {
  return (
    <details className="model-output-panel" open={defaultOpen}>
      <summary>{title}</summary>
      <pre>{formatDebugJson(data)}</pre>
    </details>
  )
}

function AICallLogDialog({ dialog, onClose }: {
  dialog: { title: string; ownerTypes: string[]; attemptId: number; records: AICallRecord[]; loading: boolean; error?: string }
  onClose: () => void
}) {
  const dialogRef = useRef<HTMLElement | null>(null)
  const closeButtonRef = useRef<HTMLButtonElement | null>(null)
  useEffect(() => closeButtonRef.current?.focus(), [])
  function trapDialogFocus(event: ReactKeyboardEvent<HTMLElement>) {
    if (event.key === 'Escape') {
      event.preventDefault()
      onClose()
      return
    }
    if (event.key !== 'Tab' || !dialogRef.current) return
    const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>('button:not([disabled]), summary, [href], input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'))
    if (!focusable.length) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }
  const records = dialog.records.filter((record) => dialog.ownerTypes.includes(record.ownerType) && record.id === dialog.attemptId)
  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <section ref={dialogRef} className="confirm-modal ai-log-modal" role="dialog" aria-modal="true" aria-label={dialog.title} onKeyDown={trapDialogFocus} onClick={(event) => event.stopPropagation()}>
        <div className="confirm-head">
          <div>
            <HeadingWithTooltip as="h2" tooltip="展示本次调用真实发生的请求、响应、解析和时间事实；不用于重放任务。">{dialog.title}</HeadingWithTooltip>
          </div>
          <button ref={closeButtonRef} className="ghost-icon" type="button" onClick={onClose} title="关闭"><X size={18} /></button>
        </div>
        {dialog.loading ? (
          <EmptyState title="读取调用记录中" text="正在加载本任务的 AI 调用快照。" />
        ) : dialog.error ? (
          <div className="inline-error">{dialog.error}</div>
        ) : records.length ? (
          <div className="ai-log-list">
            {records.map((record, index) => (
              <details className="ai-log-entry" key={record.id} open>
                <summary>
                  <div>
                    <TruncatedText as="strong" text={operationLabel(record, index)} />
                    <span>{statusText(record.status)}</span>
                  </div>
                </summary>
                <div className="ai-log-meta">
                  <span>Attempt #{record.id}</span>
                  <span>{record.provider}</span>
                  {record.providerRequestId && <span>Request ID: {record.providerRequestId}</span>}
                  {record.errorCode && <span>错误码: {record.errorCode}</span>}
                  <span>开始: {dateText(record.startedAt ?? record.createdAt)}</span>
                  {(record.completedAt || record.failedAt) && <span>结束: {dateText(record.completedAt ?? record.failedAt)}</span>}
                </div>
                {(record.technicalMessage || record.errorMessage) && <div className="inline-error">{record.technicalMessage || record.errorMessage}</div>}
                <div className="ai-log-json-grid">
                  <div>
                    <span>实际模型输入</span>
                    <pre>{formatDebugJson(record.modelInput)}</pre>
                  </div>
                  <div>
                    <span>实际模型输出</span>
                    <pre>{formatDebugJson(record.modelOutput)}</pre>
                  </div>
                  <div>
                    <span>请求过程事实</span>
                    <pre>{formatDebugJson(record.requestFacts)}</pre>
                  </div>
                  <div>
                    <span>响应过程事实</span>
                    <pre>{formatDebugJson(record.responseFacts)}</pre>
                  </div>
                </div>
              </details>
            ))}
          </div>
        ) : (
          <EmptyState title="暂无调用记录" text="当前阶段还没有保存 AI 调用快照。" />
        )}
      </section>
    </div>
  )
}

function formatDebugJson(value: unknown) {
  if (!value || (typeof value === 'object' && !Array.isArray(value) && !Object.keys(value as Record<string, unknown>).length)) return '{}'
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function TagField({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="tag-field">
      <span>{label}</span>
      <div>{renderTagValue(value)}</div>
    </div>
  )
}

function renderTagValue(value: unknown): ReactNode {
  if (Array.isArray(value)) {
    if (!value.length) return <em>空</em>
    if (value.some((item) => typeof item === 'object' && item)) {
      return (
        <div className="nested-tags">
          {value.map((item, index) => (
            <div key={index}>
              <b>{index + 1}</b>
              <TruncatedText text={typeof item === 'object' && item ? JSON.stringify(item, null, 0) : formatTagPrimitive(item)} />
            </div>
          ))}
        </div>
      )
    }
    return <div className="tag-chip-row">{value.map((item, index) => <span key={`${String(item)}-${index}`}>{String(item)}</span>)}</div>
  }
  if (typeof value === 'object' && value) {
    return (
      <div className="nested-tags">
        {Object.entries(value as Record<string, unknown>).map(([key, nested]) => (
          <div key={key}><b title={key}>{key}</b><TruncatedText text={formatTagPrimitive(nested)} /></div>
        ))}
      </div>
    )
  }
  return <>{formatTagPrimitive(value)}</>
}

function formatTagPrimitive(value: unknown) {
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (value === undefined || value === null || value === '') return '空'
  if (Array.isArray(value)) return value.length ? value.join(' / ') : '空'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function AIOperationList({ operations, compact = false, taskId, ownerType, onOpenTechnicalDetails }: {
  operations: AIOperation[]
  compact?: boolean
  taskId?: number | null
  ownerType?: string
  onOpenTechnicalDetails?: (operation: AIOperation) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const [historyItems, setHistoryItems] = useState<AIOperation[] | null>(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyTotal, setHistoryTotal] = useState(0)
  const [historyNextCursor, setHistoryNextCursor] = useState<{ beforeCreatedAt: string; beforeId: number } | null>(null)
  const [historyError, setHistoryError] = useState('')
  const historyRequestRef = useRef(0)
  useEffect(() => {
    historyRequestRef.current += 1
    setExpanded(false)
    setHistoryItems(null)
    setHistoryLoading(false)
    setHistoryTotal(0)
    setHistoryNextCursor(null)
    setHistoryError('')
  }, [taskId, ownerType])
  const combined = historyItems ? [...historyItems, ...operations] : operations
  const actualById = new Map(combined.filter((operation) => operation && operation.id > 0).map((operation) => [operation.id, operation]))
  const actual = Array.from(actualById.values())
  const history = (actual.length ? actual : combined.filter(Boolean)).sort((left, right) => operationTime(right) - operationTime(left))
  const maxVisible = compact ? 1 : expanded ? history.length : 1
  const active = expanded ? history : history.slice(0, maxVisible)
  const now = useTicker(active.some(isAttemptRunning))
  function loadHistory(cursor: { beforeCreatedAt: string; beforeId: number } | null, append: boolean) {
    if (!taskId || !ownerType || historyLoading) return
    const requestId = historyRequestRef.current + 1
    historyRequestRef.current = requestId
    setHistoryLoading(true)
    setHistoryError('')
    void api.listTaskAttempts(taskId, ownerType, { limit: 20, ...(cursor ?? {}) })
      .then((result) => {
        if (historyRequestRef.current !== requestId) return
        setHistoryItems((current) => append && current ? [...current, ...result.items] : result.items)
        setHistoryTotal(result.total)
        setHistoryNextCursor(result.nextCursor)
      })
      .catch((error) => {
        if (historyRequestRef.current === requestId) setHistoryError(error instanceof Error ? error.message : '调用记录读取失败')
      })
      .finally(() => {
        if (historyRequestRef.current === requestId) setHistoryLoading(false)
      })
  }
  function expandHistory() {
    setExpanded(true)
    if (historyItems == null && taskId && ownerType) loadHistory(null, false)
  }
  if (!active.length) return null
  return (
    <div className={`ai-operation-list ${compact ? 'compact' : ''}`}>
      {active.map((operation, index) => (
        <AIOperationCard key={`${operation.id}-${operation.ownerType}-${operation.ownerId}-${index}`} operation={operation} now={now} compact={compact} index={index} onOpenTechnicalDetails={onOpenTechnicalDetails} />
      ))}
      {!compact && !expanded && history.length > maxVisible && (
        <button className="operation-more" onClick={expandHistory}>
          展开历史调用记录
        </button>
      )}
      {!compact && !expanded && history.length === 1 && taskId && ownerType && (
        <button className="operation-more" onClick={expandHistory}>展开历史调用记录</button>
      )}
      {!compact && expanded && historyLoading && <span className="operation-history-note">正在读取调用记录...</span>}
      {!compact && expanded && historyError && (
        <div className="operation-history-error"><span>{historyError}</span><button className="operation-more" type="button" onClick={() => loadHistory(historyNextCursor, historyItems != null)}>重试</button></div>
      )}
      {!compact && expanded && historyNextCursor != null && !historyLoading && (
        <button className="operation-more" onClick={() => loadHistory(historyNextCursor, true)}>加载更多（已显示 {history.length}/{historyTotal}）</button>
      )}
      {!compact && expanded && history.length > 0 && (
        <button className="operation-more" onClick={() => setExpanded(false)}>
          收起调用记录
        </button>
      )}
    </div>
  )
}

function AIOperationCard({ operation, now, compact, index, onOpenTechnicalDetails }: { operation: AIOperation; now: number; compact?: boolean; index: number; onOpenTechnicalDetails?: (operation: AIOperation) => void }) {
  const running = isAttemptRunning(operation)
  const seconds = elapsedSeconds(operation.startedAt || operation.createdAt, now, operation.completedAt || operation.failedAt)
  const phase = running ? currentAttemptPhase(operation) : null
  const breakdown = attemptPhaseBreakdown(operation, now)
  const failureNote = attemptFriendlyFailure(operation)
  return (
    <div className={`ai-operation-card ${running ? 'is-running' : ''} ${compact ? 'compact' : ''}`}>
      <div className="ai-operation-icon">{running ? <Loader2 size={15} className="spin" /> : <Clock3 size={15} />}</div>
      <div className="ai-operation-copy">
        <TruncatedText as="strong" text={operationUserLabel(operation)} />
        {phase && <span>{attemptPhaseText(phase, operation)}</span>}
        {failureNote && <TruncatedText className="operation-failure" text={failureNote} />}
        {!compact && breakdown && <TruncatedText className="operation-breakdown" text={breakdown} />}
      </div>
      <div className="ai-operation-meta">
        <span className={`operation-status status-${operation.status.toLowerCase()}`}>{attemptStatusText(operation)}</span>
        <time dateTime={operation.startedAt ?? operation.createdAt ?? undefined} title={fullDateTimeText(operation.startedAt ?? operation.createdAt)}>{dateText(operation.startedAt ?? operation.createdAt)}</time>
        <b>{formatDuration(seconds)}</b>
        {onOpenTechnicalDetails && operation.id > 0 && <button className="operation-technical-button" type="button" onClick={() => onOpenTechnicalDetails(operation)}><Eye size={13} />技术详情</button>}
      </div>
    </div>
  )
}

const ATTEMPT_PHASE_LABELS: Record<string, string> = {
  prepare: '准备',
  upload: '上传',
  submit: '提交',
  queue: '排队',
  generate: '生成',
  deliver: '回传',
  store: '入库',
  styling_context: '提交',
  styling_gemini: '选搭',
  styling_contract_retry_wait: '修正选搭结果',
  styling_validate: '校验',
  styling_store: '保存',
}

function attemptStatusText(operation: AIOperation) {
  if (operation.status === 'FAILED' && operation.errorCode === 'DELIVERY_FAILED') return '已生成，回传失败'
  if (operation.status === 'FAILED' && operation.errorCode === 'PROVIDER_TIMEOUT') return '生成超时'
  return statusText(operation.status)
}

// 失败 attempt 的一行友好文案（按 error_code 归类），技术码仍保留在“调用记录”里。
function attemptFriendlyFailure(operation: AIOperation): string | null {
  if (operation.status !== 'FAILED') return null
  if (operation.error) {
    return operation.error.message && operation.error.message !== operation.error.title
      ? `${operation.error.title}：${operation.error.message}`
      : operation.error.title
  }
  return friendlyErrorText(operation.errorCode, operation.errorMessage)
}

function attemptPhaseText(entry: AttemptPhase, operation?: AIOperation) {
  const base = ATTEMPT_PHASE_LABELS[entry.phase] ?? entry.phase
  const detail = (entry.detail ?? {}) as Record<string, unknown>
  const retryIndex = Number(operation?.retryIndex)
  if (Number.isFinite(retryIndex) && retryIndex > 0) {
    if (operation?.retryMode === 'regenerate' && entry.phase === 'generate') return `${base}(重试${retryIndex})`
    if (operation?.retryMode === 'redeliver' && entry.phase === 'deliver') return `${base}(重试${retryIndex})`
  }
  if (entry.phase === 'upload') {
    const completed = Number(detail.completed)
    const total = Number(detail.total)
    if (Number.isFinite(completed) && Number.isFinite(total) && total > 0) {
      return `${base} ${Math.min(completed, total)}/${total}`
    }
  }
  if (entry.phase === 'deliver') {
    const retries = Number(detail.retries)
    if (Number.isFinite(retries) && retries > 0) return `${base}(重试${retries})`
  }
  if (entry.phase === 'styling_contract_retry_wait') {
    const contractRetry = Number(detail.contractRetry)
    if (Number.isFinite(contractRetry) && contractRetry > 0) return `${base}(重试${contractRetry})`
  }
  const statusCheckErrors = Number(detail.statusCheckErrors)
  if (Number.isFinite(statusCheckErrors) && statusCheckErrors > 0) return `${base}(重试${statusCheckErrors})`
  return base
}

function currentAttemptPhase(operation: AIOperation): AttemptPhase | null {
  const progress = operation.progress
  if (!progress?.current || !progress.phases?.length) return null
  for (let index = progress.phases.length - 1; index >= 0; index -= 1) {
    if (progress.phases[index].phase === progress.current) return progress.phases[index]
  }
  return null
}

function attemptPhaseBreakdown(operation: AIOperation, now = Date.now()) {
  const phases = operation.progress?.phases ?? []
  const retryPrefix = attemptRetryPrefix(phases)
  const breakdown = phases
    .filter((entry) => entry.phase !== 'prepare' && entry.startedAt)
    .map((entry) => {
      const seconds = attemptPhaseDurationSeconds(entry, operation, now)
      return `${attemptPhaseText(entry, operation)} ${formatDuration(seconds)}`
    })
    .filter(Boolean)
    .join(' · ')
  return breakdown ? `${retryPrefix}${breakdown}` : ''
}

function attemptPhaseDurationSeconds(entry: AttemptPhase, operation: AIOperation, now = Date.now()) {
  const detail = (entry.detail ?? {}) as Record<string, unknown>
  const rawDuration = detail.durationSeconds
  const explicitDuration = Number(rawDuration)
  if (rawDuration != null && Number.isFinite(explicitDuration) && explicitDuration >= 0) return Math.floor(explicitDuration)
  return elapsedSeconds(entry.startedAt, now, entry.endedAt || operation.completedAt || operation.failedAt)
}

function attemptRetryPrefix(phases: AttemptPhase[]) {
  const retryCount = phases.reduce((max, entry) => {
    const detail = (entry.detail ?? {}) as Record<string, unknown>
    const generationRetry = Number(detail.generationRetry)
    const unknownSubmissionRetry = Number(detail.unknownSubmissionRetry)
    const contractRetry = Number(detail.contractRetry)
    return Math.max(
      max,
      Number.isFinite(generationRetry) ? generationRetry : 0,
      Number.isFinite(unknownSubmissionRetry) ? unknownSubmissionRetry : 0,
      Number.isFinite(contractRetry) ? contractRetry : 0,
    )
  }, 0)
  return retryCount > 0 ? `(重试${retryCount}) ` : ''
}

function useTicker(enabled: boolean) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!enabled) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [enabled])
  return now
}

function operationFallbacks(task: GenerationTask, localOperations: AIOperation[], ownerType: string) {
  const remote = (task.aiOperations ?? []).filter((operation) => operation.ownerType === ownerType)
  const local = localOperations.filter((operation) => operation.ownerType === ownerType && operationBelongsToTask(operation, task))
  if (remote.length) return remote.sort((left, right) => operationTime(right) - operationTime(left))
  if (local.length) return local
  if (ownerType === 'generation_styling' && task.status === 'STYLING_RUNNING') {
    return [syntheticOperation(ownerType, task.id, 'gemini', task.updatedAt)]
  }
  if (ownerType === 'generation_preview' && task.status === 'PREVIEW_RUNNING') {
    return [syntheticOperation(ownerType, task.activeStoryboardPreviewId ?? task.activePreviewId ?? task.id, 'image-2', task.storyboardPreview?.createdAt || task.preview?.createdAt || task.updatedAt)]
  }
  return []
}

function latestRunningTagAttempt(item: StylingGarment): AIOperation | null {
  const attempt = (item.tagAttempts ?? []).find((entry) => entry.status === 'RUNNING')
  if (attempt) {
    return {
      id: attempt.id,
      ownerType: 'styling_garment_tagging',
      ownerId: item.id,
      provider: attempt.provider,
      status: attempt.status,
      startedAt: attempt.startedAt,
      completedAt: attempt.completedAt,
      failedAt: attempt.failedAt,
      errorMessage: attempt.errorMessage,
    }
  }
  if (item.status === 'TAGGING_RUNNING') return syntheticOperation('styling_garment_tagging', item.id, 'gemini', item.updatedAt)
  return null
}

function operationBelongsToTask(operation: AIOperation, task: GenerationTask) {
  return operation.taskId === task.id
}

function mergeOperations(operations: AIOperation[]) {
  const byKey = new Map<string, AIOperation>()
  for (const operation of operations) {
    const key = operationKey(operation)
    const existing = byKey.get(key)
    if (!existing || operationRank(operation) > operationRank(existing)) {
      byKey.set(key, operation)
    }
  }
  return Array.from(byKey.values()).sort((left, right) => operationTime(right) - operationTime(left))
}

function operationKey(operation: AIOperation) {
  if (operation.ownerType === 'generation_result_image') {
    return `${operation.ownerType}:${operation.ownerId || operation.id}`
  }
  if (isStageOperation(operation.ownerType) && isAttemptRunning(operation)) {
    return `${operation.ownerType}:${operation.provider}:active`
  }
  return `${operation.ownerType}:${operation.provider}:${operation.ownerId > 0 ? operation.ownerId : 'current'}`
}

function isStageOperation(ownerType: string) {
  return ['generation_styling', 'generation_preview'].includes(ownerType)
}

function operationRank(operation: AIOperation) {
  return operation.id > 0 ? 2 : 1
}

function operationTime(operation: AIOperation) {
  return parseRuyiTime(operation.startedAt || operation.createdAt || operation.updatedAt)
}

function syntheticOperation(ownerType: string, ownerId: number, provider: string, startedAt?: string | null): AIOperation {
  return {
    id: -ownerId,
    ownerType,
    ownerId,
    provider,
    status: 'RUNNING',
    startedAt: startedAt || new Date().toISOString(),
  }
}

function isAttemptRunning(operation: AIOperation) {
  return ['QUEUED', 'RUNNING', 'SUBMITTED', 'IN_QUEUE', 'IN_PROGRESS'].includes(operation.status)
}

function operationLabel(operation: AIOperation, index = 0) {
  if (operation.ownerType === 'styling_garment_upload') return '搭配服装上传'
  if (operation.ownerType === 'styling_garment_tagging') return '服装标签识别'
  if (operation.ownerType === 'generation_styling') return '智能选搭'
  if (operation.ownerType === 'generation_preview') return '分镜预览'
  if (operation.ownerType === 'generation_result_image') {
    const shot = operation.shotSortOrder ?? (operation.id < 0 && operation.ownerId > 0 ? operation.ownerId : null)
    return shot ? `镜头 ${shot} · Attempt #${operation.id}` : `成片调用 · Attempt #${operation.id || index + 1}`
  }
  return '系统处理中'
}

function operationUserLabel(operation: AIOperation) {
  if (operation.ownerType === 'generation_result_image') {
    const shot = operation.shotSortOrder ?? (operation.id < 0 && operation.ownerId > 0 ? operation.ownerId : null)
    return shot ? `镜头 ${shot} 成片` : '成片生成'
  }
  return operationLabel(operation)
}

function elapsedSeconds(start?: string | null, now = Date.now(), end?: string | null) {
  if (!start) return 0
  const startMs = parseRuyiTime(start)
  if (!Number.isFinite(startMs)) return 0
  const endMs = end ? parseRuyiTime(end) : now
  return Math.max(0, Math.floor((endMs - startMs) / 1000))
}

function parseRuyiTime(value?: string | null) {
  if (!value) return Number.NaN
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`
  return Date.parse(normalized)
}

function formatDuration(seconds: number) {
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(rest).padStart(2, '0')}`
}

function CurrentTaskCard({ task }: { task: GenerationTask }) {
  return <div className="current-task"><ThumbGrid assets={(task.images ?? []).map((item) => item.asset)} /><Meta label="当前任务" value={task.name} /><Meta label="状态" value={statusText(task.status)} /></div>
}

function ThumbGrid({ assets, compact = false }: { assets: MediaAsset[]; compact?: boolean }) {
  return <div className={`thumb-grid ${compact ? 'compact' : ''}`}>{assets.slice(0, compact ? 4 : 8).map((asset) => <AssetThumb key={asset.id} asset={asset} />)}</div>
}

function AssetThumb({ asset, large = false, interactive = true }: { asset?: MediaAsset | null; large?: boolean; interactive?: boolean }) {
  const [viewer, setViewer] = useState<LightboxImage | null>(null)
  const [failed, setFailed] = useState(false)
  const [srcIndex, setSrcIndex] = useState(0)
  const sources = asset ? assetDisplaySources(asset, 'thumb') : []
  const src = sources[srcIndex] ?? ''
  useEffect(() => {
    setFailed(false)
    setSrcIndex(0)
  }, [asset?.id, sources.join('|')])
  const image = asset && src && !failed ? (
    <img
      src={mediaUrl(src)}
      alt={asset.filename}
      loading="lazy"
      onError={() => {
        if (srcIndex + 1 < sources.length) {
          setSrcIndex((index) => index + 1)
          return
        }
        setFailed(true)
      }}
    />
  ) : null
  return (
    <>
      <div className={`asset-thumb ${large ? 'large' : ''} ${failed ? 'is-broken' : ''}`}>
        {image && asset ? (
          interactive ? (
            <button className="asset-open" onClick={() => setViewer(assetLightboxImage(asset))} title="放大查看">
              {image}
            </button>
          ) : image
        ) : <div className="asset-fallback"><FileImage size={20} /><span>图片不可用</span></div>}
      </div>
      {viewer && <ImageLightbox image={viewer} onClose={() => setViewer(null)} />}
    </>
  )
}

function FinalImageStrip({ images }: { images: { id: number; sortOrder: number; status: string; asset?: MediaAsset | null }[] }) {
  return <div className="final-strip">{images.map((image) => <AssetThumb key={image.id} asset={image.asset} />)}</div>
}

function RecordImagePreview({ task }: { task: GenerationTask }) {
  const [viewer, setViewer] = useState<LightboxImage | null>(null)
  const previewAsset = (task.storyboardPreview?.asset ?? task.preview?.asset) ?? null
  const finalAssets = (task.resultSet?.images ?? [])
    .slice()
    .sort((left, right) => left.sortOrder - right.sortOrder)
    .map((image) => image.asset)
    .filter((asset): asset is MediaAsset => Boolean(asset))
  const galleryAssets = [previewAsset, ...finalAssets].filter((asset): asset is MediaAsset => Boolean(asset))

  function openAsset(asset: MediaAsset) {
    setViewer(assetLightboxImage(asset, galleryAssets))
  }

  if (!galleryAssets.length) return <FinalImageStrip images={task.resultSet?.images ?? []} />

  return (
    <div className="record-image-preview">
      {previewAsset && (
        <button className="detail-preview-open" onClick={() => openAsset(previewAsset)} title="放大查看分镜预览">
          <img className="detail-preview" src={mediaUrl(assetPreviewSrc(previewAsset))} loading="lazy" alt="分镜预览" />
        </button>
      )}
      {!!finalAssets.length && (
        <div className="final-strip">
          {finalAssets.map((asset) => (
            <button className="asset-thumb record-thumb" key={asset.id} onClick={() => openAsset(asset)} title="放大查看成片">
              <img src={mediaUrl(assetThumbSrc(asset))} alt={asset.filename} loading="lazy" />
            </button>
          ))}
        </div>
      )}
      {viewer && <ImageLightbox image={viewer} onClose={() => setViewer(null)} />}
    </div>
  )
}

function ImageLightbox({ image, onClose }: { image: LightboxImage; onClose: () => void }) {
  const [items] = useState<LightboxImageItem[]>(() => image.items?.length ? image.items : [image])
  const [index, setIndex] = useState(() => image.initialIndex ?? 0)
  const [zoom, setZoom] = useState<'fit' | number>('fit')
  const [imageFailed, setImageFailed] = useState(false)
  const [naturalSizes, setNaturalSizes] = useState<Record<string, { width: number; height: number }>>({})
  const [remoteSizes, setRemoteSizes] = useState<Record<string, number>>({})
  const current = items[index] ?? items[0]
  const zoomLabel = zoom === 'fit' ? '适应' : `${Math.round(zoom * 100)}%`
  const canStep = items.length > 1
  const naturalSize = naturalSizes[current.src]
  const remoteSize = remoteSizes[current.src]
  const zoomedStyle = zoom === 'fit' ? undefined : { width: naturalSize ? `${naturalSize.width * zoom}px` : 'auto' }
  const details = {
    ...(current.details ?? {}),
    ...(naturalSize && (!current.details?.尺寸 || current.details?.尺寸 === '尺寸读取中') ? { 尺寸: dimensionText(naturalSize) } : {}),
    ...(remoteSize && (!current.details?.大小 || current.details?.大小 === '未知大小') ? { 大小: formatBytes(remoteSize) } : {}),
  }
  const setNext = () => {
    if (!canStep) return
    setIndex((value) => (value + 1) % items.length)
    setZoom('fit')
  }
  const setPrev = () => {
    if (!canStep) return
    setIndex((value) => (value - 1 + items.length) % items.length)
    setZoom('fit')
  }
  const adjustZoom = (direction: 1 | -1) => {
    setZoom((value) => {
      const base = value === 'fit' ? 1 : value
      return Math.min(4, Math.max(0.5, base + direction * 0.25))
    })
  }
  const handleImageError = () => {
    setImageFailed(true)
    void fetch(current.src, { method: 'HEAD', credentials: 'include' })
      .then((response) => {
        if (response.status === 401) window.dispatchEvent(new CustomEvent('ruyi-auth-expired'))
      })
      .catch(() => {})
  }
  useEffect(() => {
    setImageFailed(false)
  }, [current.src])
  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
      if (event.key === '+' || event.key === '=') adjustZoom(1)
      if (event.key === '-') adjustZoom(-1)
      if (event.key === '0') setZoom('fit')
      if (event.key === 'ArrowRight') setNext()
      if (event.key === 'ArrowLeft') setPrev()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [canStep, onClose])

  useEffect(() => {
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previous
    }
  }, [])

  useEffect(() => {
    if (current.details?.大小 && current.details.大小 !== '未知大小') return
    if (remoteSizes[current.src]) return
    let cancelled = false
    fetch(current.src, { method: 'HEAD', credentials: 'include' })
      .then((response) => Number(response.headers.get('content-length')))
      .then((size) => {
        if (!cancelled && Number.isFinite(size) && size > 0) setRemoteSizes((value) => ({ ...value, [current.src]: size }))
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [current.details, current.src, remoteSizes])

  return createPortal(
    <div className="lightbox" role="dialog" aria-modal="true" aria-label="图片检查器" onClick={onClose}>
      <div className="lightbox-chrome" onClick={(event) => event.stopPropagation()}>
        <div className="lightbox-head">
          <div className="inspector-title">
            <TruncatedText as="strong" text={current.title} />
            <span>{index + 1} / {items.length}{current.meta ? ` · ${current.meta}` : ''}</span>
          </div>
          <div className="lightbox-actions">
            <button onClick={() => setPrev()} disabled={!canStep} title="上一张"><ChevronLeft size={16} /></button>
            <button onClick={() => adjustZoom(-1)} title="缩小"><ZoomOut size={16} /></button>
            <button className="text-action" onClick={() => setZoom('fit')} title="适应窗口">{zoomLabel}</button>
            <button onClick={() => adjustZoom(1)} title="放大"><ZoomIn size={16} /></button>
            <button onClick={() => setZoom(1)} title="原始尺寸">1:1</button>
            {current.downloadUrl && <a href={current.downloadUrl} title="下载原图"><Download size={16} /></a>}
            <button onClick={() => setNext()} disabled={!canStep} title="下一张"><ChevronRight size={16} /></button>
            <button onClick={onClose} title="关闭"><X size={16} /></button>
          </div>
        </div>
      </div>
      <div className="lightbox-stage" onClick={(event) => event.stopPropagation()}>
        {imageFailed ? (
          <div className="lightbox-error">
            <FileImage size={28} />
            <strong>图片加载失败</strong>
            <span>登录状态可能已经过期，请重新登录后再查看原图。</span>
          </div>
        ) : (
          <img
            src={current.src}
            alt={current.title}
            className={zoom === 'fit' ? 'is-fit' : 'is-zoomed'}
            style={zoomedStyle}
            onError={handleImageError}
            onLoad={(event) => {
              const img = event.currentTarget
              setNaturalSizes((value) => ({ ...value, [current.src]: { width: img.naturalWidth, height: img.naturalHeight } }))
            }}
          />
        )}
      </div>
      <div className="lightbox-meta" onClick={(event) => event.stopPropagation()}>
        {Object.entries(details).map(([label, value]) => (
          <span key={label}><b>{label}</b>{value}</span>
        ))}
      </div>
        {items.length > 1 && (
          <div className="inspector-strip" aria-label="同组图片" onClick={(event) => event.stopPropagation()}>
            {items.map((item, itemIndex) => (
              <button className={itemIndex === index ? 'active' : ''} key={`${item.title}-${itemIndex}`} onClick={() => {
                setIndex(itemIndex)
                setZoom('fit')
              }} title={item.title}>
                <img src={item.thumbSrc ?? item.src} alt={item.title} loading="lazy" />
              </button>
            ))}
          </div>
        )}
    </div>,
    document.body,
  )
}

function TruncatedText({ as = 'span', text, className, children }: { as?: 'span' | 'strong' | 'small' | 'em'; text: string; className?: string; children?: ReactNode }) {
  const title = text || undefined
  if (as === 'strong') return <strong className={className} title={title}>{children ?? text}</strong>
  if (as === 'small') return <small className={className} title={title}>{children ?? text}</small>
  if (as === 'em') return <em className={className} title={title}>{children ?? text}</em>
  return <span className={className} title={title}>{children ?? text}</span>
}

function Meta({ label, value }: { label: string; value: ReactNode }) {
  return <div className="meta"><span>{label}</span><strong>{value}</strong></div>
}

function EmptyState({ title, text }: { title: string; text: string }) {
  return <div className="empty"><Clock3 size={22} /><strong>{title}</strong><span>{text}</span></div>
}

function SearchBox({
  value,
  onChange,
  placeholder,
  panelPlaceholder,
  busy,
}: {
  value: string
  onChange: (value: string) => void
  placeholder: string
  panelPlaceholder: string
  busy?: boolean
}) {
  const rootRef = useRef<HTMLDivElement | null>(null)
  const panelRef = useRef<HTMLDivElement | null>(null)
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(value)
  const [panelStyle, setPanelStyle] = useState<CSSProperties>({})
  const terms = splitSearchTerms(value)
  const draftTerms = splitSearchTerms(draft)
  const isBulk = terms.length >= BULK_SEARCH_THRESHOLD

  useEffect(() => {
    if (open) setDraft(value)
  }, [open, value])

  useEffect(() => {
    if (!open) return
    const updatePanelPosition = () => {
      const rect = rootRef.current?.getBoundingClientRect()
      if (!rect) return
      const viewportSafeWidth = Math.max(160, window.innerWidth - 24)
      const width = Math.min(560, viewportSafeWidth, Math.max(Math.min(420, viewportSafeWidth), rect.width))
      const left = Math.min(Math.max(12, rect.left), window.innerWidth - width - 12)
      const availableBelow = window.innerHeight - rect.bottom - 20
      const availableAbove = rect.top - 20
      const placeAbove = availableBelow < 320 && availableAbove > availableBelow
      const maxHeight = Math.max(260, Math.min(420, (placeAbove ? availableAbove : availableBelow) - 8))
      const top = placeAbove
        ? Math.max(12, rect.top - maxHeight - 8)
        : Math.min(rect.bottom + 8, window.innerHeight - maxHeight - 12)
      setPanelStyle({ left, top, width, maxHeight })
    }
    updatePanelPosition()
    window.addEventListener('resize', updatePanelPosition)
    window.addEventListener('scroll', updatePanelPosition, true)
    return () => {
      window.removeEventListener('resize', updatePanelPosition)
      window.removeEventListener('scroll', updatePanelPosition, true)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const handlePointer = (event: MouseEvent) => {
      const target = event.target as Node
      if (rootRef.current?.contains(target) || panelRef.current?.contains(target)) return
      setOpen(false)
    }
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', handlePointer)
    document.addEventListener('keydown', handleKey)
    return () => {
      document.removeEventListener('mousedown', handlePointer)
      document.removeEventListener('keydown', handleKey)
    }
  }, [open])

  function applyDraft() {
    onChange(draft)
    setOpen(false)
  }

  function clearSearch() {
    setDraft('')
    onChange('')
    setOpen(false)
  }

  const previewTerms = (isBulk ? terms : draftTerms).slice(0, 3)

  return (
    <div ref={rootRef} className={`search ${busy ? 'is-searching' : ''} ${open ? 'is-open' : ''} ${isBulk ? 'has-summary' : ''}`}>
      <Search size={16} />
      {isBulk ? (
        <button className="search-summary" type="button" onClick={() => setOpen(true)} title="编辑批量搜索" aria-haspopup="dialog" aria-expanded={open}>
          <span>批量搜索：{terms.length} 个关键词</span>
          <TruncatedText as="small" text={terms.join(' / ')}>
            {previewTerms.join(' / ')}{terms.length > previewTerms.length ? ` / +${terms.length - previewTerms.length}` : ''}
          </TruncatedText>
        </button>
      ) : (
        <input
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              setOpen(true)
            }
          }}
          placeholder={placeholder}
        />
      )}
      {busy ? (
        <span className="search-hint"><Loader2 size={14} className="spin" />搜索中</span>
      ) : isBulk ? null : terms.length > 1 ? (
        <button className="search-hint is-muted as-button" type="button" onClick={() => setOpen(true)} aria-haspopup="dialog" aria-expanded={open}>{terms.length} 词</button>
      ) : (
        <button className="search-bulk-button" type="button" onClick={() => setOpen(true)} aria-haspopup="dialog" aria-expanded={open}>批量</button>
      )}
      {value.trim() && (
        <button className="search-clear" type="button" onClick={clearSearch} title="清空搜索" aria-label="清空搜索">
          <X size={14} />
        </button>
      )}
      {open && createPortal(
        <div ref={panelRef} className="search-popover" style={panelStyle} role="dialog" aria-modal="false" aria-label="批量搜索">
          <div className="search-popover-head">
            <strong>批量搜索</strong>
            <span>换行粘贴多个货号或关键词</span>
          </div>
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') setOpen(false)
              if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') applyDraft()
            }}
            placeholder={panelPlaceholder}
            autoFocus
          />
          <div className="search-popover-meta">
            <span>已识别 {draftTerms.length} 个关键词</span>
            {draftTerms.length > 0 && (
              <div className="search-term-preview">
                {draftTerms.slice(0, 6).map((term) => <b key={term} title={term}>{term}</b>)}
                {draftTerms.length > 6 && <b>+{draftTerms.length - 6}</b>}
              </div>
            )}
          </div>
          <div className="search-popover-actions">
            <button className="management-action compact-control" type="button" onClick={() => setOpen(false)}>取消</button>
            <button className="management-action compact-control" type="button" onClick={clearSearch}>清空</button>
            <button className="task-action compact-control" type="button" onClick={applyDraft}>应用搜索</button>
          </div>
        </div>,
        document.body,
      )}
    </div>
  )
}

function getTaskStep(task: GenerationTask | null): StudioStep {
  if (!task) return 0
  if (task.status === 'CANCELLED') {
    if (task.resultSet?.images?.length) return 3
    if ((task.storyboardPreview ?? task.preview)?.asset) return 3
    if (task.styling) return 2
    return 1
  }
  if (task.status === 'FAILED') {
    if (task.resultSet?.images?.length) return 3
    if ((task.storyboardPreview ?? task.preview)?.asset) return 3
    if (task.styling) return 2
    return 1
  }
  if (['DRAFT', 'READY_FOR_STYLING', 'STYLING_RUNNING'].includes(task.status)) return 1
  if (['STYLING_READY', 'PREVIEW_RUNNING'].includes(task.status)) return 2
  if (['PREVIEW_READY', 'FINAL_RUNNING', 'FINAL_PARTIAL', 'COMPLETED'].includes(task.status)) return 3
  return 0
}

function taskHasHeroImage(task: GenerationTask) {
  // 详情序列化看 images；批次条/最近任务等轻量序列化没有 images，用 summary.cover 判断有无主推款图。
  return (task.images?.length ?? 0) > 0 || !!task.summary?.cover
}

function canStartOneClickFinal(task: GenerationTask) {
  if (isTaskRunning(task) || task.autoRunTarget) return false
  if (['ABANDONED', 'COMPLETED'].includes(task.status)) return false
  return taskHasHeroImage(task)
}

// 已完成任务可「重新一键成片」：整条重跑出全新方案，旧成片保留为历史。仅单任务提供，批次不参与。
function canRestartOneClickFinal(task: GenerationTask) {
  if (isTaskRunning(task) || task.autoRunTarget) return false
  return task.status === 'COMPLETED' && taskHasHeroImage(task)
}

function oneClickFinalLabel(task: GenerationTask) {
  if (isTaskQueuedForAutoRun(task)) return '排队中'
  if (task.autoRunTarget === 'FINAL' || isTaskRunning(task)) return '自动生成中'
  if (task.status === 'COMPLETED') return '重新一键成片'
  if (['FINAL_PARTIAL', 'FAILED', 'CANCELLED'].includes(task.status)) return '继续一键成片'
  return '一键成片'
}

type StudioTaskPresentation = {
  statusLabel: string
  currentStageLabel: string
  progressLabel: string
  executingStage: StudioStep | null
}

function studioTaskPresentation(task: GenerationTask): StudioTaskPresentation {
  const completedStageCount = completedStudioStageCount(task)
  const runningStage = runningStudioStage(task)
  const currentStageLabel = runningStage != null
    ? steps[runningStage].title
    : task.status === 'COMPLETED'
      ? '全部完成'
      : steps[getTaskStep(task)].title
  return {
    statusLabel: taskListStatusLabel(task),
    currentStageLabel,
    progressLabel: `已完成 ${completedStageCount}/4 步`,
    executingStage: runningStage,
  }
}

function runningStudioStage(task: GenerationTask): StudioStep | null {
  const map: Record<string, StudioStep> = {
    STYLING_RUNNING: 1,
    PREVIEW_RUNNING: 2,
    FINAL_RUNNING: 3,
  }
  return map[task.status] ?? null
}

function completedStudioStageCount(task: GenerationTask) {
  let count = taskHasHeroImage(task) ? 1 : 0
  if (task.activeStylingId || task.styling) count += 1
  if ((task.storyboardPreview ?? task.preview)?.asset) count += 1
  if (finalSucceededCount(task) >= 5) count += 1
  return count
}

function isTaskRunning(task: GenerationTask) {
  return ['STYLING_RUNNING', 'PREVIEW_RUNNING', 'FINAL_RUNNING'].includes(task.status)
}

function isTaskLockedForGeneration(task: GenerationTask) {
  return isTaskRunning(task) || !!task.autoRunTarget
}

function isTaskQueuedForAutoRun(task: GenerationTask) {
  if (task.autoRunQueued) return true
  return task.autoRunTarget === 'FINAL' && !isTaskRunning(task) && !['CANCELLED', 'ABANDONED', 'COMPLETED'].includes(task.status)
}

function taskListStatusLabel(task: GenerationTask) {
  if (isTaskQueuedForAutoRun(task)) {
    return task.autoRunQueuePosition && task.autoRunQueuePosition > 0 ? `排队中 · 第 ${task.autoRunQueuePosition} 位` : '排队中'
  }
  return statusText(task.status)
}

function finalSucceededCount(task: GenerationTask) {
  return task.resultSet?.images?.filter((image) => image.status === 'SUCCEEDED').length ?? 0
}

function statusText(status: string) {
  const map: Record<string, string> = {
    DRAFT: '待选搭',
    READY_FOR_STYLING: '待选搭',
    STYLING_RUNNING: '选搭中',
    STYLING_READY: '已选搭',
    PREVIEW_RUNNING: '分镜预览中',
    PREVIEW_READY: '分镜预览完成',
    FINAL_RUNNING: '生成中',
    FINAL_PARTIAL: '部分完成',
    COMPLETED: '已完成',
    FAILED: '失败',
    CANCELLED: '已取消',
    ABANDONED: '已废弃',
    SUPERSEDED: '已失效',
    READY: '可用',
    TAGGING_READY: '待复核',
    TAGGING_RUNNING: '识别中',
    TAGGING_FAILED: '识别失败',
    RUNNING: '运行中',
    SUBMITTED: '已提交',
    IN_QUEUE: '排队中',
    IN_PROGRESS: '生成中',
    SUCCEEDED: '成功',
  }
  return map[status] ?? status
}

function archiveStateText(reason?: string | null) {
  if (reason === 'deletion_pending') return '删除中'
  if (reason === 'deletion_failed') return '删除失败'
  return '已归档'
}

function archivedRestoreText(reason?: string | null) {
  if (reason === 'deletion_failed') return '恢复后重试'
  return '恢复归档'
}

function recordStatusText(status: string) {
  const map: Record<string, string> = {
    ARCHIVED: '已归档',
    RUNNING: '进行中',
    FAILED: '存在失败',
    PARTIAL: '部分完成',
    CANCELLED: '已取消',
    COMPLETED: '全部完成',
    PENDING: '待处理',
  }
  return map[status] ?? statusText(status)
}

function garmentStatus(status: string) {
  if (status === 'DRAFT') return '待识别'
  return statusText(status)
}

function formatConfidence(value: unknown) {
  const number = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(number) ? `${Math.round(number * 100)}%` : '未生成'
}

function tagText(value: unknown) {
  const text = String(value ?? '').trim()
  if (!text || ['unknown', 'other', '等待标签', '暂无'].includes(text.toLowerCase())) return ''
  return text
}

function collectTagValues(value: unknown): string[] {
  if (Array.isArray(value)) return value.map(tagText).filter(Boolean)
  if (typeof value === 'object' && value) {
    const colors = value as Record<string, unknown>
    return [colors.primary, ...(Array.isArray(colors.secondary) ? colors.secondary : [])].map(tagText).filter(Boolean)
  }
  const text = tagText(value)
  return text ? [text] : []
}

function garmentTagChips(item: StylingGarment) {
  const tags = item.aiTags ?? {}
  const effective = item.effectiveTags ?? {}
  const values = [
    ...collectTagValues(tags['一级品类'] ?? tags.category ?? effective.category_label ?? effective.category),
    ...collectTagValues(tags['二级品类'] ?? tags.sub_category ?? effective.sub_category),
    ...collectTagValues(tags['色彩'] ?? tags['颜色'] ?? tags.colors ?? effective.color_labels ?? effective.colors),
    ...collectTagValues(tags['季节'] ?? effective.season ?? tags.season),
  ]
  return Array.from(new Set(values)).slice(0, 3)
}

function firstTagText(...values: unknown[]) {
  for (const value of values) {
    const text = tagText(value)
    if (text) return text
  }
  return ''
}

function garmentBrief(item: StylingGarment, compatibility?: Record<string, unknown>) {
  const tags = item.aiTags ?? {}
  const effective = item.effectiveTags ?? {}
  return firstTagText(
    effective.pairing_brief,
    compatibility?.pairing_brief,
    tags['搭配适配建议'],
    tags['造型角色建议'],
    tags['整体视觉语义'],
    tags.overall_summary,
    tags['风格语义'],
    tags.style_summary,
    tags['结构/廓形语义'],
    tags.structure_summary,
  )
}

function compactTagLine(value: Record<string, unknown>) {
  const keys = ['category', 'sub_category', 'colors', 'season', 'occasion', 'pairing_brief', 'year', 'official_series']
  const parts = keys
    .filter((key) => value[key] !== undefined && value[key] !== null && value[key] !== '')
    .map((key) => `${key}: ${Array.isArray(value[key]) ? (value[key] as unknown[]).join('/') : typeof value[key] === 'object' ? JSON.stringify(value[key]) : String(value[key])}`)
  return parts.length ? parts.slice(0, 4).join(' · ') : '暂无'
}

function fileKey(file: File) {
  return `${file.name}-${file.size}-${file.lastModified}`
}

function useObjectUrl(file: File) {
  const [url, setUrl] = useState('')
  useEffect(() => {
    const nextUrl = URL.createObjectURL(file)
    setUrl(nextUrl)
    return () => URL.revokeObjectURL(nextUrl)
  }, [file])
  return url
}

function formatBytes(value?: number | null) {
  if (!value) return '未知大小'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(1)} MB`
}

function deleteImpactText(impact?: DeleteImpact | null) {
  if (!impact) return '影响范围暂时无法计算。彻底删除会移除关联表单数据和已登记的存储文件，且不可恢复。'
  const parts: string[] = []
  if (impact.garmentCount) parts.push(`${impact.garmentCount} 套搭配服装`)
  if (impact.garmentImageCount) parts.push(`${impact.garmentImageCount} 张服装图`)
  const taskCount = impact.dependentTaskCount ?? impact.taskCount ?? 0
  if (taskCount) parts.push(`${taskCount} 条生成记录`)
  if (impact.resultImageCount) parts.push(`${impact.resultImageCount} 张结果图记录`)
  if (impact.mediaAssetCount) parts.push(`${impact.mediaAssetCount} 个存储文件`)
  const scope = parts.length ? parts.join('、') : '当前选中项'
  return `彻底删除将移除 ${scope}，预计原始文件 ${formatBytes(impact.estimatedBytes)}。该操作不可恢复。`
}

function dimensionText(dimensions?: { width?: number | null; height?: number | null }) {
  return dimensions?.width && dimensions?.height ? `${dimensions.width} x ${dimensions.height}` : '尺寸读取中'
}

function fileMeta(file: File, dimensions?: { width: number; height: number }) {
  return `${dimensionText(dimensions)} · ${formatBytes(file.size)}`
}

function assetMeta(asset: MediaAsset) {
  return `${dimensionText({ width: asset.originalWidth ?? asset.width, height: asset.originalHeight ?? asset.height })} · ${formatBytes(asset.originalSizeBytes ?? asset.sizeBytes)}`
}

function fileLightboxImage(file: File, src: string, dimensions?: { width: number; height: number }): LightboxImage {
  return {
    src,
    thumbSrc: src,
    title: file.name,
    meta: fileMeta(file, dimensions),
    details: {
      文件名: file.name,
      尺寸: dimensionText(dimensions),
      大小: formatBytes(file.size),
      类型: file.type || '未知类型',
    },
  }
}

function assetLightboxImage(asset: MediaAsset, group?: MediaAsset[]): LightboxImage {
  const items = group?.map((item) => assetLightboxItem(item))
  const src = mediaUrl(assetOriginalSrc(asset))
  return {
    ...assetLightboxItem(asset),
    src,
    downloadUrl: mediaDownloadUrl(assetOriginalSrc(asset)),
    items,
    initialIndex: Math.max(0, group?.findIndex((item) => item.id === asset.id) ?? 0),
  }
}

function assetLightboxItem(asset: MediaAsset): LightboxImageItem {
  const src = mediaUrl(assetOriginalSrc(asset))
  return {
    src,
    thumbSrc: mediaUrl(assetThumbSrc(asset)),
    title: asset.originalFilename ?? asset.filename,
    meta: assetMeta(asset),
    downloadUrl: mediaDownloadUrl(assetOriginalSrc(asset)),
    details: {
      文件名: asset.originalFilename ?? asset.filename,
      尺寸: dimensionText({ width: asset.originalWidth ?? asset.width, height: asset.originalHeight ?? asset.height }),
      大小: formatBytes(asset.originalSizeBytes ?? asset.sizeBytes),
      类型: asset.originalMimeType || asset.mimeType || '未知类型',
    },
  }
}

function assetThumbSrc(asset: MediaAsset) {
  return asset.thumbUrl ?? asset.previewUrl ?? asset.url ?? asset.originalUrl ?? ''
}

function assetPreviewSrc(asset: MediaAsset) {
  return asset.previewUrl ?? asset.thumbUrl ?? asset.url ?? asset.originalUrl ?? ''
}

function assetOriginalSrc(asset: MediaAsset) {
  const direct = asset.originalUrl ?? asset.url ?? asset.previewUrl ?? asset.thumbUrl
  if (!direct) return ''
  return direct.replace(/([?&]variant=)(thumb|preview)(?=&|$)/, '$1original')
}

function assetDisplaySources(asset: MediaAsset, preferred: 'thumb' | 'preview' | 'original') {
  const ordered = preferred === 'preview'
    ? [assetPreviewSrc(asset), assetThumbSrc(asset), assetOriginalSrc(asset)]
    : preferred === 'original'
      ? [assetOriginalSrc(asset), assetPreviewSrc(asset), assetThumbSrc(asset)]
      : [assetThumbSrc(asset), assetPreviewSrc(asset), assetOriginalSrc(asset)]
  return Array.from(new Set(ordered.filter(Boolean)))
}

async function filesFromDataTransfer(dataTransfer: DataTransfer): Promise<File[]> {
  const entries = Array.from(dataTransfer.items)
    .map((item) => (item as DataTransferItem & { webkitGetAsEntry?: () => FileSystemEntry | null }).webkitGetAsEntry?.())
    .filter(Boolean)
  if (!entries.length) return Array.from(dataTransfer.files)
  const files = await Promise.all(entries.map((entry) => readEntry(entry as FileSystemEntry, '')))
  return files.flat()
}

function readEntry(entry: FileSystemEntry, parentPath: string): Promise<File[]> {
  if (entry.isFile) {
    return new Promise((resolve) => {
      ;(entry as FileSystemFileEntry).file((file) => {
        Object.defineProperty(file, 'relativePath', { value: `${parentPath}${file.name}` })
        resolve([file])
      }, () => resolve([]))
    })
  }
  const directory = entry as FileSystemDirectoryEntry
  const reader = directory.createReader()
  return new Promise((resolve) => {
    const allEntries: FileSystemEntry[] = []
    const readBatch = () => {
      reader.readEntries(async (batch) => {
        if (!batch.length) {
          const nested = await Promise.all(allEntries.map((child) => readEntry(child, `${parentPath}${directory.name}/`)))
          resolve(nested.flat())
          return
        }
        allEntries.push(...batch)
        readBatch()
      }, () => resolve([]))
    }
    readBatch()
  })
}

const HEAD_ANGLE_OPTIONS: { value: string; label: string }[] = [
  { value: 'front', label: '正脸' },
  { value: 'left45', label: '左45度' },
  { value: 'right45', label: '右45度' },
  { value: 'left90', label: '左侧脸' },
  { value: 'right90', label: '右侧脸' },
  { value: 'up45', label: '抬头45度' },
  { value: 'down45', label: '低头' },
  { value: 'back', label: '背面' },
  { value: 'backTurnLeft45', label: '背面左回头' },
  { value: 'backTurnRight45', label: '背面右回头' },
]

function angleLabel(angle?: string | null) {
  return HEAD_ANGLE_OPTIONS.find((option) => option.value === angle)?.label ?? '未设置'
}

function fallbackHeadRefs() {
  return ['front', 'left45', 'right45', 'right90', 'backTurnLeft45', 'down45'].map((id) => ({
    id,
    angle: id,
    filename: id,
    previewUrl: null as string | null,
  }))
}

function dateText(value?: string | null) {
  if (!value) return '无'
  return new Date(parseRuyiTime(value)).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function fullDateTimeText(value?: string | null) {
  if (!value) return '无时间记录'
  return new Date(parseRuyiTime(value)).toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', timeZoneName: 'short',
  })
}
