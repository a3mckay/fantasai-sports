import { useState, useEffect, useRef } from 'react'
import { Settings, ChevronDown, ChevronUp, Pencil, Check } from 'lucide-react'

const DEFAULT_CATEGORIES = ['R', 'HR', 'RBI', 'SB', 'AVG', 'W', 'SV', 'K', 'ERA', 'WHIP']

const ALL_CATEGORIES = [
  'R', 'HR', 'RBI', 'SB', 'AVG', 'OPS', 'H', 'XBH', 'TB', 'BB', 'OBP', 'SLG',
  'W', 'SV', 'HLD', 'K', 'ERA', 'WHIP', 'QS', 'IP', 'K/9', 'BB/9', 'SVHLD',
]

const LEAGUE_TYPES = [
  { value: 'h2h_categories', label: 'H2H Categories' },
  { value: 'roto',           label: 'Rotisserie'     },
  { value: 'points',         label: 'Points'         },
]

const DEFAULT_ROSTER = 'C, 1B, 2B, 3B, SS, OF, OF, OF, UTIL, SP, SP, SP, RP, RP, BN, BN, BN'

/** Remove empty strings and purely-numeric tokens (Yahoo slot type codes like "0", "1"). */
function cleanPositions(raw) {
  if (!raw) return []
  const arr = Array.isArray(raw) ? raw : raw.split(/[\s,]+/)
  return arr.map(p => p.trim()).filter(p => p && !/^\d+$/.test(p))
}

