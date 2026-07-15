import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { Dispatch, DragEvent, FormEvent, KeyboardEvent as ReactKeyboardEvent, ReactNode, SetStateAction, UIEvent } from 'react'
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
import { api, finalImageArchiveUrl, isAuthExpiredError, mediaDownloadUrl, mediaUrl } from './api'
import type { AICallRecord, AIOperation, AttemptPhase, CategoryTreeOption, DeleteImpact, FacetOption, FinalImageExportPrecheck, GenerationTask, MediaAsset, ModelProfile, PromptConfig, PromptInvocationOption, PromptPlaceholder, SessionRole, StylingGarment, StylingGarmentFacets, StylingGarmentListResponse } from './api'
import { formalAssetFiles, groupImportFiles, importPath, isSupportedImageFile, sanitizeClientName, SUPPORTED_IMAGE_ACCEPT, timestampName } from './lib/importGrouping'
import type { ImportBatch, ImportIssue } from './lib/importGrouping'
import { friendlyErrorText } from './lib/errorMessages'
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
type DeleteDialogTarget = Pick<GenerationTask, 'id' | 'name'>
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
const densePageSizeOptions = [12, 50, 100] as const
const recentTaskRowHeight = 62
const recentTaskMaxPageSize = 60
const authSeenStorageKey = 'ruyi-auth-seen'
const authExpiredMessage = '登录状态已过期，请重新登录后继续。'
const STYLING_GARMENT_MAX_IMAGES = 15
const HERO_PRODUCT_MAX_IMAGES = 15
const STYLING_GARMENT_SELECTED_IMAGES = 6
const HERO_PRODUCT_SELECTED_IMAGES = 8
const PRODUCT_CODE_MAX_CHARS = 20

function formatImageCount(count: number, limit: number) {
  return `${count}/${limit} 张`
}

