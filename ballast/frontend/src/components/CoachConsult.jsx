import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiFetch } from '../lib/session.js'
import {
  DEFAULT_OPTIONS,
  buildOrderIntent,
  deriveWarnings,
  validateOrderMatrix,
} from '../lib/orderOptions.js'
import { CoachCard } from './CoachCard.jsx'
import './CoachConsult.css'
import './OrderOptions.css'

/**
 * The live coach consult (Story 4.11) — Epic 4's "emotional centerpiece"
 * (EXPERIENCE.md; mockups/coach-card.html). The interactive propose→approve
 * loop the SPA never had: ask the coach (`POST /api/coach/recommend`), read the
 * blessed recommendation (rendered by `CoachCard`), then either **Approve &
 * Co-sign** the order (`POST /api/coach/approve`) or say **not now** — in place.
 *
 * Presentation-only (AD-1): computes no money/market number; renders what the
 * backend blessed. Product contract (ratified): the order approved is the
 * USER'S stated order — `recommendation.order_intent ?? the order snapshotted
 * at ask time` — so a null recommendation intent (the default plan) does not
 * erase the order the user came in with (warn-not-block, Epic 4.5; explicit
 * approval is the gate, FR8/FR9). A consult with no concrete order shows
 * guidance only — NO approve control.
 *
 * INTEGRITY (review 2026-08-03): the co-signed order is a SNAPSHOT taken when
 * the recommendation was requested — never live form state — and editing any
 * field after a recommendation renders invalidates it (you must re-ask), so the
 * order placed always matches the card the user saw and its `decision_id`.
 *
 * Calm + honest + never-red: `/approve` needs a live session, so a 409 degrades
 * to a calm reconnect prompt (session) or a "give it a moment" note (a
 * concurrent approve in flight); a 422 shows the backend's calm reason; a 401
 * routes to sign-in. Only a `filled`/`partial` outcome is framed as a placed
 * position — `rejected`/`timeout`/`pending` are shown honestly and NEVER get
 * the replay promise. An indeterminate failure (network/5xx/unparseable) never
 * claims "nothing was placed"; it asks the user to check Decisions before
 * retrying (no duplicate-order nudge). Brand-red appears ONLY on the co-sign
 * action; a loss/outcome value is never red. Guards setState-after-unmount and
 * double-submit.
 */

// A clean positive decimal string (no exponent/hex/trailing-dot) — what the
// money wire contract accepts. `Number()` alone would pass "1e3"/"0x10"/"5.".
const DECIMAL_RE = /^\d+(\.\d+)?$/

// A concrete, placeable order from raw form fields, or null. The returned
// `amount` is the exact validated string that goes on the wire (validated ==
// sent — never a decimal float).
function parseOrder(symbol, amount, side) {
  const s = symbol.trim()
  const a = amount.trim()
  if (s === '' || !DECIMAL_RE.test(a) || Number(a) <= 0) return null
  if (side !== 'buy' && side !== 'sell') return null
  return { symbol: s, side, amount: a }
}

