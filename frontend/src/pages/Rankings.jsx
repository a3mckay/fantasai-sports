import { useState, useEffect, useRef } from 'react'
import {
  TrendingUp, TrendingDown, Minus, BarChart2, RefreshCw,
  Search, X, ChevronDown, ChevronUp, ArrowUp,
} from 'lucide-react'
import { getRankings } from '../lib/api'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import PercentileBar from '../components/PercentileBar'
import { useLeague } from '../contexts/LeagueContext'

const POSITION_FILTERS = ['All', 'C', '1B', '2B', '3B', 'SS', 'OF', 'SP', 'RP', 'Batters', 'Pitchers']
const LEVEL_FILTERS    = ['All', 'MLB', 'MiLB']
const ROSTER_FILTERS   = [
  { val: 'all',            label: 'All Players'         },
  { val: 'mine',           label: 'My Team'             },
  { val: 'available',      label: 'My Team + Free Agents' },
]
const PAGE_SIZES       = [50, 100, 250, 'All']
const BLURB_TRUNCATE   = 150  // chars shown before "Show more"

const PITCHING_POS = new Set(['SP', 'RP'])

// For two-way players (e.g. Ohtani): show DH when appearing in a batting row.
function displayPositions(player) {
  if (player.stat_type === 'batting' && player.positions?.every(p => PITCHING_POS.has(p))) {
    return ['DH']
  }
  return player.positions ?? []
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function TrendIcon({ current, prior }) {
  if (prior == null) return <Minus size={12} className="text-slate-600" />
  const diff = prior - current // lower rank = better → positive diff = improved
  if (diff > 5)  return <TrendingUp   size={12} className="text-field-400"  />
  if (diff < -5) return <TrendingDown size={12} className="text-stitch-400" />
  return <Minus size={12} className="text-slate-500" />
}

function TrendBadge({ current, prior }) {
  if (prior == null) return <span className="text-slate-600 text-xs font-mono">—</span>
  const diff = prior - current
  if (diff === 0) return <span className="text-slate-600 text-xs font-mono">—</span>
  const cls = diff > 0 ? 'text-field-400' : 'text-stitch-400'
  return (
    <span className={`text-xs font-mono ${cls}`}>
      {diff > 0 ? '+' : ''}{diff}
    </span>
  )
}

function renderMarkdown(text) {
  const parts = text.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/g)
  return parts.map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**'))
      return <strong key={i} className="text-slate-200 font-semibold">{part.slice(2, -2)}</strong>
    if (part.startsWith('*') && part.endsWith('*'))
      return <em key={i}>{part.slice(1, -1)}</em>
    return part
  })
}

