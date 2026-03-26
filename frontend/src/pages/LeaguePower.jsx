import { useState, useEffect, useCallback, useRef } from 'react'
import { Zap, Crown, RefreshCw, ChevronDown, ChevronUp } from 'lucide-react'
import { leaguePower } from '../lib/api'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import Blurb from '../components/Blurb'
import CategoryStrengthBar from '../components/CategoryStrengthBar'
import { useLeague } from '../contexts/LeagueContext'

// ── Constants ──────────────────────────────────────────────────────────────

const TIER_ORDER = ['contender', 'middle', 'rebuilding']

const TIER_CFG = {
  contender:  { label: 'Contenders',  labelColor: 'text-field-300',    bgColor: 'bg-field-900/50',       dotColor: 'bg-field-500'   },
  middle:     { label: 'Middle Pack', labelColor: 'text-leather-300',  bgColor: 'bg-leather-500/10',     dotColor: 'bg-leather-400' },
  rebuilding: { label: 'Rebuilding',  labelColor: 'text-slate-400',    bgColor: 'bg-navy-800/80',        dotColor: 'bg-slate-600'   },
}

// ── Helpers ────────────────────────────────────────────────────────────────

/** Compute within-league category percentiles for every team.
 *  Returns { [team_id]: { [cat]: 0–100 } }
 */
function computeLeaguePercentiles(powerRankings) {
  const n = powerRankings.length
  if (n === 0) return {}
  const allCats = Object.keys(powerRankings[0]?.category_strengths || {})
  const result = {}
  for (const snap of powerRankings) {
    const pcts = {}
    for (const cat of allCats) {
      const myScore = snap.category_strengths[cat] ?? -Infinity
      const below   = powerRankings.filter(t => (t.category_strengths[cat] ?? -Infinity) < myScore).length
      pcts[cat] = Math.round((below / n) * 100)
    }
    result[snap.team_id] = pcts
  }
  return result
}

function timeAgo(date) {
  if (!date) return ''
  const mins = Math.floor((Date.now() - date.getTime()) / 60000)
  if (mins < 1) return 'just now'
  if (mins === 1) return '1 min ago'
  return `${mins} min ago`
}

// ── Sub-components ─────────────────────────────────────────────────────────

function TierDivider({ tier, count }) {
  const cfg = TIER_CFG[tier] || { label: tier, labelColor: 'text-slate-400', bgColor: 'bg-navy-800', dotColor: 'bg-slate-600' }
  return (
    <div className={`flex items-center gap-2.5 px-3 py-2 rounded-lg mt-3 mb-1 ${cfg.bgColor}`}>
      <div className={`w-1.5 h-3.5 rounded-full shrink-0 ${cfg.dotColor}`} />
      <span className={`text-xs font-bold uppercase tracking-widest ${cfg.labelColor}`}>
        {cfg.label}
      </span>
      {count != null && (
        <span className={`text-[10px] ${cfg.labelColor} opacity-50`}>
          · {count} team{count !== 1 ? 's' : ''}
        </span>
      )}
    </div>
  )
}

function TeamRow({ snap, rank, isWinner, isMine, leaguePcts, numTeams }) {
  const [expanded, setExpanded] = useState(false)
  const hasCats = leaguePcts && Object.keys(leaguePcts).length > 0

  return (
    <div className={`p-4 rounded-xl border transition-colors ${
      isMine   ? 'bg-[#6001d2]/10 border-[#6001d2]/40' :
      isWinner ? 'bg-field-950/40 border-field-700'    :
                 'bg-navy-800 border-navy-700'
    }`}>
      <div className="flex items-center gap-3">
        <span className="font-bold font-mono text-slate-500 text-sm w-6 text-right shrink-0">{rank}</span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            {isWinner && <Crown size={13} className="text-yellow-400 shrink-0" />}
            <span className="font-semibold text-white">{snap.team_name}</span>
            {isMine && (
              <span className="text-[10px] font-semibold text-[#6001d2] bg-[#6001d2]/10 border border-[#6001d2]/30 rounded px-1.5 py-0.5">
                Your team
              </span>
            )}
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
            className={`p-1.5 rounded-lg transition-colors shrink-0 ${
              expanded
                ? 'text-field-400 bg-field-900/40 hover:bg-field-900/60'
                : 'text-slate-500 hover:text-slate-200 hover:bg-navy-700'
            }`}
            aria-label={expanded ? 'Collapse category breakdown' : 'Expand category breakdown'}
          >
            {expanded ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
          </button>
        )}
      </div>

      {expanded && hasCats && (
        <div className="mt-4 pt-3 border-t border-navy-700">
          <CategoryStrengthBar
            data={leaguePcts}
            numTeams={numTeams}
            asPercentiles={true}
          />
        </div>
      )}
    </div>
  )
}

