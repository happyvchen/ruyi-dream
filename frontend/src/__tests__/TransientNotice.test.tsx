/* @vitest-environment jsdom */
import '@testing-library/jest-dom/vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { TransientNotice } from '../components/TransientNotice'

beforeEach(() => vi.useFakeTimers())
afterEach(() => { cleanup(); vi.useRealTimers() })

it.each([['warning', 8, 'alert'], ['info', 5, 'status']] as const)('counts down and dismisses %s notices', (tone, seconds, role) => {
  render(<TransientNotice message="操作提示" tone={tone} />)
  expect(screen.getByText(`${seconds} 秒后自动关闭`)).toBeInTheDocument()
  act(() => vi.advanceTimersByTime(1000))
  expect(screen.getByText(`${seconds - 1} 秒后自动关闭`)).toBeInTheDocument()
  act(() => vi.advanceTimersByTime((seconds - 1) * 1000))
  expect(screen.queryByRole(role)).not.toBeInTheDocument()
  expect(vi.getTimerCount()).toBe(0)
})

it.each([['warning', '关闭告警'], ['info', '关闭提示']] as const)('dismisses %s manually and clears its timer', (tone, label) => {
  render(<TransientNotice message="操作提示" tone={tone} />)
  fireEvent.click(screen.getByRole('button', { name: label }))
  expect(screen.queryByText('操作提示')).not.toBeInTheDocument()
  expect(vi.getTimerCount()).toBe(0)
})

it('pauses while reading or focusing the close button and resumes after both end', () => {
  const { container } = render(<TransientNotice message="告警" tone="warning" />)
  act(() => vi.advanceTimersByTime(2000))
  fireEvent.mouseEnter(container.firstChild!)
  fireEvent.focus(screen.getByRole('button', { name: '关闭告警' }))
  act(() => vi.advanceTimersByTime(10000))
  fireEvent.mouseLeave(container.firstChild!)
  act(() => vi.advanceTimersByTime(10000))
  expect(screen.getByRole('alert')).toHaveTextContent('告警')
  fireEvent.blur(screen.getByRole('button', { name: '关闭告警' }))
  expect(screen.getByText('6 秒后自动关闭')).toBeInTheDocument()
  act(() => vi.advanceTimersByTime(6000))
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

it('gives a repeated message a fresh countdown without keeping the old timer', () => {
  const { rerender, unmount } = render(<TransientNotice key={1} message="相同告警" tone="warning" />)
  act(() => vi.advanceTimersByTime(7000))
  rerender(<TransientNotice key={2} message="相同告警" tone="warning" />)
  act(() => vi.advanceTimersByTime(1000))
  expect(screen.getByText('7 秒后自动关闭')).toBeInTheDocument()
  expect(screen.getByRole('alert')).toBeInTheDocument()
  unmount()
  expect(vi.getTimerCount()).toBe(0)
})
