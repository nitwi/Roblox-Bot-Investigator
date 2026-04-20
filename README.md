# (Beta Release) RBI – Roblox Bot Investigator

RBI (Roblox Bot Investigator) is a Discord bot that analyzes a Roblox account’s **network** (friends, following, followers) to estimate how likely the account is being **fed by bots**.

It looks at:

- Outfits (default/bacon-style combos)
- Badge patterns in specific games (like fisch)
- How focused each account is on that game
- How many accounts fall into high-risk “botted” patterns
- How similar matched accounts are to each other and to the target (name clustering)

---

## Features

- **Combo-based network scans**
  - Define item combos (e.g. bacon / starter outfits).
  - Scan a Roblox user’s **friends**, **following**, or **followers** and find those wearing any of your combos.
  - Support for:
    - User-defined combos (`/rbi setcombo`, `/rbi mycombos`)
    - Global default combos (bacon, beanie, acorn, john, greenbean)
    - Special keywords (`mycombos`, `globalcombos`, `all`, `none`)

- **Scan source modes (friends / following / followers)**
  - `/rbi scan` now supports a `scan_source` option:
    - `Friends` – scan the target’s friends list (default).
    - `Following` – scan the accounts the target is following.
    - `Followers` – scan the accounts that follow the target.
  - The scan summary explicitly shows which relationship list was scanned and how many accounts were visible via the Roblox API.
  - “Scan this account” buttons and additional scans preserve the chosen `scan_source`, so rescans use the same relationship mode as the original run.

- **Game-aware badge analysis**
  - Configure per-game targets (`/rbi addgame`, `/rbi mygames`).
  - For each matching account:
    - Count total badges vs badges from the target game.
    - Apply a game-specific **target badge count** (e.g. 10 for fisch).
    - Use **badge bands**:
      - Below target band → gradually increases suspicion.
      - Around target band (e.g. 8–12) → most suspicious (≈100%).
      - Far above target (up to ~4× target) → suspicion decays back down.
    - Enforce a **5% coverage rule**:
      - If < 5% of badges are from this game, treat as 0% botted for that game.
    - Apply a **ratio-based adjustment** above the target:
      - High ratio (e.g. 22/30 badges from the game) → keep most of the suspicion.
      - Low ratio (e.g. 40/240) → strongly reduce suspicion.

- **Sus Score (0–100%)**
  - Combines:
    - How many accounts match the combos (quantity factor).
    - How botted those accounts look from badges (badge factor).
    - How many are in the very high-risk range.
    - How clustered their names are relative to each other and to the target.
  - Produces a 0–100 “fed by bots likelihood” for the target account.
  - Uses heuristics; it’s a signal, not proof.

- **Rich Discord UX**
  - Slash command interface (`/rbi scan`, `/rbi help`, `/rbi csvexport`, `/rbi csvimport`, etc.).
  - Paginated scan results with per-account embeds.
  - Result embeds include:
    - Per-account badge summary for the selected game (when configured).
    - Per-account bot likelihood indicator.
    - Name match percentage vs the target and optional cluster markers.
  - Help view with:
    - **About** page (overview & reasoning)
    - **Commands** page (quick reference)
    - **Formulas** page (plain-English explanation + exact math)
  - Help buttons highlight the **current page** so users know where they are.

---

## Commands

Core slash commands:

- `/rbi help`  
  Opens the multi-page help view (About, Commands, Formulas).

- `/rbi ping`  
  Simple liveness check that returns the current bot version.

- `/rbi setcombo name:<name> ids:<id1 id2 ...>`  
  Save a combo of asset IDs under a name.

- `/rbi mycombos`  
  List your saved combos.

- `/rbi addgame key:<key> place_id:<id> [target_badges:<n>]`  
  Register a game with a place ID and optional target badge count.

- `/rbi mygames`  
  List your saved game presets.

- `/rbi scan roblox_username:<name> combo_names:<names|mycombos|globalcombos|all|none> [game:<key>] [match_mode:<exact|inexact>] [scan_source:<friends|following|followers>]`  
  Scan a Roblox user’s friends, following, or followers for matching combos and, optionally, game badge stats.

  - `match_mode`:
    - `exact` – account must wear **all** items in a combo.
    - `inexact` – account can wear **any subset**; embeds show X/Y items per combo.
  - `scan_source`:
    - `friends` – scan the target’s friends list (default).
    - `following` – scan the accounts the target follows.
    - `followers` – scan the accounts that follow the target.

- `/rbi csvexport`  
  Export your combos and games as a single-line text blob.

- `/rbi csvimport data:<single-line-from-export>`  
  Import combos and games from a previous export.

---

## How RBI scores accounts (short version)

### Per-account badge likelihood

For each matched account and game:

1. Count:
   - `total_badges` (all badges).
   - `game_badges` (badges from the target game).
2. If `total_badges == 0` or `game_badges / total_badges < 5%`, likelihood = **0%**.
3. Use the game’s target `T` (e.g. 10):
   - Below the target band (e.g. 1–7) → ramps from 0% toward 100%.
   - Target band (roughly 8–12) → treated as **100%** (typical bot range).
   - Far above target (up to ~4×T, e.g. ~40) → suspicion decays toward 0% using an exponential curve.
4. If `game_badges > T`, adjust based on **ratio**:
   - High ratio (many of their badges are from this game) → keep most of the score (exploit alts like 22/30).
   - Low ratio (they play many different games) → strongly reduce the score (legit grinders like 40/240).

### Sus Score

Across all matching accounts:

- Quantity factor `Q = min(matches × 10, 100)`.
- For each matched account, compute per-account likelihood `s_i`.
- Only consider accounts with `s_i ≥ 25` when aggregating badges.
- Compute:
  - `p` = fraction of matched accounts that are “risky” (sufficiently high `s_i`).
  - `avg_badge` = weighted average of `s_i` using weights based on `s_i`.
  - `k_red` = number of very high-risk accounts (`s_i ≥ 75`).
- Combine into a final 0–100 score; more risky accounts and higher `avg_badge` yield a higher Sus Score.
- Name similarity and clustering can add additional weight when multiple matched accounts share strong name fragments with each other or the target.

---

## Configuration overview

Some important configuration points (in code):

- **Global combos**  
  Default outfits (e.g. bacon) and their asset IDs.

- **Global games**  
  Built-in games like fisch, with:
  - `placeId` for Roblox.
  - `universeId` for looking up game badges.
  - Default target badge counts.

- **Delays and rate limits**  
  Small delays between Roblox API requests to reduce the chance of rate limiting when:
  - Fetching avatar outfits for many accounts.
  - Fetching badges.
  - Fetching presence or friend counts.

You can also use `/rbi addgame` and `/rbi setcombo` in Discord to configure these per-user **without** editing code.

---

## Acknowledgements

- Roblox APIs for friends, followers, following, avatars, presence, and badges.
- discord.py for the Discord bot framework.
