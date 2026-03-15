import { Link } from 'react-router-dom'
import { BarChart2, ArrowLeftRight, Search, Star, Users, Trophy, Zap, ChevronRight } from 'lucide-react'

const FEATURES = [
  {
    to: '/compare',
    icon: BarChart2,
    color: 'field',
    title: 'Compare Players',
    description: 'Rank 2+ players head-to-head. Factor in your category priorities with a quick note.',
  },
  {
    to: '/trade',
    icon: ArrowLeftRight,
    color: 'leather',
    title: 'Evaluate Trade',
    description: 'Get a fair / favor-receive / favor-give verdict. Accounts for talent density — one star beats five fillers.',
  },
  {
    to: '/find-player',
    icon: Search,
    color: 'stitch',
    title: 'Find a Player',
    description: 'Tell us the slot you need to fill. We surface the best available pick and track history so you never get the same name twice.',
  },
  {
    to: '/team-eval',
    icon: Star,
    color: 'field',
    title: 'Team Evaluation',
    description: 'Letter grade (A–F), position-by-position breakdown, and concrete improvement suggestions.',
  },
  {
    to: '/keeper-eval',
    icon: Trophy,
    color: 'leather',
    title: 'Keeper Planning',
    description: 'Evaluate your keeper core or let the AI decide who to keep from your full roster. Includes draft target profiles.',
  },
  {
    to: '/compare-teams',
    icon: Users,
    color: 'stitch',
    title: 'Compare Teams',
    description: 'Side-by-side power scores and category profiles for 2–6 teams, with trade opportunities automatically surfaced.',
  },
  {
    to: '/league-power',
    icon: Zap,
    color: 'field',
    title: 'League Power Rankings',
    description: 'Full league standings by roster strength. Contenders, middle of the pack, and rebuilders — plus the top trade pairings.',
  },
]

const COLOR_MAP = {
  field:   'bg-field-900 border-field-700 text-field-400',
  leather: 'bg-leather-500/10 border-leather-500/30 text-leather-400',
  stitch:  'bg-stitch-500/10 border-stitch-500/30 text-stitch-400',
}

export default function Home() {
  return (
    <div className="space-y-12">
      {/* Hero */}
      <div className="text-center space-y-4 pt-4">
        <div className="text-6xl mb-4">⚾</div>
        <h1 className="text-4xl font-bold text-white tracking-tight">
          Fanta<span className="text-field-400">sAI</span> Sports
        </h1>
        <p className="text-slate-400 text-lg max-w-xl mx-auto text-balance">
          AI-powered MLB fantasy baseball analysis. Every decision — trades, waivers,
          keepers — backed by data and explained in plain English.
        </p>
        {/* Decorative stitching lines */}
        <div className="flex items-center justify-center gap-2 pt-2">
          <div className="h-px w-16 bg-gradient-to-r from-transparent to-stitch-500/40" />
          <div className="w-1.5 h-1.5 rounded-full bg-stitch-500/60" />
          <div className="w-1.5 h-1.5 rounded-full bg-stitch-500/60" />
          <div className="h-px w-16 bg-gradient-to-l from-transparent to-stitch-500/40" />
        </div>
      </div>

      {/* Feature grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {FEATURES.map(({ to, icon: Icon, color, title, description }) => (
          <Link
            key={to}
            to={to}
            className="group card hover:border-navy-600 transition-all duration-200 hover:shadow-lg hover:shadow-black/20 hover:-translate-y-0.5"
          >
            <div className="flex items-start gap-4">
              <div className={`p-2.5 rounded-lg border ${COLOR_MAP[color]} shrink-0 mt-0.5`}>
                <Icon size={16} />
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center justify-between gap-2">
                  <h2 className="font-semibold text-white text-sm">{title}</h2>
                  <ChevronRight
                    size={14}
                    className="text-slate-600 group-hover:text-slate-400 shrink-0 transition-colors"
                  />
                </div>
                <p className="text-slate-500 text-xs mt-1 leading-relaxed">{description}</p>
              </div>
            </div>
          </Link>
        ))}
      </div>

      {/* Footer note */}
      <div className="text-center text-slate-700 text-xs pb-4">
        Rankings powered by FanGraphs data · Analysis by Claude AI
      </div>
    </div>
  )
}
