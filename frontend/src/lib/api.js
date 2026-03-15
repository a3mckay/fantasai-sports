/**
 * API client for the FantasAI Sports backend.
 * All calls go to /api/v1/ — in dev the Vite proxy forwards them to localhost:8000.
 * In production, set VITE_API_URL to the absolute backend URL.
 */

const BASE = import.meta.env.VITE_API_URL
  ? `${import.meta.env.VITE_API_URL}/api/v1`
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
