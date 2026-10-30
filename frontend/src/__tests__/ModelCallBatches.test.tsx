/* @vitest-environment jsdom */
import '@testing-library/jest-dom/vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import { ModelCallBatches } from '../components/ModelCallBatches'

afterEach(cleanup)

it('pairs each batch with its own output and renders source prompt as readable text', () => {
  render(<ModelCallBatches batches={[
    { batchIndex: 1, shotIds: ['11'], systemInstruction: '系统规则', prompt: JSON.stringify({ shots: [{ shotId: '11', sourcePrompt: '第一行\n第二行' }] }), hasResponse: true, output: '第一批响应' },
    { batchIndex: 2, shotIds: ['12'], systemInstruction: '系统规则', prompt: '第二批输入', hasResponse: false },
  ]} />)
  const first = screen.getByText('第 1 批 · 1 个镜头').closest('section')!
  const second = screen.getByText('第 2 批 · 1 个镜头').closest('section')!
  expect(within(first).getByText('第一行 第二行')).toHaveTextContent('第一行 第二行')
  expect(within(first).getByText('第一批响应')).toBeInTheDocument()
  expect(within(second).queryByText('第一批响应')).not.toBeInTheDocument()
  expect(within(second).getByText('本批未记录到模型响应。')).toBeInTheDocument()
  expect(screen.getAllByText('系统规则')).toHaveLength(1)
})

it('preview never presents a generated response', () => {
  render(<ModelCallBatches preview batches={[{ batchIndex: 1, shotIds: ['1'], prompt: '维护者自定义消息' }]} />)
  expect(screen.getByText('渲染后的用户消息')).toBeInTheDocument()
  expect(screen.queryByText(/实际模型输出/)).not.toBeInTheDocument()
})
