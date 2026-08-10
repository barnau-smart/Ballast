import { Link } from 'react-router-dom'
import { MarketIndicator } from './MarketIndicator.jsx'
import {
  CORE_EXPLAINER,
  NON_CORE_EXPLAINER,
  PARKED_EXPLAINER,
  describeHolding,
  formatCurrency,
  gainDirection,
  holdingsValue,
  partitionByCash,
  totalValue,
} from '../lib/holdings.js'
import './PortfolioPanel.css'

/**
 * PortfolioPanel (Story 2.4) — the plain-English portfolio.
 *
 * Renders the Story 2.3 cache (`GET /api/portfolio`) so a beginner understands
 * what they hold: each holding gets a warm, jargon-free description (NFR6), and
 * balances/cash are shown as formatted currency. Presentation-only (AD-1).
 *
 * States:
 * - loading: a calm "reading your portfolio" note.
 * - empty (no holdings / never imported): a calm invite to connect Schwab — a
 *   dead end is never shown.
 * - populated: total + cash summary, then the holdings.
 *
 * In degraded mode (expired session) the cached holdings still render — the
 * reauth-banner (Story 2.2) owns the reconnect prompt, not this panel. Hard
 * color rule: a down holding is sky-blue ▼, NEVER red/pink.
 */
export function PortfolioPanel({ status, portfolio }) {
  if (status === 'loading') {
    return (
      <div className="ballast-card" data-testid="portfolio-loading">
        reading your portfolio…
      </div>
    )
  }

  const holdings = portfolio?.holdings ?? []

  if (holdings.length === 0) {
    return (
      <div className="ballast-portfolio__empty" data-testid="portfolio-empty">
        <h2 className="ballast-portfolio__empty-title">
          Let’s bring your portfolio in
        </h2>
        <p className="ballast-screen__prose">
          Connect your Schwab account and Ballast will pull in your current
          holdings — just to read them, never to move your money without you.
          Then you’ll see everything you own, explained in plain English.
        </p>
        <Link className="ballast-portfolio__cta" to="/onboarding">
          Connect Schwab
        </Link>
      </div>
    )
  }

  const total = totalValue(holdings, portfolio.cash)
  const { parked, core, rest } = partitionByCash(holdings)

  // The honest three-state cash split (Story 9.1). Fall back gracefully when the
  // payload predates the additive `cash_states` block (degraded / empty state).
  const cashStates = portfolio.cash_states ?? {}
  const readyToTrade = cashStates.ready_to_trade ?? portfolio.cash
  const parkedValue = holdingsValue(parked)
  const reserveDecided = Boolean(cashStates.reserve_decided)
  const reserved = cashStates.reserved

  return (
    <div data-testid="portfolio-panel">
      <div className="ballast-portfolio__summary">
        <div className="ballast-portfolio__stat">
          <span className="ballast-portfolio__stat-label">Everything you hold</span>
          <span className="ballast-portfolio__stat-value" data-testid="portfolio-total">
            {formatCurrency(total)}
          </span>
        </div>
        <div className="ballast-portfolio__stat">
          <span className="ballast-portfolio__stat-label">Ready to trade</span>
          <span className="ballast-portfolio__stat-value" data-testid="portfolio-cash">
            {formatCurrency(readyToTrade)}
          </span>
        </div>
        {parked.length > 0 ? (
          <div className="ballast-portfolio__stat">
            <span className="ballast-portfolio__stat-label">Parked cash</span>
            <span className="ballast-portfolio__stat-value" data-testid="portfolio-parked">
              {formatCurrency(parkedValue)}
            </span>
          </div>
        ) : null}
        {reserveDecided && reserved != null ? (
          <div className="ballast-portfolio__stat">
            <span className="ballast-portfolio__stat-label">Set aside (reserve)</span>
            <span className="ballast-portfolio__stat-value" data-testid="portfolio-reserved">
              {formatCurrency(reserved)}
            </span>
          </div>
        ) : null}
      </div>

      {parked.length > 0 ? (
        <HoldingGroup
          testid="portfolio-group-parked"
          title="Parked cash (money market)"
          explainer={PARKED_EXPLAINER}
          holdings={parked}
        />
      ) : null}

      {core.length > 0 ? (
        <HoldingGroup
          testid="portfolio-group-core"
          title="Your index core"
          explainer={CORE_EXPLAINER}
          holdings={core}
        />
      ) : null}

      {rest.length > 0 ? (
        <HoldingGroup
          testid="portfolio-group-rest"
          title="The rest"
          explainer={NON_CORE_EXPLAINER}
          holdings={rest}
        />
      ) : null}
    </div>
  )
}

/**
 * One labelled group of holdings ("Your index core" / "The rest", Story 2.5)
 * with a plain explainer and a value subtotal so the strategy reads at a glance.
 * Neutral styling throughout — "the rest" is NOT rendered as a loss/error, so
 * NEVER red/pink (hard color rule).
 */
function HoldingGroup({ testid, title, explainer, holdings }) {
  return (
    <section className="ballast-portfolio__group" data-testid={testid}>
      <div className="ballast-portfolio__group-head">
        <h2 className="ballast-portfolio__group-title">{title}</h2>
        <span className="ballast-portfolio__group-total" data-testid={`${testid}-total`}>
          {formatCurrency(holdingsValue(holdings))}
        </span>
      </div>
      <p className="ballast-portfolio__desc">{explainer}</p>
      <ul className="ballast-portfolio__list">
        {holdings.map((holding) => {
          const direction = gainDirection(holding)
          return (
            <li
              key={holding.symbol}
              className="ballast-portfolio__holding"
              data-testid={`holding-${holding.symbol}`}
            >
              <div className="ballast-portfolio__holding-head">
                <span className="ballast-portfolio__symbol">{holding.symbol}</span>
                <span className="ballast-portfolio__value">
                  {formatCurrency(holding.market_value)}
                </span>
              </div>
              <p className="ballast-portfolio__desc">{describeHolding(holding.symbol)}</p>
              <div className="ballast-portfolio__meta">
                <span>{holding.quantity} shares</span>
                {direction ? (
                  <MarketIndicator
                    direction={direction}
                    label={direction === 'up' ? 'up since you bought' : 'down since you bought'}
                  />
                ) : null}
              </div>
            </li>
          )
        })}
      </ul>
    </section>
  )
}
