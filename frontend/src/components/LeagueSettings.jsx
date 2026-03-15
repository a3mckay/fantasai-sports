import { useState } from 'react'
import { Settings, ChevronDown, ChevronUp } from 'lucide-react'

const DEFAULT_CATEGORIES = ['R', 'HR', 'RBI', 'SB', 'AVG', 'W', 'SV', 'K', 'ERA', 'WHIP']

const ALL_CATEGORIES = [
  // Batting
  'R', 'HR', 'RBI', 'SB', 'AVG', 'OPS', 'H', 'XBH', 'TB', 'BB', 'OBP', 'SLG',
  // Pitching
  'W', 'SV', 'HLD', 'K', 'ERA', 'WHIP', 'QS', 'IP', 'K/9', 'BB/9', 'SVHLD',
]

const LEAGUE_TYPES = [
  { value: 'h2h_categories', label: 'H2H Categories' },
  { value: 'roto',           label: 'Rotisserie'     },
  { value: 'points',         label: 'Points'          },
]

const DEFAULT_ROSTER = 'C, 1B, 2B, 3B, SS, OF, OF, OF, UTIL, SP, SP, SP, RP, RP, BN, BN, BN'

/**
 * Collapsible league settings panel.
 *
 * Props:
 *   onChange — (settings) => void, called whenever any setting changes.
 *              settings = { categories: string[], leagueType: string,
 *                           numTeams: number, rosterPositions: string[] }
 */
export default function LeagueSettings({ onChange }) {
  const [open, setOpen]                   = useState(false)
  const [numTeams, setNumTeams]           = useState('12')
  const [leagueType, setLeagueType]       = useState('h2h_categories')
  const [categories, setCategories]       = useState(new Set(DEFAULT_CATEGORIES))
  const [rosterRaw, setRosterRaw]         = useState(DEFAULT_ROSTER)

  function emit(cats, lt, num, roster) {
    onChange({
      categories:      [...cats],
      leagueType:      lt,
      numTeams:        parseInt(num) || 12,
      rosterPositions: roster.split(/[\s,]+/).map(s => s.trim()).filter(Boolean),
    })
  }

  function toggleCategory(cat) {
    const next = new Set(categories)
    if (next.has(cat)) {
      next.delete(cat)
    } else {
      next.add(cat)
    }
    setCategories(next)
    emit(next, leagueType, numTeams, rosterRaw)
  }

  function handleLeagueTypeChange(lt) {
    setLeagueType(lt)
    emit(categories, lt, numTeams, rosterRaw)
  }

  function handleNumTeamsChange(n) {
    setNumTeams(n)
    emit(categories, leagueType, n, rosterRaw)
  }

  function handleRosterChange(r) {
    setRosterRaw(r)
    emit(categories, leagueType, numTeams, r)
  }

  function handleOpen() {
    const next = !open
    setOpen(next)
    // Emit defaults when first opening so parent always has current values
    if (next) emit(categories, leagueType, numTeams, rosterRaw)
  }

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
          <span className="text-slate-600 text-xs ml-0.5">(optional)</span>
        </div>
        {open
          ? <ChevronUp size={14} className="text-slate-500" />
          : <ChevronDown size={14} className="text-slate-500" />
        }
      </button>

      {open && (
        <div className="p-4 bg-navy-900 border-t border-navy-700 space-y-4">
          {/* Row 1: teams + league type */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="section-label">Teams in league</label>
              <input
                className="field-input font-mono"
                type="number" min="4" max="24"
                value={numTeams}
                onChange={e => handleNumTeamsChange(e.target.value)}
              />
            </div>
            <div>
              <label className="section-label">League type</label>
              <select
                className="field-input"
                value={leagueType}
                onChange={e => handleLeagueTypeChange(e.target.value)}
              >
                {LEAGUE_TYPES.map(lt => (
                  <option key={lt.value} value={lt.value}>{lt.label}</option>
                ))}
              </select>
            </div>
          </div>

          {/* Scoring categories */}
          <div>
            <label className="section-label mb-2">Scoring categories</label>
            <div className="flex flex-wrap gap-1.5 mt-1">
              {ALL_CATEGORIES.map(cat => (
                <button
                  key={cat}
                  type="button"
                  onClick={() => toggleCategory(cat)}
                  className={`px-2.5 py-1 rounded-md text-xs font-medium border transition-colors ${
                    categories.has(cat)
                      ? 'bg-field-700 border-field-600 text-white'
                      : 'bg-navy-800 border-navy-600 text-slate-500 hover:text-slate-300 hover:border-navy-500'
                  }`}
                >
                  {cat}
                </button>
              ))}
            </div>
          </div>

          {/* Roster positions */}
          <div>
            <label className="section-label">Roster positions</label>
            <input
              className="field-input font-mono text-xs"
              placeholder="C, 1B, 2B, 3B, SS, OF, OF, OF, UTIL, SP, SP, RP, RP, BN"
              value={rosterRaw}
              onChange={e => handleRosterChange(e.target.value)}
            />
            <p className="text-xs text-slate-600 mt-1">
              Comma-separated position slots. Affects roster analysis accuracy.
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
