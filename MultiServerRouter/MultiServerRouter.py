import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands

from core import checks
from core.config import ConfigManager
from core.models import InvalidConfigError, PermissionLevel
from core.paginator import EmbedPaginatorSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

GLOBAL_DEFAULTS = {
    "msrouter_enabled": True,
    "msrouter_skip_single_mutual": True,
    "msrouter_timeout_seconds": 60,
    "msrouter_prompt_text": "Please select the server you need support for:",
    "msrouter_no_mutual_text": "I don't share any server with you, so I'm unable to open a support thread.",
    "msrouter_route_missing_text": "This server hasn't been configured yet. Please contact a staff member directly.",
    "msrouter_require_role": True,
    "msrouter_show_selection": True,
    "msrouter_restrict_all_other_roles": False,
    "msrouter_role": {},
    "msrouter_category": {},
    "msrouter_label": {},
}

# Colours used throughout the plugin
COLOR_INFO    = 0x5865F2   # Discord blurple  – neutral / informational
COLOR_SUCCESS = 0x57F287   # Discord green    – success
COLOR_WARNING = 0xFEE75C   # Discord yellow   – warning / timeout
COLOR_ERROR   = 0xED4245   # Discord red      – error / blocked

PLUGIN_FOOTER = "MultiServerRouter"

# JSON file sitting next to this plugin file – survives bot restarts
_LOCAL_CONFIG_PATH = Path(__file__).with_suffix(".json")

# Optional local short-name mapping: map guild ID -> short label.
# Use this to provide shorter names for channel prefixes when server
# names are too long. These act as a fallback if no `msrouter_label`
# mapping is present in the bot config.
# Example:
# LOCAL_LABELS = {
#     123456789012345678: "Kurzname",
# }
LOCAL_LABELS: Dict[int, str] = {
    1160258234083450970: "AL",
    1487798778986758257: "SA",
    1308214357725020240: "DA",
    1238248843968122952: "PlainP",
    1488364715108466841: "PrimeP"
}

# Keys persisted to the local JSON file
_PERSISTENT_KEYS = {
    "msrouter_enabled",
    "msrouter_skip_single_mutual",
    "msrouter_timeout_seconds",
    "msrouter_prompt_text",
    "msrouter_no_mutual_text",
    "msrouter_route_missing_text",
    "msrouter_require_role",
    "msrouter_show_selection",
    "msrouter_restrict_all_other_roles",
    "msrouter_role",
    "msrouter_category",
    "msrouter_label",
}

# Regex patterns for mention / invite extraction
_RE_ROLE_MENTION    = re.compile(r"^<@&(\d+)>$")
_RE_CHANNEL_MENTION = re.compile(r"^<#(\d+)>$")
_RE_INVITE          = re.compile(r"(?:https?://)?(?:www\.)?discord(?:\.gg|(?:app)?\.com/invite)/([A-Za-z0-9\-]+)")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _short(text: str, limit: int) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _slug(text: str, fallback: str = "support") -> str:
    text = str(text).lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or fallback


def _parse_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        value = str(value).strip()
        if not value:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_id_from_token(token: str) -> Optional[int]:
    """
    Parse a snowflake ID from a raw string, a role mention (<@&ID>),
    or a channel mention (<#ID>). Returns None if nothing matches.
    """
    token = token.strip()
    for pattern in (_RE_ROLE_MENTION, _RE_CHANNEL_MENTION):
        m = pattern.match(token)
        if m:
            return _parse_int(m.group(1))
    return _parse_int(token)


async def _resolve_guild_id(token: str, bot) -> Optional[int]:
    """
    Resolve a guild ID from a raw snowflake OR a Discord invite URL.
    Raises InvalidConfigError if an invite is given but cannot be resolved.
    """
    token = token.strip()
    m = _RE_INVITE.search(token)
    if m:
        invite_code = m.group(1)
        try:
            invite = await bot.fetch_invite(invite_code)
            if invite.guild:
                return invite.guild.id
        except Exception:
            raise InvalidConfigError(
                f"Could not resolve invite `{invite_code}`. Make sure the link is valid and the bot can see it."
            )
        return None
    return _parse_int(token)


def _make_embed(
    title: str,
    description: str = "",
    color: int = COLOR_INFO,
    *,
    footer: str = PLUGIN_FOOTER,
) -> discord.Embed:
    """Return a consistently styled plugin embed."""
    embed = discord.Embed(title=title, description=description or None, color=color)
    embed.set_footer(text=footer)
    return embed


def _bool_icon(value: bool) -> str:
    return "✅" if value else "❌"


# ---------------------------------------------------------------------------
# Register config keys with Modmail
# ---------------------------------------------------------------------------

ConfigManager.public_keys.update(GLOBAL_DEFAULTS)
ConfigManager.defaults.update(GLOBAL_DEFAULTS)
ConfigManager.all_keys.update(GLOBAL_DEFAULTS.keys())


# ---------------------------------------------------------------------------
# UI – Server selection dropdown
# ---------------------------------------------------------------------------

