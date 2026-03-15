import { ThumbsUp, ThumbsDown } from 'lucide-react'

export default function ProsCons({ pros = [], cons = [] }) {
  if (!pros.length && !cons.length) return null
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
      {pros.length > 0 && (
        <div className="p-4 rounded-lg bg-field-950 border border-field-800">
          <div className="flex items-center gap-1.5 text-field-400 text-xs font-semibold uppercase tracking-widest mb-3">
            <ThumbsUp size={11} />
            Strengths
          </div>
          <ul className="space-y-1.5">
            {pros.map((p, i) => (
              <li key={i} className="flex items-start gap-2 text-sm text-slate-300">
                <span className="text-field-400 mt-0.5">+</span>
                {p}
              </li>
            ))}
          </ul>
        </div>
      )}
      {cons.length > 0 && (
        <div className="p-4 rounded-lg bg-red-950/40 border border-red-900/60">
          <div className="flex items-center gap-1.5 text-red-400 text-xs font-semibold uppercase tracking-widest mb-3">
            <ThumbsDown size={11} />
            Concerns
          </div>
          <ul className="space-y-1.5">
            {cons.map((c, i) => (
              <li key={i} className="flex items-start gap-2 text-sm text-slate-300">
                <span className="text-red-400 mt-0.5">−</span>
                {c}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
