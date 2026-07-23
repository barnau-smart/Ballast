import { useReducedMotion } from '../hooks/useReducedMotion.js'
import './Wordmark.css'

/**
 * The red serif BALLAST wordmark with a gentle flicker.
 * Flicker is disabled (static) when the user prefers reduced motion —
 * both via the hook (adds --static class) and via a global CSS media query.
 */
export function Wordmark() {
  const reduced = useReducedMotion()
  const className = reduced
    ? 'ballast-wordmark ballast-wordmark--static'
    : 'ballast-wordmark'
  return (
    <span className={className} data-reduced-motion={reduced}>
      BALLAST
    </span>
  )
}
