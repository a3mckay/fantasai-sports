import { useState, useEffect } from 'react'
import { TrendingUp, TrendingDown, Minus, BarChart2, RefreshCw } from 'lucide-react'
import { getRankings } from '../lib/api'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'

const POSITION_FILTERS = ['All', 'SP', 'RP', 'OF', 'SS', '2B', '3B', '1B', 'C']

function TrendIcon({ current, prior }) {
  if (prior == null) return <Minus size={12} className="text-slate-600" />
  const diff = prior - current // lower rank = better, so positive diff = improved
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

export default function Rankings() {
  const [mode, setMode]             = useState('predictive')   // 'predictive' | 'lookback'
  const [posFilter, setPosFilter]   = useState('All')
  const [loading, setLoading]       = useState(false)
  const [error, setError]           = useState(null)
  const [predictive, setPredictive] = useState(null)
  const [lookback, setLookback]     = useState(null)

  useEffect(() => {
    fetchBoth()
  }, [])

  async function fetchBoth() {
    setLoading(true)
    setError(null)
    try {
      const [pred, look] = await Promise.all([
        getRankings({ ranking_type: 'predictive', limit: 400 }),
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

  // Build rank-lookup maps for cross-reference (prior rank)
  const predMap = {}
  const lookMap = {}
  if (predictive) predictive.forEach(p => { predMap[p.player_id] = p.overall_rank })
  if (lookback)   lookback.forEach(p   => { lookMap[p.player_id] = p.overall_rank  })

  // Active list is based on current mode
  const activeList = mode === 'predictive' ? predictive : lookback

  // Cross-reference for prior rank:
  // When viewing Projected (predictive) → prior rank = current (lookback)
  // When viewing Current (lookback)     → prior rank = projected (predictive)
  function getPriorRank(player) {
    if (mode === 'predictive') return lookMap[player.player_id]  ?? null
    return predMap[player.player_id] ?? null
  }

  // Filter by position
  const filtered = activeList
    ? (posFilter === 'All'
        ? activeList
        : activeList.filter(p =>
            p.positions?.some(pos => pos.toUpperCase() === posFilter.toUpperCase())
          )
      )
    : []

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <div className="flex items-center gap-2 mb-1">
          <BarChart2 size={18} className="text-field-400" />
          <h1 className="text-2xl font-bold text-white">Player Rankings</h1>
        </div>
        <p className="text-slate-500 text-sm">
          Top 400 players ranked by fantasy value. Compare current performance vs projected outlook.
        </p>
      </div>

      {/* Mode toggle + Refresh */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="flex gap-2">
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
        <button
          onClick={fetchBoth}
          disabled={loading}
          className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-200 transition-colors"
        >
          <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />
          Refresh
        </button>
      </div>

      {/* Position filter pills */}
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

      <ErrorBanner message={error} onClose={() => setError(null)} />
      {loading && <LoadingState message="Loading rankings…" />}

      {/* Table */}
      {filtered.length > 0 && (
        <div className="card p-0 overflow-hidden">
          <table className="w-full">
            <thead className="bg-navy-900">
              <tr className="text-[10px] text-slate-500 uppercase tracking-wider">
                <th className="py-2.5 pl-4 pr-2 text-right w-10">#</th>
                <th className="py-2.5 px-2 text-center w-12 hidden sm:table-cell">
                  {mode === 'predictive' ? '↕ vs Current' : '↕ vs Proj.'}
                </th>
                <th className="py-2.5 px-3 text-left">Player</th>
                <th className="py-2.5 px-2 text-center w-16">Pos</th>
                <th className="py-2.5 pr-4 pl-2 text-right w-20">Score</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map(player => {
                const priorRank = getPriorRank(player)
                return (
                  <tr
                    key={player.player_id}
                    className="border-t border-navy-800 hover:bg-navy-800/40 transition-colors"
                  >
                    {/* Rank */}
                    <td className="py-3 pl-4 pr-2 font-mono text-slate-400 text-sm text-right">
                      {player.overall_rank}
                    </td>

                    {/* Trend */}
                    <td className="py-3 px-2 text-center hidden sm:table-cell">
                      <div className="flex items-center justify-center gap-1">
                        <TrendIcon current={player.overall_rank} prior={priorRank} />
                        <TrendBadge current={player.overall_rank} prior={priorRank} />
                      </div>
                    </td>

                    {/* Player info */}
                    <td className="py-3 px-3">
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-white text-sm">{player.name}</span>
                        <span className="text-xs text-slate-500 hidden sm:inline">{player.team}</span>
                      </div>
                      {player.blurb && (
                        <p className="text-xs text-slate-600 mt-0.5 line-clamp-1 max-w-xs">
                          {player.blurb}
                        </p>
                      )}
                    </td>

                    {/* Positions */}
                    <td className="py-3 px-2 text-center">
                      <div className="flex flex-wrap gap-0.5 justify-center">
                        {player.positions?.slice(0, 2).map(pos => (
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
                    <td className="py-3 pr-4 pl-2 text-right font-mono text-sm text-field-400">
                      {player.score?.toFixed(2) ?? '—'}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {!loading && filtered.length === 0 && activeList?.length > 0 && (
        <p className="text-center text-slate-600 text-sm py-8">
          No players found for position "{posFilter}".
        </p>
      )}
    </div>
  )
}
