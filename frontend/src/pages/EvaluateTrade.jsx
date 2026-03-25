import { useState, useRef, useEffect, useMemo } from 'react'
import { ArrowLeftRight, Play, TrendingUp, TrendingDown, Minus, X, ChevronDown, ChevronRight, Search, Plus } from 'lucide-react'
import { evaluateTrade, searchPlayers, getRankings } from '../lib/api'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import ContextInput from '../components/ContextInput'
import Blurb from '../components/Blurb'
import ProsCons from '../components/ProsCons'
import PercentileBar from '../components/PercentileBar'
import PlayerSearch from '../components/PlayerSearch'
import LeagueSettings from '../components/LeagueSettings'
import { useLeague } from '../contexts/LeagueContext'

// ── Constants ─────────────────────────────────────────────────────────────────

const CURRENT_YEAR = new Date().getFullYear()
const TRADE_YEAR = CURRENT_YEAR + 1
const TRADEABLE_ROUNDS = [1,2,3,4,5,6,7,8,9,10,11,12,13]

function ordinal(n) {
  const s = ['th','st','nd','rd']
  const v = n % 100
  return n + (s[(v-20)%10] || s[v] || s[0])
}
function pickToString(round) {
  return `${TRADE_YEAR} ${ordinal(round)} round pick`
}

// ── Verdict config ────────────────────────────────────────────────────────────

const VERDICT_CONFIG = {
  favor_receive: {
    label: 'Take the Trade',
    Icon: TrendingUp,
    cls: 'bg-field-900 border-field-600 text-field-300',
  },
  favor_give: {
    label: 'Pass on This Trade',
    Icon: TrendingDown,
    cls: 'bg-red-950 border-red-800 text-red-300',
  },
  fair: {
    label: 'Fair Trade',
    Icon: Minus,
    cls: 'bg-navy-700 border-navy-600 text-slate-300',
  },
}

function VerdictBadge({ verdict, confidence }) {
  const cfg = VERDICT_CONFIG[verdict] || VERDICT_CONFIG.fair
  const { label, Icon, cls } = cfg
  return (
    <div className={`flex items-center gap-3 px-5 py-4 rounded-xl border ${cls}`}>
      <Icon size={22} />
      <div>
        <div className="font-bold text-lg leading-tight">{label}</div>
        <div className="text-xs opacity-70">{(confidence * 100).toFixed(0)}% confidence</div>
      </div>
    </div>
  )
}

// ── TradeSummaryBar ───────────────────────────────────────────────────────────

