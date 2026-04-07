/**
 * API client for the FantasAI Sports backend.
 *
 * Resolution order:
 *   1. VITE_API_URL env var (must include protocol, e.g. https://...)
 *   2. In production builds, call Railway directly to bypass Vercel's 25-second
 *      edge-proxy timeout (LLM endpoints can take 30-60 s).
 *   3. In dev, use /api/v1 — Vite proxies it to localhost:8000.
 *
 * CORS is open ("*") on the backend so direct browser→Railway calls work fine.
 */
const RAILWAY_URL = 'https://fantasai-sports-production.up.railway.app'

const _rawUrl = import.meta.env.VITE_API_URL || ''
const BASE = _rawUrl.startsWith('http')
  ? _rawUrl
  : import.meta.env.PROD
    ? RAILWAY_URL   // production: call Railway directly, no Vercel proxy
    : ''            // dev: Vite proxy → localhost:8000

export const API_BASE_URL = BASE || window.location.origin

async function _getIdToken() {
  try {
    const { auth } = await import('./firebase')
    const fbUser = auth.currentUser
    if (fbUser) return fbUser.getIdToken()
  } catch {}
  return null
}

export async function req(method, path, body) {
  const headers = { 'Content-Type': 'application/json' }

  const token = await _getIdToken()
  if (token) headers['Authorization'] = `Bearer ${token}`

  const opts = { method, headers }
  if (body !== undefined) opts.body = JSON.stringify(body)

  const url = path.startsWith('http') ? path : `${BASE}${path}`
  const res = await fetch(url, opts)

  if (res.status === 429) {
    let detail = {}
    try { detail = await res.json() } catch {}
    const feature = detail.detail?.feature || detail.feature || 'this feature'
    window.dispatchEvent(new CustomEvent('metering:limit', { detail: { feature } }))
    throw new Error('rate_limited')
  }

  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const err = await res.json()
      detail = err.detail || JSON.stringify(err)
    } catch {}
    throw new Error(detail)
  }
  return res.json()
}

const get  = (path)       => req('GET',  path)
const post = (path, body) => req('POST', path, body)


// ── Auth / Leagues ────────────────────────────────────────────────────────────
export const listUserLeagues  = ()          => req('GET',  '/api/v1/auth/leagues')
export const activateLeague   = (leagueId)  => req('POST', `/api/v1/auth/leagues/${encodeURIComponent(leagueId)}/activate`)

// ── Players ──────────────────────────────────────────────────────────────────
export const searchPlayers = (q, limit = 20) =>
  get(`/api/v1/players?search=${encodeURIComponent(q)}&limit=${limit}`)

// ── Leagues ───────────────────────────────────────────────────────────────────
export const listLeagues = () => get('/api/v1/leagues')
export const getLeague   = (id) => get(`/api/v1/leagues/${id}`)
export const listTeams   = (leagueId) => get(`/api/v1/leagues/${leagueId}/teams`)

// ── Analysis ─────────────────────────────────────────────────────────────────
export const comparePlayers  = (body) => post('/api/v1/analysis/compare', body)
export const evaluateTrade   = (body) => post('/api/v1/analysis/trade', body)
export const findPlayer      = (body) => post('/api/v1/analysis/find-player', body)
export const teamEval        = (body) => post('/api/v1/analysis/team-eval', body)
export const keeperEval      = (body) => post('/api/v1/analysis/keeper-eval', body)
export const compareTeams    = (body) => post('/api/v1/analysis/compare-teams', body)
export const leaguePower     = (id)   => get(`/api/v1/analysis/league-power/${id}`)
export const extractPlayers  = (body) => post('/api/v1/analysis/extract-players', body)
export const rosterAnalysis  = (teamId) => get(`/api/v1/recommendations/${teamId}/roster-analysis`)

// ── Rankings ─────────────────────────────────────────────────────────────────
export const getRankings = ({ ranking_type = 'predictive', limit = 400, season, position, horizon } = {}) => {
  const params = new URLSearchParams({ ranking_type, limit })
  if (season)   params.set('season', season)
  if (position) params.set('position', position)
  if (horizon)  params.set('horizon', horizon)
  return get(`/api/v1/rankings?${params}`)
}

export const getWeekMode = () => get('/api/v1/rankings/week-mode')
