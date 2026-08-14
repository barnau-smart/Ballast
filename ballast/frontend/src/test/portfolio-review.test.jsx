import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { CoachConsult } from '../components/CoachConsult.jsx'

afterEach(() => {
  vi.unstubAllGlobals()
})

// The same calm/no-FOMO tone bar as the backend digest FORBIDDEN list. Word-
// boundary matched.
const FORBIDDEN = [
  'urgent', 'hurry', 'act now', 'act fast', "don't miss", 'dont miss',
  'missing out', 'miss out', 'last chance', 'limited time', 'warning',
  'alarm', 'panic', 'crash', 'plunge', 'fear', 'red', 'alert', 'immediately',
]

function expectCalm(text) {
  const blob = String(text).toLowerCase()
  for (const word of FORBIDDEN) {
    const pattern = new RegExp('\\b' + word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b')
    expect(blob).not.toMatch(pattern)
  }
}

// --- fetch stub: route by URL substring --------------------------------------
//
// Story 10.4 — the review affordance calls `GET /api/allocation/review`, returning
// `{findings: [...]}`. Any other URL is unexpected in these tests (so a stray
// /approve or /recommend would blow up loudly — proving nothing was submitted).

function stubFetch({ findings, coverage = null, single_stock = null, ok = true, status = 200 } = {}) {
  const fn = vi.fn((url) => {
    const u = String(url)
    if (u.includes('/api/allocation/review')) {
      return Promise.resolve({
        ok,
        status,
        json: () => Promise.resolve({ findings: findings ?? [], coverage, single_stock }),
      })
    }
    return Promise.reject(new Error(`unexpected url: ${u}`))
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

function renderConsult() {
  return render(
    <MemoryRouter>
      <CoachConsult />
    </MemoryRouter>,
  )
}

const CONCENTRATION_FINDING = {
  kind: 'concentration',
  symbol: 'TSLA',
  switch_to: null,
  order: { symbol: 'TSLA', side: 'sell', amount: '1500.00', order_type: 'market' },
  narration: {
    action_label: 'Trim this large single position back toward your diversified core',
    reasoning:
      'Your TSLA has grown to 55.00% of your portfolio, past the 40% ' +
      'single-position ceiling. The principle here is diversification: a single ' +
      'stock is speculative, while your broad index core spreads the same money ' +
      'across the whole market. This plan trims 1500.00 of TSLA back toward that ' +
      'ceiling. The tradeoff is real: you cap the upside of this one name.',
    uncertainties: [
      'Markets move, so a fill isn’t guaranteed and this isn’t a prediction.',
    ],
    evidence: [
      {
        id: 'strat-conc1',
        kind: 'strategy',
        statement: 'Your TSLA is 55.00% of your portfolio.',
        stats: { sell_amount: '1500.00', weight: '0.55' },
        source: 'allocation-review',
        as_of: '2026-08-12',
      },
    ],
  },
}

const COST_FINDING = {
  kind: 'cost',
  symbol: 'AGTHX',
  switch_to: 'VTI',
  order: { symbol: 'AGTHX', side: 'sell', amount: '2000.00', order_type: 'market' },
  narration: {
    action_label: 'Switch this pricey fund for a cheaper one that holds the same thing',
    reasoning:
      'Your AGTHX charges a 0.61% yearly fee, while the broad VTI index fund ' +
      'charges 0.03% — a gap that quietly compounds against you over decades. The ' +
      'principle here is to minimize fund fees. This plan sells 2000.00 of AGTHX so ' +
      'you can buy the cheaper VTI. The tradeoff is honest: selling may realize a ' +
      'taxable gain, and we don’t calculate tax here.',
    uncertainties: [
      'Markets move, so a fill isn’t guaranteed; and selling may have a tax ' +
        'consequence we don’t calculate for you.',
    ],
    evidence: [
      {
        id: 'strat-cost1',
        kind: 'strategy',
        statement: 'Your AGTHX charges a 0.61% yearly fee versus 0.03% for VTI.',
        stats: { sell_amount: '2000.00', expense_ratio: '0.61' },
        source: 'allocation-review',
        as_of: '2026-08-12',
      },
    ],
  },
}

describe('CoachConsult — review-my-portfolio affordance (Story 10.4)', () => {
  it('lists each finding as an advisor card and renders its action_label/reasoning', async () => {
    stubFetch({ findings: [COST_FINDING, CONCENTRATION_FINDING] })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))

    const result = await screen.findByTestId('coach-review-result')
    expect(result).toBeInTheDocument()

    // Both findings render as CoachCards.
    const cards = await screen.findAllByTestId('coach-card')
    expect(cards).toHaveLength(2)
    const actions = screen.getAllByTestId('coach-card-action')
    expect(actions[0]).toHaveTextContent(/switch this pricey fund/i)
    expect(actions[1]).toHaveTextContent(/trim this large single position/i)
    const reasonings = screen.getAllByTestId('coach-card-reasoning')
    expect(reasonings[0]).toHaveTextContent(/minimize fund fees/i)
    expect(reasonings[1]).toHaveTextContent(/diversification/i)

    // Calm copy throughout the result region.
    expectCalm(result.textContent)
  })

  it('populates the shared controls with the SELL MARKET order and submits nothing', async () => {
    const fetchMock = stubFetch({ findings: [CONCENTRATION_FINDING] })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))
    await screen.findByTestId('coach-review-result')

    // "Fill in this order" populates the shared controls with the SELL order.
    fireEvent.click(screen.getByTestId('coach-review-fill-0'))

    await waitFor(() =>
      expect(screen.getByTestId('coach-symbol-input').value).toBe('TSLA'),
    )
    expect(screen.getByTestId('coach-amount-input').value).toBe('1500.00')
    expect(screen.getByTestId('coach-side-select').value).toBe('sell')

    // ONLY the read-only review endpoint was hit — nothing submitted (no /approve,
    // no /recommend, no order placement).
    const urls = fetchMock.mock.calls.map(([u]) => String(u))
    expect(urls.some((u) => u.includes('/api/allocation/review'))).toBe(true)
    expect(urls.some((u) => u.includes('/approve'))).toBe(false)
    expect(urls.some((u) => u.includes('/recommend'))).toBe(false)
    // The review GET carries no method/body (read-only).
    const reviewCall = fetchMock.mock.calls.find(([u]) =>
      String(u).includes('/api/allocation/review'),
    )
    const opts = reviewCall[1] ?? {}
    expect(opts.method ?? 'GET').toBe('GET')
    expect(opts.body).toBeUndefined()
  })

  it('clears a shown review list when the user deploys (mutually-exclusive result panels)', async () => {
    // Regression (Group-C review): a shown Review list left its SELL "Fill" button
    // live beside a fresh Deploy BUY — one click could overwrite the BUY with a SELL
    // the user never chose. Deploying must clear the review list.
    const fetchMock = vi.fn((url) => {
      const u = String(url)
      if (u.includes('/api/allocation/review')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve({ findings: [CONCENTRATION_FINDING] }),
        })
      }
      if (u.includes('/api/allocation/narration')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve({
              plan: {
                status: 'deploy',
                primary_order: {
                  symbol: 'VTI',
                  side: 'buy',
                  amount: '2000.00',
                  order_type: 'market',
                },
                reason: '',
              },
              narration: null,
            }),
        })
      }
      return Promise.reject(new Error(`unexpected url: ${u}`))
    })
    vi.stubGlobal('fetch', fetchMock)
    renderConsult()

    // 1) Review → the findings list (with its SELL "Fill" button) is on screen.
    fireEvent.click(screen.getByTestId('coach-review-portfolio'))
    await screen.findByTestId('coach-review-result')
    expect(screen.getByTestId('coach-review-fill-0')).toBeInTheDocument()

    // 2) Deploy → a BUY is populated AND the stale review list/Fill button are gone.
    fireEvent.click(screen.getByTestId('coach-deploy-cash'))
    await screen.findByTestId('coach-deploy-populated')

    expect(screen.queryByTestId('coach-review-result')).toBeNull()
    expect(screen.queryByTestId('coach-review-fill-0')).toBeNull()
    expect(screen.getByTestId('coach-symbol-input').value).toBe('VTI')
    expect(screen.getByTestId('coach-side-select').value).toBe('buy')
    expect(screen.getByTestId('coach-amount-input').value).toBe('2000.00')

    // Still nothing submitted.
    const urls = fetchMock.mock.calls.map(([u]) => String(u))
    expect(urls.some((u) => u.includes('/approve'))).toBe(false)
    expect(urls.some((u) => u.includes('/recommend'))).toBe(false)
  })

  it('shows the calm "nothing to fix" message on empty findings, populates nothing', async () => {
    stubFetch({ findings: [] })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))

    const note = await screen.findByTestId('coach-review-empty')
    expect(note).toHaveTextContent(/nothing to fix/i)
    expectCalm(note.textContent)

    // No cards, no populate.
    expect(screen.queryByTestId('coach-card')).not.toBeInTheDocument()
    expect(screen.getByTestId('coach-symbol-input').value).toBe('')
    expect(screen.getByTestId('coach-amount-input').value).toBe('')
  })

  it('shows a calm failed note when the review endpoint errors', async () => {
    stubFetch({ ok: false, status: 500 })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))

    const note = await screen.findByTestId('coach-review-failed')
    expectCalm(note.textContent)
    expect(screen.queryByTestId('coach-card')).not.toBeInTheDocument()
  })

  it('routes to sign-in on a 401', async () => {
    stubFetch({ ok: false, status: 401 })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))

    const note = await screen.findByTestId('coach-review-signed-out')
    expect(note).toHaveTextContent(/sign in/i)
    expectCalm(note.textContent)
  })

  it('has calm copy on the review button itself', () => {
    stubFetch({ findings: [] })
    renderConsult()
    const btn = screen.getByTestId('coach-review-portfolio')
    expect(btn).toHaveTextContent(/review my portfolio/i)
    expectCalm(btn.textContent)
  })
})

