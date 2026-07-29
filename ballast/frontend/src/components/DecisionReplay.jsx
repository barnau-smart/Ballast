import { PrecedentEvidence } from './PrecedentEvidence.jsx'
import { UncertaintyCallout } from './UncertaintyCallout.jsx'
import { formatDay } from '../lib/datetime.js'
import './DecisionReplay.css'

/**
 * Replays a co-signed decision VERBATIM from a `/api/coach/decisions/{id}`
 * detail payload (Story 4.10, FR16, AD-5). It renders the frozen
 * `recommendation_snapshot` / `cosign_snapshot` the backend persisted at
 * co-sign time — it recomputes nothing.
 *
 * Renders the fixed coach-card sequence, non-reorderable (epic UX):
 *   action_label → **Why** (reasoning) → precedent data-block(s) → uncertainty
 *   callout → co-sign zone (executed intent + reconciled outcome).
 *
 * Presentation-only (AD-1). Honest + accessible:
 * - reasoning and uncertainties are REAL DOM text (screen-reader legible).
 * - each snapshot evidence record renders through the shared `PrecedentEvidence`
 *   component (so the replayed data-block cannot drift from the live coach
 *   card's color/honesty rules). Each mount gets a distinct `idPrefix` so the
 *   disclosure DOM ids stay unique.
 * - the co-sign outcome renders as calm mono text — NEVER red/pink, color is
 *   never the sole signal (every value carries a real text label).
 */
export function DecisionReplay({ detail }) {
  if (!detail) return null

  const snapshot = detail.recommendation_snapshot ?? {}
  const evidence = Array.isArray(snapshot.evidence) ? snapshot.evidence : []
  const cosign = detail.cosign_snapshot ?? {}
  const executed = cosign.order_intent ?? null
  const outcome = cosign.outcome ?? null

  return (
    <article className="ballast-replay" data-testid="decision-replay">
      <p className="ballast-replay__action" data-testid="replay-action-label">
        {snapshot.action_label}
      </p>

      <section className="ballast-replay__section">
        <h2 className="ballast-replay__heading">Why</h2>
        <p className="ballast-replay__reasoning" data-testid="replay-reasoning">
          {snapshot.reasoning}
        </p>
      </section>

      <section className="ballast-replay__section" data-testid="replay-precedent">
        {evidence.map((record, i) => (
          <PrecedentEvidence
            key={record?.id ?? i}
            record={record}
            idPrefix={`replay-precedent-${i}`}
          />
        ))}
      </section>

      <UncertaintyCallout uncertainties={snapshot.uncertainties} />

      {detail.co_signed_at ? (
        <section className="ballast-replay__cosign" data-testid="replay-cosign">
          <p className="ballast-replay__cosign-heading">Co-signed</p>
          <p className="ballast-replay__cosign-line" data-testid="replay-cosign-at">
            On the record since {formatDay(detail.co_signed_at)}
          </p>
          {executed ? (
            <p
              className="ballast-replay__cosign-line"
              data-testid="replay-cosign-intent"
            >
              You approved: {executed.side} {executed.symbol} · {executed.amount}
            </p>
          ) : null}
          {outcome ? (
            <p
              className="ballast-replay__cosign-line"
              data-testid="replay-cosign-outcome"
            >
              Outcome: {outcome.status}
              {outcome.filled_qty != null
                ? ` · filled ${outcome.filled_qty}`
                : ''}
              {outcome.avg_price != null ? ` @ ${outcome.avg_price}` : ''}
            </p>
          ) : null}
        </section>
      ) : null}
    </article>
  )
}
