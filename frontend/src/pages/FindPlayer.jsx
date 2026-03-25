import { useState, useMemo, useEffect } from 'react'
import { Search, Play, RefreshCw, Loader2, Star, TrendingUp, X, Trash2 } from 'lucide-react'
import { findPlayer } from '../lib/api'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import ContextInput from '../components/ContextInput'
import { useLeague } from '../contexts/LeagueContext'

// ── Constants ──────────────────────────────────────────────────────────────

const COMMON_POSITIONS = ['C', '1B', '2B', '3B', 'SS', 'OF', 'Util', 'SP', 'RP']
const DEFAULT_CATS     = ['R', 'HR', 'RBI', 'SB', 'AVG', 'W', 'SV', 'K', 'ERA', 'WHIP']
const SKIP_CATS        = new Set(['H/AB', 'Batting', 'Pitching', 'AB'])

// Positions that are batter-only or pitcher-only (drives category filtering)
const BATTER_SLOTS  = new Set(['C', '1B', '2B', '3B', 'SS', 'OF'])
const PITCHER_SLOTS = new Set(['SP', 'RP', 'P'])
// Util is mixed; anything not in either set shows all cats

const BATTING_CATS  = new Set(['R', 'HR', 'RBI', 'SB', 'AVG', 'OPS', 'H', 'TB', 'XBH',
                                'BB', 'NSB', 'SLG', 'OBP', 'CS', 'HBP', 'GDP'])
const PITCHING_CATS = new Set(['W', 'L', 'SV', 'K', 'ERA', 'WHIP', 'IP', 'QS', 'HLD',
                                'SVH', 'BS', 'HD', 'HA', 'K/9', 'BB/9', 'NH', 'CG', 'SHO'])

const POOL_LABELS = { mlb: 'MLB', milb: 'MiLB', both: 'Both' }

// ── SuggestionCard ─────────────────────────────────────────────────────────

