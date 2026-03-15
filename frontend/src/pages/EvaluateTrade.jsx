import { useState } from 'react'
import { ArrowLeftRight, Play, TrendingUp, TrendingDown, Minus } from 'lucide-react'
import { evaluateTrade } from '../lib/api'
import { LoadingState } from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'
import ContextInput from '../components/ContextInput'
import Blurb from '../components/Blurb'
import ProsCons from '../components/ProsCons'
import CategoryBar from '../components/CategoryBar'

function parseIds(raw) {
  return raw.split(/[\s,]+/).map(s => parseInt(s.trim())).filter(n => !isNaN(n))
}

function parsePicks(raw) {
  return raw.split('\n').map(s => s.trim()).filter(Boolean)
}

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

function TradeSide({ title, color }) {
  return (
    <div className={`text-xs font-semibold uppercase tracking-widest mb-2 ${color}`}>{title}</div>
  )
}

export default function EvaluateTrade() {
  const [teamId,    setTeamId]    = useState('')
  const [givingIds, setGivingIds] = useState('')
  const [givingPicks, setGivingPicks] = useState('')
  const [receivingIds, setReceivingIds] = useState('')
  const [receivingPicks, setReceivingPicks] = useState('')
  const [context, setContext]     = useState('')
  const [loading, setLoading]     = useState(false)
  const [error, setError]         = useState(null)
  const [result, setResult]       = useState(null)

  async function submit(e) {
    e.preventDefault()
    if (!teamId) { setError('Team ID is required.'); return }
    setLoading(true); setError(null); setResult(null)
    try {
      const res = await evaluateTrade({
        team_id: parseInt(teamId),
        giving: {
          player_ids: parseIds(givingIds),
          draft_picks: parsePicks(givingPicks),
        },
        receiving: {
          player_ids: parseIds(receivingIds),
          draft_picks: parsePicks(receivingPicks),
        },
        context: context || null,
      })
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

      <form onSubmit={submit} className="card space-y-5">
        <div>
          <label className="section-label">Your Team ID *</label>
          <input
            className="field-input font-mono w-40"
            placeholder="e.g. 3"
            value={teamId}
            onChange={e => setTeamId(e.target.value)}
          />
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
          {/* Giving */}
          <div className="space-y-3">
            <TradeSide title="You're Giving" color="text-stitch-400" />
            <div>
              <label className="section-label">Player IDs</label>
              <input
                className="field-input font-mono"
                placeholder="19755, 20123"
                value={givingIds}
                onChange={e => setGivingIds(e.target.value)}
              />
            </div>
            <div>
              <label className="section-label">Draft picks (one per line)</label>
              <textarea
                className="field-input resize-none"
                rows={2}
                placeholder="2025 1st round"
                value={givingPicks}
                onChange={e => setGivingPicks(e.target.value)}
              />
            </div>
          </div>

          {/* Receiving */}
          <div className="space-y-3">
            <TradeSide title="You're Receiving" color="text-field-400" />
            <div>
              <label className="section-label">Player IDs</label>
              <input
                className="field-input font-mono"
                placeholder="25764, 16285"
                value={receivingIds}
                onChange={e => setReceivingIds(e.target.value)}
              />
            </div>
            <div>
              <label className="section-label">Draft picks (one per line)</label>
              <textarea
                className="field-input resize-none"
                rows={2}
                placeholder="2025 2nd round pick"
                value={receivingPicks}
                onChange={e => setReceivingPicks(e.target.value)}
              />
            </div>
          </div>
        </div>

        <ContextInput value={context} onChange={setContext} />

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

          {/* Category impact */}
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
