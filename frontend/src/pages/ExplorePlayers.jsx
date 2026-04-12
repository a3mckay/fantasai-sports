import { useState, useEffect, useRef, useCallback } from 'react'
import { useSearchParams, Link } from 'react-router-dom'
import {
  BarChart2, X, Search, Send, RefreshCw, Trash2,
  TrendingUp, ChevronDown, ChevronUp, Loader2,
} from 'lucide-react'
import { explorePlayerContext, exploreChatStream, searchPlayers, getPlayer } from '../lib/api'
import { useLeague } from '../contexts/LeagueContext'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'

// MLB headshot URL — same pattern used in grade cards
const headshotUrl = (mlbamId) =>
  mlbamId
    ? `https://img.mlbstatic.com/mlb-photos/image/upload/d_people:generic:headshot:67:current.png/w_120,q_auto:best/v1/people/${mlbamId}/headshot/67/current`
    : null

const MAX_PLAYERS = 5

// ---------------------------------------------------------------------------
// Stat formatting helpers
// ---------------------------------------------------------------------------
const BATTING_DISPLAY = [
  { key: 'PA', label: 'PA' }, { key: 'R', label: 'R' }, { key: 'HR', label: 'HR' },
  { key: 'RBI', label: 'RBI' }, { key: 'SB', label: 'SB' },
  { key: 'AVG', label: 'AVG', decimals: 3 }, { key: 'OPS', label: 'OPS', decimals: 3 },
  { key: 'xwOBA', label: 'xwOBA', decimals: 3 }, { key: 'Barrel%', label: 'Brl%', decimals: 1 },
  { key: 'wRC+', label: 'wRC+' },
]
const PITCHING_DISPLAY = [
  { key: 'IP', label: 'IP', decimals: 1 }, { key: 'W', label: 'W' },
  { key: 'SV', label: 'SV' }, { key: 'SO', label: 'K' },
  { key: 'ERA', label: 'ERA', decimals: 2 }, { key: 'WHIP', label: 'WHIP', decimals: 2 },
  { key: 'xERA', label: 'xERA', decimals: 2 }, { key: 'K/9', label: 'K/9', decimals: 1 },
  { key: 'Stuff+', label: 'Stf+' },
]

function fmtStat(val, decimals) {
  if (val == null || val === '') return '—'
  if (decimals != null) return Number(val).toFixed(decimals)
  return val
}

