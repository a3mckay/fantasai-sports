import { useState } from 'react'
import { Zap, Play, Crown, ArrowLeftRight } from 'lucide-react'
import { leaguePower } from '../lib/api'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import Blurb from '../components/Blurb'
import CategoryBar from '../components/CategoryBar'

const TIER_STYLE = {
  contender:  'bg-field-900 text-field-300 border-field-700',
  middle:     'bg-leather-500/10 text-leather-300 border-leather-500/30',
  rebuilding: 'bg-stitch-500/10 text-stitch-300 border-stitch-800',
}

const TIER_LABEL = {
  contender:  '🏆 Contender',
  middle:     '⚖️  Middle',
  rebuilding: '🔧 Rebuilding',
}

function TeamRow({ snap, rank, tier }) {
  const [expanded, setExpanded] = useState(false)
  const tierKey = Object.keys(tier).find(t => tier[t].includes(snap.team_id)) || 'middle'
  return (
    <>
      <tr className="border-b border-navy-700 hover:bg-navy-800/50 transition-colors">
        <td className="py-3 pl-4 pr-2 font-mono text-slate-500 text-sm">{rank}</td>
        <td className="py-3 px-2">
          <div className="flex items-center gap-2">
            {rank === 1 && <Crown size={12} className="text-yellow-400 shrink-0" />}
            <span className="font-medium text-white text-sm">{snap.team_name}</span>
          </div>
        </td>
        <td className="py-3 px-2">
          <span className={`stat-pill border text-[10px] ${TIER_STYLE[tierKey] || ''}`}>
            {TIER_LABEL[tierKey] || tierKey}
          </span>
        </td>
        <td className="py-3 px-2 font-mono text-field-400 text-sm">{snap.power_score.toFixed(2)}</td>
        <td className="py-3 px-2 hidden sm:table-cell">
          <div className="flex flex-wrap gap-1">
            {snap.strong_cats.slice(0, 3).map(c => (
              <span key={c} className="stat-pill bg-field-900/60 text-field-400 text-[9px]">{c}</span>
            ))}
          </div>
        </td>
        <td className="py-3 pr-4 pl-2">
          <button
            className="text-slate-600 hover:text-slate-300 text-xs"
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? '▲' : '▼'}
          </button>
        </td>
      </tr>
      {expanded && (
        <tr className="bg-navy-800/30">
          <td colSpan={6} className="px-4 py-3">
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <div className="text-[10px] text-slate-600 uppercase tracking-widest mb-1">Top players</div>
                <p className="text-xs text-slate-400">{snap.top_players.join(', ') || '—'}</p>
              </div>
              <div>
                <div className="text-[10px] text-slate-600 uppercase tracking-widest mb-2">Category strength</div>
                <CategoryBar data={snap.category_strengths} />
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

function TradeOpp({ opp, teamNames }) {
  const nameA = teamNames[opp.team_a_id] || `Team ${opp.team_a_id}`
  const nameB = teamNames[opp.team_b_id] || `Team ${opp.team_b_id}`
  return (
    <div className="p-4 rounded-lg bg-navy-800 border border-navy-700">
      <div className="flex items-center gap-2 text-sm mb-1.5">
        <span className="font-medium text-white">{nameA}</span>
        <ArrowLeftRight size={11} className="text-slate-500 shrink-0" />
        <span className="font-medium text-white">{nameB}</span>
        <span className="ml-auto font-mono text-xs text-field-400">+{opp.complementarity_score.toFixed(0)}</span>
      </div>
      <p className="text-xs text-slate-500 leading-relaxed">{opp.rationale}</p>
      {(opp.suggested_give || opp.suggested_receive) && (
        <div className="flex gap-4 mt-1.5 text-xs">
          {opp.suggested_give    && <span className="text-stitch-400">Give: {opp.suggested_give}</span>}
          {opp.suggested_receive && <span className="text-field-400">Receive: {opp.suggested_receive}</span>}
        </div>
      )}
    </div>
  )
}

export default function LeaguePower() {
  const [leagueId, setLeagueId] = useState('')
  const [loading,  setLoading]  = useState(false)
  const [error,    setError]    = useState(null)
  const [result,   setResult]   = useState(null)

  async function submit(e) {
    e.preventDefault()
    if (!leagueId) { setError('League ID is required.'); return }
    setLoading(true); setError(null); setResult(null)
    try {
      const res = await leaguePower(parseInt(leagueId))
      setResult(res)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  // Build a team-name lookup from the rankings list
  const teamNames = {}
  if (result) {
    for (const s of result.power_rankings) teamNames[s.team_id] = s.team_name
  }

  return (
    <div className="space-y-8">
      <div>
        <div className="flex items-center gap-2 mb-1">
          <Zap size={18} className="text-field-400" />
          <h1 className="text-2xl font-bold text-white">League Power Rankings</h1>
        </div>
        <p className="text-slate-500 text-sm">
          Full standings by roster strength. Contenders, middle, rebuilders — plus top trade pairings.
        </p>
      </div>

      <form onSubmit={submit} className="card space-y-4">
        <div>
          <label className="section-label">League ID *</label>
          <input
            className="field-input font-mono w-40"
            placeholder="e.g. 1"
            value={leagueId}
            onChange={e => setLeagueId(e.target.value)}
          />
        </div>
        <button type="submit" className="btn-primary" disabled={loading}>
          <Play size={14} /> Run Power Rankings
        </button>
      </form>

      <ErrorBanner message={error} onClose={() => setError(null)} />
      {loading && <LoadingState message="Computing power rankings…" />}

      {result && (
        <div className="space-y-6">
          {/* Tier summary pills */}
          <div className="flex flex-wrap gap-2">
            {Object.entries(result.tiers).map(([tier, ids]) => (
              <div key={tier} className={`px-3 py-1.5 rounded-lg border text-xs font-medium ${TIER_STYLE[tier] || ''}`}>
                {TIER_LABEL[tier] || tier}: {ids.length} team{ids.length !== 1 ? 's' : ''}
              </div>
            ))}
          </div>

          {/* Rankings table */}
          <div className="card p-0 overflow-hidden">
            <table className="w-full">
              <thead className="bg-navy-900">
                <tr className="text-[10px] text-slate-500 uppercase tracking-wider">
                  <th className="py-2 pl-4 pr-2 text-left">#</th>
                  <th className="py-2 px-2 text-left">Team</th>
                  <th className="py-2 px-2 text-left">Tier</th>
                  <th className="py-2 px-2 text-left">Power</th>
                  <th className="py-2 px-2 text-left hidden sm:table-cell">Top cats</th>
                  <th className="py-2 pr-4 pl-2" />
                </tr>
              </thead>
              <tbody>
                {result.power_rankings.map((snap, i) => (
                  <TeamRow key={snap.team_id} snap={snap} rank={i + 1} tier={result.tiers} />
                ))}
              </tbody>
            </table>
          </div>

          {/* Trade opportunities */}
          {result.trade_opportunities.length > 0 && (
            <div className="space-y-3">
              <div className="section-label flex items-center gap-1.5">
                <ArrowLeftRight size={11} /> Top trade opportunities
              </div>
              {result.trade_opportunities.map((opp, i) => (
                <TradeOpp key={i} opp={opp} teamNames={teamNames} />
              ))}
            </div>
          )}

          <Blurb text={result.analysis_blurb} />
        </div>
      )}
    </div>
  )
}
