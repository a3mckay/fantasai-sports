import { createContext, useContext, useState, useEffect, useCallback } from 'react'
import { useAuth } from './AuthContext'
import { req } from '../lib/api'

const LeagueContext = createContext(null)

export function LeagueProvider({ children }) {
  const { user } = useAuth()
  const [league, setLeague] = useState(null)
  const [loading, setLoading] = useState(false)

  const fetchLeague = useCallback(() => {
    if (!user) { setLeague(null); return }
    setLoading(true)
    req('GET', '/api/v1/auth/league')
      .then(setLeague)
      .catch(() => setLeague(null))
      .finally(() => setLoading(false))
  }, [user?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  // Initial load whenever the logged-in user changes
  useEffect(() => {
    fetchLeague()
  }, [fetchLeague])

  // Re-fetch when a background Yahoo sync completes so rosters are up-to-date
  // without requiring a full page reload.
  useEffect(() => {
    const handler = () => {
      if (user) {
        req('GET', '/api/v1/auth/league').then(setLeague).catch(() => {})
      }
    }
    window.addEventListener('yahoo:synced', handler)
    return () => window.removeEventListener('yahoo:synced', handler)
  }, [user?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const myTeam = league?.teams?.find(t => t.is_mine) ?? null

  return (
    <LeagueContext.Provider value={{
      league,
      myTeam,
      loading,
      refresh: fetchLeague,
    }}>
      {children}
    </LeagueContext.Provider>
  )
}

export function useLeague() {
  return useContext(LeagueContext)
}
