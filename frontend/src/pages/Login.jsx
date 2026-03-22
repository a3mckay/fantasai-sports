import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  signInWithPopup,
  signInWithEmailAndPassword,
  createUserWithEmailAndPassword,
} from 'firebase/auth'
import { auth, googleProvider, appleProvider, facebookProvider } from '../lib/firebase'
import { useAuth } from '../contexts/AuthContext'

export default function Login() {
  const navigate = useNavigate()
  const { user, authError } = useAuth()
  const [mode, setMode] = useState('signin') // 'signin' | 'signup'
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  // Redirect once user is set (onboarding_complete handled by AuthGuard)
  useEffect(() => {
    if (user) navigate('/', { replace: true })
  }, [user, navigate])

  async function handleSocialSignIn(provider) {
    setError('')
    setLoading(true)
    try {
      await signInWithPopup(auth, provider)
      // AuthContext picks up onAuthStateChanged → POSTs /auth/verify → sets user
      // The useEffect above will navigate once user is set
    } catch (err) {
      if (err.code !== 'auth/popup-closed-by-user') {
        setError(err.message || 'Sign in failed. Please try again.')
      }
    } finally {
      setLoading(false)
    }
  }

  async function handleEmailSubmit(e) {
    e.preventDefault()
    if (!email || !password) return
    setError('')
    setLoading(true)
    try {
      if (mode === 'signup') {
        await createUserWithEmailAndPassword(auth, email, password)
      } else {
        await signInWithEmailAndPassword(auth, email, password)
      }
      // useEffect above navigates once user is set
    } catch (err) {
      const msgs = {
        'auth/user-not-found': 'No account found with this email.',
        'auth/wrong-password': 'Incorrect password.',
        'auth/email-already-in-use': 'An account with this email already exists.',
        'auth/weak-password': 'Password must be at least 6 characters.',
        'auth/invalid-email': 'Please enter a valid email address.',
      }
      setError(msgs[err.code] || err.message || 'Something went wrong.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-navy-950 px-4">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center gap-3 mb-4">
            <div className="w-12 h-12 rounded-full bg-leather-100 flex items-center justify-center border-2 border-stitch-500">
              <span className="text-xl">⚾</span>
            </div>
          </div>
          <h1 className="text-2xl font-bold text-white">
            Fantas<span className="text-field-400">AI</span> Sports
          </h1>
          <p className="text-slate-400 text-sm mt-1">Your AI-powered fantasy GM</p>
        </div>

        <div className="bg-navy-900 border border-navy-700 rounded-xl p-6 shadow-xl">
          <h2 className="text-lg font-semibold text-white mb-5 text-center">
            {mode === 'signin' ? 'Sign in to your account' : 'Create your account'}
          </h2>

          {/* Backend auth error (e.g. server not running) */}
          {authError && (
            <div className="mb-4 text-red-400 text-xs bg-red-950/40 border border-red-800/40 rounded-lg px-3 py-2">
              {authError}
            </div>
          )}

          {/* Social sign-in buttons */}
          <div className="space-y-2 mb-5">
            <button
              onClick={() => handleSocialSignIn(googleProvider)}
              disabled={loading}
              className="w-full flex items-center justify-center gap-3 px-4 py-2.5 bg-white hover:bg-slate-100 text-slate-800 font-medium rounded-lg transition-colors disabled:opacity-50"
            >
              <GoogleIcon />
              Continue with Google
            </button>
            <button
              onClick={() => handleSocialSignIn(appleProvider)}
              disabled={loading}
              className="w-full flex items-center justify-center gap-3 px-4 py-2.5 bg-black hover:bg-slate-900 text-white font-medium rounded-lg border border-slate-700 transition-colors disabled:opacity-50"
            >
              <AppleIcon />
              Continue with Apple
            </button>
            <button
              onClick={() => handleSocialSignIn(facebookProvider)}
              disabled={loading}
              className="w-full flex items-center justify-center gap-3 px-4 py-2.5 bg-[#1877F2] hover:bg-[#166FE5] text-white font-medium rounded-lg transition-colors disabled:opacity-50"
            >
              <FacebookIcon />
              Continue with Facebook
            </button>
          </div>

          {/* Divider */}
          <div className="flex items-center gap-3 mb-5">
            <div className="flex-1 h-px bg-navy-700" />
            <span className="text-slate-500 text-xs">or</span>
            <div className="flex-1 h-px bg-navy-700" />
          </div>

          {/* Email/password form */}
          <form onSubmit={handleEmailSubmit} className="space-y-3">
            <input
              type="email"
              placeholder="Email address"
              value={email}
              onChange={e => setEmail(e.target.value)}
              className="w-full bg-navy-800 border border-navy-600 text-white placeholder-slate-500 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:border-field-500 focus:ring-1 focus:ring-field-500"
              required
            />
            <input
              type="password"
              placeholder="Password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              className="w-full bg-navy-800 border border-navy-600 text-white placeholder-slate-500 rounded-lg px-3 py-2.5 text-sm focus:outline-none focus:border-field-500 focus:ring-1 focus:ring-field-500"
              required
              minLength={6}
            />

            {(error || authError) && (
              <p className="text-red-400 text-xs bg-red-950/40 border border-red-800/40 rounded-lg px-3 py-2">
                {error || authError}
              </p>
            )}

            <button
              type="submit"
              disabled={loading}
              className="w-full bg-field-600 hover:bg-field-500 text-white font-semibold py-2.5 px-4 rounded-lg transition-colors disabled:opacity-50"
            >
              {loading ? 'Please wait…' : mode === 'signin' ? 'Sign In' : 'Create Account'}
            </button>
          </form>

          <p className="text-center text-slate-500 text-sm mt-4">
            {mode === 'signin' ? "Don't have an account? " : 'Already have an account? '}
            <button
              onClick={() => { setMode(mode === 'signin' ? 'signup' : 'signin'); setError('') }}
              className="text-field-400 hover:text-field-300 font-medium"
            >
              {mode === 'signin' ? 'Sign up' : 'Sign in'}
            </button>
          </p>
        </div>
      </div>
    </div>
  )
}

function GoogleIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18">
      <path fill="#4285F4" d="M17.64 9.2c0-.637-.057-1.251-.164-1.84H9v3.481h4.844c-.209 1.125-.843 2.078-1.796 2.716v2.259h2.908c1.702-1.567 2.684-3.875 2.684-6.615z"/>
      <path fill="#34A853" d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332C2.438 15.983 5.482 18 9 18z"/>
      <path fill="#FBBC05" d="M3.964 10.71c-.18-.54-.282-1.117-.282-1.71s.102-1.17.282-1.71V4.958H.957C.347 6.173 0 7.548 0 9s.348 2.827.957 4.042l3.007-2.332z"/>
      <path fill="#EA4335" d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0 5.482 0 2.438 2.017.957 4.958L3.964 6.29C4.672 4.163 6.656 3.58 9 3.58z"/>
    </svg>
  )
}

function AppleIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 814 1000" fill="white">
      <path d="M788.1 340.9c-5.8 4.5-108.2 62.2-108.2 190.5 0 148.4 130.3 200.9 134.2 202.2-.6 3.2-20.7 71.9-68.7 141.9-42.8 61.6-87.5 123.1-155.5 123.1s-85.5-39.5-164-39.5c-76.5 0-103.7 40.8-165.9 40.8s-105-37.6-155.5-127.6C46.7 790.7 0 663 0 541.8c0-207.4 135.4-316.8 268.9-316.8 79.2 0 144.8 49.4 192 49.4 45.3 0 122.4-52.1 215.7-52.1zM550 48c11 40.8 17.1 81.8 17.1 123.9 0 72.8-32.3 146.5-83.6 196.9-43.4 42.8-107.6 71.9-172.2 71.9-8.4 0-16.9-.5-24.7-1.6-.6-2.6-1-5.2-1-7.7 0-59.8 31.7-121.9 79.8-162.2 49.7-41.5 121.3-68.8 185.5-71.2z"/>
    </svg>
  )
}

function FacebookIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="white">
      <path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z"/>
    </svg>
  )
}
