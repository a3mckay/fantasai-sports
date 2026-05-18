import { useState, useEffect, useMemo, useCallback } from 'react'
import {
  TrendingUp, LayoutGrid, Target, Activity, Table2,
  BarChart2, Shuffle, Hexagon, Layers, Flame, ArrowLeftRight,
} from 'lucide-react'
import {
  ResponsiveContainer,
  LineChart, Line,
  BarChart, Bar,
  ScatterChart, Scatter,
  RadarChart, Radar, PolarGrid, PolarAngleAxis,
  XAxis, YAxis, CartesianGrid, Tooltip,
  ReferenceLine, ReferenceArea, Cell,
} from 'recharts'
import { getLeagueVisualData, getMonteCarlo } from '../lib/api'
import Spinner from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'

// ── Team colour palette ───────────────────────────────────────────────────────
const PALETTE = [
  '#4ade80', '#f87171', '#60a5fa', '#fbbf24', '#c084fc',
  '#34d399', '#fb923c', '#38bdf8', '#f472b6', '#a78bfa',
  '#2dd4bf', '#facc15',
]

// ── Tabs ──────────────────────────────────────────────────────────────────────
const TABS = [
  { id: 'progression', label: 'Standings',  icon: TrendingUp  },
  { id: 'heatmap',     label: 'Heat Map',   icon: LayoutGrid  },
  { id: 'luck',        label: 'Luck/Skill', icon: Target      },
  { id: 'trends',      label: 'Trends',     icon: Activity    },
  { id: 'h2h',         label: 'H2H',        icon: Table2      },
  { id: 'waterfall',   label: 'Margin',     icon: BarChart2   },
  { id: 'montecarlo',  label: 'Forecast',   icon: Shuffle     },
  { id: 'radar',       label: 'Radar',      icon: Hexagon     },
  { id: 'depth',       label: 'Depth',      icon: Layers      },
  { id: 'volatility',  label: 'Volatility', icon: Flame       },
  { id: 'trades',      label: 'Trades',     icon: ArrowLeftRight },
]

// ── Helpers ───────────────────────────────────────────────────────────────────
function apWinPct(catAllplay, teamKey) {
  const cats = Object.values(catAllplay[teamKey] ?? {})
  const w = cats.reduce((s, c) => s + c.wins, 0)
  const l = cats.reduce((s, c) => s + c.losses, 0)
  const t = cats.reduce((s, c) => s + c.ties, 0)
  const total = w + l + t
  return total ? (w + 0.5 * t) / total : 0
}

function ordinal(n) {
  const s = ['th', 'st', 'nd', 'rd']
  const v = n % 100
  return n + (s[(v - 20) % 10] || s[v] || s[0])
}

const tooltipStyle = {
  backgroundColor: '#0f172a',
  border: '1px solid #1e3a5f',
  borderRadius: '8px',
  fontSize: '12px',
}

