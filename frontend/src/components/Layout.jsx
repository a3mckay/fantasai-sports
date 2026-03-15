import { Link, useLocation } from 'react-router-dom'
import {
  BarChart2, ArrowLeftRight, Search, Star, Users,
  Trophy, Zap, Menu, X, Activity, TrendingUp
} from 'lucide-react'
import { useState } from 'react'

const NAV = [
  { to: '/',              icon: Activity,       label: 'Home'            },
  { to: '/rankings',      icon: TrendingUp,     label: 'Rankings'        },
  { to: '/compare',       icon: BarChart2,      label: 'Compare Players' },
  { to: '/trade',         icon: ArrowLeftRight, label: 'Evaluate Trade'  },
  { to: '/team-eval',     icon: Star,           label: 'Team Eval'       },
  { to: '/compare-teams', icon: Users,          label: 'Compare Teams'   },
  { to: '/keeper-eval',   icon: Trophy,         label: 'Keeper Planning' },
  { to: '/find-player',   icon: Search,         label: 'Find a Player'   },
  { to: '/league-power',  icon: Zap,            label: 'League Power'    },
]

function NavLink({ to, icon: Icon, label, onClick }) {
  const { pathname } = useLocation()
  const active = pathname === to
  return (
    <Link
      to={to}
      onClick={onClick}
      className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all duration-150
        ${active
          ? 'bg-field-700 text-white shadow-inner'
          : 'text-slate-400 hover:text-slate-100 hover:bg-navy-700'
        }`}
    >
      <Icon size={16} className={active ? 'text-field-300' : 'text-slate-500'} />
      {label}
    </Link>
  )
}

export default function Layout({ children }) {
  const [open, setOpen] = useState(false)

  return (
    <div className="min-h-screen flex">
      {/* ── Sidebar (desktop) ── */}
      <aside className="hidden md:flex flex-col w-56 shrink-0 bg-navy-950 border-r border-navy-700">
        <div className="p-5 border-b border-navy-700">
          <div className="flex items-center gap-2.5">
            {/* Baseball icon */}
            <div className="w-8 h-8 rounded-full bg-leather-100 flex items-center justify-center shrink-0 border-2 border-stitch-500">
              <span className="text-xs font-bold text-stitch-500">⚾</span>
            </div>
            <div>
              <div className="text-sm font-bold text-white leading-tight">FantasAI</div>
              <div className="text-[10px] text-field-400 font-medium tracking-widest uppercase">Sports</div>
            </div>
          </div>
        </div>
        <nav className="flex-1 p-3 space-y-0.5 overflow-y-auto">
          {NAV.map(n => <NavLink key={n.to} {...n} />)}
        </nav>
        <div className="p-4 border-t border-navy-700">
          <p className="text-[10px] text-slate-600 text-center">Powered by Claude AI</p>
        </div>
      </aside>

      {/* ── Mobile header ── */}
      <div className="md:hidden fixed top-0 inset-x-0 z-50 bg-navy-950 border-b border-navy-700 flex items-center px-4 h-14">
        <button
          onClick={() => setOpen(!open)}
          className="text-slate-400 hover:text-white p-1 shrink-0"
        >
          {open ? <X size={20} /> : <Menu size={20} />}
        </button>
        <Link
          to="/"
          onClick={() => setOpen(false)}
          className="flex items-center gap-2 flex-1 justify-center"
        >
          <span className="text-lg">⚾</span>
          <span className="font-bold text-white text-sm">
            Fantas<span className="text-field-400">AI</span> Sports
          </span>
        </Link>
        {/* Spacer to keep title visually centered */}
        <div className="w-8 shrink-0" />
      </div>

      {/* ── Mobile nav drawer ── */}
      {open && (
        <div className="md:hidden fixed inset-0 z-40 bg-black/60" onClick={() => setOpen(false)}>
          <div
            className="absolute top-14 left-0 bottom-0 w-64 bg-navy-950 border-r border-navy-700 p-3 space-y-0.5 overflow-y-auto"
            onClick={e => e.stopPropagation()}
          >
            {NAV.map(n => <NavLink key={n.to} {...n} onClick={() => setOpen(false)} />)}
          </div>
        </div>
      )}

      {/* ── Main content ── */}
      <main className="flex-1 min-w-0 pt-14 md:pt-0 overflow-y-auto">
        <div className="max-w-4xl mx-auto px-4 md:px-8 py-8">
          {children}
        </div>
      </main>
    </div>
  )
}
