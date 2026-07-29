import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Decisions } from '../routes/Decisions.jsx'

afterEach(() => {
  vi.unstubAllGlobals()
})

const LIST_PAYLOAD = {
  decisions: [
    {
      decision_id: 'dec-2',
      action_label: 'Make your regular contribution',
      symbol: 'VTI',
      co_signed_at: '2026-07-28T15:00:00+00:00',
      outcome_status: 'filled',
    },
    {
      decision_id: 'dec-1',
      action_label: 'Buy into the dip',
      symbol: 'VOO',
      co_signed_at: '2026-07-20T09:00:00+00:00',
      outcome_status: 'filled',
    },
  ],
}

// Offline seam (Story 4.10 design note): the fake pipeline falls back to the
// default plan, so a co-signed snapshot carries STRATEGY-kind evidence and a
// null proposed order_intent, while cosign_snapshot.order_intent is the
// client-supplied executed intent.
const DETAIL_PAYLOAD = {
  decision_id: 'dec-2',
  schema_version: 1,
  status: 'cosigned',
  created_at: '2026-07-28T14:59:00+00:00',
  co_signed_at: '2026-07-28T15:00:00+00:00',
  recommendation_snapshot: {
    action_label: 'Make your regular contribution',
    reasoning:
      'Stick to your plan. Regular contributions through calm and rough markets alike are what compound over time.',
    order_intent: null,
    evidence: [
      {
        id: 'strat-abc123456789',
        kind: 'strategy',
        statement:
          'Staying invested and contributing on schedule is the strategy-backed default.',
        stats: { reason: 'default_plan', windows: [] },
        source: 'VTI daily close (market_daily)',
        as_of: '2026-07-27',
      },
    ],
    uncertainties: [
      'Markets can keep falling in the short term — this is a long-horizon plan.',
    ],
  },
  cosign_snapshot: {
    order_intent: { symbol: 'VTI', side: 'buy', amount: '500' },
    outcome: {
      status: 'filled',
      filled_qty: '4.2',
      avg_price: '119.05',
      broker_ref: 'fake-order-k1',
    },
  },
}

function stubFetch(handler) {
  vi.stubGlobal('fetch', vi.fn(handler))
}

function renderDecisions() {
  return render(
    <MemoryRouter>
      <Decisions />
    </MemoryRouter>,
  )
}

