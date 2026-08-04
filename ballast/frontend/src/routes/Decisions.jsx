import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { DecisionReplay } from '../components/DecisionReplay.jsx'
import { apiFetch } from '../lib/session.js'
import { formatDay } from '../lib/datetime.js'
import './screen.css'
import './Decisions.css'

/**
 * The Decisions surface (Story 4.10, FR16): a read-only history of the user's
 * co-signed decisions and, on selection, the verbatim replay INLINE (no new
 * route — the app holds exactly six surfaces; replay is selected-decision
 * state). Fetches `GET /api/coach/decisions` on mount; selecting a decision
 * fetches `GET /api/coach/decisions/{id}` and renders `<DecisionReplay>`.
 *
 * Presentation-only (AD-1): fetch + render, no business logic; the backend is
 * the sole source of the frozen snapshot. Calm, honest states: a calm loading
 * line (no spinner urgency), a gentle empty invite when the record is truly
 * empty, and — distinct from that — a calm "couldn't load" note when the fetch
 * itself failed (so a load failure is never dishonestly reported as "you have
 * no decisions"). Mirrors the Dashboard's `useState`/`useEffect`/`apiFetch` +
 * active-flag pattern; never an alarming error screen.
 */
export function Decisions() {
  const [status, setStatus] = useState('loading')
  const [decisions, setDecisions] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  // A monotonic nonce so re-selecting the SAME decision (e.g. to retry after a
  // detail-fetch error) still re-fires the detail effect.
  const [reloadKey, setReloadKey] = useState(0)
  const [detail, setDetail] = useState(null)
  const [detailStatus, setDetailStatus] = useState('idle')

  // Story 8.3 — the resting-order cancel lifecycle for the currently-open
  // decision. `cancelPhase`: idle | cancelling | cancelled | unclear | refused
  // | reconnect. `cancelMessage` carries the backend's verbatim calm 422 (or a
  // reconnect note). `cancelledStatus` overrides the shown effective status
  // after a confirmed cancel so the row reflects the honest post-cancel truth.
  const [cancelPhase, setCancelPhase] = useState('idle')
  const [cancelMessage, setCancelMessage] = useState('')
  const [cancelledStatus, setCancelledStatus] = useState(null)

  // Synchronous double-submit guard for the cancel POST (mirrors the
  // `placingRef` pattern in CoachConsult.jsx) — a rapid double-click can't fire
  // two POSTs before the async `cancelPhase` re-render lands.
  const cancellingRef = useRef(false)
  // Tracks the currently-open decision so a late cancel response can bail if the
  // user has since selected a different decision (wrong-decision race guard).
  const selectedIdRef = useRef(selectedId)
  useEffect(() => {
    selectedIdRef.current = selectedId
  }, [selectedId])

  useEffect(() => {
    let active = true
    apiFetch('/api/coach/decisions')
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((data) => {
        if (!active) return
        setDecisions(Array.isArray(data.decisions) ? data.decisions : [])
        setStatus('ready')
      })
      .catch(() => {
        if (!active) return
        // Honest degrade: a load failure is NOT an empty history. Show a calm
        // "couldn't load" note (distinct from the empty invite) so we never tell
        // a user with real decisions that they have none. Never an error screen.
        setDecisions([])
        setStatus('error')
      })
    return () => {
      active = false
    }
  }, [])

  function selectDecision(decisionId) {
    setSelectedId(decisionId)
    setReloadKey((key) => key + 1)
  }

  useEffect(() => {
    if (selectedId == null) return
    let active = true
    setDetail(null)
    setDetailStatus('loading')
    // A fresh detail open clears any prior cancel lifecycle state.
    setCancelPhase('idle')
    setCancelMessage('')
    setCancelledStatus(null)
    apiFetch(`/api/coach/decisions/${selectedId}`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then((data) => {
        if (!active) return
        setDetail(data)
        setDetailStatus('ready')
      })
      .catch(() => {
        if (!active) return
        // Calm fallback: no error screen, just the gentle "couldn't reopen" note.
        setDetail(null)
        setDetailStatus('error')
      })
    return () => {
      active = false
    }
  }, [selectedId, reloadKey])

  // Effective outcome status = the newest-known truth (matches the backend's
  // `effective_outcome_status`): a durable reconciliation snapshot wins over the
  // co-sign snapshot. `cancelledStatus` (a confirmed cancel this session) wins
  // over both so the row reflects the honest post-cancel state immediately.
  const reconOutcome = detail?.reconciliation_snapshot?.outcome ?? null
  const cosignOutcome = detail?.cosign_snapshot?.outcome ?? null
  // Pick ONE effective outcome object first (recon wins over cosign) and read
  // BOTH status and broker_ref from it, so the two never cross snapshots (e.g.
  // recon's status paired with cosign's broker_ref). `cancelledStatus` (a
  // confirmed cancel this session) still overrides the shown status.
  const effectiveOutcome = reconOutcome ?? cosignOutcome
  const effectiveStatus =
    cancelledStatus ?? effectiveOutcome?.status ?? null
  const brokerRef = effectiveOutcome?.broker_ref ?? null
  // A working (resting) order: effectively pending AND affirmatively placed
  // (carries a broker_ref). Only such an order is cancellable.
  const isWorking = effectiveStatus === 'pending' && Boolean(brokerRef)

  // Patch the matching left-list row so it reflects the honest post-cancel
  // truth (the list is fetched once on mount; without this the detail pane
  // updates but the row still reads `pending`). Bails on a stale request.
  function patchListStatus(reqId, status) {
    if (reqId !== selectedIdRef.current) return
    setDecisions((prev) =>
      prev.map((d) =>
        d.decision_id === reqId ? { ...d, outcome_status: status } : d,
      ),
    )
  }

  async function onCancel() {
    if (selectedId == null) return
    // Synchronous double-submit guard: a rapid double-click can't fire two
    // POSTs before the async `cancelPhase` re-render lands.
    if (cancellingRef.current) return
    // Belt-and-suspenders: a stale click can't POST a cancel on a no-longer-
    // working order.
    if (!isWorking) return
    // Capture the decision this cancel is FOR — a late response must not set
    // cancel state on a since-selected different decision.
    const reqId = selectedId
    cancellingRef.current = true
    setCancelPhase('cancelling')
    setCancelMessage('')
    try {
      const res = await apiFetch(
        `/api/coach/decisions/${reqId}/cancel`,
        { method: 'POST' },
      )
      let data = null
      try {
        data = await res.json()
      } catch {
        data = null
      }
      // The user switched decisions while this was in flight — drop the result.
      if (reqId !== selectedIdRef.current) return
      if (res.ok) {
        if (data?.needs_reconfirmation) {
          // The cancel and a fill can race: the backend can return 200 with
          // needs_reconfirmation AND a known filled/partial status when the
          // order filled before the DELETE applied. Be honest about the fill
          // rather than collapsing to a generic "unclear". A full fill and a
          // PARTIAL fill are distinct truths — a partial means only some shares
          // filled, so "nothing was called off" would misstate what happened.
          if (data?.status === 'filled') {
            setCancelledStatus('filled')
            setCancelPhase('filled-first')
            patchListStatus(reqId, 'filled')
            return
          }
          if (data?.status === 'partial') {
            setCancelledStatus('partial')
            setCancelPhase('partial-first')
            patchListStatus(reqId, 'partial')
            return
          }
          // The broker was called but the post-cancel state could not be
          // positively confirmed — never claim a clean cancel.
          setCancelPhase('unclear')
          return
        }
        if (data?.status === 'rejected') {
          // A confirmed cancel: the resting order is terminal. Reflect it
          // honestly, hide the Cancel control, and patch the list row.
          setCancelledStatus('rejected')
          setCancelPhase('cancelled')
          patchListStatus(reqId, 'rejected')
          return
        }
        // A 200 without a clean rejected/needs_reconfirmation signal — treat as
        // indeterminate rather than claiming success.
        setCancelPhase('unclear')
        return
      }
      // The app-wide error envelope is `{error:{type,message}}` (api/app.py);
      // the calm 409/422 reason lives at `data.error.message`. Fall back to the
      // legacy `detail`/`message` shapes defensively.
      const detailMsg =
        data?.error?.message ?? data?.detail ?? data?.message ?? ''
      if (res.status === 409) {
        // Session lapsed — the same calm reconnect handling as elsewhere.
        setCancelMessage(detailMsg)
        setCancelPhase('reconnect')
      } else if (res.status === 422) {
        // Already settled / no longer cancellable — surface the backend's calm
        // reason verbatim; the row is unchanged.
        setCancelMessage(detailMsg)
        setCancelPhase('refused')
      } else {
        setCancelPhase('unclear')
      }
    } catch {
      if (reqId !== selectedIdRef.current) return
      setCancelPhase('unclear')
    } finally {
      cancellingRef.current = false
    }
  }

  return (
    <section className="ballast-screen">
      <p className="ballast-screen__eyebrow">decisions</p>
      <h1 className="ballast-screen__title">Decisions</h1>
      <p className="ballast-screen__prose">
        A running log of the choices you have weighed, so you can look back at
        your reasoning over time.
      </p>

      {status === 'loading' ? (
        <p className="ballast-decisions__calm" data-testid="decisions-loading">
          Looking back at your record…
        </p>
      ) : null}

      {status === 'error' ? (
        <div className="ballast-card" data-testid="decisions-load-error">
          We couldn’t load your record just now. Nothing is lost — check back in
          a moment.
        </div>
      ) : null}

      {status === 'ready' && decisions.length === 0 ? (
        <div className="ballast-card" data-testid="decisions-empty">
          No decisions on the record yet. When you co-sign a recommendation on
          the Coach, it will show up here — reasoning and all — for you to
          revisit.
        </div>
      ) : null}

      {status === 'ready' && decisions.length > 0 ? (
        <div className="ballast-decisions">
          <ul className="ballast-decisions__list" data-testid="decisions-list">
            {decisions.map((decision) => (
              <li key={decision.decision_id}>
                <button
                  type="button"
                  className={
                    decision.decision_id === selectedId
                      ? 'ballast-decisions__item ballast-decisions__item--active'
                      : 'ballast-decisions__item'
                  }
                  aria-pressed={decision.decision_id === selectedId}
                  onClick={() => selectDecision(decision.decision_id)}
                  data-testid={`decisions-item-${decision.decision_id}`}
                >
                  <span className="ballast-decisions__item-label">
                    {decision.action_label}
                  </span>
                  <span className="ballast-decisions__item-meta">
                    {decision.symbol ? `${decision.symbol} · ` : ''}
                    {decision.outcome_status} · {formatDay(decision.co_signed_at)}
                  </span>
                </button>
              </li>
            ))}
          </ul>

          <div
            className="ballast-decisions__replay"
            aria-live="polite"
            data-testid="decisions-replay-region"
          >
            {selectedId == null ? (
              <p
                className="ballast-decisions__calm"
                data-testid="decisions-prompt"
              >
                Pick a decision to replay the reasoning you co-signed.
              </p>
            ) : null}
            {detailStatus === 'loading' ? (
              <p
                className="ballast-decisions__calm"
                data-testid="decisions-detail-loading"
              >
                Reopening that decision…
              </p>
            ) : null}
            {detailStatus === 'error' ? (
              <p
                className="ballast-decisions__calm"
                data-testid="decisions-detail-error"
              >
                Couldn’t reopen that one just now — pick it again in a moment.
              </p>
            ) : null}
            {detailStatus === 'ready' && detail ? (
              <>
                <DecisionReplay detail={detail} />

                {cancelPhase === 'cancelled' ? (
                  <p
                    className="ballast-decisions__calm"
                    data-testid="decisions-cancelled"
                    role="status"
                  >
                    Cancelled — this resting order was called off and nothing
                    else will be placed.
                  </p>
                ) : null}

                {cancelPhase === 'filled-first' ? (
                  <p
                    className="ballast-decisions__calm"
                    data-testid="decisions-cancel-filled"
                    role="status"
                  >
                    This order filled before the cancel took effect — nothing
                    was called off. Your position reflects the fill.
                  </p>
                ) : null}

                {cancelPhase === 'partial-first' ? (
                  <p
                    className="ballast-decisions__calm"
                    data-testid="decisions-cancel-partial"
                    role="status"
                  >
                    Part of this order filled before the cancel took effect —
                    those shares are yours, and the rest was called off. Your
                    position reflects the partial fill.
                  </p>
                ) : null}

                {isWorking && cancelPhase !== 'cancelled' ? (
                  <div
                    className="ballast-decisions__working"
                    data-testid="decisions-working"
                  >
                    <p
                      className="ballast-decisions__working-label"
                      data-testid="decisions-working-label"
                    >
                      Working — this order is resting until your price is
                      reached, or you cancel it.
                    </p>
                    <button
                      type="button"
                      className="ballast-decisions__cancel"
                      data-testid="decisions-cancel"
                      onClick={onCancel}
                      disabled={cancelPhase === 'cancelling'}
                    >
                      {cancelPhase === 'cancelling'
                        ? 'Cancelling…'
                        : 'Cancel this order'}
                    </button>

                    {cancelPhase === 'unclear' ? (
                      <p
                        className="ballast-decisions__calm"
                        data-testid="decisions-cancel-unclear"
                        role="status"
                      >
                        We couldn’t confirm the cancel just now — the order’s
                        state is unclear. Re-check it in a moment before trying
                        again.
                      </p>
                    ) : null}

                    {cancelPhase === 'refused' ? (
                      <p
                        className="ballast-decisions__calm"
                        data-testid="decisions-cancel-refused"
                        role="status"
                      >
                        {cancelMessage ||
                          'This order can no longer be cancelled — it is already settled or partially filled.'}
                      </p>
                    ) : null}

                    {cancelPhase === 'reconnect' ? (
                      <p
                        className="ballast-decisions__calm"
                        data-testid="decisions-cancel-reconnect"
                        role="status"
                      >
                        {cancelMessage ||
                          'Reconnect your Schwab account to cancel this.'}{' '}
                        <Link
                          className="ballast-decisions__link"
                          to="/onboarding"
                        >
                          Reconnect Schwab
                        </Link>
                      </p>
                    ) : null}
                  </div>
                ) : null}
              </>
            ) : null}
          </div>
        </div>
      ) : null}
    </section>
  )
}
