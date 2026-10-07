from __future__ import annotations

import logging
import time
from collections import defaultdict
from logging.config import fileConfig
from typing import TYPE_CHECKING, Optional, Sequence

import discord
from discord import Colour, Embed, Interaction, SelectOption, app_commands
from discord.ext.commands import Cog
from discord.ui import View
from sqlalchemy import CursorResult, func, or_, select
from sqlalchemy.engine.row import Row
from sqlalchemy.sql.functions import count

from cogs.common import WordStatus
from consts import (COG_NAME_COMMON, COG_NAME_USER_CMDS, LOGGER_NAME_USER_COG, MISTAKE_PENALTY,
                    RELIABLE_ROLE_ACCURACY_THRESHOLD, RELIABLE_ROLE_KARMA_THRESHOLD, SETTINGS, GameMode)
from language import Language
from model import BannedMemberModel, Member, MemberModel, ServerConfig, ServerConfigModel
from views.dropdown import Dropdown

if TYPE_CHECKING:
    from cogs.common import CommonCog
    from main import WordChainBot

fileConfig(fname='config.ini')
logger: logging.Logger = logging.getLogger(LOGGER_NAME_USER_COG)


class UserCommandsCog(Cog, name=COG_NAME_USER_CMDS):

    def __init__(self, bot: WordChainBot) -> None:
        self.bot: WordChainBot = bot
        self.bot.tree.add_command(UserCommandsCog.StatsCmdGroup(self))
        self.bot.tree.add_command(UserCommandsCog.LeaderboardCmdGroup(self))

    @property
    def common(self) -> CommonCog:
        for _ in range(5):
            if (cog := self.bot.get_cog(COG_NAME_COMMON)) is not None:
                return cog # noqa
            time.sleep(.2)
        raise ValueError(f'Cog {COG_NAME_COMMON} not found')

    # ---------------------------------------------------------------------------------------------------------------

    def cog_load(self) -> None:
        logger.info(f'Cog {self.qualified_name} loaded.')

    # ---------------------------------------------------------------------------------------------------------------

    def cog_unload(self) -> None:

        logger.info('Removing commands...')

        for command in self.bot.tree.get_commands():  # Loop through all commands in the bot
            if command in self.__cog_commands__:  # And remove the ones that are in the specified cog
                self.bot.tree.remove_command(command.name)

        logger.info(f'Cog {self.qualified_name} unloaded.')

    # ---------------------------------------------------------------------------------------------------------------

    @app_commands.command(name='support', description='Присоединиться к серверу поддержки!')
    @app_commands.guild_only()
    async def support(self, interaction: Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=self.HelpCommand.get_support_server_embed())

    # ---------------------------------------------------------------------------------------------------------------

    @app_commands.command(name='vote', description='Проголосовать за бота!')
    @app_commands.guild_only()
    async def vote(self, interaction: Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=self.HelpCommand.get_vote_embed())

    # ---------------------------------------------------------------------------------------------------------------

    @app_commands.command(name='show_languages', description='Показывает языки, включённые на этом сервере')
    @app_commands.guild_only()
    async def show_languages(self, interaction: Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            return

        emb: Embed = Embed(colour=Colour.gold(), title='Языки, включённые на этом сервере', description='')
        emb.description = self.common.get_current_languages_string(self.common, guild.id)

        await interaction.followup.send(embed=emb)

    # ---------------------------------------------------------------------------------------------------------------

    @app_commands.command(name='check_word', description='Проверить, является ли слово правильным')
    @app_commands.describe(word='The word to check')
    @app_commands.guild_only()
    async def check_word(self, interaction: Interaction, word: str):
        """
        Checks if a word is valid.

        Hierarchy followed:
        1. Legal characters.
        2. Length of word must be > 1.
        3. Whitelist.
        4. Blacklists
        5. Check word cache.
        6. Query API.
        """
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            return

        await self.common.ensure_config(guild)
        config = self.common.server_configs[guild.id]

        word = word.lower()

        emb = Embed(color=Colour.blurple())

        status = await self.common.check_word_status(word, guild, config.languages)
        match status:
            case WordStatus.TOO_SHORT:
                emb.description = f'❌ Слово **{word}** недействительно.'
            case WordStatus.NO_LANGUAGE_MATCH:
                emb.description = f'❌ Слово **{word}** недопустимо.'
            case WordStatus.WHITELISTED | WordStatus.WORD_EXISTS:
                emb.description = f'''✅ The word **{word}** is valid.\n
-# Please note that the validity of words is checked only for the languages that are enabled in the server. \
Therefore, a word that is valid in this server may not be valid in another server.'''
            case WordStatus.BLACKLISTED:
                emb.description = f'❌ Слово **{word}** в чёрном списке и недействительно.'
            case WordStatus.WORD_DOESNT_EXIST:
                emb.description = emb.description = f'''❌ The word **{word}** is **NOT** valid.\n
-# Please note that the validity of words is checked only for the languages that are enabled in the server. \
Therefore, a word that is valid in this server may not be valid in another server.'''
            case WordStatus.ERROR | _:
                emb.description = f'⚠️ При обработке запроса произошла ошибка.'

        await interaction.followup.send(embed=emb)

    # ---------------------------------------------------------------------------------------------------------------

    @app_commands.command(name='definition', description='Проверить значение слова')
    @app_commands.describe(word='The word to check')
    @app_commands.guild_only()
    async def definition(self, interaction: Interaction, word: str):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            return

        await self.common.ensure_config(guild)
        config = self.common.server_configs[guild.id]

        status = await self.common.check_word_status(word, guild, config.languages)

        emb: Embed = Embed(colour=Colour.orange())

        match status:
            case WordStatus.WORD_EXISTS | WordStatus.WHITELISTED:
                data = self.common.query_wiktionary_definitions(word, config.languages)

                if data is None:
                    emb.description = f'⚠️ При обработке запроса произошла ошибка.'
                elif len(data) == 0:
                    emb.description = f'Для слова **{word}** не найдено определение ни на одном из включённых языков.'
                else:
                    emb.description = "\n".join(
                        f"**{language.display_name} {d.part_of_speech.lower()}**: {d.definitions[0].definition}"
                        for language, definitions in data.items()
                        for d in definitions
                        if d.definitions and d.definitions[0].definition
                    )
            case WordStatus.WORD_DOESNT_EXIST | WordStatus.BLACKLISTED | WordStatus.NO_LANGUAGE_MATCH | WordStatus.TOO_SHORT:
                emb.description = f'Слово **{word}** недействительно, поэтому определение недоступно.'
            case WordStatus.ERROR | _:
                emb.description = f'⚠️ При обработке запроса произошла ошибка.'

        await interaction.followup.send(embed=emb)

    # ---------------------------------------------------------------------------------------------------------------

    @app_commands.command(name='help', description='Показывает меню помощи')
    @app_commands.guild_only()
    async def help(self, interaction: Interaction) -> None:

        await interaction.response.defer(ephemeral=True, thinking=True)

        help_cmd: UserCommandsCog.HelpCommand = UserCommandsCog.HelpCommand(self, interaction)
        view1: discord.ui.View = discord.ui.View().add_item(help_cmd.get_dropdown())
        embed: Embed = Embed(title='Меню помощи', colour=Colour.blurple(), description=f'Выберите раздел ниже.')

        msg: discord.Message = await interaction.followup.send(embed=embed, view=view1, wait=True)
        help_cmd.original_message_id = msg.id

    # =================================================================================================================

    class HelpCommand:

        __HOW_TO_PLAY: str = "how_to_play"
        __GAME_RULES: str = "game_rules"
        __SETUP_IN_SERVER: str = "setup_in_server"
        __KARMA_SYSTEM: str = "karma_system"
        __LIST_OF_COMMANDS: str = "list_commands"
        __PRIVACY_POLICY: str = "privacy_policy"
        __SUPPORT_SERVER: str = "support_server"
        __OTHER_INFO: str = "other_info"
        __VOTE: str = "vote"
        __MULTI_LANGUAGE: str = "multi_language"

        # ------------------------------------------------------------------------------------------------------------

        def __init__(self, cog: UserCommandsCog, original_interaction: Interaction, original_message_id: int = -1) -> None:
            super().__init__()
            self.cog: UserCommandsCog = cog
            self.original_interaction: Interaction = original_interaction
            self.original_message_id: int = original_message_id  # This has to be set later, after sending the initial message

        # ------------------------------------------------------------------------------------------------------------

        def get_dropdown(self) -> Dropdown:

            options_list: list[SelectOption] = [SelectOption(label="Как играть",
                                                             value=UserCommandsCog.HelpCommand.__HOW_TO_PLAY),
                                                SelectOption(label="Правила игры",
                                                             value=UserCommandsCog.HelpCommand.__GAME_RULES),
                                                SelectOption(label="Система кармы",
                                                             value=UserCommandsCog.HelpCommand.__KARMA_SYSTEM),
                                                SelectOption(label="Поддержка нескольких языков",
                                                             value=UserCommandsCog.HelpCommand.__MULTI_LANGUAGE),
                                                SelectOption(label="Проголосовать за бота!!",
                                                             value=UserCommandsCog.HelpCommand.__VOTE),
                                                SelectOption(label="Список команд",
                                                             value=UserCommandsCog.HelpCommand.__LIST_OF_COMMANDS),
                                                SelectOption(label="Настройка бота на сервере",
                                                             value=UserCommandsCog.HelpCommand.__SETUP_IN_SERVER),
                                                SelectOption(label="Политика конфиденциальности",
                                                             value=UserCommandsCog.HelpCommand.__PRIVACY_POLICY),
                                                SelectOption(label="Сервер поддержки",
                                                             value=UserCommandsCog.HelpCommand.__SUPPORT_SERVER),
                                                SelectOption(label="Авторы и другая информация",
                                                             value=UserCommandsCog.HelpCommand.__OTHER_INFO)
                                                ]

            async def dropdown_callback(dropdown: Dropdown, interaction: Interaction) -> None:
                view1: View = View().add_item(dropdown.regenerate_self())

                match dropdown.values[0]:
                    case UserCommandsCog.HelpCommand.__HOW_TO_PLAY:
                        await interaction.followup.edit_message(self.original_message_id,
                                                                embed=UserCommandsCog.HelpCommand.get_how_to_play_embed(),
                                                                view=view1)

                    case UserCommandsCog.HelpCommand.__KARMA_SYSTEM:
                        guild = interaction.guild
                        config: ServerConfig | None = self.cog.common.server_configs.get(guild.id)

                        await interaction.followup.edit_message(self.original_message_id,
                                                                embed=UserCommandsCog.HelpCommand.get_karma_embed(config),
                                                                view=view1)

                    case UserCommandsCog.HelpCommand.__GAME_RULES:
                        await interaction.followup.edit_message(self.original_message_id,
                                                                embed=UserCommandsCog.HelpCommand.get_game_rules_embed(),
                                                                view=view1)

                    case UserCommandsCog.HelpCommand.__VOTE:
                        await interaction.followup.edit_message(self.original_message_id,
                                                                embed=UserCommandsCog.HelpCommand.get_vote_embed(),
                                                                view=view1)

                    case UserCommandsCog.HelpCommand.__LIST_OF_COMMANDS:
                        await interaction.followup.edit_message(self.original_message_id,
                                                                embed=self.get_cmd_list_embed(interaction),
                                                                view=view1)

                    case UserCommandsCog.HelpCommand.__PRIVACY_POLICY:
                        await interaction.followup.edit_message(self.original_message_id,
                                                                embed=self.get_privacy_policy_embed(),
                                                                view=view1)

                    case UserCommandsCog.HelpCommand.__SUPPORT_SERVER:
                        await interaction.followup.edit_message(self.original_message_id,
                                                                embed=self.get_support_server_embed(),
                                                                view=view1)

                    case UserCommandsCog.HelpCommand.__OTHER_INFO:
                        await interaction.followup.edit_message(self.original_message_id,
                                                                embed=self.get_credits_embed(),
                                                                view=view1)

                    case UserCommandsCog.HelpCommand.__SETUP_IN_SERVER:
                        await interaction.followup.edit_message(self.original_message_id,
                                                                embed=UserCommandsCog.HelpCommand.setup_in_server(),
                                                                view=view1)

                    case UserCommandsCog.HelpCommand.__MULTI_LANGUAGE:
                        await interaction.followup.edit_message(self.original_message_id,
                                                                embed=UserCommandsCog.HelpCommand.
                                                                get_multi_language_embed(),
                                                                view=view1)

            return Dropdown(dropdown_callback, options_list, original_interaction=self.original_interaction,
                            max_values=1)

        # ------------------------------------------------------------------------------------------------------------

        @staticmethod
        def get_how_to_play_embed() -> Embed:

            return Embed(title="Как играть", description=f'''\
## Обычный режим
The game is pretty simple.

- Enter a word that starts with the last letter of the previous correct word.  
- If your word is correct, the bot will react with a tick mark — :white_check_mark:.  
- No characters other than letters and hyphen (`-`) are accepted. Messages with anything else will be ignored. \
If only English is selected (default), only letters of the English alphabet are accepted. If other languages are \
selected, typical accentuated letters for that language are accepted as well.
- You can check if a word is correct using the `/check_word` command - this checks only if the word is generally \
accepted, it does **not** check if it has the matching starting letter to continue the current chain.
- Words that have been used once cannot be used again until the chain is broken. The chain, however, will **not**
 be broken if you enter a word that has been used previously. The bot will simply ask you to enter another word.
- Entering a wrong/non-existent word will break the chain.
- Entering words twice in a row by the same user will also break the chain.
- Once the chain is broken, all used words will be reset.
- The chain continues even if someone messes up, in the sense that one still has to enter a word beginning with the \
last letter of the previous correct word.

That's all. Go and beat the high score in your server and top the global leaderboard!! :fire:

## Сложный режим
Сложный режим is the same as normal mode, except that the first **two letters** of a word must be the 
same as the last two letters of the previous word.
''', colour=Colour.dark_orange())

        # ------------------------------------------------------------------------------------------------------------

        @staticmethod
        def get_game_rules_embed() -> Embed:

            return Embed(title="Общие правила игры", description=f'''\
You are **not** allowed to use any automation/botting of *any* kind under any circumstances. If you are reported, \
you will be banned from the bot for a lifetime.

***Please note:** In addition to these rules, the server where you are playing may \
have other rules that are not covered here. Please check with the server moderators or administrators.*
''', colour=Colour.red())

        # ------------------------------------------------------------------------------------------------------------

        @staticmethod
        def get_multi_language_embed() -> Embed:

            return Embed(title="Поддержка нескольких языков", description=f'''\
The bot now allows you to enable up to two languages in a server.

The following languages are supported:
{', '.join(f'{language.display_name}' for language in Language)}

In order to enable/disable languages, server managers can use the commands under the `/language` category.
''', colour=Colour.dark_orange())

        # ------------------------------------------------------------------------------------------------------------

        @staticmethod
        def setup_in_server() -> Embed:

            return Embed(description=f'''\
## Basic setup
1. Add the bot to your server. You can click on the bot's profile picture and click "Add App".
2. Run `/set channel` to set the channel where the game will be played. (You need to have at least `Manage Server` \
permission to run this command.)

This will be enough to let users play the game in your server. Send any word to start the chain.

If it doesn't work, managers can use the `/health_check` command to check if the permissions are set correctly.

## Highly recommended setup
- Disable the `Add reactions` permissions for `@everyone` in the game channel.
> **Why do we recommend this?**
> We have seen people put check mark reactions to words that are wrong when the bot lags in putting a reaction \
(eg. when the discord API is slow due to any reason or when the bot is going through \
a restart), and thereby mislead users on what the last correct word is.

## Optional setup
1. Set the failed role using `/set failed_role`, and the reliable role with `/set reliable_role`.
2. To make sure that people have read the game rules, create a channel with the game rules, along with a \
reaction role giving access to the game channel. This will make sure that people will be able to play \
only after agreeing that they have read the rules.

For multi-language setup, see the `Поддержка нескольких языков` section in the `/help` command.''', colour=Colour.yellow())

        # ------------------------------------------------------------------------------------------------------------

        @staticmethod
        def get_karma_embed(config: ServerConfig | None) -> Embed:
            description = f'''\
The karma system is based upon the frequency of characters as the first letter.

**You *gain* karma if:**
- your word starts with a letter that is less frequent than average (because it is harder to find)
- your word ends with a letter that is more frequent than average (because it makes it easier for the next player)
- you use a variety of words that end in different letters

**You *lose* karma if:**
- your word ends with a letter  (eg. `y`) that is less frequent than average (because it makes it harder \
for the next player)
- you keep using words that end in the same letter.

**You do *not* lose karma if:**
- your word starts with a letter that is more frequent than average (because you cannot choose the first letter)

**If you mess up:** You lose {MISTAKE_PENALTY} karma points.

:point_right:  Karma will never be < 0.
:point_right:  Check your karma in the `/stats user` command.
:point_right:  To view the karma leaderboard, use `/leaderboard user metric:karma`.
'''
            if config and config.reliable_role_id:
                description += f''':point_right:  To receive the <@&{config.reliable_role_id}> role, you must have \
karma > {RELIABLE_ROLE_KARMA_THRESHOLD} and accuracy > {RELIABLE_ROLE_ACCURACY_THRESHOLD:.1%}.\
'''

            return Embed(title='Система кармы', description=description, colour=Colour.green())

        # ------------------------------------------------------------------------------------------------------------

        @staticmethod
        def get_support_server_embed() -> Embed:
            return Embed(title='Сервер поддержки', description=f'''\
For any questions, suggestions or bug reports, or if you just want to hang out with a cool community of word chain \
players, feel free to join our support server:

https://discord.gg/yhbzVGBNw3''', colour=Colour.pink())

        # ------------------------------------------------------------------------------------------------------------

        @staticmethod
        def get_cmd_list_embed(interaction: Interaction) -> Embed:
            emb = Embed(title='Команды', color=Colour.blue(),
                        description='''\
`/stats user` — Показывает статистику игрока.
`/stats server` — Показывает статистику сервера.
`/check_word` — Проверяет существование и написание слова.
`/definition` - Проверить значение слова.
`/leaderboard user` — Показывает рейтинг игроков.
`/leaderboard server` — Показывает глобальный рейтинг серверов.
`/show_languages` — Показывает включённые языки.
`/support` — Показывает ссылку на сервер поддержки.
`/vote` — Показывает ссылки для голосования за бота.
`/help` — Показывает эту справку.''')

            member = interaction.user
            if not isinstance(member, discord.Member):
                return emb

            if member.guild_permissions.manage_guild:
                emb.description += '''\n
**Команды для менеджеров сервера**
`/set channel` — Настраивает канал для игры.
`/set failed_role` — Настраивает роль за ошибку.
`/set reliable_role` — Настраивает надёжную роль.

`/language show-all` — Показывает поддерживаемые языки и их коды.
`/language add` — Включает язык на сервере.
`/language remove` — Отключает язык.

`/unset failed_role` — Убирает роль за ошибку.
`/unset reliable_role` — Убирает надёжную роль.

`/blacklist add` — Добавляет слово в чёрный список.
`/blacklist remove` — Убирает слово из чёрного списка.
`/blacklist show` — Показывает чёрный список.

`/whitelist add` — Добавляет слово в белый список.
`/whitelist remove` — Убирает слово из белого списка.
`/whitelist show` — Показывает белый список.

`/health_check` — Проверяет настройки и права бота.
`/reset_stats` — Сбрасывает статистику сервера, сохраняя настройки.'''

            guild = interaction.guild
            if guild is None:
                return emb

            if member.guild_permissions.administrator and guild.id == SETTINGS.admin_guild_id:
                emb.description += '''\n
**Команды только для администраторов бота**
`/announce` — Отправляет объявление на игровые каналы всех серверов.
`/reload` — Перезагружает модуль (или все модули).
`/ban_server` — Управляет блокировкой сервера в глобальном рейтинге.
`/ban_member` — Управляет глобальной блокировкой игрока.
`/purge_data server` — Удаляет данные сервера.
`/purge_data user` — Удаляет данные игрока.
`/reset_config` — Сбрасывает данные сервера и создаёт настройки заново.
`/list_servers` — Экспортирует список серверов и владельцев.
`/admin_health_check` — Проверяет настройки и права бота.
`/logging status` — Показывает состояние журналов.
`/logging enable_all` — Включает журналы.
`/logging disable_all` — Отключает журналы.
`/logging enable_logger` — Включает выбранный журнал.
`/logging disable_logger` — Отключает выбранный журнал.
`/logging set_level` — Настраивает уровень журнала.
`/logging test` — Проверяет выбранный журнал.'''

            return emb

        # ------------------------------------------------------------------------------------------------------------

        @staticmethod
        def get_privacy_policy_embed() -> Embed:

            return Embed(title='Политика конфиденциальности', description=f'''\
The privacy policy is available \
[here](https://github.com/WrichikBasu/word_chain_bot_indently/blob/main/PRIVACY_POLICY.md).''',
                         color=Colour.yellow())

        # -------------------------------------------------------------------------------------------------------------

        @staticmethod
        def get_credits_embed() -> Embed:

            return Embed(title='Credits', description='''\
- **Source code**
The bot is open-source, released under the BSD-3-Clause-License. The source code is \
[available on GitHub](https://github.com/WrichikBasu/word_chain_bot_indently).
- **Hosting information**
The bot is currently hosted on Hetzner, provided by <@841541609052307458> and his friends.
- **Credits**
  - Base code taken from [Counting Bot Indently](https://github.com/guanciottaman/counting_bot_indently).
  - Base code modified for the Word Chain Bot by <@1024746441798856717>.
  - Система кармы and multi-server support completely designed by <@329857455423225856>.
  - Поддержка нескольких языков by <@1024746441798856717>, with inputs from <@329857455423225856>.
- **What/who is "Indently"?**
This bot was created for the [Indently Discord server](https://discord.com/invite/indently-1040343818274340935), \
and is owned by the Indently Bot Dev Team. Federico, the founder of Indently, has kindly allowed us to keep the \
name of his company in our bot's name. (btw, if you are keen to learn python and interact with fellow programmers, \
check out the Indently Discord linked above!)''', colour=Colour.teal())

        # ------------------------------------------------------------------------------------------------------------

        @staticmethod
        def get_vote_embed() -> Embed:

            return Embed(title='Проголосовать за бота!', description=f'''\
**Word Chain Bot Indently** is an open-source bot. We developers do not earn anything from it, but it \
is your excitement that fuels us to continue working on it. We will really appreciate it if you vote for our bot, \
as it will allow more people discover it!

[Vote on Top.gg!](https://top.gg/bot/1222301436054999181/vote)
[Vote on discordbotlist.com!](https://discordbotlist.com/bots/word-chain-bot-indently/upvote)''',
                         color=Colour.red())

    # ===================================================================================================================

    class LeaderboardCmdGroup(app_commands.Group):

        def __init__(self, parent_cog: UserCommandsCog):
            super().__init__(name='leaderboard', guild_only=True)
            self.cog: UserCommandsCog = parent_cog

        # ---------------------------------------------------------------------------------------------------------------

        @app_commands.command(description='Показывает 10 игроков с наибольшими очками/кармой')
        @app_commands.describe(metric='Выберите очки или карму для сортировки рейтинга')
        @app_commands.choices(metric=[
            app_commands.Choice(name='score', value='score'),
            app_commands.Choice(name='karma', value='karma')
        ])
        @app_commands.describe(
            scope='Выберите игроков текущего сервера или всех игроков')
        @app_commands.choices(scope=[
            app_commands.Choice(name='server', value='server'),
            app_commands.Choice(name='global', value='global')
        ])
        async def user(self, interaction: Interaction, metric: Optional[app_commands.Choice[str]],
                       scope: Optional[app_commands.Choice[str]]):
            """Command to show the top 10 users with the highest score/karma."""
            await interaction.response.defer()

            guild = interaction.guild
            if guild is None:
                return

            board_metric: str = 'score' if metric is None else metric.value
            board_scope: str = 'server' if scope is None else scope.value

            emb = Embed(
                title=f'Топ-10 игроков по {board_metric}',
                color=Colour.blue(),
                description=''
            )

            match board_scope:
                case 'server':
                    emb.set_author(name=guild.name,
                                   icon_url=guild.icon.url if guild.icon else None)
                case 'global':
                    emb.set_author(name='Global')

            async with self.cog.bot.db_connection(locked=False) as connection:
                limit = 10

                match board_metric:
                    case 'score':
                        field = MemberModel.score
                    case 'karma':
                        field = MemberModel.karma
                    case _:
                        raise ValueError(f'Unknown metric {board_metric}')

                match board_scope:
                    case 'server':
                        stmt = (select(MemberModel.member_id, field)
                                .where(MemberModel.server_id == guild.id)
                                .where(~MemberModel.member_id.in_(select(BannedMemberModel.member_id)))
                                .where(field > 0)
                                .order_by(field.desc())
                                .limit(limit))
                    case 'global':
                        stmt = (select(MemberModel.member_id, func.sum(field))
                                .group_by(MemberModel.member_id)
                                .where(~MemberModel.member_id.in_(select(BannedMemberModel.member_id)))
                                .where(field > 0)
                                .order_by(func.sum(field).desc())
                                .limit(limit))
                    case _:
                        raise ValueError(f'Unknown scope {board_scope}')

                result: CursorResult = await connection.execute(stmt)
                data: Sequence[Row[tuple[int, int | float]]] = result.fetchall()

                if len(data) == 0:  # Stop when no users could be retrieved.
                    match board_scope:
                        case 'server':
                            emb.description = ':warning: На этом сервере ещё никто не играл!'
                        case 'global':
                            emb.description = ':warning: Пока никто не играл!'
                else:
                    last_score_or_karma = None
                    last_rank = 0
                    for rank, user_data in enumerate(data, 1):
                        member_id, score_or_karma = user_data
                        if last_score_or_karma == score_or_karma:
                            rank = last_rank
                        match board_metric:
                            case 'score':
                                emb.description += f'`{str(rank).rjust(2, ' ')}.` <@{member_id}> **{score_or_karma}**\n'
                            case 'karma':
                                emb.description += f'`{str(rank).rjust(2, ' ')}.` <@{member_id}> **{score_or_karma:.2f}**\n'
                        last_score_or_karma = score_or_karma
                        last_rank = rank

                await interaction.followup.send(embed=emb)

        # ---------------------------------------------------------------------------------------------------------------

        @app_commands.command(description='Показывает 10 серверов с наибольшим рекордом')
        async def server(self, interaction: Interaction, game_mode: GameMode = GameMode.NORMAL):
            """Command to show the top 10 servers with the highest highscore"""
            await interaction.response.defer()

            guild = interaction.guild
            if guild is None:
                return

            async with self.cog.bot.db_connection(locked=False) as connection:
                limit = 10

                match game_mode:
                    case GameMode.NORMAL:
                        high_score_column = ServerConfigModel.high_score
                        game_mode_name = 'Обычный режим'
                    case GameMode.HARD:
                        high_score_column = ServerConfigModel.hard_mode_high_score
                        game_mode_name = 'Сложный режим'

                stmt = (select(ServerConfigModel.server_id, high_score_column)
                        .where(
                            or_(ServerConfigModel.is_banned == 0, ServerConfigModel.server_id == guild.id)
                        )
                        .where(high_score_column > 0)
                        .order_by(high_score_column.desc())
                        .limit(limit))

                emb = Embed(
                    title=f'Топ-10 серверов по рекорду',
                    color=Colour.blue(),
                    description=''
                ).set_author(name=f'Global ({game_mode_name})')

                result: CursorResult = await connection.execute(stmt)
                data: Sequence[Row[tuple[int, int]]] = result.fetchall()

                guild_names = defaultdict(lambda: 'unknown', {g.id: g.name for g in self.cog.bot.guilds})
                last_high_score = None
                last_rank = 0
                for rank, (server_id, high_score) in enumerate(data, 1):
                    if last_high_score == high_score:
                        rank = last_rank
                    emb.description += f'`{str(rank).rjust(2, ' ')}.` {guild_names[server_id]} **{high_score}**\n'
                    last_high_score = high_score
                    last_rank = rank

                await interaction.followup.send(embed=emb)

    # ===================================================================================================================

    class StatsCmdGroup(app_commands.Group):

        def __init__(self, parent_cog: UserCommandsCog):
            super().__init__(name='stats', guild_only=True)
            self.cog: UserCommandsCog = parent_cog

        # ---------------------------------------------------------------------------------------------------------------

        @app_commands.command(description='Показывает статистику игры в слова на сервере')
        async def server(self, interaction: Interaction, game_mode: GameMode = GameMode.NORMAL) -> None:
            """Command to show the stats of the server"""
            await interaction.response.defer()

            guild = interaction.guild
            if guild is None:
                return

            await self.cog.common.ensure_config(guild)
            config: ServerConfig = self.cog.common.server_configs[guild.id]

            if config.game_state[game_mode].channel_id is None:  # channel not set yet
                await interaction.followup.send("Канал для игры ещё не настроен!")
                return

            server_stats_embed = Embed(
                description=f'''Game Mode: {'Normal' if game_mode == GameMode.NORMAL else 'Hard'}
Current Chain Length: {config.game_state[game_mode].current_count}
Longest chain length: {config.game_state[game_mode].high_score}
{f"**Last word:** {config.game_state[game_mode].current_word}" if config.game_state[game_mode].current_word else ""}
{f"Last word by: <@{config.game_state[game_mode].last_member_id}>" if config.game_state[game_mode].last_member_id else ""}''',
                colour=Colour.blurple()
            )
            server_stats_embed.set_author(name=guild,
                                          icon_url=guild.icon if guild.icon else None)

            await interaction.followup.send(embed=server_stats_embed)

        # ---------------------------------------------------------------------------------------------------------------

        @app_commands.command(description='Показывает статистику игрока в игре в слова')
        @app_commands.describe(member="Игрок, чью статистику показать")
        async def user(self, interaction: Interaction, member: Optional[discord.Member]) -> None:
            """Command to show the stats of a specific user"""
            await interaction.response.defer()

            if member is not None:
                scope_member = member
            elif isinstance(interaction.user, discord.Member):
                scope_member = interaction.user
            else:
                return

            def get_member_avatar() -> Optional[discord.Asset]:
                if scope_member.avatar:
                    return scope_member.avatar
                elif scope_member.display_avatar:
                    return scope_member.display_avatar
                else:
                    return None

            async with self.cog.bot.db_connection(locked=False) as connection:
                stmt = select(MemberModel).where(
                    MemberModel.server_id == scope_member.guild.id,
                    MemberModel.member_id == scope_member.id
                )
                result: CursorResult = await connection.execute(stmt)
                row = result.fetchone()

                if row is None:
                    if member is None:
                        await interaction.followup.send('Вы ещё не играли на этом сервере!')
                    else:
                        await interaction.followup.send(f'{member} ещё не играл на этом сервере!')
                    return

                db_member = Member.model_validate(row)

                stmt = select(count(MemberModel.member_id)).where(
                    MemberModel.server_id == scope_member.guild.id,
                    MemberModel.score >= db_member.score
                )
                result: CursorResult = await connection.execute(stmt)
                pos_by_score = result.scalar()

                stmt = select(count(MemberModel.member_id)).where(
                    MemberModel.server_id == scope_member.guild.id,
                    MemberModel.karma >= db_member.karma
                )
                result: CursorResult = await connection.execute(stmt)
                pos_by_karma = result.scalar()

                emb = discord.Embed(
                    color=discord.Color.blue(),
                    description=f'''**Очки:** {db_member.score} (#{pos_by_score})
**🌟Karma:** {db_member.karma:.2f} (#{pos_by_karma})
**✅Правильных:** {db_member.correct}
**❌Ошибок:** {db_member.wrong}
**Точность:** {(db_member.correct / (db_member.correct + db_member.wrong)):.2%}'''
                ).set_author(name=f"{scope_member} | stats", icon_url=get_member_avatar())

                await interaction.followup.send(embed=emb)


# ===================================================================================================================


async def setup(bot: WordChainBot):
    await bot.add_cog(UserCommandsCog(bot))
