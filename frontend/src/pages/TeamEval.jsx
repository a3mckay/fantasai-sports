import { useState, useRef } from 'react'
import { usePlayerListFocus } from '../hooks/usePlayerListFocus'
import { Star, Play, Plus, X, Upload, ImageIcon, Loader2, AlertCircle, Users, ChevronDown, ChevronRight } from 'lucide-react'
import { teamEval, extractPlayers, searchPlayers } from '../lib/api'
import { useLeague } from '../contexts/LeagueContext'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import ContextInput from '../components/ContextInput'
import Blurb from '../components/Blurb'
import ProsCons from '../components/ProsCons'
import CategoryStrengthBar from '../components/CategoryStrengthBar'
import CategoryPills from '../components/CategoryPills'
import RadarChart from '../components/RadarChart'
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
  elite:   'bg-emerald-900/70 border border-emerald-600 text-emerald-300',
  solid:   'bg-field-900/70 border border-field-600 text-field-300',
  average: 'bg-navy-800 border border-navy-600 text-slate-400',
  weak:    'bg-red-900/60 border border-red-700 text-red-300',
  empty:   'bg-navy-900 border border-navy-700 text-slate-600',
}

function emptyPlayer() {
  return { name: '', playerId: null }
}

// ── TeamChips ─────────────────────────────────────────────────────────────────
// Horizontal scrolling chips for each league team. Selected team is highlighted.

