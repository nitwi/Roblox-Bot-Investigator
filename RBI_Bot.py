# RBI Bot v0.15 [Beta]

import os
import time
import asyncio
import requests
import discord
from discord import app_commands
from dotenv import load_dotenv
from datetime import datetime, timezone

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

BOT_VERSION = "v0.15 [Beta]"

# ------ CONFIG ------

USER_COMBOS: dict[tuple[str, str], set[int]] = {}

GLOBAL_COMBOS: dict[str, set[int]] = {
    "bacon": {144076760, 144076358, 63690008},
    "beanie": {382537569, 4047884939, 1772336109},
    "acorn": {62724852, 144076512, 144076436},
}

GLOBAL_DESCRIPTIONS: dict[str, str] = {
    "bacon": "Bacon (Default Male Account)",
    "beanie": "Beany (Default Genderless Account)",
    "acorn": "Acorn (Default Female Account)",
}

GLOBAL_GAMES: dict[str, dict] = {
    "fisch": {
        "key": "fisch",
        "placeId": 16732694052,
        "universeId": 5750914919,
    },
}

GLOBAL_BADGE_TARGETS: dict[str, int] = {
    "fisch": 10,
}

USER_GAMES: dict[tuple[str, str], dict] = {}
USER_BADGE_TARGETS: dict[tuple[str, str], int] = {}

AVATAR_REQUEST_DELAY = 0.25
BADGE_REQUEST_DELAY = 0.3
FRIEND_COUNT_REQUEST_DELAY = 0.15

FRIENDS_API = "https://friends.roblox.com/v1/users/{userId}/friends"
AVATAR_API = "https://avatar.roblox.com/v1/users/{userId}/avatar"
USERNAME_TO_ID_API = "https://users.roblox.com/v1/usernames/users"

USER_BADGES_API = "https://badges.roblox.com/v1/users/{userId}/badges"
PLACE_TO_UNIVERSE_API = "https://apis.roblox.com/universes/v1/places/{placeId}/universe"

GAME_ICONS_API = (
    "https://thumbnails.roblox.com/v1/games/icons"
    "?universeIds={universeId}&size=256x256&format=Png&isCircular=false"
)
USER_HEADSHOT_API = (
    "https://thumbnails.roblox.com/v1/users/avatar-headshot"
    "?userIds={userId}&size=150x150&format=Png&isCircular=false"
)

PRESENCE_API = "https://presence.roblox.com/v1/presence/users"

# Cache of seen user IDs per (user, target, combos, game, mode)
SCAN_CACHE: dict[tuple[int, str, str, str | None, str], set[int]] = {}

# ------ PER-CHANNEL ACTIVE SCAN / RESCAN STATE ------

# Only the latest scan per channel is allowed to emit plaintext results.
ACTIVE_SCAN_TOKENS: dict[int, str] = {}       # channel_id -> token
RESCAN_IN_PROGRESS: dict[int, bool] = {}      # channel_id -> bool

import uuid
import re

class ScanControl:
    def __init__(self):
        self.cancelled = False


# ------ DISCORD CLIENT WITH APP COMMANDS (GLOBAL SYNC) ------

