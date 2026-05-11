import { useEffect, useMemo, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { createPortal } from 'react-dom'
import {
  Archive,
  Check,
  Clock,
  Edit3,
  Plus,
  Save,
  FolderOpen,
  Trash2,
  Download,
  FileImage,
  Images,
  History,
  ImagePlus,
  Loader2,
  RefreshCw,
  Search,
  Sparkles,
  Upload,
  X,
} from 'lucide-react'
import { api, mediaUrl } from './api'
import { parseRoute, routeToUrl } from './routes'
import type { AppRoute, Page } from './routes'
import type { GarmentSku, GenerationImage, GenerationRun, ModelProfile, Shot, ShotPlan } from './types'

const SHOW_DEBUG_INFO = import.meta.env.VITE_SHOW_DEBUG_INFO === 'true'

type BusyState = 'idle' | 'uploading' | 'planning' | 'saving-plan' | 'previewing' | 'generating' | 'regenerating'
type ImportStatus = 'draft' | 'uploading' | 'uploaded' | 'failed'
type ImportImage = {
  id: string
  file: File
  previewUrl: string
  relativePath: string
}
type ImportPackage = {
  id: string
  sourceName: string
  sku: string
  title: string
  images: ImportImage[]
  status: ImportStatus
  error?: string
  createdSkuId?: number
}
type ModelForm = {
  modelId: string
  name: string
  isDefault: boolean
  height: string
  weight: string
  bodyShape: string
  bust: string
  waist: string
  hips: string
  notes: string
}
type ImageDimensions = {
  width: number
  height: number
}
type ModelHeadDraft = {
  id: string
  file: File
  previewUrl: string
  angle: string
}
type ModelHeadEntry = {
  id: string
  angle: string
  filename: string
  url: string
  fileSize?: number | null
}
type AuthState = {
  checked: boolean
  authRequired: boolean
  authenticated: boolean
}
type DeleteSkuDialogState = {
  sku: GarmentSku
  confirmSku: string
  deleteRelated: boolean
}

export function App() {
  const [auth, setAuth] = useState<AuthState>({ checked: false, authRequired: false, authenticated: false })
  const appReady = auth.checked && auth.authenticated
  const [skus, setSkus] = useState<GarmentSku[]>([])
  const [models, setModels] = useState<ModelProfile[]>([])
  const [selectedSku, setSelectedSku] = useState<GarmentSku | null>(null)
  const [selectedModelId, setSelectedModelId] = useState<number | undefined>()
  const [shotPlan, setShotPlan] = useState<ShotPlan | null>(null)
  const [previewRun, setPreviewRun] = useState<GenerationRun | null>(null)
  const [generationRun, setGenerationRun] = useState<GenerationRun | null>(null)
  const [historyRuns, setHistoryRuns] = useState<GenerationRun[]>([])
  const [selectedHistoryRun, setSelectedHistoryRun] = useState<GenerationRun | null>(null)
  const [historyFilter, setHistoryFilter] = useState('')
  const [generateSkuFilter, setGenerateSkuFilter] = useState('')
  const [busy, setBusy] = useState<BusyState>('idle')
  const [error, setError] = useState<string | null>(null)
  const [page, setPage] = useState<Page>(() => parseRoute().page)
  const [route, setRoute] = useState<AppRoute>(() => parseRoute())
  const [hydratedRouteKey, setHydratedRouteKey] = useState('')
  const [editSkuForm, setEditSkuForm] = useState({ sku: '', title: '', status: 'NEW' })
  const [extraFiles, setExtraFiles] = useState<File[]>([])
  const [importPackages, setImportPackages] = useState<ImportPackage[]>([])
  const [looseSkuInput, setLooseSkuInput] = useState('')
  const [editingModelId, setEditingModelId] = useState<number | undefined>()
  const [modelForm, setModelForm] = useState<ModelForm>(() => createBlankModelForm())
  const [modelHeadDrafts, setModelHeadDrafts] = useState<ModelHeadDraft[]>([])
  const [deleteSkuDialog, setDeleteSkuDialog] = useState<DeleteSkuDialogState | null>(null)
  const [showArchivedSkus, setShowArchivedSkus] = useState(false)
  const [showArchivedRuns, setShowArchivedRuns] = useState(false)
  const [nowMs, setNowMs] = useState(() => Date.now())

  const defaultModel = useMemo(() => models.find((model) => model.isDefault) ?? models[0], [models])
  const selectedModel = models.find((model) => model.id === selectedModelId) ?? defaultModel
  const planModel = models.find((model) => model.id === shotPlan?.modelProfileId) ?? selectedModel
  const shotPlanPlanning = shotPlan?.status === 'PLANNING'
  const canGenerateGroup = Boolean(
    selectedSku
      && shotPlan
      && !shotPlanPlanning
      && previewRun
      && previewRun.status === 'SUCCEEDED'
      && previewRun.skuId === selectedSku.id
      && previewRun.shotPlanId === shotPlan.id,
  )
  const relevantHistoryRuns = useMemo(() => {
    const runs = selectedSku ? historyRuns.filter((run) => run.skuId === selectedSku.id) : [...historyRuns]
    return runs.sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt))
  }, [historyRuns, selectedSku?.id])
  const taskStage = getGenerateTaskStage(selectedSku, shotPlan, previewRun, generationRun)
  const filteredGenerateSkus = useMemo(() => {
    const keyword = generateSkuFilter.trim().toLowerCase()
    if (!keyword) return skus
    return skus.filter((sku) =>
      [sku.sku, sku.title, sku.status]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(keyword)),
    )
  }, [generateSkuFilter, skus])

  function navigate(routeInput: AppRoute, replace = false) {
    const url = routeToUrl(routeInput)
    if (replace) window.history.replaceState(null, '', url)
    else window.history.pushState(null, '', url)
    setRoute(routeInput)
    setPage(routeInput.page)
  }

  function currentGenerateRoute(): AppRoute {
    if (generationRun?.kind && generationRun.kind !== 'preview_grid') return { page: 'generate', generateRunId: generationRun.id }
    if (selectedSku) return { page: 'generate', generateSku: selectedSku.sku, generatePreviewRunId: previewRun?.id }
    if (previewRun?.kind === 'preview_grid') return { page: 'generate', generatePreviewRunId: previewRun.id }
    return { page: 'generate' }
  }

  useEffect(() => {
    void refreshAuth()
  }, [])

  useEffect(() => {
    if (!appReady) return
    void refreshBaseData()
  }, [appReady, showArchivedSkus, showArchivedRuns])

  useEffect(() => {
    const handlePopState = () => {
      const nextRoute = parseRoute()
      setRoute(nextRoute)
      setPage(nextRoute.page)
    }
    window.addEventListener('popstate', handlePopState)
    return () => window.removeEventListener('popstate', handlePopState)
  }, [])

  useEffect(() => {
    if (!appReady) return
    const storedRunId = window.localStorage.getItem('app:lastGenerationRunId')
    if (!storedRunId) return
    const runId = Number(storedRunId)
    if (!Number.isFinite(runId)) return
    const currentRoute = parseRoute()
    if (currentRoute.page === 'generate' && !currentRoute.generateSku && !currentRoute.generateRunId && window.localStorage.getItem('app:lastPreviewRunId')) {
      return
    }
    void api.getRun(runId)
      .then((result) => {
        if (result.item.kind === 'preview_grid') {
          window.localStorage.removeItem('app:lastGenerationRunId')
          window.localStorage.setItem('app:lastPreviewRunId', String(result.item.id))
          if (currentRoute.page === 'generate' && !currentRoute.generateSku && !currentRoute.generateRunId) {
            void restorePreviewRun(result.item, { syncUrl: false })
          }
          return
        }
        if (currentRoute.page === 'generate' && !currentRoute.generateSku && !currentRoute.generateRunId) {
          void restoreHistoryRun(result.item, { syncUrl: false })
          return
        }
        setGenerationRun(result.item)
      })
      .catch(() => window.localStorage.removeItem('app:lastGenerationRunId'))
  }, [appReady])

  useEffect(() => {
    if (!appReady) return
    const currentRoute = parseRoute()
    if (currentRoute.page !== 'generate' || currentRoute.generateSku || currentRoute.generateRunId) return
    const storedPreviewRunId = window.localStorage.getItem('app:lastPreviewRunId')
    if (!storedPreviewRunId) return
    const runId = Number(storedPreviewRunId)
    if (!Number.isFinite(runId)) return
    void api.getRun(runId)
      .then((result) => {
        if (result.item.kind === 'preview_grid') void restorePreviewRun(result.item, { syncUrl: false })
      })
      .catch(() => window.localStorage.removeItem('app:lastPreviewRunId'))
  }, [appReady])

  useEffect(() => {
    if (!generationRun) return
    if (generationRun.kind === 'preview_grid') return
    window.localStorage.setItem('app:lastGenerationRunId', String(generationRun.id))
  }, [generationRun?.id])

  useEffect(() => {
    if (!previewRun || previewRun.kind !== 'preview_grid') return
    window.localStorage.setItem('app:lastPreviewRunId', String(previewRun.id))
  }, [previewRun?.id])

  useEffect(() => {
    if (!(generationRun && isRunActive(generationRun)) && !(previewRun && isRunActive(previewRun))) return
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [generationRun?.id, generationRun?.status, previewRun?.id, previewRun?.status])

  useEffect(() => {
    if (!previewRun || !isRunActive(previewRun)) return
    const poll = async () => {
      try {
        const result = await api.getRun(previewRun.id)
        setPreviewRun(result.item)
      } catch (err) {
        setError(err instanceof Error ? err.message : '刷新预览状态失败')
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 3000)
    return () => window.clearInterval(timer)
  }, [previewRun?.id, previewRun?.status])

  useEffect(() => {
    if (!generationRun || !isRunActive(generationRun)) return
    const poll = async () => {
      try {
        const result = await api.getRun(generationRun.id)
        setGenerationRun(result.item)
        if (!isRunActive(result.item)) await refreshHistoryRuns()
      } catch (err) {
        setError(err instanceof Error ? err.message : '刷新生成状态失败')
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 3000)
    return () => window.clearInterval(timer)
  }, [generationRun?.id, generationRun?.status])

  useEffect(() => {
    if (!shotPlan || shotPlan.status !== 'PLANNING') return
    const poll = async () => {
      try {
        const result = await api.getPlan(shotPlan.id)
        setShotPlan(result.item)
        if (selectedSku) setSelectedSku((await api.getSku(selectedSku.id)).item)
        if (result.item.status !== 'PLANNING') await refreshBaseData()
      } catch (err) {
        setError(err instanceof Error ? err.message : '刷新方案状态失败')
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 3000)
    return () => window.clearInterval(timer)
  }, [shotPlan?.id, shotPlan?.status, selectedSku?.id])

  useEffect(() => {
    const editingModel = models.find((model) => model.id === editingModelId)
    if (editingModel) {
      setModelForm(modelToForm(editingModel))
      setModelHeadDrafts([])
    }
  }, [editingModelId, models])

  useEffect(() => {
    if (!appReady) return
    if (routeKey(route) === hydratedRouteKey) return
    if (route.page === 'garments' && route.sku) {
      const targetSku = skus.find((sku) => sku.sku === route.sku)
      if (targetSku) {
        setHydratedRouteKey(routeKey(route))
        void chooseSku(targetSku, { syncUrl: false })
      }
      return
    }
    if (route.page === 'history' && route.runId) {
      setHydratedRouteKey(routeKey(route))
      void openHistoryRun(route.runId)
      return
    }
    if (route.page === 'generate' && route.generateRunId) {
      const targetRun = historyRuns.find((run) => run.id === route.generateRunId)
      if (targetRun) {
        setHydratedRouteKey(routeKey(route))
        void restoreHistoryRun(targetRun, { syncUrl: false })
      }
      return
    }
    if (route.page === 'generate' && route.generateSku) {
      const targetSku = skus.find((sku) => sku.sku === route.generateSku)
      if (targetSku) {
        setHydratedRouteKey(routeKey(route))
        void chooseSku(targetSku, { syncUrl: false, previewRunId: route.generatePreviewRunId })
      }
      return
    }
    if (route.page === 'models' && route.modelId && models.length) {
      const targetModel = models.find((model) => model.modelId === route.modelId)
      if (targetModel) {
        setHydratedRouteKey(routeKey(route))
        setEditingModelId(targetModel.id)
      }
    }
  }, [appReady, route, skus, models, historyRuns, hydratedRouteKey])

  async function refreshAuth() {
    try {
      const result = await api.session()
      setAuth({ checked: true, authRequired: result.authRequired, authenticated: result.authenticated })
    } catch {
      setAuth({ checked: true, authRequired: true, authenticated: false })
    }
  }

  async function handleLogin(password: string) {
    const result = await api.login(password)
    setAuth({ checked: true, authRequired: result.authRequired, authenticated: result.authenticated })
    await refreshBaseData()
  }

  async function handleLogout() {
    await api.logout()
    setAuth({ checked: true, authRequired: true, authenticated: false })
    setSkus([])
    setModels([])
    setHistoryRuns([])
    setSelectedSku(null)
    setShotPlan(null)
    setPreviewRun(null)
    setGenerationRun(null)
    setSelectedHistoryRun(null)
  }

  async function refreshBaseData() {
    setError(null)
    const [skuResult, modelResult, runResult] = await Promise.all([
      api.listSkus({ includeArchived: showArchivedSkus }),
      api.listModels(),
      api.listRuns({ includeArchived: showArchivedRuns }),
    ])
    setSkus(skuResult.items)
    setModels(modelResult.items)
    setHistoryRuns(runResult.items)
    if (!selectedModelId && modelResult.items[0]) setSelectedModelId(modelResult.items[0].id)
    if (!editingModelId && modelResult.items[0]) setEditingModelId(modelResult.items[0].id)
  }

  async function refreshHistoryRuns() {
    const result = await api.listRuns({ includeArchived: showArchivedRuns })
    setHistoryRuns(result.items)
  }

  async function chooseSku(sku: GarmentSku, options: { syncUrl?: boolean; targetPage?: Page; previewRunId?: number } = {}) {
    setError(null)
    const detail = await api.getSku(sku.id)
    const planResult = await api.getDefaultPlan(sku.id)
    const rawPreferredPreview = await resolvePreviewRun(detail.item.id, undefined, options.previewRunId)
    const preferredPreview = rawPreferredPreview && (!planResult.item || rawPreferredPreview.shotPlanId === planResult.item.id)
      ? rawPreferredPreview
      : null
    const activePlan = planResult.item
    setSelectedSku(detail.item)
    setEditSkuForm({
      sku: detail.item.sku,
      title: detail.item.title ?? detail.item.sku,
      status: detail.item.status,
    })
    setExtraFiles([])
    setShotPlan(activePlan)
    const restoredPreview = preferredPreview ?? await resolvePreviewRun(detail.item.id, activePlan?.id)
    setPreviewRun(restoredPreview)
    setGenerationRun(null)
    if (options.syncUrl !== false) {
      const targetPage = options.targetPage ?? page
      if (targetPage === 'garments') navigate({ page: 'garments', sku: detail.item.sku })
      if (targetPage === 'generate') navigate({ page: 'generate', generateSku: detail.item.sku, generatePreviewRunId: restoredPreview?.id })
    }
  }

  async function resolvePreviewRun(skuId: number, shotPlanId?: number, preferredRunId?: number) {
    if (preferredRunId) {
      try {
        const result = await api.getRun(preferredRunId)
        if (result.item.kind === 'preview_grid' && result.item.skuId === skuId && (!shotPlanId || result.item.shotPlanId === shotPlanId)) {
          return result.item
        }
      } catch {
        return null
      }
    }
    if (!shotPlanId) return null
    const runs = (await api.listRuns()).items
    const preview = runs.find((run) => run.kind === 'preview_grid' && run.skuId === skuId && run.shotPlanId === shotPlanId)
    if (!preview) return null
    try {
      return (await api.getRun(preview.id)).item
    } catch {
      return null
    }
  }

  function stageLooseImages(selectedFiles: File[]) {
    if (selectedFiles.length === 0) return
    const defaultSku = looseSkuInput.trim() || `未命名货号-${importPackages.length + 1}`
    setImportPackages((current) => [
      ...current,
      createImportPackage(defaultSku, selectedFiles, '散图上传'),
    ])
    setLooseSkuInput('')
  }

  function stageFolderImages(selectedFiles: File[]) {
    if (selectedFiles.length === 0) return
    const groups = groupFilesByTopFolder(selectedFiles)
    setImportPackages((current) => [
      ...current,
      ...Object.entries(groups).map(([folderName, folderFiles]) => createImportPackage(folderName, folderFiles, folderName)),
    ])
  }

  function updateImportPackage(packageId: string, patch: Partial<Pick<ImportPackage, 'sku' | 'title'>>) {
    setImportPackages((current) =>
      current.map((item) => (item.id === packageId ? { ...item, ...patch } : item)),
    )
  }

  function removeImportImage(packageId: string, imageId: string) {
    setImportPackages((current) =>
      current
        .map((item) =>
          item.id === packageId
            ? { ...item, images: item.images.filter((image) => image.id !== imageId) }
            : item,
        )
        .filter((item) => item.images.length > 0),
    )
  }

  function removeImportPackage(packageId: string) {
    setImportPackages((current) => current.filter((item) => item.id !== packageId))
  }

  async function uploadImportPackage(packageId: string) {
    const target = importPackages.find((item) => item.id === packageId)
    if (!target) return
    if (!target.sku.trim()) {
      setImportPackages((current) =>
        current.map((item) => (item.id === packageId ? { ...item, status: 'failed', error: '请填写货号' } : item)),
      )
      return
    }
    if (target.images.length === 0) {
      setImportPackages((current) =>
        current.map((item) => (item.id === packageId ? { ...item, status: 'failed', error: '至少保留一张图片' } : item)),
      )
      return
    }
    setBusy('uploading')
    setError(null)
    setImportPackages((current) =>
      current.map((item) => (item.id === packageId ? { ...item, status: 'uploading', error: undefined } : item)),
    )
    try {
      const result = await api.uploadSku(target.sku.trim(), target.images.map((image) => image.file), target.title.trim())
      const detail = await api.getSku(result.item.id)
      setSelectedSku(detail.item)
      setEditSkuForm({
        sku: detail.item.sku,
        title: detail.item.title ?? detail.item.sku,
        status: detail.item.status,
      })
      setImportPackages((current) => current.filter((item) => item.id !== packageId))
      await refreshBaseData()

      try {
        const planResult = await api.createPlan(result.item.id, selectedModel?.id)
        const plannedDetail = await api.getSku(result.item.id)
        setSelectedSku(plannedDetail.item)
        setShotPlan(planResult.item)
        setPreviewRun(null)
        setEditSkuForm({
        sku: plannedDetail.item.sku,
          title: plannedDetail.item.title ?? plannedDetail.item.sku,
          status: plannedDetail.item.status,
        })
        navigate({ page: 'garments', sku: plannedDetail.item.sku })
        await refreshBaseData()
      } catch (planErr) {
        setShotPlan(null)
        setError(planErr instanceof Error ? `图包已上传成功，但默认方案生成失败：${planErr.message}` : '图包已上传成功，但默认方案生成失败')
      }
    } catch (err) {
      setImportPackages((current) =>
        current.map((item) =>
          item.id === packageId
            ? { ...item, status: 'failed', error: err instanceof Error ? err.message : '上传失败' }
            : item,
        ),
      )
    } finally {
      setBusy('idle')
    }
  }

  async function uploadAllDraftPackages() {
    for (const item of importPackages) {
      if (item.status === 'draft' || item.status === 'failed') {
        await uploadImportPackage(item.id)
      }
    }
  }

  async function createPlan(skuOverride?: GarmentSku) {
    const targetSku = skuOverride ?? selectedSku
    if (!targetSku) return
    setBusy('planning')
    setError(null)
    try {
      const result = await api.createPlan(targetSku.id, selectedModel?.id)
      setShotPlan(result.item)
      setPreviewRun(null)
      const detail = (await api.getSku(targetSku.id)).item
      setSelectedSku(detail)
      setGenerationRun(null)
      if (page === 'garments') navigate({ page: 'garments', sku: detail.sku }, true)
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
      const result = await api.generateGroup(shotPlan.id, previewRun?.id)
      setGenerationRun(result.item)
      setPreviewRun(result.item)
      navigate({ page: 'generate', generateRunId: result.item.id }, true)
      await refreshHistoryRuns()
    } catch (err) {
      setError(err instanceof Error ? err.message : '生成失败')
    } finally {
      setBusy('idle')
    }
  }

  async function previewGrid() {
    if (!shotPlan) return
    setBusy('previewing')
    setError(null)
    try {
      const result = await api.previewGrid(shotPlan.id)
      setPreviewRun(result.item)
      setGenerationRun(null)
      if (selectedSku) navigate({ page: 'generate', generateSku: selectedSku.sku, generatePreviewRunId: result.item.id }, true)
      await refreshHistoryRuns()
    } catch (err) {
      setError(err instanceof Error ? err.message : '生成分镜预览失败')
    } finally {
      setBusy('idle')
    }
  }

  async function saveShotPlan(plan: ShotPlan['plan']) {
    if (!shotPlan) return
    setBusy('saving-plan')
    setError(null)
    try {
      const result = await api.updatePlan(shotPlan.id, plan)
      setShotPlan(result.item)
      setPreviewRun(null)
      if (selectedSku) setSelectedSku((await api.getSku(selectedSku.id)).item)
      await refreshBaseData()
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存组图方案失败')
    } finally {
      setBusy('idle')
    }
  }

  async function regenerate(image: GenerationImage, reason: string) {
    setBusy('regenerating')
    setError(null)
    try {
      const result = await api.regenerate(image.id, reason)
      if (previewRun?.id === result.item.id) {
        setPreviewRun(result.item)
        setGenerationRun((current) => current?.id === result.item.id ? result.item : current)
      } else {
        setGenerationRun((current) => mergeRun(current, result.item))
      }
      await refreshHistoryRuns()
    } catch (err) {
      setError(err instanceof Error ? err.message : '重生失败')
    } finally {
      setBusy('idle')
    }
  }

  async function toggleFinal(image: GenerationImage) {
    const result = await api.setFinal(image.id, !image.isFinal)
    updateImageInState(result.item)
    await refreshHistoryRuns()
  }

  function updateImageInState(image: GenerationImage) {
    if (previewRun?.id === image.runId) {
      setPreviewRun((current) => {
        if (!current?.images) return current
        return {
          ...current,
          images: current.images.map((item) => (item.id === image.id ? image : item)),
        }
      })
      setGenerationRun((current) => {
        if (!current?.images || current.id !== image.runId) return current
        return {
          ...current,
          images: current.images.map((item) => (item.id === image.id ? image : item)),
        }
      })
      return
    }
    setGenerationRun((current) => {
      if (!current?.images) return current
      return {
        ...current,
        images: current.images.map((item) => (item.id === image.id ? image : item)),
      }
    })
    setSelectedHistoryRun((current) => {
      if (!current?.images || current.id !== image.runId) return current
      return {
        ...current,
        images: current.images.map((item) => (item.id === image.id ? image : item)),
      }
    })
  }

  async function updateReviewStatus(image: GenerationImage, status: string) {
    try {
      const result = await api.setReviewStatus(image.id, status)
      updateImageInState(result.item)
      await refreshHistoryRuns()
    } catch (err) {
      setError(err instanceof Error ? err.message : '更新交付状态失败')
    }
  }

  async function cancelImage(image: GenerationImage) {
    const confirmed = window.confirm(`确认取消图片记录 #${image.id}？`)
    if (!confirmed) return
    try {
      const result = await api.cancelImage(image.id)
      updateImageInState(result.item)
      await refreshHistoryRuns()
    } catch (err) {
      setError(err instanceof Error ? err.message : '取消生成图片失败')
    }
  }

  async function openHistoryRun(runId: number) {
    setError(null)
    try {
      const result = await api.getRun(runId)
      setSelectedHistoryRun(result.item)
      navigate({ page: 'history', runId: result.item.id })
    } catch (err) {
      setError(err instanceof Error ? err.message : '读取生成记录失败')
    }
  }

  async function restoreHistoryRun(run: GenerationRun, options: { syncUrl?: boolean } = {}) {
    if (run.kind === 'preview_grid') {
      await restorePreviewRun(run, options)
      return
    }
    setError(null)
    try {
      const [skuResult, planResult, runResult] = await Promise.all([
        api.getSku(run.skuId),
        api.getPlan(run.shotPlanId),
        api.getRun(run.id),
      ])
      setSelectedSku(skuResult.item)
      setEditSkuForm({
        sku: skuResult.item.sku,
        title: skuResult.item.title ?? skuResult.item.sku,
        status: skuResult.item.status,
      })
      setShotPlan(planResult.item)
      setGenerationRun(runResult.item)
      setPreviewRun(runResult.item)
      if (options.syncUrl === false) setPage('generate')
      else navigate({ page: 'generate', generateRunId: runResult.item.id })
    } catch (err) {
      setError(err instanceof Error ? err.message : '恢复历史入参失败')
    }
  }

  async function restorePreviewRun(run: GenerationRun, options: { syncUrl?: boolean } = {}) {
    setError(null)
    try {
      const [skuResult, planResult, runResult] = await Promise.all([
        api.getSku(run.skuId),
        api.getPlan(run.shotPlanId),
        api.getRun(run.id),
      ])
      setSelectedSku(skuResult.item)
      setEditSkuForm({
        sku: skuResult.item.sku,
        title: skuResult.item.title ?? skuResult.item.sku,
        status: skuResult.item.status,
      })
      setShotPlan(planResult.item)
      setPreviewRun(runResult.item)
      setGenerationRun(null)
      if (options.syncUrl === false) setPage('generate')
      else navigate({ page: 'generate', generateSku: skuResult.item.sku, generatePreviewRunId: runResult.item.id })
    } catch (err) {
      setError(err instanceof Error ? err.message : '恢复预览记录失败')
    }
  }

  async function updateSelectedSku() {
    if (!selectedSku) return
    setBusy('uploading')
    setError(null)
    try {
      const result = await api.updateSku(selectedSku.id, editSkuForm)
      setSelectedSku(result.item)
      await refreshBaseData()
      if (page === 'garments') navigate({ page: 'garments', sku: result.item.sku }, true)
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存失败')
    } finally {
      setBusy('idle')
    }
  }

  function requestDeleteSelectedSku() {
    if (!selectedSku) return
    setDeleteSkuDialog({ sku: selectedSku, confirmSku: '', deleteRelated: false })
  }

  async function deleteSelectedSku(deleteRelated: boolean, confirmSku?: string) {
    const targetSku = deleteSkuDialog?.sku ?? selectedSku
    if (!targetSku) return
    setBusy('uploading')
    setError(null)
    try {
      const result = await api.deleteSku(targetSku.id, { purge: deleteRelated, confirmSku })
      setSelectedSku(null)
      setShotPlan(null)
      setPreviewRun(null)
      if (deleteRelated) setGenerationRun(null)
      setDeleteSkuDialog(null)
      navigate({ page: 'garments' }, true)
      await refreshBaseData()
      if (result.objectCleanupErrors?.length) {
        setError('货号已清理，但部分对象存储文件删除失败，请稍后重试或联系管理员。')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除失败')
    } finally {
      setBusy('idle')
    }
  }

  async function addImagesToSelectedSku() {
    if (!selectedSku || extraFiles.length === 0) return
    setBusy('uploading')
    setError(null)
    try {
      const result = await api.addSkuImages(selectedSku.id, extraFiles)
      setSelectedSku(result.item)
      setExtraFiles([])
      if (page === 'garments') navigate({ page: 'garments', sku: result.item.sku }, true)
      await refreshBaseData()
    } catch (err) {
      setError(err instanceof Error ? err.message : '追加图片失败')
    } finally {
      setBusy('idle')
    }
  }

  async function reorderSelectedSkuImage(imageId: number, orderIndex: number) {
    if (!selectedSku?.images) return
    const moving = selectedSku.images.find((image) => image.id === imageId)
    if (!moving) return
    const list = selectedSku.images.filter((image) => image.id !== imageId)
    const nextIndex = Math.max(0, Math.min(orderIndex, list.length))
    list.splice(nextIndex, 0, moving)
    const reordered = list.map((image, index) => ({ ...image, sortOrder: index }))
    setSelectedSku({ ...selectedSku, images: reordered, coverUrl: reordered[0]?.url ?? selectedSku.coverUrl })
    setBusy('uploading')
    setError(null)
    try {
      await Promise.all(reordered.map((image, index) => api.updateGarmentImage(image.id, { sortOrder: index })))
      const refreshed = (await api.getSku(selectedSku.id)).item
      setSelectedSku(refreshed)
      await refreshBaseData()
    } catch (err) {
      setError(err instanceof Error ? err.message : '更新图片顺序失败')
      setSelectedSku((await api.getSku(selectedSku.id)).item)
    } finally {
      setBusy('idle')
    }
  }

  async function deleteImage(imageId: number) {
    if (!selectedSku) return
    const confirmed = window.confirm('确认删除这张素材图？这只会删除应用内上传副本。')
    if (!confirmed) return
    setError(null)
    try {
      await api.deleteGarmentImage(imageId)
      setSelectedSku((await api.getSku(selectedSku.id)).item)
      await refreshBaseData()
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除图片失败')
    }
  }

  function beginNewModel() {
    setEditingModelId(undefined)
    setModelForm(createBlankModelForm(models.length + 1))
    setModelHeadDrafts([])
    navigate({ page: 'models' })
  }

  async function saveModel() {
    setBusy('uploading')
    setError(null)
    try {
      const payload = {
        modelId: modelForm.modelId,
        name: modelForm.name,
        isDefault: modelForm.isDefault,
        profile: modelFormToProfile(modelForm, models.find((model) => model.id === editingModelId)),
      }
      const result = editingModelId
        ? await api.updateModel(editingModelId, payload)
        : await api.createModel(payload)
      if (!editingModelId && modelHeadDrafts.length > 0) {
        let latest = result.item
        for (const draft of modelHeadDrafts) {
          latest = (await api.addModelHeadReferences(latest.id, [draft.file], draft.angle)).item
        }
        result.item = latest
      }
      setEditingModelId(result.item.id)
      if (result.item.isDefault || !selectedModelId) setSelectedModelId(result.item.id)
      setModelHeadDrafts([])
      navigate({ page: 'models', modelId: result.item.modelId }, true)
      await refreshBaseData()
    } catch (err) {
      setError(err instanceof Error ? err.message : '保存数字模特失败')
    } finally {
      setBusy('idle')
    }
  }

  async function deleteEditingModel() {
    const editingModel = models.find((model) => model.id === editingModelId)
    if (!editingModel) return
    const confirmed = window.confirm(`确认删除数字模特 ${editingModel.name}？\n如果它已有生成记录，系统会阻止删除以保留历史。`)
    if (!confirmed) return
    setBusy('uploading')
    setError(null)
    try {
      await api.deleteModel(editingModel.id)
      const nextModels = (await api.listModels()).items
      setModels(nextModels)
      const nextModel = nextModels[0]
      setEditingModelId(nextModel?.id)
      if (selectedModelId === editingModel.id) setSelectedModelId(nextModel?.id)
      navigate(nextModel ? { page: 'models', modelId: nextModel.modelId } : { page: 'models' }, true)
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除数字模特失败')
    } finally {
      setBusy('idle')
    }
  }

  async function deleteGenerationRun(run: GenerationRun) {
    const activeNote = isRunActive(run) ? '\n该任务仍在生成中，归档后历史列表将不再展示，但后台状态和审计记录仍会保留。' : ''
    const confirmed = window.confirm(`确认归档生成记录 #${run.id}（${run.sku ?? '未知货号'}）？\n归档只会从历史列表隐藏记录，不会删除生成图片、输出文件或审计信息。${activeNote}`)
    if (!confirmed) return
    setError(null)
    try {
      await api.deleteRun(run.id)
      const nextRuns = (await api.listRuns()).items
      setHistoryRuns(nextRuns)
      if (selectedHistoryRun?.id === run.id) {
        setSelectedHistoryRun(null)
        if (page === 'history') navigate({ page: 'history' }, true)
      }
      if (previewRun?.id === run.id) {
        setPreviewRun(null)
        window.localStorage.removeItem('app:lastPreviewRunId')
      }
      if (generationRun?.id === run.id) {
        setGenerationRun(null)
        window.localStorage.removeItem('app:lastGenerationRunId')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '归档生成记录失败')
    }
  }

  async function cancelGenerationRun(run: GenerationRun) {
    const confirmed = window.confirm(`确认取消生成记录 #${run.id}？正在队列中或生成中的图片会标记为已取消。`)
    if (!confirmed) return
    setError(null)
    try {
      const result = await api.cancelRun(run.id)
      if (previewRun?.id === run.id) setPreviewRun(result.item)
      if (generationRun?.id === run.id) setGenerationRun(result.item)
      if (selectedHistoryRun?.id === run.id) setSelectedHistoryRun(result.item)
      await refreshHistoryRuns()
    } catch (err) {
      setError(err instanceof Error ? err.message : '取消生成记录失败')
    }
  }

  async function uploadModelHeadReferences(files: File[]) {
    if (files.length === 0) return
    if (!editingModelId) {
      setModelHeadDrafts((current) => {
        const next = [...current]
        for (const file of files) {
          next.push({
            id: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
            file,
            previewUrl: URL.createObjectURL(file),
            angle: '',
          })
        }
        return next
      })
      return
    }
    setBusy('uploading')
    setError(null)
    try {
      const result = await api.addModelHeadReferences(editingModelId, files)
      setModels((current) => current.map((model) => (model.id === result.item.id ? result.item : model)))
      setModelForm(modelToForm(result.item))
      navigate({ page: 'models', modelId: result.item.modelId }, true)
    } catch (err) {
      setError(err instanceof Error ? err.message : '上传头像失败')
    } finally {
      setBusy('idle')
    }
  }

  async function replaceModelHeadReference(angle: string, file: File) {
    if (!editingModelId) {
      setModelHeadDrafts((current) =>
        current.map((item) =>
          item.id === angle
            ? { ...item, file, previewUrl: URL.createObjectURL(file) }
            : item,
        ),
      )
      return
    }
    setBusy('uploading')
    setError(null)
    try {
      const result = await api.replaceModelHeadReference(editingModelId, angle, file)
      setModels((current) => current.map((model) => (model.id === result.item.id ? result.item : model)))
      setModelForm(modelToForm(result.item))
      navigate({ page: 'models', modelId: result.item.modelId }, true)
    } catch (err) {
      setError(err instanceof Error ? err.message : '替换头像失败')
    } finally {
      setBusy('idle')
    }
  }

  async function updateModelHeadReference(angle: string, payload: { angle?: string; orderIndex?: number }) {
    if (!editingModelId) {
      setModelHeadDrafts((current) => {
        const target = current.find((item) => item.id === angle)
        if (!target) return current
        if (payload.angle && current.some((item) => item.angle === payload.angle && item.id !== angle)) {
          setError('该角度已经有头像')
          return current
        }
        const updated = current.map((item) =>
          item.id === angle
            ? { ...item, angle: payload.angle ?? item.angle }
            : item,
        )
        if (payload.orderIndex === undefined) return updated
        const list = updated.filter((item) => item.id !== angle)
        const moving = updated.find((item) => item.id === angle)
        if (!moving) return updated
        const orderIndex = Math.max(0, Math.min(payload.orderIndex, list.length))
        list.splice(orderIndex, 0, moving)
        return list
      })
      return
    }
    setError(null)
    try {
      const result = await api.updateModelHeadReference(editingModelId, angle, payload)
      setModels((current) => current.map((model) => (model.id === result.item.id ? result.item : model)))
      setModelForm(modelToForm(result.item))
      navigate({ page: 'models', modelId: result.item.modelId }, true)
    } catch (err) {
      setError(err instanceof Error ? err.message : '更新头像失败')
    }
  }

  async function deleteModelHeadReference(angle: string) {
    if (!editingModelId) {
      setModelHeadDrafts((current) => current.filter((item) => item.id !== angle))
      return
    }
    const confirmed = window.confirm(`确认删除 ${headReferenceLabel(angle)} 头像？`)
    if (!confirmed) return
    setError(null)
    try {
      const result = await api.deleteModelHeadReference(editingModelId, angle)
      setModels((current) => current.map((model) => (model.id === result.item.id ? result.item : model)))
      setModelForm(modelToForm(result.item))
      navigate({ page: 'models', modelId: result.item.modelId }, true)
    } catch (err) {
      setError(err instanceof Error ? err.message : '删除头像失败')
    }
  }

  if (!auth.checked) {
    return <AuthLoading />
  }

  if (auth.authRequired && !auth.authenticated) {
    return <LoginPage onLogin={handleLogin} />
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div>
          <div className="brand">ruyi-dream</div>
          <h1>{pageTitle(page)}</h1>
          <p>{pageSubtitle(page)}</p>
        </div>
        <nav>
          <button className={page === 'generate' ? 'active' : ''} onClick={() => navigate(currentGenerateRoute())}>生成组图</button>
          <button className={page === 'garments' ? 'active' : ''} onClick={() => navigate(selectedSku ? { page: 'garments', sku: selectedSku.sku } : { page: 'garments' })}>服装素材</button>
          <button className={page === 'models' ? 'active' : ''} onClick={() => {
            const editingModel = models.find((model) => model.id === editingModelId)
            navigate(editingModel ? { page: 'models', modelId: editingModel.modelId } : { page: 'models' })
          }}>数字模特</button>
          <button className={page === 'history' ? 'active' : ''} onClick={() => navigate(selectedHistoryRun ? { page: 'history', runId: selectedHistoryRun.id } : { page: 'history' })}>生成记录</button>
          {auth.authRequired && <button onClick={() => void handleLogout()}>退出</button>}
        </nav>
      </header>

      {error && <div className="alert">{error}</div>}

      {page === 'generate' && <main className="generate-workbench">
        <section className="task-hero">
          <div className="task-hero-copy">
            <span className="eyebrow">当前拍摄任务</span>
            <h2>{selectedSku ? selectedSku.sku : '选择一个服装货号'}</h2>
            <p>{selectedSku?.analysis?.summary ?? '从左侧选择已保存货号，或先导入图包并生成默认拍摄方案。'}</p>
          </div>
          <div className="task-status-strip">
            <TaskStatus label="服装" value={selectedSku ? `${selectedSku.imageCount ?? selectedSku.images?.length ?? 0} 张素材` : '未选择'} state={selectedSku ? 'ready' : 'idle'} />
            <TaskStatus label="默认方案" value={shotPlan ? `${shotPlan.plan.shots.length} 张图位` : '未保存'} state={shotPlanPlanning ? 'running' : shotPlan ? 'ready' : 'idle'} />
            <TaskStatus label="分镜" value={previewRun ? statusText(previewRun.status) : '未预览'} state={previewRun?.status === 'SUCCEEDED' ? 'ready' : previewRun && isRunActive(previewRun) ? 'running' : 'idle'} />
            <TaskStatus label="最终图" value={generationRun ? statusText(generationRun.status) : '未生成'} state={generationRun?.status === 'SUCCEEDED' ? 'ready' : generationRun && isRunActive(generationRun) ? 'running' : 'idle'} />
          </div>
        </section>

        <section className="task-layout">
          <aside className="task-sidebar">
            <Panel title="服装货号" icon={<FileImage size={18} />}>
              <button className="primary full" onClick={() => navigate({ page: 'garments' })}>
                <Upload size={17} />导入新图包
              </button>
              <label className="search-field">
                <Search size={16} />
                <input
                  value={generateSkuFilter}
                  onChange={(event) => setGenerateSkuFilter(event.target.value)}
                  placeholder="搜索货号或标题"
                />
              </label>
              <div className="sku-list task-sku-list">
                {filteredGenerateSkus.length === 0 && <EmptyText text={skus.length === 0 ? '还没有上传货号' : '没有匹配的货号'} />}
                {filteredGenerateSkus.map((sku) => (
                  <button
                    key={sku.id}
                    className={`sku-item ${selectedSku?.id === sku.id ? 'selected' : ''}`}
                    onClick={() => void chooseSku(sku, { targetPage: 'generate' })}
                  >
                    {sku.coverUrl ? (
                      <ImagePreviewThumb
                        src={mediaUrl(sku.coverUrl)}
                        name={sku.sku}
                        fileSize={sku.coverFileSize}
                        compact
                      />
                    ) : <div className="thumb-fallback" />}
                    <span>
                      <strong>{sku.sku}</strong>
                      <small>{sku.imageCount ?? 0} 张图片 · {sku.archivedAt ? '已归档' : statusText(sku.status)}</small>
                    </span>
                  </button>
                ))}
              </div>
            </Panel>
          </aside>

          <section className="production-stage">
            <div className="stage-board">
              <div className="stage-board-head">
                <div>
                  <span className="eyebrow">生产进度</span>
                  <h2>{taskStage.title}</h2>
                  <p>{taskStage.description}</p>
                </div>
                <Tag>{taskStage.tag}</Tag>
              </div>
              <div className="stage-steps">
                {['选择服装', '确认方案', '分镜预览', '最终组图', '交付导出'].map((label, index) => (
                  <span className={index <= taskStage.index ? 'active' : ''} key={label}>
                    <b>{index + 1}</b>{label}
                  </span>
                ))}
              </div>
            </div>

            <div className="visual-workspace">
              <div className="visual-head">
                <div>
                  <h2>{generationRun ? '最终组图' : '分镜预览'}</h2>
                  <p>{generationRun ? '确认可用图，必要时单张重生，最后导出交付。' : '先确认 2x3 分镜方向，再进入最终 5 张成片。'}</p>
                </div>
                {shotPlan && <span>方案 #{shotPlan.id}</span>}
              </div>

              {previewRun && (
                <ResultGallery
                  title="分镜预览"
                  run={previewRun}
                  imageFilter={(image) => image.shotIndex === 0}
                  loading={busy === 'previewing' || isRunActive(previewRun)}
                  nowMs={nowMs}
                  onRegenerate={(image, reason) => void regenerate(image, reason)}
                  onToggleFinal={(image) => void toggleFinal(image)}
                  onReviewStatus={(image, status) => void updateReviewStatus(image, status)}
                  onCancelImage={(image) => void cancelImage(image)}
                  onExport={(runId) => void api.exportRun(runId)}
                  previewOnly
                />
              )}

              <ResultGallery
                title="最终组图"
                run={generationRun}
                imageFilter={(image) => image.shotIndex > 0}
                loading={busy === 'generating' || busy === 'regenerating' || Boolean(generationRun && isRunActive(generationRun))}
                nowMs={nowMs}
                onRegenerate={(image, reason) => void regenerate(image, reason)}
                onToggleFinal={(image) => void toggleFinal(image)}
                onReviewStatus={(image, status) => void updateReviewStatus(image, status)}
                onCancelImage={(image) => void cancelImage(image)}
                onExport={(runId) => void api.exportRun(runId)}
              />
            </div>
          </section>

          <aside className="task-control-panel">
            <Panel title="下一步" icon={<Sparkles size={18} />}>
              <div className="next-action-card">
                <strong>{taskStage.actionTitle}</strong>
                <p>{taskStage.actionText}</p>
                <GeneratePrimaryAction
                  selectedSku={selectedSku}
                  shotPlan={shotPlan}
                  shotPlanPlanning={shotPlanPlanning}
                  previewRun={previewRun}
                  generationRun={generationRun}
                  busy={busy}
                  canGenerateGroup={canGenerateGroup}
                  onImport={() => navigate({ page: 'garments' })}
                  onPlan={() => selectedSku && navigate({ page: 'garments', sku: selectedSku.sku })}
                  onPreview={() => void previewGrid()}
                  onGenerate={() => void generateGroup()}
                />
              </div>
            </Panel>

            <Panel title="任务摘要" icon={<Images size={18} />}>
              <div className="task-summary-list">
                <TaskSummary label="货号" value={selectedSku?.sku ?? '未选择'} />
                <TaskSummary label="数字模特" value={planModel?.name ?? '未设置'} />
                <TaskSummary label="品牌风格" value={shotPlan?.plan.brandStyle?.summary ?? '默认白/浅灰棚拍'} />
                <TaskSummary label="最近记录" value={`${relevantHistoryRuns.length} 条`} />
              </div>
            </Panel>

            <Panel title={selectedSku ? '当前货号记录' : '最近生成记录'} icon={<History size={18} />}>
              <GenerationHistoryOverview
                runs={relevantHistoryRuns.slice(0, 8)}
                onOpenRun={(run) => void restoreHistoryRun(run)}
                onDeleteRun={(run) => void deleteGenerationRun(run)}
                onCancelRun={(run) => void cancelGenerationRun(run)}
              />
            </Panel>
          </aside>
        </section>
      </main>}

      {page === 'garments' && (
        <GarmentLibraryPage
          skus={skus}
          selectedSku={selectedSku}
          shotPlan={shotPlan}
          busy={busy}
          extraFiles={extraFiles}
          importPackages={importPackages}
          showArchivedSkus={showArchivedSkus}
          looseSkuInput={looseSkuInput}
          onShowArchivedSkusChange={setShowArchivedSkus}
          onLooseSkuInputChange={setLooseSkuInput}
          onStageLooseImages={stageLooseImages}
          onStageFolderImages={stageFolderImages}
          onUpdateImportPackage={updateImportPackage}
          onRemoveImportImage={removeImportImage}
          onRemoveImportPackage={removeImportPackage}
          onUploadImportPackage={(packageId) => void uploadImportPackage(packageId)}
          onUploadAllDraftPackages={() => void uploadAllDraftPackages()}
          onExtraFilesChange={setExtraFiles}
          editSkuForm={editSkuForm}
          onEditSkuFormChange={setEditSkuForm}
          onSaveSku={() => void updateSelectedSku()}
          onDeleteSelectedSku={requestDeleteSelectedSku}
          onAddImages={() => void addImagesToSelectedSku()}
          onReorderImage={(imageId, orderIndex) => void reorderSelectedSkuImage(imageId, orderIndex)}
          onDeleteImage={(imageId) => void deleteImage(imageId)}
          onSelectSku={(sku) => void chooseSku(sku, { targetPage: 'garments' })}
          onUseInGenerate={(sku) => {
            void chooseSku(sku, { targetPage: 'generate' })
          }}
          onCreatePlan={(sku) => {
            void createPlan(sku)
          }}
          onSavePlan={(plan) => {
            void saveShotPlan(plan)
          }}
        />
      )}

      {page === 'models' && (
        <ModelProfilesPage
          models={models}
          editingModelId={editingModelId}
          form={modelForm}
          busy={busy}
          headDrafts={modelHeadDrafts}
          onSelectModel={(modelId) => {
            setEditingModelId(modelId)
            const model = models.find((item) => item.id === modelId)
            navigate(model ? { page: 'models', modelId: model.modelId } : { page: 'models' })
          }}
          onNewModel={beginNewModel}
          onFormChange={setModelForm}
          onSaveModel={() => void saveModel()}
          onDeleteModel={() => void deleteEditingModel()}
          onUploadHeadReferences={(files) => void uploadModelHeadReferences(files)}
          onReplaceHeadReference={(angle, file) => void replaceModelHeadReference(angle, file)}
          onUpdateHeadReference={(angle, payload) => void updateModelHeadReference(angle, payload)}
          onDeleteHeadReference={(angle) => void deleteModelHeadReference(angle)}
        />
      )}

      {page === 'history' && (
        <HistoryPage
          runs={historyRuns}
          selectedRun={selectedHistoryRun}
          filter={historyFilter}
          showArchivedRuns={showArchivedRuns}
          onFilterChange={setHistoryFilter}
          onShowArchivedRunsChange={setShowArchivedRuns}
          onOpenRun={(runId) => void openHistoryRun(runId)}
          onRestoreRun={(run) => void restoreHistoryRun(run)}
          onDeleteRun={(run) => void deleteGenerationRun(run)}
          onCancelRun={(run) => void cancelGenerationRun(run)}
        />
      )}

      {deleteSkuDialog && (
        <DeleteSkuDialog
          sku={deleteSkuDialog.sku}
          confirmSku={deleteSkuDialog.confirmSku}
          deleteRelated={deleteSkuDialog.deleteRelated}
          busy={busy}
          onConfirmSkuChange={(confirmSku) => setDeleteSkuDialog((current) => current ? { ...current, confirmSku } : current)}
          onDeleteRelatedChange={(deleteRelated) => setDeleteSkuDialog((current) => current ? { ...current, deleteRelated } : current)}
          onClose={() => setDeleteSkuDialog(null)}
          onDelete={() => void deleteSelectedSku(deleteSkuDialog.deleteRelated, deleteSkuDialog.confirmSku)}
        />
      )}
    </div>
  )
}

function AuthLoading() {
  return (
    <div className="auth-shell">
      <div className="auth-panel">
        <div className="brand">ruyi-dream</div>
        <h1>正在进入工作台</h1>
      </div>
    </div>
  )
}

function LoginPage({ onLogin }: { onLogin: (password: string) => Promise<void> }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await onLogin(password)
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="auth-shell">
      <form className="auth-panel" onSubmit={(event) => void submit(event)}>
        <div className="brand">ruyi-dream</div>
        <h1>访问工作台</h1>
        <label className="field">
          <span>访问密码</span>
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoFocus
            required
          />
        </label>
        {error && <div className="inline-error">{error}</div>}
        <button className="primary full" disabled={submitting || !password.trim()}>
          {submitting ? <Loader2 size={16} className="spin" /> : null}
          进入
        </button>
      </form>
    </div>
  )
}

function DeleteSkuDialog({
  sku,
  confirmSku,
  deleteRelated,
  busy,
  onConfirmSkuChange,
  onDeleteRelatedChange,
  onClose,
  onDelete,
}: {
  sku: GarmentSku
  confirmSku: string
  deleteRelated: boolean
  busy: BusyState
  onConfirmSkuChange: (value: string) => void
  onDeleteRelatedChange: (value: boolean) => void
  onClose: () => void
  onDelete: () => void
}) {
  const hasHistory = skuHasLinkedHistory(sku)
  const impact = sku.deleteImpact
  const canDelete = !deleteRelated || confirmSku === sku.sku
  return createPortal(
    <div className="preview-backdrop" role="dialog" aria-modal="true" onClick={onClose}>
      <div className="danger-dialog" onClick={(event) => event.stopPropagation()}>
        <div className="danger-dialog-header">
          <div>
            <span className="eyebrow">删除货号</span>
            <h2>{sku.sku}</h2>
          </div>
          <button className="icon-only secondary" onClick={onClose} disabled={busy !== 'idle'} title="关闭">
            <X size={18} />
          </button>
        </div>
        <p>
          {hasHistory
            ? '这个货号仍有关联的方案和生成记录。默认操作会归档货号并从日常列表隐藏；勾选后会同时删除关联记录和文件。'
            : '默认操作会归档货号并从日常列表隐藏。勾选后会同时删除素材副本和文件。'}
        </p>
        <div className="delete-impact-grid">
          <Stat label="素材图" value={sku.imageCount ?? 0} />
          <Stat label="拍摄方案" value={impact?.shotPlanCount ?? 0} />
          <Stat label="生成记录" value={impact?.generationRunCount ?? 0} />
          <Stat label="已归档" value={impact?.archivedGenerationRunCount ?? 0} />
        </div>
        <label className="checkbox-row danger-option">
          <input
            type="checkbox"
            checked={deleteRelated}
            onChange={(event) => onDeleteRelatedChange(event.target.checked)}
            disabled={busy !== 'idle'}
          />
          <span>同时删除关联记录和文件</span>
        </label>
        {deleteRelated && (
          <label className="field">
            <span>输入货号确认删除</span>
            <input
              value={confirmSku}
              onChange={(event) => onConfirmSkuChange(event.target.value)}
              placeholder={sku.sku}
              autoFocus
            />
          </label>
        )}
        <div className="danger-dialog-actions">
          <button className="secondary" onClick={onClose} disabled={busy !== 'idle'}>取消</button>
          <button className="danger" onClick={onDelete} disabled={busy !== 'idle' || !canDelete}>
            {busy === 'uploading' ? <Loader2 className="spin" size={16} /> : <Trash2 size={16} />}
            {deleteRelated ? '删除货号及关联记录' : '归档货号'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

function GarmentLibraryPage({
  skus,
	  selectedSku,
	  shotPlan,
	  busy,
  extraFiles,
  importPackages,
  showArchivedSkus,
  looseSkuInput,
  onShowArchivedSkusChange,
  onLooseSkuInputChange,
  onStageLooseImages,
  onStageFolderImages,
  onUpdateImportPackage,
  onRemoveImportImage,
  onRemoveImportPackage,
	  onUploadImportPackage,
	  onUploadAllDraftPackages,
	  onExtraFilesChange,
  editSkuForm,
  onEditSkuFormChange,
  onSaveSku,
	  onDeleteSelectedSku,
  onAddImages,
  onReorderImage,
  onDeleteImage,
  onSelectSku,
  onUseInGenerate,
  onCreatePlan,
  onSavePlan,
}: {
  skus: GarmentSku[]
  selectedSku: GarmentSku | null
	  shotPlan: ShotPlan | null
	  busy: BusyState
	  extraFiles: File[]
  importPackages: ImportPackage[]
  showArchivedSkus: boolean
  looseSkuInput: string
  onShowArchivedSkusChange: (value: boolean) => void
  onLooseSkuInputChange: (value: string) => void
  onStageLooseImages: (files: File[]) => void
  onStageFolderImages: (files: File[]) => void
  onUpdateImportPackage: (packageId: string, patch: Partial<Pick<ImportPackage, 'sku' | 'title'>>) => void
  onRemoveImportImage: (packageId: string, imageId: string) => void
  onRemoveImportPackage: (packageId: string) => void
	  onUploadImportPackage: (packageId: string) => void
	  onUploadAllDraftPackages: () => void
	  onExtraFilesChange: (files: File[]) => void
  editSkuForm: { sku: string; title: string; status: string }
  onEditSkuFormChange: (value: { sku: string; title: string; status: string }) => void
  onSaveSku: () => void
	  onDeleteSelectedSku: () => void
  onAddImages: () => void
  onReorderImage: (imageId: number, orderIndex: number) => void
  onDeleteImage: (imageId: number) => void
  onSelectSku: (sku: GarmentSku) => void
  onUseInGenerate: (sku: GarmentSku) => void
  onCreatePlan: (sku: GarmentSku) => void
  onSavePlan: (plan: ShotPlan['plan']) => void
}) {
  const analyzed = skus.filter((sku) => sku.status === 'ANALYZED' || sku.analysis).length
  const pendingImportPackages = importPackages.filter((item) => item.status !== 'uploaded')
  const [draggingImageId, setDraggingImageId] = useState<number | null>(null)
  const [dragOverImageId, setDragOverImageId] = useState<number | null>(null)
  const skuFormChanged = Boolean(
    selectedSku
      && (
        editSkuForm.sku !== selectedSku.sku
        || editSkuForm.title !== (selectedSku.title ?? selectedSku.sku)
        || editSkuForm.status !== selectedSku.status
      ),
  )
  return (
    <main className="garment-page">
      <section className="library-toolbar">
        <div>
          <h2>服装素材</h2>
          <p>上传、查看和使用货号图包。MVP 会保留原始上传记录，不覆盖素材。</p>
        </div>
        <div className="library-stats">
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={showArchivedSkus}
              onChange={(event) => onShowArchivedSkusChange(event.target.checked)}
            />
            <span>显示已归档</span>
          </label>
          <Stat label="货号" value={skus.length} />
          <Stat label="已策划" value={analyzed} />
          <Stat label="待策划" value={Math.max(skus.length - analyzed, 0)} />
        </div>
      </section>

      <section className="garment-layout">
        <aside className="garment-sidebar">
          <Panel title="导入新图包" icon={<Upload size={18} />}>
            <label className="field">
              <span>散图货号</span>
              <input value={looseSkuInput} onChange={(event) => onLooseSkuInputChange(event.target.value)} placeholder="上传散图时填写" />
            </label>
            <label className="dropzone compact">
              <ImagePlus size={22} />
              <span>上传 N 张图片</span>
              <input
                type="file"
                accept="image/*"
                multiple
                onChange={(event) => {
                  onStageLooseImages(Array.from(event.target.files ?? []))
                  event.currentTarget.value = ''
                }}
              />
            </label>
            <label className="dropzone compact folder-drop">
              <FolderOpen size={22} />
              <span>上传一个或多个文件夹</span>
              <input
                type="file"
                multiple
                // @ts-expect-error webkitdirectory is supported by Chromium/WebKit for folder uploads.
                webkitdirectory=""
                onChange={(event) => {
                  onStageFolderImages(Array.from(event.target.files ?? []))
                  event.currentTarget.value = ''
                }}
              />
            </label>
            <p className="helper-text">先生成本地预览，可改货号、删图片，再保存上传。第一张图片默认作为封面。</p>
          </Panel>

          <Panel title="待保存图包" icon={<Clock size={18} />}>
            <div className="import-summary">
              <strong>{pendingImportPackages.length}</strong>
              <span>个待处理图包</span>
            </div>
            <button
              className="primary full"
              onClick={onUploadAllDraftPackages}
              disabled={busy !== 'idle' || pendingImportPackages.length === 0}
            >
              {busy === 'uploading' ? <Loader2 className="spin" size={17} /> : <Upload size={17} />}
              保存并上传全部
            </button>
          </Panel>

          <Panel title="货号列表" icon={<Images size={18} />}>
            <div className="library-list">
              {skus.length === 0 && <EmptyText text="还没有上传货号" />}
              {skus.map((sku) => (
                <button
                  key={sku.id}
                  className={`library-list-item ${selectedSku?.id === sku.id ? 'selected' : ''}`}
                  onClick={() => onSelectSku(sku)}
                >
                  {sku.coverUrl ? (
                    <ImagePreviewThumb
                      src={mediaUrl(sku.coverUrl)}
                      name={sku.sku}
                      fileSize={sku.coverFileSize}
                      compact
                    />
                  ) : <div className="thumb-fallback" />}
                  <span>
                    <strong>{sku.sku}</strong>
                    <small>{sku.imageCount ?? 0} 张图片 · {statusText(sku.status)}</small>
                  </span>
                </button>
              ))}
            </div>
          </Panel>
        </aside>

        <section className="garment-detail">
          {pendingImportPackages.length > 0 ? (
            <ImportPreviewBoard
              packages={pendingImportPackages}
              busy={busy}
              onUpdatePackage={onUpdateImportPackage}
              onRemoveImage={onRemoveImportImage}
              onRemovePackage={onRemoveImportPackage}
              onUploadPackage={onUploadImportPackage}
            />
          ) : selectedSku ? (
            <>
              <div className="detail-header">
                <div>
                  <span className="eyebrow">当前货号</span>
                  <h2>{selectedSku.sku}</h2>
                  <p>{selectedSku.archivedAt ? '该货号已归档，可在显示已归档时查看。' : selectedSku.analysis?.summary ?? '还没有生成组图方案，暂未形成服装摘要。'}</p>
                </div>
                <div className="detail-actions">
                  <button className="secondary" onClick={() => onUseInGenerate(selectedSku)}>
                    <FileImage size={16} />用于生成
                  </button>
	                  <button className="primary" onClick={() => onCreatePlan(selectedSku)} disabled={busy !== 'idle'}>
	                    {busy === 'planning' ? <Loader2 className="spin" size={16} /> : <Sparkles size={16} />}
	                    {shotPlan ? '更新默认方案' : '生成默认方案'}
	                  </button>
	                  <button className="danger ghost" onClick={onDeleteSelectedSku} disabled={busy !== 'idle'} title="删除货号">
	                    <Trash2 size={16} />删除
	                  </button>
	                </div>
	              </div>

	              <div className="detail-grid">
                  <section className="material-board sku-edit-panel">
                    <div className="section-heading tight">
                      <h2>货号信息</h2>
                      <Edit3 size={16} />
                    </div>
                    <div className="sku-edit-grid">
                      <label className="field">
                        <span>货号</span>
                        <input value={editSkuForm.sku} onChange={(event) => onEditSkuFormChange({ ...editSkuForm, sku: event.target.value })} />
                      </label>
                      <label className="field">
                        <span>标题</span>
                        <input value={editSkuForm.title} onChange={(event) => onEditSkuFormChange({ ...editSkuForm, title: event.target.value })} />
                      </label>
                      <label className="field">
                        <span>状态</span>
                        <select value={editSkuForm.status} onChange={(event) => onEditSkuFormChange({ ...editSkuForm, status: event.target.value })}>
                          <option value="NEW">新建</option>
                          <option value="ANALYZED">已策划</option>
                          <option value="CONFIRMED">已确认</option>
                        </select>
                      </label>
                      <button className="primary" onClick={onSaveSku} disabled={busy !== 'idle' || !skuFormChanged}>
                        {busy === 'uploading' ? <Loader2 className="spin" size={16} /> : <Save size={16} />}
                        保存货号
                      </button>
                    </div>
                  </section>

	                <section className="material-board asset-panel">
	                  <div className="section-heading tight">
	                    <h2>素材图片</h2>
	                    <span>{selectedSku.images?.length ?? 0} 张</span>
	                  </div>
                  <div className="append-images">
                    <label className="mini-upload">
                      <Plus size={16} />
                      <span>{extraFiles.length ? `已选择 ${extraFiles.length} 张` : '追加图片'}</span>
                      <input
                        type="file"
                        accept="image/*"
                        multiple
                        onChange={(event) => onExtraFilesChange(Array.from(event.target.files ?? []))}
                      />
                    </label>
                    <button className="secondary" onClick={onAddImages} disabled={busy !== 'idle' || extraFiles.length === 0}>
                      <Upload size={15} />保存图片
                    </button>
                  </div>
                  <div className="asset-grid">
                    {(selectedSku.images ?? []).map((image, index) => (
                      <article
                        className={`asset-card ${draggingImageId === image.id ? 'dragging' : ''} ${dragOverImageId === image.id ? 'drag-over' : ''}`}
                        key={image.id}
                        draggable={busy === 'idle'}
                        onDragStart={() => {
                          setDraggingImageId(image.id)
                          setDragOverImageId(null)
                        }}
                        onDragEnd={() => {
                          setDraggingImageId(null)
                          setDragOverImageId(null)
                        }}
                        onDragOver={(event) => {
                          event.preventDefault()
                          if (draggingImageId && draggingImageId !== image.id) setDragOverImageId(image.id)
                        }}
                        onDrop={(event) => {
                          event.preventDefault()
                          if (!draggingImageId || draggingImageId === image.id) return
                          onReorderImage(draggingImageId, index)
                          setDraggingImageId(null)
                          setDragOverImageId(null)
                        }}
                      >
                        <div className="asset-thumb">
                          {image.url ? (
                            <ImagePreviewThumb
                              src={mediaUrl(image.url)}
                              name={image.filename}
                              fileSize={image.fileSize}
                            />
                          ) : <span>无预览</span>}
                        </div>
                        <div className="asset-meta">
                          <strong title={image.filename}>{middleEllipsis(image.filename)}</strong>
                          <small>
                            {image.url ? (
                              <ImageInfoLine src={mediaUrl(image.url)} fileSize={image.fileSize} />
                            ) : '图片信息读取中'}
                          </small>
                          <div className="asset-order-row">
                            <span className="drag-hint">拖动排序</span>
                            <b>#{index + 1}</b>
                          </div>
                          <div className="asset-actions single">
                            <button onClick={() => onDeleteImage(image.id)} title="删除图片">
                              <Trash2 size={14} />
                            </button>
                          </div>
                        </div>
                      </article>
	                    ))}
	                  </div>
	                </section>

	                <section className="material-board shot-plan-panel">
	                  <div className="section-heading tight">
	                    <h2>拍摄方案</h2>
	                    <span>{shotPlan ? `#${shotPlan.id}` : '未保存'}</span>
	                  </div>
	                  {shotPlan ? (
	                    <ShotPlanBoard shotPlan={shotPlan} editable={busy === 'idle'} onSave={onSavePlan} saving={busy === 'saving-plan'} />
	                  ) : (
	                    <div className="plan-empty">
	                      <p>还没有拍摄方案</p>
	                      <span>点击右上角“生成默认方案”，系统会根据服装素材整理品类、服饰搭配和分镜剧本；生成组图时将直接使用这里保存的方案。</span>
	                    </div>
	                  )}
	                </section>
	              </div>
            </>
          ) : (
            <div className="library-empty">
              <FileImage size={38} />
              <h2>选择一个货号查看素材</h2>
              <p>上传或点击左侧货号后，这里会展示完整图片、拍摄方案和使用入口。</p>
            </div>
          )}
        </section>
      </section>
    </main>
  )
}

function ModelProfilesPage({
  models,
  editingModelId,
  form,
  busy,
  headDrafts,
  onSelectModel,
  onNewModel,
  onFormChange,
  onSaveModel,
  onDeleteModel,
  onUploadHeadReferences,
  onReplaceHeadReference,
  onUpdateHeadReference,
  onDeleteHeadReference,
}: {
  models: ModelProfile[]
  editingModelId?: number
  form: ModelForm
  busy: BusyState
  headDrafts: ModelHeadDraft[]
  onSelectModel: (id: number) => void
  onNewModel: () => void
  onFormChange: (value: ModelForm) => void
  onSaveModel: () => void
  onDeleteModel: () => void
  onUploadHeadReferences: (files: File[]) => void
  onReplaceHeadReference: (angle: string, file: File) => void
  onUpdateHeadReference: (angle: string, payload: { angle?: string; orderIndex?: number }) => void
  onDeleteHeadReference: (angle: string) => void
}) {
  const editingModel = models.find((model) => model.id === editingModelId)
  const isNew = !editingModel
  const headEntries = isNew ? getDraftHeadEntries(headDrafts) : getSavedHeadEntries(editingModel)
  const [draggingId, setDraggingId] = useState<string | null>(null)
  const [dragOverId, setDragOverId] = useState<string | null>(null)

  return (
    <main className="model-page">
      <section className="library-toolbar">
        <div>
          <h2>数字模特</h2>
          <p>维护模特身份图、身体参数和生成约束。每次组图只选择一个模特作为主体。</p>
        </div>
        <div className="library-stats">
          <Stat label="模特" value={models.length} />
          <Stat label="头像" value={headEntries.length} />
          <Stat label="默认" value={models.some((model) => model.isDefault) ? 1 : 0} />
        </div>
      </section>

      <section className="model-layout">
        <aside className="model-sidebar">
          <Panel title="模特列表" icon={<Images size={18} />}>
            <div className="library-list">
              {models.length === 0 && <EmptyText text="还没有数字模特" />}
              {models.map((model) => (
                <button
                  key={model.id}
                  className={`library-list-item ${editingModelId === model.id ? 'selected' : ''}`}
                  onClick={() => onSelectModel(model.id)}
                >
                  {model.previewUrl ? (
                    <ImagePreviewThumb
                      src={mediaUrl(model.previewUrl)}
                      name={model.name}
                      fileSize={modelPreviewFileSize(model)}
                      compact
                    />
                  ) : <div className="thumb-fallback" />}
                  <span>
                    <strong>{model.name}</strong>
                    <small>{model.modelId} · {model.isDefault ? '默认模特' : '备用模特'}</small>
                  </span>
                </button>
              ))}
            </div>
          </Panel>

          <Panel title="新建模特" icon={<Plus size={18} />}>
            <p className="small-muted">基础档案和头像可以一起编辑，最后统一保存。</p>
            <button className="primary full" onClick={onNewModel}>
              <Plus size={17} />新建数字模特
            </button>
          </Panel>
        </aside>

        <section className="model-detail">
          <div className="detail-header">
            <div>
              <span className="eyebrow">{isNew ? '新建档案' : editingModel?.modelId}</span>
              <h2>{isNew ? '新增数字模特' : editingModel?.name}</h2>
              <p>{isNew ? '基础档案和头像可以一起编辑，最后统一保存。' : modelSummary(editingModel)}</p>
            </div>
            <div className="detail-actions">
              <button className="primary" onClick={onSaveModel} disabled={busy !== 'idle'}>
                {busy === 'uploading' ? <Loader2 className="spin" size={16} /> : <Save size={16} />}
                保存档案
              </button>
              {!isNew && (
                <button className="danger" onClick={onDeleteModel} disabled={busy !== 'idle'}>
                  <Trash2 size={16} />删除模特
                </button>
              )}
            </div>
          </div>

          <div className="model-editor-grid">
            <section className="model-form-board">
              <div className="section-heading tight">
                <h2>基础档案</h2>
                <Edit3 size={16} />
              </div>
              <div className="model-form-grid">
                <label className="field">
                  <span>模特 ID</span>
                  <input value={form.modelId} onChange={(event) => onFormChange({ ...form, modelId: event.target.value })} />
                </label>
                <label className="field">
                  <span>名称</span>
                  <input value={form.name} onChange={(event) => onFormChange({ ...form, name: event.target.value })} />
                </label>
                <label className="field">
                  <span>身高</span>
                  <input value={form.height} onChange={(event) => onFormChange({ ...form, height: event.target.value })} placeholder="170cm" />
                </label>
                <label className="field">
                  <span>体重</span>
                  <input value={form.weight} onChange={(event) => onFormChange({ ...form, weight: event.target.value })} placeholder="可选，如 50kg" />
                </label>
                <label className="field">
                  <span>身型</span>
                  <input value={form.bodyShape} onChange={(event) => onFormChange({ ...form, bodyShape: event.target.value })} placeholder="H型 / 梨型 / 苹果型" />
                </label>
                <label className="field">
                  <span>胸围</span>
                  <input value={form.bust} onChange={(event) => onFormChange({ ...form, bust: event.target.value })} placeholder="80cm" />
                </label>
                <label className="field">
                  <span>腰围</span>
                  <input value={form.waist} onChange={(event) => onFormChange({ ...form, waist: event.target.value })} placeholder="59cm" />
                </label>
                <label className="field">
                  <span>臀围</span>
                  <input value={form.hips} onChange={(event) => onFormChange({ ...form, hips: event.target.value })} placeholder="89cm" />
                </label>
                <label className="field">
                  <span>其它信息</span>
                  <input value={form.notes} onChange={(event) => onFormChange({ ...form, notes: event.target.value })} placeholder="都市、通勤、轻熟、自然、克制" />
                </label>
                <label className="field toggle-field">
                  <span>默认模特</span>
                  <input type="checkbox" checked={form.isDefault} onChange={(event) => onFormChange({ ...form, isDefault: event.target.checked })} />
                </label>
              </div>
            </section>

            <aside className="head-reference-panel">
              <div className="section-heading tight">
                <h2>头像角度</h2>
                <span>{headEntries.length} 张</span>
              </div>

              {!headEntries.some((item) => item.angle === 'front') && (
                <div className="inline-warning">当前没有“正脸”头像，建议补一张，后续生成会更稳定。</div>
              )}

              <div className="head-grid">
                <label className="head-upload-card">
                  <ImagePlus size={16} />
                  <strong>上传头像</strong>
                  <small>支持单张或多张，上传后再逐张设置角度</small>
                  <input
                    type="file"
                    accept="image/*"
                    multiple
                    onChange={(event) => {
                      const files = Array.from(event.target.files ?? [])
                      onUploadHeadReferences(files)
                      event.currentTarget.value = ''
                    }}
                  />
                </label>

                {headEntries.map((entry, index) => (
                  <article
                    className={`head-card ${draggingId === entry.id ? 'dragging' : ''} ${dragOverId === entry.id ? 'drag-over' : ''}`}
                    key={entry.id}
                    draggable
                    onDragStart={() => {
                      setDraggingId(entry.id)
                      setDragOverId(null)
                    }}
                    onDragEnd={() => {
                      setDraggingId(null)
                      setDragOverId(null)
                    }}
                    onDragOver={(event) => {
                      event.preventDefault()
                      if (draggingId && draggingId !== entry.id) setDragOverId(entry.id)
                    }}
                    onDrop={(event) => {
                      event.preventDefault()
                      if (!draggingId || draggingId === entry.id) return
                      void onUpdateHeadReference(draggingId, { orderIndex: index })
                      setDraggingId(null)
                      setDragOverId(null)
                    }}
                  >
                    <div className="head-thumb">
                      {entry.url ? (
                        <ImagePreviewThumb
                          src={entry.url}
                          name={entry.filename}
                          fileSize={entry.fileSize}
                        />
                      ) : <span>无预览</span>}
                    </div>
                    <div className="head-card-row">
                      <select
                        value={entry.angle}
                        disabled={busy !== 'idle'}
                        onChange={(event) => void onUpdateHeadReference(entry.id, { angle: event.target.value })}
                      >
                        <option value="">未设置</option>
                        {HEAD_REFERENCE_OPTIONS.map((option) => {
                          const usedByOther = headEntries.some((item) => item.angle === option.value && item.id !== entry.id)
                          return (
                            <option key={option.value} value={option.value} disabled={usedByOther}>
                              {option.label}
                            </option>
                          )
                        })}
                      </select>
                      <span className="drag-hint">拖动排序</span>
                    </div>
                    <small title={entry.filename}>{middleEllipsis(entry.filename)}</small>
                    <div className="head-card-actions">
                      <label className="mini-upload">
                        <Upload size={14} />
                        <span>更换</span>
                        <input
                          type="file"
                          accept="image/*"
                          disabled={busy !== 'idle'}
                          onChange={(event) => {
                            const file = event.target.files?.[0]
                            if (file) void onReplaceHeadReference(entry.id, file)
                            event.currentTarget.value = ''
                          }}
                        />
                      </label>
                      <button
                        className="danger icon-only"
                        onClick={() => onDeleteHeadReference(entry.id)}
                        title="删除头像"
                        disabled={busy !== 'idle'}
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </article>
                ))}
              </div>

              {editingModel && (
                <div className="storage-box">
                  <strong>素材目录</strong>
                  <small>{editingModel.assetDir}</small>
                </div>
              )}
            </aside>
          </div>
        </section>
      </section>
    </main>
  )
}

function ImportPreviewBoard({
  packages,
  busy,
  onUpdatePackage,
  onRemoveImage,
  onRemovePackage,
  onUploadPackage,
}: {
  packages: ImportPackage[]
  busy: BusyState
  onUpdatePackage: (packageId: string, patch: Partial<Pick<ImportPackage, 'sku' | 'title'>>) => void
  onRemoveImage: (packageId: string, imageId: string) => void
  onRemovePackage: (packageId: string) => void
  onUploadPackage: (packageId: string) => void
}) {
  const duplicateSkus = findDuplicateSkus(packages)
  return (
    <div className="import-preview">
      <div className="detail-header">
        <div>
          <span className="eyebrow">上传新货号</span>
          <h2>预览与编辑</h2>
          <p>图片还在浏览器本地暂存。确认每个图包的货号和图片后，再逐个保存上传；第一张图片默认作为封面。</p>
        </div>
      </div>

      <div className="import-package-list">
        {packages.map((item) => {
          const hasDuplicate = duplicateSkus.has(item.sku.trim()) && item.sku.trim()
          return (
            <article className={`import-package ${item.status}`} key={item.id}>
              <div className="import-package-header">
                <div>
                  <strong>{item.sourceName}</strong>
                  <small>{item.images.length} 张图片 · {importStatusText(item.status)}</small>
                </div>
                <div className="import-actions">
                  <button className="secondary" onClick={() => onUploadPackage(item.id)} disabled={busy !== 'idle' || item.status === 'uploaded'}>
                    {item.status === 'uploading' ? <Loader2 className="spin" size={15} /> : <Upload size={15} />}
                    {item.status === 'failed' ? '重试' : '保存上传'}
                  </button>
                  <button className="danger icon-only" onClick={() => onRemovePackage(item.id)} disabled={item.status === 'uploading'}>
                    <Trash2 size={15} />
                  </button>
                </div>
              </div>

              <div className="import-fields">
                <label className="field">
                  <span>货号</span>
                  <input
                    value={item.sku}
                    onChange={(event) => onUpdatePackage(item.id, { sku: event.target.value })}
                  />
                </label>
                <label className="field">
                  <span>标题</span>
                  <input
                    value={item.title}
                    onChange={(event) => onUpdatePackage(item.id, { title: event.target.value })}
                  />
                </label>
              </div>

              {hasDuplicate && <div className="inline-warning">当前待上传列表中存在重复货号，保存前建议修改。</div>}
              {item.error && <div className="inline-error">{item.error}</div>}

              <div className="import-image-section">
                <div className="section-heading tight">
                  <h2>素材图片</h2>
                  <span>{item.images.length} 张</span>
                </div>
                <div className="asset-grid import-image-grid">
                {item.images.map((image) => (
                  <article className="asset-card" key={image.id}>
                    <div className="asset-thumb">
                      <ImagePreviewThumb
                        src={image.previewUrl}
                        name={image.file.name}
                        fileSize={image.file.size}
                      />
                    </div>
                    <div className="asset-meta">
                      <strong title={image.file.name}>{middleEllipsis(image.file.name)}</strong>
                      <small title={image.relativePath}>
                        <ImageInfoLine src={image.previewUrl} fileSize={image.file.size} />
                      </small>
                      <div className="asset-actions single">
                        <button onClick={() => onRemoveImage(item.id, image.id)} title="移除图片">
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </div>
                  </article>
                ))}
                </div>
              </div>
            </article>
          )
        })}
      </div>
    </div>
  )
}

function FactRows({ analysis }: { analysis?: GarmentSku['analysis'] }) {
  if (!analysis) {
    return (
      <div className="fact-empty">
        <p>待策划</p>
        <span>点击“生成默认方案”后，系统会根据服装素材生成品类、结构和组图建议。</span>
      </div>
    )
  }
  return (
    <div className="fact-rows">
      <FactRow label="品类" value={analysis.category} />
      <FactRow label="颜色" value={analysis.color} />
      <FactRow label="面料" value={analysis.fabric} />
      <FactRow label="关键结构" value={(analysis.keyStructures ?? []).join('、')} />
      <FactRow label="保真重点" value={(analysis.preserveStrictly ?? []).join('、')} />
    </div>
  )
}

function FactRow({ label, value }: { label: string; value?: string }) {
  return (
    <div className="fact-row">
      <span>{label}</span>
      <strong>{value || '待确认'}</strong>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="stat">
      <strong>{value}</strong>
      <span>{label}</span>
    </div>
  )
}

function GeneratePrimaryAction({
  selectedSku,
  shotPlan,
  shotPlanPlanning,
  previewRun,
  generationRun,
  busy,
  canGenerateGroup,
  onImport,
  onPlan,
  onPreview,
  onGenerate,
}: {
  selectedSku: GarmentSku | null
  shotPlan: ShotPlan | null
  shotPlanPlanning: boolean
  previewRun: GenerationRun | null
  generationRun: GenerationRun | null
  busy: BusyState
  canGenerateGroup: boolean
  onImport: () => void
  onPlan: () => void
  onPreview: () => void
  onGenerate: () => void
}) {
  if (!selectedSku) {
    return (
      <button className="primary full" onClick={onImport}>
        <Upload size={18} />导入或选择服装
      </button>
    )
  }

  if (!shotPlan) {
    return (
      <button className="primary full" onClick={onPlan}>
        <Sparkles size={18} />去保存默认方案
      </button>
    )
  }

  if (shotPlanPlanning) {
    return (
      <button className="primary full" disabled>
        <Loader2 className="spin" size={18} />默认方案生成中
      </button>
    )
  }

  if (!previewRun || previewRun.status !== 'SUCCEEDED') {
    return (
      <button className="primary full" onClick={onPreview} disabled={busy !== 'idle' || Boolean(previewRun && isRunActive(previewRun))}>
        {busy === 'previewing' || (previewRun && isRunActive(previewRun)) ? <Loader2 className="spin" size={18} /> : <Sparkles size={18} />}
        生成分镜预览
      </button>
    )
  }

  if (!generationRun || generationRun.status !== 'SUCCEEDED') {
    return (
      <button className="primary full" onClick={onGenerate} disabled={!canGenerateGroup || busy !== 'idle' || Boolean(generationRun && isRunActive(generationRun))}>
        {busy === 'generating' || (generationRun && isRunActive(generationRun)) ? <Loader2 className="spin" size={18} /> : <Sparkles size={18} />}
        生成最终组图
      </button>
    )
  }

  return (
    <button className="secondary full" onClick={onPreview} disabled={busy !== 'idle'}>
      <RefreshCw size={18} />重新生成分镜
    </button>
  )
}

function TaskStatus({ label, value, state }: { label: string; value: string; state: 'idle' | 'ready' | 'running' }) {
  return (
    <div className={`task-status ${state}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  )
}

function TaskSummary({ label, value }: { label: string; value: string }) {
  return (
    <div className="task-summary-item">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  )
}

function PlaceholderPage({ title, text }: { title: string; text: string }) {
  return (
    <main className="placeholder-page">
      <h2>{title}</h2>
      <p>{text}</p>
    </main>
  )
}

function GenerationHistoryOverview({
  runs,
  onOpenRun,
  onDeleteRun,
  onCancelRun,
}: {
  runs: GenerationRun[]
  onOpenRun: (run: GenerationRun) => void
  onDeleteRun: (run: GenerationRun) => void
  onCancelRun: (run: GenerationRun) => void
}) {
  const relevantRuns = [...runs]
    .sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt))

  if (relevantRuns.length === 0) {
    return <EmptyText text="还没有生成任务" />
  }

  return (
    <div className="generation-history-overview">
      {relevantRuns.map((run) => (
        <article className="generation-history-card" key={run.id}>
          <button className="generation-history-main" onClick={() => onOpenRun(run)}>
            <div className="mini-preview-strip">
              {(run.summary?.previews ?? []).slice(0, 3).map((preview) => (
                <HistoryPreviewImage key={preview.id} url={preview.url} title={preview.title} />
              ))}
              {(run.summary?.previews ?? []).length === 0 && <span>无图</span>}
            </div>
            <span>
              <strong>{run.sku ?? `记录 #${run.id}`}</strong>
              <small>{statusText(run.status)} · {run.summary?.succeededCount ?? 0}/{run.summary?.imageCount ?? 0} 张</small>
              <small>
                记录 #{run.id}
                {run.summary?.regenerationCount ? ` · 重生 ${run.summary.regenerationCount}` : ''}
              </small>
            </span>
          </button>
          <button className="secondary icon-only history-delete-button archive-button" onClick={() => onDeleteRun(run)} title="归档记录">
            <Archive size={14} />
          </button>
          {isRunActive(run) && (
            <button className="secondary icon-only history-delete-button" onClick={() => onCancelRun(run)} title="取消任务">
              <X size={14} />
            </button>
          )}
        </article>
      ))}
    </div>
  )
}

function HistoryPreviewImage({ url, title }: { url: string | null; title: string }) {
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    setFailed(false)
  }, [url])

  if (!url || failed) return <span className="history-preview-fallback">{failed ? '加载失败' : '无图'}</span>

  return <img src={mediaUrl(url)} alt={title} onError={() => setFailed(true)} />
}

function HistoryPage({
  runs,
  selectedRun,
  filter,
  showArchivedRuns,
  onFilterChange,
  onShowArchivedRunsChange,
  onOpenRun,
  onRestoreRun,
  onDeleteRun,
  onCancelRun,
}: {
  runs: GenerationRun[]
  selectedRun: GenerationRun | null
  filter: string
  showArchivedRuns: boolean
  onFilterChange: (value: string) => void
  onShowArchivedRunsChange: (value: boolean) => void
  onOpenRun: (runId: number) => void
  onRestoreRun: (run: GenerationRun) => void
  onDeleteRun: (run: GenerationRun) => void
  onCancelRun: (run: GenerationRun) => void
}) {
  const keyword = filter.trim().toLowerCase()
  const visibleRuns = keyword
    ? runs.filter((run) =>
        [run.sku, run.skuTitle, run.status, String(run.id), String(run.shotPlanId)]
          .filter(Boolean)
          .some((value) => String(value).toLowerCase().includes(keyword)),
      )
    : runs

  return (
    <main className="history-page">
      <section className="panel history-list-panel">
        <div className="panel-title"><History size={18} /><span>历史任务</span></div>
        <div className="history-toolbar">
          <input
            value={filter}
            onChange={(event) => onFilterChange(event.target.value)}
            placeholder="搜索货号、记录号、状态"
          />
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={showArchivedRuns}
              onChange={(event) => onShowArchivedRunsChange(event.target.checked)}
            />
            <span>显示已归档</span>
          </label>
          <span>{visibleRuns.length} 条任务</span>
        </div>
        <div className="history-run-list">
          {visibleRuns.length === 0 && <EmptyText text="还没有匹配的生成任务" />}
          {visibleRuns.map((run) => (
            <article className={`history-run-card ${selectedRun?.id === run.id ? 'selected' : ''}`} key={run.id}>
              <button className="history-run-main" onClick={() => onOpenRun(run.id)}>
                <div className="history-preview-strip">
                  {(run.summary?.previews ?? []).slice(0, 4).map((preview) => (
                    <HistoryPreviewImage key={preview.id} url={preview.url} title={preview.title} />
                  ))}
                  {(run.summary?.previews ?? []).length === 0 && <div className="history-preview-empty">无图</div>}
                </div>
                <span className="history-run-copy">
                  <strong>{run.sku ?? `记录 #${run.id}`}</strong>
                  <small>{run.skuTitle || `方案 #${run.shotPlanId}`} · {run.archivedAt ? '已归档' : run.createdAt}</small>
                  <small>
                    {statusText(run.status)} · {run.summary?.succeededCount ?? 0}/{run.summary?.imageCount ?? 0} 张完成
                    {run.summary?.finalCount ? ` · ${run.summary.finalCount} 张最终图` : ''}
                    {run.summary?.regenerationCount ? ` · 重生 ${run.summary.regenerationCount} 次` : ''}
                  </small>
                </span>
              </button>
              <button className="secondary" onClick={() => onRestoreRun(run)}>恢复</button>
              {isRunActive(run) && <button className="secondary" onClick={() => onCancelRun(run)}>取消</button>}
              <button className="secondary icon-only archive-button" onClick={() => onDeleteRun(run)} title="归档记录">
                <Archive size={15} />
              </button>
            </article>
          ))}
        </div>
      </section>

      <section className="panel history-detail-panel">
        <div className="panel-title"><FileImage size={18} /><span>任务详情</span></div>
        {!selectedRun ? (
          <EmptyText text="选择一条历史任务查看入参、输出和重生过程" />
        ) : (
          <HistoryRunDetail run={selectedRun} onRestoreRun={onRestoreRun} onDeleteRun={onDeleteRun} onCancelRun={onCancelRun} />
        )}
      </section>
    </main>
  )
}

function HistoryRunDetail({
  run,
  onRestoreRun,
  onDeleteRun,
  onCancelRun,
}: {
  run: GenerationRun
  onRestoreRun: (run: GenerationRun) => void
  onDeleteRun: (run: GenerationRun) => void
  onCancelRun: (run: GenerationRun) => void
}) {
  const firstImage = run.images?.[0]
  const firstTaskPackage = firstImage?.taskPackage ?? {}
  const garment = firstImage?.garmentSnapshot ?? (firstTaskPackage.garment as Record<string, unknown> | undefined)
  const model = firstImage?.modelSnapshot ?? (firstTaskPackage.model as Record<string, unknown> | undefined)
  const brandStyle = (firstTaskPackage.brandStyle as Record<string, unknown> | undefined) ?? {}
  const totalAttempts = run.images?.reduce((sum, image) => sum + Math.max(image.currentAttemptIndex || image.attempts?.length || 1, 1), 0) ?? 0

  return (
    <div className="history-detail">
      <div className="history-detail-head">
        <div>
          <h2>{run.sku ?? `生成任务 #${run.id}`}</h2>
          <p>记录 #{run.id} · {statusText(run.status)} · {run.createdAt}</p>
        </div>
        <div className="history-detail-actions">
          <button className="primary" onClick={() => onRestoreRun(run)}>
            <RefreshCw size={16} />恢复到生成页
          </button>
          {isRunActive(run) && (
            <button className="secondary" onClick={() => onCancelRun(run)}>
              <X size={16} />取消任务
            </button>
          )}
          <button className="danger" onClick={() => onDeleteRun(run)}>
            <Trash2 size={16} />归档记录
          </button>
        </div>
      </div>

      <div className="history-stats">
        <span><strong>{run.images?.length ?? 0}</strong><small>图位</small></span>
        <span><strong>{totalAttempts}</strong><small>调用/尝试</small></span>
        <span><strong>{run.images?.filter((image) => image.isFinal).length ?? 0}</strong><small>最终图</small></span>
        <span><strong>{run.shotPlanId}</strong><small>方案</small></span>
      </div>

      <div className="history-section">
        <h3>本次入参快照</h3>
        <div className="snapshot-grid">
          <SnapshotItem label="货号" value={run.sku ?? '-'} />
          <SnapshotItem label="模特" value={snapshotText(model, ['modelId'])} />
          <SnapshotItem label="品牌模板" value={snapshotText(brandStyle, ['name', 'summary'])} />
          <SnapshotItem label="商品摘要" value={snapshotText(garment, ['summary', 'category', 'color', 'fabric'])} />
        </div>
      </div>

      <div className="history-section">
        <h3>输出与重生过程</h3>
        <div className="history-shot-list">
          {(run.images ?? []).map((image) => (
            <article className="history-shot-card" key={image.id}>
              <div className="history-shot-image">
                {image.url ? <img src={mediaUrl(image.url)} alt={image.title} /> : <span>{statusText(image.status)}</span>}
                {image.isFinal && <b>最终图</b>}
              </div>
	              <div className="history-shot-body">
	                <strong>{String(image.shotIndex).padStart(2, '0')} {image.title}</strong>
	                <small>{statusText(image.status)} · {reviewStatusText(image.reviewStatus)} · 图片记录 #{image.id}</small>
	                {SHOW_DEBUG_INFO && (
	                  <>
	                    <p>{image.prompt || '暂无提示词快照'}</p>
	                    <details>
	                      <summary>查看 {Math.max(image.attempts?.length || image.currentAttemptIndex || 1, 1)} 次调用</summary>
	                      <div className="attempt-list">
	                        {(image.attempts?.length ? image.attempts : [{ attemptIndex: 1, kind: 'initial', prompt: image.prompt }]).map((attempt) => (
	                          <div className="attempt-row" key={String(attempt['attemptIndex'] ?? attempt['createdAt'] ?? image.id)}>
	                            <span>#{String(attempt['attemptIndex'] ?? 1)} · {attempt['kind'] === 'regeneration' ? '重生' : '初次生成'}</span>
	                            <small>{String(attempt['reason'] || image.regenerateReason || '无原因备注')} · {String(attempt['providerStatus'] || image.providerStatus || '-')}</small>
	                            {attempt['prompt'] ? <p>{String(attempt['prompt'])}</p> : null}
	                          </div>
	                        ))}
	                      </div>
	                    </details>
	                  </>
	                )}
	              </div>
            </article>
          ))}
        </div>
      </div>

	      {SHOW_DEBUG_INFO && firstImage && (
        <details className="debug-json">
          <summary>高级调试 JSON</summary>
          <pre>{JSON.stringify({
            taskPackage: firstImage.taskPackage,
            providerRequest: firstImage.providerRequest,
            providerResponse: firstImage.providerResponse,
          }, null, 2)}</pre>
        </details>
      )}
    </div>
  )
}

function SnapshotItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="snapshot-item">
      <small>{label}</small>
      <strong>{value || '-'}</strong>
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

function ShotPlanBoard({
  shotPlan,
  editable = false,
  saving = false,
  onSave,
}: {
  shotPlan: ShotPlan
  editable?: boolean
  saving?: boolean
  onSave?: (plan: ShotPlan['plan']) => void
}) {
  const [draft, setDraft] = useState<ShotPlan['plan']>(shotPlan.plan)
  const [isEditing, setIsEditing] = useState(false)

  useEffect(() => {
    setDraft(shotPlan.plan)
    setIsEditing(false)
  }, [shotPlan.id, shotPlan.updatedAt])

  const updateShot = (index: number, patch: Partial<Shot>) => {
    setDraft((current) => ({
      ...current,
      shots: current.shots.map((shot, shotIndex) => (shotIndex === index ? { ...shot, ...patch } : shot)),
    }))
  }

  const updateGenerationPlan = (patch: NonNullable<ShotPlan['plan']['generationPlan']>) => {
    setDraft((current) => ({
      ...current,
      generationPlan: { ...(current.generationPlan ?? {}), ...patch },
    }))
  }
  const plannerLog = parsePlannerLog(shotPlan.rawResponse)
  const generationPlan = draft.generationPlan ?? {}
  const canEdit = editable && isEditing

  return (
    <div className="shot-plan">
      <div className="plan-section-heading">
        <div>
          <h3>拍摄方案</h3>
          <span>服装识别、品牌调性、搭配与统一背景</span>
        </div>
        {!isEditing && editable && (
          <button className="secondary" onClick={() => setIsEditing(true)}>
            <Edit3 size={15} />编辑
          </button>
        )}
      </div>
      {canEdit ? (
        <div className="shooting-script">
          <PlanField label="货号" value={draft.sku} />
          <PlanField label="商品品类" value={draft.category ?? draft.garment?.category ?? ''} editing={canEdit} onChange={(value) => setDraft((current) => ({ ...current, category: value }))} />
          <PlanField label="服装摘要" value={draft.garment?.summary ?? ''} wide />
          <PlanField label="颜色" value={draft.garment?.color ?? ''} />
          <PlanField label="面料" value={draft.garment?.fabric ?? ''} />
          <PlanField label="关键结构" value={(draft.garment?.keyStructures ?? []).join('、')} />
          <PlanField label="主销售信息" value={generationPlan.salesInfo ?? ''} editing={canEdit} onChange={(value) => updateGenerationPlan({ salesInfo: value })} wide multiline />
          <PlanField label="Lily 调性" value={generationPlan.lilyTone ?? draft.brandStyle?.summary ?? ''} editing={canEdit} onChange={(value) => updateGenerationPlan({ lilyTone: value })} wide multiline />
          <PlanField label="服饰搭配" value={generationPlan.styling ?? ''} editing={canEdit} onChange={(value) => updateGenerationPlan({ styling: value })} wide multiline />
          <PlanField label="统一背景" value={generationPlan.background ?? ''} editing={canEdit} onChange={(value) => updateGenerationPlan({ background: value })} wide multiline />
        </div>
      ) : (
        <div className="plan-business-summary">
          <TaskSummary label="品类" value={draft.category ?? draft.garment?.category ?? '待确认'} />
          <TaskSummary label="服装" value={draft.garment?.summary ?? '待确认'} />
          <TaskSummary label="棚拍风格" value={generationPlan.background ?? draft.brandStyle?.summary ?? '白/浅灰棚拍'} />
          <TaskSummary label="搭配边界" value={generationPlan.styling ?? '简洁搭配，商品优先'} />
        </div>
      )}

      <div className="plan-section-heading">
        <div>
          <h3>分镜剧本</h3>
          <span>固定 5 张图位，逐张控制景别、身位和姿势</span>
        </div>
      </div>
      <div className={canEdit ? 'script-shot-list' : 'plan-shot-cards'}>
        {draft.shots.map((shot, index) => (
          <div key={shot.shotIndex} className={canEdit ? 'shot-editor' : 'plan-shot-card'}>
            {canEdit ? (
              <>
              <div className="shot-editor-title">
                <strong>图 {shot.shotIndex} {shot.title}</strong>
                <small>{shot.englishName}</small>
              </div>
              <label className="field">
                <span>标题</span>
                <input value={shot.title ?? ''} onChange={(event) => updateShot(index, { title: event.target.value })} />
              </label>
              <label className="field wide">
                <span>任务</span>
                <textarea value={shot.task ?? ''} onChange={(event) => updateShot(index, { task: event.target.value })} />
              </label>
              <label className="field">
                <span>景别</span>
                <input value={shot.scene ?? ''} onChange={(event) => updateShot(index, { scene: event.target.value })} />
              </label>
              <label className="field">
                <span>取景范围</span>
                <input value={shot.cropRange ?? ''} onChange={(event) => updateShot(index, { cropRange: event.target.value })} />
              </label>
              <label className="field">
                <span>模特占画幅</span>
                <input value={shot.modelScale ?? ''} onChange={(event) => updateShot(index, { modelScale: event.target.value })} />
              </label>
              <label className="field">
                <span>头部处理</span>
                <input value={shot.headTreatment ?? ''} onChange={(event) => updateShot(index, { headTreatment: event.target.value })} />
              </label>
              <label className="field">
                <span>身位</span>
                <input value={shot.bodyPosition ?? ''} onChange={(event) => updateShot(index, { bodyPosition: event.target.value })} />
              </label>
              <label className="field wide">
                <span>姿势调性</span>
                <textarea value={shot.poseTone ?? ''} onChange={(event) => updateShot(index, { poseTone: event.target.value })} />
              </label>
              <label className="field wide">
                <span>画面目的</span>
                <textarea value={shot.imagePurpose ?? ''} onChange={(event) => updateShot(index, { imagePurpose: event.target.value })} />
              </label>
              </>
            ) : (
              <>
                <b>{String(shot.shotIndex).padStart(2, '0')}</b>
                <strong>{shot.title}</strong>
                <span>{[shot.scene, shot.cropRange].filter(Boolean).join(' · ') || '默认棚拍图位'}</span>
                <small>{shot.imagePurpose || shot.task || '展示服装重点与品牌棚拍质感'}</small>
              </>
            )}
          </div>
        ))}
      </div>
      {!canEdit && (
        <details className="plan-advanced-details">
          <summary>查看图位详情</summary>
          <div className="script-shot-list">
            {draft.shots.map((shot) => (
              <div key={shot.shotIndex} className="shot-editor">
                <div className="shot-editor-title">
                  <strong>图 {shot.shotIndex} {shot.title}</strong>
                  <small>{shot.englishName}</small>
                </div>
                <PlanField label="任务" value={shot.task ?? ''} wide />
                <PlanField label="景别" value={shot.scene ?? ''} />
                <PlanField label="取景范围" value={shot.cropRange ?? ''} />
                <PlanField label="模特占画幅" value={shot.modelScale ?? ''} />
                <PlanField label="头部处理" value={shot.headTreatment ?? ''} />
                <PlanField label="身位" value={shot.bodyPosition ?? ''} />
                <PlanField label="姿势调性" value={shot.poseTone ?? ''} wide />
                <PlanField label="画面目的" value={shot.imagePurpose ?? ''} wide />
              </div>
            ))}
          </div>
        </details>
      )}
      {isEditing && (
        <div className="plan-save-row">
          <button className="secondary" onClick={() => { setDraft(shotPlan.plan); setIsEditing(false) }} disabled={saving}>取消</button>
          <button className="primary" onClick={() => { onSave?.(draft); setIsEditing(false) }} disabled={saving}>
            {saving ? <Loader2 className="spin" size={16} /> : <Save size={16} />}
            保存方案
          </button>
        </div>
      )}
	      {SHOW_DEBUG_INFO && (
	        <details>
	          <summary>策划器调用记录</summary>
	          <div className="planner-log">
	            <FactRow label="Planner" value={shotPlan.plannerProvider} />
	            <FactRow label="调用模型" value={plannerLog?.providerRequest?.model} />
	            <FactRow label="调用说明" value={plannerLog?.note} />
	            <label className="field">
	              <span>调用提示词</span>
	              <textarea readOnly value={plannerLog?.providerRequest?.prompt ?? ''} />
	            </label>
	            <label className="field">
	              <span>完整 raw_response</span>
	              <textarea readOnly value={shotPlan.rawResponse ?? ''} />
	            </label>
	          </div>
	        </details>
	      )}
    </div>
  )
}

function PlanField({
  label,
  value,
  wide = false,
  multiline = false,
  editing = false,
  onChange,
}: {
  label: string
  value?: string
  wide?: boolean
  multiline?: boolean
  editing?: boolean
  onChange?: (value: string) => void
}) {
  return (
    <label className={`script-field ${wide ? 'wide' : ''}`}>
      <span>{label}</span>
      {editing && onChange ? (
        multiline ? (
          <textarea value={value ?? ''} onChange={(event) => onChange(event.target.value)} />
        ) : (
          <input value={value ?? ''} onChange={(event) => onChange(event.target.value)} />
        )
      ) : (
        <strong>{value || '-'}</strong>
      )}
    </label>
  )
}

function ResultGallery({
  title = '生成结果',
  run,
  loading,
  nowMs,
  imageFilter,
  previewOnly = false,
  onRegenerate,
  onToggleFinal,
  onReviewStatus,
  onCancelImage,
  onExport,
}: {
  title?: string
  run: GenerationRun | null
  loading: boolean
  nowMs: number
  imageFilter?: (image: GenerationImage) => boolean
  previewOnly?: boolean
  onRegenerate: (image: GenerationImage, reason: string) => void
  onToggleFinal: (image: GenerationImage) => void
  onReviewStatus: (image: GenerationImage, status: string) => void
  onCancelImage: (image: GenerationImage) => void
  onExport: (runId: number) => void
}) {
  const isPreviewGallery = title?.includes('预览') ?? false
  const visibleImages = run?.images?.filter((image) => imageFilter ? imageFilter(image) : true) ?? []
  const placeholders = Array.from({ length: isPreviewGallery ? 1 : 5 }, (_, index) => index + 1)
  const runStartedAt = visibleImages[0]?.submittedAt ?? run?.createdAt
  const [regenerateReasons, setRegenerateReasons] = useState<Record<number, string>>({})
  const selectedReason = (image: GenerationImage) => regenerateReasons[image.id] ?? REGENERATION_REASONS[0].value
  return (
    <section className="results">
      <div className="section-heading">
        <h2>{title}</h2>
        {run && (
          <span>
            {isPreviewGallery ? '预览记录' : run.kind === 'regeneration' ? '重生记录' : '组图记录'} #{run.id} · {statusText(run.status)}
            {runStartedAt ? ` · 已运行 ${elapsedText(runStartedAt, nowMs, run.updatedAt, !isRunActive(run))}` : ''}
          </span>
        )}
      </div>
      <div className="result-grid">
        {visibleImages.length
          ? visibleImages.map((image) => {
              const imageActive = isImageActive(image)
              const imageElapsed = elapsedText(image.submittedAt ?? image.createdAt, nowMs, image.completedAt ?? image.updatedAt, !imageActive)
              const canShowImage = image.status === 'SUCCEEDED' && Boolean(image.url)
              const displayTitle = image.shotIndex === 0 ? '分镜预览' : image.title
              const displayLabel = `${String(image.shotIndex).padStart(2, '0')} ${displayTitle}`
              return (
              <article className="result-card" key={image.id}>
                <div className="result-image">
                  {canShowImage ? (
                    <ImagePreviewThumb
                      src={mediaUrl(image.url)}
                      name={displayLabel}
                      fileSize={image.fileSize}
                    >
                      {image.isFinal && <div className="final-badge"><Check size={14} />最终图</div>}
                    </ImagePreviewThumb>
                  ) : (
                    <div className="result-pending">
                      {imageActive && <Loader2 className="spin" size={22} />}
                      <strong>{statusText(image.status)}</strong>
                      <small>{providerStatusText(image.providerStatus)}</small>
                      {image.status === 'FAILED' && image.providerStatusResponse && (
                        <small>{providerErrorText(image.providerStatusResponse)}</small>
                      )}
                    </div>
                  )}
                </div>
                <div className="result-meta">
                  <strong title={displayLabel}>
                    {middleEllipsis(displayLabel)}
                  </strong>
	                  <small>{statusText(image.status)} · {imageElapsed} · 记录 #{image.id}</small>
	                  {SHOW_DEBUG_INFO && image.providerRequestId && <small>任务 #{shortId(image.providerRequestId)} · {providerStatusText(image.providerStatus)}</small>}
	                  {!previewOnly && <small>交付状态：{reviewStatusText(image.reviewStatus)}</small>}
	                </div>
	                <div className="result-actions">
                    {previewOnly ? (
                      <button onClick={() => onRegenerate(image, '重新生成分镜预览')} disabled={loading || imageActive}>
                        <RefreshCw size={15} />重新预览
                      </button>
                    ) : (
                      <>
                        {image.status === 'SUCCEEDED' && (
                          <select
                            value={selectedReason(image)}
                            onChange={(event) => setRegenerateReasons((current) => ({ ...current, [image.id]: event.target.value }))}
                          >
                            {REGENERATION_REASONS.map((reason) => (
                              <option key={reason.value} value={reason.value}>{reason.label}</option>
                            ))}
                          </select>
                        )}
                        <button onClick={() => onRegenerate(image, selectedReason(image))} disabled={loading || imageActive}>
                          <RefreshCw size={15} />重生
                        </button>
                        <button onClick={() => onToggleFinal(image)} disabled={image.status !== 'SUCCEEDED'}>
                          <Check size={15} />{image.isFinal ? '取消最终' : '设为最终'}
                        </button>
                      </>
                    )}
	                  {imageActive && (
	                    <button onClick={() => onCancelImage(image)}>
	                      <X size={15} />取消
	                    </button>
	                  )}
	                  {!previewOnly && image.status === 'SUCCEEDED' && (
	                    <select value={image.reviewStatus ?? 'candidate'} onChange={(event) => onReviewStatus(image, event.target.value)}>
	                      <option value="candidate">候选</option>
	                      <option value="approved">可用</option>
	                      <option value="needs_regen">需重生</option>
	                      <option value="needs_retouch">需精修</option>
	                      <option value="delivered">已交付</option>
	                    </select>
	                  )}
	                </div>
              </article>
              )
            })
          : placeholders.map((index) => (
              <article className="result-card empty" key={index}>
                <div className="result-image"><span>待生成</span></div>
                <div className="result-meta">
                  <strong>{String(index).padStart(2, '0')} 图位</strong>
                  <small>{isPreviewGallery ? '预览后用于最终生成' : '生成后可单张重生'}</small>
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

function parsePlannerLog(raw?: string) {
  if (!raw) return null
  try {
    return JSON.parse(raw) as {
      note?: string
      providerRequest?: {
        model?: string
        prompt?: string
      }
    }
  } catch {
    return null
  }
}

function EmptyText({ text }: { text: string }) {
  return <div className="empty-text">{text}</div>
}

function Tag({ children }: { children: ReactNode }) {
  return <span className="tag">{children}</span>
}

function ImagePreviewThumb({
  src,
  name,
  fileSize,
  compact,
  round,
  children,
}: {
  src: string
  name: string
  fileSize?: number | null
  compact?: boolean
  round?: boolean
  children?: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const [dimensions, setDimensions] = useState<ImageDimensions | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    if (!open) return undefined
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [open])

  useEffect(() => {
    setDimensions(null)
    setFailed(false)
  }, [src])

  const summary = imageSummary(dimensions, fileSize)
  const canOpen = !failed

  return (
    <>
      <div
        className={`image-preview-thumb ${compact ? 'compact' : ''} ${round ? 'round' : ''} ${failed ? 'failed' : ''}`}
        role="button"
        tabIndex={canOpen ? 0 : -1}
        title={failed ? `${name} · 图片加载失败` : `${name}${summary ? ` · ${summary}` : ''}`}
        onClick={(event) => {
          event.stopPropagation()
          if (!canOpen) return
          setOpen(true)
        }}
        onKeyDown={(event) => {
          if (canOpen && (event.key === 'Enter' || event.key === ' ')) {
            event.preventDefault()
            event.stopPropagation()
            setOpen(true)
          }
        }}
      >
        {failed ? (
          <span className="image-preview-fallback">图片加载失败</span>
        ) : (
          <img
            src={src}
            alt={name}
            onLoad={(event) => {
              const target = event.currentTarget
              setDimensions({ width: target.naturalWidth, height: target.naturalHeight })
            }}
            onError={() => {
              setOpen(false)
              setFailed(true)
            }}
          />
        )}
        {children}
      </div>

      {open && !failed && createPortal(
        <div
          className="preview-backdrop"
          role="dialog"
          aria-modal="true"
          onClick={() => setOpen(false)}
        >
          <div className="preview-dialog" onClick={(event) => event.stopPropagation()}>
            <button className="preview-close" onClick={() => setOpen(false)} title="关闭预览">
              <X size={18} />
            </button>
            <div className="preview-image-frame">
              <img src={src} alt={name} />
            </div>
            <div className="preview-info">
              <strong title={name}>{middleEllipsis(name, 42)}</strong>
              <span>{imageSummary(dimensions, fileSize) || '图片信息读取中'}</span>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </>
  )
}

function ImageInfoLine({ src, fileSize }: { src: string; fileSize?: number | null }) {
  const [dimensions, setDimensions] = useState<ImageDimensions | null>(null)

  useEffect(() => {
    let cancelled = false
    setDimensions(null)
    const image = new Image()
    image.onload = () => {
      if (!cancelled) setDimensions({ width: image.naturalWidth, height: image.naturalHeight })
    }
    image.src = src
    return () => {
      cancelled = true
    }
  }, [src])

  return <>{imageSummary(dimensions, fileSize) || '图片信息读取中'}</>
}

function statusText(status: string) {
  const map: Record<string, string> = {
    NEW: '待策划',
    ANALYZED: '已策划',
    CONFIRMED: '已确认',
    DRAFT: '待确认',
    PLANNING: '策划中',
    RUNNING: '生成中',
    QUEUED: '待提交',
    SUBMITTED: '已提交',
    IN_QUEUE: 'fal 排队中',
    IN_PROGRESS: 'fal 生成中',
    SUCCEEDED: '已生成',
    PARTIAL_SUCCEEDED: '部分完成',
    FAILED: '失败',
    CANCELLED: '已取消',
    CREATED: '已创建',
  }
  return map[status] ?? status
}

function reviewStatusText(status?: string | null) {
  const map: Record<string, string> = {
    candidate: '候选',
    approved: '可用',
    needs_regen: '需重生',
    needs_retouch: '需精修',
    delivered: '已交付',
  }
  return map[status || 'candidate'] ?? status ?? '候选'
}

function providerStatusText(status?: string | null) {
  if (!status) return '等待 fal 状态'
  const map: Record<string, string> = {
    SUBMITTED: '已提交 fal',
    IN_QUEUE: 'fal 队列中',
    IN_PROGRESS: 'fal 生成中',
    COMPLETED: 'fal 已完成',
    FAILED: 'fal 失败',
    ERROR: 'fal 错误',
    CANCELED: 'fal 已取消',
    CANCELLED: 'fal 已取消',
    STATUS_CHECK_FAILED: '状态查询失败',
  }
  return map[status] ?? status
}

function providerErrorText(value?: Record<string, unknown> | null) {
  const error = value?.['error']
  if (!error) return ''
  const text = typeof error === 'string' ? error : JSON.stringify(error)
  return text.length > 120 ? `${text.slice(0, 120)}...` : text
}

function snapshotText(snapshot: Record<string, unknown> | null | undefined, keys: string[]) {
  if (!snapshot) return '-'
  const values = keys
    .map((key) => snapshot[key])
    .filter((value) => value !== undefined && value !== null && String(value).trim())
    .map((value) => Array.isArray(value) ? value.join('、') : String(value))
  return values.length ? values.join(' · ') : '-'
}

function isRunActive(run: GenerationRun) {
  return run.status === 'RUNNING' || Boolean(run.images?.some(isImageActive))
}

function skuHasLinkedHistory(sku: GarmentSku) {
  const impact = sku.deleteImpact
  return Boolean((impact?.generationRunCount ?? 0) > 0 || (impact?.shotPlanCount ?? 0) > 0)
}

function isImageActive(image: GenerationImage) {
  return ['QUEUED', 'SUBMITTED', 'IN_QUEUE', 'IN_PROGRESS', 'RUNNING'].includes(image.status)
}

function getGenerateTaskStage(
  selectedSku: GarmentSku | null,
  shotPlan: ShotPlan | null,
  previewRun: GenerationRun | null,
  generationRun: GenerationRun | null,
) {
  if (!selectedSku) {
    return {
      index: 0,
      title: '先选择本次要拍摄的服装',
      description: '选择已保存货号，或进入服装素材页导入图包。系统会围绕这个货号组织整套拍摄任务。',
      tag: '待开始',
      actionTitle: '选择服装',
      actionText: '左侧选择货号后，这里会显示默认方案、分镜和最终生成动作。',
    }
  }
  if (!shotPlan) {
    return {
      index: 1,
      title: '缺少默认拍摄方案',
      description: '生成组图页只执行已保存方案。请回到服装素材页，让系统为这个货号生成默认 5 张图位方案。',
      tag: '需保存方案',
      actionTitle: '保存默认方案',
      actionText: '进入服装素材页生成默认方案后，再回到这里预览分镜。',
    }
  }
  if (shotPlan.status === 'PLANNING') {
    return {
      index: 1,
      title: '正在整理默认拍摄方案',
      description: '系统正在识别服装品类、整理品牌棚拍风格和 5 张图位。完成后即可生成分镜预览。',
      tag: '策划中',
      actionTitle: '等待方案完成',
      actionText: '方案完成后，“生成分镜预览”会自动可用。',
    }
  }
  if (!previewRun) {
    return {
      index: 2,
      title: '先生成 2x3 分镜预览',
      description: '分镜用于确认姿势方向、图位结构和整体棚拍语言，满意后再生成最终大图。',
      tag: '待预览',
      actionTitle: '生成分镜预览',
      actionText: '先看一张 2x3 contact sheet，避免直接生成整组后才发现方向不对。',
    }
  }
  if (isRunActive(previewRun)) {
    return {
      index: 2,
      title: '分镜预览生成中',
      description: '后台会持续刷新 fal 状态。完成后即可进入最终 5 张成片生成。',
      tag: '预览中',
      actionTitle: '等待分镜完成',
      actionText: '分镜完成前不需要离开页面，刷新后也可以恢复任务。',
    }
  }
  if (previewRun.status !== 'SUCCEEDED') {
    return {
      index: 2,
      title: '分镜预览不可用',
      description: '当前分镜未完成或已经失败。请重新生成分镜预览，再进入最终组图。',
      tag: statusText(previewRun.status),
      actionTitle: '重新预览',
      actionText: '重新生成分镜后，系统会把它绑定到当前货号和默认方案。',
    }
  }
  if (!generationRun) {
    return {
      index: 3,
      title: '分镜已就绪，可以生成最终图',
      description: '最终生成会在同一任务下追加 5 张大图，之后可单张保留、重生和导出。',
      tag: '可生成',
      actionTitle: '生成最终组图',
      actionText: '确认分镜方向后，开始生成最终 5 张电商主图。',
    }
  }
  if (isRunActive(generationRun)) {
    return {
      index: 3,
      title: '最终组图生成中',
      description: '5 张图会逐张回传状态。失败或不满意的图位后续可单张重生。',
      tag: '生成中',
      actionTitle: '等待最终图',
      actionText: '生成完成后，在结果卡上标记最终图或单张重生。',
    }
  }
  return {
    index: 4,
    title: generationRun.status === 'SUCCEEDED' ? '最终组图已生成' : '最终组图需要处理',
    description: '检查每张图的商品保真、姿势和构图。可用图设为最终图，不满意的图位直接重生。',
    tag: statusText(generationRun.status),
    actionTitle: '确认并导出',
    actionText: '标记最终图后即可导出 ZIP，未达标图位可以保留在同一任务里重生。',
  }
}

function elapsedText(startIso?: string | null, nowMs = Date.now(), endIso?: string | null, useEnd = false) {
  if (!startIso) return '00:00'
  const start = Date.parse(startIso)
  if (!Number.isFinite(start)) return '00:00'
  const end = useEnd && endIso ? Date.parse(endIso) : nowMs
  const seconds = Math.max(0, Math.floor(((Number.isFinite(end) ? end : nowMs) - start) / 1000))
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const remainSeconds = seconds % 60
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, '0')}:${String(remainSeconds).padStart(2, '0')}`
  return `${String(minutes).padStart(2, '0')}:${String(remainSeconds).padStart(2, '0')}`
}

function shortId(value: string) {
  return value.length <= 12 ? value : `${value.slice(0, 6)}…${value.slice(-4)}`
}

function importStatusText(status: ImportStatus) {
  const map: Record<ImportStatus, string> = {
    draft: '待上传',
    uploading: '上传中',
    uploaded: '已上传',
    failed: '上传失败',
  }
  return map[status]
}

function createImportPackage(sku: string, sourceFiles: File[], sourceName: string): ImportPackage {
  const images = sourceFiles
    .filter((file) => file.type.startsWith('image/'))
    .map((file) => {
      const relativePath = getRelativePath(file)
      return {
        id: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
        file,
        previewUrl: URL.createObjectURL(file),
        relativePath,
      }
    })
  return {
    id: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
    sourceName,
    sku: sanitizeClientSku(sku),
    title: sanitizeClientSku(sku),
    images,
    status: 'draft',
  }
}

function groupFilesByTopFolder(files: File[]) {
  return files.reduce<Record<string, File[]>>((groups, file) => {
    const relativePath = getRelativePath(file)
    const folderName = relativePath.includes('/') ? relativePath.split('/')[0] : '未命名文件夹'
    groups[folderName] = groups[folderName] ?? []
    groups[folderName].push(file)
    return groups
  }, {})
}

function getRelativePath(file: File) {
  return ((file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name)
}

function sanitizeClientSku(value: string) {
  return value.trim().replace(/\.[^.]+$/, '').replace(/[^\p{L}\p{N}_-]/gu, '-').replace(/^-+|-+$/g, '') || '未命名货号'
}

function findDuplicateSkus(packages: ImportPackage[]) {
  const counts = packages.reduce<Record<string, number>>((acc, item) => {
    const sku = item.sku.trim()
    if (sku) acc[sku] = (acc[sku] ?? 0) + 1
    return acc
  }, {})
  return new Set(Object.entries(counts).filter(([, count]) => count > 1).map(([sku]) => sku))
}

const HEAD_REFERENCE_OPTIONS = [
  { value: 'front', label: '正脸' },
  { value: 'left45', label: '左 45 度' },
  { value: 'right45', label: '右 45 度' },
  { value: 'right90', label: '右侧 90 度' },
  { value: 'left90', label: '左侧 90 度' },
  { value: 'up45', label: '抬头' },
  { value: 'down45', label: '低头' },
  { value: 'back', label: '背面' },
  { value: 'backTurnLeft45', label: '背面回头左 45 度' },
  { value: 'backTurnRight45', label: '背面回头右 45 度' },
]

const REGENERATION_REASONS = [
  { value: '服装不准确', label: '服装不准确' },
  { value: '姿势不合适', label: '姿势不合适' },
  { value: '模特不像', label: '模特不像' },
  { value: '构图不合适', label: '构图不合适' },
  { value: '搭配不合适', label: '搭配不合适' },
  { value: '其他补充', label: '其他补充' },
]

function createBlankModelForm(index = 1): ModelForm {
  const suffix = String(index).padStart(2, '0')
  return {
    modelId: `Face-${suffix}`,
    name: `Face-${suffix} 数字模特`,
    isDefault: false,
    height: '170cm',
    weight: '',
    bodyShape: 'H型',
    bust: '80cm',
    waist: '59cm',
    hips: '89cm',
    notes: '都市、通勤、轻熟、自然、克制',
  }
}

function modelToForm(model: ModelProfile): ModelForm {
  const profile = model.profile
  return {
    modelId: model.modelId,
    name: model.name,
    isDefault: model.isDefault,
    height: profileString(profile, 'height'),
    weight: profileString(profile, 'weight'),
    bodyShape: profileString(profile, 'bodyShape'),
    bust: profileString(profile, 'bust'),
    waist: profileString(profile, 'waist'),
    hips: profileString(profile, 'hips'),
    notes: profileString(profile, 'notes') || [profileString(profile, 'temperament'), profileString(profile, 'expression')].filter(Boolean).join('、'),
  }
}

function modelFormToProfile(form: ModelForm, existing?: ModelProfile): Record<string, unknown> {
  const {
    bodyType,
    shoulder,
    legRatio,
    skinTone,
    hair,
    expression,
    temperament,
    ...existingProfile
  } = existing?.profile ?? {}
  return {
    ...existingProfile,
    height: form.height.trim(),
    weight: form.weight.trim(),
    bodyShape: form.bodyShape.trim(),
    bust: form.bust.trim(),
    waist: form.waist.trim(),
    hips: form.hips.trim(),
    notes: form.notes.trim(),
  }
}

function profileString(profile: Record<string, unknown>, key: string) {
  const value = profile[key]
  return typeof value === 'string' ? value : ''
}

function getSavedHeadEntries(model?: ModelProfile): ModelHeadEntry[] {
  const entries = model?.profile.headReferenceEntries
  if (!entries || !Array.isArray(entries)) return []
  return entries
    .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null && !Array.isArray(item))
    .map((item) => ({
      id: typeof item.id === 'string' ? item.id : '',
      angle: typeof item.angle === 'string' ? item.angle : '',
      filename: typeof item.filename === 'string' ? item.filename : '',
      url: typeof item.url === 'string' ? mediaUrl(item.url) : '',
      fileSize: typeof item.fileSize === 'number' ? item.fileSize : undefined,
    }))
    .filter((item) => item.id)
}

function getDraftHeadEntries(drafts: ModelHeadDraft[]): ModelHeadEntry[] {
  return drafts.map((item) => ({
    id: item.id,
    angle: item.angle,
    filename: item.file.name,
    url: item.previewUrl,
    fileSize: item.file.size,
  }))
}

function headReferenceLabel(angle: string) {
  return HEAD_REFERENCE_OPTIONS.find((option) => option.value === angle)?.label ?? angle
}

function modelInitials(model: ModelProfile) {
  return model.modelId.replace(/[^A-Za-z0-9]/g, '').slice(0, 3).toUpperCase() || 'MOD'
}

function modelSummary(model?: ModelProfile) {
  if (!model) return ''
  const profile = model.profile
  return [
    model.isDefault ? '默认模特' : '备用模特',
    profileString(profile, 'height'),
    profileString(profile, 'weight'),
    profileString(profile, 'bodyShape'),
    profileString(profile, 'notes') || profileString(profile, 'temperament'),
  ].filter(Boolean).join(' / ')
}

function modelPreviewFileSize(model: ModelProfile) {
  const entries = getSavedHeadEntries(model)
  return entries.find((item) => item.angle === 'front')?.fileSize ?? entries[0]?.fileSize
}

function imageSummary(dimensions: ImageDimensions | null, fileSize?: number | null) {
  const parts = [
    dimensions ? `${dimensions.width}x${dimensions.height}` : undefined,
    formatFileSize(fileSize),
  ].filter(Boolean)
  return parts.join(' · ')
}

function formatFileSize(bytes?: number | null) {
  if (!bytes && bytes !== 0) return ''
  if (bytes < 1024) return `${bytes} B`
  const kb = bytes / 1024
  if (kb < 1024) return `${kb >= 100 ? Math.round(kb) : kb.toFixed(1)} KB`
  const mb = kb / 1024
  return `${mb >= 100 ? Math.round(mb) : mb.toFixed(1)} MB`
}

function middleEllipsis(value: string, maxLength = 28) {
  if (value.length <= maxLength) return value
  const extensionMatch = value.match(/(\.[^.]{1,8})$/)
  const extension = extensionMatch?.[1] ?? ''
  const text = extension ? value.slice(0, -extension.length) : value
  const available = Math.max(maxLength - extension.length - 3, 8)
  const head = Math.ceil(available * 0.58)
  const tail = Math.floor(available * 0.42)
  return `${text.slice(0, head)}...${text.slice(Math.max(text.length - tail, head))}${extension}`
}

function routeKey(route: AppRoute) {
  return routeToUrl(route)
}

function pageTitle(page: Page) {
  const map: Record<Page, string> = {
    generate: '生成组图',
    garments: '服装素材',
    models: '数字模特',
    history: '生成记录',
  }
  return map[page]
}

function pageSubtitle(page: Page) {
  const map: Record<Page, string> = {
    generate: '选择服装货号，直接使用默认组图方案生成电商主图',
    garments: '上传、管理和使用服装货号图包',
    models: '维护数字模特身份、角度和身体参数',
    history: '查看生成、重生、导出和系统事件',
  }
  return map[page]
}

function mergeRun(current: GenerationRun | null, next: GenerationRun): GenerationRun {
  if (!current?.images) return next
  return next
}
