import { useState, useRef } from 'react'
import {
  Users, Play, ArrowLeftRight, Crown, Plus, X,
  Upload, ImageIcon, Loader2, AlertCircle,
} from 'lucide-react'
import { compareTeams, extractPlayers, searchPlayers } from '../lib/api'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import ContextInput from '../components/ContextInput'
import Blurb from '../components/Blurb'
import PercentileBar from '../components/PercentileBar'
import PlayerSearch from '../components/PlayerSearch'
import LeagueSettings from '../components/LeagueSettings'

// ── TeamCard result display ───────────────────────────────────────────────────

function TeamCard({ snap, rank, isWinner }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <div className={`card ${isWinner ? 'border-field-600' : ''}`}>
      <div className="flex items-start gap-3 mb-3">
        <div className="text-2xl font-bold font-mono text-slate-600 w-6 shrink-0">{rank}</div>
        <div className="flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            {isWinner && <Crown size={13} className="text-yellow-400" />}
            <span className="font-semibold text-white">{snap.team_name}</span>
            <span className="ml-auto font-mono text-sm text-field-400">{snap.power_score.toFixed(2)}</span>
          </div>
          <div className="flex flex-wrap gap-1 mt-1.5">
            {snap.strong_cats.map(c => (
              <span key={c} className="stat-pill bg-field-900 text-field-300 text-[10px]">{c} ▲</span>
            ))}
            {snap.weak_cats.slice(0, 3).map(c => (
              <span key={c} className="stat-pill bg-red-950/50 text-red-400 text-[10px]">{c} ▼</span>
            ))}
          </div>
          {snap.top_players.length > 0 && (
            <div className="text-xs text-slate-500 mt-1">Top: {snap.top_players.join(', ')}</div>
          )}
        </div>
        <button
          className="text-slate-600 hover:text-slate-300 text-xs shrink-0"
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? 'less' : 'more'}
        </button>
      </div>
      {expanded && <PercentileBar data={snap.category_strengths} />}
    </div>
  )
}

function TradeOpp({ opp, snapshots }) {
  const nameA = snapshots.find(s => s.team_id === opp.team_a_id)?.team_name || `Team ${opp.team_a_id}`
  const nameB = snapshots.find(s => s.team_id === opp.team_b_id)?.team_name || `Team ${opp.team_b_id}`
  return (
    <div className="p-4 rounded-lg bg-navy-800 border border-navy-700">
      <div className="flex items-center gap-2 text-sm mb-2">
        <span className="font-medium text-white">{nameA}</span>
        <ArrowLeftRight size={12} className="text-slate-500 shrink-0" />
        <span className="font-medium text-white">{nameB}</span>
        <span className="ml-auto font-mono text-xs text-field-400">+{opp.complementarity_score.toFixed(0)}</span>
      </div>
      <p className="text-xs text-slate-500 leading-relaxed">{opp.rationale}</p>
      {(opp.suggested_give || opp.suggested_receive) && (
        <div className="flex gap-4 mt-2 text-xs">
          {opp.suggested_give    && <span className="text-stitch-400">Give: {opp.suggested_give}</span>}
          {opp.suggested_receive && <span className="text-field-400">Receive: {opp.suggested_receive}</span>}
        </div>
      )}
    </div>
  )
}

// ── Team input block (with per-team screenshot upload) ────────────────────────

const TEAM_COLORS = [
  'border-field-700 bg-field-950/30',
  'border-stitch-700 bg-stitch-950/30',
  'border-leather-500/40 bg-leather-500/5',
  'border-blue-700/50 bg-blue-950/20',
  'border-purple-700/50 bg-purple-950/20',
  'border-yellow-700/50 bg-yellow-950/20',
]