// --- Story 10.5: linked cost-switch SELL + BUY pair -------------------------

// A router-by-URL stub that also fields /recommend and /approve so a full
// review → fill → ask → approve co-sign flow runs. `approve` is the canned
// /approve body — set `linked_buy_queued` to drive the step-2 note.
function stubReviewFlow({ findings, recommend, approve } = {}) {
  const fn = vi.fn((url) => {
    const u = String(url)
    if (u.includes('/api/allocation/review')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ findings: findings ?? [] }),
      })
    }
    if (u.includes('/api/coach/recommend')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve(
            recommend ?? {
              decision_id: 'dec-switch-1',
              action_label: 'Switch this pricey fund',
              reasoning: 'A two-step switch.',
              evidence: [],
              uncertainties: ['Markets move.'],
              order_intent: {
                symbol: 'AGTHX',
                side: 'sell',
                amount: '2000.00',
              },
            },
          ),
      })
    }
    if (u.includes('/api/coach/approve')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () =>
          Promise.resolve(
            approve ?? {
              status: 'filled',
              filled_qty: '20',
              avg_price: '100.00',
              broker_ref: 'fake-order-1',
              linked_buy_queued: true,
            },
          ),
      })
    }
    // /api/portfolio is consulted by the liquidation pre-check on a BUY step; a
    // SELL never triggers it, but answer calmly if it is ever hit.
    if (u.includes('/api/portfolio')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ cash_states: { ready_to_trade: '0' } }),
      })
    }
    return Promise.reject(new Error(`unexpected url: ${u}`))
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

