import { useEffect, useMemo, useState } from 'react'
import type { DragEvent, FormEvent, ReactNode } from 'react'
import {
  Bot,
  ArrowDown,
  ArrowUp,
  ChevronLeft,
  ChevronRight,
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
import type { AIOperation, GenerationTask, MediaAsset, ModelProfile, StylingGarment } from './api'
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
  title: string
  meta?: string
  downloadUrl?: string
}

const steps = [
  { title: '上传主推款', icon: Upload },
  { title: 'AI 智能选搭', icon: WandSparkles },
  { title: '六宫格预览', icon: GalleryVerticalEnd },
  { title: '生成成片', icon: Sparkles },
] as const

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
    const handlePopState = () => setPage(parseRoute().page)
    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [])

  useEffect(() => {
    if (page === 'studio' && task?.id) window.history.replaceState(null, '', routeToUrl({ page, taskId: task.id }))
    if (page === 'records' && recordDetail?.id) window.history.replaceState(null, '', routeToUrl({ page, recordId: recordDetail.id }))
  }, [page, task?.id, recordDetail?.id])

  const runningTaggingIds = useMemo(() => garments.filter((item) => item.status === 'TAGGING_RUNNING').map((item) => item.id), [garments])
  const runningTaggingKey = runningTaggingIds.join(',')
  const hasRunningTagging = runningTaggingIds.length > 0
  useEffect(() => {
    if (!authenticated || !hasRunningTagging) return
    const timer = window.setInterval(() => {
      void Promise.all(runningTaggingIds.map((id) => api.getStylingGarment(id).then((response) => response.item).catch(() => null)))
        .then((latestItems) => {
          const latestById = new Map(latestItems.filter((item): item is StylingGarment => Boolean(item)).map((item) => [item.id, item]))
          if (!latestById.size) return
          setGarments((items) => items.map((item) => latestById.get(item.id) ?? item))
        })
    }, 3000)
    return () => window.clearInterval(timer)
  }, [authenticated, hasRunningTagging, runningTaggingKey])

  const hasRunningGeneration = task?.status === 'PREVIEW_RUNNING' || task?.status === 'FINAL_RUNNING'
  useEffect(() => {
    if (!authenticated || !task?.id || !hasRunningGeneration) return
    const timer = window.setInterval(() => {
      void api.getTask(task.id).then((response) => setTask(response.item)).catch(() => undefined)
    }, 2000)
    return () => window.clearInterval(timer)
  }, [authenticated, task?.id, hasRunningGeneration])

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
    if ((!task || requestedTaskId) && (requestedTaskId || taskResult.items[0])) {
      const detail = await api.getTask(requestedTaskId ?? taskResult.items[0].id).then((r) => r.item).catch(() => null)
      if (detail) setTask(detail)
    }
    if (route.page === 'records' && route.recordId && !recordDetail) {
      const detail = await api.getRecord(route.recordId, true).then((r) => r.item).catch(() => null)
      if (detail) setRecordDetail(detail)
    }
  }

  function navigate(next: Page) {
    setPage(next)
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
        {page === 'studio' && <StudioPage task={task} setTask={setTask} tasks={tasks} run={run} localOperations={localOperations} startLocalOperation={startLocalOperation} clearLocalOperationsFor={clearLocalOperationsFor} />}
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
  const [username, setUsername] = useState('user')
  const [password, setPassword] = useState('password')
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
        <p>保护性入口。当前演示账号为 user / password。</p>
        <label>用户名<input value={username} onChange={(e) => setUsername(e.target.value)} /></label>
        <label>密码<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
        {error && <div className="form-error">{error}</div>}
        <button className="primary wide" disabled={busy}>{busy ? <Loader2 className="spin" size={16} /> : <Shield size={16} />} 登录</button>
      </form>
    </div>
  )
}

