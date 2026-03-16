import { useEffect } from 'react'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
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

const KEEPALIVE_MS = 4 * 60 * 1000 // 4 minutes — keeps Railway from sleeping

export default function App() {
  useEffect(() => {
    const ping = () => fetch(`${API_BASE_URL}/api/v1/health`, { method: 'HEAD' }).catch(() => {})
    ping()
    const id = setInterval(ping, KEEPALIVE_MS)
    return () => clearInterval(id)
  }, [])

  return (
    <BrowserRouter>
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
        </Routes>
      </Layout>
    </BrowserRouter>
  )
}
