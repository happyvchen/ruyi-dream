import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import type { DragEvent, FormEvent, ReactNode } from 'react'
import {
  Bot,
  ArrowDown,
  ArrowUp,
  ChevronLeft,
  ChevronRight,
  CircleStop,
  Clock3,
  Download,
  Eye,
  FileText,
  FileImage,
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
import { api, mediaUrl } from './api'
import type { AIOperation, BrandInfo, BrandInfoPayload, DeleteImpact, GenerationTask, MediaAsset, ModelProfile, PromptConfig, PromptPlaceholder, SessionRole, ShootConfig, ShootConfigCategoryOption, ShootConfigExportPayload, ShootConfigGlobalRule, ShootConfigPromptCard, ShootConfigShotRule, StylingGarment, TaxonomyApplicabilityPayload, TaxonomyApplicabilityRule, TaxonomyDimensionPayload, TaxonomyExportPayload, TaxonomyPromptCard, TaxonomyRelation, TaxonomyRelationPayload, TaxonomyRules, TaxonomyTag, TaxonomyTagPayload } from './api'
import { parseRoute, routeToUrl } from './routes'
import type { Page } from './routes'

type StudioStep = 0 | 1 | 2 | 3 | 4
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

const NON_SHOOT_CONFIG_CATEGORY_CODES = new Set(['unknown', 'unsupported_accessory'])
type LightboxImageItem = Omit<LightboxImage, 'items' | 'initialIndex'>
type DeleteDialogTarget = Pick<GenerationTask, 'id' | 'name'>
type RunOptions = { refresh?: boolean | 'background' }

const steps = [
  { title: '上传主推款', icon: Upload },
  { title: 'AI 智能选搭', icon: WandSparkles },
  { title: '生成拍摄剧本', icon: FileText },
  { title: '生成分镜预览', icon: GalleryVerticalEnd },
  { title: '生成成片', icon: Sparkles },
] as const
const adminPages = new Set<Page>(['brand', 'shoot-config', 'tag-rules', 'prompt-maintenance'])

const defaultPageSizeOptions = [20, 50, 100] as const
const densePageSizeOptions = [12, 50, 100] as const

export function App() {
  const [checked, setChecked] = useState(false)
  const [authenticated, setAuthenticated] = useState(false)
  const [role, setRole] = useState<SessionRole | null>(null)
  const [page, setPage] = useState<Page>(() => parseRoute().page)
  const [collapsed, setCollapsed] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [task, setTask] = useState<GenerationTask | null>(null)
  const [tasks, setTasks] = useState<GenerationTask[]>([])
  const [records, setRecords] = useState<GenerationTask[]>([])
  const [garments, setGarments] = useState<StylingGarment[]>([])
  const [models, setModels] = useState<ModelProfile[]>([])
  const [brandInfo, setBrandInfo] = useState<BrandInfo | null>(null)
  const [recordDetail, setRecordDetail] = useState<GenerationTask | null>(null)
  const [localOperations, setLocalOperations] = useState<AIOperation[]>([])

  useEffect(() => {
    void api.session()
      .then((session) => {
        setAuthenticated(session.authenticated)
        setRole(session.role ?? null)
        if (session.role !== 'admin' && adminPages.has(parseRoute().page)) {
          setPage('studio')
          window.history.replaceState(null, '', routeToUrl({ page: 'studio' }))
        }
        setChecked(true)
      })
      .catch(() => setChecked(true))
  }, [])

  useEffect(() => {
    if (!authenticated) return
    void refreshAll()
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
        void api.getTask(route.taskId).then((result) => setTask(result.item)).catch(() => undefined)
      }
      if (route.page === 'records' && !route.recordId) setRecordDetail(null)
      if (route.page === 'records' && route.recordId) {
        void api.getRecord(route.recordId, false).then((result) => setRecordDetail(result.item)).catch(() => undefined)
      }
    }
    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [])

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
    if (role === 'admin') {
      const brandResult = await api.getBrandInfo().catch(() => ({ item: null }))
      setBrandInfo(brandResult.item)
    } else {
      setBrandInfo(null)
    }
    const route = parseRoute()
    const requestedTaskId = route.page === 'studio' ? route.taskId : undefined
    if (requestedTaskId && (!task || task.id !== requestedTaskId)) {
      const detail = await api.getTask(requestedTaskId).then((r) => r.item).catch(() => null)
      if (detail) setTask(detail)
    }
    if (route.page === 'records' && route.recordId && !recordDetail) {
      const detail = await api.getRecord(route.recordId, false).then((r) => r.item).catch(() => null)
      if (detail) setRecordDetail(detail)
    }
  }

  function navigate(next: Page) {
    setPage(next)
    if (next === 'studio') setTask(null)
    if (next === 'records') setRecordDetail(null)
    window.history.pushState(null, '', routeToUrl({ page: next }))
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
      if (options.refresh === 'background') void refreshAll().catch(() => undefined)
      else if (options.refresh !== false) await refreshAll().catch(() => undefined)
      setError(err instanceof Error ? err.message : '操作失败')
      throw err
    } finally {
      setBusy('')
    }
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

  function clearLocalOperationsFor(ownerTypes: string[]) {
    setLocalOperations((items) => items.filter((item) => !ownerTypes.includes(item.ownerType)))
  }

  if (!checked) return <BootScreen />
  if (!authenticated) return <LoginPage onAuthed={(session) => {
    setAuthenticated(true)
    setRole(session.role ?? null)
  }} />

  const nav = [
    { id: 'studio' as const, label: 'AI 摄影棚', icon: Bot },
    { id: 'library' as const, label: '搭配服饰库', icon: Library },
    { id: 'model' as const, label: '数字模特', icon: UserRound },
    ...(role === 'admin' ? [{ id: 'prompt-maintenance' as const, label: 'Prompt 维护', icon: FileText }] : []),
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
              setAuthenticated(false)
              setRole(null)
            })}><LogOut size={16} /></button>}
          </div>
        </div>
      </aside>
      <main className="workspace">
        {error && <div className="toast">{error}</div>}
        {busy && <div className="busy"><Loader2 size={16} className="spin" />{busy}</div>}
        {page === 'studio' && <StudioPage task={task} setTask={setTask} records={records} run={run} localOperations={localOperations} startLocalOperation={startLocalOperation} clearLocalOperationsFor={clearLocalOperationsFor} />}
        {page === 'library' && <LibraryPage garments={garments} run={run} />}
        {page === 'model' && <ModelPage models={models} />}
        {page === 'brand' && role === 'admin' && <BrandInfoPage initialBrandInfo={brandInfo} setBrandInfo={setBrandInfo} run={run} />}
        {page === 'shoot-config' && role === 'admin' && <ShootConfigPage run={run} />}
        {page === 'tag-rules' && role === 'admin' && <TagRulesPage run={run} />}
        {page === 'prompt-maintenance' && role === 'admin' && <PromptMaintenancePage run={run} />}
        {page === 'records' && <RecordsPage records={records} detail={recordDetail} setDetail={setRecordDetail} run={run} />}
      </main>
    </div>
  )
}

function BootScreen() {
  return <div className="login-screen"><div className="login-card"><Loader2 className="spin" /> 启动中...</div></div>
}

function LoginPage({ onAuthed }: { onAuthed: (session: { role?: SessionRole | null }) => void }) {
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
        <p>{showDemoLogin ? '保护性入口。当前演示账号为 user / password 或 admin / password。' : '保护性入口。请输入管理员提供的访问凭据。'}</p>
        <label>用户名<input autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} /></label>
        <label>密码<input autoComplete="current-password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
        {error && <div className="form-error">{error}</div>}
        <button className="primary wide" disabled={busy}>{busy ? <Loader2 className="spin" size={16} /> : <Shield size={16} />} 登录</button>
      </form>
    </div>
  )
}

function StudioPage({ task, setTask, records, run, localOperations, startLocalOperation, clearLocalOperationsFor }: {
  task: GenerationTask | null
  setTask: (task: GenerationTask | null) => void
  records: GenerationTask[]
  run: <T>(label: string, action: () => Promise<T>, after?: (value: T) => void, options?: RunOptions) => Promise<T | undefined>
  localOperations: AIOperation[]
  startLocalOperation: (operation: Omit<AIOperation, 'id' | 'status' | 'startedAt'> & { status?: string; startedAt?: string | null }) => AIOperation
  clearLocalOperationsFor: (ownerTypes: string[]) => void
}) {
  const [step, setStep] = useState<StudioStep>(0)
  const [taskItems, setTaskItems] = useState<GenerationTask[]>(records.slice(0, 10))
  const currentStep = getTaskStep(task)
  const visibleStep = Math.min(Math.max(step, 0), currentStep) as StudioStep
  function startNewTask() {
    setTask(null)
    window.history.pushState(null, '', routeToUrl({ page: 'studio' }))
  }
  function cancelCurrentTask() {
    if (!task || !isTaskRunning(task)) return
    if (!window.confirm('确定取消本次生成吗？已提交给生成服务的图片可能仍会完成并产生费用；系统会停止后续生成，并保留已完成结果。')) return
    void run('取消生成中...', () => api.cancelTask(task.id), (r: { item: GenerationTask }) => {
      clearLocalOperationsFor(['generation_styling', 'generation_shoot_script', 'generation_preview', 'generation_result_image'])
      setTask(r.item)
    }, { refresh: 'background' })
  }
  useEffect(() => setStep(currentStep), [task?.id, task?.status])
  useEffect(() => {
    let cancelled = false
    void api.listRecords({ limit: 10, offset: 0 }).then((result) => {
      if (cancelled) return
      setTaskItems(result.items)
    }).catch(() => undefined)
    return () => { cancelled = true }
  }, [records])
  useEffect(() => {
    if (!task || !['STYLING_RUNNING', 'SCRIPT_RUNNING', 'PREVIEW_RUNNING', 'FINAL_RUNNING'].includes(task.status)) return
    const timer = window.setInterval(() => {
      void api.getTask(task.id).then((result) => setTask(result.item)).catch(() => undefined)
    }, 1500)
    return () => window.clearInterval(timer)
  }, [task?.id, task?.status, setTask])
  return (
    <section className="page">
      <PageTitle
        title="AI 摄影棚"
        subtitle="上传主推款，让系统选搭配角服饰并生成电商棚拍成片。"
        actions={task ? <button className="secondary" onClick={startNewTask}><ImagePlus size={16} />新建拍摄任务</button> : undefined}
      />
      <div className="studio-grid">
        <div className="panel studio-main">
          <StepRail active={visibleStep} max={currentStep} onSelect={setStep} />
          {visibleStep === 0 && <UploadHeroStep task={task} setTask={setTask} run={run} />}
          {visibleStep === 1 && <StylingStep task={task} setTask={setTask} run={run} localOperations={localOperations} startLocalOperation={startLocalOperation} clearLocalOperationsFor={clearLocalOperationsFor} />}
          {visibleStep === 2 && <ShootScriptStep task={task} setTask={setTask} run={run} localOperations={localOperations} startLocalOperation={startLocalOperation} clearLocalOperationsFor={clearLocalOperationsFor} />}
          {visibleStep === 3 && <PreviewStep task={task} setTask={setTask} run={run} localOperations={localOperations} startLocalOperation={startLocalOperation} clearLocalOperationsFor={clearLocalOperationsFor} />}
          {visibleStep === 4 && <FinalStep task={task} setTask={setTask} run={run} localOperations={localOperations} startLocalOperation={startLocalOperation} clearLocalOperationsFor={clearLocalOperationsFor} />}
        </div>
        <aside className="panel task-rail">
          <h3>任务摘要</h3>
          {task ? (
            <>
              <ThumbGrid assets={(task.images ?? []).map((item) => item.asset)} compact />
              <Meta label="任务" value={task.name} />
              <Meta label="状态" value={statusText(task.status)} />
              <Meta label="搭配服饰" value={`${task.styling?.selectedMaterials?.length ?? 0} 件`} />
              <Meta label="最终图" value={`${task.resultSet?.images?.filter((i) => i.status === 'SUCCEEDED').length ?? 0}/5`} />
              {isTaskRunning(task) && (
                <button className="secondary wide" onClick={cancelCurrentTask}>
                  <CircleStop size={16} /> 取消生成
                </button>
              )}
              <AIOperationList operations={visibleTaskOperations(task, localOperations)} compact />
            </>
          ) : <EmptyState title="暂无任务" text="上传主推款后会在这里形成拍摄任务。" />}
          <h3>最近任务</h3>
          <div className="mini-list">
            {taskItems.map((item) => (
              <div key={item.id} className={`mini-row view-only ${task?.id === item.id ? 'active' : ''} ${item.archivedAt ? 'is-archived' : ''}`}>
                <button onClick={() => {
                  setTask(item)
                  window.history.pushState(null, '', routeToUrl({ page: 'studio', taskId: item.id }))
                  void api.getRecord(item.id).then((r) => setTask(r.item)).catch(() => undefined)
                }}>
                  <span>{item.name}</span><em>{item.archivedAt ? '已归档' : statusText(item.status)}</em>
                </button>
                {isTaskRunning(item) && (
                  <button
                    className="secondary mini-action"
                    onClick={() => {
                      if (!window.confirm('确定取消本次生成吗？已提交给生成服务的图片可能仍会完成并产生费用；系统会停止后续生成，并保留已完成结果。')) return
                      void run('取消生成中...', () => api.cancelTask(item.id), (r: { item: GenerationTask }) => {
                        clearLocalOperationsFor(['generation_styling', 'generation_shoot_script', 'generation_preview', 'generation_result_image'])
                        if (task?.id === item.id) setTask(r.item)
                        setTaskItems((current) => current.map((entry) => entry.id === item.id ? r.item : entry))
                      }, { refresh: 'background' })
                    }}
                  >
                    取消
                  </button>
                )}
              </div>
            ))}
            {!taskItems.length && <EmptyState title="暂无任务" text="创建拍摄任务后会出现在这里。" />}
          </div>
        </aside>
      </div>
    </section>
  )
}

function StepRail({ active, max, onSelect }: { active: StudioStep; max: StudioStep; onSelect: (step: StudioStep) => void }) {
  return (
    <div className="step-rail">
      {steps.map((step, index) => {
        const Icon = step.icon
        const enabled = index <= max
        return (
          <button key={step.title} className={active === index ? 'active' : ''} disabled={!enabled} onClick={() => onSelect(index as StudioStep)}>
            <Icon size={18} />
            <span>{step.title}</span>
          </button>
        )
      })}
    </div>
  )
}

