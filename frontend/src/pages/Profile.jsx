import { useState, useEffect } from 'react'
import { useAuth } from '../contexts/AuthContext'
import { req } from '../lib/api'
import Spinner from '../components/Spinner'

export default function Profile() {
  const { user, refreshProfile, signOut } = useAuth()

  const [name, setName] = useState(user?.name || '')
  const [dob, setDob] = useState(user?.date_of_birth || '')
  const [style, setStyle] = useState(user?.managing_style || '')
  const [saving, setSaving] = useState(false)
  const [profileMsg, setProfileMsg] = useState('')

  const [yahooStatus, setYahooStatus] = useState(null)
  const [yahooLoading, setYahooLoading] = useState(true)
  const [resyncing, setResyncing] = useState(false)
  const [resyncSteps, setResyncSteps] = useState([])   // [{label, done, error}]
  const [resyncProgress, setResyncProgress] = useState({ current: 0, total: 0 })
  const [resyncDone, setResyncDone] = useState(false)

  const [prefs, setPrefs] = useState({ weekly_digest: true, waiver_alerts: true })
  const [prefsLoading, setPrefsLoading] = useState(true)
  const [prefsSaving, setPrefsSaving] = useState(false)

  useEffect(() => {
    req('GET', '/api/v1/auth/yahoo/status').then(setYahooStatus).catch(() => {}).finally(() => setYahooLoading(false))
    req('GET', '/api/v1/settings').then(data => setPrefs(data.notification_prefs || prefs)).catch(() => {}).finally(() => setPrefsLoading(false))
  }, [])

  async function saveProfile(e) {
    e.preventDefault()
    setSaving(true); setProfileMsg('')
    try {
      await req('PUT', '/api/v1/auth/me', { name: name.trim(), date_of_birth: dob || null, managing_style: style || null })
      await refreshProfile()
      setProfileMsg('Profile saved!')
    } catch {
      setProfileMsg('Failed to save. Please try again.')
    } finally {
      setSaving(false)
    }
  }

  async function connectYahoo() {
    try {
      const data = await req('GET', '/api/v1/auth/yahoo/connect')
      window.location.href = data.auth_url
    } catch {
      alert('Could not connect to Yahoo. Please try again.')
    }
  }

  async function resyncYahoo() {
    setResyncing(true)
    setResyncDone(false)
    setResyncSteps([])
    setResyncProgress({ current: 0, total: 0 })

    const addStep = (label, done = false, error = false) =>
      setResyncSteps(prev => [...prev, { label, done, error }])
    const finishStep = (label) =>
      setResyncSteps(prev => prev.map((s, i) => i === prev.length - 1 ? { ...s, label, done: true } : s))
    const failStep = (label) =>
      setResyncSteps(prev => prev.map((s, i) => i === prev.length - 1 ? { ...s, label, error: true } : s))

    try {
      // Step 1 — fetch league + team list
      addStep('Connecting to Yahoo Fantasy…')
      const start = await req('POST', '/api/v1/auth/yahoo/resync/start')
      finishStep(`Connected — ${start.league_name}`)
      setResyncProgress({ current: 1, total: start.teams.length + 1 })

      // Step 2…N — import each team's roster
      for (let i = 0; i < start.teams.length; i++) {
        const team = start.teams[i]
        addStep(`Importing ${team.team_name}…`)
        setResyncProgress({ current: i + 1, total: start.teams.length + 1 })
        try {
          const result = await req('POST', '/api/v1/auth/yahoo/resync/team', {
            team_key:     team.team_key,
            team_name:    team.team_name,
            manager_name: team.manager_name || '',
          })
          finishStep(
            `${team.team_name}${team.is_mine ? ' (your team)' : ''} — ${result.resolved_count} players`
          )
        } catch (err) {
          failStep(`${team.team_name} — failed: ${err.message}`)
        }
        setResyncProgress({ current: i + 2, total: start.teams.length + 1 })
      }

      setResyncDone(true)
      const status = await req('GET', '/api/v1/auth/yahoo/status')
      setYahooStatus(status)
    } catch (err) {
      failStep(`Failed: ${err.message}`)
    } finally {
      setResyncing(false)
    }
  }

  async function disconnectYahoo() {
    if (!confirm('Disconnect your Yahoo account? Your league and team data will remain.')) return
    await req('DELETE', '/api/v1/auth/yahoo/disconnect')
    setYahooStatus({ connected: false })
  }

  async function savePrefs() {
    setPrefsSaving(true)
    try {
      await req('PUT', '/api/v1/settings', prefs)
    } catch {
      alert('Failed to save preferences.')
    } finally {
      setPrefsSaving(false)
    }
  }

  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-bold text-white mb-6">Profile & Settings</h1>

      {/* Profile form */}
      <section className="bg-navy-900 border border-navy-700 rounded-xl p-6 mb-5">
        <h2 className="text-base font-semibold text-white mb-4">Profile</h2>
        <form onSubmit={saveProfile} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-slate-300 mb-1.5">Email</label>
            <p className="text-slate-400 text-sm bg-navy-800 rounded-lg px-3 py-2.5">{user?.email || '—'}</p>
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-300 mb-1.5">Name</label>
            <input
              type="text"
              value={name}
              onChange={e => setName(e.target.value)}
              className="w-full bg-navy-800 border border-navy-600 text-white rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:border-field-500 focus:ring-1 focus:ring-field-500"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-300 mb-1.5">Date of Birth</label>
            <input
              type="date"
              value={dob}
              onChange={e => setDob(e.target.value)}
              className="w-full bg-navy-800 border border-navy-600 text-white rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:border-field-500 focus:ring-1 focus:ring-field-500"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-slate-300 mb-1.5">Managing Style</label>
            <textarea
              value={style}
              onChange={e => setStyle(e.target.value)}
              rows={3}
              className="w-full bg-navy-800 border border-navy-600 text-white rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:border-field-500 focus:ring-1 focus:ring-field-500 resize-none"
            />
          </div>
          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={saving}
              className="bg-field-600 hover:bg-field-500 text-white font-medium py-2 px-5 rounded-lg text-sm transition-colors disabled:opacity-50"
            >
              {saving ? 'Saving…' : 'Save Profile'}
            </button>
            {profileMsg && <span className="text-sm text-field-400">{profileMsg}</span>}
          </div>
        </form>
      </section>

      {/* Yahoo connection */}
      <section className="bg-navy-900 border border-navy-700 rounded-xl p-6 mb-5">
        <h2 className="text-base font-semibold text-white mb-4">Yahoo Fantasy</h2>
        {yahooLoading ? (
          <Spinner />
        ) : yahooStatus?.connected ? (
          <div>
            <div className="flex items-center gap-2 mb-3">
              <div className="w-2 h-2 rounded-full bg-emerald-500" />
              <span className="text-emerald-400 text-sm font-medium">Connected</span>
              {yahooStatus.league_key && (
                <span className="text-slate-500 text-xs ml-1">· League: {yahooStatus.league_key}</span>
              )}
            </div>
            <div className="flex items-center gap-3 flex-wrap">
              <button
                onClick={resyncYahoo}
                disabled={resyncing}
                className="bg-[#6001d2] hover:bg-[#5200b8] text-white font-medium py-2 px-4 rounded-lg text-sm transition-colors disabled:opacity-50 flex items-center gap-2"
              >
                <span className="font-bold">Y!</span>
                {resyncing ? 'Syncing…' : 'Re-sync League Data'}
              </button>
              <button
                onClick={disconnectYahoo}
                className="text-red-400 hover:text-red-300 text-sm transition-colors"
              >
                Disconnect
              </button>
            </div>

            {/* Progress UI */}
            {resyncSteps.length > 0 && (
              <div className="mt-4 space-y-2">
                {/* Progress bar */}
                {resyncProgress.total > 0 && (
                  <div className="w-full bg-navy-700 rounded-full h-1.5 overflow-hidden">
                    <div
                      className="h-full bg-[#6001d2] rounded-full transition-all duration-300"
                      style={{ width: `${Math.round(100 * resyncProgress.current / resyncProgress.total)}%` }}
                    />
                  </div>
                )}
                {/* Step log */}
                <div className="space-y-1">
                  {resyncSteps.map((step, i) => (
                    <div key={i} className="flex items-center gap-2 text-xs">
                      {step.error  ? <span className="text-red-400">✕</span>
                       : step.done ? <span className="text-emerald-400">✓</span>
                       :             <span className="text-slate-500 animate-pulse">·</span>
                      }
                      <span className={step.error ? 'text-red-400' : step.done ? 'text-slate-300' : 'text-slate-400'}>
                        {step.label}
                      </span>
                    </div>
                  ))}
                </div>
                {resyncDone && (
                  <p className="text-xs text-emerald-400 font-medium pt-1">All done — league data is up to date.</p>
                )}
              </div>
            )}
          </div>
        ) : (
          <div>
            <p className="text-slate-400 text-sm mb-3">
              Connect your Yahoo Fantasy account to import your league and team automatically.
            </p>
            <button
              onClick={connectYahoo}
              className="bg-[#6001d2] hover:bg-[#5200b8] text-white font-medium py-2 px-5 rounded-lg text-sm transition-colors flex items-center gap-2"
            >
              <span className="font-bold">Y!</span> Connect Yahoo
            </button>
          </div>
        )}
      </section>

      {/* Notification preferences */}
      <section className="bg-navy-900 border border-navy-700 rounded-xl p-6 mb-5">
        <h2 className="text-base font-semibold text-white mb-4">Notifications</h2>
        {prefsLoading ? <Spinner /> : (
          <div className="space-y-3">
            <Toggle
              label="Weekly digest"
              description="Summary of top waiver pickups and rankings changes"
              checked={prefs.weekly_digest}
              onChange={v => setPrefs(p => ({ ...p, weekly_digest: v }))}
            />
            <Toggle
              label="Waiver alerts"
              description="Alerts when high-value players hit the waiver wire"
              checked={prefs.waiver_alerts}
              onChange={v => setPrefs(p => ({ ...p, waiver_alerts: v }))}
            />
            <button
              onClick={savePrefs}
              disabled={prefsSaving}
              className="mt-2 bg-navy-700 hover:bg-navy-600 text-white font-medium py-2 px-5 rounded-lg text-sm transition-colors disabled:opacity-50"
            >
              {prefsSaving ? 'Saving…' : 'Save Preferences'}
            </button>
          </div>
        )}
      </section>

      {/* Sign out */}
      <section className="bg-navy-900 border border-navy-700 rounded-xl p-6">
        <h2 className="text-base font-semibold text-white mb-3">Account</h2>
        <button
          onClick={signOut}
          className="text-red-400 hover:text-red-300 text-sm transition-colors"
        >
          Sign out
        </button>
      </section>
    </div>
  )
}

function Toggle({ label, description, checked, onChange }) {
  return (
    <div className="flex items-center justify-between gap-4">
      <div>
        <div className="text-sm font-medium text-white">{label}</div>
        <div className="text-xs text-slate-500">{description}</div>
      </div>
      <button
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={`relative w-11 h-6 rounded-full transition-colors flex-shrink-0 overflow-hidden ${
          checked ? 'bg-field-600' : 'bg-navy-600'
        }`}
      >
        <span className={`absolute top-1 left-1 w-4 h-4 rounded-full bg-white transition-transform ${
          checked ? 'translate-x-5' : 'translate-x-0'
        }`} />
      </button>
    </div>
  )
}
