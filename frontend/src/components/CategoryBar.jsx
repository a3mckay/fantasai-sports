/**
 * Shows a horizontal bar chart of category strengths.
 * Values are z-scores; we clamp display to ±3.
 */
export default function CategoryBar({ data = {} }) {
  if (!Object.keys(data).length) return null

  const entries = Object.entries(data).sort((a, b) => b[1] - a[1])
  const max = Math.max(...entries.map(([, v]) => Math.abs(v)), 1)

  return (
    <div className="space-y-1.5">
      {entries.map(([cat, val]) => {
        const pct = Math.min(Math.abs(val) / max, 1) * 100
        const positive = val >= 0
        return (
          <div key={cat} className="flex items-center gap-2 text-xs">
            <span className="w-10 text-right text-slate-500 font-mono shrink-0">{cat}</span>
            <div className="flex-1 h-2 bg-navy-700 rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full transition-all duration-500 ${
                  positive ? 'bg-field-500' : 'bg-stitch-500'
                }`}
                style={{ width: `${pct}%` }}
              />
            </div>
            <span className={`w-12 font-mono text-right ${positive ? 'text-field-400' : 'text-stitch-400'}`}>
              {val >= 0 ? '+' : ''}{val.toFixed(2)}
            </span>
          </div>
        )
      })}
    </div>
  )
}
