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

// A resting/working order (Story 8.3): effective status `pending` with a
// broker_ref present — the only shape that surfaces a Cancel control.
const WORKING_LIST_PAYLOAD = {
  decisions: [
    {
      decision_id: 'dec-working',
      action_label: 'Buy VTI near the low',
      symbol: 'VTI',
      co_signed_at: '2026-07-28T15:00:00+00:00',
      outcome_status: 'pending',
    },
  ],
}

const WORKING_DETAIL_PAYLOAD = {
  decision_id: 'dec-working',
  schema_version: 1,
  status: 'cosigned',
  created_at: '2026-07-28T14:59:00+00:00',
  co_signed_at: '2026-07-28T15:00:00+00:00',
  recommendation_snapshot: {
    action_label: 'Buy VTI near the low',
    reasoning: 'A resting limit that waits for your price.',
    order_intent: null,
    evidence: [],
    uncertainties: ['A resting order may not fill.'],
  },
  cosign_snapshot: {
    order_intent: {
      symbol: 'VTI',
      side: 'buy',
      amount: '500',
      order_type: 'limit',
      limit_price: '99.50',
    },
    outcome: {
      status: 'pending',
      filled_qty: '0',
      avg_price: null,
      broker_ref: 'fake-order-resting-1',
    },
  },
}

// A detail whose cosign snapshot still reads `pending` with a broker_ref (the
// resting/working shape), but whose NEWER reconciliation snapshot reports the
// order `filled`. The reconcile snapshot is the newer truth and must win —
// so the order is NOT shown as working and no Cancel control renders.
const RECONCILED_DETAIL_PAYLOAD = {
  decision_id: 'dec-working',
  schema_version: 1,
  status: 'cosigned',
  created_at: '2026-07-28T14:59:00+00:00',
  co_signed_at: '2026-07-28T15:00:00+00:00',
  recommendation_snapshot: {
    action_label: 'Buy VTI near the low',
    reasoning: 'A resting limit that waits for your price.',
    order_intent: null,
    evidence: [],
    uncertainties: ['A resting order may not fill.'],
  },
  cosign_snapshot: {
    order_intent: {
      symbol: 'VTI',
      side: 'buy',
      amount: '500',
      order_type: 'limit',
      limit_price: '99.50',
    },
    outcome: {
      status: 'pending',
      filled_qty: '0',
      avg_price: null,
      broker_ref: 'fake-order-resting-1',
    },
  },
  reconciliation_snapshot: {
    outcome: {
      status: 'filled',
      filled_qty: '5',
      avg_price: '99.50',
      broker_ref: 'fake-order-resting-1',
    },
  },
}

function stubFetch(handler) {
  vi.stubGlobal('fetch', vi.fn(handler))
}

// A route-by-URL stub for the working-order flow: list + detail + a cancel
// response the test supplies.
function stubWorking(cancel) {
  const fn = vi.fn((url, init) => {
    const u = String(url)
    if (u.endsWith('/api/coach/decisions/dec-working/cancel')) {
      return Promise.resolve({
        ok: cancel.ok ?? true,
        status: cancel.status ?? 200,
        json: () => Promise.resolve(cancel.body ?? {}),
      })
    }
    if (u.endsWith('/api/coach/decisions/dec-working')) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve(WORKING_DETAIL_PAYLOAD),
      })
    }
    if (u.endsWith('/api/coach/decisions')) {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve(WORKING_LIST_PAYLOAD),
      })
    }
    throw new Error(`unexpected fetch: ${u} ${init?.method ?? ''}`)
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

