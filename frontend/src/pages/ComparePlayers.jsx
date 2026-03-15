import { useState } from 'react'
import { BarChart2, Play } from 'lucide-react'
import { comparePlayers } from '../lib/api'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import ContextInput from '../components/ContextInput'
import Blurb from '../components/Blurb'
import CategoryBar from '../components/CategoryBar'

function parseIds(raw) {
  return raw.split(/[\s,]+/).map(s => parseInt(s.trim())).filter(n => !isNaN(n))
}

const RANK_COLORS = ['text-yellow-400', 'text-slate-300', 'text-leather-400']

export default function ComparePlayers() {
  const [playerIds, setPlayerIds] = useState('')
  const [leagueId, setLeagueId]   = useState('')
  const [context, setContext]     = useState('')
  const [rankingType, setRankingType] = useState('predictive')
  const [loading, setLoading]     = useState(false)
  const [error, setError]         = useState(null)
  const [result, setResult]       = useState(null)

  async function submit(e) {
    e.preventDefault()
    const ids = parseIds(playerIds)
    if (ids.length < 2) { setError('Enter at least 2 player IDs.'); return }
    setLoading(true); setError(null); setResult(null)
    try {
      const res = await comparePlayers({
        player_ids: ids,
        league_id: leagueId ? parseInt(leagueId) : null,
        context: context || null,
        ranking_type: rankingType,
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
      {/* Header */}
      <div>
        <div className="flex items-center gap-2 mb-1">
          <BarChart2 size={18} className="text-field-400" />
          <h1 className="text-2xl font-bold text-white">Compare Players</h1>
        </div>
        <p className="text-slate-500 text-sm">Rank 2+ players head-to-head. Enter FanGraphs player IDs.</p>
      </div>

      {/* Form */}
      <form onSubmit={submit} className="card space-y-5">
        <div>
          <label className="section-label">Player IDs *</label>
          <input
            className="field-input font-mono"
            placeholder="e.g. 19755, 20123, 25764"
            value={playerIds}
            onChange={e => setPlayerIds(e.target.value)}
          />
          <p className="text-xs text-slate-600 mt-1">Comma or space separated FanGraphs IDfg values.</p>
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
          <div>
            <label className="section-label">Ranking type</label>
            <select
              className="field-input"
              value={rankingType}
              onChange={e => setRankingType(e.target.value)}
            >
              <option value="predictive">Predictive (forward-looking)</option>
              <option value="lookback">Current (season-to-date)</option>
            </select>
          </div>
        </div>

        <ContextInput value={context} onChange={setContext} />

        <button type="submit" className="btn-primary" disabled={loading}>
          <Play size={14} /> Compare Players
        </button>
      </form>

      <ErrorBanner message={error} onClose={() => setError(null)} />
      {loading && <LoadingState message="Comparing players…" />}

      {/* Results */}
      {result && (
        <div className="space-y-6">
          {result.context_applied && (
            <div className="text-xs text-field-400 bg-field-950 border border-field-800 rounded-lg px-3 py-2">
              {result.context_applied}
            </div>
          )}

          {/* Player cards */}
          <div className="space-y-3">
            {result.ranked_players.map((p, i) => (
              <div key={p.player_id} className="card">
                <div className="flex items-start gap-4">
                  {/* Rank */}
                  <div className={`text-3xl font-bold font-mono w-10 shrink-0 ${RANK_COLORS[i] || 'text-slate-500'}`}>
                    #{p.rank}
                  </div>
                  {/* Info */}
                  <div className="flex-1 min-w-0">
                    <div className="flex flex-wrap items-center gap-2 mb-1">
                      <span className="font-semibold text-white">{p.player_name}</span>
                      <span className="text-xs text-slate-500">{p.team}</span>
                      {p.positions.map(pos => (
                        <span key={pos} className="stat-pill bg-navy-700 text-slate-400">{pos}</span>
                      ))}
                      <span className="stat-pill bg-field-900 text-field-300 font-mono ml-auto">
                        {p.composite_score.toFixed(2)}
                      </span>
                    </div>
                    <CategoryBar data={p.category_scores} />
                  </div>
                </div>
              </div>
            ))}
          </div>

          <Blurb text={result.analysis_blurb} />
        </div>
      )}
    </div>
  )
}
