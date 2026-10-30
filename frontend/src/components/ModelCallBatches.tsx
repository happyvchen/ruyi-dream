import type { ModelCallBatch } from '../api'

function PromptBody({ text }: { text: string }) {
  // Render JSON text fields as text, avoiding another layer of escaped JSON.
  let body: { shots?: { shotId: string; shotIndex?: string; sourcePrompt: string; references?: unknown }[]; imageManifest?: unknown } | null = null
  try { body = JSON.parse(text) } catch { /* A maintained template may be plain text. */ }
  if (!body || !Array.isArray(body.shots) || !body.shots.every(shot => typeof shot?.sourcePrompt === 'string')) {
    return <pre className="prompt-comparison-text">{text}</pre>
  }
  return <>
    {body.shots.map((shot, index) => <details key={`${shot.shotId}-${index}`} open={body.shots?.length === 1}>
      <summary>镜头 {shot.shotIndex ?? shot.shotId} · 记录 {shot.shotId}</summary>
      <pre className="prompt-comparison-text">{shot.sourcePrompt}</pre>
      <details><summary>参考图编号映射</summary><pre className="prompt-comparison-text">{JSON.stringify(shot.references, null, 2)}</pre></details>
    </details>)}
    <details><summary>完整用户消息（原文）</summary><pre className="prompt-comparison-text">{text}</pre></details>
  </>
}

export function ModelCallBatches({ batches, preview = false }: { batches: ModelCallBatch[]; preview?: boolean }) {
  return <div className="model-call-batches">
    {batches[0]?.systemInstruction && <details>
      <summary>系统消息{batches.length > 1 ? ' · 每批发送' : ''} · {batches[0].systemInstruction.length.toLocaleString()} 字符</summary>
      <pre className="prompt-comparison-text">{batches[0].systemInstruction}</pre>
    </details>}
    {batches.map(batch => <section className="model-call-batch" key={batch.batchIndex}>
      <h4>{batches.length === 1 ? '单次调用' : `第 ${batch.batchIndex} 批`} · {batch.shotIds.length} 个镜头</h4>
      <p className="edit-hint">用户消息 {batch.prompt.length.toLocaleString()} 字符{batch.images?.length ? ` · ${batch.images.length} 张参考图` : ''}</p>
      <h4>{preview ? '渲染后的用户消息' : '实际模型输入 · 用户消息'}</h4>
      <PromptBody text={batch.prompt} />
      {!!batch.images?.length && <details><summary>图片附件顺序与来源</summary>
        <pre className="prompt-comparison-text">{JSON.stringify(batch.images, null, 2)}</pre>
      </details>}
      {!preview && <details open={batches.length === 1}>
        <summary>实际模型输出{batches.length > 1 ? ` · 第 ${batch.batchIndex} 批` : ''}</summary>
        {batch.hasResponse ? <pre className="prompt-comparison-text">{batch.output}</pre> : <p className="edit-hint">本批未记录到模型响应。</p>}
      </details>}
    </section>)}
  </div>
}
