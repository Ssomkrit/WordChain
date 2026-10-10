import asyncio
import contextlib
import json
import logging
import sys
from json import JSONDecodeError
from logging.config import fileConfig
from typing import Any, AsyncIterator

import discord
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from discord import Colour, Embed, Interaction, Object, app_commands
from discord.ext.commands import AutoShardedBot, ExtensionError, ExtensionNotLoaded
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from consts import (COG_NAME_ADMIN_CMDS, COG_NAME_COMMON, COG_NAME_GAME, COG_NAME_MANAGER_CMDS, COG_NAME_USER_CMDS,
                    COGS_LIST, LOGGER_NAME_MAIN, SETTINGS)

# load logging config from alembic file because it would be loaded anyway when using alembic
fileConfig(fname='config.ini')
logger = logging.getLogger(LOGGER_NAME_MAIN)


async def sync_admin_guilds(tree: app_commands.CommandTree, current_guild_ids: list[int],
                            previous_guild_ids: list[int]):
    """Synchronize bot-admin commands to every allowed guild and remove stale registrations."""
    last_sync = None
    synced_guild_ids: list[int] = []

    for guild_id in current_guild_ids:
        try:
            last_sync = await tree.sync(guild=Object(id=guild_id))
            synced_guild_ids.append(guild_id)
            logger.info(f'Synchronized {len(last_sync)} admin commands for guild {guild_id}')
        except discord.HTTPException:
            # A configured guild may not have the bot yet; do not prevent the bot from starting.
            logger.exception(f'Failed to synchronize admin commands for guild {guild_id}')

    for guild_id in sorted(set(previous_guild_ids) - set(current_guild_ids)):
        guild = Object(id=guild_id)
        tree.clear_commands(guild=guild)
        try:
            await tree.sync(guild=guild)
            logger.info(f'Removed stale admin commands from guild {guild_id}')
        except discord.HTTPException:
            logger.exception(f'Failed to remove stale admin commands from guild {guild_id}')

    return last_sync, synced_guild_ids


class WordChainCommandTree(app_commands.CommandTree):
    async def on_error(self, interaction: Interaction, error: app_commands.AppCommandError, /) -> None:
        original = getattr(error, 'original', error)  # unwrap CommandInvokeError
        command = interaction.command.qualified_name if interaction.command else 'unknown'
        logger.error(
            f'Unhandled {type(original).__name__} in /{command} '
            f'(guild {interaction.guild_id}, channel {interaction.channel_id}, '
            f'user {interaction.user.id})',
            exc_info=original,
        )

        error_embed = Embed(title='Ошибка', colour=Colour.red(),
                            description='Что-то пошло не так.')
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=error_embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=error_embed, ephemeral=True)
        except discord.HTTPException:
            logger.warning(f'Unable to notify user about the error in /{command}')


