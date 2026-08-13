import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { CoachConsult } from '../components/CoachConsult.jsx'

afterEach(() => {
  vi.unstubAllGlobals()
})

// The same calm/no-FOMO tone bar as the backend digest FORBIDDEN list
// (test_digest_compose.py) — the spec's testable standard. Word-boundary matched.
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
// Story 10.3 — the deploy affordance now calls `GET /api/allocation/narration`,
// which returns `{plan, narration}` (the plan shapes are unchanged, just nested
// under `plan`). Any other URL is unexpected in these tests.

function stubFetch({ plan, narration, ok = true, status = 200 } = {}) {
  const fn = vi.fn((url) => {
    const u = String(url)
    if (u.includes('/api/allocation/narration')) {
      return Promise.resolve({
        ok,
        status,
        json: () => Promise.resolve({ plan: plan ?? {}, narration: narration ?? {} }),
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

const DEPLOY_PLAN = {
  status: 'deploy',
  action_items: [
    { asset_class: 'intl_equity', symbol: 'VXUS', amount: '3000.00' },
    { asset_class: 'bonds', symbol: 'BND', amount: '1000.00' },
  ],
  primary_order: {
    symbol: 'VXUS',
    side: 'buy',
    amount: '3000.00',
    order_type: 'market',
  },
  current: {},
  unclassified: { market_value: '0', symbols: [] },
  investable_cash: '4000.00',
  undeployed_cash: '0.00',
  reason: '',
  as_of: null,
}

const DEPLOY_NARRATION = {
  action_label: 'Put your idle cash to work toward your target mix',
  reasoning:
    'Compared with the target mix you chose, you’re light on International ' +
    'stocks and Bonds, so this plan buys 3000.00 of VXUS and 1000.00 of BND — ' +
    'the broad, low-cost index funds for those classes — to move you back toward ' +
    'that balance. This is plain diversification and rebalancing toward your ' +
    'target, not a bet on any hot pick. It doesn’t try to time the market, and ' +
    'any leftover cash (0.00) stays put.',
  uncertainties: [
    'Markets move, so a fill isn’t guaranteed and this isn’t a prediction.',
  ],
  evidence: [
    {
      id: 'strat-abc123',
      kind: 'strategy',
      statement: 'You’re underweight International stocks versus your target mix.',
      stats: { amount: '3000.00', current_weight: '0' },
      source: 'allocation-engine',
      as_of: '2026-08-12',
    },
  ],
}

// Story 10.7 — a deploy plan carrying the full current-vs-target x-ray + an
// unclassified holding.
const XRAY_PLAN = {
  ...DEPLOY_PLAN,
  current: {
    us_equity: { market_value: '6000.00', weight: '0.6000' },
    intl_equity: { market_value: '0.00', weight: '0.0000' },
    bonds: { market_value: '0.00', weight: '0.0000' },
  },
  target_weights: { us_equity: '0.60', intl_equity: '0.30', bonds: '0.10' },
  unclassified: { market_value: '500.00', symbols: ['TSLA'] },
}

describe('CoachConsult — deploy-my-cash affordance (Story 10.2/10.3)', () => {
  it('populates the order controls + renders the advisor narration on a deploy plan, no submit', async () => {
    const fetchMock = stubFetch({ plan: DEPLOY_PLAN, narration: DEPLOY_NARRATION })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-deploy-cash'))

    // The primary MARKET BUY lands in the controls.
    await waitFor(() =>
      expect(screen.getByTestId('coach-symbol-input').value).toBe('VXUS'),
    )
    expect(screen.getByTestId('coach-amount-input').value).toBe('3000.00')
    expect(screen.getByTestId('coach-side-select').value).toBe('buy')

    // A calm populated note appears.
    const note = await screen.findByTestId('coach-deploy-populated')
    expectCalm(note.textContent)

    // Story 10.3 — the advisor narration card renders (action_label + reasoning).
    const card = await screen.findByTestId('coach-card')
    expect(card).toBeInTheDocument()
    expect(screen.getByTestId('coach-card-action')).toHaveTextContent(
      /put your idle cash to work toward your target mix/i,
    )
    const reasoning = screen.getByTestId('coach-card-reasoning')
    expect(reasoning).toHaveTextContent(/diversification and rebalancing/i)
    expectCalm(card.textContent)

    // ONLY the read-only narration endpoint was hit — nothing was submitted (no
    // /approve, no /recommend, no order placement).
    const urls = fetchMock.mock.calls.map(([u]) => String(u))
    expect(urls.some((u) => u.includes('/api/allocation/narration'))).toBe(true)
    expect(urls.some((u) => u.includes('/approve'))).toBe(false)
    expect(urls.some((u) => u.includes('/recommend'))).toBe(false)
    // The narration GET carries no method/body (read-only).
    const narrationCall = fetchMock.mock.calls.find(([u]) =>
      String(u).includes('/api/allocation/narration'),
    )
    const opts = narrationCall[1] ?? {}
    expect(opts.method ?? 'GET').toBe('GET')
    expect(opts.body).toBeUndefined()
  })

  it('renders the current-vs-target x-ray + unclassified sleeve on a deploy plan (Story 10.7)', async () => {
    stubFetch({ plan: XRAY_PLAN, narration: DEPLOY_NARRATION })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-deploy-cash'))

    const xray = await screen.findByTestId('coach-deploy-xray')
    expect(xray).toBeInTheDocument()
    expect(screen.getByTestId('coach-xray-us_equity')).toHaveTextContent(
      /US stocks.*60% now.*target 60%/i,
    )
    expect(screen.getByTestId('coach-xray-intl_equity')).toHaveTextContent(
      /International.*0% now.*target 30%/i,
    )
    expect(screen.getByTestId('coach-xray-bonds')).toHaveTextContent(
      /Bonds.*0% now.*target 10%/i,
    )
    // Unclassified holdings surfaced honestly (excluded from the target math).
    expect(screen.getByTestId('coach-xray-unclassified')).toHaveTextContent(
      /Not counted toward your target mix: \$500\.00 \(TSLA\)/i,
    )
    expectCalm(xray.textContent)
  })

  it('shows the honest money-market funding split + protected reserve (Story 10.8 AC5)', async () => {
    const MM_PLAN = {
      ...XRAY_PLAN,
      investable_cash: '65949.08',
      settlement_cash: '12182.82',
      from_money_market: '53766.26',
      reserve: '40000.00',
      money_market_symbols: ['SWVXX'],
    }
    stubFetch({ plan: MM_PLAN, narration: DEPLOY_NARRATION })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-deploy-cash'))

    const funding = await screen.findByTestId('coach-deploy-funding')
    expect(funding).toHaveTextContent(
      /\$12182\.82 of settled cash plus \$53766\.26 from selling your money-market fund \(SWVXX\)/i,
    )
    expect(funding).toHaveTextContent(/\$40000\.00 reserve stays untouched/i)
    expectCalm(funding.textContent)
  })

  it('omits the money-market split when the deploy is pure settled cash (from_money_market 0)', async () => {
    // XRAY_PLAN has no funding-split fields → from_money_market is undefined/0.
    stubFetch({ plan: XRAY_PLAN, narration: DEPLOY_NARRATION })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-deploy-cash'))

    await screen.findByTestId('coach-deploy-xray')
    expect(screen.queryByTestId('coach-deploy-funding')).not.toBeInTheDocument()
  })

  it('shows the calm reason and populates NOTHING on a no-action status', async () => {
    const NO_TARGET = {
      status: 'no_target',
      action_items: [],
      primary_order: null,
      current: {},
      unclassified: { market_value: '0', symbols: [] },
      investable_cash: '0.00',
      undeployed_cash: '0.00',
      reason: "Pick a target mix first, and I'll show you how to move your cash toward it.",
      as_of: null,
    }
    const NO_TARGET_NARRATION = {
      action_label: 'Nothing to buy right now',
      reasoning: NO_TARGET.reason,
      uncertainties: ['This can change as your cash or target mix change.'],
      evidence: [],
    }
    stubFetch({ plan: NO_TARGET, narration: NO_TARGET_NARRATION })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-deploy-cash'))

    const note = await screen.findByTestId('coach-deploy-no-action')
    expect(note).toHaveTextContent(/pick a target mix first/i)
    expectCalm(note.textContent)

    // NOTHING populated — the controls stay empty.
    expect(screen.getByTestId('coach-symbol-input').value).toBe('')
    expect(screen.getByTestId('coach-amount-input').value).toBe('')
    expect(screen.getByTestId('coach-side-select').value).toBe('')
    // Neither the populated note nor a narration card is shown.
    expect(screen.queryByTestId('coach-deploy-populated')).not.toBeInTheDocument()
    expect(screen.queryByTestId('coach-card')).not.toBeInTheDocument()
  })

  it('shows a calm reason for a decide_reserve status, still no populate', async () => {
    const DECIDE_RESERVE = {
      status: 'decide_reserve',
      action_items: [],
      primary_order: null,
      current: {},
      unclassified: { market_value: '0', symbols: [] },
      investable_cash: '0.00',
      undeployed_cash: '0.00',
      reason: "Set your cash cushion first, and I'll only ever deploy what's above it.",
      as_of: null,
    }
    const DECIDE_RESERVE_NARRATION = {
      action_label: 'Nothing to buy right now',
      reasoning: DECIDE_RESERVE.reason,
      uncertainties: ['This can change as your cushion or holdings change.'],
      evidence: [],
    }
    stubFetch({ plan: DECIDE_RESERVE, narration: DECIDE_RESERVE_NARRATION })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-deploy-cash'))

    const note = await screen.findByTestId('coach-deploy-no-action')
    expect(note).toHaveTextContent(/cash cushion first/i)
    expectCalm(note.textContent)
    expect(screen.getByTestId('coach-amount-input').value).toBe('')
  })

  it('fails calmly when the narration endpoint errors, populating nothing', async () => {
    stubFetch({ ok: false, status: 500 })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-deploy-cash'))

    const note = await screen.findByTestId('coach-deploy-failed')
    expectCalm(note.textContent)
    expect(screen.getByTestId('coach-symbol-input').value).toBe('')
  })

  it('does NOT populate on a malformed primary_order (zero amount), shows calm failed note', async () => {
    const BAD_ZERO = {
      ...DEPLOY_PLAN,
      primary_order: {
        symbol: 'VXUS',
        side: 'buy',
        amount: '0.00',
        order_type: 'market',
      },
    }
    stubFetch({ plan: BAD_ZERO, narration: DEPLOY_NARRATION })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-deploy-cash'))

    // Contract-drift insurance: a zero amount is refused rather than populating an
    // un-co-signable order — falls through to the calm failed note.
    const note = await screen.findByTestId('coach-deploy-failed')
    expectCalm(note.textContent)
    // NOTHING populated — controls stay empty, no populated note, no narration card.
    expect(screen.getByTestId('coach-symbol-input').value).toBe('')
    expect(screen.getByTestId('coach-amount-input').value).toBe('')
    expect(screen.getByTestId('coach-side-select').value).toBe('')
    expect(screen.queryByTestId('coach-deploy-populated')).not.toBeInTheDocument()
    expect(screen.queryByTestId('coach-card')).not.toBeInTheDocument()
  })

  it('does NOT populate on a malformed primary_order (blank symbol), shows calm failed note', async () => {
    const BAD_SYMBOL = {
      ...DEPLOY_PLAN,
      primary_order: {
        symbol: '',
        side: 'buy',
        amount: '3000.00',
        order_type: 'market',
      },
    }
    stubFetch({ plan: BAD_SYMBOL, narration: DEPLOY_NARRATION })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-deploy-cash'))

    const note = await screen.findByTestId('coach-deploy-failed')
    expectCalm(note.textContent)
    expect(screen.getByTestId('coach-symbol-input').value).toBe('')
    expect(screen.getByTestId('coach-amount-input').value).toBe('')
    expect(screen.queryByTestId('coach-deploy-populated')).not.toBeInTheDocument()
    expect(screen.queryByTestId('coach-card')).not.toBeInTheDocument()
  })

  it('has calm copy on the deploy button itself', () => {
    stubFetch({ plan: DEPLOY_PLAN, narration: DEPLOY_NARRATION })
    renderConsult()
    const btn = screen.getByTestId('coach-deploy-cash')
    expect(btn).toHaveTextContent(/deploy your cash toward your target/i)
    expectCalm(btn.textContent)
  })
})
