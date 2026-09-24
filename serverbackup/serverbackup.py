from __future__ import annotations

import asyncio
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands, tasks


BACKUP_USER_ID = 1148212880722890783
BACKUP_INTERVAL_HOURS = None
MAX_FILES = 10000
COMPRESSION = zipfile.ZIP_DEFLATED

SKIP_DIRECTORIES = {
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "backups",
}

SKIP_FILES = {
    ".DS_Store",
}

SKIP_SUFFIXES = {
    ".pyc",
    ".pyo",
}


class Backup(commands.Cog):
    """Backup the Modmail bot's files into a ZIP and DM it."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.backup_lock = asyncio.Lock()

        if BACKUP_INTERVAL_HOURS:
            self.automatic_backup.start()

    async def cog_unload(self):
        if self.automatic_backup.is_running():
            self.automatic_backup.cancel()


    def _get_bot_root(self) -> Path:
        """
        Get the bot's working directory.

        ModmailDev normally runs with the bot project as
        the working directory.
        """

        root = Path.cwd().resolve()

        if not root.exists():
            raise RuntimeError(
                f"Bot directory does not exist: {root}"
            )

        return root


    async def _get_backup_user(self) -> discord.User:
        """
        Get the Discord user who receives backups.
        """

        user = self.bot.get_user(BACKUP_USER_ID)

        if user is not None:
            return user

        try:
            user = await self.bot.fetch_user(
                BACKUP_USER_ID
            )
        except discord.HTTPException as exc:
            raise RuntimeError(
                f"Could not find backup user: {exc}"
            )

        return user



    def _should_skip(
        self,
        path: Path,
        root: Path,
    ) -> bool:
        """
        Determine whether a file should be excluded.
        """

        relative = path.relative_to(root)

        # Skip excluded directories.
        for part in relative.parts[:-1]:
            if part in SKIP_DIRECTORIES:
                return True

        # Skip excluded filenames.
        if path.name in SKIP_FILES:
            return True

        # Skip excluded extensions.
        if path.suffix.lower() in SKIP_SUFFIXES:
            return True

        # Never include a backup ZIP.
        if path.name.startswith("modmail-backup-"):
            return True

        return False



    def _create_backup(
        self,
        root: Path,
    ) -> tuple[Path, int, int]:
        """
        Create the backup ZIP.

        Returns:
            (zip_path, file_count, total_bytes)
        """

        timestamp = datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%d_%H-%M-%S")

        temp_dir = Path(
            tempfile.mkdtemp(
                prefix="modmail-backup-"
            )
        )

        zip_path = (
            temp_dir
            / f"modmail-backup-{timestamp}.zip"
        )

        file_count = 0
        total_bytes = 0

        with zipfile.ZipFile(
            zip_path,
            mode="w",
            compression=COMPRESSION,
            compresslevel=6,
        ) as archive:

            for path in root.rglob("*"):

                if not path.is_file():
                    continue

                if self._should_skip(
                    path,
                    root,
                ):
                    continue

                # Do not accidentally include the ZIP itself.
                if path.resolve() == zip_path.resolve():
                    continue

                if file_count >= MAX_FILES:
                    raise RuntimeError(
                        f"Backup contains more than "
                        f"{MAX_FILES} files."
                    )

                try:
                    relative_path = path.relative_to(root)

                    archive.write(
                        path,
                        arcname=str(relative_path),
                    )

                    file_count += 1

                    try:
                        total_bytes += path.stat().st_size
                    except OSError:
                        pass

                except (
                    PermissionError,
                    OSError,
                ):
                    # A file disappearing while the backup is
                    # running shouldn't necessarily kill the
                    # entire backup.
                    continue

        return (
            zip_path,
            file_count,
            total_bytes,
        )



    def _format_size(
        self,
        size: int,
    ) -> str:

        units = (
            "B",
            "KB",
            "MB",
            "GB",
            "TB",
        )

        value = float(size)

        for unit in units:

            if value < 1024:
                return f"{value:.2f} {unit}"

            value /= 1024

        return f"{value:.2f} PB"


    async def _send_backup(
        self,
        *,
        automatic: bool = False,
    ):

        # Prevent two backups running at once.
        if self.backup_lock.locked():
            return

        async with self.backup_lock:

            zip_path: Optional[Path] = None

            try:

                root = self._get_bot_root()

                user = await self._get_backup_user()

                (
                    zip_path,
                    file_count,
                    uncompressed_size,
                ) = await asyncio.to_thread(
                    self._create_backup,
                    root,
                )

                zip_size = zip_path.stat().st_size

                timestamp = datetime.now(
                    timezone.utc
                ).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                )

                embed = discord.Embed(
                    title="📦 Modmail Bot Backup",
                    description=(
                        "A backup of the bot files "
                        "has been created successfully."
                    ),
                    colour=discord.Colour.green(),
                )

                embed.add_field(
                    name="📁 Files",
                    value=str(file_count),
                    inline=True,
                )

                embed.add_field(
                    name="📦 ZIP Size",
                    value=self._format_size(
                        zip_size
                    ),
                    inline=True,
                )

                embed.add_field(
                    name="💾 Original Size",
                    value=self._format_size(
                        uncompressed_size
                    ),
                    inline=True,
                )

                embed.add_field(
                    name="🕐 Created",
                    value=timestamp,
                    inline=False,
                )

                embed.add_field(
                    name="⚙️ Type",
                    value=(
                        "Automatic"
                        if automatic
                        else "Manual"
                    ),
                    inline=True,
                )

                embed.set_footer(
                    text="ModmailDev Backup System"
                )


                await user.send(
                    embed=embed,
                    file=discord.File(
                        str(zip_path),
                        filename=zip_path.name,
                    ),
                )

                print(
                    "[Backup] Backup successfully sent "
                    f"to Discord user {BACKUP_USER_ID}."
                )

            except discord.HTTPException as exc:

                print(
                    "[Backup] Discord error while "
                    f"sending backup: {exc}"
                )

            except Exception as exc:

                print(
                    "[Backup] Backup failed: "
                    f"{type(exc).__name__}: {exc}"
                )

                try:

                    user = await self._get_backup_user()

                    await user.send(
                        "❌ **Modmail backup failed.**\n\n"
                        f"`{type(exc).__name__}: "
                        f"{exc}`"
                    )

                except Exception:
                    pass

            finally:


                if zip_path is not None:

                    try:

                        if zip_path.exists():
                            zip_path.unlink()

                        parent = zip_path.parent

                        if parent.exists():
                            parent.rmdir()

                    except Exception:
                        pass

    @commands.command(
        name="backup",
        aliases=[
            "botbackup",
            "createbackup",
        ],
    )
    @commands.is_owner()
    async def backup(
        self,
        ctx: commands.Context,
    ):
        """
        Manually create a full bot backup.

        Only the bot owner can run this.
        """

        if self.backup_lock.locked():

            await ctx.send(
                "⏳ A backup is already being created."
            )
            return

        message = await ctx.send(
            "📦 Creating bot backup...\n"
            "This may take a little while."
        )

        await self._send_backup(
            automatic=False
        )

        try:
            await message.edit(
                content=(
                    "✅ Backup process finished.\n"
                    "Check your DMs for the backup ZIP."
                )
            )

        except discord.HTTPException:
            pass


    @tasks.loop(
        hours=BACKUP_INTERVAL_HOURS
        if BACKUP_INTERVAL_HOURS
        else 24
    )
    async def automatic_backup(self):

        await self.bot.wait_until_ready()

        await self._send_backup(
            automatic=True
        )

    @automatic_backup.before_loop
    async def before_automatic_backup(
        self,
    ):

        await self.bot.wait_until_ready()


    @commands.Cog.listener()
    async def on_ready(self):

        # Only run the startup message once.
        if getattr(
            self,
            "_startup_logged",
            False,
        ):
            return

        self._startup_logged = True

        print(
            "[Backup] Backup system loaded."
        )

        print(
            f"[Backup] Backup recipient: "
            f"{BACKUP_USER_ID}"
        )

        if BACKUP_INTERVAL_HOURS:
            print(
                f"[Backup] Automatic backups: "
                f"every {BACKUP_INTERVAL_HOURS} hours"
            )
        else:
            print(
                "[Backup] Automatic backups disabled."
            )


async def setup(
    bot: commands.Bot,
):
    await bot.add_cog(
        Backup(bot)
    )
