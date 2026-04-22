import { Link } from 'react-router-dom'
import { TrendingUp, BarChart2, ArrowLeftRight, Star, Users, Trophy, Search, Zap, Swords, ScrollText, Grid3X3, ChevronRight, RefreshCw } from 'lucide-react'
import { useLeague } from '../contexts/LeagueContext'

// Order mirrors the left-hand nav
const FEATURES = [
  {
    to: '/rankings',
    icon: TrendingUp,
    color: 'field',
    title: 'Rankings',
    description: 'Predictive and current-season player rankings powered by advanced metrics and FanGraphs data.',
  },
  {
    to: '/scoring-grid',
    icon: Grid3X3,
    color: 'field',
    title: 'League Scoring Grid',
    description: "Every team's weekly stats side-by-side. See how you stack up in each category against the whole league.",
  },
  {
    to: '/league-power',
    icon: Zap,
    color: 'field',
    title: 'League Power Rankings',
    description: 'Full league standings by roster strength. Contenders, middle of the pack, and rebuilders — plus the top trade pairings.',
  },
  {
    to: '/transactions',
    icon: ScrollText,
    color: 'leather',
    title: 'Move Grades',
    description: 'Every add, drop, and trade in your league graded A–F by Claude. See who won the waiver wire this week.',
  },
  {
    to: '/find-player',
    icon: Search,
    color: 'leather',
    title: 'Recommend a Player',
    description: 'Tell us the slot you need to fill. We surface the best available pick and track history so you never get the same name twice.',
  },
  {
    to: '/team-eval',
    icon: Star,
    color: 'leather',
    title: 'Team Eval',
    description: 'Letter grade (A–F), position-by-position breakdown, and concrete improvement suggestions.',
  },
  {
    to: '/trade',
    icon: ArrowLeftRight,
    color: 'field',
    title: 'Evaluate Trade',
    description: 'Get a fair / favor-receive / favor-give verdict. Accounts for talent density — one star beats five fillers.',
  },
  {
    to: '/explore',
    icon: BarChart2,
    color: 'stitch',
    title: 'Explore Players',
    description: 'Deep-dive on up to 5 players — stats, injury info, schedule context, and AI analyst chat. Compare head-to-head too.',
  },
  {
    to: '/compare-teams',
    icon: Users,
    color: 'stitch',
    title: 'Compare Teams',
    description: 'Side-by-side power scores and category profiles for 2–6 teams, with trade opportunities automatically surfaced.',
  },
  {
    to: '/matchups',
    icon: Swords,
    color: 'stitch',
    title: 'Matchup Analyzer',
    description: 'Weekly H2H projections by category with a Claude-generated preview — see where you have the edge before the week starts.',
  },
  {
    to: '/keeper-eval',
    icon: Trophy,
    color: 'leather',
    title: 'Keeper Planning',
    description: 'Evaluate your keeper core or let the AI decide who to keep from your full roster. Includes draft target profiles.',
  },
]

const COLOR_MAP = {
  field:   'bg-field-900 border-field-700 text-field-400',
  leather: 'bg-leather-500/10 border-leather-500/30 text-leather-400',
  stitch:  'bg-stitch-500/10 border-stitch-500/30 text-stitch-400',
}

function LeagueSwitcher() {
  const { league, allLeagues, switching, switchLeague } = useLeague() || {}

  // Only render when the user has 2+ leagues
  if (!allLeagues || allLeagues.length < 2) return null

  return (
    <div className="card space-y-3">
      <div className="flex items-center gap-2">
        <div className="w-1.5 h-3.5 rounded-full bg-field-500 shrink-0" />
        <span className="text-sm font-semibold text-white">Your Leagues</span>
        <span className="text-xs text-slate-500 ml-1">· click to switch active league</span>
        {switching && (
          <RefreshCw size={12} className="ml-auto animate-spin text-slate-500" />
        )}
      </div>

      <div className="flex flex-col gap-2">
        {allLeagues.map(lg => (
          <button
            key={lg.league_id}
            onClick={() => !lg.is_active && switchLeague(lg.league_id)}
            disabled={switching || lg.is_active}
            className={`w-full text-left px-3 py-2.5 rounded-lg border transition-colors ${
              lg.is_active
                ? 'bg-field-900/50 border-field-700 cursor-default'
                : 'bg-navy-800 border-navy-700 hover:border-navy-600 hover:bg-navy-750 cursor-pointer'
            } ${switching && !lg.is_active ? 'opacity-50' : ''}`}
          >
            <div className="flex items-center gap-2">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className={`text-sm font-medium ${lg.is_active ? 'text-white' : 'text-slate-300'}`}>
                    {lg.league_name}
                  </span>
                  {lg.is_active && (
                    <span className="text-[10px] font-semibold text-field-400 bg-field-900 border border-field-700 rounded px-1.5 py-0.5">
                      Active
                    </span>
                  )}
                </div>
                <div className="text-[11px] text-slate-500 mt-0.5">
                  {lg.season} · {lg.num_teams} teams · {lg.league_type}
                </div>
              </div>
              {!lg.is_active && (
                <ChevronRight size={13} className="text-slate-600 shrink-0" />
              )}
            </div>
          </button>
        ))}
      </div>
    </div>
  )
}

export default function Home() {
  return (
    <div className="space-y-12">
      {/* Hero */}
      <div className="text-center space-y-4 pt-4">
        <div className="text-6xl mb-4">⚾</div>
        <h1 className="text-4xl font-bold text-white tracking-tight">
          Fantas<span className="text-field-400">AI</span> Sports
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

      {/* League switcher — only rendered when user has 2+ leagues */}
      <LeagueSwitcher />

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
