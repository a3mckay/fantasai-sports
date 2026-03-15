import { useState } from 'react'
import { Trophy, Play, Scissors, CheckCircle } from 'lucide-react'
import { keeperEval } from '../lib/api'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import ContextInput from '../components/ContextInput'
import Blurb from '../components/Blurb'
import ProsCons from '../components/ProsCons'

function parseIds(raw) {
  return raw.split(/[\s,]+/).map(s => parseInt(s.trim())).filter(n => !isNaN(n))
}

const GRADE_STYLE = {
  A: 'text-field-300',
  B: 'text-field-400',
  C: 'text-leather-300',
  D: 'text-stitch-300',
  F: 'text-red-400',
}

function PlayerList({ players, variant = 'keep' }) {
  if (!players.length) return <p className="text-xs text-slate-600 italic">None</p>
  const isKeep = variant === 'keep'
  return (
    <div className="space-y-1.5">
      {players.map(p => (
        <div key={p.player_id} className="flex items-center gap-2 text-sm">
          {isKeep
            ? <CheckCircle size={13} className="text-field-500 shrink-0" />
            : <Scissors size={13} className="text-stitch-500 shrink-0" />
          }
          <span className={isKeep ? 'text-white' : 'text-slate-500 line-through'}>{p.player_name}</span>
          {p.positions.map(pos => (
            <span key={pos} className="stat-pill bg-navy-700 text-slate-500 text-[10px]">{pos}</span>
          ))}
          <span className="ml-auto font-mono text-xs text-slate-600">{p.score.toFixed(2)}</span>
        </div>
      ))}
    </div>
  )
}