describe('CoachConsult — linked cost-switch SELL + BUY (Story 10.5)', () => {
  it('frames the cost finding as a linked two-step switch', async () => {
    stubFetch({ findings: [COST_FINDING] })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))
    await screen.findByTestId('coach-review-result')

    // The cost card is rendered as an advisor CoachCard; the backend narration
    // copy carries the switch framing. (The two-step copy itself is authored + gate
    // -tested on the backend; here we assert the card surfaces the switch intent.)
    const reasonings = screen.getAllByTestId('coach-card-reasoning')
    expect(reasonings[0]).toHaveTextContent(/switch|cheaper|minimize fund fees/i)
    expectCalm(screen.getByTestId('coach-review-result').textContent)
  })

  it('carries switch_to to /recommend when a cost finding is filled + asked', async () => {
    const fn = stubReviewFlow({ findings: [COST_FINDING] })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))
    await screen.findByTestId('coach-review-result')

    // Fill the cost finding's SELL — this stashes switch_to (VTI).
    fireEvent.click(screen.getByTestId('coach-review-fill-0'))
    await waitFor(() =>
      expect(screen.getByTestId('coach-symbol-input').value).toBe('AGTHX'),
    )
    expect(screen.getByTestId('coach-side-select').value).toBe('sell')

    // Ask → /recommend carries the stashed switch_to (untrusted; backend verifies).
    fireEvent.click(screen.getByTestId('coach-ask-submit'))
    await waitFor(() =>
      expect(
        fn.mock.calls.some(([u]) => String(u).includes('/api/coach/recommend')),
      ).toBe(true),
    )
    const recCall = fn.mock.calls.find(([u]) =>
      String(u).includes('/api/coach/recommend'),
    )
    const body = JSON.parse(recCall[1].body)
    expect(body.side).toBe('sell')
    expect(body.symbol).toBe('AGTHX')
    expect(body.switch_to).toBe('VTI')
  })

  it('shows the "step 2 queued" note ONLY when linked_buy_queued is true', async () => {
    stubReviewFlow({
      findings: [COST_FINDING],
      approve: {
        status: 'filled',
        filled_qty: '20',
        avg_price: '100.00',
        broker_ref: 'fake-order-1',
        linked_buy_queued: true,
      },
    })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))
    await screen.findByTestId('coach-review-result')
    fireEvent.click(screen.getByTestId('coach-review-fill-0'))
    await waitFor(() =>
      expect(screen.getByTestId('coach-symbol-input').value).toBe('AGTHX'),
    )
    fireEvent.click(screen.getByTestId('coach-ask-submit'))
    await screen.findByTestId('coach-approve')
    fireEvent.click(screen.getByTestId('coach-approve'))

    const note = await screen.findByTestId('coach-linked-buy-note')
    expect(note).toHaveTextContent(/step 2 of 2 is queued/i)
    expectCalm(note.textContent)
  })

  it('shows the "step 2 queued" note on a pending SELL with a broker_ref (live-broker path)', async () => {
    // Regression: a real venue commonly returns pending+broker_ref before settling.
    // The backend treats that as placed and queues the linked buy (linked_buy_queued
    // true); the UI must surface the reassurance from that server-truth flag, not
    // only for filled/partial — else a beginner is left unsure step 2 is queued.
    stubReviewFlow({
      findings: [COST_FINDING],
      approve: {
        status: 'pending',
        filled_qty: '0',
        avg_price: null,
        broker_ref: 'fake-order-1',
        linked_buy_queued: true,
      },
    })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))
    await screen.findByTestId('coach-review-result')
    fireEvent.click(screen.getByTestId('coach-review-fill-0'))
    await waitFor(() =>
      expect(screen.getByTestId('coach-symbol-input').value).toBe('AGTHX'),
    )
    fireEvent.click(screen.getByTestId('coach-ask-submit'))
    await screen.findByTestId('coach-approve')
    fireEvent.click(screen.getByTestId('coach-approve'))

    const note = await screen.findByTestId('coach-linked-buy-note')
    expect(note).toHaveTextContent(/step 2 of 2 is queued/i)
    expectCalm(note.textContent)
  })

  it('does NOT show the step-2 note when linked_buy_queued is false', async () => {
    stubReviewFlow({
      findings: [COST_FINDING],
      approve: {
        status: 'filled',
        filled_qty: '20',
        avg_price: '100.00',
        broker_ref: 'fake-order-1',
        linked_buy_queued: false,
      },
    })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))
    await screen.findByTestId('coach-review-result')
    fireEvent.click(screen.getByTestId('coach-review-fill-0'))
    await waitFor(() =>
      expect(screen.getByTestId('coach-symbol-input').value).toBe('AGTHX'),
    )
    fireEvent.click(screen.getByTestId('coach-ask-submit'))
    await screen.findByTestId('coach-approve')
    fireEvent.click(screen.getByTestId('coach-approve'))

    // The placed outcome renders, but the step-2 note NEVER appears (server truth).
    await screen.findByTestId('coach-outcome')
    expect(screen.queryByTestId('coach-linked-buy-note')).toBeNull()
  })

  it('clears the stashed switch_to on decline so a later ask does not carry it', async () => {
    const fn = stubReviewFlow({ findings: [COST_FINDING] })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))
    await screen.findByTestId('coach-review-result')
    fireEvent.click(screen.getByTestId('coach-review-fill-0'))
    await waitFor(() =>
      expect(screen.getByTestId('coach-symbol-input').value).toBe('AGTHX'),
    )

    // Ask, then decline (clears the stash), then re-ask — the second /recommend
    // must NOT carry switch_to (a stale switch could bypass the SELL guardrail).
    fireEvent.click(screen.getByTestId('coach-ask-submit'))
    await screen.findByTestId('coach-approve')
    fireEvent.click(screen.getByTestId('coach-decline'))
    // Re-ask the same SELL form.
    fireEvent.click(screen.getByTestId('coach-ask-submit'))

    await waitFor(() => {
      const recCalls = fn.mock.calls.filter(([u]) =>
        String(u).includes('/api/coach/recommend'),
      )
      expect(recCalls.length).toBe(2)
    })
    const recCalls = fn.mock.calls.filter(([u]) =>
      String(u).includes('/api/coach/recommend'),
    )
    // First ask carried it; the post-decline re-ask did not.
    expect(JSON.parse(recCalls[0][1].body).switch_to).toBe('VTI')
    expect(JSON.parse(recCalls[1][1].body).switch_to).toBeNull()
  })

  it('shows the calm fallback note when a placed switch SELL failed to queue the linked buy', async () => {
    // PATCH 2: the SELL placed (filled) but linked_buy_queued=false (the seeding
    // raised) — the beginner must NOT be silently stranded. A calm fallback note
    // (naming switch_to) appears; the normal "step 2 queued" note does NOT.
    const fn = stubReviewFlow({
      findings: [COST_FINDING],
      approve: {
        status: 'filled',
        filled_qty: '20',
        avg_price: '100.00',
        broker_ref: 'fake-order-1',
        linked_buy_queued: false,
      },
    })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))
    await screen.findByTestId('coach-review-result')
    fireEvent.click(screen.getByTestId('coach-review-fill-0'))
    await waitFor(() =>
      expect(screen.getByTestId('coach-symbol-input').value).toBe('AGTHX'),
    )
    fireEvent.click(screen.getByTestId('coach-ask-submit'))
    await screen.findByTestId('coach-approve')
    fireEvent.click(screen.getByTestId('coach-approve'))

    // The calm fallback appears and names the switch target (VTI).
    const fallback = await screen.findByTestId('coach-linked-buy-fallback')
    expect(fallback).toHaveTextContent(/VTI/)
    expect(fallback).toHaveTextContent(/nothing was lost/i)
    expectCalm(fallback.textContent)

    // The normal "step 2 queued" note does NOT show (server truth = false).
    expect(screen.queryByTestId('coach-linked-buy-note')).toBeNull()

    // Nothing auto-submitted beyond the explicit approve click (one /approve).
    const approveCalls = fn.mock.calls.filter(([u]) =>
      String(u).includes('/api/coach/approve'),
    )
    expect(approveCalls.length).toBe(1)
  })

  it('does NOT show the switch fallback note for an ordinary non-switch SELL', async () => {
    // A concentration finding has no switch_to — a placed SELL with
    // linked_buy_queued=false must show NEITHER the step-2 note NOR the fallback.
    stubReviewFlow({
      findings: [CONCENTRATION_FINDING],
      recommend: {
        decision_id: 'dec-conc-1',
        action_label: 'Trim',
        reasoning: 'Trim it.',
        evidence: [],
        uncertainties: ['Markets move.'],
        order_intent: { symbol: 'TSLA', side: 'sell', amount: '1500.00' },
      },
      approve: {
        status: 'filled',
        filled_qty: '10',
        avg_price: '150.00',
        broker_ref: 'fake-order-2',
        linked_buy_queued: false,
      },
    })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))
    await screen.findByTestId('coach-review-result')
    fireEvent.click(screen.getByTestId('coach-review-fill-0'))
    await waitFor(() =>
      expect(screen.getByTestId('coach-symbol-input').value).toBe('TSLA'),
    )
    fireEvent.click(screen.getByTestId('coach-ask-submit'))
    await screen.findByTestId('coach-approve')
    fireEvent.click(screen.getByTestId('coach-approve'))

    await screen.findByTestId('coach-outcome')
    expect(screen.queryByTestId('coach-linked-buy-fallback')).toBeNull()
    expect(screen.queryByTestId('coach-linked-buy-note')).toBeNull()
  })

  it('editing the symbol after filling a cost finding drops the stashed switch_to before ask', async () => {
    // PATCH 4: fill a cost finding (stashes VTI), then EDIT the symbol BEFORE asking
    // (recommendation === null). The unconditional edit() clear drops the stash, so
    // the subsequent /recommend must NOT carry a stale switch_to.
    const fn = stubReviewFlow({ findings: [COST_FINDING] })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))
    await screen.findByTestId('coach-review-result')
    fireEvent.click(screen.getByTestId('coach-review-fill-0'))
    await waitFor(() =>
      expect(screen.getByTestId('coach-symbol-input').value).toBe('AGTHX'),
    )
    expect(screen.getByTestId('coach-side-select').value).toBe('sell')

    // Edit the symbol BEFORE asking (no recommendation shown yet) — drops the stash.
    fireEvent.change(screen.getByTestId('coach-symbol-input'), {
      target: { value: 'BND' },
    })

    // Ask → /recommend must NOT carry switch_to (stale switch dropped by edit()).
    fireEvent.click(screen.getByTestId('coach-ask-submit'))
    await waitFor(() =>
      expect(
        fn.mock.calls.some(([u]) => String(u).includes('/api/coach/recommend')),
      ).toBe(true),
    )
    const recCall = fn.mock.calls.find(([u]) =>
      String(u).includes('/api/coach/recommend'),
    )
    expect(JSON.parse(recCall[1].body).switch_to).toBeNull()
  })

  it('does not auto-submit anything when a cost finding is merely filled', async () => {
    const fn = stubReviewFlow({ findings: [COST_FINDING] })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))
    await screen.findByTestId('coach-review-result')
    fireEvent.click(screen.getByTestId('coach-review-fill-0'))
    await waitFor(() =>
      expect(screen.getByTestId('coach-symbol-input').value).toBe('AGTHX'),
    )

    // Filling populates but submits NOTHING — no /recommend, no /approve.
    const urls = fn.mock.calls.map(([u]) => String(u))
    expect(urls.some((u) => u.includes('/api/coach/recommend'))).toBe(false)
    expect(urls.some((u) => u.includes('/api/coach/approve'))).toBe(false)
  })
})

