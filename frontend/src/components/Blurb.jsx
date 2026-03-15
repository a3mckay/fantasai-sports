import { Sparkles } from 'lucide-react'

export default function Blurb({ text }) {
  if (!text) return null
  return (
    <div className="p-4 rounded-lg bg-field-950 border border-field-800">
      <div className="flex items-center gap-1.5 text-field-400 text-xs font-semibold uppercase tracking-widest mb-2">
        <Sparkles size={11} />
        AI Analysis
      </div>
      <p className="text-slate-300 text-sm leading-relaxed">{text}</p>
    </div>
  )
}
