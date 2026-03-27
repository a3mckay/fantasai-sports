/**
 * TransactionTicker — site-wide banner that cycles new graded moves.
 *
 * - Polls /api/v1/transactions/unseen every 5 min
 * - Remembers the highest seen id in localStorage so it won't re-show old items
 * - Rotates through unseen items every 6 seconds
 * - Can be dismissed per item (stored in localStorage)
 */
import { useState, useEffect, useRef, useCallback } from 'react'
import { Link } from 'react-router-dom'
import { X, ChevronRight } from 'lucide-react'
import { req } from '../lib/api'
import { useAuth } from '../contexts/AuthContext'

const LS_SEEN_KEY   = 'fantasai_ticker_seen_id'
const LS_HIDDEN_KEY = 'fantasai_ticker_hidden_ids'
const ROTATE_MS     = 6000
const POLL_MS       = 5 * 60 * 1000

const GRADE_COLOR = {
  'A+': '#22c55e', 'A': '#22c55e', 'A-': '#4ade80',
  'B+': '#86efac', 'B': '#86efac', 'B-': '#bef264',
  'C+': '#fde047', 'C': '#fde047', 'C-': '#fbbf24',
  'D+': '#f97316', 'D': '#f97316', 'D-': '#ef4444',
  'F':  '#dc2626',
}

function tickerLabel(txn) {
  const p = txn.participants?.[0]
  if (!p) return txn.transaction_type
  if (txn.transaction_type === 'trade') {
    const names = (p.players_added || []).map(x => x.player_name).join(' & ')
    return `${p.team_name} acquires ${names || '?'}`
  }
  const verb = p.action === 'add' ? 'adds' : 'drops'
  return `${p.team_name} ${verb} ${p.player_name}`
}

export default function TransactionTicker() {
  const { user } = useAuth()
  const [items, setItems]     = useState([])
  const [idx, setIdx]         = useState(0)
  const rotateRef             = useRef(null)

  const getSeenId   = () => parseInt(localStorage.getItem(LS_SEEN_KEY) || '0', 10)
  const getHidden   = () => JSON.parse(localStorage.getItem(LS_HIDDEN_KEY) || '[]')

  // On first mount, if no watermark is stored yet, fetch the current max id
  // so that backfilled transactions never appear in the ticker.
  const initWatermark = useCallback(async () => {
    if (!user) return
    if (localStorage.getItem(LS_SEEN_KEY) !== null) return  // already initialized
    try {
      const { max_id } = await req('GET', '/api/v1/transactions/watermark')
      localStorage.setItem(LS_SEEN_KEY, String(max_id))
    } catch { /* silent */ }
  }, [user])

  const fetchItems = useCallback(async () => {
    if (!user) return
    try {
      const sinceId = getSeenId()
      const data = await req('GET', `/api/v1/transactions/unseen?since_id=${sinceId}`)
      const hidden = getHidden()
      const visible = data.filter(t => !hidden.includes(t.id))
      if (visible.length > 0) {
        // Update seen watermark
        const maxId = Math.max(...visible.map(t => t.id))
        if (maxId > sinceId) localStorage.setItem(LS_SEEN_KEY, String(maxId))
        setItems(visible)
        setIdx(0)
      }
    } catch { /* silent */ }
  }, [user])

  // Initial load + polling
  useEffect(() => {
    initWatermark().then(() => fetchItems())
    const pollId = setInterval(fetchItems, POLL_MS)
    return () => clearInterval(pollId)
  }, [initWatermark, fetchItems])

  // Rotation
  useEffect(() => {
    if (items.length <= 1) return
    rotateRef.current = setInterval(() => {
      setIdx(i => (i + 1) % items.length)
    }, ROTATE_MS)
    return () => clearInterval(rotateRef.current)
  }, [items])

  const dismiss = (id) => {
    const hidden = getHidden()
    if (!hidden.includes(id)) {
      localStorage.setItem(LS_HIDDEN_KEY, JSON.stringify([...hidden, id]))
    }
    setItems(prev => {
      const next = prev.filter(t => t.id !== id)
      setIdx(i => Math.min(i, Math.max(0, next.length - 1)))
      return next
    })
  }

  if (!user || items.length === 0) return null

  const txn   = items[idx]
  const color = GRADE_COLOR[txn.grade_letter] || '#94a3b8'
  const label = tickerLabel(txn)

  return (
    <div
      className="fixed bottom-0 inset-x-0 z-40 flex items-center px-4 py-2.5 text-sm border-t"
      style={{ backgroundColor: '#0d1526', borderColor: '#1e3a5f' }}
    >
      {/* Grade badge */}
      <span
        className="text-xs font-extrabold px-2 py-0.5 rounded-full border mr-3 shrink-0"
        style={{ color, borderColor: color, backgroundColor: `${color}20` }}
      >
        {txn.grade_letter}
      </span>

      {/* Label */}
      <span className="text-slate-300 truncate flex-1">{label}</span>

      {/* Counter */}
      {items.length > 1 && (
        <span className="text-slate-600 text-xs mx-3 shrink-0">
          {idx + 1}/{items.length}
        </span>
      )}

      {/* View link */}
      <Link
        to="/transactions"
        className="flex items-center gap-0.5 text-xs text-slate-400 hover:text-white transition-colors shrink-0 mr-2"
      >
        View <ChevronRight size={12} />
      </Link>

      {/* Dismiss */}
      <button
        onClick={() => dismiss(txn.id)}
        className="p-1 text-slate-600 hover:text-slate-300 transition-colors shrink-0"
      >
        <X size={14} />
      </button>
    </div>
  )
}
