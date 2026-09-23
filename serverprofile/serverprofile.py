from __future__ import annotations

import base64
import json
import re
from typing import Optional

import aiohttp
import discord
from discord.ext import commands


API_VERSION = 10
DISCORD_API = f"https://discord.com/api/v{API_VERSION}"

IMAGE_TYPES = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_BIO_LENGTH = 190
MAX_NICKNAME_LENGTH = 32


class ServerProfile(commands.Cog):
    """Per-server bot profile controls."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20)
        )

    async def cog_unload(self):
        if self.session and not self.session.closed:
            await self.session.close()

    # ---------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------

    def _token(self) -> str:
        token = getattr(getattr(self.bot, "http", None), "token", None)

        if not token:
            token = getattr(self.bot, "token", None)

        if not token:
            raise RuntimeError("Could not obtain the bot token from Modmail.")

        return token

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            )

        return self.session

    def _admin_check(self, ctx: commands.Context) -> bool:
        return (
            ctx.guild is not None
            and isinstance(ctx.author, discord.Member)
            and ctx.author.guild_permissions.administrator
        )

    async def _deny_if_needed(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            await ctx.send(
                "❌ This command can only be used inside a server."
            )
            return True

        if not self._admin_check(ctx):
            await ctx.send(
                "❌ You need the **Administrator** permission to manage "
                "the bot's server profile."
            )
            return True

        return False

    def _error_text(self, exc: Exception) -> str:
        if isinstance(exc, discord.HTTPException):
            if exc.status == 400:
                return (
                    "Discord rejected one of the supplied values. "
                    "Check the image format, size, or profile value."
                )

            if exc.status == 403:
                return (
                    "Discord denied the change. Make sure the bot is "
                    "allowed to modify its server profile."
                )

            if exc.status == 404:
                return (
                    "Discord could not find the server profile endpoint."
                )

            if exc.status == 429:
                return (
                    "Discord rate-limited the bot. "
                    "Please wait a moment and try again."
                )

            return f"Discord returned HTTP {exc.status}."

        return str(exc)

    # ---------------------------------------------------------
    # Discord API
    # ---------------------------------------------------------

    async def _modify_current_member(
        self,
        guild: discord.Guild,
        *,
        nick: Optional[str] = None,
        avatar: Optional[str] = None,
        banner: Optional[str] = None,
        bio: Optional[str] = None,
        reset_avatar: bool = False,
        reset_banner: bool = False,
        reset_bio: bool = False,
    ):
        """
        Modify the bot's server-specific profile.

        Endpoint:
        PATCH /guilds/{guild.id}/members/@me
        """

        payload = {}

        if nick is not None:
            payload["nick"] = nick

        if avatar is not None or reset_avatar:
            payload["avatar"] = None if reset_avatar else avatar

        if banner is not None or reset_banner:
            payload["banner"] = None if reset_banner else banner

        if bio is not None or reset_bio:
            payload["bio"] = None if reset_bio else bio

        if not payload:
            raise ValueError("Nothing to change.")

        session = await self._get_session()

        headers = {
            "Authorization": f"Bot {self._token()}",
            "Content-Type": "application/json",
            "User-Agent": "ModmailDev-ServerProfile/1.0",
        }

        url = (
            f"{DISCORD_API}/guilds/"
            f"{guild.id}/members/@me"
        )

        async with session.patch(
            url,
            json=payload,
            headers=headers,
        ) as response:

            body = await response.text()

            if response.status >= 400:
                raise discord.HTTPException(
                    response,
                    body,
                )

            if not body:
                return None

            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return body

    # ---------------------------------------------------------
    # Image handling
    # ---------------------------------------------------------

    async def _image_to_data_uri(self, url: str) -> str:
        """Download an image and convert it into a Discord data URI."""

        if not re.match(r"^https?://", url, re.IGNORECASE):
            raise ValueError(
                "The image must be a valid http:// or https:// URL."
            )

        session = await self._get_session()

        headers = {
            "User-Agent": "ModmailDev-ServerProfile/1.0",
            "Accept": "image/*",
        }

        async with session.get(
            url,
            headers=headers,
            allow_redirects=True,
        ) as response:

            if response.status != 200:
                raise ValueError(
                    f"Could not download that image "
                    f"(HTTP {response.status})."
                )

            content_type = (
                response.headers
                .get("Content-Type", "")
                .split(";")[0]
                .lower()
            )

            extension = IMAGE_TYPES.get(content_type)

            # Some image hosts return application/octet-stream.
            if extension is None:
                lower_url = (
                    str(response.url)
                    .lower()
                    .split("?", 1)[0]
                )

                if lower_url.endswith(".png"):
                    content_type = "image/png"

                elif lower_url.endswith((".jpg", ".jpeg")):
                    content_type = "image/jpeg"

                elif lower_url.endswith(".gif"):
                    content_type = "image/gif"

                elif lower_url.endswith(".webp"):
                    content_type = "image/webp"

                else:
                    raise ValueError(
                        "That URL does not appear to point to a "
                        "supported image. Use PNG, JPG, GIF or WEBP."
                    )

            data = await response.read(MAX_IMAGE_BYTES + 1)

            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError(
                    "The image is larger than the 10 MB upload limit."
                )

            encoded = base64.b64encode(data).decode("ascii")

            return f"data:{content_type};base64,{encoded}"

    # ---------------------------------------------------------
    # Main command
    # ---------------------------------------------------------

    @commands.command(
        name="botprofile",
        aliases=[
            "serverprofile",
            "bot-profile",
        ],
    )
    async def botprofile(
        self,
        ctx: commands.Context,
        action: Optional[str] = None,
        *,
        value: Optional[str] = None,
    ):
        """
        Manage the bot's server-specific profile.

        Usage:

        botprofile
        botprofile name <name>
        botprofile avatar <url>
        botprofile banner <url>
        botprofile bio <text>
        botprofile reset avatar
        botprofile reset banner
        botprofile reset bio
        botprofile reset all
        """

        if await self._deny_if_needed(ctx):
            return

        # -----------------------------------------------------
        # Help
        # -----------------------------------------------------

        if not action:
            embed = discord.Embed(
                title="🤖 Bot Server Profile",
                description=(
                    "Change how the bot appears **in this server only**."
                ),
                colour=discord.Colour.blurple(),
            )

            embed.add_field(
                name="✏️ Name",
                value=(
                    f"`{ctx.clean_prefix}"
                    f"botprofile name <name>`"
                ),
                inline=False,
            )

            embed.add_field(
                name="🖼️ Avatar",
                value=(
                    f"`{ctx.clean_prefix}"
                    f"botprofile avatar <image-url>`"
                ),
                inline=False,
            )

            embed.add_field(
                name="🎨 Banner",
                value=(
                    f"`{ctx.clean_prefix}"
                    f"botprofile banner <image-url>`"
                ),
                inline=False,
            )

            embed.add_field(
                name="📝 Bio",
                value=(
                    f"`{ctx.clean_prefix}"
                    f"botprofile bio <text>`"
                ),
                inline=False,
            )

            embed.add_field(
                name="♻️ Reset",
                value=(
                    f"`{ctx.clean_prefix}botprofile reset avatar`\n"
                    f"`{ctx.clean_prefix}botprofile reset banner`\n"
                    f"`{ctx.clean_prefix}botprofile reset bio`\n"
                    f"`{ctx.clean_prefix}botprofile reset all`"
                ),
                inline=False,
            )

            embed.set_footer(
                text="Administrator only • Changes are per-server"
            )

            await ctx.send(embed=embed)
            return

        action = action.lower().strip()
        value = (value or "").strip()

        # -----------------------------------------------------
        # Name
        # -----------------------------------------------------

        if action == "name":

            if not value:
                await ctx.send(
                    f"❌ Usage: `{ctx.clean_prefix}"
                    f"botprofile name <name>`"
                )
                return

            if len(value) > MAX_NICKNAME_LENGTH:
                await ctx.send(
                    "❌ The server profile name can be at most "
                    f"{MAX_NICKNAME_LENGTH} characters."
                )
                return

            try:
                await self._modify_current_member(
                    ctx.guild,
                    nick=value,
                )

                await ctx.send(
                    "✅ The bot's server profile name is now "
                    f"**{discord.utils.escape_markdown(value)}**."
                )

            except Exception as exc:
                await ctx.send(
                    f"❌ {self._error_text(exc)}"
                )

            return

        # -----------------------------------------------------
        # Avatar
        # -----------------------------------------------------

        if action == "avatar":

            if not value:
                await ctx.send(
                    f"❌ Usage: `{ctx.clean_prefix}"
                    f"botprofile avatar <image-url>`"
                )
                return

            try:
                data_uri = await self._image_to_data_uri(value)

                await self._modify_current_member(
                    ctx.guild,
                    avatar=data_uri,
                )

                await ctx.send(
                    "✅ The bot's **server avatar** has been changed."
                )

            except Exception as exc:
                await ctx.send(
                    f"❌ {self._error_text(exc)}"
                )

            return

        # -----------------------------------------------------
        # Banner
        # -----------------------------------------------------

        if action == "banner":

            if not value:
                await ctx.send(
                    f"❌ Usage: `{ctx.clean_prefix}"
                    f"botprofile banner <image-url>`"
                )
                return

            try:
                data_uri = await self._image_to_data_uri(value)

                await self._modify_current_member(
                    ctx.guild,
                    banner=data_uri,
                )

                await ctx.send(
                    "✅ The bot's **server banner** has been changed."
                )

            except Exception as exc:
                await ctx.send(
                    f"❌ {self._error_text(exc)}"
                )

            return

        # -----------------------------------------------------
        # Bio
        # -----------------------------------------------------

        if action == "bio":

            if len(value) > MAX_BIO_LENGTH:
                await ctx.send(
                    "❌ The bio can be at most "
                    f"{MAX_BIO_LENGTH} characters."
                )
                return

            try:
                await self._modify_current_member(
                    ctx.guild,
                    bio=value,
                )

                await ctx.send(
                    "✅ The bot's **server bio** has been changed."
                )

            except Exception as exc:
                await ctx.send(
                    f"❌ {self._error_text(exc)}"
                )

            return

        # -----------------------------------------------------
        # Reset
        # -----------------------------------------------------

        if action == "reset":

            target = value.lower()

            if target not in {
                "avatar",
                "banner",
                "bio",
                "all",
            }:
                await ctx.send(
                    "❌ Reset must be `avatar`, `banner`, "
                    "`bio`, or `all`."
                )
                return

            try:

                if target == "avatar":

                    await self._modify_current_member(
                        ctx.guild,
                        reset_avatar=True,
                    )

                    await ctx.send(
                        "✅ The server avatar has been reset."
                    )

                elif target == "banner":

                    await self._modify_current_member(
                        ctx.guild,
                        reset_banner=True,
                    )

                    await ctx.send(
                        "✅ The server banner has been reset."
                    )

                elif target == "bio":

                    await self._modify_current_member(
                        ctx.guild,
                        reset_bio=True,
                    )

                    await ctx.send(
                        "✅ The server bio has been reset."
                    )

                elif target == "all":

                    await self._modify_current_member(
                        ctx.guild,
                        reset_avatar=True,
                        reset_banner=True,
                        reset_bio=True,
                    )

                    await ctx.send(
                        "✅ The bot's server-specific "
                        "profile has been reset."
                    )

            except Exception as exc:
                await ctx.send(
                    f"❌ {self._error_text(exc)}"
                )

            return

        # -----------------------------------------------------
        # Unknown option
        # -----------------------------------------------------

        await ctx.send(
            f"❌ Unknown option `{discord.utils.escape_markdown(action)}`.\n"
            f"Use `{ctx.clean_prefix}botprofile` to see the "
            "available options."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerProfile(bot))
