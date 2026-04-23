# RBI – Roblox Bot Investigator (v0.18.0 Beta)

RBI (Roblox Bot Investigator) is a Discord bot that analyzes a Roblox account’s **network** — friends, following, or followers — to estimate how likely the account is being **fed by bots**.

It looks at:

- Outfit patterns and known combo matches
- Badge patterns in specific games such as `fisch`
- How concentrated each matched account is in the selected game
- How many matched accounts fall into higher-risk “botted” patterns
- How similar matched account names are to each other and to the target

---

## Features

- **Combo-based network scans**
  - Define your own outfit/item combos with `/rbi addcombo name:<name> ids:<id1 id2 ...>`.
  - Use `/rbi mycombos` to browse:
    - Built-in global combo categories
    - Your own saved personal combos
  - RBI organizes built-in combos into categories so you do not need to type every combo name manually. Current built-in categories are:
    - `defaultcombos` – classic default avatar combos such as bacon, beanie, and acorn
    - `xboxcombos` – Xbox-style default avatar combos such as john, oakley, claire, casey, lin, and serena
    - `freeoutfitcombos` – free outfit / bundle-based combos such as greenbean, junkbot, knightsofredcliff, dennis, lindsey, denny, linlin, kenneth, oliver, cindy, citylifewoman, squadghoulstedd, and summer
  - In `/rbi scan`, the `combonames` input can be:
    - A specific combo name, such as `bacon` or one of your saved custom names
    - A category keyword such as `defaultcombos`, `xboxcombos`, or `freeoutfitcombos`, which expands into every combo in that category
    - `mycombos`, which expands into all of your saved personal combos
    - `globalcombos`, which expands into all built-in global combos
    - `all`, which expands into both your personal combos and all built-in global combos; this is also the default when `combonames` is omitted
    - `none`, which disables combo filtering entirely and scans all visible accounts in the selected scan source
  - RBI removes duplicate combo names after expansion, so overlapping keywords do not cause the same combo to be scanned twice.
  - RBI resolves combo names against both your user-specific combos and the built-in global combo list before scanning.
  - Outfit matching supports:
    - `exact` – the account must wear **all** items in a combo
    - `inexact` – the account can wear **any subset** of a combo, and RBI reports the matched item count as X/Y

- **Duplicate-safe combo creation**
  - `/rbi addcombo` can reject duplicate personal combo names.
  - Personal combo names can also be blocked from colliding with global combo names or reserved scan keywords such as `all`, `none`, `mycombos`, `globalcombos`, `defaultcombos`, `xboxcombos`, and `freeoutfitcombos`.

- **Scan source modes**
  - `/rbi scan` supports a `scansource` option:
    - `friends` – scan the target’s friends list
    - `following` – scan accounts the target is following
    - `followers` – scan accounts following the target
  - Scan summaries and reruns preserve the chosen scan source.

- **Game-aware badge analysis**
  - Configure per-game targets with `/rbi addgame` and view them with `/rbi mygames`.
  - RBI can:
    - Count total badges vs. badges from the selected game
    - Apply a game-specific target badge count, such as `10` for `fisch`
    - Score accounts differently when they are below, near, or far above that target band
    - Apply a low-coverage rule when too few total badges come from the selected game
    - Reduce suspicion for accounts whose badge totals suggest broader legitimate play

- **Sus Score (0–100%)**
  - Combines:
    - How many accounts matched the selected combos
    - How suspicious those matched accounts look from badge behavior
    - How many fall into high-risk ranges
    - How strongly their names cluster with each other and the target
  - Produces a 0–100 “fed by bots likelihood” for the target account
  - Uses heuristics and should be treated as a signal, not proof

- **Rich Discord UX**
  - Slash command interface with commands such as `/rbi scan`, `/rbi help`, `/rbi addcombo`, `/rbi mycombos`, `/rbi addgame`, `/rbi mygames`, `/rbi csvexport`, and `/rbi csvimport`
  - Paginated scan results with per-account embeds and follow-up scan actions
  - Multi-page help view with:
    - **About** page
    - **Commands** page
    - **Formulas** page
  - Paginated combo viewing for global combo categories and personal combos in `/rbi mycombos`

---

## Commands

Core slash commands:

- `/rbi help`  
  Opens the multi-page help view with About, Commands, and Formulas.

- `/rbi ping`  
  Returns the current bot version.

- `/rbi addcombo name:<name> ids:<id1 id2 ...>`  
  Saves a named combo for the current user.

- `/rbi mycombos`  
  Lists built-in combo categories and your personal combos.

- `/rbi addgame key:<key> placeid:<id> [targetbadges:<n>]`  
  Registers a game by place ID and optionally stores a target badge count.

- `/rbi mygames`  
  Lists your saved game presets.

- `/rbi scan robloxusername:<name> [combonames:<names|mycombos|globalcombos|defaultcombos|xboxcombos|freeoutfitcombos|all|none>] [game:<key|none>] [matchmode:<exact|inexact>] [scansource:<friends|following|followers>]`  
  Scans a Roblox user’s selected relationship network for matching combos and optional badge analysis.

  - `matchmode`:
    - `exact` – account must wear **all** items in a combo
    - `inexact` – account can wear **any subset** of a combo; embeds show X/Y item matches

  - `scansource`:
    - `friends` – scan the target’s friends list
    - `following` – scan the accounts the target follows
    - `followers` – scan the accounts following the target

  - `game`:
    - Use a configured key such as `fisch` for badge analysis
    - Use `none` to disable badge/game analysis

- `/rbi csvexport`  
  Exports your saved combos and games as a single-line text payload.

- `/rbi csvimport data:<single-line-from-export>`  
  Imports combos and games from a previous RBI export.

- `/rbi debugscan`  
  Shows active scan state for the current channel.

---

## How RBI scores accounts

### Per-account badge likelihood

For each matched account and selected game:

1. RBI counts:
   - `total_badges` = all badges on the account
   - `game_badges` = badges from the selected game

2. If badge coverage from that game is too low, the account is treated as low or zero risk for that game.

3. RBI compares `game_badges` against the configured target badge count `T`. Accounts near the target band are treated as more suspicious than accounts far below or far above it.

4. If an account has many badges overall but only a small share from the selected game, the score is reduced to avoid over-flagging broader legitimate players.

### Sus Score

Across all matched accounts, RBI combines:

- Match quantity
- Per-account badge likelihood
- The number of very high-risk matched accounts
- Name similarity to the target and name clustering among matched accounts

The result is a final 0–100 estimate of how likely the target account is being fed by bots.

---

## Configuration overview

Important configuration points in code include:

- **Global combos**  
  Built-in combos and combo categories used by global scanning keywords

- **Global descriptions**  
  Friendly names/descriptions for built-in combos shown in combo listings

- **Global games**  
  Built-in games such as `fisch`, including:
  - `placeId`
  - `universeId`
  - Default badge target counts

- **API pacing / delay values**  
  Small request delays help reduce rate-limit pressure when fetching avatars, badges, presence, and friend counts

You can also configure many user-specific settings directly from Discord using `/rbi addcombo` and `/rbi addgame` without editing the code.

---

## Notes

- RBI relies on Roblox APIs, so some scans may be limited by API visibility, incomplete data, or rate limiting.
- Friend scans are constrained by what Roblox returns through the public API, while following/follower scans may require pagination.
- RBI is a helper tool and should be used alongside human review rather than as a ban decision system.

---

## Acknowledgements

- Roblox APIs for friends, following, followers, avatars, presence, and badges
- `discord.py` for the Discord bot framework
