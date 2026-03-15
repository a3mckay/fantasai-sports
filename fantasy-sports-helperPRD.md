# FantasAI Sports — MLB Fantasy Assistant
## Product Requirements Document (PRD) v1.0

**Author:** Andrew McKay
**Last Updated:** March 13, 2026
**Target Launch:** MLB Opening Day 2026

---

## 1. System Overview

FantasAI Sports is an intelligent fantasy baseball assistant that helps managers make better decisions by combining statistical analysis, predictive modeling, and AI-generated insights. The MVP focuses on MLB head-to-head category leagues on Yahoo Fantasy, with an architecture designed to support additional sports (NFL, NBA, NHL) and platforms in the future.

The system analyzes real player performance data, generates current and predictive player rankings, and provides league-aware recommendations — including waiver pickups, roster optimization, and trade targets — tailored to each manager's specific team needs and league settings.

---

## 2. Architecture

The system is organized into three distinct layers. Each layer should be built as an independent module with clean interfaces between them.

### Layer 1 — The Engine (Data & Scoring)
- Ingests MLB play-by-play and statistical data via `pybaseball`
- Computes current player rankings based on counting/rate stats
- Computes predictive player rankings using underlying/predictive indicators
- Scoring models are configurable per league (stat categories, weights)
- Sport-agnostic interfaces: all sport-specific logic lives behind a "sport adapter" pattern so NFL, NBA, NHL can be added later by implementing the same interface

### Layer 2 — The Brain (League Intelligence)
- Understands a specific league's settings, rosters, and constraints
- Combines Engine rankings with league context to produce personalized recommendations
- Identifies team needs, waiver targets, trade opportunities, and roster moves
- Enforces league rules (max acquisitions/week, roster position constraints, keeper rules)
- Generates LLM-powered analysis blurbs for ranked players and recommendations

### Layer 3 — The Interface (User Interaction)
- **MVP:** CLI or simple web dashboard for viewing rankings and recommendations
- **Future:** Twilio SMS for push notifications and transaction execution
- **Future:** Conversational AI for natural language queries about trades, comparisons, etc.
- **Future:** Full web UI at fantasaisports.com

---

## 3. Data Sources

| Source | Data Provided | Priority |
|--------|--------------|----------|
| `pybaseball` | MLB play-by-play, player stats, Statcast data, game logs | Critical — primary stat engine |
| The Odds API | Vegas betting lines, over/unders, player props | High — informs projections |
| OpenWeather API | Game-day weather for outdoor stadiums | Medium — projection modifier |
| OpenAI API (→ Anthropic future) | LLM-generated player blurbs and analysis | High — content generation |
| Yahoo Fantasy API | League settings, rosters, transactions, waivers | Future (post-MVP) |

### Data Pipeline Notes
- `pybaseball` is new territory — early spike needed to understand data format, reliability, and gaps
- Play-by-play data is the preferred source (weekly files were too flakey in the NFL build)
- All player data should be normalized into a common schema before entering the scoring engine
- Data refresh cadence: daily during MLB season (games are daily)

---

## 4. Sport: MLB

### 4.1 Stat Categories (Configurable)

The system must support arbitrary stat category configurations. The reference league uses 6x6 head-to-head categories:

**Hitting (6):** R, HR, RBI, SB, AVG, OPS
**Pitching (6):** IP, W, SV, K, ERA, WHIP

The scoring engine must treat stat categories as configuration, not hardcoded values. Every league may use different categories. The system should be able to ingest a league's category list and dynamically adjust rankings and recommendations.

### 4.2 Position Groups

**Hitters:** C, 1B, 2B, 3B, SS, OF, Util (DH-eligible)
**Pitchers:** SP, RP (some slots accept either as P)

Position eligibility is important for recommendations — a player is only a useful waiver target if they can fill a roster slot.

### 4.3 Current Player Rankings (Lookback)

These rankings reflect **what has already happened** — who are the best performers so far this season.

**Scoring approach:**
- Weight counting stats and rate stats according to the league's category configuration
- A player who contributes across many categories is more valuable than a one-category specialist
- Positional scarcity matters: a C hitting .280 with 15 HR is more valuable than a 1B with the same line
- Rankings should be segmented: overall, by position, hitters-only, pitchers-only

