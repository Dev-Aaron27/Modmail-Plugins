import discord
from discord.ext import commands


SETTINGS_ID = "settings"


class BreakMessageModal(discord.ui.Modal, title="Set the ModMail message"):
    message = discord.ui.TextInput(
        label="Message sent when someone opens ModMail",
        style=discord.TextStyle.paragraph,
        placeholder="Enter the message users should receive...",
        required=True,
        min_length=1,
        max_length=4000,
    )

    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.coll.update_one(
            {"_id": SETTINGS_ID},
            {"$set": {"message": self.message.value}},
            upsert=True,
        )
        embed = discord.Embed(
            title="Message saved",
            description="The ModMail opening message has been updated.",
            colour=discord.Colour.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class BreakSetupView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    @discord.ui.button(
        label="Click me to setup your message",
        style=discord.ButtonStyle.primary,
        emoji="✏️",
    )
    async def setup_message(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(BreakMessageModal(self.cog))


class BreakPlugin(commands.Cog):
    """Configurable first-response message for new ModMail threads."""

    def __init__(self, bot):
        self.bot = bot
        # ModMail plugins should use their own database partition.
        self.coll = bot.api.get_plugin_partition(self)

    async def get_settings(self):
        settings = await self.coll.find_one({"_id": SETTINGS_ID})
        if settings is None:
            return {"_id": SETTINGS_ID, "enabled": False, "message": ""}
        return settings

    @commands.group(name="break", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def break_command(self, ctx):
        """Manage the ModMail opening message."""
        await ctx.send_help(ctx.command)

    @break_command.command(name="msg")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def break_msg(self, ctx):
        """Open the button and modal used to configure the message."""
        embed = discord.Embed(
            title="Configure the ModMail message",
            description=(
                "Click the button below to write the message that users will receive "
                "when they open a new ModMail conversation."
            ),
            colour=discord.Colour.blurple(),
        )
        await ctx.send(embed=embed, view=BreakSetupView(self))

    @break_command.command(name="enable")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def break_enable(self, ctx):
        """Enable the automatic opening message."""
        settings = await self.get_settings()
        if not settings.get("message", "").strip():
            return await ctx.send(
                embed=discord.Embed(
                    title="Message not configured",
                    description="Run `!break msg` first to create your opening message.",
                    colour=discord.Colour.orange(),
                )
            )

        await self.coll.update_one(
            {"_id": SETTINGS_ID}, {"$set": {"enabled": True}}, upsert=True
        )
        await ctx.send(
            embed=discord.Embed(
                title="Break message enabled",
                description="New ModMail conversations will now receive the configured message.",
                colour=discord.Colour.green(),
            )
        )

    @break_command.command(name="disable", aliases=["dissable"])
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def break_disable(self, ctx):
        """Disable the automatic opening message."""
        await self.coll.update_one(
            {"_id": SETTINGS_ID}, {"$set": {"enabled": False}}, upsert=True
        )
        await ctx.send(
            embed=discord.Embed(
                title="Break message disabled",
                description="The automatic opening message will no longer be sent. ModMail will continue working normally.",
                colour=discord.Colour.orange(),
            )
        )

    @break_command.command(name="view")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def break_view(self, ctx):
        """Show the current configuration."""
        settings = await self.get_settings()
        message = settings.get("message", "").strip()
        embed = discord.Embed(
            title="Break message overview",
            colour=discord.Colour.blurple(),
        )
        embed.add_field(
            name="Status",
            value="🟢 Enabled" if settings.get("enabled", False) else "🔴 Disabled",
            inline=True,
        )
        embed.add_field(
            name="Message configured",
            value="Yes" if message else "No",
            inline=True,
        )
        embed.add_field(
            name="Current message",
            value=message[:1024] if message else "*No message has been set. Run `!break msg` to configure one.*",
            inline=False,
        )
        embed.set_footer(text="Only new ModMail conversations are affected.")
        await ctx.send(embed=embed)


    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        """Send the configured opening message to the thread recipient."""
        settings = await self.get_settings()
        message = settings.get("message", "").strip()

        if not settings.get("enabled", False) or not message:
            return

        # The recipient is the user who opened the conversation.
        recipient = thread.recipient

        if recipient is None:
            return

        embed = discord.Embed(
            description=message,
            colour=discord.Colour.blurple(),
        )

        try:
            await recipient.send(embed=embed)
        except discord.Forbidden:
            self.bot.logger.warning(
                "Break message could not be sent: user has DMs disabled."
            )
        except discord.HTTPException:
            self.bot.logger.exception(
                "Failed to send the Break opening message."
            )



async def setup(bot):
    await bot.add_cog(BreakPlugin(bot))