function SuggestionCard({ s, isLatest, onDismiss }) {
  return (
    <div className={`p-4 rounded-xl border ${isLatest ? 'bg-field-950/40 border-field-700' : 'bg-navy-800 border-navy-700'}`}>
      <div className="flex items-start gap-3">
        {isLatest && <Star size={14} className="text-field-400 shrink-0 mt-1" />}
        <div className="flex-1 min-w-0">
          {/* Search params label */}
          <div className="text-[10px] text-slate-500 font-mono uppercase tracking-wide mb-1.5">
            {s._label}
          </div>

          {/* Player name + position chips + score */}
          <div className="flex items-center gap-2 flex-wrap mb-1">
            <span className="font-semibold text-white">{s.player_name}</span>
            {s.positions.map(pos => (
              <span key={pos} className="stat-pill bg-navy-700 text-slate-400 text-[10px]">{pos}</span>
            ))}
            {s.is_prospect && (
              <span className="stat-pill bg-purple-900/50 border border-purple-700 text-purple-300 text-[10px]">MiLB</span>
            )}
            <span className="ml-auto font-mono text-sm text-field-400">
              {s.is_prospect
                ? `PAV ${(s.pav_score ?? 0).toFixed(1)}`
                : s.priority_score.toFixed(2)}
            </span>
          </div>

          {/* Blurb */}
          {s.blurb && (
            <p className="text-xs text-slate-400 leading-relaxed mt-1">{s.blurb}</p>
          )}
          {s.is_prospect && !s.blurb && (
            <p className="text-xs text-slate-500 italic mt-1">
              Top prospect by PAV score — category impact not available for MiLB players.
            </p>
          )}

          {/* Category impact pills */}
          {!s.is_prospect && Object.keys(s.category_impact || {}).length > 0 && (
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

        {/* Dismiss */}
        <button
          onClick={onDismiss}
          className="text-slate-600 hover:text-slate-400 transition-colors shrink-0 mt-0.5"
          aria-label="Dismiss recommendation"
        >
          <X size={14} />
        </button>
      </div>
    </div>
  )
}

// ── FindPlayer page ────────────────────────────────────────────────────────

export default function FindPlayer() {
  const { league, myTeam } = useLeague() || {}

  // Form state
  const [positionSlot, setPositionSlot] = useState('')
  const [priorityCats, setPriorityCats] = useState([])
  const [playerPool,   setPlayerPool]   = useState('mlb')
  const [context,      setContext]      = useState('')

  // Results (session-local — clears on page reload)
  const [suggestions, setSuggestions] = useState([])
  const [loading,     setLoading]     = useState(false)
  const [error,       setError]       = useState(null)

  // Roster positions from connected league, or fallback
  const rosterPositions = league?.roster_positions?.length > 0
    ? [...new Set(league.roster_positions.filter(p => !['BN', 'DL', 'NA', 'IL'].includes(p)))]
    : COMMON_POSITIONS

  // All scoring categories, filtered of Yahoo junk
  const allCats = useMemo(() => {
    const raw = league?.scoring_categories || DEFAULT_CATS
    return raw.filter(c => !SKIP_CATS.has(c))
  }, [league])

  // Visible categories depend on selected position slot
  const visibleCats = useMemo(() => {
    if (BATTER_SLOTS.has(positionSlot))  return allCats.filter(c => BATTING_CATS.has(c))
    if (PITCHER_SLOTS.has(positionSlot)) return allCats.filter(c => PITCHING_CATS.has(c))
    return allCats
  }, [positionSlot, allCats])

  // Drop category selections that conflict with the newly selected position
  useEffect(() => {
    setPriorityCats(prev => prev.filter(c => visibleCats.includes(c)))
  }, [positionSlot]) // eslint-disable-line react-hooks/exhaustive-deps

  const hasResults = suggestions.length > 0
  const canSubmit  = !loading && !!myTeam && (!!positionSlot || priorityCats.length > 0)

  function togglePosition(pos) {
    setPositionSlot(prev => prev === pos ? '' : pos)
  }

  function toggleCat(cat) {
    setPriorityCats(prev =>
      prev.includes(cat) ? prev.filter(c => c !== cat) : [...prev, cat]
    )
  }

  async function submit(e) {
    e?.preventDefault()
    if (!canSubmit) return
    setLoading(true)
    setError(null)
    try {
      const res = await findPlayer({
        team_id:             myTeam.team_id,
        position_slot:       positionSlot || null,
        priority_categories: priorityCats,
        player_pool:         playerPool,
        context:             context || null,
      })

      const labelParts = [positionSlot, ...priorityCats].filter(Boolean)
      const label = labelParts.length > 0 ? labelParts.join(' + ') : 'Best Available'

      const newCards = [{ ...res.suggestion, _label: label, _id: Date.now() }]

      // "Both" mode: also prepend the MiLB suggestion
      if (res.milb_suggestion) {
        const milbLabel = [positionSlot, 'MiLB'].filter(Boolean).join(' + ') || 'MiLB'
        newCards.push({ ...res.milb_suggestion, _label: milbLabel, _id: Date.now() + 1 })
      }

      setSuggestions(prev => [...newCards, ...prev])
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <div className="flex items-center gap-2 mb-1">
          <Search size={18} className="text-field-400" />
          <h1 className="text-2xl font-bold text-white">Recommend a Player</h1>
        </div>
        <p className="text-slate-500 text-sm">
          Select a position, priority categories, or both — all fields are optional and combinable.
        </p>
      </div>

      {!myTeam && (
        <div className="p-4 rounded-xl border border-amber-800/40 bg-amber-950/20 text-amber-400 text-sm">
          Connect your Yahoo account from Profile to use this feature.
        </div>
      )}

      <form onSubmit={submit} className="card space-y-5">

        {/* Position slot (optional, click to toggle) */}
        <div>
          <div className="flex items-baseline gap-2 mb-2">
            <label className="section-label">Position Slot</label>
            <span className="text-[10px] text-slate-500 uppercase tracking-wide">optional · click to deselect</span>
          </div>
          <div className="flex flex-wrap gap-2">
            {rosterPositions.map(pos => (
              <button
                key={pos}
                type="button"
                onClick={() => togglePosition(pos)}
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

        {/* Priority categories (hidden for MiLB-only) */}
        {playerPool !== 'milb' && (
          <div>
            <div className="flex items-baseline gap-2 mb-2">
              <label className="section-label">Priority Category</label>
              <span className="text-[10px] text-slate-500 uppercase tracking-wide">optional · multi-select</span>
            </div>
            <div className="flex flex-wrap gap-2">
              {visibleCats.map(cat => (
                <button
                  key={cat}
                  type="button"
                  onClick={() => toggleCat(cat)}
                  className={`px-3 py-1.5 rounded-lg text-sm font-medium border transition-colors ${
                    priorityCats.includes(cat)
                      ? 'bg-field-700 border-field-600 text-white'
                      : 'bg-navy-800 border-navy-700 text-slate-400 hover:text-white hover:border-navy-600'
                  }`}
                >
                  {cat}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Player pool */}
        <div>
          <label className="section-label mb-2">Player Pool</label>
          <div className="flex gap-2">
            {['mlb', 'milb', 'both'].map(pool => (
              <button
                key={pool}
                type="button"
                onClick={() => setPlayerPool(pool)}
                className={`px-4 py-1.5 rounded-lg text-sm font-medium border transition-colors ${
                  playerPool === pool
                    ? 'bg-field-700 border-field-600 text-white'
                    : 'bg-navy-800 border-navy-700 text-slate-400 hover:text-white hover:border-navy-600'
                }`}
              >
                {POOL_LABELS[pool]}
              </button>
            ))}
          </div>
          {playerPool !== 'mlb' && (
            <p className="text-[11px] text-slate-500 mt-1.5">
              MiLB players are ranked by PAV score only — category impact is not available.
            </p>
          )}
        </div>

        <ContextInput
          value={context}
          onChange={setContext}
          placeholder='e.g. "targeting saves" or "need stolen base help"'
        />

        {/* Validation hint */}
        {myTeam && !positionSlot && priorityCats.length === 0 && (
          <p className="text-[11px] text-slate-500">
            Select at least one position or category above to enable search.
          </p>
        )}

        {/* Buttons */}
        <div className="flex gap-3 items-center flex-wrap">
          <button type="submit" className="btn-primary" disabled={!canSubmit}>
            <Play size={14} />
            Find a Player
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={!hasResults || !canSubmit}
            className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium border transition-colors ${
              hasResults && canSubmit
                ? 'bg-navy-800 border-navy-600 text-slate-200 hover:border-field-600 hover:text-white'
                : 'bg-navy-900 border-navy-800 text-slate-600 cursor-not-allowed'
            }`}
          >
            <RefreshCw size={13} />
            Recommend Another
          </button>
        </div>
      </form>

      <ErrorBanner message={error} onClose={() => setError(null)} />

      {/* Loading — inline when results exist, full-screen when not */}
      {loading && !hasResults && <LoadingState message="Finding the best available player…" />}

      {/* Results stack */}
      {hasResults && (
        <div>
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center gap-2">
              <TrendingUp size={14} className="text-field-400" />
              <span className="text-sm font-semibold text-white">Recommendations</span>
            </div>
            <button
              type="button"
              onClick={() => setSuggestions([])}
              className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-300 transition-colors"
            >
              <Trash2 size={12} /> Clear all
            </button>
          </div>

          {loading && (
            <div className="flex items-center gap-2 text-xs text-slate-500 mb-3">
              <Loader2 size={12} className="animate-spin" />
              Finding another recommendation…
            </div>
          )}

          <div className="space-y-3">
            {suggestions.map((s, i) => (
              <SuggestionCard
                key={s._id}
                s={s}
                isLatest={i === 0 && !loading}
                onDismiss={() => setSuggestions(prev => prev.filter(x => x._id !== s._id))}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
