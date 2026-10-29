/* @vitest-environment jsdom */
import '@testing-library/jest-dom/vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { RecordImagePreview } from '../App'
import { api, type GenerationTask } from '../api'

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api')
  return { ...actual, api: { ...actual.api, retryResultImage: vi.fn(), getRecord: vi.fn(), regenerateResultSet: vi.fn(), continueResultSet: vi.fn() } }
})
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals() })

function task(): GenerationTask {
  const asset = (id: number) => ({ id, objectKey: `output-${id}.png`, url: `/output-${id}.png`, thumbUrl: `/thumb-${id}.png`, originalFilename: `output-${id}.png` })
  return { id: 10, status: 'COMPLETED', resultSet: null, resultSets: [
    { id: 20, status: 'PARTIAL', images: [{ id: 200, sortOrder: 1, status: 'FAILED', asset: asset(2), versions: [
      { id: 102, asset: asset(2), isCurrent: true, canRegenerate: true },
      { id: 101, asset: asset(1), isCurrent: false, canRegenerate: true },
    ] }] },
    { id: 19, status: 'SUPERSEDED', images: [{ id: 190, sortOrder: 1, status: 'SUCCEEDED', asset: asset(3), versions: [
      { id: 100, asset: asset(3), isCurrent: true, canRegenerate: false },
    ] }] },
  ] } as unknown as GenerationTask
}

it('shows all rounds without an active result and retains the successful image after failure', () => {
  vi.stubGlobal('IntersectionObserver', undefined)
  render(<RecordImagePreview task={task()} />)
  expect(screen.getByRole('region', { name: '第 2 次成片' })).toBeInTheDocument()
  expect(screen.getByRole('region', { name: '第 1 次成片' })).toBeInTheDocument()
  expect(screen.getByText('本次生成失败，已保留成功图片')).toBeInTheDocument()
  expect(screen.getAllByRole('img', { hidden: true })).toHaveLength(2)
  const old = screen.getByRole('region', { name: '第 1 次成片' })
  expect(within(old).getByRole('button', { name: '重新生成' })).toBeDisabled()
  expect(within(old).getByRole('link', { name: '下载' })).toBeInTheDocument()
})

it('retries the selected image version and refreshes the unified record', async () => {
  const item = task()
  const changed = vi.fn()
  vi.mocked(api.getRecord).mockResolvedValue({ item })
  render(<RecordImagePreview task={item} onChange={changed} />)
  fireEvent.change(screen.getByRole('combobox'), { target: { value: '1' } })
  const group = screen.getByRole('region', { name: '第 2 次成片' })
  fireEvent.click(within(group).getByRole('button', { name: '重新生成' }))
  await waitFor(() => expect(api.retryResultImage).toHaveBeenCalledWith(200, undefined, 101))
  await waitFor(() => expect(changed).toHaveBeenCalledWith(item))
  expect(api.regenerateResultSet).not.toHaveBeenCalled()
})

it('continues the selected round and scopes its download', async () => {
  const item = task()
  vi.mocked(api.getRecord).mockResolvedValue({ item })
  render(<RecordImagePreview task={item} onChange={vi.fn()} />)
  const group = screen.getByRole('region', { name: '第 2 次成片' })
  expect(within(group).getByRole('link', { name: '下载本组成片' }).getAttribute('href')).toContain('resultSetId=20')
  fireEvent.click(within(group).getByRole('button', { name: '继续生成' }))
  await waitFor(() => expect(api.continueResultSet).toHaveBeenCalledWith(20))
})
