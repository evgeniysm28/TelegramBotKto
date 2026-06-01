#!/usr/bin/env python3
"""
Telegram бот: гра "Хто я?"
Команди:
  /newgame  — створити гру в груповому чаті
  /endgame  — завершити гру (тільки організатор)
  /help     — правила

Правила:
  • Кожен гравець надсилає боту в особисті слово/персонажа
  • Слова рандомно призначаються іншим гравцям
  • Кожен гравець знає ЧУЖІ слова, але не своє
  • За хід: до 3 питань (відповідають інші гравці в чаті),
    потім — вгадати або пропустити
  • Вгадав правильно → виходить переможцем з гри
  • Вгадав неправильно → нічого, хід далі
"""

import random
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── ЗАМІНИТИ НА СВІЙ ТОКЕН ────────────────────────────────────────────────
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
# ──────────────────────────────────────────────────────────────────────────

# Сховища стану
games: dict[int, "Game"] = {}          # chat_id -> Game
user_to_chat: dict[int, int] = {}       # user_id -> chat_id  (для роутингу приватних повідомлень)


# ══════════════════════════════════════════════════════════════════════════════
#  Клас гри
# ══════════════════════════════════════════════════════════════════════════════

class Game:
    MAX_QUESTIONS = 3

    def __init__(self, chat_id: int, creator_id: int, creator_name: str):
        self.chat_id      = chat_id
        self.creator_id   = creator_id
        # Стани: lobby | collecting | playing | finished
        self.state        = "lobby"
        # uid -> {"name": str, "word": str|None, "eliminated": bool, "guesses": int}
        self.players: dict[int, dict] = {}
        # Слова, що надіслали гравці (для подальшого розподілу)
        self.submitted: dict[int, str] = {}
        self.turn_order: list[int]  = []
        self.turn_idx: int          = 0
        self.q_left: int            = self.MAX_QUESTIONS
        # Що очікуємо від поточного гравця в особистих: None | "question" | "guess"
        self.waiting_for: str | None = None

        self._add_player(creator_id, creator_name)

    # ── Гравці ────────────────────────────────────────────────────────────

    def _add_player(self, uid: int, name: str):
        self.players[uid] = {
            "name": name,
            "word": None,
            "eliminated": False,
            "guesses": 0,
        }

    def join(self, uid: int, name: str) -> bool:
        """Повертає True якщо вдалось приєднатись."""
        if uid in self.players:
            return False
        self._add_player(uid, name)
        return True

    def player_names_list(self) -> str:
        names = [p["name"] for p in self.players.values()]
        return ", ".join(names)

    def active_ids(self) -> list[int]:
        return [uid for uid, p in self.players.items() if not p["eliminated"]]

    # ── Поточний хід ──────────────────────────────────────────────────────

    @property
    def cur_id(self) -> int:
        return self.turn_order[self.turn_idx]

    @property
    def cur_name(self) -> str:
        return self.players[self.cur_id]["name"]

    # ── Розподіл слів ─────────────────────────────────────────────────────

    def assign_words(self):
        """Рандомно розподіляє слова між гравцями (ніхто не отримує своє)."""
        uids  = list(self.players.keys())
        words = [self.submitted[uid] for uid in uids]

        # Деранжування — ніхто не отримує власне слово
        shuffled = _derange(words)
        for uid, word in zip(uids, shuffled):
            self.players[uid]["word"] = word

        self.turn_order = uids[:]
        random.shuffle(self.turn_order)
        self.turn_idx    = 0
        self.q_left      = self.MAX_QUESTIONS
        self.waiting_for = None

    # ── Наступний хід ─────────────────────────────────────────────────────

    def advance(self) -> bool:
        """Переходить до наступного невилученого гравця.
        Повертає False якщо залишився 1 або 0 активних → кінець гри."""
        if len(self.active_ids()) <= 1:
            return False

        self.turn_idx    = (self.turn_idx + 1) % len(self.turn_order)
        self.q_left      = self.MAX_QUESTIONS
        self.waiting_for = None

        # Пропускаємо вилучених
        safety = 0
        while self.players[self.turn_order[self.turn_idx]]["eliminated"]:
            self.turn_idx = (self.turn_idx + 1) % len(self.turn_order)
            safety += 1
            if safety > len(self.turn_order):
                return False

        return True


