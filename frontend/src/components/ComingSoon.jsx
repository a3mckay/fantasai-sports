import { Clock } from 'lucide-react'

/**
 * Coming Soon placeholder page.
 *
 * Props:
 *   icon    — Lucide icon component (optional)
 *   title   — page title
 *   message — optional description
 */
export default function ComingSoon({ icon: Icon, title, message }) {
  return (
    <div className="flex flex-col items-center justify-center min-h-[50vh] text-center space-y-5 py-16">
      <div className="w-20 h-20 rounded-full bg-navy-800 border border-navy-600 flex items-center justify-center">
        {Icon
          ? <Icon size={36} className="text-slate-500" />
          : <Clock size={36} className="text-slate-500" />
        }
      </div>
      <div>
        <h1 className="text-2xl font-bold text-white mb-2">{title || 'Coming Soon'}</h1>
        <p className="text-slate-500 text-sm max-w-sm">
          {message || "We're working on this feature. Check back soon!"}
        </p>
      </div>
      <div className="flex items-center gap-2 px-4 py-2 rounded-full bg-navy-800 border border-navy-700">
        <Clock size={13} className="text-field-500" />
        <span className="text-xs text-field-400 font-medium">In development</span>
      </div>
    </div>
  )
}
