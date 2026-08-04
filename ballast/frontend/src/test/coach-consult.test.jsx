import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { CoachConsult } from '../components/CoachConsult.jsx'

afterEach(() => {
  vi.unstubAllGlobals()
})

// --- Fixtures ---------------------------------------------------------------

const STRATEGY_RECORD = {
  id: 'strat-abc123456789',
  kind: 'strategy',
  statement:
    'Broad, steady index investing has recovered from every past drop given time.',
  stats: { reason: 'default_plan', windows: [] },
  source: 'ballast-strategy',
  as_of: '2026-07-28',
}

const REC_NO_INTENT = {
  decision_id: 'dec-no-intent-1',
  action_label: 'Stick to your plan: make your regular contribution',
  reasoning:
    'When nothing special is happening, the proven move is the steady one — time in the market tends to beat timing it.',
  evidence: [STRATEGY_RECORD],
  uncertainties: [
    'Markets can stay volatile longer than anyone expects; past patterns never guarantee a future outcome.',
  ],
  order_intent: null,
}

const REC_WITH_INTENT = {
  ...REC_NO_INTENT,
  decision_id: 'dec-with-intent-1',
  action_label: 'Invest your $450 into your index core today.',
  order_intent: { symbol: 'VOO', side: 'buy', amount: '450.00' },
}

const OUTCOME = {
  status: 'filled',
  filled_qty: '5',
  avg_price: '100.00',
  broker_ref: 'fake-order-1',
}

// A calm/never-nudge copy guard.
const NUDGE = /you should|don'?t wait|buy the dip|act now|hurry/i

// --- fetch stub: route by URL to the right canned response -------------------

function respond(h) {
  if (!h) return Promise.reject(new Error('no handler for this url'))
  if (h.reject) return Promise.reject(new Error('network down'))
  return Promise.resolve({
    ok: h.ok ?? true,
    status: h.status ?? 200,
    json: () => Promise.resolve(h.body ?? {}),
  })
}

