/**
 * Category bar that displays z-scores as percentile ranks (1–99).
 *
 * z → percentile via the standard normal CDF approximation so that:
 *   z = 0    → 50th percentile
 *   z = +2   → ~97th percentile
 *   z = -2   → ~3rd percentile
 *
 * Bar width is proportional to percentile (100% = 99th, 0% = 1st).
 * Values at/above 75th are green; below 40th are red; in-between are slate.
 */

/** Abramowitz & Stegun approximation of the standard normal CDF (error < 7.5e-8). */
function normCdf(z) {
  const sign = z < 0 ? -1 : 1
  const x = Math.abs(z) / Math.SQRT2
  const t = 1 / (1 + 0.3275911 * x)
  const poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))))
  const erf = 1 - poly * Math.exp(-x * x)
  return 0.5 * (1 + sign * erf)
}

function zToPercentile(z) {
  return Math.round(Math.min(99, Math.max(1, normCdf(z) * 100)))
}

/** Returns the correct ordinal suffix for any integer: 1→st, 2→nd, 3→rd, else→th. */
function ordinal(n) {
  const mod100 = n % 100
  // 11, 12, 13 are exceptions (11th, 12th, 13th — not 11st/12nd/13rd)
  if (mod100 >= 11 && mod100 <= 13) return `${n}th`
  switch (n % 10) {
    case 1: return `${n}st`
    case 2: return `${n}nd`
    case 3: return `${n}rd`
    default: return `${n}th`
  }
}

export default function PercentileBar({ data = {}, asPercentiles = false }) {
  if (!Object.keys(data).length) return null

  // When asPercentiles=true, values are already 0–100 league percentile ranks.
  // Otherwise interpret as z-scores and convert via the normal CDF.
  const entries = Object.entries(data)
    .map(([cat, val]) => {
      const pct = asPercentiles
        ? Math.round(Math.min(99, Math.max(1, val)))
        : zToPercentile(val)
      return [cat, val, pct]
    })
    .sort((a, b) => Math.abs(b[2] - 50) - Math.abs(a[2] - 50))

  return (
    <div className="space-y-1.5">
      {entries.map(([cat, _z, pct]) => {
        const isStrong = pct >= 75
        const isWeak   = pct < 40
        const barColor  = isStrong ? 'bg-field-500' : isWeak ? 'bg-stitch-500' : 'bg-slate-600'
        const textColor = isStrong ? 'text-field-400' : isWeak ? 'text-stitch-400' : 'text-slate-400'

        return (
          <div key={cat} className="flex items-center gap-2 text-xs">
            <span className="w-10 text-right text-slate-500 font-mono shrink-0">{cat}</span>
            <div className="flex-1 h-2 bg-navy-700 rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full transition-all duration-500 ${barColor}`}
                style={{ width: `${pct}%` }}
              />
            </div>
            <span className={`w-16 font-mono text-right ${textColor}`}>
              {ordinal(pct)} <span className="text-slate-600">%ile</span>
            </span>
          </div>
        )
      })}
    </div>
  )
}