function StudioPage({ task, setTask, tasks, run, localOperations, startLocalOperation, clearLocalOperationsFor }: {
  task: GenerationTask | null
  setTask: (task: GenerationTask | null) => void
  tasks: GenerationTask[]
  run: <T>(label: string, action: () => Promise<T>, after?: (value: T) => void) => Promise<T | undefined>
  localOperations: AIOperation[]
  startLocalOperation: (operation: Omit<AIOperation, 'id' | 'status' | 'startedAt'> & { status?: string; startedAt?: string | null }) => AIOperation
  clearLocalOperationsFor: (ownerTypes: string[]) => void
}) {
  const [step, setStep] = useState<StudioStep>(0)
  const currentStep = getTaskStep(task)
  const visibleStep = Math.min(Math.max(step, 0), currentStep) as StudioStep
  useEffect(() => setStep(currentStep), [task?.id, task?.status])
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
              <AIOperationList operations={visibleTaskOperations(task, localOperations)} compact />
            </>
          ) : <EmptyState title="暂无任务" text="上传主推款后会在这里形成拍摄任务。" />}
          <h3>最近任务</h3>
          <div className="mini-list">
            {tasks.slice(0, 5).map((item) => (
              <button key={item.id} onClick={() => void api.getTask(item.id).then((r) => setTask(r.item))}>
                <span>{item.name}</span><em>{statusText(item.status)}</em>
              </button>
            ))}
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
            <button className="primary" disabled={!files.length || files.length > 8 || !sanitizeClientName(name)} onClick={() => void run('创建拍摄任务中...', () => api.createTask(name, files), (r: { item: GenerationTask }) => setTask(r.item))}>
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
            <button className="image-open" onClick={() => setViewer(assetLightboxImage(image.asset))} title="放大查看">
              <img src={mediaUrl(image.asset.previewUrl ?? image.asset.thumbUrl)} alt={image.asset.filename} />
            </button>
            <span>{image.asset.filename}<small>{assetMeta(image.asset)}</small></span>
            <div className="thumb-actions">
              <button onClick={() => setViewer(assetLightboxImage(image.asset))} title="放大查看"><Eye size={14} /></button>
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
      <p className="edit-hint">确认后可进入智能选搭；点击智能选搭时会冻结主推款图片和默认数字模特快照。</p>
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
  const canSelect = task?.status === 'READY_FOR_STYLING' || (task?.status === 'FAILED' && !task.styling)
  const operations = task ? operationFallbacks(task, localOperations, 'generation_styling') : []
  return (
    <div className="step-panel">
      <div className="step-copy"><h2>AI 智能选搭</h2><p>系统只从搭配服饰库选择真实服饰，暂不支持手动替换或重新搭配。</p></div>
      <AIOperationList operations={operations} />
      {task?.styling ? (
        <div className="selected-strip">
          {task.styling.selectedMaterials.length ? task.styling.selectedMaterials.map((item) => <GarmentCard key={item.id} item={item} />) : <EmptyState title="未选择库内服饰" text="Gemini 判断本次可以不加入库内搭配服饰。" />}
        </div>
      ) : <EmptyState title="等待智能选搭" text="点击按钮后会冻结当前主推款输入版本。" />}
      <button className="primary" disabled={!canSelect} onClick={() => task && void run('智能选搭中...', () => {
        startLocalOperation({ ownerType: 'generation_styling', ownerId: task.id, provider: 'gemini' })
        setTask({ ...task, status: 'STYLING_RUNNING' })
        return api.selectStyling(task.id)
      }, (r: { item: GenerationTask }) => {
        clearLocalOperationsFor(['generation_styling'])
        setTask(r.item)
      })}>
        <WandSparkles size={16} /> AI 智能选搭
      </button>
    </div>
  )
}