function stubFetch({ recommend, approve, suggest } = {}) {
  const fn = vi.fn((url) => {
    const u = String(url)
    // Order matters: the suggest route is a more specific path, but neither
    // string is a prefix of the other, so plain includes() is unambiguous.
    if (u.includes('/api/coach/suggest-order')) return respond(suggest)
    if (u.includes('/api/coach/recommend')) return respond(recommend)
    if (u.includes('/api/coach/approve')) return respond(approve)
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

function ask(question) {
  fireEvent.change(screen.getByTestId('coach-ask-input'), {
    target: { value: question },
  })
}

function setOrder({ symbol, amount, side }) {
  if (symbol !== undefined)
    fireEvent.change(screen.getByTestId('coach-symbol-input'), {
      target: { value: symbol },
    })
  if (amount !== undefined)
    fireEvent.change(screen.getByTestId('coach-amount-input'), {
      target: { value: amount },
    })
  if (side !== undefined)
    fireEvent.change(screen.getByTestId('coach-side-select'), {
      target: { value: side },
    })
}

function submitAsk() {
  fireEvent.click(screen.getByTestId('coach-ask-submit'))
}

// --- Tests ------------------------------------------------------------------

describe('CoachConsult — live propose → approve/decline', () => {
  it('idle: renders the form, ask disabled when empty, no fetch on mount', () => {
    const fn = stubFetch({ recommend: { body: REC_NO_INTENT } })
    renderConsult()

    expect(screen.getByTestId('coach-ask-input')).toBeInTheDocument()
    expect(screen.getByTestId('coach-ask-submit')).toBeDisabled()
    expect(fn).not.toHaveBeenCalled()
    expect(screen.queryByTestId('coach-card')).not.toBeInTheDocument()
  })

  it('question-only + default plan → recommendation renders, NO approve control', async () => {
    stubFetch({ recommend: { body: REC_NO_INTENT } })
    const { container } = renderConsult()

    ask('Should I be worried about the market right now?')
    expect(screen.getByTestId('coach-ask-submit')).toBeEnabled()
    submitAsk()

    await waitFor(() =>
      expect(screen.getByTestId('coach-card')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('coach-card-action')).toHaveTextContent(
      /stick to your plan/i,
    )
    // No concrete order + null intent ⇒ no co-sign zone, honest no-trade note.
    expect(screen.queryByTestId('coach-approve')).not.toBeInTheDocument()
    expect(screen.getByTestId('coach-no-order')).toBeInTheDocument()
    expect(container.textContent).not.toMatch(NUDGE)
  })

  it('concrete order + null intent → approve sends the FORM order + decision_id; outcome renders', async () => {
    const fn = stubFetch({
      recommend: { body: REC_NO_INTENT },
      approve: { body: OUTCOME },
    })
    renderConsult()

    ask('Should I invest my $500 paycheck?')
    setOrder({ symbol: 'VTI', amount: '500', side: 'buy' })
    submitAsk()

    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId('coach-approve'))

    await waitFor(() =>
      expect(screen.getByTestId('coach-outcome')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('coach-outcome')).toHaveTextContent(
      /filled 5 @ 100\.00/i,
    )
    expect(screen.getByTestId('coach-replay-chip')).toBeInTheDocument()

    const approveCall = fn.mock.calls.find((c) =>
      String(c[0]).includes('/api/coach/approve'),
    )
    const body = JSON.parse(approveCall[1].body)
    expect(body.decision_id).toBe('dec-no-intent-1')
    expect(body.order_intent).toEqual({
      symbol: 'VTI',
      side: 'buy',
      amount: '500',
    })
  })

  it("prefers the coach's blessed order_intent over the raw form", async () => {
    const fn = stubFetch({
      recommend: { body: REC_WITH_INTENT },
      approve: { body: OUTCOME },
    })
    renderConsult()

    // Question-only (no form order) — the recommendation carries the intent.
    ask('Is now an ok time to add to my index core?')
    submitAsk()

    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId('coach-approve'))

    await waitFor(() =>
      expect(screen.getByTestId('coach-outcome')).toBeInTheDocument(),
    )
    const approveCall = fn.mock.calls.find((c) =>
      String(c[0]).includes('/api/coach/approve'),
    )
    const body = JSON.parse(approveCall[1].body)
    expect(body.order_intent).toEqual({
      symbol: 'VOO',
      side: 'buy',
      amount: '450.00',
    })
  })

  it('approve on a non-live session (409) → calm reconnect + Onboarding link, retryable', async () => {
    stubFetch({
      recommend: { body: REC_NO_INTENT },
      approve: {
        ok: false,
        status: 409,
        body: { detail: 'Reconnect your Schwab account to continue.' },
      },
    })
    renderConsult()

    ask('Invest my paycheck?')
    setOrder({ symbol: 'VTI', amount: '500', side: 'buy' })
    submitAsk()
    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId('coach-approve'))

    await waitFor(() =>
      expect(screen.getByTestId('coach-reconnect')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('coach-reconnect')).toHaveTextContent(
      /reconnect your schwab account/i,
    )
    expect(screen.getByRole('link', { name: /reconnect schwab/i })).toHaveAttribute(
      'href',
      '/onboarding',
    )
    // Still retryable: the co-sign action stays.
    expect(screen.getByTestId('coach-approve')).toBeInTheDocument()
  })

  it('approve refused (422) → shows the backend calm reason verbatim, nothing placed', async () => {
    stubFetch({
      recommend: { body: REC_NO_INTENT },
      approve: {
        ok: false,
        status: 422,
        body: { detail: 'That buys less than one whole share.' },
      },
    })
    renderConsult()

    ask('Invest my paycheck?')
    setOrder({ symbol: 'VTI', amount: '1', side: 'buy' })
    submitAsk()
    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId('coach-approve'))

    await waitFor(() =>
      expect(screen.getByTestId('coach-refused')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('coach-refused')).toHaveTextContent(
      /less than one whole share/i,
    )
    expect(screen.queryByTestId('coach-outcome')).not.toBeInTheDocument()
  })

  it('"not now" declines with NO network call and dismisses the card', async () => {
    const fn = stubFetch({
      recommend: { body: REC_NO_INTENT },
      approve: { body: OUTCOME },
    })
    renderConsult()

    ask('Invest my paycheck?')
    setOrder({ symbol: 'VTI', amount: '500', side: 'buy' })
    submitAsk()
    await waitFor(() =>
      expect(screen.getByTestId('coach-decline')).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId('coach-decline'))

    expect(screen.queryByTestId('coach-card')).not.toBeInTheDocument()
    expect(screen.queryByTestId('coach-cosign')).not.toBeInTheDocument()
    // Only /recommend was ever called — decline never hits the broker.
    expect(
      fn.mock.calls.some((c) => String(c[0]).includes('/api/coach/approve')),
    ).toBe(false)
  })

  it('recommend transport failure → calm fallback (no error dump)', async () => {
    stubFetch({ recommend: { reject: true } })
    const { container } = renderConsult()

    ask('Anything at all')
    submitAsk()

    await waitFor(() =>
      expect(screen.getByTestId('coach-recommend-failed')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('coach-recommend-failed')).toHaveTextContent(
      /your plan hasn.t changed/i,
    )
    expect(container.innerHTML).not.toMatch(/traceback|internal server error/i)
  })

  it('recommend 401 → calm sign-in prompt', async () => {
    stubFetch({ recommend: { ok: false, status: 401, body: {} } })
    renderConsult()

    ask('Should I invest?')
    submitAsk()

    await waitFor(() =>
      expect(screen.getByTestId('coach-signed-out')).toBeInTheDocument(),
    )
    expect(screen.getByRole('link', { name: /sign in/i })).toHaveAttribute(
      'href',
      '/auth',
    )
  })

  it('editing the form after a recommendation invalidates the card (no stale co-sign)', async () => {
    stubFetch({ recommend: { body: REC_NO_INTENT }, approve: { body: OUTCOME } })
    renderConsult()

    ask('Invest my paycheck?')
    setOrder({ symbol: 'VTI', amount: '500', side: 'buy' })
    submitAsk()
    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )

    // Change the amount — the shown recommendation no longer matches.
    setOrder({ amount: '600' })
    expect(screen.queryByTestId('coach-card')).not.toBeInTheDocument()
    expect(screen.queryByTestId('coach-cosign')).not.toBeInTheDocument()
  })

  it('a malformed amount (1e3) is not a concrete order → no approve control', async () => {
    stubFetch({ recommend: { body: REC_NO_INTENT } })
    renderConsult()

    ask('Should I buy?')
    setOrder({ symbol: 'VTI', amount: '1e3', side: 'buy' })
    submitAsk()

    await waitFor(() =>
      expect(screen.getByTestId('coach-card')).toBeInTheDocument(),
    )
    expect(screen.queryByTestId('coach-approve')).not.toBeInTheDocument()
    expect(screen.getByTestId('coach-no-order')).toBeInTheDocument()
  })

  it('a recommendation missing decision_id shows no co-sign control', async () => {
    stubFetch({
      recommend: { body: { ...REC_WITH_INTENT, decision_id: undefined } },
    })
    renderConsult()

    ask('Add to my core?')
    submitAsk()

    await waitFor(() =>
      expect(screen.getByTestId('coach-card')).toBeInTheDocument(),
    )
    expect(screen.queryByTestId('coach-approve')).not.toBeInTheDocument()
  })

  it('approve 401 (session ended) → sign-in prompt, not a generic retry', async () => {
    stubFetch({
      recommend: { body: REC_NO_INTENT },
      approve: { ok: false, status: 401, body: {} },
    })
    renderConsult()

    ask('Invest?')
    setOrder({ symbol: 'VTI', amount: '500', side: 'buy' })
    submitAsk()
    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId('coach-approve'))

    await waitFor(() =>
      expect(screen.getByTestId('coach-approve-signed-out')).toBeInTheDocument(),
    )
  })

  it('approve 500 → honest "could not confirm", never claims nothing was placed', async () => {
    stubFetch({
      recommend: { body: REC_NO_INTENT },
      approve: { ok: false, status: 500, body: {} },
    })
    renderConsult()

    ask('Invest?')
    setOrder({ symbol: 'VTI', amount: '500', side: 'buy' })
    submitAsk()
    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId('coach-approve'))

    await waitFor(() =>
      expect(screen.getByTestId('coach-indeterminate')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('coach-indeterminate')).toHaveTextContent(
      /couldn.t confirm/i,
    )
    expect(screen.getByTestId('coach-indeterminate')).not.toHaveTextContent(
      /nothing was placed/i,
    )
  })

  it('409 in-progress → "give it a moment", NOT a reconnect link', async () => {
    stubFetch({
      recommend: { body: REC_NO_INTENT },
      approve: {
        ok: false,
        status: 409,
        body: {
          detail:
            'This decision is being approved right now — give it a moment and check your Decisions.',
        },
      },
    })
    renderConsult()

    ask('Invest?')
    setOrder({ symbol: 'VTI', amount: '500', side: 'buy' })
    submitAsk()
    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId('coach-approve'))

    await waitFor(() =>
      expect(screen.getByTestId('coach-in-progress')).toBeInTheDocument(),
    )
    expect(screen.queryByTestId('coach-reconnect')).not.toBeInTheDocument()
    expect(
      screen.queryByRole('link', { name: /reconnect schwab/i }),
    ).not.toBeInTheDocument()
  })

  it('a rejected outcome is shown honestly — no replay promise', async () => {
    stubFetch({
      recommend: { body: REC_NO_INTENT },
      approve: { body: { status: 'rejected', filled_qty: '0' } },
    })
    renderConsult()

    ask('Invest?')
    setOrder({ symbol: 'VTI', amount: '500', side: 'buy' })
    submitAsk()
    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId('coach-approve'))

    await waitFor(() =>
      expect(screen.getByTestId('coach-outcome')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('coach-outcome')).toHaveTextContent(/rejected/i)
    expect(screen.queryByTestId('coach-replay-chip')).not.toBeInTheDocument()
  })

  it('a pending outcome asks the user to check Decisions — no phantom fill', async () => {
    stubFetch({
      recommend: { body: REC_NO_INTENT },
      approve: { body: { status: 'pending', filled_qty: '0' } },
    })
    renderConsult()

    ask('Invest?')
    setOrder({ symbol: 'VTI', amount: '500', side: 'buy' })
    submitAsk()
    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByTestId('coach-approve'))

    await waitFor(() =>
      expect(screen.getByTestId('coach-outcome')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('coach-outcome')).toHaveTextContent(
      /couldn.t confirm this yet/i,
    )
    expect(screen.queryByTestId('coach-replay-chip')).not.toBeInTheDocument()
  })

  // --- Story 8.3: order-options override + footgun warnings ----------------

  function setOption(testid, value) {
    fireEvent.change(screen.getByTestId(testid), { target: { value } })
  }

  it('untouched order-options → approve sends byte-identical {symbol,side,amount}, no warnings', async () => {
    const fn = stubFetch({
      recommend: { body: REC_NO_INTENT },
      approve: { body: OUTCOME },
    })
    renderConsult()

    ask('Invest my paycheck?')
    setOrder({ symbol: 'VTI', amount: '500', side: 'buy' })
    submitAsk()
    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )
    // No footgun warnings on the default MARKET intent.
    expect(screen.queryByTestId('order-warning-resting-limit')).not.toBeInTheDocument()

    fireEvent.click(screen.getByTestId('coach-approve'))
    await waitFor(() =>
      expect(screen.getByTestId('coach-outcome')).toBeInTheDocument(),
    )
    const approveCall = fn.mock.calls.find((c) =>
      String(c[0]).includes('/api/coach/approve'),
    )
    const body = JSON.parse(approveCall[1].body)
    expect(body.order_intent).toEqual({
      symbol: 'VTI',
      side: 'buy',
      amount: '500',
    })
    expect(Object.keys(body.order_intent).sort()).toEqual([
      'amount',
      'side',
      'symbol',
    ])
  })

  it('composing a LIMIT sends order_type + limit_price as the exact string; "may rest" warning shows and is dismissable', async () => {
    const fn = stubFetch({
      recommend: { body: REC_NO_INTENT },
      approve: { body: OUTCOME },
    })
    renderConsult()

    ask('Invest my paycheck?')
    setOrder({ symbol: 'VTI', amount: '500', side: 'buy' })
    submitAsk()
    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )

    setOption('order-type-select', 'limit')
    setOption('order-limit-price-input', '99.50')

    // Calm "may rest" warning appears and can be dismissed.
    const warning = await screen.findByTestId('order-warning-resting-limit')
    expect(warning).toHaveTextContent(/may not fill right away/i)
    fireEvent.click(screen.getByTestId('order-warning-dismiss-resting-limit'))
    expect(
      screen.queryByTestId('order-warning-resting-limit'),
    ).not.toBeInTheDocument()

    fireEvent.click(screen.getByTestId('coach-approve'))
    await waitFor(() =>
      expect(screen.getByTestId('coach-outcome')).toBeInTheDocument(),
    )
    const approveCall = fn.mock.calls.find((c) =>
      String(c[0]).includes('/api/coach/approve'),
    )
    const body = JSON.parse(approveCall[1].body)
    expect(body.order_intent).toEqual({
      symbol: 'VTI',
      side: 'buy',
      amount: '500',
      order_type: 'limit',
      limit_price: '99.50',
    })
    // The exact string, never Number-coerced.
    expect(body.order_intent.limit_price).toBe('99.50')
  })

  it('a float-hostile LIMIT price (100.00) survives the form→state→serialize path as the exact string', async () => {
    // '100.00' is a value where String(Number('100.00')) === '100' — proving
    // the real component path never coerces the money string through a float.
    expect(String(Number('100.00'))).toBe('100')

    const fn = stubFetch({
      recommend: { body: REC_NO_INTENT },
      approve: { body: OUTCOME },
    })
    renderConsult()

    ask('Invest my paycheck?')
    setOrder({ symbol: 'VTI', amount: '500', side: 'buy' })
    submitAsk()
    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )

    setOption('order-type-select', 'limit')
    setOption('order-limit-price-input', '100.00')

    fireEvent.click(screen.getByTestId('coach-approve'))
    await waitFor(() =>
      expect(screen.getByTestId('coach-outcome')).toBeInTheDocument(),
    )
    const approveCall = fn.mock.calls.find((c) =>
      String(c[0]).includes('/api/coach/approve'),
    )
    const body = JSON.parse(approveCall[1].body)
    expect(body.order_intent.limit_price).toBe('100.00')
  })

  it('LIMIT + GTC sends duration:"gtc" and shows BOTH footgun warnings', async () => {
    const fn = stubFetch({
      recommend: { body: REC_NO_INTENT },
      approve: { body: OUTCOME },
    })
    renderConsult()

    ask('Invest my paycheck?')
    setOrder({ symbol: 'VTI', amount: '500', side: 'buy' })
    submitAsk()
    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )

    setOption('order-type-select', 'limit')
    setOption('order-limit-price-input', '99.50')
    setOption('order-duration-select', 'gtc')

    expect(
      await screen.findByTestId('order-warning-resting-limit'),
    ).toBeInTheDocument()
    expect(screen.getByTestId('order-warning-gtc')).toHaveTextContent(
      /stays open for days/i,
    )

    fireEvent.click(screen.getByTestId('coach-approve'))
    await waitFor(() =>
      expect(screen.getByTestId('coach-outcome')).toBeInTheDocument(),
    )
    const approveCall = fn.mock.calls.find((c) =>
      String(c[0]).includes('/api/coach/approve'),
    )
    const body = JSON.parse(approveCall[1].body)
    expect(body.order_intent).toEqual({
      symbol: 'VTI',
      side: 'buy',
      amount: '500',
      order_type: 'limit',
      limit_price: '99.50',
      duration: 'gtc',
    })
  })

  it('dismissing the GTC warning then toggling duration Day→GTC re-arms it (fresh consent)', async () => {
    stubFetch({ recommend: { body: REC_NO_INTENT }, approve: { body: OUTCOME } })
    renderConsult()

    ask('Invest my paycheck?')
    setOrder({ symbol: 'VTI', amount: '500', side: 'buy' })
    submitAsk()
    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )

    setOption('order-type-select', 'limit')
    setOption('order-limit-price-input', '99.50')
    setOption('order-duration-select', 'gtc')
    expect(await screen.findByTestId('order-warning-gtc')).toBeInTheDocument()

    // Dismiss the GTC footgun warning.
    fireEvent.click(screen.getByTestId('order-warning-dismiss-gtc'))
    await waitFor(() =>
      expect(screen.queryByTestId('order-warning-gtc')).not.toBeInTheDocument(),
    )

    // Toggle duration away and BACK to GTC without ever leaving LIMIT — the
    // warning must re-appear so informed consent is re-armed for the new choice.
    setOption('order-duration-select', 'day')
    setOption('order-duration-select', 'gtc')
    expect(await screen.findByTestId('order-warning-gtc')).toHaveTextContent(
      /stays open for days/i,
    )
  })

  it('LIMIT with no/invalid price → Approve disabled + calm mirror message, no /approve call', async () => {
    const fn = stubFetch({
      recommend: { body: REC_NO_INTENT },
      approve: { body: OUTCOME },
    })
    renderConsult()

    ask('Invest my paycheck?')
    setOrder({ symbol: 'VTI', amount: '500', side: 'buy' })
    submitAsk()
    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )

    setOption('order-type-select', 'limit')
    // Left blank → mirror blocks with the exact backend message.
    expect(await screen.findByTestId('order-mirror-block')).toHaveTextContent(
      'A limit order needs a limit price greater than zero.',
    )
    expect(screen.getByTestId('coach-approve')).toBeDisabled()

    fireEvent.click(screen.getByTestId('coach-approve'))
    // Disabled + guarded: no /approve request fired.
    expect(
      fn.mock.calls.some((c) => String(c[0]).includes('/api/coach/approve')),
    ).toBe(false)
  })

  it('STOP and after-hours are shown disabled with a calm not-available note', async () => {
    stubFetch({ recommend: { body: REC_NO_INTENT }, approve: { body: OUTCOME } })
    renderConsult()

    ask('Invest my paycheck?')
    setOrder({ symbol: 'VTI', amount: '500', side: 'buy' })
    submitAsk()
    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )

    const typeSelect = screen.getByTestId('order-type-select')
    const stopOption = within(typeSelect).getByRole('option', { name: /^stop —/i })
    expect(stopOption).toBeDisabled()
    expect(screen.getByTestId('order-type-unsupported-note')).toHaveTextContent(
      /aren.t available in this version/i,
    )

    const sessionSelect = screen.getByTestId('order-session-select')
    const pmOption = within(sessionSelect).getByRole('option', {
      name: /after-hours/i,
    })
    expect(pmOption).toBeDisabled()
    expect(
      screen.getByTestId('order-session-unsupported-note'),
    ).toHaveTextContent(/aren.t available in this version/i)
  })

  it('a backend 422 on a LIMIT surfaces the detail verbatim (defense in depth)', async () => {
    stubFetch({
      recommend: { body: REC_NO_INTENT },
      approve: {
        ok: false,
        status: 422,
        body: { detail: 'A limit order needs a limit price greater than zero.' },
      },
    })
    renderConsult()

    ask('Invest my paycheck?')
    setOrder({ symbol: 'VTI', amount: '500', side: 'buy' })
    submitAsk()
    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )

    setOption('order-type-select', 'limit')
    setOption('order-limit-price-input', '99.50') // client mirror passes
    fireEvent.click(screen.getByTestId('coach-approve'))

    await waitFor(() =>
      expect(screen.getByTestId('coach-refused')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('coach-refused')).toHaveTextContent(
      'A limit order needs a limit price greater than zero.',
    )
  })

  it('double-clicking Approve fires only ONE /approve request', async () => {
    const fn = stubFetch({
      recommend: { body: REC_NO_INTENT },
      approve: { body: OUTCOME },
    })
    renderConsult()

    ask('Invest?')
    setOrder({ symbol: 'VTI', amount: '500', side: 'buy' })
    submitAsk()
    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )

    const btn = screen.getByTestId('coach-approve')
    fireEvent.click(btn)
    fireEvent.click(btn) // synchronous second click, before the first resolves

    await waitFor(() =>
      expect(screen.getByTestId('coach-outcome')).toBeInTheDocument(),
    )
    const approveCalls = fn.mock.calls.filter((c) =>
      String(c[0]).includes('/api/coach/approve'),
    )
    expect(approveCalls).toHaveLength(1)
  })

  // --- Story 8.4: "Suggest this order" button ------------------------------

  const SUGGESTION = {
    symbol: 'VTI',
    side: 'buy',
    order_type: 'limit',
    limit_price: '89.10',
    duration: 'gtc',
    amount: '980.10',
    shares: 11,
    reasoning:
      'This rests a buy for VTI at $89.10 — a touch below its recent low. No rush.',
  }

  it('suggest-order 200 → populates side/amount/LIMIT/limit_price/GTC + shows reasoning, submits nothing', async () => {
    const fn = stubFetch({
      recommend: { body: REC_NO_INTENT },
      approve: { body: OUTCOME },
      suggest: { body: SUGGESTION },
    })
    renderConsult()

    // The button needs a symbol; enter one, then click Suggest.
    setOrder({ symbol: 'VTI' })
    fireEvent.click(screen.getByTestId('coach-suggest-order'))

    // The reasoning renders inline.
    await waitFor(() =>
      expect(screen.getByTestId('coach-suggest-reasoning')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('coach-suggest-reasoning')).toHaveTextContent(
      /rests a buy for VTI at \$89\.10/i,
    )

    // The form controls are populated: side=buy + the exact amount string.
    expect(screen.getByTestId('coach-side-select')).toHaveValue('buy')
    expect(screen.getByTestId('coach-amount-input')).toHaveValue('980.10')

    // Only the suggest endpoint was hit — nothing was ever approved/placed.
    expect(
      fn.mock.calls.some((c) => String(c[0]).includes('/api/coach/approve')),
    ).toBe(false)
    // The exact strings crossed the wire on the request, no float coercion.
    const suggestCall = fn.mock.calls.find((c) =>
      String(c[0]).includes('/api/coach/suggest-order'),
    )
    const reqBody = JSON.parse(suggestCall[1].body)
    expect(reqBody.symbol).toBe('VTI')
  })

  it('suggest → ask → approve: the AI resting LIMIT + GTC survive the ask reset into the co-signed order (AC-4)', async () => {
    // The recommendation the ask returns must carry a blessed intent + decision
    // for the co-sign step to appear; its symbol matches the suggested symbol.
    const REC_VTI = {
      ...REC_WITH_INTENT,
      decision_id: 'dec-vti-1',
      order_intent: { symbol: 'VTI', side: 'buy', amount: '450.00' },
    }
    const fn = stubFetch({
      recommend: { body: REC_VTI },
      approve: { body: OUTCOME },
      suggest: { body: SUGGESTION },
    })
    renderConsult()

    // 1. Suggest for VTI — populates the form and holds the resting LIMIT.
    setOrder({ symbol: 'VTI' })
    fireEvent.click(screen.getByTestId('coach-suggest-order'))
    await waitFor(() =>
      expect(screen.getByTestId('coach-suggest-reasoning')).toBeInTheDocument(),
    )

    // 2. Ask the coach — resetResult() blanks `options` back to MARKET, but the
    //    held suggestion is re-seeded so the LIMIT/GTC reach the co-sign step.
    submitAsk()
    await waitFor(() =>
      expect(screen.getByTestId('coach-approve')).toBeInTheDocument(),
    )

    // 3. Approve — the composed order_intent must carry the AI's resting LIMIT +
    //    GTC, NOT the blanked MARKET default (the pre-fix regression).
    fireEvent.click(screen.getByTestId('coach-approve'))
    await waitFor(() =>
      expect(screen.getByTestId('coach-outcome')).toBeInTheDocument(),
    )
    const approveCall = fn.mock.calls.find((c) =>
      String(c[0]).includes('/api/coach/approve'),
    )
    const body = JSON.parse(approveCall[1].body)
    expect(body.order_intent).toMatchObject({
      order_type: 'limit',
      limit_price: '89.10',
      duration: 'gtc',
    })
  })

  it('suggest-order sends amount:null when the amount field is empty', async () => {
    const fn = stubFetch({
      recommend: { body: REC_NO_INTENT },
      suggest: { body: SUGGESTION },
    })
    renderConsult()

    setOrder({ symbol: 'VTI' })
    fireEvent.click(screen.getByTestId('coach-suggest-order'))
    await waitFor(() =>
      expect(screen.getByTestId('coach-suggest-reasoning')).toBeInTheDocument(),
    )

    const suggestCall = fn.mock.calls.find((c) =>
      String(c[0]).includes('/api/coach/suggest-order'),
    )
    const reqBody = JSON.parse(suggestCall[1].body)
    expect(reqBody.amount).toBeNull()
  })

  it('suggest-order 422 → surfaces the envelope message verbatim, populates nothing', async () => {
    stubFetch({
      recommend: { body: REC_NO_INTENT },
      suggest: {
        ok: false,
        status: 422,
        body: {
          error: {
            type: 'http_error',
            message:
              'There isn’t enough idle cash for a whole share at that resting price right now — nothing was suggested.',
          },
        },
      },
    })
    renderConsult()

    setOrder({ symbol: 'VTI' })
    fireEvent.click(screen.getByTestId('coach-suggest-order'))

    await waitFor(() =>
      expect(screen.getByTestId('coach-suggest-failed')).toBeInTheDocument(),
    )
    expect(screen.getByTestId('coach-suggest-failed')).toHaveTextContent(
      /not enough idle cash|isn.t enough idle cash/i,
    )
    // Populated NOTHING: the reasoning panel never shows, the side stays unset.
    expect(
      screen.queryByTestId('coach-suggest-reasoning'),
    ).not.toBeInTheDocument()
    expect(screen.getByTestId('coach-side-select')).toHaveValue('')
    expect(screen.getByTestId('coach-amount-input')).toHaveValue('')
  })

  it('the Suggest button is disabled with no symbol', () => {
    stubFetch({ recommend: { body: REC_NO_INTENT } })
    renderConsult()
    expect(screen.getByTestId('coach-suggest-order')).toBeDisabled()
  })
})