async function openWorkingDecision() {
  await waitFor(() =>
    expect(screen.getByTestId('decisions-list')).toBeInTheDocument(),
  )
  screen.getByTestId('decisions-item-dec-working').click()
  await waitFor(() =>
    expect(screen.getByTestId('decisions-working')).toBeInTheDocument(),
  )
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

  // --- Story 8.3: resting/working order lifecycle + Cancel -------------------

  it('a pending order with a broker_ref shows "working" + a Cancel control', async () => {
    stubWorking({ body: {} })
    renderDecisions()
    await openWorkingDecision()

    expect(screen.getByTestId('decisions-working-label')).toHaveTextContent(
      /working/i,
    )
    expect(screen.getByTestId('decisions-cancel')).toBeInTheDocument()
  })

  it('cancel → rejected updates the row honestly and hides the Cancel control', async () => {
    const fn = stubWorking({
      body: { status: 'rejected', filled_qty: '0', needs_reconfirmation: false },
    })
    renderDecisions()
    await openWorkingDecision()

    screen.getByTestId('decisions-cancel').click()
    await waitFor(() =>
      expect(screen.getByTestId('decisions-cancelled')).toBeInTheDocument(),
    )
    // Cancel is gone; the honest cancelled note remains.
    expect(screen.queryByTestId('decisions-cancel')).not.toBeInTheDocument()
    expect(screen.getByTestId('decisions-cancelled')).toHaveTextContent(
      /cancelled/i,
    )
    // A POST cancel request was actually fired.
    const cancelCall = fn.mock.calls.find((c) =>
      String(c[0]).endsWith('/api/coach/decisions/dec-working/cancel'),
    )
    expect(cancelCall[1].method).toBe('POST')
  })

  it('cancel → needs_reconfirmation shows an honest "state unclear" note, NOT a clean cancel', async () => {
    stubWorking({
      body: { status: 'pending', filled_qty: '0', needs_reconfirmation: true },
    })
    renderDecisions()
    await openWorkingDecision()

    screen.getByTestId('decisions-cancel').click()
    await waitFor(() =>
      expect(
        screen.getByTestId('decisions-cancel-unclear'),
      ).toBeInTheDocument(),
    )
    expect(screen.getByTestId('decisions-cancel-unclear')).toHaveTextContent(
      /unclear/i,
    )
    // Never a clean-cancel claim.
    expect(screen.queryByTestId('decisions-cancelled')).not.toBeInTheDocument()
  })

  it('cancel → calm 422 surfaces the backend detail verbatim, row unchanged', async () => {
    // The REAL app-wide error envelope is `{error:{type,message}}` (api/app.py),
    // not a bare `{detail}` — the calm reason lives at `error.message`.
    stubWorking({
      ok: false,
      status: 422,
      body: {
        error: {
          type: 'http_error',
          message:
            'This order can no longer be cancelled — it is already settled or partially filled.',
        },
      },
    })
    renderDecisions()
    await openWorkingDecision()

    screen.getByTestId('decisions-cancel').click()
    await waitFor(() =>
      expect(
        screen.getByTestId('decisions-cancel-refused'),
      ).toBeInTheDocument(),
    )
    expect(screen.getByTestId('decisions-cancel-refused')).toHaveTextContent(
      'This order can no longer be cancelled — it is already settled or partially filled.',
    )
    // The row is unchanged — still working, Cancel still available for retry.
    expect(screen.getByTestId('decisions-cancel')).toBeInTheDocument()
    expect(screen.queryByTestId('decisions-cancelled')).not.toBeInTheDocument()
  })

  it('cancel → 409 surfaces the backend reconnect message verbatim from the {error:{message}} envelope', async () => {
    // A message DISTINCT from the frontend fallback proves it is read from
    // `error.message` (the real envelope), not the hardcoded fallback string.
    const backendReconnect =
      'Your Schwab connection needs a quick reconnect before this can go through.'
    stubWorking({
      ok: false,
      status: 409,
      body: { error: { type: 'http_error', message: backendReconnect } },
    })
    renderDecisions()
    await openWorkingDecision()

    screen.getByTestId('decisions-cancel').click()
    await waitFor(() =>
      expect(
        screen.getByTestId('decisions-cancel-reconnect'),
      ).toBeInTheDocument(),
    )
    expect(screen.getByTestId('decisions-cancel-reconnect')).toHaveTextContent(
      backendReconnect,
    )
  })

  it('cancel → needs_reconfirmation with status=filled is honest about the fill (nothing called off)', async () => {
    stubWorking({
      body: { status: 'filled', filled_qty: '5', needs_reconfirmation: true },
    })
    renderDecisions()
    await openWorkingDecision()

    screen.getByTestId('decisions-cancel').click()
    await waitFor(() =>
      expect(
        screen.getByTestId('decisions-cancel-filled'),
      ).toBeInTheDocument(),
    )
    expect(screen.getByTestId('decisions-cancel-filled')).toHaveTextContent(
      /filled before the cancel took effect/i,
    )
    // A full fill is not a clean cancel — the Cancel control and the cancelled
    // note are both gone.
    expect(screen.queryByTestId('decisions-cancel')).not.toBeInTheDocument()
    expect(screen.queryByTestId('decisions-cancelled')).not.toBeInTheDocument()
  })

  it('cancel → needs_reconfirmation with status=partial does NOT claim a full fill', async () => {
    stubWorking({
      body: { status: 'partial', filled_qty: '2', needs_reconfirmation: true },
    })
    renderDecisions()
    await openWorkingDecision()

    screen.getByTestId('decisions-cancel').click()
    await waitFor(() =>
      expect(
        screen.getByTestId('decisions-cancel-partial'),
      ).toBeInTheDocument(),
    )
    // Honest partial copy: some shares filled, the rest was called off — never
    // the full-fill "nothing was called off" wording, never a clean cancel.
    const note = screen.getByTestId('decisions-cancel-partial')
    expect(note).toHaveTextContent(/part of this order filled/i)
    expect(note).toHaveTextContent(/the rest was called off/i)
    expect(
      screen.queryByTestId('decisions-cancel-filled'),
    ).not.toBeInTheDocument()
    expect(screen.queryByTestId('decisions-cancelled')).not.toBeInTheDocument()
    // A partial is terminal for the working state — no Cancel control remains.
    expect(screen.queryByTestId('decisions-cancel')).not.toBeInTheDocument()
  })

  it('a reconciliation snapshot (filled) beats a stale cosign pending — not working, no Cancel', async () => {
    stubFetch((url) => {
      if (url.endsWith('/api/coach/decisions')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(WORKING_LIST_PAYLOAD),
        })
      }
      if (url.endsWith('/api/coach/decisions/dec-working')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(RECONCILED_DETAIL_PAYLOAD),
        })
      }
      throw new Error(`unexpected fetch: ${url}`)
    })
    renderDecisions()

    await waitFor(() =>
      expect(screen.getByTestId('decisions-list')).toBeInTheDocument(),
    )
    screen.getByTestId('decisions-item-dec-working').click()
    await waitFor(() =>
      expect(screen.getByTestId('decision-replay')).toBeInTheDocument(),
    )
    // The newer reconcile status (filled) wins over the cosign pending — the
    // order is terminal, so no working banner and no Cancel control.
    expect(screen.queryByTestId('decisions-working')).not.toBeInTheDocument()
    expect(screen.queryByTestId('decisions-cancel')).not.toBeInTheDocument()
  })

  it('a filled (non-working) decision shows no Cancel control', async () => {
    stubFetch((url) => {
      if (url.endsWith('/api/coach/decisions')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve(LIST_PAYLOAD) })
      }
      if (url.endsWith('/api/coach/decisions/dec-2')) {
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
      expect(screen.getByTestId('decision-replay')).toBeInTheDocument(),
    )
    expect(screen.queryByTestId('decisions-working')).not.toBeInTheDocument()
    expect(screen.queryByTestId('decisions-cancel')).not.toBeInTheDocument()
  })
})