export function CoachConsult() {
  const [question, setQuestion] = useState('')
  const [symbol, setSymbol] = useState('')
  const [amount, setAmount] = useState('')
  const [side, setSide] = useState('') // '' | 'buy' | 'sell'

  // idle | thinking | ready | recommend-failed | signed-out
  const [phase, setPhase] = useState('idle')
  const [recommendation, setRecommendation] = useState(null)
  // Snapshot the recommendation was made for: { question, order: {…}|null }.
  const [submitted, setSubmitted] = useState(null)

  // idle | placing | placed | reconnect | in-progress | refused | signed-out
  // | indeterminate
  const [approve, setApprove] = useState('idle')
  const [approveMessage, setApproveMessage] = useState('')
  const [outcome, setOutcome] = useState(null)

  // Story 8.3 — the human's approve-time order-model override (progressive
  // disclosure). Defaults to the blessed MARKET order so the composed intent
  // stays byte-identical `{symbol, side, amount}`. `dismissed` holds the set of
  // footgun-warning kinds the user has read and set aside (informed consent).
  const [options, setOptions] = useState(DEFAULT_OPTIONS)
  const [dismissed, setDismissed] = useState([])

  // Story 8.4 — MasterB's "Suggest this order" button. The backend computes a
  // resting BUY LIMIT (GTC) deterministically and the LLM narrates it; on 200 we
  // populate the frozen 8.3 controls via the existing setters (side/amount/
  // options) and show the reasoning inline. On 422/409 we surface the calm
  // envelope message verbatim and populate NOTHING. `suggest` phases:
  // idle | suggesting | suggested | suggest-failed.
  const [suggest, setSuggest] = useState('idle')
  const [suggestMessage, setSuggestMessage] = useState('')
  const [suggestReasoning, setSuggestReasoning] = useState('')
  // Story 8.6 — backend-computed honesty lines shown beside the reasoning: the
  // banded fill-likelihood note (always present on a suggestion) and an optional
  // calm delayed-data note. Both are threaded through `pendingSuggestion` so they
  // survive the ask round-trip exactly like `reasoning`.
  const [suggestFillNote, setSuggestFillNote] = useState('')
  const [suggestStaleNote, setSuggestStaleNote] = useState('')
  // Story 8.4 — a computed AI suggestion held OUTSIDE `options` so it survives
  // the ask reset (`resetResult` blanks `options` back to MARKET). Re-seeded into
  // the co-sign `options` by `onAsk` so the resting LIMIT reaches /approve (AC-4).
  const [pendingSuggestion, setPendingSuggestion] = useState(null)
  const suggestingRef = useRef(false) // synchronous double-click guard

  const mounted = useRef(true)
  const placingRef = useRef(false) // synchronous double-submit guard
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const liveOrder = parseOrder(symbol, amount, side)
  // Something must be on the table to ask: a question OR a concrete order.
  const askDisabled =
    phase === 'thinking' || (question.trim() === '' && liveOrder === null)

  // The order to co-sign: the coach's blessed intent, else the SNAPSHOT taken
  // at ask time — never live form state. Null when there's nothing to approve.
  const orderIntent = recommendation
    ? recommendation.order_intent ?? submitted?.order ?? null
    : null

  function resetResult() {
    setRecommendation(null)
    setSubmitted(null)
    setApprove('idle')
    setApproveMessage('')
    setOutcome(null)
    // A fresh ask/decline returns the order-options override to the blessed
    // MARKET default and clears any dismissed footgun warnings.
    setOptions(DEFAULT_OPTIONS)
    setDismissed([])
    // Clear any prior suggest-order narration/message (a fresh ask starts clean).
    setSuggest('idle')
    setSuggestMessage('')
    setSuggestReasoning('')
    setSuggestFillNote('')
    setSuggestStaleNote('')
  }

  // Editing any field invalidates a shown recommendation — it no longer matches
  // the order/question on the card, so the user must re-ask before co-signing.
  function edit(setter) {
    return (value) => {
      if (recommendation !== null) {
        resetResult()
        setPhase('idle')
      }
      setter(value)
    }
  }

  async function readDetail(res) {
    try {
      const data = await res.json()
      return data?.detail ?? data?.message ?? ''
    } catch {
      return ''
    }
  }

  async function onAsk(event) {
    event.preventDefault()
    if (askDisabled) return
    const snapshot = { question, order: liveOrder }
    const validAmount = DECIMAL_RE.test(amount.trim()) ? amount.trim() : null
    // Capture any prior AI suggestion BEFORE resetResult() blanks `options`, so we
    // can re-seed the resting LIMIT into the co-sign step once the decision lands.
    const pending = pendingSuggestion
    setPhase('thinking')
    resetResult()
    try {
      const res = await apiFetch('/api/coach/recommend', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: symbol.trim() || null,
          question,
          amount: validAmount,
          side: side || null,
        }),
      })
      if (!mounted.current) return
      if (res.status === 401) {
        setPhase('signed-out')
        return
      }
      if (!res.ok) {
        setPhase('recommend-failed')
        return
      }
      const data = await res.json()
      if (!mounted.current) return
      setRecommendation(data)
      setSubmitted(snapshot)
      setPhase('ready')
      // Story 8.4 — carry a prior AI suggestion through the ask so its computed
      // resting LIMIT (GTC) survives to the co-sign step (AC-4). resetResult()
      // above blanked `options`; re-seed from the suggestion when it still matches
      // the asked symbol AND the recommendation is actually co-signable (a blessed
      // decision_id), then spend it (one-shot — further edits are the user's).
      if (pending && pending.symbol === symbol.trim().toUpperCase()) {
        if (data?.decision_id) {
          setOptions({
            ...DEFAULT_OPTIONS,
            order_type: 'limit',
            limit_price: pending.limit_price,
            duration: pending.duration,
          })
          setSuggestReasoning(pending.reasoning)
          // Story 8.6 — re-seed the honesty notes so they survive the ask reset
          // exactly like the reasoning.
          setSuggestFillNote(pending.fill_note ?? '')
          setSuggestStaleNote(pending.stale_note ?? '')
          setSuggest('suggested')
          setPendingSuggestion(null)
        }
        // else: the coach answered without an executable order — KEEP the held
        // suggestion so the next co-signable ask of this symbol still carries the
        // resting LIMIT (spending it here would resurrect the P1 MARKET downgrade).
      } else {
        // A different symbol (or none) — the held suggestion no longer applies to
        // what's on the table; discard it.
        setPendingSuggestion(null)
      }
    } catch {
      if (!mounted.current) return
      setPhase('recommend-failed')
    }
  }

  async function onApprove() {
    if (!recommendation?.decision_id || !orderIntent) return
    if (!matrix.ok) return // the client mirror blocks an invalid combination
    if (placingRef.current) return // synchronous double-click guard
    placingRef.current = true
    setApprove('placing')
    setApproveMessage('')
    try {
      const res = await apiFetch('/api/coach/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          decision_id: recommendation.decision_id,
          // Story 8.3: upgrade the blessed MARKET base with the human's
          // approve-time order-model override. MARKET+REGULAR+DAY stays
          // byte-identical `{symbol, side, amount}`.
          order_intent: buildOrderIntent(orderIntent, options),
        }),
      })
      if (!mounted.current) return
      if (res.ok) {
        let data
        try {
          data = await res.json()
        } catch {
          // 200 but unreadable body — the order may well have been placed.
          setApprove('indeterminate')
          return
        }
        if (!mounted.current) return
        setOutcome(data)
        setApprove('placed')
        return
      }
      const detail = await readDetail(res)
      if (!mounted.current) return
      if (res.status === 401) {
        setApprove('signed-out')
      } else if (res.status === 409) {
        // 409 has two causes: no live session (reconnect) vs a concurrent
        // approve of this decision already in flight (give it a moment).
        setApproveMessage(detail)
        setApprove(
          /moment|in progress|being approved|already/i.test(detail)
            ? 'in-progress'
            : 'reconnect',
        )
      } else if (res.status === 422) {
        // A deliberate pre-placement refusal — nothing was placed, retryable.
        setApproveMessage(detail)
        setApprove('refused')
      } else {
        // 5xx/404/other — the order MAY have been placed; never claim it wasn't.
        setApprove('indeterminate')
      }
    } catch {
      if (!mounted.current) return
      setApprove('indeterminate')
    } finally {
      placingRef.current = false
    }
  }

  // Story 8.4 — "Suggest this order": ask the backend to COMPUTE a resting BUY
  // LIMIT (GTC) and NARRATE it, then populate the frozen 8.3 controls via the
  // existing setters. On 200 we set side=buy + the exact amount/limit_price
  // strings + LIMIT + GTC and show the reasoning inline. On 422/409 we surface
  // the calm envelope reason verbatim and populate NOTHING. Nothing executes —
  // the human still runs the /approve co-sign path.
  async function onSuggest() {
    if (suggestingRef.current) return // synchronous double-click guard
    if (phase === 'thinking') return // don't race an in-flight ask
    const s = symbol.trim()
    if (s === '') {
      setSuggestMessage('Enter a symbol to suggest an order.')
      setSuggest('suggest-failed')
      return
    }
    suggestingRef.current = true
    setSuggest('suggesting')
    setSuggestMessage('')
    setSuggestReasoning('')
    setSuggestFillNote('')
    setSuggestStaleNote('')
    setPendingSuggestion(null) // a fresh suggest supersedes any prior one
    // A shown recommendation no longer matches once we repopulate the form.
    if (recommendation !== null) {
      resetResult()
      setPhase('idle')
    }
    try {
      const validAmount = DECIMAL_RE.test(amount.trim()) ? amount.trim() : null
      const res = await apiFetch('/api/coach/suggest-order', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: s, amount: validAmount }),
      })
      if (!mounted.current) return
      if (res.ok) {
        let data
        try {
          data = await res.json()
        } catch {
          setSuggestMessage('We couldn’t read that suggestion — try again.')
          setSuggest('suggest-failed')
          return
        }
        if (!mounted.current) return
        // Defense in depth: the endpoint's response_model guarantees string
        // money fields, but never write literal "undefined" into a money input.
        if (
          typeof data.amount !== 'string' ||
          typeof data.limit_price !== 'string'
        ) {
          setSuggestMessage('We couldn’t read that suggestion — try again.')
          setSuggest('suggest-failed')
          return
        }
        // Populate the 8.3 controls via the existing setters (no fork of
        // orderOptions.js). limit_price/amount travel as the EXACT strings.
        setSide('buy')
        setAmount(data.amount)
        // Seed from a clean DEFAULT_OPTIONS base (mirrors onAsk) so no stale
        // option key (a leftover session/stop from a prior card) can ride along
        // into the suggested resting LIMIT.
        setOptions({
          ...DEFAULT_OPTIONS,
          order_type: 'limit',
          limit_price: data.limit_price,
          duration: 'gtc',
        })
        // Hold the suggestion outside `options` so the resting LIMIT survives the
        // ask reset and reaches co-sign (AC-4). Keyed by the backend's normalized
        // (upper-cased) symbol so a later ask for a DIFFERENT symbol won't seed it.
        setPendingSuggestion({
          symbol: String(data.symbol),
          limit_price: data.limit_price,
          duration: 'gtc',
          reasoning: data.reasoning ?? '',
          // Story 8.6 — carry the honesty notes so they survive the ask reset.
          fill_note: data.fill_note ?? '',
          stale_note: data.stale_note ?? '',
        })
        setSuggestReasoning(data.reasoning ?? '')
        setSuggestFillNote(data.fill_note ?? '')
        setSuggestStaleNote(data.stale_note ?? '')
        setSuggest('suggested')
        return
      }
      // Calm refusal — surface the backend envelope message verbatim (the
      // 8.3-hardened shape), populate NOTHING.
      let message = ''
      try {
        const data = await res.json()
        message = data?.error?.message ?? data?.detail ?? data?.message ?? ''
      } catch {
        message = ''
      }
      if (!mounted.current) return
      if (res.status === 401) {
        setSuggestMessage('Sign in to suggest an order.')
      } else {
        setSuggestMessage(message)
      }
      setSuggest('suggest-failed')
    } catch {
      if (!mounted.current) return
      setSuggestMessage('We couldn’t reach the coach just now — try again.')
      setSuggest('suggest-failed')
    } finally {
      suggestingRef.current = false
    }
  }

  // "not now" — always equally easy, never penalized (EXPERIENCE.md). No network
  // call; dismiss the card back to a calm ready-to-ask state, keeping the form.
  function onDecline() {
    resetResult()
    setPendingSuggestion(null)
    setPhase('idle')
  }

  const showCosign =
    phase === 'ready' &&
    orderIntent !== null &&
    Boolean(recommendation?.decision_id) &&
    approve !== 'placed'
  const placedPosition =
    outcome && (outcome.status === 'filled' || outcome.status === 'partial')

  // Story 8.3 — the client-side matrix mirror + footgun warnings, derived live
  // from the human's approve-time options. The mirror gates Approve (defense in
  // depth; the backend stays authoritative); warnings the user has dismissed
  // drop out but never block.
  const matrix = validateOrderMatrix(options)
  const warnings = deriveWarnings(options).filter(
    (w) => !dismissed.includes(w.kind),
  )
  const isLimit = options.order_type === 'limit'

  function setOption(patch) {
    setOptions((prev) => ({ ...prev, ...patch }))
  }

  function dismissWarning(kind) {
    setDismissed((prev) => (prev.includes(kind) ? prev : [...prev, kind]))
  }

  return (
    <div className="ballast-consult" data-testid="coach-consult">
      <form className="ballast-form ballast-consult__form" onSubmit={onAsk}>
        <label className="ballast-form__label" htmlFor="coach-ask-input">
          Ask the coach
        </label>
        <textarea
          id="coach-ask-input"
          className="ballast-form__input"
          data-testid="coach-ask-input"
          value={question}
          rows={2}
          maxLength={500}
          placeholder="e.g. “Should I invest my $500 paycheck? The market feels scary right now.”"
          onChange={(e) => edit(setQuestion)(e.target.value)}
        />

        <div className="ballast-consult__order">
          <div className="ballast-consult__field">
            <label className="ballast-form__label" htmlFor="coach-symbol-input">
              Symbol (optional)
            </label>
            <input
              id="coach-symbol-input"
              className="ballast-form__input"
              data-testid="coach-symbol-input"
              type="text"
              autoComplete="off"
              value={symbol}
              placeholder="VTI"
              onChange={(e) => edit(setSymbol)(e.target.value)}
            />
          </div>
          <div className="ballast-consult__field">
            <label className="ballast-form__label" htmlFor="coach-amount-input">
              Amount (optional)
            </label>
            <input
              id="coach-amount-input"
              className="ballast-form__input"
              data-testid="coach-amount-input"
              type="text"
              inputMode="decimal"
              value={amount}
              placeholder="500"
              onChange={(e) => edit(setAmount)(e.target.value)}
            />
          </div>
          <div className="ballast-consult__field">
            <label className="ballast-form__label" htmlFor="coach-side-select">
              Side (optional)
            </label>
            <select
              id="coach-side-select"
              className="ballast-form__input"
              data-testid="coach-side-select"
              value={side}
              onChange={(e) => edit(setSide)(e.target.value)}
            >
              <option value="">—</option>
              <option value="buy">buy</option>
              <option value="sell">sell</option>
            </select>
          </div>
        </div>

        <button
          type="submit"
          className="ballast-form__submit"
          data-testid="coach-ask-submit"
          disabled={askDisabled}
        >
          {phase === 'thinking' ? 'Looking at the record…' : 'Ask the coach'}
        </button>

        {/* Story 8.4 — MasterB's optional "Suggest this order" button. Computes a
            calm resting BUY LIMIT (GTC) and populates the form; never executes. */}
        <button
          type="button"
          className="ballast-consult__suggest"
          data-testid="coach-suggest-order"
          onClick={onSuggest}
          disabled={
            suggest === 'suggesting' ||
            phase === 'thinking' ||
            symbol.trim() === ''
          }
        >
          {suggest === 'suggesting' ? 'Suggesting…' : 'Suggest this order'}
        </button>
      </form>

      {suggest === 'suggested' ? (
        <div
          className="ballast-consult__suggest-panel"
          data-testid="coach-suggest-reasoning"
          role="status"
        >
          {suggestReasoning}
          {/* Story 8.6 — the backend-computed honesty lines: the banded
              fill-likelihood note (always present) and an optional calm
              delayed-data note, shown as calm lines beside the reasoning. */}
          {suggestFillNote ? (
            <p
              className="ballast-consult__suggest-fill-note"
              data-testid="coach-suggest-fill-note"
            >
              {suggestFillNote}
            </p>
          ) : null}
          {suggestStaleNote ? (
            <p
              className="ballast-consult__suggest-stale-note"
              data-testid="coach-suggest-stale-note"
            >
              {suggestStaleNote}
            </p>
          ) : null}
        </div>
      ) : null}

      {suggest === 'suggest-failed' ? (
        <p
          className="ballast-consult__note"
          data-testid="coach-suggest-failed"
          role="status"
        >
          {suggestMessage ||
            'We couldn’t suggest an order just now — try again in a moment.'}
        </p>
      ) : null}

      {phase === 'thinking' ? (
        <p
          className="ballast-consult__note"
          data-testid="coach-thinking"
          role="status"
        >
          Looking at the record…
        </p>
      ) : null}

      {phase === 'signed-out' ? (
        <p
          className="ballast-consult__note"
          data-testid="coach-signed-out"
          role="status"
        >
          Sign in to ask the coach.{' '}
          <Link className="ballast-consult__link" to="/auth">
            Sign in
          </Link>
        </p>
      ) : null}

      {phase === 'recommend-failed' ? (
        <p
          className="ballast-consult__note"
          data-testid="coach-recommend-failed"
          role="status"
        >
          We couldn’t reach the coach just now. Your plan hasn’t changed — try
          again in a moment.
        </p>
      ) : null}

      {phase === 'ready' && recommendation ? (
        <div className="ballast-consult__result">
          <CoachCard
            recommendation={recommendation}
            question={submitted?.question}
          />

          {orderIntent === null ? (
            <p className="ballast-consult__note" data-testid="coach-no-order">
              No trade to approve here — the coach isn’t recommending one.
            </p>
          ) : null}

          {showCosign ? (
            <div className="ballast-consult__cosign" data-testid="coach-cosign">
              <div
                className="ballast-order-options"
                data-testid="order-options"
              >
                <p className="ballast-order-options__heading">Order options</p>
                <div className="ballast-order-options__row">
                  <div className="ballast-order-options__field">
                    <label
                      className="ballast-order-options__label"
                      htmlFor="order-type-select"
                    >
                      Order type
                    </label>
                    <select
                      id="order-type-select"
                      className="ballast-form__input"
                      data-testid="order-type-select"
                      value={options.order_type}
                      onChange={(e) => {
                        const order_type = e.target.value
                        // Guard: the disabled STOP/STOP_LIMIT options are
                        // unsupported — ignore a change to one via any path so
                        // they're truly unselectable.
                        if (
                          order_type === 'stop' ||
                          order_type === 'stop_limit'
                        )
                          return
                        // Leaving LIMIT clears the limit-only fields so a stale
                        // price/GTC can't linger on a MARKET intent, AND clears
                        // dismissed footgun warnings so re-entering LIMIT
                        // re-shows them for fresh informed consent.
                        setOptions((prev) => ({
                          ...prev,
                          order_type,
                          ...(order_type === 'limit'
                            ? {}
                            : { limit_price: '', duration: 'day' }),
                        }))
                        if (order_type !== 'limit') setDismissed([])
                      }}
                    >
                      <option value="market">Market</option>
                      <option value="limit">Limit</option>
                      <option value="stop" disabled>
                        Stop — not available in this version
                      </option>
                      <option value="stop_limit" disabled>
                        Stop-limit — not available in this version
                      </option>
                    </select>
                    <p
                      className="ballast-order-options__note"
                      data-testid="order-type-unsupported-note"
                    >
                      Stop and stop-limit orders aren’t available in this
                      version.
                    </p>
                  </div>

                  {isLimit ? (
                    <div className="ballast-order-options__field">
                      <label
                        className="ballast-order-options__label"
                        htmlFor="order-limit-price-input"
                      >
                        Limit price
                      </label>
                      <input
                        id="order-limit-price-input"
                        className="ballast-form__input"
                        data-testid="order-limit-price-input"
                        type="text"
                        inputMode="decimal"
                        value={options.limit_price}
                        placeholder="99.50"
                        onChange={(e) =>
                          setOption({ limit_price: e.target.value })
                        }
                      />
                    </div>
                  ) : null}

                  {isLimit ? (
                    <div className="ballast-order-options__field">
                      <label
                        className="ballast-order-options__label"
                        htmlFor="order-duration-select"
                      >
                        Time in force
                      </label>
                      <select
                        id="order-duration-select"
                        className="ballast-form__input"
                        data-testid="order-duration-select"
                        value={options.duration}
                        onChange={(e) => {
                          setOption({ duration: e.target.value })
                          // Re-arm the GTC footgun warning on any duration
                          // toggle so a dismiss → Day → GTC round-trip (without
                          // ever leaving LIMIT) re-shows it for fresh consent.
                          setDismissed((prev) =>
                            prev.filter((k) => k !== 'gtc'),
                          )
                        }}
                      >
                        <option value="day">Day</option>
                        <option value="gtc">Good ’til canceled (GTC)</option>
                      </select>
                    </div>
                  ) : null}

                  <div className="ballast-order-options__field">
                    <label
                      className="ballast-order-options__label"
                      htmlFor="order-session-select"
                    >
                      Session
                    </label>
                    <select
                      id="order-session-select"
                      className="ballast-form__input"
                      data-testid="order-session-select"
                      value={options.session}
                      onChange={(e) => {
                        const session = e.target.value
                        // Guard: AM/PM extended-hours sessions are unsupported —
                        // ignore a change to one so they're truly unselectable.
                        if (session === 'am' || session === 'pm') return
                        setOption({ session })
                      }}
                    >
                      <option value="regular">Regular</option>
                      <option value="am" disabled>
                        Pre-market — not available in this version
                      </option>
                      <option value="pm" disabled>
                        After-hours — not available in this version
                      </option>
                    </select>
                    <p
                      className="ballast-order-options__note"
                      data-testid="order-session-unsupported-note"
                    >
                      Pre-market and after-hours sessions aren’t available in
                      this version.
                    </p>
                  </div>
                </div>

                {warnings.map((w) => (
                  <div
                    key={w.kind}
                    className="ballast-order-options__warning"
                    data-testid={`order-warning-${w.kind}`}
                    role="status"
                  >
                    <p className="ballast-order-options__warning-text">
                      {w.message}
                    </p>
                    <button
                      type="button"
                      className="ballast-order-options__dismiss"
                      data-testid={`order-warning-dismiss-${w.kind}`}
                      onClick={() => dismissWarning(w.kind)}
                    >
                      dismiss
                    </button>
                  </div>
                ))}

                {!matrix.ok ? (
                  <p
                    id="order-mirror-block"
                    className="ballast-order-options__mirror"
                    data-testid="order-mirror-block"
                    role="status"
                  >
                    {matrix.detail}
                  </p>
                ) : null}
              </div>

              <p className="ballast-consult__cosign-note">
                <span aria-hidden="true">✎</span> I’ll put my name on this with
                you.
              </p>
              <div className="ballast-consult__cosign-actions">
                <button
                  type="button"
                  className="ballast-consult__approve"
                  data-testid="coach-approve"
                  onClick={onApprove}
                  disabled={approve === 'placing' || !matrix.ok}
                  aria-describedby={
                    !matrix.ok ? 'order-mirror-block' : undefined
                  }
                >
                  {approve === 'placing' ? 'Placing…' : 'Approve & Co-sign'}
                </button>
                <button
                  type="button"
                  className="ballast-consult__decline"
                  data-testid="coach-decline"
                  onClick={onDecline}
                >
                  not now
                </button>
              </div>

              {approve === 'reconnect' ? (
                <p
                  className="ballast-consult__cosign-msg"
                  data-testid="coach-reconnect"
                  role="status"
                >
                  {approveMessage ||
                    'Reconnect your Schwab account to place this.'}{' '}
                  <Link className="ballast-consult__link" to="/onboarding">
                    Reconnect Schwab
                  </Link>
                </p>
              ) : null}

              {approve === 'in-progress' ? (
                <p
                  className="ballast-consult__cosign-msg"
                  data-testid="coach-in-progress"
                  role="status"
                >
                  {approveMessage ||
                    'This is being approved right now — give it a moment and check your Decisions.'}
                </p>
              ) : null}

              {approve === 'refused' ? (
                <p
                  className="ballast-consult__cosign-msg"
                  data-testid="coach-refused"
                  role="status"
                >
                  {approveMessage ||
                    'That order can’t be placed as-is. Nothing was placed.'}
                </p>
              ) : null}

              {approve === 'signed-out' ? (
                <p
                  className="ballast-consult__cosign-msg"
                  data-testid="coach-approve-signed-out"
                  role="status"
                >
                  Your session ended. Sign in and try again.{' '}
                  <Link className="ballast-consult__link" to="/auth">
                    Sign in
                  </Link>
                </p>
              ) : null}

              {approve === 'indeterminate' ? (
                <p
                  className="ballast-consult__cosign-msg"
                  data-testid="coach-indeterminate"
                  role="status"
                >
                  We couldn’t confirm whether that went through. Check your
                  Decisions before trying again.{' '}
                  <Link className="ballast-consult__link" to="/decisions">
                    Open Decisions
                  </Link>
                </p>
              ) : null}
            </div>
          ) : null}

          {approve === 'placed' && outcome ? (
            <div
              className="ballast-consult__outcome"
              data-testid="coach-outcome"
              role="status"
            >
              {placedPosition ? (
                <>
                  <p className="ballast-consult__outcome-line">
                    Outcome: {outcome.status}
                    {outcome.filled_qty != null
                      ? ` · filled ${outcome.filled_qty}`
                      : ''}
                    {outcome.avg_price != null ? ` @ ${outcome.avg_price}` : ''}
                  </p>
                  <p
                    className="ballast-consult__chip"
                    data-testid="coach-replay-chip"
                  >
                    <span aria-hidden="true">↻</span> if it dips, I’ll replay
                    this back to you
                  </p>
                </>
              ) : outcome.status === 'rejected' ? (
                <p className="ballast-consult__outcome-line">
                  Outcome: rejected — nothing was placed. Nothing to worry
                  about; you can try again when you’re ready.
                </p>
              ) : (
                <p className="ballast-consult__outcome-line">
                  Outcome: {outcome.status} — we couldn’t confirm this yet.
                  Check your Decisions before trying again.
                </p>
              )}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
