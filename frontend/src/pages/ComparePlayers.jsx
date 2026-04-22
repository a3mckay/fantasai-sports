import { useState } from 'react'
import { BarChart2, Play, Plus, X } from 'lucide-react'
import { comparePlayers } from '../lib/api'
import { usePlayerListFocus } from '../hooks/usePlayerListFocus'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import ContextInput from '../components/ContextInput'
import Blurb from '../components/Blurb'
import PercentileBar from '../components/PercentileBar'
import PlayerSearch from '../components/PlayerSearch'
import LeagueSettings from '../components/LeagueSettings'

const RANK_COLORS = ['text-yellow-400', 'text-slate-300', 'text-leather-400']

// Categories that only apply to pitchers / batters — filter out irrelevant
// zeros so the bar chart stays meaningful.
const PITCHING_CATS = new Set(['W', 'SV', 'HLD', 'K', 'ERA', 'WHIP', 'IP', 'QS', 'K/9', 'BB/9', 'SVHLD'])
const BATTING_CATS  = new Set(['R', 'HR', 'RBI', 'SB', 'AVG', 'OPS', 'OBP', 'SLG', 'H', 'TB', 'XBH', 'BB', 'SO', 'NSB'])

function filterCategoryScores(scores, statType) {
  const relevant = statType === 'pitching' ? PITCHING_CATS : BATTING_CATS
  return Object.fromEntries(Object.entries(scores).filter(([cat]) => relevant.has(cat)))
}

function emptyPlayer() {
  return { name: '', playerId: null }
}

export default function ComparePlayers({ compact = false }) {
  const [players, setPlayers]         = useState([emptyPlayer(), emptyPlayer()])
  const [context, setContext]         = useState('')
  const [rankingType, setRankingType] = useState('predictive')
  const [horizon, setHorizon]         = useState('season')
  const [leagueSettings, setLeagueSettings] = useState(null)
  const [loading, setLoading]         = useState(false)
  const [error, setError]             = useState(null)
  const [result, setResult]           = useState(null)

  const { playerRefs, focusNextOrAdd } = usePlayerListFocus(players, addPlayer)

  function updatePlayer(idx, name, playerId) {
    setPlayers(prev => prev.map((p, i) => i === idx ? { name, playerId } : p))
  }

  function addPlayer() {
    if (players.length < 8) setPlayers(prev => [...prev, emptyPlayer()])
  }

  function removePlayer(idx) {
    if (players.length > 2) setPlayers(prev => prev.filter((_, i) => i !== idx))
  }

  async function submit(e) {
    e.preventDefault()
    const resolved = players.filter(p => p.playerId != null)
    if (resolved.length < 2) {
      setError('Select at least 2 players using the search boxes.')
      return
    }
    setLoading(true); setError(null); setResult(null)
    try {
      const body = {
        player_ids:   resolved.map(p => p.playerId),
        context:      context || null,
        ranking_type: rankingType,
        horizon:      rankingType === 'predictive' ? horizon : 'season',
      }
      if (leagueSettings) {
        body.custom_categories      = leagueSettings.categories
        body.custom_league_type     = leagueSettings.leagueType
      }
      const res = await comparePlayers(body)
      setResult(res)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="space-y-8">
      {!compact && (
        <div>
          <div className="flex items-center gap-2 mb-1">
            <BarChart2 size={18} className="text-field-400" />
            <h1 className="text-2xl font-bold text-white">Compare Players</h1>
          </div>
          <p className="text-slate-500 text-sm">
            Rank 2–8 players head-to-head. Search by name to find any player.
          </p>
        </div>
      )}

      <form
        onSubmit={submit}
        onKeyDown={e => { if (e.key === 'Enter' && e.target.tagName === 'INPUT') e.preventDefault() }}
        className="card space-y-5"
      >
        {/* Player search list */}
        <div>
          <label className="section-label mb-2">Players *</label>
          <div className="space-y-2">
            {players.map((p, idx) => (
              <div key={idx} className="flex items-center gap-2">
                <PlayerSearch
                  ref={el => { playerRefs.current[idx] = el }}
                  value={p.name}
                  playerId={p.playerId}
                  onChange={(name, playerId) => updatePlayer(idx, name, playerId)}
                  onEnterKey={() => focusNextOrAdd(idx)}
                  placeholder={`Player ${idx + 1}…`}
                  className="flex-1"
                />
                {players.length > 2 && (
                  <button
                    type="button"
                    onClick={() => removePlayer(idx)}
                    className="shrink-0 text-slate-600 hover:text-stitch-400 transition-colors p-1"
                  >
                    <X size={15} />
                  </button>
                )}
              </div>
            ))}
          </div>

          {players.length < 8 && (
            <button
              type="button"
              onClick={addPlayer}
              className="mt-2 flex items-center gap-1.5 text-xs text-field-400 hover:text-field-300 transition-colors"
            >
              <Plus size={13} /> Add another player
            </button>
          )}
        </div>

        {/* Ranking type + horizon */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="section-label">Ranking type</label>
            <select
              className="field-input"
              value={rankingType}
              onChange={e => setRankingType(e.target.value)}
            >
              <option value="predictive">Projected (forward-looking)</option>
              <option value="lookback">Current (season-to-date)</option>
            </select>
          </div>
          {rankingType === 'predictive' && (
            <div>
              <label className="section-label">Projection window</label>
              <select
                className="field-input"
                value={horizon}
                onChange={e => setHorizon(e.target.value)}
              >
                <option value="season">Full Season</option>
                <option value="month">This Month</option>
                <option value="week">This Week</option>
              </select>
            </div>
          )}
        </div>

        <ContextInput value={context} onChange={setContext} />
        <LeagueSettings onChange={setLeagueSettings} />

        <button type="submit" className="btn-primary" disabled={loading}>
          <Play size={14} /> Compare Players
        </button>
      </form>

      <ErrorBanner message={error} onClose={() => setError(null)} />
      {loading && <LoadingState message="Comparing players…" />}

      {result && (
        <div className="space-y-6">
          {result.context_applied && (
            <div className="text-xs text-field-400 bg-field-950 border border-field-800 rounded-lg px-3 py-2">
              {result.context_applied}
            </div>
          )}

          <div className="space-y-3">
            {result.ranked_players.map((p, i) => (
              <div key={p.player_id} className="card">
                <div className="flex items-start gap-4">
                  <div className={`text-3xl font-bold font-mono w-10 shrink-0 ${RANK_COLORS[i] || 'text-slate-500'}`}>
                    #{p.rank}
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex flex-wrap items-center gap-2 mb-1">
                      <span className="font-semibold text-white">{p.player_name}</span>
                      <span className="text-xs text-slate-500">{p.team}</span>
                      {p.positions.map(pos => (
                        <span key={pos} className="stat-pill bg-navy-700 text-slate-400">{pos}</span>
                      ))}
                      {p.overall_rank > 0 && (
                        <span className="stat-pill bg-field-900 text-field-300 font-mono ml-auto text-xs">
                          Overall #{p.overall_rank}
                        </span>
                      )}
                    </div>
                    <PercentileBar data={filterCategoryScores(p.category_scores, p.stat_type)} />
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