class ServerSelect(discord.ui.Select):
    """Dropdown shown in the user's DMs for picking their source server."""

    def __init__(
        self,
        cog: "MultiServerRouter",
        user_id: int,
        guilds: List[discord.Guild],
        placeholder: str,
    ):
        self.cog = cog
        self.user_id = user_id

        options = []
        for guild in guilds[:25]:
            # Always show the actual server name in the user's selection
            # dropdown. Short-name mappings are only applied to the
            # staff-visible channel name, not the DM menu.
            label = guild.name
            options.append(
                discord.SelectOption(
                    label=_short(label, 100),
                    value=str(guild.id),
                    emoji="🏷️",
                )
            )

        super().__init__(
            placeholder=_short(placeholder, 150),
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=_make_embed(
                    "⛔ Not Your Menu",
                    "This selection menu belongs to someone else.",
                    COLOR_ERROR,
                ),
                ephemeral=True,
            )
            return

        selected_guild_id = int(self.values[0])
        selected_guild = self.cog.bot.get_guild(selected_guild_id)
        guild_name = selected_guild.name if selected_guild else f"Server {selected_guild_id}"

        self.view.selected_guild_id = selected_guild_id
        self.view.stop()

        confirm_embed = _make_embed(
            "✅ Server Selected",
            f"Opening a support thread for **{guild_name}**.\nPlease wait a moment…",
            COLOR_SUCCESS,
        )

        try:
            await interaction.response.edit_message(embed=confirm_embed, view=None)
        except Exception:
            try:
                await interaction.response.defer()
                await interaction.message.edit(embed=confirm_embed, view=None)
            except Exception:
                pass


class ServerSelectView(discord.ui.View):
    """View wrapper that holds the ServerSelect dropdown."""

    def __init__(
        self,
        cog: "MultiServerRouter",
        user_id: int,
        guilds: List[discord.Guild],
        timeout: int,
        placeholder: str,
    ):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.user_id = user_id
        self.selected_guild_id: Optional[int] = None
        self.message: Optional[discord.Message] = None
        self.add_item(ServerSelect(cog, user_id, guilds, placeholder))

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

        timeout_embed = _make_embed(
            "⏰ Selection Timed Out",
            "You didn't select a server in time.\nSend another message to try again.",
            COLOR_WARNING,
        )

        if self.message is not None:
            try:
                await self.message.edit(embed=timeout_embed, view=self)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Main Cog
# ---------------------------------------------------------------------------

