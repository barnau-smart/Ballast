import { describe, it, expect, vi, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import { renderHook } from '@testing-library/react'
import { Wordmark } from '../components/Wordmark.jsx'
import { Cursor } from '../components/Cursor.jsx'
import { useReducedMotion } from '../hooks/useReducedMotion.js'

/**
 * Reduced-motion approach (documented in README):
 * We drive reduced motion with a `useReducedMotion` hook backed by
 * window.matchMedia('(prefers-reduced-motion: reduce)'). When it returns
 * true, Wordmark and Cursor add a `--static` class that sets
 * `animation: none`. A global CSS media query is the belt-and-suspenders
 * fallback. Here we override matchMedia to assert the static variant.
 */
function mockReducedMotion(reduce) {
  window.matchMedia = vi.fn().mockImplementation((query) => ({
    matches: reduce,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }))
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('reduced motion', () => {
  it('useReducedMotion returns true when the media query matches', () => {
    mockReducedMotion(true)
    const { result } = renderHook(() => useReducedMotion())
    expect(result.current).toBe(true)
  })

  it('useReducedMotion returns false when motion is allowed', () => {
    mockReducedMotion(false)
    const { result } = renderHook(() => useReducedMotion())
    expect(result.current).toBe(false)
  })

  it('Wordmark renders the static (animation-disabled) variant under reduced motion', () => {
    mockReducedMotion(true)
    const { container } = render(<Wordmark />)
    const el = container.querySelector('.ballast-wordmark')
    expect(el).toHaveClass('ballast-wordmark--static')
    expect(el).toHaveAttribute('data-reduced-motion', 'true')
  })

  it('Cursor renders the static (blink-disabled) variant under reduced motion', () => {
    mockReducedMotion(true)
    const { container } = render(<Cursor />)
    const el = container.querySelector('.ballast-cursor')
    expect(el).toHaveClass('ballast-cursor--static')
    expect(el).toHaveAttribute('data-reduced-motion', 'true')
  })

  it('Wordmark animates (no static class) when motion is allowed', () => {
    mockReducedMotion(false)
    const { container } = render(<Wordmark />)
    const el = container.querySelector('.ballast-wordmark')
    expect(el).not.toHaveClass('ballast-wordmark--static')
  })
})
