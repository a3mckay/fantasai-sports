import { useState } from 'react'
import { Star, Play } from 'lucide-react'
import { teamEval } from '../lib/api'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import ContextInput from '../components/ContextInput'
import Blurb from '../components/Blurb'
import ProsCons from '../components/ProsCons'
import CategoryBar from '../components/CategoryBar'

function parseIds(raw) {
  return raw.split(/[\s,]+/).map(s => parseInt(s.trim())).filter(n => !isNaN(n))
}

const GRADE_STYLE = {
  A: 'border-field-500 text-field-300 bg-field-950',
  B: 'border-field-600 text-field-400 bg-field-950',
  C: 'border-leather-500 text-leather-300 bg-leather-500/10',
  D: 'border-stitch-500 text-stitch-300 bg-stitch-500/10',
  F: 'border-red-700 text-red-300 bg-red-950',
}

const ASSESSMENT_PILL = {
  elite:   'bg-field-900 text-field-300',
  solid:   'bg-field-900/50 text-field-400',
  average: 'bg-navy-700 text-slate-400',
  weak:    'bg-red-950/50 text-red-400',
  empty:   'bg-navy-800 text-slate-600',
}

export default function TeamEval() {
  const [teamId, setTeamId]       = useState('')
  const [playerIds, setPlayerIds] = useState('')
  const [leagueId, setLeagueId]   = useState('')
  const [context, setContext]     = useState('')
  const [rankingType, setRankingType] = useState('predictive')
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
        league_id:    leagueId   ? parseInt(leagueId)   : null,
        context:      context    || null,
        ranking_type: rankingType,
      }
      if (teamId) {
        body.team_id = parseInt(teamId)
      } else {
        body.player_ids = parseIds(playerIds)
      }
      const res = await teamEval(body)
      setResult(res)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const gradeCls = result ? (GRADE_STYLE[result.letter_grade] || GRADE_STYLE.C) : ''

  return (
    <div className="space-y-8">
      <div>
        <div className="flex items-center gap-2 mb-1">
          <Star size={18} className="text-field-400" />
          <h1 className="text-2xl font-bold text-white">Team Evaluation</h1>
        </div>
        <p className="text-slate-500 text-sm">
          Letter grade, position-by-position breakdown, and improvement suggestions.
        </p>
      </div>

      <form onSubmit={submit} className="card space-y-5">
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
            <select className="field-input" value={rankingType} onChange={e => setRankingType(e.target.value)}>
              <option value="predictive">Predictive</option>
              <option value="lookback">Current</option>
            </select>
          </div>
        </div>

        <ContextInput value={context} onChange={setContext} />

        <button type="submit" className="btn-primary" disabled={loading}>
          <Play size={14} /> Evaluate Team
        </button>
      </form>

      <ErrorBanner message={error} onClose={() => setError(null)} />
      {loading && <LoadingState message="Grading your roster…" />}

      {result && (
        <div className="space-y-6">
          {/* Grade hero */}
          <div className="card flex items-center gap-6">
            <div className={`grade-badge ${gradeCls} shrink-0`}>
              {result.letter_grade}
            </div>
            <div>
              <div className="text-slate-400 text-sm">
                {result.grade_percentile.toFixed(0)}th percentile · overall score{' '}
                <span className="font-mono text-white">{result.overall_score.toFixed(2)}</span>
              </div>
              <div className="flex flex-wrap gap-1.5 mt-2">
                {result.strong_categories.map(c => (
                  <span key={c} className="stat-pill bg-field-900 text-field-300">{c} ▲</span>
                ))}
                {result.weak_categories.map(c => (
                  <span key={c} className="stat-pill bg-red-950/50 text-red-400">{c} ▼</span>
                ))}
              </div>
            </div>
          </div>

          {/* Category strengths */}
          <div className="card">
            <div className="section-label">Category strength</div>
            <CategoryBar data={result.category_strengths} />
          </div>

          {/* Position breakdown */}
          <div className="card">
            <div className="section-label mb-3">Position breakdown</div>
            <div className="space-y-2">
              {result.position_breakdown.map(g => (
                <div key={g.position} className="flex items-center gap-3">
                  <span className="w-10 text-right font-mono text-xs text-slate-500 shrink-0">{g.position}</span>
                  <span className={`stat-pill w-16 justify-center text-[10px] ${ASSESSMENT_PILL[g.assessment] || ''}`}>
                    {g.assessment}
                  </span>
                  <span className="text-xs text-slate-400 truncate">{g.players.join(', ') || '—'}</span>
                  <span className="ml-auto font-mono text-xs text-slate-600 shrink-0">
                    {g.group_score.toFixed(1)}
                  </span>
                </div>
              ))}
            </div>
          </div>

          {/* Improvement suggestions */}
          {result.improvement_suggestions.length > 0 && (
            <div className="card">
              <div className="section-label mb-3">Improvement suggestions</div>
              <ul className="space-y-2">
                {result.improvement_suggestions.map((s, i) => (
                  <li key={i} className="flex items-start gap-2 text-sm text-slate-300">
                    <span className="text-field-500 shrink-0 mt-0.5">→</span>
                    {s}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <ProsCons pros={result.pros} cons={result.cons} />
          <Blurb text={result.analysis_blurb} />
        </div>
      )}
    </div>
  )
}