class WordChainBot(AutoShardedBot):
    """Word chain bot"""

    __SQL_ENGINE: AsyncEngine = create_async_engine('sqlite+aiosqlite:///database_word_chain.sqlite3')
    __LOCK: asyncio.Lock = asyncio.Lock()

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(command_prefix='!', intents=intents, tree_cls=WordChainCommandTree)

    # ----------------------------------------------------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def db_connection(self, locked=True) -> AsyncIterator[AsyncConnection]:
        if locked:
            async with self.__LOCK:
                async with self.__SQL_ENGINE.begin() as connection:
                    yield connection
        else:
            async with self.__SQL_ENGINE.begin() as connection:
                yield connection

    # ---------------------------------------------------------------------------------------------------------------

    async def on_ready(self) -> None:
        """Override the on_ready method"""
        user = self.user
        if not user:
            logger.info('Bot is ready')
        else:
            logger.info(f'Bot is ready as {user.name}#{user.discriminator}')

    # ---------------------------------------------------------------------------------------------------------------

    async def on_error(self, event_method: str, /, *args: Any, **kwargs: Any) -> None:
        _, exc, _ = sys.exc_info()

        def extract_guild_id(objects: tuple) -> int | None:
            for discord_object in objects:
                if isinstance(discord_object, discord.Guild):
                    return discord_object.id
                guild = getattr(discord_object, 'guild', None)
                if isinstance(guild, discord.Guild):
                    return guild.id
                g_id = getattr(discord_object, 'guild_id', None)
                if isinstance(g_id, int):
                    return g_id
            return None

        guild_id = extract_guild_id(args)
        logger.error(f'Unhandled exception in {event_method} ({guild_id=}):', exc_info=exc)

    # ---------------------------------------------------------------------------------------------------------------

    async def setup_hook(self) -> None:

        for cog_name in COGS_LIST:
            await self.load_extension(f'cogs.{cog_name}')

        signature = load_command_signature()
        current_admin_guild_ids = SETTINGS.all_admin_guild_ids
        previous_admin_guild_ids = signature.get('admin_guild_ids', [SETTINGS.admin_guild_id])
        admin_guild = Object(id=current_admin_guild_ids[0])

        global_payload = [command.to_dict(self.tree) for command in self.tree.get_commands()]
        admin_payload = [command.to_dict(self.tree) for command in self.tree.get_commands(guild=admin_guild)]

        global_changed = signature['global_commands'] != global_payload
        admin_changed = signature['admin_commands'] != admin_payload
        admin_guilds_changed = set(previous_admin_guild_ids) != set(current_admin_guild_ids)

        if global_changed:
            global_sync = await self.tree.sync()
            logger.info(f'Synchronized {len(global_sync)} global commands')
        else:
            logger.info('No changes in global commands detected')

        if admin_changed or admin_guilds_changed:
            admin_sync, synced_admin_guild_ids = await sync_admin_guilds(
                self.tree, current_admin_guild_ids, previous_admin_guild_ids
            )
        else:
            admin_sync = None
            synced_admin_guild_ids = previous_admin_guild_ids
            logger.info('No changes in admin commands or authorized servers detected')

        if global_changed or admin_changed or admin_guilds_changed:
            store_command_signature(global_payload, admin_payload, synced_admin_guild_ids)

        alembic_cfg = AlembicConfig('config.ini')
        alembic_command.upgrade(alembic_cfg, 'head')


word_chain_bot: WordChainBot = WordChainBot()


# ===================================================================================================================


def load_command_signature() -> dict:
    try:
        with open(SETTINGS.command_signature_file,'r') as f:
            signature = json.load(f)
    except (JSONDecodeError, FileNotFoundError):
        logger.error('Failed to load existing command signature')
        signature = {
            'global_commands': [],
            'admin_commands': [],
            'admin_guild_ids': [SETTINGS.admin_guild_id]
        }
    # Compatibility with the signature file created by older versions.
    signature.setdefault('admin_guild_ids', [SETTINGS.admin_guild_id])
    return signature


def store_command_signature(global_commands: list[dict[str, Any]], admin_commands: list[dict[str, Any]],
                            admin_guild_ids: list[int]):
    with open(SETTINGS.command_signature_file, 'w') as f:
        signature = {
            'global_commands': global_commands,
            'admin_commands': admin_commands,
            'admin_guild_ids': admin_guild_ids
        }
        json.dump(signature, f)
        logger.info('Dumped latest command signature')


