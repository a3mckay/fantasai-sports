import { useState, useEffect, useRef } from 'react'
import { Search, X } from 'lucide-react'
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
 *   placeholder — string
 *   className  — extra CSS classes
 */
export default function PlayerSearch({ value, onChange, placeholder, className }) {
  const [query, setQuery]     = useState(value || '')
  const [results, setResults] = useState([])
  const [open, setOpen]       = useState(false)
  const [busy, setBusy]       = useState(false)
  const containerRef          = useRef(null)
  const debouncedQuery        = useDebounce(query, 280)

  // Keep query in sync when parent resets value
  useEffect(() => {
    setQuery(value || '')
  }, [value])

  // Fetch autocomplete results
  useEffect(() => {
    if (debouncedQuery.length < 2) {
      setResults([])
      setOpen(false)
      return
    }
    setBusy(true)
    searchPlayers(debouncedQuery, 8)
      .then(data => {
        // API may return array directly or { players: [...] }
        const list = Array.isArray(data) ? data : (data.players || [])
        setResults(list)
        setOpen(list.length > 0)
      })
      .catch(() => setResults([]))
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
    onChange(player.name, player.player_id)
  }

  function clearInput() {
    setQuery('')
    setResults([])
    setOpen(false)
    onChange('', null)
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

      {busy && query.length >= 2 && !open && (
        <div className="absolute z-50 mt-1 w-full bg-navy-800 border border-navy-600 rounded-xl px-3 py-2.5 text-xs text-slate-500">
          Searching…
        </div>
      )}
    </div>
  )
}
