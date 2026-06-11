# FantasAI Sports — Work Plan

> Living spec for active bug work and upcoming features.
> **Status key:** `[ ]` not started · `[~]` in progress · `[x]` done · `[!]` blocked
>
> **Process:** Review and approve each item before build starts. Claude checks in before
> moving to the next item. File is updated as work completes.

---

## Part 1 — Bug Fixes & Infrastructure

### B-1 · FanGraphs Season Stats Sync Failure `[ ]`

**What's broken**
FanGraphs stopped syncing successfully around April 6, 2026. The `sync_current_season_stats()`
pipeline function silently swallows the error and continues. Production DB state as of June 11:

| Metric | Source | Status |
|---|---|---|
| K%, BB% — batters | FanGraphs | ⚠️ Stale (last synced April 6) |
| K%, BB%, K-BB%, xFIP, SIERA — pitchers | FanGraphs | ❌ Never in actual rows |
| Barrel%, EV, xwOBA, BatSpeed | Savant | ✅ Syncing daily |
| HR, R, RBI, IP, K, BB (counting) | BRef | ✅ Syncing daily |

**Impact on rankings**
Moderate. Rankings still look reasonable because:
- Steamer projections (working) supply talent-level K%/BB% for the `steamer_z` component
- Savant metrics (working) supply Barrel%, vFA, PitchRV100 for `statcast_z`
- Missing: actual-season K%/BB%/K-BB%/SIERA/xFIP trends in the statcast component

Players who are striking out significantly more or less than their Steamer projection this year
are not having that signal reflected in rankings.

**Likely causes**
1. FanGraphs rate-limiting or IP-blocking the pybaseball scraper (most probable)
2. pybaseball update that changed the response format
3. The FanGraphs leaderboard URL or auth changed

**Investigation steps**
- [ ] Run `batting_stats(2026, qual=0)` manually in a Python shell and capture the error
- [ ] Check pybaseball version; compare to changelog for breaking changes
- [ ] If rate-limited: add retry with exponential backoff + longer inter-request delay
- [ ] If blocked: evaluate switching to direct FanGraphs CSV endpoint (the leaderboard
      exports a `?type=...&download=true` CSV that may not be rate-limited)

**Acceptance criteria**
- `sync_current_season_stats()` successfully populates K%, BB%, wRC+/FIP for 300+ batters/pitchers
- Rows have `updated_at` within 24 hours of today
- No silent swallowing — if the sync fails, it logs a warning visible in the Railway log stream

---

### B-2 · FanGraphs Dependency Reduction (follows B-1) `[ ]`

**Context**
Even after fixing B-1, FanGraphs is a fragile dependency — it's a scraped leaderboard, not an
official API. Several metrics we rely on for scoring (K-BB%, SwStr%, CSW%, SIERA, Stuff+) come
exclusively from FanGraphs and have no Savant equivalent.

**Plan**
Audit which metrics in `PITCHER_COMPOSITES` and `BATTER_COMPOSITES` currently come from
FanGraphs and have no reliable alternative source. For each:
- **If available from Savant**: switch source (e.g., SwStr% is available from Savant swing-take)
- **If computable from BRef counts**: derive it (K% = K/PA, BB% = BB/PA, ISO = SLG - AVG)
- **If FanGraphs-only with no alternative**: document and accept the risk

**Acceptance criteria**
- Scoring engine produces correct results even when `batting_stats()` / `pitching_stats()` return None
- At minimum: K%, BB%, K-BB% derivable from BRef counting stats as fallback

---

## Part 2 — "Recommend a Player" Page Redesign

> Current page has two tabs: Roster Analysis + Find a Player.
> Redesign: two tabs — **Roster Analysis** (unchanged) + **Add a Player** (new).

### F-1 · Top Adds Panel (backend endpoint) `[ ]`

**What it does**
A new API endpoint that returns up to 15 add/drop pairs for a given team. Each pair identifies
the best available free agent and the most logical player to drop to make room, accounting for:
1. Roster slot compatibility — accounting for Util, multi-position eligibility, and bench flexibility (see algorithm below)
2. RoS score gap (FA must be a meaningful upgrade)
3. Drop candidate droppability: weighted by lowest RoS score + most negative `rank_delta` +
   biggest gap between actual-season performance and Steamer projection
4. Category impact of the swap (net change per scoring category)
5. Deduplication: each rostered player can only appear as a suggested drop once

**Endpoint**
`GET /api/v1/recommendations/{team_id}/top-adds?limit=15`