**Input stats (hitters):** PA, AB, R, H, HR, RBI, SB, BB, K, AVG, OBP, SLG, OPS, wOBA, wRC+
**Input stats (pitchers):** IP, W, L, SV, HLD, K, BB, ERA, WHIP, FIP, xFIP, K/9, BB/9, K/BB, HR/9

### 4.4 Predictive Player Rankings (Forward-Looking)

These rankings predict **who will perform best in the upcoming period** (next week, next 2 weeks, rest of season).

**Scoring approach — different from lookback rankings:**
- Emphasize *predictive* stats over *descriptive* stats
- Hitters: xwOBA, xBA, xSLG, Hard Hit %, Barrel %, sprint speed, launch angle, pull rate, flyball rate, groundball rate, line drive rate, swing speed, whiff rate, chase rate, expected stats vs. actual (regression candidates)
- Pitchers: xERA, xFIP, SIERA, Stuff+, K% vs. xK%, CSW%, groundball rate, flyball rate, line drive rate allowed, swing speed against, whiff rate induced, chase rate induced, expected stats vs. actual
- Factor in schedule: number of games, opponent quality, home/away splits
- Factor in external modifiers: weather (temperature, wind for outdoor parks), Vegas lines (implied runs), injury status
- Factor in recent trends: hot/cold streaks, workload concerns, platoon situations

**Key insight from NFL build:** The lookback and predictive models must use *different stat weightings*. Counting stats describe the past; underlying metrics and external factors predict the future. This distinction is the core value proposition.

### 4.5 LLM-Generated Blurbs

Every ranked player (in both lookback and predictive rankings) should have an AI-generated blurb that:
- Reads like a fantasy baseball analyst wrote it (not robotic or generic)
- Cites specific stats that justify the ranking
- For predictive rankings: explains *why* the player is expected to perform well/poorly (matchup, Statcast trends, regression, etc.)
- Is concise: 2-4 sentences per player
- Includes upside/downside framing where appropriate ("ceiling/floor" language)

**Blurb examples (aspirational tone):**

> **Lookback:** "Julio Rodriguez has been a five-category monster over the last 30 days, slashing .312/.375/.598 with 8 HR and 5 SB. He's one of only three players contributing elite value in both power and speed categories. The OPS upside makes him especially valuable in 6x6 formats."

> **Predictive:** "Logan Webb projects as a top-10 SP this week with two home starts against the Rockies and Marlins. His 3.12 xFIP is nearly a full run below his 4.01 ERA, suggesting positive regression ahead. The matchups are the cherry on top — Colorado and Miami rank bottom-5 in wRC+ vs. RHP."

---

## 5. League Settings & Constraints (Reference League)

These settings should be stored as league configuration, not hardcoded. The system must support varying league rules.

| Setting | Value |
|---------|-------|
| League type | Head-to-head categories |
| Teams | 12 |
| Format | Keeper (8 keepers/team; min 3 hitters, 3 pitchers) |
| Categories (hitting) | R, HR, RBI, SB, AVG, OPS |
| Categories (pitching) | IP, W, SV, K, ERA, WHIP |
| Max acquisitions/week | 4 |
| Max acquisitions/season | 60 |
| Minimum IP/week | 15 |
| Waiver type | Continual rolling list |
| Waiver time | 1 day |
| Weekly deadline | Daily — today |
| Trade review | League votes (6 to veto) |
| Trade reject time | 1 day |
| Playoffs | 6 teams, weeks 23-25 |
| Playoff tie-breaker | Best regular season record vs. opponent wins |
| Playoff reseeding | Yes |
| Lock eliminated teams | Yes |

### Roster Construction

| Slot | Count | Notes |
|------|-------|-------|
| C | 1 | |
| 1B | 1 | |
| 2B | 1 | |
| 3B | 1 | |
| SS | 1 | |
| OF | 3 | |
| Util | 1 | Any hitter |
| SP | 2 | |
| RP | 2 | |
| P | 3 | SP or RP eligible |
| BN | 5 | Bench |
| IL | 3 | Injured list |
| NA | 1 | Minor league / not available |

**Active hitters:** 10 (including Util)
**Active pitchers:** 7 (2 SP + 2 RP + 3 P flex)
**Total roster:** 26

---

## 6. MVP Feature Scope (Opening Day Target)

### P0 — Must Have (Priority 1: Waiver/Pickup Recommendations)