function TeamInputBlock({ team, teamIdx, onChange, onRemove, canRemove }) {
  const [showUpload, setShowUpload]     = useState(false)
  const [extracting, setExtracting]     = useState(false)
  const [extractError, setExtractError] = useState(null)
  const fileRef                         = useRef(null)

  function addPlayer() {
    if (team.players.length < 30) {
      onChange({ ...team, players: [...team.players, { name: '', playerId: null }] })
    }
  }

  function removePlayer(idx) {
    onChange({ ...team, players: team.players.filter((_, i) => i !== idx) })
  }

  function updatePlayer(idx, name, playerId) {
    onChange({ ...team, players: team.players.map((p, i) => i === idx ? { name, playerId } : p) })
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

    // Auto-resolve each name
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
    const existing = team.players.filter(p => p.name || p.playerId)
    const merged   = [...existing, ...resolved]
    onChange({ ...team, players: merged.length > 0 ? merged : [{ name: '', playerId: null }] })
    setExtracting(false)
    setShowUpload(false)
  }

  const colorCls = TEAM_COLORS[teamIdx % TEAM_COLORS.length]

  return (
    <div className={'rounded-xl border p-4 space-y-3 ' + colorCls}>
      {/* Team name + remove */}
      <div className="flex items-center gap-2">
        <input
          className="field-input flex-1 font-medium"
          placeholder={'Team ' + (teamIdx + 1) + ' name…'}
          value={team.name}
          onChange={e => onChange({ ...team, name: e.target.value })}
        />
        {canRemove && (
          <button type="button" onClick={onRemove}
            className="shrink-0 text-slate-600 hover:text-stitch-400 transition-colors p-1">
            <X size={15} />
          </button>
        )}
      </div>

      {/* Player list */}
      <div className="space-y-2">
        {team.players.map((p, idx) => (
          <div key={idx} className="flex items-center gap-2">
            <PlayerSearch
              value={p.name}
              playerId={p.playerId}
              onChange={(name, playerId) => updatePlayer(idx, name, playerId)}
              onEnterKey={addPlayer}
              placeholder={'Player ' + (idx + 1) + '…'}
              className="flex-1"
            />
            {team.players.length > 1 && (
              <button type="button" onClick={() => removePlayer(idx)}
                className="shrink-0 text-slate-600 hover:text-stitch-400 transition-colors p-1">
                <X size={14} />
              </button>
            )}
          </div>
        ))}
      </div>

      {/* Actions row */}
      <div className="flex items-center gap-4">
        <button type="button" onClick={addPlayer}
          className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-300 transition-colors">
          <Plus size={12} /> Add player
        </button>
        <button
          type="button"
          onClick={() => { setShowUpload(v => !v); setExtractError(null) }}
          className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-300 transition-colors"
        >
          <Upload size={12} />
          {showUpload ? 'Hide upload' : 'Upload screenshots'}
        </button>
      </div>

      {/* Screenshot upload area (per-team) */}
      {showUpload && (
        <div className="space-y-2">
          <div
            className="border-2 border-dashed border-navy-600 rounded-xl p-4 text-center cursor-pointer hover:border-field-600 transition-colors"
            onClick={() => fileRef.current?.click()}
            onDragOver={e => e.preventDefault()}
            onDrop={e => { e.preventDefault(); handleFiles(e.dataTransfer.files) }}
          >
            <ImageIcon size={24} className="mx-auto text-slate-600 mb-1" />
            <p className="text-xs text-slate-400">Upload roster screenshots for this team</p>
            <p className="text-xs text-slate-600 mt-0.5">
              Drag & drop or click · Select multiple files if roster doesn't fit in one image
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
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

function newTeam(idx) {
  return { name: 'Team ' + (idx + 1), players: [{ name: '', playerId: null }] }
}

export default function CompareTeams() {
  const [teams, setTeams]               = useState([newTeam(0), newTeam(1)])
  const [context, setContext]           = useState('')
  const [leagueSettings, setLeagueSettings] = useState(null)
  const [loading, setLoading]           = useState(false)
  const [tradeLoading, setTradeLoading] = useState(false)
  const [error, setError]               = useState(null)
  const [result, setResult]             = useState(null)

  function updateTeam(idx, team) {
    setTeams(prev => prev.map((t, i) => i === idx ? team : t))
  }

  function buildBody(includeTrades) {
    const manualTeams = teams.map(team => ({
      name:       team.name || 'Unnamed Team',
      player_ids: team.players.filter(p => p.playerId != null).map(p => p.playerId),
    })).filter(t => t.player_ids.length > 0)
    if (manualTeams.length < 2) return null
    const body = {
      manual_teams:              manualTeams,
      context:                   context || null,
      include_trade_suggestions: includeTrades,
    }
    if (leagueSettings) {
      body.custom_categories  = leagueSettings.categories
      body.custom_league_type = leagueSettings.leagueType
    }
    return body
  }

  async function submit(e) {
    e.preventDefault()
    const body = buildBody(false)
    if (!body) {
      setError('Each team needs at least one resolved player. Search and select players by name.')
      return
    }
    setLoading(true); setError(null); setResult(null)
    try {
      const res = await compareTeams(body)
      setResult(res)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  async function addTradeSuggestions() {
    const body = buildBody(true)
    if (!body) return
    setTradeLoading(true)
    try {
      const res = await compareTeams(body)
      setResult(res)
    } catch (err) {
      setError(err.message)
    } finally {
      setTradeLoading(false)
    }
  }

  return (
    <div className="space-y-8">
      <div>
        <div className="flex items-center gap-2 mb-1">
          <Users size={18} className="text-field-400" />
          <h1 className="text-2xl font-bold text-white">Compare Teams</h1>
        </div>
        <p className="text-slate-500 text-sm">
          Power scores, category profiles, and optional trade opportunities for 2–6 teams.
        </p>
      </div>

      {/* Prevent Enter from submitting while in player or team-name inputs */}
      <form
        onSubmit={submit}
        onKeyDown={e => { if (e.key === 'Enter' && e.target.tagName === 'INPUT') e.preventDefault() }}
        className="space-y-5"
      >
        <div className="space-y-4">
          {teams.map((team, idx) => (
            <TeamInputBlock
              key={idx}
              team={team}
              teamIdx={idx}
              onChange={t => updateTeam(idx, t)}
              onRemove={() => setTeams(prev => prev.filter((_, i) => i !== idx))}
              canRemove={teams.length > 2}
            />
          ))}
        </div>

        {teams.length < 6 && (
          <button type="button"
            onClick={() => setTeams(prev => [...prev, newTeam(prev.length)])}
            className="flex items-center gap-1.5 text-sm text-field-400 hover:text-field-300 transition-colors">
            <Plus size={14} /> Add another team
          </button>
        )}

        <ContextInput value={context} onChange={setContext} />
        <LeagueSettings onChange={setLeagueSettings} />

        <button type="submit" className="btn-primary" disabled={loading}>
          <Play size={14} /> Compare Teams
        </button>
      </form>

      <ErrorBanner message={error} onClose={() => setError(null)} />
      {loading && <LoadingState message="Comparing rosters…" />}

      {result && (
        <div className="space-y-6">
          <div className="space-y-3">
            {result.snapshots.map((snap, i) => (
              <TeamCard key={snap.team_id} snap={snap} rank={i + 1} isWinner={snap.team_id === result.winner} />
            ))}
          </div>

          {result.trade_opportunities.length === 0 && !tradeLoading && (
            <button type="button" onClick={addTradeSuggestions} disabled={tradeLoading}
              className="flex items-center gap-2 px-4 py-2.5 rounded-lg border border-field-700 text-field-400 hover:bg-field-900 text-sm transition-colors">
              <ArrowLeftRight size={14} /> Find Trade Opportunities
            </button>
          )}
          {tradeLoading && (
            <div className="flex items-center gap-2 text-xs text-field-400">
              <Loader2 size={13} className="animate-spin" /> Finding trade opportunities…
            </div>
          )}

          {result.trade_opportunities.length > 0 && (
            <div className="card space-y-3">
              <div className="section-label">Trade opportunities</div>
              {result.trade_opportunities.map((opp, i) => (
                <TradeOpp key={i} opp={opp} snapshots={result.snapshots} />
              ))}
            </div>
          )}

          <Blurb text={result.analysis_blurb} />
        </div>
      )}
    </div>
  )
}