function TradeSummaryBar({
  givingPlayers, givingPicks,
  receivingPlayers, receivingPicks,
  givingValue, receivingValue, valueDiff, hasValueData,
  onRemoveGivingPlayer, onRemoveGivingPick,
  onRemoveReceivingPlayer, onRemoveReceivingPick,
  pickValue,
  onEvaluate, loading,
}) {
  const bothEmpty = givingPlayers.length === 0 && givingPicks.length === 0 &&
    receivingPlayers.length === 0 && receivingPicks.length === 0
  const showValues = hasValueData && (givingValue > 0 || receivingValue > 0)

  return (
    <div className="sticky top-0 z-30 bg-navy-950/95 backdrop-blur border-b border-navy-700 px-4 py-3 space-y-2">
      {/* Row 1: pills + evaluate */}
      <div className="flex items-start gap-3">
        {/* Giving */}
        <div className="flex-1 min-w-0">
          <div className="text-xs font-semibold text-stitch-400 uppercase tracking-wider mb-1">Giving</div>
          <div className="flex flex-wrap gap-1">
            {givingPlayers.map(p => (
              <button
                key={p.playerId}
                type="button"
                onClick={() => onRemoveGivingPlayer(p.playerId)}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-stitch-900/60 border border-stitch-700 text-stitch-300 max-w-[11rem] hover:bg-stitch-900/80 group transition-colors"
              >
                <span className="truncate">{p.name}{p.positions?.[0] ? ` · ${p.positions[0]}` : ''}</span>
                <X size={9} className="shrink-0 opacity-40 group-hover:opacity-100 transition-opacity" />
              </button>
            ))}
            {givingPicks.map(r => {
              const pv = pickValue(r)
              return (
                <button
                  key={r}
                  type="button"
                  onClick={() => onRemoveGivingPick(r)}
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-stitch-900/40 border border-stitch-800 text-stitch-400 hover:bg-stitch-900/60 group transition-colors"
                >
                  <span>R{r}{pv > 0 ? ` ~${pv.toFixed(0)}` : ''}</span>
                  <X size={9} className="shrink-0 opacity-40 group-hover:opacity-100 transition-opacity" />
                </button>
              )
            })}
            {givingPlayers.length === 0 && givingPicks.length === 0 && (
              <span className="text-xs text-slate-600 italic">none selected</span>
            )}
          </div>
        </div>

        {/* Center arrow */}
        <div className="shrink-0 text-slate-500 mt-5">
          <ArrowLeftRight size={18} />
        </div>

        {/* Receiving */}
        <div className="flex-1 min-w-0">
          <div className="text-xs font-semibold text-field-400 uppercase tracking-wider mb-1">Receiving</div>
          <div className="flex flex-wrap gap-1">
            {receivingPlayers.map(p => (
              <button
                key={p.playerId}
                type="button"
                onClick={() => onRemoveReceivingPlayer(p.playerId)}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-field-900/60 border border-field-700 text-field-300 max-w-[11rem] hover:bg-field-900/80 group transition-colors"
              >
                <span className="truncate">{p.name}{p.positions?.[0] ? ` · ${p.positions[0]}` : ''}</span>
                <X size={9} className="shrink-0 opacity-40 group-hover:opacity-100 transition-opacity" />
              </button>
            ))}
            {receivingPicks.map(r => {
              const pv = pickValue(r)
              return (
                <button
                  key={r}
                  type="button"
                  onClick={() => onRemoveReceivingPick(r)}
                  className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-field-900/40 border border-field-800 text-field-400 hover:bg-field-900/60 group transition-colors"
                >
                  <span>R{r}{pv > 0 ? ` ~${pv.toFixed(0)}` : ''}</span>
                  <X size={9} className="shrink-0 opacity-40 group-hover:opacity-100 transition-opacity" />
                </button>
              )
            })}
            {receivingPlayers.length === 0 && receivingPicks.length === 0 && (
              <span className="text-xs text-slate-600 italic">none selected</span>
            )}
          </div>
        </div>

        {/* Evaluate button */}
        <div className="shrink-0 mt-4">
          <button
            onClick={onEvaluate}
            disabled={loading || bothEmpty}
            className="btn-primary text-xs py-1.5 px-3 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Play size={12} />
            Evaluate
          </button>
        </div>
      </div>

      {/* Row 2: Live value estimates */}
      {showValues && (
        <div className="flex items-center gap-3 pt-1 border-t border-navy-800">
          <div className="flex-1 text-xs font-mono text-stitch-400">
            ~{givingValue.toFixed(1)}
          </div>
          <div className={`shrink-0 text-xs font-mono font-bold ${valueDiff >= 0 ? 'text-field-400' : 'text-stitch-400'}`}>
            {valueDiff >= 0 ? '+' : ''}{valueDiff.toFixed(1)}
          </div>
          <div className="flex-1 text-xs font-mono text-field-400 text-right">
            ~{receivingValue.toFixed(1)}
          </div>
        </div>
      )}
      {showValues && (
        <div className="text-[10px] text-slate-600 -mt-1">est. projected season value</div>
      )}
    </div>
  )
}

// ── DraftPickSelector ─────────────────────────────────────────────────────────

