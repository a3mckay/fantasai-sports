import { useState, useEffect, useCallback } from 'react'
import { Download, RefreshCw, Filter } from 'lucide-react'
import { req } from '../lib/api'
import Spinner from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'

const GRADE_COLOR = {
  'A+': '#22c55e', 'A': '#22c55e', 'A-': '#4ade80',
  'B+': '#86efac', 'B': '#86efac', 'B-': '#bef264',
  'C+': '#fde047', 'C': '#fde047', 'C-': '#fbbf24',
  'D+': '#f97316', 'D': '#f97316', 'D-': '#ef4444',
  'F':  '#dc2626',
}

const TYPE_LABELS = { add: 'Add', drop: 'Drop', trade: 'Trade' }
const TYPE_COLORS = {
  add:   'bg-field-900 text-field-300 border border-field-700',
  drop:  'bg-red-900/40 text-red-300 border border-red-800',
  trade: 'bg-amber-900/40 text-amber-300 border border-amber-800',
}

function GradeCircle({ letter }) {
  const color = GRADE_COLOR[letter] || '#94a3b8'
  return (
    <div
      className="w-14 h-14 rounded-full flex items-center justify-center text-xl font-extrabold border-2 shrink-0"
      style={{ borderColor: color, color, backgroundColor: `${color}18` }}
    >
      {letter ?? '—'}
    </div>
  )
}

function ParticipantLine({ p, type }) {
  if (type === 'trade') {
    const gained = p.players_added?.map(x => x.player_name).join(', ') || '—'
    const lost   = p.players_dropped?.map(x => x.player_name).join(', ') || '—'
    return (
      <div className="text-sm text-slate-300">
        <span className="text-white font-medium">{p.team_name}</span>
        {' '}gets <span className="text-field-400">{gained}</span>
        {lost !== '—' && <>, gives <span className="text-red-400">{lost}</span></>}
      </div>
    )
  }
  const verb = p.action === 'add' ? 'adds' : 'drops'
  const nameColor = p.action === 'add' ? 'text-field-400' : 'text-red-400'
  return (
    <div className="text-sm text-slate-300">
      <span className="text-white font-medium">{p.team_name}</span>
      {' '}{verb} <span className={nameColor}>{p.player_name}</span>
    </div>
  )
}

function TransactionCard({ txn, onDownloadCard }) {
  const ts = txn.yahoo_timestamp
    ? new Date(txn.yahoo_timestamp).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
    : txn.graded_at
      ? new Date(txn.graded_at).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
      : null

  return (
    <div className="bg-navy-900 border border-navy-700 rounded-xl p-5 space-y-4">
      {/* Header row */}
      <div className="flex items-start gap-4">
        <GradeCircle letter={txn.grade_letter} />
        <div className="flex-1 min-w-0 space-y-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${TYPE_COLORS[txn.transaction_type] || ''}`}>
              {TYPE_LABELS[txn.transaction_type] || txn.transaction_type}
            </span>
            {ts && <span className="text-xs text-slate-500">{ts}</span>}
          </div>
          <div className="space-y-0.5">
            {(txn.participants || []).map((p, i) => (
              <ParticipantLine key={i} p={p} type={txn.transaction_type} />
            ))}
          </div>
        </div>

        {txn.has_card && (
          <button
            onClick={() => onDownloadCard(txn.id)}
            title="Download grade card"
            className="p-2 rounded-lg text-slate-500 hover:text-slate-200 hover:bg-navy-700 transition-colors shrink-0"
          >
            <Download size={15} />
          </button>
        )}
      </div>

      {/* Rationale */}
      {txn.grade_rationale && (
        <p className="text-sm text-slate-400 border-t border-navy-700 pt-3 leading-relaxed">
          {txn.grade_rationale}
        </p>
      )}
    </div>
  )
}

const FILTERS = [
  { value: '',      label: 'All' },
  { value: 'add',   label: 'Adds' },
  { value: 'drop',  label: 'Drops' },
  { value: 'trade', label: 'Trades' },
]