// ── Team Toggles (with Select / Deselect All) ─────────────────────────────────
function TeamToggles({ teams, colors, visible, onToggle, onSetAll }) {
  const allOn = teams.every(t => visible.has(t.team_key))
  const myKey = teams.find(t => t.is_mine)?.team_key ?? teams[0]?.team_key

  return (
    <div className="space-y-1 mb-4">
      <div className="flex items-center justify-between">
        <span className="text-xs text-slate-600 uppercase tracking-wide">Teams</span>
        <button
          onClick={() => onSetAll(allOn
            ? new Set([myKey].filter(Boolean))
            : new Set(teams.map(t => t.team_key))
          )}
          className="text-xs text-slate-500 hover:text-field-400 transition-colors"
        >
          {allOn ? 'Deselect All' : 'Select All'}
        </button>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {teams.map(t => {
          const on = visible.has(t.team_key)
          return (
            <button
              key={t.team_key}
              onClick={() => onToggle(t.team_key)}
              className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium transition-all border ${
                on ? 'border-transparent opacity-100' : 'border-navy-600 opacity-35'
              }`}
              style={on ? { backgroundColor: colors[t.team_key] + '22', color: colors[t.team_key], borderColor: colors[t.team_key] + '55' } : {}}
            >
              <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: colors[t.team_key] }} />
              {t.team_name}
            </button>
          )
        })}
      </div>
    </div>
  )
}

function ComingSoon({ title, description }) {
  return (
    <div className="flex flex-col items-center justify-center py-24 gap-4 text-center">
      <div className="text-4xl">🚧</div>
      <h3 className="text-lg font-semibold text-slate-300">{title}</h3>
      <p className="text-sm text-slate-500 max-w-sm">{description}</p>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Chart 1 — Team Progression
// ────────────────────────────────────────────────────────────────────────────
function ProgressionChart({ teams, weeklyAllplay, currentWeek, colors }) {
  const [visible, setVisible] = useState(() => new Set(teams.map(t => t.team_key)))
  const toggle = k => setVisible(prev => {
    const n = new Set(prev); n.has(k) ? (n.size > 1 && n.delete(k)) : n.add(k); return n
  })

  const chartData = useMemo(() => {
    const pts = (tk, upTo) => {
      let s = 0
      for (let w = 1; w <= upTo; w++) {
        const wk = weeklyAllplay[tk]?.[String(w)]
        if (wk) s += wk.wins * 2 + wk.ties
      }
      return s
    }
    const rows = [{ week: 0 }]
    teams.forEach(t => {
      rows[0][`${t.team_key}_s`] = 0
      if (currentWeek <= 1) rows[0][`${t.team_key}_d`] = 0
    })
    for (let w = 1; w <= currentWeek; w++) {
      const row = { week: w }
      teams.forEach(t => {
        const tk = t.team_key
        const cum = pts(tk, w)
        if (w < currentWeek) {
          row[`${tk}_s`] = cum
        }
        // Bridge: last completed week also starts the dashed segment
        if (w === currentWeek - 1 || (w === currentWeek && currentWeek === 1)) {
          row[`${tk}_d`] = cum
        }
        if (w === currentWeek) {
          row[`${tk}_d`] = cum
        }
      })
      rows.push(row)
    }
    return rows
  }, [teams, weeklyAllplay, currentWeek])

  // Custom tooltip — suppress bridge-point dashes (week currentWeek-1 _d entries)
  const CustomTooltip = useCallback(({ active, payload, label }) => {
    if (!active || !payload?.length) return null
    const filtered = payload.filter(p => {
      if (p.value == null) return false
      // Hide _d entries on every week except the actual current week
      if (p.dataKey?.endsWith('_d') && label !== currentWeek) return false
      return true
    })
    if (!filtered.length) return null
    // Sort descending by value
    const sorted = [...filtered].sort((a, b) => b.value - a.value)
    return (
      <div style={tooltipStyle} className="p-2.5 rounded-lg max-h-64 overflow-y-auto">
        <p className="text-slate-500 text-xs mb-1.5 font-medium">Week {label}</p>
        {sorted.map(p => {
          const tk = p.dataKey?.replace(/_[sd]$/, '')
          const t = teams.find(x => x.team_key === tk)
          const isLive = p.dataKey?.endsWith('_d')
          return (
            <p key={p.dataKey} className="text-xs mb-0.5 flex items-center gap-1.5">
              <span className="w-2 h-2 rounded-full shrink-0 inline-block" style={{ backgroundColor: p.stroke }} />
              <span style={{ color: p.stroke }}>{t?.team_name ?? tk}</span>
              {isLive && <span className="text-[10px] text-amber-400">live</span>}
              <span className="ml-auto pl-3 font-semibold text-slate-200">{p.value} pts</span>
            </p>
          )
        })}
      </div>
    )
  }, [teams, currentWeek])

  const maxPts = useMemo(() => {
    let mx = 0
    teams.forEach(t => {
      let cum = 0
      for (let w = 1; w <= currentWeek; w++) {
        const wk = weeklyAllplay[t.team_key]?.[String(w)]
        if (wk) cum += wk.wins * 2 + wk.ties
      }
      mx = Math.max(mx, cum)
    })
    return mx
  }, [teams, weeklyAllplay, currentWeek])

  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        Cumulative all-play points (win = 2, tie = 1) vs every other team each week.
        Solid = completed week · dashed = current week in progress.
      </p>
      <TeamToggles teams={teams} colors={colors} visible={visible} onToggle={toggle} onSetAll={setVisible} />
      <ResponsiveContainer width="100%" height={380}>
        <LineChart data={chartData} margin={{ top: 4, right: 16, left: 0, bottom: 8 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e3a5f" />
          <XAxis dataKey="week" tick={{ fill: '#64748b', fontSize: 11 }}
            label={{ value: 'Week', position: 'insideBottom', offset: -4, fill: '#475569', fontSize: 11 }} />
          <YAxis tick={{ fill: '#64748b', fontSize: 11 }} domain={[0, Math.ceil(maxPts * 1.08) || 100]} />
          <Tooltip content={<CustomTooltip />} />
          {teams.filter(t => visible.has(t.team_key)).map(t => ([
            <Line key={`${t.team_key}_s`} type="monotone" dataKey={`${t.team_key}_s`}
              stroke={colors[t.team_key]} strokeWidth={2} dot={false}
              connectNulls={false} isAnimationActive={false} legendType="none" />,
            <Line key={`${t.team_key}_d`} type="monotone" dataKey={`${t.team_key}_d`}
              stroke={colors[t.team_key]} strokeWidth={2} strokeDasharray="5 4"
              dot={false} connectNulls={false} isAnimationActive={false} legendType="none" />,
          ]))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Chart 2 — Category Heat Map
// ────────────────────────────────────────────────────────────────────────────
function CategoryHeatMap({ teams, activeCats, catAllplay, teamColors }) {
  function cellData(tk, cat) {
    const d = catAllplay[tk]?.[cat]
    if (!d) return { pct: 0.5, label: '—' }
    const total = d.wins + d.losses + d.ties
    if (!total) return { pct: 0.5, label: '—' }
    const pct = (d.wins + 0.5 * d.ties) / total
    return { pct, label: `${(pct * 100).toFixed(0)}%` }
  }
  function hsl(pct) { return `hsl(${Math.round(pct * 120)}, 55%, 22%)` }
  function textColor(pct) { return pct > 0.6 ? '#4ade80' : pct < 0.4 ? '#f87171' : '#94a3b8' }

  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        All-play win% per category across all weeks. <span className="text-field-400">Green</span> = category strength, <span className="text-stitch-400">red</span> = weakness.
      </p>
      <div className="overflow-x-auto rounded-lg border border-navy-700">
        <table className="w-full text-xs border-collapse min-w-max">
          <thead>
            <tr className="bg-navy-800 border-b border-navy-700">
              <th className="sticky left-0 z-10 bg-navy-800 px-3 py-2 text-left text-slate-400 uppercase tracking-wide font-semibold w-40">Team</th>
              {activeCats.map(cat => (
                <th key={cat} className="px-2 py-2 text-center text-slate-400 uppercase tracking-wide font-semibold">{cat}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {teams.map((t, idx) => {
              const rowBg  = idx % 2 === 0 ? 'bg-navy-900' : 'bg-navy-800/60'
              // Fully opaque for the sticky cell so scrolled content doesn't bleed through
              const stickyBg = idx % 2 === 0 ? 'bg-navy-900' : 'bg-navy-800'
              return (
                <tr key={t.team_key} className={`border-b border-navy-700/40 ${rowBg}`}>
                  <td className={`sticky left-0 z-10 px-3 py-2 font-medium whitespace-nowrap ${stickyBg}`}
                    style={{ color: teamColors[t.team_key] }}>
                    {t.team_name}
                  </td>
                  {activeCats.map(cat => {
                    const { pct, label } = cellData(t.team_key, cat)
                    return (
                      <td key={cat} className="px-2 py-2 text-center tabular-nums font-semibold"
                        style={{ backgroundColor: hsl(pct), color: textColor(pct) }}>
                        {label}
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Chart 3 — Luck vs Skill (with quadrant overlays)
// ────────────────────────────────────────────────────────────────────────────
const QUADRANTS = [
  {
    label: 'Lucky',
    sub: 'Beating expectations',
    x1: 0, x2: 50, y1: 50, y2: 100,
    fill: '#f87171', textColor: '#fca5a5',
    pos: 'insideTopLeft',
  },
  {
    label: 'Contenders',
    sub: 'Skill + results',
    x1: 50, x2: 100, y1: 50, y2: 100,
    fill: '#4ade80', textColor: '#86efac',
    pos: 'insideTopRight',
  },
  {
    label: 'Rebuilding',
    sub: 'Weak and losing',
    x1: 0, x2: 50, y1: 0, y2: 50,
    fill: '#94a3b8', textColor: '#94a3b8',
    pos: 'insideBottomLeft',
  },
  {
    label: 'Unlucky',
    sub: 'Stronger than record shows',
    x1: 50, x2: 100, y1: 0, y2: 50,
    fill: '#fbbf24', textColor: '#fde68a',
    pos: 'insideBottomRight',
  },
]

function LuckSkillScatter({ teams, catAllplay, actualRecord, colors }) {
  const scatterData = useMemo(() => teams.map(t => {
    const apPct = apWinPct(catAllplay, t.team_key) * 100
    const ar = actualRecord[t.team_key] ?? { wins: 0, losses: 0, ties: 0 }
    const actTotal = ar.wins + ar.losses + ar.ties
    const actPct = actTotal ? (ar.wins + 0.5 * ar.ties) / actTotal * 100 : 50
    return { x: apPct, y: actPct, name: t.team_name, team_key: t.team_key }
  }), [teams, catAllplay, actualRecord])

  const CustomDot = ({ cx, cy, payload }) => {
    const color = colors[payload.team_key]
    return (
      <g>
        <circle cx={cx} cy={cy} r={7} fill={color} fillOpacity={0.85} stroke={color} strokeWidth={1} />
        <text x={cx + 10} y={cy + 4} fontSize={10} fill={color}>{payload.name}</text>
      </g>
    )
  }

  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500">
        X = all-play win% (true strength — schedule-independent). Y = actual schedule win%.
        The dashed diagonal = exactly on skill. Distance above/below shows luck impact.
      </p>
      <ResponsiveContainer width="100%" height={420}>
        <ScatterChart margin={{ top: 20, right: 40, left: 0, bottom: 24 }}>
          {QUADRANTS.map(q => (
            <ReferenceArea
              key={q.label}
              x1={q.x1} x2={q.x2} y1={q.y1} y2={q.y2}
              fill={q.fill} fillOpacity={0.05}
              label={{
                value: q.label,
                position: q.pos,
                fill: q.textColor,
                fontSize: 11,
                fontWeight: 600,
                opacity: 0.7,
              }}
            />
          ))}
          <CartesianGrid strokeDasharray="3 3" stroke="#1e3a5f" />
          {/* Diagonal "perfect skill" line */}
          <ReferenceLine
            segment={[{ x: 20, y: 20 }, { x: 80, y: 80 }]}
            stroke="#334155" strokeDasharray="5 5" strokeWidth={1.5}
          />
          <ReferenceLine x={50} stroke="#1e3a5f" strokeWidth={1} />
          <ReferenceLine y={50} stroke="#1e3a5f" strokeWidth={1} />
          <XAxis type="number" dataKey="x" name="All-Play Win%" domain={[20, 80]}
            tick={{ fill: '#64748b', fontSize: 11 }}
            label={{ value: 'All-Play Win%  (true strength →)', position: 'insideBottom', offset: -14, fill: '#475569', fontSize: 11 }} />
          <YAxis type="number" dataKey="y" name="Actual Schedule Win%" domain={[0, 100]}
            tick={{ fill: '#64748b', fontSize: 11 }}
            label={{ value: 'Actual Schedule Win%', angle: -90, position: 'insideLeft', offset: 14, fill: '#475569', fontSize: 11 }} />
          <Tooltip
            contentStyle={tooltipStyle}
            formatter={(val, name) => [`${Number(val).toFixed(1)}%`, name]}
            cursor={{ strokeDasharray: '3 3', stroke: '#334155' }}
          />
          <Scatter data={scatterData} shape={<CustomDot />} isAnimationActive={false} />
        </ScatterChart>
      </ResponsiveContainer>
      <div className="grid grid-cols-2 gap-2 text-xs">
        {QUADRANTS.map(q => (
          <div key={q.label} className="flex items-start gap-2 p-2 rounded-lg bg-navy-800/50 border border-navy-700/50">
            <span style={{ color: q.textColor }} className="text-base leading-none mt-0.5">■</span>
            <div>
              <span className="font-semibold text-slate-200">{q.label}</span>
              <span className="text-slate-500 ml-1">— {q.sub}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Chart 4 — Category Trends
// ────────────────────────────────────────────────────────────────────────────
function CategoryTrends({ teams, activeCats, weeklyStats, currentWeek, colors }) {
  const [selectedCat, setSelectedCat] = useState(activeCats[0] ?? '')
  const [visible, setVisible] = useState(() => new Set(teams.map(t => t.team_key)))
  const toggle = k => setVisible(prev => {
    const n = new Set(prev); n.has(k) ? (n.size > 1 && n.delete(k)) : n.add(k); return n
  })

  const chartData = useMemo(() => Array.from({ length: currentWeek }, (_, i) => {
    const w = i + 1
    const row = { week: w }
    teams.forEach(t => {
      const v = weeklyStats[t.team_key]?.[String(w)]?.[selectedCat]
      if (v != null) row[t.team_key] = Number(v)
    })
    return row
  }), [teams, weeklyStats, selectedCat, currentWeek])

  const fmt = v => {
    if (v == null) return '—'
    if (['AVG', 'OBP', 'SLG', 'OPS'].includes(selectedCat)) return Number(v).toFixed(3).replace(/^0\./, '.')
    if (['ERA', 'WHIP'].includes(selectedCat)) return Number(v).toFixed(2)
    return Math.round(v)
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <label className="text-xs text-slate-500 uppercase tracking-wide">Category</label>
        <select value={selectedCat} onChange={e => setSelectedCat(e.target.value)}
          className="text-sm bg-navy-800 border border-navy-600 rounded px-2 py-1.5 text-slate-200 focus:outline-none focus:ring-1 focus:ring-leather-400">
          {activeCats.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
        {(selectedCat === 'ERA' || selectedCat === 'WHIP') && (
          <span className="text-xs text-slate-500 italic">lower is better</span>
        )}
      </div>
      <TeamToggles teams={teams} colors={colors} visible={visible} onToggle={toggle} onSetAll={setVisible} />
      <ResponsiveContainer width="100%" height={340}>
        <LineChart data={chartData} margin={{ top: 4, right: 16, left: 0, bottom: 8 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e3a5f" />
          <XAxis dataKey="week" tick={{ fill: '#64748b', fontSize: 11 }}
            label={{ value: 'Week', position: 'insideBottom', offset: -4, fill: '#475569', fontSize: 11 }} />
          <YAxis tick={{ fill: '#64748b', fontSize: 11 }} tickFormatter={fmt} />
          <Tooltip contentStyle={tooltipStyle} labelFormatter={w => `Week ${w}`} formatter={v => [fmt(v)]} />
          {teams.filter(t => visible.has(t.team_key)).map(t => (
            <Line key={t.team_key} type="monotone" dataKey={t.team_key} name={t.team_name}
              stroke={colors[t.team_key]} strokeWidth={2}
              dot={{ r: 3, fill: colors[t.team_key] }}
              connectNulls={false} isAnimationActive={false} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Chart 5 — Head-to-Head Matrix
// ────────────────────────────────────────────────────────────────────────────
function H2HMatrix({ teams, h2hResults, teamColors }) {
  function cell(rk, ck) {
    if (rk === ck) return null
    return h2hResults[rk]?.[ck] ?? { wins: 0, losses: 0, ties: 0 }
  }
  function winPct(d) {
    if (!d) return null
    const t = d.wins + d.losses + d.ties
    return t ? (d.wins + 0.5 * d.ties) / t : null
  }
  function bg(pct) {
    if (pct == null) return '#0f172a'
    return `hsl(${Math.round(pct * 120)}, 50%, 18%)`
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        Row team's cumulative all-play W-L-T vs column team across all weeks. <span className="text-field-400">Green</span> = row team dominates.
      </p>
      <div className="overflow-x-auto rounded-lg border border-navy-700">
        <table className="text-xs border-collapse min-w-max">
          <thead>
            <tr className="bg-navy-800 border-b border-navy-700">
              <th className="sticky left-0 z-10 bg-navy-800 px-3 py-2 text-left text-slate-500 w-36" />
              {teams.map(t => (
                <th key={t.team_key} className="px-2 py-2 text-center font-semibold"
                  style={{ color: teamColors[t.team_key] }}>
                  {t.team_name.length > 8 ? t.team_name.slice(0, 8) + '…' : t.team_name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {teams.map((rowT, ri) => (
              <tr key={rowT.team_key} className={ri % 2 === 0 ? 'bg-navy-900' : 'bg-navy-800/60'}>
                <td className={`sticky left-0 z-10 px-3 py-2 font-medium whitespace-nowrap ${ri % 2 === 0 ? 'bg-navy-900' : 'bg-navy-800'}`}
                  style={{ color: teamColors[rowT.team_key] }}>
                  {rowT.team_name}
                </td>
                {teams.map(colT => {
                  const d = cell(rowT.team_key, colT.team_key)
                  const pct = winPct(d)
                  return (
                    <td key={colT.team_key} className="px-2 py-2 text-center tabular-nums font-medium"
                      style={{ backgroundColor: bg(pct), color: pct == null ? '#1e3a5f' : pct > 0.55 ? '#4ade80' : pct < 0.45 ? '#f87171' : '#94a3b8' }}>
                      {d == null ? '—' : `${d.wins}-${d.losses}${d.ties ? `-${d.ties}` : ''}`}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Chart 6 — Matchup Margin Waterfall
// ────────────────────────────────────────────────────────────────────────────
function WaterfallChart({ teams, weeklyAllplay, currentWeek, colors }) {
  const [selectedKey, setSelectedKey] = useState(() => teams.find(t => t.is_mine)?.team_key ?? teams[0]?.team_key ?? '')

  const chartData = useMemo(() => Array.from({ length: currentWeek }, (_, i) => {
    const w = i + 1
    const wk = weeklyAllplay[selectedKey]?.[String(w)]
    const margin = wk ? wk.wins - wk.losses : 0
    return { week: `Wk ${w}`, pos: Math.max(0, margin), neg: Math.min(0, margin), margin, wins: wk?.wins ?? 0, losses: wk?.losses ?? 0, ties: wk?.ties ?? 0 }
  }), [selectedKey, weeklyAllplay, currentWeek])

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <label className="text-xs text-slate-500 uppercase tracking-wide">Team</label>
        <select value={selectedKey} onChange={e => setSelectedKey(e.target.value)}
          className="text-sm bg-navy-800 border border-navy-600 rounded px-2 py-1.5 text-slate-200 focus:outline-none focus:ring-1 focus:ring-leather-400">
          {teams.map(t => <option key={t.team_key} value={t.team_key}>{t.team_name}{t.is_mine ? ' (Me)' : ''}</option>)}
        </select>
      </div>
      <p className="text-xs text-slate-500">Weekly all-play margin (category wins − losses vs every other team). Green = net positive, red = net negative.</p>
      <ResponsiveContainer width="100%" height={300}>
        <BarChart data={chartData} margin={{ top: 4, right: 16, left: 0, bottom: 4 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e3a5f" vertical={false} />
          <XAxis dataKey="week" tick={{ fill: '#64748b', fontSize: 11 }} />
          <YAxis tick={{ fill: '#64748b', fontSize: 11 }} />
          <Tooltip contentStyle={tooltipStyle}
            formatter={(val, key, props) => {
              const d = props.payload
              return [`${d.wins}W – ${d.losses}L – ${d.ties}T  (net: ${d.margin > 0 ? '+' : ''}${d.margin})`, 'Result']
            }} />
          <ReferenceLine y={0} stroke="#334155" strokeWidth={1.5} />
          <Bar dataKey="pos" name="Positive" radius={[3, 3, 0, 0]} isAnimationActive={false}>
            {chartData.map((_, i) => <Cell key={i} fill="#4ade80" fillOpacity={0.8} />)}
          </Bar>
          <Bar dataKey="neg" name="Negative" radius={[0, 0, 3, 3]} isAnimationActive={false}>
            {chartData.map((_, i) => <Cell key={i} fill="#f87171" fillOpacity={0.8} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Chart 7 — Monte Carlo (full position distribution)
// ────────────────────────────────────────────────────────────────────────────
function posColor(rank, total) {
  const t = (rank - 1) / Math.max(total - 1, 1)
  return `hsl(${Math.round((1 - t) * 120)}, 60%, 42%)`
}

function MonteCarloChart() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState(null)
  const [fetched, setFetched] = useState(false)

  const load = useCallback(async () => {
    setLoading(true); setErr(null)
    try { const d = await getMonteCarlo(22); setData(d); setFetched(true) }
    catch (e) { setErr(e.message) }
    finally { setLoading(false) }
  }, [])

  if (!fetched && !loading) return (
    <div className="flex flex-col items-center justify-center py-20 gap-4">
      <p className="text-sm text-slate-400 text-center max-w-sm">
        Simulates the remainder of the season 1,000 times using each team&apos;s current
        all-play win rate as their strength. Returns a probability for each finishing position.
      </p>
      <button onClick={load}
        className="px-5 py-2.5 bg-field-700 hover:bg-field-600 text-white text-sm font-semibold rounded-lg transition-colors">
        Run Simulation
      </button>
    </div>
  )
  if (loading) return (
    <div className="flex items-center justify-center py-20 gap-3 text-slate-400">
      <Spinner /><span>Simulating 1,000 seasons…</span>
    </div>
  )
  if (err) return <ErrorBanner message={err} />
  if (!data) return null

  const { teams, finish_probs, remaining_weeks, simulations } = data
  const n = teams.length

  // Expected finish per team (weighted average)
  const expected = (tk) => {
    const probs = finish_probs[tk] ?? Array(n).fill(0)
    return probs.reduce((s, p, i) => s + p * (i + 1), 0)
  }

  // Sort teams best-to-worst expected finish
  const sorted = [...teams].sort((a, b) => expected(a.team_key) - expected(b.team_key))

  const barData = sorted.map(t => {
    const probs = finish_probs[t.team_key] ?? Array(n).fill(0)
    const entry = {
      name: t.team_name.length > 12 ? t.team_name.slice(0, 12) + '…' : t.team_name,
      team_key: t.team_key,
      expected: expected(t.team_key).toFixed(1),
    }
    probs.forEach((p, i) => { entry[`pos${i + 1}`] = Math.round(p * 100) })
    return entry
  })

  // Custom tooltip showing full distribution compactly
  const CustomTooltip = ({ active, payload, label }) => {
    if (!active || !payload?.length) return null
    const row = barData.find(d => d.name === label)
    if (!row) return null
    const probs = Array.from({ length: n }, (_, i) => ({ pos: i + 1, pct: row[`pos${i + 1}`] ?? 0 }))
      .filter(p => p.pct > 0)
      .sort((a, b) => b.pct - a.pct)
    return (
      <div style={tooltipStyle} className="p-2.5 rounded-lg min-w-[160px]">
        <p className="text-slate-300 text-xs font-semibold mb-1">{label}</p>
        <p className="text-slate-500 text-xs mb-2">Expected finish: <span className="text-slate-300 font-medium">{ordinal(Math.round(Number(row.expected)))}</span></p>
        <div className="space-y-0.5">
          {probs.slice(0, 6).map(p => (
            <div key={p.pos} className="flex items-center gap-2 text-xs">
              <span className="w-2 h-2 rounded-sm shrink-0" style={{ backgroundColor: posColor(p.pos, n) }} />
              <span className="text-slate-400">{ordinal(p.pos)}</span>
              <div className="flex-1 h-1 bg-navy-700 rounded-full overflow-hidden">
                <div className="h-full rounded-full" style={{ width: `${p.pct}%`, backgroundColor: posColor(p.pos, n) }} />
              </div>
              <span className="text-slate-300 tabular-nums w-8 text-right">{p.pct}%</span>
            </div>
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500">
        {simulations.toLocaleString()} simulations · {remaining_weeks} weeks remaining of 22.
        Each segment = probability of finishing in that position. Sorted by expected finish (best left).
      </p>

      {/* Position color legend */}
      <div className="flex flex-wrap gap-1.5 items-center">
        {Array.from({ length: n }, (_, i) => (
          <span key={i} className="flex items-center gap-1 text-xs text-slate-500">
            <span className="w-3 h-3 rounded-sm inline-block" style={{ backgroundColor: posColor(i + 1, n) }} />
            {ordinal(i + 1)}
          </span>
        ))}
      </div>

      <ResponsiveContainer width="100%" height={360}>
        <BarChart data={barData} margin={{ top: 4, right: 16, left: 0, bottom: 72 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e3a5f" vertical={false} />
          <XAxis dataKey="name" tick={{ fill: '#64748b', fontSize: 10 }} angle={-40} textAnchor="end" interval={0} />
          <YAxis tick={{ fill: '#64748b', fontSize: 11 }} unit="%" />
          <Tooltip content={<CustomTooltip />} />
          {/* Stack position 1 at bottom (green), last at top (red) */}
          {Array.from({ length: n }, (_, i) => (
            <Bar key={`pos${i + 1}`} dataKey={`pos${i + 1}`} stackId="a"
              fill={posColor(i + 1, n)}
              isAnimationActive={false}
              radius={i === n - 1 ? [3, 3, 0, 0] : [0, 0, 0, 0]}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
      <button onClick={load} className="text-xs text-slate-500 hover:text-slate-300 underline transition-colors">
        Re-run simulation
      </button>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Chart 8 — Category Radar
// ────────────────────────────────────────────────────────────────────────────
function CategoryRadar({ teams, activeCats, catAllplay, colors }) {
  const [visible, setVisible] = useState(() => new Set(teams.map(t => t.team_key)))
  const toggle = k => setVisible(prev => {
    const n = new Set(prev); n.has(k) ? (n.size > 1 && n.delete(k)) : n.add(k); return n
  })

  const radarData = useMemo(() => activeCats.map(cat => {
    const entry = { cat }
    teams.forEach(t => {
      const d = catAllplay[t.team_key]?.[cat]
      const total = d ? d.wins + d.losses + d.ties : 0
      entry[t.team_key] = total ? Math.round((d.wins + 0.5 * d.ties) / total * 100) : 50
    })
    return entry
  }), [teams, activeCats, catAllplay])

  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        All-play win% per scoring category. Outer edge = 100% (dominating), center = 0%. Compare roster builds across teams.
      </p>
      <TeamToggles teams={teams} colors={colors} visible={visible} onToggle={toggle} onSetAll={setVisible} />
      <ResponsiveContainer width="100%" height={420}>
        <RadarChart data={radarData} cx="50%" cy="50%" outerRadius="72%">
          <PolarGrid stroke="#1e3a5f" />
          <PolarAngleAxis dataKey="cat" tick={{ fill: '#64748b', fontSize: 11 }} />
          <Tooltip contentStyle={tooltipStyle} formatter={v => [`${v}%`]} />
          {teams.filter(t => visible.has(t.team_key)).map(t => (
            <Radar key={t.team_key} name={t.team_name} dataKey={t.team_key}
              stroke={colors[t.team_key]} fill={colors[t.team_key]}
              fillOpacity={0.08} strokeWidth={1.5} isAnimationActive={false} />
          ))}
        </RadarChart>
      </ResponsiveContainer>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Chart 10 — Volatility Scatter (avg vs consistency)
// ────────────────────────────────────────────────────────────────────────────
function VolatilityChart({ teams, weeklyAllplay, currentWeek, colors }) {
  const scatterData = useMemo(() => {
    return teams.map(t => {
      const weeks = Array.from({ length: currentWeek }, (_, i) => {
        const wk = weeklyAllplay[t.team_key]?.[String(i + 1)]
        return wk ? wk.wins * 2 + wk.ties : null
      }).filter(v => v !== null)
      if (!weeks.length) return null
      const avg = weeks.reduce((s, v) => s + v, 0) / weeks.length
      const std = Math.sqrt(weeks.reduce((s, v) => s + (v - avg) ** 2, 0) / weeks.length)
      return {
        x: Math.round(avg * 10) / 10,
        y: Math.round(std * 10) / 10,
        name: t.team_name,
        team_key: t.team_key,
      }
    }).filter(Boolean)
  }, [teams, weeklyAllplay, currentWeek])

  // Medians for reference lines (dividing quadrants)
  const sorted = d => [...scatterData.map(s => s[d])].sort((a, b) => a - b)
  const med = d => {
    const s = sorted(d); const m = Math.floor(s.length / 2)
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2
  }
  const medX = scatterData.length ? med('x') : 0
  const medY = scatterData.length ? med('y') : 0

  const maxX = scatterData.length ? Math.max(...scatterData.map(d => d.x)) * 1.15 : 100
  const maxY = scatterData.length ? Math.max(...scatterData.map(d => d.y)) * 1.15 : 30
  const minX = scatterData.length ? Math.min(...scatterData.map(d => d.x)) * 0.85 : 0

  const VQUADS = [
    { x1: minX, x2: medX, y1: medY, y2: maxY, fill: '#f87171', label: 'Chaotic',               pos: 'insideTopLeft',    textColor: '#fca5a5' },
    { x1: medX, x2: maxX, y1: medY, y2: maxY, fill: '#fbbf24', label: 'Boom/Bust',              pos: 'insideTopRight',   textColor: '#fde68a' },
    { x1: minX, x2: medX, y1: 0,    y2: medY, fill: '#94a3b8', label: 'Consistently Weak',      pos: 'insideBottomLeft', textColor: '#94a3b8' },
    { x1: medX, x2: maxX, y1: 0,    y2: medY, fill: '#4ade80', label: 'Consistent Contenders',  pos: 'insideBottomRight',textColor: '#86efac' },
  ]

  const CustomDot = ({ cx, cy, payload }) => {
    const color = colors[payload.team_key]
    return (
      <g>
        <circle cx={cx} cy={cy} r={7} fill={color} fillOpacity={0.85} stroke="#0f172a" strokeWidth={1} />
        <text x={cx + 10} y={cy + 4} fontSize={10} fill={color}>{payload.name}</text>
      </g>
    )
  }

  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500">
        Each dot = one team. X-axis = average weekly all-play points (how good).
        Y-axis = standard deviation (how consistent — <span className="text-field-400">lower is steadier</span>).
        Dashed lines = league medians.
      </p>
      <ResponsiveContainer width="100%" height={400}>
        <ScatterChart margin={{ top: 16, right: 40, left: 0, bottom: 24 }}>
          {VQUADS.map(q => (
            <ReferenceArea key={q.label}
              x1={q.x1} x2={q.x2} y1={q.y1} y2={q.y2}
              fill={q.fill} fillOpacity={0.05}
              label={{ value: q.label, position: q.pos, fill: q.textColor, fontSize: 10, fontWeight: 600, opacity: 0.7 }}
            />
          ))}
          <CartesianGrid strokeDasharray="3 3" stroke="#1e3a5f" />
          <ReferenceLine x={medX} stroke="#334155" strokeDasharray="5 5" strokeWidth={1.5} label={{ value: 'avg median', fill: '#475569', fontSize: 9, position: 'insideTopRight' }} />
          <ReferenceLine y={medY} stroke="#334155" strokeDasharray="5 5" strokeWidth={1.5} label={{ value: 'std median', fill: '#475569', fontSize: 9, position: 'insideTopLeft' }} />
          <XAxis type="number" dataKey="x" name="Avg Weekly Points" domain={['auto', 'auto']}
            tick={{ fill: '#64748b', fontSize: 11 }}
            label={{ value: 'Average Weekly All-Play Points  (higher = stronger →)', position: 'insideBottom', offset: -14, fill: '#475569', fontSize: 10 }} />
          <YAxis type="number" dataKey="y" name="Std Dev" domain={[0, 'auto']}
            tick={{ fill: '#64748b', fontSize: 11 }}
            label={{ value: 'Std Dev  (↑ = more volatile)', angle: -90, position: 'insideLeft', offset: 12, fill: '#475569', fontSize: 10 }} />
          <Tooltip contentStyle={tooltipStyle}
            formatter={(v, name, props) => {
              const d = props?.payload
              if (!d) return [v, name]
              return name === 'Avg Weekly Points'
                ? [`${d.x} pts/wk`, 'Average']
                : [`${d.y} pts`, 'Std Dev']
            }}
          />
          <Scatter data={scatterData} shape={<CustomDot />} isAnimationActive={false} />
        </ScatterChart>
      </ResponsiveContainer>
      <div className="grid grid-cols-2 gap-2 text-xs">
        {VQUADS.map(q => (
          <div key={q.label} className="flex items-start gap-2 p-2 rounded-lg bg-navy-800/50 border border-navy-700/50">
            <span style={{ color: q.textColor }} className="text-base leading-none mt-0.5">■</span>
            <div>
              <span className="font-semibold text-slate-200">{q.label}</span>
              <p className="text-slate-500 mt-0.5">
                {q.label === 'Consistent Contenders' && 'High output, week in week out. Tough to beat.'}
                {q.label === 'Boom/Bust' && 'Strong on average but swings wildly. Can steal any week or collapse.'}
                {q.label === 'Consistently Weak' && 'Predictably below average. Rebuilding or roster-limited.'}
                {q.label === 'Chaotic' && 'Below average AND unpredictable. Hard to trade with or plan around.'}
              </p>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Main Page
// ────────────────────────────────────────────────────────────────────────────
export default function VisualLeagueData() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [activeTab, setActiveTab] = useState('progression')

  useEffect(() => {
    getLeagueVisualData()
      .then(d => { setData(d); setLoading(false) })
      .catch(e => { setError(e.message); setLoading(false) })
  }, [])

  const colors = useMemo(() => {
    if (!data) return {}
    const map = {}
    ;(data.teams ?? []).forEach((t, i) => { map[t.team_key] = PALETTE[i % PALETTE.length] })
    return map
  }, [data])

  return (
    <div className="-mx-4 md:-mx-8 -mt-8 min-h-full">
      <div className="px-4 md:px-6 py-6 space-y-4">
        <div>
          <h1 className="text-xl font-bold text-slate-100 flex items-center gap-2">
            <BarChart2 className="w-5 h-5 text-leather-400" />
            Visual League Data
          </h1>
          <p className="text-sm text-slate-500 mt-0.5">League-wide analytics and data visualizations</p>
        </div>

        <div className="flex gap-0.5 border-b border-navy-700 overflow-x-auto scrollbar-none">
          {TABS.map(tab => (
            <button key={tab.id} onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-1.5 px-3 py-2 text-xs font-medium whitespace-nowrap transition-colors border-b-2 -mb-px shrink-0 ${
                activeTab === tab.id
                  ? 'border-field-500 text-field-300'
                  : 'border-transparent text-slate-500 hover:text-slate-300'
              }`}>
              <tab.icon className="w-3 h-3" />
              {tab.label}
            </button>
          ))}
        </div>

        {loading ? (
          <div className="flex items-center justify-center py-20 gap-3 text-slate-400">
            <Spinner /><span>Loading league data…</span>
          </div>
        ) : error ? (
          <ErrorBanner message={error} />
        ) : data ? (
          <div className="pt-2">
            {activeTab === 'progression' && <ProgressionChart teams={data.teams} weeklyAllplay={data.weekly_allplay} currentWeek={data.current_week} colors={colors} />}
            {activeTab === 'heatmap'     && <CategoryHeatMap teams={data.teams} activeCats={data.active_cats} catAllplay={data.cat_allplay} teamColors={colors} />}
            {activeTab === 'luck'        && <LuckSkillScatter teams={data.teams} catAllplay={data.cat_allplay} actualRecord={data.actual_record} colors={colors} />}
            {activeTab === 'trends'      && <CategoryTrends teams={data.teams} activeCats={data.active_cats} weeklyStats={data.weekly_stats} currentWeek={data.current_week} colors={colors} />}
            {activeTab === 'h2h'         && <H2HMatrix teams={data.teams} h2hResults={data.h2h_results} teamColors={colors} />}
            {activeTab === 'waterfall'   && <WaterfallChart teams={data.teams} weeklyAllplay={data.weekly_allplay} currentWeek={data.current_week} colors={colors} />}
            {activeTab === 'montecarlo'  && <MonteCarloChart />}
            {activeTab === 'radar'       && <CategoryRadar teams={data.teams} activeCats={data.active_cats} catAllplay={data.cat_allplay} colors={colors} />}
            {activeTab === 'depth'       && <ComingSoon title="Roster Depth Contribution" description="Shows what proportion of each team's category wins come from their top 3 players vs the rest of the roster. Requires per-player roster data from Yahoo — coming soon." />}
            {activeTab === 'volatility'  && <VolatilityChart teams={data.teams} weeklyAllplay={data.weekly_allplay} currentWeek={data.current_week} colors={colors} />}
            {activeTab === 'trades'      && <ComingSoon title="Trade Impact Timeline" description="Overlays your league's transaction history on the team progression chart — so you can see whether trades and waiver pickups actually moved the needle. Coming soon." />}
          </div>
        ) : null}
      </div>
    </div>
  )
}