export default function LeagueSettings({ onChange, initialValues }) {
  const [open, setOpen]             = useState(false)
  const [editing, setEditing]       = useState(false)
  const [numTeams, setNumTeams]     = useState('12')
  const [leagueType, setLeagueType] = useState('h2h_categories')
  const [categories, setCategories] = useState(new Set(DEFAULT_CATEGORIES))
  const [rosterRaw, setRosterRaw]   = useState(DEFAULT_ROSTER)
  const [fromYahoo, setFromYahoo]   = useState(false)
  const seeded = useRef(false)

  useEffect(() => {
    if (!initialValues || seeded.current) return
    seeded.current = true
    const cats   = initialValues.categories?.length ? new Set(initialValues.categories) : new Set(DEFAULT_CATEGORIES)
    const lt     = initialValues.leagueType || 'h2h_categories'
    const num    = String(initialValues.numTeams || 12)
    const cleaned = cleanPositions(initialValues.rosterPositions)
    const roster = cleaned.length ? cleaned.join(', ') : DEFAULT_ROSTER
    setCategories(cats)
    setLeagueType(lt)
    setNumTeams(num)
    setRosterRaw(roster)
    setFromYahoo(true)
    setOpen(true)
    onChange({ categories: [...cats], leagueType: lt, numTeams: parseInt(num) || 12, rosterPositions: cleaned })
  }, [initialValues])

  function emit(cats, lt, num, roster) {
    onChange({
      categories:      [...cats],
      leagueType:      lt,
      numTeams:        parseInt(num) || 12,
      rosterPositions: cleanPositions(roster),
    })
  }

  function toggleCategory(cat) {
    const next = new Set(categories)
    next.has(cat) ? next.delete(cat) : next.add(cat)
    setCategories(next)
    emit(next, leagueType, numTeams, rosterRaw)
  }

  function handleOpen() {
    const next = !open
    setOpen(next)
    if (!next) setEditing(false)
    if (next) emit(categories, leagueType, numTeams, rosterRaw)
  }

  const leagueTypeLabel = LEAGUE_TYPES.find(lt => lt.value === leagueType)?.label || leagueType
  const cleanedPositions = cleanPositions(rosterRaw)

  return (
    <div className="border border-navy-600 rounded-xl overflow-hidden">
      <button
        type="button"
        onClick={handleOpen}
        className="w-full flex items-center justify-between px-4 py-3 bg-navy-800 hover:bg-navy-700 transition-colors"
      >
        <div className="flex items-center gap-2 text-sm text-slate-300">
          <Settings size={14} className="text-slate-500" />
          League Settings
          {fromYahoo
            ? <span className="text-[10px] font-semibold text-[#6001d2] bg-[#6001d2]/10 border border-[#6001d2]/30 rounded px-1.5 py-0.5 ml-0.5">Y! Auto-loaded</span>
            : <span className="text-slate-600 text-xs ml-0.5">(optional)</span>
          }
        </div>
        {open ? <ChevronUp size={14} className="text-slate-500" /> : <ChevronDown size={14} className="text-slate-500" />}
      </button>

      {open && !editing && (
        <div className="p-4 bg-navy-900 border-t border-navy-700 space-y-3">
          {/* Read-only summary */}
          <div className="flex items-center justify-between">
            <div className="flex gap-4 text-xs text-slate-400">
              <span><span className="text-slate-500">Teams:</span> {numTeams}</span>
              <span><span className="text-slate-500">Type:</span> {leagueTypeLabel}</span>
            </div>
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="flex items-center gap-1 text-xs text-slate-500 hover:text-slate-300 transition-colors"
            >
              <Pencil size={11} /> Edit
            </button>
          </div>

          {/* Scoring categories (read-only pills) */}
          <div>
            <div className="text-[10px] font-semibold text-slate-500 uppercase tracking-wide mb-1.5">Scoring Categories</div>
            <div className="flex flex-wrap gap-1">
              {[...categories].map(cat => (
                <span key={cat} className="px-2 py-0.5 rounded text-[10px] font-medium bg-field-900 border border-field-700 text-field-300">{cat}</span>
              ))}
            </div>
          </div>

          {/* Roster positions (read-only) */}
          <div>
            <div className="text-[10px] font-semibold text-slate-500 uppercase tracking-wide mb-1.5">Roster Positions</div>
            <div className="flex flex-wrap gap-1">
              {cleanedPositions.map((pos, i) => (
                <span key={i} className="px-2 py-0.5 rounded text-[10px] font-medium bg-navy-800 border border-navy-600 text-slate-400">{pos}</span>
              ))}
            </div>
          </div>
        </div>
      )}

      {open && editing && (
        <div className="p-4 bg-navy-900 border-t border-navy-700 space-y-4">
          {/* Edit-mode banner */}
          <div className="flex items-center justify-between rounded-lg bg-amber-950/40 border border-amber-800/50 px-3 py-2">
            <p className="text-xs text-amber-400">Changes apply to this comparison only — your saved league settings are unchanged.</p>
            <button
              type="button"
              onClick={() => setEditing(false)}
              className="ml-3 flex items-center gap-1 text-xs text-slate-400 hover:text-white transition-colors shrink-0"
            >
              <Check size={12} /> Done
            </button>
          </div>

          {/* Teams + league type */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="section-label">Teams in league</label>
              <input className="field-input font-mono" type="number" min="4" max="24"
                value={numTeams}
                onChange={e => { setNumTeams(e.target.value); emit(categories, leagueType, e.target.value, rosterRaw) }} />
            </div>
            <div>
              <label className="section-label">League type</label>
              <select className="field-input" value={leagueType}
                onChange={e => { setLeagueType(e.target.value); emit(categories, e.target.value, numTeams, rosterRaw) }}>
                {LEAGUE_TYPES.map(lt => <option key={lt.value} value={lt.value}>{lt.label}</option>)}
              </select>
            </div>
          </div>

          {/* Scoring categories */}
          <div>
            <label className="section-label mb-2">Scoring categories</label>
            <div className="flex flex-wrap gap-1.5 mt-1">
              {ALL_CATEGORIES.map(cat => (
                <button key={cat} type="button" onClick={() => toggleCategory(cat)}
                  className={`px-2.5 py-1 rounded-md text-xs font-medium border transition-colors ${
                    categories.has(cat)
                      ? 'bg-field-700 border-field-600 text-white'
                      : 'bg-navy-800 border-navy-600 text-slate-500 hover:text-slate-300 hover:border-navy-500'
                  }`}>
                  {cat}
                </button>
              ))}
            </div>
          </div>

          {/* Roster positions */}
          <div>
            <label className="section-label">Roster positions</label>
            <input className="field-input font-mono text-xs" placeholder="C, 1B, 2B, 3B, SS, OF, OF, OF, UTIL, SP, SP, RP, RP, BN"
              value={rosterRaw}
              onChange={e => { setRosterRaw(e.target.value); emit(categories, leagueType, numTeams, e.target.value) }} />
            <p className="text-xs text-slate-600 mt-1">Comma-separated position slots. Affects roster analysis accuracy.</p>
          </div>
        </div>
      )}
    </div>
  )
}
