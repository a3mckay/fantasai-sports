import { useState, useEffect, useCallback, useRef } from 'react'
import { RefreshCw, Swords } from 'lucide-react'
import { req } from '../lib/api'
import Spinner from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'

// ---------------------------------------------------------------------------
// Category metadata
// ---------------------------------------------------------------------------

// Rate stats where lower is better — the team with the LOWER value wins the edge
const LOWER_IS_BETTER = new Set(['ERA', 'WHIP', 'BB/9', 'BB9', 'BB', 'HBP'])

// Format a stat value based on the category name
function formatStat(cat, value) {
  if (value === null || value === undefined) return '—'
  const n = Number(value)
  if (Number.isNaN(n)) return String(value)

  const upper = cat.toUpperCase()

  // AVG / OBP / SLG / OPS and any ratio that reads as a decimal
  if (['AVG', 'OBP', 'SLG', 'OPS', 'BABIP'].includes(upper)) {
    return n.toFixed(3)
  }
  // ERA / WHIP — 2 decimal places
  if (['ERA', 'WHIP'].includes(upper)) {
    return n.toFixed(2)
  }
  // Per-9 rates — 1 decimal
  if (['K/9', 'K9', 'BB/9', 'BB9', 'H/9', 'HR/9', 'SO9'].includes(upper)) {
    return n.toFixed(1)
  }
  // IP — 1 decimal
  if (upper === 'IP') {
    return n.toFixed(1)
  }
  // Default: counting stats as integers
  return Math.round(n).toString()
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function EdgeSummary({ categoryProjections }) {
  let team1Leads = 0
  let team2Leads = 0

  for (const proj of Object.values(categoryProjections)) {
    if (proj.edge === 'team1') team1Leads++
    else if (proj.edge === 'team2') team2Leads++
  }

  if (team1Leads === 0 && team2Leads === 0) return null

  return (
    <div className="flex items-center gap-2 text-xs text-slate-400">
      <span className="text-field-400 font-semibold">{team1Leads}</span>
      <span>–</span>
      <span className="text-stitch-400 font-semibold">{team2Leads}</span>
      <span className="text-slate-500">category edge</span>
      {team1Leads !== team2Leads && (
        <span className="text-slate-500">
          ({team1Leads > team2Leads ? 'left team leads' : 'right team leads'})
        </span>
      )}
    </div>
  )
}

function CategoryTable({ categoryProjections, team1Name, team2Name }) {
  const entries = Object.entries(categoryProjections)
  if (entries.length === 0) return null

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm border-collapse">
        <thead>
          <tr>
            <th className="text-right pr-3 pb-1.5 text-xs font-medium text-slate-500 w-1/3">
              {team1Name}
            </th>
            <th className="text-center pb-1.5 text-xs font-medium text-slate-500 w-1/3">
              Category
            </th>
            <th className="text-left pl-3 pb-1.5 text-xs font-medium text-slate-500 w-1/3">
              {team2Name}
            </th>
          </tr>
        </thead>
        <tbody>
          {entries.map(([cat, proj], idx) => {
            const lowerIsBetter = LOWER_IS_BETTER.has(cat.toUpperCase())
            const isTossUp = proj.edge === 'toss_up'

            // For lower-is-better stats, the winner is the one with a LOWER value
            const team1Wins = lowerIsBetter
              ? proj.edge === 'team1'  // "team1" edge still means team1 wins
              : proj.edge === 'team1'

            const team2Wins = lowerIsBetter
              ? proj.edge === 'team2'
              : proj.edge === 'team2'

            const team1Class = isTossUp
              ? 'text-slate-400'
              : team1Wins
                ? 'text-field-400 font-semibold'
                : 'text-slate-500'

            const team2Class = isTossUp
              ? 'text-slate-400'
              : team2Wins
                ? 'text-stitch-400 font-semibold'
                : 'text-slate-500'

            const catClass = isTossUp ? 'text-slate-500' : 'text-slate-300'

            return (
              <tr
                key={cat}
                className={idx % 2 === 0 ? 'bg-navy-800/40' : ''}
              >
                <td className={`text-right pr-3 py-1.5 tabular-nums ${team1Class}`}>
                  {formatStat(cat, proj.team1)}
                </td>
                <td className={`text-center py-1.5 font-medium text-xs ${catClass}`}>
                  {cat}
                </td>
                <td className={`text-left pl-3 py-1.5 tabular-nums ${team2Class}`}>
                  {formatStat(cat, proj.team2)}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function LiveStatsSection({ liveStats, categoryProjections }) {
  if (!liveStats || Object.keys(liveStats).length === 0) return null

  const cats = Object.keys(liveStats)

  return (
    <div className="border-t border-navy-700 pt-3 mt-3 space-y-2">
      <div className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
        Live (Actual vs Projected)
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs border-collapse">
          <thead>
            <tr>
              <th className="text-left pb-1 text-slate-500 font-medium w-1/5">Cat</th>
              <th className="text-center pb-1 text-slate-500 font-medium" colSpan={2}>
                Team 1
              </th>
              <th className="text-center pb-1 text-slate-500 font-medium" colSpan={2}>
                Team 2
              </th>
            </tr>
            <tr>
              <th className="text-left pb-1 text-slate-600 font-normal"></th>
              <th className="text-center pb-1 text-slate-600 font-normal">Live</th>
              <th className="text-center pb-1 text-slate-600 font-normal">Proj</th>
              <th className="text-center pb-1 text-slate-600 font-normal">Live</th>
              <th className="text-center pb-1 text-slate-600 font-normal">Proj</th>
            </tr>
          </thead>
          <tbody>
            {cats.map((cat, idx) => {
              const live = liveStats[cat] || {}
              const proj = categoryProjections[cat] || {}
              return (
                <tr key={cat} className={idx % 2 === 0 ? 'bg-navy-800/30' : ''}>
                  <td className="py-1 text-slate-400 font-medium">{cat}</td>
                  <td className="py-1 text-center text-white tabular-nums">
                    {live.team1 !== undefined ? formatStat(cat, live.team1) : '—'}
                  </td>
                  <td className="py-1 text-center text-slate-500 tabular-nums">
                    {proj.team1 !== undefined ? formatStat(cat, proj.team1) : '—'}
                  </td>
                  <td className="py-1 text-center text-white tabular-nums">
                    {live.team2 !== undefined ? formatStat(cat, live.team2) : '—'}
                  </td>
                  <td className="py-1 text-center text-slate-500 tabular-nums">
                    {proj.team2 !== undefined ? formatStat(cat, proj.team2) : '—'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function SuggestionsSection({ suggestions }) {
  if (!suggestions || suggestions.length === 0) return null

  return (
    <div className="border-t border-navy-700 pt-3 mt-3 space-y-2">
      <div className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
        Suggested Moves
      </div>
      <ul className="space-y-1.5">
        {suggestions.map((s, i) => (
          <li key={i} className="text-sm text-slate-300 flex items-start gap-2">
            <span
              className={`mt-0.5 text-[10px] font-semibold px-1.5 py-0.5 rounded uppercase shrink-0 ${
                s.type === 'add'
                  ? 'bg-field-900 text-field-400 border border-field-700'
                  : 'bg-navy-800 text-slate-400 border border-navy-700'
              }`}
            >
              {s.type || 'tip'}
            </span>
            <span>
              {s.player_name && (
                <span className="font-medium text-white">{s.player_name}: </span>
              )}
              {s.rationale}
              {s.category_impact && (
                <span className="text-slate-500"> [{s.category_impact}]</span>
              )}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function MatchupCard({ matchup }) {
  return (
    <div className="bg-navy-900 border border-navy-700 rounded-xl p-5 space-y-4">
      {/* Team header */}
      <div className="flex items-start justify-between gap-4">
        {/* Team 1 */}
        <div className="flex-1 min-w-0">
          <div className="text-white font-bold text-sm truncate">
            {matchup.team1_name}
          </div>
          {matchup.manager1_name && (
            <div className="text-slate-400 text-xs mt-0.5 truncate">
              {matchup.manager1_name}
            </div>
          )}
        </div>

        {/* VS badge */}
        <div className="flex items-center justify-center shrink-0">
          <div className="bg-navy-800 border border-navy-600 rounded-lg px-2.5 py-1 flex items-center gap-1.5">
            <Swords size={11} className="text-slate-500" />
            <span className="text-slate-500 text-xs font-medium">VS</span>
          </div>
        </div>

        {/* Team 2 */}
        <div className="flex-1 min-w-0 text-right">
          <div className="text-white font-bold text-sm truncate">
            {matchup.team2_name}
          </div>
          {matchup.manager2_name && (
            <div className="text-slate-400 text-xs mt-0.5 truncate">
              {matchup.manager2_name}
            </div>
          )}
        </div>
      </div>

      {/* Edge summary */}
      {Object.keys(matchup.category_projections).length > 0 && (
        <div className="flex justify-center">
          <EdgeSummary categoryProjections={matchup.category_projections} />
        </div>
      )}

      {/* Category table */}
      {Object.keys(matchup.category_projections).length > 0 && (
        <CategoryTable
          categoryProjections={matchup.category_projections}
          team1Name={matchup.team1_name}
          team2Name={matchup.team2_name}
        />
      )}

      {/* Live stats (mid-week actuals) */}
      <LiveStatsSection
        liveStats={matchup.live_stats}
        categoryProjections={matchup.category_projections}
      />

      {/* Suggestions */}
      <SuggestionsSection suggestions={matchup.suggestions} />

      {/* Claude narrative */}
      {matchup.narrative && (
        <p className="text-slate-400 italic text-sm border-t border-navy-700 pt-3 mt-3 leading-relaxed">
          {matchup.narrative}
        </p>
      )}

      {/* Generated-at timestamp */}
      {matchup.generated_at && (
        <div className="text-[11px] text-slate-600 text-right">
          Updated {new Date(matchup.generated_at).toLocaleString('en-US', {
            month: 'short', day: 'numeric',
            hour: 'numeric', minute: '2-digit',
          })}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

const POLL_INTERVAL = 6000   // ms between polls while generating
const POLL_MAX      = 10     // max poll attempts (~60 seconds)

export default function Matchups() {
  const [matchups, setMatchups]   = useState([])
  const [loading, setLoading]     = useState(true)
  const [error, setError]         = useState(null)
  const [refreshing, setRefreshing] = useState(false)

  const didAutoTrigger = useRef(false)
  const pollCount      = useRef(0)
  const pollTimer      = useRef(null)

  const currentWeek = matchups.length > 0 ? matchups[0].week : null

  const load = useCallback(async () => {
    try {
      const data = await req('GET', '/api/v1/matchups')
      setMatchups(data)
      return data.length
    } catch (e) {
      setError(e.message || 'Failed to load matchups')
      return 0
    }
  }, [])

  // Poll until results appear or we hit the limit
  const startPolling = useCallback(() => {
    pollCount.current = 0
    const tick = async () => {
      const count = await load()
      pollCount.current += 1
      if (count > 0 || pollCount.current >= POLL_MAX) {
        setRefreshing(false)
        setLoading(false)
      } else {
        pollTimer.current = setTimeout(tick, POLL_INTERVAL)
      }
    }
    pollTimer.current = setTimeout(tick, POLL_INTERVAL)
  }, [load])

  // On mount: load then auto-trigger analysis if empty
  useEffect(() => {
    const init = async () => {
      setLoading(true)
      const count = await load()
      if (count === 0 && !didAutoTrigger.current) {
        didAutoTrigger.current = true
        setRefreshing(true)
        try {
          await req('POST', '/api/v1/matchups/refresh')
          startPolling()
        } catch {
          setRefreshing(false)
          setLoading(false)
        }
      } else {
        setLoading(false)
      }
    }
    init()
    return () => { if (pollTimer.current) clearTimeout(pollTimer.current) }
  }, [load, startPolling])

  const handleRefresh = async () => {
    if (pollTimer.current) clearTimeout(pollTimer.current)
    setRefreshing(true)
    setError(null)
    try {
      await req('POST', '/api/v1/matchups/refresh')
      startPolling()
    } catch (e) {
      setError(e.message || 'Refresh failed')
      setRefreshing(false)
    }
  }

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white">Matchup Analyzer</h1>
          {currentWeek !== null && (
            <p className="text-sm text-slate-400 mt-0.5">
              Week {currentWeek} — projected category totals
            </p>
          )}
        </div>
        <button
          onClick={handleRefresh}
          disabled={refreshing || loading}
          className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm bg-navy-800 border border-navy-600 text-slate-300 hover:text-white hover:border-navy-500 transition-colors disabled:opacity-50"
        >
          <RefreshCw size={14} className={refreshing ? 'animate-spin' : ''} />
          {refreshing ? 'Analyzing…' : 'Refresh'}
        </button>
      </div>

      <ErrorBanner error={error} />

      {/* Body */}
      {loading || refreshing ? (
        <div className="bg-navy-900 border border-navy-700 rounded-xl p-10 text-center space-y-3">
          <Spinner />
          <p className="text-slate-400 text-sm mt-3">
            {refreshing
              ? 'Generating matchup projections — this takes about 30 seconds…'
              : 'Loading…'}
          </p>
        </div>
      ) : matchups.length === 0 ? (
        <div className="bg-navy-900 border border-navy-700 rounded-xl p-10 text-center space-y-3">
          <Swords size={36} className="mx-auto text-slate-600" />
          <p className="text-slate-400 font-medium">No matchup analysis generated.</p>
          <p className="text-slate-500 text-sm">
            Click <span className="font-semibold text-slate-300">Refresh</span> to try again.
          </p>
        </div>
      ) : (
        <div className="space-y-6">
          {matchups.map(m => (
            <MatchupCard key={m.id} matchup={m} />
          ))}
        </div>
      )}
    </div>
  )
}
