import { AlertCircle, X } from 'lucide-react'

export default function ErrorBanner({ message, onClose }) {
  if (!message) return null
  return (
    <div className="flex items-start gap-3 p-4 rounded-lg bg-red-950 border border-red-800 text-red-300 text-sm">
      <AlertCircle size={16} className="shrink-0 mt-0.5 text-red-400" />
      <p className="flex-1">{message}</p>
      {onClose && (
        <button onClick={onClose} className="text-red-400 hover:text-red-200 shrink-0">
          <X size={14} />
        </button>
      )}
    </div>
  )
}
