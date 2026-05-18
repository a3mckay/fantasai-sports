import { useState, useEffect, useMemo, useCallback } from 'react'
import {
  TrendingUp, LayoutGrid, Target, Activity, Table2,
  BarChart2, Shuffle, Hexagon, Layers, Flame, ArrowLeftRight,
} from 'lucide-react'
import {
  ResponsiveContainer,
  LineChart, Line,
  BarChart, Bar,
  ScatterChart, Scatter, ZAxis,
  RadarChart, Radar, PolarGrid, PolarAngleAxis,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ReferenceLine, Cell,
} from 'recharts'
import { getLeagueVisualData, getMonteCarlo } from '../lib/api'
import Spinner from '../components/Spinner'
import ErrorBanner from '../components/ErrorBanner'

// ── Team colour palette (12 distinct, dark-bg friendly) ──────────────────────
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

// ── Shared helpers ────────────────────────────────────────────────────────────
function apWinPct(catAllplay, teamKey) {
  const cats = Object.values(catAllplay[teamKey] ?? {})
  const w = cats.reduce((s, c) => s + c.wins, 0)
  const l = cats.reduce((s, c) => s + c.losses, 0)
  const t = cats.reduce((s, c) => s + c.ties, 0)
  const total = w + l + t
  return total ? (w + 0.5 * t) / total : 0
}

function winPctLabel(pct) { return `${(pct * 100).toFixed(1)}%` }

// ── Team Toggle Row ───────────────────────────────────────────────────────────
function TeamToggles({ teams, colors, visible, onToggle }) {
  return (
    <div className="flex flex-wrap gap-2 mb-4">
      {teams.map(t => {
        const on = visible.has(t.team_key)
        return (
          <button
            key={t.team_key}
            onClick={() => onToggle(t.team_key)}
            className={`flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium transition-all border ${
              on ? 'border-transparent opacity-100' : 'border-navy-600 opacity-40'
            }`}
            style={on ? { backgroundColor: colors[t.team_key] + '22', color: colors[t.team_key], borderColor: colors[t.team_key] + '55' } : {}}
          >
            <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: colors[t.team_key] }} />
            {t.team_name}
          </button>
        )
      })}
    </div>
  )
}

// ── Tooltip styles ────────────────────────────────────────────────────────────
const tooltipStyle = {
  backgroundColor: '#0f172a',
  border: '1px solid #1e3a5f',
  borderRadius: '8px',
  fontSize: '12px',
}

