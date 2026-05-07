import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import {
  Check,
  Clock,
  Download,
  FileImage,
  History,
  ImagePlus,
  Loader2,
  RefreshCw,
  Sparkles,
  Upload,
} from 'lucide-react'
import { api, mediaUrl } from './api'
import type { EventLog, GarmentSku, GenerationImage, GenerationRun, ModelProfile, ShotPlan } from './types'

type BusyState = 'idle' | 'uploading' | 'planning' | 'generating' | 'regenerating'

export function App() {
  const [skus, setSkus] = useState<GarmentSku[]>([])
  const [models, setModels] = useState<ModelProfile[]>([])
  const [selectedSku, setSelectedSku] = useState<GarmentSku | null>(null)
  const [selectedModelId, setSelectedModelId] = useState<number | undefined>()
  const [shotPlan, setShotPlan] = useState<ShotPlan | null>(null)
  const [generationRun, setGenerationRun] = useState<GenerationRun | null>(null)
  const [logs, setLogs] = useState<EventLog[]>([])
  const [busy, setBusy] = useState<BusyState>('idle')
  const [error, setError] = useState<string | null>(null)
  const [skuInput, setSkuInput] = useState('')
  const [files, setFiles] = useState<File[]>([])

  const defaultModel = useMemo(() => models.find((model) => model.isDefault) ?? models[0], [models])
  const selectedModel = models.find((model) => model.id === selectedModelId) ?? defaultModel

  useEffect(() => {
    void refreshBaseData()
  }, [])

  async function refreshBaseData() {
    setError(null)
    const [skuResult, modelResult, logResult] = await Promise.all([api.listSkus(), api.listModels(), api.logs()])
    setSkus(skuResult.items)
    setModels(modelResult.items)
    setLogs(logResult.items)
    if (!selectedModelId && modelResult.items[0]) setSelectedModelId(modelResult.items[0].id)
  }

  async function chooseSku(sku: GarmentSku) {
    setError(null)
    const detail = await api.getSku(sku.id)
    setSelectedSku(detail.item)
    setShotPlan(null)
    setGenerationRun(null)
  }

  async function uploadSku() {
    if (!skuInput.trim() || files.length === 0) {
      setError('请填写货号并上传至少一张服装图片')
      return
    }
    setBusy('uploading')
    setError(null)
    try {
      const result = await api.uploadSku(skuInput.trim(), files)
      setSelectedSku(result.item)
      setShotPlan(null)
      setGenerationRun(null)
      setSkuInput('')
      setFiles([])
      await refreshBaseData()
    } catch (err) {
      setError(err instanceof Error ? err.message : '上传失败')
    } finally {
      setBusy('idle')
    }
  }

  async function createPlan() {
    if (!selectedSku) return
    setBusy('planning')
    setError(null)
    try {
      const result = await api.createPlan(selectedSku.id, selectedModel?.id)
      setShotPlan(result.item)
      setSelectedSku((await api.getSku(selectedSku.id)).item)
      setGenerationRun(null)
      setLogs((await api.logs()).items)
    } catch (err) {
      setError(err instanceof Error ? err.message : '生成方案失败')
    } finally {
      setBusy('idle')
    }
  }

  async function generateGroup() {
    if (!shotPlan) return
    setBusy('generating')
    setError(null)
    try {
      const result = await api.generateGroup(shotPlan.id)
      setGenerationRun(result.item)
      setLogs((await api.logs()).items)
    } catch (err) {
      setError(err instanceof Error ? err.message : '生成失败')
    } finally {
      setBusy('idle')
    }
  }

  async function regenerate(image: GenerationImage, reason: string) {
    setBusy('regenerating')
    setError(null)
    try {
      const result = await api.regenerate(image.id, reason)
      setGenerationRun((current) => mergeRun(current, result.item))
      setLogs((await api.logs()).items)
    } catch (err) {
      setError(err instanceof Error ? err.message : '重生失败')
    } finally {
      setBusy('idle')
    }
  }

  async function toggleFinal(image: GenerationImage) {
    const result = await api.setFinal(image.id, !image.isFinal)
    setGenerationRun((current) => {
      if (!current?.images) return current
      return {
        ...current,
        images: current.images.map((item) => (item.id === image.id ? result.item : item)),
      }
    })
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <div className="brand">Lily AI 模特图</div>
          <h1>生成组图</h1>
          <p>选择服装和模特，确认组图后即可生成电商主图</p>
        </div>
        <nav>
          <a className="active">生成组图</a>
          <a>服装素材</a>
          <a>数字模特</a>
          <a>生成记录</a>
        </nav>
      </header>

      {error && <div className="alert">{error}</div>}

      <main className="workspace">
        <section className="left-rail">
          <Panel title="上传服装" icon={<Upload size={18} />}>
            <label className="field">
              <span>货号</span>
              <input value={skuInput} onChange={(event) => setSkuInput(event.target.value)} placeholder="例如 125309C7948510" />
            </label>
            <label className="dropzone">
              <ImagePlus size={24} />
              <span>{files.length ? `已选择 ${files.length} 张图片` : '选择服装图片'}</span>
              <input
                type="file"
                accept="image/*"
                multiple
                onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
              />
            </label>
            <button className="primary full" onClick={uploadSku} disabled={busy !== 'idle'}>
              {busy === 'uploading' ? <Loader2 className="spin" size={17} /> : <Upload size={17} />}
              上传并使用
            </button>
          </Panel>

          <Panel title="服装素材" icon={<FileImage size={18} />}>
            <div className="sku-list">
              {skus.length === 0 && <EmptyText text="还没有上传货号" />}
              {skus.map((sku) => (
                <button
                  key={sku.id}
                  className={`sku-item ${selectedSku?.id === sku.id ? 'selected' : ''}`}
                  onClick={() => void chooseSku(sku)}
                >
                  {sku.coverUrl ? <img src={mediaUrl(sku.coverUrl)} alt={sku.sku} /> : <div className="thumb-fallback" />}
                  <span>
                    <strong>{sku.sku}</strong>
                    <small>{sku.imageCount ?? 0} 张图片 · {statusText(sku.status)}</small>
                  </span>
                </button>
              ))}
            </div>
          </Panel>
        </section>

        <section className="main-flow">
          <div className="step-grid">
            <StepCard number="1" title="选择服装">
              {selectedSku ? (
                <div className="selected-garment">
                  <div className="image-strip">
                    {(selectedSku.images ?? []).slice(0, 5).map((image) => (
                      <img key={image.id} src={mediaUrl(image.url)} alt={image.filename} />
                    ))}
                  </div>
                  <h3>{selectedSku.sku}</h3>
                  <p>{selectedSku.analysis?.summary ?? '等待生成组图方案后显示服装摘要'}</p>
                  <Tag>{selectedSku.analysis?.category ?? statusText(selectedSku.status)}</Tag>
                </div>
              ) : (
                <EmptyText text="选择或上传一个货号开始" />
              )}
            </StepCard>

            <StepCard number="2" title="选择模特">
              {selectedModel ? (
                <div className="model-summary">
                  <div className="model-avatar">F01</div>
                  <h3>{selectedModel.name}</h3>
                  <p>默认模特 / 短发 / 适合棚拍</p>
                  <select value={selectedModel.id} onChange={(event) => setSelectedModelId(Number(event.target.value))}>
                    {models.map((model) => (
                      <option key={model.id} value={model.id}>{model.name}</option>
                    ))}
                  </select>
                </div>
              ) : (
                <EmptyText text="暂无数字模特" />
              )}
            </StepCard>

            <StepCard number="3" title="确认组图方案" wide>
              {shotPlan ? (
                <ShotPlanBoard shotPlan={shotPlan} />
              ) : (
                <div className="plan-empty">
                  <p>Lily 默认棚拍风格</p>
                  <span>白/浅灰背景 · 自然姿态 · 简洁搭配</span>
                  <button className="secondary" onClick={createPlan} disabled={!selectedSku || busy !== 'idle'}>
                    {busy === 'planning' ? <Loader2 className="spin" size={17} /> : <Sparkles size={17} />}
                    预览方案
                  </button>
                </div>
              )}
            </StepCard>
          </div>

          <div className="action-row">
            <button className="primary" onClick={shotPlan ? generateGroup : createPlan} disabled={!selectedSku || busy !== 'idle'}>
              {busy === 'generating' || busy === 'planning' ? <Loader2 className="spin" size={18} /> : <Sparkles size={18} />}
              {shotPlan ? '生成组图' : '预览方案'}
            </button>
            <span>预计生成 5 张，可单张重生；生成与重生记录会保留。</span>
          </div>

          <ResultGallery
            run={generationRun}
            loading={busy === 'generating' || busy === 'regenerating'}
            onRegenerate={(image, reason) => void regenerate(image, reason)}
            onToggleFinal={(image) => void toggleFinal(image)}
            onExport={(runId) => void api.exportRun(runId)}
          />
        </section>

        <aside className="right-rail">
          <Panel title="生成记录" icon={<History size={18} />}>
            <div className="log-list">
              {logs.map((log) => (
                <div className="log-item" key={log.id}>
                  <Clock size={14} />
                  <span>
                    <strong>{log.eventType}</strong>
                    <small>{log.entityType} #{log.entityId ?? '-'} · {log.createdAt}</small>
                  </span>
                </div>
              ))}
            </div>
          </Panel>
        </aside>
      </main>
    </div>
  )
}

