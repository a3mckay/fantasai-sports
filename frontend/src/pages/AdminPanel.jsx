import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { req } from '../lib/api'
import Spinner from '../components/Spinner'
import { Shield, Trash2, Search, Activity } from 'lucide-react'

export default function AdminPanel() {
  const { user } = useAuth()
  const navigate = useNavigate()

  const [users, setUsers] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [txnStatus, setTxnStatus] = useState(null)
  const [txnStatusLoading, setTxnStatusLoading] = useState(false)

  async function loadTxnStatus() {
    setTxnStatusLoading(true)
    try {
      const data = await req('GET', '/api/v1/transactions/debug-status')
      setTxnStatus(data)
    } catch (e) {
      setTxnStatus({ error: e.message })
    } finally {
      setTxnStatusLoading(false)
    }
  }

  async function forceReimport() {
    if (!confirm('Delete and re-import all transactions? This will wipe existing move grades.')) return
    try {
      await req('POST', '/api/v1/transactions/force-reimport?count=200')
      alert('Re-import started in background. Check Move Grades in ~60 seconds.')
    } catch (e) {
      alert('Failed: ' + e.message)
    }
  }

  async function regradeTransactions(txnType) {
    const label = txnType ? `${txnType}s` : 'all transactions'
    if (!confirm(`Re-grade ${label}? Existing blurbs will be rewritten with the current prompt logic.`)) return
    try {
      const qs = txnType ? `?transaction_type=${txnType}` : ''
      await req('POST', `/api/v1/transactions/regrade${qs}`)
      alert(`Re-grading ${label} in background. Check Move Grades in ~60 seconds.`)
    } catch (e) {
      alert('Failed: ' + e.message)
    }
  }

  // Redirect non-admins
  useEffect(() => {
    if (user && user.role !== 'admin') navigate('/', { replace: true })
  }, [user, navigate])

  useEffect(() => {
    loadUsers()
  }, [page, search])

  async function loadUsers() {
    setLoading(true)
    try {
      const params = new URLSearchParams({ page, per_page: 50 })
      if (search) params.set('search', search)
      const data = await req('GET', `/api/v1/users?${params}`)
      setUsers(data.users)
      setTotal(data.total)
    } catch {
      setError('Failed to load users.')
    } finally {
      setLoading(false)
    }
  }

  async function setRole(userId, newRole) {
    try {
      const updated = await req('PUT', `/api/v1/users/${userId}/role`, { role: newRole })
      setUsers(prev => prev.map(u => u.id === userId ? updated : u))
    } catch {
      alert('Failed to update role.')
    }
  }

  async function deleteUser(userId, email) {
    if (!confirm(`Permanently delete ${email}? This cannot be undone.`)) return
    try {
      await req('DELETE', `/api/v1/users/${userId}`)
      setUsers(prev => prev.filter(u => u.id !== userId))
      setTotal(t => t - 1)
    } catch {
      alert('Failed to delete user.')
    }
  }

  if (!user || user.role !== 'admin') return null

  return (
    <div className="space-y-8">
      {/* ── Transaction diagnostics ── */}
      <div>
        <div className="flex items-center gap-2 mb-3">
          <Activity size={16} className="text-field-400" />
          <h2 className="text-lg font-semibold text-white">Transaction Diagnostics</h2>
        </div>
        <div className="bg-navy-900 border border-navy-700 rounded-xl p-4 space-y-3">
          <div className="flex flex-wrap gap-3">
            <button onClick={loadTxnStatus} disabled={txnStatusLoading}
              className="px-3 py-1.5 text-xs bg-field-800 hover:bg-field-700 text-white rounded border border-field-600 transition-colors">
              {txnStatusLoading ? 'Loading…' : 'Check Status'}
            </button>
            <button onClick={() => regradeTransactions('trade')}
              className="px-3 py-1.5 text-xs bg-amber-900/40 hover:bg-amber-800/60 text-amber-300 rounded border border-amber-700 transition-colors">
              Re-grade Trades
            </button>
            <button onClick={() => regradeTransactions(null)}
              className="px-3 py-1.5 text-xs bg-navy-800 hover:bg-navy-700 text-slate-300 rounded border border-navy-600 transition-colors">
              Re-grade All Transactions
            </button>
            <button onClick={forceReimport}
              className="px-3 py-1.5 text-xs bg-red-900/40 hover:bg-red-800/60 text-red-300 rounded border border-red-700 transition-colors">
              Force Re-import All
            </button>
          </div>
          {txnStatus && (
            <pre className="text-xs text-slate-300 bg-navy-950 rounded p-3 overflow-x-auto whitespace-pre-wrap">
              {JSON.stringify(txnStatus, null, 2)}
            </pre>
          )}
        </div>
      </div>

      <div>
      <div className="flex items-center gap-2 mb-6">
        <Shield size={20} className="text-field-400" />
        <h1 className="text-2xl font-bold text-white">Admin Panel</h1>
        <span className="text-slate-500 text-sm ml-2">{total} total users</span>
      </div>

      {/* Search */}
      <div className="relative mb-4">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
        <input
          type="text"
          placeholder="Search by name or email…"
          value={search}
          onChange={e => { setSearch(e.target.value); setPage(1) }}
          className="w-full pl-9 pr-4 py-2 bg-navy-900 border border-navy-700 text-white placeholder-slate-500 rounded-lg text-sm focus:outline-none focus:border-field-500"
        />
      </div>

      {error && <p className="text-red-400 text-sm mb-4">{error}</p>}

      {loading ? (
        <div className="flex justify-center py-12"><Spinner /></div>
      ) : (
        <div className="bg-navy-900 border border-navy-700 rounded-xl overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-navy-700">
                <th className="text-left px-4 py-3 text-slate-400 font-medium">User</th>
                <th className="text-left px-4 py-3 text-slate-400 font-medium">Joined</th>
                <th className="text-left px-4 py-3 text-slate-400 font-medium">Status</th>
                <th className="text-left px-4 py-3 text-slate-400 font-medium">Role</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody>
              {users.map(u => (
                <tr key={u.id} className="border-b border-navy-800 last:border-0 hover:bg-navy-800/40">
                  <td className="px-4 py-3">
                    <div className="font-medium text-white">{u.name || '—'}</div>
                    <div className="text-slate-500 text-xs">{u.email || u.firebase_uid.slice(0, 16) + '…'}</div>
                  </td>
                  <td className="px-4 py-3 text-slate-400 text-xs">
                    {u.created_at ? new Date(u.created_at).toLocaleDateString() : '—'}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex flex-col gap-0.5">
                      <span className={`text-xs ${u.onboarding_complete ? 'text-emerald-400' : 'text-amber-400'}`}>
                        {u.onboarding_complete ? '✓ Onboarded' : '⏳ Onboarding'}
                      </span>
                      {u.yahoo_connected && <span className="text-xs text-purple-400">Y! Connected</span>}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    {u.id === user.id ? (
                      <span className="text-xs text-field-400 font-medium">Admin (you)</span>
                    ) : (
                      <select
                        value={u.role}
                        onChange={e => setRole(u.id, e.target.value)}
                        className="bg-navy-800 border border-navy-600 text-white text-xs rounded px-2 py-1"
                      >
                        <option value="user">User</option>
                        <option value="admin">Admin</option>
                      </select>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right">
                    {u.id !== user.id && (
                      <button
                        onClick={() => deleteUser(u.id, u.email)}
                        className="text-slate-600 hover:text-red-400 transition-colors p-1"
                        title="Delete user"
                      >
                        <Trash2 size={14} />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {users.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-4 py-8 text-center text-slate-500">No users found.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
      </div>  {/* end users section */}
    </div>
  )
}