function UploadHeroStep({ task, setTask, run }: { task: GenerationTask | null; setTask: (task: GenerationTask) => void; run: any }) {
  const [files, setFiles] = useState<File[]>([])
  const [name, setName] = useState('')

  function addHeroFiles(nextFiles: File[]) {
    const imageFiles = nextFiles
      .filter((file) => file.type.startsWith('image/') || /\.(png|jpe?g|webp)$/i.test(file.name))
      .sort((a, b) => importPath(a).localeCompare(importPath(b), 'zh-CN', { numeric: true }))
    setFiles(imageFiles)
    const firstPath = imageFiles[0] ? importPath(imageFiles[0]) : ''
    const defaultName = firstPath.includes('/') ? sanitizeClientName(firstPath.split('/')[0]) : timestampName()
    setName(defaultName)
  }

  async function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    const droppedFiles = await filesFromDataTransfer(event.dataTransfer)
    addHeroFiles(droppedFiles)
  }

  return (
    <div className="step-panel">
      <div className="step-copy"><h2>上传主推款</h2><p>支持多张图片或一个文件夹。推荐 3-5 张，最多 8 张，排序越靠前越重要。</p></div>
      {task?.status === 'DRAFT' && <HeroDraftEditor task={task} setTask={setTask} run={run} />}
      {!task && (
        <>
          <div className="hero-upload" onDragOver={(event) => event.preventDefault()} onDrop={(event) => void handleDrop(event)}>
            <div>
              <Images size={22} />
              <strong>拖入主推款图片或文件夹</strong>
              <span>{files.length ? `已选择 ${files.length} 张，可拖动排序` : '支持多张图片或一个文件夹'}</span>
            </div>
            <div className="import-actions">
              <label className="secondary">
                <ImagePlus size={16} /> 选择图片
                <input type="file" accept="image/png,image/jpeg,image/webp" multiple onChange={(event) => addHeroFiles(Array.from(event.target.files ?? []))} />
              </label>
              <label className="secondary">
                <FolderOpen size={16} /> 选择文件夹
                <input type="file" multiple {...{ webkitdirectory: '', directory: '' }} onChange={(event) => addHeroFiles(Array.from(event.target.files ?? []))} />
              </label>
            </div>
          </div>
          {files.length > 0 && (
            <div className="upload-review">
              <div className="batch-card-head">
                <div className="batch-icon"><Layers3 size={18} /></div>
                <input value={name} onChange={(e) => setName(e.target.value)} placeholder="任务名称" aria-label="任务名称" />
                <span className={`count-pill ${files.length > 8 ? 'is-over' : ''}`}>{files.length}/8 张</span>
              </div>
              <div className="batch-note">
                <span>推荐 3-5 张，最多只允许 8 张用于创建任务。</span>
                {files.length > 8 && <strong>当前超过 8 张，请删除或调整后再创建</strong>}
              </div>
              <FileThumbList files={files} onChange={setFiles} />
              <div className="inline-form">
                <button className="primary" disabled={!files.length || files.length > 8 || !sanitizeClientName(name)} onClick={() => void run('创建拍摄任务中...', () => api.createTask(name, files), (r: { item: GenerationTask }) => {
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
      {task && task.status !== 'DRAFT' && <CurrentTaskCard task={task} />}
    </div>
  )
}

function HeroDraftEditor({ task, setTask, run }: { task: GenerationTask; setTask: (task: GenerationTask) => void; run: any }) {
  const [name, setName] = useState(task.name)
  const [viewer, setViewer] = useState<LightboxImage | null>(null)

  useEffect(() => setName(task.name), [task.id, task.name])

  async function move(index: number, direction: -1 | 1) {
    const images = task.images ?? []
    const other = images[index + direction]
    if (!other) return
    await run('调整主推款顺序中...', async () => {
      await api.updateTaskImage(images[index].id, other.sortOrder)
      return api.updateTaskImage(other.id, images[index].sortOrder)
    }, (result: { item: GenerationTask }) => setTask(result.item), { refresh: 'background' })
  }

  return (
    <div className="hero-draft">
      <div className="section-head"><strong>当前草稿</strong><span className="status draft">待确认</span></div>
      <div className="inline-form">
        <input value={name} onChange={(event) => setName(event.target.value)} placeholder="任务名称" />
        <button className="secondary" disabled={name.trim() === task.name} onClick={() => void run('保存任务名称中...', () => api.updateTask(task.id, { name }), (r: { item: GenerationTask }) => setTask(r.item), { refresh: 'background' })}>保存名称</button>
        <label className="secondary">
          <ImagePlus size={16} /> 追加图片
          <input type="file" accept="image/png,image/jpeg,image/webp" multiple onChange={(event) => {
            const next = Array.from(event.target.files ?? [])
            if (next.length) void run('追加主推款图片中...', () => api.addTaskImages(task.id, next), (r: { item: GenerationTask }) => setTask(r.item), { refresh: 'background' })
          }} />
        </label>
      </div>
      <div className="draft-image-grid">
        {(task.images ?? []).map((image, index) => (
          <div className="saved-image-card" key={image.id}>
            <button className="image-open" onClick={() => setViewer(assetLightboxImage(image.asset, (task.images ?? []).map((item) => item.asset)))} title="放大查看">
              <img src={mediaUrl(assetThumbSrc(image.asset))} alt={image.asset.filename} />
            </button>
            <span>{image.asset.filename}<small>{assetMeta(image.asset)}</small></span>
            <div className="thumb-actions">
              <button onClick={() => setViewer(assetLightboxImage(image.asset, (task.images ?? []).map((item) => item.asset)))} title="放大查看"><Eye size={14} /></button>
              <button disabled={index === 0} onClick={() => void move(index, -1)} title="前移"><ArrowUp size={14} /></button>
              <button disabled={index === (task.images?.length ?? 0) - 1} onClick={() => void move(index, 1)} title="后移"><ArrowDown size={14} /></button>
              <label title="替换图片">
                <ImagePlus size={14} />
                <input type="file" accept="image/png,image/jpeg,image/webp" onChange={(event) => {
                  const replacement = event.target.files?.[0]
                  if (replacement) void run('替换主推款图片中...', () => api.replaceTaskImage(image.id, replacement), (r: { item: GenerationTask }) => setTask(r.item), { refresh: 'background' })
                }} />
              </label>
              <button disabled={(task.images?.length ?? 0) <= 1} onClick={() => void run('移除主推款图片中...', () => api.deleteTaskImage(image.id), (r: { item: GenerationTask }) => setTask(r.item), { refresh: 'background' })} title="删除图片"><Trash2 size={14} /></button>
            </div>
          </div>
        ))}
      </div>
      <p className="edit-hint">确认后可进入智能选搭；系统会保留本次主推款和默认数字模特信息用于后续生成。</p>
      <button className="primary" onClick={() => void run('确认主推款输入中...', () => api.readyTask(task.id), (r: { item: GenerationTask }) => setTask(r.item), { refresh: 'background' })}>
        确认并进入智能选搭
      </button>
      {viewer && <ImageLightbox image={viewer} onClose={() => setViewer(null)} />}
    </div>
  )
}

function StylingStep({ task, setTask, run, localOperations, startLocalOperation, clearLocalOperationsFor }: {
  task: GenerationTask | null
  setTask: (task: GenerationTask) => void
  run: any
  localOperations: AIOperation[]
  startLocalOperation: (operation: Omit<AIOperation, 'id' | 'status' | 'startedAt'> & { status?: string; startedAt?: string | null }) => AIOperation
  clearLocalOperationsFor: (ownerTypes: string[]) => void
}) {
  const canSelect = !!task && ['READY_FOR_STYLING', 'STYLING_READY', 'SCRIPT_READY', 'PREVIEW_READY', 'COMPLETED', 'FINAL_PARTIAL', 'FAILED', 'CANCELLED'].includes(task.status)
  const hasStyling = !!task?.styling
  const operations = task ? operationFallbacks(task, localOperations, 'generation_styling') : []
  return (
    <div className="step-panel">
      <div className="step-copy"><h2>AI 智能选搭</h2><p>AI Stylist 会先理解主推款，再生成推荐造型方案；搭配服饰必须服务主推款展示。</p></div>
      <AIOperationList operations={operations} />
      {task?.styling ? (
        <StylingPlanSummary task={task} />
      ) : <EmptyState title="等待智能选搭" text="点击按钮后会冻结当前主推款输入版本。" />}
      <button className="primary" disabled={!canSelect} onClick={() => task && void run(hasStyling ? '重新选搭中...' : '智能选搭中...', () => {
        startLocalOperation({ ownerType: 'generation_styling', ownerId: task.id, provider: 'gemini' })
        setTask({
          ...task,
          status: 'STYLING_RUNNING',
          shootScript: null,
          shotPlans: [],
          preview: null,
          storyboardPreview: null,
          resultSet: null,
          activeShootScriptId: null,
          activeStoryboardPreviewId: null,
          activePreviewId: null,
          activeResultSetId: null,
        })
        return api.selectStyling(task.id)
      }, (r: { item: GenerationTask }) => {
        clearLocalOperationsFor(['generation_styling'])
        setTask(r.item)
      }, { refresh: false })}>
        <WandSparkles size={16} /> {hasStyling ? '重新选搭' : 'AI 智能选搭'}
      </button>
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
  return (
    <div className="styling-plan">
      <div className="styling-plan-head">
        <div>
          <span>推荐造型方案</span>
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
      <div className="styling-plan-grid">
        <InfoBlock title="主推款理解" items={[stringValue(hero.hero_visual_summary), ...stringList(hero.hero_selling_points)].filter(Boolean)} />
        <InfoBlock title="审美诊断" items={aestheticDiagnosis} empty="暂无审美诊断" />
        <InfoBlock title="图位重点" items={heroFocus.length ? heroFocus : stringList(hero.hero_selling_points)} />
        <InfoBlock title="生成配饰" items={generatedAccessories.length ? generatedAccessories : ['低存在感鞋包饰品']} />
        <InfoBlock title="风险提示" items={[...stringList(hero.hero_visibility_risks), ...(styling.warnings ?? [])].filter(Boolean)} empty="暂无明显风险" />
      </div>
      <div className="selected-strip">
        {styling.selectedMaterials.length ? styling.selectedMaterials.map((item) => <GarmentCard key={item.id} item={item} />) : <EmptyState title="无需库内搭配服饰" text="AI Stylist 判断本次以主推款单穿或生成配饰辅助更合适。" />}
      </div>
      {rejected.length > 0 && (
        <div className="rejected-list">
          <strong>未采用方案</strong>
          {rejected.slice(0, 3).map((item, index) => {
            const reason = userFacingMaterialText(stringValue(item.reason), materialCodesById)
            const codes = rejectedMaterialCodes(item, materialCodesById)
            const prefix = codes.length && !codes.some((code) => reason.includes(code)) ? codes.join('、') : ''
            return <span key={`${stringValue(item.outfit_key) || stringValue(item.id)}-${index}`}>{[prefix, reason].filter(Boolean).join(' · ')}</span>
          })}
        </div>
      )}
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
    result = result.replace(new RegExp(`(库内(?:针织衫|衬衫|外套|西装|上衣|下装|裤装|半裙|连衣裙|搭配服饰|服饰|单品)|皮革马甲)\\s*${escapedId}(?![A-Za-z0-9])`, 'g'), `$1 ${code} `)
  }
  result = result.replace(/\b(?:material[_\s-]*id|服饰ID|商品ID|ID)\s*[:：#-]?\s*\d+(?![A-Za-z0-9])/gi, '该服饰')
  result = result.replace(/(库内(?:针织衫|衬衫|外套|西装|上衣|下装|裤装|半裙|连衣裙|搭配服饰|服饰|单品)|皮革马甲)\s*\d+(?![A-Za-z0-9])/g, '$1')
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

function ShootScriptStep({ task, setTask, run, localOperations, startLocalOperation, clearLocalOperationsFor }: {
  task: GenerationTask | null
  setTask: (task: GenerationTask) => void
  run: any
  localOperations: AIOperation[]
  startLocalOperation: (operation: Omit<AIOperation, 'id' | 'status' | 'startedAt'> & { status?: string; startedAt?: string | null }) => AIOperation
  clearLocalOperationsFor: (ownerTypes: string[]) => void
}) {
  const canGenerate = (
    !!task?.styling && ['STYLING_READY', 'SCRIPT_READY', 'PREVIEW_READY', 'COMPLETED', 'FINAL_PARTIAL', 'FAILED', 'CANCELLED'].includes(task?.status ?? '')
  )
  const hasScript = !!task?.shootScript?.script
  const operations = task ? operationFallbacks(task, localOperations, 'generation_shoot_script') : []
  return (
    <div className="step-panel">
      <div className="step-copy"><h2>生成拍摄剧本</h2><p>AI Director 会一次性生成 6 个镜头的景别、取景、姿势、走位、背景光线和商品展示要求。</p></div>
      <AIOperationList operations={operations} />
      {task?.shootScript ? <ShootScriptSummary task={task} /> : <EmptyState title="尚未生成拍摄剧本" text="点击生成后，系统会把选搭方案转成拍摄前可执行的分镜剧本。" />}
      <button className="primary" disabled={!canGenerate} onClick={() => task && void run(hasScript ? '重新生成拍摄剧本中...' : '生成拍摄剧本中...', () => {
        startLocalOperation({ ownerType: 'generation_shoot_script', ownerId: task.id, provider: 'gemini' })
        setTask({
          ...task,
          status: 'SCRIPT_RUNNING',
          shootScript: task.shootScript ? { ...task.shootScript, status: 'RUNNING' } : null,
          preview: null,
          storyboardPreview: null,
          resultSet: null,
          activeStoryboardPreviewId: null,
          activePreviewId: null,
          activeResultSetId: null,
        })
        return api.createShootScript(task.id)
      }, (r: { item: GenerationTask }) => {
        clearLocalOperationsFor(['generation_shoot_script'])
        setTask(r.item)
      }, { refresh: false })}>
        <FileText size={16} /> {hasScript ? '重新生成拍摄剧本' : '生成拍摄剧本'}
      </button>
    </div>
  )
}

function ShootScriptSummary({ task }: { task: GenerationTask }) {
  const shootScript = task.shootScript
  if (!shootScript) return null
  const script = shootScript.script ?? {}
  const summary = (shootScript.summary && Object.keys(shootScript.summary).length ? shootScript.summary : planValue<Record<string, unknown>>(script, 'user_visible_summary') ?? {}) as Record<string, unknown>
  const directing = (planValue<Record<string, unknown>>(script, 'global_directing') ?? {}) as Record<string, unknown>
  const shots = shootScriptShots(task)
  const risks = shootScript.stylingRisks?.length ? shootScript.stylingRisks : arrayValue<Record<string, unknown>>(script, 'styling_risks')
  const adjustments = shootScript.accessoryAdjustments?.length ? shootScript.accessoryAdjustments : arrayValue<Record<string, unknown>>(script, 'accessory_adjustments')
  return (
    <div className="styling-plan shoot-script-plan">
      <div className="styling-plan-head">
        <div>
          <span>拍摄剧本</span>
          <h3>{stringValue(summary.title) || 'Lily 电商棚拍拍摄剧本'}</h3>
        </div>
        <div className="score-strip script-strip">
          <span><b>{shots.length || 6}</b>镜头</span>
          <span><b>{risks.length}</b>风险</span>
          <span><b>{adjustments.length}</b>调整</span>
        </div>
      </div>
      <p>{stringValue(summary.summary) || '本组拍摄以主推款商品表达为核心，覆盖完整比例、品类核心、设计细节、动态补充和标准正面图。'}</p>
      <div className="styling-plan-grid script-directing-grid">
        <InfoBlock title="品牌调性" items={[stringValue(directing.brand_tone)]} />
        <InfoBlock title="背景光线" items={[stringValue(directing.background), stringValue(directing.lighting)].filter(Boolean)} />
        <InfoBlock title="模特导演" items={[stringValue(directing.model_direction), stringValue(directing.hair_makeup_boundary)].filter(Boolean)} />
        <InfoBlock title="商品保真" items={stringList(directing.product_fidelity_rules)} />
      </div>
      {risks.length > 0 && (
        <div className="rejected-list">
          <strong>选搭风险与建议</strong>
          {risks.map((item, index) => (
            <span key={`${stringValue(item.risk_id)}-${index}`}>{[stringValue(item.severity), stringValue(item.risk), stringValue(item.user_visible_recommendation)].filter(Boolean).join(' · ')}</span>
          ))}
        </div>
      )}
      {adjustments.length > 0 && (
        <div className="rejected-list">
          <strong>生成配饰调整</strong>
          {adjustments.map((item, index) => (
            <span key={`${stringValue(item.item_type)}-${index}`}>{[stringValue(item.item_type), stringValue(item.adjusted_direction), stringValue(item.reason)].filter(Boolean).join(' · ')}</span>
          ))}
        </div>
      )}
      <div className="shot-card-grid">
        {shots.map((shot, index) => (
          <div className="shot-card" key={`${numberValue(shot.shot_index) || index + 1}-${stringValue(shot.shot_role)}`}>
            <div><span>#{numberValue(shot.shot_index) || index + 1}</span><strong>{stringValue(shot.shot_role_label) || stringValue(shot.shot_role) || '镜头'}</strong></div>
            <p>{stringValue(shot.main_focus) || stringValue(shot.mainFocus)}</p>
            <dl>
              <dt>景别</dt><dd>{stringValue(shot.framing) || '未指定'}</dd>
              <dt>取景</dt><dd>{stringValue(shot.crop_range) || stringValue(shot.cropRange) || '未指定'}</dd>
              <dt>姿势</dt><dd>{stringValue(shot.pose) || '未指定'}</dd>
              <dt>走位</dt><dd>{stringValue(shot.movement) || stringValue(shot.body_position) || stringValue(shot.bodyPosition) || '未指定'}</dd>
            </dl>
          </div>
        ))}
      </div>
    </div>
  )
}

function shootScriptShots(task: GenerationTask): Record<string, unknown>[] {
  const scriptShots = arrayValue<Record<string, unknown>>(task.shootScript?.script ?? {}, 'shots')
  if (scriptShots.length) return scriptShots
  return (task.shotPlans ?? []).map((item) => ({ shot_index: item.sortOrder, shot_role: item.shotType, ...(item.plan ?? {}) } as Record<string, unknown>))
}

function PreviewStep({ task, setTask, run, localOperations, startLocalOperation, clearLocalOperationsFor }: {
  task: GenerationTask | null
  setTask: (task: GenerationTask) => void
  run: any
  localOperations: AIOperation[]
  startLocalOperation: (operation: Omit<AIOperation, 'id' | 'status' | 'startedAt'> & { status?: string; startedAt?: string | null }) => AIOperation
  clearLocalOperationsFor: (ownerTypes: string[]) => void
}) {
  const [viewer, setViewer] = useState<LightboxImage | null>(null)
  const preview = task?.storyboardPreview ?? task?.preview ?? null
  const canPreview = (
    !!task?.shootScript && ['SCRIPT_READY', 'PREVIEW_READY', 'COMPLETED', 'FINAL_PARTIAL', 'CANCELLED'].includes(task?.status ?? '')
  ) || (task?.status === 'FAILED' && !!task.shootScript && !preview?.asset)
  const hasPreview = !!preview?.asset
  const operations = task ? operationFallbacks(task, localOperations, 'generation_preview') : []
  return (
    <div className="step-panel">
      <div className="step-copy"><h2>生成分镜预览</h2><p>按拍摄剧本生成一张 3x2 分镜预览图。最终固定生成前 5 个镜头，第 6 个镜头忽略。</p></div>
      <AIOperationList operations={operations} />
      {preview?.asset ? (
        <button className="preview-board-open" onClick={() => preview.asset && setViewer(assetLightboxImage(preview.asset))} title="放大查看">
          <img className="preview-board" src={mediaUrl(assetPreviewSrc(preview.asset))} />
        </button>
      ) : <EmptyState title="尚未生成分镜预览" text="点击生成预览后，系统会根据拍摄剧本输出一张 3x2 分镜预览图。" />}
      <button className="primary" disabled={!canPreview} onClick={() => task && void run(hasPreview ? '重新生成分镜预览中...' : '生成分镜预览中...', () => {
        startLocalOperation({ ownerType: 'generation_preview', ownerId: task.id, provider: 'image-2' })
        setTask({ ...task, status: 'PREVIEW_RUNNING', resultSet: null, activeResultSetId: null })
        return api.previewTask(task.id)
      }, (r: { item: GenerationTask }) => {
        clearLocalOperationsFor(['generation_preview'])
        setTask(r.item)
      }, { refresh: false })}>
        <GalleryVerticalEnd size={16} /> {hasPreview ? '重新生成分镜预览' : '生成预览'}
      </button>
      {viewer && <ImageLightbox image={viewer} onClose={() => setViewer(null)} />}
    </div>
  )
}

function FinalStep({ task, setTask, run, localOperations, startLocalOperation, clearLocalOperationsFor }: {
  task: GenerationTask | null
  setTask: (task: GenerationTask) => void
  run: any
  localOperations: AIOperation[]
  startLocalOperation: (operation: Omit<AIOperation, 'id' | 'status' | 'startedAt'> & { status?: string; startedAt?: string | null }) => AIOperation
  clearLocalOperationsFor: (ownerTypes: string[]) => void
}) {
  const [viewer, setViewer] = useState<LightboxImage | null>(null)
  const images = task?.resultSet?.images ?? []
  const finalAssets = images.map((image) => image.asset).filter((asset): asset is MediaAsset => !!asset)
  const preview = task?.storyboardPreview ?? task?.preview ?? null
  const canGenerate = !!preview?.asset && ['PREVIEW_READY', 'COMPLETED', 'FINAL_PARTIAL', 'CANCELLED'].includes(task?.status ?? '')
  const hasFinalImages = images.some((image) => !!image.asset)
  const finalOperations = task ? finalImageOperations(task, localOperations) : []
  return (
    <div className="step-panel">
      <div className="step-copy"><h2>生成成片</h2><p>并发生成前 5 个镜头。每张图可单独下载，也可单张重新生成。</p></div>
      <AIOperationList operations={finalOperations} />
      <div className="result-grid">
        {images.length ? images.map((image) => (
          <div className="result-card" key={image.id}>
            {image.asset ? (
              <button className="result-image-open" onClick={() => image.asset && setViewer(assetLightboxImage(image.asset, finalAssets))} title="放大查看">
                <img src={mediaUrl(assetPreviewSrc(image.asset))} />
              </button>
            ) : <div className="image-placeholder">{statusText(image.status)}{image.activeAttempt && isAttemptRunning(image.activeAttempt) && <small>{formatDuration(elapsedSeconds(image.activeAttempt.startedAt || image.updatedAt))}</small>}</div>}
            <div><span>#{image.sortOrder}</span>{image.asset && <a href={mediaUrl(assetOriginalSrc(image.asset))} target="_blank" rel="noreferrer"><Download size={15} /></a>}{!['QUEUED', 'RUNNING'].includes(image.status) && <button onClick={() => void run('单张重新生成中...', () => api.retryResultImage(image.id), (r: { item: GenerationTask }) => setTask(r.item), { refresh: 'background' })} title="重新生成"><RotateCcw size={14} /></button>}</div>
          </div>
        )) : <EmptyState title="尚未生成最终图" text="点击生成成片后会生成前 5 个镜头。" />}
      </div>
      <button className="primary" disabled={!canGenerate} onClick={() => task && void run(hasFinalImages ? '重新生成最终成片中...' : '生成最终成片中...', () => {
        for (let index = 1; index <= 5; index += 1) {
          startLocalOperation({ ownerType: 'generation_result_image', ownerId: index, provider: 'image-2' })
        }
        setTask({ ...task, status: 'FINAL_RUNNING' })
        return api.generateFinal(task.id)
      }, (r: { item: GenerationTask }) => {
        clearLocalOperationsFor(['generation_result_image'])
        setTask(r.item)
      }, { refresh: false })}>
        <Sparkles size={16} /> {hasFinalImages ? '重新生成成片' : '生成成片'}
      </button>
      {viewer && <ImageLightbox image={viewer} onClose={() => setViewer(null)} />}
    </div>
  )
}

function LibraryPage({ garments, run }: { garments: StylingGarment[]; run: any }) {
  const [query, setQuery] = useState('')
  const [detail, setDetail] = useState<StylingGarment | null>(null)
  const [items, setItems] = useState<StylingGarment[]>(garments)
  const [showImport, setShowImport] = useState(false)
  const [offset, setOffset] = useState(0)
  const [total, setTotal] = useState(0)
  const [totalPages, setTotalPages] = useState(0)
  const [pageSize, setPageSize] = useState(12)
  const [searching, setSearching] = useState(false)
  const [includeArchived, setIncludeArchived] = useState(false)
  const [selectedGarmentIds, setSelectedGarmentIds] = useState<Set<number>>(() => new Set())
  const [deleteGarmentDialog, setDeleteGarmentDialog] = useState<StylingGarment[] | null>(null)
  const [deleteGarmentImpact, setDeleteGarmentImpact] = useState<DeleteImpact | null>(null)
  const [deleteGarmentImpactLoading, setDeleteGarmentImpactLoading] = useState(false)
  const selectableGarmentIds = items.filter((item) => item.status !== 'TAGGING_RUNNING').map((item) => item.id)
  const selectedVisibleGarments = items.filter((item) => selectedGarmentIds.has(item.id) && selectableGarmentIds.includes(item.id))
  const allVisibleGarmentsSelected = selectableGarmentIds.length > 0 && selectableGarmentIds.every((id) => selectedGarmentIds.has(id))

  async function openDetail(item: StylingGarment) {
    const result = await api.getStylingGarment(item.id)
    updateGarmentInView(result.item)
    setDetail(result.item)
  }

  function refreshLibraryPage(targetOffset = offset) {
    const keyword = query.trim()
    return api.listStylingGarments({ limit: pageSize, offset: targetOffset, keyword: keyword || undefined, includeArchived }).then((result) => {
      setItems(result.items)
      setTotal(result.total)
      setTotalPages(result.totalPages)
    })
  }

  function refreshFirstLibraryPage() {
    setOffset(0)
    return refreshLibraryPage(0)
  }

  useEffect(() => {
    setOffset(0)
  }, [query])

  useEffect(() => {
    const keyword = query.trim()
    let cancelled = false
    setSearching(true)
    const timer = window.setTimeout(() => {
      void api.listStylingGarments({ limit: pageSize, offset, keyword: keyword || undefined, includeArchived }).then((result) => {
        if (!cancelled) {
          setItems(result.items)
          setTotal(result.total)
          setTotalPages(result.totalPages)
        }
      }).catch(() => undefined).finally(() => {
        if (!cancelled) setSearching(false)
      })
    }, 250)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [garments, offset, query, includeArchived, pageSize])

  useEffect(() => setSelectedGarmentIds(new Set()), [offset, includeArchived, query, pageSize])

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
    void run(purge ? '彻底删除搭配服饰中...' : '归档搭配服饰中...', action, () => {
      if (detail && ids.includes(detail.id)) setDetail(null)
      setSelectedGarmentIds(new Set())
      void refreshLibraryPage().catch(() => undefined)
    }, { refresh: 'background' })
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

  return (
    <section className="page">
      <div className="library-title-row">
        <PageTitle title="搭配服饰库" subtitle="真实会出现在最终图里的配角服装。当前不支持鞋、包、饰品入库。" />
        <button className="primary" onClick={() => setShowImport((value) => !value)}>
          <ImagePlus size={16} /> {showImport ? '收起导入' : '导入搭配服饰'}
        </button>
      </div>
      {showImport && <BatchImportPanel onImported={() => void refreshFirstLibraryPage().catch(() => undefined)} />}
      <div className="panel library-panel">
        <div className="toolbar">
          <div className={`search ${searching ? 'is-searching' : ''}`}>
            <Search size={16} />
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索货号 / 标签" />
            {searching && <span className="search-hint"><Loader2 size={14} className="spin" />搜索中</span>}
          </div>
          <div className="archive-controls">
            <label><input type="checkbox" checked={includeArchived} onChange={(event) => {
              setOffset(0)
              setIncludeArchived(event.target.checked)
            }} /> 查看已归档</label>
            <label><input type="checkbox" checked={allVisibleGarmentsSelected} disabled={!selectableGarmentIds.length} onChange={(event) => toggleVisibleGarmentSelection(event.target.checked)} /> 全选本页</label>
          </div>
          <div className="record-list-meta">
            {selectedVisibleGarments.length > 0 && (
              <button className="secondary" onClick={() => openGarmentDeleteDialog(selectedVisibleGarments)}>
                归档 {selectedVisibleGarments.length} 项
              </button>
            )}
            <span className="muted-count">{total ? `${offset + 1}-${Math.min(offset + items.length, total)} 套` : '0 套'}</span>
          </div>
        </div>
        <div className={`library-results ${searching ? 'is-loading' : ''}`}>
        {items.length ? (
          <div className="garment-grid">{items.map((item) => (
            <GarmentCard
              key={item.id}
              item={item}
              onOpen={() => void openDetail(item)}
              onTag={['DRAFT', 'TAGGING_FAILED', 'TAGGING_READY'].includes(item.status) ? () => void run('重新识别标签中...', () => api.tagStylingGarment(item.id), (r: { item: StylingGarment }) => updateGarmentInView(r.item), { refresh: 'background' }) : undefined}
              selected={selectedGarmentIds.has(item.id)}
              onSelect={(checked) => toggleGarmentSelection(item.id, checked)}
              onDelete={() => openGarmentDeleteDialog([item])}
            />
          ))}</div>
        ) : <EmptyState title={query ? '没有匹配的搭配服饰' : '暂无搭配服饰'} text={query ? '换一个货号或标签关键词试试。' : '点击右上角导入搭配服饰。'} />}
        </div>
        <PaginationControls
          disabled={searching}
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
      {detail && <GarmentDetailDrawer item={detail} run={run} onClose={() => setDetail(null)} onChange={updateGarmentInView} onDelete={() => openGarmentDeleteDialog([detail])} />}
      {deleteGarmentDialog && (
        <DeleteConfirmDialog
          title={deleteGarmentDialog.length === 1 ? '归档搭配服饰' : `归档 ${deleteGarmentDialog.length} 套搭配服饰`}
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

type ImportBatch = {
  id: string
  code: string
  files: File[]
  error?: string
}

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
  const [batches, setBatches] = useState<ImportBatch[]>([])
  const [localError, setLocalError] = useState('')
  const [importJobs, setImportJobs] = useState<ImportJob[]>([])

  function addFiles(files: File[]) {
    const next = groupImportFiles(files)
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
    const ready: ImportBatch[] = []
    const blocked: ImportBatch[] = []
    batches.forEach((batch) => {
      const code = sanitizeClientName(batch.code)
      if (!code) {
        blocked.push({ ...batch, error: '每套搭配服饰都需要一个有效货号' })
        return
      }
      if (batch.files.length > 6) {
        blocked.push({ ...batch, code, error: '超过 6 张，需删除后才能导入' })
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
      setLocalError(blocked.length ? '没有可导入的新服饰，请先处理标红项' : '请先选择图片或文件夹')
      return
    }
    setLocalError(blocked.length ? '已开始导入非重复项，标红项处理后可再次导入' : '')
    startImportJob(ready)
  }

  function updateJob(jobId: string, updater: (job: ImportJob) => ImportJob) {
    setImportJobs((jobs) => jobs.map((job) => job.id === jobId ? updater(job) : job))
  }

  function addSkippedBatch(batch: ImportBatch, error: string) {
    setBatches((items) => {
      if (items.some((item) => item.id === batch.id)) return items
      return [...items, { ...batch, error }]
    })
  }

  function startImportJob(jobBatches: ImportBatch[]) {
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
      title: `${jobBatches.length} 套搭配服饰`,
      status: 'RUNNING',
      total: jobBatches.length,
      uploaded: 0,
      skipped: 0,
      operations: initialOperations,
    }
    setImportJobs((jobs) => [nextJob, ...jobs].slice(0, 5))
    void runImportJob(jobId, jobBatches)
  }

  async function runImportJob(jobId: string, jobBatches: ImportBatch[]) {
    const createdItems: StylingGarment[] = []
    let skipped = 0
    for (const batch of jobBatches) {
      updateJob(jobId, (job) => ({
        ...job,
        operations: job.operations.map((item) => item.providerRequestId === batch.code ? { ...item, status: 'RUNNING', startedAt: item.startedAt || new Date().toISOString() } : item),
      }))
      try {
        const created = await api.createStylingGarment(batch.code, batch.files)
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
        addSkippedBatch(batch, message === '搭配服饰代码已存在' ? '代码已存在，已跳过' : message)
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
          <h3>导入搭配服饰</h3>
          <p>散图会合并为一套；单文件夹是一套；拖入多个一级文件夹会拆成多套。导入后系统会自动识别标签，单套最多 6 张。</p>
        </div>
        <button className="primary" disabled={!batches.length} onClick={saveDrafts}>导入</button>
      </div>
      <div className="import-drop" onDragOver={(event) => event.preventDefault()} onDrop={(event) => void handleDrop(event)}>
        <Images size={22} />
        <strong>拖入图片或文件夹</strong>
        <span>支持多张图片、一个文件夹、多个一级文件夹</span>
        <div className="import-actions">
          <label className="secondary">
            <ImagePlus size={16} /> 选择图片
            <input type="file" accept="image/png,image/jpeg,image/webp" multiple onChange={(event) => addFiles(Array.from(event.target.files ?? []))} />
          </label>
          <label className="secondary">
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
  batch: ImportBatch
  onCode: (code: string) => void
  onFiles: (files: File[]) => void
  onRemove: () => void
}) {
  return (
    <div className={`batch-card ${batch.files.length > 6 || batch.error ? 'has-error' : ''}`}>
      <div className="batch-card-head">
        <div className="batch-icon"><Layers3 size={18} /></div>
        <input value={batch.code} onChange={(event) => onCode(event.target.value)} aria-label="搭配服饰货号" />
        <button className="secondary icon-button" onClick={onRemove} title="移除整套"><Trash2 size={16} /></button>
      </div>
      <div className="batch-note">
        <span>{batch.files.length} 张图片</span>
        {(batch.error || batch.files.length > 6) && <strong>{batch.error ?? '超过 6 张，需删除后才能导入'}</strong>}
      </div>
      <FileThumbList files={batch.files} onChange={onFiles} />
    </div>
  )
}

function FileThumbList({ files, onChange }: { files: File[]; onChange: (files: File[]) => void }) {
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
      {files.map((file, index) => (
        <div
          className="file-thumb"
          key={`${file.name}-${file.size}-${index}`}
          draggable
          onDragStart={() => setDragIndex(index)}
          onDragOver={(event) => event.preventDefault()}
          onDrop={() => {
            if (dragIndex !== null) move(dragIndex, index)
            setDragIndex(null)
          }}
        >
          <FilePreviewButton
            file={file}
            dimensions={dimensions[fileKey(file)]}
            onDimensions={(value) => setDimensions((current) => ({ ...current, [fileKey(file)]: value }))}
            onOpen={(image) => setViewer(image)}
          />
          <span>{file.name}<small>{fileMeta(file, dimensions[fileKey(file)])}</small></span>
          <div className="thumb-actions">
            <FilePreviewAction file={file} dimensions={dimensions[fileKey(file)]} onOpen={(image) => setViewer(image)} />
            <button onClick={() => move(index, index - 1)} title="前移"><ArrowUp size={14} /></button>
            <button onClick={() => move(index, index + 1)} title="后移"><ArrowDown size={14} /></button>
            <label title="替换图片">
              <ImagePlus size={14} />
              <input type="file" accept="image/png,image/jpeg,image/webp" onChange={(event) => {
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
      ))}
      <label className="file-thumb file-thumb-add" title="新增图片">
        <div className="add-icon-wrapper">
          <ImagePlus size={24} />
        </div>
        <span>新增图片</span>
        <input type="file" accept="image/png,image/jpeg,image/webp" multiple onChange={(event) => {
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

function FilePreviewAction({ file, dimensions, onOpen }: {
  file: File
  dimensions?: { width: number; height: number }
  onOpen: (image: LightboxImage) => void
}) {
  const url = useObjectUrl(file)
  return <button onClick={() => onOpen(fileLightboxImage(file, url, dimensions))} title="放大查看"><Eye size={14} /></button>
}

function ModelPage({ models }: { models: ModelProfile[] }) {
  const model = models.find((item) => item.isDefault) ?? models[0]
  const refs = model?.profile?.headReferenceEntries ?? []
  return (
    <section className="page">
      <PageTitle title="数字模特" subtitle="当前只读展示默认模特的多角度头部参考图，没有身体图。" />
      <div className="panel model-page">
        <div className="model-meta">
          <span className="chip">只读</span>
          <h2>{model?.name ?? 'Face-01 默认模特'}</h2>
          <Meta label="发型/气质" value={model?.profile?.notes ?? '短发、自然淡妆、克制通勤气质'} />
          <Meta label="身型参数" value={`${model?.profile?.height ?? '170cm'} · ${model?.profile?.bodyShape ?? 'H型'}`} />
        </div>
        <div className="head-grid">
          {(refs.length ? refs : fallbackHeadRefs()).map((ref) => (
            <div className="head-card" key={ref.id}>
              {ref.previewUrl ? <img src={mediaUrl(ref.previewUrl)} /> : <div className="head-placeholder"><UserRound /></div>}
              <span>{angleLabel(ref.angle ?? ref.id)}</span>
            </div>
          ))}
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

  const activeItem = items.find((item) => item.key === activeKey) || items[0]
  const dirty = Boolean(activeItem && draft !== activeItem.content)
  const segments = items.filter((item) => item.groupType === 'segment')
  const assemblies = items.filter((item) => item.groupType === 'assembly')

  function selectItem(key: string) {
    const next = items.find((item) => item.key === key)
    if (!next) return
    setActiveKey(key)
    setDraft(next.content)
    setRendered(next.renderedPreview)
    setMissing(next.missingPlaceholders)
    setUsedSegments(next.usedSegments)
  }

  function refreshPreview() {
    if (!activeItem) return
    void run('渲染 Prompt 预览中...', () => api.previewPromptConfig(activeItem.key), (result: { rendered: string; diagnostics: { missingPlaceholders: string[]; usedSegments: string[] } }) => {
      setRendered(result.rendered)
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

  function reset() {
    if (!activeItem || !window.confirm(`恢复「${activeItem.title}」为系统默认 Prompt？`)) return
    void run('恢复默认 Prompt 中...', () => api.resetPromptConfig(activeItem.key), (result: { item: PromptConfig; preview: { rendered: string; diagnostics: { missingPlaceholders: string[]; usedSegments: string[] } } }) => {
      setDraft(result.item.content)
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
    <section className="page">
      <PageTitle
        title="Prompt 维护"
        subtitle="维护分段 Prompt 和各生成步骤的组装模板；预览只做文本渲染，不触发 AI 额度。"
        actions={<div className="toolbar"><span className="chip">Admin</span><button className="secondary" onClick={refreshPreview} disabled={!activeItem}><Eye size={16} />预览</button><button className="secondary" onClick={reset} disabled={!activeItem}><RotateCcw size={16} />恢复默认</button><button className="primary" onClick={save} disabled={!activeItem || !dirty}><Save size={16} />保存</button></div>}
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
                  <h2>{activeItem.title}</h2>
                  <p>{activeItem.description}</p>
                </div>
                <div className="prompt-status">
                  <span className={dirty ? 'status-badge warning' : 'status-badge ok'}>{dirty ? '未保存' : activeItem.isCustomized ? '已自定义' : '系统默认'}</span>
                  {activeItem.updatedAt && <small>更新于 {dateText(activeItem.updatedAt)}</small>}
                </div>
              </div>
              <textarea className="prompt-editor-textarea" value={draft} onChange={(event) => setDraft(event.target.value)} spellCheck={false} />
            </>
          ) : <EmptyState title="暂无 Prompt 配置" text="后端还没有返回可维护的 Prompt 配置。" />}
        </section>
        <aside className="panel prompt-preview-panel">
          <div className="prompt-side-section">
            <h3>预览</h3>
            <div className="prompt-rendered-section"><pre>{rendered}</pre></div>
          </div>
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
          <strong>{item.title}</strong>
          <span>{item.isCustomized ? '已自定义' : '系统默认'}{item.missingPlaceholders.length ? ` · 缺失 ${item.missingPlaceholders.length}` : ''}</span>
        </button>
      ))}
    </div>
  )
}

function BrandInfoPage({ initialBrandInfo, setBrandInfo, run }: {
  initialBrandInfo: BrandInfo | null
  setBrandInfo: (item: BrandInfo | null) => void
  run: <T>(label: string, action: () => Promise<T>, after?: (value: T) => void, options?: RunOptions) => Promise<T | undefined>
}) {
  const [jsonDialog, setJsonDialog] = useState<{ mode: 'import' | 'export'; text: string; error?: string } | null>(null)
  const [brandId, setBrandId] = useState<number | null>(initialBrandInfo?.id ?? null)
  const [name, setName] = useState(initialBrandInfo?.name ?? '')
  const [tagline, setTagline] = useState(initialBrandInfo?.tagline ?? '')
  const [brandCore, setBrandCore] = useState(initialBrandInfo?.brandCore ?? '')
  const [styleDna, setStyleDna] = useState(initialBrandInfo?.styleDna ?? '')
  const [stylingGuidelines, setStylingGuidelines] = useState(initialBrandInfo?.stylingGuidelines ?? '')
  const [shootingGuidelines, setShootingGuidelines] = useState(initialBrandInfo?.shootingGuidelines ?? '')
  const [negativeConstraints, setNegativeConstraints] = useState(initialBrandInfo?.negativeConstraints ?? '')

  useEffect(() => {
    setBrandId(initialBrandInfo?.id ?? null)
    setName(initialBrandInfo?.name ?? '')
    setTagline(initialBrandInfo?.tagline ?? '')
    setBrandCore(initialBrandInfo?.brandCore ?? '')
    setStyleDna(initialBrandInfo?.styleDna ?? '')
    setStylingGuidelines(initialBrandInfo?.stylingGuidelines ?? '')
    setShootingGuidelines(initialBrandInfo?.shootingGuidelines ?? '')
    setNegativeConstraints(initialBrandInfo?.negativeConstraints ?? '')
  }, [initialBrandInfo?.id, initialBrandInfo?.updatedAt])

  const cleanBrandCore = brandCore.trim()
  const cleanStyleDna = styleDna.trim()
  const cleanStylingGuidelines = stylingGuidelines.trim()
  const cleanShootingGuidelines = shootingGuidelines.trim()
  const cleanNegativeConstraints = negativeConstraints.trim()
  const hasContent = Boolean(name.trim() || tagline.trim() || cleanBrandCore || cleanStyleDna || cleanStylingGuidelines || cleanShootingGuidelines || cleanNegativeConstraints)
  const summaryName = name.trim() || '未填写品牌名称'

  function payload() {
    return {
      name: name.trim(),
      tagline: tagline.trim(),
      brandCore: cleanBrandCore,
      styleDna: cleanStyleDna,
      stylingGuidelines: cleanStylingGuidelines,
      shootingGuidelines: cleanShootingGuidelines,
      negativeConstraints: cleanNegativeConstraints,
    }
  }

  function applyPayload(next: BrandInfoPayload) {
    setName(next.name ?? '')
    setTagline(next.tagline ?? '')
    setBrandCore(next.brandCore ?? '')
    setStyleDna(next.styleDna ?? '')
    setStylingGuidelines(next.stylingGuidelines ?? '')
    setShootingGuidelines(next.shootingGuidelines ?? '')
    setNegativeConstraints(next.negativeConstraints ?? '')
  }

  function openExportJson() {
    setJsonDialog({ mode: 'export', text: `${JSON.stringify(payload(), null, 2)}\n` })
  }

  function openImportJson() {
    if (hasContent && !window.confirm('导入 JSON 会覆盖当前表单内容，是否继续？')) return
    setJsonDialog({ mode: 'import', text: '' })
  }

  function confirmImportJson() {
    if (!jsonDialog || jsonDialog.mode !== 'import') return
    if (jsonDialog.text.length > 64 * 1024) {
      setJsonDialog({ ...jsonDialog, error: 'JSON 内容过大，请控制在 64KB 以内。' })
      return
    }
    try {
      const data = parseBrandInfoJson(jsonDialog.text)
      applyPayload(data)
      setJsonDialog(null)
      window.alert('已导入到表单，请检查后保存。')
    } catch (error) {
      setJsonDialog({ ...jsonDialog, error: error instanceof Error ? error.message : 'JSON 导入失败。' })
    }
  }

  function save() {
    const action = brandId ? () => api.updateBrandInfo(payload()) : () => api.createBrandInfo(payload())
    void run(brandId ? '保存品牌信息中...' : '创建品牌信息中...', action, (result: { item: BrandInfo }) => {
      setBrandInfo(result.item)
      setBrandId(result.item.id)
    }, { refresh: false })
  }

  function remove() {
    if (!brandId) return
    if (!window.confirm('确定删除当前品牌信息吗？删除后页面会回到空表单，后续新任务将无法读取这份品牌资料。')) return
    void run('删除品牌信息中...', () => api.deleteBrandInfo(), () => {
      setBrandInfo(null)
      setBrandId(null)
      setName('')
      setTagline('')
      setBrandCore('')
      setStyleDna('')
      setStylingGuidelines('')
      setShootingGuidelines('')
      setNegativeConstraints('')
    }, { refresh: false })
  }

  return (
    <section className="page">
      <PageTitle
        title="品牌信息"
        subtitle="维护用于 AI Stylist 和棚拍生成的单一品牌资料。"
        actions={<div className="toolbar"><span className="chip">Admin</span><button className="secondary" onClick={openImportJson}>导入</button><button className="secondary" onClick={openExportJson}>导出</button><button className="secondary" onClick={remove} disabled={!brandId}><Trash2 size={16} />删除</button><button className="primary" onClick={save}><Save size={16} />保存</button></div>}
      />
      {jsonDialog && (
        <BrandJsonDialog
          dialog={jsonDialog}
          setDialog={setJsonDialog}
          onClose={() => setJsonDialog(null)}
          onImport={confirmImportJson}
        />
      )}
      <div className="panel brand-info-page">
        <aside className="brand-info-meta">
          <span className="chip">{brandId ? '单一品牌资料' : '空白草稿'}</span>
          <h2>{summaryName}</h2>
          <Meta label="状态" value={brandId ? '已保存' : hasContent ? '未保存' : '初始为空'} />
          <Meta label="标语" value={tagline.trim() ? '已填写' : '未填写'} />
          <Meta label="品牌核心" value={cleanBrandCore ? '已填写' : '未填写'} />
          <Meta label="风格 DNA" value={cleanStyleDna ? '已填写' : '未填写'} />
          <Meta label="负向约束" value={cleanNegativeConstraints ? '已填写' : '未填写'} />
          <p>保存后将作为后续选搭与 ShotPlan 的品牌参考。</p>
        </aside>
        <div className="brand-info-form">
          <div className="brand-field">
            <label>品牌名称</label>
            <input value={name} onChange={(event) => setName(event.target.value)} />
          </div>
          <div className="brand-field">
            <label>品牌标语</label>
            <textarea value={tagline} onChange={(event) => setTagline(event.target.value)} />
          </div>
          <div className="brand-field">
            <label>品牌核心</label>
            <textarea value={brandCore} onChange={(event) => setBrandCore(event.target.value)} />
          </div>
          <div className="brand-field">
            <label>风格 DNA</label>
            <textarea value={styleDna} onChange={(event) => setStyleDna(event.target.value)} />
          </div>
          <div className="brand-field">
            <label>选搭准则</label>
            <textarea value={stylingGuidelines} onChange={(event) => setStylingGuidelines(event.target.value)} />
          </div>
          <div className="brand-field">
            <label>拍摄准则</label>
            <textarea value={shootingGuidelines} onChange={(event) => setShootingGuidelines(event.target.value)} />
          </div>
          <div className="brand-field">
            <label>负向约束</label>
            <textarea className="large" value={negativeConstraints} onChange={(event) => setNegativeConstraints(event.target.value)} />
          </div>
        </div>
      </div>
    </section>
  )
}

const BRAND_INFO_JSON_FIELDS = [
  'name',
  'tagline',
  'brandCore',
  'styleDna',
  'stylingGuidelines',
  'shootingGuidelines',
  'negativeConstraints',
] as const

function parseBrandInfoJson(text: string): BrandInfoPayload {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error('JSON 格式不正确。')
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('JSON 内容必须是对象。')
  }
  const source = parsed as Record<string, unknown>
  const result: BrandInfoPayload = {}
  for (const field of BRAND_INFO_JSON_FIELDS) {
    const value = source[field]
    if (value == null) {
      result[field] = ''
    } else if (typeof value === 'string') {
      result[field] = value
    } else {
      throw new Error(`${field} 必须是字符串。`)
    }
  }
  return result
}

function BrandJsonDialog({ dialog, setDialog, onClose, onImport }: {
  dialog: { mode: 'import' | 'export'; text: string; error?: string }
  setDialog: (dialog: { mode: 'import' | 'export'; text: string; error?: string }) => void
  onClose: () => void
  onImport: () => void
}) {
  const isImport = dialog.mode === 'import'
  return createPortal(
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <form className="confirm-modal brand-json-modal" role="dialog" aria-modal="true" aria-label={isImport ? '导入品牌信息' : '导出品牌信息'} onSubmit={(event) => {
        event.preventDefault()
        if (isImport) onImport()
      }} onClick={(event) => event.stopPropagation()}>
        <div className="confirm-head">
          <div><h2>{isImport ? '导入' : '导出'}</h2></div>
          <button type="button" className="ghost-icon" onClick={onClose} title="关闭"><X size={18} /></button>
        </div>
        <textarea
          readOnly={!isImport}
          value={dialog.text}
          onChange={(event) => setDialog({ mode: dialog.mode, text: event.target.value })}
        />
        {dialog.error && <p className="json-error">{dialog.error}</p>}
        <div className="confirm-actions">
          <button type="button" className="secondary" onClick={onClose}>关闭</button>
          {isImport && <button type="submit" className="primary">导入到表单</button>}
        </div>
      </form>
    </div>,
    document.body,
  )
}

function ShootConfigPage({ run }: { run: any }) {
  const [config, setConfig] = useState<ShootConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [categoryToAdd, setCategoryToAdd] = useState('')
  const [jsonDialog, setJsonDialog] = useState<{ mode: 'import' | 'export'; text: string; error?: string } | null>(null)
  const [promptDialog, setPromptDialog] = useState<{ text: string } | null>(null)

  const categoryOptions = useMemo(
    () => [...(config?.categoryOptions ?? [])].sort((a, b) => (a.sortOrder - b.sortOrder) || (a.id - b.id)),
    [config?.categoryOptions],
  )
  const configuredCategoryKeys = useMemo(
    () => new Set((config?.categoryShotRules ?? []).map((rule) => `${rule.categoryDimensionCode}:${rule.categoryTagCode}`)),
    [config?.categoryShotRules],
  )
  const configurableCategoryOptions = categoryOptions.filter((option) => !NON_SHOOT_CONFIG_CATEGORY_CODES.has(option.code))
  const addableCategoryOptions = configurableCategoryOptions.filter((option) => option.isActive && !configuredCategoryKeys.has(`${option.dimensionCode}:${option.code}`))
  const enabledGlobalRuleCount = config?.globalRules.filter((rule) => rule.enabled).length ?? 0
  const activeCategoryRuleCount = (config?.categoryShotRules ?? []).filter((rule) => {
    const option = findShootCategoryOption(categoryOptions, rule.categoryDimensionCode, rule.categoryTagCode)
    return option?.isActive
  }).length

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    void api.getShootConfig()
      .then((result) => {
        if (!cancelled) setConfig(result.item)
      })
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => { cancelled = true }
  }, [])

  function updateGlobalRule(index: number, field: keyof ShootConfigGlobalRule, value: string | boolean | number) {
    setConfig((current) => {
      if (!current) return current
      const globalRules = [...current.globalRules]
      globalRules[index] = { ...globalRules[index], [field]: value }
      return { ...current, globalRules }
    })
  }

  function addGlobalRule() {
    setConfig((current) => {
      if (!current) return current
      const nextSort = Math.max(0, ...current.globalRules.map((rule) => Number(rule.sortOrder || 0))) + 10
      return {
        ...current,
        globalRules: [
          ...current.globalRules,
          { id: `rule_${Date.now().toString(36)}`, label: '新导演规则', value: '', enabled: true, sortOrder: nextSort },
        ],
      }
    })
  }

  function removeGlobalRule(index: number) {
    setConfig((current) => current ? { ...current, globalRules: current.globalRules.filter((_, ruleIndex) => ruleIndex !== index) } : current)
  }

  function updateDefaultShotRule(index: number, field: keyof ShootConfigShotRule, value: string) {
    setConfig((current) => {
      if (!current) return current
      const shots = [...current.defaultShotRule.shots]
      shots[index] = { ...shots[index], [field]: value }
      return { ...current, defaultShotRule: { shots } }
    })
  }

  function updateCategoryShotRule(ruleIndex: number, shotIndex: number, field: keyof ShootConfigShotRule, value: string) {
    setConfig((current) => {
      if (!current) return current
      const categoryShotRules = [...current.categoryShotRules]
      const target = categoryShotRules[ruleIndex]
      const shots = [...target.shots]
      shots[shotIndex] = { ...shots[shotIndex], [field]: value }
      categoryShotRules[ruleIndex] = { ...target, shots }
      return { ...current, categoryShotRules }
    })
  }

  function addCategoryRule() {
    if (!addableCategoryOptions.length) return
    const selectedCode = categoryToAdd || addableCategoryOptions[0].code
    const option = addableCategoryOptions.find((item) => item.code === selectedCode) ?? addableCategoryOptions[0]
    setConfig((current) => {
      if (!current) return current
      return {
        ...current,
        categoryShotRules: [
          ...current.categoryShotRules,
          {
            categoryDimensionCode: option.dimensionCode,
            categoryTagCode: option.code,
            shots: cloneShootConfigShots(current.defaultShotRule.shots),
          },
        ],
      }
    })
    setCategoryToAdd('')
  }

  function removeCategoryRule(index: number) {
    setConfig((current) => current ? { ...current, categoryShotRules: current.categoryShotRules.filter((_, ruleIndex) => ruleIndex !== index) } : current)
  }

  function save() {
    if (!config) return
    void run('保存拍摄配置中...', () => api.updateShootConfig(config), (result: { item: ShootConfig }) => setConfig(result.item), { refresh: false })
  }

  function openExportJson() {
    void run('导出拍摄配置中...', () => api.exportShootConfig(), (payload: ShootConfigExportPayload) => {
      setJsonDialog({ mode: 'export', text: `${JSON.stringify(payload, null, 2)}\n` })
    }, { refresh: false })
  }

  function openPromptCard() {
    void run('生成拍摄配置 Prompt 预览中...', () => api.getShootConfigPromptCard(), (payload: ShootConfigPromptCard) => {
      setPromptDialog({ text: payload.content })
    }, { refresh: false })
  }

  function openImportJson() {
    if (!window.confirm('导入 JSON 会覆盖当前拍摄配置，并按当前标签规则校验品类引用；不影响标签规则本身。是否继续？')) return
    setJsonDialog({ mode: 'import', text: '' })
  }

  function confirmImportJson() {
    if (!jsonDialog || jsonDialog.mode !== 'import') return
    if (jsonDialog.text.length > 1024 * 1024) {
      setJsonDialog({ ...jsonDialog, error: 'JSON 内容过大，请控制在 1MB 以内。' })
      return
    }
    let payload: unknown
    try {
      payload = parseShootConfigJson(jsonDialog.text)
    } catch (error) {
      setJsonDialog({ ...jsonDialog, error: error instanceof Error ? error.message : 'JSON 导入失败。' })
      return
    }
    void run('导入拍摄配置中...', () => api.importShootConfig(payload), (result: { item: ShootConfig }) => {
      setConfig(result.item)
      setJsonDialog(null)
      window.alert('拍摄配置已导入并应用。')
    }, { refresh: false })
  }

  return (
    <section className="page">
      <PageTitle
        title="拍摄配置"
        subtitle="维护 AI Director 生成拍摄剧本时读取的全局导演规则和品类图位规则。"
        actions={<div className="toolbar"><span className="chip">Admin</span><button className="secondary" onClick={openPromptCard} disabled={!config}><FileText size={16} />Prompt 预览</button><button className="secondary" onClick={openImportJson}><Upload size={16} />导入</button><button className="secondary" onClick={openExportJson} disabled={!config}><Download size={16} />导出</button><button className="primary" onClick={save} disabled={!config}><Save size={16} />保存</button></div>}
      />
      {promptDialog && (
        <ShootConfigPromptDialog
          dialog={promptDialog}
          onClose={() => setPromptDialog(null)}
        />
      )}
      {jsonDialog && (
        <ShootConfigJsonDialog
          dialog={jsonDialog}
          setDialog={setJsonDialog}
          onClose={() => setJsonDialog(null)}
          onImport={confirmImportJson}
        />
      )}
      {loading && <div className="panel"><EmptyState title="读取拍摄配置中" text="正在加载当前拍摄规则。" /></div>}
      {config && (
        <>
          <div className="taxonomy-summary shoot-config-summary">
            <MetricCard label="启用全局规则" value={enabledGlobalRuleCount} />
            <MetricCard label="默认镜头" value={config.defaultShotRule.shots.length} />
            <MetricCard label="品类覆盖" value={activeCategoryRuleCount} />
            <MetricCard label="可新增品类" value={addableCategoryOptions.length} />
          </div>
          <div className="shoot-config-layout">
            <details className="panel shoot-config-section" open>
              <summary>
                <div>
                  <strong>全局导演规则</strong>
                  <span>{enabledGlobalRuleCount} / {config.globalRules.length} 条启用，按排序进入 AI Director Prompt。</span>
                </div>
                <button className="secondary" type="button" onClick={(event) => { event.preventDefault(); addGlobalRule() }}>新增规则</button>
              </summary>
              <p className="shoot-config-note">可删除、改名、停用或调整排序；保存后，后端会按当前条目动态组装 Prompt。</p>
              <div className="shoot-global-rules">
              {config.globalRules.map((rule, index) => (
                <div className="shoot-global-rule-card" key={`${rule.id}-${index}`}>
                  <div className="shoot-global-rule-head">
                    <label className="checkbox-line"><input type="checkbox" checked={rule.enabled} onChange={(event) => updateGlobalRule(index, 'enabled', event.target.checked)} /> 启用</label>
                    <button className="ghost-icon danger-icon" type="button" onClick={() => removeGlobalRule(index)} title="删除规则"><Trash2 size={16} /></button>
                  </div>
                  <div className="shoot-global-rule-fields">
                    <label>名称<input value={rule.label} onChange={(event) => updateGlobalRule(index, 'label', event.target.value)} /></label>
                    <label>排序<input type="number" value={rule.sortOrder} onChange={(event) => updateGlobalRule(index, 'sortOrder', Number(event.target.value))} /></label>
                    <label className="wide">规则内容<textarea value={rule.value} onChange={(event) => updateGlobalRule(index, 'value', event.target.value)} /></label>
                  </div>
                </div>
              ))}
              </div>
            </details>

            <details className="panel shoot-config-section" open>
              <summary>
                <div>
                  <strong>基础/默认 6 镜头规则</strong>
                  <span>未命中具体品类、品类停用或品类规则被删除时使用。</span>
                </div>
                <span className="chip">Default</span>
              </summary>
              <ShootRuleEditor shots={config.defaultShotRule.shots} onChangeShot={updateDefaultShotRule} />
            </details>

            <details className="panel shoot-config-section" open>
              <summary>
                <div>
                  <strong>品类 6 镜头基础规则</strong>
                  <span>从标签规则的一级品类选择，只维护每类独有的 6 个 ShotPlan。</span>
                </div>
              </summary>
              <div className="shoot-category-add">
                <select value={categoryToAdd} disabled={!addableCategoryOptions.length} onChange={(event) => setCategoryToAdd(event.target.value)}>
                  <option value="">{addableCategoryOptions.length ? '选择一级品类' : '无可新增品类'}</option>
                  {addableCategoryOptions.map((option) => <option key={`${option.dimensionCode}:${option.code}`} value={option.code}>{option.labelZh} / {option.code}</option>)}
                </select>
                <button className="secondary" type="button" disabled={!addableCategoryOptions.length} onClick={addCategoryRule}>新增品类规则</button>
              </div>
              <div className="shoot-category-rule-list">
            {config.categoryShotRules.map((rule, ruleIndex) => {
              const option = findShootCategoryOption(categoryOptions, rule.categoryDimensionCode, rule.categoryTagCode)
              return (
                <details className="shoot-category-rule" key={`${rule.categoryDimensionCode}:${rule.categoryTagCode}:${ruleIndex}`} open={ruleIndex === 0}>
                  <summary>
                    <div>
                      <strong>{option?.labelZh ?? rule.categoryTagCode}</strong>
                      <span>{rule.categoryDimensionCode}:{rule.categoryTagCode}{option && !option.isActive ? ' / 已停用' : ''}</span>
                    </div>
                    <small>{rule.shots.length} shots</small>
                  </summary>
                  <div className="shoot-rule-detail-head">
                    <div>
                      <Meta label="来源" value="标签规则一级品类" />
                      <Meta label="状态" value={option ? (option.isActive ? '启用' : '停用') : '标签已删除'} />
                    </div>
                    <button className="secondary" type="button" onClick={() => removeCategoryRule(ruleIndex)}><Trash2 size={16} />删除品类规则</button>
                  </div>
                  <ShootRuleEditor shots={rule.shots} onChangeShot={(shotIndex, field, value) => updateCategoryShotRule(ruleIndex, shotIndex, field, value)} />
                </details>
              )
            })}
              </div>
            {!config.categoryShotRules.length && <EmptyState title="暂无品类覆盖规则" text="未命中具体品类时会使用基础/默认 6 镜头规则。" />}
            </details>
          </div>
        </>
      )}
    </section>
  )
}

function ShootRuleEditor({ shots, onChangeShot }: {
  shots: ShootConfigShotRule[]
  onChangeShot: (index: number, field: keyof ShootConfigShotRule, value: string) => void
}) {
  return (
    <div className="shoot-rule-grid">
      {shots.map((rule, index) => (
        <div className="shoot-rule-card" key={index}>
          <div><span>#{index + 1}</span><input value={rule.shotRoleLabel} onChange={(event) => onChangeShot(index, 'shotRoleLabel', event.target.value)} /></div>
          <label>类型<input value={rule.shotType} onChange={(event) => onChangeShot(index, 'shotType', event.target.value)} /></label>
          <label>景别/取景<textarea value={rule.framing} onChange={(event) => onChangeShot(index, 'framing', event.target.value)} /></label>
          <label>姿势<textarea value={rule.pose} onChange={(event) => onChangeShot(index, 'pose', event.target.value)} /></label>
          <label>角度<input value={rule.cameraAngle} onChange={(event) => onChangeShot(index, 'cameraAngle', event.target.value)} /></label>
          <label>展示重点<textarea value={rule.mainFocus} onChange={(event) => onChangeShot(index, 'mainFocus', event.target.value)} /></label>
        </div>
      ))}
    </div>
  )
}

function cloneShootConfigShots(shots: ShootConfigShotRule[]): ShootConfigShotRule[] {
  return shots.slice(0, 6).map((shot) => ({ ...shot }))
}

function findShootCategoryOption(options: ShootConfigCategoryOption[], dimensionCode: string, tagCode: string) {
  return options.find((option) => option.dimensionCode === dimensionCode && option.code === tagCode)
}

function parseShootConfigJson(text: string): unknown {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error('JSON 格式不正确。')
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('JSON 内容必须是对象。')
  }
  const source = parsed as Record<string, unknown>
  const config = source.config && typeof source.config === 'object' && !Array.isArray(source.config) ? source.config : source
  const configObject = config as Record<string, unknown>
  if (!Array.isArray(configObject.globalRules)) throw new Error('globalRules 必须是数组。')
  if (!configObject.defaultShotRule || typeof configObject.defaultShotRule !== 'object') throw new Error('defaultShotRule 必须是对象。')
  if (!Array.isArray(configObject.categoryShotRules)) throw new Error('categoryShotRules 必须是数组。')
  return config
}

function ShootConfigPromptDialog({ dialog, onClose }: {
  dialog: { text: string }
  onClose: () => void
}) {
  return createPortal(
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <form className="confirm-modal brand-json-modal" role="dialog" aria-modal="true" aria-label="Prompt 预览" onSubmit={(event) => event.preventDefault()} onClick={(event) => event.stopPropagation()}>
        <div className="confirm-head">
          <div>
            <h2>Prompt 预览</h2>
            <p>展示后端实际传给 AI Director 的拍摄配置规则段，不触发模型调用。</p>
          </div>
          <button type="button" className="ghost-icon" onClick={onClose} title="关闭"><X size={18} /></button>
        </div>
        <textarea readOnly value={dialog.text} />
        <div className="confirm-actions">
          <button type="button" className="secondary" onClick={onClose}>关闭</button>
        </div>
      </form>
    </div>,
    document.body,
  )
}

function ShootConfigJsonDialog({ dialog, setDialog, onClose, onImport }: {
  dialog: { mode: 'import' | 'export'; text: string; error?: string }
  setDialog: (dialog: { mode: 'import' | 'export'; text: string; error?: string }) => void
  onClose: () => void
  onImport: () => void
}) {
  const isImport = dialog.mode === 'import'
  return createPortal(
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <form className="confirm-modal brand-json-modal" role="dialog" aria-modal="true" aria-label={isImport ? '导入拍摄配置' : '导出拍摄配置'} onSubmit={(event) => {
        event.preventDefault()
        if (isImport) onImport()
      }} onClick={(event) => event.stopPropagation()}>
        <div className="confirm-head">
          <div>
            <h2>{isImport ? '导入拍摄配置' : '导出拍摄配置'}</h2>
            <p>{isImport ? '导入后会覆盖当前拍摄配置，并按当前标签规则过滤不可用的品类引用。' : '导出包含当前配置和标签品类快照；导入时只应用 config。'}</p>
          </div>
          <button type="button" className="ghost-icon" onClick={onClose} title="关闭"><X size={18} /></button>
        </div>
        <textarea
          readOnly={!isImport}
          value={dialog.text}
          onChange={(event) => setDialog({ mode: dialog.mode, text: event.target.value })}
        />
        {dialog.error && <p className="json-error">{dialog.error}</p>}
        <div className="confirm-actions">
          <button type="button" className="secondary" onClick={onClose}>关闭</button>
          {isImport && <button type="submit" className="primary">导入并应用</button>}
        </div>
      </form>
    </div>,
    document.body,
  )
}

function TagRulesPage({ run }: { run: any }) {
  const [rules, setRules] = useState<TaxonomyRules | null>(null)
  const [activeCode, setActiveCode] = useState<string | 'new'>('')
  const [centerView, setCenterView] = useState<'tags' | 'relations' | 'applicability'>('tags')
  const [editorMode, setEditorMode] = useState<'dimension' | 'tag' | 'relation' | 'applicability'>('dimension')
  const [selectedTagId, setSelectedTagId] = useState<number | 'new' | null>(null)
  const [selectedRelationId, setSelectedRelationId] = useState<number | 'new' | null>(null)
  const [selectedApplicabilityId, setSelectedApplicabilityId] = useState<number | 'new' | null>(null)
  const [loading, setLoading] = useState(false)
  const [dimensionDraft, setDimensionDraft] = useState<TaxonomyDimensionPayload>({})
  const [tagDraft, setTagDraft] = useState<TaxonomyTagPayload>({})
  const [relationDraft, setRelationDraft] = useState<TaxonomyRelationPayload>({})
  const [applicabilityDraft, setApplicabilityDraft] = useState<TaxonomyApplicabilityPayload>({})
  const [jsonDialog, setJsonDialog] = useState<{ mode: 'import' | 'export'; text: string; error?: string } | null>(null)
  const [promptDialog, setPromptDialog] = useState<{ text: string } | null>(null)

  function loadRules() {
    setLoading(true)
    return api.getTaxonomyRules().then((result) => {
      setRules(result)
      setActiveCode((current) => current && (current === 'new' || result.dimensions.some((item) => item.code === current)) ? current : result.dimensions[0]?.code || '')
    }).finally(() => setLoading(false))
  }

  useEffect(() => {
    void loadRules().catch(() => undefined)
  }, [])

  const activeDimension = activeCode === 'new' ? null : rules?.dimensions.find((item) => item.code === activeCode) ?? rules?.dimensions[0]
  const activeTags = activeDimension?.tags ?? []
  const flatTags = flattenTaxonomyTags(activeTags)
  const allFlatTags = flattenTaxonomyDimensions(rules?.dimensions ?? [])
  const selectedTag = typeof selectedTagId === 'number' ? flatTags.find((tag) => tag.id === selectedTagId) ?? null : null
  const selectedRelation = typeof selectedRelationId === 'number' ? rules?.relations.find((relation) => relation.id === selectedRelationId) ?? null : null
  const selectedApplicability = typeof selectedApplicabilityId === 'number' ? rules?.applicabilityRules.find((rule) => rule.id === selectedApplicabilityId) ?? null : null
  const visibleRelations = (rules?.relations ?? []).filter((relation) => (
    activeDimension
      ? relation.fromDimensionCode === activeDimension.code || relation.toDimensionCode === activeDimension.code
      : true
  ))
  const visibleApplicabilityRules = (rules?.applicabilityRules ?? []).filter((rule) => (
    activeDimension ? rule.targetDimensionCode === activeDimension.code || rule.categoryDimensionCode === activeDimension.code : true
  ))
  const editingNewDimension = activeCode === 'new'

  useEffect(() => {
    if (activeCode === 'new') {
      setDimensionDraft({ code: '', labelZh: '', descriptionZh: '', sortOrder: (rules?.dimensions.length ?? 0) * 10 + 10, isActive: true })
      return
    }
    if (activeDimension) {
      setDimensionDraft({
        labelZh: activeDimension.labelZh,
        descriptionZh: activeDimension.descriptionZh ?? '',
        sortOrder: activeDimension.sortOrder,
        isActive: activeDimension.isActive,
      })
    }
    setSelectedTagId(null)
    setSelectedRelationId(null)
    setSelectedApplicabilityId(null)
  }, [activeCode, activeDimension?.updatedAt])

  useEffect(() => {
    if (!activeDimension) return
    if (selectedTagId === 'new') {
      setTagDraft({
        dimensionCode: activeDimension.code,
        code: '',
        labelZh: '',
        descriptionZh: '',
        parentCode: null,
        level: 1,
        valueKind: 'tag',
        isSelectable: true,
        isActive: true,
        sortOrder: 0,
      })
      return
    }
    if (selectedTag) {
      setTagDraft({
        labelZh: selectedTag.labelZh,
        descriptionZh: selectedTag.descriptionZh ?? '',
        parentCode: selectedTag.parentCode ?? null,
        level: selectedTag.level,
        valueKind: selectedTag.valueKind,
        isSelectable: selectedTag.isSelectable,
        isActive: selectedTag.isActive,
        sortOrder: selectedTag.sortOrder,
      })
      return
    }
    setTagDraft({})
  }, [activeDimension?.code, selectedTagId, selectedTag?.id])

  useEffect(() => {
    if (selectedRelationId === 'new') {
      const fromTag = flatTags[0]
      const toTag = allFlatTags.find((item) => item.code !== fromTag?.code || item.dimensionCode !== fromTag?.dimensionCode)
      setRelationDraft({
        fromDimensionCode: fromTag?.dimensionCode ?? activeDimension?.code ?? '',
        fromTagCode: fromTag?.code ?? '',
        relationType: 'implies',
        toDimensionCode: toTag?.dimensionCode ?? activeDimension?.code ?? '',
        toTagCode: toTag?.code ?? '',
        weight: 1,
        descriptionZh: '',
        isActive: true,
      })
      return
    }
    if (selectedRelation) {
      setRelationDraft({
        fromDimensionCode: selectedRelation.fromDimensionCode,
        fromTagCode: selectedRelation.fromTagCode,
        relationType: selectedRelation.relationType,
        toDimensionCode: selectedRelation.toDimensionCode,
        toTagCode: selectedRelation.toTagCode,
        weight: selectedRelation.weight,
        descriptionZh: selectedRelation.descriptionZh ?? '',
        isActive: selectedRelation.isActive,
      })
      return
    }
    setRelationDraft({})
  }, [selectedRelationId, selectedRelation?.id, activeDimension?.code, rules?.relations.length])

  useEffect(() => {
    const categoryTags = flattenTaxonomyTags(rules?.dimensions.find((dimension) => dimension.code === 'category')?.tags ?? [])
    if (selectedApplicabilityId === 'new') {
      const targetTag = flatTags[0] ?? allFlatTags.find((tag) => tag.dimensionCode !== 'category')
      setApplicabilityDraft({
        categoryDimensionCode: 'category',
        categoryTagCode: categoryTags[0]?.code ?? '',
        targetDimensionCode: targetTag?.dimensionCode ?? activeDimension?.code ?? '',
        targetTagCode: targetTag?.code ?? '',
        mode: 'allowed',
        appliesToDescendants: true,
        note: '',
        isActive: true,
      })
      return
    }
    if (selectedApplicability) {
      setApplicabilityDraft({
        categoryDimensionCode: selectedApplicability.categoryDimensionCode,
        categoryTagCode: selectedApplicability.categoryTagCode,
        targetDimensionCode: selectedApplicability.targetDimensionCode,
        targetTagCode: selectedApplicability.targetTagCode,
        mode: selectedApplicability.mode,
        appliesToDescendants: selectedApplicability.appliesToDescendants,
        note: selectedApplicability.note ?? '',
        isActive: selectedApplicability.isActive,
      })
      return
    }
    setApplicabilityDraft({})
  }, [selectedApplicabilityId, selectedApplicability?.id, activeDimension?.code, rules?.applicabilityRules.length])

  function applyRules(next: TaxonomyRules) {
    setRules(next)
    setActiveCode((current) => current === 'new' || next.dimensions.some((item) => item.code === current) ? current : next.dimensions[0]?.code ?? '')
  }

  function selectDimension(code: string | 'new') {
    setActiveCode(code)
    setEditorMode('dimension')
    setSelectedTagId(null)
    setSelectedRelationId(null)
    setSelectedApplicabilityId(null)
  }

  function selectTag(id: number) {
    setSelectedTagId(id)
    setSelectedRelationId(null)
    setSelectedApplicabilityId(null)
    setEditorMode('tag')
  }

  function selectRelation(id: number) {
    setSelectedRelationId(id)
    setSelectedTagId(null)
    setSelectedApplicabilityId(null)
    setEditorMode('relation')
  }

  function selectApplicability(id: number) {
    setSelectedApplicabilityId(id)
    setSelectedTagId(null)
    setSelectedRelationId(null)
    setEditorMode('applicability')
  }

  function saveDimension() {
    const labelZh = String(dimensionDraft.labelZh ?? '').trim()
    if (editingNewDimension) {
      const code = String(dimensionDraft.code ?? '').trim()
      void run('新增标签维度中...', () => api.createTaxonomyDimension({ ...dimensionDraft, code, labelZh }), (next: TaxonomyRules) => {
        applyRules(next)
        const created = next.dimensions.find((item) => item.code === code) ?? next.dimensions.find((item) => item.labelZh === labelZh)
        setActiveCode(created?.code ?? next.dimensions[0]?.code ?? '')
      }, { refresh: false })
      return
    }
    if (!activeDimension) return
    void run('保存标签维度中...', () => api.updateTaxonomyDimension(activeDimension.code, { ...dimensionDraft, labelZh }), applyRules, { refresh: false })
  }

  function deleteDimension() {
    if (!activeDimension) return
    if (!window.confirm(`确定删除维度「${activeDimension.labelZh}」吗？仅空维度可删除；已有标签或打标引用时后端会拒绝。`)) return
    void run('删除标签维度中...', () => api.deleteTaxonomyDimension(activeDimension.code), (next: TaxonomyRules) => {
      applyRules(next)
      setActiveCode(next.dimensions[0]?.code ?? '')
    }, { refresh: false })
  }

  function saveTag() {
    if (!activeDimension) return
    if (selectedTagId === 'new') {
      const payload = {
        ...tagDraft,
        dimensionCode: activeDimension.code,
        code: String(tagDraft.code ?? ''),
        labelZh: String(tagDraft.labelZh ?? ''),
      }
      void run('新增标签中...', () => api.createTaxonomyTag(payload), (next: TaxonomyRules) => {
        applyRules(next)
        const created = flattenTaxonomyTags(next.dimensions.find((item) => item.code === activeDimension.code)?.tags ?? []).find((item) => item.code === payload.code)
        setSelectedTagId(created?.id ?? null)
      }, { refresh: false })
      return
    }
    if (selectedTag) {
      void run('保存标签中...', () => api.updateTaxonomyTag(selectedTag.id, tagDraft), applyRules, { refresh: false })
    }
  }

  function deleteTag() {
    if (!selectedTag) return
    if (!window.confirm(`确定删除标签「${selectedTag.labelZh}」吗？已有子标签或商品引用时后端会拒绝；生产中更推荐停用。`)) return
    void run('删除标签中...', () => api.deleteTaxonomyTag(selectedTag.id), (next: TaxonomyRules) => {
      applyRules(next)
      setSelectedTagId(null)
      setEditorMode('dimension')
    }, { refresh: false })
  }

  function saveRelation() {
    const payload = {
      ...relationDraft,
      fromDimensionCode: String(relationDraft.fromDimensionCode ?? ''),
      fromTagCode: String(relationDraft.fromTagCode ?? ''),
      relationType: String(relationDraft.relationType ?? ''),
      toDimensionCode: String(relationDraft.toDimensionCode ?? ''),
      toTagCode: String(relationDraft.toTagCode ?? ''),
    }
    if (selectedRelationId === 'new') {
      void run('新增标签关系中...', () => api.createTaxonomyRelation(payload), (next: TaxonomyRules) => {
        applyRules(next)
        const created = next.relations.find((item) => (
          item.fromDimensionCode === payload.fromDimensionCode
          && item.fromTagCode === payload.fromTagCode
          && item.relationType === payload.relationType
          && item.toDimensionCode === payload.toDimensionCode
          && item.toTagCode === payload.toTagCode
        ))
        setSelectedRelationId(created?.id ?? null)
      }, { refresh: false })
      return
    }
    if (selectedRelation) {
      void run('保存标签关系中...', () => api.updateTaxonomyRelation(selectedRelation.id, relationDraft), applyRules, { refresh: false })
    }
  }

  function deleteRelation() {
    if (!selectedRelation) return
    if (!window.confirm('确定删除这条标签关系吗？')) return
    void run('删除标签关系中...', () => api.deleteTaxonomyRelation(selectedRelation.id), (next: TaxonomyRules) => {
      applyRules(next)
      setSelectedRelationId(null)
      setEditorMode('dimension')
    }, { refresh: false })
  }

  function saveApplicability() {
    const payload = {
      ...applicabilityDraft,
      categoryDimensionCode: String(applicabilityDraft.categoryDimensionCode || 'category'),
      categoryTagCode: String(applicabilityDraft.categoryTagCode ?? ''),
      targetDimensionCode: String(applicabilityDraft.targetDimensionCode ?? ''),
      targetTagCode: String(applicabilityDraft.targetTagCode ?? ''),
    }
    if (selectedApplicabilityId === 'new') {
      void run('新增适用规则中...', () => api.createTaxonomyApplicabilityRule(payload), (next: TaxonomyRules) => {
        applyRules(next)
        const created = next.applicabilityRules.find((item) => (
          item.categoryDimensionCode === payload.categoryDimensionCode
          && item.categoryTagCode === payload.categoryTagCode
          && item.targetDimensionCode === payload.targetDimensionCode
          && item.targetTagCode === payload.targetTagCode
        ))
        setSelectedApplicabilityId(created?.id ?? null)
      }, { refresh: false })
      return
    }
    if (selectedApplicability) {
      void run('保存适用规则中...', () => api.updateTaxonomyApplicabilityRule(selectedApplicability.id, applicabilityDraft), applyRules, { refresh: false })
    }
  }

  function deleteApplicability() {
    if (!selectedApplicability) return
    if (!window.confirm('确定删除这条品类适用规则吗？')) return
    void run('删除适用规则中...', () => api.deleteTaxonomyApplicabilityRule(selectedApplicability.id), (next: TaxonomyRules) => {
      applyRules(next)
      setSelectedApplicabilityId(null)
      setEditorMode('dimension')
    }, { refresh: false })
  }

  function openExportJson() {
    void run('导出标签规则中...', () => api.exportTaxonomyRules(), (payload: TaxonomyExportPayload) => {
      setJsonDialog({ mode: 'export', text: `${JSON.stringify(payload, null, 2)}\n` })
    }, { refresh: false })
  }

  function openPromptCard() {
    void run('生成标签规则 Prompt 预览中...', () => api.getTaxonomyPromptCard(), (payload: TaxonomyPromptCard) => {
      setPromptDialog({ text: payload.content })
    }, { refresh: false })
  }

  function openImportJson() {
    if (!window.confirm('导入 JSON 会按 code 合并更新维度、标签和关系，不会自动删除缺失项，是否继续？')) return
    setJsonDialog({ mode: 'import', text: '' })
  }

  function confirmImportJson() {
    if (!jsonDialog || jsonDialog.mode !== 'import') return
    if (jsonDialog.text.length > 2 * 1024 * 1024) {
      setJsonDialog({ ...jsonDialog, error: 'JSON 内容过大，请控制在 2MB 以内。' })
      return
    }
    let payload: { dimensions: unknown[]; tags: unknown[]; relations: unknown[]; applicabilityRules: unknown[] }
    try {
      payload = parseTaxonomyJson(jsonDialog.text)
    } catch (error) {
      setJsonDialog({ ...jsonDialog, error: error instanceof Error ? error.message : 'JSON 导入失败。' })
      return
    }
    void run('导入标签规则中...', () => api.importTaxonomyRules(payload), (next: TaxonomyRules) => {
      applyRules(next)
      setJsonDialog(null)
      window.alert('标签规则已导入并应用。')
    }, { refresh: false })
  }

  return (
    <section className="page">
      <PageTitle
        title="标签规则"
        subtitle="维护服装标签字典、层级树、标签关系和商品打标投影。"
        actions={<div className="toolbar"><span className="chip">Admin</span><button className="secondary" onClick={openPromptCard}><FileText size={16} />Prompt 预览</button><button className="secondary" onClick={openImportJson}><Upload size={16} />导入</button><button className="secondary" onClick={openExportJson}><Download size={16} />导出</button><button className="primary" onClick={() => void run('同步标签投影中...', () => api.backfillTaxonomyAssignments(), () => void loadRules(), { refresh: false })}><Save size={16} />同步现有标签</button></div>}
      />
      {promptDialog && (
        <TaxonomyPromptDialog
          dialog={promptDialog}
          onClose={() => setPromptDialog(null)}
        />
      )}
      {jsonDialog && (
        <TaxonomyJsonDialog
          dialog={jsonDialog}
          setDialog={setJsonDialog}
          onClose={() => setJsonDialog(null)}
          onImport={confirmImportJson}
        />
      )}
      <div className="taxonomy-summary">
        <MetricCard label="维度" value={rules?.summary.dimensionCount ?? 0} />
        <MetricCard label="标准标签" value={rules?.summary.tagCount ?? 0} />
        <MetricCard label="标签关系" value={rules?.summary.relationCount ?? 0} />
        <MetricCard label="适用规则" value={rules?.summary.applicabilityRuleCount ?? 0} />
        <MetricCard label="已投影服饰" value={rules?.summary.taggedGarmentCount ?? 0} />
      </div>
      <div className="taxonomy-layout">
        <aside className="panel taxonomy-dimensions">
          <button className={activeCode === 'new' ? 'active' : ''} onClick={() => selectDimension('new')}>
            <span>新增维度</span>
            <em>new_dimension</em>
          </button>
          {(rules?.dimensions ?? []).map((dimension) => (
            <button key={dimension.code} className={dimension.code === activeDimension?.code ? 'active' : ''} onClick={() => selectDimension(dimension.code)}>
              <span>{dimension.labelZh}</span>
              <em>{dimension.code}{!dimension.isActive ? ' / 停用' : ''}</em>
            </button>
          ))}
          {!rules && <EmptyState title={loading ? '加载中' : '暂无标签规则'} text={loading ? '正在读取标签字典。' : '标签字典尚未初始化。'} />}
        </aside>
        <div className="panel taxonomy-tree-panel">
          {activeDimension ? (
            <>
              <div className="taxonomy-panel-head">
                <div>
                  <span>{activeDimension.code}</span>
                  <h2>{activeDimension.labelZh}</h2>
                </div>
                <div className="taxonomy-head-actions">
                  <div className="segmented">
                    <button className={centerView === 'tags' ? 'active' : ''} onClick={() => setCenterView('tags')}>标签树</button>
                    <button className={centerView === 'relations' ? 'active' : ''} onClick={() => setCenterView('relations')}>关系</button>
                    <button className={centerView === 'applicability' ? 'active' : ''} onClick={() => setCenterView('applicability')}>适用范围</button>
                  </div>
                  <strong>{centerView === 'tags' ? `${countTaxonomyTags(activeTags)} 个标签` : centerView === 'relations' ? `${visibleRelations.length} 条关系` : `${visibleApplicabilityRules.length} 条规则`}</strong>
                  {centerView === 'tags' && <button className="secondary" onClick={() => { setSelectedTagId('new'); setEditorMode('tag') }}>新增标签</button>}
                  {centerView === 'relations' && <button className="secondary" onClick={() => { setSelectedRelationId('new'); setSelectedTagId(null); setSelectedApplicabilityId(null); setEditorMode('relation') }}>新增关系</button>}
                  {centerView === 'applicability' && <button className="secondary" onClick={() => { setSelectedApplicabilityId('new'); setSelectedTagId(null); setSelectedRelationId(null); setEditorMode('applicability') }}>新增适用规则</button>}
                </div>
              </div>
              {centerView === 'tags' ? (
                <div className="taxonomy-tree">
                  {activeTags.map((tag) => <TaxonomyTagNode key={tag.code} tag={tag} selectedId={typeof selectedTagId === 'number' ? selectedTagId : null} onSelect={selectTag} />)}
                  {!activeTags.length && <EmptyState title="暂无标签" text="当前维度还没有标签，可从右上角新增。" />}
                </div>
              ) : centerView === 'relations' ? (
                <div className="taxonomy-relation-list">
                  {visibleRelations.map((relation) => (
                    <button key={relation.id} className={`taxonomy-relation-card ${selectedRelationId === relation.id ? 'active' : ''} ${relation.isActive ? '' : 'is-inactive'}`} onClick={() => selectRelation(relation.id)}>
                      <strong>{relationLabel(relation, allFlatTags)}</strong>
                      <span>{relation.relationType} / weight {relation.weight}</span>
                      {relation.descriptionZh && <em>{relation.descriptionZh}</em>}
                    </button>
                  ))}
                  {!visibleRelations.length && <EmptyState title="暂无关系" text="可新增品类映射、暗含关系、搭配兼容或避免规则。" />}
                </div>
              ) : (
                <div className="taxonomy-relation-list">
                  {visibleApplicabilityRules.map((rule) => (
                    <button key={rule.id} className={`taxonomy-relation-card ${selectedApplicabilityId === rule.id ? 'active' : ''} ${rule.isActive ? '' : 'is-inactive'}`} onClick={() => selectApplicability(rule.id)}>
                      <strong>{applicabilityRuleLabel(rule, allFlatTags)}</strong>
                      <span>{applicabilityModeLabel(rule.mode)} / {rule.appliesToDescendants ? '含子品类' : '仅当前品类'}</span>
                      {rule.note && <em>{rule.note}</em>}
                    </button>
                  ))}
                  {!visibleApplicabilityRules.length && <EmptyState title="暂无适用规则" text="用于限定某品类可使用哪些服装部件和设计细节，避免所有品类共用一套字段。" />}
                </div>
              )}
            </>
          ) : editingNewDimension ? <EmptyState title="正在新增维度" text="先在右侧填写 code 和中文名称，保存后即可维护标签树。" /> : <EmptyState title="暂无标签规则" text="标签字典尚未初始化。" />}
        </div>
        <aside className="panel taxonomy-editor">
          {editingNewDimension || activeDimension ? (
            <>
              {editorMode === 'dimension' && <div className="taxonomy-editor-section">
                <div className="section-head"><strong>{editingNewDimension ? '新增维度' : '维度编辑'}</strong><span>{editingNewDimension ? 'new' : activeDimension?.code}</span></div>
                {editingNewDimension && <label>code<input value={String(dimensionDraft.code ?? '')} onChange={(event) => setDimensionDraft((current) => ({ ...current, code: event.target.value }))} /></label>}
                <label>中文名称<input value={String(dimensionDraft.labelZh ?? '')} onChange={(event) => setDimensionDraft((current) => ({ ...current, labelZh: event.target.value }))} /></label>
                <label>说明<textarea value={String(dimensionDraft.descriptionZh ?? '')} onChange={(event) => setDimensionDraft((current) => ({ ...current, descriptionZh: event.target.value }))} /></label>
                <label>排序<input type="number" value={Number(dimensionDraft.sortOrder ?? 0)} onChange={(event) => setDimensionDraft((current) => ({ ...current, sortOrder: Number(event.target.value) }))} /></label>
                <label className="checkbox-line"><input type="checkbox" checked={dimensionDraft.isActive !== false} onChange={(event) => setDimensionDraft((current) => ({ ...current, isActive: event.target.checked }))} /> 启用</label>
                <div className="taxonomy-editor-actions">
                  {!editingNewDimension && <button className="secondary" onClick={deleteDimension}><Trash2 size={16} />删除</button>}
                  <button className="primary" onClick={saveDimension}><Save size={16} />{editingNewDimension ? '新增维度' : '保存维度'}</button>
                </div>
              </div>}
              {editorMode === 'tag' && <div className="taxonomy-editor-section">
                <div className="section-head"><strong>{selectedTagId === 'new' ? '新增标签' : '标签编辑'}</strong>{selectedTag && <span>{selectedTag.code}</span>}</div>
                {activeDimension && (selectedTagId || selectedTag) ? (
                  <>
                    {selectedTagId === 'new' && <label>code<input value={String(tagDraft.code ?? '')} onChange={(event) => setTagDraft((current) => ({ ...current, code: event.target.value }))} /></label>}
                    <label>中文名称<input value={String(tagDraft.labelZh ?? '')} onChange={(event) => setTagDraft((current) => ({ ...current, labelZh: event.target.value }))} /></label>
                    <label>说明<textarea value={String(tagDraft.descriptionZh ?? '')} onChange={(event) => setTagDraft((current) => ({ ...current, descriptionZh: event.target.value }))} /></label>
                    <label>父级
                      <select value={tagDraft.parentCode ?? ''} onChange={(event) => setTagDraft((current) => ({ ...current, parentCode: event.target.value || null }))}>
                        <option value="">无父级</option>
                        {flatTags.filter((tag) => tag.id !== selectedTag?.id).map((tag) => <option key={tag.id} value={tag.code}>{tag.labelZh} / {tag.code}</option>)}
                      </select>
                    </label>
                    <div className="taxonomy-editor-grid">
                      <label>层级<input type="number" min={1} max={8} value={Number(tagDraft.level ?? 1)} onChange={(event) => setTagDraft((current) => ({ ...current, level: Number(event.target.value) }))} /></label>
                      <label>排序<input type="number" value={Number(tagDraft.sortOrder ?? 0)} onChange={(event) => setTagDraft((current) => ({ ...current, sortOrder: Number(event.target.value) }))} /></label>
                    </div>
                    <label>值类型
                      <select value={String(tagDraft.valueKind ?? 'tag')} onChange={(event) => setTagDraft((current) => ({ ...current, valueKind: event.target.value }))}>
                        <option value="tag">标签</option>
                        <option value="score">分值</option>
                        <option value="boolean">开关</option>
                        <option value="text">文本</option>
                      </select>
                    </label>
                    <label className="checkbox-line"><input type="checkbox" checked={tagDraft.isSelectable !== false} onChange={(event) => setTagDraft((current) => ({ ...current, isSelectable: event.target.checked }))} /> 可用于服装打标</label>
                    <label className="checkbox-line"><input type="checkbox" checked={tagDraft.isActive !== false} onChange={(event) => setTagDraft((current) => ({ ...current, isActive: event.target.checked }))} /> 启用</label>
                    <div className="taxonomy-editor-actions">
                      {selectedTag && <button className="secondary" onClick={deleteTag}><Trash2 size={16} />删除</button>}
                      <button className="primary" onClick={saveTag}><Save size={16} />{selectedTagId === 'new' ? '新增标签' : '保存标签'}</button>
                    </div>
                  </>
                ) : <p className="empty-inline">选择一个标签，或点击新增标签。</p>}
              </div>}
              {editorMode === 'relation' && <div className="taxonomy-editor-section">
                <div className="section-head"><strong>{selectedRelationId === 'new' ? '新增关系' : '关系编辑'}</strong>{selectedRelation && <span>#{selectedRelation.id}</span>}</div>
                {selectedRelationId || selectedRelation ? (
                  <>
                    <TaxonomyTagPicker label="来源标签" dimensions={rules?.dimensions ?? []} draftDimensionCode={relationDraft.fromDimensionCode ?? ''} draftTagCode={relationDraft.fromTagCode ?? ''} onChange={(dimensionCode, tagCode) => setRelationDraft((current) => ({ ...current, fromDimensionCode: dimensionCode, fromTagCode: tagCode }))} />
                    <label>关系类型
                      <input list="taxonomy-relation-types" value={String(relationDraft.relationType ?? '')} onChange={(event) => setRelationDraft((current) => ({ ...current, relationType: event.target.value }))} />
                    </label>
                    <datalist id="taxonomy-relation-types">
                      <option value="implies" />
                      <option value="slot_mapping" />
                      <option value="style_mapping" />
                      <option value="compatible_with" />
                      <option value="avoid_with" />
                      <option value="parent_of" />
                    </datalist>
                    <TaxonomyTagPicker label="目标标签" dimensions={rules?.dimensions ?? []} draftDimensionCode={relationDraft.toDimensionCode ?? ''} draftTagCode={relationDraft.toTagCode ?? ''} onChange={(dimensionCode, tagCode) => setRelationDraft((current) => ({ ...current, toDimensionCode: dimensionCode, toTagCode: tagCode }))} />
                    <label>权重<input type="number" min={-10} max={10} step={0.1} value={Number(relationDraft.weight ?? 1)} onChange={(event) => setRelationDraft((current) => ({ ...current, weight: Number(event.target.value) }))} /></label>
                    <label>说明<textarea value={String(relationDraft.descriptionZh ?? '')} onChange={(event) => setRelationDraft((current) => ({ ...current, descriptionZh: event.target.value }))} /></label>
                    <label className="checkbox-line"><input type="checkbox" checked={relationDraft.isActive !== false} onChange={(event) => setRelationDraft((current) => ({ ...current, isActive: event.target.checked }))} /> 启用</label>
                    <div className="taxonomy-editor-actions">
                      {selectedRelation && <button className="secondary" onClick={deleteRelation}><Trash2 size={16} />删除</button>}
                      <button className="primary" onClick={saveRelation}><Save size={16} />{selectedRelationId === 'new' ? '新增关系' : '保存关系'}</button>
                    </div>
                  </>
                ) : <p className="empty-inline">选择一条关系，或点击新增关系。</p>}
              </div>}
              {editorMode === 'applicability' && <div className="taxonomy-editor-section">
                <div className="section-head"><strong>{selectedApplicabilityId === 'new' ? '新增适用规则' : '适用规则编辑'}</strong>{selectedApplicability && <span>#{selectedApplicability.id}</span>}</div>
                {selectedApplicabilityId || selectedApplicability ? (
                  <>
                    <TaxonomyCategoryPicker
                      label="适用品类"
                      dimensions={rules?.dimensions ?? []}
                      draftDimensionCode={applicabilityDraft.categoryDimensionCode ?? 'category'}
                      draftTagCode={applicabilityDraft.categoryTagCode ?? ''}
                      onChange={(dimensionCode, tagCode) => setApplicabilityDraft((current) => ({ ...current, categoryDimensionCode: dimensionCode, categoryTagCode: tagCode }))}
                    />
                    <TaxonomyTagPicker
                      label="可用部件/细节"
                      dimensions={(rules?.dimensions ?? []).filter((dimension) => dimension.code !== 'category')}
                      draftDimensionCode={applicabilityDraft.targetDimensionCode ?? activeDimension?.code ?? ''}
                      draftTagCode={applicabilityDraft.targetTagCode ?? ''}
                      onChange={(dimensionCode, tagCode) => setApplicabilityDraft((current) => ({ ...current, targetDimensionCode: dimensionCode, targetTagCode: tagCode }))}
                    />
                    <label>规则模式
                      <select value={String(applicabilityDraft.mode ?? 'allowed')} onChange={(event) => setApplicabilityDraft((current) => ({ ...current, mode: event.target.value }))}>
                        <option value="allowed">允许</option>
                        <option value="recommended">推荐</option>
                        <option value="required">必填</option>
                        <option value="disallowed">禁用</option>
                      </select>
                    </label>
                    <label>说明<textarea value={String(applicabilityDraft.note ?? '')} onChange={(event) => setApplicabilityDraft((current) => ({ ...current, note: event.target.value }))} /></label>
                    <label className="checkbox-line"><input type="checkbox" checked={applicabilityDraft.appliesToDescendants !== false} onChange={(event) => setApplicabilityDraft((current) => ({ ...current, appliesToDescendants: event.target.checked }))} /> 适用于子品类</label>
                    <label className="checkbox-line"><input type="checkbox" checked={applicabilityDraft.isActive !== false} onChange={(event) => setApplicabilityDraft((current) => ({ ...current, isActive: event.target.checked }))} /> 启用</label>
                    <div className="taxonomy-editor-actions">
                      {selectedApplicability && <button className="secondary" onClick={deleteApplicability}><Trash2 size={16} />删除</button>}
                      <button className="primary" onClick={saveApplicability}><Save size={16} />{selectedApplicabilityId === 'new' ? '新增规则' : '保存规则'}</button>
                    </div>
                  </>
                ) : <p className="empty-inline">选择一条适用规则，或点击新增适用规则。</p>}
              </div>}
            </>
          ) : <EmptyState title="暂无维度" text="标签字典尚未初始化。" />}
        </aside>
      </div>
    </section>
  )
}

function MetricCard({ label, value }: { label: string; value: number }) {
  return <div className="panel taxonomy-metric"><span>{label}</span><strong>{value}</strong></div>
}

function TaxonomyTagNode({ tag, selectedId, onSelect }: { tag: TaxonomyTag; selectedId: number | null; onSelect: (id: number) => void }) {
  return (
    <div className={`taxonomy-node ${tag.isActive ? '' : 'is-inactive'}`}>
      <button className={`taxonomy-node-row ${selectedId === tag.id ? 'active' : ''}`} onClick={() => onSelect(tag.id)}>
        <div>
          <strong>{tag.labelZh}</strong>
          <span>{tag.code}</span>
        </div>
        <div className="taxonomy-node-meta">
          <em>{valueKindLabel(tag.valueKind)}</em>
          {!tag.isActive && <em>停用</em>}
          {tag.usageCount > 0 && <b>{tag.usageCount}</b>}
        </div>
      </button>
      {tag.children.length > 0 && <div className="taxonomy-children">{tag.children.map((child) => <TaxonomyTagNode key={child.code} tag={child} selectedId={selectedId} onSelect={onSelect} />)}</div>}
    </div>
  )
}

function countTaxonomyTags(tags: TaxonomyTag[]): number {
  return tags.reduce((total, tag) => total + 1 + countTaxonomyTags(tag.children), 0)
}

function flattenTaxonomyTags(tags: TaxonomyTag[]): TaxonomyTag[] {
  return tags.flatMap((tag) => [tag, ...flattenTaxonomyTags(tag.children)])
}

function flattenTaxonomyDimensions(dimensions: { tags: TaxonomyTag[] }[]): TaxonomyTag[] {
  return dimensions.flatMap((dimension) => flattenTaxonomyTags(dimension.tags))
}

function valueKindLabel(kind: string) {
  if (kind === 'score') return '分值'
  if (kind === 'boolean') return '开关'
  if (kind === 'text') return '文本'
  return '标签'
}

function tagLabel(tags: TaxonomyTag[], dimensionCode?: string, tagCode?: string) {
  const tag = tags.find((item) => item.dimensionCode === dimensionCode && item.code === tagCode)
  return tag ? `${tag.labelZh} / ${tag.dimensionCode}.${tag.code}` : `${dimensionCode ?? '-'} / ${tagCode ?? '-'}`
}

function relationLabel(relation: TaxonomyRelation, tags: TaxonomyTag[]) {
  return `${tagLabel(tags, relation.fromDimensionCode, relation.fromTagCode)} -> ${tagLabel(tags, relation.toDimensionCode, relation.toTagCode)}`
}

function applicabilityRuleLabel(rule: TaxonomyApplicabilityRule, tags: TaxonomyTag[]) {
  return `${tagLabel(tags, rule.categoryDimensionCode, rule.categoryTagCode)} -> ${tagLabel(tags, rule.targetDimensionCode, rule.targetTagCode)}`
}

function applicabilityModeLabel(mode: string) {
  if (mode === 'recommended') return '推荐'
  if (mode === 'required') return '必填'
  if (mode === 'disallowed') return '禁用'
  return '允许'
}

function parseTaxonomyJson(text: string): { dimensions: unknown[]; tags: unknown[]; relations: unknown[]; applicabilityRules: unknown[] } {
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error('JSON 格式不正确。')
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('JSON 内容必须是对象。')
  }
  const source = parsed as Record<string, unknown>
  const dimensions = source.dimensions
  const tags = source.tags
  const relations = source.relations
  const applicabilityRules = source.applicabilityRules
  if (!Array.isArray(dimensions)) throw new Error('dimensions 必须是数组。')
  if (!Array.isArray(tags)) throw new Error('tags 必须是数组。')
  if (relations != null && !Array.isArray(relations)) throw new Error('relations 必须是数组。')
  if (applicabilityRules != null && !Array.isArray(applicabilityRules)) throw new Error('applicabilityRules 必须是数组。')
  return { dimensions, tags, relations: Array.isArray(relations) ? relations : [], applicabilityRules: Array.isArray(applicabilityRules) ? applicabilityRules : [] }
}

function TaxonomyPromptDialog({ dialog, onClose }: {
  dialog: { text: string }
  onClose: () => void
}) {
  return createPortal(
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <form className="confirm-modal brand-json-modal" role="dialog" aria-modal="true" aria-label="Prompt 预览" onSubmit={(event) => event.preventDefault()} onClick={(event) => event.stopPropagation()}>
        <div className="confirm-head">
          <div>
            <h2>Prompt 预览</h2>
            <p>展示后端实际传给服装打标模型的标签规则卡，不触发模型调用。</p>
          </div>
          <button type="button" className="ghost-icon" onClick={onClose} title="关闭"><X size={18} /></button>
        </div>
        <textarea readOnly value={dialog.text} />
        <div className="confirm-actions">
          <button type="button" className="secondary" onClick={onClose}>关闭</button>
        </div>
      </form>
    </div>,
    document.body,
  )
}

function TaxonomyJsonDialog({ dialog, setDialog, onClose, onImport }: {
  dialog: { mode: 'import' | 'export'; text: string; error?: string }
  setDialog: (dialog: { mode: 'import' | 'export'; text: string; error?: string }) => void
  onClose: () => void
  onImport: () => void
}) {
  const isImport = dialog.mode === 'import'
  return createPortal(
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <form className="confirm-modal brand-json-modal" role="dialog" aria-modal="true" aria-label={isImport ? '导入标签规则' : '导出标签规则'} onSubmit={(event) => {
        event.preventDefault()
        if (isImport) onImport()
      }} onClick={(event) => event.stopPropagation()}>
        <div className="confirm-head">
          <div><h2>{isImport ? '导入标签规则' : '导出标签规则'}</h2></div>
          <button type="button" className="ghost-icon" onClick={onClose} title="关闭"><X size={18} /></button>
        </div>
        <textarea
          readOnly={!isImport}
          value={dialog.text}
          onChange={(event) => setDialog({ mode: dialog.mode, text: event.target.value })}
        />
        {dialog.error && <p className="json-error">{dialog.error}</p>}
        <div className="confirm-actions">
          <button type="button" className="secondary" onClick={onClose}>关闭</button>
          {isImport && <button type="submit" className="primary">导入并应用</button>}
        </div>
      </form>
    </div>,
    document.body,
  )
}

function TaxonomyTagPicker({ label, dimensions, draftDimensionCode, draftTagCode, onChange }: {
  label: string
  dimensions: { code: string; labelZh: string; tags: TaxonomyTag[] }[]
  draftDimensionCode: string
  draftTagCode: string
  onChange: (dimensionCode: string, tagCode: string) => void
}) {
  const selectedDimension = dimensions.find((dimension) => dimension.code === draftDimensionCode) ?? dimensions[0]
  const tags = flattenTaxonomyTags(selectedDimension?.tags ?? [])
  const selectedTagCode = tags.some((tag) => tag.code === draftTagCode) ? draftTagCode : tags[0]?.code ?? ''
  return (
    <div className="taxonomy-tag-picker">
      <span>{label}</span>
      <select value={selectedDimension?.code ?? ''} onChange={(event) => {
        const nextDimension = dimensions.find((dimension) => dimension.code === event.target.value)
        const nextTag = flattenTaxonomyTags(nextDimension?.tags ?? [])[0]
        onChange(event.target.value, nextTag?.code ?? '')
      }}>
        {dimensions.map((dimension) => <option key={dimension.code} value={dimension.code}>{dimension.labelZh} / {dimension.code}</option>)}
      </select>
      <select value={selectedTagCode} onChange={(event) => onChange(selectedDimension?.code ?? '', event.target.value)}>
        {tags.map((tag) => <option key={tag.id} value={tag.code}>{tag.labelZh} / {tag.code}</option>)}
      </select>
    </div>
  )
}

function TaxonomyCategoryPicker({ label, dimensions, draftDimensionCode, draftTagCode, onChange }: {
  label: string
  dimensions: { code: string; labelZh: string; tags: TaxonomyTag[] }[]
  draftDimensionCode: string
  draftTagCode: string
  onChange: (dimensionCode: string, tagCode: string) => void
}) {
  const categoryDimension = dimensions.find((dimension) => dimension.code === (draftDimensionCode || 'category')) ?? dimensions.find((dimension) => dimension.code === 'category') ?? dimensions[0]
  const tags = flattenTaxonomyTags(categoryDimension?.tags ?? [])
  const selectedTagCode = tags.some((tag) => tag.code === draftTagCode) ? draftTagCode : tags[0]?.code ?? ''
  return (
    <div className="taxonomy-tag-picker">
      <span>{label}</span>
      <select value={categoryDimension?.code ?? ''} onChange={(event) => {
        const nextDimension = dimensions.find((dimension) => dimension.code === event.target.value)
        const nextTag = flattenTaxonomyTags(nextDimension?.tags ?? [])[0]
        onChange(event.target.value, nextTag?.code ?? '')
      }}>
        {dimensions.filter((dimension) => dimension.code === 'category').map((dimension) => <option key={dimension.code} value={dimension.code}>{dimension.labelZh} / {dimension.code}</option>)}
      </select>
      <select value={selectedTagCode} onChange={(event) => onChange(categoryDimension?.code ?? 'category', event.target.value)}>
        {tags.map((tag) => <option key={tag.id} value={tag.code}>{tag.labelZh} / {tag.code}</option>)}
      </select>
    </div>
  )
}

function RecordsPage({ records, detail, setDetail, run }: { records: GenerationTask[]; detail: GenerationTask | null; setDetail: (task: GenerationTask | null) => void; run: any }) {
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
  const [pageSize, setPageSize] = useState(12)
  const selectableRecordIds = items.filter((item) => !isTaskRunning(item)).map((item) => item.id)
  const selectedVisibleRecords = items.filter((item) => selectedRecordIds.has(item.id) && selectableRecordIds.includes(item.id))
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

  function cancelRecordTask(item: GenerationTask) {
    if (!window.confirm('确定取消本次生成吗？已提交给生成服务的图片可能仍会完成并产生费用；系统会停止后续生成，并保留已完成结果。')) return
    void run('取消生成中...', () => api.cancelTask(item.id), (r: { item: GenerationTask }) => {
      setItems((current) => current.map((entry) => entry.id === item.id ? r.item : entry))
      if (detail?.id === item.id) setDetail(r.item)
    }, { refresh: 'background' })
  }

  return (
    <section className="page">
      <PageTitle title="生成记录" subtitle="查看历史拍摄任务、分镜预览和最终成片。" />
      <div className="records-layout">
        <div className="panel record-list">
          <div className="record-list-head">
            <div className="archive-controls">
              <label><input type="checkbox" checked={includeArchived} onChange={(event) => {
                setOffset(0)
                setIncludeArchived(event.target.checked)
                setSelectedRecordIds(new Set())
              }} /> 查看已归档</label>
              <label><input type="checkbox" checked={allVisibleRecordsSelected} disabled={!selectableRecordIds.length} onChange={(event) => toggleVisibleRecordSelection(event.target.checked)} /> 全选本页</label>
            </div>
            <div className="record-list-meta">
              {selectedVisibleRecords.length > 0 && (
                <button className="secondary" onClick={runRecordBatchAction}>
                  归档 {selectedVisibleRecords.length} 项
                </button>
              )}
              <span className="muted-count">{total ? `${offset + 1}-${Math.min(offset + items.length, total)} 条` : '0 条'}</span>
            </div>
          </div>
          {items.map((item) => (
            <div
              key={item.id}
              className={`record-row ${detail?.id === item.id ? 'active' : ''} ${item.archivedAt ? 'is-archived' : ''}`}
            >
              <input
                type="checkbox"
                checked={selectedRecordIds.has(item.id)}
                disabled={isTaskRunning(item)}
                onChange={(event) => toggleRecordSelection(item.id, event.target.checked)}
                aria-label={`选择 ${item.name}`}
              />
              <button className="record-open" aria-current={detail?.id === item.id ? 'true' : undefined} title="查看记录详情" onClick={() => openRecord(item)}>
                <AssetThumb asset={item.summary?.cover ?? null} interactive={false} />
                <div><strong>{item.name}</strong><span>{item.archivedAt ? '已归档' : statusText(item.status)} · {dateText(item.createdAt)}</span></div>
                <div className="strip">{(item.summary?.finalImages ?? []).slice(0, 5).map((asset) => <AssetThumb key={asset.id} asset={asset} interactive={false} />)}</div>
                <span className="record-eye" aria-hidden="true"><Eye size={16} /></span>
              </button>
              <button
                className="secondary record-action-button"
                onClick={() => isTaskRunning(item) ? cancelRecordTask(item) : openRecordDeleteDialog(item)}
              >
                {isTaskRunning(item) ? '取消生成' : '归档'}
              </button>
            </div>
          ))}
          {!items.length && <EmptyState title="暂无生成记录" text="创建拍摄任务后会出现在这里。" />}
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
              <Meta label="拍摄剧本" value={detail.shootScript ? '已生成' : '未生成'} />
              {detail.shootScript?.summary?.summary && <p className="record-script-summary">{String(detail.shootScript.summary.summary)}</p>}
              {(detail.storyboardPreview?.asset ?? detail.preview?.asset) && <img className="detail-preview" src={mediaUrl(assetPreviewSrc((detail.storyboardPreview?.asset ?? detail.preview?.asset) as MediaAsset))} />}
              <FinalImageStrip images={detail.resultSet?.images ?? []} />
            </>
          ) : <EmptyState title="选择一条记录" text="查看分镜预览、搭配服饰和最终成片。" />}
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
    </section>
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
          <button type="button" className="secondary" onClick={onClose}>取消</button>
          <button className={purge ? 'danger' : 'primary'} disabled={!canConfirm}>
            {submitting ? '处理中...' : '确认'}
          </button>
        </div>
      </form>
    </div>,
    document.body,
  )
}

function PaginationControls({ offset, pageSize, pageSizeOptions = defaultPageSizeOptions, total, totalPages, disabled, compact = false, onOffsetChange, onPageSizeChange }: {
  offset: number
  pageSize: number
  pageSizeOptions?: readonly number[]
  total: number
  totalPages: number
  disabled?: boolean
  compact?: boolean
  onOffsetChange: (offset: number) => void
  onPageSizeChange: (pageSize: number) => void
}) {
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
        <button className="secondary" disabled={!canPrev} onClick={() => goToPage(1)}>首页</button>
        <button className="secondary" disabled={!canPrev} onClick={() => goToPage(currentPage - 1)}><ChevronLeft size={16} />上一页</button>
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
        <button className="secondary" disabled={!canNext} onClick={() => goToPage(currentPage + 1)}>下一页<ChevronRight size={16} /></button>
        <button className="secondary" disabled={!canNext} onClick={() => onOffsetChange(lastOffset)}>尾页</button>
      </div>
      <div className="pagination-meta">
        <span>共 {total} 条</span>
        <span>{total > 0 ? `${firstItem}-${lastItem} 条` : '当前 0 条'}</span>
        <label>每页
          <select value={pageSize} disabled={disabled} onChange={(event) => onPageSizeChange(Number(event.target.value))}>
            {pageSizeOptions.map((value) => <option key={value} value={value}>{value} 行</option>)}
          </select>
        </label>
      </div>
    </div>
  )
}

function PageTitle({ title, subtitle, actions }: { title: string; subtitle: string; actions?: ReactNode }) {
  return <div className="page-title"><div><h1>{title}</h1><p>{subtitle}</p></div>{actions}</div>
}

function UploadBox({ files, setFiles, max, compact = false }: { files: File[]; setFiles: (files: File[]) => void; max: number; compact?: boolean }) {
  return (
    <label className={`upload-box ${compact ? 'compact' : ''}`}>
      <input type="file" accept="image/png,image/jpeg,image/webp" multiple onChange={(e) => setFiles(Array.from(e.target.files ?? []).slice(0, max))} />
      <Upload size={22} />
      <strong>上传图片</strong>
      <span>已选择 {files.length}/{max} 张</span>
    </label>
  )
}

function GarmentCard({ item, onTag, onOpen, onDelete, selected, onSelect }: {
  item: StylingGarment
  onTag?: () => void
  onOpen?: () => void
  onDelete?: () => void
  selected?: boolean
  onSelect?: (checked: boolean) => void
}) {
  const tags = item.aiTags ?? {}
  const effective = item.effectiveTags ?? {}
  const meta = item.tagMeta ?? {}
  const compatibility = typeof tags.styling_compatibility === 'object' && tags.styling_compatibility ? tags.styling_compatibility as Record<string, unknown> : {}
  const brief = tagText(effective.pairing_brief || compatibility.pairing_brief)
  const tagging = item.status === 'TAGGING_RUNNING'
  const failed = item.status === 'TAGGING_FAILED'
  const showTagResult = !tagging && !failed
  const chips = garmentTagChips(item)
  const runningTagAttempt = latestRunningTagAttempt(item)
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
      {failed && item.tagError && <span className="inline-error">{item.tagError}</span>}
      <p>{tagging ? '正在识别服饰信息，完成后可进入智能选搭。' : failed ? '打标未完成，可查看详情或重新打标。' : brief || '待识别搭配信息'}</p>
      {tagging && runningTagAttempt && <AIOperationList operations={[runningTagAttempt]} compact />}
      <div className="tag-summary">
        {showTagResult && chips.map((chip) => <span key={chip}>{chip}</span>)}
        {(!showTagResult || !chips.length) && <span>{tagging ? '识别中' : failed ? '待重新打标' : '待识别'}</span>}
      </div>
      {meta.overall_confidence !== undefined && showTagResult && <span className="confidence-text">AI 置信度 {formatConfidence(meta.overall_confidence)}</span>}
      <div className="card-actions">
        {onOpen && <button className="secondary" onClick={onOpen}><Eye size={16} /> 详情</button>}
        {onTag && <button className="secondary" disabled={tagging} onClick={onTag}>重新识别标签</button>}
        {onDelete && <button className="secondary" disabled={tagging} onClick={onDelete}><Trash2 size={16} /> 归档</button>}
      </div>
    </div>
  )
}

function GarmentDetailDrawer({ item, run, onClose, onChange, onDelete }: {
  item: StylingGarment
  run: any
  onClose: () => void
  onChange: (item: StylingGarment) => void
  onDelete: () => void
}) {
  const [working, setWorking] = useState(item.images ?? [])
  const [pendingFiles, setPendingFiles] = useState<File[]>([])
  const [replacementFiles, setReplacementFiles] = useState<Record<number, File>>({})
  const [removedIds, setRemovedIds] = useState<number[]>([])
  const [dirty, setDirty] = useState(false)
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const [viewer, setViewer] = useState<LightboxImage | null>(null)
  const [pendingDimensions, setPendingDimensions] = useState<Record<string, { width: number; height: number }>>({})
  const [replacementDimensions, setReplacementDimensions] = useState<Record<number, { width: number; height: number }>>({})
  const tagging = item.status === 'TAGGING_RUNNING'
  const runningTagAttempt = latestRunningTagAttempt(item)
  const failedTagAttempt = latestFailedTagAttempt(item)
  const hasTaggingFailure = item.status === 'TAGGING_FAILED' || Boolean(item.tagError || failedTagAttempt)

  useEffect(() => {
    setWorking(item.images ?? [])
    setPendingFiles([])
    setReplacementFiles({})
    setReplacementDimensions({})
    setRemovedIds([])
    setDirty(false)
  }, [item.id])

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
    const visibleCount = working.length + pendingFiles.length
    if (visibleCount > 6) throw new Error('单套搭配服饰最多保存 6 张图片')
    await run(retag ? '保存并重新识别标签中...' : '保存搭配服饰中...', async () => {
      await Promise.all(Object.entries(replacementFiles).map(([id, file]) => api.replaceStylingGarmentImage(Number(id), file)))
      const addBeforeDelete = working.length === 0 && pendingFiles.length > 0
      if (addBeforeDelete) await api.addStylingGarmentImages(item.id, pendingFiles)
      await Promise.all(removedIds.map((id) => api.deleteStylingGarmentImage(id)))
      await Promise.all(working.map((image, index) => api.updateStylingGarmentImage(image.id, { sortOrder: index })))
      if (pendingFiles.length && !addBeforeDelete) await api.addStylingGarmentImages(item.id, pendingFiles)
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
          <button className="secondary icon-button" onClick={onClose} title="关闭"><X size={16} /></button>
        </div>
        <div className="drawer-section">
          <Meta label="更新时间" value={dateText(item.updatedAt)} />
          <Meta label="图片" value={`${working.length + pendingFiles.length}/6 张`} />
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
            <label className={`secondary ${tagging ? 'is-disabled' : ''}`}>
              <ImagePlus size={16} /> 追加
              <input disabled={tagging} type="file" accept="image/png,image/jpeg,image/webp" multiple onChange={(event) => {
                setPendingFiles((current) => [...current, ...Array.from(event.target.files ?? [])])
                setDirty(true)
              }} />
            </label>
          </div>
          <p className="edit-hint">此次修改会影响标签识别。排序第一张会作为封面和主要参考图。</p>
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
                      <img src={mediaUrl(assetThumbSrc(image.asset))} alt={image.asset.filename} />
                    </button>
                  )}
                  <span>{replacement?.name ?? image.asset.filename}<small>{replacement ? fileMeta(replacement, replacementDimensions[image.id]) : assetMeta(image.asset)}</small></span>
                  <div className="thumb-actions">
                    {replacement ? (
                      <FilePreviewAction file={replacement} dimensions={replacementDimensions[image.id]} onOpen={(nextImage) => setViewer(nextImage)} />
                    ) : (
                      <button onClick={() => setViewer(assetLightboxImage(image.asset, working.map((itemImage) => itemImage.asset)))} title="放大查看"><Eye size={14} /></button>
                    )}
                    <button disabled={tagging} onClick={() => move(index, index - 1)} title="前移"><ArrowUp size={14} /></button>
                    <button disabled={tagging} onClick={() => move(index, index + 1)} title="后移"><ArrowDown size={14} /></button>
                    <label className={tagging ? 'is-disabled' : ''} title="替换图片">
                      <ImagePlus size={14} />
                      <input disabled={tagging} type="file" accept="image/png,image/jpeg,image/webp" onChange={(event) => {
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
            {pendingFiles.map((file, index) => (
              <div className="saved-image-card is-pending" key={`${file.name}-${file.size}-${index}`}>
                <FilePreviewButton
                  file={file}
                  dimensions={pendingDimensions[fileKey(file)]}
                  onDimensions={(value) => setPendingDimensions((current) => ({ ...current, [fileKey(file)]: value }))}
                  onOpen={(image) => setViewer(image)}
                />
                <span>{file.name}<small>{fileMeta(file, pendingDimensions[fileKey(file)])}</small></span>
                <div className="thumb-actions">
                  <FilePreviewAction file={file} dimensions={pendingDimensions[fileKey(file)]} onOpen={(image) => setViewer(image)} />
                  <button onClick={() => setPendingFiles((files) => files.filter((_, fileIndex) => fileIndex !== index))} title="删除图片"><Trash2 size={14} /></button>
                </div>
              </div>
            ))}
          </div>
          {working.length + pendingFiles.length > 6 && <span className="inline-error">超过 6 张，需删除后才能保存</span>}
        </div>
        <div className="drawer-section">
          <div className="section-head"><strong>标签</strong></div>
          <TagObjectPanel title={hasTaggingFailure ? '上次 AI 打标返回' : 'AI 打标返回'} data={item.aiTags ?? {}} defaultOpen />
          <TagObjectPanel title="人工业务标签" data={item.manualTags ?? {}} />
        </div>
        <div className="drawer-actions">
          <button className="secondary" disabled={tagging} onClick={retag}>重新打标</button>
          <button className="secondary" disabled={tagging || !dirty || working.length + pendingFiles.length > 6} onClick={() => void save(false)}>仅保存</button>
          <button className="primary" disabled={tagging || !dirty || working.length + pendingFiles.length > 6} onClick={() => void save(true)}>保存并重新识别标签</button>
        </div>
        <div className="danger-zone">
          <strong>归档</strong>
          <button className="secondary" disabled={tagging} onClick={() => {
            onDelete()
            onClose()
          }}>归档</button>
        </div>
      </aside>
      {viewer && <ImageLightbox image={viewer} onClose={() => setViewer(null)} />}
    </div>
  )
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
        <button className="secondary" disabled={disabled || !changed} onClick={() => void run('保存货号中...', () => api.updateStylingGarment(item.id, { code: value }), undefined, { refresh: 'background' })}>保存</button>
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

function AIOperationList({ operations, compact = false }: { operations: AIOperation[]; compact?: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const merged = mergeOperations(operations.filter(Boolean))
  const maxVisible = compact ? 4 : expanded ? merged.length : 3
  const active = merged.slice(0, maxVisible)
  const now = useTicker(active.some(isAttemptRunning))
  if (!active.length) return null
  return (
    <div className={`ai-operation-list ${compact ? 'compact' : ''}`}>
      {active.map((operation, index) => (
        <AIOperationCard key={`${operation.id}-${operation.ownerType}-${operation.ownerId}-${index}`} operation={operation} now={now} compact={compact} index={index} />
      ))}
      {!compact && !expanded && merged.length > maxVisible && (
        <button className="operation-more" onClick={() => setExpanded(true)}>
          展开其余 {merged.length - maxVisible} 项进度
        </button>
      )}
      {!compact && expanded && merged.length > 3 && (
        <button className="operation-more" onClick={() => setExpanded(false)}>
          收起进度
        </button>
      )}
    </div>
  )
}

function AIOperationCard({ operation, now, compact, index }: { operation: AIOperation; now: number; compact?: boolean; index: number }) {
  const running = isAttemptRunning(operation)
  const seconds = elapsedSeconds(operation.startedAt || operation.createdAt, now, operation.completedAt || operation.failedAt)
  return (
    <div className={`ai-operation-card ${running ? 'is-running' : ''} ${compact ? 'compact' : ''}`}>
      <div className="ai-operation-icon">{running ? <Loader2 size={15} className="spin" /> : <Clock3 size={15} />}</div>
      <div>
        <strong>{operationLabel(operation, index)}</strong>
        <span>{statusText(operation.status)}</span>
      </div>
      <time>{formatDuration(seconds)}</time>
    </div>
  )
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
  const local = localOperations.filter((operation) => operation.ownerType === ownerType)
  const runningRemote = remote.filter(isAttemptRunning)
  const runningLocal = local.filter(isAttemptRunning)
  if (runningRemote.length || runningLocal.length) return mergeOperations([...runningRemote, ...runningLocal])
  if (remote.length) return mergeOperations([...remote, ...local])
  if (local.length) return local
  if (ownerType === 'generation_styling' && task.status === 'STYLING_RUNNING') {
    return [syntheticOperation(ownerType, task.id, 'gemini', task.updatedAt)]
  }
  if (ownerType === 'generation_shoot_script' && task.status === 'SCRIPT_RUNNING') {
    return [syntheticOperation(ownerType, task.activeShootScriptId ?? task.id, 'gemini', task.shootScript?.createdAt || task.updatedAt)]
  }
  if (ownerType === 'generation_preview' && task.status === 'PREVIEW_RUNNING') {
    return [syntheticOperation(ownerType, task.activeStoryboardPreviewId ?? task.activePreviewId ?? task.id, 'image-2', task.storyboardPreview?.createdAt || task.preview?.createdAt || task.updatedAt)]
  }
  return []
}

function finalImageOperations(task: GenerationTask, localOperations: AIOperation[]) {
  const imageAttempts = (task.resultSet?.images ?? []).map((image) => image.activeAttempt).filter(Boolean) as AIOperation[]
  const local = localOperations.filter((operation) => operation.ownerType === 'generation_result_image')
  if (imageAttempts.length) return mergeOperations([...imageAttempts, ...local])
  if (local.length) return local
  if (task.status === 'FINAL_RUNNING') {
    return Array.from({ length: 5 }, (_, index) => syntheticOperation('generation_result_image', index + 1, 'image-2', task.updatedAt))
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

function visibleTaskOperations(task: GenerationTask, localOperations: AIOperation[]) {
  const activeRemote = (task.aiOperations ?? []).filter((operation) => isAttemptRunning(operation))
  const activeLocal = localOperations.filter(isAttemptRunning)
  return mergeOperations([...activeRemote, ...activeLocal])
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
  return ['generation_styling', 'generation_shoot_script', 'generation_preview'].includes(ownerType)
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
  return ['RUNNING', 'SUBMITTED', 'IN_QUEUE', 'IN_PROGRESS'].includes(operation.status)
}

function operationLabel(operation: AIOperation, index = 0) {
  if (operation.ownerType === 'styling_garment_upload') return '搭配服饰上传'
  if (operation.ownerType === 'styling_garment_tagging') return '服饰标签识别'
  if (operation.ownerType === 'generation_styling') return '智能选搭'
  if (operation.ownerType === 'generation_shoot_script') return '拍摄剧本'
  if (operation.ownerType === 'generation_preview') return '分镜预览'
  if (operation.ownerType === 'generation_result_image') return `最终图 ${operation.ownerId > 0 && operation.id < 0 ? operation.ownerId : index + 1}`
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
  const image = asset && src && !failed ? <img src={mediaUrl(src)} alt={asset.filename} onError={() => setFailed(true)} /> : null
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

function ImageLightbox({ image, onClose }: { image: LightboxImage; onClose: () => void }) {
  const [items] = useState<LightboxImageItem[]>(() => image.items?.length ? image.items : [image])
  const [index, setIndex] = useState(() => image.initialIndex ?? 0)
  const [zoom, setZoom] = useState<'fit' | number>('fit')
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
    fetch(current.src, { method: 'HEAD' })
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
            {current.downloadUrl && <a href={current.downloadUrl} target="_blank" rel="noreferrer" title="打开原图"><Download size={16} /></a>}
            <button onClick={() => setNext()} disabled={!canStep} title="下一张"><ChevronRight size={16} /></button>
            <button onClick={onClose} title="关闭"><X size={16} /></button>
          </div>
        </div>
      </div>
      <div className="lightbox-stage" onClick={(event) => event.stopPropagation()}>
        <img
          src={current.src}
          alt={current.title}
          className={zoom === 'fit' ? 'is-fit' : 'is-zoomed'}
          style={zoomedStyle}
          onLoad={(event) => {
            const img = event.currentTarget
            setNaturalSizes((value) => ({ ...value, [current.src]: { width: img.naturalWidth, height: img.naturalHeight } }))
          }}
        />
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
                <img src={item.thumbSrc ?? item.src} alt={item.title} />
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
    if (task.resultSet?.images?.length) return 4
    if (task.storyboardPreview || task.preview) return 3
    if (task.shootScript || task.styling) return 2
    return 1
  }
  if (task.status === 'FAILED') {
    if (task.resultSet?.images?.length) return 4
    if (task.storyboardPreview || task.preview) return 3
    if (task.shootScript || task.styling) return 2
    return 1
  }
  if (['READY_FOR_STYLING', 'STYLING_RUNNING'].includes(task.status)) return 1
  if (['STYLING_READY', 'SCRIPT_RUNNING', 'SCRIPT_READY'].includes(task.status)) return 2
  if (['PREVIEW_RUNNING', 'PREVIEW_READY'].includes(task.status)) return 3
  if (['FINAL_RUNNING', 'FINAL_PARTIAL', 'COMPLETED'].includes(task.status)) return 4
  return 0
}

function isTaskRunning(task: GenerationTask) {
  return ['STYLING_RUNNING', 'SCRIPT_RUNNING', 'PREVIEW_RUNNING', 'FINAL_RUNNING'].includes(task.status)
}

function statusText(status: string) {
  const map: Record<string, string> = {
    DRAFT: '待确认',
    READY_FOR_STYLING: '待选搭',
    STYLING_RUNNING: '选搭中',
    STYLING_READY: '已选搭',
    SCRIPT_RUNNING: '剧本生成中',
    SCRIPT_READY: '剧本完成',
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

function compactTagLine(value: Record<string, unknown>) {
  const keys = ['category', 'sub_category', 'colors', 'season', 'occasion', 'pairing_brief', 'year', 'official_series']
  const parts = keys
    .filter((key) => value[key] !== undefined && value[key] !== null && value[key] !== '')
    .map((key) => `${key}: ${Array.isArray(value[key]) ? (value[key] as unknown[]).join('/') : typeof value[key] === 'object' ? JSON.stringify(value[key]) : String(value[key])}`)
  return parts.length ? parts.slice(0, 4).join(' · ') : '暂无'
}

function groupImportFiles(files: File[]): ImportBatch[] {
  const imageFiles = files
    .filter((file) => file.type.startsWith('image/') || /\.(png|jpe?g|webp)$/i.test(file.name))
    .sort((a, b) => importPath(a).localeCompare(importPath(b), 'zh-CN', { numeric: true }))
  if (!imageFiles.length) return []

  const paths = imageFiles.map(importPath)
  const hasFolders = paths.some((path) => path.includes('/'))
  if (!hasFolders) {
    return [{ id: `loose-${Date.now()}`, code: timestampName(), files: imageFiles }]
  }

  const splitPaths = paths.map((path) => path.split('/').filter(Boolean))
  const roots = new Set(splitPaths.map((parts) => parts[0]))
  const root = roots.size === 1 ? splitPaths[0][0] : ''
  const hasImagesAtRoot = Boolean(root) && splitPaths.some((parts) => parts.length === 2)
  const hasNestedFolders = Boolean(root) && splitPaths.some((parts) => parts.length > 2)
  const grouped = new Map<string, File[]>()

  imageFiles.forEach((file) => {
    const parts = importPath(file).split('/').filter(Boolean)
    const key = roots.size === 1 && hasNestedFolders && !hasImagesAtRoot ? parts.slice(0, 2).join('/') : parts[0]
    grouped.set(key, [...(grouped.get(key) ?? []), file])
  })

  return Array.from(grouped.entries()).map(([key, groupFiles], index) => ({
    id: `${key}-${index}`,
    code: sanitizeClientName(key.split('/').pop() || key || timestampName()),
    files: groupFiles,
  }))
}

function importPath(file: File) {
  return ((file as File & { webkitRelativePath?: string; relativePath?: string }).webkitRelativePath || (file as File & { relativePath?: string }).relativePath || file.name).replace(/^\/+/, '')
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
  if (impact.garmentCount) parts.push(`${impact.garmentCount} 套搭配服饰`)
  if (impact.garmentImageCount) parts.push(`${impact.garmentImageCount} 张服饰图`)
  const taskCount = impact.dependentTaskCount ?? impact.taskCount ?? 0
  if (taskCount) parts.push(`${taskCount} 条生成记录`)
  if (impact.shootScriptCount) parts.push(`${impact.shootScriptCount} 份拍摄剧本`)
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
    downloadUrl: src,
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
    downloadUrl: src,
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

function sanitizeClientName(value: string) {
  return value.trim().replace(/[^\u4e00-\u9fa5A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 80)
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

function angleLabel(angle?: string | null) {
  const map: Record<string, string> = { front: '正脸', left45: '左45度', right45: '右45度', right90: '右侧脸', left90: '左侧脸', back: '背面', backTurnLeft45: '背面回头', down45: '低头' }
  return map[angle ?? ''] ?? '未设置'
}

function fallbackHeadRefs() {
  return ['front', 'left45', 'right45', 'right90', 'backTurnLeft45', 'down45'].map((id) => ({
    id,
    angle: id,
    filename: id,
    previewUrl: null as string | null,
  }))
}

function timestampName() {
  const date = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`
}

function dateText(value: string) {
  return new Date(parseRuyiTime(value)).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}