function Panel({ title, icon, children }: { title: string; icon: ReactNode; children: ReactNode }) {
  return (
    <section className="panel">
      <div className="panel-title">{icon}<span>{title}</span></div>
      {children}
    </section>
  )
}

function StepCard({ number, title, wide, children }: { number: string; title: string; wide?: boolean; children: ReactNode }) {
  return (
    <section className={`step-card ${wide ? 'wide' : ''}`}>
      <div className="step-title"><b>{number}</b><span>{title}</span></div>
      {children}
    </section>
  )
}

function ShotPlanBoard({ shotPlan }: { shotPlan: ShotPlan }) {
  return (
    <div className="shot-plan">
      <div className="style-summary">
        <strong>{shotPlan.plan.brandStyle?.name ?? 'Lily 默认棚拍风格'}</strong>
        <span>{shotPlan.plan.brandStyle?.summary}</span>
      </div>
      <div className="shot-list">
        {shotPlan.plan.shots.map((shot) => (
          <div className="shot-row" key={shot.shotIndex}>
            <b>{String(shot.shotIndex).padStart(2, '0')}</b>
            <span>{shot.title}</span>
            <small>{shot.frame} · {shot.angle}</small>
          </div>
        ))}
      </div>
      <details>
        <summary>高级设置</summary>
        <div className="advanced-grid">
          {shotPlan.plan.shots.map((shot) => (
            <div key={shot.shotIndex}>
              <strong>{shot.title}</strong>
              <p>{shot.pose}</p>
              <small>{shot.styling?.shoes}</small>
            </div>
          ))}
        </div>
      </details>
    </div>
  )
}

