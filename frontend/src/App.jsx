import { BrowserRouter, Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import Home           from './pages/Home'
import Rankings       from './pages/Rankings'
import ComparePlayers from './pages/ComparePlayers'
import EvaluateTrade  from './pages/EvaluateTrade'
import FindPlayer     from './pages/FindPlayer'
import TeamEval       from './pages/TeamEval'
import KeeperEval     from './pages/KeeperEval'
import CompareTeams   from './pages/CompareTeams'
import LeaguePower    from './pages/LeaguePower'

export default function App() {
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
