import { useState, useEffect } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { req } from '../lib/api'
import Spinner from '../components/Spinner'

const STEPS = ['Profile', 'Connect Yahoo', 'All Set']

export default function Onboarding() {
  const { user, refreshProfile } = useAuth()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()

  const yahooResult = searchParams.get('yahoo') // 'connected' | 'error' | null
  const stepParam = parseInt(searchParams.get('step') || '1', 10)
  const [step, setStep] = useState(yahooResult ? 3 : stepParam)

  const [name, setName] = useState(user?.name || '')
  const [dob, setDob] = useState(user?.date_of_birth || '')
  const [style, setStyle] = useState(user?.managing_style || '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [yahooConnecting, setYahooConnecting] = useState(false)
  const [yahooStatus, setYahooStatus] = useState(null) // null | 'connected' | 'error'

  useEffect(() => {
    if (yahooResult === 'connected') setYahooStatus('connected')
    if (yahooResult === 'error') setYahooStatus('error')
  }, [yahooResult])

  async function saveProfile() {
    if (!name.trim()) { setError('Name is required'); return }
    setSaving(true); setError('')
    try {
      await req('PUT', '/api/v1/auth/me', { name: name.trim(), date_of_birth: dob || null, managing_style: style || null })
      setStep(2)
    } catch {
      setError('Failed to save profile. Please try again.')
    } finally {
      setSaving(false)
    }
  }

  async function connectYahoo() {
    setYahooConnecting(true); setError('')
    try {
      const data = await req('GET', '/api/v1/auth/yahoo/connect')
      window.location.href = data.auth_url
    } catch {
      setError('Could not initiate Yahoo connection. Please try again.')
      setYahooConnecting(false)
    }
  }

  async function finish() {
    setSaving(true)
    try {
      await req('POST', '/api/v1/auth/complete-onboarding')
      await refreshProfile()
      navigate('/')
    } catch {
      setError('Failed to complete setup. Please try again.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-navy-950 px-4">
      <div className="w-full max-w-lg">
        {/* Header */}
        <div className="text-center mb-8">
          <div className="text-3xl mb-2">⚾</div>
          <h1 className="text-2xl font-bold text-white">
            Welcome to Fantas<span className="text-field-400">AI</span>
          </h1>
          <p className="text-slate-400 text-sm mt-1">Let's get your account set up</p>
        </div>

        {/* Step indicator */}
        <div className="flex items-center justify-center gap-2 mb-8">
          {STEPS.map((label, i) => (
            <div key={i} className="flex items-center gap-2">
              <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold border-2 transition-all ${
                i + 1 < step
                  ? 'bg-field-600 border-field-600 text-white'
                  : i + 1 === step
                    ? 'border-field-500 text-field-400'
                    : 'border-navy-600 text-slate-600'
              }`}>
                {i + 1 < step ? '✓' : i + 1}
              </div>
              <span className={`text-xs font-medium ${i + 1 === step ? 'text-white' : 'text-slate-600'}`}>
                {label}
              </span>
              {i < STEPS.length - 1 && <div className="w-6 h-px bg-navy-700 mx-1" />}
            </div>
          ))}
        </div>

        <div className="bg-navy-900 border border-navy-700 rounded-xl p-7 shadow-xl">
          {/* Step 1: Profile */}
          {step === 1 && (
            <div>
              <h2 className="text-lg font-semibold text-white mb-1">Tell us about yourself</h2>
              <p className="text-slate-400 text-sm mb-5">This helps personalize your AI recommendations.</p>

              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-slate-300 mb-1.5">
                    Name <span className="text-stitch-500">*</span>
                  </label>
                  <input
                    type="text"
                    placeholder="Your name"
                    value={name}
                    onChange={e => setName(e.target.value)}
                    className="w-full bg-navy-800 border border-navy-600 text-white placeholder-slate-500 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:border-field-500 focus:ring-1 focus:ring-field-500"
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
                    placeholder="e.g. 'Win-now, aggressive on the waiver wire' or 'Dynasty builder, favor youth'"
                    value={style}
                    onChange={e => setStyle(e.target.value)}
                    rows={3}
                    className="w-full bg-navy-800 border border-navy-600 text-white placeholder-slate-500 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:border-field-500 focus:ring-1 focus:ring-field-500 resize-none"
                  />
                </div>
              </div>

              {error && <p className="text-red-400 text-xs mt-3">{error}</p>}

              <button
                onClick={saveProfile}
                disabled={saving}
                className="mt-6 w-full bg-field-600 hover:bg-field-500 text-white font-semibold py-2.5 px-4 rounded-lg transition-colors disabled:opacity-50"
              >
                {saving ? <Spinner size="sm" /> : 'Next: Connect Yahoo →'}
              </button>
            </div>
          )}

          {/* Step 2: Connect Yahoo */}
          {step === 2 && (
            <div>
              <h2 className="text-lg font-semibold text-white mb-1">Connect your Yahoo league</h2>
              <p className="text-slate-400 text-sm mb-5">
                We'll import your league settings, scoring categories, and team roster automatically.
              </p>

              <div className="bg-navy-800/60 border border-navy-700 rounded-lg p-4 mb-5 text-sm text-slate-300 space-y-1">
                <div className="font-medium text-white mb-2">What we import:</div>
                <div>✓ League scoring categories</div>
                <div>✓ Roster positions</div>
                <div>✓ Your team name and roster</div>
              </div>

              {yahooStatus === 'error' && (
                <p className="text-red-400 text-xs mb-4 bg-red-950/40 border border-red-800/40 rounded-lg px-3 py-2">
                  Yahoo connection failed. Please try again.
                </p>
              )}

              {error && <p className="text-red-400 text-xs mb-4">{error}</p>}

              <button
                onClick={connectYahoo}
                disabled={yahooConnecting}
                className="w-full bg-[#6001d2] hover:bg-[#5200b8] text-white font-semibold py-2.5 px-4 rounded-lg transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
              >
                {yahooConnecting ? <Spinner size="sm" /> : (
                  <>
                    <span className="text-lg">Y!</span>
                    Connect Yahoo Account
                  </>
                )}
              </button>

              <button
                onClick={() => setStep(3)}
                className="mt-3 w-full text-slate-500 hover:text-slate-300 text-sm py-2 transition-colors"
              >
                Skip for now (you can connect later in Profile)
              </button>
            </div>
          )}

          {/* Step 3: Done */}
          {step === 3 && (
            <div className="text-center">
              <div className="text-5xl mb-4">
                {yahooStatus === 'connected' ? '🎉' : '✅'}
              </div>
              <h2 className="text-lg font-semibold text-white mb-2">
                {yahooStatus === 'connected' ? 'Yahoo connected!' : "You're all set!"}
              </h2>
              {yahooStatus === 'connected' ? (
                <p className="text-slate-400 text-sm mb-6">
                  Your Yahoo league and team have been imported. FantasAI is ready to go.
                </p>
              ) : (
                <p className="text-slate-400 text-sm mb-6">
                  Your profile is saved. You can connect Yahoo anytime from your Profile page.
                </p>
              )}

              {error && <p className="text-red-400 text-xs mb-4">{error}</p>}

              <button
                onClick={finish}
                disabled={saving}
                className="w-full bg-field-600 hover:bg-field-500 text-white font-semibold py-2.5 px-4 rounded-lg transition-colors disabled:opacity-50"
              >
                {saving ? <Spinner size="sm" /> : "Let's Go →"}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