// ── Coming-soon placeholder ───────────────────────────────────────────────────
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
// Chart 1 — Team Progression (cumulative all-play points over time)
// ────────────────────────────────────────────────────────────────────────────
function ProgressionChart({ teams, weeklyAllplay, currentWeek, colors }) {
  const [visible, setVisible] = useState(() => new Set(teams.map(t => t.team_key)))
  const toggle = k => setVisible(prev => {
    const n = new Set(prev)
    n.has(k) ? (n.size > 1 && n.delete(k)) : n.add(k)
    return n
  })

  const chartData = useMemo(() => {
    const pts = (tk, upToWeek) => {
      let sum = 0
      for (let w = 1; w <= upToWeek; w++) {
        const wk = weeklyAllplay[tk]?.[String(w)]
        if (wk) sum += wk.wins * 2 + wk.ties
      }
      return sum
    }

    const rows = [{ week: 0 }]
    teams.forEach(t => {
      rows[0][`${t.team_key}_s`] = 0
      if (currentWeek === 1) rows[0][`${t.team_key}_d`] = 0
    })

    for (let w = 1; w <= currentWeek; w++) {
      const row = { week: w }
      teams.forEach(t => {
        const tk = t.team_key
        const cumPts = pts(tk, w)
        if (w < currentWeek) {
          row[`${tk}_s`] = cumPts
        } else {
          // current week: solid bridge + dashed endpoint
          row[`${tk}_d`] = cumPts
        }
        // Bridge: last solid week also gets a dash value
        if (w === currentWeek - 1) {
          row[`${tk}_d`] = cumPts
        }
      })
      rows.push(row)
    }
    return rows
  }, [teams, weeklyAllplay, currentWeek])

  const maxPts = useMemo(() => {
    let mx = 0
    teams.forEach(t => {
      const wk = weeklyAllplay[t.team_key]?.[String(currentWeek)]
      if (wk) {
        let cum = 0
        for (let w = 1; w <= currentWeek; w++) {
          const d = weeklyAllplay[t.team_key]?.[String(w)]
          if (d) cum += d.wins * 2 + d.ties
        }
        mx = Math.max(mx, cum)
      }
    })
    return mx
  }, [teams, weeklyAllplay, currentWeek])

  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500">
        Cumulative all-play points (win = 2, tie = 1) vs every other team each week.
        Solid = completed · <span className="border-b border-dashed border-slate-500 inline-block w-6 mb-0.5" /> = current week in progress.
      </p>
      <TeamToggles teams={teams} colors={colors} visible={visible} onToggle={toggle} />
      <ResponsiveContainer width="100%" height={360}>
        <LineChart data={chartData} margin={{ top: 4, right: 16, left: 0, bottom: 4 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e3a5f" />
          <XAxis dataKey="week" tick={{ fill: '#64748b', fontSize: 11 }} label={{ value: 'Week', position: 'insideBottom', offset: -2, fill: '#475569', fontSize: 11 }} />
          <YAxis tick={{ fill: '#64748b', fontSize: 11 }} domain={[0, Math.ceil(maxPts * 1.05) || 100]} />
          <Tooltip
            contentStyle={tooltipStyle}
            labelFormatter={w => `Week ${w}`}
            formatter={(val, key) => {
              const tk = key.replace(/_[sd]$/, '')
              const t = teams.find(x => x.team_key === tk)
              return [val, t?.team_name ?? tk]
            }}
          />
          {teams.filter(t => visible.has(t.team_key)).map(t => (
            [
              <Line
                key={`${t.team_key}_s`}
                type="monotone"
                dataKey={`${t.team_key}_s`}
                stroke={colors[t.team_key]}
                strokeWidth={2}
                dot={false}
                connectNulls={false}
                isAnimationActive={false}
                name={t.team_name}
              />,
              <Line
                key={`${t.team_key}_d`}
                type="monotone"
                dataKey={`${t.team_key}_d`}
                stroke={colors[t.team_key]}
                strokeWidth={2}
                strokeDasharray="5 4"
                dot={false}
                connectNulls={false}
                isAnimationActive={false}
                name={`${t.team_name} (live)`}
                legendType="none"
              />,
            ]
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Chart 2 — Category Dominance Heat Map
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

  // hsl: 0=red, 120=green; map 0-1 → 0-120
  function hsl(pct) {
    const h = Math.round(pct * 120)
    return `hsl(${h}, 55%, 22%)`
  }
  function textColor(pct) {
    return pct > 0.6 ? '#4ade80' : pct < 0.4 ? '#f87171' : '#94a3b8'
  }

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
              const rowBg = idx % 2 === 0 ? 'bg-navy-900' : 'bg-navy-800/60'
              return (
                <tr key={t.team_key} className={`border-b border-navy-700/40 ${rowBg}`}>
                  <td className={`sticky left-0 z-10 px-3 py-2 font-medium whitespace-nowrap ${rowBg}`}
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
// Chart 3 — Luck vs Skill Scatter
// ────────────────────────────────────────────────────────────────────────────
function LuckSkillScatter({ teams, catAllplay, actualRecord, colors }) {
  const scatterData = useMemo(() => teams.map(t => {
    const apPct = apWinPct(catAllplay, t.team_key) * 100
    const ar = actualRecord[t.team_key] ?? { wins: 0, losses: 0, ties: 0 }
    const actTotal = ar.wins + ar.losses + ar.ties
    const actPct = actTotal ? (ar.wins + 0.5 * ar.ties) / actTotal * 100 : 50
    return { x: apPct, y: actPct, name: t.team_name, team_key: t.team_key, is_mine: t.is_mine }
  }), [teams, catAllplay, actualRecord])

  const min = 30, max = 70
  const diagData = [{ x: min, y: min }, { x: max, y: max }]

  const CustomDot = (props) => {
    const { cx, cy, payload } = props
    const color = colors[payload.team_key]
    return (
      <g>
        <circle cx={cx} cy={cy} r={7} fill={color} fillOpacity={0.8} stroke={color} strokeWidth={1.5} />
        <text x={cx + 10} y={cy + 4} fontSize={10} fill={color}>{payload.name}</text>
      </g>
    )
  }

  return (
    <div className="space-y-3">
      <div className="flex gap-6 text-xs text-slate-500">
        <span><span className="text-field-400">Above diagonal</span> = lucky (good record vs weaker all-play)</span>
        <span><span className="text-stitch-400">Below diagonal</span> = unlucky (poor record vs stronger all-play)</span>
      </div>
      <ResponsiveContainer width="100%" height={400}>
        <ScatterChart margin={{ top: 8, right: 32, left: 0, bottom: 20 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e3a5f" />
          <XAxis type="number" dataKey="x" name="All-Play Win%" domain={[25, 75]}
            tick={{ fill: '#64748b', fontSize: 11 }}
            label={{ value: 'All-Play Win%  (true strength)', position: 'insideBottom', offset: -12, fill: '#475569', fontSize: 11 }} />
          <YAxis type="number" dataKey="y" name="Actual Schedule Win%" domain={[0, 100]}
            tick={{ fill: '#64748b', fontSize: 11 }}
            label={{ value: 'Actual Schedule Win%', angle: -90, position: 'insideLeft', offset: 12, fill: '#475569', fontSize: 11 }} />
          <Tooltip
            contentStyle={tooltipStyle}
            formatter={(val, name) => [`${val.toFixed(1)}%`, name]}
            cursor={{ strokeDasharray: '3 3', stroke: '#334155' }}
          />
          {/* Diagonal "fair" line */}
          <Scatter data={diagData} line={{ stroke: '#475569', strokeDasharray: '5 5', strokeWidth: 1 }} shape={() => null} legendType="none" />
          {/* Team dots */}
          <Scatter data={scatterData} shape={<CustomDot />} isAnimationActive={false} />
        </ScatterChart>
      </ResponsiveContainer>
      <p className="text-xs text-slate-600">
        X-axis = all-play win% (how you would do vs every team every week — eliminates schedule luck).
        Y-axis = actual matchup win% from your real schedule. The dashed line = exactly on skill.
      </p>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Chart 4 — Category Trend Lines
// ────────────────────────────────────────────────────────────────────────────
function CategoryTrends({ teams, activeCats, weeklyStats, currentWeek, colors }) {
  const [selectedCat, setSelectedCat] = useState(activeCats[0] ?? '')
  const [visible, setVisible] = useState(() => new Set(teams.map(t => t.team_key)))
  const toggle = k => setVisible(prev => {
    const n = new Set(prev)
    n.has(k) ? (n.size > 1 && n.delete(k)) : n.add(k)
    return n
  })

  const chartData = useMemo(() => {
    if (!selectedCat) return []
    return Array.from({ length: currentWeek }, (_, i) => {
      const w = i + 1
      const row = { week: w }
      teams.forEach(t => {
        const val = weeklyStats[t.team_key]?.[String(w)]?.[selectedCat]
        if (val != null) row[t.team_key] = Number(val)
      })
      return row
    })
  }, [teams, weeklyStats, selectedCat, currentWeek])

  const isLower = selectedCat === 'ERA' || selectedCat === 'WHIP'
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
        <select
          value={selectedCat}
          onChange={e => setSelectedCat(e.target.value)}
          className="text-sm bg-navy-800 border border-navy-600 rounded px-2 py-1.5 text-slate-200 focus:outline-none focus:ring-1 focus:ring-leather-400"
        >
          {activeCats.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
        {isLower && <span className="text-xs text-slate-500 italic">lower is better</span>}
      </div>
      <TeamToggles teams={teams} colors={colors} visible={visible} onToggle={toggle} />
      <ResponsiveContainer width="100%" height={340}>
        <LineChart data={chartData} margin={{ top: 4, right: 16, left: 0, bottom: 4 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e3a5f" />
          <XAxis dataKey="week" tick={{ fill: '#64748b', fontSize: 11 }} label={{ value: 'Week', position: 'insideBottom', offset: -2, fill: '#475569', fontSize: 11 }} />
          <YAxis tick={{ fill: '#64748b', fontSize: 11 }} tickFormatter={fmt} />
          <Tooltip contentStyle={tooltipStyle} labelFormatter={w => `Week ${w}`} formatter={v => [fmt(v)]} />
          {teams.filter(t => visible.has(t.team_key)).map(t => (
            <Line
              key={t.team_key}
              type="monotone"
              dataKey={t.team_key}
              name={t.team_name}
              stroke={colors[t.team_key]}
              strokeWidth={2}
              dot={{ r: 3, fill: colors[t.team_key] }}
              connectNulls={false}
              isAnimationActive={false}
            />
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
  function cell(rowKey, colKey) {
    if (rowKey === colKey) return null
    return h2hResults[rowKey]?.[colKey] ?? { wins: 0, losses: 0, ties: 0 }
  }
  function winPct(d) {
    if (!d) return null
    const t = d.wins + d.losses + d.ties
    return t ? (d.wins + 0.5 * d.ties) / t : null
  }
  function bg(pct) {
    if (pct == null) return '#0f172a'
    const h = Math.round(pct * 120)
    return `hsl(${h}, 50%, 18%)`
  }

  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        Row team's all-play W-L-T vs column team (summed across all weeks). <span className="text-field-400">Green</span> = row team dominates.
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
                    <td key={colT.team_key}
                      className="px-2 py-2 text-center tabular-nums font-medium"
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

  const chartData = useMemo(() => {
    return Array.from({ length: currentWeek }, (_, i) => {
      const w = i + 1
      const wk = weeklyAllplay[selectedKey]?.[String(w)]
      const margin = wk ? wk.wins - wk.losses : 0
      return {
        week: `Wk ${w}`,
        pos: Math.max(0, margin),
        neg: Math.min(0, margin),
        margin,
        wins: wk?.wins ?? 0,
        losses: wk?.losses ?? 0,
        ties: wk?.ties ?? 0,
      }
    })
  }, [selectedKey, weeklyAllplay, currentWeek])

  const color = colors[selectedKey]

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <label className="text-xs text-slate-500 uppercase tracking-wide">Team</label>
        <select
          value={selectedKey}
          onChange={e => setSelectedKey(e.target.value)}
          className="text-sm bg-navy-800 border border-navy-600 rounded px-2 py-1.5 text-slate-200 focus:outline-none focus:ring-1 focus:ring-leather-400"
        >
          {teams.map(t => <option key={t.team_key} value={t.team_key}>{t.team_name}{t.is_mine ? ' (Me)' : ''}</option>)}
        </select>
      </div>
      <p className="text-xs text-slate-500">
        Weekly all-play margin (category wins − losses vs every other team). Green = net positive week, red = net negative.
      </p>
      <ResponsiveContainer width="100%" height={300}>
        <BarChart data={chartData} margin={{ top: 4, right: 16, left: 0, bottom: 4 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e3a5f" vertical={false} />
          <XAxis dataKey="week" tick={{ fill: '#64748b', fontSize: 11 }} />
          <YAxis tick={{ fill: '#64748b', fontSize: 11 }} />
          <Tooltip
            contentStyle={tooltipStyle}
            formatter={(val, key, props) => {
              const d = props.payload
              return [`${d.wins}W - ${d.losses}L - ${d.ties}T  (margin: ${d.margin > 0 ? '+' : ''}${d.margin})`, 'Result']
            }}
          />
          <ReferenceLine y={0} stroke="#334155" strokeWidth={1.5} />
          <Bar dataKey="pos" name="Positive" radius={[3, 3, 0, 0]} isAnimationActive={false}>
            {chartData.map((d, i) => <Cell key={i} fill="#4ade80" fillOpacity={0.8} />)}
          </Bar>
          <Bar dataKey="neg" name="Negative" radius={[0, 0, 3, 3]} isAnimationActive={false}>
            {chartData.map((d, i) => <Cell key={i} fill="#f87171" fillOpacity={0.8} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Chart 7 — Monte Carlo Projected Standings
// ────────────────────────────────────────────────────────────────────────────
function MonteCarloChart({ colors }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState(null)
  const [fetched, setFetched] = useState(false)

  const load = useCallback(async () => {
    setLoading(true); setErr(null)
    try {
      const d = await getMonteCarlo(22)
      setData(d); setFetched(true)
    } catch (e) { setErr(e.message) }
    finally { setLoading(false) }
  }, [])

  if (!fetched && !loading) {
    return (
      <div className="flex flex-col items-center justify-center py-20 gap-4">
        <p className="text-sm text-slate-400 text-center max-w-sm">
          Simulates the remainder of the season 1,000 times using each team&apos;s current all-play win rate as their strength.
        </p>
        <button onClick={load}
          className="px-5 py-2.5 bg-field-700 hover:bg-field-600 text-white text-sm font-semibold rounded-lg transition-colors">
          Run Simulation
        </button>
      </div>
    )
  }
  if (loading) return <div className="flex items-center justify-center py-20 gap-3 text-slate-400"><Spinner /><span>Simulating 1,000 seasons…</span></div>
  if (err) return <ErrorBanner message={err} />
  if (!data) return null

  const { teams, finish_probs, remaining_weeks, simulations } = data
  const n = teams.length
  const topN = Math.ceil(n / 3)
  const botN = Math.ceil(n / 3)

  // Build stacked bar data: P(top third), P(middle third), P(bottom third)
  const barData = teams.map(t => {
    const probs = finish_probs[t.team_key] ?? []
    const top = probs.slice(0, topN).reduce((s, p) => s + p, 0)
    const bot = probs.slice(n - botN).reduce((s, p) => s + p, 0)
    const mid = 1 - top - bot
    return {
      name: t.team_name.length > 10 ? t.team_name.slice(0, 10) + '…' : t.team_name,
      team_key: t.team_key,
      top: Math.round(top * 100),
      mid: Math.round(mid * 100),
      bot: Math.round(bot * 100),
    }
  })

  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500">
        {simulations.toLocaleString()} simulations · {remaining_weeks} weeks remaining of 22.
        Probability each team finishes in the <span className="text-field-400">top third</span>, <span className="text-amber-400">middle</span>, or <span className="text-stitch-400">bottom third</span>.
      </p>
      <ResponsiveContainer width="100%" height={340}>
        <BarChart data={barData} margin={{ top: 4, right: 16, left: 0, bottom: 60 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e3a5f" vertical={false} />
          <XAxis dataKey="name" tick={{ fill: '#64748b', fontSize: 10 }} angle={-35} textAnchor="end" interval={0} />
          <YAxis tick={{ fill: '#64748b', fontSize: 11 }} unit="%" />
          <Tooltip contentStyle={tooltipStyle} formatter={(v, name) => [`${v}%`, name]} />
          <Bar dataKey="top" name="Top third" stackId="a" fill="#4ade80" fillOpacity={0.85} radius={[3, 3, 0, 0]} isAnimationActive={false} />
          <Bar dataKey="mid" name="Middle" stackId="a" fill="#fbbf24" fillOpacity={0.75} isAnimationActive={false} />
          <Bar dataKey="bot" name="Bottom third" stackId="a" fill="#f87171" fillOpacity={0.85} radius={[0, 0, 3, 3]} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
      <button onClick={load}
        className="text-xs text-slate-500 hover:text-slate-300 underline transition-colors">
        Re-run simulation
      </button>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Chart 8 — Category Radar (Spider Chart)
// ────────────────────────────────────────────────────────────────────────────
function CategoryRadar({ teams, activeCats, catAllplay, colors }) {
  const [visible, setVisible] = useState(() => new Set(teams.map(t => t.team_key)))
  const toggle = k => setVisible(prev => {
    const n = new Set(prev)
    n.has(k) ? (n.size > 1 && n.delete(k)) : n.add(k)
    return n
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
    <div className="space-y-4">
      <p className="text-xs text-slate-500">
        All-play win% per scoring category. Outer edge = 100% (dominating that category), center = 0%.
      </p>
      <TeamToggles teams={teams} colors={colors} visible={visible} onToggle={toggle} />
      <ResponsiveContainer width="100%" height={400}>
        <RadarChart data={radarData} cx="50%" cy="50%" outerRadius="72%">
          <PolarGrid stroke="#1e3a5f" />
          <PolarAngleAxis dataKey="cat" tick={{ fill: '#64748b', fontSize: 11 }} />
          <Tooltip contentStyle={tooltipStyle} formatter={v => [`${v}%`]} />
          {teams.filter(t => visible.has(t.team_key)).map(t => (
            <Radar
              key={t.team_key}
              name={t.team_name}
              dataKey={t.team_key}
              stroke={colors[t.team_key]}
              fill={colors[t.team_key]}
              fillOpacity={0.08}
              strokeWidth={1.5}
              isAnimationActive={false}
            />
          ))}
        </RadarChart>
      </ResponsiveContainer>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Chart 10 — Weekly Volatility
// ────────────────────────────────────────────────────────────────────────────
function VolatilityChart({ teams, weeklyAllplay, currentWeek, colors }) {
  const barData = useMemo(() => {
    return teams.map(t => {
      const weeks = Array.from({ length: currentWeek }, (_, i) => {
        const wk = weeklyAllplay[t.team_key]?.[String(i + 1)]
        return wk ? wk.wins * 2 + wk.ties : 0
      })
      const avg = weeks.reduce((s, v) => s + v, 0) / (weeks.length || 1)
      const variance = weeks.reduce((s, v) => s + (v - avg) ** 2, 0) / (weeks.length || 1)
      const stddev = Math.sqrt(variance)
      return {
        name: t.team_name.length > 10 ? t.team_name.slice(0, 10) + '…' : t.team_name,
        team_key: t.team_key,
        stddev: Math.round(stddev * 10) / 10,
        avg: Math.round(avg * 10) / 10,
      }
    }).sort((a, b) => b.stddev - a.stddev)
  }, [teams, weeklyAllplay, currentWeek])

  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        Standard deviation of weekly all-play points (wins×2 + ties×1). High = boom-or-bust. Low = consistent. Number above bar = average weekly points.
      </p>
      <ResponsiveContainer width="100%" height={320}>
        <BarChart data={barData} margin={{ top: 20, right: 16, left: 0, bottom: 60 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#1e3a5f" vertical={false} />
          <XAxis dataKey="name" tick={{ fill: '#64748b', fontSize: 10 }} angle={-35} textAnchor="end" interval={0} />
          <YAxis tick={{ fill: '#64748b', fontSize: 11 }} label={{ value: 'Std Dev (pts)', angle: -90, position: 'insideLeft', offset: 10, fill: '#475569', fontSize: 10 }} />
          <Tooltip
            contentStyle={tooltipStyle}
            formatter={(val, key, props) => {
              const d = props.payload
              return [`Std Dev: ${d.stddev}  ·  Avg: ${d.avg} pts/wk`, d.name]
            }}
          />
          <Bar dataKey="stddev" radius={[4, 4, 0, 0]} isAnimationActive={false} label={{ position: 'top', fill: '#64748b', fontSize: 10, formatter: (v, i, d) => barData[i]?.avg }}>
            {barData.map((d, i) => <Cell key={i} fill={colors[d.team_key]} fillOpacity={0.75} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
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

        {/* ── Header ── */}
        <div>
          <h1 className="text-xl font-bold text-slate-100 flex items-center gap-2">
            <BarChart2 className="w-5 h-5 text-leather-400" />
            Visual League Data
          </h1>
          <p className="text-sm text-slate-500 mt-0.5">League-wide analytics and data visualizations</p>
        </div>

        {/* ── Tab bar ── */}
        <div className="flex gap-0.5 border-b border-navy-700 overflow-x-auto scrollbar-none">
          {TABS.map(tab => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-1.5 px-3 py-2 text-xs font-medium whitespace-nowrap transition-colors border-b-2 -mb-px shrink-0 ${
                activeTab === tab.id
                  ? 'border-field-500 text-field-300'
                  : 'border-transparent text-slate-500 hover:text-slate-300'
              }`}
            >
              <tab.icon className="w-3 h-3" />
              {tab.label}
            </button>
          ))}
        </div>

        {/* ── Content ── */}
        {loading ? (
          <div className="flex items-center justify-center py-20 gap-3 text-slate-400">
            <Spinner /><span>Loading league data…</span>
          </div>
        ) : error ? (
          <ErrorBanner message={error} />
        ) : data ? (
          <div className="pt-2">
            {activeTab === 'progression' && (
              <ProgressionChart
                teams={data.teams}
                weeklyAllplay={data.weekly_allplay}
                currentWeek={data.current_week}
                colors={colors}
              />
            )}
            {activeTab === 'heatmap' && (
              <CategoryHeatMap
                teams={data.teams}
                activeCats={data.active_cats}
                catAllplay={data.cat_allplay}
                teamColors={colors}
              />
            )}
            {activeTab === 'luck' && (
              <LuckSkillScatter
                teams={data.teams}
                catAllplay={data.cat_allplay}
                actualRecord={data.actual_record}
                colors={colors}
              />
            )}
            {activeTab === 'trends' && (
              <CategoryTrends
                teams={data.teams}
                activeCats={data.active_cats}
                weeklyStats={data.weekly_stats}
                currentWeek={data.current_week}
                colors={colors}
              />
            )}
            {activeTab === 'h2h' && (
              <H2HMatrix
                teams={data.teams}
                h2hResults={data.h2h_results}
                teamColors={colors}
              />
            )}
            {activeTab === 'waterfall' && (
              <WaterfallChart
                teams={data.teams}
                weeklyAllplay={data.weekly_allplay}
                currentWeek={data.current_week}
                colors={colors}
              />
            )}
            {activeTab === 'montecarlo' && (
              <MonteCarloChart colors={colors} />
            )}
            {activeTab === 'radar' && (
              <CategoryRadar
                teams={data.teams}
                activeCats={data.active_cats}
                catAllplay={data.cat_allplay}
                colors={colors}
              />
            )}
            {activeTab === 'depth' && (
              <ComingSoon
                title="Roster Depth Contribution"
                description="Shows what proportion of each team's category wins come from their top 3 players vs the rest of the roster. Requires per-player roster data from Yahoo — coming soon."
              />
            )}
            {activeTab === 'volatility' && (
              <VolatilityChart
                teams={data.teams}
                weeklyAllplay={data.weekly_allplay}
                currentWeek={data.current_week}
                colors={colors}
              />
            )}
            {activeTab === 'trades' && (
              <ComingSoon
                title="Trade Impact Timeline"
                description="Overlays your league's transaction history on the team progression chart — so you can see whether trades and waiver pickups actually moved the needle. Coming soon."
              />
            )}
          </div>
        ) : null}

      </div>
    </div>
  )
}