class MultiServerRouter(commands.Cog):
    """
    Multi-server DM router for Modmail.

    Intercepts incoming DMs and asks the user which mutual server they need
    support for *before* the normal Modmail thread flow starts. Then it
    creates the thread in the correct category and restricts visibility to
    the configured staff role.
    """

    def __init__(self, bot):
        self.bot = bot
        self._original_process_dm_modmail = None
        self._original_threads_create = None
        self._original_config_set = None
        self._pending_routes: Dict[int, Dict[str, Any]] = {}
        self._prompting_users: set[int] = set()

    async def cog_load(self):
        self._load_local_config()
        self._patch_config_manager()
        self._patch_core_hooks()
        self._register_help_text()

    def cog_unload(self):
        self._restore_core_hooks()
        self._restore_config_manager()

    # ------------------------------------------------------------------
    # Local JSON persistence
    # ------------------------------------------------------------------

    def _load_local_config(self):
        """Load persisted msrouter values from the local JSON file into bot.config cache."""
        if not _LOCAL_CONFIG_PATH.exists():
            return
        try:
            with _LOCAL_CONFIG_PATH.open("r", encoding="utf-8") as f:
                data: dict = json.load(f)
        except Exception as exc:
            print(f"[MultiServerRouter] Failed to load local config: {exc}")
            return

        for key, value in data.items():
            if key in _PERSISTENT_KEYS:
                self.bot.config._cache[key] = value

    def _save_local_config(self):
        """Persist all msrouter config values to the local JSON file."""
        data = {}
        for key in _PERSISTENT_KEYS:
            value = self.bot.config.get(key, convert=False)
            if value is not None:
                data[key] = value
        try:
            with _LOCAL_CONFIG_PATH.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            print(f"[MultiServerRouter] Failed to save local config: {exc}")

    # ------------------------------------------------------------------
    # Config patching
    # ------------------------------------------------------------------

    def _patch_config_manager(self):
        if self._original_config_set is not None:
            return

        self._original_config_set = self.bot.config.set

        async def wrapped_set(key: str, item: Any, convert: bool = True):
            key = key.lower()

            if key in {"msrouter_role", "msrouter_category", "msrouter_label"}:
                # Tokenise the raw input string
                if isinstance(item, str):
                    parts = shlex.split(item)
                elif isinstance(item, (list, tuple)):
                    parts = [str(x) for x in item]
                else:
                    parts = shlex.split(str(item))

                if len(parts) < 2:
                    raise InvalidConfigError(
                        "Expected two arguments: <guild_id_or_invite> <value>"
                    )

                guild_id_token = parts[0]
                value_token    = " ".join(parts[1:]).strip()

                # First argument: plain snowflake OR Discord invite URL
                guild_id = await _resolve_guild_id(guild_id_token, self.bot)
                if guild_id is None:
                    raise InvalidConfigError(
                        "The first argument must be a valid guild ID or a Discord invite link."
                    )

                current = self.bot.config.get(key, convert=False)
                if not isinstance(current, dict):
                    current = {}

                if key == "msrouter_label":
                    current[str(guild_id)] = value_token
                else:
                    # Second argument: plain snowflake, role mention <@&ID>, or channel mention <#ID>
                    target_id = _extract_id_from_token(value_token)
                    if target_id is None:
                        raise InvalidConfigError(
                            "The second argument must be a valid ID or mention "
                            "(e.g. a role mention <@&ID> or a raw snowflake)."
                        )
                    current[str(guild_id)] = target_id

                result = await self._original_config_set(key, current, convert=False)
                self._save_local_config()
                return result

            result = await self._original_config_set(key, item, convert=convert)
            if key in _PERSISTENT_KEYS:
                self._save_local_config()
            return result

        self.bot.config.set = wrapped_set

    def _restore_config_manager(self):
        if self._original_config_set is not None:
            self.bot.config.set = self._original_config_set
            self._original_config_set = None

    # ------------------------------------------------------------------
    # Config help registration
    # ------------------------------------------------------------------

    def _register_help_entry(
        self,
        key: str,
        default: Any,
        description: str,
        *,
        examples: Optional[List[str]] = None,
        notes: Optional[List[str]] = None,
    ):
        self.bot.config.public_keys[key] = default
        self.bot.config.defaults[key] = default
        self.bot.config.all_keys.add(key)
        self.bot.config._cache.setdefault(key, default)
        self.bot.config.config_help.setdefault(
            key,
            {
                "default": default,
                "description": description,
                "examples": examples or [],
                "notes": notes or [],
                "image": None,
                "thumbnail": None,
            },
        )

    def _register_help_text(self):
        self._register_help_entry(
            "msrouter_enabled",
            GLOBAL_DEFAULTS["msrouter_enabled"],
            (
                "Enables or disables the MultiServerRouter plugin entirely. "
                "When set to `false`, all incoming DMs bypass the server-selection "
                "prompt and go straight to the normal Modmail thread flow."
            ),
            examples=["true", "false"],
            notes=[
                "Usage: `.config set msrouter_enabled <true|false>`",
                "Setting this to `false` disables the router without unloading the plugin.",
            ],
        )
        self._register_help_entry(
            "msrouter_skip_single_mutual",
            GLOBAL_DEFAULTS["msrouter_skip_single_mutual"],
            (
                "When `true`, users who share exactly one mutual server with the bot are "
                "routed to that server automatically — no dropdown is shown. "
                "Set to `false` to always display the selection menu, even for single-server users."
            ),
            examples=["true", "false"],
            notes=[
                "Usage: `.config set msrouter_skip_single_mutual <true|false>`",
            ],
        )
        self._register_help_entry(
            "msrouter_timeout_seconds",
            GLOBAL_DEFAULTS["msrouter_timeout_seconds"],
            (
                "How many seconds the server-selection dropdown stays active in the user's DMs "
                "before it expires. Once expired, the menu is disabled and the user must "
                "send a new message to start over."
            ),
            examples=["60", "120"],
            notes=[
                "Usage: `.config set msrouter_timeout_seconds <seconds>`",
                "A minimum of 30 seconds is recommended.",
            ],
        )
        self._register_help_entry(
            "msrouter_prompt_text",
            GLOBAL_DEFAULTS["msrouter_prompt_text"],
            (
                "The body text of the embed displayed to the user when the "
                "server-selection dropdown appears. Keep it short and action-oriented."
            ),
            examples=[
                "Please select the server you need support for:",
                "Which server are you contacting us about?",
            ],
            notes=[
                'Usage: `.config set msrouter_prompt_text "Your text here"`',
            ],
        )
        self._register_help_entry(
            "msrouter_no_mutual_text",
            GLOBAL_DEFAULTS["msrouter_no_mutual_text"],
            (
                "The message sent to users who share no mutual servers with the bot. "
                "These users cannot open a thread because there is no server to route them to."
            ),
            examples=[
                "I don't share any server with you, so I'm unable to open a support thread.",
            ],
            notes=[
                'Usage: `.config set msrouter_no_mutual_text "Your text here"`',
                "Plain text only — an error embed is added automatically.",
            ],
        )
        self._register_help_entry(
            "msrouter_route_missing_text",
            GLOBAL_DEFAULTS["msrouter_route_missing_text"],
            (
                "Internal fallback text used when a user selects a server that has no "
                "staff role configured. The user always sees a generic error embed; "
                "this text is logged for diagnostic purposes only."
            ),
            examples=[
                "This server hasn't been configured yet. Please contact a staff member directly.",
            ],
            notes=[
                'Usage: `.config set msrouter_route_missing_text "Your text here"`',
                "This text is not shown directly to users.",
            ],
        )
        self._register_help_entry(
            "msrouter_require_role",
            GLOBAL_DEFAULTS["msrouter_require_role"],
            (
                "When `true`, every routed server must have a staff role configured via "
                "`msrouter_role`. Users who select a server without a role mapping "
                "receive an error and cannot open a thread. "
                "Set to `false` to allow threads from unconfigured servers."
            ),
            examples=["true", "false"],
            notes=[
                "Usage: `.config set msrouter_require_role <true|false>`",
                "If set to `false`, threads are still sorted into the correct category "
                "(if configured), but no channel permission restrictions are applied.",
            ],
        )
        self._register_help_entry(
            "msrouter_show_selection",
            GLOBAL_DEFAULTS["msrouter_show_selection"],
            (
                "When `true`, an informational embed is automatically posted at the top of "
                "each new thread showing which server the user selected, the assigned staff "
                "role, and the category the thread was created in. "
                "Only staff can see this note — it is not sent to the user."
            ),
            examples=["true", "false"],
            notes=[
                "Usage: `.config set msrouter_show_selection <true|false>`",
                "Disable this if you prefer a cleaner thread view without routing metadata.",
            ],
        )
        self._register_help_entry(
            "msrouter_restrict_all_other_roles",
            GLOBAL_DEFAULTS["msrouter_restrict_all_other_roles"],
            (
                "When `true`, all roles except the configured staff role are explicitly "
                "blocked from viewing the thread channel. This provides maximum security "
                "by ensuring only the whitelisted role can see the channel. "
                "When `false` (default), Discord's standard permission inheritance applies."
            ),
            examples=["true", "false"],
            notes=[
                "Usage: `.config set msrouter_restrict_all_other_roles <true|false>`",
                "Enable this if you want strict access control and prevent accidental role inheritance.",
                "The configured staff role and bot role are always whitelisted.",
            ],
        )
        self._register_help_entry(
            "msrouter_role",
            GLOBAL_DEFAULTS["msrouter_role"],
            (
                "Maps a source server to the staff role that should have access to threads "
                "routed from that server. All other roles are hidden from the thread channel "
                "via Discord permission overwrites."
            ),
            examples=[
                "1308214357725020240 1493463698210029576",
                "discord.gg/myserver <@&1493463698210029576>",
                "https://discord.gg/myserver @StaffRole",
            ],
            notes=[
                "Usage: `.config set msrouter_role <guild> <role>`",
                "`<guild>` accepts: a guild ID (snowflake) or a Discord invite link (discord.gg/…).",
                "`<role>` accepts: a role ID (snowflake) or a role mention (<@&ID>).",
                "The guild is the *source* server users are a member of.",
                "The role must exist in the *Modmail* server.",
            ],
        )
        self._register_help_entry(
            "msrouter_category",
            GLOBAL_DEFAULTS["msrouter_category"],
            (
                "Maps a source server to a category channel in the Modmail server "
                "where new threads from that server will be created. "
                "If not configured, threads fall back to the main Modmail category."
            ),
            examples=[
                "1308214357725020240 987654321098765432",
                "discord.gg/myserver 987654321098765432",
            ],
            notes=[
                "Usage: `.config set msrouter_category <guild> <category_id>`",
                "`<guild>` accepts: a guild ID (snowflake) or a Discord invite link (discord.gg/…).",
                "`<category_id>` must be the ID of a category channel in the Modmail server.",
            ],
        )
        self._register_help_entry(
            "msrouter_label",
            GLOBAL_DEFAULTS["msrouter_label"],
            (
                "Maps a source server to a custom display label used in the user's "
                "server-selection dropdown and as a prefix in the thread channel name. "
                "If not set, the actual server name is used instead."
            ),
            examples=[
                '1308214357725020240 "Main Community"',
                "discord.gg/myserver Support Server",
            ],
            notes=[
                "Usage: `.config set msrouter_label <guild> <label>`",
                "`<guild>` accepts: a guild ID (snowflake) or a Discord invite link (discord.gg/…).",
                "Wrap multi-word labels in quotes when entering them directly.",
            ],
        )

    # ------------------------------------------------------------------
    # Core hook patching
    # ------------------------------------------------------------------

    def _patch_core_hooks(self):
        if self._original_process_dm_modmail is None:
            self._original_process_dm_modmail = self.bot.process_dm_modmail
        if self._original_threads_create is None:
            self._original_threads_create = self.bot.threads.create

        self.bot.process_dm_modmail = self._process_dm_modmail_router
        self.bot.threads.create = self._threads_create_router

    def _restore_core_hooks(self):
        if self._original_process_dm_modmail is not None:
            self.bot.process_dm_modmail = self._original_process_dm_modmail
        if self._original_threads_create is not None:
            self.bot.threads.create = self._original_threads_create

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _mapping(self, key: str) -> Dict[str, Any]:
        value = self.bot.config.get(key, convert=False)
        if isinstance(value, dict):
            return value
        return {}

    def _get_role_id(self, guild_id: int) -> Optional[int]:
        return _parse_int(self._mapping("msrouter_role").get(str(guild_id)))

    def _get_category_id(self, guild_id: int) -> Optional[int]:
        return _parse_int(self._mapping("msrouter_category").get(str(guild_id)))

    def _get_label(self, guild_id: int) -> Optional[str]:
        # Prefer the configurable `msrouter_label` mapping first
        value = self._mapping("msrouter_label").get(str(guild_id))
        if value:
            return str(value)

        # Fallback to local hardcoded `LOCAL_LABELS` (int keys)
        local = None
        try:
            local = LOCAL_LABELS.get(int(guild_id))
        except Exception:
            local = LOCAL_LABELS.get(str(guild_id))

        return str(local) if local else None

    def _get_route_data(self, guild_id: int) -> Dict[str, Any]:
        return {
            "role_id": self._get_role_id(guild_id),
            "category_id": self._get_category_id(guild_id),
            "label": self._get_label(guild_id),
        }

    def _get_mutual_guilds(self, user_id: int) -> List[discord.Guild]:
        modmail_guild = self.bot.modmail_guild or self.bot.guild
        mutuals = []
        for guild in self.bot.guilds:
            if modmail_guild and guild.id == modmail_guild.id:
                continue
            if guild.get_member(user_id):
                mutuals.append(guild)
        mutuals.sort(key=lambda g: g.name.lower())
        return mutuals

    def _resolve_category(self, guild_id: int) -> Optional[discord.CategoryChannel]:
        modmail_guild = self.bot.modmail_guild or self.bot.guild
        if modmail_guild is None:
            return None
        category_id = self._get_category_id(guild_id)
        if category_id is not None:
            channel = modmail_guild.get_channel(category_id)
            if isinstance(channel, discord.CategoryChannel):
                return channel
        return self.bot.main_category

    def _resolve_role(self, guild_id: int) -> Optional[discord.Role]:
        modmail_guild = self.bot.modmail_guild or self.bot.guild
        if modmail_guild is None:
            return None
        role_id = self._get_role_id(guild_id)
        if role_id is not None:
            return modmail_guild.get_role(role_id)
        return None

    def _channel_name_for_route(self, guild: discord.Guild, user: discord.abc.User) -> str:
        label = self._get_label(guild.id) or guild.name
        base = self.bot.format_channel_name(user)
        name = _slug(f"{label}-{base}", fallback="support-thread")
        return _short(name, 90)

    # ------------------------------------------------------------------
    # Routing logic
    # ------------------------------------------------------------------

    async def _send_dm_embed(self, user: discord.abc.User, embed: discord.Embed):
        try:
            await user.send(embed=embed)
        except discord.Forbidden:
            pass

    async def _prompt_for_guild(
        self, message: discord.Message, mutuals: List[discord.Guild]
    ) -> Optional[discord.Guild]:
        if not mutuals:
            embed = _make_embed(
                "❌ No Shared Servers",
                self.bot.config.get("msrouter_no_mutual_text"),
                COLOR_ERROR,
            )
            await self._send_dm_embed(message.author, embed)
            return None

        skip_single = self.bot.config.get("msrouter_skip_single_mutual")
        if skip_single is None:
            skip_single = True

        if len(mutuals) == 1 and skip_single:
            return mutuals[0]

        prompt_text = self.bot.config.get("msrouter_prompt_text") or GLOBAL_DEFAULTS["msrouter_prompt_text"]
        timeout = _parse_int(self.bot.config.get("msrouter_timeout_seconds")) or 60

        view = ServerSelectView(
            self,
            message.author.id,
            mutuals,
            timeout=timeout,
            placeholder="Choose a server…",
        )

        prompt_embed = _make_embed(
            "🎫 Support Request",
            prompt_text,
            COLOR_INFO,
            footer=f"{PLUGIN_FOOTER} • Expires in {timeout}s",
        )

        try:
            sent = await message.author.send(embed=prompt_embed, view=view)
        except discord.Forbidden:
            return None

        view.message = sent
        await view.wait()

        if view.selected_guild_id is None:
            return None

        return self.bot.get_guild(view.selected_guild_id)

    async def _post_selection_note(self, thread, selected_guild: discord.Guild):
        """
        Post a staff-only informational embed in the thread channel showing
        which server the user selected and the routing details applied.
        """
        show = self.bot.config.get("msrouter_show_selection")
        if show is False:
            logger.debug(f"[MultiServerRouter] Skipping selection note for {selected_guild.name} (disabled in config)")
            return

        logger.debug(f"[MultiServerRouter] _post_selection_note called: thread.id={thread.id if hasattr(thread, 'id') else 'NO ID'}, thread type={type(thread).__name__}")
        channel = getattr(thread, "channel", None)
        logger.debug(f"[MultiServerRouter] Got channel: {channel}, type={type(channel).__name__}")
        if channel is None:
            logger.warning(f"[MultiServerRouter] Cannot post selection note: thread.channel is None")
            return
        if not isinstance(channel, discord.TextChannel):
            logger.warning(f"[MultiServerRouter] Cannot post selection note: thread.channel is not a TextChannel, got {type(channel).__name__}")
            return

        try:
            route = self._get_route_data(selected_guild.id)
            label = route["label"] or selected_guild.name
            role_id     = route["role_id"]
            category_id = route["category_id"]

            embed = discord.Embed(
                title="🔀 Routed via MultiServerRouter",
                color=self.bot.main_color,
            )
            embed.add_field(
                name="Selected Server",
                value=f"**{label}**\n`{selected_guild.id}`",
                inline=True,
            )
            embed.add_field(
                name="Staff Role",
                value=f"<@&{role_id}>" if role_id else "*none configured*",
                inline=True,
            )
            embed.set_footer(text=f"{PLUGIN_FOOTER} • This note is only visible to staff")

            await channel.send(embed=embed)
            logger.info(f"[MultiServerRouter] Selection note posted for {selected_guild.name} in thread #{channel.name}")
        except discord.Forbidden:
            logger.error(f"[MultiServerRouter] No permission to send selection note in #{channel.name}")
        except discord.HTTPException as e:
            logger.error(f"[MultiServerRouter] HTTP error posting selection note: {e}")
        except Exception as e:
            logger.exception(f"[MultiServerRouter] Unexpected error posting selection note for {selected_guild.name}: {e}")

    async def _apply_route_permissions_and_name(self, thread, selected_guild: discord.Guild):
        logger.debug(f"[MultiServerRouter] _apply_route_permissions_and_name called: thread.id={thread.id if hasattr(thread, 'id') else 'NO ID'}, thread type={type(thread).__name__}")
        channel = getattr(thread, "channel", None)
        logger.debug(f"[MultiServerRouter] Got channel: {channel}, type={type(channel).__name__}")
        if channel is None:
            logger.warning(f"[MultiServerRouter] Cannot apply permissions: thread.channel is None")
            return
        if not isinstance(channel, discord.TextChannel):
            logger.warning(f"[MultiServerRouter] Cannot apply permissions: thread.channel is not a TextChannel, got {type(channel).__name__}")
            return

        modmail_guild = self.bot.modmail_guild or self.bot.guild
        if modmail_guild is None:
            logger.error(f"[MultiServerRouter] Cannot apply permissions: modmail_guild is None")
            return

        role = self._resolve_role(selected_guild.id)
        require_role = self.bot.config.get("msrouter_require_role")
        if require_role is None:
            require_role = True

        if require_role and role is None:
            logger.error(f"[MultiServerRouter] Cannot apply permissions for {selected_guild.name}: role required but not configured")
            return

        try:
            # Build permission overwrites using whitelist approach:
            # - Bot gets full access
            # - Configured staff role gets access (if exists)
            # - Everyone else (@everyone) is explicitly blocked
            # - All other roles are explicitly blocked to prevent inheritance
            overwrites = {
                modmail_guild.default_role: discord.PermissionOverwrite(view_channel=False),
                modmail_guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    read_message_history=True,
                    send_messages=True,
                    manage_channels=True,
                    manage_messages=True,
                    attach_files=True,
                    embed_links=True,
                    add_reactions=True,
                ),
            }

            if role is not None:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    read_message_history=True,
                    send_messages=True,
                )
                logger.debug(f"[MultiServerRouter] Adding staff role {role.name} (ID: {role.id}) to overwrites")
            else:
                logger.debug(f"[MultiServerRouter] No staff role to add (require_role={require_role})")

            # Optionally block all other roles explicitly for maximum security
            restrict_all = self.bot.config.get("msrouter_restrict_all_other_roles")
            if restrict_all:
                restricted_count = 0
                for other_role in modmail_guild.roles:
                    if other_role not in overwrites and other_role != modmail_guild.default_role and other_role != modmail_guild.me:
                        overwrites[other_role] = discord.PermissionOverwrite(view_channel=False)
                        restricted_count += 1
                logger.debug(f"[MultiServerRouter] Restricted view_channel for {restricted_count} other roles")

            # Apply name AND overwrites in a single atomic API call.
            # This prevents Modmail's category permissions from interfering
            # between separate set_permissions() calls.
            new_channel_name = self._channel_name_for_route(selected_guild, thread.recipient)
            await channel.edit(
                name=new_channel_name,
                overwrites=overwrites,
                reason=f"MultiServerRouter: route for {selected_guild.name}",
            )
            logger.info(f"[MultiServerRouter] Successfully applied permissions and renamed channel to '{new_channel_name}' for {selected_guild.name}")
        except discord.Forbidden:
            logger.error(f"[MultiServerRouter] No permission to modify channel permissions for {selected_guild.name}")
        except discord.HTTPException as exc:
            logger.error(f"[MultiServerRouter] Discord API error applying permissions for {selected_guild.name}: {exc}")
        except Exception as exc:
            logger.exception(f"[MultiServerRouter] Unexpected error applying permissions/name for {selected_guild.name}: {exc}")

    async def _threads_create_router(self, *args, **kwargs):
        recipient = kwargs.get("recipient") or (args[0] if args else None)
        recipient_id = getattr(recipient, "id", None) if recipient else None
        logger.debug(f"[MultiServerRouter] _threads_create_router called with recipient_id: {recipient_id}")

        route = None
        if recipient is not None:
            route = self._pending_routes.get(getattr(recipient, "id", None))

        if route and kwargs.get("category") is None:
            kwargs["category"] = route.get("category")
            logger.debug(f"[MultiServerRouter] Setting category for thread creation: {route.get('category')}")

        try:
            thread = await self._original_threads_create(*args, **kwargs)
            logger.info(f"[MultiServerRouter] Thread created for recipient_id {recipient_id}")
            logger.debug(f"[MultiServerRouter] Thread object type: {type(thread).__name__}, has channel attr: {hasattr(thread, 'channel')}")
        except Exception as exc:
            logger.error(f"[MultiServerRouter] Error creating thread: {exc}")
            if recipient is not None:
                self._pending_routes.pop(getattr(recipient, "id", None), None)
            raise

        if route and thread is not None:
            try:
                guild_name = route["guild"].name if route["guild"] else "Unknown"
                logger.info(f"[MultiServerRouter] Applying route for guild {guild_name}")
                
                # Wait until thread is ready to ensure channel is initialized
                logger.debug(f"[MultiServerRouter] Checking thread ready state: ready={thread.ready}")
                if not thread.ready:
                    logger.debug(f"[MultiServerRouter] Thread not ready yet, waiting...")
                    await thread.wait_until_ready()
                    logger.debug(f"[MultiServerRouter] Thread is now ready")
                
                channel = thread.channel
                logger.debug(f"[MultiServerRouter] After wait, thread.channel is None: {channel is None}, type={type(channel).__name__ if channel else 'None'}")
                await self._apply_route_permissions_and_name(thread, route["guild"])
                await self._post_selection_note(thread, route["guild"])
                logger.info(f"[MultiServerRouter] Route applied successfully for {guild_name}")
            except Exception as exc:
                logger.exception(f"[MultiServerRouter] Error applying route: {exc}")
            finally:
                if recipient is not None:
                    self._pending_routes.pop(getattr(recipient, "id", None), None)

        return thread

    async def _process_dm_modmail_router(self, message: discord.Message):
        if self.bot.config.get("msrouter_enabled") is False:
            logger.debug("[MultiServerRouter] Router disabled, using original process_dm_modmail")
            return await self._original_process_dm_modmail(message)

        if message.author.bot or not isinstance(message.channel, discord.DMChannel):
            return await self._original_process_dm_modmail(message)

        if message.author.id in self._prompting_users:
            logger.debug(f"[MultiServerRouter] User {message.author.id} already being prompted")
            return

        existing = await self.bot.threads.find(recipient=message.author)
        if existing is not None:
            logger.debug(f"[MultiServerRouter] Existing thread found for {message.author.id}")
            return await self._original_process_dm_modmail(message)

        if getattr(message, "type", discord.MessageType.default) not in (
            discord.MessageType.default,
            discord.MessageType.reply,
        ):
            return await self._original_process_dm_modmail(message)

        self._prompting_users.add(message.author.id)
        try:
            logger.info(f"[MultiServerRouter] Processing DM from {message.author} ({message.author.id})")
            mutuals = self._get_mutual_guilds(message.author.id)
            logger.debug(f"[MultiServerRouter] Found {len(mutuals)} mutual guild(s) for {message.author.id}")
            
            selected_guild = await self._prompt_for_guild(message, mutuals)
            if selected_guild is None:
                logger.info(f"[MultiServerRouter] No guild selected by {message.author.id}")
                return

            logger.info(f"[MultiServerRouter] Guild selected: {selected_guild.name} ({selected_guild.id}) for user {message.author.id}")
            
            require_role = self.bot.config.get("msrouter_require_role")
            if require_role is None:
                require_role = True

            route_role_id = self._get_role_id(selected_guild.id)
            if require_role and route_role_id is None:
                logger.error(f"[MultiServerRouter] Role required for {selected_guild.name} but not configured. Blocking {message.author.id}")
                embed = _make_embed(
                    "❌ Something Went Wrong",
                    "We were unable to process your support request.\nPlease reach out to the staff team directly.",
                    COLOR_ERROR,
                )
                await self._send_dm_embed(message.author, embed)
                return

            category = self._resolve_category(selected_guild.id)
            self._pending_routes[message.author.id] = {
                "guild": selected_guild,
                "category": category,
            }
            logger.debug(f"[MultiServerRouter] Pending route created for {message.author.id}: guild={selected_guild.name}, category={category}")

            return await self._original_process_dm_modmail(message)
        except Exception as exc:
            logger.exception(f"[MultiServerRouter] Error in router for {message.author.id}: {exc}")
        finally:
            self._prompting_users.discard(message.author.id)

    # ------------------------------------------------------------------
    # Admin commands
    # ------------------------------------------------------------------

    @commands.group(name="msrouter", invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.OWNER)
    async def msrouter(self, ctx):
        """Manage and inspect the MultiServerRouter plugin."""
        await ctx.send_help(ctx.command)

    @msrouter.command(name="status")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def msrouter_status(self, ctx):
        """Shows the current global plugin settings at a glance."""
        enabled        = self.bot.config.get("msrouter_enabled")
        skip_single    = self.bot.config.get("msrouter_skip_single_mutual")
        timeout        = _parse_int(self.bot.config.get("msrouter_timeout_seconds")) or 60
        require_role   = self.bot.config.get("msrouter_require_role")
        show_selection = self.bot.config.get("msrouter_show_selection")
        restrict_all   = self.bot.config.get("msrouter_restrict_all_other_roles")
        prompt_text    = self.bot.config.get("msrouter_prompt_text") or GLOBAL_DEFAULTS["msrouter_prompt_text"]
        no_mutual_text = self.bot.config.get("msrouter_no_mutual_text")
        mutuals        = self._get_mutual_guilds(ctx.author.id)

        embed = discord.Embed(
            title="🔀 MultiServerRouter — Plugin Status",
            color=self.bot.main_color,
        )
        embed.add_field(
            name="General",
            value=(
                f"{_bool_icon(enabled)} **Enabled**\n"
                f"{_bool_icon(skip_single)} **Skip single mutual**\n"
                f"{_bool_icon(require_role)} **Require role mapping**\n"
                f"{_bool_icon(show_selection)} **Show selection in thread**\n"
                f"{_bool_icon(restrict_all)} **Restrict all other roles**\n"
                f"⏱ **Timeout:** {timeout}s"
            ),
            inline=True,
        )
        embed.add_field(
            name="Mutual Servers",
            value=f"**{len(mutuals)}** server(s) detected",
            inline=True,
        )
        embed.add_field(
            name="DM Prompt Text",
            value=f"```{_short(prompt_text, 200)}```",
            inline=False,
        )
        embed.add_field(
            name="No Mutual Text",
            value=f"```{_short(no_mutual_text, 200)}```",
            inline=False,
        )
        embed.set_footer(text=f"{PLUGIN_FOOTER} • Use '.msrouter show' for per-server details")
        await ctx.send(embed=embed)

    @msrouter.command(name="show")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def msrouter_show(self, ctx):
        """Shows the routing configuration for every mutual server."""
        mutuals = self._get_mutual_guilds(ctx.author.id)
        if not mutuals:
            embed = _make_embed("ℹ️ No Mutual Servers", "No mutual servers were found.", COLOR_INFO)
            return await ctx.send(embed=embed)

        require_role = self.bot.config.get("msrouter_require_role")
        if require_role is None:
            require_role = True

        embeds = []
        for guild in mutuals:
            route = self._get_route_data(guild.id)
            has_role = bool(route["role_id"])
            has_cat  = bool(route["category_id"])
            has_lbl  = bool(route["label"])
            fully_ok = has_role or not require_role

            embed = discord.Embed(
                title=f"🗺 Route — {guild.name}",
                color=self.bot.main_color if fully_ok else COLOR_ERROR,
            )
            embed.add_field(name="Guild ID",  value=f"`{guild.id}`",            inline=True)
            embed.add_field(name="Members",   value=f"`{guild.member_count:,}`", inline=True)
            embed.add_field(name="\u200b",    value="\u200b",                    inline=True)

            embed.add_field(
                name=f"{_bool_icon(has_lbl)} Display Label",
                value=route["label"] or "*using server name*",
                inline=True,
            )
            embed.add_field(
                name=f"{_bool_icon(has_role)} Staff Role",
                value=f"<@&{route['role_id']}>" if route["role_id"] else "❌ **Not set**",
                inline=True,
            )
            embed.add_field(
                name=f"{_bool_icon(has_cat)} Category",
                value=f"<#{route['category_id']}>" if route["category_id"] else "*main category*",
                inline=True,
            )

            footer = PLUGIN_FOOTER
            if not has_role and require_role:
                footer += " • ⚠️ Role required but not set — users will be blocked"
            embed.set_footer(text=footer)
            embeds.append(embed)

        if len(embeds) == 1:
            return await ctx.send(embed=embeds[0])

        await EmbedPaginatorSession(ctx, *embeds).run()

    @msrouter.command(name="refresh")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def msrouter_refresh(self, ctx):
        """Re-registers help entries and syncs config defaults."""
        self._register_help_text()
        await self.bot.config.update()
        embed = _make_embed(
            "🔄 Config Refreshed",
            "All help entries have been re-registered and config defaults synced.",
            COLOR_SUCCESS,
        )
        await ctx.send(embed=embed)

    @msrouter.command(name="clear")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def msrouter_clear(self, ctx, kind: str.lower, guild_id: int = None):
        """
        Clear a route mapping.

        Usage:
        - `.msrouter clear role <guild_id>`
        - `.msrouter clear category <guild_id>`
        - `.msrouter clear label <guild_id>`
        - `.msrouter clear all`
        """
        kind = kind.lower()

        if kind == "all":
            self.bot.config["msrouter_role"]     = {}
            self.bot.config["msrouter_category"] = {}
            self.bot.config["msrouter_label"]    = {}
            await self.bot.config.update()
            self._save_local_config()
            embed = _make_embed(
                "🗑 All Routes Cleared",
                "Every role, category, and label mapping has been removed.",
                COLOR_SUCCESS,
            )
            return await ctx.send(embed=embed)

        if guild_id is None:
            return await ctx.send_help(ctx.command)

        if kind not in {"role", "category", "label"}:
            return await ctx.send_help(ctx.command)

        key = f"msrouter_{kind}"
        mapping = self._mapping(key)
        was_set = str(guild_id) in mapping
        mapping.pop(str(guild_id), None)
        await self.bot.config.set(key, mapping, convert=False)
        await self.bot.config.update()
        self._save_local_config()

        if was_set:
            embed = _make_embed(
                f"🗑 {kind.capitalize()} Mapping Cleared",
                f"Removed the **{kind}** mapping for guild `{guild_id}`.",
                COLOR_SUCCESS,
            )
        else:
            embed = _make_embed(
                "ℹ️ Nothing to Clear",
                f"No **{kind}** mapping was set for guild `{guild_id}`.",
                COLOR_INFO,
            )

        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        if self.bot.modmail_guild and guild.id == self.bot.modmail_guild.id:
            return
        self._register_help_text()


async def setup(bot):
    await bot.add_cog(MultiServerRouter(bot))