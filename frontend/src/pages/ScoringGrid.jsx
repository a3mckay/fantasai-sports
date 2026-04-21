import { useState, useEffect, useCallback } from 'react'
import { ChevronLeft, ChevronRight, Grid3X3 } from 'lucide-react'
import { getScoringGrid } from '../lib/api'
import Spinner from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'

// Rate stats where lower is better
const LOWER_IS_BETTER_FALLBACK = new Set(['ERA', 'WHIP'])

// Categories that are H/AB artifacts or Yahoo placeholder rows — never shown
const HIDE_CATS = new Set(['H/AB', 'Batting', 'Pitching', 'H'])

function formatStat(cat, value) {
  if (value == null) return '—'
  const n = Number(value)
  if (Number.isNaN(n)) return '—'
  const upper = cat.toUpperCase()
  if (['AVG', 'OBP', 'SLG', 'OPS'].includes(upper)) return n.toFixed(3)
  if (['ERA', 'WHIP'].includes(upper)) return n.toFixed(2)
  if (upper === 'IP') return n.toFixed(1)
  return Math.round(n).toString()
}

// Returns 'win' | 'loss' | 'tie' | 'own' | 'none'
function compare(selVal, rowVal, lowerIsBetter) {
  if (selVal == null || rowVal == null) return 'none'
  if (selVal === rowVal) return 'tie'
  return (lowerIsBetter ? selVal < rowVal : selVal > rowVal) ? 'win' : 'loss'
}

function cellBg(result) {
  switch (result) {
    case 'win':  return 'bg-field-700/50 text-field-300'
    case 'loss': return 'bg-stitch-500/20 text-stitch-300'
    case 'tie':  return 'text-slate-500'
    default:     return 'text-slate-300'
  }
}

