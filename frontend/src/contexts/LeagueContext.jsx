import { createContext, useContext, useState, useEffect, useCallback } from 'react'
import { useAuth } from './AuthContext'
import { req, listUserLeagues, activateLeague } from '../lib/api'

const LeagueContext = createContext(null)

export function LeagueProvider({ children }) {
  const { user } = useAuth()
  const [league,     setLeague]     = useState(null)
  const [allLeagues, setAllLeagues] = useState([])
  const [loading,    setLoading]    = useState(false)
  const [switching,  setSwitching]  = useState(false)

  const fetchLeague = useCallback(() => {
    if (!user) { setLeague(null); setAllLeagues([]); return }
    setLoading(true)
    // Fetch active league and all-leagues list in parallel
    Promise.all([
      req('GET', '/api/v1/auth/league').catch(() => null),
      listUserLeagues().catch(() => []),
    ])
      .then(([leagueData, leaguesList]) => {
        setLeague(leagueData)
        setAllLeagues(leaguesList || [])
      })
      .finally(() => setLoading(false))
  }, [user?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  // Initial load whenever the logged-in user changes
  useEffect(() => {
    fetchLeague()
  }, [fetchLeague])

  // Re-fetch when a background Yahoo sync completes
  useEffect(() => {
    const handler = () => {
      if (user) fetchLeague()
    }
    window.addEventListener('yahoo:synced', handler)
    return () => window.removeEventListener('yahoo:synced', handler)
  }, [user?.id, fetchLeague]) // eslint-disable-line react-hooks/exhaustive-deps

  // Switch to a different league — calls API, updates allLeagues active flag,
  // and replaces the active league data without a full refetch.
  const switchLeague = useCallback(async (leagueId) => {
    if (switching) return
    setSwitching(true)
    try {
      const newLeagueData = await activateLeague(leagueId)
      setLeague(newLeagueData)
      setAllLeagues(prev =>
        prev.map(lg => ({ ...lg, is_active: lg.league_id === leagueId }))
      )
    } finally {
      setSwitching(false)
    }
  }, [switching])

  const myTeam = league?.teams?.find(t => t.is_mine) ?? null

  return (
    <LeagueContext.Provider value={{
      league,
      myTeam,
      allLeagues,
      loading,
      switching,
      refresh: fetchLeague,
      switchLeague,
    }}>
      {children}
    </LeagueContext.Provider>
  )
}

export function useLeague() {
  return useContext(LeagueContext)
}
