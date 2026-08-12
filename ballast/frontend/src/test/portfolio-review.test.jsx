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

function stubFetch({ findings, ok = true, status = 200 } = {}) {
  const fn = vi.fn((url) => {
    const u = String(url)
    if (u.includes('/api/allocation/review')) {
      return Promise.resolve({
        ok,
        status,
        json: () => Promise.resolve({ findings: findings ?? [] }),
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
