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
  ? `${_rawUrl}/api/v1`
  : import.meta.env.PROD
    ? `${RAILWAY_URL}/api/v1`   // production: call Railway directly, no Vercel proxy
    : '/api/v1'                  // dev: Vite proxy → localhost:8000

export const API_BASE_URL = BASE.replace('/api/v1', '') // used by keepalive

async function req(method, path, body) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json' },
  }
  if (body !== undefined) opts.body = JSON.stringify(body)

  const res = await fetch(`${BASE}${path}`, opts)
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

// ── Players ──────────────────────────────────────────────────────────────────
export const searchPlayers = (q, limit = 20) =>
  get(`/players?search=${encodeURIComponent(q)}&limit=${limit}`)

// ── Leagues ───────────────────────────────────────────────────────────────────
export const listLeagues = () => get('/leagues')
export const getLeague   = (id) => get(`/leagues/${id}`)
export const listTeams   = (leagueId) => get(`/leagues/${leagueId}/teams`)

// ── Analysis ─────────────────────────────────────────────────────────────────
export const comparePlayers  = (body) => post('/analysis/compare', body)
export const evaluateTrade   = (body) => post('/analysis/trade', body)
export const findPlayer      = (body) => post('/analysis/find-player', body)
export const teamEval        = (body) => post('/analysis/team-eval', body)
export const keeperEval      = (body) => post('/analysis/keeper-eval', body)
export const compareTeams    = (body) => post('/analysis/compare-teams', body)
export const leaguePower     = (id)   => get(`/analysis/league-power/${id}`)
export const extractPlayers  = (body) => post('/analysis/extract-players', body)

// ── Rankings ─────────────────────────────────────────────────────────────────
export const getRankings = ({ ranking_type = 'predictive', limit = 400, season, position, horizon } = {}) => {
  const params = new URLSearchParams({ ranking_type, limit })
  if (season)   params.set('season', season)
  if (position) params.set('position', position)
  if (horizon)  params.set('horizon', horizon)
  return get(`/rankings?${params}`)
}
