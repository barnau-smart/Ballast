import { useReducedMotion } from '../hooks/useReducedMotion.js'
import './Cursor.css'

/**
 * The terminal signature: a blinking green (phosphor) block.
 * Blink is disabled (static) when the user prefers reduced motion —
 * both via the hook (adds --static class) and via a global CSS media query.
 */
export function Cursor() {
  const reduced = useReducedMotion()
  const className = reduced
    ? 'ballast-cursor ballast-cursor--static'
    : 'ballast-cursor'
  return (
    <span
      className={className}
      role="presentation"
      aria-hidden="true"
      data-reduced-motion={reduced}
    />
  )
}
