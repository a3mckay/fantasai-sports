import { useState, useEffect, useCallback, useRef } from 'react'
import { Share2, RefreshCw } from 'lucide-react'
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

function TransactionCard({ txn, onShareCard }) {
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

        {txn.grade_letter && (
          <button
            onClick={() => onShareCard(txn.share_token)}
            title="Share this grade card"
            className="p-2 rounded-lg text-slate-500 hover:text-field-400 hover:bg-navy-700 transition-colors shrink-0"
          >
            <Share2 size={15} />
          </button>
        )}
      </div>

      {/* Rationale */}
      {txn.grade_rationale && (
        <p className="text-sm text-slate-400 border-t border-navy-700 pt-3 leading-relaxed">
          {txn.grade_rationale}
        </p>
      )}

      {/* Lookback grade */}
      {txn.lookback_grade_letter && (
        <div className="mt-3 pt-3 border-t border-navy-700">
          <div className="flex items-center gap-2 mb-1.5">
            <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Lookback</span>
            <span
              className="text-xs font-bold px-1.5 py-0.5 rounded"
              style={{
                color: GRADE_COLOR[txn.lookback_grade_letter] || '#94a3b8',
                backgroundColor: `${GRADE_COLOR[txn.lookback_grade_letter] || '#94a3b8'}22`,
                border: `1px solid ${GRADE_COLOR[txn.lookback_grade_letter] || '#94a3b8'}55`,
              }}
            >
              {txn.lookback_grade_letter}
            </span>
          </div>
          {txn.lookback_grade_rationale && (
            <p className="text-sm text-slate-400 italic leading-relaxed">
              {txn.lookback_grade_rationale}
            </p>
          )}
          {txn.lookback_graded_at && (
            <div className="text-[11px] text-slate-600 mt-1">
              Reviewed {new Date(txn.lookback_graded_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}
            </div>
          )}
        </div>
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

// How long to wait for backfill+grading before reloading (ms)
const BACKFILL_RELOAD_DELAY = 35000

export default function Transactions() {
  const [txns, setTxns]       = useState([])
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)  // background backfill in progress
  const [error, setError]     = useState(null)
  const [filter, setFilter]   = useState('')
  const [offset, setOffset]   = useState(0)
  const [hasMore, setHasMore] = useState(true)
  const didAutoSync           = useRef(false)   // prevent double-firing in StrictMode

  const LIMIT = 20

  const load = useCallback(async (resetOffset = false) => {
    setLoading(true)
    setError(null)
    const o = resetOffset ? 0 : offset
    try {
      const params = new URLSearchParams({ limit: LIMIT, offset: o })
      if (filter) params.set('transaction_type', filter)
      const data = await req('GET', `/api/v1/transactions?${params}`)
      if (resetOffset) {
        setTxns(data)
        setOffset(LIMIT)
      } else {
        setTxns(prev => [...prev, ...data])
        setOffset(o + LIMIT)
      }
      setHasMore(data.length === LIMIT)
      return data.length
    } catch (e) {
      setError(e.message || 'Failed to load transactions')
      return 0
    } finally {
      setLoading(false)
    }
  }, [filter, offset])

  // On first load: if the feed is empty, automatically trigger a backfill
  // so historical moves appear without the user needing to do anything.
  useEffect(() => {
    const init = async () => {
      const count = await load(true)
      if (count === 0 && !didAutoSync.current) {
        didAutoSync.current = true
        setSyncing(true)
        try {
          await req('POST', '/api/v1/transactions/backfill?count=200')
          // Grading ~200 moves takes ~30s; reload once done
          setTimeout(async () => {
            await load(true)
            setSyncing(false)
          }, BACKFILL_RELOAD_DELAY)
        } catch {
          setSyncing(false)
        }
      }
    }
    init()
  }, []) // eslint-disable-line

  // Reload when filter changes (after initial mount)
  useEffect(() => { load(true) }, [filter]) // eslint-disable-line

  const handlePoll = async () => {
    setSyncing(true)
    try {
      await req('POST', '/api/v1/transactions/poll')
      setTimeout(async () => {
        await load(true)
        setSyncing(false)
      }, 3000)
    } catch {
      setSyncing(false)
    }
  }

  const handleShareCard = useCallback(async (shareToken) => {
    // Build the public share URL — this serves the card without auth
    const shareUrl = `${window.location.origin}/api/v1/transactions/share/${shareToken}`
    try {
      // Try native Web Share API first (works great on mobile)
      if (navigator.share) {
        await navigator.share({
          title: 'FantasAI Move Grade',
          url: shareUrl,
        })
        return
      }
      // Fall back to copying the link to clipboard
      await navigator.clipboard.writeText(shareUrl)
      // Brief visual feedback — we'll use a simple alert for now
      // (the user can see the URL was copied via the system clipboard)
    } catch {
      // Last resort: open in new tab so user can at least see the card
      window.open(shareUrl, '_blank')
    }
  }, [])

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
          disabled={syncing}
          className="flex items-center gap-2 px-3 py-2 rounded-lg text-sm bg-navy-800 border border-navy-600 text-slate-300 hover:text-white hover:border-navy-500 transition-colors disabled:opacity-50"
        >
          <RefreshCw size={14} className={syncing ? 'animate-spin' : ''} />
          {syncing ? 'Syncing…' : 'Refresh Now'}
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
      {(loading && txns.length === 0) || syncing ? (
        <div className="flex flex-col items-center justify-center py-16 gap-3">
          <Spinner />
          {syncing && txns.length === 0 && (
            <p className="text-sm text-slate-500">Loading move history from Yahoo — this takes about 30 seconds on first visit…</p>
          )}
        </div>
      ) : txns.length === 0 ? (
        <div className="text-center py-16 text-slate-500">
          <p>No moves found yet.</p>
          <p className="text-sm mt-1">New moves are checked automatically every 20 minutes.</p>
        </div>
      ) : (
        <div className="space-y-4">
          {txns.map(t => (
            <TransactionCard key={t.id} txn={t} onShareCard={handleShareCard} />
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
