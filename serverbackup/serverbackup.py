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
    ".cache",
    ".trash",
    "venv",
    "env",
    "logs",
    "tmp",
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

    def _get_files(self):
        root = Path.cwd().resolve()
        files = []

        for path in root.rglob("*"):
            if not path.is_file():
                continue

            if not self._should_include(path, root):
                continue

            files.append(path)

            if len(files) >= MAX_FILES:
                break

        return root, files

    def _create_zip_parts(self):
        root, files = self._get_files()

        if not files:
            raise RuntimeError("No files were found to backup.")

        parts = []
        current_zip = io.BytesIO()
        current_archive = zipfile.ZipFile(
            current_zip,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        )

        for file_path in files:
            try:
                relative_path = file_path.relative_to(root)

                with file_path.open("rb") as source:
                    file_data = source.read()

                test_zip = io.BytesIO()
                with zipfile.ZipFile(
                    test_zip,
                    "w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
                ) as test_archive:
                    with current_archive.open(
                        relative_path,
                        "w",
                    ) as destination:
                        destination.write(file_data)

                    current_archive.close()

                    test_zip.write(
                        current_zip.getvalue()
                    )

                current_size = len(current_zip.getvalue())

                if current_size > PART_SIZE:
                    current_zip = io.BytesIO()
                    current_archive = zipfile.ZipFile(
                        current_zip,
                        "w",
                        compression=zipfile.ZIP_DEFLATED,
                        compresslevel=6,
                    )

                    current_archive.writestr(
                        relative_path,
                        file_data,
                    )

            except (OSError, PermissionError):
                continue

        current_archive.close()

        if current_zip.getvalue():
            parts.append(current_zip.getvalue())

        return parts

    def _create_zip_parts_safe(self):
        root, files = self._get_files()

        if not files:
            raise RuntimeError("No files were found to backup.")

        parts = []
        current_files = []
        current_size = 0

        for file_path in files:
            try:
                file_size = file_path.stat().st_size
            except OSError:
                continue

            if current_files and current_size + file_size > PART_SIZE:
                parts.append(
                    self._build_zip(root, current_files)
                )
                current_files = []
                current_size = 0

            current_files.append(file_path)
            current_size += file_size

        if current_files:
            parts.append(
                self._build_zip(root, current_files)
            )

        return parts

    def _build_zip(self, root, files):
        memory_file = io.BytesIO()

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

        return memory_file.getvalue()

    async def _get_backup_user(self):
        user = self.bot.get_user(BACKUP_USER_ID)

        if user is None:
            user = await self.bot.fetch_user(BACKUP_USER_ID)

        return user

    async def _send_backup(self):

        user = None
        parts = []

        try:
            user = await self._get_backup_user()

            await user.send(
                "📦 **Creating bot backup...**\n"
                "The backup is being generated entirely in memory."
            )

            parts = await asyncio.to_thread(
                self._create_zip_parts_safe
            )

            timestamp = datetime.now().strftime(
                "%Y-%m-%d_%H-%M-%S"
            )

            total_size = sum(len(part) for part in parts)

            await user.send(
                f"📦 **Backup ready**\n"
                f"Size: `{total_size / 1024 / 1024:.2f} MB`\n"
                f"ZIP files: **{len(parts)}**"
            )

            for index, part in enumerate(parts, start=1):

                filename = (
                    f"modmail-backup-{timestamp}"
                    f"-part-{index:03d}.zip"
                )

                await user.send(
                    content=f"📦 Backup part **{index}/{len(parts)}**",
                    file=discord.File(
                        io.BytesIO(part),
                        filename=filename,
                    ),
                )

            await user.send(
                "✅ **Backup complete.**\n"
                "Every part is a valid ZIP file and "
                "nothing was saved to the server filesystem."
            )

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
            parts.clear()

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