function ResultGallery({
  run,
  loading,
  onRegenerate,
  onToggleFinal,
  onExport,
}: {
  run: GenerationRun | null
  loading: boolean
  onRegenerate: (image: GenerationImage, reason: string) => void
  onToggleFinal: (image: GenerationImage) => void
  onExport: (runId: number) => void
}) {
  const placeholders = Array.from({ length: 5 }, (_, index) => index + 1)
  return (
    <section className="results">
      <div className="section-heading">
        <h2>生成结果</h2>
        {run && <span>{run.kind === 'regeneration' ? '重生记录' : '组图记录'} #{run.id} · {statusText(run.status)}</span>}
      </div>
      <div className="result-grid">
        {run?.images?.length
          ? run.images.map((image) => (
              <article className="result-card" key={image.id}>
                <div className="result-image">
                  {image.url ? <img src={mediaUrl(image.url)} alt={image.title} /> : <span>{statusText(image.status)}</span>}
                  {image.isFinal && <div className="final-badge"><Check size={14} />最终图</div>}
                </div>
                <div className="result-meta">
                  <strong>{String(image.shotIndex).padStart(2, '0')} {image.title}</strong>
                  <small>{statusText(image.status)} · 记录 #{image.id}</small>
                </div>
                <div className="result-actions">
                  <button onClick={() => onRegenerate(image, '姿势不合适')} disabled={loading}>
                    <RefreshCw size={15} />重生
                  </button>
                  <button onClick={() => onToggleFinal(image)}>
                    <Check size={15} />{image.isFinal ? '取消最终' : '设为最终'}
                  </button>
                </div>
              </article>
            ))
          : placeholders.map((index) => (
              <article className="result-card empty" key={index}>
                <div className="result-image"><span>待生成</span></div>
                <div className="result-meta">
                  <strong>{String(index).padStart(2, '0')} 图位</strong>
                  <small>生成后可单张重生</small>
                </div>
              </article>
            ))}
      </div>
      {run?.images?.some((image) => image.isFinal) && (
        <div className="export-note">
          <Download size={16} />
          最终图已标记；导出时优先打包最终图。
          <button className="link-button" onClick={() => onExport(run.id)}>导出 ZIP</button>
        </div>
      )}
    </section>
  )
}

function EmptyText({ text }: { text: string }) {
  return <div className="empty-text">{text}</div>
}

function Tag({ children }: { children: ReactNode }) {
  return <span className="tag">{children}</span>
}

function statusText(status: string) {
  const map: Record<string, string> = {
    NEW: '待策划',
    ANALYZED: '已策划',
    CONFIRMED: '已确认',
    DRAFT: '待确认',
    RUNNING: '生成中',
    SUCCEEDED: '已生成',
    FAILED: '失败',
    CREATED: '已创建',
  }
  return map[status] ?? status
}

function mergeRun(current: GenerationRun | null, next: GenerationRun): GenerationRun {
  if (!current?.images) return next
  return {
    ...current,
    images: [...current.images, ...(next.images ?? [])],
    updatedAt: next.updatedAt,
  }
}