**Response schema (new)**
```
TopAddPair:
  add_player:       PlayerRankingRead   # free agent
  drop_player:      PlayerRankingRead   # suggested drop
  swap_type:        "direct_upgrade" | "category_need" | "both"
  category_impact:     dict[str, float]    # per-category delta (positive = gain)
  slot_fit:            str               # "direct" (FA fills exact slot) | "util" | "bench"
  tight_position_match: bool             # True when FA and drop share a rare position (C/SS/2B/3B)
```

**Position compatibility rules**

Fantasy rosters are more flexible than a simple position-to-position match:
- **Util slot**: every league has one or more. Any hitter qualifies. A batter FA can therefore
  fill Util regardless of their specific position — meaning a batter FA can in principle replace
  *any* rostered batter. Position-specific eligibility matters for optimising the swap cleanness,
  not for gating it.
- **Multi-position eligibility**: a player who qualifies at 2B/SS can occupy a 2B slot, SS slot,
  or Util. Their full position list (not just primary) must be used when evaluating fit.
- **Bench (BN)**: any player can sit on bench; a bench player being dropped doesn't constrain
  which FA you add beyond stat type.
- **Pitching slots**: SP and RP eligibility are distinct in most leagues. SP FAs should pair
  primarily with SP drop candidates; RP FAs with RP. Cross-matching (SP added for RP drop) is
  only surfaced if the team has a flex P slot in `roster_positions`.
- **Tight-position bonus**: dropping a C to add a C, or an SS to add an SS, is a cleaner swap
  than shuffling pieces through Util. Pairs where the FA directly fills the vacated slot get a
  small score bonus so they surface ahead of Util-only matches.

**Algorithm (rule-based, no LLM)**
1. Pull season/predictive rankings for all players (reuse existing scoring engine)
2. Compute owned player set from all team rosters in the league
3. For each top-60 FA by overall rank:
   a. Determine the FA's stat type (batting or pitching) and full position list
   b. Candidate drops = all rostered players on the target team of the same stat type
      (any batter can be dropped to make room for a batter FA via Util; pitching is SP/RP-aware)
   c. Score each drop candidate:
      `drop_score = -ros_score + abs(min(rank_delta, 0)) * 0.5 + tight_position_bonus`
      where `tight_position_bonus` is a small positive weight when FA and drop share a rare
      position (C, SS, 2B, 3B) directly
   d. Pick the highest-scoring drop candidate not already used in a previous pair
4. Compute category_impact from the difference in each player's `category_contributions`
5. Classify swap_type: direct_upgrade if FA rank < drop rank by 20+; category_need if
   category_impact shows strong improvement in any team weak category; both if both are true
6. Return top 15 pairs sorted by (FA rank + drop urgency score)

**Acceptance criteria**
- Endpoint returns in <3s for any team
- Batter FAs are never paired with pitcher drop candidates (and vice versa)
- No player appears as a drop candidate in more than one pair
- `category_impact` sums are non-zero for any pair where both players have category data
- A league with a Util slot produces batter pairs even when no exact position overlap exists

---

### F-2 · Top Adds Panel (frontend) `[ ]`

**Dependencies:** F-1 complete

**What it does**
Replaces the current "Find a Player" tab with "Add a Player". The tab has two sections:

**Section 1 — Top Adds** (auto-loads on first tab visit)
- Position filter pills: All / C / 1B / 2B / 3B / SS / OF / SP / RP
- Up to 15 add/drop pair cards, each showing:
  - ADD row: overall rank, name, position pills with rank (e.g. `SP #2`), trend delta badge (↑8 / ↓3 / new)
  - DROP row: name, position pills, trend delta, brief drop rationale (rule-generated string)
  - Category impact chips: `+K 0.42`, `+W 0.18`, `-ERA 0.21`
- Refresh button
- Loading/empty/no-league states

**Section 2 — Targeted Search** (existing Find a Player form, unchanged)
- Separated by a labelled divider
- All existing functionality preserved

**Acceptance criteria**
- Tab auto-loads Top Adds on first visit (no button press needed)
- Switching position filters does not re-fetch (client-side filter)
- Targeted Search still works exactly as before
- No regression on Roster Analysis tab

---

### F-3 · Rolling Savant Advanced Stats Infrastructure `[ ]`

**What it does**
Extends the rolling stats pipeline to capture advanced/Statcast metrics for 7/14/30/60-day windows.
Currently rolling stats (via BRef `batting_stats_range`) only have traditional counting and rate stats.
This adds a parallel sync from Baseball Savant using the same date-range capability we already use
for the bat-tracking endpoint.

