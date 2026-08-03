import { PrecedentEvidence } from './PrecedentEvidence.jsx'
import { UncertaintyCallout } from './UncertaintyCallout.jsx'
import './CoachCard.css'

/**
 * The live coach card (Story 4.11, FR7/FR12/FR14; UX "emotional centerpiece",
 * mockups/coach-card.html). Pure presentation of a live `/api/coach/recommend`
 * response — the co-sign zone (approve/decline) is owned by the interactive
 * `CoachConsult` parent, which renders it BELOW this card.
 *
 * Renders the fixed, non-reorderable sequence (UX-DR4): termbar, the echoed
 * question, the "> action_label" line, the "why" (reasoning), the "what the
 * record shows" precedent block(s), and the "what I can't know" uncertainty
 * callout.
 *
 * Presentation-only (AD-1): renders what the backend blessed, computes nothing.
 * Reasoning and uncertainties are real DOM text (never collapsed by default,
 * never hidden from assistive tech). Each evidence record renders through the
 * shared `PrecedentEvidence` block so the live card cannot drift from the
 * replay/reference views on the color/honesty rules; a distinct `idPrefix` per
 * record keeps the disclosure DOM ids unique.
 */
export function CoachCard({ recommendation, question }) {
  if (!recommendation) return null

  const evidence = Array.isArray(recommendation.evidence)
    ? recommendation.evidence
    : []
  const asked = typeof question === 'string' ? question.trim() : ''

  return (
    <article className="ballast-coach-card" data-testid="coach-card">
      <p className="ballast-coach-card__termbar">ballast:~$ coach --review</p>

      {asked ? (
        <p className="ballast-coach-card__ask" data-testid="coach-card-ask">
          “{asked}”
        </p>
      ) : null}

      <p className="ballast-coach-card__rec" data-testid="coach-card-action">
        <span className="ballast-coach-card__prompt">&gt;</span>{' '}
        {recommendation.action_label}
      </p>

      <section className="ballast-coach-card__section">
        <p className="ballast-coach-card__key">// why</p>
        <p
          className="ballast-coach-card__reasoning"
          data-testid="coach-card-reasoning"
        >
          {recommendation.reasoning}
        </p>
      </section>

      {evidence.length > 0 ? (
        <section
          className="ballast-coach-card__section"
          data-testid="coach-card-precedent"
        >
          <p className="ballast-coach-card__key">// what the record shows</p>
          {evidence.map((record, i) => (
            <PrecedentEvidence
              key={record?.id ?? i}
              record={record}
              idPrefix={`coach-precedent-${i}`}
            />
          ))}
        </section>
      ) : null}

      <UncertaintyCallout uncertainties={recommendation.uncertainties} />
    </article>
  )
}
