import { useState, useMemo, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { Search, Play, RefreshCw, Loader2, Star, TrendingUp, X, Trash2, Users, AlertTriangle, ChevronDown, ChevronRight, ArrowUpCircle, Handshake, ArrowLeftRight } from 'lucide-react'
import { findPlayer, rosterAnalysis } from '../lib/api'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import ContextInput from '../components/ContextInput'
import { useLeague } from '../contexts/LeagueContext'

// ── Constants ──────────────────────────────────────────────────────────────

const COMMON_POSITIONS = ['C', '1B', '2B', '3B', 'SS', 'OF', 'Util', 'SP', 'RP']
const DEFAULT_CATS     = ['R', 'HR', 'RBI', 'SB', 'AVG', 'W', 'SV', 'K', 'ERA', 'WHIP']
const SKIP_CATS        = new Set(['H/AB', 'Batting', 'Pitching', 'AB'])

const BATTER_SLOTS  = new Set(['C', '1B', '2B', '3B', 'SS', 'OF'])
const PITCHER_SLOTS = new Set(['SP', 'RP', 'P'])

const BATTING_CATS  = new Set(['R', 'HR', 'RBI', 'SB', 'AVG', 'OPS', 'H', 'TB', 'XBH',
                                'BB', 'NSB', 'SLG', 'OBP', 'CS', 'HBP', 'GDP'])
const PITCHING_CATS = new Set(['W', 'L', 'SV', 'K', 'ERA', 'WHIP', 'IP', 'QS', 'HLD',
                                'SVH', 'BS', 'HD', 'HA', 'K/9', 'BB/9', 'NH', 'CG', 'SHO'])

const POOL_LABELS = { mlb: 'MLB', milb: 'MiLB', both: 'Both' }

// Assessment → visual style
const ASSESSMENT_BORDER = {
  elite:   'border-field-600',
  solid:   'border-field-800',
  average: 'border-navy-600',
  weak:    'border-stitch-700',
  empty:   'border-red-900',
}
const ASSESSMENT_LABEL_STYLE = {
  elite:   'text-field-300 bg-field-900/60',
  solid:   'text-field-400 bg-field-950',
  average: 'text-slate-400 bg-navy-800',
  weak:    'text-stitch-300 bg-stitch-950/50',
  empty:   'text-red-400 bg-red-950/50',
}

// Trade difficulty → visual style
const DIFFICULTY_STYLE = {
  possible:    'text-field-300 bg-field-900/40 border-field-800',
  hard:        'text-leather-200 bg-leather-900/30 border-leather-800',
  unrealistic: 'text-slate-500 bg-navy-800 border-navy-700',
}

// ── SuggestionCard (Find a Player tab) ────────────────────────────────────

function SuggestionCard({ s, isLatest, onDismiss }) {
  return (
    <div className={`p-4 rounded-xl border ${isLatest ? 'bg-field-950/40 border-field-700' : 'bg-navy-800 border-navy-700'}`}>
      <div className="flex items-start gap-3">
        {isLatest && <Star size={14} className="text-field-400 shrink-0 mt-1" />}
        <div className="flex-1 min-w-0">
          <div className="text-[10px] text-slate-500 font-mono uppercase tracking-wide mb-1.5">
            {s._label}
          </div>
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
          {s.blurb && (
            <p className="text-xs text-slate-400 leading-relaxed mt-1">{s.blurb}</p>
          )}
          {s.is_prospect && !s.blurb && (
            <p className="text-xs text-slate-500 italic mt-1">
              Top prospect by PAV score — category impact not available for MiLB players.
            </p>
          )}
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

// ── Roster Analysis sub-components ────────────────────────────────────────

// Positions that belong to each section
const BATTER_START_SLOTS = new Set(['C', '1B', '2B', '3B', 'SS', 'OF', 'Util', 'DH'])
const PITCHER_START_SLOTS = new Set(['SP', 'RP', 'P'])

// Injury status → display
const INJURY_LABEL = {
  il_10:         { text: 'IL-10',  cls: 'bg-stitch-900/50 border-stitch-700 text-stitch-300' },
  il_60:         { text: 'IL-60',  cls: 'bg-red-900/50 border-red-700 text-red-300' },
  day_to_day:    { text: 'DTD',    cls: 'bg-leather-900/40 border-leather-700 text-leather-300' },
  out_for_season:{ text: 'OUT',    cls: 'bg-red-900/60 border-red-700 text-red-200' },
}

function InjuryBadge({ status }) {
  if (!status) return null
  const cfg = INJURY_LABEL[status]
  if (!cfg) return null
  return (
    <span className={`text-[9px] font-bold px-1 py-0.5 rounded border ${cfg.cls}`}>{cfg.text}</span>
  )
}

function GradeBadge({ grade }) {
  const colours = {
    A: 'text-field-300 border-field-500 bg-field-950',
    B: 'text-field-400 border-field-600 bg-field-950',
    C: 'text-leather-300 border-leather-500 bg-leather-500/10',
    D: 'text-stitch-300 border-stitch-500 bg-stitch-500/10',
    F: 'text-red-300 border-red-700 bg-red-950',
  }
  const cls = colours[grade] || colours.C
  return (
    <span className={`inline-flex items-center justify-center w-9 h-9 rounded-lg border-2 font-bold text-lg ${cls}`}>
      {grade}
    </span>
  )
}

// ── Upgrade section components ────────────────────────────────────────────

function TradeTargetRow({ t, myTeamId }) {
  const navigate = useNavigate()

  const handleEvaluateTrade = () => {
    navigate('/trade', {
      state: {
        preloadReceiving: {
          player_name: t.player_name,
          player_id: t.player_id,
        },
        my_team_id: myTeamId,
      }
    })
  }

  return (
    <div className="flex items-start gap-2">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="text-white text-sm font-medium">{t.player_name}</span>
          {t.positions.map(p => (
            <span key={p} className="stat-pill bg-navy-700 text-slate-500 text-[10px]">{p}</span>
          ))}
          <InjuryBadge status={t.injury_status} />
          <span className="text-slate-600 text-[10px]">({t.owner_team_name})</span>
        </div>
        <p className="text-[10px] text-slate-500 mt-0.5">{t.difficulty_reason}</p>
        {t.blurb && <p className="text-[10px] text-slate-600 mt-0.5 italic">{t.blurb}</p>}
        <button
          onClick={handleEvaluateTrade}
          className="mt-1 flex items-center gap-1 text-[10px] text-field-500 hover:text-field-300 transition-colors"
        >
          <ArrowLeftRight size={10} />
          Evaluate Trade
        </button>
      </div>
      <span className={`shrink-0 text-[10px] font-medium px-1.5 py-0.5 rounded border capitalize ${DIFFICULTY_STYLE[t.difficulty] || ''}`}>
        {t.difficulty}
      </span>
    </div>
  )
}

function WaiverRow({ w, isSpecialSlot }) {
  return (
    <div className="flex items-start gap-2">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="text-white text-sm font-medium">{w.player_name}</span>
          {w.positions.map(p => (
            <span key={p} className="stat-pill bg-navy-700 text-slate-500 text-[10px]">{p}</span>
          ))}
          <InjuryBadge status={w.injury_status} />
        </div>
        {w.blurb && <p className="text-[10px] text-slate-500 mt-0.5 italic">{w.blurb}</p>}
        {!isSpecialSlot && Object.keys(w.category_impact || {}).length > 0 && (
          <div className="flex flex-wrap gap-1 mt-1">
            {Object.entries(w.category_impact)
              .sort((a, b) => b[1] - a[1])
              .slice(0, 4)
              .map(([cat, val]) => (
                <span key={cat} className="text-[9px] font-mono px-1 py-0.5 rounded bg-field-900/50 border border-field-800 text-field-400">
                  {cat} +{val.toFixed(2)}
                </span>
              ))
            }
          </div>
        )}
      </div>
      <span className="font-mono text-xs text-field-400 shrink-0 pt-0.5">{w.score.toFixed(2)}</span>
    </div>
  )
}

function UpgradeSection({ slot, myTeamId }) {
  const isSpecial = slot.position === 'NA' || slot.position === 'IL'
  const actionableTrades = slot.trade_targets.filter(t => t.difficulty !== 'unrealistic')
  const unrealisticTrades = slot.trade_targets.filter(t => t.difficulty === 'unrealistic')
  const hasAny = slot.waiver_upgrades.length > 0 || slot.trade_targets.length > 0

  const waiverLabel = isSpecial
    ? (slot.position === 'NA' ? 'MiLB Stash Candidates' : 'IL Stash Candidates')
    : 'Free Agents / Waivers'

  if (!hasAny) {
    return (
      <p className="text-[10px] text-slate-600 italic pt-1">
        No upgrade candidates found — sync your league for the latest data.
      </p>
    )
  }

  return (
    <div className="space-y-4 pt-1">
      {slot.waiver_upgrades.length > 0 && (
        <div>
          <div className="flex items-center gap-1.5 mb-2">
            <ArrowUpCircle size={11} className="text-field-500" />
            <span className="text-[10px] font-semibold text-field-400 uppercase tracking-wide">{waiverLabel}</span>
          </div>
          <div className="space-y-2.5">
            {slot.waiver_upgrades.map(w => <WaiverRow key={w.player_id} w={w} isSpecialSlot={isSpecial} />)}
          </div>
        </div>
      )}
      {slot.trade_targets.length > 0 && (
        <div>
          <div className="flex items-center gap-1.5 mb-2">
            <Handshake size={11} className="text-leather-400" />
            <span className="text-[10px] font-semibold text-leather-300 uppercase tracking-wide">Trade Targets</span>
          </div>
          <div className="space-y-2.5">
            {actionableTrades.map(t => <TradeTargetRow key={t.player_id} t={t} myTeamId={myTeamId} />)}
            {actionableTrades.length > 0 && unrealisticTrades.length > 0 && (
              <div className="flex items-center gap-2 pt-2 pb-1">
                <div className="flex-1 h-px bg-navy-700" />
                <span className="text-[10px] text-slate-500 font-medium uppercase tracking-widest shrink-0">Long shots</span>
                <div className="flex-1 h-px bg-navy-700" />
              </div>
            )}
            {unrealisticTrades.map(t => <TradeTargetRow key={t.player_id} t={t} myTeamId={myTeamId} />)}
          </div>
        </div>
      )}
    </div>
  )
}

// ── SlotRow — one roster slot row (1 player + optional upgrade section) ────

function SlotRow({ slot, myTeamId }) {
  // Upgrade slots start expanded; solid/elite start collapsed
  const defaultExpanded = slot.has_upgrades &&
    (slot.assessment === 'weak' || slot.assessment === 'empty' || slot.assessment === 'average')
  const [expanded, setExpanded] = useState(defaultExpanded)

  const isSpecial = slot.position === 'NA' || slot.position === 'IL'
  const player = slot.player_details[0] || null

  const borderCls = ASSESSMENT_BORDER[slot.assessment] || 'border-navy-700'

  return (
    <div className={`rounded-xl border bg-navy-900 overflow-hidden ${borderCls}`}>
      {/* Player row */}
      <div className="flex items-center gap-3 px-4 py-2.5">
        <span className="text-[10px] font-bold text-slate-500 w-8 shrink-0 font-mono uppercase">
          {slot.position}
        </span>

        <div className="flex-1 min-w-0 flex items-center gap-1.5 flex-wrap">
          {player ? (
            <>
              <span className="text-sm text-slate-200 font-medium">{player.player_name}</span>
              <InjuryBadge status={player.injury_status} />
              {player.top_categories.map(cat => (
                <span key={cat} className="text-[9px] font-mono px-1 py-0.5 rounded bg-navy-700 border border-navy-600 text-slate-400">
                  {cat}
                </span>
              ))}
            </>
          ) : isSpecial ? (
            <span className="text-xs text-slate-500 italic">
              {slot.position === 'NA'
                ? 'Stash a MiLB prospect until their debut'
                : 'Stash an injured player returning within 60 days'}
            </span>
          ) : (
            <span className="text-xs text-slate-600 italic">Empty</span>
          )}
        </div>

        {/* Collapse/expand toggle for slots with upgrades */}
        {slot.has_upgrades && (
          <button
            onClick={() => setExpanded(e => !e)}
            className="shrink-0 flex items-center gap-1 text-[10px] text-slate-500 hover:text-slate-300 transition-colors"
            aria-label={expanded ? 'Collapse upgrades' : 'Show upgrades'}
          >
            {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          </button>
        )}
      </div>

      {/* Upgrade section — shown when expanded */}
      {slot.has_upgrades && expanded && (
        <div className="border-t border-navy-800 px-4 py-3">
          <UpgradeSection slot={slot} myTeamId={myTeamId} />
        </div>
      )}
    </div>
  )
}

// ── SlotGroup — a labeled group of slots (collapsible) ────────────────────

function SlotGroup({ title, slots, defaultCollapsed = false, myTeamId }) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed)
  if (slots.length === 0) return null
  return (
    <div>
      <button
        onClick={() => setCollapsed(c => !c)}
        className="flex items-center gap-1.5 text-[10px] text-slate-500 hover:text-slate-300 transition-colors mb-2 w-full text-left"
      >
        {collapsed ? <ChevronRight size={11} /> : <ChevronDown size={11} />}
        <span className="uppercase tracking-widest font-semibold">{title}</span>
        <span className="text-slate-700 ml-1">({slots.length})</span>
      </button>
      {!collapsed && (
        <div className="space-y-2">
          {slots.map(s => <SlotRow key={`${s.position}-${s.slot_index}`} slot={s} myTeamId={myTeamId} />)}
        </div>
      )}
    </div>
  )
}

// ── RosterAnalysisPanel ───────────────────────────────────────────────────

function RosterAnalysisPanel({ selectedTeam, data, loading, error, onLoad, onDismissError }) {
  if (!selectedTeam) {
    return (
      <div className="p-4 rounded-xl border border-amber-800/40 bg-amber-950/20 text-amber-400 text-sm">
        Connect your Yahoo account from Profile to use this feature.
      </div>
    )
  }

  if (loading) return <LoadingState message="Analyzing roster…" />

  // Split slots by type and upgrade status
  const isBatter  = s => BATTER_START_SLOTS.has(s.position) ||
    (s.position === 'BN' && s.player_details[0] && !['SP','RP','P'].some(p => (s.player_details[0].positions || []).includes(p)))
  const isPitcher = s => PITCHER_START_SLOTS.has(s.position) ||
    (s.position === 'BN' && s.player_details[0] && ['SP','RP','P'].some(p => (s.player_details[0].positions || []).includes(p)))

  const allSlots = data?.slots || []
  const batterUpgrade  = allSlots.filter(s => isBatter(s)  && s.has_upgrades)
  const batterSolid    = allSlots.filter(s => isBatter(s)  && !s.has_upgrades)
  const pitcherUpgrade = allSlots.filter(s => isPitcher(s) && s.has_upgrades)
  const pitcherSolid   = allSlots.filter(s => isPitcher(s) && !s.has_upgrades)
  const specialSlots   = allSlots.filter(s => s.position === 'NA' || s.position === 'IL')

  const hasUpgrades = batterUpgrade.length > 0 || pitcherUpgrade.length > 0

  return (
    <div className="space-y-5">
      <ErrorBanner message={error} onClose={onDismissError} />

      {data && (
        <>
          {/* Summary bar */}
          <div className="card flex items-center gap-4 flex-wrap">
            <GradeBadge grade={data.overall_grade} />
            <div>
              <p className="text-xs text-slate-500">Overall grade</p>
              <p className="text-sm text-white font-medium">{data.grade_percentile.toFixed(0)}th percentile in league</p>
            </div>
            {data.weak_categories.length > 0 && (
              <div className="flex items-center gap-2 flex-wrap">
                <AlertTriangle size={13} className="text-stitch-400 shrink-0" />
                <span className="text-xs text-slate-500">Weak in:</span>
                {data.weak_categories.map(c => (
                  <span key={c} className="stat-pill bg-stitch-950/50 border border-stitch-800 text-stitch-300 text-[10px]">{c}</span>
                ))}
              </div>
            )}
            <button
              onClick={onLoad}
              className="ml-auto flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-300 transition-colors"
            >
              <RefreshCw size={12} />
              Refresh
            </button>
          </div>

          {/* Upgrades available — Batters then Pitchers */}
          {hasUpgrades && (
            <div className="space-y-4">
              <h3 className="text-xs font-semibold text-slate-400 uppercase tracking-widest">Upgrades Available</h3>
              {batterUpgrade.length > 0 && (
                <div className="space-y-2">
                  <p className="text-[10px] text-slate-600 uppercase tracking-widest font-semibold">Batters</p>
                  {batterUpgrade.map(s => <SlotRow key={`${s.position}-${s.slot_index}`} slot={s} myTeamId={selectedTeam?.team_id} />)}
                </div>
              )}
              {pitcherUpgrade.length > 0 && (
                <div className="space-y-2">
                  <p className="text-[10px] text-slate-600 uppercase tracking-widest font-semibold">Pitchers</p>
                  {pitcherUpgrade.map(s => <SlotRow key={`${s.position}-${s.slot_index}`} slot={s} myTeamId={selectedTeam?.team_id} />)}
                </div>
              )}
            </div>
          )}

          {/* Strong positions — collapsed by default */}
          {(batterSolid.length > 0 || pitcherSolid.length > 0) && (
            <div className="space-y-3">
              <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-widest">No Upgrades Needed</h3>
              <SlotGroup title="Batters" slots={batterSolid} defaultCollapsed={true} myTeamId={selectedTeam?.team_id} />
              <SlotGroup title="Pitchers" slots={pitcherSolid} defaultCollapsed={true} myTeamId={selectedTeam?.team_id} />
            </div>
          )}

          {/* Special stash slots (NA / IL) */}
          {specialSlots.length > 0 && (
            <div className="space-y-3">
              <h3 className="text-xs font-semibold text-slate-500 uppercase tracking-widest">Stash Spots</h3>
              <div className="space-y-2">
                {specialSlots.map(s => <SlotRow key={`${s.position}-${s.slot_index}`} slot={s} myTeamId={selectedTeam?.team_id} />)}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}

// ── FindPlayer page ────────────────────────────────────────────────────────

export default function FindPlayer() {
  const { league, myTeam } = useLeague() || {}

  const [tab, setTab] = useState('analysis')

  // Team selector — defaults to myTeam, but can be switched to any league team
  const [selectedTeamId, setSelectedTeamId] = useState(null)
  const allTeams = league?.teams || []
  const selectedTeam = allTeams.find(t => t.team_id === selectedTeamId) || myTeam || null

  // Sync selectedTeamId to myTeam when it first loads
  useEffect(() => {
    if (myTeam && !selectedTeamId) setSelectedTeamId(myTeam.team_id)
  }, [myTeam?.team_id]) // eslint-disable-line react-hooks/exhaustive-deps

  // Roster Analysis state (lifted here so it survives tab switches)
  const [analysisData,    setAnalysisData]    = useState(null)
  const [analysisLoading, setAnalysisLoading] = useState(false)
  const [analysisError,   setAnalysisError]   = useState(null)

  async function loadAnalysis(teamId) {
    const tid = teamId ?? selectedTeam?.team_id
    if (!tid) return
    setAnalysisLoading(true)
    setAnalysisError(null)
    try {
      const res = await rosterAnalysis(tid)
      setAnalysisData(res)
    } catch (err) {
      setAnalysisError(err.message)
    } finally {
      setAnalysisLoading(false)
    }
  }

  // Auto-fetch when the selected team changes (or on first load)
  useEffect(() => {
    if (selectedTeam?.team_id && !analysisLoading) {
      setAnalysisData(null)
      loadAnalysis(selectedTeam.team_id)
    }
  }, [selectedTeam?.team_id]) // eslint-disable-line react-hooks/exhaustive-deps

  // Find a Player form state
  const [positionSlot, setPositionSlot] = useState('')
  const [priorityCats, setPriorityCats] = useState([])
  const [playerPool,   setPlayerPool]   = useState('mlb')
  const [context,      setContext]      = useState('')

  const [suggestions, setSuggestions] = useState([])
  const [loading,     setLoading]     = useState(false)
  const [error,       setError]       = useState(null)

  const rosterPositions = league?.roster_positions?.length > 0
    ? [...new Set(league.roster_positions.filter(p => !['BN', 'DL', 'NA', 'IL'].includes(p)))]
    : COMMON_POSITIONS

  const allCats = useMemo(() => {
    const raw = league?.scoring_categories || DEFAULT_CATS
    return raw.filter(c => !SKIP_CATS.has(c))
  }, [league])

  const visibleCats = useMemo(() => {
    if (BATTER_SLOTS.has(positionSlot))  return allCats.filter(c => BATTING_CATS.has(c))
    if (PITCHER_SLOTS.has(positionSlot)) return allCats.filter(c => PITCHING_CATS.has(c))
    return allCats
  }, [positionSlot, allCats])

  useEffect(() => {
    setPriorityCats(prev => prev.filter(c => visibleCats.includes(c)))
  }, [positionSlot]) // eslint-disable-line react-hooks/exhaustive-deps

  const hasResults = suggestions.length > 0
  const canSubmit  = !loading && !!myTeam && (!!positionSlot || priorityCats.length > 0)

  function togglePosition(pos) { setPositionSlot(prev => prev === pos ? '' : pos) }
  function toggleCat(cat) {
    setPriorityCats(prev => prev.includes(cat) ? prev.filter(c => c !== cat) : [...prev, cat])
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
          Roster analysis and targeted upgrade recommendations for your team.
        </p>
      </div>

      {/* Tab switcher */}
      <div className="flex gap-2">
        <button
          onClick={() => setTab('analysis')}
          className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium border transition-colors ${
            tab === 'analysis'
              ? 'bg-field-700 border-field-600 text-white'
              : 'bg-navy-800 border-navy-700 text-slate-400 hover:text-white hover:border-navy-600'
          }`}
        >
          <Users size={14} />
          Roster Analysis
        </button>
        <button
          onClick={() => setTab('find')}
          className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium border transition-colors ${
            tab === 'find'
              ? 'bg-field-700 border-field-600 text-white'
              : 'bg-navy-800 border-navy-700 text-slate-400 hover:text-white hover:border-navy-600'
          }`}
        >
          <Search size={14} />
          Find a Player
        </button>
      </div>

      {/* Team selector pills — shown below tabs on analysis tab */}
      {tab === 'analysis' && allTeams.length > 0 && (
        <div className="space-y-2">
          <p className="text-[10px] text-slate-500 uppercase tracking-widest font-semibold">Select a Team</p>
          <div className="flex flex-wrap gap-2">
            {allTeams.map(t => {
              const selected = t.team_id === selectedTeamId
              return (
                <button
                  key={t.team_id}
                  type="button"
                  onClick={() => setSelectedTeamId(t.team_id)}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium border transition-colors ${
                    selected
                      ? 'bg-field-700 border-field-600 text-white'
                      : 'bg-navy-800 border-navy-700 text-slate-400 hover:border-navy-500 hover:text-slate-200'
                  }`}
                >
                  <Users size={11} className={selected ? 'text-field-300' : 'text-slate-600'} />
                  {t.team_name}
                </button>
              )
            })}
          </div>
        </div>
      )}

      {/* Tab content */}
      {tab === 'analysis' && (
        <RosterAnalysisPanel
          selectedTeam={selectedTeam}
          data={analysisData}
          loading={analysisLoading}
          error={analysisError}
          onLoad={() => loadAnalysis(selectedTeam?.team_id)}
          onDismissError={() => setAnalysisError(null)}
        />
      )}

      {tab === 'find' && (
        <>
          {!myTeam && (
            <div className="p-4 rounded-xl border border-amber-800/40 bg-amber-950/20 text-amber-400 text-sm">
              Connect your Yahoo account from Profile to use this feature.
            </div>
          )}

          <form onSubmit={submit} className="card space-y-5">
            {/* Position slot */}
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

            {/* Priority categories */}
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

            {myTeam && !positionSlot && priorityCats.length === 0 && (
              <p className="text-[11px] text-slate-500">
                Select at least one position or category above to enable search.
              </p>
            )}

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

          {loading && !hasResults && <LoadingState message="Finding the best available player…" />}

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
        </>
      )}
    </div>
  )
}
