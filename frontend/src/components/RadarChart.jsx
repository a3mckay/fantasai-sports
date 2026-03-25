/**
 * Pure-SVG radar/spider chart for team category percentiles.
 *
 * Props:
 *   data        – { [cat]: percentile 0–100, ... }
 *   numTeams    – total teams in league (for legend copy)
 *   asPercentiles – values are already 0-100 (pass true when using league_category_percentiles)
 */

import { useState } from 'react'

// Canonical display order for radar axes
const RADAR_ORDER = ['R', 'HR', 'RBI', 'SB', 'AVG', 'OPS', 'W', 'SV', 'K', 'ERA', 'WHIP']
const SKIP_CATS  = new Set(['H/AB', 'Batting', 'Pitching', 'AB'])

const W = 300, H = 280, CX = 150, CY = 140, R = 95

/** Convert polar coords (angle in degrees, radius) → [x, y] */
function polar(angleDeg, radius) {
  const rad = (angleDeg - 90) * (Math.PI / 180)
  return [CX + radius * Math.cos(rad), CY + radius * Math.sin(rad)]
}

/** Approximate normal CDF for z→percentile conversion */
function normCdf(z) {
  const sign = z < 0 ? -1 : 1
  const x = Math.abs(z) / Math.SQRT2
  const t = 1 / (1 + 0.3275911 * x)
  const poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))))
  const erf = 1 - poly * Math.exp(-x * x)
  return 0.5 * (1 + sign * erf)
}

function toPct(val, asPercentiles) {
  return asPercentiles
    ? Math.min(99, Math.max(1, val))
    : Math.min(99, Math.max(1, normCdf(val) * 100))
}

export default function RadarChart({ data = {}, numTeams = 12, asPercentiles = false }) {
  const [tooltip, setTooltip] = useState(null)

  // Build ordered category list from available data
  const cats = [
    ...RADAR_ORDER.filter(c => data[c] != null && !SKIP_CATS.has(c)),
    ...Object.keys(data).filter(c => !RADAR_ORDER.includes(c) && !SKIP_CATS.has(c) && data[c] != null),
  ]

  if (cats.length < 3) return null

  const n       = cats.length
  const angles  = cats.map((_, i) => (i / n) * 360)
  const pcts    = cats.map(c => toPct(data[c], asPercentiles))

  // Polygon points for this team
  const teamPts  = pcts.map((p, i) => polar(angles[i], (p / 100) * R))
  // 50th-percentile reference ring
  const midPts   = angles.map(a => polar(a, R * 0.5))

  // Grid ring polygons at 25 / 50 / 75 / 100
  const gridRings = [0.25, 0.5, 0.75, 1.0].map(frac =>
    angles.map(a => polar(a, R * frac))
  )

  return (
    <div className="relative">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="w-full max-w-[300px] mx-auto block"
        onMouseLeave={() => setTooltip(null)}
      >
        {/* Grid rings */}
        {gridRings.map((pts, gi) => (
          <polygon
            key={gi}
            points={pts.map(p => p.join(',')).join(' ')}
            fill="none"
            stroke={gi === 1 ? '#2d4a6b' : '#1a3050'}
            strokeWidth={gi === 1 ? 1 : 0.5}
            strokeDasharray={gi === 1 ? '4 3' : undefined}
          />
        ))}

        {/* Axis spokes */}
        {angles.map((a, i) => {
          const [x, y] = polar(a, R)
          return <line key={i} x1={CX} y1={CY} x2={x} y2={y} stroke="#1a3050" strokeWidth="0.7" />
        })}

        {/* 50th-percentile reference outline (dashed) */}
        <polygon
          points={midPts.map(p => p.join(',')).join(' ')}
          fill="none"
          stroke="#334155"
          strokeWidth="1"
          strokeDasharray="3 2"
        />

        {/* Team fill polygon */}
        <polygon
          points={teamPts.map(p => p.join(',')).join(' ')}
          fill="rgba(74, 222, 128, 0.12)"
          stroke="#4ade80"
          strokeWidth="1.5"
          strokeLinejoin="round"
        />

        {/* Dots + hover targets */}
        {cats.map((cat, i) => {
          const [x, y] = teamPts[i]
          const pct    = pcts[i]
          const isStrong = pct >= 75
          const isWeak   = pct <  40
          const dotColor = isStrong ? '#4ade80' : isWeak ? '#f87171' : '#64748b'
          const rank     = numTeams ? Math.max(1, Math.round((1 - pct / 100) * numTeams) + 1) : null

          return (
            <g key={i}>
              {/* invisible larger hit area */}
              <circle
                cx={x} cy={y} r="8"
                fill="transparent"
                onMouseEnter={() => setTooltip({ cat, pct: Math.round(pct), rank, x, y })}
              />
              <circle cx={x} cy={y} r="3.5" fill={dotColor} />
            </g>
          )
        })}

        {/* Axis labels */}
        {cats.map((cat, i) => {
          const labelR  = R + 16
          const [lx, ly] = polar(angles[i], labelR)
          // nudge labels that land near horizontal axis ends
          const angle = angles[i]
          const anchor = angle > 10 && angle < 170 ? 'start'
                       : angle > 190 && angle < 350 ? 'end'
                       : 'middle'
          return (
            <text
              key={i}
              x={lx.toFixed(1)}
              y={ly.toFixed(1)}
              textAnchor={anchor}
              dominantBaseline="middle"
              fontSize="8.5"
              fontFamily="ui-monospace,monospace"
              fill="#94a3b8"
            >
              {cat}
            </text>
          )
        })}

        {/* Centre label */}
        <text x={CX} y={CY - 4}  textAnchor="middle" fontSize="7.5" fill="#334155">50th</text>
        <text x={CX} y={CY + 5}  textAnchor="middle" fontSize="7.5" fill="#334155">%ile</text>
      </svg>

      {/* Tooltip */}
      {tooltip && (
        <div
          className="absolute pointer-events-none z-10 bg-navy-950 border border-navy-600 rounded px-2 py-1 text-xs shadow-lg"
          style={{
            left: `calc(${(tooltip.x / W) * 100}% - 42px)`,
            top:  `calc(${(tooltip.y / H) * 100}% - 48px)`,
          }}
        >
          <span className="font-mono text-white">{tooltip.cat}</span>
          {' '}
          <span className="text-slate-400">{tooltip.pct}th %ile</span>
          {tooltip.rank && (
            <span className="text-slate-600 ml-1">· {tooltip.rank}/{numTeams}</span>
          )}
        </div>
      )}

      {/* Legend */}
      <div className="flex items-center justify-center gap-4 mt-1 text-[10px] text-slate-600">
        <span className="flex items-center gap-1">
          <span className="inline-block w-4 border-t border-dashed border-slate-600" /> avg
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block w-3 h-0.5 rounded bg-[#4ade80]" /> your team
        </span>
      </div>
    </div>
  )
}
