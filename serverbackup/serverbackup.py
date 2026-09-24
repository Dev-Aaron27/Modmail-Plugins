```python
import asyncio
import logging
import math
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands


# ============================================================
# CONFIG
# ============================================================

# Discord user who receives the backups
BACKUP_USER_ID = 1148212880722890783

# Maximum size of each Discord attachment.
# 20 MB gives plenty of room below Discord's upload limit.
PART_SIZE = 20 * 1024 * 1024

# Directories that should NOT be included
EXCLUDED_DIRS = {
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

# Files that should NOT be included
EXCLUDED_FILES = {
    ".DS_Store",
}

MAX_FILES = 10000



class ServerBackup(commands.Cog):
    """
    Manual bot backup system.

    Usage:
        .backup

    The backup is:
        1. Created temporarily.
        2. Split into 20 MB parts.
        3. Sent to the configured Discord user.
        4. Completely deleted from the server.
    """

    def __init__(self, bot):
        self.bot = bot

        logging.info(
            "[Backup] Server backup plugin loaded "
            "(manual backups only)"
        )



    def _get_bot_root(self):
        """
        Uses the current working directory as the bot root.

        This is normally the directory the ModmailDev process
        was started from.
        """
        return Path.cwd().resolve()



    def _should_include(self, path: Path, root: Path):
        try:
            relative = path.relative_to(root)
        except ValueError:
            return False

        # Skip excluded directories
        for part in relative.parts[:-1]:
            if part in EXCLUDED_DIRS:
                return False

        # Skip excluded files
        if path.name in EXCLUDED_FILES:
            return False

        # Skip Python bytecode
        if path.suffix.lower() in {".pyc", ".pyo"}:
            return False

        # Skip previous backup files
        if path.name.startswith("modmail-backup-"):
            return False

        return True


    def _create_backup(self, output_zip: Path):
        root = self._get_bot_root()

        logging.info(f"[Backup] Creating backup from: {root}")

        files = []

        for path in root.rglob("*"):
            if not path.is_file():
                continue

            if not self._should_include(path, root):
                continue

            files.append(path)

            if len(files) >= MAX_FILES:
                logging.warning(
                    f"[Backup] Reached maximum file limit: {MAX_FILES}"
                )
                break

        if not files:
            raise RuntimeError("No files were found to backup.")

        logging.info(
            f"[Backup] Adding {len(files)} files to backup..."
        )

        with zipfile.ZipFile(
            output_zip,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:

            for file_path in files:
                try:
                    relative_path = file_path.relative_to(root)

                    archive.write(
                        file_path,
                        arcname=relative_path,
                    )

                except (OSError, PermissionError) as error:
                    logging.warning(
                        f"[Backup] Could not add {file_path}: {error}"
                    )

        size = output_zip.stat().st_size

        logging.info(
            f"[Backup] ZIP created: "
            f"{size / 1024 / 1024:.2f} MB"
        )

        return size


    def _split_file(self, source: Path, output_dir: Path):
        """
        Splits the completed ZIP into smaller files.

        Each part is independently uploaded to Discord.
        """

        parts = []

        total_size = source.stat().st_size

        if total_size <= PART_SIZE:
            parts.append(source)
            return parts

        logging.info(
            f"[Backup] ZIP is too large "
            f"({total_size / 1024 / 1024:.2f} MB). "
            f"Splitting..."
        )

        with source.open("rb") as source_file:

            part_number = 1

            while True:
                chunk = source_file.read(PART_SIZE)

                if not chunk:
                    break

                part_path = (
                    output_dir
                    / f"modmail-backup-part-{part_number:03d}.zip"
                )

                with part_path.open("wb") as part_file:
                    part_file.write(chunk)

                parts.append(part_path)

                logging.info(
                    f"[Backup] Created part "
                    f"{part_number}: "
                    f"{part_path.stat().st_size / 1024 / 1024:.2f} MB"
                )

                part_number += 1

        return parts


    async def _get_backup_user(self):
        try:
            user = self.bot.get_user(BACKUP_USER_ID)

            if user is None:
                user = await self.bot.fetch_user(BACKUP_USER_ID)

            return user

        except Exception as error:
            logging.error(
                f"[Backup] Could not find backup user: {error}"
            )
            return None



    async def _send_backup(self):

        temp_dir = Path(
            tempfile.mkdtemp(prefix="modmail-backup-")
        )

        zip_path = temp_dir / "modmail-backup.zip"

        try:



            logging.info("[Backup] Starting manual backup...")

            await asyncio.to_thread(
                self._create_backup,
                zip_path,
            )



            parts = await asyncio.to_thread(
                self._split_file,
                zip_path,
                temp_dir,
            )

            total_parts = len(parts)



            user = await self._get_backup_user()

            if user is None:
                raise RuntimeError(
                    "Could not find the backup Discord user."
                )



            await user.send(
                f"📦 **Modmail backup created**\n"
                f"Backup date: <t:{int(datetime.now().timestamp())}:F>\n"
                f"Parts: **{total_parts}**\n\n"
                f"The backup files below are all parts of the same ZIP."
            )



            for index, part in enumerate(parts, start=1):

                file_size = part.stat().st_size

                logging.info(
                    f"[Backup] Sending part "
                    f"{index}/{total_parts} "
                    f"({file_size / 1024 / 1024:.2f} MB)"
                )

                await user.send(
                    content=(
                        f"📦 Backup part **{index}/{total_parts}**"
                    ),
                    file=discord.File(
                        str(part),
                        filename=(
                            f"modmail-backup-"
                            f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
                            f"-part-{index}.zip"
                        ),
                    ),
                )



            await user.send(
                "✅ **Backup upload complete.**\n"
                "All temporary backup files have been deleted "
                "from the server."
            )

            logging.info(
                f"[Backup] Successfully sent "
                f"{total_parts} backup part(s)."
            )

        except discord.HTTPException as error:

            logging.error(
                f"[Backup] Discord error while sending backup: {error}"
            )

            try:
                await user.send(
                    f"❌ **Backup upload failed.**\n"
                    f"Discord returned: `{error}`"
                )
            except Exception:
                pass

        except Exception as error:

            logging.exception(
                f"[Backup] Backup failed: {error}"
            )

            try:
                user = await self._get_backup_user()

                if user:
                    await user.send(
                        f"❌ **Backup failed.**\n"
                        f"`{error}`"
                    )

            except Exception:
                pass

        finally:



            try:
                shutil.rmtree(temp_dir, ignore_errors=True)

                logging.info(
                    "[Backup] Temporary backup files deleted."
                )

            except Exception as error:
                logging.error(
                    f"[Backup] Could not clean temporary files: {error}"
                )



    @commands.command(name="backup")
    @commands.is_owner()
    async def backup(self, ctx):
        """
        Manually create a complete bot backup.

        Usage:
            .backup
        """

        await ctx.send(
            "📦 Creating bot backup...\n"
            "I'll DM the backup parts when it's ready."
        )

        # Don't block the command handler
        asyncio.create_task(self._send_backup())


async def setup(bot):
    await bot.add_cog(ServerBackup(bot))
