/**
 * CategoryPills — shared strong/weak category pill strip.
 *
 * Single source of truth for the ▲/▼ category pills shown across
 * LeaguePower, CompareTeams, and TeamEval.
 *
 * Usage:
 *   // Preferred — derive pills from within-league percentiles (consistent with bars)
 *   <CategoryPills percentiles={{ R: 82, HR: 36, SV: 99, IP: 1, … }} />
 *
 *   // Fallback — explicit arrays (e.g. no league context available)
 *   <CategoryPills strongCats={['OPS','ERA']} weakCats={['SB','IP']} />
 *
 * When `percentiles` is supplied it always takes priority over
 * `strongCats`/`weakCats`.  Strong = ≥70th %ile, weak = ≤30th %ile,
 * sorted by magnitude so the most extreme show first.
 */

const SKIP_CATS = new Set(['H/AB', 'Batting', 'Pitching', 'AB'])

function derivePills(percentiles) {
  const entries = Object.entries(percentiles).filter(([c]) => !SKIP_CATS.has(c))
  const strong = entries
    .filter(([, p]) => p >= 70)
    .sort((a, b) => b[1] - a[1])
    .map(([c]) => c)
  const weak = entries
    .filter(([, p]) => p <= 30)
    .sort((a, b) => a[1] - b[1])
    .map(([c]) => c)
  return { strong, weak }
}

export default function CategoryPills({
  percentiles  = null,
  strongCats   = [],
  weakCats     = [],
  maxStrong    = 4,
  maxWeak      = 3,
  className    = '',
}) {
  const { strong, weak } = percentiles
    ? derivePills(percentiles)
    : {
        strong: strongCats.filter(c => !SKIP_CATS.has(c)),
        weak:   weakCats.filter(c => !SKIP_CATS.has(c)),
      }

  if (!strong.length && !weak.length) return null

  return (
    <div className={`flex flex-wrap gap-1 ${className}`}>
      {strong.slice(0, maxStrong).map(c => (
        <span key={c} className="stat-pill bg-field-900 text-field-300 text-[10px]">{c} ▲</span>
      ))}
      {weak.slice(0, maxWeak).map(c => (
        <span key={c} className="stat-pill bg-red-950/50 text-red-400 text-[10px]">{c} ▼</span>
      ))}
    </div>
  )
}