**DB change**
Add `advanced_stats: JSON` column to `PlayerRollingStats` model + Alembic migration.

**New pipeline function:** `sync_rolling_advanced_stats(db, season, windows=[7, 14, 30, 60])`

For each window, fetches from Savant leaderboards with `startDate`/`endDate`:

| Metric group | Savant endpoint | Batters | Pitchers |
|---|---|---|---|
| xwOBA, xBA, xSLG, Barrel%, HardHit% | `expected_statistics` | ✅ | ✅ |
| EV (exit velo), EV50, launch angle | `exit-velocity-barrels` | ✅ | — |
| BatSpeed, Blast%, Squared-Up% | `bat-tracking` | ✅ | — |
| Whiff%, chase% (O-Swing%), CSW% | `swing-take` | — | ✅ |
| Pitch velocity per type | `pitch-arsenal-stats` | — | ✅ |

All endpoints use the same direct `urllib.request` + CSV pattern already established in
`sync_statcast_advanced_stats()`. Date ranges are parameterised by window length.

Minimum thresholds per window (skip noisy small samples):
- Batters: 10 PA (7d), 20 PA (14d), 40 PA (30d), 80 PA (60d)
- Pitchers: 3 IP (7d), 5 IP (14d), 10 IP (30d), 18 IP (60d)

**Nightly pipeline integration**
Add `sync_rolling_advanced_stats()` call in `main.py` nightly job, after existing
`sync_rolling_windows()`.

**Acceptance criteria**
- `PlayerRollingStats.advanced_stats` populated for 7/14/30/60-day windows for 200+ batters and 150+ pitchers
- Missing/null handled gracefully (player with < min PA simply has no record for that window)
- No FanGraphs dependency

---

### F-4 · Risers Panel `[ ]`

**Dependencies:** F-3 complete

**What it does**
A third section within the "Add a Player" tab that surfaces players who aren't yet highly ranked
but whose rolling advanced metrics show a meaningful positive shift — the "Jordan Walker" signal.

A riser is a player who qualifies on ALL of:
1. Overall rank > 50 (not already priced in by the market)
2. Unowned in the user's league
3. At least one key advanced metric in their most recent rolling window (7 or 14 days) is
   significantly better than their 30 or 60-day baseline for that same metric

**Signal directionality — batters**

All signals compare the most recent short window (7d or 14d) against the 30d or 60d baseline.
Thresholds are constants tunable without code changes.

| Metric | Direction | Threshold (7d vs 30d) | Notes |
|---|---|---|---|
| xwOBA | ↑ | ≥ +0.040 | Best single signal. Removes luck from contact |
| Barrel% | ↑ | ≥ +3 pp | Strongest power predictor. Precedes HR/SLG by days |
| HardHit% | ↑ | ≥ +5 pp | Broader than barrel; corroborates xwOBA |
| EV (avg exit velo) | ↑ | ≥ +2 mph | Overall contact hardness |
| EV50 | ↑ | ≥ +2 mph | Top-50% exit velo; removes weak-contact noise |
| xBA | ↑ | ≥ +0.030 | If xBA rising but AVG not yet, regression incoming |
| BatSpeed | ↑ | ≥ +1.5 mph | Raw swing speed. Mid-season tick often = mechanical fix |
| Blast% | ↑ | ≥ +3 pp | Combines bat speed + contact efficiency |
| Squared-Up% | ↑ | ≥ +3 pp | Better bat-to-ball mechanics |
| K% | ↓ | ≥ −4 pp | Fewer strikeouts = more contact. Computed from BRef (K/PA) |
| BB% | ↑ | ≥ +2 pp | Better pitch recognition. Computed from BRef (BB/PA) |
| Launch Angle | → 10–25° | Δ toward 17° ≥ 4° | **Not simply up or down.** Ground-ball hitters (LA < 8°) moving toward 12–20° is the classic "air ball revolution" breakout. LA already above 25° moving higher adds pop-ups, not power. Signal is: `abs(LA − 17)` shrinking |
| Sprint Speed | ↑ | ≥ +0.3 ft/s | SB and infield hit value. Less common signal but meaningful for speed profiles |

**Caution — batters:**
- BABIP alone is not a riser signal (can be luck). Only meaningful when xwOBA or Barrel% also rises.
- AVG/OPS in a 7-day window are results-based and lag Statcast by days to weeks. Use as confirmation only.

---

**Signal directionality — pitchers**

Key principle: for contact-quality metrics (xwOBA, xBA, Barrel%, HardHit%) the direction is
**opposite** to batters — we are measuring what the pitcher *allows*.
For swing-and-miss metrics (Whiff%, Chase%, CSW%) the direction is also opposite to batters.