# ══════════════════════════════════════════════════════════════════════════════
#  Допоміжні функції
# ══════════════════════════════════════════════════════════════════════════════

def _derange(lst: list) -> list:
    """Деранжування списку (жоден елемент не залишається на своєму місці)."""
    if len(lst) < 2:
        return lst[:]
    for _ in range(1000):
        shuffled = lst[:]
        random.shuffle(shuffled)
        if all(shuffled[i] != lst[i] for i in range(len(lst))):
            return shuffled
    # Fallback: циклічний зсув
    return lst[1:] + lst[:1]


def build_lobby_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✋ Приєднатись", callback_data=f"join|{chat_id}"),
        InlineKeyboardButton("▶️ Старт",       callback_data=f"start|{chat_id}"),
    ]])


def build_turn_keyboard(game: Game) -> InlineKeyboardMarkup:
    rows = []
    if game.q_left > 0:
        rows.append([InlineKeyboardButton(
            f"❓ Задати питання  ({game.q_left} із {Game.MAX_QUESTIONS})",
            callback_data=f"ask|{game.chat_id}"
        )])
    rows.append([
        InlineKeyboardButton("🎯 Вгадати слово",  callback_data=f"guess|{game.chat_id}"),
        InlineKeyboardButton("⏭ Пропустити хід", callback_data=f"skip|{game.chat_id}"),
    ])
    rows.append([InlineKeyboardButton(
        "🏁 Завершити гру",
        callback_data=f"endgame|{game.chat_id}"
    )])
    return InlineKeyboardMarkup(rows)


def build_player_info(game: Game, uid: int) -> str:
    """Текст для особистих: хто є хто (крім самого гравця)."""
    lines = ["📋 *Хто є хто (крім тебе):*\n"]
    for pid, p in game.players.items():
        if pid == uid:
            continue
        if p["eliminated"]:
            lines.append(f"✅ {p['name']} — ~~вгадано~~ (*{p['word']}*)")
        else:
            lines.append(f"• {p['name']} — *{p['word']}*")
    lines.append("\n🔒 *Твоє слово* — загадка тільки для тебе 😄")
    return "\n".join(lines)


async def announce_turn(bot, game: Game):
    """Оголошує чий зараз хід і показує кнопки дій."""
    active = game.active_ids()
    emoji_bar = "🟢" * len(active) + "⚫" * (len(game.players) - len(active))
    await bot.send_message(
        game.chat_id,
        f"🎯 Хід *{game.cur_name}*!\n\n"
        f"Гравців залишилось: {emoji_bar}",
        parse_mode="Markdown",
        reply_markup=build_turn_keyboard(game),
    )


async def finish_game(bot, game: Game, reason: str):
    """Завершує гру і показує всі слова."""
    game.state = "finished"
    reveal_lines = [f"• {p['name']} — *{p['word']}*" for p in game.players.values()]
    reveal = "\n".join(reveal_lines)
    await bot.send_message(
        game.chat_id,
        f"🏁 *{reason}*\n\n"
        f"📋 *Всі слова були:*\n{reveal}",
        parse_mode="Markdown",
    )
    # Прибираємо гру
    games.pop(game.chat_id, None)
    for uid in game.players:
        user_to_chat.pop(uid, None)