**What it does:** Given a manager's roster and the league's available player pool, identify the best waiver wire and free agent pickups.

**Requirements:**
- Input: manager's current roster (manual input for MVP — paste player names or upload)
- Input: league category configuration
- Engine identifies which categories the team is weakest in
- Engine scans available players (not on any roster) and ranks them by their ability to fill team needs
- Recommendations include: player name, position, key stats, which categories they help, who to drop (if applicable)
- Each recommendation has an LLM-generated blurb explaining the rationale
- Respects league constraints: position eligibility, roster limits, max 4 acquisitions/week
- Recommendations are refreshed daily

### P0 — Must Have (Priority 2: Predictive Rankings)

**What it does:** Rank players by expected future performance for the upcoming scoring period.

**Requirements:**
- Generate positional rankings (C, 1B, 2B, 3B, SS, OF, SP, RP) for the next week
- Use predictive stats, schedule, weather, and Vegas lines as inputs
- Each ranked player has an LLM-generated blurb
- Support "rest of season" projections as a secondary view
- Flag regression candidates (overperformers due for a decline, underperformers due for a bounce)

### P1 — Should Have (Priority 3: Current Rankings)

**What it does:** Rank all MLB players by season-to-date performance, calibrated to the league's scoring categories.

**Requirements:**
- Overall rankings and positional rankings
- Configurable to any category set (not just 6x6)
- Category contribution breakdown per player (which categories they help/hurt)
- LLM-generated blurbs for top players
- Positional scarcity adjustment

### P2 — Deferred (Priority 4: Yahoo Integration)

**What it does:** Automatically read league data from Yahoo Fantasy API.

**Deferred because:** Manual roster input is acceptable for MVP. Yahoo OAuth adds significant complexity (app registration, token refresh, rate limits). Better to validate the engine and brain logic first.

**When we build it:**
- OAuth 2.0 flow with Yahoo
- Read: league settings, all team rosters, available players, matchup schedule, transaction history
- Write (future): execute waiver claims, propose trades, set lineups

### P2 — Deferred (Future Features)

- Twilio SMS notifications and transaction execution
- Conversational AI interface (ask questions about your league)
- Trade analyzer and trade partner finder
- Lineup optimizer (daily start/sit decisions respecting min IP)
- Keeper value rankings (future value + surplus value above draft cost)
- Multi-sport support (NFL, NBA, NHL)
- Multi-platform support (ESPN, Sleeper, Fantrax)
- Web UI at fantasaisports.com

---

## 7. Tech Stack

| Component | Choice | Notes |
|-----------|--------|-------|
| Language | Python | Primary — data pipeline, scoring engine, API |
| Framework | FastAPI | API layer — async, auto-generated docs, modern Python |
| Database | PostgreSQL (Railway) | Persistent storage for player data, rankings, league configs |
| LLM | OpenAI API (GPT-4) | Blurb generation; migrate to Anthropic API later |
| Data source | `pybaseball` | MLB statistical data |
| Odds data | The Odds API | Betting lines for projections |
| Weather | OpenWeather API | Game-day conditions |
| Repo | GitHub | Source control |
| Hosting | Railway | App hosting + managed Postgres |
| Domain | fantasaisports.com (Namecheap) | Future web UI |
| IDE | Claude Code | Development environment |

---

## 8. Data Model (Core Entities)