// ---------------------------------------------------------------------------
// StatRow — compact key/value display
// ---------------------------------------------------------------------------
function StatRow({ stats, defs }) {
  const present = defs.filter(d => stats?.[d.key] != null)
  if (!present.length) return <p className="text-xs text-slate-600">No data available</p>
  return (
    <div className="grid grid-cols-3 gap-x-3 gap-y-1">
      {present.map(d => (
        <div key={d.key} className="flex items-baseline justify-between gap-1">
          <span className="text-[10px] text-slate-500 shrink-0">{d.label}</span>
          <span className="text-xs font-mono text-slate-200">{fmtStat(stats[d.key], d.decimals)}</span>
        </div>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// PAV bar
// ---------------------------------------------------------------------------
function PavBar({ label, value }) {
  const pct = Math.min(100, Math.max(0, value))
  return (
    <div className="space-y-0.5">
      <div className="flex justify-between text-[10px]">
        <span className="text-slate-400">{label}</span>
        <span className="font-mono text-field-400">{pct.toFixed(1)}</span>
      </div>
      <div className="h-1 rounded-full bg-navy-700">
        <div className="h-1 rounded-full bg-field-500" style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// PlayerCard — Zone B stat card
// ---------------------------------------------------------------------------
function PlayerCard({ context, onRemove }) {
  const [showProjection, setShowProjection] = useState(false)
  const [showPav, setShowPav] = useState(false)
  const defs = context.stat_type === 'pitching' ? PITCHING_DISPLAY : BATTING_DISPLAY
  const hs = headshotUrl(context.mlbam_id)

  return (
    <div className="bg-navy-800 border border-navy-700 rounded-xl p-4 relative flex flex-col gap-3">
      {/* Remove button */}
      <button
        onClick={onRemove}
        className="absolute top-3 right-3 text-slate-600 hover:text-stitch-400 transition-colors"
        aria-label={`Remove ${context.name}`}
      >
        <X size={14} />
      </button>

      {/* Header row */}
      <div className="flex items-center gap-3 pr-5">
        {hs ? (
          <img
            src={hs}
            alt={context.name}
            className="w-12 h-12 rounded-full object-cover bg-navy-700 shrink-0"
            onError={e => { e.target.style.display = 'none' }}
          />
        ) : (
          <div className="w-12 h-12 rounded-full bg-navy-700 flex items-center justify-center shrink-0">
            <span className="text-slate-500 text-sm font-bold">
              {context.positions[0] || '?'}
            </span>
          </div>
        )}
        <div className="min-w-0">
          <p className="text-white font-semibold text-sm truncate">{context.name}</p>
          <p className="text-slate-400 text-xs">{context.team} · {context.positions.join(', ')}</p>
          {context.overall_rank != null ? (
            <p className="text-[10px] text-field-400 mt-0.5">
              #{context.overall_rank} · {context.rank_list_name}
            </p>
          ) : (
            <p className="text-[10px] text-slate-600 mt-0.5">Unranked</p>
          )}
        </div>
      </div>

      {/* Ownership + injury badges */}
      <div className="flex flex-wrap items-center gap-1.5">
        {context.owned_by ? (
          <span className="text-[10px] font-semibold text-emerald-300 bg-emerald-950/50 border border-emerald-800/50 rounded px-1.5 py-0.5">
            {context.owned_by}
          </span>
        ) : (
          <span className="text-[10px] font-semibold text-slate-400 bg-navy-700 border border-navy-600 rounded px-1.5 py-0.5">
            Available
          </span>
        )}
        {context.injury?.status && (
          <span className="text-[10px] font-semibold text-stitch-300 bg-stitch-950/40 border border-stitch-800/50 rounded px-1.5 py-0.5 uppercase">
            {context.injury.status.replace(/_/g, '-')}
          </span>
        )}
        {!context.injury?.status && context.injury?.risk_flag && (
          <span className="text-[10px] font-semibold text-amber-400 bg-amber-950/30 border border-amber-800/40 rounded px-1.5 py-0.5">
            {context.injury.risk_flag === 'fragile' ? 'Fragile' : 'Post-Surgery Risk'}
          </span>
        )}
      </div>

      {/* Injury details */}
      {context.injury && (context.injury.description || context.injury.expected_return) && (
        <div className="text-[10px] text-slate-500 space-y-0.5">
          {context.injury.description && <p>{context.injury.description}</p>}
          {context.injury.expected_return && (
            <p className="text-slate-600">ETA: {context.injury.expected_return}</p>
          )}
        </div>
      )}

      {/* This week schedule */}
      {context.schedule && (
        <div>
          <p className="text-[10px] font-semibold text-slate-500 uppercase tracking-wide mb-1">
            This Week
          </p>
          <div className="space-y-0.5">
            <p className="text-xs text-slate-400">
              {context.schedule.games_this_week} game{context.schedule.games_this_week !== 1 ? 's' : ''}
              {context.schedule.probable_starts > 0 && (
                <> · <span className="text-slate-300">{context.schedule.future_starts} start{context.schedule.future_starts !== 1 ? 's' : ''} remaining</span></>
              )}
            </p>
            {context.schedule.today_opponent && (
              <p className="text-xs text-slate-400">
                <span className="text-slate-300">Today:</span>{' '}
                {context.schedule.today_is_home ? 'vs' : '@'} {context.schedule.today_opponent}
                {context.schedule.today_sp_name && (
                  <span className="text-slate-500"> — {context.schedule.today_sp_name}{context.schedule.today_sp_throws ? ` (${context.schedule.today_sp_throws})` : ''}</span>
                )}
              </p>
            )}
            {context.schedule.week_context_text && (
              <p className="text-[10px] text-slate-600 leading-relaxed">{context.schedule.week_context_text}</p>
            )}
          </div>
        </div>
      )}

      {/* 2026 Actuals */}
      <div>
        <p className="text-[10px] font-semibold text-slate-500 uppercase tracking-wide mb-1.5">
          2026 Actuals
        </p>
        <StatRow stats={context.actual_stats} defs={defs} />
      </div>

      {/* Steamer projections (collapsible) */}
      {context.projection_stats && (
        <div>
          <button
            onClick={() => setShowProjection(v => !v)}
            className="flex items-center gap-1 text-[10px] text-slate-500 hover:text-slate-300 transition-colors"
          >
            {showProjection ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
            <span className="font-semibold uppercase tracking-wide">Steamer Projections</span>
          </button>
          {showProjection && (
            <div className="mt-1.5">
              <StatRow stats={context.projection_stats} defs={defs} />
            </div>
          )}
        </div>
      )}

      {/* PAV — prospects only */}
      {context.is_prospect && context.pav_score != null && (
        <div>
          <button
            onClick={() => setShowPav(v => !v)}
            className="flex items-center gap-1 text-[10px] text-field-500 hover:text-field-300 transition-colors"
          >
            {showPav ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
            <span className="font-semibold uppercase tracking-wide">
              PAV Score: {context.pav_score}/100
            </span>
          </button>
          {showPav && context.pav_components && (
            <div className="mt-2 space-y-2">
              <PavBar label="Prospect Grade" value={context.pav_components.prospect_grade} />
              <PavBar label="Age-Adj. Performance" value={context.pav_components.age_adj_performance} />
              <PavBar label="Vertical Velocity" value={context.pav_components.vertical_velocity} />
              <PavBar label="ETA Proximity" value={context.pav_components.eta_proximity} />
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// ChatMessage display
// ---------------------------------------------------------------------------
function renderMarkdown(text) {
  const parts = text.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/g)
  return parts.map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**'))
      return <strong key={i} className="text-slate-200 font-semibold">{part.slice(2, -2)}</strong>
    if (part.startsWith('*') && part.endsWith('*'))
      return <em key={i}>{part.slice(1, -1)}</em>
    return part
  })
}

function ChatBubble({ msg }) {
  const isUser = msg.role === 'user'
  const isDivider = msg.role === 'divider'

  if (isDivider) {
    return (
      <div className="flex items-center gap-2 py-2">
        <div className="flex-1 border-t border-navy-700" />
        <span className="text-[10px] text-slate-600 whitespace-nowrap">{msg.content}</span>
        <div className="flex-1 border-t border-navy-700" />
      </div>
    )
  }

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[85%] rounded-xl px-3.5 py-2.5 text-sm leading-relaxed ${
          isUser
            ? 'bg-field-800 border border-field-700 text-slate-200'
            : 'bg-navy-800 border border-navy-700 text-slate-300'
        }`}
      >
        {isUser ? (
          <p>{msg.content}</p>
        ) : (
          <>
            <div className="whitespace-pre-wrap">{renderMarkdown(msg.content)}</div>
            {!msg.streaming && (
              <p className="text-[9px] text-slate-600 mt-1.5 border-t border-navy-700 pt-1">
                Based on FanGraphs + PAV data · FantasAI engine
              </p>
            )}
          </>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Suggested prompts — context-aware, generated from player stat data
// ---------------------------------------------------------------------------
function generatePrompts(players, contexts) {
  if (!players.length) return []
  const first = players[0]
  const ctx = contexts?.[first.player_id]
  const prompts = []

  if (ctx) {
    const isBatter = ctx.stat_type === 'batting'
    const pa = Number(ctx.actual_stats?.PA || 0)
    const avg = Number(ctx.actual_stats?.AVG || 0)
    const era = Number(ctx.actual_stats?.ERA || 0)
    const xera = Number(ctx.actual_stats?.xERA || 0)
    const kp9 = Number(ctx.actual_stats?.['K/9'] || 0)
    const isAvailable = !ctx.owned_by
    const hasToday = !!ctx.schedule?.today_opponent
    const isInjured = !!ctx.injury?.status

    if (isInjured) {
      prompts.push(`Is ${first.name} worth holding through his injury?`)
      prompts.push(`What should I expect from ${first.name} when he comes back?`)
    } else if (ctx.is_prospect) {
      prompts.push(`How close is ${first.name} to the majors?`)
      prompts.push(`What's ${first.name}'s realistic ceiling?`)
    } else if (isBatter) {
      if (hasToday) {
        const opp = ctx.schedule.today_opponent
        prompts.push(`Is ${first.name} a good start today vs. ${opp}?`)
      } else if (pa > 0 && pa < 80) {
        prompts.push(`Is ${first.name}'s early-season performance sustainable?`)
      } else if (avg > 0.335 && pa >= 80) {
        prompts.push(`What's the regression risk on ${first.name}'s average?`)
      } else {
        prompts.push(`Make the bull case for ${first.name}`)
      }
      prompts.push(isAvailable ? `Should I add ${first.name} off waivers?` : `What are the risks of rostering ${first.name}?`)
    } else {
      // Pitcher
      if (hasToday && ctx.schedule.future_starts > 0) {
        const opp = ctx.schedule.today_opponent
        prompts.push(`Is ${first.name} a good start today vs. ${opp}?`)
      } else if (era > 4.5 && xera > 0 && xera < era - 0.75) {
        prompts.push(`Is ${first.name}'s ERA about to improve?`)
      } else if (kp9 >= 10) {
        prompts.push(`What's ${first.name}'s strikeout upside?`)
      } else {
        prompts.push(`Make the bull case for ${first.name}`)
      }
      prompts.push(isAvailable ? `Should I stream ${first.name} this week?` : `What are the risks of rostering ${first.name}?`)
    }
  } else {
    // Context not loaded yet — generic fallbacks
    prompts.push(`Tell me about ${first.name}`)
    prompts.push(`Make the bull case for ${first.name}`)
    prompts.push(`What are the risks of rostering ${first.name}?`)
  }

  if (players.length >= 2) {
    prompts.push(`Compare ${players[0].name} and ${players[1].name} for the rest of the season`)
  }

  return prompts.slice(0, 4)
}

function SuggestedPrompts({ players, contexts, onSelect }) {
  if (!players.length) return null
  const prompts = generatePrompts(players, contexts)
  return (
    <div className="flex flex-wrap gap-2 justify-center">
      {prompts.map(p => (
        <button
          key={p}
          onClick={() => onSelect(p)}
          className="text-xs text-slate-400 bg-navy-800 border border-navy-700 rounded-full px-3 py-1.5 hover:border-field-600 hover:text-slate-200 transition-colors"
        >
          {p}
        </button>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------
export default function ExplorePlayers() {
  const [searchParams] = useSearchParams()
  const { league } = useLeague() || {}

  const leagueId = league?.league_id ?? null

  // ---- Zone A: player selection ----
  const [selectedPlayers, setSelectedPlayers] = useState([])  // [{player_id, name, ...}]
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState([])
  const [searchLoading, setSearchLoading] = useState(false)
  const [maxNudge, setMaxNudge] = useState(false)
  const searchRef = useRef(null)
  const searchDebounce = useRef(null)

  // ---- Zone B: stat contexts ----
  const [contexts, setContexts] = useState({})   // player_id → PlayerContextResponse
  const [contextLoading, setContextLoading] = useState({})  // player_id → bool
  const [contextError, setContextError] = useState({})      // player_id → string

  // ---- Zone C: chat ----
  const [chatMessages, setChatMessages] = useState([])       // displayed thread
  const [chatHistory, setChatHistory] = useState([])         // API history (last 5 pairs)
  const [inputText, setInputText] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  const [chatError, setChatError] = useState(null)
  const chatBottomRef = useRef(null)
  const inputRef = useRef(null)

  // ---- Load player from URL ?players= param on mount ----
  // Use the lightweight /players/{id} endpoint so the chip appears immediately,
  // independent of the heavier context endpoint. The selectedPlayers effect then
  // triggers the context load in the background.
  useEffect(() => {
    const param = searchParams.get('players')
    if (!param) return
    const id = parseInt(param, 10)
    if (isNaN(id)) return
    getPlayer(id)
      .then(p => {
        setSelectedPlayers([{ player_id: p.player_id, name: p.name, team: p.team, positions: p.positions || [] }])
      })
      .catch(err => console.warn('Could not load player from URL param:', err))
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps

  // ---- Fetch context for newly added players ----
  useEffect(() => {
    const missing = selectedPlayers.filter(p => !contexts[p.player_id] && !contextLoading[p.player_id])
    if (!missing.length) return
    setContextLoading(prev => {
      const next = { ...prev }
      missing.forEach(p => { next[p.player_id] = true })
      return next
    })
    missing.forEach(async p => {
      try {
        const ctx = await explorePlayerContext(p.player_id, leagueId)
        setContexts(prev => ({ ...prev, [p.player_id]: ctx }))
        setContextError(prev => { const n = { ...prev }; delete n[p.player_id]; return n })
      } catch (err) {
        setContextError(prev => ({ ...prev, [p.player_id]: err.message }))
      } finally {
        setContextLoading(prev => { const n = { ...prev }; delete n[p.player_id]; return n })
      }
    })
  }, [selectedPlayers])  // eslint-disable-line react-hooks/exhaustive-deps

  // ---- Typeahead search ----
  useEffect(() => {
    clearTimeout(searchDebounce.current)
    if (!searchQuery.trim()) { setSearchResults([]); return }
    searchDebounce.current = setTimeout(async () => {
      setSearchLoading(true)
      try {
        const results = await searchPlayers(searchQuery.trim(), 8)
        const selectedIds = new Set(selectedPlayers.map(p => p.player_id))
        setSearchResults((results || []).filter(r => !selectedIds.has(r.player_id)))
      } catch { setSearchResults([]) }
      finally { setSearchLoading(false) }
    }, 300)
    return () => clearTimeout(searchDebounce.current)
  }, [searchQuery])  // eslint-disable-line react-hooks/exhaustive-deps

  // ---- Auto-scroll chat ----
  useEffect(() => {
    chatBottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [chatMessages])

  // ---- Player add/remove handlers ----
  function addPlayer(player) {
    if (selectedPlayers.length >= MAX_PLAYERS) { setMaxNudge(true); setTimeout(() => setMaxNudge(false), 3000); return }
    if (selectedPlayers.some(p => p.player_id === player.player_id)) return
    setSelectedPlayers(prev => [...prev, player])
    setSearchQuery('')
    setSearchResults([])
    // Insert divider if chat has messages
    if (chatMessages.length > 0) {
      setChatMessages(prev => [...prev, { role: 'divider', content: `— ${player.name} added · context updated —`, id: Date.now() }])
      // Reset history so next question uses fresh context
      setChatHistory([])
    }
  }

  function removePlayer(playerId) {
    const removed = selectedPlayers.find(p => p.player_id === playerId)
    setSelectedPlayers(prev => prev.filter(p => p.player_id !== playerId))
    setContexts(prev => { const n = { ...prev }; delete n[playerId]; return n })
    if (removed && chatMessages.length > 0) {
      setChatMessages(prev => [...prev, { role: 'divider', content: `— ${removed.name} removed · context updated —`, id: Date.now() }])
      setChatHistory([])
    }
  }

  function clearChat() {
    setChatMessages([])
    setChatHistory([])
    setChatError(null)
  }

  // ---- Chat send ----
  async function sendMessage(text) {
    const msg = (text ?? inputText).trim()
    if (!msg || isStreaming || !selectedPlayers.length) return
    setInputText('')
    setChatError(null)

    const userMsg = { role: 'user', content: msg, id: Date.now() }
    const assistantMsg = { role: 'assistant', content: '', streaming: true, id: Date.now() + 1 }
    setChatMessages(prev => [...prev, userMsg, assistantMsg])
    setIsStreaming(true)

    try {
      const stream = await exploreChatStream({
        playerIds: selectedPlayers.map(p => p.player_id),
        messages: chatHistory,
        userMessage: msg,
        leagueId,
      })

      const reader = stream.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let fullResponse = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          const raw = line.slice(6).trim()
          if (!raw) continue
          let event
          try { event = JSON.parse(raw) } catch { continue }
          if (event.type === 'text') {
            fullResponse += event.text
            setChatMessages(prev => {
              const updated = [...prev]
              const last = updated[updated.length - 1]
              if (last?.role === 'assistant') {
                updated[updated.length - 1] = { ...last, content: fullResponse }
              }
              return updated
            })
          } else if (event.type === 'error') {
            setChatError(event.message)
          }
        }
      }

      // Finalize: mark not streaming, update history
      setChatMessages(prev => {
        const updated = [...prev]
        const last = updated[updated.length - 1]
        if (last?.role === 'assistant') {
          updated[updated.length - 1] = { ...last, streaming: false }
        }
        return updated
      })

      // Keep last 5 turns (10 messages) in history
      setChatHistory(prev => {
        const next = [
          ...prev,
          { role: 'user', content: msg },
          { role: 'assistant', content: fullResponse },
        ]
        return next.slice(-10)
      })
    } catch (err) {
      setChatError(err.message)
      setChatMessages(prev => prev.filter(m => m.id !== assistantMsg.id))
    } finally {
      setIsStreaming(false)
      inputRef.current?.focus()
    }
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage() }
  }

  const hasPlayers = selectedPlayers.length > 0
  const orderedContexts = selectedPlayers.map(p => contexts[p.player_id]).filter(Boolean)

  return (
    <div className="space-y-5">

      {/* ── Header ── */}
      <div>
        <div className="flex items-center gap-2 mb-1">
          <BarChart2 size={18} className="text-field-400" />
          <h1 className="text-2xl font-bold text-white">Explore Players</h1>
        </div>
        <p className="text-slate-500 text-sm">
          Select up to 5 players, review their stats, and ask our AI analyst anything.
        </p>
      </div>

      {/* ── Zone A: Player Selector ── */}
      <div className="space-y-3">
        {/* Selected chips */}
        {selectedPlayers.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {selectedPlayers.map(p => (
              <div
                key={p.player_id}
                className="flex items-center gap-1.5 bg-navy-800 border border-navy-700 rounded-full pl-3 pr-2 py-1"
              >
                <span className="text-sm text-slate-200">{p.name}</span>
                <button
                  onClick={() => removePlayer(p.player_id)}
                  className="text-slate-500 hover:text-stitch-400 transition-colors"
                  aria-label={`Remove ${p.name}`}
                >
                  <X size={13} />
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Search input */}
        <div className="relative max-w-md">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500 pointer-events-none" />
          <input
            ref={searchRef}
            type="text"
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            placeholder={selectedPlayers.length < MAX_PLAYERS ? 'Search and add a player…' : 'Maximum 5 players selected'}
            disabled={selectedPlayers.length >= MAX_PLAYERS}
            className="w-full bg-navy-800 border border-navy-700 rounded-lg text-sm text-white placeholder-slate-600 pl-9 pr-3 py-2 focus:outline-none focus:border-field-600 transition-colors disabled:opacity-50"
          />
          {searchLoading && (
            <Loader2 size={13} className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-500 animate-spin" />
          )}

          {/* Dropdown results */}
          {searchResults.length > 0 && (
            <div className="absolute top-full left-0 right-0 mt-1 z-50 bg-navy-800 border border-navy-700 rounded-lg shadow-xl overflow-hidden">
              {searchResults.map(r => (
                <button
                  key={r.player_id}
                  onClick={() => addPlayer(r)}
                  className="w-full text-left px-3 py-2 text-sm hover:bg-navy-700 transition-colors flex items-center justify-between gap-2"
                >
                  <span className="text-slate-200">{r.name}</span>
                  <span className="text-xs text-slate-500 shrink-0">{r.team} · {(r.positions || []).join(', ')}</span>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Max nudge */}
        {maxNudge && (
          <p className="text-xs text-amber-400">You can explore up to 5 players at a time.</p>
        )}
      </div>

      {/* ── Zone B: Stats Panel ── */}
      {hasPlayers && (
        <div className={`grid gap-4 ${
          orderedContexts.length === 1 ? 'grid-cols-1 max-w-sm' :
          orderedContexts.length === 2 ? 'grid-cols-1 sm:grid-cols-2' :
          orderedContexts.length === 3 ? 'grid-cols-1 sm:grid-cols-3' :
          'grid-cols-1 sm:grid-cols-2 lg:grid-cols-4'
        }`}>
          {selectedPlayers.map(p => {
            const ctx = contexts[p.player_id]
            const loading = contextLoading[p.player_id]
            const err = contextError[p.player_id]
            if (loading) return (
              <div key={p.player_id} className="bg-navy-800 border border-navy-700 rounded-xl p-4 flex items-center justify-center min-h-[200px]">
                <Loader2 size={18} className="animate-spin text-slate-500" />
              </div>
            )
            if (err) return (
              <div key={p.player_id} className="bg-navy-800 border border-navy-700 rounded-xl p-4 text-center space-y-1">
                <p className="text-xs text-stitch-400">Could not load stats for {p.name}</p>
                <p className="text-[10px] text-slate-600">{contextError[p.player_id]}</p>
              </div>
            )
            if (!ctx) return null
            return (
              <PlayerCard
                key={p.player_id}
                context={ctx}
                onRemove={() => removePlayer(p.player_id)}
              />
            )
          })}
        </div>
      )}

      {/* ── Zone C: Analyst Chat ── */}
      {hasPlayers && (
        <div className="bg-navy-900 border border-navy-700 rounded-xl overflow-hidden">

          {/* Chat header */}
          <div className="flex items-center justify-between px-4 py-2.5 border-b border-navy-700">
            <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">AI Analyst</p>
            {chatMessages.length > 0 && (
              <button
                onClick={clearChat}
                className="flex items-center gap-1 text-xs text-slate-500 hover:text-slate-200 transition-colors"
              >
                <Trash2 size={12} />
                Clear chat
              </button>
            )}
          </div>

          {/* Messages */}
          <div className="min-h-[200px] max-h-[480px] overflow-y-auto p-4 space-y-3">
            {chatMessages.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-8 gap-4">
                <p className="text-sm text-slate-500">Ask the analyst anything about {selectedPlayers.map(p => p.name).join(', ')}.</p>
                <SuggestedPrompts players={selectedPlayers} contexts={contexts} onSelect={t => { setInputText(t); inputRef.current?.focus() }} />
              </div>
            ) : (
              chatMessages.map(msg => (
                <ChatBubble key={msg.id ?? `${msg.role}-${msg.content.slice(0, 10)}`} msg={msg} />
              ))
            )}
            {isStreaming && chatMessages[chatMessages.length - 1]?.content === '' && (
              <div className="flex justify-start">
                <div className="bg-navy-800 border border-navy-700 rounded-xl px-4 py-3">
                  <div className="flex gap-1">
                    <span className="w-1.5 h-1.5 rounded-full bg-slate-500 animate-bounce" style={{ animationDelay: '0ms' }} />
                    <span className="w-1.5 h-1.5 rounded-full bg-slate-500 animate-bounce" style={{ animationDelay: '150ms' }} />
                    <span className="w-1.5 h-1.5 rounded-full bg-slate-500 animate-bounce" style={{ animationDelay: '300ms' }} />
                  </div>
                </div>
              </div>
            )}
            <div ref={chatBottomRef} />
          </div>

          <ErrorBanner message={chatError} onClose={() => setChatError(null)} />

          {/* Input bar */}
          <div className="border-t border-navy-700 px-3 py-2.5 flex items-end gap-2">
            <textarea
              ref={inputRef}
              value={inputText}
              onChange={e => setInputText(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask the analyst…"
              rows={1}
              disabled={isStreaming}
              className="flex-1 bg-transparent text-sm text-white placeholder-slate-600 resize-none focus:outline-none py-1 max-h-32 disabled:opacity-50"
              style={{ fieldSizing: 'content' }}
            />
            <button
              onClick={() => sendMessage()}
              disabled={!inputText.trim() || isStreaming}
              className="p-2 rounded-lg bg-field-700 text-white hover:bg-field-600 disabled:opacity-40 disabled:cursor-not-allowed transition-colors shrink-0"
              aria-label="Send"
            >
              {isStreaming ? <Loader2 size={15} className="animate-spin" /> : <Send size={15} />}
            </button>
          </div>
        </div>
      )}

      {/* Empty state — no players selected */}
      {!hasPlayers && (
        <div className="text-center py-16 text-slate-600">
          <BarChart2 size={36} className="mx-auto mb-3 opacity-30" />
          <p className="text-sm">Search for a player above to get started.</p>
          <p className="text-xs mt-1">
            Or browse the <Link to="/rankings" className="text-field-500 hover:text-field-300">Rankings</Link> and click any player name to explore them here.
          </p>
        </div>
      )}

      {/* ── Methodology Note ── */}
      <details className="mt-8 border-t border-navy-800 pt-4 group">
        <summary className="text-[10px] text-slate-700 cursor-pointer hover:text-slate-500 select-none flex items-center gap-1.5 w-fit">
          <span className="group-open:rotate-90 transition-transform inline-block">▸</span>
          How rankings &amp; analysis work
        </summary>
        <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-3 text-[10px] text-slate-600 max-w-3xl">
          <div>
            <p className="text-slate-500 font-semibold uppercase tracking-wide mb-1">Fantasy Categories</p>
            <p>Batting: R, HR, RBI, SB, AVG, OPS</p>
            <p>Pitching: IP, W, SV, K, ERA, WHIP</p>
          </div>
          <div>
            <p className="text-slate-500 font-semibold uppercase tracking-wide mb-1">Ranking Formula</p>
            <p>Z-scores across all categories, summed into a composite. ERA/WHIP/BB/9 inverted (lower is better). Capped at ±3.5σ to prevent single-category dominance.</p>
          </div>
          <div>
            <p className="text-slate-500 font-semibold uppercase tracking-wide mb-1">Horizon Weights (talent / actuals)</p>
            <p>This Week: 35% / 65%</p>
            <p>This Month: 60% / 40%</p>
            <p>Rest of Season: 85% / 15%</p>
          </div>
          <div>
            <p className="text-slate-500 font-semibold uppercase tracking-wide mb-1">Advanced Stats Used</p>
            <p>Batting: xwOBA, xBA, xSLG, Barrel%, HardHit%, EV, wRC+, BB%, K%, SwStr%, LD%, Spd</p>
            <p className="mt-1">Pitching: xERA, xFIP, SIERA, Stuff+, CSW%, K%, BB%, GB%, HR/9</p>
          </div>
          <div>
            <p className="text-slate-500 font-semibold uppercase tracking-wide mb-1">Statcast Overlay</p>
            <p>Process metrics (xwOBA, xERA) ramp from 0% weight at 0 PA/IP to a max of 35% at 150+ PA / 30+ IP. Below those thresholds, Steamer projections carry more weight.</p>
          </div>
          <div>
            <p className="text-slate-500 font-semibold uppercase tracking-wide mb-1">Data Sources</p>
            <p>Actuals &amp; advanced stats: FanGraphs (via pybaseball)</p>
            <p>Projections: Steamer rest-of-season</p>
            <p>Statcast: Baseball Savant aggregates</p>
          </div>
        </div>
      </details>
    </div>
  )
}