function TeamChips({ teams, selectedTeamId, onSelect }) {
  return (
    <div className="flex flex-wrap gap-2">
      {teams.map(team => {
        const selected = team.team_id === selectedTeamId
        return (
          <button
            key={team.team_id}
            type="button"
            onClick={() => onSelect(team)}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium border transition-colors ${
              selected
                ? 'bg-field-700 border-field-600 text-white'
                : 'bg-navy-800 border-navy-700 text-slate-400 hover:border-navy-500 hover:text-slate-200'
            }`}
          >
            <Users size={11} className={selected ? 'text-field-300' : 'text-slate-600'} />
            {team.team_name}
          </button>
        )
      })}
    </div>
  )
}

export default function TeamEval() {
  const { league, myTeam } = useLeague() || {}

  const [players, setPlayers]               = useState([emptyPlayer()])
  const [selectedTeam, setSelectedTeam]     = useState(null)  // league team loaded into roster
  const [rankingType, setRankingType]       = useState('predictive')
  const [context, setContext]               = useState('')
  const [leagueSettings, setLeagueSettings] = useState(null)
  const [loading, setLoading]               = useState(false)
  const [error, setError]                   = useState(null)
  const [result, setResult]                 = useState(null)
  const [evaluatedTeamName, setEvaluatedTeamName] = useState(null)  // team name at time of last eval
  const [showManual, setShowManual]         = useState(false)  // collapse manual entry when team loaded

  // Screenshot upload state
  const [showUpload, setShowUpload]         = useState(false)
  const [extracting, setExtracting]         = useState(false)
  const [extractError, setExtractError]     = useState(null)
  const fileRef                             = useRef(null)

  const { playerRefs, focusNextOrAdd } = usePlayerListFocus(players, addPlayer)

  // If no league is connected, show manual entry by default
  const hasLeague = !!(league?.teams?.length)

  function loadTeam(team) {
    const roster = team.roster || []
    const loaded = roster.map(p => ({ name: p.name, playerId: p.player_id }))
    setPlayers(loaded.length > 0 ? loaded : [emptyPlayer()])
    setSelectedTeam(team)
    setShowManual(false)
    setError(null)
    // Don't clear result — previous eval stays visible until user clicks Evaluate again
  }

  function clearTeam() {
    setSelectedTeam(null)
    setPlayers([emptyPlayer()])
    setShowManual(true)
  }

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
    setSelectedTeam(null)  // screenshot replaces team selection
  }

  async function submit(e) {
    e.preventDefault()
    const resolved = players.filter(p => p.playerId != null).map(p => p.playerId)
    if (resolved.length < 1) {
      setError('Add at least one player to evaluate your team. Search by name and select from the dropdown.')
      return
    }
    setLoading(true); setError(null); setResult(null)
    setEvaluatedTeamName(selectedTeam?.team_name || null)
    try {
      const body = {
        player_ids:   resolved,
        context:      context || null,
        ranking_type: rankingType,
        league_id:    league?.league_id || null,
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
  const resolvedCount = players.filter(p => p.playerId != null).length

  // When a result exists for a league team, collapse the form to just
  // the team picker + evaluate button so results appear near the top.
  const compactForm = !!(result && hasLeague)

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

      <form
        onSubmit={submit}
        onKeyDown={e => { if (e.key === 'Enter' && e.target.tagName === 'INPUT') e.preventDefault() }}
        className="card space-y-5"
      >
        {/* ── League team picker (primary, only shown when connected) ─────────── */}
        {hasLeague && (
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <label className="section-label">Select a team</label>
              {selectedTeam && (
                <button
                  type="button"
                  onClick={clearTeam}
                  className="flex items-center gap-1 text-xs text-slate-600 hover:text-stitch-400 transition-colors"
                >
                  <X size={11} /> Clear
                </button>
              )}
            </div>
            <TeamChips
              teams={league.teams}
              selectedTeamId={selectedTeam?.team_id}
              onSelect={loadTeam}
            />
            {selectedTeam && !compactForm && (
              <div className="flex items-center gap-2 text-xs text-slate-500 pt-1">
                <Users size={12} className="text-field-500" />
                <span>
                  Loaded <strong className="text-white">{selectedTeam.team_name}</strong>
                  {' '}— {resolvedCount} player{resolvedCount !== 1 ? 's' : ''}
                </span>
              </div>
            )}
          </div>
        )}

        {/* ── Everything below is hidden in compact (post-result) mode ─────── */}
        {!compactForm && (
          <>
            {/* ── Divider / secondary toggle ──────────────────────────────── */}
            {hasLeague && (
              <div>
                <button
                  type="button"
                  onClick={() => setShowManual(v => !v)}
                  className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-300 transition-colors"
                >
                  {showManual
                    ? <ChevronDown size={13} />
                    : <ChevronRight size={13} />
                  }
                  {showManual ? 'Hide' : 'Or add players manually / upload screenshots'}
                </button>
              </div>
            )}

            {/* ── Manual entry + screenshot upload ──────────────────────── */}
            {(!hasLeague || showManual) && (
              <div className="space-y-4">
                <div>
                  <div className="flex items-center justify-between mb-2">
                    <label className="section-label">
                      {hasLeague ? 'Add or replace players' : 'Your Roster *'}
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
              </div>
            )}

            {/* Compact roster preview when league team loaded and manual hidden */}
            {hasLeague && !showManual && selectedTeam && (
              <div className="rounded-lg bg-navy-800/50 border border-navy-700 px-3 py-2 space-y-1 max-h-48 overflow-y-auto">
                {players.filter(p => p.name).map((p, i) => (
                  <div key={i} className="flex items-center justify-between gap-2">
                    <span className="text-xs text-slate-300 truncate">{p.name}</span>
                    {!p.playerId && (
                      <span className="text-[10px] text-stitch-500 shrink-0">unresolved</span>
                    )}
                  </div>
                ))}
                {players.filter(p => p.name).length === 0 && (
                  <span className="text-xs text-slate-600 italic">No players loaded.</span>
                )}
              </div>
            )}

            {/* ── Ranking type ──────────────────────────────────────────── */}
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
          </>
        )}

        {/* ── Evaluate button ── always visible ────────────────────────────── */}
        <button type="submit" className="btn-primary" disabled={loading}>
          <Play size={14} /> Evaluate Team
        </button>

        {/* ── League settings ── last item, collapsed by default ───────────── */}
        {!compactForm && (
          <LeagueSettings
            onChange={setLeagueSettings}
            initialValues={league ? {
              categories: league.scoring_categories,
              leagueType: league.league_type,
              numTeams: league.num_teams,
              rosterPositions: league.roster_positions,
            } : null}
          />
        )}
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
              {evaluatedTeamName && (
                <div className="text-xs text-slate-500 mt-0.5">{evaluatedTeamName}</div>
              )}
              <CategoryPills
                percentiles={result.league_category_percentiles || null}
                strongCats={result.strong_categories}
                weakCats={result.weak_categories}
                className="mt-2"
              />
            </div>
          </div>

          {/* Category strengths */}
          <div className="card">
            <div className="section-label mb-3">
              Category strength
              {result.league_category_percentiles && (
                <span className="ml-2 text-[10px] font-normal text-slate-600 normal-case tracking-normal">vs league</span>
              )}
            </div>
            {result.league_category_percentiles && (
              <div className="mb-4">
                <RadarChart
                  data={result.league_category_percentiles}
                  numTeams={league?.num_teams || 12}
                  asPercentiles
                />
              </div>
            )}
            <CategoryStrengthBar
              data={result.league_category_percentiles || result.category_strengths}
              numTeams={league?.num_teams || 12}
              asPercentiles={!!result.league_category_percentiles}
            />
          </div>

          {/* Position breakdown */}
          <div className="card">
            <div className="section-label mb-3">Position breakdown</div>
            <div className="divide-y divide-navy-800">
              {result.position_breakdown.map(g => (
                <div key={g.position} className="flex items-center gap-3 py-2.5 first:pt-0 last:pb-0">
                  {/* Position abbreviation */}
                  <span className="w-9 text-right font-mono text-xs font-bold text-slate-200 shrink-0">{g.position}</span>

                  {/* Players */}
                  <div className="flex-1 flex flex-wrap gap-x-3 gap-y-0.5 min-w-0">
                    {g.players.length > 0
                      ? g.players.map((name, i) => (
                          <span key={i} className="text-xs text-slate-300">{name}</span>
                        ))
                      : <span className="text-xs text-slate-600 italic">Empty</span>
                    }
                  </div>

                  {/* Assessment pill + score, grouped right */}
                  <div className="flex items-center gap-2 shrink-0">
                    <span className={`text-[10px] font-semibold px-2 py-0.5 rounded capitalize ${ASSESSMENT_PILL[g.assessment] || ''}`}>
                      {g.assessment}
                    </span>
                    <span className="text-[10px] font-mono text-slate-500 w-8 text-right">{g.group_score.toFixed(1)}</span>
                  </div>
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