function Blurb({ text }) {
  const [open, setOpen] = useState(false)
  if (!text) return null
  const long = text.length > BLURB_TRUNCATE
  const displayed = open || !long ? text : text.slice(0, BLURB_TRUNCATE) + '…'
  return (
    <div className="mt-1.5 text-xs text-slate-400 leading-relaxed">
      {renderMarkdown(displayed)}
      {long && (
        <button
          onClick={e => { e.stopPropagation(); setOpen(v => !v) }}
          className="ml-1.5 text-field-500 hover:text-field-300 transition-colors font-medium whitespace-nowrap"
        >
          {open ? 'Show less' : 'Show more'}
        </button>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

const HORIZON_OPTIONS = [
  { val: 'week',   label: 'This Week'    },
  { val: 'month',  label: 'This Month'   },
  { val: 'season', label: 'Full Season'  },
]

export default function Rankings() {
  const { league, myTeam } = useLeague() || {}

  // Build player_id → team_name ownership map from league context
  const ownedByMap = {}
  if (league?.teams) {
    for (const team of league.teams) {
      for (const p of team.roster || []) {
        ownedByMap[p.player_id] = team.team_name
      }
    }
  }


  const [mode, setMode]             = useState('predictive')
  const [horizon, setHorizon]       = useState('season')
  const [posFilter, setPosFilter]   = useState('All')
  const [levelFilter, setLevelFilter] = useState('All')
  const [rosterFilter, setRosterFilter] = useState('all')
  const [search, setSearch]         = useState('')
  const [pageSize, setPageSize]     = useState(50)
  const [loading, setLoading]       = useState(false)
  const [error, setError]           = useState(null)
  const [predictive, setPredictive] = useState(null)
  const [lookback, setLookback]     = useState(null)
  const [expandedRows, setExpandedRows] = useState(new Set())
  const [showBackTop, setShowBackTop]   = useState(false)
  const searchRef     = useRef(null)
  const initialRender = useRef(true)

  useEffect(() => { fetchBoth() }, [])

  // Re-fetch predictive when horizon changes; skip the initial render since
  // fetchBoth already fetches the default horizon on mount.
  useEffect(() => {
    if (initialRender.current) { initialRender.current = false; return }
    fetchPredictive(horizon)
  }, [horizon])

  // Reset to first page whenever filters change
  useEffect(() => { setPageSize(50) }, [mode, posFilter, levelFilter, rosterFilter, search, horizon])

  // Back-to-top button
  useEffect(() => {
    const onScroll = () => setShowBackTop(window.scrollY > 500)
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  async function fetchPredictive(h) {
    setLoading(true)
    setError(null)
    try {
      const pred = await getRankings({ ranking_type: 'predictive', limit: 400, horizon: h })
      setPredictive(Array.isArray(pred) ? pred : (pred.rankings || pred))
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  async function fetchBoth() {
    setLoading(true)
    setError(null)
    try {
      const [pred, look] = await Promise.all([
        getRankings({ ranking_type: 'predictive', limit: 400, horizon }),
        getRankings({ ranking_type: 'lookback',   limit: 400 }),
      ])
      setPredictive(Array.isArray(pred) ? pred : (pred.rankings || pred))
      setLookback(Array.isArray(look)  ? look  : (look.rankings  || look))
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  // Rank-lookup maps for cross-referencing. Use player_id+stat_type as key
  // so two-way players (e.g. Ohtani batting vs pitching) are tracked separately.
  const rowKey  = p => `${p.player_id}_${p.stat_type}`
  const predMap = {}
  const lookMap = {}
  if (predictive) predictive.forEach(p => { predMap[rowKey(p)] = p.overall_rank })
  if (lookback)   lookback.forEach(p   => { lookMap[rowKey(p)] = p.overall_rank  })

  const activeList = mode === 'predictive' ? predictive : lookback

  function getPriorRank(player) {
    return mode === 'predictive'
      ? (lookMap[rowKey(player)] ?? null)
      : (predMap[rowKey(player)] ?? null)
  }

  function toggleRow(key) {
    setExpandedRows(prev => {
      const next = new Set(prev)
      next.has(key) ? next.delete(key) : next.add(key)
      return next
    })
  }

  // Normalize a string for accent-insensitive search:
  // "Julio Rodríguez" → "julio rodriguez"
  const normalize = s => s.normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase()

  // Filter by position + level + roster + search
  const filtered = (activeList ?? []).filter(p => {
    const posOk =
      posFilter === 'All'     ? true
      : posFilter === 'Batters'  ? p.stat_type === 'batting'
      : posFilter === 'Pitchers' ? p.stat_type === 'pitching'
      : displayPositions(p).some(pos => pos.toUpperCase() === posFilter.toUpperCase())

    const levelOk =
      levelFilter === 'All'  ? true
      : levelFilter === 'MiLB' ? p.is_prospect === true
      : /* MLB */               p.is_prospect !== true

    const ownedBy = ownedByMap[p.player_id]
    const myTeamName = myTeam?.team_name
    const rosterOk =
      rosterFilter === 'all'  ? true
      : rosterFilter === 'mine' ? ownedBy === myTeamName
      : /* available */          ownedBy === myTeamName || !ownedBy

    const searchOk = !search.trim() ||
      normalize(p.name).includes(normalize(search.trim()))

    return posOk && levelOk && rosterOk && searchOk
  })

  // Pagination: show all when searching, otherwise slice
  const isSearching = search.trim().length > 0
  const displayed   = isSearching || pageSize === 'All'
    ? filtered
    : filtered.slice(0, pageSize)

  return (
    <div className="space-y-5">

      {/* ── Header ── */}
      <div>
        <div className="flex items-center gap-2 mb-1">
          <BarChart2 size={18} className="text-field-400" />
          <h1 className="text-2xl font-bold text-white">Player Rankings</h1>
        </div>
        <p className="text-slate-500 text-sm">
          Top 400 players ranked by fantasy value. Compare current performance vs projected outlook.
        </p>
      </div>

      {/* ── Mode toggle + Search + Refresh ── */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex gap-2 shrink-0">
          {[
            { val: 'predictive', label: 'Projected' },
            { val: 'lookback',   label: 'Current'   },
          ].map(({ val, label }) => (
            <button
              key={val}
              onClick={() => setMode(val)}
              className={`px-4 py-2 rounded-lg text-sm font-medium border transition-colors ${
                mode === val
                  ? 'bg-field-700 border-field-600 text-white'
                  : 'bg-navy-800 border-navy-700 text-slate-400 hover:text-slate-200'
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {/* Search */}
        <div className="relative flex-1 min-w-[180px]">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500 pointer-events-none" />
          <input
            ref={searchRef}
            type="text"
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search players…"
            className="w-full bg-navy-800 border border-navy-700 rounded-lg text-sm text-white placeholder-slate-600 pl-9 pr-8 py-2 focus:outline-none focus:border-field-600 transition-colors"
          />
          {search && (
            <button
              onClick={() => { setSearch(''); searchRef.current?.focus() }}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-600 hover:text-slate-300 transition-colors"
            >
              <X size={13} />
            </button>
          )}
        </div>

        <button
          onClick={fetchBoth}
          disabled={loading}
          className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-200 transition-colors shrink-0"
        >
          <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />
          Refresh
        </button>
      </div>

      {/* ── Horizon selector (Projected mode only) ── */}
      {mode === 'predictive' && (
        <div className="flex items-center gap-2">
          <span className="text-xs text-slate-500 shrink-0">Horizon:</span>
          <div className="flex gap-1.5">
            {HORIZON_OPTIONS.map(({ val, label }) => (
              <button
                key={val}
                onClick={() => setHorizon(val)}
                className={`px-3 py-1 rounded-md text-xs font-medium border transition-colors ${
                  horizon === val
                    ? 'bg-field-800 border-field-600 text-field-300'
                    : 'bg-navy-900 border-navy-700 text-slate-500 hover:text-slate-300'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* ── Position filter pills ── */}
      <div className="flex items-center gap-1.5 flex-wrap">
        <span className="text-xs text-slate-300 font-semibold shrink-0 uppercase tracking-wide">Position:</span>
        <div className="flex flex-wrap gap-1.5">
        {POSITION_FILTERS.map(pos => (
          <button
            key={pos}
            onClick={() => setPosFilter(pos)}
            className={`px-2.5 py-1 rounded-md text-xs font-medium border transition-colors ${
              posFilter === pos
                ? 'bg-navy-600 border-navy-500 text-white'
                : 'bg-navy-900 border-navy-700 text-slate-500 hover:text-slate-300'
            }`}
          >
            {pos}
          </button>
        ))}
        </div>
      </div>

      {/* ── Level + Roster filters ── */}
      <div className="flex items-center gap-4 flex-wrap">
        {/* MLB / MiLB toggle */}
        <div className="flex items-center gap-1.5">
          <span className="text-xs text-slate-300 font-semibold shrink-0 uppercase tracking-wide">Level:</span>
          <div className="flex gap-1">
            {LEVEL_FILTERS.map(lf => (
              <button
                key={lf}
                onClick={() => setLevelFilter(lf)}
                className={`px-2.5 py-1 rounded-md text-xs font-medium border transition-colors ${
                  levelFilter === lf
                    ? 'bg-navy-600 border-navy-500 text-white'
                    : 'bg-navy-900 border-navy-700 text-slate-500 hover:text-slate-300'
                }`}
              >
                {lf}
              </button>
            ))}
          </div>
        </div>

        {/* Roster scope (only shown when league is connected) */}
        {league && (
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-slate-300 font-semibold shrink-0 uppercase tracking-wide">Show:</span>
            <div className="flex gap-1">
              {ROSTER_FILTERS.map(rf => (
                <button
                  key={rf.val}
                  onClick={() => setRosterFilter(rf.val)}
                  className={`px-2.5 py-1 rounded-md text-xs font-medium border transition-colors ${
                    rosterFilter === rf.val
                      ? 'bg-navy-600 border-navy-500 text-white'
                      : 'bg-navy-900 border-navy-700 text-slate-500 hover:text-slate-300'
                  }`}
                >
                  {rf.label}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      <ErrorBanner message={error} onClose={() => setError(null)} />
      {loading && <LoadingState message="Loading rankings…" />}

      {/* Result count when filtering */}
      {!loading && (search || posFilter !== 'All') && filtered.length > 0 && (
        <p className="text-xs text-slate-600">
          {filtered.length} player{filtered.length !== 1 ? 's' : ''}
          {isSearching && ` matching "${search}"`}
        </p>
      )}

      {/* ── Table ── */}
      {displayed.length > 0 && (
        <div className="rounded-xl border border-navy-700 overflow-x-auto">
          <table className="w-full">
            <thead className="bg-navy-900">
              <tr className="text-[10px] text-slate-500 uppercase tracking-wider">
                <th className="py-2.5 pl-4 pr-2 text-right w-10">#</th>
                <th className="py-2.5 px-2 text-center w-12 hidden sm:table-cell">
                  {mode === 'predictive' ? '↕ vs Current' : '↕ vs Proj.'}
                </th>
                <th className="py-2.5 px-3 text-left">Player</th>
                <th className="py-2.5 px-2 text-center w-16">Pos</th>
                <th className="py-2.5 pl-2 pr-2 text-right w-20">Score</th>
                <th className="py-2.5 pr-3 w-8" />
              </tr>
            </thead>
            <tbody>
              {displayed.map(player => {
                const key        = rowKey(player)
                const priorRank  = getPriorRank(player)
                const isExpanded = expandedRows.has(key)
                const hasCats    = Object.keys(player.category_contributions || {}).length > 0
                return (
                  <tr
                    key={key}
                    className="border-t border-navy-800 transition-colors hover:bg-navy-800/30"
                  >
                    {/* Rank */}
                    <td className="py-3 pl-4 pr-2 font-mono text-slate-400 text-sm text-right align-top pt-3.5">
                      {player.overall_rank}
                    </td>

                    {/* Trend */}
                    <td className="py-3 px-2 text-center hidden sm:table-cell align-top pt-3.5">
                      <div className="flex items-center justify-center gap-1">
                        <TrendIcon current={player.overall_rank} prior={priorRank} />
                        <TrendBadge current={player.overall_rank} prior={priorRank} />
                      </div>
                    </td>

                    {/* Player name + blurb + (expanded) category bar */}
                    <td className="py-3 px-3">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <span className="font-medium text-white text-sm">{player.name}</span>
{player.team && (
                          <span className="text-xs text-slate-500 hidden sm:inline">({player.team})</span>
                        )}
                        {/* Current IL badge */}
                        {player.injury_status && player.injury_status !== 'active' && (
                          <span
                            className="text-[10px] font-semibold text-red-400 bg-red-950/60 border border-red-800/50 rounded px-1 py-0.5 leading-none"
                            title={player.risk_note || player.injury_status.replace('_', ' ').toUpperCase()}
                          >
                            ⚠ {player.injury_status === 'il_60' ? 'IL60'
                               : player.injury_status === 'il_10' ? 'IL10'
                               : player.injury_status === 'out_for_season' ? 'OUT'
                               : 'DTD'}
                          </span>
                        )}
                        {/* Chronic risk flag (only shown when NOT on IL — avoid double-badging) */}
                        {(!player.injury_status || player.injury_status === 'active') && player.risk_flag && (
                          <span
                            className="text-[10px] font-semibold text-amber-400 bg-amber-950/40 border border-amber-800/40 rounded px-1 py-0.5 leading-none"
                            title={player.risk_note || (player.risk_flag === 'fragile' ? 'Injury-prone history' : 'Post-surgery risk')}
                          >
                            ⚠
                          </span>
                        )}
                        {/* Ownership badge — shows team name */}
                        {ownedByMap[player.player_id] && (
                          <span className="text-[10px] font-semibold text-emerald-300 bg-emerald-950/50 border border-emerald-800/50 rounded px-1.5 py-0.5 leading-none">
                            {ownedByMap[player.player_id]}
                          </span>
                        )}
                        {/* MiLB prospect badge — shown for minor-league players injected via PAV */}
                        {player.is_prospect && (
                          <span
                            className="text-[10px] font-semibold text-emerald-400 bg-emerald-950/50 border border-emerald-800/50 rounded px-1.5 py-0.5 leading-none"
                            title={`PAV Score: ${player.pav_score != null ? player.pav_score.toFixed(1) : '—'}/100`}
                          >
                            {player.team} · MiLB
                          </span>
                        )}
                      </div>
                      <Blurb text={player.blurb} />
                      {isExpanded && player.is_prospect && player.pav_score != null && (
                        <div className="mt-2 text-xs text-emerald-400/80">
                          PAV Score: <span className="font-semibold">{player.pav_score.toFixed(1)}</span>/100
                          <span className="text-slate-500 ml-2">— Prospect Adjusted Value</span>
                        </div>
                      )}
                      {isExpanded && player.stat_type && (
                        <div className="mt-1 text-[10px] text-slate-600 uppercase tracking-wide">
                          {player.stat_type === 'batting' ? 'Batting stats' : 'Pitching stats'}
                        </div>
                      )}
                      {isExpanded && hasCats && (
                        <div className="mt-3 pr-2">
                          <PercentileBar data={player.category_contributions} />
                        </div>
                      )}
                    </td>

                    {/* Position pills */}
                    <td className="py-3 px-2 text-center align-top pt-3.5">
                      <div className="flex flex-wrap gap-0.5 justify-center">
                        {displayPositions(player).slice(0, 2).map(pos => (
                          <span
                            key={pos}
                            className="stat-pill bg-navy-700 text-slate-400 text-[10px] px-1.5 py-0.5"
                          >
                            {pos}
                          </span>
                        ))}
                      </div>
                    </td>

                    {/* Score */}
                    <td className="py-3 pr-2 pl-2 text-right font-mono text-sm text-field-400 align-top pt-3.5">
                      {player.score?.toFixed(2) ?? '—'}
                    </td>

                    {/* Expand button */}
                    <td className="py-3 pr-3 text-right align-top pt-2.5">
                      {hasCats && (
                        <button
                          onClick={() => toggleRow(key)}
                          className={`p-1 rounded transition-colors ${
                            isExpanded
                              ? 'text-field-400 bg-field-900/40 hover:bg-field-900/60'
                              : 'text-slate-500 hover:text-slate-200 hover:bg-navy-700'
                          }`}
                          aria-label={isExpanded ? 'Collapse' : 'Expand category breakdown'}
                        >
                          {isExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Empty state */}
      {!loading && filtered.length === 0 && (activeList?.length ?? 0) > 0 && (
        <p className="text-center text-slate-600 text-sm py-8">
          {search
            ? `No players found matching "${search}".`
            : `No players found for position "${posFilter}".`}
        </p>
      )}

      {/* ── Pagination ── */}
      {!isSearching && filtered.length > 50 && (
        <div className="flex items-center justify-between gap-4 text-xs text-slate-500">
          <span>
            Showing <span className="text-slate-300">{displayed.length}</span> of{' '}
            <span className="text-slate-300">{filtered.length}</span> players
          </span>
          <div className="flex gap-1.5">
            {PAGE_SIZES.map(size => (
              <button
                key={size}
                onClick={() => setPageSize(size)}
                className={`px-2.5 py-1 rounded border transition-colors ${
                  pageSize === size
                    ? 'bg-navy-600 border-navy-500 text-white'
                    : 'bg-navy-900 border-navy-700 text-slate-500 hover:text-slate-300'
                }`}
              >
                {size}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* ── Back to top ── */}
      {showBackTop && (
        <button
          onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}
          className="fixed bottom-20 right-5 z-50 p-2.5 rounded-full bg-navy-700 border border-navy-600 text-slate-300 hover:bg-navy-600 hover:text-white shadow-lg transition-all"
          aria-label="Back to top"
        >
          <ArrowUp size={16} />
        </button>
      )}
    </div>
  )
}
