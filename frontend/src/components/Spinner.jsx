export default function Spinner({ size = 'md', className = '' }) {
  const s = { sm: 'w-4 h-4', md: 'w-6 h-6', lg: 'w-10 h-10' }[size]
  return (
    <div className={`${s} rounded-full border-2 border-field-700 border-t-field-400 animate-spin ${className}`} />
  )
}

export function LoadingState({ message = 'Analysing…' }) {
  return (
    <div className="flex flex-col items-center justify-center gap-4 py-20 text-slate-400">
      <Spinner size="lg" />
      <p className="text-sm animate-pulse">{message}</p>
    </div>
  )
}