export default function Transactions() {
  const [txns, setTxns]         = useState([])
  const [loading, setLoading]   = useState(true)
  const [error, setError]       = useState(null)
  const [filter, setFilter]     = useState('')
  const [polling, setPolling]   = useState(false)
  const [offset, setOffset]     = useState(0)
  const [hasMore, setHasMore]   = useState(true)

  const LIMIT = 20

  const load = useCallback(async (resetOffset = false) => {
    setLoading(true)
    setError(null)
    const o = resetOffset ? 0 : offset
    try {
      const params = new URLSearchParams({ limit: LIMIT, offset: o })
      if (filter) params.set('transaction_type', filter)
      const data = await req('GET',`/api/v1/transactions?${params}`)
      if (resetOffset) {
        setTxns(data)
        setOffset(LIMIT)
      } else {
        setTxns(prev => [...prev, ...data])
        setOffset(o + LIMIT)
      }
      setHasMore(data.length === LIMIT)
    } catch (e) {
      setError(e.message || 'Failed to load transactions')
    } finally {
      setLoading(false)
    }
  }, [filter, offset])

  useEffect(() => { load(true) }, [filter]) // eslint-disable-line

  const handlePoll = async () => {
    setPolling(true)
    try {
      await req('POST', '/api/v1/transactions/poll')
      // Wait a moment then reload
      setTimeout(() => { load(true); setPolling(false) }, 3000)
    } catch {
      setPolling(false)
    }
  }

  const handleDownloadCard = async (id) => {
    try {
      const res = await fetch(`/api/v1/transactions/${id}/card`, {
        headers: { Authorization: `Bearer ${localStorage.getItem('token')}` }
      })
      if (!res.ok) return
      const blob = await res.blob()
      const url  = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `grade_card_${id}.png`
      a.click()
      URL.revokeObjectURL(url)
    } catch { /* ignore */ }
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white">Move Grades</h1>
          <p className="text-sm text-slate-400 mt-0.5">AI-graded adds, drops &amp; trades from your league</p>
        </div>
        <button
          onClick={handlePoll}
          disabled={polling}
          className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm bg-navy-800 border border-navy-600 text-slate-300 hover:text-white hover:border-navy-500 transition-colors disabled:opacity-50"
        >
          <RefreshCw size={14} className={polling ? 'animate-spin' : ''} />
          {polling ? 'Polling…' : 'Refresh Now'}
        </button>
      </div>

      {/* Filter tabs */}
      <div className="flex gap-1 bg-navy-900 p-1 rounded-lg w-fit">
        {FILTERS.map(f => (
          <button
            key={f.value}
            onClick={() => setFilter(f.value)}
            className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
              filter === f.value
                ? 'bg-navy-700 text-white shadow'
                : 'text-slate-400 hover:text-slate-200'
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      <ErrorBanner error={error} />

      {/* Feed */}
      {loading && txns.length === 0 ? (
        <div className="flex justify-center py-16"><Spinner /></div>
      ) : txns.length === 0 ? (
        <div className="text-center py-16 text-slate-500">
          <Filter size={32} className="mx-auto mb-3 opacity-40" />
          <p>No graded transactions yet.</p>
          <p className="text-sm mt-1">Moves are graded automatically every 20 minutes.</p>
        </div>
      ) : (
        <div className="space-y-4">
          {txns.map(t => (
            <TransactionCard key={t.id} txn={t} onDownloadCard={handleDownloadCard} />
          ))}

          {hasMore && (
            <button
              onClick={() => load(false)}
              disabled={loading}
              className="w-full py-3 rounded-lg border border-navy-700 text-slate-400 hover:text-white hover:border-navy-600 text-sm transition-colors disabled:opacity-50"
            >
              {loading ? 'Loading…' : 'Load more'}
            </button>
          )}
        </div>
      )}
    </div>
  )
}
