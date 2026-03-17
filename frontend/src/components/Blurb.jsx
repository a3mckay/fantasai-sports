import { Sparkles } from 'lucide-react'

/**
 * Converts a small subset of Markdown to React elements:
 *   **bold**  →  <strong>
 *   *italic*  →  <em>
 * Handles multiple occurrences in a single string without a library.
 */
function renderMarkdown(text) {
  const parts = text.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/g)
  return parts.map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**'))
      return <strong key={i} className="text-white font-semibold">{part.slice(2, -2)}</strong>
    if (part.startsWith('*') && part.endsWith('*'))
      return <em key={i}>{part.slice(1, -1)}</em>
    return part
  })
}

export default function Blurb({ text }) {
  if (!text) return null
  return (
    <div className="p-4 rounded-lg bg-field-950 border border-field-800">
      <div className="flex items-center gap-1.5 text-field-400 text-xs font-semibold uppercase tracking-widest mb-2">
        <Sparkles size={11} />
        AI Analysis
      </div>
      <p className="text-slate-300 text-sm leading-relaxed">{renderMarkdown(text)}</p>
    </div>
  )
}