function DraftPickSelector({ selected, onChange, side, pickValueFn }) {
  function toggle(round) {
    if (selected.includes(round)) {
      onChange(selected.filter(r => r !== round))
    } else {
      onChange([...selected, round].sort((a, b) => a - b))
    }
  }

  const selectedCls = side === 'give'
    ? 'bg-stitch-900/70 border-stitch-600 text-stitch-300'
    : 'bg-field-900/70 border-field-600 text-field-300'
  const unselectedCls = 'bg-navy-800 border-navy-600 text-slate-500 hover:border-navy-500 hover:text-slate-300'

  return (
    <div className="space-y-2">
      <div className="section-label">{TRADE_YEAR} Draft Picks</div>
      <div className="flex flex-wrap gap-1.5">
        {TRADEABLE_ROUNDS.map(r => {
          const pv = pickValueFn ? pickValueFn(r) : 0
          return (
            <button
              key={r}
              type="button"
              onClick={() => toggle(r)}
              className={`flex flex-col items-center px-2 py-1 rounded text-xs font-medium border transition-colors ${selected.includes(r) ? selectedCls : unselectedCls}`}
            >
              <span>R{r}</span>
              {pv > 0 && <span className="text-[9px] opacity-60 leading-none mt-0.5">~{pv.toFixed(0)}</span>}
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ── PlayerCard ────────────────────────────────────────────────────────────────

function PlayerCard({ player, rankData, inTrade, side, onAdd, onRemove }) {
  const hoverBorder = side === 'give'
    ? 'hover:border-stitch-700 hover:bg-stitch-900/20'
    : 'hover:border-field-700 hover:bg-field-900/20'

  const positions = rankData?.positions || player.positions || []
  const value = rankData?.score

  if (inTrade) {
    return (
      <div className="flex items-center gap-2 px-3 py-2 rounded-lg border border-navy-700 bg-navy-800/50 opacity-50">
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium text-white truncate">{player.name}</div>
          <div className="text-xs text-slate-500 flex items-center gap-2">
            {positions.length > 0 && <span>{positions.join('/')}</span>}
            {value != null && <span className="text-field-500 font-mono">{value.toFixed(1)}</span>}
          </div>
        </div>
        <button
          type="button"
          onClick={e => { e.stopPropagation(); onRemove(player.player_id) }}
          className="shrink-0 flex items-center gap-1 text-xs text-slate-500 hover:text-stitch-400 transition-colors px-2 py-1 rounded border border-navy-600 hover:border-stitch-700"
        >
          <X size={11} /> Remove
        </button>
      </div>
    )
  }

  return (
    <button
      type="button"
      onClick={() => onAdd(player)}
      className={`w-full flex items-center gap-2 px-3 py-2 rounded-lg border border-navy-700 bg-navy-800/30 transition-colors text-left ${hoverBorder}`}
    >
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-white truncate">{player.name}</div>
        <div className="text-xs text-slate-500 flex items-center gap-2">
          {positions.length > 0 && <span>{positions.join('/')}</span>}
          {value != null && <span className="text-field-500 font-mono">{value.toFixed(1)}</span>}
        </div>
      </div>
      <ChevronRight size={14} className="shrink-0 text-slate-600" />
    </button>
  )
}

// ── TeamRosterPanel ───────────────────────────────────────────────────────────

function TeamRosterPanel({ team, side, tradedPlayerIds, onAddPlayer, onRemovePlayer, picks, onPicksChange, rankingsMap, pickValueFn }) {
  const roster = team?.roster || []

  return (
    <div className="space-y-2">
      <div className="space-y-1 max-h-80 overflow-y-auto pr-1">
        {roster.map(player => (
          <PlayerCard
            key={player.player_id}
            player={player}
            rankData={rankingsMap?.[player.player_id]}
            inTrade={tradedPlayerIds.has(player.player_id)}
            side={side}
            onAdd={onAddPlayer}
            onRemove={onRemovePlayer}
          />
        ))}
        {roster.length === 0 && (
          <div className="text-xs text-slate-600 italic px-2">No roster players found.</div>
        )}
      </div>
      <DraftPickSelector selected={picks} onChange={onPicksChange} side={side} pickValueFn={pickValueFn} />
    </div>
  )
}

// ── TeamPickerPanel ───────────────────────────────────────────────────────────
// Generic team search + accordion for both the giving and receiving sides.
// `excludeTeamId` filters out whichever team is already loaded on the other side.

function TeamPickerPanel({ league, excludeTeamId, side, onLoadTeam, onAddPlayer, tradedPlayerIds, picks, onPicksChange, rankingsMap, pickValueFn }) {
  const [query, setQuery] = useState('')
  const [searchResults, setSearchResults] = useState([])
  const [searchOpen, setSearchOpen] = useState(false)
  const [expandedTeamId, setExpandedTeamId] = useState(null)
  const searchTimeoutRef = useRef(null)
  const searchContainerRef = useRef(null)

  const otherTeams = (league?.teams || []).filter(t => t.team_id !== excludeTeamId)

  // Build ownedByMap: player_id → team
  const ownedByMap = {}
  for (const team of (league?.teams || [])) {
    for (const player of (team.roster || [])) {
      ownedByMap[player.player_id] = team
    }
  }

  function handleSearchInput(e) {
    const q = e.target.value
    setQuery(q)
    if (searchTimeoutRef.current) clearTimeout(searchTimeoutRef.current)
    if (q.length < 2) {
      setSearchResults([])
      setSearchOpen(false)
      return
    }
    searchTimeoutRef.current = setTimeout(async () => {
      try {
        const data = await searchPlayers(q, 8)
        const list = Array.isArray(data) ? data : (data?.players || [])
        // Annotate with ownership and rankData
        const annotated = list.map(p => ({
          ...p,
          ownedByTeam: ownedByMap[p.player_id] || null,
          rankData: rankingsMap?.[p.player_id] || null,
        }))
        setSearchResults(annotated)
        setSearchOpen(annotated.length > 0)
      } catch {
        setSearchResults([])
        setSearchOpen(false)
      }
    }, 280)
  }

  function handleSearchSelect(player) {
    setSearchOpen(false)
    setQuery('')
    setSearchResults([])
    if (player.ownedByTeam) {
      onLoadTeam(player.ownedByTeam, player)
    } else {
      onAddPlayer(player)
    }
  }

  // Close dropdown on outside click
  useEffect(() => {
    function handleClick(e) {
      if (searchContainerRef.current && !searchContainerRef.current.contains(e.target)) {
        setSearchOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  function toggleTeamExpand(teamId) {
    setExpandedTeamId(prev => prev === teamId ? null : teamId)
  }

  return (
    <div className="space-y-4">
      {/* Search bar */}
      <div ref={searchContainerRef} className="relative">
        <div className="relative">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500 pointer-events-none" />
          <input
            type="text"
            className="field-input pl-8 w-full"
            placeholder="Search player to find their team…"
            value={query}
            onChange={handleSearchInput}
            onFocus={() => searchResults.length > 0 && setSearchOpen(true)}
            autoComplete="off"
          />
        </div>
        {searchOpen && searchResults.length > 0 && (
          <ul className="absolute z-50 mt-1 w-full bg-navy-800 border border-navy-600 rounded-xl shadow-2xl overflow-hidden max-h-60 overflow-y-auto">
            {searchResults.map(player => (
              <li key={player.player_id}>
                <button
                  type="button"
                  onMouseDown={e => e.preventDefault()}
                  onClick={() => handleSearchSelect(player)}
                  className="w-full text-left px-3 py-2.5 hover:bg-navy-700 flex items-center gap-3 transition-colors"
                >
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium text-white truncate">{player.name}</div>
                    <div className="text-xs text-slate-500">
                      {player.team}
                      {player.positions?.length > 0 && ` · ${player.positions.join('/')}`}
                      {player.rankData?.score != null && <span className="ml-1 text-field-500 font-mono">{player.rankData.score.toFixed(1)}</span>}
                    </div>
                  </div>
                  {player.ownedByTeam ? (
                    <span className="text-xs text-field-400 shrink-0">{player.ownedByTeam.team_name}</span>
                  ) : (
                    <span className="text-xs text-slate-600 shrink-0">Free Agent</span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Team accordion */}
      <div className="space-y-1">
        <div className="section-label">Or select a team</div>
        {otherTeams.map(team => {
          const isExpanded = expandedTeamId === team.team_id
          return (
            <div key={team.team_id} className="rounded-lg border border-navy-700 overflow-hidden">
              <div
                className="flex items-center gap-2 px-3 py-2 bg-navy-800/50 cursor-pointer hover:bg-navy-800 transition-colors"
                onClick={() => toggleTeamExpand(team.team_id)}
              >
                <div className="flex-1 min-w-0 text-sm font-medium text-white truncate">{team.team_name}</div>
                <button
                  type="button"
                  onClick={e => { e.stopPropagation(); onLoadTeam(team, null) }}
                  className={`shrink-0 text-xs border px-2 py-0.5 rounded transition-colors ${
                    side === 'give'
                      ? 'text-stitch-400 hover:text-stitch-300 border-stitch-700 hover:border-stitch-500'
                      : 'text-field-400 hover:text-field-300 border-field-700 hover:border-field-500'
                  }`}
                >
                  Load team
                </button>
                {isExpanded ? (
                  <ChevronDown size={14} className="shrink-0 text-slate-500" />
                ) : (
                  <ChevronRight size={14} className="shrink-0 text-slate-500" />
                )}
              </div>
              {isExpanded && (
                <div className="p-2 space-y-1 bg-navy-900/30">
                  {(team.roster || []).map(player => (
                    <PlayerCard
                      key={player.player_id}
                      player={player}
                      rankData={rankingsMap?.[player.player_id]}
                      inTrade={tradedPlayerIds.has(player.player_id)}
                      side={side}
                      onAdd={p => onLoadTeam(team, p)}
                      onRemove={() => {}}
                    />
                  ))}
                  {(team.roster || []).length === 0 && (
                    <div className="text-xs text-slate-600 italic px-2 py-1">No roster players.</div>
                  )}
                  <div className="pt-2">
                    <DraftPickSelector selected={picks} onChange={onPicksChange} side={side} pickValueFn={pickValueFn} />
                  </div>
                </div>
              )}
            </div>
          )
        })}
        {otherTeams.length === 0 && (
          <div className="text-xs text-slate-600 italic px-2">No other teams in league.</div>
        )}
      </div>
    </div>
  )
}

// ── ReplaceTeamModal ──────────────────────────────────────────────────────────

function ReplaceTeamModal({ currentTeam, newTeam, onConfirm, onCancel }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm">
      <div className="bg-navy-900 border border-navy-700 rounded-2xl shadow-2xl p-6 max-w-sm w-full space-y-4">
        <div className="text-sm text-white leading-relaxed">
          Replace <strong className="text-stitch-300">{currentTeam?.team_name}</strong> with{' '}
          <strong className="text-field-300">{newTeam?.team_name}</strong>? This will clear players selected from the current team.
        </div>
        <div className="flex gap-3">
          <button type="button" onClick={onConfirm} className="btn-primary flex-1">
            Replace
          </button>
          <button
            type="button"
            onClick={onCancel}
            className="flex-1 px-4 py-2 rounded-lg border border-navy-600 bg-navy-800 text-slate-300 hover:bg-navy-700 text-sm font-medium transition-colors"
          >
            Keep Current
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export default function EvaluateTrade() {
  const { league, myTeam } = useLeague() || {}
  const [team1, setTeam1] = useState(null)   // giving side — defaults to myTeam
  const [team2, setTeam2] = useState(null)   // receiving side
  const [replaceModal, setReplaceModal] = useState(null)

  // Seed team1 from myTeam on first load
  useEffect(() => {
    if (myTeam && !team1) setTeam1(myTeam)
  }, [myTeam]) // eslint-disable-line react-hooks/exhaustive-deps
  const [givingIds, setGivingIds] = useState(new Set())
  const [receivingIds, setReceivingIds] = useState(new Set())
  const [givingPicks, setGivingPicks] = useState([])
  const [receivingPicks, setReceivingPicks] = useState([])
  const [manualGiving, setManualGiving] = useState([])
  const [manualReceiving, setManualReceiving] = useState([])
  const [context, setContext] = useState('')
  const [horizon, setHorizon] = useState('season')
  const [leagueSettings, setLeagueSettings] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [result, setResult] = useState(null)
  const [mobileTab, setMobileTab] = useState('team1')
  const touchStartX = useRef(null)

  // Rankings state for live value data
  const [rankingsList, setRankingsList] = useState([])
  useEffect(() => {
    getRankings({ limit: 500, ranking_type: 'predictive' })
      .then(data => setRankingsList(data?.rankings || (Array.isArray(data) ? data : [])))
      .catch(() => {})
  }, [])

  const rankingsMap = useMemo(() => {
    const m = {}
    rankingsList.forEach(p => { m[p.player_id] = p })
    return m
  }, [rankingsList])

  // Derived values
  const leagueInitialValues = league ? {
    categories: league.scoring_categories,
    leagueType: league.league_type,
    numTeams: league.num_teams,
    rosterPositions: league.roster_positions,
  } : null

  const numTeams = league?.num_teams || 12
  const keepersPerTeam = league?.keepers_per_team || 0

  function pickValue(round) {
    // For keeper leagues: keepers fill the first N rounds of the draft.
    // The "1st round pick" is actually the (keepersPerTeam + 1)th round.
    // Average pick rank in tradeable round R = (keepersPerTeam + R - 0.5) * numTeams
    const avgRank = Math.round((keepersPerTeam + round - 0.5) * numTeams)
    return rankingsList[avgRank - 1]?.score || 0
  }

  const givingValue = (() => {
    let total = 0
    givingIds.forEach(id => { total += rankingsMap[id]?.score || 0 })
    manualGiving.filter(p => p.playerId).forEach(p => { total += rankingsMap[p.playerId]?.score || 0 })
    givingPicks.forEach(r => { total += pickValue(r) })
    return total
  })()

  const receivingValue = (() => {
    let total = 0
    receivingIds.forEach(id => { total += rankingsMap[id]?.score || 0 })
    manualReceiving.filter(p => p.playerId).forEach(p => { total += rankingsMap[p.playerId]?.score || 0 })
    receivingPicks.forEach(r => { total += pickValue(r) })
    return total
  })()

  const valueDiff = receivingValue - givingValue
  const hasValueData = rankingsList.length > 0

  const givingPlayers = [
    ...Array.from(givingIds).map(id => {
      const p = team1?.roster?.find(r => r.player_id === id)
      const rd = rankingsMap[id]
      return p ? { playerId: id, name: p.name, positions: rd?.positions || [] } : null
    }).filter(Boolean),
    ...manualGiving.filter(p => p.playerId).map(p => {
      const rd = rankingsMap[p.playerId]
      return { playerId: p.playerId, name: p.name, positions: rd?.positions || [] }
    }),
  ]

  const receivingPlayers = [
    ...Array.from(receivingIds).map(id => {
      const p = team2?.roster?.find(r => r.player_id === id)
      const rd = rankingsMap[id]
      return p ? { playerId: id, name: p.name, positions: rd?.positions || [] } : null
    }).filter(Boolean),
    ...manualReceiving.filter(p => p.playerId).map(p => {
      const rd = rankingsMap[p.playerId]
      return { playerId: p.playerId, name: p.name, positions: rd?.positions || [] }
    }),
  ]

  // Player add/remove helpers
  function addGivingPlayer(player) {
    setGivingIds(prev => new Set([...prev, player.player_id]))
  }
  function removeGivingPlayer(playerId) {
    setGivingIds(prev => { const n = new Set(prev); n.delete(playerId); return n })
  }
  function addReceivingPlayer(player) {
    setReceivingIds(prev => new Set([...prev, player.player_id]))
  }
  function removeReceivingPlayer(playerId) {
    setReceivingIds(prev => { const n = new Set(prev); n.delete(playerId); return n })
  }

  function loadTeam1(team, playerToAdd) {
    if (team1 && team1.team_id !== team.team_id) {
      setReplaceModal({ side: 'give', newTeam: team, pendingPlayer: playerToAdd })
      return
    }
    setTeam1(team)
    if (playerToAdd) addGivingPlayer(playerToAdd)
  }

  function loadTeam2(team, playerToAdd) {
    if (team2 && team2.team_id !== team.team_id) {
      setReplaceModal({ side: 'receive', newTeam: team, pendingPlayer: playerToAdd })
      return
    }
    setTeam2(team)
    if (playerToAdd) addReceivingPlayer(playerToAdd)
  }

  function confirmReplaceTeam() {
    const { side, newTeam, pendingPlayer } = replaceModal
    if (side === 'give') {
      setTeam1(newTeam)
      setGivingIds(new Set())
      if (pendingPlayer) setTimeout(() => addGivingPlayer(pendingPlayer), 0)
    } else {
      setTeam2(newTeam)
      setReceivingIds(new Set())
      if (pendingPlayer) setTimeout(() => addReceivingPlayer(pendingPlayer), 0)
    }
    setReplaceModal(null)
  }

  async function evaluate() {
    if (givingPicks.length !== receivingPicks.length && (givingPicks.length > 0 || receivingPicks.length > 0)) {
      setError("Trade is incompatible with your league settings — number of draft picks must match.")
      return
    }
    const allGivingIds = [...givingIds, ...manualGiving.filter(p => p.playerId).map(p => p.playerId)]
    const allReceivingIds = [...receivingIds, ...manualReceiving.filter(p => p.playerId).map(p => p.playerId)]
    if (allGivingIds.length + givingPicks.length === 0) {
      setError("Add at least one player or pick to \"You're Giving\".")
      return
    }
    if (allReceivingIds.length + receivingPicks.length === 0) {
      setError("Add at least one player or pick to \"You're Receiving\".")
      return
    }
    setLoading(true); setError(null); setResult(null)
    try {
      const body = {
        giving:    { player_ids: allGivingIds,    draft_picks: givingPicks.map(pickToString)    },
        receiving: { player_ids: allReceivingIds, draft_picks: receivingPicks.map(pickToString) },
        context: context || null,
        horizon,
      }
      const rosterIds = (team1?.roster || []).map(p => p.player_id).filter(Boolean)
      if (rosterIds.length > 0) body.roster_player_ids = rosterIds
      if (leagueSettings) {
        body.custom_categories  = leagueSettings.categories
        body.custom_league_type = leagueSettings.leagueType
      }
      const res = await evaluateTrade(body)
      setResult(res)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  // Mobile swipe handlers
  function handleTouchStart(e) { touchStartX.current = e.touches[0].clientX }
  function handleTouchEnd(e) {
    if (touchStartX.current === null) return
    const dx = e.changedTouches[0].clientX - touchStartX.current
    if (dx < -50) setMobileTab('team2')
    if (dx > 50) setMobileTab('team1')
    touchStartX.current = null
  }

  // ── Shared panel content builders ────────────────────────────────────────────

  const givingSideContent = team1 ? (
    <TeamRosterPanel
      team={team1}
      side="give"
      tradedPlayerIds={givingIds}
      onAddPlayer={addGivingPlayer}
      onRemovePlayer={removeGivingPlayer}
      picks={givingPicks}
      onPicksChange={setGivingPicks}
      rankingsMap={rankingsMap}
      pickValueFn={pickValue}
    />
  ) : league ? (
    <TeamPickerPanel
      league={league}
      excludeTeamId={team2?.team_id}
      side="give"
      onLoadTeam={loadTeam1}
      onAddPlayer={addGivingPlayer}
      tradedPlayerIds={givingIds}
      picks={givingPicks}
      onPicksChange={setGivingPicks}
      rankingsMap={rankingsMap}
      pickValueFn={pickValue}
    />
  ) : (
    <div className="space-y-2">
      <p className="text-xs text-slate-500">Connect Yahoo Fantasy to auto-load your roster, or enter players manually:</p>
      {manualGiving.map((p, i) => (
        <div key={i} className="flex items-center gap-2">
          <PlayerSearch
            value={p.name}
            playerId={p.playerId}
            onChange={(name, playerId) =>
              setManualGiving(prev => prev.map((x, j) => j === i ? { name, playerId } : x))
            }
            onEnterKey={() => setManualGiving(prev => [...prev, { name: '', playerId: null }])}
            placeholder={`Player ${i + 1}…`}
            className="flex-1"
          />
          <button
            type="button"
            onClick={() => setManualGiving(prev => prev.filter((_, j) => j !== i))}
            className="shrink-0 text-slate-600 hover:text-stitch-400 transition-colors p-1"
          >
            <X size={15} />
          </button>
        </div>
      ))}
      <button
        type="button"
        onClick={() => setManualGiving(prev => [...prev, { name: '', playerId: null }])}
        className="flex items-center gap-1.5 text-xs text-stitch-500 hover:text-stitch-300 transition-colors"
      >
        <Plus size={13} /> Add player
      </button>
      <DraftPickSelector selected={givingPicks} onChange={setGivingPicks} side="give" pickValueFn={pickValue} />
    </div>
  )

  const receivingSideContent = team2 ? (
    <TeamRosterPanel
      team={team2}
      side="receive"
      tradedPlayerIds={receivingIds}
      onAddPlayer={addReceivingPlayer}
      onRemovePlayer={removeReceivingPlayer}
      picks={receivingPicks}
      onPicksChange={setReceivingPicks}
      rankingsMap={rankingsMap}
      pickValueFn={pickValue}
    />
  ) : league ? (
    <TeamPickerPanel
      league={league}
      excludeTeamId={team1?.team_id}
      side="receive"
      onLoadTeam={loadTeam2}
      onAddPlayer={addReceivingPlayer}
      tradedPlayerIds={receivingIds}
      picks={receivingPicks}
      onPicksChange={setReceivingPicks}
      rankingsMap={rankingsMap}
      pickValueFn={pickValue}
    />
  ) : (
    <div className="space-y-2">
      {manualReceiving.map((p, i) => (
        <div key={i} className="flex items-center gap-2">
          <PlayerSearch
            value={p.name}
            playerId={p.playerId}
            onChange={(name, playerId) =>
              setManualReceiving(prev => prev.map((x, j) => j === i ? { name, playerId } : x))
            }
            onEnterKey={() => setManualReceiving(prev => [...prev, { name: '', playerId: null }])}
            placeholder={`Player ${i + 1}…`}
            className="flex-1"
          />
          <button
            type="button"
            onClick={() => setManualReceiving(prev => prev.filter((_, j) => j !== i))}
            className="shrink-0 text-slate-600 hover:text-field-400 transition-colors p-1"
          >
            <X size={15} />
          </button>
        </div>
      ))}
      <button
        type="button"
        onClick={() => setManualReceiving(prev => [...prev, { name: '', playerId: null }])}
        className="flex items-center gap-1.5 text-xs text-field-500 hover:text-field-300 transition-colors"
      >
        <Plus size={13} /> Add player
      </button>
      <DraftPickSelector selected={receivingPicks} onChange={setReceivingPicks} side="receive" pickValueFn={pickValue} />
    </div>
  )

  return (
    <div className="space-y-0">
      {/* Page header */}
      <div className="pb-4">
        <div className="flex items-center gap-2 mb-1">
          <ArrowLeftRight size={18} className="text-field-400" />
          <h1 className="text-2xl font-bold text-white">Evaluate Trade</h1>
        </div>
        <p className="text-slate-500 text-sm">Select players from each team, then evaluate.</p>
      </div>

      {/* Sticky trade summary bar */}
      <TradeSummaryBar
        givingPlayers={givingPlayers}
        givingPicks={givingPicks}
        receivingPlayers={receivingPlayers}
        receivingPicks={receivingPicks}
        givingValue={givingValue}
        receivingValue={receivingValue}
        valueDiff={valueDiff}
        hasValueData={hasValueData}
        onRemoveGivingPlayer={removeGivingPlayer}
        onRemoveGivingPick={r => setGivingPicks(prev => prev.filter(x => x !== r))}
        onRemoveReceivingPlayer={removeReceivingPlayer}
        onRemoveReceivingPick={r => setReceivingPicks(prev => prev.filter(x => x !== r))}
        pickValue={pickValue}
        onEvaluate={evaluate}
        loading={loading}
      />

      {/* Error + Loading + Results — above the panels so they're immediately visible */}
      <ErrorBanner message={error} onClose={() => setError(null)} />
      {loading && <div className="mt-6"><LoadingState message="Evaluating trade…" /></div>}
      {result && (
        <div className="space-y-5 mt-6">
          <VerdictBadge verdict={result.verdict} confidence={result.confidence} />

          {/* Value comparison */}
          <div className="card grid grid-cols-3 gap-4 text-center">
            <div>
              <div className="section-label">Giving value</div>
              <div className="text-2xl font-bold font-mono text-stitch-400">
                {result.give_value.toFixed(2)}
              </div>
            </div>
            <div>
              <div className="section-label">Differential</div>
              <div className={`text-2xl font-bold font-mono ${result.value_differential >= 0 ? 'text-field-400' : 'text-stitch-400'}`}>
                {result.value_differential >= 0 ? '+' : ''}{result.value_differential.toFixed(2)}
              </div>
              <div className="text-xs text-slate-600 mt-1">density-adjusted</div>
            </div>
            <div>
              <div className="section-label">Receiving value</div>
              <div className="text-2xl font-bold font-mono text-field-400">
                {result.receive_value.toFixed(2)}
              </div>
            </div>
          </div>

          {result.talent_density_note && (
            <div className="text-xs text-slate-500 italic px-1">{result.talent_density_note}</div>
          )}

          <div className="card">
            <div className="section-label">Category impact after trade</div>
            <PercentileBar data={result.category_impact} />
          </div>

          <ProsCons pros={result.pros} cons={result.cons} />
          <Blurb text={result.analysis_blurb} />
        </div>
      )}

      {/* DESKTOP layout */}
      <div className="hidden md:grid grid-cols-2 gap-4 mt-8">
        {/* Left: giving side */}
        <div className="bg-navy-900 border border-navy-700 rounded-xl overflow-hidden flex flex-col">
          <div className="px-4 py-3 border-b border-navy-700/60 bg-navy-800/40 flex items-center justify-between">
            <span className="text-xs font-semibold text-stitch-400 uppercase tracking-wider">
              You're Giving{team1 ? ` · ${team1.team_name}` : ''}
            </span>
            {team1 && (
              <button
                type="button"
                onClick={() => { setTeam1(null); setGivingIds(new Set()) }}
                className="text-xs text-slate-600 hover:text-stitch-400 flex items-center gap-1 transition-colors"
              >
                <X size={11} /> Change team
              </button>
            )}
          </div>
          <div className="p-4 flex-1">
            {givingSideContent}
          </div>
        </div>

        {/* Right: receiving side */}
        <div className="bg-navy-900 border border-navy-700 rounded-xl overflow-hidden flex flex-col">
          <div className="px-4 py-3 border-b border-navy-700/60 bg-navy-800/40 flex items-center justify-between">
            <span className="text-xs font-semibold text-field-400 uppercase tracking-wider">
              You're Receiving{team2 ? ` · ${team2.team_name}` : ''}
            </span>
            {team2 && (
              <button
                type="button"
                onClick={() => { setTeam2(null); setReceivingIds(new Set()) }}
                className="text-xs text-slate-600 hover:text-field-400 flex items-center gap-1 transition-colors"
              >
                <X size={11} /> Change team
              </button>
            )}
          </div>
          <div className="p-4 flex-1">
            {receivingSideContent}
          </div>
        </div>
      </div>

      {/* MOBILE layout */}
      <div className="md:hidden mt-8">
        {/* Tab switcher */}
        <div className="flex gap-2 mb-3">
          <button
            type="button"
            onClick={() => setMobileTab('team1')}
            className={`flex-1 py-2 rounded-lg text-xs font-medium border transition-colors ${
              mobileTab === 'team1'
                ? 'bg-stitch-900/50 border-stitch-700 text-stitch-300'
                : 'bg-navy-800 border-navy-700 text-slate-500'
            }`}
          >
            {team1?.team_name || 'Select Team'}
          </button>
          <button
            type="button"
            onClick={() => setMobileTab('team2')}
            className={`flex-1 py-2 rounded-lg text-xs font-medium border transition-colors ${
              mobileTab === 'team2'
                ? 'bg-field-900/50 border-field-700 text-field-300'
                : 'bg-navy-800 border-navy-700 text-slate-500'
            }`}
          >
            {team2?.team_name || 'Select Team'}
          </button>
        </div>

        {/* Swipeable container */}
        <div
          className="overflow-hidden relative"
          onTouchStart={handleTouchStart}
          onTouchEnd={handleTouchEnd}
        >
          <div
            className="flex transition-transform duration-300 ease-in-out"
            style={{
              width: 'calc(200% + 2rem)',
              transform: mobileTab === 'team1'
                ? 'translateX(0)'
                : 'translateX(calc(-50% - 1rem + 2.5rem))',
              gap: '1rem',
            }}
          >
            {/* Team 1 panel */}
            <div
              style={{ width: 'calc(50% - 0.5rem - 1.25rem)', flexShrink: 0 }}
              className="space-y-3"
            >
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-stitch-400 uppercase tracking-wider">
                  Giving{team1 ? ` · ${team1.team_name}` : ''}
                </span>
                {team1 && (
                  <button
                    type="button"
                    onClick={() => { setTeam1(null); setGivingIds(new Set()) }}
                    className="text-xs text-slate-600 hover:text-stitch-400 flex items-center gap-1 transition-colors"
                  >
                    <X size={11} /> Change team
                  </button>
                )}
              </div>
              {givingSideContent}
            </div>

            {/* Team 2 panel */}
            <div
              style={{ width: 'calc(50% - 0.5rem - 1.25rem)', flexShrink: 0 }}
              className="space-y-3"
            >
              <div className="flex items-center justify-between">
                <span className="text-xs font-semibold text-field-400 uppercase tracking-wider">
                  Receiving{team2 ? ` · ${team2.team_name}` : ''}
                </span>
                {team2 && (
                  <button
                    type="button"
                    onClick={() => { setTeam2(null); setReceivingIds(new Set()) }}
                    className="text-xs text-slate-600 hover:text-field-400 flex items-center gap-1 transition-colors"
                  >
                    <X size={11} /> Change team
                  </button>
                )}
              </div>
              {receivingSideContent}
            </div>
          </div>
        </div>
      </div>

      {/* Settings */}
      <div className="mt-6 space-y-4">
        <div>
          <label className="section-label">Evaluate over</label>
          <div className="flex gap-2 mt-1">
            {[
              { value: 'season', label: 'Full Season' },
              { value: 'month',  label: 'This Month'  },
              { value: 'week',   label: 'This Week'   },
            ].map(({ value, label }) => (
              <button
                key={value}
                type="button"
                onClick={() => setHorizon(value)}
                className={`px-3 py-1.5 rounded-md text-xs font-medium border transition-colors ${
                  horizon === value
                    ? 'bg-field-700 border-field-600 text-white'
                    : 'bg-navy-800 border-navy-600 text-slate-500 hover:text-slate-300 hover:border-navy-500'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          <p className="text-xs text-slate-600 mt-1">
            Use "This Month" or "This Week" for deadline/stretch-run trades.
          </p>
        </div>

        <ContextInput value={context} onChange={setContext} />
        <LeagueSettings onChange={setLeagueSettings} initialValues={leagueInitialValues} />
      </div>

      {/* Replace team modal */}
      {replaceModal && (
        <ReplaceTeamModal
          currentTeam={replaceModal.side === 'give' ? team1 : team2}
          newTeam={replaceModal.newTeam}
          onConfirm={confirmReplaceTeam}
          onCancel={() => setReplaceModal(null)}
        />
      )}
    </div>
  )
}
