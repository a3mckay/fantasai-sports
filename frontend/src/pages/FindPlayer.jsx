import { useState } from 'react'
import { Search, Play, Loader2, Star, TrendingUp } from 'lucide-react'
import { findPlayer } from '../lib/api'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import ContextInput from '../components/ContextInput'
import Blurb from '../components/Blurb'
import { useLeague } from '../contexts/LeagueContext'

const COMMON_POSITIONS = ['C', '1B', '2B', '3B', 'SS', 'OF', 'UTIL', 'SP', 'RP']

function SuggestionCard({ s, isTop }) {
  return (
    <div className={`p-4 rounded-xl border ${isTop ? 'bg-field-950/40 border-field-700' : 'bg-navy-800 border-navy-700'}`}>
      <div className="flex items-start gap-3">
        {isTop && <Star size={14} className="text-field-400 shrink-0 mt-0.5" />}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap mb-1">
            <span className="font-semibold text-white">{s.player_name}</span>
            {s.positions.map(pos => (
              <span key={pos} className="stat-pill bg-navy-700 text-slate-400 text-[10px]">{pos}</span>
            ))}
            <span className="ml-auto font-mono text-sm text-field-400">{s.priority_score.toFixed(2)}</span>
          </div>
          {s.blurb && <p className="text-xs text-slate-400 leading-relaxed">{s.blurb}</p>}
          {Object.keys(s.category_impact || {}).length > 0 && (
            <div className="flex flex-wrap gap-1.5 mt-2">
              {Object.entries(s.category_impact)
                .filter(([, v]) => Math.abs(v) > 0.01)
                .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
                .slice(0, 5)
                .map(([cat, val]) => (
                  <span
                    key={cat}
                    className={`text-[10px] font-mono rounded px-1.5 py-0.5 border ${
                      val > 0
                        ? 'text-field-300 bg-field-900/50 border-field-800'
                        : 'text-stitch-300 bg-stitch-900/30 border-stitch-800/50'
                    }`}
                  >
                    {cat} {val > 0 ? '+' : ''}{val.toFixed(2)}
                  </span>
                ))
              }
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

export default function FindPlayer() {
  const { league, myTeam } = useLeague() || {}
  const [positionSlot, setPositionSlot] = useState('')
  const [context, setContext]           = useState('')
  const [loading, setLoading]           = useState(false)
  const [error, setError]               = useState(null)
  const [result, setResult]             = useState(null)

  const rosterPositions = league?.roster_positions?.length > 0
    ? [...new Set(league.roster_positions.filter(p => !['BN', 'DL', 'NA', 'IL'].includes(p)))]
    : COMMON_POSITIONS

  async function submit(e) {
    e.preventDefault()
    if (!myTeam?.team_id) {
      setError('No Yahoo team found. Connect your Yahoo account and re-sync from Profile.')
      return
    }
    if (!positionSlot) {
      setError('Select a position slot.')
      return
    }
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const res = await findPlayer({
        team_id:       myTeam.team_id,
        position_slot: positionSlot,
        context:       context || null,
      })
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
          <Search size={18} className="text-field-400" />
          <h1 className="text-2xl font-bold text-white">Find a Player</h1>
        </div>
        <p className="text-slate-500 text-sm">
          AI recommends the best available player for a roster slot based on your team's needs.
        </p>
      </div>

      {!myTeam && (
        <div className="p-4 rounded-xl border border-amber-800/40 bg-amber-950/20 text-amber-400 text-sm">
          Connect your Yahoo account from Profile to use this feature.
        </div>
      )}

      <form onSubmit={submit} className="card space-y-5">
        <div>
          <label className="section-label mb-2">Position slot to fill</label>
          <div className="flex flex-wrap gap-2">
            {rosterPositions.map(pos => (
              <button
                key={pos}
                type="button"
                onClick={() => setPositionSlot(pos)}
                className={`px-3 py-1.5 rounded-lg text-sm font-medium border transition-colors ${
                  positionSlot === pos
                    ? 'bg-field-700 border-field-600 text-white'
                    : 'bg-navy-800 border-navy-700 text-slate-400 hover:text-white hover:border-navy-600'
                }`}
              >
                {pos}
              </button>
            ))}
          </div>
        </div>

        <ContextInput
          value={context}
          onChange={setContext}
          placeholder='e.g. "targeting saves" or "need stolen base help"'
        />

        <button type="submit" className="btn-primary" disabled={loading || !myTeam}>
          <Play size={14} /> Find a Player
        </button>
      </form>

      <ErrorBanner message={error} onClose={() => setError(null)} />
      {loading && <LoadingState message="Finding the best available player…" />}

      {result && (
        <div className="space-y-4">
          <div>
            <div className="flex items-center gap-2 mb-3">
              <TrendingUp size={14} className="text-field-400" />
              <span className="text-sm font-semibold text-white">Top Recommendation</span>
            </div>
            <SuggestionCard s={result.suggestion} isTop />
          </div>

          {result.all_suggestions?.length > 1 && (
            <div>
              <div className="section-label mb-3">Other options considered</div>
              <div className="space-y-2">
                {result.all_suggestions.slice(1, 5).map(s => (
                  <SuggestionCard key={s.player_id} s={s} isTop={false} />
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
