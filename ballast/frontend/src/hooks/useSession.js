import { useSyncExternalStore } from 'react'
import { isSignedIn, subscribe } from '../lib/session.js'

/**
 * React binding for the session token store. Returns the current signed-in
 * boolean and re-renders subscribers whenever the token changes. Presentation
 * -only: it reflects whether a token is held, nothing more.
 */
export function useSession() {
  const signedIn = useSyncExternalStore(subscribe, isSignedIn, isSignedIn)
  return { signedIn }
}
