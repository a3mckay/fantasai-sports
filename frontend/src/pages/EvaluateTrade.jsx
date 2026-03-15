import { useState } from 'react'
import { ArrowLeftRight, Play, TrendingUp, TrendingDown, Minus, Plus, X } from 'lucide-react'
import { evaluateTrade } from '../lib/api'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import ContextInput from '../components/ContextInput'
import Blurb from '../components/Blurb'
import ProsCons from '../components/ProsCons'
import CategoryBar from '../components/CategoryBar'
import PlayerSearch from '../components/PlayerSearch'
import LeagueSettings from '../components/LeagueSettings'

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

// ── Player slot list component ────────────────────────────────────────────────

function PlayerSlotList({ players, onChange, side }) {
  const color = side === 'give' ? 'text-stitch-400' : 'text-field-400'
  const label = side === 'give' ? "You're Giving" : "You're Receiving"

  function addSlot() {
    if (players.length < 6) onChange([...players, { name: '', playerId: null }])
  }

  function removeSlot(idx) {
    onChange(players.filter((_, i) => i !== idx))
  }

  function updateSlot(idx, name, playerId) {
    onChange(players.map((p, i) => i === idx ? { name, playerId } : p))
  }

  return (
    <div className="space-y-3">
      <div className={`text-xs font-semibold uppercase tracking-widest ${color}`}>{label}</div>

      {players.map((p, idx) => (
        <div key={idx} className="flex items-center gap-2">
          <PlayerSearch
            value={p.name}
            playerId={p.playerId}
            onChange={(name, playerId) => updateSlot(idx, name, playerId)}
            placeholder={`Player ${idx + 1}…`}
            className="flex-1"
          />
          {players.length > 0 && (
            <button
              type="button"
              onClick={() => removeSlot(idx)}
              className="shrink-0 text-slate-600 hover:text-stitch-400 transition-colors p-1"
            >
              <X size={15} />
            </button>
          )}
        </div>
      ))}

      {players.length < 6 && (
        <button
          type="button"
          onClick={addSlot}
          className={`flex items-center gap-1.5 text-xs transition-colors ${
            side === 'give'
              ? 'text-stitch-500 hover:text-stitch-300'
              : 'text-field-500 hover:text-field-300'
          }`}
        >
          <Plus size={13} /> Add player
        </button>
      )}
    </div>
  )
}

// ── Draft picks list component ────────────────────────────────────────────────

