import { useState } from 'react'
import { Search, Play, Clock, ChevronDown, ChevronUp } from 'lucide-react'
import { findPlayer } from '../lib/api'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import Blurb from '../components/Blurb'

const POSITIONS = ['C','1B','2B','3B','SS','OF','DH','Util','SP','RP','P']

function parseIds(raw) {
  return raw.split(/[\s,]+/).map(s => parseInt(s.trim())).filter(n => !isNaN(n))
}

function SuggestionCard({ s, isCurrent }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <div className={`card ${isCurrent ? 'border-field-700' : 'opacity-60'}`}>
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2 flex-wrap">
            {isCurrent && (
              <span className="stat-pill bg-field-900 text-field-300 text-[10px]">Current pick</span>
            )}
            <span className="font-semibold text-white">{s.player_name}</span>
            {s.positions.map(p => (
              <span key={p} className="stat-pill bg-navy-700 text-slate-400">{p}</span>
            ))}
          </div>
          <div className="text-xs text-slate-500 mt-1 font-mono">Score: {s.priority_score.toFixed(2)}</div>
        </div>
        {s.blurb && (
          <button
            className="text-slate-600 hover:text-slate-400 shrink-0"
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          </button>
        )}
      </div>

      {expanded && s.blurb && (
        <p className="mt-3 text-sm text-slate-400 leading-relaxed border-t border-navy-700 pt-3">
          {s.blurb}
        </p>
      )}

      {s.created_at && !isCurrent && (
        <div className="flex items-center gap-1 mt-2 text-xs text-slate-600">
          <Clock size={10} />
          {new Date(s.created_at).toLocaleDateString()}
        </div>
      )}
    </div>
  )
}

export default function FindPlayer() {
  const [teamId,    setTeamId]    = useState('')
  const [slot,      setSlot]      = useState('OF')
  const [excludeIds, setExcludeIds] = useState('')
  const [loading,   setLoading]   = useState(false)
  const [error,     setError]     = useState(null)
  const [result,    setResult]    = useState(null)

  async function submit(e) {
    e.preventDefault()
    if (!teamId) { setError('Team ID is required.'); return }
    setLoading(true); setError(null); setResult(null)
    try {
      const res = await findPlayer({
        team_id: parseInt(teamId),
        position_slot: slot,
        extra_exclude_ids: parseIds(excludeIds),
      })
      setResult(res)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const history = result?.all_suggestions?.slice(1) || []

  return (
    <div className="space-y-8">
      <div>
        <div className="flex items-center gap-2 mb-1">
          <Search size={18} className="text-field-400" />
          <h1 className="text-2xl font-bold text-white">Find a Player</h1>
        </div>
        <p className="text-slate-500 text-sm">
          Best available pick for a roster slot. History is tracked — repeat calls always return a fresh name.
        </p>
      </div>

      <form onSubmit={submit} className="card space-y-5">
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="section-label">Team ID *</label>
            <input
              className="field-input font-mono"
              placeholder="e.g. 3"
              value={teamId}
              onChange={e => setTeamId(e.target.value)}
            />
          </div>
          <div>
            <label className="section-label">Position slot</label>
            <select className="field-input" value={slot} onChange={e => setSlot(e.target.value)}>
              {POSITIONS.map(p => <option key={p} value={p}>{p}</option>)}
            </select>
          </div>
        </div>

        <div>
          <label className="section-label">Also exclude player IDs (optional)</label>
          <input
            className="field-input font-mono"
            placeholder="19755, 20123"
            value={excludeIds}
            onChange={e => setExcludeIds(e.target.value)}
          />
          <p className="text-xs text-slate-600 mt-1">
            In addition to all rostered players, manually exclude any IDs you don't want suggested.
          </p>
        </div>

        <button type="submit" className="btn-primary" disabled={loading}>
          <Play size={14} /> Find Next Player
        </button>
      </form>

      <ErrorBanner message={error} onClose={() => setError(null)} />
      {loading && <LoadingState message="Searching the waiver wire…" />}

      {result && (
        <div className="space-y-6">
          <SuggestionCard s={result.suggestion} isCurrent />

          {result.suggestion.blurb && <Blurb text={result.suggestion.blurb} />}

          {history.length > 0 && (
            <div>
              <div className="section-label flex items-center gap-1.5 mb-3">
                <Clock size={11} /> Previous suggestions
              </div>
              <div className="space-y-2">
                {history.map((s, i) => <SuggestionCard key={i} s={s} isCurrent={false} />)}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