// ── Main page ──────────────────────────────────────────────────────────────

export default function LeaguePower() {
  const { league, myTeam } = useLeague() || {}
  const [loading,  setLoading]  = useState(false)
  const [error,    setError]    = useState(null)
  const [result,   setResult]   = useState(null)
  const [loadedAt, setLoadedAt] = useState(null)
  const [tick,     setTick]     = useState(0)  // forces timeAgo to re-render
  const loadedRef = useRef(false)

  const load = useCallback(async () => {
    if (!league?.league_id) return
    setLoading(true)
    setError(null)
    try {
      const res = await leaguePower(league.league_id)
      setResult(res)
      setLoadedAt(new Date())
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [league?.league_id])

  // Auto-load on first visit
  useEffect(() => {
    if (league?.league_id && !loadedRef.current) {
      loadedRef.current = true
      load()
    }
  }, [league?.league_id, load])

  // Tick every 30s so "X min ago" stays fresh
  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 30_000)
    return () => clearInterval(id)
  }, [])

  // Pre-compute within-league percentiles once, not per render
  const leaguePercentiles = result
    ? computeLeaguePercentiles(result.power_rankings)
    : {}

  // Build tier map: team_id → tier name, and count per tier
  const tierMap = {}
  const tierCounts = {}
  Object.entries(result?.tiers || {}).forEach(([tier, ids]) => {
    tierCounts[tier] = ids.length
    ids.forEach(id => { tierMap[id] = tier })
  })

  // Use league.num_teams as the authoritative team count for rank display
  const numTeams = league?.num_teams || result?.power_rankings.length || 12

  // Render ranked list with inline tier dividers
  let lastTier = null
  const rankedRows = (result?.power_rankings ?? []).map((snap, i) => {
    const tier = tierMap[snap.team_id]
    const showDivider = tier && tier !== lastTier
    if (tier) lastTier = tier
    return (
      <div key={snap.team_id}>
        {showDivider && <TierDivider tier={tier} count={tierCounts[tier]} />}
        <TeamRow
          snap={snap}
          rank={i + 1}
          isWinner={i === 0}
          isMine={snap.team_id === myTeam?.team_id}
          leaguePcts={leaguePercentiles[snap.team_id]}
          numTeams={numTeams}
        />
      </div>
    )
  })

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <div className="flex items-center gap-2 mb-1">
          <Zap size={18} className="text-field-400" />
          <h1 className="text-2xl font-bold text-white">League Power Rankings</h1>
        </div>
        <p className="text-slate-500 text-sm">
          Full-league roster strength rankings with tier groupings and AI analysis.
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
          {loadedAt && (
            <span className="text-xs text-slate-600" key={tick}>
              Updated {timeAgo(loadedAt)}
            </span>
          )}
          <button
            onClick={load}
            disabled={loading}
            className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-white border border-navy-600 hover:border-navy-500 bg-navy-800 px-3 py-1.5 rounded-lg transition-colors disabled:opacity-50"
          >
            <RefreshCw size={12} className={loading ? 'animate-spin' : ''} />
            Refresh
          </button>
        </div>
      )}

      <ErrorBanner message={error} onClose={() => setError(null)} />
      {loading && !result && <LoadingState message="Ranking all league rosters…" />}

      {result && (
        <div className="space-y-6">
          {/* Ranked list with inline tier dividers */}
          <div className="space-y-2">
            {rankedRows}
          </div>

          {/* AI Analysis */}
          <Blurb text={result.analysis_blurb} />
        </div>
      )}
    </div>
  )
}
