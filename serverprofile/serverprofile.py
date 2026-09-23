

from __future__ import annotations

import base64
import io
import re
from typing import Optional

import aiohttp
import discord
from discord.ext import commands


API_VERSION = 10
DISCORD_API = f"https://discord.com/api/v{API_VERSION}"

# Discord currently documents these common image formats for profile images.
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
    """Per-server profile controls for the Modmail bot."""

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

    def _token(self) -> str:
        token = getattr(getattr(self.bot, "http", None), "token", None)
        if not token:
            token = getattr(self.bot, "token", None)
        if not token:
            raise RuntimeError("Could not obtain the bot token from Modmail.")
        return token

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
        """PATCH /guilds/{guild.id}/members/@me."""

        payload = {}

        if nick is not None:
            payload["nick"] = nick

        if avatar is not None or reset_avatar:
            payload["avatar"] = avatar

        if banner is not None or reset_banner:
            payload["banner"] = banner

        if bio is not None or reset_bio:
            payload["bio"] = bio

        if not payload:
            raise ValueError("Nothing to change.")

        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            )

        headers = {
            "Authorization": f"Bot {self._token()}",
            "Content-Type": "application/json",
            "User-Agent": "ModmailDev-ServerProfile/1.0",
        }

        url = f"{DISCORD_API}/guilds/{guild.id}/members/@me"

        async with self.session.patch(url, json=payload, headers=headers) as response:
            body = await response.text()

            if response.status >= 400:
                raise discord.HTTPException(
                    response,
                    body,
                )

            if not body:
                return None

            try:
                import json
                return json.loads(body)
            except Exception:
                return body

    async def _image_to_data_uri(self, url: str) -> str:
        """Download an image and turn it into a Discord data URI."""
        if not re.match(r"^https?://", url, re.I):
            raise ValueError("The image must be a valid http:// or https:// URL.")

        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20)
            )

        headers = {
            "User-Agent": "ModmailDev-ServerProfile/1.0",
            "Accept": "image/*",
        }

        async with self.session.get(url, headers=headers, allow_redirects=True) as response:
            if response.status != 200:
                raise ValueError(f"Discord could not download that image (HTTP {response.status}).")

            content_type = response.headers.get("Content-Type", "").split(";")[0].lower()
            extension = IMAGE_TYPES.get(content_type)

            if extension is None:
                # Some hosts return octet-stream. Infer from the URL in that case.
                lower_url = str(response.url).lower().split("?", 1)[0]
                if lower_url.endswith(".png"):
                    content_type, extension = "image/png", "png"
                elif lower_url.endswith((".jpg", ".jpeg")):
                    content_type, extension = "image/jpeg", "jpeg"
                elif lower_url.endswith(".gif"):
                    content_type, extension = "image/gif", "gif"
                elif lower_url.endswith(".webp"):
                    content_type, extension = "image/webp", "webp"
                else:
                    raise ValueError(
                        "That URL does not appear to point to a supported image "
                        "(PNG, JPG, GIF or WEBP)."
                    )

            data = await response.read(MAX_IMAGE_BYTES + 1)

            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError("The image is larger than the 10 MB upload limit.")

            encoded = base64.b64encode(data).decode("ascii")
            return f"data:{content_type};base64,{encoded}"

    def _admin_check(self, ctx: commands.Context) -> bool:
        return (
            ctx.guild is not None
            and isinstance(ctx.author, discord.Member)
            and ctx.author.guild_permissions.administrator
        )

    async def _deny_if_needed(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            await ctx.send("❌ This command can only be used inside a server.")
            return True

        if not self._admin_check(ctx):
            await ctx.send("❌ You need the **Administrator** permission to manage the bot's server profile.")
            return True

        return False

    def _error_text(self, exc: Exception) -> str:
        if isinstance(exc, discord.HTTPException):
            if exc.status == 400:
                return "Discord rejected one of the supplied values. Check the image format/size and try again."
            if exc.status == 403:
                return "Discord denied the change. Make sure the bot is allowed to change its own server profile."
            if exc.status == 429:
                return "Discord rate-limited the bot. Please wait a moment and try again."
            return f"Discord returned HTTP {exc.status}."

        return str(exc)

    @commands.command(name="botprofile", aliases=["serverprofile", "bot-profile"])
    async def botprofile(self, ctx: commands.Context):
        """Open the server-specific bot profile control panel."""
        if await self._deny_if_needed(ctx):
            return

        embed = discord.Embed(
            title="🤖 Bot Server Profile",
            description=(
                "Change how the bot appears **in this server only**.\n\n"
                "Use the commands below:"
            ),
            colour=discord.Colour.blurple(),
        )

        embed.add_field(
            name="✏️ Name",
            value=f"`{ctx.clean_prefix}botprofile name <name>`",
            inline=False,
        )
        embed.add_field(
            name="🖼️ Avatar",
            value=f"`{ctx.clean_prefix}botprofile avatar <image-url>`",
            inline=False,
        )
        embed.add_field(
            name="🎨 Banner",
            value=f"`{ctx.clean_prefix}botprofile banner <image-url>`",
            inline=False,
        )
        embed.add_field(
            name="📝 Bio",
            value=f"`{ctx.clean_prefix}botprofile bio <text>`",
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
        embed.set_footer(text="Administrator only • Changes are per-server")

        await ctx.send(embed=embed)

    @botprofile.command(name="name")
    async def profile_name(self, ctx: commands.Context, *, name: str):
        if await self._deny_if_needed(ctx):
            return

        name = name.strip()
        if not name:
            await ctx.send("❌ The name cannot be empty.")
            return
        if len(name) > MAX_NICKNAME_LENGTH:
            await ctx.send(f"❌ The server profile name can be at most {MAX_NICKNAME_LENGTH} characters.")
            return

        try:
            await self._modify_current_member(ctx.guild, nick=name)
            await ctx.send(f"✅ The bot's server profile name is now **{discord.utils.escape_markdown(name)}**.")
        except Exception as exc:
            await ctx.send(f"❌ {self._error_text(exc)}")

    @botprofile.command(name="avatar")
    async def profile_avatar(self, ctx: commands.Context, url: str):
        if await self._deny_if_needed(ctx):
            return

        try:
            data_uri = await self._image_to_data_uri(url)
            await self._modify_current_member(ctx.guild, avatar=data_uri)
            await ctx.send("✅ The bot's **server avatar** has been changed.")
        except Exception as exc:
            await ctx.send(f"❌ {self._error_text(exc)}")

    @botprofile.command(name="banner")
    async def profile_banner(self, ctx: commands.Context, url: str):
        if await self._deny_if_needed(ctx):
            return

        try:
            data_uri = await self._image_to_data_uri(url)
            await self._modify_current_member(ctx.guild, banner=data_uri)
            await ctx.send("✅ The bot's **server banner** has been changed.")
        except Exception as exc:
            await ctx.send(f"❌ {self._error_text(exc)}")

    @botprofile.command(name="bio")
    async def profile_bio(self, ctx: commands.Context, *, bio: str):
        if await self._deny_if_needed(ctx):
            return

        bio = bio.strip()
        if len(bio) > MAX_BIO_LENGTH:
            await ctx.send(f"❌ The bio can be at most {MAX_BIO_LENGTH} characters.")
            return

        try:
            await self._modify_current_member(ctx.guild, bio=bio)
            await ctx.send("✅ The bot's **server bio** has been changed.")
        except Exception as exc:
            await ctx.send(f"❌ {self._error_text(exc)}")

    @botprofile.group(name="reset", invoke_without_command=False)
    async def profile_reset(self, ctx: commands.Context):
        if await self._deny_if_needed(ctx):
            return

    @profile_reset.command(name="avatar")
    async def reset_avatar(self, ctx: commands.Context):
        if await self._deny_if_needed(ctx):
            return
        try:
            await self._modify_current_member(ctx.guild, reset_avatar=True)
            await ctx.send("✅ The server avatar has been reset.")
        except Exception as exc:
            await ctx.send(f"❌ {self._error_text(exc)}")

    @profile_reset.command(name="banner")
    async def reset_banner(self, ctx: commands.Context):
        if await self._deny_if_needed(ctx):
            return
        try:
            await self._modify_current_member(ctx.guild, reset_banner=True)
            await ctx.send("✅ The server banner has been reset.")
        except Exception as exc:
            await ctx.send(f"❌ {self._error_text(exc)}")

    @profile_reset.command(name="bio")
    async def reset_bio(self, ctx: commands.Context):
        if await self._deny_if_needed(ctx):
            return
        try:
            await self._modify_current_member(ctx.guild, reset_bio=True)
            await ctx.send("✅ The server bio has been reset.")
        except Exception as exc:
            await ctx.send(f"❌ {self._error_text(exc)}")

    @profile_reset.command(name="all")
    async def reset_all(self, ctx: commands.Context):
        if await self._deny_if_needed(ctx):
            return
        try:
            await self._modify_current_member(
                ctx.guild,
                nick=ctx.guild.me.name if ctx.guild.me else self.bot.user.name,
                reset_avatar=True,
                reset_banner=True,
                reset_bio=True,
            )
            await ctx.send("✅ The bot's server-specific avatar, banner and bio have been reset.")
        except Exception as exc:
            await ctx.send(f"❌ {self._error_text(exc)}")


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerProfile(bot))
