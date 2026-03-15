/**
 * API client for the FantasAI Sports backend.
 * All calls go to /api/v1/ — in dev the Vite proxy forwards them to localhost:8000.
 * In production the Vercel rewrite proxies /api/* to Railway, so VITE_API_URL is
 * not required. If set, it MUST include the protocol (https://...) — bare hostnames
 * without a protocol are treated the same as unset to avoid relative-URL breakage.
 */
const _rawUrl = import.meta.env.VITE_API_URL || ''
const BASE = _rawUrl.startsWith('http')
  ? `${_rawUrl}/api/v1`
  : '/api/v1'

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
export const getRankings = ({ ranking_type = 'predictive', limit = 400, season, position } = {}) => {
  const params = new URLSearchParams({ ranking_type, limit })
  if (season)   params.set('season', season)
  if (position) params.set('position', position)
  return get(`/rankings?${params}`)
}
