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

function stubFetch({ plan, planStatus = 200, planOk = true } = {}) {
  const fn = vi.fn((url) => {
    const u = String(url)
    if (u.includes('/api/allocation/plan')) {
      return Promise.resolve({
        ok: planOk,
        status: planStatus,
        json: () => Promise.resolve(plan ?? {}),
      })
    }
    // The deploy affordance only calls /api/allocation/plan; any other URL is
    // unexpected in these tests.
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

describe('CoachConsult — deploy-my-cash affordance (Story 10.2)', () => {
  it('populates the order controls (side/amount/symbol/market) on a deploy plan, no submit', async () => {
    const fetchMock = stubFetch({ plan: DEPLOY_PLAN })
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

    // ONLY the read-only plan endpoint was hit — nothing was submitted (no
    // /approve, no /recommend, no order placement).
    const urls = fetchMock.mock.calls.map(([u]) => String(u))
    expect(urls.some((u) => u.includes('/api/allocation/plan'))).toBe(true)
    expect(urls.some((u) => u.includes('/approve'))).toBe(false)
    expect(urls.some((u) => u.includes('/recommend'))).toBe(false)
    // The plan GET carries no method/body (read-only).
    const planCall = fetchMock.mock.calls.find(([u]) =>
      String(u).includes('/api/allocation/plan'),
    )
    const opts = planCall[1] ?? {}
    expect(opts.method ?? 'GET').toBe('GET')
    expect(opts.body).toBeUndefined()
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
    stubFetch({ plan: NO_TARGET })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-deploy-cash'))

    const note = await screen.findByTestId('coach-deploy-no-action')
    expect(note).toHaveTextContent(/pick a target mix first/i)
    expectCalm(note.textContent)

    // NOTHING populated — the controls stay empty.
    expect(screen.getByTestId('coach-symbol-input').value).toBe('')
    expect(screen.getByTestId('coach-amount-input').value).toBe('')
    expect(screen.getByTestId('coach-side-select').value).toBe('')
    // The populated note is NOT shown.
    expect(screen.queryByTestId('coach-deploy-populated')).not.toBeInTheDocument()
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
    stubFetch({ plan: DECIDE_RESERVE })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-deploy-cash'))

    const note = await screen.findByTestId('coach-deploy-no-action')
    expect(note).toHaveTextContent(/cash cushion first/i)
    expectCalm(note.textContent)
    expect(screen.getByTestId('coach-amount-input').value).toBe('')
  })

  it('fails calmly when the plan endpoint errors, populating nothing', async () => {
    stubFetch({ planOk: false, planStatus: 500 })
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
    stubFetch({ plan: BAD_ZERO })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-deploy-cash'))

    // Contract-drift insurance: a zero amount is refused rather than populating an
    // un-co-signable order — falls through to the calm failed note.
    const note = await screen.findByTestId('coach-deploy-failed')
    expectCalm(note.textContent)
    // NOTHING populated — controls stay empty, no populated note.
    expect(screen.getByTestId('coach-symbol-input').value).toBe('')
    expect(screen.getByTestId('coach-amount-input').value).toBe('')
    expect(screen.getByTestId('coach-side-select').value).toBe('')
    expect(screen.queryByTestId('coach-deploy-populated')).not.toBeInTheDocument()
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
    stubFetch({ plan: BAD_SYMBOL })
    renderConsult()

    fireEvent.click(screen.getByTestId('coach-deploy-cash'))

    const note = await screen.findByTestId('coach-deploy-failed')
    expectCalm(note.textContent)
    expect(screen.getByTestId('coach-symbol-input').value).toBe('')
    expect(screen.getByTestId('coach-amount-input').value).toBe('')
    expect(screen.queryByTestId('coach-deploy-populated')).not.toBeInTheDocument()
  })

  it('has calm copy on the deploy button itself', () => {
    stubFetch({ plan: DEPLOY_PLAN })
    renderConsult()
    const btn = screen.getByTestId('coach-deploy-cash')
    expect(btn).toHaveTextContent(/deploy your cash toward your target/i)
    expectCalm(btn.textContent)
  })
})
