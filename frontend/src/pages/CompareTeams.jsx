import { useState } from 'react'
import { Users, Play, ArrowLeftRight, Crown } from 'lucide-react'
import { compareTeams } from '../lib/api'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import ContextInput from '../components/ContextInput'
import Blurb from '../components/Blurb'
import CategoryBar from '../components/CategoryBar'

function parseIds(raw) {
  return raw.split(/[\s,]+/).map(s => parseInt(s.trim())).filter(n => !isNaN(n))
}

function TeamCard({ snap, rank, isWinner }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <div className={`card ${isWinner ? 'border-field-600' : ''}`}>
      <div className="flex items-start gap-3 mb-3">
        <div className="text-2xl font-bold font-mono text-slate-600 w-6 shrink-0">
          {rank}
        </div>
        <div className="flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            {isWinner && <Crown size={13} className="text-yellow-400" />}
            <span className="font-semibold text-white">{snap.team_name}</span>
            <span className="ml-auto font-mono text-sm text-field-400">{snap.power_score.toFixed(2)}</span>
          </div>
          <div className="flex flex-wrap gap-1 mt-1.5">
            {snap.strong_cats.map(c => (
              <span key={c} className="stat-pill bg-field-900 text-field-300 text-[10px]">{c} ▲</span>
            ))}
            {snap.weak_cats.slice(0, 3).map(c => (
              <span key={c} className="stat-pill bg-red-950/50 text-red-400 text-[10px]">{c} ▼</span>
            ))}
          </div>
          {snap.top_players.length > 0 && (
            <div className="text-xs text-slate-500 mt-1">
              Top: {snap.top_players.join(', ')}
            </div>
          )}
        </div>
        <button
          className="text-slate-600 hover:text-slate-300 text-xs"
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? 'less' : 'more'}
        </button>
      </div>
      {expanded && <CategoryBar data={snap.category_strengths} />}
    </div>
  )
}

function TradeOpp({ opp, snapshots }) {
  const nameA = snapshots.find(s => s.team_id === opp.team_a_id)?.team_name || `Team ${opp.team_a_id}`
  const nameB = snapshots.find(s => s.team_id === opp.team_b_id)?.team_name || `Team ${opp.team_b_id}`
  return (
    <div className="p-4 rounded-lg bg-navy-800 border border-navy-700">
      <div className="flex items-center gap-2 text-sm mb-2">
        <span className="font-medium text-white">{nameA}</span>
        <ArrowLeftRight size={12} className="text-slate-500 shrink-0" />
        <span className="font-medium text-white">{nameB}</span>
        <span className="ml-auto font-mono text-xs text-field-400">+{opp.complementarity_score.toFixed(0)}</span>
      </div>
      <p className="text-xs text-slate-500 leading-relaxed">{opp.rationale}</p>
      {(opp.suggested_give || opp.suggested_receive) && (
        <div className="flex gap-4 mt-2 text-xs">
          {opp.suggested_give && (
            <span className="text-stitch-400">Give: {opp.suggested_give}</span>
          )}
          {opp.suggested_receive && (
            <span className="text-field-400">Receive: {opp.suggested_receive}</span>
          )}
        </div>
      )}
    </div>
  )
}

export default function CompareTeams() {
  const [teamIds,   setTeamIds]   = useState('')
  const [leagueId,  setLeagueId]  = useState('')
  const [context,   setContext]   = useState('')
  const [includeTrades, setIncludeTrades] = useState(true)
  const [loading,   setLoading]   = useState(false)
  const [error,     setError]     = useState(null)
  const [result,    setResult]    = useState(null)

  async function submit(e) {
    e.preventDefault()
    const ids = parseIds(teamIds)
    if (ids.length < 2) { setError('Enter at least 2 team IDs.'); return }
    setLoading(true); setError(null); setResult(null)
    try {
      const res = await compareTeams({
        team_ids: ids,
        league_id: leagueId ? parseInt(leagueId) : null,
        context: context || null,
        include_trade_suggestions: includeTrades,
      })
      setResult(res)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="space-y-8">
      <div>
        <div className="flex items-center gap-2 mb-1">
          <Users size={18} className="text-field-400" />
          <h1 className="text-2xl font-bold text-white">Compare Teams</h1>
        </div>
        <p className="text-slate-500 text-sm">
          Power scores, category profiles, and trade opportunities for 2–6 teams.
        </p>
      </div>

      <form onSubmit={submit} className="card space-y-5">
        <div>
          <label className="section-label">Team IDs *</label>
          <input
            className="field-input font-mono"
            placeholder="e.g. 1, 3, 7"
            value={teamIds}
            onChange={e => setTeamIds(e.target.value)}
          />
          <p className="text-xs text-slate-600 mt-1">2 to 6 team IDs, comma separated.</p>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="section-label">League ID (optional)</label>
            <input
              className="field-input font-mono"
              placeholder="e.g. 1"
              value={leagueId}
              onChange={e => setLeagueId(e.target.value)}
            />
          </div>
          <div className="flex items-end pb-px">
            <label className="flex items-center gap-2 cursor-pointer select-none">
              <input
                type="checkbox"
                className="w-4 h-4 rounded border-navy-600 bg-navy-900 accent-field-500"
                checked={includeTrades}
                onChange={e => setIncludeTrades(e.target.checked)}
              />
              <span className="text-sm text-slate-300">Include trade suggestions</span>
            </label>
          </div>
        </div>

        <ContextInput value={context} onChange={setContext} />

        <button type="submit" className="btn-primary" disabled={loading}>
          <Play size={14} /> Compare Teams
        </button>
      </form>

      <ErrorBanner message={error} onClose={() => setError(null)} />
      {loading && <LoadingState message="Comparing rosters…" />}

      {result && (
        <div className="space-y-6">
          {/* Team cards */}
          <div className="space-y-3">
            {result.snapshots.map((snap, i) => (
              <TeamCard
                key={snap.team_id}
                snap={snap}
                rank={i + 1}
                isWinner={snap.team_id === result.winner}
              />
            ))}
          </div>

          {/* Trade opportunities */}
          {result.trade_opportunities.length > 0 && (
            <div className="space-y-3">
              <div className="section-label flex items-center gap-1.5">
                <ArrowLeftRight size={11} /> Trade opportunities
              </div>
              {result.trade_opportunities.map((opp, i) => (
                <TradeOpp key={i} opp={opp} snapshots={result.snapshots} />
              ))}
            </div>
          )}

          <Blurb text={result.analysis_blurb} />
        </div>
      )}
    </div>
  )
}