| Metric | Direction | Threshold (7d vs 30d) | Notes |
|---|---|---|---|
| xwOBA against | ↓ | ≥ −0.035 | Best single signal. Hitters making worse contact |
| xBA against | ↓ | ≥ −0.025 | Leading indicator before ERA/WHIP improves |
| Barrel% against | ↓ | ≥ −3 pp | Fewer elite hits allowed |
| HardHit% against | ↓ | ≥ −5 pp | Softer contact allowed overall |
| Whiff% | ↑ | ≥ +4 pp | Best swing-and-miss signal. Rising = better pitch shape or new weapon |
| Chase% (O-Swing%) | ↑ | ≥ +4 pp | Getting hitters to expand zone. Opposite of batters |
| CSW% | ↑ | ≥ +3 pp | Called Strike + Whiff. Comprehensive "getting ahead" metric |
| SwStr% | ↑ | ≥ +3 pp | Swinging strikes / total pitches. Similar to Whiff%; both valid |
| K% | ↑ | ≥ +4 pp | More strikeouts. Computed from BRef (K/PA). Opposite of batters |
| BB% | ↓ | ≥ −2 pp | Fewer walks. Computed from BRef (BB/PA). Opposite of batters |
| K-BB% | ↑ | ≥ +4 pp | Net strikeout rate. Most reliable short-window command indicator |
| vFA | ↑ | ≥ +1.5 mph | Velocity tick mid-season often signals return from minor injury or mechanical fix. Strongly predicts Whiff% improvement |
| PitchRV100 | ↑ | ≥ +0.5 | Run value per 100 pitches improving. Reflects pitch-mix effectiveness overall |

**Caution — pitchers:**
- ERA/WHIP in a 7-day window: extreme luck variance (one bad inning = 3+ ERA spike). Only use at 30d, and only as confirming signal, never trigger.
- BABIP against: low BABIP can mean good command *or* just luck. Only signal when paired with declining hard-contact metrics.
- HR/9 in short windows: too sparse — a single HR meaningfully moves this.

---

**Confidence scoring**

Each triggered signal adds 1 point to the player's confidence score (max 5).
Bonus +1 if the same signal holds at both 7d *and* 14d (persistence bonus).

| Score | Label | Display |
|---|---|---|
| 1 | Flickering | 1 signal chip |
| 2–3 | Emerging | 2–3 signal chips |
| 4–5 | Strong | 3 chips + highlight border |

Only show players with confidence ≥ 2 in the panel. A single triggered signal is noise;
two or more overlapping signals are a pattern worth surfacing.

**Display**
Each riser card shows:
- Name, team, positions + position ranks
- Overall rank + rank_delta trend badge
- Triggered signal chips: `↑ xwOBA +.052 (7d)`, `↑ Barrel% +4.1pp (7d)`, `↑ Whiff% +5.2pp`
- Confidence indicator (Emerging / Strong)
- Blurb (if available)

**Acceptance criteria**
- Panel shows 5–10 risers per position filter (confidence ≥ 2 only)
- No riser appears in the Top Adds section (deduped)
- Correct directionality enforced for each metric per stat type (batters vs pitchers)
- All thresholds defined as named constants — tunable without touching algorithm logic
- Launch angle signal uses distance-to-optimal (abs(LA − 17)) not raw direction

---

## Sequencing

```
B-1  FanGraphs fix          ──► B-2  FanGraphs reduction
                                      │
F-1  Top Adds backend  ──► F-2  Top Adds frontend
                                      │
F-3  Rolling Savant infra  ──► F-4  Risers panel
```

F-1/F-2 and F-3/F-4 are independent tracks. B-1 should happen first but does not block F-1+.

**Recommended order:**
1. B-1 (investigate + fix FanGraphs)
2. F-1 + F-2 (Top Adds — highest user-facing value, no new infra)
3. F-3 (rolling Savant infra — foundational)
4. F-4 (Risers — depends on F-3)
5. B-2 (FanGraphs reduction — longer-term resilience)

---

## Completed Work (reference)

- [x] Position ranks in rankings (all eligible positions) — `6d7f0bb`
- [x] App-wide audit — 7 bugs fixed — `db1b857`
- [x] Hide weekly/monthly projections; rename "Projections RoS" / "Season Rankings" — `1880740`
- [x] Savant pitch arsenal sync (vFA + PitchRV100) — `77493a5`
- [x] Castillo mlbam_id fix + win weight revert — `7de3632`
