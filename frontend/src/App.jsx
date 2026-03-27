import { useEffect } from 'react'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { AuthProvider } from './contexts/AuthContext'
import { LeagueProvider } from './contexts/LeagueContext'
import AuthGuard from './components/AuthGuard'
import Layout from './components/Layout'
import MeterBanner from './components/MeterBanner'
import { API_BASE_URL } from './lib/api'
import Home           from './pages/Home'
import Rankings       from './pages/Rankings'
import ComparePlayers from './pages/ComparePlayers'
import EvaluateTrade  from './pages/EvaluateTrade'
import FindPlayer     from './pages/FindPlayer'
import TeamEval       from './pages/TeamEval'
import KeeperEval     from './pages/KeeperEval'
import CompareTeams   from './pages/CompareTeams'
import LeaguePower    from './pages/LeaguePower'
import Login          from './pages/Login'
import Onboarding     from './pages/Onboarding'
import Profile        from './pages/Profile'
import AdminPanel        from './pages/AdminPanel'
import Transactions     from './pages/Transactions'
import TransactionTicker from './components/TransactionTicker'

const KEEPALIVE_MS = 2 * 60 * 1000 // 2 minutes — keeps Railway from sleeping

export default function App() {
  useEffect(() => {
    const ping = () => fetch(`${API_BASE_URL}/health`).catch(() => {})
    ping()
    const id = setInterval(ping, KEEPALIVE_MS)
    return () => clearInterval(id)
  }, [])

  return (
    <BrowserRouter>
      <AuthProvider>
        <LeagueProvider>
        <MeterBanner />
        <TransactionTicker />
        <Routes>
          {/* Public routes — no auth required */}
          <Route path="/login"      element={<Login />} />
          <Route path="/onboarding" element={<Onboarding />} />

          {/* All other routes require auth + completed onboarding */}
          <Route path="/*" element={
            <AuthGuard>
              <Layout>
                <Routes>
                  <Route path="/"              element={<Home />} />
                  <Route path="/rankings"      element={<Rankings />} />
                  <Route path="/compare"       element={<ComparePlayers />} />
                  <Route path="/trade"         element={<EvaluateTrade />} />
                  <Route path="/find-player"   element={<FindPlayer />} />
                  <Route path="/team-eval"     element={<TeamEval />} />
                  <Route path="/keeper-eval"   element={<KeeperEval />} />
                  <Route path="/compare-teams" element={<CompareTeams />} />
                  <Route path="/league-power"  element={<LeaguePower />} />
                  <Route path="/transactions"   element={<Transactions />} />
                  <Route path="/profile"       element={<Profile />} />
                  <Route path="/admin"         element={<AdminPanel />} />
                </Routes>
              </Layout>
            </AuthGuard>
          } />
        </Routes>
        </LeagueProvider>
      </AuthProvider>
    </BrowserRouter>
  )
}