// --- Story 11.1: coverage meta-check line ------------------------------------

const LOW_COVERAGE = {
  coverage: '60.00',
  adequate: false,
  unclassified_value: '7500.00',
  unclassified_symbols: ['TSLA', 'AGTHX'],
  message:
    'I can categorize about 60.00% of your portfolio into stocks and bonds. The rest — ' +
    '$7,500.00 in TSLA, AGTHX — is in individual stocks and specialty funds I don’t ' +
    'classify, so when I describe your mix, keep in mind I’m only describing the part I can see.',
}

describe('Review — coverage meta-check (Story 11.1)', () => {
  it('shows the coverage line alongside findings when coverage is inadequate', async () => {
    stubFetch({ findings: [CONCENTRATION_FINDING], coverage: LOW_COVERAGE })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))
    await screen.findByTestId('coach-review-result')

    const line = await screen.findByTestId('coach-review-coverage')
    expect(line.textContent).toMatch(/60\.00%/)
    expect(line.textContent).toMatch(/TSLA/)
    expectCalm(line.textContent)
  })

  it('shows the coverage line even when there is nothing to fix (empty findings)', async () => {
    // MasterB's real case: no SELL findings, but most of the account is unclassified.
    stubFetch({ findings: [], coverage: LOW_COVERAGE })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))
    await screen.findByTestId('coach-review-empty')

    const line = await screen.findByTestId('coach-review-coverage')
    expect(line.textContent).toMatch(/only describing the part I can see/)
  })

  it('renders NO coverage line when coverage is adequate (message null)', async () => {
    stubFetch({
      findings: [],
      coverage: { coverage: '100.00', adequate: true, unclassified_value: '0.00', unclassified_symbols: [], message: null },
    })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))
    await screen.findByTestId('coach-review-empty')
    expect(screen.queryByTestId('coach-review-coverage')).toBeNull()
  })

  it('degrades safely when coverage is absent (null)', async () => {
    stubFetch({ findings: [], coverage: null })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))
    await screen.findByTestId('coach-review-empty')
    expect(screen.queryByTestId('coach-review-coverage')).toBeNull()
  })
})