### Players
- `player_id` (primary key — use MLB's official player IDs from pybaseball)
- `name`, `team`, `positions[]` (multi-position eligible)
- `status` (active, IL, minors, etc.)
- Sport-specific identifiers for cross-referencing

### PlayerStats (time-series)
- `player_id`, `season`, `week` (or date range)
- Raw counting stats (sport/position specific)
- Rate stats (AVG, OPS, ERA, WHIP, etc.)
- Advanced/predictive stats (xwOBA, xFIP, Barrel%, etc.)

### Rankings
- `player_id`, `ranking_type` (lookback vs. predictive), `period`
- `overall_rank`, `position_rank`
- `score` (composite scoring engine output)
- `category_contributions{}` (JSON — how much value in each scoring category)
- `blurb` (LLM-generated text)

### Leagues
- `league_id`, `platform` (yahoo, espn, etc.), `sport`
- `scoring_categories[]`, `league_type` (h2h categories, roto, points)
- `settings{}` (JSON — all league-specific rules)
- `roster_positions[]`

### Teams
- `team_id`, `league_id`, `manager_name`
- `roster[]` → references Players

### Recommendations
- `team_id`, `type` (waiver_add, trade_target, drop_candidate, start_sit)
- `player_id`, `action`, `rationale_blurb`
- `category_impact{}` (which categories this move improves)
- `priority_score`
- `created_at`, `expires_at`

---

## 9. Sport Adapter Pattern (Multi-Sport Architecture)

To support future sports without rewriting core logic, the system should use an adapter/plugin pattern:

```
┌─────────────────────────────────────────────┐
│              Core Engine                     │
│  (scoring, ranking, recommendations)         │
│  - Accepts normalized stat arrays            │
│  - Configurable category weights             │
│  - Position-agnostic roster logic            │
└────────────┬────────────────────────────────┘
             │ uses
┌────────────▼────────────────────────────────┐
│          Sport Adapter Interface             │
│  - fetch_player_data(season, week)           │
│  - get_positions() → [C, 1B, 2B, ...]       │
│  - get_available_stats() → [HR, AVG, ...]    │
│  - get_predictive_stats() → [xwOBA, ...]     │
│  - normalize_stats(raw) → common format      │
│  - get_schedule(week) → matchups             │
└────────────┬────────────────────────────────┘
             │ implemented by
    ┌────────┴────────┬──────────┬──────────┐
    ▼                 ▼          ▼          ▼
 MLBAdapter     NFLAdapter  NBAAdapter  NHLAdapter
 (pybaseball)  (nfl-data-py) (nba_api)  (nhl-api)
```

For MVP, only `MLBAdapter` is implemented. But the interface should be defined from the start so adding sports later is additive, not a rewrite.

---

## 10. Open Questions & Risks

1. **pybaseball reliability:** This is a new library for us. Need an early spike to validate data availability, format, and update cadence during the season. What happens when a game is in progress? How quickly do stats update?

2. **Statcast data access:** Advanced metrics (xwOBA, Barrel%, etc.) may have different availability than basic stats in pybaseball. Need to verify what's accessible and how current it is.

3. **LLM cost management:** Generating blurbs for hundreds of players daily could get expensive. Consider: caching blurbs (only regenerate when stats meaningfully change), tiering (top 100 get full blurbs, others get templated summaries), batching API calls.

4. **Season start timing:** If the league drafts before the engine is ready, manual roster input still lets us provide value during the season. The system doesn't need draft support for MVP.

5. **Rate stats vs. counting stats tension:** In H2H categories, a player who plays more games has more opportunity for counting stats. The predictive model needs to account for games played in the upcoming period.

6. **Minimum IP constraint:** The 15 IP minimum/week means the system should warn if a recommended pitcher drop would put the team at risk of not hitting the minimum. This is a real strategic constraint.

---

## 11. Success Criteria

The MVP is successful if, during the first month of the MLB season:

1. The system produces daily updated player rankings (lookback + predictive) that feel credible and useful
2. Waiver recommendations correctly identify players who would improve the user's team in weak categories
3. LLM blurbs are genuinely informative — not generic filler
4. The data pipeline runs reliably without manual intervention
5. At least one waiver pickup recommended by the system meaningfully helps the user's team

---

## Appendix A: Glossary of Key MLB Predictive Stats

| Stat | What It Measures | Why It's Predictive |
|------|-----------------|-------------------|
| xwOBA | Expected weighted on-base average (Statcast) | Removes luck/defense — shows true offensive quality |
| xBA | Expected batting average | Based on exit velocity + launch angle, not actual outcomes |
| xSLG | Expected slugging percentage | Same methodology as xBA but for power |
| Barrel% | % of batted balls hit at optimal exit velo + launch angle | Strongly correlates with future HR and XBH |
| Hard Hit% | % of batted balls ≥95 mph exit velocity | Underlying quality of contact |
| Sprint Speed | Feet per second on competitive runs | Predicts SB opportunity and success |
| Pull Rate | % of batted balls hit to pull side | Indicates power approach; extreme pull can signal vulnerability |
| FB/GB/LD Rate | Flyball, groundball, and line drive percentages | Batted ball profile predicts HR upside (FB) and AVG sustainability (LD) |
| Swing Speed | Bat speed in mph (Statcast) | Correlates with hard contact and power ceiling |
| Whiff Rate | Swings and misses / total swings | Measures contact ability (hitters) or swing-and-miss stuff (pitchers) |
| Chase Rate | Swings at pitches outside zone / pitches outside zone | Plate discipline indicator; high chase = regression risk |
| xERA | Expected ERA (Statcast) | Based on quality of contact allowed, not outcomes |
| xFIP | Expected FIP (fielding independent) | Normalizes HR/FB rate to league average |
| SIERA | Skill-Interactive ERA | Accounts for batted ball types and K/BB |
| Stuff+ | Pitch quality metric | Measures how "nasty" a pitcher's stuff is independent of results |
| CSW% | Called strikes + whiffs / total pitches | Measures pitch effectiveness |
| K-BB% | Strikeout rate minus walk rate | Single best predictor of pitcher quality |

---

## Appendix B: Getting Started with Claude Code

### Setup Steps

1. **Install Claude Code** (if not already installed):
   ```bash
   npm install -g @anthropic-ai/claude-code
   ```
   Requires Node.js 18+. See https://docs.anthropic.com for current install instructions.

2. **Create your repo and add this PRD:**
   ```bash
   mkdir fantasai-sports && cd fantasai-sports
   git init
   cp /path/to/PRD.md ./PRD.md
   git add PRD.md && git commit -m "Add PRD"
   ```

3. **Launch Claude Code from the repo root:**
   ```bash
   claude
   ```

4. **First prompt — the pybaseball spike:**
   Start with this to de-risk the data pipeline before building anything else:
   ```
   Read PRD.md for full project context. Before we build anything, I need to
   validate that pybaseball gives us the data we need. Write a Python script
   that:
   1. Installs pybaseball
   2. Pulls 2025 batting stats and pitching stats (or 2024 full season if
      2025 isn't available yet)
   3. Pulls Statcast data (xwOBA, xBA, Barrel%, Hard Hit%, etc.)
   4. Shows me: what columns are available, how many players, any gaps or
      missing data, and how the data is structured
   5. Saves sample output to a CSV so I can eyeball it

   Don't build the full pipeline yet — this is just a data exploration spike.
   ```

5. **Second prompt — project scaffolding:**
   Once you've validated pybaseball works:
   ```
   Read PRD.md. Now set up the project structure:
   - Python project with pyproject.toml
   - FastAPI app skeleton
   - PostgreSQL connection config (Railway — I'll provide the connection string)
   - The sport adapter interface from Section 9 of the PRD
   - MLBAdapter stub that uses pybaseball
   - Database models from Section 8
   - A basic Makefile or scripts for common tasks (run server, refresh data, etc.)

   Don't implement business logic yet — just clean scaffolding I can build on.
   ```

6. **Third prompt — build the engine (Layer 1):**
   ```
   Read PRD.md sections 4.3 and 4.4. Build the MLB scoring engine:
   - Implement the data pipeline: pybaseball → normalized stats → Postgres
   - Build the lookback ranking model using the league's 6x6 categories
   - Build the predictive ranking model using the Statcast/advanced metrics
   - Make stat categories configurable (not hardcoded to 6x6)
   - Include positional scarcity adjustments
   - Write tests for the scoring logic
   ```

7. **From there, follow the PRD priority order:**
   - Waiver recommendation engine (P0-1)
   - Predictive rankings with blurbs (P0-2)
   - Current rankings with blurbs (P1)
   - Yahoo integration (P2, deferred)

### Tips for Working with Claude Code

- **Always point it to the PRD:** Start prompts with "Read PRD.md" so it has full context.
- **Work in focused chunks:** One feature or module per session. Don't ask it to build everything at once.
- **Commit frequently:** After each working milestone, commit. Claude Code can run git commands.
- **Test as you go:** Ask Claude Code to write tests alongside features. This prevents the "it worked yesterday but broke today" problem.
- **When something breaks:** Paste the full error. Claude Code can read stack traces and fix them in place — no copy-paste loop needed.
- **Use CLAUDE.md:** Create a `CLAUDE.md` file in your repo root with project conventions, commands, and notes. Claude Code reads this automatically for context on every interaction.

---

*This document is the single source of truth for the FantasAI Sports MLB MVP. It should live in the repo root and be referenced by Claude Code throughout development.*

