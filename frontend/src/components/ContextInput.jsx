import { MessageSquare } from 'lucide-react'

export default function ContextInput({ value, onChange, placeholder }) {
  return (
    <div>
      <label className="section-label flex items-center gap-1.5">
        <MessageSquare size={11} />
        Additional context (optional)
      </label>
      <textarea
        className="field-input resize-none"
        rows={2}
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder || "e.g. 'I'm punting stolen bases' or 'win-now mode, need a closer'"}
      />
      <p className="text-xs text-slate-600 mt-1">
        Free-text hints that help the AI tailor its analysis to your situation.
      </p>
    </div>
  )
}
