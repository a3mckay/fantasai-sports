import { createContext, useContext, useState, useEffect } from 'react'
import { useAuth } from './AuthContext'
import { req } from '../lib/api'

const LeagueContext = createContext(null)

export function LeagueProvider({ children }) {
  const { user } = useAuth()
  const [league, setLeague] = useState(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!user) { setLeague(null); return }
    setLoading(true)
    req('GET', '/api/v1/auth/league')
      .then(setLeague)
      .catch(() => setLeague(null))
      .finally(() => setLoading(false))
  }, [user?.id])

  const myTeam = league?.teams?.find(t => t.is_mine) ?? null

  return (
    <LeagueContext.Provider value={{ league, myTeam, loading, refresh: () => {
      if (!user) return
      req('GET', '/api/v1/auth/league').then(setLeague).catch(() => {})
    }}}>
      {children}
    </LeagueContext.Provider>
  )
}

export function useLeague() {
  return useContext(LeagueContext)
}