function DraftPickList({ picks, onChange, side }) {
  const color = side === 'give' ? 'text-stitch-400' : 'text-field-400'

  function addPick() {
    onChange([...picks, ''])
  }

  function removePick(idx) {
    onChange(picks.filter((_, i) => i !== idx))
  }

  function updatePick(idx, val) {
    onChange(picks.map((p, i) => i === idx ? val : p))
  }

  return (
    <div className="space-y-2">
      <div className="section-label">Draft picks</div>
      {picks.map((pick, idx) => (
        <div key={idx} className="flex items-center gap-2">
          <input
            className="field-input flex-1 text-sm"
            placeholder="e.g. 2026 1st round"
            value={pick}
            onChange={e => updatePick(idx, e.target.value)}
          />
          <button
            type="button"
            onClick={() => removePick(idx)}
            className="shrink-0 text-slate-600 hover:text-stitch-400 transition-colors p-1"
          >
            <X size={15} />
          </button>
        </div>
      ))}
      <button
        type="button"
        onClick={addPick}
        className={`flex items-center gap-1.5 text-xs transition-colors ${
          side === 'give'
            ? 'text-stitch-500 hover:text-stitch-300'
            : 'text-field-500 hover:text-field-300'
        }`}
      >
        <Plus size={13} /> Add pick
      </button>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export default function EvaluateTrade() {
  const [givingPlayers,   setGivingPlayers]   = useState([{ name: '', playerId: null }])
  const [receivingPlayers, setReceivingPlayers] = useState([{ name: '', playerId: null }])
  const [givingPicks,     setGivingPicks]     = useState([])
  const [receivingPicks,  setReceivingPicks]  = useState([])
  const [myRosterPlayers, setMyRosterPlayers] = useState([])   // for team context
  const [context, setContext]                 = useState('')
  const [leagueSettings, setLeagueSettings]   = useState(null)
  const [loading, setLoading]                 = useState(false)
  const [error, setError]                     = useState(null)
  const [result, setResult]                   = useState(null)
  const [showMyRoster, setShowMyRoster]       = useState(false)

  async function submit(e) {
    e.preventDefault()
    const givingIds    = givingPlayers.filter(p => p.playerId).map(p => p.playerId)
    const receivingIds = receivingPlayers.filter(p => p.playerId).map(p => p.playerId)
    const filteredGivingPicks    = givingPicks.filter(Boolean)
    const filteredReceivingPicks = receivingPicks.filter(Boolean)

    if (givingIds.length + filteredGivingPicks.length === 0) {
      setError('Add at least one player or pick to the "You\'re Giving" side.')
      return
    }
    if (receivingIds.length + filteredReceivingPicks.length === 0) {
      setError('Add at least one player or pick to the "You\'re Receiving" side.')
      return
    }

    setLoading(true); setError(null); setResult(null)
    try {
      const body = {
        giving:    { player_ids: givingIds,    draft_picks: filteredGivingPicks    },
        receiving: { player_ids: receivingIds, draft_picks: filteredReceivingPicks },
        context:   context || null,
      }

      // Include roster context for better team-strength analysis
      const rosterIds = myRosterPlayers.filter(p => p.playerId).map(p => p.playerId)
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

  return (
    <div className="space-y-8">
      <div>
        <div className="flex items-center gap-2 mb-1">
          <ArrowLeftRight size={18} className="text-field-400" />
          <h1 className="text-2xl font-bold text-white">Evaluate Trade</h1>
        </div>
        <p className="text-slate-500 text-sm">
          Talent-density aware verdict — one elite player beats five average ones even if raw totals are equal.
        </p>
      </div>

      <form onSubmit={submit} className="card space-y-6">
        {/* Give / Receive side-by-side */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
          {/* Giving side */}
          <div className="space-y-4 p-4 rounded-xl bg-stitch-500/5 border border-stitch-800/40">
            <PlayerSlotList
              players={givingPlayers}
              onChange={setGivingPlayers}
              side="give"
            />
            <DraftPickList
              picks={givingPicks}
              onChange={setGivingPicks}
              side="give"
            />
          </div>

          {/* Receiving side */}
          <div className="space-y-4 p-4 rounded-xl bg-field-500/5 border border-field-800/40">
            <PlayerSlotList
              players={receivingPlayers}
              onChange={setReceivingPlayers}
              side="receive"
            />
            <DraftPickList
              picks={receivingPicks}
              onChange={setReceivingPicks}
              side="receive"
            />
          </div>
        </div>

        {/* Optional: my roster for team context */}
        <div>
          <button
            type="button"
            onClick={() => setShowMyRoster(!showMyRoster)}
            className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-300 transition-colors"
          >
            <Plus size={12} className={showMyRoster ? 'rotate-45 transition-transform' : 'transition-transform'} />
            {showMyRoster ? 'Hide' : 'Add'} my roster (improves analysis)
          </button>
          {showMyRoster && (
            <div className="mt-3 space-y-2">
              <p className="text-xs text-slate-500">
                Adding your full roster helps calibrate team strength and category needs.
              </p>
              {myRosterPlayers.map((p, idx) => (
                <div key={idx} className="flex items-center gap-2">
                  <PlayerSearch
                    value={p.name}
                    onChange={(name, playerId) =>
                      setMyRosterPlayers(prev =>
                        prev.map((r, i) => i === idx ? { name, playerId } : r)
                      )
                    }
                    placeholder={`Roster player ${idx + 1}…`}
                    className="flex-1"
                  />
                  <button
                    type="button"
                    onClick={() => setMyRosterPlayers(prev => prev.filter((_, i) => i !== idx))}
                    className="shrink-0 text-slate-600 hover:text-stitch-400 p-1"
                  >
                    <X size={15} />
                  </button>
                </div>
              ))}
              {myRosterPlayers.length < 30 && (
                <button
                  type="button"
                  onClick={() => setMyRosterPlayers(prev => [...prev, { name: '', playerId: null }])}
                  className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-300 transition-colors"
                >
                  <Plus size={13} /> Add roster player
                </button>
              )}
            </div>
          )}
        </div>

        <ContextInput value={context} onChange={setContext} />
        <LeagueSettings onChange={setLeagueSettings} />

        <button type="submit" className="btn-primary" disabled={loading}>
          <Play size={14} /> Evaluate Trade
        </button>
      </form>

      <ErrorBanner message={error} onClose={() => setError(null)} />
      {loading && <LoadingState message="Evaluating trade…" />}

      {result && (
        <div className="space-y-6">
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
            <CategoryBar data={result.category_impact} />
          </div>

          <ProsCons pros={result.pros} cons={result.cons} />
          <Blurb text={result.analysis_blurb} />
        </div>
      )}
    </div>
  )
}