# ══════════════════════════════════════════════════════════════════════════════
#  Команди
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎮 *Гра «Хто я?»*\n\n"
        "*Як грати:*\n"
        "1️⃣ `/newgame` — створити гру в груповому чаті\n"
        "2️⃣ Всі натискають *Приєднатись*\n"
        "3️⃣ Організатор натискає *Старт*\n"
        "4️⃣ Кожен надсилає боту *в особисті* слово або персонажа\n"
        "5️⃣ Слова рандомно розподіляються — ти не знаєш своє!\n\n"
        "*За хід:*\n"
        "❓ Задай до 3 питань (інші відповідають у чаті)\n"
        "🎯 Вгадай своє слово — якщо правильно, виходиш переможцем\n"
        "⏭ Або пропусти хід\n\n"
        "❌ Неправильна відповідь — нічого не відбувається, хід далі\n"
        "🏁 `/endgame` — завершити примусово (тільки організатор)"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_newgame(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("⚠️ Ця команда тільки для групових чатів!")
        return

    existing = games.get(chat.id)
    if existing and existing.state != "finished":
        await update.message.reply_text(
            "⚠️ Гра вже йде! Організатор може зупинити її командою /endgame."
        )
        return

    game = Game(chat.id, user.id, user.first_name)
    games[chat.id] = game
    user_to_chat[user.id] = chat.id

    await update.message.reply_text(
        f"🎮 *Нова гра «Хто я?»!*\n\n"
        f"👑 Організатор: {user.first_name}\n"
        f"👥 Гравці: {user.first_name}\n\n"
        f"Натискайте *Приєднатись*, потім організатор натисне *Старт*.\n"
        f"_(мінімум 2 гравці)_",
        parse_mode="Markdown",
        reply_markup=build_lobby_keyboard(chat.id),
    )


async def cmd_endgame(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    game = games.get(chat.id)

    if not game:
        await update.message.reply_text("⚠️ Активної гри немає.")
        return
    if user.id != game.creator_id:
        await update.message.reply_text("❌ Тільки організатор може завершити гру.")
        return

    await finish_game(ctx.bot, game, "Гру завершено організатором!")


# ══════════════════════════════════════════════════════════════════════════════
#  Обробник кнопок (Inline Keyboard)
# ══════════════════════════════════════════════════════════════════════════════

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    user = q.from_user
    data = q.data

    action, chat_id_str = data.split("|", 1)
    chat_id = int(chat_id_str)
    game    = games.get(chat_id)

    await q.answer()

    # ── JOIN ──────────────────────────────────────────────────────────────
    if action == "join":
        if not game or game.state != "lobby":
            await q.answer("❌ Лобі вже закрите.", show_alert=True)
            return
        joined = game.join(user.id, user.first_name)
        if not joined:
            await q.answer("Ти вже в грі!", show_alert=True)
            return

        user_to_chat[user.id] = chat_id
        count = len(game.players)
        await q.edit_message_text(
            f"🎮 *Гра «Хто я?»*\n\n"
            f"👑 Організатор: {game.players[game.creator_id]['name']}\n"
            f"👥 Гравці ({count}): {game.player_names_list()}\n\n"
            f"Чекаємо всіх. Організатор натисне *Старт* коли готові.",
            parse_mode="Markdown",
            reply_markup=build_lobby_keyboard(chat_id),
        )

    # ── START ─────────────────────────────────────────────────────────────
    elif action == "start":
        if not game or game.state != "lobby":
            return
        if user.id != game.creator_id:
            await q.answer("❌ Тільки організатор може почати!", show_alert=True)
            return
        if len(game.players) < 2:
            await q.answer("❌ Потрібно мінімум 2 гравці!", show_alert=True)
            return

        game.state = "collecting"
        await q.edit_message_text(
            f"✅ *Гравці ({len(game.players)}):* {game.player_names_list()}\n\n"
            f"📩 Тепер кожен гравець напишіть боту *в особисті повідомлення* "
            f"слово, персонажа або відому особистість!\n\n"
            f"_Це слово отримає хтось із інших гравців._",
            parse_mode="Markdown",
        )
        # Повідомляємо кожного в особисті
        failed = []
        for uid in game.players:
            try:
                await ctx.bot.send_message(
                    uid,
                    "📝 *Надішли слово для гри!*\n\n"
                    "Напиши слово, персонажа або відому особистість.\n"
                    "Наприклад: _Гаррі Поттер_, _кіт_, _Наполеон_, _холодильник_ 😄\n\n"
                    "Це отримає хтось інший!",
                    parse_mode="Markdown",
                )
            except Exception:
                failed.append(game.players[uid]["name"])

        if failed:
            await ctx.bot.send_message(
                chat_id,
                f"⚠️ Не вдалось написати в особисті: {', '.join(failed)}\n"
                f"Ці гравці мають спочатку *запустити бота* (@{ctx.bot.username}).",
                parse_mode="Markdown",
            )

    # ── ASK ───────────────────────────────────────────────────────────────
    elif action == "ask":
        if not game or game.state != "playing":
            return
        if user.id != game.cur_id:
            await q.answer("⛔ Зараз не твій хід!", show_alert=True)
            return
        if game.q_left <= 0:
            await q.answer("❌ Питання на цей хід закінчились!", show_alert=True)
            return

        game.waiting_for = "question"
        try:
            await ctx.bot.send_message(
                user.id,
                f"❓ Напиши своє питання — я перешлю його в групу.\n"
                f"_(Залишилось питань: {game.q_left})_",
                parse_mode="Markdown",
            )
            await q.answer("✅ Напиши питання в особистих!")
        except Exception:
            game.waiting_for = None
            await q.answer("❌ Спочатку запусти бота в особистих!", show_alert=True)

    # ── GUESS ─────────────────────────────────────────────────────────────
    elif action == "guess":
        if not game or game.state != "playing":
            return
        if user.id != game.cur_id:
            await q.answer("⛔ Зараз не твій хід!", show_alert=True)
            return

        game.waiting_for = "guess"
        try:
            await ctx.bot.send_message(
                user.id,
                "🎯 *Хто ти?* Напиши своє слово або персонажа!",
                parse_mode="Markdown",
            )
            await q.answer("✅ Напиши відповідь в особистих!")
        except Exception:
            game.waiting_for = None
            await q.answer("❌ Спочатку запусти бота в особистих!", show_alert=True)

    # ── SKIP ──────────────────────────────────────────────────────────────
    elif action == "skip":
        if not game or game.state != "playing":
            return
        if user.id != game.cur_id:
            await q.answer("⛔ Зараз не твій хід!", show_alert=True)
            return

        name = game.cur_name
        still = game.advance()
        await ctx.bot.send_message(
            chat_id,
            f"⏭ *{name}* пропустив хід.",
            parse_mode="Markdown",
        )
        if still:
            await announce_turn(ctx.bot, game)
        else:
            await finish_game(ctx.bot, game, "Гра закінчена!")

    # ── END GAME (кнопка) ─────────────────────────────────────────────────
    elif action == "endgame":
        if not game:
            return
        if user.id != game.creator_id:
            await q.answer("❌ Тільки організатор може завершити гру!", show_alert=True)
            return
        await finish_game(ctx.bot, game, "Гру завершено організатором!")


# ══════════════════════════════════════════════════════════════════════════════
#  Обробник приватних повідомлень
# ══════════════════════════════════════════════════════════════════════════════

async def on_private_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()

    chat_id = user_to_chat.get(user.id)
    if not chat_id:
        await update.message.reply_text(
            "⚠️ Ти не в жодній активній грі.\n"
            "Приєднайся до гри в груповому чаті!"
        )
        return

    game = games.get(chat_id)
    if not game:
        await update.message.reply_text("⚠️ Гра вже закінчилась або не знайдена.")
        user_to_chat.pop(user.id, None)
        return

    # ── Збір слів ─────────────────────────────────────────────────────────
    if game.state == "collecting":
        if user.id not in game.players:
            await update.message.reply_text("⚠️ Ти не в цій грі!")
            return
        if user.id in game.submitted:
            await update.message.reply_text("✅ Ти вже надіслав слово! Чекай на інших.")
            return

        game.submitted[user.id] = text
        submitted_count = len(game.submitted)
        total           = len(game.players)
        await update.message.reply_text(
            f"✅ Прийнято: *{text}*\n\n"
            f"Надіслали {submitted_count}/{total} гравців. Чекаємо...",
            parse_mode="Markdown",
        )
        await ctx.bot.send_message(
            chat_id,
            f"⏳ Слово надіслано ({submitted_count}/{total})...",
        )

        # Всі надіслали — починаємо
        if submitted_count == total:
            game.assign_words()
            game.state = "playing"

            # Кожному — його інформаційне повідомлення
            for uid in game.players:
                try:
                    await ctx.bot.send_message(
                        uid,
                        build_player_info(game, uid),
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

            await ctx.bot.send_message(
                chat_id,
                "🎲 *Всі слова надіслані та розподілені!*\n\n"
                "Кожен отримав в особисті *хто є хто* — але не знає своє слово 😄\n\n"
                "*Правила:*\n"
                "❓ Задавай питання (інші відповідають: так / ні)\n"
                "🎯 Вгадай своє слово\n"
                "⏭ Або пропусти хід\n"
                "✅ Вгадав — виходиш переможцем!\n"
                "❌ Помилка — нічого, хід далі",
                parse_mode="Markdown",
            )
            await announce_turn(ctx.bot, game)

    # ── Під час гри ────────────────────────────────────────────────────────
    elif game.state == "playing":
        if user.id != game.cur_id:
            await update.message.reply_text("⛔ Зараз не твій хід!")
            return

        # ── Питання ──────────────────────────────────────────────────────
        if game.waiting_for == "question":
            game.waiting_for = None
            game.q_left -= 1

            await ctx.bot.send_message(
                chat_id,
                f"❓ *{user.first_name}* питає:\n\n_{text}_",
                parse_mode="Markdown",
            )
            await update.message.reply_text(
                f"✅ Питання відправлено в чат!\n"
                f"Залишилось питань: {game.q_left}",
            )
            # Оновлюємо кнопки в чаті
            await ctx.bot.send_message(
                chat_id,
                f"💬 Гравці, відповідайте! Потім *{game.cur_name}* обирає наступну дію:",
                parse_mode="Markdown",
                reply_markup=build_turn_keyboard(game),
            )

        # ── Відгадування ──────────────────────────────────────────────────
        elif game.waiting_for == "guess":
            game.waiting_for = None
            correct = game.players[user.id]["word"]

            if text.strip().lower() == correct.strip().lower():
                # ✅ Правильно!
                game.players[user.id]["eliminated"] = True

                await update.message.reply_text(
                    f"🎉 *ПРАВИЛЬНО!*\n\nТвоє слово — *{correct}*!\n\nТи вийшов переможцем! 🏆",
                    parse_mode="Markdown",
                )
                await ctx.bot.send_message(
                    chat_id,
                    f"🎉 *{user.first_name}* вгадав своє слово: *{correct}*!\n"
                    f"Виходить з гри переможцем! 🏆",
                    parse_mode="Markdown",
                )

                active = game.active_ids()
                if len(active) == 0:
                    await finish_game(ctx.bot, game, "Всі вгадали свої слова! Браво всім! 🎊")
                    return
                elif len(active) == 1:
                    last_uid  = active[0]
                    last_name = game.players[last_uid]["name"]
                    last_word = game.players[last_uid]["word"]
                    await finish_game(
                        ctx.bot, game,
                        f"Гра закінчена! *{last_name}* залишився останнім зі словом *{last_word}* 😅"
                    )
                    return

                still = game.advance()
                if still:
                    await announce_turn(ctx.bot, game)
                else:
                    await finish_game(ctx.bot, game, "Гра закінчена!")

            else:
                # ❌ Неправильно
                game.players[user.id]["guesses"] = game.players[user.id].get("guesses", 0) + 1

                await update.message.reply_text(
                    f"❌ Неправильно! Спробуй ще раз наступного ходу.",
                )
                await ctx.bot.send_message(
                    chat_id,
                    f"❌ *{user.first_name}* не вгадав. Хід переходить далі.",
                    parse_mode="Markdown",
                )

                still = game.advance()
                if still:
                    await announce_turn(ctx.bot, game)
                else:
                    await finish_game(ctx.bot, game, "Гра закінчена!")

        else:
            await update.message.reply_text(
                "💡 Натисни кнопку в груповому чаті щоб обрати дію!"
            )


# ══════════════════════════════════════════════════════════════════════════════
#  Запуск
# ══════════════════════════════════════════════════════════════════════════════

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Заміни BOT_TOKEN на справжній токен від @BotFather!")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("start",   cmd_help))   # /start в особистих
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("endgame", cmd_endgame))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
        on_private_message,
    ))

    print("🤖 Бот запущено! Ctrl+C для зупинки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