function PreviewStep({ task, setTask, run, localOperations, startLocalOperation, clearLocalOperationsFor }: {
  task: GenerationTask | null
  setTask: (task: GenerationTask) => void
  run: any
  localOperations: AIOperation[]
  startLocalOperation: (operation: Omit<AIOperation, 'id' | 'status' | 'startedAt'> & { status?: string; startedAt?: string | null }) => AIOperation
  clearLocalOperationsFor: (ownerTypes: string[]) => void
}) {
  const canPreview = task?.status === 'STYLING_READY' || (task?.status === 'FAILED' && !!task.styling && !task.preview)
  const operations = task ? operationFallbacks(task, localOperations, 'generation_preview') : []
  return (
    <div className="step-panel">
      <div className="step-copy"><h2>六宫格预览</h2><p>只显示一张 3x2 预览图。最终固定生成前 5 格，第 6 格忽略。</p></div>
      <AIOperationList operations={operations} />
      {task?.preview?.asset ? <img className="preview-board" src={mediaUrl(task.preview.asset.previewUrl || task.preview.asset.url)} /> : <EmptyState title="尚未生成六宫格" text="系统会先生成 ShotPlan，再调用 Image-2 输出六宫格。" />}
      <button className="primary" disabled={!canPreview} onClick={() => task && void run('生成六宫格预览中...', () => {
        startLocalOperation({ ownerType: 'generation_preview', ownerId: task.id, provider: 'image-2' })
        setTask({ ...task, status: 'PREVIEW_RUNNING' })
        return api.previewTask(task.id)
      }, (r: { item: GenerationTask }) => {
        clearLocalOperationsFor(['generation_preview'])
        setTask(r.item)
      })}>
        <GalleryVerticalEnd size={16} /> 生成预览
      </button>
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
  const images = task?.resultSet?.images ?? []
  const canGenerate = task?.status === 'PREVIEW_READY'
  const finalOperations = task ? finalImageOperations(task, localOperations) : []
  return (
    <div className="step-panel">
      <div className="step-copy"><h2>生成成片</h2><p>并发生成前 5 格。每张图可单独下载，失败可单张重试。</p></div>
      <AIOperationList operations={finalOperations} />
      <div className="result-grid">
        {images.length ? images.map((image) => (
          <div className="result-card" key={image.id}>
            {image.asset ? <img src={mediaUrl(image.asset.previewUrl || image.asset.url)} /> : <div className="image-placeholder">{statusText(image.status)}{image.activeAttempt && isAttemptRunning(image.activeAttempt) && <small>{formatDuration(elapsedSeconds(image.activeAttempt.startedAt || image.updatedAt))}</small>}</div>}
            <div><span>#{image.sortOrder}</span>{image.asset && <a href={mediaUrl(image.asset.originalUrl)} target="_blank" rel="noreferrer"><Download size={15} /></a>}{image.status === 'FAILED' && <button onClick={() => void run('单张重试中...', () => api.retryResultImage(image.id), (r: { item: GenerationTask }) => setTask(r.item))}><RotateCcw size={14} /></button>}</div>
          </div>
        )) : <EmptyState title="尚未生成最终图" text="点击生成成片后会生成前 5 张。" />}
      </div>
      <button className="primary" disabled={!canGenerate} onClick={() => task && void run('生成最终成片中...', () => {
        for (let index = 1; index <= 5; index += 1) {
          startLocalOperation({ ownerType: 'generation_result_image', ownerId: index, provider: 'image-2' })
        }
        setTask({ ...task, status: 'FINAL_RUNNING' })
        return api.generateFinal(task.id)
      }, (r: { item: GenerationTask }) => {
        clearLocalOperationsFor(['generation_result_image'])
        setTask(r.item)
      })}>
        <Sparkles size={16} /> 生成成片
      </button>
    </div>
  )
}

function LibraryPage({ garments, run }: { garments: StylingGarment[]; run: any }) {
  const [query, setQuery] = useState('')
  const [detail, setDetail] = useState<StylingGarment | null>(null)
  const [items, setItems] = useState<StylingGarment[]>(garments)
  const [showImport, setShowImport] = useState(false)

  async function openDetail(item: StylingGarment) {
    await run('读取搭配服饰详情中...', () => api.getStylingGarment(item.id), (result: { item: StylingGarment }) => setDetail(result.item))
  }

  useEffect(() => {
    const keyword = query.trim()
    if (!keyword) {
      setItems(garments)
      return
    }
    let cancelled = false
    const timer = window.setTimeout(() => {
      void api.listStylingGarments({ limit: 80, keyword }).then((result) => {
        if (!cancelled) setItems(result.items)
      }).catch(() => undefined)
    }, 250)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [garments, query])

  useEffect(() => {
    if (!detail) return
    const latest = garments.find((item) => item.id === detail.id)
    if (latest && (latest.status !== detail.status || latest.code !== detail.code || latest.updatedAt !== detail.updatedAt)) void openDetail(latest)
  }, [garments, detail?.id, detail?.status, detail?.code, detail?.updatedAt])
  return (
    <section className="page">
      <div className="library-title-row">
        <PageTitle title="搭配服饰库" subtitle="真实会出现在最终图里的配角服装。当前不支持鞋、包、饰品入库。" />
        <button className="primary" onClick={() => setShowImport((value) => !value)}>
          <ImagePlus size={16} /> {showImport ? '收起导入' : '导入搭配服饰'}
        </button>
      </div>
      {showImport && <BatchImportPanel run={run} />}
      <div className="panel library-panel">
        <div className="toolbar">
          <div className="search"><Search size={16} /><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索 code / 标签" /></div>
          <span className="muted-count">{items.length} 套</span>
        </div>
        {items.length ? (
          <div className="garment-grid">{items.map((item) => (
            <GarmentCard
              key={item.id}
              item={item}
              run={run}
              onOpen={() => void openDetail(item)}
              onTag={['TAGGING_FAILED', 'TAGGING_READY'].includes(item.status) ? () => void run('重新打标中...', () => api.tagStylingGarment(item.id)) : undefined}
            />
          ))}</div>
        ) : <EmptyState title={query ? '没有匹配的搭配服饰' : '暂无搭配服饰'} text={query ? '换一个 code 或标签关键词试试。' : '点击右上角导入搭配服饰。'} />}
      </div>
      {detail && <GarmentDetailDrawer item={detail} run={run} onClose={() => setDetail(null)} onChange={setDetail} />}
    </section>
  )
}

type ImportBatch = {
  id: string
  code: string
  files: File[]
}

function BatchImportPanel({ run }: { run: any }) {
  const [batches, setBatches] = useState<ImportBatch[]>([])
  const [localError, setLocalError] = useState('')
  const [importOperations, setImportOperations] = useState<AIOperation[]>([])

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
            return item
          }
          const code = `${item.code}-${current.length + 1}`
          existingCodes.add(code)
          return { ...item, id: `${item.id}-${current.length}`, code }
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
    setBatches((items) => items.map((item) => item.id === id ? { ...item, code } : item))
  }

  function updateFiles(id: string, files: File[]) {
    setBatches((items) => items.map((item) => item.id === id ? { ...item, files } : item))
  }

  function removeBatch(id: string) {
    setBatches((items) => items.filter((item) => item.id !== id))
  }

  async function saveDrafts() {
    const invalid = batches.find((batch) => !sanitizeClientName(batch.code))
    if (invalid) {
      setLocalError('每套搭配服饰都需要一个有效 code')
      return
    }
    const oversized = batches.find((batch) => batch.files.length > 6)
    if (oversized) {
      setLocalError(`${oversized.code} 已选择 ${oversized.files.length} 张图片，每款服饰最多只允许导入 6 张`)
      return
    }
    await run(`导入 ${batches.length} 套搭配服饰中...`, async () => {
      const startedAt = new Date().toISOString()
      setImportOperations(batches.map((batch, index) => ({
        id: -(index + 1),
        ownerType: 'styling_garment_tagging',
        ownerId: index + 1,
        provider: 'gemini',
        status: 'SUBMITTED',
        startedAt,
        providerRequestId: batch.code,
      })))
      for (const batch of batches) {
        setImportOperations((items) => items.map((item) => item.providerRequestId === batch.code ? { ...item, status: 'RUNNING', startedAt: item.startedAt || new Date().toISOString() } : item))
        const created = await api.createStylingGarment(batch.code, batch.files)
        await api.tagStylingGarment(created.item.id)
        setImportOperations((items) => items.map((item) => item.providerRequestId === batch.code ? { ...item, status: 'SUBMITTED' } : item))
      }
      return true
    }, () => {
      setBatches([])
      window.setTimeout(() => setImportOperations([]), 1600)
    })
  }

  return (
    <div className="panel import-panel">
      <div className="import-head">
        <div>
          <h3>导入搭配服饰</h3>
          <p>散图会合并为一套；单文件夹是一套；拖入多个一级文件夹会拆成多套。导入后系统会自动打标，单套最多 6 张，推荐 1-3 张。</p>
        </div>
        <button className="primary" disabled={!batches.length} onClick={() => void saveDrafts()}>导入</button>
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
      <AIOperationList operations={importOperations.map((operation) => ({ ...operation, ownerType: 'styling_garment_tagging', provider: operation.provider || 'gemini' }))} />
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
    <div className={`batch-card ${batch.files.length > 6 ? 'has-error' : ''}`}>
      <div className="batch-card-head">
        <div className="batch-icon"><Layers3 size={18} /></div>
        <input value={batch.code} onChange={(event) => onCode(event.target.value)} aria-label="搭配服饰 code" />
        <button className="secondary icon-button" onClick={onRemove} title="移除整套"><Trash2 size={16} /></button>
      </div>
      <div className="batch-note">
        <span>{batch.files.length} 张图片</span>
        {batch.files.length > 6 && <strong>超过 6 张，需删除后才能导入</strong>}
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
  return (
    <section className="page">
      <PageTitle title="生成记录" subtitle="只展示进入过 AI 阶段的任务，技术详情默认折叠。" />
      <div className="records-layout">
        <div className="panel record-list">
          {records.filter((item) => item.status !== 'DRAFT').map((item) => (
            <button key={item.id} className="record-row" onClick={() => void run('读取记录中...', () => api.getRecord(item.id, true), (r: { item: GenerationTask }) => setDetail(r.item))}>
              <AssetThumb asset={item.summary?.cover ?? null} />
              <div><strong>{item.name}</strong><span>{statusText(item.status)} · {dateText(item.updatedAt)}</span></div>
              <div className="strip">{(item.summary?.finalImages ?? []).slice(0, 5).map((asset) => <AssetThumb key={asset.id} asset={asset} />)}</div>
              <Eye size={16} />
            </button>
          ))}
        </div>
        <div className="panel record-detail">
          {detail ? (
            <>
              <h3>{detail.name}</h3>
              <Meta label="状态" value={statusText(detail.status)} />
              <Meta label="选中搭配" value={`${detail.styling?.selectedMaterials?.length ?? 0} 件`} />
              {detail.preview?.asset && <img className="detail-preview" src={mediaUrl(detail.preview.asset.previewUrl)} />}
              <FinalImageStrip images={detail.resultSet?.images ?? []} />
              <details><summary>技术详情</summary><pre>{JSON.stringify({ shotPlans: detail.shotPlans, attempts: detail.attempts }, null, 2)}</pre></details>
            </>
          ) : <EmptyState title="选择一条记录" text="查看生成细节和技术快照。" />}
        </div>
      </div>
    </section>
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

function GarmentCard({ item, onTag, onOpen, onArchive, run }: {
  item: StylingGarment
  onTag?: () => void
  onOpen?: () => void
  onArchive?: () => void
  run?: any
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
    <div className={`garment-card ${tagging ? 'is-tagging' : ''}`}>
      <div className="garment-cover">
        <AssetThumb asset={item.images?.[0]?.asset ?? (item.coverUrl ? { id: item.id, objectKey: '', filename: item.code, mimeType: '', thumbUrl: item.coverUrl } : null)} large />
        {tagging && <span className="cover-badge">识别中</span>}
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
        {onTag && <button className="secondary" disabled={tagging} onClick={onTag}>重新打标</button>}
        {onArchive && <button className="secondary" disabled={tagging} onClick={onArchive}><Trash2 size={16} /> 归档</button>}
      </div>
    </div>
  )
}

function GarmentDetailDrawer({ item, run, onClose, onChange }: {
  item: StylingGarment
  run: any
  onClose: () => void
  onChange: (item: StylingGarment | null) => void
}) {
  const [working, setWorking] = useState(item.images ?? [])
  const [pendingFiles, setPendingFiles] = useState<File[]>([])
  const [replacementFiles, setReplacementFiles] = useState<Record<number, File>>({})
  const [removedIds, setRemovedIds] = useState<number[]>([])
  const [dirty, setDirty] = useState(false)
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const [confirmPurge, setConfirmPurge] = useState(false)
  const [confirmCode, setConfirmCode] = useState('')
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
    await run(retag ? '保存并重新打标中...' : '保存搭配服饰中...', async () => {
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
          <p className="edit-hint">此次修改会影响打标。排序第一张会作为封面和主要参考图。</p>
          {tagging && <p className="edit-hint">正在打标，完成后可继续修改或归档。</p>}
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
                    <button className="image-open" onClick={() => setViewer(assetLightboxImage(image.asset))} title="放大查看">
                      <img src={mediaUrl(image.asset.previewUrl ?? image.asset.thumbUrl)} alt={image.asset.filename} />
                    </button>
                  )}
                  <span>{replacement?.name ?? image.asset.filename}<small>{replacement ? fileMeta(replacement, replacementDimensions[image.id]) : assetMeta(image.asset)}</small></span>
                  <div className="thumb-actions">
                    {replacement ? (
                      <FilePreviewAction file={replacement} dimensions={replacementDimensions[image.id]} onOpen={(nextImage) => setViewer(nextImage)} />
                    ) : (
                      <button onClick={() => setViewer(assetLightboxImage(image.asset))} title="放大查看"><Eye size={14} /></button>
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
          <GeminiTagAttemptPanel attempts={item.tagAttempts ?? []} />
        </div>
        <div className="drawer-actions">
          <button className="secondary" disabled={tagging || !dirty || working.length + pendingFiles.length > 6} onClick={() => void save(false)}>仅保存</button>
          <button className="primary" disabled={tagging || !dirty || working.length + pendingFiles.length > 6} onClick={() => void save(true)}>保存并重新打标</button>
        </div>
        <div className="danger-zone">
          <strong>删除</strong>
          <button className="secondary" disabled={tagging} onClick={() => void run('归档搭配服饰中...', () => api.deleteStylingGarment(item.id), () => {
            onChange(null)
            onClose()
          })}>归档</button>
          <label className="danger-check"><input disabled={tagging} type="checkbox" checked={confirmPurge} onChange={(event) => setConfirmPurge(event.target.checked)} /> 同时删除关联记录和文件</label>
          {confirmPurge && <input value={confirmCode} onChange={(event) => setConfirmCode(event.target.value)} placeholder={`输入 ${item.code} 确认`} />}
          {confirmPurge && <button className="danger" disabled={tagging || confirmCode !== item.code} onClick={() => void run('级联删除搭配服饰中...', () => api.deleteStylingGarment(item.id, { purge: true, confirmCode }), () => {
            onChange(null)
            onClose()
          })}>级联删除</button>}
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
      <label>code</label>
      <div>
        <input disabled={disabled} value={value} onChange={(event) => setValue(event.target.value)} />
        <button className="secondary" disabled={disabled || !changed} onClick={() => void run('保存 code 中...', () => api.updateStylingGarment(item.id, { code: value }))}>保存</button>
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
          <details className="json-details">
            <summary>JSON</summary>
            <pre>{JSON.stringify(data, null, 2)}</pre>
          </details>
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

function GeminiTagAttemptPanel({ attempts }: { attempts: NonNullable<StylingGarment['tagAttempts']> }) {
  return (
    <details className="tag-details provider-details">
      <summary>Gemini 调用详情</summary>
      {attempts.length ? attempts.map((attempt, index) => (
        <details className="attempt-card" key={attempt.id} open={index === 0}>
          <summary>
            <span>{attempt.provider}</span>
            <span className={`status ${attempt.status.toLowerCase()}`}>{statusText(attempt.status)}</span>
            <span>{attempt.startedAt ? dateText(attempt.startedAt) : '未记录时间'}</span>
          </summary>
          {attempt.errorMessage && <p className="inline-error">{attempt.errorMessage}</p>}
          <Meta label="完成时间" value={attempt.completedAt ? dateText(attempt.completedAt) : attempt.failedAt ? dateText(attempt.failedAt) : '未完成'} />
          <details className="json-details">
            <summary>请求参数</summary>
            <pre>{JSON.stringify(attempt.requestSnapshot ?? {}, null, 2)}</pre>
          </details>
          <details className="json-details">
            <summary>返回结果</summary>
            <pre>{JSON.stringify(attempt.responseSnapshot ?? {}, null, 2)}</pre>
          </details>
        </details>
      )) : <p className="empty-inline">暂无调用记录</p>}
    </details>
  )
}

function AIOperationList({ operations, compact = false }: { operations: AIOperation[]; compact?: boolean }) {
  const active = operations.filter(Boolean).slice(0, compact ? 4 : 8)
  const now = useTicker(active.some(isAttemptRunning))
  if (!active.length) return null
  return (
    <div className={`ai-operation-list ${compact ? 'compact' : ''}`}>
      {active.map((operation, index) => (
        <AIOperationCard key={`${operation.id}-${operation.ownerType}-${operation.ownerId}-${index}`} operation={operation} now={now} compact={compact} index={index} />
      ))}
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
        <span>{operationProvider(operation.provider)} · {statusText(operation.status)}</span>
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
  if (remote.length) return remote
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
  if (imageAttempts.length) return imageAttempts
  const local = localOperations.filter((operation) => operation.ownerType === 'generation_result_image')
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
  return [...activeLocal, ...activeRemote]
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
  if (operation.ownerType === 'styling_garment_tagging') return operation.providerRequestId ? `服饰标签识别 · ${operation.providerRequestId}` : '服饰标签识别'
  if (operation.ownerType === 'generation_styling') return '智能选搭'
  if (operation.ownerType === 'generation_preview') return '六宫格预览'
  if (operation.ownerType === 'generation_result_image') return `最终图 ${operation.ownerId > 0 && operation.id < 0 ? operation.ownerId : index + 1}`
  return 'AI 调用'
}

function operationProvider(provider: string) {
  if (provider === 'gemini') return 'Gemini'
  if (provider === 'image-2') return 'Image-2'
  if (provider === 'fal') return 'Image-2 / fal'
  if (provider === 'toapis') return 'Image-2 / toapis'
  return provider || 'AI'
}

function elapsedSeconds(start?: string | null, now = Date.now(), end?: string | null) {
  if (!start) return 0
  const startMs = Date.parse(start)
  if (!Number.isFinite(startMs)) return 0
  const endMs = end ? Date.parse(end) : now
  return Math.max(0, Math.floor((endMs - startMs) / 1000))
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

function AssetThumb({ asset, large = false }: { asset?: MediaAsset | null; large?: boolean }) {
  const [viewer, setViewer] = useState<LightboxImage | null>(null)
  const hasImage = asset?.thumbUrl || asset?.previewUrl || asset?.url
  return (
    <>
      <div className={`asset-thumb ${large ? 'large' : ''}`}>
        {asset && hasImage ? (
          <button className="asset-open" onClick={() => setViewer(assetLightboxImage(asset))} title="放大查看">
            <img src={mediaUrl(asset.thumbUrl || asset.previewUrl || asset.url)} alt={asset.filename} />
          </button>
        ) : <FileImage size={18} />}
      </div>
      {viewer && <ImageLightbox image={viewer} onClose={() => setViewer(null)} />}
    </>
  )
}

function FinalImageStrip({ images }: { images: { id: number; sortOrder: number; status: string; asset?: MediaAsset | null }[] }) {
  return <div className="final-strip">{images.map((image) => <AssetThumb key={image.id} asset={image.asset} />)}</div>
}

function ImageLightbox({ image, onClose }: { image: LightboxImage; onClose: () => void }) {
  const [zoom, setZoom] = useState(1)
  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
      if (event.key === '+' || event.key === '=') setZoom((value) => Math.min(3, value + 0.25))
      if (event.key === '-') setZoom((value) => Math.max(0.5, value - 0.25))
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [onClose])

  return (
    <div className="lightbox" role="dialog" aria-modal="true" onClick={onClose}>
      <div className="lightbox-panel" onClick={(event) => event.stopPropagation()}>
        <div className="lightbox-head">
          <div>
            <strong>{image.title}</strong>
            {image.meta && <span>{image.meta}</span>}
          </div>
          <div className="lightbox-actions">
            <button onClick={() => setZoom((value) => Math.max(0.5, value - 0.25))} title="缩小"><ZoomOut size={16} /></button>
            <span>{Math.round(zoom * 100)}%</span>
            <button onClick={() => setZoom((value) => Math.min(3, value + 0.25))} title="放大"><ZoomIn size={16} /></button>
            {image.downloadUrl && <a href={image.downloadUrl} target="_blank" rel="noreferrer" title="打开原图"><Download size={16} /></a>}
            <button onClick={onClose} title="关闭"><X size={16} /></button>
          </div>
        </div>
        <div className="lightbox-stage">
          <img src={image.src} alt={image.title} style={{ transform: `scale(${zoom})` }} />
        </div>
      </div>
    </div>
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
  if (task.status === 'FAILED') return task.styling ? 2 : 1
  if (['READY_FOR_STYLING', 'STYLING_RUNNING'].includes(task.status)) return 1
  if (['STYLING_READY', 'PREVIEW_RUNNING'].includes(task.status)) return 2
  if (['PREVIEW_READY', 'FINAL_RUNNING', 'FINAL_PARTIAL', 'COMPLETED'].includes(task.status)) return 3
  return 0
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
    READY: '可用',
    TAGGING_READY: '待复核',
    TAGGING_RUNNING: '正在打标',
    TAGGING_FAILED: '打标失败',
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

function dimensionText(dimensions?: { width?: number | null; height?: number | null }) {
  return dimensions?.width && dimensions?.height ? `${dimensions.width} x ${dimensions.height}` : '尺寸读取中'
}

function fileMeta(file: File, dimensions?: { width: number; height: number }) {
  return `${dimensionText(dimensions)} · ${formatBytes(file.size)}`
}

function assetMeta(asset: MediaAsset) {
  return `${dimensionText(asset)} · ${formatBytes(asset.sizeBytes)}`
}

function fileLightboxImage(file: File, src: string, dimensions?: { width: number; height: number }): LightboxImage {
  return {
    src,
    title: file.name,
    meta: fileMeta(file, dimensions),
  }
}

function assetLightboxImage(asset: MediaAsset): LightboxImage {
  const src = mediaUrl(asset.originalUrl ?? asset.previewUrl ?? asset.thumbUrl ?? asset.url)
  return {
    src,
    title: asset.filename,
    meta: assetMeta(asset),
    downloadUrl: src,
  }
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
  return new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}
