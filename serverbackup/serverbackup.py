import asyncio
import io
import logging
import zipfile
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands


BACKUP_USER_ID = 1148212880722890783
PART_SIZE = 20 * 1024 * 1024
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
EXCLUDED_FILES = {
    ".DS_Store",
}
MAX_FILES = 10000


class ServerBackup(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    def _should_include(self, path, root):
        try:
            relative = path.relative_to(root)
        except ValueError:
            return False

        for part in relative.parts[:-1]:
            if part in EXCLUDED_DIRS:
                return False

        if path.name in EXCLUDED_FILES:
            return False

        if path.suffix.lower() in {".pyc", ".pyo"}:
            return False

        if path.name.startswith("modmail-backup-"):
            return False

        return True

    def _create_backup(self):
        root = Path.cwd().resolve()
        memory_file = io.BytesIO()
        files = []

        for path in root.rglob("*"):
            if not path.is_file():
                continue

            if not self._should_include(path, root):
                continue

            files.append(path)

            if len(files) >= MAX_FILES:
                break

        if not files:
            raise RuntimeError("No files were found to backup.")

        with zipfile.ZipFile(
            memory_file,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            for file_path in files:
                try:
                    archive.write(
                        file_path,
                        arcname=file_path.relative_to(root),
                    )
                except (OSError, PermissionError):
                    continue

        memory_file.seek(0)
        return memory_file

    async def _get_backup_user(self):
        user = self.bot.get_user(BACKUP_USER_ID)

        if user is None:
            user = await self.bot.fetch_user(BACKUP_USER_ID)

        return user

    async def _send_backup(self):

        user = None
        memory_file = None

        try:
            user = await self._get_backup_user()

            await user.send(
                "📦 **Creating bot backup...**\n"
                "The backup is being generated entirely in memory."
            )

            memory_file = await asyncio.to_thread(
                self._create_backup
            )

            data = memory_file.getvalue()
            total_size = len(data)

            parts = [
                data[i:i + PART_SIZE]
                for i in range(0, total_size, PART_SIZE)
            ]

            timestamp = datetime.now().strftime(
                "%Y-%m-%d_%H-%M-%S"
            )

            await user.send(
                f"📦 **Backup ready**\n"
                f"Size: `{total_size / 1024 / 1024:.2f} MB`\n"
                f"Parts: **{len(parts)}**"
            )

            for index, part in enumerate(parts, start=1):

                file = discord.File(
                    io.BytesIO(part),
                    filename=(
                        f"modmail-backup-{timestamp}"
                        f"-part-{index:03d}.bin"
                    ),
                )

                await user.send(
                    content=f"📦 Backup part **{index}/{len(parts)}**",
                    file=file,
                )

            await user.send(
                "✅ **Backup complete.**\n"
                "Nothing was saved to the server filesystem."
            )

            del parts
            del data

        except discord.HTTPException as error:

            logging.error(
                f"[Backup] Discord error while sending backup: {error}"
            )

            if user:
                try:
                    await user.send(
                        f"❌ **Backup failed.**\n`{error}`"
                    )
                except Exception:
                    pass

        except Exception as error:

            logging.exception(
                f"[Backup] Backup failed: {error}"
            )

            if user:
                try:
                    await user.send(
                        f"❌ **Backup failed.**\n`{error}`"
                    )
                except Exception:
                    pass

        finally:

            if memory_file:
                memory_file.close()

            logging.info(
                "[Backup] In-memory backup released."
            )

    @commands.command(name="backup")
    @commands.is_owner()
    async def backup(self, ctx):

        await ctx.send(
            "📦 Creating backup..."
        )

        asyncio.create_task(
            self._send_backup()
        )


async def setup(bot):
    await bot.add_cog(ServerBackup(bot))