describe('Decisions surface — co-signed history & verbatim replay', () => {
  it('renders the co-signed list newest-first, then replays a selection inline', async () => {
    stubFetch((url) => {
      if (url.endsWith('/api/coach/decisions')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(LIST_PAYLOAD) })
      }
      if (url.endsWith('/api/coach/decisions/dec-2')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(DETAIL_PAYLOAD),
        })
      }
      throw new Error(`unexpected fetch: ${url}`)
    })
    const { container } = renderDecisions()

    // The list renders the co-signed decisions.
    await waitFor(() =>
      expect(screen.getByTestId('decisions-list')).toBeInTheDocument(),
    )
    expect(
      screen.getByText('Make your regular contribution'),
    ).toBeInTheDocument()
    expect(screen.getByText('Buy into the dip')).toBeInTheDocument()

    // Select the first (newest) decision → it replays inline.
    screen.getByTestId('decisions-item-dec-2').click()

    await waitFor(() =>
      expect(screen.getByTestId('decision-replay')).toBeInTheDocument(),
    )

    // action_label + reasoning are real DOM text.
    expect(screen.getByTestId('replay-action-label')).toHaveTextContent(
      'Make your regular contribution',
    )
    expect(screen.getByTestId('replay-reasoning')).toHaveTextContent(
      /Stick to your plan/i,
    )

    // Precedent renders through the shared PrecedentEvidence (strategy branch
    // offline) — the data-block cannot drift from the live coach card.
    expect(screen.getByTestId('precedent-strategy')).toHaveTextContent(
      /strategy-backed default/i,
    )
    expect(screen.getByTestId('precedent-source')).toHaveTextContent(
      'VTI daily close (market_daily) · as of 2026-07-27',
    )

    // Violet uncertainty callout, always present, as real DOM text.
    const uncertainty = screen.getByTestId('uncertainty-callout')
    expect(uncertainty).toHaveTextContent(/Markets can keep falling/i)

    // Co-sign zone: executed intent + reconciled outcome as calm text.
    expect(screen.getByTestId('replay-cosign-intent')).toHaveTextContent(
      /buy VTI · 500/i,
    )
    expect(screen.getByTestId('replay-cosign-outcome')).toHaveTextContent(
      /filled/i,
    )

    // HARD color rule: no red/pink for any amount/loss anywhere in the replay.
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink/)
    expect(within(uncertainty).queryByText(/error|failed/i)).toBeNull()
  })

  it('shows a gentle empty invite (not an error) when there is no history', async () => {
    stubFetch((url) => {
      if (url.endsWith('/api/coach/decisions')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ decisions: [] }),
        })
      }
      throw new Error(`unexpected fetch: ${url}`)
    })
    const { container } = renderDecisions()

    await waitFor(() =>
      expect(screen.getByTestId('decisions-empty')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('decisions-empty')).toHaveTextContent(
      /No decisions on the record yet/i,
    )
    // Gentle invite, never an error dead end.
    expect(container.innerHTML).not.toMatch(/error|failed to load/i)
    expect(screen.queryByTestId('decisions-list')).not.toBeInTheDocument()
  })

  it('shows a distinct calm "couldn\'t load" note (NOT a false empty) when the list fetch fails', async () => {
    stubFetch(() => Promise.reject(new Error('network down')))
    renderDecisions()

    // A load failure must NOT be reported as "no decisions yet" — that would be
    // dishonest for a user who actually has history.
    await waitFor(() =>
      expect(screen.getByTestId('decisions-load-error')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('decisions-load-error')).toHaveTextContent(
      /couldn’t load your record/i,
    )
    expect(screen.queryByTestId('decisions-empty')).not.toBeInTheDocument()
    expect(screen.queryByTestId('decisions-list')).not.toBeInTheDocument()
  })

  it('renders co_signed_at as a calm human day, never the raw ISO timestamp', async () => {
    stubFetch((url) => {
      if (url.endsWith('/api/coach/decisions')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(LIST_PAYLOAD) })
      }
      if (url.endsWith('/api/coach/decisions/dec-2')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(DETAIL_PAYLOAD) })
      }
      throw new Error(`unexpected fetch: ${url}`)
    })
    const { container } = renderDecisions()

    await waitFor(() =>
      expect(screen.getByTestId('decisions-list')).toBeInTheDocument(),
    )
    screen.getByTestId('decisions-item-dec-2').click()
    await waitFor(() =>
      expect(screen.getByTestId('replay-cosign-at')).toBeInTheDocument(),
    )

    expect(screen.getByTestId('replay-cosign-at')).toHaveTextContent(
      'On the record since Jul 28, 2026',
    )
    // The raw wire timestamp (time-of-day / offset) never reaches the DOM.
    expect(container.innerHTML).not.toMatch(/T15:00:00/)
  })

  it('re-fires the detail fetch when the same decision is re-selected after an error', async () => {
    let detailCalls = 0
    stubFetch((url) => {
      if (url.endsWith('/api/coach/decisions')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(LIST_PAYLOAD) })
      }
      if (url.endsWith('/api/coach/decisions/dec-2')) {
        detailCalls += 1
        // First attempt fails; the retry (same id re-selected) succeeds.
        if (detailCalls === 1) return Promise.reject(new Error('flaky'))
        return Promise.resolve({ ok: true, json: () => Promise.resolve(DETAIL_PAYLOAD) })
      }
      throw new Error(`unexpected fetch: ${url}`)
    })
    renderDecisions()

    await waitFor(() =>
      expect(screen.getByTestId('decisions-list')).toBeInTheDocument(),
    )
    screen.getByTestId('decisions-item-dec-2').click()
    await waitFor(() =>
      expect(screen.getByTestId('decisions-detail-error')).toBeInTheDocument(),
    )

    // Re-selecting the SAME (still-active) item must retry, not no-op.
    screen.getByTestId('decisions-item-dec-2').click()
    await waitFor(() =>
      expect(screen.getByTestId('decision-replay')).toBeInTheDocument(),
    )
    expect(detailCalls).toBe(2)
  })
})
