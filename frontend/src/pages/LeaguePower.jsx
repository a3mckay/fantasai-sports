import { useState } from 'react'
import { Zap, Play, Crown, ArrowLeftRight, RefreshCw } from 'lucide-react'
import { leaguePower } from '../lib/api'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import Blurb from '../components/Blurb'
import PercentileBar from '../components/PercentileBar'
import { useLeague } from '../contexts/LeagueContext'

const TIER_LABELS = {
  contender: { label: 'Contenders', color: 'text-field-300 bg-field-950/50 border-field-700' },
  middle:    { label: 'Middle Pack', color: 'text-leather-300 bg-leather-500/5 border-leather-500/30' },
  rebuilding:{ label: 'Rebuilding',  color: 'text-slate-400 bg-navy-800 border-navy-600' },
}

function TeamRow({ snap, rank, isWinner, isMine }) {
  const [expanded, setExpanded] = useState(false)
  const hasCats = Object.keys(snap.category_strengths || {}).length > 0
  return (
    <div className={`p-4 rounded-xl border transition-colors ${
      isMine    ? 'bg-[#6001d2]/10 border-[#6001d2]/40' :
      isWinner  ? 'bg-field-950/40 border-field-700' :
                  'bg-navy-800 border-navy-700'
    }`}>
      <div className="flex items-center gap-3">
        <span className="font-bold font-mono text-slate-500 text-sm w-6 text-right shrink-0">{rank}</span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            {isWinner && <Crown size={13} className="text-yellow-400 shrink-0" />}
            <span className="font-semibold text-white">{snap.team_name}</span>
            {isMine && <span className="text-[10px] font-semibold text-[#6001d2] bg-[#6001d2]/10 border border-[#6001d2]/30 rounded px-1.5 py-0.5">Your team</span>}
            <span className="ml-auto font-mono text-sm text-field-400">{snap.power_score.toFixed(2)}</span>
          </div>
          <div className="flex flex-wrap gap-1 mt-1.5">
            {snap.strong_cats.slice(0, 4).map(c => (
              <span key={c} className="stat-pill bg-field-900 text-field-300 text-[10px]">{c} ▲</span>
            ))}
            {snap.weak_cats.slice(0, 3).map(c => (
              <span key={c} className="stat-pill bg-red-950/50 text-red-400 text-[10px]">{c} ▼</span>
            ))}
          </div>
          {snap.top_players.length > 0 && (
            <p className="text-xs text-slate-500 mt-1">Top: {snap.top_players.join(', ')}</p>
          )}
        </div>
        {hasCats && (
          <button
            onClick={() => setExpanded(v => !v)}
            className="text-slate-600 hover:text-slate-300 text-xs shrink-0"
          >
            {expanded ? 'less' : 'more'}
          </button>
        )}
      </div>
      {expanded && hasCats && (
        <div className="mt-3">
          <PercentileBar data={snap.category_strengths} />
        </div>
      )}
    </div>
  )
}

function TierSection({ tier, teamIds, snapshots, myTeamId }) {
  const cfg = TIER_LABELS[tier] || { label: tier, color: 'text-slate-400 bg-navy-800 border-navy-600' }
  const tierSnaps = teamIds
    .map(id => snapshots.find(s => s.team_id === id))
    .filter(Boolean)
  if (!tierSnaps.length) return null
  return (
    <div>
      <span className={`inline-block text-xs font-semibold rounded px-2 py-0.5 border mb-2 ${cfg.color}`}>
        {cfg.label}
      </span>
      <div className="space-y-2">
        {tierSnaps.map(snap => (
          <div key={snap.team_id} className="text-sm text-slate-400 pl-2">
            {snap.team_name}
          </div>
        ))}
      </div>
    </div>
  )
}

export default function LeaguePower() {
  const { league, myTeam } = useLeague() || {}
  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState(null)
  const [result, setResult]   = useState(null)

  async function load() {
    if (!league?.league_id) {
      setError('No Yahoo league connected. Connect from Profile and re-sync.')
      return
    }
    setLoading(true)
    setError(null)
    try {
      const res = await leaguePower(league.league_id)
      setResult(res)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <div className="flex items-center gap-2 mb-1">
          <Zap size={18} className="text-field-400" />
          <h1 className="text-2xl font-bold text-white">League Power Rankings</h1>
        </div>
        <p className="text-slate-500 text-sm">
          Full-league roster strength rankings with tiers and trade opportunity surfacing.
        </p>
      </div>

      {!league && (
        <div className="p-4 rounded-xl border border-amber-800/40 bg-amber-950/20 text-amber-400 text-sm">
          Connect your Yahoo account from Profile to use this feature.
        </div>
      )}

      {league && (
        <div className="flex items-center gap-3">
          <span className="text-slate-400 text-sm">{league.league_name}</span>
          <button
            onClick={load}
            disabled={loading}
            className="flex items-center gap-1.5 btn-primary"
          >
            {result
              ? <><RefreshCw size={14} /> Refresh</>
              : <><Play size={14} /> Run Power Rankings</>
            }
          </button>
        </div>
      )}

      <ErrorBanner message={error} onClose={() => setError(null)} />
      {loading && <LoadingState message="Ranking all league rosters…" />}

      {result && (
        <div className="space-y-6">
          {/* Power rankings list */}
          <div className="space-y-2">
            {result.power_rankings.map((snap, i) => (
              <TeamRow
                key={snap.team_id}
                snap={snap}
                rank={i + 1}
                isWinner={snap.team_id === result.power_rankings[0]?.team_id}
                isMine={snap.team_id === myTeam?.team_id}
              />
            ))}
          </div>

          {/* Tiers summary */}
          {Object.keys(result.tiers || {}).length > 0 && (
            <div className="card space-y-4">
              <div className="section-label">League tiers</div>
              {Object.entries(result.tiers).map(([tier, ids]) => (
                <TierSection
                  key={tier}
                  tier={tier}
                  teamIds={ids}
                  snapshots={result.power_rankings}
                  myTeamId={myTeam?.team_id}
                />
              ))}
            </div>
          )}

          {/* Trade opportunities */}
          {result.trade_opportunities?.length > 0 && (
            <div className="card space-y-3">
              <div className="section-label">Top trade opportunities</div>
              {result.trade_opportunities.slice(0, 8).map((opp, i) => {
                const nameA = result.power_rankings.find(s => s.team_id === opp.team_a_id)?.team_name || `Team ${opp.team_a_id}`
                const nameB = result.power_rankings.find(s => s.team_id === opp.team_b_id)?.team_name || `Team ${opp.team_b_id}`
                return (
                  <div key={i} className="p-3 rounded-lg bg-navy-800 border border-navy-700">
                    <div className="flex items-center gap-2 text-sm mb-1.5">
                      <span className="font-medium text-white">{nameA}</span>
                      <ArrowLeftRight size={12} className="text-slate-500 shrink-0" />
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
              })}
            </div>
          )}

          <Blurb text={result.analysis_blurb} />
        </div>
      )}
    </div>
  )
}
