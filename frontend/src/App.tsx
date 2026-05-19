import { useEffect, useMemo, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import {
  Bot,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Download,
  Eye,
  FileImage,
  GalleryVerticalEnd,
  History,
  ImagePlus,
  Library,
  Loader2,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
  RotateCcw,
  Search,
  Shield,
  Sparkles,
  Upload,
  User,
  UserRound,
  WandSparkles,
} from 'lucide-react'
import { api, mediaUrl } from './api'
import type { GenerationTask, MediaAsset, ModelProfile, StylingGarment } from './api'

type Page = 'studio' | 'library' | 'model' | 'records'
type StudioStep = 0 | 1 | 2 | 3

const steps = [
  { title: '上传主推款', icon: Upload },
  { title: 'AI 智能选搭', icon: WandSparkles },
  { title: '六宫格预览', icon: GalleryVerticalEnd },
  { title: '生成成片', icon: Sparkles },
] as const

export function App() {
  const [checked, setChecked] = useState(false)
  const [authenticated, setAuthenticated] = useState(false)
  const [page, setPage] = useState<Page>('studio')
  const [collapsed, setCollapsed] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [task, setTask] = useState<GenerationTask | null>(null)
  const [tasks, setTasks] = useState<GenerationTask[]>([])
  const [garments, setGarments] = useState<StylingGarment[]>([])
  const [models, setModels] = useState<ModelProfile[]>([])
  const [recordDetail, setRecordDetail] = useState<GenerationTask | null>(null)

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

  async function refreshAll() {
    const [garmentResult, taskResult, modelResult] = await Promise.all([
      api.listStylingGarments({ limit: 80 }),
      api.listTasks({ limit: 30 }),
      api.listModels(),
    ])
    setGarments(garmentResult.items)
    setTasks(taskResult.items)
    setModels(modelResult.items)
    if (!task && taskResult.items[0]) {
      const detail = await api.getTask(taskResult.items[0].id).then((r) => r.item).catch(() => null)
      if (detail) setTask(detail)
    }
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
              <button key={item.id} className={page === item.id ? 'active' : ''} onClick={() => setPage(item.id)} title={item.label}>
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
        {page === 'studio' && <StudioPage task={task} setTask={setTask} tasks={tasks} run={run} />}
        {page === 'library' && <LibraryPage garments={garments} run={run} />}
        {page === 'model' && <ModelPage models={models} />}
        {page === 'records' && <RecordsPage records={tasks} detail={recordDetail} setDetail={setRecordDetail} run={run} />}
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

function StudioPage({ task, setTask, tasks, run }: {
  task: GenerationTask | null
  setTask: (task: GenerationTask | null) => void
  tasks: GenerationTask[]
  run: <T>(label: string, action: () => Promise<T>, after?: (value: T) => void) => Promise<T | undefined>
}) {
  const [step, setStep] = useState<StudioStep>(0)
  const currentStep = getTaskStep(task)
  const visibleStep = Math.min(Math.max(step, 0), currentStep) as StudioStep
  useEffect(() => setStep(currentStep), [task?.id, task?.status])

  return (
    <section className="page">
      <PageTitle title="AI 摄影棚" subtitle="上传主推款，让系统选搭配角服饰并生成电商棚拍成片。" />
      <div className="studio-grid">
        <div className="panel studio-main">
          <StepRail active={visibleStep} max={currentStep} onSelect={setStep} />
          {visibleStep === 0 && <UploadHeroStep task={task} setTask={setTask} run={run} />}
          {visibleStep === 1 && <StylingStep task={task} setTask={setTask} run={run} />}
          {visibleStep === 2 && <PreviewStep task={task} setTask={setTask} run={run} />}
          {visibleStep === 3 && <FinalStep task={task} setTask={setTask} run={run} />}
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
  const [name, setName] = useState(() => timestampName())
  return (
    <div className="step-panel">
      <div className="step-copy"><h2>上传主推款</h2><p>推荐 3-5 张，最多 8 张。按正面、背面/侧面、细节排序。</p></div>
      <UploadBox files={files} setFiles={setFiles} max={8} />
      <div className="inline-form">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="任务名称" />
        <button className="primary" disabled={!files.length} onClick={() => void run('创建拍摄任务中...', () => api.createTask(name, files), (r: { item: GenerationTask }) => setTask(r.item))}>
          <ImagePlus size={16} /> 创建任务
        </button>
      </div>
      {task && <CurrentTaskCard task={task} />}
    </div>
  )
}

function StylingStep({ task, setTask, run }: { task: GenerationTask | null; setTask: (task: GenerationTask) => void; run: any }) {
  return (
    <div className="step-panel">
      <div className="step-copy"><h2>AI 智能选搭</h2><p>系统只从搭配服饰库选择真实服饰，暂不支持手动替换或重新搭配。</p></div>
      {task?.styling ? (
        <div className="selected-strip">
          {task.styling.selectedMaterials.length ? task.styling.selectedMaterials.map((item) => <GarmentCard key={item.id} item={item} />) : <EmptyState title="未选择库内服饰" text="Gemini 判断本次可以不加入库内搭配服饰。" />}
        </div>
      ) : <EmptyState title="等待智能选搭" text="点击按钮后会冻结当前主推款输入版本。" />}
      <button className="primary" disabled={!task} onClick={() => task && void run('智能选搭中...', () => api.selectStyling(task.id), (r: { item: GenerationTask }) => setTask(r.item))}>
        <WandSparkles size={16} /> AI 智能选搭
      </button>
    </div>
  )
}

function PreviewStep({ task, setTask, run }: { task: GenerationTask | null; setTask: (task: GenerationTask) => void; run: any }) {
  return (
    <div className="step-panel">
      <div className="step-copy"><h2>六宫格预览</h2><p>只显示一张 3x2 预览图。最终固定生成前 5 格，第 6 格忽略。</p></div>
      {task?.preview?.asset ? <img className="preview-board" src={mediaUrl(task.preview.asset.previewUrl || task.preview.asset.url)} /> : <EmptyState title="尚未生成六宫格" text="系统会先生成 ShotPlan，再调用 Image-2 输出六宫格。" />}
      <button className="primary" disabled={!task?.styling} onClick={() => task && void run('生成六宫格预览中...', () => api.previewTask(task.id), (r: { item: GenerationTask }) => setTask(r.item))}>
        <GalleryVerticalEnd size={16} /> 生成预览
      </button>
    </div>
  )
}

function FinalStep({ task, setTask, run }: { task: GenerationTask | null; setTask: (task: GenerationTask) => void; run: any }) {
  const images = task?.resultSet?.images ?? []
  return (
    <div className="step-panel">
      <div className="step-copy"><h2>生成成片</h2><p>并发生成前 5 格。每张图可单独下载，失败可单张重试。</p></div>
      <div className="result-grid">
        {images.length ? images.map((image) => (
          <div className="result-card" key={image.id}>
            {image.asset ? <img src={mediaUrl(image.asset.previewUrl || image.asset.url)} /> : <div className="image-placeholder">{statusText(image.status)}</div>}
            <div><span>#{image.sortOrder}</span>{image.asset && <a href={mediaUrl(image.asset.originalUrl)} target="_blank" rel="noreferrer"><Download size={15} /></a>}{image.status === 'FAILED' && <button onClick={() => void run('单张重试中...', () => api.retryResultImage(image.id), (r: { item: GenerationTask }) => setTask(r.item))}><RotateCcw size={14} /></button>}</div>
          </div>
        )) : <EmptyState title="尚未生成最终图" text="点击生成成片后会生成前 5 张。" />}
      </div>
      <button className="primary" disabled={!task?.preview} onClick={() => task && void run('生成最终成片中...', () => api.generateFinal(task.id), (r: { item: GenerationTask }) => setTask(r.item))}>
        <Sparkles size={16} /> 生成成片
      </button>
    </div>
  )
}

function LibraryPage({ garments, run }: { garments: StylingGarment[]; run: any }) {
  const [files, setFiles] = useState<File[]>([])
  const [code, setCode] = useState(timestampName())
  const [query, setQuery] = useState('')
  const visible = garments.filter((item) => !query || item.code.toLowerCase().includes(query.toLowerCase()))
  return (
    <section className="page">
      <PageTitle title="搭配服饰库" subtitle="真实会出现在最终图里的配角服装。当前不支持鞋、包、饰品入库。" />
      <div className="library-layout">
        <div className="panel">
          <div className="toolbar"><div className="search"><Search size={16} /><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索 code / 标签" /></div></div>
          <div className="garment-grid">{visible.map((item) => <GarmentCard key={item.id} item={item} onTag={() => void run('AI 打标中...', () => api.tagStylingGarment(item.id))} />)}</div>
        </div>
        <div className="panel side-editor">
          <h3>导入搭配服饰</h3>
          <UploadBox files={files} setFiles={setFiles} max={6} compact />
          <input value={code} onChange={(e) => setCode(e.target.value)} />
          <button className="primary wide" disabled={!files.length} onClick={() => void run('保存搭配服饰中...', () => api.createStylingGarment(code, files), () => { setFiles([]); setCode(timestampName()) })}>保存</button>
        </div>
      </div>
    </section>
  )
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

function GarmentCard({ item, onTag }: { item: StylingGarment; onTag?: () => void }) {
  const tags = item.aiTags ?? {}
  return (
    <div className="garment-card">
      <AssetThumb asset={item.images?.[0]?.asset ?? (item.coverUrl ? { id: item.id, objectKey: '', filename: item.code, mimeType: '', thumbUrl: item.coverUrl } : null)} large />
      <div><strong>{item.code}</strong><span className={`status ${item.status.toLowerCase()}`}>{garmentStatus(item.status)}</span></div>
      <p>{String(tags.brief ?? '等待标签')}</p>
      {onTag && <button className="secondary" onClick={onTag}>{item.status === 'READY' ? '重新打标' : '保存并打标'}</button>}
    </div>
  )
}

function CurrentTaskCard({ task }: { task: GenerationTask }) {
  return <div className="current-task"><ThumbGrid assets={(task.images ?? []).map((item) => item.asset)} /><Meta label="当前任务" value={task.name} /><Meta label="状态" value={statusText(task.status)} /></div>
}

function ThumbGrid({ assets, compact = false }: { assets: MediaAsset[]; compact?: boolean }) {
  return <div className={`thumb-grid ${compact ? 'compact' : ''}`}>{assets.slice(0, compact ? 4 : 8).map((asset) => <AssetThumb key={asset.id} asset={asset} />)}</div>
}

function AssetThumb({ asset, large = false }: { asset?: MediaAsset | null; large?: boolean }) {
  return <div className={`asset-thumb ${large ? 'large' : ''}`}>{asset?.thumbUrl || asset?.previewUrl || asset?.url ? <img src={mediaUrl(asset.thumbUrl || asset.previewUrl || asset.url)} /> : <FileImage size={18} />}</div>
}

function FinalImageStrip({ images }: { images: { id: number; sortOrder: number; status: string; asset?: MediaAsset | null }[] }) {
  return <div className="final-strip">{images.map((image) => <AssetThumb key={image.id} asset={image.asset} />)}</div>
}

function Meta({ label, value }: { label: string; value: ReactNode }) {
  return <div className="meta"><span>{label}</span><strong>{value}</strong></div>
}

function EmptyState({ title, text }: { title: string; text: string }) {
  return <div className="empty"><Clock3 size={22} /><strong>{title}</strong><span>{text}</span></div>
}

function getTaskStep(task: GenerationTask | null): StudioStep {
  if (!task) return 0
  if (['READY_FOR_STYLING', 'STYLING_RUNNING', 'FAILED'].includes(task.status)) return 1
  if (['STYLING_READY', 'PREVIEW_RUNNING'].includes(task.status)) return 2
  if (['PREVIEW_READY', 'FINAL_RUNNING', 'FINAL_PARTIAL', 'COMPLETED'].includes(task.status)) return 3
  return 0
}

function statusText(status: string) {
  const map: Record<string, string> = {
    DRAFT: '草稿',
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
    TAGGING_RUNNING: '正在打标',
    TAGGING_FAILED: '打标失败',
  }
  return map[status] ?? status
}

function garmentStatus(status: string) {
  return statusText(status)
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