@word_chain_bot.tree.command(name='reload', description='Выгрузить и перезагрузить модуль')
@app_commands.guilds(*SETTINGS.all_admin_guild_ids)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(cog_name='Модуль для перезагрузки')
@app_commands.choices(cog_name=[
    app_commands.Choice(name='Команды администратора', value=COG_NAME_ADMIN_CMDS),
    app_commands.Choice(name='Команды менеджера', value=COG_NAME_MANAGER_CMDS),
    app_commands.Choice(name='Команды пользователя', value=COG_NAME_USER_CMDS),
    app_commands.Choice(name='Игра', value=COG_NAME_GAME),
    app_commands.Choice(name='Общие', value=COG_NAME_COMMON),
    app_commands.Choice(name='Все модули', value='all')
])
async def reload(interaction: Interaction, cog_name: str, force_sync: bool = False):
    """Reloads a particular cog/all cogs."""
    await interaction.response.defer()

    cogs_failed: list[str] = []

    match cog_name:

        case 'all':
            for cog_name in COGS_LIST:
                try:
                    try:  # Try to unload each cog
                        await word_chain_bot.unload_extension(f'cogs.{cog_name}')
                    except ExtensionNotLoaded:
                        logger.info(f'Extension {cog_name} not loaded.')

                    await word_chain_bot.load_extension(f'cogs.{cog_name}')  # Then reload the
                except ExtensionError as e:
                    logger.exception(f'Failed to load extension {cog_name}', e)
                    cogs_failed.append(cog_name)
        case _:
            try:
                try:
                    await word_chain_bot.unload_extension(f'cogs.{cog_name}')
                except ExtensionNotLoaded:
                    logger.info(f'Extension {cog_name} not loaded.')

                await word_chain_bot.load_extension(f'cogs.{cog_name}')
            except ExtensionError as e:
                logger.exception(f'Failed to load extension {cog_name}', e)
                cogs_failed.append(cog_name)

    signature = load_command_signature()
    current_admin_guild_ids = SETTINGS.all_admin_guild_ids
    previous_admin_guild_ids = signature.get('admin_guild_ids', [SETTINGS.admin_guild_id])
    admin_guild = Object(id=current_admin_guild_ids[0])
    global_payload = [command.to_dict(word_chain_bot.tree) for command in word_chain_bot.tree.get_commands()]
    admin_payload = [command.to_dict(word_chain_bot.tree) for command in word_chain_bot.tree.get_commands(guild=admin_guild)]

    global_changed = signature['global_commands'] != global_payload
    admin_changed = signature['admin_commands'] != admin_payload
    admin_guilds_changed = set(previous_admin_guild_ids) != set(current_admin_guild_ids)

    emb: Embed = Embed(title=f'Статус синхронизации', description=f'Синхронизация завершена.', colour=Colour.dark_magenta())

    global_sync: list[app_commands.AppCommand] | None
    admin_sync: list[app_commands.AppCommand] | None

    if force_sync:
        global_sync = await word_chain_bot.tree.sync()
        admin_sync, synced_admin_guild_ids = await sync_admin_guilds(
            word_chain_bot.tree, current_admin_guild_ids, previous_admin_guild_ids
        )
        store_command_signature(global_payload, admin_payload, synced_admin_guild_ids)
    else:
        if global_changed:
            global_sync = await word_chain_bot.tree.sync()
        else:
            global_sync = None

        if admin_changed or admin_guilds_changed:
            admin_sync, synced_admin_guild_ids = await sync_admin_guilds(
                word_chain_bot.tree, current_admin_guild_ids, previous_admin_guild_ids
            )
        else:
            admin_sync = None
            synced_admin_guild_ids = previous_admin_guild_ids

        if global_changed or admin_changed or admin_guilds_changed:
            store_command_signature(global_payload, admin_payload, synced_admin_guild_ids)

    emb.add_field(name="Глобальные команды", value=f"{len(global_sync)}" if global_sync else "ПРОПУЩЕНО")
    emb.add_field(name="Команды администратора", value=f"{len(admin_sync)}" if admin_sync else "ПРОПУЩЕНО")

    if cogs_failed:
        emb.add_field(name="Модули", value=f'{",".join([f"*{c}*" for c in cogs_failed])} не удалось загрузить')

    await interaction.followup.send(embed=emb)


# ===================================================================================================================

if __name__ == '__main__':
    word_chain_bot.run(SETTINGS.token, log_handler=None)
