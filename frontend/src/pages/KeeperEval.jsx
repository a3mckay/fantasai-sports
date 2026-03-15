import { useState, useRef } from 'react'
import { Trophy, Play, Scissors, CheckCircle, Plus, X, Upload, ImageIcon, Loader2, AlertCircle } from 'lucide-react'
import { keeperEval, extractPlayers, searchPlayers } from '../lib/api'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import ContextInput from '../components/ContextInput'
import Blurb from '../components/Blurb'
import ProsCons from '../components/ProsCons'
import PlayerSearch from '../components/PlayerSearch'
import LeagueSettings from '../components/LeagueSettings'

const GRADE_STYLE = {
  A: 'text-field-300',
  B: 'text-field-400',
  C: 'text-leather-300',
  D: 'text-stitch-300',
  F: 'text-red-400',
}

const MODES = [
  {
    val: 'plan_keepers',
    label: 'Plan My Keepers',
    description: 'Enter your full roster → AI picks the best N players to keep and recommends who to cut.',
  },
  {
    val: 'evaluate_keepers',
    label: 'Evaluate My Keepers',
    description: 'Enter your confirmed keeper list → AI grades the foundation and suggests draft targets.',
  },
]

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
          <span className={isKeep ? 'text-white' : 'text-slate-500 line-through'}>
            {p.player_name}
          </span>
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

function emptyPlayer() {
  return { name: '', playerId: null }
}

export default function KeeperEval() {
  const [mode, setMode]               = useState('plan_keepers')
  const [players, setPlayers]         = useState([emptyPlayer()])
  const [nKeepers, setNKeepers]       = useState('5')
  const [context, setContext]         = useState('')
  const [leagueSettings, setLeagueSettings] = useState(null)
  const [loading, setLoading]         = useState(false)
  const [error, setError]             = useState(null)
  const [result, setResult]           = useState(null)

  // Screenshot upload state
  const [showUpload, setShowUpload]     = useState(false)
  const [extracting, setExtracting]     = useState(false)
  const [extractError, setExtractError] = useState(null)
  const fileRef                         = useRef(null)

  function addPlayer() {
    if (players.length < 30) setPlayers(prev => [...prev, emptyPlayer()])
  }

  function removePlayer(idx) {
    if (players.length > 1) setPlayers(prev => prev.filter((_, i) => i !== idx))
  }

  function updatePlayer(idx, name, playerId) {
    setPlayers(prev => prev.map((p, i) => i === idx ? { name, playerId } : p))
  }

  function switchMode(m) {
    setMode(m)
    setPlayers([emptyPlayer()])
    setResult(null)
    setError(null)
    setShowUpload(false)
    setExtractError(null)
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
      setError(`Add at least one player ${mode === 'plan_keepers' ? 'from your roster' : 'to your keeper list'}. Search by name and select from the dropdown.`)
      return
    }
    setLoading(true); setError(null); setResult(null)
    try {
      const body = {
        mode,
        player_ids: resolved,
        n_keepers:  parseInt(nKeepers) || 5,
        context:    context || null,
      }
      if (leagueSettings) {
        body.custom_categories        = leagueSettings.categories
        body.custom_league_type       = leagueSettings.leagueType
        body.custom_roster_positions  = leagueSettings.rosterPositions
      }
      const res = await keeperEval(body)
      setResult(res)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const currentMode = MODES.find(m => m.val === mode)

  return (
    <div className="space-y-6">
      <div>
        <div className="flex items-center gap-2 mb-1">
          <Trophy size={18} className="text-field-400" />
          <h1 className="text-2xl font-bold text-white">Keeper Planning</h1>
        </div>
        <p className="text-slate-500 text-sm">
          Evaluate your keeper core or let AI decide who to keep from your full roster.
        </p>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        {MODES.map(m => (
          <button
            key={m.val}
            type="button"
            onClick={() => switchMode(m.val)}
            className={`text-left p-4 rounded-xl border transition-all ${
              mode === m.val
                ? 'bg-field-900 border-field-600 shadow-lg'
                : 'bg-navy-800 border-navy-600 hover:border-navy-500'
            }`}
          >
            <div className={`font-semibold text-sm mb-1 ${mode === m.val ? 'text-field-300' : 'text-slate-300'}`}>
              {m.label}
            </div>
            <p className="text-xs text-slate-500 leading-relaxed">{m.description}</p>
          </button>
        ))}
      </div>

      {/* Prevent Enter from submitting while typing in player search inputs */}
      <form
        onSubmit={submit}
        onKeyDown={e => { if (e.key === 'Enter' && e.target.tagName === 'INPUT') e.preventDefault() }}
        className="card space-y-5"
      >
        <div className="flex items-center gap-2 px-3 py-2 bg-field-950 border border-field-800/50 rounded-lg">
          <Trophy size={13} className="text-field-500 shrink-0" />
          <span className="text-xs text-field-400">{currentMode?.label}</span>
        </div>

        {/* Player inputs */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <label className="section-label">
              {mode === 'plan_keepers' ? 'Your Full Roster *' : 'Your Confirmed Keepers *'}
            </label>
            <button
              type="button"
              onClick={() => { setShowUpload(v => !v); setExtractError(null) }}
              className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-300 transition-colors"
            >
              <Upload size={12} />
              {showUpload ? 'Hide upload' : 'Upload screenshots'}
            </button>
          </div>

          {showUpload && (
            <div className="mb-3 space-y-2">
              <div
                className="border-2 border-dashed border-navy-600 rounded-xl p-5 text-center cursor-pointer hover:border-field-600 transition-colors"
                onClick={() => fileRef.current?.click()}
                onDragOver={e => e.preventDefault()}
                onDrop={e => { e.preventDefault(); handleFiles(e.dataTransfer.files) }}
              >
                <ImageIcon size={28} className="mx-auto text-slate-600 mb-2" />
                <p className="text-sm text-slate-400">
                  Upload {mode === 'plan_keepers' ? 'roster' : 'keeper list'} screenshots
                </p>
                <p className="text-xs text-slate-600 mt-0.5">
                  Drag & drop or click · Select multiple files if it doesn't fit in one image
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

          <div className="space-y-2">
            {players.map((p, idx) => (
              <div key={idx} className="flex items-center gap-2">
                <PlayerSearch
                  value={p.name}
                  playerId={p.playerId}
                  onChange={(name, playerId) => updatePlayer(idx, name, playerId)}
                  onEnterKey={addPlayer}
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

        {mode === 'plan_keepers' && (
          <div>
            <label className="section-label">How many keepers to keep</label>
            <input
              className="field-input font-mono w-24"
              type="number" min="1" max="20"
              value={nKeepers}
              onChange={e => setNKeepers(e.target.value)}
            />
          </div>
        )}

        <ContextInput value={context} onChange={setContext} />
        <LeagueSettings onChange={setLeagueSettings} />

        <button type="submit" className="btn-primary" disabled={loading}>
          <Play size={14} />
          {mode === 'plan_keepers' ? 'Plan My Keepers' : 'Evaluate Keepers'}
        </button>
      </form>

      <ErrorBanner message={error} onClose={() => setError(null)} />
      {loading && <LoadingState message="Evaluating keeper core…" />}

      {result && (
        <div className="space-y-6">
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
                  : 'No major category gaps'
                }
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

          <div className="card">
            <div className="section-label mb-3">
              {result.mode === 'plan_keepers' ? 'Recommended keeps' : 'Keeper core'}
            </div>
            <PlayerList players={result.keepers} variant="keep" />
          </div>

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
