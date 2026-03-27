import { Link, useLocation } from 'react-router-dom'
import {
  BarChart2, ArrowLeftRight, Search, Star, Users,
  Trophy, Zap, Menu, X, Activity, TrendingUp, UserCircle, Shield, LogOut, ScrollText, Swords
} from 'lucide-react'
import { useState } from 'react'
import { useAuth } from '../contexts/AuthContext'

const NAV = [
  { to: '/',              icon: Activity,       label: 'Home'               },
  { to: '/rankings',      icon: TrendingUp,     label: 'Rankings'           },
  { to: '/find-player',   icon: Search,         label: 'Recommend a Player' },
  { to: '/compare',       icon: BarChart2,      label: 'Compare Players'    },
  { to: '/trade',         icon: ArrowLeftRight, label: 'Evaluate Trade'     },
  { to: '/team-eval',     icon: Star,           label: 'Team Eval'          },
  { to: '/compare-teams', icon: Users,          label: 'Compare Teams'      },
  { to: '/league-power',  icon: Zap,            label: 'League Power'       },
  { to: '/keeper-eval',   icon: Trophy,         label: 'Keeper Planning'    },
  { to: '/matchups',      icon: Swords,         label: 'Matchup Analyzer'   },
  { to: '/transactions',  icon: ScrollText,     label: 'Move Grades'        },
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

function UserMenu({ onClose }) {
  const { user, signOut } = useAuth()
  if (!user) return null

  return (
    <div className="space-y-0.5">
      <Link
        to="/profile"
        onClick={onClose}
        className="flex items-center gap-3 px-3 py-2 rounded-lg text-sm text-slate-400 hover:text-slate-100 hover:bg-navy-700 transition-all"
      >
        <UserCircle size={15} className="text-slate-500" />
        Profile & Settings
      </Link>
      {user.role === 'admin' && (
        <Link
          to="/admin"
          onClick={onClose}
          className="flex items-center gap-3 px-3 py-2 rounded-lg text-sm text-slate-400 hover:text-slate-100 hover:bg-navy-700 transition-all"
        >
          <Shield size={15} className="text-slate-500" />
          Admin Panel
        </Link>
      )}
      <button
        onClick={() => { signOut(); onClose?.() }}
        className="w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm text-slate-400 hover:text-red-400 hover:bg-navy-700 transition-all"
      >
        <LogOut size={15} className="text-slate-500" />
        Sign out
      </button>
    </div>
  )
}

export default function Layout({ children }) {
  const [open, setOpen] = useState(false)
  const { user } = useAuth()

  return (
    <div className="h-screen flex overflow-hidden">
      {/* ── Sidebar (desktop) ── */}
      <aside className="hidden md:flex flex-col w-56 shrink-0 bg-navy-950 border-r border-navy-700 sticky top-0 h-screen">
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

        {/* User section */}
        <div className="p-3 border-t border-navy-700">
          {user ? (
            <div>
              <div className="flex items-center gap-2.5 px-3 py-2 mb-1">
                <div className="w-7 h-7 rounded-full bg-field-700 flex items-center justify-center shrink-0">
                  <span className="text-xs font-bold text-field-200">
                    {(user.name || user.email || '?')[0].toUpperCase()}
                  </span>
                </div>
                <div className="min-w-0">
                  <div className="text-xs font-medium text-white truncate">{user.name || 'Manager'}</div>
                  {user.role === 'admin' && (
                    <div className="text-[10px] text-field-400">Admin</div>
                  )}
                </div>
              </div>
              <UserMenu onClose={() => {}} />
            </div>
          ) : (
            <Link
              to="/login"
              className="flex items-center justify-center gap-2 w-full px-3 py-2 bg-field-700 hover:bg-field-600 text-white text-sm font-medium rounded-lg transition-colors"
            >
              Sign In
            </Link>
          )}
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
        {/* Avatar on mobile */}
        {user ? (
          <Link to="/profile" className="w-8 h-8 rounded-full bg-field-700 flex items-center justify-center shrink-0">
            <span className="text-xs font-bold text-field-200">
              {(user.name || user.email || '?')[0].toUpperCase()}
            </span>
          </Link>
        ) : (
          <div className="w-8 shrink-0" />
        )}
      </div>

      {/* ── Mobile nav drawer ── */}
      {open && (
        <div className="md:hidden fixed inset-0 z-40 bg-black/60" onClick={() => setOpen(false)}>
          <div
            className="absolute top-14 left-0 bottom-0 w-64 bg-navy-950 border-r border-navy-700 p-3 space-y-0.5 overflow-y-auto flex flex-col"
            onClick={e => e.stopPropagation()}
          >
            <div className="flex-1 space-y-0.5">
              {NAV.map(n => <NavLink key={n.to} {...n} onClick={() => setOpen(false)} />)}
            </div>
            <div className="border-t border-navy-700 pt-3 mt-3">
              <UserMenu onClose={() => setOpen(false)} />
            </div>
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
