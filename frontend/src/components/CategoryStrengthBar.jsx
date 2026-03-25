/**
 * Category strength bar for Team Evaluation.
 *
 * Groups categories into Batting / Pitching subheaders,
 * shows a percentile bar and league rank (e.g. "3rd of 12"),
 * and filters out Yahoo composite labels (H/AB, Batting, Pitching).
 */

/** Ordered batting categories (subset we recognise). */
const BATTING_ORDER = ['R', 'HR', 'RBI', 'SB', 'AVG', 'OPS', 'OBP', 'H', 'BB']
/** Ordered pitching categories (subset we recognise). */
const PITCHING_ORDER = ['IP', 'W', 'SV', 'K', 'ERA', 'WHIP', 'HLD', 'QS', 'K/BB']
/** Yahoo composite/slot labels — never display these. */
const SKIP_CATS = new Set(['H/AB', 'Batting', 'Pitching', 'AB'])

function ordinal(n) {
  const mod100 = n % 100
  if (mod100 >= 11 && mod100 <= 13) return `${n}th`
  switch (n % 10) {
    case 1: return `${n}st`
    case 2: return `${n}nd`
    case 3: return `${n}rd`
    default: return `${n}th`
  }
}

function CatRow({ cat, pct, numTeams }) {
  const isStrong = pct >= 75
  const isWeak   = pct < 40
  const barColor  = isStrong ? 'bg-field-500' : isWeak ? 'bg-stitch-500' : 'bg-slate-600'
  const textColor = isStrong ? 'text-field-400' : isWeak ? 'text-stitch-400' : 'text-slate-400'

  // League rank: if pct=69 of 12 teams, rank ≈ round((1 - pct/100) * numTeams) + 1
  // Clamp to [1, numTeams] — near-zero percentiles otherwise produce numTeams+1
  const rank = numTeams ? Math.min(numTeams, Math.max(1, Math.round((1 - pct / 100) * numTeams) + 1)) : null

  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="w-10 text-right text-slate-500 font-mono shrink-0">{cat}</span>
      <div className="flex-1 h-2 bg-navy-700 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-500 ${barColor}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className={`w-20 font-mono text-right ${textColor}`}>
        {ordinal(Math.round(pct))} <span className="text-slate-600">%ile</span>
      </span>
      {rank && numTeams ? (
        <span className="w-14 text-right text-slate-600 text-[10px] shrink-0">
          {rank}/{numTeams}
        </span>
      ) : null}
    </div>
  )
}

export default function CategoryStrengthBar({ data = {}, numTeams = 12, asPercentiles = false }) {
  if (!Object.keys(data).length) return null

  // Normalise: if asPercentiles the values are already 0–100; otherwise treat as z-scores
  function normCdf(z) {
    const sign = z < 0 ? -1 : 1
    const x = Math.abs(z) / Math.SQRT2
    const t = 1 / (1 + 0.3275911 * x)
    const poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))))
    const erf = 1 - poly * Math.exp(-x * x)
    return 0.5 * (1 + sign * erf)
  }
  function toPct(val) {
    return asPercentiles
      ? Math.round(Math.min(99, Math.max(1, val)))
      : Math.round(Math.min(99, Math.max(1, normCdf(val) * 100)))
  }

  // Split into batting / pitching / other, filtering junk
  const entries = Object.entries(data).filter(([cat]) => !SKIP_CATS.has(cat))

  const batting  = BATTING_ORDER .filter(c => data[c] != null && !SKIP_CATS.has(c))
  const pitching = PITCHING_ORDER.filter(c => data[c] != null && !SKIP_CATS.has(c))
  const knownSet = new Set([...batting, ...pitching])
  const other    = entries.filter(([c]) => !knownSet.has(c)).map(([c]) => c)

  function Section({ label, cats }) {
    if (!cats.length) return null
    return (
      <div className="space-y-1.5">
        <div className="text-[10px] text-slate-600 font-semibold uppercase tracking-wider pt-1">
          {label}
        </div>
        {cats.map(cat => (
          <CatRow key={cat} cat={cat} pct={toPct(data[cat])} numTeams={numTeams} />
        ))}
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <Section label="Batting"  cats={batting}  />
      <Section label="Pitching" cats={pitching} />
      {other.length > 0 && <Section label="Other" cats={other} />}
    </div>
  )
}
