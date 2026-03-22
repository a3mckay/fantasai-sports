import { createContext, useContext, useEffect, useState } from 'react'
import { onAuthStateChanged, signOut as firebaseSignOut } from 'firebase/auth'
import { auth } from '../lib/firebase'
import { API_BASE_URL } from '../lib/api'

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
      } else {
        let detail = `Server error ${res.status}`
        try { detail = (await res.json()).detail || detail } catch {}
        console.error('[AuthContext] /auth/verify failed:', res.status, detail)
        if (res.status === 401) {
          // Stale or invalid Firebase session — sign out so the user gets
          // a clean login screen rather than a looping error banner.
          await firebaseSignOut(auth)
          setAuthError(null)
        } else {
          setAuthError(detail)
        }
        setUser(null)
      }
    } catch (err) {
      console.error('[AuthContext] /auth/verify network error:', err)
      setAuthError('Could not reach the server. Is the backend running?')
      setUser(null)
    } finally {
      setLoading(false)
    }
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
