import discord
from discord import app_commands

from database.admins import is_bot_admin


async def user_is_bot_admin(
    interaction: discord.Interaction,
) -> bool:
    if interaction.guild_id is None:
        return False

    application = await interaction.client.application_info()

    if interaction.user.id == application.owner.id:
        return True

    if (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    ):
        return True

    return await is_bot_admin(
        interaction.guild_id,
        interaction.user.id,
    )


def bot_admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not await user_is_bot_admin(interaction):
            raise app_commands.CheckFailure(
                "You do not have bot administrator permission "
                "for this server."
            )
        return True

    predicate.__bot_admin_only__ = True
    return app_commands.check(predicate)