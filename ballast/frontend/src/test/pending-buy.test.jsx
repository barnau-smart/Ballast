import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { PendingBuyCard } from '../components/PendingBuyCard.jsx'

afterEach(() => {
  vi.unstubAllGlobals()
})

function renderCard() {
  return render(
    <MemoryRouter>
      <PendingBuyCard />
    </MemoryRouter>,
  )
}

function pending({ funds_ready }) {
  return {
    pending_buys: [
      {
        pending_buy_id: 'pb-1',
        buy_intent: { symbol: 'VTI', side: 'buy', amount: '700.00' },
        amount: '700.00',
        status: 'awaiting_funds',
        funds_ready,
        ready_to_trade: funds_ready ? '800.00' : '200.00',
        sell_decision_id: 'dec-sell',
        created_at: '2026-07-27T00:00:00+00:00',
        as_of: '2026-07-27T00:00:00+00:00',
      },
    ],
  }
}

function stubList(payload, { ok = true } = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve({ ok, status: ok ? 200 : 500, json: () => Promise.resolve(payload) })),
  )
}

const NUDGE_RE = /invest now|you should|don'?t wait|act now|hurry|last chance/i

describe('PendingBuyCard — durable, pull-only, resume only when funds_ready', () => {
  it('renders a waiting state (no resume) when funds are NOT ready', async () => {
    stubList(pending({ funds_ready: false }))
    const { container } = renderCard()

    await waitFor(() =>
      expect(screen.getByTestId('pending-buy-row')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('pending-buy-symbol')).toHaveTextContent('VTI')
    expect(screen.getByTestId('pending-buy-amount')).toHaveTextContent('$700.00')
    // The waiting (settle) state shows; NO resume control.
    expect(screen.getByTestId('pending-buy-waiting')).toBeInTheDocument()
    expect(screen.queryByTestId('pending-buy-resume')).not.toBeInTheDocument()
    // Data freshness surfaced.
    expect(screen.getByTestId('pending-buy-asof')).toHaveTextContent(/as of/i)
    // Calm, never-red, no FOMO.
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
    expect(container.textContent).not.toMatch(NUDGE_RE)
  })

  it('shows a resume control only when funds ARE ready', async () => {
    stubList(pending({ funds_ready: true }))
    const { container } = renderCard()

    await waitFor(() =>
      expect(screen.getByTestId('pending-buy-resume')).toBeInTheDocument(),
    )
    expect(screen.queryByTestId('pending-buy-waiting')).not.toBeInTheDocument()
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
    expect(container.textContent).not.toMatch(NUDGE_RE)
  })

  it('renders nothing when there are no pending buys (pull-only, no noise)', async () => {
    stubList({ pending_buys: [] })
    const { container } = renderCard()
    // Give the effect a tick; the card renders nothing.
    await waitFor(() => {
      expect(screen.queryByTestId('pending-buys')).not.toBeInTheDocument()
    })
    expect(container.textContent).toBe('')
  })

  it('resume → approve routes through /resume then /api/coach/approve', async () => {
    const calls = []
    const fetchMock = vi.fn((url, opts) => {
      calls.push([String(url), opts])
      if (String(url).endsWith('/api/cash/pending-buys')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(pending({ funds_ready: true })),
        })
      }
      if (String(url).includes('/resume')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () =>
            Promise.resolve({
              decision_id: 'dec-buy',
              order_intent: { symbol: 'VTI', side: 'buy', amount: '700.00' },
              pending_buy_id: 'pb-1',
            }),
        })
      }
      // /api/coach/approve
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ status: 'filled', filled_qty: '2' }),
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    renderCard()
    await waitFor(() =>
      expect(screen.getByTestId('pending-buy-resume')).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId('pending-buy-resume'))

    // After resume, the ready + approve control appears.
    await waitFor(() =>
      expect(screen.getByTestId('pending-buy-approve')).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId('pending-buy-approve'))

    await waitFor(() =>
      expect(screen.getByTestId('pending-buy-placed')).toBeInTheDocument(),
    )
    const urls = calls.map((c) => c[0])
    expect(urls.some((u) => /\/api\/cash\/pending-buys\/pb-1\/resume$/.test(u))).toBe(true)
    expect(urls.some((u) => /\/api\/coach\/approve$/.test(u))).toBe(true)
    // The approve carried the resumed buy decision + pre-filled intent.
    const approveCall = calls.find((c) => /\/api\/coach\/approve$/.test(c[0]))
    const body = JSON.parse(approveCall[1].body)
    expect(body.decision_id).toBe('dec-buy')
    expect(body.order_intent).toEqual({ symbol: 'VTI', side: 'buy', amount: '700.00' })
  })

  it('calmly refuses a not-yet-settled resume (409) without red', async () => {
    const fetchMock = vi.fn((url) => {
      if (String(url).endsWith('/api/cash/pending-buys')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: () => Promise.resolve(pending({ funds_ready: true })),
        })
      }
      // /resume returns a calm 409 (race: cash moved back below the amount).
      return Promise.resolve({
        ok: false,
        status: 409,
        json: () =>
          Promise.resolve({
            error: { type: 'conflict', message: 'Your cash hasn’t settled yet.' },
          }),
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    const { container } = renderCard()
    await waitFor(() =>
      expect(screen.getByTestId('pending-buy-resume')).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId('pending-buy-resume'))

    await waitFor(() =>
      expect(screen.getByTestId('pending-buy-message')).toHaveTextContent(
        /hasn’t settled yet/i,
      ),
    )
    expect(container.innerHTML).not.toMatch(/brand-red|accent-pink|line-red/)
  })
})
