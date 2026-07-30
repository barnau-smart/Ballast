import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Settings } from '../routes/Settings.jsx'

afterEach(() => {
  vi.unstubAllGlobals()
})

// A tiny fake backend: GET returns the current opt-in; PUT echoes the new value
// and remembers it. Every call is recorded on the returned mock.
function stubDigestApi(initial) {
  let state = initial
  const fetchMock = vi.fn((url, options = {}) => {
    const method = options.method ?? 'GET'
    if (method === 'PUT') {
      state = JSON.parse(options.body).opted_in
    }
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve({ opted_in: state }),
    })
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function renderSettings() {
  return render(
    <MemoryRouter>
      <Settings />
    </MemoryRouter>,
  )
}

describe('Settings — weekly digest opt-in', () => {
  it('reflects the off-by-default state and shows calm, non-FOMO copy', async () => {
    stubDigestApi(false)
    renderSettings()

    const toggle = await screen.findByTestId('digest-opt-in-toggle')
    expect(toggle.checked).toBe(false)
    expect(screen.getByTestId('digest-opt-in-state')).toHaveTextContent('Off')

    const card = screen.getByTestId('digest-preference-card')
    // Calm framing: opt-in, gentle, unsubscribe-anytime.
    expect(card.textContent.toLowerCase()).toContain('weekly email')
    expect(card.textContent.toLowerCase()).toContain('unsubscribe')
    // Never alarmist / urgency / FOMO wording.
    expect(card.textContent).not.toMatch(
      /miss out|missing out|hurry|urgent|act now|last chance|don'?t miss|fear/i,
    )
  })

  it('reflects an already-on preference from the server', async () => {
    stubDigestApi(true)
    renderSettings()

    const toggle = await screen.findByTestId('digest-opt-in-toggle')
    expect(toggle.checked).toBe(true)
    expect(screen.getByTestId('digest-opt-in-state')).toHaveTextContent('On')
  })

  it('issues a PUT when the user enables the digest', async () => {
    const fetchMock = stubDigestApi(false)
    renderSettings()

    const toggle = await screen.findByTestId('digest-opt-in-toggle')
    fireEvent.click(toggle)

    await waitFor(() => {
      const putCall = fetchMock.mock.calls.find(
        ([, opts]) => opts?.method === 'PUT',
      )
      expect(putCall).toBeTruthy()
      expect(JSON.parse(putCall[1].body)).toEqual({ opted_in: true })
    })

    await waitFor(() =>
      expect(screen.getByTestId('digest-opt-in-toggle').checked).toBe(true),
    )
  })
})
