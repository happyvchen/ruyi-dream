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
import type { AIOperation, DeleteImpact, GenerationTask, MediaAsset, ModelProfile, StylingGarment } from './api'
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

const steps = [
  { title: '上传主推款', icon: Upload },
  { title: 'AI 智能选搭', icon: WandSparkles },
  { title: '六宫格预览', icon: GalleryVerticalEnd },
  { title: '生成成片', icon: Sparkles },
] as const

const defaultPageSizeOptions = [20, 50, 100] as const
const densePageSizeOptions = [12, 50, 100] as const

export function App() {
  const [checked, setChecked] = useState(false)
  const [authenticated, setAuthenticated] = useState(false)
  const [page, setPage] = useState<Page>(() => parseRoute().page)
  const [collapsed, setCollapsed] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
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
        setChecked(true)
      })
      .catch(() => setChecked(true))
  }, [])

  useEffect(() => {
    if (!authenticated) return
    void refreshAll()
  }, [authenticated])

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

  async function run<T>(label: string, action: () => Promise<T>, after?: (value: T) => void) {
    setBusy(label)
    setError('')
    try {
      const value = await action()
      after?.(value)
      await refreshAll().catch(() => undefined)
      return value
    } catch (err) {
      setLocalOperations((items) => items.filter((item) => !isAttemptRunning(item)))
      await refreshAll().catch(() => undefined)
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
  if (!authenticated) return <LoginPage onAuthed={() => setAuthenticated(true)} />

  const nav = [
    { id: 'studio' as const, label: 'AI 摄影棚', icon: Bot },
    { id: 'library' as const, label: '搭配服饰库', icon: Library },
    { id: 'model' as const, label: '数字模特', icon: UserRound },
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
            {!collapsed && <div><span>用户</span><strong>user</strong></div>}
            {!collapsed && <button className="ghost-icon" onClick={() => void api.logout().then(() => setAuthenticated(false))}><LogOut size={16} /></button>}
          </div>
        </div>
      </aside>
      <main className="workspace">
        {error && <div className="toast">{error}</div>}
        {busy && <div className="busy"><Loader2 size={16} className="spin" />{busy}</div>}
        {page === 'studio' && <StudioPage task={task} setTask={setTask} records={records} run={run} localOperations={localOperations} startLocalOperation={startLocalOperation} clearLocalOperationsFor={clearLocalOperationsFor} />}
        {page === 'library' && <LibraryPage garments={garments} run={run} />}
        {page === 'model' && <ModelPage models={models} />}
        {page === 'records' && <RecordsPage records={records} detail={recordDetail} setDetail={setRecordDetail} run={run} />}
      </main>
    </div>
  )
}

function BootScreen() {
  return <div className="login-screen"><div className="login-card"><Loader2 className="spin" /> 启动中...</div></div>
}

function LoginPage({ onAuthed }: { onAuthed: () => void }) {
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
      await api.login(username, password)
      onAuthed()
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
        <p>{showDemoLogin ? '保护性入口。当前演示账号为 user / password。' : '保护性入口。请输入管理员提供的访问凭据。'}</p>
        <label>用户名<input value={username} onChange={(e) => setUsername(e.target.value)} /></label>
        <label>密码<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
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
  run: <T>(label: string, action: () => Promise<T>, after?: (value: T) => void) => Promise<T | undefined>
  localOperations: AIOperation[]
  startLocalOperation: (operation: Omit<AIOperation, 'id' | 'status' | 'startedAt'> & { status?: string; startedAt?: string | null }) => AIOperation
  clearLocalOperationsFor: (ownerTypes: string[]) => void
}) {
  const [step, setStep] = useState<StudioStep>(0)
  const [taskItems, setTaskItems] = useState<GenerationTask[]>(records.slice(0, 10))
  const currentStep = getTaskStep(task)
  const visibleStep = Math.min(Math.max(step, 0), currentStep) as StudioStep
  function cancelCurrentTask() {
    if (!task || !isTaskRunning(task)) return
    if (!window.confirm('确定取消本次生成吗？已提交给生成服务的图片可能仍会完成并产生费用；系统会停止后续生成，并保留已完成结果。')) return
    void run('取消生成中...', () => api.cancelTask(task.id), (r: { item: GenerationTask }) => {
      clearLocalOperationsFor(['generation_styling', 'generation_preview', 'generation_result_image'])
      setTask(r.item)
    })
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
    if (!task || !['STYLING_RUNNING', 'PREVIEW_RUNNING', 'FINAL_RUNNING'].includes(task.status)) return
    const timer = window.setInterval(() => {
      void api.getTask(task.id).then((result) => setTask(result.item)).catch(() => undefined)
    }, 1500)
    return () => window.clearInterval(timer)
  }, [task?.id, task?.status, setTask])
  return (
    <section className="page">
      <PageTitle title="AI 摄影棚" subtitle="上传主推款，让系统选搭配角服饰并生成电商棚拍成片。" />
      <div className="studio-grid">
        <div className="panel studio-main">
          <StepRail active={visibleStep} max={currentStep} onSelect={setStep} />
          {visibleStep === 0 && <UploadHeroStep task={task} setTask={setTask} run={run} />}
          {visibleStep === 1 && <StylingStep task={task} setTask={setTask} run={run} localOperations={localOperations} startLocalOperation={startLocalOperation} clearLocalOperationsFor={clearLocalOperationsFor} />}
          {visibleStep === 2 && <PreviewStep task={task} setTask={setTask} run={run} localOperations={localOperations} startLocalOperation={startLocalOperation} clearLocalOperationsFor={clearLocalOperationsFor} />}
          {visibleStep === 3 && <FinalStep task={task} setTask={setTask} run={run} localOperations={localOperations} startLocalOperation={startLocalOperation} clearLocalOperationsFor={clearLocalOperationsFor} />}
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
                <button onClick={() => void api.getRecord(item.id).then((r) => {
                  setTask(r.item)
                  window.history.pushState(null, '', routeToUrl({ page: 'studio', taskId: item.id }))
                })}>
                  <span>{item.name}</span><em>{item.archivedAt ? '已归档' : statusText(item.status)}</em>
                </button>
                {isTaskRunning(item) && (
                  <button
                    className="secondary mini-action"
                    onClick={() => {
                      if (!window.confirm('确定取消本次生成吗？已提交给生成服务的图片可能仍会完成并产生费用；系统会停止后续生成，并保留已完成结果。')) return
                      void run('取消生成中...', () => api.cancelTask(item.id), (r: { item: GenerationTask }) => {
                        clearLocalOperationsFor(['generation_styling', 'generation_preview', 'generation_result_image'])
                        if (task?.id === item.id) setTask(r.item)
                        setTaskItems((current) => current.map((entry) => entry.id === item.id ? r.item : entry))
                      })
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
      <div className="hero-upload" onDragOver={(event) => event.preventDefault()} onDrop={(event) => void handleDrop(event)}>
        <div>
          <Images size={22} />
          <strong>{task?.status === 'DRAFT' ? '拖入图片或文件夹新建另一项拍摄任务' : '拖入主推款图片或文件夹'}</strong>
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
              window.history.pushState(null, '', routeToUrl({ page: 'studio', taskId: r.item.id }))
            })}>
              <ImagePlus size={16} /> 创建任务
            </button>
          </div>
        </div>
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
    }, (result: { item: GenerationTask }) => setTask(result.item))
  }

  return (
    <div className="hero-draft">
      <div className="section-head"><strong>当前草稿</strong><span className="status draft">待确认</span></div>
      <div className="inline-form">
        <input value={name} onChange={(event) => setName(event.target.value)} placeholder="任务名称" />
        <button className="secondary" disabled={name.trim() === task.name} onClick={() => void run('保存任务名称中...', () => api.updateTask(task.id, { name }), (r: { item: GenerationTask }) => setTask(r.item))}>保存名称</button>
        <label className="secondary">
          <ImagePlus size={16} /> 追加图片
          <input type="file" accept="image/png,image/jpeg,image/webp" multiple onChange={(event) => {
            const next = Array.from(event.target.files ?? [])
            if (next.length) void run('追加主推款图片中...', () => api.addTaskImages(task.id, next), (r: { item: GenerationTask }) => setTask(r.item))
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
                  if (replacement) void run('替换主推款图片中...', () => api.replaceTaskImage(image.id, replacement), (r: { item: GenerationTask }) => setTask(r.item))
                }} />
              </label>
              <button disabled={(task.images?.length ?? 0) <= 1} onClick={() => void run('移除主推款图片中...', () => api.deleteTaskImage(image.id), (r: { item: GenerationTask }) => setTask(r.item))} title="删除图片"><Trash2 size={14} /></button>
            </div>
          </div>
        ))}
      </div>
      <p className="edit-hint">确认后可进入智能选搭；系统会保留本次主推款和默认数字模特信息用于后续生成。</p>
      <button className="primary" onClick={() => void run('确认主推款输入中...', () => api.readyTask(task.id), (r: { item: GenerationTask }) => setTask(r.item))}>
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
  const canSelect = !!task && ['READY_FOR_STYLING', 'STYLING_READY', 'PREVIEW_READY', 'COMPLETED', 'FINAL_PARTIAL', 'FAILED', 'CANCELLED'].includes(task.status)
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
        setTask({ ...task, status: 'STYLING_RUNNING', preview: null, resultSet: null, activePreviewId: null, activeResultSetId: null })
        return api.selectStyling(task.id)
      }, (r: { item: GenerationTask }) => {
        clearLocalOperationsFor(['generation_styling'])
        setTask(r.item)
      })}>
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
  const selectedOutfitId = stringValue(planValue(plan, 'selected_outfit_id'))
  const selected = candidates.find((item) => stringValue(item.id) === selectedOutfitId) ?? candidates[0]
  const scores = (styling.criticScores && Object.keys(styling.criticScores).length ? styling.criticScores : planValue<Record<string, unknown>>(selected ?? {}, 'scores') ?? {}) as Record<string, unknown>
  const generatedAccessories = stringList(planValue(selected ?? {}, 'generated_accessories'))
  const heroFocus = stringList(planValue(selected ?? {}, 'hero_focus'))
  const aestheticDiagnosis = diagnosisItems(planValue<Record<string, unknown>>(selected ?? {}, 'aesthetic_diagnosis') ?? {})
  const rejected = styling.rejectedCandidates?.length ? styling.rejectedCandidates : planValue<Record<string, unknown>[]>(plan, 'rejected_candidates') ?? []
  return (
    <div className="styling-plan">
      <div className="styling-plan-head">
        <div>
          <span>推荐造型方案</span>
          <h3>{stringValue(planValue(selected ?? {}, 'mode')) || '主推款优先'}</h3>
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
          {rejected.slice(0, 3).map((item, index) => <span key={`${stringValue(item.id)}-${index}`}>{stringValue(item.reason)}</span>)}
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
  return typeof value === 'string' ? value : ''
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : []
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

function PreviewStep({ task, setTask, run, localOperations, startLocalOperation, clearLocalOperationsFor }: {
  task: GenerationTask | null
  setTask: (task: GenerationTask) => void
  run: any
  localOperations: AIOperation[]
  startLocalOperation: (operation: Omit<AIOperation, 'id' | 'status' | 'startedAt'> & { status?: string; startedAt?: string | null }) => AIOperation
  clearLocalOperationsFor: (ownerTypes: string[]) => void
}) {
  const [viewer, setViewer] = useState<LightboxImage | null>(null)
  const canPreview = (
    !!task?.styling && ['STYLING_READY', 'PREVIEW_READY', 'COMPLETED', 'FINAL_PARTIAL', 'CANCELLED'].includes(task?.status ?? '')
  ) || (task?.status === 'FAILED' && !!task.styling && !task.preview)
  const hasPreview = !!task?.preview?.asset
  const operations = task ? operationFallbacks(task, localOperations, 'generation_preview') : []
  return (
    <div className="step-panel">
      <div className="step-copy"><h2>六宫格预览</h2><p>只显示一张 3x2 预览图。最终固定生成前 5 格，第 6 格忽略。</p></div>
      <AIOperationList operations={operations} />
      {task?.preview?.asset ? (
        <button className="preview-board-open" onClick={() => task.preview?.asset && setViewer(assetLightboxImage(task.preview.asset))} title="放大查看">
          <img className="preview-board" src={mediaUrl(assetPreviewSrc(task.preview.asset))} />
        </button>
      ) : <EmptyState title="尚未生成六宫格" text="点击生成预览后，系统会输出一张 3x2 分镜预览图。" />}
      <button className="primary" disabled={!canPreview} onClick={() => task && void run(hasPreview ? '重新生成六宫格预览中...' : '生成六宫格预览中...', () => {
        startLocalOperation({ ownerType: 'generation_preview', ownerId: task.id, provider: 'image-2' })
        setTask({ ...task, status: 'PREVIEW_RUNNING', resultSet: null, activeResultSetId: null })
        return api.previewTask(task.id)
      }, (r: { item: GenerationTask }) => {
        clearLocalOperationsFor(['generation_preview'])
        setTask(r.item)
      })}>
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
  const canGenerate = !!task?.preview?.asset && ['PREVIEW_READY', 'COMPLETED', 'FINAL_PARTIAL', 'CANCELLED'].includes(task?.status ?? '')
  const hasFinalImages = images.some((image) => !!image.asset)
  const finalOperations = task ? finalImageOperations(task, localOperations) : []
  return (
    <div className="step-panel">
      <div className="step-copy"><h2>生成成片</h2><p>并发生成前 5 格。每张图可单独下载，失败可单张重试。</p></div>
      <AIOperationList operations={finalOperations} />
      <div className="result-grid">
        {images.length ? images.map((image) => (
          <div className="result-card" key={image.id}>
            {image.asset ? (
              <button className="result-image-open" onClick={() => image.asset && setViewer(assetLightboxImage(image.asset, finalAssets))} title="放大查看">
                <img src={mediaUrl(assetPreviewSrc(image.asset))} />
              </button>
            ) : <div className="image-placeholder">{statusText(image.status)}{image.activeAttempt && isAttemptRunning(image.activeAttempt) && <small>{formatDuration(elapsedSeconds(image.activeAttempt.startedAt || image.updatedAt))}</small>}</div>}
            <div><span>#{image.sortOrder}</span>{image.asset && <a href={mediaUrl(assetOriginalSrc(image.asset))} target="_blank" rel="noreferrer"><Download size={15} /></a>}{image.status === 'FAILED' && <button onClick={() => void run('单张重试中...', () => api.retryResultImage(image.id), (r: { item: GenerationTask }) => setTask(r.item))}><RotateCcw size={14} /></button>}</div>
          </div>
        )) : <EmptyState title="尚未生成最终图" text="点击生成成片后会生成前 5 张。" />}
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
      })}>
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
  const allVisibleGarmentsSelected = selectableGarmentIds.length > 0 && selectableGarmentIds.every((id) => selectedGarmentIds.has(id))

  async function openDetail(item: StylingGarment) {
    const result = await api.getStylingGarment(item.id)
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

  useEffect(() => setSelectedGarmentIds(new Set()), [offset, includeArchived, query])

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
    void run(purge ? '彻底删除搭配服饰中...' : '归档搭配服饰中...', action, () => {
      if (detail && ids.includes(detail.id)) setDetail(null)
      setSelectedGarmentIds(new Set())
      setDeleteGarmentDialog(null)
      setDeleteGarmentImpact(null)
      void refreshLibraryPage().catch(() => undefined)
    })
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
            {selectedGarmentIds.size > 0 && (
              <button className="secondary" onClick={() => openGarmentDeleteDialog(items.filter((item) => selectedGarmentIds.has(item.id)))}>
                归档 {selectedGarmentIds.size} 项
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
              onTag={['DRAFT', 'TAGGING_FAILED', 'TAGGING_READY'].includes(item.status) ? () => void run('重新识别标签中...', () => api.tagStylingGarment(item.id)) : undefined}
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
      {detail && <GarmentDetailDrawer item={detail} run={run} onClose={() => setDetail(null)} onChange={setDetail} onDelete={() => openGarmentDeleteDialog([detail])} />}
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

  useEffect(() => setSelectedRecordIds(new Set()), [offset, includeArchived])

  function openRecord(item: GenerationTask) {
    void run('读取记录中...', () => api.getRecord(item.id, false), (r: { item: GenerationTask }) => setDetail(r.item))
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
    const ids = Array.from(selectedRecordIds)
    if (!ids.length) return
    openRecordDeleteDialogForItems(items.filter((item) => selectedRecordIds.has(item.id)))
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
    void run(purge ? '彻底删除记录中...' : '归档记录中...', action, () => {
      clearRecordDetailIfAffected(ids)
      setSelectedRecordIds(new Set())
      setDeleteRecordDialog(null)
      setDeleteRecordImpact(null)
      void refreshRecordPage().catch(() => undefined)
    })
  }

  function cancelRecordTask(item: GenerationTask) {
    if (!window.confirm('确定取消本次生成吗？已提交给生成服务的图片可能仍会完成并产生费用；系统会停止后续生成，并保留已完成结果。')) return
    void run('取消生成中...', () => api.cancelTask(item.id), (r: { item: GenerationTask }) => {
      setItems((current) => current.map((entry) => entry.id === item.id ? r.item : entry))
      if (detail?.id === item.id) setDetail(r.item)
    })
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
              {selectedRecordIds.size > 0 && (
                <button className="secondary" onClick={runRecordBatchAction}>
                  归档 {selectedRecordIds.size} 项
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
              {detail.preview?.asset && <img className="detail-preview" src={mediaUrl(assetPreviewSrc(detail.preview.asset))} />}
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
  const expectedText = items.length === 1 ? items[0].name : '全部删除'
  const canConfirm = !purge || confirmText === expectedText

  useEffect(() => {
    setPurge(false)
    setConfirmText('')
  }, [items])

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!canConfirm) return
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
            确认
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

function PageTitle({ title, subtitle }: { title: string; subtitle: string }) {
  return <div className="page-title"><div><h1>{title}</h1><p>{subtitle}</p></div></div>
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
      {item.tagError && <span className="inline-error">{item.tagError}</span>}
      <p>{tagging ? '正在识别服饰信息，完成后可进入智能选搭。' : brief || '待识别搭配信息'}</p>
      {tagging && runningTagAttempt && <AIOperationList operations={[runningTagAttempt]} compact />}
      <div className="tag-summary">
        {chips.map((chip) => <span key={chip}>{chip}</span>)}
        {!chips.length && <span>{tagging ? '识别中' : '待识别'}</span>}
      </div>
      {meta.overall_confidence !== undefined && !tagging && <span className="confidence-text">AI 置信度 {formatConfidence(meta.overall_confidence)}</span>}
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
  onChange: (item: StylingGarment | null) => void
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
      for (const [id, file] of Object.entries(replacementFiles)) await api.replaceStylingGarmentImage(Number(id), file)
      const addBeforeDelete = working.length === 0 && pendingFiles.length > 0
      if (addBeforeDelete) await api.addStylingGarmentImages(item.id, pendingFiles)
      for (const id of removedIds) await api.deleteStylingGarmentImage(id)
      for (const [index, image] of working.entries()) await api.updateStylingGarmentImage(image.id, { sortOrder: index })
      if (pendingFiles.length && !addBeforeDelete) await api.addStylingGarmentImages(item.id, pendingFiles)
      return retag ? (await api.tagStylingGarment(item.id)).item : (await api.getStylingGarment(item.id)).item
    }, (latest: StylingGarment) => onChange(latest))
  }

  return (
    <div className="drawer-backdrop">
      <aside className="detail-drawer">
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
          <TagObjectPanel title="AI 识别标签" data={item.aiTags ?? {}} defaultOpen />
          <TagObjectPanel title="生效标签" data={item.effectiveTags ?? {}} defaultOpen />
          <TagObjectPanel title="人工业务标签" data={item.manualTags ?? {}} />
          <TagObjectPanel title="官方标题信息" data={item.officialTitleTags ?? {}} />
          <TagObjectPanel title="标签元信息" data={item.tagMeta ?? {}} />
        </div>
        <div className="drawer-actions">
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
        <button className="secondary" disabled={disabled || !changed} onClick={() => void run('保存货号中...', () => api.updateStylingGarment(item.id, { code: value }))}>保存</button>
      </div>
    </div>
  )
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
  if (remote.length) return mergeOperations([...remote, ...local])
  if (local.length) return local
  if (ownerType === 'generation_styling' && task.status === 'STYLING_RUNNING') {
    return [syntheticOperation(ownerType, task.id, 'gemini', task.updatedAt)]
  }
  if (ownerType === 'generation_preview' && task.status === 'PREVIEW_RUNNING') {
    return [syntheticOperation(ownerType, task.activePreviewId ?? task.id, 'image-2', task.preview?.createdAt || task.updatedAt)]
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
  return `${operation.ownerType}:${operation.provider}:${operation.ownerId > 0 ? operation.ownerId : 'current'}`
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
  if (operation.ownerType === 'generation_preview') return '六宫格预览'
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
    if (task.resultSet?.images?.length) return 3
    if (task.preview || task.styling) return 2
    return 1
  }
  if (task.status === 'FAILED') return task.styling ? 2 : 1
  if (['READY_FOR_STYLING', 'STYLING_RUNNING'].includes(task.status)) return 1
  if (['STYLING_READY', 'PREVIEW_RUNNING'].includes(task.status)) return 2
  if (['PREVIEW_READY', 'FINAL_RUNNING', 'FINAL_PARTIAL', 'COMPLETED'].includes(task.status)) return 3
  return 0
}

function isTaskRunning(task: GenerationTask) {
  return ['STYLING_RUNNING', 'PREVIEW_RUNNING', 'FINAL_RUNNING'].includes(task.status)
}

function statusText(status: string) {
  const map: Record<string, string> = {
    DRAFT: '待确认',
    READY_FOR_STYLING: '待选搭',
    STYLING_RUNNING: '选搭中',
    STYLING_READY: '已选搭',
    PREVIEW_RUNNING: '预览中',
    PREVIEW_READY: '预览完成',
    FINAL_RUNNING: '生成中',
    FINAL_PARTIAL: '部分完成',
    COMPLETED: '已完成',
    FAILED: '失败',
    CANCELLED: '已取消',
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
    ...collectTagValues(tags.category ?? effective.category),
    ...collectTagValues(tags.sub_category ?? effective.sub_category),
    ...collectTagValues(tags.colors ?? effective.colors),
    ...collectTagValues(effective.season ?? tags.season),
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