export function App() {
  const [checked, setChecked] = useState(false)
  const [authenticated, setAuthenticated] = useState(false)
  const [role, setRole] = useState<SessionRole | null>(null)
  const [page, setPage] = useState<Page>(() => parseRoute().page)
  const [collapsed, setCollapsed] = useState(false)
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
    if (!authenticated) return
    void refreshAll().catch((err) => {
      if (isAuthExpiredError(err)) markAuthExpired()
    })
  }, [authenticated, role])

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
    }
    if (route.page === 'records' && route.recordId && !recordDetail) {
      const detail = await api.getRecord(route.recordId, false).then((r) => r.item).catch(() => null)
      if (detail) setRecordDetail(detail)
    }
  }

  function navigate(next: Page) {
    if (next !== page && document.body.dataset.promptDirty === 'true' && !window.confirm('Prompt 有未保存修改，确定离开并放弃这些修改吗？')) return
    setPage(next)
    if (next === 'studio') setTask(null)
    if (next === 'records') setRecordDetail(null)
    window.history.pushState(null, '', routeToUrl({ page: next }))
  }

  function openTaskInStudio(item: GenerationTask) {
    setPage('studio')
    setTask(item)
    window.history.pushState(null, '', routeToUrl({ page: 'studio', taskId: item.id }))
    void api.getTask(item.id, role === 'admin').then((result) => setTask(result.item)).catch(() => undefined)
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
      // 接口同步失败体是 {detail: "..."}（HTTPException），不含 errorCode；
      // 这里 fallback 到 detail 文案，能拿到 attempt errorCode 的展示点（如各阶段
      // AIOperationCard、成片卡片）会优先用 friendlyError(errorCode) 显示友好文案。
      setError(err instanceof Error ? friendlyErrorText(undefined, err.message) : '操作失败')
      throw err
    } finally {
      setBusy('')
    }
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
    ...(role === 'admin' ? [
      { id: 'prompt-maintenance' as const, label: 'Prompt 维护', icon: FileText },
    ] : []),
    { id: 'records' as const, label: '生成记录', icon: History },
  ]

  return (
    <div className="app-shell">
      <aside className={`sidebar ${collapsed ? 'is-collapsed' : ''}`}>
        <div className="brand-mark">
          <div className="brand-orbit">R</div>
          {!collapsed && <div><strong>Ruyi-Dream</strong><span>AI Photo Studio</span></div>}
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
          <button className="collapse-btn" onClick={() => setCollapsed(!collapsed)} title={collapsed ? '展开侧边栏' : '收起侧边栏'}>
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
        {page === 'studio' && <StudioPage task={task} setTask={setTask} role={role} run={run} localOperations={localOperations} startLocalOperation={startLocalOperation} clearLocalOperationsFor={clearLocalOperationsFor} />}
        {page === 'library' && <LibraryPage garments={garments} run={run} role={role} />}
        {page === 'model' && <ModelPage models={models} role={role} run={run} />}
        {page === 'prompt-maintenance' && role === 'admin' && <PromptMaintenancePage run={run} />}
        {page === 'records' && <RecordsPage records={records} detail={recordDetail} setDetail={setRecordDetail} openTaskInStudio={openTaskInStudio} run={run} />}
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
        <button className="primary wide" disabled={busy}>{busy ? <Loader2 className="spin" size={16} /> : <Shield size={16} />} 登录</button>
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

function StudioPage({ task, setTask, role, run, localOperations, startLocalOperation, clearLocalOperationsFor }: {
  task: GenerationTask | null
  setTask: (task: GenerationTask | null) => void
  role: SessionRole | null
  run: <T>(label: string, action: () => Promise<T>, after?: (value: T) => void, options?: RunOptions) => Promise<T | undefined>
  localOperations: AIOperation[]
  startLocalOperation: (operation: Omit<AIOperation, 'id' | 'status' | 'startedAt'> & { status?: string; startedAt?: string | null }) => AIOperation
  clearLocalOperationsFor: (ownerTypes: string[], taskId?: number | null) => void
}) {
  const [step, setStep] = useState<StudioStep>(0)
  const [taskRailTab, setTaskRailTab] = useState<'summary' | 'recent'>('summary')
  const [recentTaskPageSize, setRecentTaskPageSize] = useState(() => preferredRecentTaskPageSize())
  const [taskItems, setTaskItems] = useState<GenerationTask[]>([])
  const [recentTaskTotal, setRecentTaskTotal] = useState(0)
  const [recentTaskHasMore, setRecentTaskHasMore] = useState(false)
  const [recentTaskNextOffset, setRecentTaskNextOffset] = useState<number | null>(null)
  const [recentTaskLoading, setRecentTaskLoading] = useState(false)
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
  const studioView = task ? studioTaskPresentation(task) : null
  const batchContextMatches = !!task?.batchId && activeBatchIdRef.current === task.batchId
  const visibleBatchItems = batchContextMatches ? batchItems : []
  const batchContextLoading = !!task?.batchId && (!batchContextMatches || batchItemsLoading)
  const isAdmin = role === 'admin'
  function applyTaskUpdate(nextTask: GenerationTask) {
    setTask(nextTask)
    setTaskItems((current) => upsertRecentTaskItem(current, nextTask))
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
    void api.listRecords({ limit: recentTaskPageSize, offset }).then((result) => {
      if (recentTaskRequestRef.current !== requestId) return
      setRecentTaskTotal(result.total)
      setRecentTaskHasMore(result.hasMore)
      setRecentTaskNextOffset(result.nextOffset)
      setTaskItems((current) => {
        if (!append) return result.items
        return mergeRecentTaskItems(current, result.items)
      })
    }).catch(() => undefined).finally(() => {
      if (recentTaskRequestRef.current === requestId) setRecentTaskLoading(false)
    })
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
      setTaskItems((current) => current.map((entry) => entry.id === item.id ? { ...entry, ...r.item } : entry))
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
  const hasActiveRecentTasks = taskItems.some((item) => isTaskRunning(item) || isTaskQueuedForAutoRun(item))
  useEffect(() => {
    if (!hasActiveRecentTasks) return
    const timer = window.setInterval(() => {
      void api.listRecords({ limit: recentTaskPageSize, offset: 0 }).then((result) => {
        setRecentTaskTotal(result.total)
        setTaskItems((current) => {
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
      <div className="studio-grid">
        <div className="panel studio-main">
          <div className="studio-main-top">
            <StepRail active={visibleStep} executing={studioView?.executingStage ?? null} max={currentStep} onSelect={setStep} />
          </div>
          <div className="studio-stage-viewport">
            {visibleStep === 0 && <UploadHeroStep task={task} setTask={applyTaskUpdate} run={run} />}
            {visibleStep === 1 && <StylingStep task={task} setTask={applyTaskUpdate} run={run} userInstruction={aiStylingInstruction} localOperations={localOperations} startLocalOperation={startLocalOperation} clearLocalOperationsFor={clearLocalOperationsFor} isAdmin={isAdmin} onOpenAICallLogs={openAICallLogs} />}
            {visibleStep === 2 && <PreviewStep task={task} setTask={applyTaskUpdate} run={run} userInstruction={storyboardInstruction} localOperations={localOperations} startLocalOperation={startLocalOperation} clearLocalOperationsFor={clearLocalOperationsFor} isAdmin={isAdmin} onOpenAICallLogs={openAICallLogs} />}
            {visibleStep === 3 && <FinalStep task={task} setTask={applyTaskUpdate} run={run} userInstruction={finalImageInstruction} localOperations={localOperations} startLocalOperation={startLocalOperation} clearLocalOperationsFor={clearLocalOperationsFor} isAdmin={isAdmin} onOpenAICallLogs={openAICallLogs} />}
          </div>
        </div>
        <aside className="panel task-rail">
          <div className="task-rail-tabs" role="tablist" aria-label="任务侧栏">
            <button id="task-rail-summary-tab" role="tab" aria-selected={taskRailTab === 'summary'} aria-controls="task-summary-panel" tabIndex={taskRailTab === 'summary' ? 0 : -1} className={taskRailTab === 'summary' ? 'active' : ''} onClick={() => setTaskRailTab('summary')} onKeyDown={handleTaskRailTabKeyDown}>任务摘要</button>
            <button id="task-rail-recent-tab" role="tab" aria-selected={taskRailTab === 'recent'} aria-controls="recent-tasks-panel" tabIndex={taskRailTab === 'recent' ? 0 : -1} className={taskRailTab === 'recent' ? 'active' : ''} onClick={() => setTaskRailTab('recent')} onKeyDown={handleTaskRailTabKeyDown}>最近任务 <span>{recentTaskTotal}</span></button>
          </div>
          <div id="task-summary-panel" role="tabpanel" aria-labelledby="task-rail-summary-tab" className="task-rail-panel task-summary-panel" hidden={taskRailTab !== 'summary'}>
            {task ? (
              <>
                <div className="task-summary-identity">
                  <AssetThumb asset={taskCoverAsset(task)} interactive={false} />
                  <div><strong>{task.name}</strong><span>{studioView?.statusLabel ?? statusText(task.status)}</span><em>主推款 {task.images?.length ?? 0} 张</em></div>
                </div>
                <div className="task-summary-progress">
                  <Meta label="当前步骤" value={studioView?.currentStageLabel ?? '上传主推款'} />
                  <Meta label="总体进度" value={studioView?.progressLabel ?? '等待开始'} />
                  <Meta label="最终成片" value={`${task.resultSet?.images?.filter((i) => i.status === 'SUCCEEDED').length ?? 0}/5`} />
                  {!!task.styling?.selectedMaterials?.length && <Meta label="搭配服装" value={`${task.styling.selectedMaterials.length} 件`} />}
                </div>
                <div className="task-action-row">
                  {isTaskRunning(task) ? (
                    <button className="management-action" onClick={cancelCurrentTask}><CircleStop size={15} />取消生成</button>
                  ) : isTaskQueuedForAutoRun(task) ? (
                    <button className="management-action" onClick={() => cancelAutoRunQueue(task)}><CircleStop size={15} />取消排队</button>
                  ) : (canStartOneClickFinal(task) || canRestartOneClickFinal(task)) ? (
                    <button className="task-action" onClick={startOneClickFinal}><Sparkles size={15} />{oneClickFinalLabel(task)}</button>
                  ) : null}
                </div>
                {visibleStep > 0 && (
                  <StageUserInputModule
                    step={visibleStep}
                    task={task}
                    value={stageInputValue(visibleStep, { aiStylingInstruction, storyboardInstruction, finalImageInstruction })}
                    savedValue={savedStageInputValue(visibleStep, task)}
                    onChange={(value) => updateStageInput(visibleStep, value, { setAiStylingInstruction, setStoryboardInstruction, setFinalImageInstruction })}
                    onSave={() => void run('保存用户输入中...', () => api.updateTask(task.id, stageInputSavePayload(visibleStep, stageInputValue(visibleStep, { aiStylingInstruction, storyboardInstruction, finalImageInstruction }))), (r: { item: GenerationTask }) => applyTaskUpdate(r.item), { refresh: 'background' })}
                    disabled={isTaskRunning(task) || !!task.autoRunTarget}
                  />
                )}
              </>
            ) : (
              <EmptyState title="暂无任务" text="上传主推款后会在这里形成拍摄任务。" />
            )}
          </div>
          <div id="recent-tasks-panel" role="tabpanel" aria-labelledby="task-rail-recent-tab" className="task-rail-panel recent-tasks-panel" hidden={taskRailTab !== 'recent'}>
            <div className="mini-list recent-task-list" onScroll={handleRecentTaskScroll}>
              {taskItems.map((item) => (
                <div key={item.id} className={`mini-row view-only ${task?.id === item.id ? 'active' : ''} ${item.archivedAt ? 'is-archived' : ''}`}>
                  <button onClick={() => openRecentTask(item)} title="恢复拍摄任务">
                    <AssetThumb asset={taskCoverAsset(item)} interactive={false} />
                    <span className="recent-task-copy"><strong>{item.name}</strong><span className="recent-task-meta"><em>{item.archivedAt ? '已归档' : taskListStatusLabel(item)}</em><time dateTime={item.createdAt}>{dateText(item.createdAt)}</time></span></span>
                  </button>
                </div>
              ))}
              {!taskItems.length && <EmptyState title="暂无任务" text="创建拍摄任务后会出现在这里。" />}
              {!!taskItems.length && (
                <div className="recent-task-footer" role="status" aria-live="polite">
                  <span>{recentTaskLoading ? '正在加载更多...' : recentTaskHasMore ? `已显示 ${taskItems.length} / 共 ${recentTaskTotal} 条` : `已触底，共 ${recentTaskTotal} 条`}</span>
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

function mergeRecentTaskItems(current: GenerationTask[], incoming: GenerationTask[]) {
  const seen = new Set<number>()
  return [...current, ...incoming].filter((item) => {
    if (seen.has(item.id)) return false
    seen.add(item.id)
    return true
  })
}

function taskCoverAsset(task: GenerationTask) {
  return task.summary?.cover ?? task.images?.[0]?.asset ?? null
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
  return (
    <div className="panel batch-bar">
      <div className="batch-bar-main">
        <span className="batch-bar-title">{loading ? '正在读取批次...' : `本批次 ${items.length} 个任务`}</span>
        <div className="batch-bar-chips">
          {items.map((item) => (
            <button
              key={item.id}
              className={`batch-chip ${batchChipTone(item)} ${item.id === currentTaskId ? 'active' : ''}`}
              onClick={() => onOpenTask(item)}
              title={`切换到 ${item.name}`}
            >
              <strong>{item.name}</strong>
              <span>{batchChipStatus(item)}</span>
            </button>
          ))}
        </div>
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
        所属批次 · {loading ? '读取中' : currentIndex >= 0 ? `第 ${currentIndex + 1}/${items.length} 个任务` : `共 ${items.length} 个任务`}
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
  files: File[]
  issues?: ImportIssue[]
  error?: string
}

function UploadHeroStep({ task, setTask, run }: { task: GenerationTask | null; setTask: (task: GenerationTask) => void; run: any }) {
  const [files, setFiles] = useState<File[]>([])
  const [name, setName] = useState('')
  const [singleIssues, setSingleIssues] = useState<ImportIssue[]>([])
  const [batchGroups, setBatchGroups] = useState<PendingBatchGroup[]>([])
  const [batchCreating, setBatchCreating] = useState<{ current: number; total: number } | null>(null)
  const [batchError, setBatchError] = useState('')
  const [existingTaskNames, setExistingTaskNames] = useState<Set<string> | null>(null)
  const batchIdRef = useRef('')
  const firstCreatedTaskRef = useRef<GenerationTask | null>(null)

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
    setSingleIssues(issues)
  }

  function pendingGroupFromSingle(): PendingBatchGroup | null {
    if (!files.length) return null
    return {
      id: `single-${Date.now()}`,
      name: sanitizeClientName(name) || timestampName(),
      files,
      issues: singleIssues,
    }
  }

  function pendingGroupsFromImports(groups: ImportBatch[]): PendingBatchGroup[] {
    return groups.map((group, index) => ({
      id: `${group.id}-${Date.now()}-${index}`,
      name: group.code,
      files: group.files,
      issues: group.issues,
    }))
  }

  function addHeroFiles(nextFiles: File[]) {
    const imageFiles = nextFiles
      .filter(isSupportedImageFile)
      .sort((a, b) => importPath(a).localeCompare(importPath(b), 'zh-CN', { numeric: true }))
    const folderFiles = nextFiles.filter((file) => importPath(file).includes('/'))
    const grouping = groupImportFiles(folderFiles.length ? folderFiles : nextFiles)
    const groups = grouping.batches
    if (groups.length > 1 || batchGroups.length > 0) {
      const existingSingle = pendingGroupFromSingle()
      setBatchGroups((current) => [
        ...current,
        ...(current.length ? [] : existingSingle ? [existingSingle] : []),
        ...pendingGroupsFromImports(groups),
      ])
      setBatchError('')
      setFiles([])
      setName('')
      setSingleIssues([])
      batchIdRef.current = ''
      firstCreatedTaskRef.current = null
      return
    }
    setBatchGroups([])
    setBatchError('')
    const group = groups[0]
    if (files.length > 0) {
      const addedFiles = group && folderFiles.length ? group.files : imageFiles
      applySingleFiles([...files, ...addedFiles], name, [...singleIssues, ...(group?.issues ?? [])])
      return
    }
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
    const created: GenerationTask[] = []
    let lastError = ''
    let failedCount = 0
    for (let index = 0; index < ready.length; index += 1) {
      const group = ready[index]
      setBatchCreating({ current: index + 1, total: ready.length })
      try {
        const uploadFiles = formalAssetFiles(group.files, HERO_PRODUCT_MAX_IMAGES)
        const result = await api.createTask(group.name, uploadFiles, batchIdRef.current)
        created.push(result.item)
        if (!firstCreatedTaskRef.current) firstCreatedTaskRef.current = result.item
        setBatchGroups((current) => current.filter((item) => item.id !== group.id))
      } catch (err) {
        lastError = err instanceof Error ? err.message : '创建任务失败'
        failedCount += 1
        setBatchGroups((current) => current.map((item) => item.id === group.id ? { ...item, error: lastError } : item))
      }
    }
    setBatchCreating(null)
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
    const droppedFiles = await filesFromDataTransfer(event.dataTransfer)
    addHeroFiles(droppedFiles)
  }

  return (
    <div className="step-panel">
      <div className="step-scroll-content">
        <div className="step-copy"><h2>上传主推款</h2><p>支持多张图片、一个货号文件夹或一个上级文件夹。每套最多 15 张进入候选池，AI 智能选搭后保留挑选排序好的 8 张主推款参考图。</p></div>
        {task?.status === 'DRAFT' && <HeroDraftEditor task={task} setTask={setTask} run={run} />}
        {!task && (
          <>
            <div className="hero-upload" onDragOver={(event) => event.preventDefault()} onDrop={(event) => void handleDrop(event)}>
              <div>
                <Images size={22} />
                <strong>拖入主推款图片或文件夹</strong>
                <span>{files.length ? `已选择 ${files.length} 张，可拖动排序` : '支持多张图片、一个文件夹，或多个货号文件夹（批量成片）'}</span>
              </div>
              <div className="import-actions">
                <label className="management-action">
                  <ImagePlus size={16} /> 选择图片
                  <input type="file" accept={SUPPORTED_IMAGE_ACCEPT} multiple onChange={(event) => addHeroFiles(Array.from(event.target.files ?? []))} />
                </label>
                <label className="management-action">
                  <FolderOpen size={16} /> 选择文件夹
                  <input type="file" multiple {...{ webkitdirectory: '', directory: '' }} onChange={(event) => addHeroFiles(Array.from(event.target.files ?? []))} />
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
                    <strong className="batch-detect-title">检测到 {batchGroups.length} 个货号文件夹</strong>
                    <span className="count-pill">{batchGroups.reduce((sum, group) => sum + group.files.length, 0)} 张图</span>
                  </div>
                  <div className="batch-note">
                    <span>每个文件夹对应一个拍摄任务，可在下方逐个调整任务名和图片；创建后可从顶部批次条一键批量成片。</span>
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
                                aria-label="任务名称"
                                disabled={!!batchCreating}
                              />
                            </label>
                            <span className="batch-form-spacer" />
                            <span className={`count-pill ${overLimit ? 'is-over' : ''}`}>{formatImageCount(group.files.length, HERO_PRODUCT_MAX_IMAGES)}</span>
                            <button className="management-action icon-button" onClick={() => setBatchGroups((current) => current.filter((item) => item.id !== group.id))} title="移除该任务" disabled={!!batchCreating}><Trash2 size={16} /></button>
                          </div>
                            <div className="batch-note">
                              <span>推荐 3-5 张，排序越靠前越重要。{existsHint ? ' 与已有任务重名，将共存（任务名不要求唯一）。' : ''}</span>
                              {hasBlockingError && <strong>{group.error ?? (!group.files.length ? '没有可用图片' : '本批次任务名重复')}</strong>}
                              {overLimit && <strong>{`超出 ${HERO_PRODUCT_MAX_IMAGES} 张，红框图片不会进入后续识别`}</strong>}
                              {group.issues?.map((issue) => <strong key={`${issue.code}-${issue.path ?? issue.count ?? issue.message}`}>{issue.message}</strong>)}
                          </div>
                          <FileThumbList files={group.files} onChange={(nextFiles) => updateBatchGroup(group.id, { files: nextFiles })} limit={HERO_PRODUCT_MAX_IMAGES} />
                        </div>
                      )
                    })}
                  </div>
                  {batchError && <span className="inline-error">{batchError}</span>}
                  {batchCreating ? (
                    <p className="edit-hint">正在创建第 {batchCreating.current}/{batchCreating.total} 个任务，请勿刷新或离开页面...</p>
                  ) : (
                    <div className="inline-form">
                      <button className="management-action" onClick={() => void createBatchTasks()}>
                        <ImagePlus size={16} /> 批量创建 {batchGroups.length} 个任务
                      </button>
                      <button className="management-action" onClick={() => setBatchGroups([])}>取消</button>
                    </div>
                  )}
                </div>
              )
            })()}
            {files.length > 0 && (
              <div className="upload-review">
                <div className="batch-card-head">
                  <div className="batch-icon"><Layers3 size={18} /></div>
                  <input value={name} maxLength={PRODUCT_CODE_MAX_CHARS} onChange={(e) => setName(e.target.value.slice(0, PRODUCT_CODE_MAX_CHARS))} placeholder="任务名称" aria-label="任务名称" />
                  <span className={`count-pill ${files.length > HERO_PRODUCT_MAX_IMAGES ? 'is-over' : ''}`}>{formatImageCount(files.length, HERO_PRODUCT_MAX_IMAGES)}</span>
                </div>
                  <div className="batch-note">
                    <span>{`最多 ${HERO_PRODUCT_MAX_IMAGES} 张进入候选池，AI 智能选搭后保留 ${HERO_PRODUCT_SELECTED_IMAGES} 张参考图。`}</span>
                    {files.length > HERO_PRODUCT_MAX_IMAGES && <strong>{`当前超过 ${HERO_PRODUCT_MAX_IMAGES} 张，红框图片不会进入后续识别`}</strong>}
                    {singleIssues.map((issue) => <strong key={`${issue.code}-${issue.path ?? issue.count ?? issue.message}`}>{issue.message}</strong>)}
                  </div>
                <FileThumbList files={files} onChange={setFiles} limit={HERO_PRODUCT_MAX_IMAGES} />
                <div className="inline-form">
                    <button className="management-action" disabled={!files.length || !sanitizeClientName(name)} onClick={() => void run('创建拍摄任务中...', () => api.createTask(name, formalAssetFiles(files, HERO_PRODUCT_MAX_IMAGES)), (r: { item: GenerationTask }) => {
                    setTask(r.item)
                    setFiles([])
                    setName('')
                    window.history.pushState(null, '', routeToUrl({ page: 'studio', taskId: r.item.id }))
                  }, { refresh: 'background' })}>
                    <ImagePlus size={16} /> 创建任务
                  </button>
                </div>
              </div>
            )}
          </>
        )}
        {task && task.status !== 'DRAFT' && <HeroDraftEditor task={task} setTask={setTask} run={run} />}
      </div>
    </div>
  )
}

function HeroDraftEditor({ task, setTask, run }: { task: GenerationTask; setTask: (task: GenerationTask) => void; run: any }) {
  const [name, setName] = useState(task.name)
  const [viewer, setViewer] = useState<LightboxImage | null>(null)
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const imageCount = task.images?.length ?? 0
  const imageLimit = task.status === 'DRAFT' ? HERO_PRODUCT_MAX_IMAGES : HERO_PRODUCT_SELECTED_IMAGES

  useEffect(() => setName(task.name), [task.id, task.name])

  async function moveTo(from: number, to: number) {
    const images = task.images ?? []
    if (from === to || from < 0 || to < 0 || from >= images.length || to >= images.length) return
    const next = [...images]
    const [item] = next.splice(from, 1)
    next.splice(to, 0, item)
    const sortOrders = images.map((image) => image.sortOrder)
    await run('调整主推款顺序中...', async () => {
      const results = await Promise.all(next.map((image, index) => api.updateTaskImage(image.id, sortOrders[index])))
      return results[results.length - 1]
    }, (result: { item: GenerationTask }) => setTask(result.item), { refresh: 'background' })
  }

  return (
    <div className="hero-draft">
      <div className="section-head"><strong>主推款图片</strong><span className={`count-pill ${imageCount > imageLimit ? 'is-over' : ''}`}>{formatImageCount(imageCount, imageLimit)}</span><span className="status draft">{task.status === 'DRAFT' ? '待选搭' : '可调整'}</span></div>
      <div className="inline-form">
          <input value={name} maxLength={PRODUCT_CODE_MAX_CHARS} onChange={(event) => setName(event.target.value.slice(0, PRODUCT_CODE_MAX_CHARS))} placeholder="任务名称" />
          <button className="management-action" disabled={name.trim() === task.name} onClick={() => void run('保存任务名称中...', () => api.updateTask(task.id, { name }), (r: { item: GenerationTask }) => setTask(r.item), { refresh: 'background' })}>保存名称</button>
      </div>
      <div className="draft-image-grid">
        {(task.images ?? []).map((image, index) => (
          <div
            className="saved-image-card"
            key={image.id}
            draggable={(task.images?.length ?? 0) > 1}
            onDragStart={() => setDragIndex(index)}
            onDragOver={(event) => event.preventDefault()}
            onDrop={() => {
              if (dragIndex !== null) void moveTo(dragIndex, index)
              setDragIndex(null)
            }}
          >
            <button className="image-open" onClick={() => setViewer(assetLightboxImage(image.asset, (task.images ?? []).map((item) => item.asset)))} title="放大查看">
              <img src={mediaUrl(assetThumbSrc(image.asset))} alt={image.asset.filename} loading="lazy" />
            </button>
            <span>{image.asset.filename}<small>{assetMeta(image.asset)}</small></span>
            <div className="thumb-actions">
              <label title="替换图片">
                <RefreshCw size={14} />
                <input type="file" accept={SUPPORTED_IMAGE_ACCEPT} onChange={(event) => {
                  const replacement = event.target.files?.[0]
                  if (replacement) void run('替换主推款图片中...', () => api.replaceTaskImage(image.id, replacement), (r: { item: GenerationTask }) => setTask(r.item), { refresh: 'background' })
                }} />
              </label>
              <button disabled={(task.images?.length ?? 0) <= 1} onClick={() => void run('移除主推款图片中...', () => api.deleteTaskImage(image.id), (r: { item: GenerationTask }) => setTask(r.item), { refresh: 'background' })} title="删除图片"><Trash2 size={14} /></button>
            </div>
          </div>
        ))}
      </div>
      <p className="edit-hint">{task.status === 'DRAFT' ? `最多 ${HERO_PRODUCT_MAX_IMAGES} 张进入候选池；AI 智能选搭后保留 ${HERO_PRODUCT_SELECTED_IMAGES} 张主推款参考图。` : `当前展示 AI 选搭后保留的主推款参考图，最多 ${HERO_PRODUCT_SELECTED_IMAGES} 张；调整图片会让当前任务回到待选搭状态。`}</p>
      {viewer && <ImageLightbox image={viewer} onClose={() => setViewer(null)} />}
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
    { title: '成片生成要求', placeholder: '例如：整体更干净，减少配饰存在感，保持棚拍电商质感。' },
  ][step]
  const saveDisabled = disabled || !task || value.trim() === savedValue.trim()
  const hasValue = Boolean(value.trim())
  const [expanded, setExpanded] = useState(!task || hasValue)
  return (
    <details className="side-input-module" open={expanded} onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary><span>{labels.title}</span><em>{hasValue ? '已填写' : '选填'}</em></summary>
      <div className="side-input-control">
        <textarea value={value} maxLength={800} disabled={disabled} onChange={(event) => onChange(event.target.value)} placeholder={labels.placeholder} />
        {task && <p>保存后用于下一次执行，不会改变当前结果。</p>}
        <div className="side-input-actions">
          <em>{value.length}/800</em>
          {task && <button className="management-action" type="button" disabled={saveDisabled} onClick={onSave}>保存</button>}
        </div>
      </div>
    </details>
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
  const canSelect = !!task && !locked && ['READY_FOR_STYLING', 'STYLING_READY', 'PREVIEW_READY', 'COMPLETED', 'FINAL_PARTIAL', 'FAILED', 'CANCELLED'].includes(task.status)
  const hasStyling = !!task?.styling
  const operations = task ? operationFallbacks(task, localOperations, 'generation_styling') : []
  return (
    <div className="step-panel">
      <div className="step-scroll-content">
        <div className="step-copy"><h2>AI 智能选搭</h2><p>理解主推款与商品卖点，评估多套造型方向，并选择最能突出主推款的搭配方案。</p></div>
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
  const requirementText = stringValue(requirement.instruction ?? requirement.combined_requirement)
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
        {styling.selectedMaterials.length ? styling.selectedMaterials.map((item) => <GarmentCard key={item.id} item={item} />) : <EmptyState title="无需库内搭配服装" text="AI Stylist 判断本次以主推款单穿或生成配饰辅助更合适。" />}
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
  const text = Number.isFinite(number) ? `${Math.round(number * 100)}` : '-'
  return <span><b>{text}</b>{label}</span>
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
        <div className="step-copy"><h2>生成分镜预览</h2><p>按主推款类型与选搭方案生成 3:2 六格分镜；前 5 格用于生成成片，第 6 格仅作备用参考。</p></div>
        <div className="stage-priority-content">{['result', 'status'].map((section) => section === 'status' ? (
          <div className="stage-operation-status" key="status"><AIOperationList operations={operations} taskId={task?.id} ownerType="generation_preview" onOpenTechnicalDetails={isAdmin ? (operation) => onOpenAICallLogs(`生成分镜预览 · Attempt #${operation.id}`, ['generation_preview'], operation.id) : undefined} /></div>
        ) : (
          <div className="stage-business-result" key="result">{preview?.asset ? (
            <div className="storyboard-review"><button className="preview-board-open" onClick={() => preview.asset && setViewer(assetLightboxImage(preview.asset))} title="放大查看"><img className="preview-board" src={mediaUrl(assetPreviewSrc(preview.asset))} loading="lazy" alt="分镜预览" /></button><div className="storyboard-legend"><span><strong>1–5</strong>生成成片</span><span><strong>6</strong>备用参考，不生成</span><em>点击图片放大检查姿势、构图与光线</em></div></div>
          ) : <EmptyState title="尚未生成分镜预览" text="点击生成预览后，系统会根据主推款品类与选搭方案输出一张 3:2 横版分镜预览图。" />}</div>
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
          <GalleryVerticalEnd size={16} /> {hasPreview ? '重新生成分镜预览' : '生成预览'}
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
        <div className="step-copy"><h2>生成成片</h2><p>并发生成分镜前 5 个镜头；可逐张检查、下载或重新生成，也可打包下载全部成片。</p></div>
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
              <div><span><b>#{index + 1}</b>{image?.shotRole || '镜头'}</span>{image?.asset && <a className="management-icon-action" href={mediaDownloadUrl(assetOriginalSrc(image.asset))} aria-label={`下载镜头 ${index + 1}`}><Download size={15} /></a>}{image?.status === 'FAILED' && image.activeAttempt?.errorCode === 'DELIVERY_FAILED' && <button className="management-icon-action" disabled={!!task && isTaskLockedForGeneration(task)} onClick={() => void run('重新回传成片中...', () => api.redeliverResultImage(image.id), (r: { item: GenerationTask }) => setTask(r.item), { refresh: 'background' })} title="重新回传（图已生成，仅重新拉取，不消耗生成额度）" aria-label={`重新回传镜头 ${index + 1}`}><RefreshCw size={14} /></button>}{image && !['QUEUED', 'RUNNING'].includes(image.status) && <button className="task-icon-action" disabled={!!task && isTaskLockedForGeneration(task)} onClick={() => void run('单张重新生成中...', () => api.retryResultImage(image.id, userInstruction), (r: { item: GenerationTask }) => setTask(r.item), { refresh: 'background' })} title="重新生成" aria-label={`重新生成镜头 ${index + 1}`}><RotateCcw size={14} /></button>}</div>
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

function LibraryPage({ garments, run, role }: { garments: StylingGarment[]; run: any; role: SessionRole | null }) {
  const libraryResultsRef = useRef<HTMLDivElement | null>(null)
  const autoPageSize = useAutoGarmentPageSize(libraryResultsRef)
  const [query, setQuery] = useState('')
  const [detail, setDetail] = useState<StylingGarment | null>(null)
  const [items, setItems] = useState<StylingGarment[]>([])
  const [showImport, setShowImport] = useState(false)
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
  const [openFilter, setOpenFilter] = useState<'category' | 'date' | 'sort' | 'status' | null>(null)
  const [pageSizeMode, setPageSizeMode] = useState<'auto' | 'fixed'>('auto')
  const [pageSize, setPageSize] = useState(() => estimateAutoGarmentPageSize())
  const [searching, setSearching] = useState(true)
  const [includeArchived, setIncludeArchived] = useState(false)
  const [bulkMode, setBulkMode] = useState(false)
  const [selectedGarmentIds, setSelectedGarmentIds] = useState<Set<number>>(() => new Set())
  const [deleteGarmentDialog, setDeleteGarmentDialog] = useState<StylingGarment[] | null>(null)
  const [manualTagDialog, setManualTagDialog] = useState<StylingGarment[] | null>(null)
  const [deleteGarmentImpact, setDeleteGarmentImpact] = useState<DeleteImpact | null>(null)
  const [deleteGarmentImpactLoading, setDeleteGarmentImpactLoading] = useState(false)
  const selectableGarmentIds = items.filter((item) => item.status !== 'TAGGING_RUNNING').map((item) => item.id)
  const selectedVisibleGarments = items.filter((item) => selectedGarmentIds.has(item.id) && selectableGarmentIds.includes(item.id))
  const selectedArchivedGarments = selectedVisibleGarments.filter((item) => item.archivedAt)
  const selectedActiveGarments = selectedVisibleGarments.filter((item) => !item.archivedAt)
  const allVisibleGarmentsSelected = selectableGarmentIds.length > 0 && selectableGarmentIds.every((id) => selectedGarmentIds.has(id))

  async function openDetail(item: StylingGarment) {
    const result = await api.getStylingGarment(item.id)
    updateGarmentInView(result.item)
    setDetail(result.item)
  }

  function libraryQueryOptions(targetOffset = offset) {
    const keyword = query.trim()
    return {
      limit: pageSize,
      offset: targetOffset,
      keyword: keyword || undefined,
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
      if (!result.items.length && result.total > 0 && targetOffset > 0) {
        const fallbackOffset = Math.max(0, Math.floor((result.total - 1) / pageSize) * pageSize)
        setOffset(fallbackOffset)
        return api.listStylingGarments(libraryQueryOptions(fallbackOffset)).then(applyLibraryResult)
      }
      applyLibraryResult(result)
    })
  }

  function applyLibraryResult(result: StylingGarmentListResponse) {
    setItems(result.items)
    setTotal(result.total)
    setTotalPages(result.totalPages)
    setFacets(result.facets ?? {})
    setStableFacets(result.stableFacets ?? result.facets ?? {})
  }

  function refreshFirstLibraryPage() {
    setOffset(0)
    return refreshLibraryPage(0)
  }

  useEffect(() => {
    setOffset(0)
  }, [query, includeArchived, statusFilter, primaryCategoryFilter, secondaryCategoryFilter, uploadedFrom, uploadedTo, sortBy, sortDir])

  useEffect(() => {
    if (pageSizeMode !== 'auto' || pageSize === autoPageSize) return
    setPageSize(autoPageSize)
    setOffset(0)
  }, [autoPageSize, pageSize, pageSizeMode])

  useEffect(() => {
    let cancelled = false
    setSearching(true)
    const timer = window.setTimeout(() => {
      void api.listStylingGarments(libraryQueryOptions(offset)).then((result) => {
        if (!cancelled) {
          setItems(result.items)
          setTotal(result.total)
          setTotalPages(result.totalPages)
          setFacets(result.facets ?? {})
          setStableFacets(result.stableFacets ?? result.facets ?? {})
        }
      }).catch(() => undefined).finally(() => {
        if (!cancelled) setSearching(false)
      })
    }, 250)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [offset, query, includeArchived, statusFilter, primaryCategoryFilter, secondaryCategoryFilter, uploadedFrom, uploadedTo, sortBy, sortDir, pageSize])

  useEffect(() => {
    setItems((current) => current.map((item) => garments.find((latest) => latest.id === item.id) ?? item))
  }, [garments])

  useEffect(() => setSelectedGarmentIds(new Set()), [offset, includeArchived, query, statusFilter, primaryCategoryFilter, secondaryCategoryFilter, uploadedFrom, uploadedTo, sortBy, sortDir, pageSize])

  useEffect(() => {
    if (!detail) return
    const latest = garments.find((item) => item.id === detail.id)
    if (latest && (latest.status !== detail.status || latest.code !== detail.code || latest.updatedAt !== detail.updatedAt)) void openDetail(latest)
  }, [garments, detail?.id, detail?.status, detail?.code, detail?.updatedAt])

  function toggleGarmentSelection(id: number, checked: boolean) {
    setSelectedGarmentIds((current) => {
      const next = new Set(current)
      if (checked) next.add(id)
      else next.delete(id)
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
    void run(purge ? '彻底删除搭配服装中...' : '归档搭配服装中...', action, () => {
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

  function updateGarmentInView(next: StylingGarment) {
    setItems((current) => current.map((item) => item.id === next.id ? next : item))
    if (detail?.id === next.id) setDetail(next)
  }

  function updateGarmentsInView(nextItems: StylingGarment[]) {
    const byId = new Map(nextItems.map((item) => [item.id, item]))
    setItems((current) => current.map((item) => byId.get(item.id) ?? item))
    if (detail && byId.has(detail.id)) setDetail(byId.get(detail.id) ?? detail)
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
  }

  const categoryFilterCount = primaryCategoryFilter.length + secondaryCategoryFilter.length
  const hasActiveFilters = Boolean(query.trim() || statusFilter || categoryFilterCount || uploadedFrom || uploadedTo || includeArchived || sortBy !== 'createdAt' || sortDir !== 'desc')
  const stableCategoryTree = stableFacets.categoryTree ?? facets.categoryTree ?? []

  return (
    <section className="page workbench-page library-page">
      <div className="library-title-row">
        <PageTitle title="搭配服装库" />
        <button className="management-action" onClick={() => setShowImport((value) => !value)}>
          <ImagePlus size={16} /> {showImport ? '收起导入' : '导入搭配服装'}
        </button>
      </div>
      {showImport && <BatchImportPanel onImported={() => void refreshFirstLibraryPage().catch(() => undefined)} />}
      <div className="panel library-panel">
        <div className="library-commandbar">
          <div className={`search ${searching ? 'is-searching' : ''}`}>
            <Search size={16} />
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索货号 / 标签" />
            {searching && <span className="search-hint"><Loader2 size={14} className="spin" />搜索中</span>}
          </div>
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
              onChange={(nextSortBy, nextSortDir) => {
                setSortBy(nextSortBy)
                setSortDir(nextSortDir)
              }}
            />
            <StatusFilter
              open={openFilter === 'status'}
              onOpen={(open) => setOpenFilter(open ? 'status' : null)}
              value={statusFilter}
              options={mergedFacetOptions(facets.status, statusFilter ? [statusFilter] : [])}
              onChange={setStatusFilter}
            />
            <button className={`filter-pill checkbox-pill ${includeArchived ? 'is-active' : ''}`} type="button" onClick={() => {
              setOffset(0)
              setIncludeArchived(!includeArchived)
            }}><Check size={15} /><span>查看已归档</span></button>
            {hasActiveFilters && <button className="filter-pill reset-filter-pill" type="button" onClick={clearLibraryFilters}><span>清空筛选</span></button>}
          </div>
          <div className="library-management-tools">
            {bulkMode && <label className="quiet-check"><input type="checkbox" checked={allVisibleGarmentsSelected} disabled={!selectableGarmentIds.length} onChange={(event) => toggleVisibleGarmentSelection(event.target.checked)} /> 全选本页</label>}
            {selectedVisibleGarments.length > 0 && (
              selectedArchivedGarments.length === selectedVisibleGarments.length
                ? <button className="management-action" onClick={() => restoreGarments(selectedArchivedGarments)}>恢复 {selectedArchivedGarments.length} 项</button>
                : <button className="management-action" disabled={selectedArchivedGarments.length > 0} title={selectedArchivedGarments.length ? '请分别选择已归档和未归档服装' : undefined} onClick={() => openGarmentDeleteDialog(selectedActiveGarments)}>归档 {selectedActiveGarments.length} 项</button>
            )}
            {role === 'admin' && selectedVisibleGarments.length > 0 && <button className="management-action" onClick={() => setManualTagDialog(selectedVisibleGarments)}>批量重新打标</button>}
            <button className="management-action compact-control" onClick={() => { setBulkMode((value) => !value); setSelectedGarmentIds(new Set()) }}>{bulkMode ? '退出批量' : '批量管理'}</button>
          </div>
        </div>
        <div ref={libraryResultsRef} className={`library-results ${searching ? 'is-loading' : ''}`}>
        {items.length ? (
          <div className="garment-grid">{items.map((item) => (
            <GarmentCard
              key={item.id}
              item={item}
              onOpen={() => void openDetail(item)}
              onTag={['DRAFT', 'TAGGING_FAILED', 'TAGGING_READY'].includes(item.status) ? () => void run('重新识别标签中...', () => api.tagStylingGarment(item.id), (r: { item: StylingGarment }) => updateGarmentInView(r.item), { refresh: 'background' }) : undefined}
              selected={bulkMode && selectedGarmentIds.has(item.id)}
              onSelect={bulkMode ? (checked) => toggleGarmentSelection(item.id, checked) : undefined}
              onDelete={() => openGarmentDeleteDialog([item])}
              onRestore={() => restoreGarments([item])}
            />
          ))}</div>
        ) : searching ? null : <EmptyState title={hasActiveFilters ? '没有匹配的搭配服装' : '暂无搭配服装'} text={hasActiveFilters ? '减少筛选条件或换一个货号关键词试试。' : '点击右上角导入搭配服装。'} />}
        </div>
        <div className="library-pagination-stick">
          <PaginationControls
            disabled={searching}
            offset={offset}
            pageSize={pageSize}
            pageSizeOptions={[{ value: autoPageSize, label: `${autoPageSize} 套`, key: 'auto' }, 50, 100]}
            pageSizeOptionKey={pageSizeMode === 'auto' ? 'auto' : String(pageSize)}
            pageSizeUnit="套"
            total={total}
            totalPages={totalPages}
            onOffsetChange={setOffset}
            onPageSizeChange={(value, optionKey) => {
              setPageSizeMode(optionKey === 'auto' ? 'auto' : 'fixed')
              setPageSize(value)
              setOffset(0)
            }}
          />
        </div>
      </div>
      {detail && <GarmentDetailDrawer item={detail} run={run} isAdmin={role === 'admin'} onClose={() => setDetail(null)} onChange={updateGarmentInView} onDelete={() => openGarmentDeleteDialog([detail])} onRestore={() => restoreGarments([detail])} />}
      {manualTagDialog && (
        <BatchManualTagsDialog
          items={manualTagDialog}
          run={run}
          onClose={() => setManualTagDialog(null)}
          onSaved={(nextItems) => {
            updateGarmentsInView(nextItems)
            setSelectedGarmentIds(new Set())
            setManualTagDialog(null)
            void refreshLibraryPage().catch(() => undefined)
          }}
        />
      )}
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
  const primary = options.find((item) => item.value === activePrimary) ?? options[0]
  const selectedCount = selectedPrimary.length + selectedSecondary.length
  useOutsideClose(open, wrapRef, () => onOpen(false))

  useEffect(() => {
    if (!open || activePrimary || !options.length) return
    setActivePrimary(options[0].value)
  }, [activePrimary, open, options])

  function togglePrimary(value: string, checked: boolean) {
    onPrimary(checked ? Array.from(new Set([...selectedPrimary, value])) : selectedPrimary.filter((item) => item !== value))
  }

  function toggleSecondary(value: string, checked: boolean) {
    onSecondary(checked ? Array.from(new Set([...selectedSecondary, value])) : selectedSecondary.filter((item) => item !== value))
  }

  const allPrimarySelected = options.length > 0 && options.every((item) => selectedPrimary.includes(item.value))
  const allSecondarySelected = !!primary?.children?.length && primary.children.every((item) => selectedSecondary.includes(item.value))

  return (
    <div className="filter-popover-wrap" ref={wrapRef}>
      <button className={`filter-pill ${selectedCount ? 'is-active' : ''}`} type="button" onClick={() => onOpen(!open)}>
        <span>{selectedCount ? `品类 ${selectedCount}` : '品类'}</span>
        <ChevronDown size={15} />
      </button>
      {open && (
        <div className="filter-popover category-popover">
          <div className="popover-search"><Search size={15} /><span>按一级 / 二级品类筛选</span></div>
          <div className="category-tree">
            <div className="category-column">
              <label className="tree-option">
                <input type="checkbox" checked={allPrimarySelected} disabled={!options.length} onChange={(event) => onPrimary(event.target.checked ? options.map((item) => item.value) : [])} />
                <span>选择全部一级品类</span>
              </label>
              {options.map((option) => (
                <div className={`tree-row ${primary?.value === option.value ? 'is-current' : ''}`} role="button" tabIndex={0} key={option.value} onClick={() => setActivePrimary(option.value)} onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    setActivePrimary(option.value)
                  }
                }}>
                  <label onClick={(event) => event.stopPropagation()}>
                    <input type="checkbox" checked={selectedPrimary.includes(option.value)} onChange={(event) => togglePrimary(option.value, event.target.checked)} />
                    <span>{option.label}</span>
                  </label>
                  <small>{option.count}</small>
                  <ChevronRight size={15} />
                </div>
              ))}
            </div>
            <div className="category-column">
              <label className="tree-option">
                <input type="checkbox" checked={allSecondarySelected} disabled={!primary?.children?.length} onChange={(event) => onSecondary(event.target.checked ? Array.from(new Set([...selectedSecondary, ...(primary?.children ?? []).map((item) => item.value)])) : selectedSecondary.filter((item) => !(primary?.children ?? []).some((child) => child.value === item)))} />
                <span>{primary ? `选择 ${primary.label} 下全部二级品类` : '选择全部二级品类'}</span>
              </label>
              {primary?.children?.length ? primary.children.map((child) => (
                <label className="tree-option" key={child.value}>
                  <input type="checkbox" checked={selectedSecondary.includes(child.value)} onChange={(event) => toggleSecondary(child.value, event.target.checked)} />
                  <span>{child.label}</span>
                  <small>{child.count}</small>
                </label>
              )) : <span className="facet-empty">暂无二级品类</span>}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function DateRangeFilter({ open, onOpen, from, to, onFrom, onTo }: {
  open: boolean
  onOpen: (open: boolean) => void
  from: string
  to: string
  onFrom: (value: string) => void
  onTo: (value: string) => void
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const [calendarMonth, setCalendarMonth] = useState(() => monthStart(from ? parseDateInput(from) ?? new Date() : new Date()))
  const [draftFrom, setDraftFrom] = useState('')
  const [draftTo, setDraftTo] = useState('')
  const pendingRange = draftFrom && !draftTo ? `${draftFrom} 至 ...` : ''
  const label = from && to ? `${from} 至 ${to}` : '上传时间'
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

  function applyQuickRange(days: number) {
    const today = new Date()
    const nextFrom = new Date(today)
    nextFrom.setDate(today.getDate() - Math.max(days - 1, 0))
    onFrom(dateInputValue(nextFrom))
    onTo(dateInputValue(today))
    onOpen(false)
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
      <button className={`filter-pill ${from && to ? 'is-active' : ''}`} type="button" onClick={toggleOpen}>
        <span>{pendingRange || label}</span>
        <ChevronDown size={15} />
      </button>
      {open && (
        <div className="filter-popover date-popover">
          <button type="button" onClick={() => applyQuickRange(7)}>近 7 天</button>
          <button type="button" onClick={() => applyQuickRange(30)}>近 30 天</button>
          <CalendarRangePicker month={calendarMonth} from={draftFrom} to={draftTo} onMonth={setCalendarMonth} onSelect={selectDate} />
        </div>
      )}
    </div>
  )
}

function SortFilter({ open, onOpen, sortBy, sortDir, onChange }: {
  open: boolean
  onOpen: (open: boolean) => void
  sortBy: string
  sortDir: 'asc' | 'desc'
  onChange: (sortBy: string, sortDir: 'asc' | 'desc') => void
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const current = `${sortBy}:${sortDir}`
  const currentLabel = sortOptions.find((option) => option.value === current)?.label ?? '排序'
  useOutsideClose(open, wrapRef, () => onOpen(false))
  return (
    <div className="filter-popover-wrap" ref={wrapRef}>
      <button className={`filter-pill ${current !== 'createdAt:desc' ? 'is-active' : ''}`} type="button" onClick={() => onOpen(!open)}>
        <span>{currentLabel}</span>
        <ChevronDown size={15} />
      </button>
      {open && (
        <div className="filter-popover sort-popover">
          {sortOptions.map((option) => (
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

const sortOptions = [
  { value: 'createdAt:desc', label: '最新上传' },
  { value: 'createdAt:asc', label: '最早上传' },
  { value: 'updatedAt:desc', label: '最近更新' },
  { value: 'updatedAt:asc', label: '最早更新' },
  { value: 'tagCompletedAt:desc', label: '最近识别' },
  { value: 'tagCompletedAt:asc', label: '最早识别' },
  { value: 'code:asc', label: '货号 A-Z' },
  { value: 'code:desc', label: '货号 Z-A' },
]

function StatusFilter({ open, onOpen, value, options, onChange }: {
  open: boolean
  onOpen: (open: boolean) => void
  value: string
  options: FacetOption[]
  onChange: (value: string) => void
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  useOutsideClose(open, wrapRef, () => onOpen(false))
  return (
    <div className="filter-popover-wrap" ref={wrapRef}>
      <button className={`filter-pill ${value ? 'is-active' : ''}`} type="button" onClick={() => onOpen(!open)}>
        <span>{value ? garmentStatus(value) : '全部状态'}</span>
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
              {garmentStatus(option.value)}
              <small>{option.count}</small>
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
  return (
    <div className="calendar-range">
      <div className="calendar-head">
        <button type="button" onClick={() => onMonth(addMonths(month, -1))}><ChevronLeft size={15} /></button>
        <strong>{monthLabel}</strong>
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

function BatchManualTagsDialog({ items, run, onClose, onSaved }: {
  items: StylingGarment[]
  run: any
  onClose: () => void
  onSaved: (items: StylingGarment[]) => void
}) {
  const [launchBatch, setLaunchBatch] = useState('')
  const [series, setSeries] = useState('')
  const [retailChannel, setRetailChannel] = useState('')
  const [internalCollection, setInternalCollection] = useState('')
  const [stylingPriority, setStylingPriority] = useState('')
  const [notes, setNotes] = useState('')
  const [applyDoNotUse, setApplyDoNotUse] = useState(false)
  const [doNotUse, setDoNotUse] = useState(false)

  function save() {
    const manualTags: Record<string, unknown> = {}
    if (launchBatch.trim()) manualTags.launch_batch = launchBatch.trim()
    if (series.trim()) manualTags.series = parseTagListInput(series)
    if (retailChannel.trim()) manualTags.retail_channel = parseTagListInput(retailChannel)
    if (internalCollection.trim()) manualTags.internal_collection = parseTagListInput(internalCollection)
    if (stylingPriority) manualTags.styling_priority = stylingPriority
    if (notes.trim()) manualTags.notes = notes.trim()
    if (applyDoNotUse) manualTags.do_not_use_for_styling = doNotUse
    if (!Object.keys(manualTags).length) return
    const ids = items.map((item) => item.id)
    void run(`批量保存 ${items.length} 套搭配服装标签中...`, () => api.updateStylingGarmentManualTags(ids, manualTags), (result: { items: StylingGarment[] }) => onSaved(result.items), { refresh: 'background' })
  }

  const canSave = Boolean(launchBatch.trim() || series.trim() || retailChannel.trim() || internalCollection.trim() || stylingPriority || notes.trim() || applyDoNotUse)

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="modal-panel manual-tags-dialog" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-head">
          <div>
            <h2>批量维护人工标签</h2>
            <p>{items.length} 套搭配服装</p>
          </div>
          <button className="management-action icon-button" onClick={onClose} title="关闭"><X size={16} /></button>
        </div>
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
        />
        <label className="manual-checkbox">
          <input type="checkbox" checked={applyDoNotUse} onChange={(event) => setApplyDoNotUse(event.target.checked)} />
          应用禁用选搭设置
        </label>
        {applyDoNotUse && (
          <label className="manual-checkbox">
            <input type="checkbox" checked={doNotUse} onChange={(event) => setDoNotUse(event.target.checked)} />
            禁止进入智能选搭
          </label>
        )}
        <div className="drawer-actions">
          <button className="management-action" onClick={onClose}>取消</button>
          <button className="management-action" disabled={!canSave} onClick={save}>保存标签</button>
        </div>
      </aside>
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

function estimateAutoGarmentPageSize() {
  if (typeof window === 'undefined') return 12
  const sidebarWidth = window.innerWidth > 900 ? 248 : 0
  const horizontalChrome = window.innerWidth > 900 ? 80 : 24
  const availableWidth = Math.max(320, window.innerWidth - sidebarWidth - horizontalChrome)
  const availableHeight = Math.max(360, window.innerHeight - (window.innerWidth > 900 ? 260 : 280))
  return calculateAutoGarmentPageSize(availableWidth, availableHeight)
}

function calculateAutoGarmentPageSize(width: number, height: number, cardHeight = 386, columnWidth = 230, gap = 14) {
  const columns = Math.max(1, Math.floor((Math.max(width, columnWidth) + gap) / (columnWidth + gap)))
  const rows = Math.max(1, Math.floor((Math.max(height, cardHeight) + gap) / (cardHeight + gap)))
  return Math.max(1, Math.min(100, columns * rows))
}

function useAutoGarmentPageSize(resultsRef: { current: HTMLDivElement | null }) {
  const [pageSize, setPageSize] = useState(() => estimateAutoGarmentPageSize())

  useEffect(() => {
    let frame = 0
    const measure = () => {
      window.cancelAnimationFrame(frame)
      frame = window.requestAnimationFrame(() => {
        const results = resultsRef.current
        const grid = results?.querySelector<HTMLElement>('.garment-grid') ?? null
        const card = results?.querySelector<HTMLElement>('.garment-card') ?? null
        const gridStyle = grid ? window.getComputedStyle(grid) : null
        const columnGap = gridStyle ? Number.parseFloat(gridStyle.columnGap) || 14 : 14
        const rowGap = gridStyle ? Number.parseFloat(gridStyle.rowGap) || columnGap : columnGap
        const width = grid?.clientWidth || results?.clientWidth || Math.max(320, window.innerWidth - 328)
        const height = results?.clientHeight || Math.max(360, window.innerHeight - 260)
        const measuredCardHeight = card?.getBoundingClientRect().height ?? 0
        const cardHeight = measuredCardHeight > 120 ? measuredCardHeight : 386
        setPageSize(calculateAutoGarmentPageSize(width, height, cardHeight, 230, Math.max(columnGap, rowGap)))
      })
    }

    measure()
    window.addEventListener('resize', measure)
    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : null
    if (resultsRef.current) observer?.observe(resultsRef.current)
    return () => {
      window.cancelAnimationFrame(frame)
      window.removeEventListener('resize', measure)
      observer?.disconnect()
    }
  }, [resultsRef])

  return pageSize
}

type StylingImportBatch = ImportBatch & { error?: string }

type ImportJob = {
  id: string
  title: string
  status: 'RUNNING' | 'SUCCEEDED' | 'FAILED'
  total: number
  uploaded: number
  skipped: number
  operations: AIOperation[]
}

function BatchImportPanel({ onImported }: { onImported: () => void }) {
  const [batches, setBatches] = useState<StylingImportBatch[]>([])
  const [localError, setLocalError] = useState('')
  const [importJobs, setImportJobs] = useState<ImportJob[]>([])

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
    setBatches((items) => items.filter((item) => item.id !== id))
  }

  function saveDrafts() {
    const seen = new Set<string>()
    const ready: StylingImportBatch[] = []
    const blocked: StylingImportBatch[] = []
    batches.forEach((batch) => {
      const code = sanitizeClientName(batch.code)
      if (!code) {
        blocked.push({ ...batch, error: '每套搭配服装都需要一个有效货号' })
        return
      }
      if (!batch.files.length) {
        blocked.push({ ...batch, code, error: '没有可用图片' })
        return
      }
      if (seen.has(code)) {
        blocked.push({ ...batch, code, error: '本批次代码重复，已跳过' })
        return
      }
      seen.add(code)
      ready.push({ ...batch, code, error: undefined })
    })
    setBatches(blocked)
    if (!ready.length) {
      setLocalError(blocked.length ? '没有可导入的新服装，请先处理标红项' : '请先选择图片或文件夹')
      return
    }
    setLocalError(blocked.length ? '已开始导入非重复项，标红项处理后可再次导入' : '')
    startImportJob(ready)
  }

  function updateJob(jobId: string, updater: (job: ImportJob) => ImportJob) {
    setImportJobs((jobs) => jobs.map((job) => job.id === jobId ? updater(job) : job))
  }

  function addSkippedBatch(batch: StylingImportBatch, error: string) {
    setBatches((items) => {
      if (items.some((item) => item.id === batch.id)) return items
      return [...items, { ...batch, error }]
    })
  }

  function startImportJob(jobBatches: StylingImportBatch[]) {
    const jobId = `import-${Date.now()}-${Math.random().toString(36).slice(2)}`
    const startedAt = new Date().toISOString()
    const initialOperations: AIOperation[] = jobBatches.map((batch, index) => ({
      id: -(index + 1),
      ownerType: 'styling_garment_upload',
      ownerId: index + 1,
      provider: 'browser',
      status: 'SUBMITTED',
      startedAt,
      providerRequestId: batch.code,
    }))
    const nextJob: ImportJob = {
      id: jobId,
      title: `${jobBatches.length} 套搭配服装`,
      status: 'RUNNING',
      total: jobBatches.length,
      uploaded: 0,
      skipped: 0,
      operations: initialOperations,
    }
    setImportJobs((jobs) => [nextJob, ...jobs].slice(0, 5))
    void runImportJob(jobId, jobBatches)
  }

  async function runImportJob(jobId: string, jobBatches: StylingImportBatch[]) {
    const createdItems: StylingGarment[] = []
    let skipped = 0
    for (const batch of jobBatches) {
      updateJob(jobId, (job) => ({
        ...job,
        operations: job.operations.map((item) => item.providerRequestId === batch.code ? { ...item, status: 'RUNNING', startedAt: item.startedAt || new Date().toISOString() } : item),
      }))
      try {
        const uploadFiles = formalAssetFiles(batch.files, STYLING_GARMENT_MAX_IMAGES)
        const created = await api.createStylingGarment(batch.code, uploadFiles)
        createdItems.push(created.item)
        updateJob(jobId, (job) => ({
          ...job,
          uploaded: createdItems.length,
          operations: job.operations.map((item) => item.providerRequestId === batch.code ? { ...item, status: 'SUCCEEDED', completedAt: new Date().toISOString() } : item),
        }))
        onImported()
      } catch (err) {
        const message = err instanceof Error ? err.message : '导入失败'
        skipped += 1
        addSkippedBatch(batch, message === '搭配服装代码已存在' ? '代码已存在，已跳过' : message)
        updateJob(jobId, (job) => ({
          ...job,
          skipped,
          operations: job.operations.map((item) => item.providerRequestId === batch.code ? { ...item, status: 'FAILED', failedAt: new Date().toISOString(), errorMessage: message } : item),
        }))
      }
    }
    if (!createdItems.length) {
      updateJob(jobId, (job) => ({ ...job, status: 'FAILED' }))
      return
    }
    updateJob(jobId, (job) => ({
      ...job,
      operations: [
        ...createdItems.map((item, index) => ({
          id: -(index + 1 + job.total),
          ownerType: 'styling_garment_upload',
          ownerId: item.id,
          provider: 'browser',
          status: 'SUCCEEDED',
          completedAt: new Date().toISOString(),
          providerRequestId: item.code,
        })),
        ...createdItems.map((item, index) => ({
          id: -(index + 1 + job.total + createdItems.length),
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
      await api.tagStylingGarments(createdItems.map((item) => item.id))
      updateJob(jobId, (job) => ({
        ...job,
        status: job.skipped ? 'FAILED' : 'SUCCEEDED',
        operations: job.operations.map((operation) => operation.ownerType === 'styling_garment_tagging' ? { ...operation, status: 'SUBMITTED' } : operation),
      }))
    } catch {
      updateJob(jobId, (job) => ({ ...job, status: 'FAILED' }))
    } finally {
      onImported()
    }
  }

  return (
    <div className="panel import-panel">
      <div className="import-head">
        <div>
          <h3>导入搭配服装</h3>
          <p>{`散图会合并为一套；单货号文件夹是一套；上级文件夹会递归识别最底层图片目录。最多 ${STYLING_GARMENT_MAX_IMAGES} 张进入候选池，AI 打标后只保留挑选排序好的 ${STYLING_GARMENT_SELECTED_IMAGES} 张纯服装参考图。`}</p>
        </div>
        <button className="management-action" disabled={!batches.length} onClick={saveDrafts}>导入</button>
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
          <label className="management-action">
            <FolderOpen size={16} /> 选择文件夹
            <input type="file" multiple {...{ webkitdirectory: '', directory: '' }} onChange={(event) => addFiles(Array.from(event.target.files ?? []))} />
          </label>
        </div>
      </div>
      {localError && <span className="inline-error">{localError}</span>}
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
      {importJobs.length > 0 && (
        <div className="import-job-list">
          {importJobs.map((job) => (
            <div className={`import-job ${job.status === 'FAILED' ? 'has-error' : ''}`} key={job.id}>
              <div className="import-job-head">
                <strong>{job.title}</strong>
                <span>{job.uploaded}/{job.total} 已入库{job.skipped ? `，${job.skipped} 项跳过` : ''}</span>
              </div>
              <AIOperationList operations={job.operations} />
            </div>
          ))}
        </div>
      )}
    </div>
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

function FileThumbList({ files, onChange, limit }: { files: File[]; onChange: (files: File[]) => void; limit?: number }) {
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const [viewer, setViewer] = useState<LightboxImage | null>(null)
  const [dimensions, setDimensions] = useState<Record<string, { width: number; height: number }>>({})
  function move(from: number, to: number) {
    if (to < 0 || to >= files.length) return
    const next = [...files]
    const [item] = next.splice(from, 1)
    next.splice(to, 0, item)
    onChange(next)
  }
  return (
    <div className="file-thumb-grid">
      {files.map((file, index) => {
        const overLimit = typeof limit === 'number' && index >= limit
        return (
          <div
            className={`file-thumb ${overLimit ? 'is-over-limit' : ''}`}
            key={`${file.name}-${file.size}-${index}`}
            draggable
            onDragStart={() => setDragIndex(index)}
            onDragOver={(event) => event.preventDefault()}
            onDrop={() => {
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
            <span>{file.name}<small>{fileMeta(file, dimensions[fileKey(file)])}</small></span>
            <div className="thumb-actions">
              <label title="替换图片">
                <RefreshCw size={14} />
                <input type="file" accept={SUPPORTED_IMAGE_ACCEPT} onChange={(event) => {
                  const replacement = event.target.files?.[0]
                  if (!replacement) return
                  const next = [...files]
                  next[index] = replacement
                  onChange(next)
                }} />
              </label>
              <button onClick={() => onChange(files.filter((_, itemIndex) => itemIndex !== index))} title="删除图片"><X size={14} /></button>
            </div>
          </div>
        )
      })}
      <label className="file-thumb file-thumb-add" title="新增图片">
        <div className="add-icon-wrapper">
          <ImagePlus size={24} />
        </div>
        <span>新增图片</span>
        <input type="file" accept={SUPPORTED_IMAGE_ACCEPT} multiple onChange={(event) => {
          const added = Array.from(event.target.files ?? [])
          if (added.length) {
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
      <PageTitle title="模特与背景" />
      <div className="panel model-page">
        <div className="model-meta">
          <div className="model-cover">
            {coverThumb ? <img src={mediaUrl(coverThumb)} loading="lazy" alt={model?.name ?? '数字模特'} /> : <div className="image-placeholder"><UserRound size={40} /></div>}
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
              <strong>数字模特头部参考图</strong>
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
                      <span className="has-angle">
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
                      <span>{angleLabel(ref.angle ?? ref.id)}</span>
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
                  <span>拍摄背景参考图<small>{assetMeta(env)}</small></span>
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
  const [showTools, setShowTools] = useState(false)

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
    setShowTools(true)
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
        actions={<div className="toolbar"><span className="chip">Admin</span><button className="management-action" onClick={() => setShowTools((value) => !value)}><Eye size={16} />{showTools ? '收起预览工具' : '预览工具'}</button>{showTools && <button className="management-action" onClick={refreshPreview} disabled={!activeItem}><RefreshCw size={16} />刷新预览</button>}<button className="management-action" onClick={save} disabled={!activeItem || !dirty}><Save size={16} />保存</button></div>}
      />
      <div className={`prompt-maintenance-layout ${showTools ? '' : 'tools-collapsed'}`}>
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
                  <h2>{activeItem.title}</h2>
                  <p>{activeItem.description}</p>
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
        {showTools && <aside className="panel prompt-preview-panel">
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
        </aside>}
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
          <strong>{item.title}</strong>
          <span>{item.missingPlaceholders.length ? `缺失 ${item.missingPlaceholders.length}` : '已保存'}</span>
        </button>
      ))}
    </div>
  )
}

function RecordsPage({ records, detail, setDetail, openTaskInStudio, run }: {
  records: GenerationTask[]
  detail: GenerationTask | null
  setDetail: Dispatch<SetStateAction<GenerationTask | null>>
  openTaskInStudio: (task: GenerationTask) => void
  run: any
}) {
  const [items, setItems] = useState<GenerationTask[]>(records)
  const [offset, setOffset] = useState(0)
  const [total, setTotal] = useState(0)
  const [totalPages, setTotalPages] = useState(0)
  const [loading, setLoading] = useState(false)
  const [includeArchived, setIncludeArchived] = useState(false)
  const [selectedRecordIds, setSelectedRecordIds] = useState<Set<number>>(() => new Set())
  const [deleteRecordDialog, setDeleteRecordDialog] = useState<GenerationTask[] | null>(null)
  const [deleteRecordImpact, setDeleteRecordImpact] = useState<DeleteImpact | null>(null)
  const [deleteRecordImpactLoading, setDeleteRecordImpactLoading] = useState(false)
  const [downloadPrecheck, setDownloadPrecheck] = useState<FinalImageExportPrecheck | null>(null)
  const [downloadChecking, setDownloadChecking] = useState(false)
  const [pageSize, setPageSize] = useState(12)
  const [bulkMode, setBulkMode] = useState(false)
  const selectableRecordIds = items.filter((item) => !isTaskRunning(item)).map((item) => item.id)
  const selectedVisibleRecords = items.filter((item) => selectedRecordIds.has(item.id) && selectableRecordIds.includes(item.id))
  const selectedArchivedRecords = selectedVisibleRecords.filter((item) => item.archivedAt)
  const selectedActiveRecords = selectedVisibleRecords.filter((item) => !item.archivedAt)
  const allVisibleRecordsSelected = selectableRecordIds.length > 0 && selectableRecordIds.every((id) => selectedRecordIds.has(id))

  function refreshRecordPage(targetOffset = offset) {
    return api.listRecords({ limit: pageSize, offset: targetOffset, includeArchived }).then((result) => {
      setItems(result.items)
      setTotal(result.total)
      setTotalPages(result.totalPages)
      if (!result.items.length && targetOffset > 0) setOffset(Math.max(0, targetOffset - pageSize))
    })
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    void api.listRecords({ limit: pageSize, offset, includeArchived }).then((result) => {
      if (cancelled) return
      setItems(result.items)
      setTotal(result.total)
      setTotalPages(result.totalPages)
    }).catch(() => undefined).finally(() => {
      if (!cancelled) setLoading(false)
    })
    return () => { cancelled = true }
  }, [records, offset, includeArchived, pageSize])

  useEffect(() => setSelectedRecordIds(new Set()), [offset, includeArchived, pageSize])

  function openRecord(item: GenerationTask) {
    setDetail(item)
    window.history.replaceState(null, '', routeToUrl({ page: 'records', recordId: item.id }))
    void api.getRecord(item.id, false)
      .then((r) => setDetail(r.item))
      .catch(() => undefined)
  }

  function toggleRecordSelection(id: number, checked: boolean) {
    setSelectedRecordIds((current) => {
      const next = new Set(current)
      if (checked) next.add(id)
      else next.delete(id)
      return next
    })
  }

  function toggleVisibleRecordSelection(checked: boolean) {
    setSelectedRecordIds((current) => {
      const next = new Set(current)
      selectableRecordIds.forEach((id) => {
        if (checked) next.add(id)
        else next.delete(id)
      })
      return next
    })
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
    void run(purge ? '彻底删除记录中...' : '归档记录中...', action, () => {
      clearRecordDetailIfAffected(ids)
      setSelectedRecordIds(new Set())
      void refreshRecordPage().catch(() => undefined)
    }, { refresh: 'background' })
  }

  function restoreRecords(targets: GenerationTask[]) {
    const ids = targets.map((item) => item.id)
    void run(ids.length === 1 ? '恢复生成记录中...' : `恢复 ${ids.length} 条生成记录中...`, () => api.restoreTasks(ids), () => {
      setDetail((current) => current && ids.includes(current.id) ? { ...current, archivedAt: null, archiveReason: null } : current)
      setSelectedRecordIds(new Set())
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

  function cancelRecordTask(item: GenerationTask) {
    if (!window.confirm('确定取消本次生成吗？已提交给生成服务的图片可能仍会完成并产生费用；系统会停止后续生成，并保留已完成结果。')) return
    void run('取消生成中...', () => api.cancelTask(item.id), (r: { item: GenerationTask }) => {
      setItems((current) => current.map((entry) => entry.id === item.id ? r.item : entry))
      if (detail?.id === item.id) setDetail(r.item)
    }, { refresh: 'background' })
  }

  return (
    <section className="page workbench-page records-page">
      <PageTitle title="生成记录" />
      <div className="records-layout">
        <div className="panel record-list">
          <div className="record-list-head">
            <div className="archive-controls">
              <label><input type="checkbox" checked={includeArchived} onChange={(event) => {
                setOffset(0)
                setIncludeArchived(event.target.checked)
                setSelectedRecordIds(new Set())
              }} /> 查看已归档</label>
              {bulkMode && <label><input type="checkbox" checked={allVisibleRecordsSelected} disabled={!selectableRecordIds.length} onChange={(event) => toggleVisibleRecordSelection(event.target.checked)} /> 全选本页</label>}
            </div>
            <div className="record-list-meta">
              {selectedVisibleRecords.length > 0 && (
                <button className="management-action" disabled={downloadChecking} onClick={startBatchDownload} title="打包下载所选任务的最终成片，按任务名分文件夹">
                  <Download size={14} /> {downloadChecking ? '检查成片中...' : `下载成片 ${selectedVisibleRecords.length} 项`}
                </button>
              )}
              <button className="management-action compact-control" onClick={() => { setBulkMode((value) => !value); setSelectedRecordIds(new Set()) }}>{bulkMode ? '退出批量' : '批量管理'}</button>
              {selectedVisibleRecords.length > 0 && (
                selectedArchivedRecords.length === selectedVisibleRecords.length
                  ? <button className="management-action" onClick={() => restoreRecords(selectedArchivedRecords)}>恢复 {selectedArchivedRecords.length} 项</button>
                  : <button className="management-action" disabled={selectedArchivedRecords.length > 0} title={selectedArchivedRecords.length ? '请分别选择已归档和未归档记录' : undefined} onClick={runRecordBatchAction}>归档 {selectedActiveRecords.length} 项</button>
              )}
              <span className="muted-count">{total ? `${offset + 1}-${Math.min(offset + items.length, total)} 条` : '0 条'}</span>
            </div>
          </div>
          <div className="record-list-body">
            {items.map((item) => (
              <div
                key={item.id}
                className={`record-row ${detail?.id === item.id ? 'active' : ''} ${item.archivedAt ? 'is-archived' : ''}`}
              >
                {bulkMode && <input
                  type="checkbox"
                  checked={selectedRecordIds.has(item.id)}
                  disabled={isTaskRunning(item)}
                  onChange={(event) => toggleRecordSelection(item.id, event.target.checked)}
                  aria-label={`选择 ${item.name}`}
                />}
                <button className="record-open" aria-current={detail?.id === item.id ? 'true' : undefined} title="查看记录详情" onClick={() => openRecord(item)}>
                  <AssetThumb asset={taskCoverAsset(item)} interactive={false} />
                  <div><strong>{item.name}</strong><span>{item.archivedAt ? '已归档' : statusText(item.status)} · {dateText(item.createdAt)}</span></div>
                  <div className="strip">{(item.summary?.finalImages ?? []).slice(0, 5).map((asset) => <AssetThumb key={asset.id} asset={asset} interactive={false} />)}</div>
                </button>
                <button
                  className="task-action record-action-button"
                  onClick={() => openTaskInStudio(item)}
                  title="回到 AI 摄影棚恢复该任务"
                >
                  <TaskResumeActionContent task={item} />
                </button>
                <button
                  className="management-action record-action-button"
                  onClick={() => isTaskRunning(item) ? cancelRecordTask(item) : item.archivedAt ? restoreRecords([item]) : openRecordDeleteDialog(item)}
                >
                  {isTaskRunning(item) ? '取消生成' : item.archivedAt ? '恢复归档' : '归档'}
                </button>
              </div>
            ))}
            {!items.length && <EmptyState title="暂无生成记录" text="创建拍摄任务后会出现在这里。" />}
          </div>
          <PaginationControls
            disabled={loading}
            offset={offset}
            pageSize={pageSize}
            pageSizeOptions={densePageSizeOptions}
            total={total}
            totalPages={totalPages}
            onOffsetChange={setOffset}
            onPageSizeChange={(value) => {
              setPageSize(value)
              setOffset(0)
            }}
          />
        </div>
        <div className="panel record-detail">
          {detail ? (
            <>
              <h3>{detail.name}</h3>
              <Meta label="状态" value={statusText(detail.status)} />
              <Meta label="选中搭配" value={`${detail.styling?.selectedMaterials?.length ?? 0} 件`} />
              <RecordImagePreview task={detail} />
              {detail.archivedAt && <button className="management-action" onClick={() => restoreRecords([detail])}>恢复归档</button>}
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
              <strong>{item.taskName}</strong>
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
  const canConfirm = !submitting && (!purge || confirmText === expectedText)

  useEffect(() => {
    setPurge(false)
    setConfirmText('')
    setSubmitting(false)
  }, [items])

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
        <span>共 {total} 条</span>
        <span>{total > 0 ? `${firstItem}-${lastItem} 条` : '当前 0 条'}</span>
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
  return <div className="page-title"><div><h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</div>{actions}</div>
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

function GarmentCard({ item, onTag, onOpen, onDelete, onRestore, selected, onSelect }: {
  item: StylingGarment
  onTag?: () => void
  onOpen?: () => void
  onDelete?: () => void
  onRestore?: () => void
  selected?: boolean
  onSelect?: (checked: boolean) => void
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
  const runningTagAttempt = latestRunningTagAttempt(item)
  const failedTagAttempt = failed ? latestFailedTagAttempt(item) : null
  return (
    <div className={`garment-card ${tagging ? 'is-tagging' : ''} ${item.archivedAt ? 'is-archived' : ''}`}>
      {onSelect && (
        <label className="card-select" title="选择">
          <input type="checkbox" checked={!!selected} disabled={tagging} onChange={(event) => onSelect(event.target.checked)} />
        </label>
      )}
      <div className="garment-cover">
        <AssetThumb asset={item.images?.[0]?.asset ?? item.coverAsset ?? (item.coverUrl ? { id: item.id, objectKey: '', filename: item.code, mimeType: '', thumbUrl: item.coverUrl } : null)} large />
        {tagging && <span className="cover-badge">识别中</span>}
        {item.archivedAt && <span className="cover-badge">已归档</span>}
      </div>
      <div className="garment-card-head"><strong title={item.code}>{item.code}</strong><span className={`status ${item.status.toLowerCase()}`}>{garmentStatus(item.status)}</span></div>
      {failed && (item.tagError || failedTagAttempt) && <span className="inline-error">{friendlyErrorText(failedTagAttempt?.errorCode, failedTagAttempt?.errorMessage ?? item.tagError)}</span>}
      <p>{tagging ? '正在识别服装信息，完成后可进入智能选搭。' : failed ? '打标未完成，可查看详情或重新打标。' : brief || '待识别搭配信息'}</p>
      {tagging && runningTagAttempt && <AIOperationList operations={[runningTagAttempt]} compact />}
      <div className="tag-summary">
        {showTagResult && chips.map((chip) => <span key={chip}>{chip}</span>)}
        {(!showTagResult || !chips.length) && <span>{tagging ? '识别中' : failed ? '待重新打标' : '待识别'}</span>}
      </div>
      {meta.overall_confidence !== undefined && showTagResult && <span className="confidence-text">AI 置信度 {formatConfidence(meta.overall_confidence)}</span>}
      <div className="card-actions">
        {onOpen && <button className="management-action" onClick={onOpen}><Eye size={16} /> 详情</button>}
        {onTag && <button className="management-action" disabled={tagging} onClick={onTag}>重新识别标签</button>}
        {item.archivedAt ? onRestore && <button className="management-action" disabled={tagging} onClick={onRestore}><RefreshCw size={16} /> 恢复归档</button> : onDelete && <button className="management-action" disabled={tagging} onClick={onDelete}><Trash2 size={16} /> 归档</button>}
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
  const hasTaggingFailure = item.status === 'TAGGING_FAILED' || Boolean(item.tagError || failedTagAttempt)
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
    if (dirty && !window.confirm('当前有未保存的图片修改，本次重新打标将基于已保存的图片。是否继续？')) return
    void run('重新打标中...', () => api.tagStylingGarment(item.id), (result: { item: StylingGarment }) => onChange(result.item), { refresh: 'background' })
  }

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="detail-drawer" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-head">
          <div>
            <h2>{item.code}</h2>
            <span className={`status ${item.status.toLowerCase()}`}>{garmentStatus(item.status)}</span>
          </div>
          <button className="management-action icon-button" onClick={onClose} title="关闭"><X size={16} /></button>
        </div>
        <div className="drawer-section">
          <div className="section-head"><strong>基础信息</strong></div>
          <Meta label="上传时间" value={dateText(item.createdAt)} />
          <Meta label="更新时间" value={dateText(item.updatedAt)} />
          <Meta label="打标时间" value={dateText(item.tagCompletedAt)} />
          <Meta label="选搭状态" value={selectableText} />
          <Meta label="图片" value={`${working.length}/${STYLING_GARMENT_MAX_IMAGES} 张`} />
          {hasTaggingFailure && (
            <TagObjectPanel
              title="识别错误"
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
          <p className="edit-hint">此次修改会影响标签识别。排序第一张会作为封面和主要参考图；如需补充图片，请重新导入一套素材。</p>
          {tagging && <p className="edit-hint">正在识别标签，完成后可继续修改或删除。</p>}
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
                  <span>{replacement?.name ?? image.asset.filename}<small>{replacement ? fileMeta(replacement, replacementDimensions[image.id]) : assetMeta(image.asset)}</small></span>
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
          <TagObjectPanel title={hasTaggingFailure ? '上次 AI 打标返回' : 'AI 识别标签'} data={item.aiTags ?? {}} defaultOpen />
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
        <div className="drawer-section">
          <div className="section-head"><strong>技术详情</strong></div>
          <TagObjectPanel title="标签元信息" data={item.tagMeta ?? {}} />
          {isAdmin && (item.tagAttempts ?? []).length > 0 && (
            <JsonOutputPanel title="Gemini 调用过程事实" data={item.tagAttempts} />
          )}
        </div>
        <div className="drawer-actions">
          <button className="management-action" disabled={tagging} onClick={retag}>重新打标</button>
          <button className="management-action" disabled={tagging || !dirty || !working.length} onClick={() => void save(false)}>仅保存</button>
          <button className="management-action" disabled={tagging || !dirty || !working.length} onClick={() => void save(true)}>保存并重新识别标签</button>
        </div>
        <div className="danger-zone">
          <strong>{item.archivedAt ? '恢复归档' : '归档'}</strong>
          <button className="management-action" disabled={tagging} onClick={() => {
            if (item.archivedAt) {
              onRestore()
            } else {
              onDelete()
              onClose()
            }
          }}>{item.archivedAt ? '恢复归档' : '归档'}</button>
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

function taggingErrorDisplay(item: StylingGarment, attempt: NonNullable<StylingGarment['tagAttempts']>[number] | null): Record<string, unknown> {
  const data: Record<string, unknown> = {
    当前状态: garmentStatus(item.status),
  }
  if (item.tagError || attempt?.errorMessage) data.错误信息 = item.tagError || attempt?.errorMessage
  if (attempt?.errorCode) data.错误码 = attempt.errorCode
  if (attempt?.provider) data.服务 = attempt.provider
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
            <h2>{dialog.title}</h2>
            <p>展示本次调用真实发生的请求、响应、解析和时间事实；不用于重放任务。</p>
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
                    <strong>{operationLabel(record, index)}</strong>
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
              <span>{typeof item === 'object' && item ? JSON.stringify(item, null, 0) : formatTagPrimitive(item)}</span>
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
          <div key={key}><b>{key}</b><span>{formatTagPrimitive(nested)}</span></div>
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
        <strong>{operationLabel(operation, index)}</strong>
        {phase && <span>{attemptPhaseText(phase, operation)}</span>}
        {failureNote && <span className="operation-failure">{failureNote}</span>}
        {!compact && breakdown && <span className="operation-breakdown">{breakdown}</span>}
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
    return Math.max(
      max,
      Number.isFinite(generationRetry) ? generationRetry : 0,
      Number.isFinite(unknownSubmissionRetry) ? unknownSubmissionRetry : 0,
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
  const src = asset ? assetThumbSrc(asset) : ''
  useEffect(() => setFailed(false), [asset?.id, src])
  const image = asset && src && !failed ? <img src={mediaUrl(src)} alt={asset.filename} loading="lazy" onError={() => setFailed(true)} /> : null
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
            <strong>{current.title}</strong>
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

function Meta({ label, value }: { label: string; value: ReactNode }) {
  return <div className="meta"><span>{label}</span><strong>{value}</strong></div>
}

function EmptyState({ title, text }: { title: string; text: string }) {
  return <div className="empty"><Clock3 size={22} /><strong>{title}</strong><span>{text}</span></div>
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

function garmentStatus(status: string) {
  if (status === 'DRAFT') return '草稿'
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
