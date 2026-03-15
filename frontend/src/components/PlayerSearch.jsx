import { useState, useEffect, useRef } from 'react'
import { Search, X, AlertCircle } from 'lucide-react'
import { searchPlayers } from '../lib/api'

function useDebounce(value, delay) {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(t)
  }, [value, delay])
  return debounced
}

/**
 * Player name autocomplete input.
 *
 * Props:
 *   value      — current display name (string)
 *   playerId   — currently resolved player ID (int | null)
 *   onChange   — (name: string, playerId: int | null) => void
 *   onEnterKey — () => void  called when Enter is pressed with no dropdown selection
 *   placeholder — string
 *   className  — extra CSS classes
 */
export default function PlayerSearch({ value, onChange, onEnterKey, placeholder, className }) {
  const [query, setQuery]       = useState(value || '')
  const [results, setResults]   = useState([])
  const [open, setOpen]         = useState(false)
  const [busy, setBusy]         = useState(false)
  const [apiError, setApiError] = useState(false)
  const containerRef            = useRef(null)
  const debouncedQuery          = useDebounce(query, 280)

  // Keep query in sync when parent resets value
  useEffect(() => {
    setQuery(value || '')
  }, [value])

  // Fetch autocomplete results
  useEffect(() => {
    if (debouncedQuery.length < 2) {
      setResults([])
      setOpen(false)
      setApiError(false)
      return
    }
    setBusy(true)
    searchPlayers(debouncedQuery, 8)
      .then(data => {
        setApiError(false)
        const list = Array.isArray(data) ? data : (data.players || [])
        setResults(list)
        setOpen(list.length > 0)
      })
      .catch(err => {
        console.error('PlayerSearch API error:', err)
        setApiError(true)
        setResults([])
        setOpen(false)
      })
      .finally(() => setBusy(false))
  }, [debouncedQuery])

  // Close dropdown when clicking outside
  useEffect(() => {
    function handleClick(e) {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [])

  function selectPlayer(player) {
    setQuery(player.name)
    setResults([])
    setOpen(false)
    setApiError(false)
    onChange(player.name, player.player_id)
  }

  function clearInput() {
    setQuery('')
    setResults([])
    setOpen(false)
    setApiError(false)
    onChange('', null)
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter') {
      e.preventDefault()
      if (open && results.length > 0) {
        // Select the first result
        selectPlayer(results[0])
      } else {
        // No dropdown — trigger add-next-player callback
        onEnterKey?.()
      }
    } else if (e.key === 'Escape') {
      setOpen(false)
    }
  }

  return (
    <div ref={containerRef} className={`relative ${className || ''}`}>
      <div className="relative">
        <Search
          size={14}
          className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500 pointer-events-none"
        />
        <input
          type="text"
          className="field-input pl-8 pr-8"
          placeholder={placeholder || 'Search player name…'}
          value={query}
          onChange={e => {
            const v = e.target.value
            setQuery(v)
            if (!v) onChange('', null)
          }}
          onFocus={() => results.length > 0 && setOpen(true)}
          onKeyDown={handleKeyDown}
          autoComplete="off"
        />
        {query && (
          <button
            type="button"
            onClick={clearInput}
            className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-200 transition-colors"
          >
            <X size={13} />
          </button>
        )}
      </div>

      {/* Dropdown */}
      {open && results.length > 0 && (
        <ul className="absolute z-50 mt-1 w-full bg-navy-800 border border-navy-600 rounded-xl shadow-2xl overflow-hidden max-h-60 overflow-y-auto">
          {results.map(player => (
            <li key={player.player_id}>
              <button
                type="button"
                onMouseDown={e => e.preventDefault()} // prevent blur before click
                onClick={() => selectPlayer(player)}
                className="w-full text-left px-3 py-2.5 hover:bg-navy-700 flex items-center gap-3 transition-colors"
              >
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium text-white truncate">{player.name}</div>
                  <div className="text-xs text-slate-500">
                    {player.team}
                    {player.positions?.length > 0 && ` · ${player.positions.join('/')}`}
                  </div>
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* Loading indicator */}
      {busy && query.length >= 2 && !open && (
        <div className="absolute z-50 mt-1 w-full bg-navy-800 border border-navy-600 rounded-xl px-3 py-2.5 text-xs text-slate-500">
          Searching…
        </div>
      )}

      {/* API error indicator */}
      {apiError && query.length >= 2 && !busy && (
        <div className="absolute z-50 mt-1 w-full bg-navy-800 border border-red-800/50 rounded-xl px-3 py-2 flex items-center gap-2 text-xs text-red-400">
          <AlertCircle size={12} className="shrink-0" />
          Search unavailable — backend unreachable
        </div>
      )}
    </div>
  )
}