class RBIClient(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        synced = await self.tree.sync()
        print(f"[DEBUG] Globally synced {len(synced)} application commands")
        for cmd in synced:
            print(f"[DEBUG] - /{cmd.name}")


client = RBIClient()


@client.event
async def on_ready():
    print(f"Logged in as {client.user} (ID: {client.user.id})")
    print(f"RBI Bot version: {BOT_VERSION}")
    print("[DEBUG] Commands in tree:", [c.name for c in client.tree.walk_commands()])
    print("Slash commands are ready.")


# ------ ROBLOX HELPERS ------

def get_user_id_from_username(username: str) -> int | None:
    payload = {"usernames": [username], "excludeBannedUsers": True}
    try:
        r = requests.post(USERNAME_TO_ID_API, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        arr = data.get("data", [])
        if not arr:
            return None
        return arr[0].get("id")
    except Exception:
        return None


def get_user_basic_info(user_id: int) -> tuple[str | None, str | None, datetime | None]:
    url = f"https://users.roblox.com/v1/users/{user_id}"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None, None, None
        data = r.json()
        name = data.get("name")
        display_name = data.get("displayName")
        created_str = data.get("created")
        created_dt = None
        if isinstance(created_str, str):
            try:
                created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            except ValueError:
                created_dt = None
        return name, display_name, created_dt
    except Exception:
        return None, None, None


def format_join_date(dt: datetime | None) -> str:
    if dt is None:
        return "Unknown"
    now = datetime.now(timezone.utc)
    delta_years = (now - dt).days / 365.25
    return f"{dt.date().isoformat()} (~{delta_years:.1f} years ago)"


def get_friends(user_id: int) -> list[dict]:
    url = FRIENDS_API.format(userId=user_id)
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data.get("data", [])


def get_avatar_assets(user_id: int) -> set[int]:
    url = AVATAR_API.format(userId=user_id)
    r = requests.get(url, timeout=10)
    if r.status_code != 200:
        return set()
    data = r.json()
    assets = data.get("assets", [])
    return {a.get("id") for a in assets if isinstance(a.get("id"), int)}


def friend_matches_exact(
    friend_assets: set[int],
    combos: list[tuple[str, set[int]]]
) -> list[tuple[str, int, int]]:
    matched: list[tuple[str, int, int]] = []
    for label, ids in combos:
        if not ids:
            continue
        if ids.issubset(friend_assets):
            matched.append((label, len(ids), len(ids)))
    return matched


def friend_matches_inexact(
    friend_assets: set[int],
    combos: list[tuple[str, set[int]]]
) -> list[tuple[str, int, int]]:
    matched: list[tuple[str, int, int]] = []
    for label, ids in combos:
        if not ids:
            continue
        overlap = friend_assets.intersection(ids)
        if overlap:
            matched.append((label, len(overlap), len(ids)))
    return matched


def resolve_combo_for_user(user_id: int, combo_name: str) -> tuple[str, set[int]] | None:
    user_key = str(user_id)
    lowered = combo_name.lower()
    key = (user_key, lowered)
    if key in USER_COMBOS:
        return (f"{combo_name} (your combo)", USER_COMBOS[key])
    if lowered in GLOBAL_COMBOS:
        desc = GLOBAL_DESCRIPTIONS.get(lowered, lowered)
        return (f"{desc} (global)", GLOBAL_COMBOS[lowered])
    return None


def place_to_universe(place_id: int) -> int | None:
    url = PLACE_TO_UNIVERSE_API.format(placeId=place_id)
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        uni = data.get("universeId")
        return uni if isinstance(uni, int) else None
    except Exception:
        return None


def get_user_badges(user_id: int, max_pages: int | None = None) -> list[dict]:
    badges: list[dict] = []
    cursor: str | None = None
    pages_fetched = 0

    while True:
        params = {"limit": 100, "sortOrder": "Asc"}
        if cursor:
            params["cursor"] = cursor
        url = USER_BADGES_API.format(userId=user_id)
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            break
        data = r.json()
        badges.extend(data.get("data", []))
        cursor = data.get("nextPageCursor")
        pages_fetched += 1
        if not cursor:
            break
        if max_pages is not None and pages_fetched >= max_pages:
            break
        time.sleep(BADGE_REQUEST_DELAY)
    return badges


def get_universe_badge_ids(universe_id: int, max_pages: int | None = None) -> set[int]:
    badge_ids: set[int] = set()
    cursor: str | None = None
    pages_fetched = 0

    while True:
        params = {"limit": 100, "sortOrder": "Asc"}
        if cursor:
            params["cursor"] = cursor
        url = f"https://badges.roblox.com/v1/universes/{universe_id}/badges"
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            break
        data = r.json()
        for b in data.get("data", []):
            bid = b.get("id")
            if isinstance(bid, int):
                badge_ids.add(bid)
        cursor = data.get("nextPageCursor")
        pages_fetched += 1
        if not cursor:
            break
        if max_pages is not None and pages_fetched >= max_pages:
            break
        time.sleep(BADGE_REQUEST_DELAY)
    return badge_ids


def count_badges_for_universe(badges: list[dict], universe_badge_ids: set[int]) -> int:
    if not universe_badge_ids:
        return 0
    count = 0
    for b in badges:
        bid = b.get("id")
        if isinstance(bid, int) and bid in universe_badge_ids:
            count += 1
    return count


def get_game_icon_url(universe_id: int) -> str | None:
    url = GAME_ICONS_API.format(universeId=universe_id)
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        arr = data.get("data", [])
        if not arr:
            return None
        return arr[0].get("imageUrl")
    except Exception:
        return None


def get_user_headshot_url(user_id: int) -> str | None:
    url = USER_HEADSHOT_API.format(userId=user_id)
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        arr = data.get("data", [])
        if not arr:
            return None
        return arr[0].get("imageUrl")
    except Exception:
        return None


def get_badge_target_for_game(user_id: int, game_key: str) -> int | None:
    user_key = str(user_id)
    k = (user_key, game_key.lower())
    if k in USER_BADGE_TARGETS:
        return USER_BADGE_TARGETS[k]
    return GLOBAL_BADGE_TARGETS.get(game_key.lower())


def get_presence_for_users(user_ids: list[int]) -> tuple[dict[int, dict], bool]:
    """
    Returns (presence_map, error_flag).
    error_flag=True means we likely hit a rate limit or other error.
    """
    if not user_ids:
        return {}, False
    payload = {"userIds": user_ids}
    try:
        r = requests.post(PRESENCE_API, json=payload, timeout=10)
        if r.status_code == 429:
            return {}, True
        if r.status_code != 200:
            return {}, True
        data = r.json()
        result = {}
        for p in data.get("userPresences", []):
            uid = p.get("userId")
            if isinstance(uid, int):
                result[uid] = p
        return result, False
    except Exception:
        return {}, True


def presence_label(p: dict | None) -> str:
    """
    Roblox presence mapping (userPresenceType):
      0 = Offline
      1 = Online
      2 = In Game
      3 = In Studio
    """
    if not p:
        return "Unknown"

    t = p.get("userPresenceType")

    if t == 0:
        return "⚫ Offline"
    if t == 1:
        return "🔵 Online"
    if t == 2:
        return "🟢 In Game"
    if t == 3:
        return "🟠 In Studio"

    return "Unknown"


def get_game_icon_for_entry(owner_key: str | None, game_key: str) -> str | None:
    """
    owner_key = None => global game, else Discord user id string for per-user game.
    """
    if owner_key is None:
        data = GLOBAL_GAMES.get(game_key.lower())
    else:
        data = USER_GAMES.get((owner_key, game_key.lower()))
    if not data:
        return None
    return get_game_icon_url(data["universeId"])


def get_friend_count_safe(user_id: int) -> tuple[int | str, bool]:
    """
    Returns (friend_count_or_label, error_flag).
    error_flag=True means rate limit or other error.
    """
    url = FRIENDS_API.format(userId=user_id)
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 429:
            return "Rate limited", True
        if r.status_code != 200:
            return "Error", True
        data = r.json()
        return len(data.get("data", [])), False
    except Exception:
        return "Error", True
    
def export_presets_for_user(discord_user_id: int) -> str:
    user_key = str(discord_user_id)

    combo_chunks: list[str] = []
    game_chunks: list[str] = []

    # Combos: COMBO:name:id id id
    for (owner, combo_name), assets in USER_COMBOS.items():
        if owner != user_key:
            continue
        asset_str = " ".join(str(a) for a in sorted(assets))
        combo_chunks.append(f"{combo_name}:{asset_str}")

    # Games: GAME:key:placeId:universeId:target
    for (owner, game_key), game_data in USER_GAMES.items():
        if owner != user_key:
            continue
        place_id = game_data.get("placeId", "")
        universe_id = game_data.get("universeId", "")
        target = USER_BADGE_TARGETS.get((owner, game_key), "")
        game_chunks.append(f"{game_key}:{place_id}:{universe_id}:{target}")

    # Final single-line payload
    return (
        "COMBOS=" + ";".join(combo_chunks) + "|"
        "GAMES=" + ";".join(game_chunks)
    )

    return "\n".join(lines)


def import_presets_for_user(discord_user_id: int, text: str) -> tuple[int, int]:
    user_key = str(discord_user_id)
    imported_combos = 0
    imported_games = 0

    s = text.strip()

    # Strip ``` fences if user pasted a code block
    if s.startswith("```"):
        s = s[3:]
        if "\n" in s:
            s = s.split("\n", 1)[1]
        s = s.strip()
        if s.endswith("```"):
            s = s[:-3].strip()

    # Expect format: COMBOS=...|GAMES=...
    parts = s.split("|", 1)
    combos_part = ""
    games_part = ""
    for part in parts:
        part = part.strip()
        if part.upper().startswith("COMBOS="):
            combos_part = part[len("COMBOS="):]
        elif part.upper().startswith("GAMES="):
            games_part = part[len("GAMES="):]

    temp_combos: dict[tuple[str, str], set[int]] = {}
    temp_games: dict[tuple[str, str], dict] = {}
    temp_targets: dict[tuple[str, str], int] = {}

    # Parse combos chunk: name:id id id;name2:id id
    if combos_part:
        for chunk in combos_part.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ":" not in chunk:
                continue
            name, ids_str = chunk.split(":", 1)
            name = name.strip()
            ids_str = ids_str.strip()
            if not name or not ids_str:
                continue
            try:
                asset_ids = {int(x) for x in ids_str.split() if x.strip()}
            except ValueError:
                continue
            if not asset_ids:
                continue
            temp_combos[(user_key, name.lower())] = asset_ids
            imported_combos += 1

    # Parse games chunk: key:place:universe:target;key2:...
    if games_part:
        for chunk in games_part.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            pieces = chunk.split(":")
            if len(pieces) < 3:
                continue
            game_key = pieces.strip().lower()
            place_str = pieces.strip()
            universe_str = pieces.strip()
            target_str = pieces.strip() if len(pieces) > 3 else ""
            try:
                place_id = int(place_str)
                universe_id = int(universe_str)
            except ValueError:
                continue
            temp_games[(user_key, game_key)] = {
                "key": game_key,
                "placeId": place_id,
                "universeId": universe_id,
            }
            if target_str:
                try:
                    temp_targets[(user_key, game_key)] = int(target_str)
                except ValueError:
                    pass
            imported_games += 1

    # If nothing valid parsed, do not touch existing presets
    if imported_combos == 0 and imported_games == 0:
        return 0, 0

    # Replace this user's presets
    for key in list(USER_COMBOS.keys()):
        if key == user_key:
            del USER_COMBOS[key]
    for key in list(USER_GAMES.keys()):
        if key == user_key:
            del USER_GAMES[key]
    for key in list(USER_BADGE_TARGETS.keys()):
        if key == user_key:
            del USER_BADGE_TARGETS[key]

    USER_COMBOS.update(temp_combos)
    USER_GAMES.update(temp_games)
    USER_BADGE_TARGETS.update(temp_targets)

    return imported_combos, imported_games


# ------ EMBED / PAGINATION HELPERS FOR SCAN ------

def sus_square(sus: float) -> str:
    if sus >= 75.0:
        return "🟥"  # red
    if sus >= 50.0:
        return "🟧"  # orange
    if sus >= 25.0:
        return "🟨"  # yellow
    if sus == 0.0:
        return "🟩"  # green for clearly low
    return "🟩"      # default low

def build_friend_embed(m: dict, game_info: dict | None) -> discord.Embed:
    if game_info is not None:
        if m["total_badges"] == 0:
            badge_summary = "No badges."
        else:
            badge_summary = (
                f"Badges: {m['total_badges']}\n"
                f"Game badges: {m['game_badges']}\n"
                f"{m['pct']:.2f}% of their badges from this game"
            )
    else:
        badge_summary = "Badge data not requested."

    combo_detail_lines = m.get("combo_match_detail") or []
    if combo_detail_lines:
        combo_line = "Matched combos: " + ", ".join(m["matched_combos"])
        detail_block = "Combo detail:\n" + "\n".join(
            f"- {line}" for line in combo_detail_lines
        )
    else:
        combo_line = f"Matched combos: {', '.join(m['matched_combos'])}"
        detail_block = ""

    friend_count_line = (
        f"Friends (API-visible, max 200): {m.get('friend_count', 'Unknown')}"
    )

    lines = [
        f"userId: `{m['user_id']}`",
        f"[Profile link]({m['profile_url']})",
        friend_count_line,
        f"Account created: {m['join_text']}",
        f"Presence: {m.get('presence_text', 'Unknown')}",
        combo_line,
    ]
    if detail_block:
        lines.append(detail_block)
    lines.append("")
    lines.append(badge_summary)

    if game_info is not None:
        sus = m.get("sus_score", 0.0) or 0.0
        sq = sus_square(sus)
        lines.append(
            f"Bot likelihood from badges: {sq} **{sus:.2f}%**"
        )

    sus = m.get("sus_score", 0.0) or 0.0
    if sus == 0.0:
        color = discord.Color.green()
    elif sus >= 75.0:
        color = discord.Color.red()
    elif sus >= 50.0:
        color = discord.Color.orange()
    elif sus >= 25.0:
        color = discord.Color.gold()
    else:
        color = discord.Color.orange()

    title_text = m.get("display_name") or m["username"]
    username_text = m["username"]

    emb = discord.Embed(
        title=f"{title_text} (@{username_text})",
        description="\n".join(lines),
        color=color,
    )
    if m["headshot_url"]:
        emb.set_thumbnail(url=m["headshot_url"])
    return emb


class FriendScanView(discord.ui.View):
    def __init__(self,
                 invoker_id: int,
                 friend_username: str,
                 combo_names: str,
                 game: str | None,
                 match_mode: str | None,
                 timeout: float | None = 600):
        super().__init__(timeout=timeout)
        self.invoker_id = invoker_id
        self.friend_username = friend_username
        self.combo_names = combo_names
        self.game = game
        self.match_mode = match_mode

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the user who ran the original scan can use this button.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Scan this friend", style=discord.ButtonStyle.primary, emoji="🔍")
    async def scan_friend(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"Starting scan for `{self.friend_username}` with the same parameters..."
        )
        await run_scan_core(
            interaction=interaction,
            roblox_username=self.friend_username,
            combo_names=self.combo_names,
            game=self.game,
            effective_mode=self.match_mode,
            invoked_from_button=True,
            previous_cumulative_size=None,
            only_show_new=False,
            final_run=True,
            keep_scanning_embed=False,
        )


def build_page_embeds_with_views(
    match_data: list[dict],
    page: int,
    per_page: int,
    game_info: dict | None,
    invoker_id: int,
    combo_names: str,
    game: str | None,
    match_mode: str | None,
) -> tuple[list[discord.Embed], list[FriendScanView]]:
    start = page * per_page
    end = start + per_page
    slice_data = match_data[start:end]
    embeds: list[discord.Embed] = []
    views: list[FriendScanView] = []
    for m in slice_data:
        embeds.append(build_friend_embed(m, game_info))
        views.append(
            FriendScanView(
                invoker_id=invoker_id,
                friend_username=m["username"],
                combo_names=combo_names,
                game=game,
                match_mode=match_mode,
            )
        )
    return embeds, views


class ResultsPaginator(discord.ui.View):
    def __init__(self,
                 invoker_id: int,
                 match_data: list[dict],
                 game_info: dict | None,
                 combo_names: str,
                 game_key: str | None,
                 match_mode: str | None,
                 target_username: str,
                 target_display_name: str,
                 per_page: int = 3,
                 timeout: float | None = 300,
                 channel_id: int | None = None,
                 scan_token: str | None = None):
        super().__init__(timeout=timeout)
        self.invoker_id = invoker_id
        self.match_data = match_data
        self.game_info = game_info
        self.combo_names = combo_names
        self.game_key = game_key
        self.match_mode = match_mode
        self.target_username = target_username
        self.target_display_name = target_display_name
        self.per_page = per_page
        self.page = 0
        self.max_page = max((len(match_data) - 1) // per_page, 0)
        self.result_messages: list[discord.Message] = []

        self.header_message: discord.Message | None = None
        self.latest_interaction: discord.Interaction | None = None

        # NEW: channel + token to enforce one active scan per channel
        self.channel_id: int | None = channel_id
        self.scan_token: str | None = scan_token


    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the user who started this scan can change pages.",
                ephemeral=True,
            )
            return False
        self.latest_interaction = interaction
        return True

    async def on_timeout(self) -> None:
        await self._close_and_print()

    async def _close_and_print(self, interaction: discord.Interaction | None = None):
        # Determine channel id for gating
        chan = None
        if interaction is not None:
            chan = interaction.channel
        elif self.latest_interaction is not None:
            chan = self.latest_interaction.channel
        elif self.header_message is not None:
            chan = self.header_message.channel

        if isinstance(chan, discord.abc.Messageable):
            ch_id = chan.id
        else:
            ch_id = self.channel_id

        if ch_id is not None:
            # If a rescan is running in this channel, do not dump plaintext.
            if RESCAN_IN_PROGRESS.get(ch_id, False):
                return
            # If this paginator is not the latest scan for the channel, skip.
            if self.scan_token is not None:
                active = ACTIVE_SCAN_TOKENS.get(ch_id)
                if active is not None and active != self.scan_token:
                    return

        # Delete header + result embeds
        if self.header_message is not None:
            try:
                await self.header_message.delete()
            except Exception:
                pass

        for msg in self.result_messages:
            try:
                await msg.delete()
            except Exception:
                pass
        self.result_messages.clear()

        # Build text chunks
        if not self.match_data:
            final_chunks = ["No results to display."]
        else:
            lines: list[str] = []
            for idx, m in enumerate(self.match_data, start=1):
                lines.append(
                    f"{idx}. {m.get('display_name') or m['username']} "
                    f"(@{m['username']}, id {m['user_id']}): {m['profile_url']}"
                )
                lines.append(
                    f"   Presence: {m.get('presence_text', 'Unknown')}, "
                    f"Friends (API-visible): {m.get('friend_count', 'Unknown')}"
                )
                lines.append(
                    f"   Matched combos: {', '.join(m['matched_combos'])}"
                )
                if m.get("combo_match_detail"):
                    lines.append(
                        "   Combo detail: " + "; ".join(m["combo_match_detail"])
                    )
                if self.game_info is not None:
                    sus = m.get("sus_score", 0.0) or 0.0
                    sq = sus_square(sus)
                    lines.append(
                        f"   Badges: total {m['total_badges']}, "
                        f"game {m['game_badges']} "
                        f"({m['pct']:.2f}% of badges from this game), "
                        f"bot likelihood: {sq} {sus:.2f}%"
                    )
                lines.append("")

            header_first = (
                f"**__Results for {self.target_display_name} (@{self.target_username})__** "
                f"**__(plain text, interactions expired)__**:"
            )
            header_follow = (
                f"**__Results for {self.target_display_name} (@{self.target_username})__** "
                f"**__(continued):__**"
            )

            import re

            body_text = "\n".join(lines)

            # First, group lines into per-result blocks (starting at "N." or "N.M.")
            blocks: list[str] = []
            current_block: list[str] = []

            for line in body_text.split("\n"):
                # Start of a new result: "10. ", "1.1 ", etc.
                if re.match(r"^\d+(\.\d+)?\.\s", line):
                    if current_block:
                        blocks.append("\n".join(current_block))
                        current_block = []
                current_block.append(line)

            if current_block:
                blocks.append("\n".join(current_block))

            hard_limit = 2000
            content_limit = hard_limit - 50  # safety margin for headers etc.

            # Now pack whole blocks into message chunks
            raw_chunks: list[str] = []
            current = ""

            for block in blocks:
                extra_len = len(block) + (1 if current else 0)
                if len(current) + extra_len > content_limit:
                    if current:
                        raw_chunks.append(current)
                    current = block
                else:
                    if current:
                        current += "\n" + block
                    else:
                        current = block

            if current:
                raw_chunks.append(current)

            final_chunks: list[str] = []
            for i, chunk in enumerate(raw_chunks):
                header = header_first if i == 0 else header_follow
                # Ensure we stay under the absolute Discord limit
                if len(header) + 1 + len(chunk) > hard_limit:
                    available = hard_limit - len(header) - 1
                    chunk = chunk[:available]
                final_chunks.append(header + "\n" + chunk)

        channel = None
        if interaction is not None:
            channel = interaction.channel
        elif self.latest_interaction is not None:
            channel = self.latest_interaction.channel
        elif self.header_message is not None:
            channel = self.header_message.channel

        if channel is not None:
            for chunk in final_chunks:
                if chunk.strip():
                    await channel.send(chunk)

    async def update_page(self, interaction: discord.Interaction):
        for msg in self.result_messages:
            try:
                await msg.delete()
            except Exception:
                pass
        self.result_messages.clear()

        embeds, views = build_page_embeds_with_views(
            self.match_data,
            self.page,
            self.per_page,
            self.game_info,
            self.invoker_id,
            self.combo_names,
            self.game_key,
            self.match_mode,
        )

        content = f"Scan results (page {self.page + 1}/{self.max_page + 1}, total matches: {len(self.match_data)})"
        await interaction.response.edit_message(content=content, view=self, embeds=[])

        channel = interaction.channel
        for emb, view in zip(embeds, views):
            msg = await channel.send(embed=emb, view=view)
            self.result_messages.append(msg)

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        await self.update_page(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.max_page:
            self.page += 1
        await self.update_page(interaction)

    @discord.ui.button(label="Close and print", style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._close_and_print(interaction)
        self.stop()


class ScanCancelView(discord.ui.View):
    def __init__(self, control: ScanControl, scan_message: discord.Message, invoker_id: int, timeout: float | None = 600):
        super().__init__(timeout=timeout)
        self.control = control
        self.scan_message = scan_message
        self.invoker_id = invoker_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the user who started this scan can cancel it.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Cancel Scan", style=discord.ButtonStyle.danger, emoji="🛑")
    async def cancel_scan(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.control.cancelled = True

        if getattr(self.scan_message, "embeds", None):
            old = self.scan_message.embeds[0]
            cancelled_desc = f"~~{old.description or ''}~~"
            cancelled_title = f"~~{old.title or 'Scan'}~~"

            cancelled_embed = discord.Embed(
                title=cancelled_title,
                description=cancelled_desc,
                color=discord.Color.red(),
            )
            if old.thumbnail and old.thumbnail.url:
                cancelled_embed.set_thumbnail(url=old.thumbnail.url)
            if old.image and old.image.url:
                cancelled_embed.set_image(url=old.image.url)

            try:
                await self.scan_message.edit(embed=cancelled_embed)
            except Exception:
                pass

        # Remove buttons from the progress / interaction message as well
        try:
            await interaction.message.edit(view=None)
        except Exception:
            pass

        await interaction.response.edit_message(content="Scan cancelled.", view=None)
        self.stop()


class AutoRerunConfirmView(discord.ui.View):
    def __init__(self, invoker_id: int, timeout: float | None = 60):
        super().__init__(timeout=timeout)
        self.invoker_id = invoker_id
        self.confirmed: bool = False

    async def on_timeout(self) -> None:
        # Grey out buttons when the confirmation view times out
        for item in self.children:
            item.disabled = True
        try:
            # interaction.message is not stored here, but Discord will reuse
            # the same view instance, so we can use the message attached to it
            # via the last interaction, similar to other views.
            # Safest is to only attempt edit if we have a message reference.
            if hasattr(self, "_message") and self._message is not None:
                await self._message.edit(view=self)
        except Exception:
            pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the user who started this scan can confirm auto rerun.",
                ephemeral=True,
            )
            return False
        return True

    async def _disable_all(self, interaction: discord.Interaction):
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(label="Yes, auto rerun", style=discord.ButtonStyle.green)
    async def yes_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await self._disable_all(interaction)
        await interaction.response.send_message(
            content="Auto rerun started. This may take some time..."
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
    async def no_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        await self._disable_all(interaction)

        try:
            await interaction.message.edit(
                content="Cancelled, deleting message...",
                view=self,
            )
            await asyncio.sleep(3)
            await interaction.message.delete()
        except Exception:
            pass

        self.stop()


class ScanSummaryView(discord.ui.View):
    def __init__(
        self,
        invoker_id: int,
        roblox_username: str,
        combo_names: str,
        game: str | None,
        match_mode: str | None,
        timeout: float | None = 600,
    ):
        super().__init__(timeout=timeout)
        self.invoker_id = invoker_id
        self.roblox_username = roblox_username
        self.combo_names = combo_names
        self.game = game
        self.match_mode = match_mode

        self.summary_message: discord.Message | None = None
        self.paginator: ResultsPaginator | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the user who started this scan can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        # Grey out buttons when the view times out
        for item in self.children:
            item.disabled = True
        if self.summary_message is not None:
            try:
                await self.summary_message.edit(view=self)
            except Exception:
                pass

    def _effective_mode(self) -> str | None:
        if self.match_mode in ("exact", "inexact"):
            return self.match_mode
        return None

    def _build_cache_key(self) -> tuple[int, str, str, str | None, str]:
        return (
            self.invoker_id,
            self.roblox_username.lower(),
            self.combo_names.lower(),
            (self.game.lower() if self.game else None),
            (self.match_mode or "inexact"),
        )

    @discord.ui.button(
        label="Additional Scan",
        style=discord.ButtonStyle.primary,
        emoji="🔁",
    )
    async def rerun_scan(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        # 1) Delete old summary + paginated results (like auto rerun does after confirm)
        if self.summary_message is not None:
            try:
                await self.summary_message.delete()
            except Exception:
                pass

        if self.paginator is not None:
            if self.paginator.header_message is not None:
                try:
                    await self.paginator.header_message.delete()
                except Exception:
                    pass
            for msg in self.paginator.result_messages:
                try:
                    await msg.delete()
                except Exception:
                    pass
            self.paginator.result_messages.clear()

        # 2) Compute previous cumulative size from cache for this config.
        key = self._build_cache_key()
        prev_size = len(SCAN_CACHE.get(key, set()))

        await interaction.response.send_message(
            f"Running an additional scan for `{self.roblox_username}`..."
        )

        # 3) Run a fresh final scan using the previous_cumulative_size.
        channel_id = interaction.channel.id
        RESCAN_IN_PROGRESS[channel_id] = True
        scan_token = uuid.uuid4().hex
        ACTIVE_SCAN_TOKENS[channel_id] = scan_token

        try:
            await run_scan_core(
                interaction=interaction,
                roblox_username=self.roblox_username,
                combo_names=self.combo_names,
                game=self.game,
                effective_mode=self._effective_mode(),
                invoked_from_button=True,
                previous_cumulative_size=prev_size,
                only_show_new=False,
                final_run=True,
                keep_scanning_embed=False,
                channel_id=channel_id,
                scan_token=scan_token,
            )
        finally:
            RESCAN_IN_PROGRESS[channel_id] = False


        # 4) Grey out these buttons; this summary is now “spent”.
        for item in self.children:
            item.disabled = True
        try:
            if self.summary_message is not None:
                await self.summary_message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(
        label="Auto Additional Scan",
        style=discord.ButtonStyle.secondary,
        emoji="⚙️",
    )
    async def auto_rerun(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        # Ask for confirmation, without deleting current results yet.
        confirm_view = AutoRerunConfirmView(invoker_id=self.invoker_id)

        await interaction.response.send_message(
            content=(
                "Auto additional scan will repeatedly run new scans until **no new matches** are found "
                "or a safety limit is reached. This may take more time and hit API rate limits.\n\n"
                "Are you sure you want to proceed?"
            ),
            view=confirm_view,
        )
        # Store the confirmation message on the view so on_timeout can edit it
        try:
            confirm_view._message = await interaction.original_response()
        except Exception:
            confirm_view._message = None

        await confirm_view.wait()

        if not confirm_view.confirmed:
            # User cancelled or timed out: leave existing results intact.
            return

        # User confirmed: clean up old summary + paginated embeds.
        if self.summary_message is not None:
            try:
                await self.summary_message.delete()
            except Exception:
                pass

        if self.paginator is not None:
            if self.paginator.header_message is not None:
                try:
                    await self.paginator.header_message.delete()
                except Exception:
                    pass
            for msg in self.paginator.result_messages:
                try:
                    await msg.delete()
                except Exception:
                    pass
            self.paginator.result_messages.clear()

        channel_id = interaction.channel.id
        RESCAN_IN_PROGRESS[channel_id] = True

        max_iterations = 10
        iteration = 0

        status_msg = await interaction.channel.send(
            content="Auto additional scan in progress..."
        )

        key = self._build_cache_key()
        prev_size = len(SCAN_CACHE.get(key, set()))

        while iteration < max_iterations:
            iteration += 1

            scan_token = uuid.uuid4().hex
            ACTIVE_SCAN_TOKENS[channel_id] = scan_token

            await run_scan_core(
                interaction=interaction,
                roblox_username=self.roblox_username,
                combo_names=self.combo_names,
                game=self.game,
                effective_mode=self._effective_mode(),
                invoked_from_button=True,
                previous_cumulative_size=prev_size,
                only_show_new=False,
                final_run=False,
                keep_scanning_embed=False,
                channel_id=channel_id,
                scan_token=scan_token,
            )

            await asyncio.sleep(2)

            new_size = len(SCAN_CACHE.get(key, set()))
            new_count = new_size - prev_size

            if new_count > 0:
                await status_msg.edit(
                    content=(
                        f"Auto additional scan iteration {iteration}: "
                        f"found {new_count} new matches (total unique so far: {new_size})."
                    )
                )
                prev_size = new_size
                await asyncio.sleep(3)
                continue
            else:
                await status_msg.edit(
                    content=(
                        f"Auto additional scan stopped after {iteration} iteration(s); "
                        "no new matches were found on the last run. Printing final combined results..."
                    )
                )
                break

        if iteration >= max_iterations:
            await status_msg.edit(
                content=(
                    f"Auto additional scan reached the safety limit of {max_iterations} iterations "
                    "and has been stopped. Printing combined results so far..."
                )
            )

        RESCAN_IN_PROGRESS[channel_id] = False

        scan_token = uuid.uuid4().hex
        ACTIVE_SCAN_TOKENS[channel_id] = scan_token

        await run_scan_core(
            interaction=interaction,
            roblox_username=self.roblox_username,
            combo_names=self.combo_names,
            game=self.game,
            effective_mode=self._effective_mode(),
            invoked_from_button=True,
            previous_cumulative_size=None,
            only_show_new=False,
            final_run=True,
            keep_scanning_embed=False,
            channel_id=channel_id,
            scan_token=scan_token,
        )


        # Grey out summary buttons after auto-run
        for item in self.children:
            item.disabled = True
        if self.summary_message is not None:
            try:
                await self.summary_message.edit(view=self)
            except Exception:
                pass


# ------ RBI COMMAND GROUP ------

rbi_group = app_commands.Group(name="rbi", description="RBI helper commands")

@rbi_group.command(
    name="csvexport",
    description="Export your RBI presets (combos and games) as text.",
)
async def rbi_csvexport(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    payload = export_presets_for_user(interaction.user.id)
    if not payload.strip():
        await interaction.followup.send(
            "You have no saved combos or games to export.",
            ephemeral=True,
        )
        return

    # Wrap in ``` for easy copy-paste
    await interaction.followup.send(
        content=f"Here are your presets. Copy everything inside the code block:\n```text\n{payload}\n```",
        ephemeral=True,
    )

@rbi_group.command(
    name="csvimport",
    description="Import your RBI presets (combos and games) from pasted text.",
)
@app_commands.describe(data="Preset text previously produced by /rbi csvexport")
async def rbi_csvimport(interaction: discord.Interaction, data: str):
    await interaction.response.defer(ephemeral=True)

    combos, games = import_presets_for_user(interaction.user.id, data)

    if combos == 0 and games == 0:
        msg = (
            "No valid presets were found in the provided text.\n"
            "Make sure you pasted the content exactly as exported by `/rbi csvexport`."
        )
    else:
        msg = (
            f"Imported {combos} combo preset(s) and {games} game preset(s) "
            "for your user."
        )

    await interaction.followup.send(msg, ephemeral=True)


@rbi_group.command(name="ping", description="Check if RBI is alive.")
async def rbi_ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"pong (rbi slash) – {BOT_VERSION}")

@rbi_group.command(
    name="debugscan",
    description="Debug: show active scan state for this channel.",
)
async def rbi_debugscan(interaction: discord.Interaction):
    channel_id = interaction.channel.id

    active_token = ACTIVE_SCAN_TOKENS.get(channel_id)
    rescan_flag = RESCAN_IN_PROGRESS.get(channel_id, False)

    await interaction.response.send_message(
        content=(
            f"Debug for channel `{channel_id}`:\n"
            f"- Active scan token: `{active_token}`\n"
            f"- Rescan in progress: `{rescan_flag}`"
        ),
        ephemeral=True,
    )


# ------ HELP PAGINATION VIEW ------

class RBIHelpView(discord.ui.View):

    def _apply_button_styles(self):
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            if child.label == "Commands":
                child.style = (
                    discord.ButtonStyle.primary
                    if self.current_page == "commands"
                    else discord.ButtonStyle.secondary
                )
            elif child.label == "Formulas":
                child.style = (
                    discord.ButtonStyle.primary
                    if self.current_page == "formulas"
                    else discord.ButtonStyle.secondary
                )
            elif child.label == "About":
                child.style = (
                    discord.ButtonStyle.primary
                    if self.current_page == "about"
                    else discord.ButtonStyle.secondary
                )

    def __init__(self, invoker_id: int):
        super().__init__(timeout=300)
        self.invoker_id = invoker_id
        self.current_page = "about"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the user who ran `/rbi help` can switch help pages.",
                ephemeral=True,
            )
            return False
        return True

    def build_commands_embed(self) -> discord.Embed:
        description_lines = [
            f"# RBI Commands",
            "",
            "`/rbi help`",
            "`/rbi ping`",
            "`/rbi setcombo name:<name> ids:<id1 id2 ...>`",
            "`/rbi mycombos`",
            "`/rbi addgame key:<key> place_id:<id> [target_badges:<n>]`",
            "`/rbi mygames`",
            "`/rbi scan roblox_username:<name> combo_names:<names|mycombos|globalcombos|all|none> "
            "[game:<key>] [match_mode:<exact|inexact>]`",
            "`/rbi csvexport`",
            "`/rbi csvimport data:<single-line-from-export>`",
            "",
            "Special `combo_names` keywords:",
            "- `mycombos`: all combos you created",
            "- `globalcombos`: all global default combos",
            "- `all`: mycombos + globalcombos",
            "- `none`: disable combo filtering, scan all friends",
            "",
            "Preset import/export:",
            "- `csvexport`: sends your combos/games as a single line.",
            "- `csvimport`: paste that line back into `data` to restore presets.",
            "",
            "Match mode:",
            "- `Exact`: friend must wear all items in a combo.",
            "- `Inexact`: friend can wear any subset; embeds show X/Y items per combo.",
        ]

        embed = discord.Embed(
            title="RBI Help – Commands (page 2/3)",
            description="\n".join(description_lines),
            color=discord.Color.blurple(),
        )

        embed.set_footer(
            text="Use the buttons below to switch between About, Commands, and Formulas."
        )
        return embed

    def build_about_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="About RBI (page 1/3)",
            description=(
                "# RBI (Roblox Bot Investigator) estimates how likely a Roblox account is being "
                "fed by automation or bot farms.\n\n"
                "It looks at outfits, badge patterns, and friend networks to spot accounts "
                "that look more like bots than normal players."
            ),
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="How RBI thinks about bots",
            value=(
                "- **Badge goals**: Bot farms usually aim for a small badge goal in a game "
                "(for example, 8–12 badges) and then stop.\n"
                "- **Real players**: Either have very few badges (new players) or way more badges "
                "from playing normally over time.\n"
                "- **Exploit alts**: Often have most of their badges in one game, and also match "
                "default outfits like bacon / starter combos.\n"
            ),
            inline=False,
        )

        embed.add_field(
            name="What the scores mean",
            value=(
                "- **Per-friend badge likelihood**: how botted a single friend looks for a specific game.\n"
                "- **Sus Score**: how likely the **target account** is being fed by bots, based on how many "
                "friends look botted, how strongly they look botted, and how many are in the top risk band.\n"
                "- These scores are **signals**, not bans: high numbers suggest you should take a closer look, "
                "not instantly assume guilt.\n"
            ),
            inline=False,
        )

        embed.add_field(
            name="Version and links",
            value=(
                f"- Current version: **{BOT_VERSION}**\n"
                "- Last major scoring update: **03/19/2026**\n"
                "- Source code / GitHub: **[TEMPORARY PLACEHOLDER]**\n"
            ),
            inline=False,
        )

        embed.add_field(
            name="Limitations",
            value=(
                "- Roblox APIs can be rate-limited or incomplete; RBI shows warnings when data may be partial.\n"
                "- Different games may need different targets and thresholds as they update over time.\n"
                "- RBI is a helper tool; always combine scores with your own judgment.\n"
            ),
            inline=False,
        )

        embed.set_footer(
            text="Use the buttons below to switch between About, Commands, and Formulas."
        )
        return embed


    def build_formulas_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="RBI Help – Formulas (page 3/3)",
            description="# How RBI calculates friend percentage, per-friend badge likelihood, and Sus Score.",
            color=discord.Color.blurple(),
        )

        # Friend percentage
        embed.add_field(
            name="Friend percentage",
            value=(
                "Friends % = **matches / visible_friends × 100**\n"
                "- **Matches**: how many of the target's friends wear one of the selected combos.\n"
                "- **Visible friends**: how many friends Roblox returns (max 200 per user).\n"
            ),
            inline=False,
        )

        # Per-friend badge likelihood – plain English
        embed.add_field(
            name="Per-friend badge likelihood (idea)",
            value=(
                "- Count how many badges a friend has in total, and how many are from this game.\n"
                "- If almost none of their badges are from this game (<5%), they count as **0% botted** "
                "for this game.\n"
                "- Around the game’s target badge count (for example 8–12 when the target is 10), they’re "
                "treated as most suspicious (close to 100%).\n"
                "- Far above the target (for example 20, 30, 40+), the score slowly falls back down "
                "towards 0% because that looks more like a real grinder.\n"
            ),
            inline=False,
        )

        # Per-friend badge likelihood – exact steps (shortened)
        embed.add_field(
            name="Per-friend badge likelihood (steps)",
            value=(
                "Let `T` = target badges, `game_badges` = badges from this game, "
                "`total_badges` = all badges, and `ratio = game_badges / total_badges`.\n"
                "1. If `total_badges == 0` or `ratio < 0.05`: likelihood = **0%**.\n"
                "2. `low = round(0.8 × T)`, `high = round(1.2 × T)`, `upper_zero ≈ 4 × T`.\n"
                "3. Base score `raw` from `game_badges`:\n"
                "   - If `0 < game_badges < low`: `raw = (game_badges / low) × 100`.\n"
                "   - If `low ≤ game_badges ≤ high`: `raw = 100`.\n"
                "   - If `high < game_badges < upper_zero`:\n"
                "     - `x = (game_badges - high) / (upper_zero - high)` (0–1)\n"
                "     - `raw = 100 × (0.4 ** x)`.\n"
                "   - If `game_badges ≥ upper_zero`: `raw = 0`.\n"
            ),
            inline=False,
        )

        # Ratio-based adjustment – idea + compact formula
        embed.add_field(
            name="Badge ratio adjustment (above target)",
            value=(
                "When `game_badges > T` and `raw > 0`, we adjust based on how focused the account is "
                "on this game:\n"
                "- `ratio = game_badges / total_badges`.\n"
                "- If `ratio ≤ 0.20`: `ratio_factor = 0.2` (strong damping, generalist player).\n"
                "- If `ratio ≥ 0.60`: `ratio_factor = 0.9` (keep most of the score, focused alt).\n"
                "- If in between:\n"
                "  - `t = (ratio - 0.20) / (0.60 - 0.20)`\n"
                "  - `ratio_factor = 0.2 + t × (0.9 - 0.2)`\n"
                "- Final per-friend likelihood = `raw × ratio_factor`.\n"
            ),
            inline=False,
        )

        # Sus Score – detailed but within limits
        embed.add_field(
            name="Sus Score (fed by bots likelihood)",
            value=(
                "Inputs:\n"
                "- Quantity factor `Q = min(matches × 10, 100)`.\n"
                "- Per-friend likelihoods `s_i` for each matched friend.\n"
                "- Only friends with `s_i ≥ 25` are used in badge aggregation.\n"
                "- Let `k` = # of matched friends with `s_i ≥ 25`, `n` = total matched friends, "
                "`p = k / n`.\n"
                "- Weighted badge factor:\n"
                "  - weights `w_i = s_i / 100`\n"
                "  - `avg_badge = (Σ (s_i × w_i)) / (Σ w_i)` (or 0 if no risky friends).\n"
                "- Let `k_red` = # of matched friends with `s_i ≥ 75`.\n\n"
                "Formula:\n"
                "1. `BaseSus = (Q + avg_badge) / 2`.\n"
                "2. `boost = min(1 + 0.15 × (k_red^1.2), 2)`.\n"
                "3. `Sus Score = clamp(BaseSus × p × boost, 0, 100)`.\n"
                "4. If no game is set, Sus Score is not evaluated.\n"
            ),
            inline=False,
        )

        embed.set_footer(
            text="Use the buttons below to switch between About, Commands, and Formulas."
        )
        return embed


    @discord.ui.button(label="About", style=discord.ButtonStyle.primary)
    async def about_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = "about"
        self._apply_button_styles()
        await interaction.response.edit_message(
            embed=self.build_about_embed(),
            view=self,
        )

    @discord.ui.button(label="Commands", style=discord.ButtonStyle.secondary)
    async def commands_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = "commands"
        self._apply_button_styles()
        await interaction.response.edit_message(
            embed=self.build_commands_embed(),
            view=self,
        )

    @discord.ui.button(label="Formulas", style=discord.ButtonStyle.secondary)
    async def formulas_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = "formulas"
        self._apply_button_styles()
        await interaction.response.edit_message(
            embed=self.build_formulas_embed(),
            view=self,
        )


@rbi_group.command(name="help", description="Show bot info and all RBI commands and formulas.")
async def rbi_help(interaction: discord.Interaction):
    view = RBIHelpView(invoker_id=interaction.user.id)
    view._apply_button_styles()
    about_embed = view.build_about_embed()
    await interaction.response.send_message(embed=about_embed, view=view)


@rbi_group.command(name="setcombo", description="Create or update a named combo for you.")
@app_commands.describe(
    name="Name for this combo (e.g. bacon_sus)",
    ids="Accessory asset IDs separated by spaces (e.g. 111111 222222)"
)
async def rbi_setcombo(interaction: discord.Interaction, name: str, ids: str):
    parts = ids.split()
    if not parts:
        await interaction.response.send_message(
            "You must provide at least one accessory asset ID.", ephemeral=True
        )
        return
    try:
        id_ints = {int(p) for p in parts}
    except ValueError:
        await interaction.response.send_message(
            "All IDs must be integers (numbers).", ephemeral=True
        )
        return
    user_key = str(interaction.user.id)
    combo_key = (user_key, name.lower())
    USER_COMBOS[combo_key] = id_ints
    await interaction.response.send_message(
        f"Combo `{name}` saved with IDs: {', '.join(str(i) for i in sorted(id_ints))}"
    )


@rbi_group.command(
    name="addgame",
    description="Save a game (by placeId) for badge scans, optionally with a bot badge target."
)
@app_commands.describe(
    key="Short key for this game (e.g. fisch_custom)",
    place_id="Place ID for the game (number)",
    target_badges="Optional: badge count that likely indicates a bot (e.g. 9)"
)
async def rbi_addgame(
    interaction: discord.Interaction,
    key: str,
    place_id: str,
    target_badges: int | None = None
):
    await interaction.response.defer(ephemeral=True)
    try:
        place_int = int(place_id)
    except ValueError:
        await interaction.followup.send("place_id must be a number.", ephemeral=True)
        return
    universe_id = place_to_universe(place_int)
    if universe_id is None:
        await interaction.followup.send(
            "Could not resolve that placeId to a universeId.", ephemeral=True
        )
        return
    user_key = str(interaction.user.id)
    game_key = (user_key, key.lower())
    USER_GAMES[game_key] = {
        "key": key.lower(),
        "placeId": place_int,
        "universeId": universe_id,
    }

    if target_badges is not None and target_badges > 0:
        USER_BADGE_TARGETS[(user_key, key.lower())] = target_badges
        extra = f"\nBot badge target for `{key.lower()}` set to {target_badges}."
    else:
        extra = ""

    await interaction.followup.send(
        f"Saved game `{key.lower()}` (placeId {place_int}, universeId {universe_id})."
        + extra,
        ephemeral=True
    )


@rbi_group.command(name="mycombos", description="Show your combos and global combos.")
async def rbi_mycombos(interaction: discord.Interaction):
    user_key = str(interaction.user.id)

    embed = discord.Embed(
        title="RBI – Combos",
        description="Global combos first, then your personal combos.",
        color=discord.Color.blurple(),
    )

    # Global combos
    if GLOBAL_COMBOS:
        lines: list[str] = []
        for key, ids in GLOBAL_COMBOS.items():
            desc = GLOBAL_DESCRIPTIONS.get(key, key)
            lines.append(
                f"- `{key}` – {desc}\n"
                f"  IDs: {', '.join(str(i) for i in sorted(ids))}"
            )
        embed.add_field(
            name="Global combos",
            value="\n".join(lines),
            inline=False,
        )
    else:
        embed.add_field(
            name="Global combos",
            value="No global combos are configured.",
            inline=False,
        )

    # User combos
    user_entries = [
        (name, ids)
        for (uid, name), ids in USER_COMBOS.items()
        if uid == user_key
    ]
    if user_entries:
        lines: list[str] = []
        for name, ids in user_entries:
            lines.append(
                f"- `{name}`\n"
                f"  IDs: {', '.join(str(i) for i in sorted(ids))}"
            )
        embed.add_field(
            name="Your combos",
            value="\n".join(lines),
            inline=False,
        )
    else:
        embed.add_field(
            name="Your combos",
            value="You have not created any combos yet.",
            inline=False,
        )

    embed.set_footer(text=f"Bot version: {BOT_VERSION}")
    await interaction.response.send_message(embed=embed)


@rbi_group.command(name="mygames", description="Show games you have saved for badge scanning.")
async def rbi_mygames(interaction: discord.Interaction):
    user_key = str(interaction.user.id)

    embed = discord.Embed(
        title="RBI – Games",
        description="A list of target games you can scan user badges for.",
        color=discord.Color.blurple(),
    )

    # Global games
    if GLOBAL_GAMES:
        lines: list[str] = []
        for key, data in GLOBAL_GAMES.items():
            target = GLOBAL_BADGE_TARGETS.get(key.lower())
            extra = f", bot badge target: {target}" if target is not None else ""
            lines.append(
                f"- `{key}` → placeId {data['placeId']}, universeId {data['universeId']}{extra}"
            )
        embed.add_field(
            name="Global games",
            value="\n".join(lines),
            inline=False,
        )
    else:
        embed.add_field(
            name="Global games",
            value="No global games are configured.",
            inline=False,
        )

    # User games
    user_entries = [
        ((uid, name), data)
        for (uid, name), data in USER_GAMES.items()
        if uid == user_key
    ]
    if user_entries:
        lines: list[str] = []
        for (uid, name), data in user_entries:
            per_target = USER_BADGE_TARGETS.get((user_key, name))
            extra = f", bot badge target: {per_target}" if per_target is not None else ""
            lines.append(
                f"- `{name}` → placeId {data['placeId']}, universeId {data['universeId']}{extra}"
            )
        embed.add_field(
            name="Your games",
            value="\n".join(lines),
            inline=False,
        )
    else:
        embed.add_field(
            name="Your games",
            value="You have not saved any games yet.",
            inline=False,
        )

    embed.set_footer(text=f"Bot version: {BOT_VERSION}")
    await interaction.response.send_message(embed=embed)


# ------ SCAN CORE (with Sus Score logic and new/old handling) ------

async def run_scan_core(
    interaction: discord.Interaction,
    roblox_username: str,
    combo_names: str,
    game: str | None,
    effective_mode: str | None,
    invoked_from_button: bool,
    previous_cumulative_size: int | None = None,
    only_show_new: bool = False,
    final_run: bool = True,
    keep_scanning_embed: bool = True,
    channel_id: int | None = None,
    scan_token: str | None = None,
):
    # Default channel_id / token if not provided (e.g. first /rbi scan)
    if channel_id is None and interaction.channel is not None:
        channel_id = interaction.channel.id
    if channel_id is not None and scan_token is None:
        scan_token = ACTIVE_SCAN_TOKENS.get(channel_id)
    raw_names = [c.strip() for c in combo_names.split(",") if c.strip()]
    lower_raw = [n.lower() for n in raw_names]

    combos_disabled = False
    if len(lower_raw) == 1 and lower_raw[0] == "none":
        combos_disabled = True

    user_key = str(interaction.user.id)
    expanded_names: list[str] = []

    resolved_combos: list[tuple[str, set[int]]] = []
    missing: list[str] = []

    # Track external API issues for summary disclaimers
    presence_rate_limited = False
    friendcount_rate_limited = False

    if not combos_disabled:
        def add_my_combos():
            for (uid, name), _ids in USER_COMBOS.items():
                if uid == user_key:
                    expanded_names.append(name)

        def add_global_combos():
            for gname in GLOBAL_COMBOS.keys():
                expanded_names.append(gname)

        if "all" in lower_raw:
            add_my_combos()
            add_global_combos()
        else:
            if "mycombos" in lower_raw:
                add_my_combos()
            if "globalcombos" in lower_raw:
                add_global_combos()
            for name in raw_names:
                if name.lower() not in {"all", "mycombos", "globalcombos"}:
                    expanded_names.append(name)

        seen = set()
        expanded_names = [n for n in expanded_names if not (n in seen or seen.add(n))]

        if not expanded_names:
            await interaction.channel.send(
                "No combos resolved from combo_names. "
                "Use explicit names, `mycombos`, `globalcombos`, `all`, or `none`."
            )
            return

        for name in expanded_names:
            res = resolve_combo_for_user(interaction.user.id, name)
            if res is None:
                missing.append(name)
            else:
                resolved_combos.append(res)

        if missing:
            msg = (
                "Could not find these combos: "
                + ", ".join(f"`{m}`" for m in missing)
            )
            if invoked_from_button:
                await interaction.channel.send(msg)
            else:
                await interaction.followup.send(msg)
            if not resolved_combos:
                return
    else:
        expanded_names = []
        resolved_combos = []

    if effective_mode is None:
        if len(resolved_combos) <= 1:
            effective_mode = "exact"
        else:
            effective_mode = "inexact"

    game_info = None
    universe_badge_ids: set[int] | None = None
    badge_target: int | None = None
    badge_api_failed = False

    if game:
        lowered = game.lower()
        if lowered in GLOBAL_GAMES:
            game_info = GLOBAL_GAMES[lowered]
        else:
            gkey = (user_key, lowered)
            if gkey not in USER_GAMES:
                msg = f"Could not find a game with key `{game}`."
                if invoked_from_button:
                    await interaction.channel.send(msg)
                else:
                    await interaction.followup.send(msg)
                return
            game_info = USER_GAMES[gkey]

        try:
            universe_badge_ids = get_universe_badge_ids(
                game_info["universeId"], max_pages=None
            )
        except requests.exceptions.SSLError as e:
            print(f"[WARN] SSLError fetching universe badges: {e}")
            badge_api_failed = True
            universe_badge_ids = set()
        except Exception as e:
            print(f"[WARN] Error fetching universe badges: {e}")
            badge_api_failed = True
            universe_badge_ids = set()

        badge_target = get_badge_target_for_game(interaction.user.id, game_info["key"])


    user_id = get_user_id_from_username(roblox_username)
    if user_id is None:
        msg = "Could not find a Roblox user with that username."
        if invoked_from_button:
            await interaction.channel.send(msg)
        else:
            await interaction.followup.send(msg)
        return

    name, display_name_full, created_dt = get_user_basic_info(user_id)
    username_target = name or roblox_username
    display_name_target = display_name_full or username_target
    join_text = format_join_date(created_dt)

    headshot_url = get_user_headshot_url(user_id)
    profile_url = f"https://www.roblox.com/users/{user_id}/profile"

    try:
        friends = get_friends(user_id)
    except requests.HTTPError as e:
        # Some HTTPError instances don't have .response; fall back to string match.
        text = str(e)
        if "403" in text or "Forbidden" in text:
            msg = (
                "Error fetching friends: Roblox Friends API returned **403 Forbidden** "
                "for this user. Their friends list is not accessible via the public API."
            )
        else:
            msg = f"Error fetching friends: `{e}`"
        if invoked_from_button:
            await interaction.channel.send(msg)
        else:
            await interaction.followup.send(msg)
        return
    except Exception as e:
        msg = f"Error fetching friends: `{e}`"
        if invoked_from_button:
            await interaction.channel.send(msg)
        else:
            await interaction.followup.send(msg)
        return


    total_friends = len(friends)
    if total_friends == 0:
        msg = "This user has no public friends (or none returned by the API)."
        if invoked_from_button:
            await interaction.channel.send(msg)
        else:
            await interaction.followup.send(msg)
        return

    friend_ids = [f.get("id") for f in friends if isinstance(f.get("id"), int)]
    presence_map, presence_err = get_presence_for_users([user_id] + friend_ids)
    if presence_err:
        presence_rate_limited = True

    target_presence = presence_map.get(user_id)
    target_presence_text = presence_label(target_presence)

    combo_labels = [label for (label, _) in resolved_combos]
    combo_text = (
        "\n".join(f"- {lbl}" for lbl in combo_labels)
        if combo_labels
        else "None (combo scan disabled)"
    )

    desc_lines = [
        f"userId: `{user_id}`",
        f"[Profile link]({profile_url})",
        f"Account created: {join_text}",
        f"Presence: {target_presence_text}",
        f"Friends returned by API (max 200): {total_friends}",
        "",
        f"Match mode: {effective_mode}",
        "",
        "**Combos being scanned:**",
        combo_text,
        "",
        f"Bot version: {BOT_VERSION}",
    ]
    if game_info:
        desc_lines += [
            "",
            "**Game for badge stats:**",
            f"- key: `{game_info['key']}`",
            f"- placeId: `{game_info['placeId']}`",
            f"- universeId: `{game_info['universeId']}`",
        ]
        if badge_target:
            desc_lines.append(f"- Bot badge target: {badge_target} badges")

    start_embed = discord.Embed(
        title=f"Scanning {display_name_target} (@{username_target})'s friends",
        description="\n".join(desc_lines),
        color=discord.Color.green(),
    )
    if headshot_url:
        start_embed.set_thumbnail(url=headshot_url)
    if game_info:
        icon_url = get_game_icon_url(game_info["universeId"])
        if icon_url:
            start_embed.set_image(url=icon_url)

    if invoked_from_button:
        scan_msg = await interaction.channel.send(embed=start_embed)
    else:
        scan_msg: discord.WebhookMessage = await interaction.followup.send(
            embed=start_embed, wait=True
        )

    if not keep_scanning_embed:
        try:
            await scan_msg.delete()
        except Exception:
            pass

        class _ScanMsgLike:
            def __init__(self, channel):
                self.channel = channel

        scan_msg = _ScanMsgLike(interaction.channel)

    combos_part = (
        f"Combos: {', '.join(expanded_names)}"
        if expanded_names
        else "Combos: none (combo filter disabled)"
    )

    scan_control = ScanControl()
    cancel_view = ScanCancelView(
        control=scan_control,
        scan_message=scan_msg,
        invoker_id=interaction.user.id,
    )

    progress_msg = await scan_msg.channel.send(
        content=(
            f"Checking friends (combos): 0/{total_friends} processed...\n"
            f"{combos_part}"
        ),
        reference=None,
        mention_author=False,
        view=cancel_view,
    )

    combo_matches: list[dict] = []
    match_count = 0

    for idx, friend in enumerate(friends, start=1):
        if scan_control.cancelled:
            return

        friend_id = friend.get("id")

        try:
            if friend_id is None:
                await asyncio.sleep(AVATAR_REQUEST_DELAY)
                continue

            f_name, f_display, f_created = get_user_basic_info(friend_id)
            username = f_name or str(friend_id)
            display_name_friend = f_display or username

            if combos_disabled:
                matched_combos = ["(combo scan disabled)"]
                combo_match_detail = []
            else:
                assets = get_avatar_assets(friend_id)

                if effective_mode == "exact":
                    combo_matches_info = friend_matches_exact(assets, resolved_combos)
                else:
                    combo_matches_info = friend_matches_inexact(assets, resolved_combos)

                if not combo_matches_info:
                    await asyncio.sleep(AVATAR_REQUEST_DELAY)
                    if idx % 25 == 0 or idx == total_friends:
                        await progress_msg.edit(
                            content=(
                                f"Checking friends (combos): {idx}/{total_friends} processed...\n"
                                f"{combos_part} | Matches so far: {match_count}"
                            ),
                            view=cancel_view,
                        )
                    continue

                matched_combos = [
                    label for (label, matched_count, total_count) in combo_matches_info
                ]
                combo_match_detail = [
                    f"{label}: {matched_count}/{total_count} items"
                    for (label, matched_count, total_count) in combo_matches_info
                ]

            match_count += 1
            friend_headshot = get_user_headshot_url(friend_id)
            friend_join_text = format_join_date(f_created)

            friend_presence = presence_map.get(friend_id)
            friend_presence_text = presence_label(friend_presence)

            friend_total_friends, fc_err = get_friend_count_safe(friend_id)
            if fc_err:
                friendcount_rate_limited = True
            await asyncio.sleep(FRIEND_COUNT_REQUEST_DELAY)

            combo_matches.append(
                {
                    "username": username,
                    "display_name": display_name_friend,
                    "user_id": friend_id,
                    "profile_url": f"https://www.roblox.com/users/{friend_id}/profile",
                    "headshot_url": friend_headshot,
                    "matched_combos": matched_combos,
                    "combo_match_detail": combo_match_detail,
                    "join_text": friend_join_text,
                    "presence_text": friend_presence_text,
                    "friend_count": friend_total_friends,
                }
            )
        except Exception:
            pass

        await asyncio.sleep(AVATAR_REQUEST_DELAY)

        if idx % 25 == 0 or idx == total_friends:
            await progress_msg.edit(
                content=(
                    f"Checking friends (combos): {idx}/{total_friends} processed...\n"
                    f"{combos_part} | Matches so far: {match_count}"
                ),
                view=cancel_view,
            )

    match_data: list[dict] = []

    if game_info and combo_matches:
        await progress_msg.edit(
            content=(
                f"Checking friends (combos): {total_friends}/{total_friends} processed "
                f"({match_count} matches found).\n"
                f"Now checking badges for {len(combo_matches)} matched friends: "
                f"0/{len(combo_matches)} processed..."
            ),
            view=cancel_view,
        )

        enriched_matches: list[dict] = []

        for idx, m in enumerate(combo_matches, start=1):
            if scan_control.cancelled:
                return

            uid = m["user_id"]

            total_badges = 0
            game_badges = 0
            pct_of_total = 0.0
            badge_likelihood = 0.0

            try:
                badges = get_user_badges(uid, max_pages=None)
                total_badges = len(badges)
                game_badges = count_badges_for_universe(badges, universe_badge_ids)
                if total_badges > 0:
                    pct_of_total = (game_badges / total_badges) * 100.0

                badge_likelihood = 0.0

                if badge_target and badge_target > 0:
                    T = float(badge_target)

                    # Core band: ±20% around target
                    low = round(0.8 * T)
                    high = round(1.2 * T)
                    if low < 1:
                        low = 1
                    if high <= low:
                        high = low + 1

                    # Far-legit cutoff based on target (e.g. 4× target)
                    legit_factor = 4.0   # T=10 -> cutoff ~40
                    upper_zero = int(round(legit_factor * T))
                    if upper_zero <= high:
                        upper_zero = high + (high - low)

                    gb = game_badges

                    # 1) Base likelihood from badge count shape
                    if gb <= 0:
                        raw_likelihood = 0.0
                    elif gb < low:
                        raw_likelihood = (gb / low) * 100.0
                    elif gb <= high:
                        raw_likelihood = 100.0
                    elif gb < upper_zero:
                        # Exponential decay from 100 at high -> ~0 at upper_zero
                        x = (gb - high) / (upper_zero - high)  # 0..1
                        base = 0.4  # 0.4 gives moderate decay
                        decay = base ** x
                        raw_likelihood = 100.0 * decay
                    else:
                        raw_likelihood = 0.0

                    badge_likelihood = raw_likelihood

                    # 2) Ratio-based modulation ONLY when above target
                    if gb > T and total_badges > 0 and raw_likelihood > 0.0:
                        ratio = game_badges / total_badges  # 0..1

                        # Tuned for exploit alts like 22/30 (~0.73):
                        # - ratio <= 0.2 -> strong damping (0.2x)
                        # - ratio >= 0.6 -> almost full strength (0.9x)
                        # - in between -> smoothly interpolate
                        if ratio <= 0.2:
                            ratio_factor = 0.2  # was 0.3
                        elif ratio >= 0.6:
                            ratio_factor = 0.9
                        else:
                            t = (ratio - 0.2) / (0.6 - 0.2)
                            ratio_factor = 0.2 + t * (0.9 - 0.2)

                        badge_likelihood *= ratio_factor

                # Coverage rule: if <5% of their badges are from this game, treat as 0.
                if pct_of_total < 5.0:
                    badge_likelihood = 0.0
            except Exception:
                pass



            m_enriched = dict(m)
            m_enriched.update(
                {
                    "total_badges": total_badges,
                    "game_badges": game_badges,
                    "pct": pct_of_total,
                    "sus_score": badge_likelihood,
                }
            )
            enriched_matches.append(m_enriched)

            await asyncio.sleep(BADGE_REQUEST_DELAY)
            if idx % 5 == 0 or idx == len(combo_matches):
                await progress_msg.edit(
                    content=(
                        f"Checking badges for {len(combo_matches)} matched friends: "
                        f"{idx}/{len(combo_matches)} processed..."
                    ),
                    view=cancel_view,
                )

        match_data = enriched_matches
    else:
        match_data = [
            dict(m, total_badges=0, game_badges=0, pct=0.0, sus_score=0.0)
            for m in combo_matches
        ]

    cache_key = (
        interaction.user.id,
        roblox_username.lower(),
        combo_names.lower(),
        (game.lower() if game else None),
        effective_mode,
    )
    prev_set = SCAN_CACHE.get(cache_key, set())
    old_set = set(prev_set)
    current_ids = {m["user_id"] for m in match_data}

    new_ids_this_run = current_ids - old_set

    if prev_set:
        prev_set.update(current_ids)
        cumulative_set = prev_set
    else:
        cumulative_set = set(current_ids)
    SCAN_CACHE[cache_key] = cumulative_set

    cumulative_count = len(cumulative_set)

    match_data_to_show = match_data

    if not final_run:
        await progress_msg.edit(
            content=(
                f"Scan completed. Found {len(match_data)} matches this run, "
                f"{len(new_ids_this_run)} of them new. Total unique cached: {cumulative_count}."
            ),
            view=None,
        )
        return

    match_data_to_show = [m for m in match_data if m["user_id"] in cumulative_set]

    total_matches = len(match_data_to_show)
    percentage = (total_matches / total_friends) * 100 if total_friends > 0 else 0.0
    quantity_likelihood = min(total_matches * 10.0, 100.0) if total_matches > 0 else 0.0

    if game_info and match_data_to_show:
        risky = [m for m in match_data_to_show if m["sus_score"] >= 25.0]
        very_risky = [m for m in match_data_to_show if m["sus_score"] >= 75.0]
        k = len(risky)
        k_red = len(very_risky)
        n = len(match_data_to_show)
        if k > 0:
            weights = [m["sus_score"] / 100.0 for m in risky]
            weighted_sum = sum(m["sus_score"] * w for m, w in zip(risky, weights))
            weight_total = sum(weights)
            avg_badge = weighted_sum / weight_total if weight_total > 0 else 0.0
            p = k / n
        else:
            avg_badge = 0.0
            p = 0.0

        if k_red > 0:
            boost = 1.0 + 0.15 * (k_red ** 1.2)
            if boost > 2.0:
                boost = 2.0
        else:
            boost = 1.0

        contributing_count = k
        contributing_red_count = k_red
    else:
        avg_badge = 0.0
        p = 0.0
        boost = 1.0
        contributing_count = 0
        contributing_red_count = 0

    # If no game is set, we do NOT assign a Sus Score at all
    if game_info:
        base_sus = (quantity_likelihood + avg_badge) / 2.0
        target_sus_score = base_sus * p * boost
        if target_sus_score < 0.0:
            target_sus_score = 0.0
        if target_sus_score > 100.0:
            target_sus_score = 100.0
    else:
        target_sus_score = None

    combo_list_for_summary = (
        ", ".join(expanded_names)
        if expanded_names
        else "none (combo scan disabled)"
    )

    summary_lines = [
        f"Friends of {display_name_target}, @{username_target} wearing ANY of combos: {combo_list_for_summary}",
        f"Match mode: {effective_mode}",
        f"Matches in this output: **{total_matches}/{total_friends} ({percentage:.2f}% of API-visible friends, max 200)**",
        f"New unique matches this run: **{len(new_ids_this_run)}**",
        f"Total unique matches across runs for this config (cached): **{cumulative_count}**",
        "Note: Roblox friends API currently returns at most **200** friends.",
    ]

    if badge_api_failed and game_info:
        summary_lines.append(
            "⚠ Badge API error occurred during this scan; badge stats and Sus Scores "
            "may be incomplete."
        )

    summary_lines.extend(
        [
            "",
            "For exact formulas for the friend percentage and **Sus Score**, use `/rbi help` → Formulas.",
        ]
    )


    if game_info and total_matches > 0:
        if badge_target:
            summary_lines.append(
                f"Badge target for `{game_info['key']}` bots: **{badge_target} badges**"
            )
        p_display = p * 100.0
        summary_lines.append(
            f"Matched friends with orange/red badge likelihood (≥25% of matches): "
            f"**{contributing_count}/{total_matches} ({p_display:.2f}% of matches)**"
        )
        summary_lines.append(
            f"Matched friends in very high badge range (≥75% of matches): "
            f"**{contributing_red_count}/{total_matches}**"
        )

    # API disclaimers
    if presence_rate_limited:
        summary_lines.append(
            "⚠ Presence API returned partial or no data during this scan; some friend statuses "
            "may show as Unknown due to Roblox rate limiting."
        )
    if friendcount_rate_limited:
        summary_lines.append(
            "⚠ Friends API returned partial or no data for some users; friend counts marked "
            "as 'Rate limited' or 'Error' reflect Roblox API limits, not missing data in the bot."
        )

    if target_sus_score is None:
        title_text = "Risk level: **Not evaluated (no game set)**"
        summary_color = discord.Color.blurple()
    else:
        sus = target_sus_score
        if sus >= 70.0:
            risk_label = "High Risk"
            summary_color = discord.Color.red()
        elif sus >= 50.0:
            risk_label = "High"
            summary_color = discord.Color.orange()
        elif sus >= 10.0:
            risk_label = "Medium"
            summary_color = discord.Color.gold()
        else:
            risk_label = "Low"
            summary_color = discord.Color.green()
        title_text = f"Risk level: **{risk_label}**"

    # Scan finished; remove the Cancel button on progress message
    try:
        await progress_msg.edit(view=None)
    except Exception:
        pass

    summary_embed = discord.Embed(
        title=title_text,
        description="\n".join(summary_lines),
        color=summary_color,
    )
    if headshot_url:
        summary_embed.set_thumbnail(url=headshot_url)

    if target_sus_score is not None:
        summary_embed.add_field(
            name="Sus Score (fed by bots likelihood)",
            value=f"**{target_sus_score:.2f}%**",
            inline=False,
        )

    # Move bot version into footer instead of description
    summary_embed.set_footer(text=f"Bot version: {BOT_VERSION}")


    summary_view = ScanSummaryView(
        invoker_id=interaction.user.id,
        roblox_username=roblox_username,
        combo_names=combo_names,
        game=game,
        match_mode=effective_mode,
    )

    summary_message = await scan_msg.channel.send(
        reference=None,
        mention_author=False,
        embed=summary_embed,
        view=summary_view,
    )
    summary_view.summary_message = summary_message

    if not match_data_to_show:
        await scan_msg.channel.send(
            reference=None,
            mention_author=False,
            content=(
                "No friends found wearing any of those combos."
                if not combos_disabled
                else "Scan completed with combo filter disabled; no friends met the badge/game filters."
            ),
        )
        return

    match_data_to_show.sort(
        key=lambda m: (m["sus_score"], m["pct"], m["game_badges"], m["total_badges"]),
        reverse=True,
    )

    paginator = ResultsPaginator(
        invoker_id=interaction.user.id,
        match_data=match_data_to_show,
        game_info=game_info,
        combo_names=combo_names,
        game_key=(game.lower() if game else None),
        match_mode=effective_mode,
        target_username=username_target,
        target_display_name=display_name_target,
        per_page=3,
        channel_id=channel_id,
        scan_token=scan_token,
    )

    embeds, views = build_page_embeds_with_views(
        match_data_to_show,
        0,
        paginator.per_page,
        game_info,
        interaction.user.id,
        combo_names,
        (game.lower() if game else None),
        effective_mode,
    )

    header_message = await scan_msg.channel.send(
        reference=None,
        mention_author=False,
        content=f"Scan results (page 1/{paginator.max_page + 1}, total matches in this output: {len(match_data_to_show)})",
        view=paginator,
        embeds=[],
    )
    paginator.header_message = header_message
    paginator.result_messages = []
    channel = header_message.channel
    for emb, view in zip(embeds, views):
        msg = await channel.send(embed=emb, view=view)
        paginator.result_messages.append(msg)

    summary_view.paginator = paginator


@rbi_group.command(
    name="scan",
    description="Scan a Roblox user's friends for combos, optionally with badge stats for a game."
)
@app_commands.describe(
    roblox_username="Roblox username to scan",
    combo_names="Comma-separated combos (names, mycombos, globalcombos, all, none)",
    game="(Optional) game key (e.g. fisch or from /rbi mygames)",
    match_mode="Exact = full outfit, Inexact = any overlap"
)
@app_commands.choices(match_mode=[
    app_commands.Choice(name="Exact", value="exact"),
    app_commands.Choice(name="Inexact", value="inexact"),
])
async def rbi_scan(
    interaction: discord.Interaction,
    roblox_username: str,
    combo_names: str,
    game: str | None = None,
    match_mode: app_commands.Choice[str] | None = None,
):
    await interaction.response.defer()

    if match_mode is not None:
        effective_mode = match_mode.value
    else:
        effective_mode = None

    channel_id = interaction.channel.id
    scan_token = uuid.uuid4().hex
    ACTIVE_SCAN_TOKENS[channel_id] = scan_token
    RESCAN_IN_PROGRESS[channel_id] = False

    await run_scan_core(
        interaction=interaction,
        roblox_username=roblox_username,
        combo_names=combo_names,
        game=game,
        effective_mode=effective_mode,
        invoked_from_button=False,
        previous_cumulative_size=None,
        only_show_new=False,
        final_run=True,
        keep_scanning_embed=True,
        channel_id=channel_id,
        scan_token=scan_token,
    )


client.tree.add_command(rbi_group)

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN not found in environment or .env file.")
    client.run(DISCORD_TOKEN)
