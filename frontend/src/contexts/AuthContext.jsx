import { createContext, useContext, useEffect, useState } from 'react'
import { onAuthStateChanged, signOut as firebaseSignOut } from 'firebase/auth'
import { auth } from '../lib/firebase'
import { API_BASE_URL } from '../lib/api'

const sleep = ms => new Promise(r => setTimeout(r, ms))

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [firebaseUser, setFirebaseUser] = useState(undefined) // undefined = loading
  const [user, setUser] = useState(null)       // our backend User object
  const [loading, setLoading] = useState(true)
  const [authError, setAuthError] = useState(null) // backend sync error

  useEffect(() => {
    const unsubscribe = onAuthStateChanged(auth, async (fbUser) => {
      setFirebaseUser(fbUser)
      if (fbUser) {
        await _syncWithBackend(fbUser)
      } else {
        setUser(null)
        setAuthError(null)
        setLoading(false)
      }
    })
    return unsubscribe
  }, [])

  async function _syncWithBackend(fbUser) {
    setAuthError(null)

    // Retry up to 3× on network errors or 5xx — Railway containers can take
    // 15-30 s to wake from idle on first load.
    const MAX_ATTEMPTS = 3
    const BACKOFF_MS   = [1000, 2000, 4000]

    for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
      try {
        const idToken = await fbUser.getIdToken()
        const res = await fetch(`${API_BASE_URL}/api/v1/auth/verify`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${idToken}`,
          },
          body: JSON.stringify({ id_token: idToken }),
        })

        if (res.ok) {
          const data = await res.json()
          setUser(data.user)
          setAuthError(null)
          setLoading(false)

          // If the user has a Yahoo connection, a background sync was queued on
          // the server.  Dispatch yahoo:synced after 15 s so LeagueContext
          // re-fetches with the freshly-imported roster data.
          if (data.user?.yahoo_connected) {
            setTimeout(
              () => window.dispatchEvent(new CustomEvent('yahoo:synced')),
              15_000,
            )
          }

          return
        }

        // 401 = stale Firebase session — sign out immediately, no retry
        if (res.status === 401) {
          await firebaseSignOut(auth)
          setUser(null)
          setAuthError(null)
          setLoading(false)
          return
        }

        // Other 4xx = real error, no point retrying
        if (res.status >= 400 && res.status < 500) {
          let detail = `Server error ${res.status}`
          try { detail = (await res.json()).detail || detail } catch {}
          console.error('[AuthContext] /auth/verify failed:', res.status, detail)
          setAuthError(detail)
          setUser(null)
          setLoading(false)
          return
        }

        // 5xx — fall through to retry
        console.warn(`[AuthContext] /auth/verify attempt ${attempt + 1} got ${res.status}, retrying…`)

      } catch (err) {
        // Network error (container still waking up) — fall through to retry
        console.warn(`[AuthContext] /auth/verify attempt ${attempt + 1} network error:`, err.message)
      }

      if (attempt < MAX_ATTEMPTS - 1) {
        setAuthError(`Server is starting up… (attempt ${attempt + 2} of ${MAX_ATTEMPTS})`)
        await sleep(BACKOFF_MS[attempt])
      }
    }

    // All attempts exhausted
    console.error('[AuthContext] /auth/verify failed after', MAX_ATTEMPTS, 'attempts')
    setAuthError('Could not reach the server after several attempts. Please try again.')
    setUser(null)
    setLoading(false)
  }

  async function refreshProfile() {
    const fbUser = auth.currentUser
    if (!fbUser) return
    setLoading(true)
    await _syncWithBackend(fbUser)
  }

  async function signOut() {
    await firebaseSignOut(auth)
    setUser(null)
    setFirebaseUser(null)
  }

  return (
    <AuthContext.Provider value={{ user, firebaseUser, loading, authError, signOut, refreshProfile }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>')
  return ctx
}