function DraftProfiles({ profiles }) {
  if (!profiles.length) return null
  return (
    <div className="card space-y-4">
      <div className="section-label">Draft target profiles</div>
      {profiles.map(dp => (
        <div key={dp.priority} className="flex gap-3">
          <div className="w-6 h-6 rounded-full bg-field-900 border border-field-700 flex items-center justify-center text-xs font-bold text-field-300 shrink-0">
            {dp.priority}
          </div>
          <div>
            <div className="flex items-center gap-2 flex-wrap mb-0.5">
              <span className="font-medium text-white text-sm">{dp.position}</span>
              {dp.category_targets.map(c => (
                <span key={c} className="stat-pill bg-field-900 text-field-400 text-[10px]">{c}</span>
              ))}
            </div>
            <p className="text-xs text-slate-400">{dp.rationale}</p>
            {dp.example_players.length > 0 && (
              <p className="text-xs text-slate-600 mt-1">e.g. {dp.example_players.join(', ')}</p>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}

export default function KeeperEval() {
  const [mode, setMode]           = useState('plan_keepers')
  const [teamId, setTeamId]       = useState('')
  const [playerIds, setPlayerIds] = useState('')
  const [nKeepers, setNKeepers]   = useState('5')
  const [leagueId, setLeagueId]   = useState('')
  const [context, setContext]     = useState('')
  const [loading, setLoading]     = useState(false)
  const [error, setError]         = useState(null)
  const [result, setResult]       = useState(null)

  async function submit(e) {
    e.preventDefault()
    if (!teamId && !playerIds.trim()) {
      setError('Provide either a Team ID or a list of Player IDs.')
      return
    }
    setLoading(true); setError(null); setResult(null)
    try {
      const body = {
        mode,
        n_keepers:  parseInt(nKeepers) || 5,
        league_id:  leagueId ? parseInt(leagueId) : null,
        context:    context  || null,
      }
      if (teamId) {
        body.team_id = parseInt(teamId)
      } else {
        body.player_ids = parseIds(playerIds)
      }
      const res = await keeperEval(body)
      setResult(res)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="space-y-8">
      <div>
        <div className="flex items-center gap-2 mb-1">
          <Trophy size={18} className="text-field-400" />
          <h1 className="text-2xl font-bold text-white">Keeper Planning</h1>
        </div>
        <p className="text-slate-500 text-sm">
          Evaluate your keeper core, or let the AI decide who to keep from your full roster.
        </p>
      </div>

      <form onSubmit={submit} className="card space-y-5">
        {/* Mode toggle */}
        <div>
          <label className="section-label">Mode</label>
          <div className="flex gap-2">
            {[
              { val: 'plan_keepers',     label: 'Plan keepers (full roster → recommend N to keep)' },
              { val: 'evaluate_keepers', label: 'Evaluate keepers (input IS your keeper list)' },
            ].map(({ val, label }) => (
              <button
                key={val}
                type="button"
                onClick={() => setMode(val)}
                className={`flex-1 text-xs px-3 py-2.5 rounded-lg border transition-colors ${
                  mode === val
                    ? 'bg-field-700 border-field-600 text-white'
                    : 'bg-navy-800 border-navy-600 text-slate-400 hover:text-slate-200'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="section-label">Team ID</label>
            <input
              className="field-input font-mono"
              placeholder="e.g. 3"
              value={teamId}
              onChange={e => { setTeamId(e.target.value); if (e.target.value) setPlayerIds('') }}
            />
          </div>
          <div>
            <label className="section-label">— or — Player IDs</label>
            <input
              className="field-input font-mono"
              placeholder="19755, 20123, …"
              value={playerIds}
              onChange={e => { setPlayerIds(e.target.value); if (e.target.value) setTeamId('') }}
            />
          </div>
        </div>

        <div className="grid grid-cols-2 gap-4">
          {mode === 'plan_keepers' && (
            <div>
              <label className="section-label">Keepers to keep (#)</label>
              <input
                className="field-input font-mono w-24"
                type="number" min="1" max="20"
                value={nKeepers}
                onChange={e => setNKeepers(e.target.value)}
              />
            </div>
          )}
          <div>
            <label className="section-label">League ID (optional)</label>
            <input
              className="field-input font-mono"
              placeholder="e.g. 1"
              value={leagueId}
              onChange={e => setLeagueId(e.target.value)}
            />
          </div>
        </div>

        <ContextInput value={context} onChange={setContext} />

        <button type="submit" className="btn-primary" disabled={loading}>
          <Play size={14} /> {mode === 'plan_keepers' ? 'Plan My Keepers' : 'Evaluate Keepers'}
        </button>
      </form>

      <ErrorBanner message={error} onClose={() => setError(null)} />
      {loading && <LoadingState message="Evaluating keeper core…" />}

      {result && (
        <div className="space-y-6">
          {/* Foundation grade */}
          <div className="card flex items-center gap-4">
            <div className="text-center shrink-0">
              <div className={`text-5xl font-bold ${GRADE_STYLE[result.keeper_foundation_grade] || 'text-slate-300'}`}>
                {result.keeper_foundation_grade}
              </div>
              <div className="text-xs text-slate-600 mt-1">foundation</div>
            </div>
            <div>
              <div className="text-sm text-slate-400 mb-2">
                {result.category_gaps.length > 0
                  ? `Draft targets needed: ${result.category_gaps.slice(0, 4).join(', ')}`
                  : 'No major category gaps'}
              </div>
              {result.position_gaps.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {result.position_gaps.map(p => (
                    <span key={p} className="stat-pill bg-stitch-500/10 text-stitch-300 border border-stitch-800">
                      {p} needed
                    </span>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* Keepers list */}
          <div className="card">
            <div className="section-label mb-3">
              {result.mode === 'plan_keepers' ? 'Recommended keeps' : 'Keeper core'}
            </div>
            <PlayerList players={result.keepers} variant="keep" />
          </div>

          {/* Cuts list (plan mode) */}
          {result.cuts.length > 0 && (
            <div className="card">
              <div className="section-label mb-3">Cut recommendations</div>
              <PlayerList players={result.cuts} variant="cut" />
            </div>
          )}

          <DraftProfiles profiles={result.draft_profiles} />
          <ProsCons pros={result.pros} cons={result.cons} />
          <Blurb text={result.analysis_blurb} />
        </div>
      )}
    </div>
  )
}
