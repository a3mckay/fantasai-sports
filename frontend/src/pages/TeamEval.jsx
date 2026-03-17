import { useState, useRef } from 'react'
import { usePlayerListFocus } from '../hooks/usePlayerListFocus'
import { Star, Play, Plus, X, Upload, ImageIcon, Loader2, AlertCircle } from 'lucide-react'
import { teamEval, extractPlayers, searchPlayers } from '../lib/api'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import ContextInput from '../components/ContextInput'
import Blurb from '../components/Blurb'
import ProsCons from '../components/ProsCons'
import CategoryBar from '../components/CategoryBar'
import PlayerSearch from '../components/PlayerSearch'
import LeagueSettings from '../components/LeagueSettings'

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

function emptyPlayer() {
  return { name: '', playerId: null }
}

export default function TeamEval() {
  const [players, setPlayers]               = useState([emptyPlayer()])
  const [rankingType, setRankingType]       = useState('predictive')
  const [context, setContext]               = useState('')
  const [leagueSettings, setLeagueSettings] = useState(null)
  const [loading, setLoading]               = useState(false)
  const [error, setError]                   = useState(null)
  const [result, setResult]                 = useState(null)

  // Screenshot upload state
  const [showUpload, setShowUpload]         = useState(false)
  const [extracting, setExtracting]         = useState(false)
  const [extractError, setExtractError]     = useState(null)
  const fileRef                             = useRef(null)

  const { playerRefs, focusNextOrAdd } = usePlayerListFocus(players, addPlayer)

  function addPlayer() {
    if (players.length < 30) setPlayers(prev => [...prev, emptyPlayer()])
  }

  function removePlayer(idx) {
    if (players.length > 1) setPlayers(prev => prev.filter((_, i) => i !== idx))
  }

  function updatePlayer(idx, name, playerId) {
    setPlayers(prev => prev.map((p, i) => i === idx ? { name, playerId } : p))
  }

  async function handleFiles(files) {
    if (!files.length) return
    setExtracting(true)
    setExtractError(null)

    const allNames = []
    for (const file of Array.from(files)) {
      try {
        const b64 = await new Promise((resolve, reject) => {
          const r = new FileReader()
          r.onload  = e => resolve(e.target.result)
          r.onerror = reject
          r.readAsDataURL(file)
        })
        const res = await extractPlayers({ image_base64: b64, image_type: file.type || 'image/jpeg' })
        allNames.push(...(res.player_names || []))
      } catch (err) {
        setExtractError(err.message)
      }
    }

    if (allNames.length === 0) {
      setExtracting(false)
      if (!extractError) setExtractError('No player names found. Try a clearer screenshot.')
      return
    }

    // Auto-resolve each extracted name via search
    const resolved = await Promise.all(
      allNames.map(async name => {
        try {
          const results = await searchPlayers(name, 1)
          const list = Array.isArray(results) ? results : (results.players || [])
          if (list.length > 0) return { name: list[0].name, playerId: list[0].player_id }
        } catch {}
        return { name, playerId: null }
      })
    )

    // Merge with existing non-empty players
    const existing = players.filter(p => p.name || p.playerId)
    const merged   = [...existing, ...resolved]
    setPlayers(merged.length > 0 ? merged : [emptyPlayer()])
    setExtracting(false)
    setShowUpload(false)
  }

  async function submit(e) {
    e.preventDefault()
    const resolved = players.filter(p => p.playerId != null).map(p => p.playerId)
    if (resolved.length < 1) {
      setError('Add at least one player to evaluate your team. Search by name and select from the dropdown.')
      return
    }
    setLoading(true); setError(null); setResult(null)
    try {
      const body = {
        player_ids:   resolved,
        context:      context || null,
        ranking_type: rankingType,
      }
      if (leagueSettings) {
        body.custom_categories        = leagueSettings.categories
        body.custom_league_type       = leagueSettings.leagueType
        body.custom_roster_positions  = leagueSettings.rosterPositions
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

      {/* Prevent Enter from submitting while typing in player search inputs */}
      <form
        onSubmit={submit}
        onKeyDown={e => { if (e.key === 'Enter' && e.target.tagName === 'INPUT') e.preventDefault() }}
        className="card space-y-5"
      >
        {/* Player list */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <label className="section-label">Your Roster *</label>
            <button
              type="button"
              onClick={() => { setShowUpload(v => !v); setExtractError(null) }}
              className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-300 transition-colors"
            >
              <Upload size={12} />
              {showUpload ? 'Hide upload' : 'Upload screenshots'}
            </button>
          </div>

          {/* Screenshot upload area */}
          {showUpload && (
            <div className="mb-3 space-y-2">
              <div
                className="border-2 border-dashed border-navy-600 rounded-xl p-5 text-center cursor-pointer hover:border-field-600 transition-colors"
                onClick={() => fileRef.current?.click()}
                onDragOver={e => e.preventDefault()}
                onDrop={e => { e.preventDefault(); handleFiles(e.dataTransfer.files) }}
              >
                <ImageIcon size={28} className="mx-auto text-slate-600 mb-2" />
                <p className="text-sm text-slate-400">Upload roster screenshots</p>
                <p className="text-xs text-slate-600 mt-0.5">
                  Drag & drop or click · Select multiple files to cover the full roster
                </p>
                <input
                  ref={fileRef}
                  type="file"
                  accept="image/*"
                  multiple
                  className="hidden"
                  onChange={e => handleFiles(e.target.files)}
                />
              </div>
              {extracting && (
                <div className="flex items-center gap-2 text-xs text-field-400">
                  <Loader2 size={13} className="animate-spin" /> Analyzing screenshots…
                </div>
              )}
              {extractError && (
                <div className="flex items-center gap-2 text-xs text-stitch-400">
                  <AlertCircle size={13} /> {extractError}
                </div>
              )}
            </div>
          )}

          {/* Manual player entries */}
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
                {players.length > 1 && (
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
          {players.length < 30 && (
            <button
              type="button"
              onClick={addPlayer}
              className="mt-2 flex items-center gap-1.5 text-xs text-field-400 hover:text-field-300 transition-colors"
            >
              <Plus size={13} /> Add player
            </button>
          )}
        </div>

        {/* Ranking type */}
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

        <ContextInput value={context} onChange={setContext} />
        <LeagueSettings onChange={setLeagueSettings} />

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