// --- Story 11.3: aggregate single-stock note ---------------------------------

const SINGLE_STOCK = {
  value: '7,500.00',
  pct: '75.00',
  symbols: ['TSLA', 'NVDA'],
  // Exact copy the backend single_stock_message emits (kept in sync with review.py).
  message:
    "About 75.00% of your portfolio is in individual stocks and specialty funds (TSLA, " +
    "NVDA) — that's a lot riding on a handful of specific companies. Spreading some of it " +
    "into a broad, diversified fund would lower that single-company risk without changing " +
    "your overall stock-and-bond balance. This isn't a prediction — it's just how " +
    "concentrated that slice is right now.",
}

describe('Review — aggregate single-stock note (Story 11.3)', () => {
  it('shows the single-stock line when the sleeve is over the band', async () => {
    stubFetch({ findings: [], single_stock: SINGLE_STOCK })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))
    await screen.findByTestId('coach-review-empty')

    const line = await screen.findByTestId('coach-review-single-stock')
    expect(line.textContent).toMatch(/75\.00%/)
    expect(line.textContent).toMatch(/TSLA/)
    expectCalm(line.textContent)
  })

  it('renders nothing when single_stock is absent (null)', async () => {
    stubFetch({ findings: [], single_stock: null })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))
    await screen.findByTestId('coach-review-empty')
    expect(screen.queryByTestId('coach-review-single-stock')).toBeNull()
  })
})

