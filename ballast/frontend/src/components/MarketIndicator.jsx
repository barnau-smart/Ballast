import './MarketIndicator.css'

/**
 * Direction indicator that never relies on color alone:
 * up = green ▲, down = sky-blue ▼, always paired with a sign + label.
 */
export function MarketIndicator({ direction = 'up', label }) {
  const isUp = direction === 'up'
  const glyph = isUp ? '▲' : '▼'
  const sign = isUp ? '+' : '−'
  const modifier = isUp
    ? 'ballast-market-indicator--up'
    : 'ballast-market-indicator--down'
  return (
    <span className={`ballast-market-indicator ${modifier}`}>
      <span aria-hidden="true">{glyph}</span>
      <span>{sign}</span>
      {label ? (
        <span className="ballast-market-indicator__label">{label}</span>
      ) : null}
    </span>
  )
}
