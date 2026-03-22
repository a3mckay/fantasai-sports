import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { X } from 'lucide-react'

const FEATURE_LABELS = {
  'compare': 'Player Comparison',
  'trade': 'Trade Evaluation',
  'find-player': 'Find a Player',
  'team-eval': 'Team Evaluation',
  'keeper-eval': 'Keeper Planning',
  'compare-teams': 'Compare Teams',
  'league-power': 'League Power',
  'extract-players': 'Player Extraction',
}

export default function MeterBanner() {
  const [banner, setBanner] = useState(null) // {feature}
  const navigate = useNavigate()

  useEffect(() => {
    function onMeteringLimit(e) {
      setBanner({ feature: e.detail?.feature || 'this feature' })
    }
    window.addEventListener('metering:limit', onMeteringLimit)
    return () => window.removeEventListener('metering:limit', onMeteringLimit)
  }, [])

  if (!banner) return null

  const featureLabel = FEATURE_LABELS[banner.feature] || banner.feature

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="relative bg-navy-900 border border-navy-600 rounded-xl shadow-2xl p-8 max-w-md w-full mx-4">
        <button
          onClick={() => setBanner(null)}
          className="absolute top-4 right-4 text-slate-500 hover:text-slate-300"
        >
          <X size={18} />
        </button>

        <div className="text-center">
          <div className="text-4xl mb-4">⚾</div>
          <h2 className="text-xl font-bold text-white mb-2">
            Free limit reached
          </h2>
          <p className="text-slate-400 text-sm mb-6">
            You've used your free <span className="text-field-400 font-medium">{featureLabel}</span> for today.
            Create a free account to get unlimited access.
          </p>

          <div className="flex flex-col gap-3">
            <button
              onClick={() => { setBanner(null); navigate('/login') }}
              className="w-full bg-field-600 hover:bg-field-500 text-white font-semibold py-2.5 px-4 rounded-lg transition-colors"
            >
              Sign in or create account
            </button>
            <button
              onClick={() => setBanner(null)}
              className="text-slate-500 hover:text-slate-300 text-sm transition-colors"
            >
              Maybe later
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