// --- Story 11.2: bond-floor finding ------------------------------------------

const BOND_FLOOR_FINDING = {
  kind: 'bond_floor',
  symbol: 'VTI',
  switch_to: 'BND',
  order: { symbol: 'VTI', side: 'sell', amount: '4000.00', order_type: 'market' },
  current_weight: '20.00',
  target_weight: '60.00',
  narration: {
    action_label: 'Add to your bonds to match the risk level you chose',
    reasoning:
      'Your bonds are 20.00% of your invested mix, while the plan you chose aims for ' +
      '60.00% — you are holding more in stocks than your risk level calls for. This is a ' +
      'two-step move: sell 4000.00 of VTI now and buy the broad BND bond fund. It is a ' +
      'rebalance toward your own plan, not a bet on where markets go next.',
    uncertainties: [
      'Markets move, so a fill isn’t guaranteed and this isn’t a prediction.',
    ],
    evidence: [
      { id: 'strat-bond1', kind: 'strategy', statement: 'bonds vs target', stats: {}, source: 'allocation-review', as_of: '2026-08-14' },
    ],
  },
}

describe('Review — bond-floor finding (Story 11.2)', () => {
  it('renders the bond-floor card + current-vs-target bond mix, and fills a SELL', async () => {
    stubReviewFlow({ findings: [BOND_FLOOR_FINDING] })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-review-portfolio'))
    await screen.findByTestId('coach-review-result')

    const mix = screen.getByTestId('coach-review-bondmix-0')
    expect(mix.textContent).toMatch(/Bonds: 20\.00% now · target 60\.00%/)
    expectCalm(screen.getByTestId('coach-review-finding-0').textContent)

    fireEvent.click(screen.getByTestId('coach-review-fill-0'))
    await waitFor(() =>
      expect(screen.getByTestId('coach-symbol-input').value).toBe('VTI'),
    )
  })
})