export default function ScoringGrid() {
  const [data, setData]                   = useState(null)
  const [loading, setLoading]             = useState(true)
  const [error, setError]                 = useState(null)
  const [displayWeek, setDisplayWeek]     = useState(null)
  const [selectedTeamKey, setSelectedKey] = useState(null)

  const fetchGrid = useCallback(async (week) => {
    setLoading(true)
    setError(null)
    try {
      const result = await getScoringGrid(week)
      setData(result)
      setDisplayWeek(result.week)
      // Only set the default team on first load (not when user is navigating weeks)
      setSelectedKey(prev => prev ?? result.my_team_key ?? result.teams?.[0]?.team_key ?? null)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchGrid(null) }, [fetchGrid])

  const lowerIsBetterSet = new Set(data?.lower_is_better ?? [...LOWER_IS_BETTER_FALLBACK])
  const categories = (data?.categories ?? []).filter(c => !HIDE_CATS.has(c))
  const teams      = data?.teams ?? []
  const teamStats  = data?.team_stats ?? {}
  const maxWeek    = data?.current_week ?? displayWeek ?? 1

  const selectedStats = selectedTeamKey ? teamStats[selectedTeamKey] : null

  function getCmp(teamKey, cat) {
    if (!selectedTeamKey || teamKey === selectedTeamKey) return 'own'
    return compare(
      selectedStats?.[cat],
      teamStats[teamKey]?.[cat],
      lowerIsBetterSet.has(cat),
    )
  }

  function getRecord(teamKey) {
    let wins = 0, losses = 0, ties = 0
    for (const cat of categories) {
      const r = getCmp(teamKey, cat)
      if (r === 'win') wins++
      else if (r === 'loss') losses++
      else if (r === 'tie') ties++
    }
    return { wins, losses, ties }
  }

  // Aggregate record for the selected team row (wins/losses across all opponents × categories)
  function getAggregateRecord() {
    let wins = 0, losses = 0, ties = 0
    for (const team of teams) {
      if (team.team_key === selectedTeamKey) continue
      const r = getRecord(team.team_key)
      wins   += r.wins
      losses += r.losses
      ties   += r.ties
    }
    return { wins, losses, ties }
  }

  // Sort: selected team first, then others by descending wins against selected
  const sortedTeams = [...teams].sort((a, b) => {
    if (a.team_key === selectedTeamKey) return -1
    if (b.team_key === selectedTeamKey) return 1
    const ra = getRecord(a.team_key)
    const rb = getRecord(b.team_key)
    return rb.wins - ra.wins || ra.losses - rb.losses
  })

  function handleWeekChange(newWeek) {
    if (newWeek === displayWeek || newWeek < 1 || newWeek > maxWeek) return
    setDisplayWeek(newWeek)
    fetchGrid(newWeek)
  }

  const aggRecord = selectedTeamKey ? getAggregateRecord() : null

  return (
    <div className="space-y-4">
      {/* ── Header ────────────────────────────────────────────────────────── */}
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold text-slate-100 flex items-center gap-2">
            <Grid3X3 className="w-5 h-5 text-leather-400" />
            League Scoring Grid
          </h1>
          {aggRecord && selectedTeamKey && (
            <p className="text-sm text-slate-400 mt-0.5">
              <span className="text-field-300 font-semibold">{aggRecord.wins}W</span>
              {' – '}
              <span className="text-stitch-300 font-semibold">{aggRecord.losses}L</span>
              {' – '}
              <span className="text-slate-400 font-semibold">{aggRecord.ties}T</span>
              <span className="text-slate-500 ml-1.5">overall record vs all teams</span>
            </p>
          )}
        </div>

        <div className="flex items-center gap-3 flex-wrap">
          {/* Team selector */}
          {teams.length > 0 && (
            <div className="flex flex-col gap-0.5">
              <label className="text-xs text-slate-500 uppercase tracking-wide">Perspective</label>
              <select
                value={selectedTeamKey ?? ''}
                onChange={e => setSelectedKey(e.target.value)}
                className="text-sm bg-navy-800 border border-navy-600 rounded px-2 py-1.5 text-slate-200 focus:outline-none focus:ring-1 focus:ring-leather-400"
              >
                {teams.map(t => (
                  <option key={t.team_key} value={t.team_key}>
                    {t.team_name}{t.is_mine ? ' (My Team)' : ''}
                  </option>
                ))}
              </select>
            </div>
          )}

          {/* Week navigation */}
          {maxWeek > 0 && (
            <div className="flex flex-col gap-0.5">
              <label className="text-xs text-slate-500 uppercase tracking-wide">Week</label>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => handleWeekChange((displayWeek ?? 1) - 1)}
                  disabled={loading || (displayWeek ?? 1) <= 1}
                  className="p-1.5 rounded border border-navy-600 text-slate-400 hover:text-slate-200 hover:border-navy-500 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  <ChevronLeft className="w-3.5 h-3.5" />
                </button>
                <select
                  value={displayWeek ?? ''}
                  onChange={e => handleWeekChange(Number(e.target.value))}
                  disabled={loading}
                  className="text-sm bg-navy-800 border border-navy-600 rounded px-2 py-1.5 text-slate-200 focus:outline-none focus:ring-1 focus:ring-leather-400"
                >
                  {Array.from({ length: maxWeek }, (_, i) => i + 1).map(w => (
                    <option key={w} value={w}>Week {w}</option>
                  ))}
                </select>
                <button
                  onClick={() => handleWeekChange((displayWeek ?? 1) + 1)}
                  disabled={loading || (displayWeek ?? 1) >= maxWeek}
                  className="p-1.5 rounded border border-navy-600 text-slate-400 hover:text-slate-200 hover:border-navy-500 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                >
                  <ChevronRight className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>
          )}
        </div>
      </div>

      {error && <ErrorBanner message={error} />}

      {loading ? (
        <div className="flex items-center justify-center py-16 gap-3 text-slate-400">
          <Spinner />
          <span>Loading scoring grid…</span>
        </div>
      ) : data && categories.length > 0 ? (
        <>
          <div className="overflow-x-auto rounded-lg border border-navy-700 shadow-lg">
            <table className="min-w-full text-sm border-collapse">
              <thead>
                <tr className="bg-navy-800 border-b border-navy-700">
                  <th
                    scope="col"
                    className="sticky left-0 z-20 bg-navy-800 text-left px-3 py-2.5 text-xs font-semibold text-slate-400 uppercase tracking-wider min-w-[160px] border-r border-navy-700"
                  >
                    Team
                  </th>
                  {categories.map(cat => (
                    <th
                      key={cat}
                      scope="col"
                      className="px-2 py-2.5 text-center text-xs font-semibold text-slate-400 uppercase tracking-wider min-w-[52px]"
                    >
                      {cat}
                    </th>
                  ))}
                  <th
                    scope="col"
                    className="px-3 py-2.5 text-center text-xs font-semibold text-slate-400 uppercase tracking-wider min-w-[90px] border-l border-navy-700"
                  >
                    Record
                  </th>
                </tr>
              </thead>
              <tbody>
                {sortedTeams.map((team, idx) => {
                  const isSelected = team.team_key === selectedTeamKey
                  const record     = isSelected ? aggRecord : getRecord(team.team_key)

                  const rowBase = isSelected
                    ? 'bg-navy-700/60'
                    : idx % 2 === 0
                      ? 'bg-navy-900'
                      : 'bg-navy-800/60'

                  const stickyBase = isSelected
                    ? 'bg-navy-700/80'
                    : idx % 2 === 0
                      ? 'bg-navy-900'
                      : 'bg-navy-800'

                  return (
                    <tr
                      key={team.team_key}
                      onClick={() => setSelectedKey(team.team_key)}
                      className={`cursor-pointer border-b border-navy-700/50 transition-colors hover:bg-navy-700/40 ${rowBase}`}
                    >
                      {/* Sticky team name */}
                      <td
                        className={`sticky left-0 z-10 px-3 py-2 border-r border-navy-700/50 whitespace-nowrap ${stickyBase}`}
                      >
                        <div className="flex items-center gap-1.5">
                          {isSelected && (
                            <span className="text-leather-400 text-[10px] font-bold">▶</span>
                          )}
                          <span className={`font-medium ${isSelected ? 'text-leather-200' : 'text-slate-200'}`}>
                            {team.team_name}
                          </span>
                          {team.is_mine && !isSelected && (
                            <span className="text-slate-500 text-[10px]">(me)</span>
                          )}
                        </div>
                      </td>

                      {/* Category cells */}
                      {categories.map(cat => {
                        const cmp = getCmp(team.team_key, cat)
                        const val = teamStats[team.team_key]?.[cat]
                        return (
                          <td
                            key={cat}
                            className={`px-2 py-2 text-center tabular-nums ${
                              isSelected ? 'text-slate-200 font-medium' : cellBg(cmp)
                            }`}
                          >
                            {formatStat(cat, val)}
                          </td>
                        )
                      })}

                      {/* Record column */}
                      <td className="px-3 py-2 text-center border-l border-navy-700/50 tabular-nums">
                        {record ? (
                          <span className={`font-semibold text-xs tracking-wide ${isSelected ? 'text-slate-300' : 'text-slate-400'}`}>
                            <span className="text-field-400">{record.wins}</span>
                            <span className="text-slate-600 mx-0.5">-</span>
                            <span className="text-stitch-400">{record.losses}</span>
                            <span className="text-slate-600 mx-0.5">-</span>
                            <span className="text-slate-500">{record.ties}</span>
                          </span>
                        ) : (
                          <span className="text-slate-600">—</span>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          <p className="text-xs text-slate-500 text-right">
            Colors show{' '}
            <span className="text-leather-400 font-medium">
              {teams.find(t => t.team_key === selectedTeamKey)?.team_name ?? 'selected team'}
            </span>
            's result vs each row.{' '}
            <span className="text-field-400">Green</span> = win,{' '}
            <span className="text-stitch-400">red</span> = loss,{' '}
            gray = tie. Click any row to switch perspective.
          </p>
        </>
      ) : !loading && (
        <div className="text-center py-16 text-slate-500">
          No scoring data available yet.
        </div>
      )}
    </div>
  )
}
