"""
Telegram Group Broadcaster — Bot Version
-----------------------------------------
A userbot-powered broadcaster controlled via a Telegram bot with inline buttons.

How it works:
  • A Telethon USERBOT (your account) does the actual broadcasting
  • A python-telegram-bot BOT acts as the control panel via inline buttons
  • You chat with your bot from any device to control broadcasting
  • A private LOG CHANNEL is auto-created to track every message sent

Requirements:
    pip install telethon python-telegram-bot

Setup:
  1. Get API_ID and API_HASH from https://my.telegram.org/apps
  2. Create a bot via @BotFather and get BOT_TOKEN
  3. Fill in your OWNER_ID (your Telegram user ID — get it from @userinfobot)
  4. Run: python telegram_broadcaster_bot.py
"""

import asyncio
import logging
import threading
from telethon import TelegramClient
from telethon.tl.types import Chat, Channel, ChatForbidden, ChannelForbidden
from telethon.tl.functions.channels import CreateChannelRequest, InviteToChannelRequest, ExportInviteRequest
from telethon.errors import (
    FloodWaitError, ChatWriteForbiddenError,
    UserBannedInChannelError, ChannelPrivateError
)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters, ConversationHandler
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.WARNING
)

# ══════════════════════════════════════════════
#  ★  FILL THESE IN BEFORE RUNNING  ★
# ══════════════════════════════════════════════
API_ID       = 21752358          # From https://my.telegram.org/apps  e.g. 1234567
API_HASH     = "fb46a136fed4a4de27ab057c7027fec3"          # e.g. "abcdef1234567890abcdef1234567890"
BOT_TOKEN    = 8628015085:AAHzJx-6NvaHYAlFx-b1gfS0SDU0wFpEO68          # From @BotFather  e.g. "123456:ABCdef..."
OWNER_ID     = 1899208318          # Your Telegram user ID (integer)  e.g. 987654321
SESSION_NAME = "broadcaster_userbot"
# ══════════════════════════════════════════════

# ── Conversation states ───────────────────────
(
    ST_MAIN,
    ST_AWAIT_MODE,
    ST_AWAIT_TEXT,
    ST_AWAIT_FWD_SOURCE,
    ST_AWAIT_FWD_MSGID,
    ST_AWAIT_SEND_INTERVAL,
    ST_AWAIT_ROUND_INTERVAL,
) = range(7)

# ── Shared state ─────────────────────────────
state = {
    "mode":           None,    # "text" | "forward"
    "text":           None,
    "fwd_source":     None,
    "fwd_source_ent": None,
    "fwd_msg_id":     None,
    "send_interval":  5.0,
    "round_interval": 3600.0,
    "running":        False,
    "round_num":      0,
    "last_success":   0,
    "last_failed":    0,
    "groups":         [],
    "broadcast_task": None,
    # ── Logging channel ──────────────────────
    "log_channel_id":   None,   # Telethon entity id of the log channel
    "log_channel_link": None,   # Invite link shared with admin
}

userbot: TelegramClient = None
bot_loop: asyncio.AbstractEventLoop = None


# ══════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════

def fmt_seconds(secs: float) -> str:
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    if h:   return f"{h}h {m}m {s}s"
    if m:   return f"{m}m {s}s"
    return f"{s}s"


def owner_only(func):
    """Decorator — ignore messages from anyone except the owner."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = (update.effective_user or update.callback_query.from_user).id
        if uid != OWNER_ID:
            return
        return await func(update, ctx)
    return wrapper


def main_menu_keyboard():
    running = state["running"]
    groups  = len(state["groups"])
    mode    = state["mode"] or "not set"
    si      = state["send_interval"]
    ri      = fmt_seconds(state["round_interval"])

    toggle_label = "⏹ Stop Broadcast" if running else "▶️ Start Broadcast"
    log_label    = "📋 Log Channel ✅" if state["log_channel_id"] else "📋 Setup Log Channel"

    keyboard = [
        [InlineKeyboardButton("📝 Set Text Message",    callback_data="set_text"),
         InlineKeyboardButton("↪️ Set Forward Message", callback_data="set_forward")],
        [InlineKeyboardButton(f"⏱ Send Interval: {si}s",         callback_data="set_send_interval"),
         InlineKeyboardButton(f"🔄 Round Interval: {ri}",        callback_data="set_round_interval")],
        [InlineKeyboardButton(f"🔍 Refresh Groups ({groups})",    callback_data="refresh_groups")],
        [InlineKeyboardButton(toggle_label,                        callback_data="toggle_broadcast")],
        [InlineKeyboardButton("📊 Status",                         callback_data="status"),
         InlineKeyboardButton(log_label,                           callback_data="setup_log")],
    ]
    return InlineKeyboardMarkup(keyboard)


def main_menu_text():
    running = state["running"]
    mode    = state["mode"] or "❌ Not set"
    si      = state["send_interval"]
    ri      = fmt_seconds(state["round_interval"])
    groups  = len(state["groups"])
    rnd     = state["round_num"]
    ok      = state["last_success"]
    fail    = state["last_failed"]

    status = "🟢 *RUNNING*" if running else "🔴 *STOPPED*"

    msg_preview = ""
    if state["mode"] == "text" and state["text"]:
        preview = state["text"][:60].replace("*","\\*").replace("_","\\_")
        msg_preview = f"\n📝 *Message:* `{preview}{'...' if len(state['text'])>60 else ''}`"
    elif state["mode"] == "forward" and state["fwd_source"]:
        msg_preview = f"\n↪️ *Forward from:* `{state['fwd_source']}` msg `#{state['fwd_msg_id']}`"

    log_status = f"[📋 Log channel active]({state['log_channel_link']})" \
                 if state["log_channel_link"] else "📋 Log channel: ❌ not set up"

    return (
        f"🤖 *Telegram Group Broadcaster*\n"
        f"{'─'*30}\n"
        f"Status : {status}\n"
        f"Mode   : *{mode}*{msg_preview}\n"
        f"Groups : *{groups}*\n"
        f"⏱ Send interval  : *{si}s*\n"
        f"🔄 Round interval : *{ri}*\n"
        f"{'─'*30}\n"
        f"Rounds completed : *{rnd}*\n"
        f"Last round       : ✅ {ok}  ❌ {fail}\n"
        f"{'─'*30}\n"
        f"{log_status}\n"
    )


# ══════════════════════════════════════════════
#  Logging Channel helpers
# ══════════════════════════════════════════════

async def setup_log_channel() -> str:
    """
    Creates a private Telegram channel named '📋 Broadcaster Logs'
    using the userbot, then returns a permanent invite link.
    If a log channel was already created in this session, returns its existing link.
    """
    if state["log_channel_id"] and state["log_channel_link"]:
        return state["log_channel_link"]

    # Create private channel
    result = await userbot(CreateChannelRequest(
        title="📋 Broadcaster Logs",
        about="Auto-generated log channel for Telegram Broadcaster Bot.",
        megagroup=False,   # False = broadcast channel (admins only can post)
    ))
    channel = result.chats[0]
    state["log_channel_id"] = channel.id

    # Generate permanent invite link
    invite = await userbot(ExportInviteRequest(channel))
    state["log_channel_link"] = invite.link

    return invite.link


async def log_to_channel(group_name: str, group_id: int, sent_msg_id: int, message_text: str):
    """
    Posts a log entry to the log channel.
    Includes: group name, direct message link, and message preview.
    """
    if not state["log_channel_id"]:
        return

    # Build a t.me link. Works for public groups; for private groups it uses
    # the internal link format (best effort).
    try:
        # Positive channel IDs need the -100 prefix stripped for links
        link_id = str(group_id)
        if link_id.startswith("-100"):
            link_id = link_id[4:]
        msg_link = f"https://t.me/c/{link_id}/{sent_msg_id}"
    except Exception:
        msg_link = None

    preview = (message_text or "")[:200]
    if len(message_text or "") > 200:
        preview += "…"

    link_part = f"\n🔗 [View message]({msg_link})" if msg_link else ""

    log_text = (
        f"📨 *Message Sent*\n"
        f"{'─'*28}\n"
        f"🏘 *Group:* `{group_name}`\n"
        f"🆔 *Group ID:* `{group_id}`{link_part}\n"
        f"{'─'*28}\n"
        f"📝 *Content:*\n`{preview}`"
    )

    try:
        await userbot.send_message(
            state["log_channel_id"],
            log_text,
            parse_mode="md",
            link_preview=False,
        )
    except Exception as e:
        logging.warning(f"[LogChannel] Failed to post log: {e}")


# ══════════════════════════════════════════════
#  Userbot helpers
# ══════════════════════════════════════════════

async def fetch_groups():
    groups = []
    async for dialog in userbot.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, (ChatForbidden, ChannelForbidden)):
            continue
        if isinstance(entity, (Chat, Channel)):
            groups.append(dialog)
    state["groups"] = groups
    return groups


async def resolve_source(source: str):
    source_parsed = int(source) if source.lstrip("-").isdigit() else source
    try:
        return await userbot.get_entity(source_parsed)
    except Exception:
        target_id = abs(int(source)) if str(source).lstrip("-").isdigit() else None
        async for dialog in userbot.iter_dialogs():
            did = dialog.entity.id
            uname = getattr(dialog.entity, "username", None)
            if (target_id and did == target_id) or \
               (uname and uname.lower() == str(source).lstrip("@").lower()):
                return dialog.entity
        return None


async def do_broadcast(send_msg_func):
    """Core broadcast loop — runs in the userbot event loop."""
    groups = state["groups"]
    total  = len(groups)

    while state["running"]:
        state["round_num"] += 1
        success = 0
        failed  = 0

        await send_msg_func(
            f"📣 *Round {state['round_num']} started* — {total} group(s)"
        )

        for idx, dialog in enumerate(groups, 1):
            if not state["running"]:
                break
            name    = dialog.name or str(dialog.id)
            ent_id  = dialog.entity.id

            sent_msg = None
            try:
                if state["mode"] == "text":
                    sent_msg = await userbot.send_message(dialog.entity, state["text"])
                else:
                    fwd_msgs = await userbot.forward_messages(
                        dialog.entity,
                        state["fwd_msg_id"],
                        state["fwd_source_ent"]
                    )
                    # forward_messages returns a list; grab first
                    sent_msg = fwd_msgs[0] if isinstance(fwd_msgs, list) else fwd_msgs
                success += 1

                # ── Log the successful send ────────────────────
                msg_text = state["text"] if state["mode"] == "text" else \
                           f"[Forwarded message #{state['fwd_msg_id']} from {state['fwd_source']}]"
                msg_id   = getattr(sent_msg, "id", None)
                await log_to_channel(name, ent_id, msg_id, msg_text)

            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
                try:
                    if state["mode"] == "text":
                        sent_msg = await userbot.send_message(dialog.entity, state["text"])
                    else:
                        fwd_msgs = await userbot.forward_messages(
                            dialog.entity,
                            state["fwd_msg_id"],
                            state["fwd_source_ent"]
                        )
                        sent_msg = fwd_msgs[0] if isinstance(fwd_msgs, list) else fwd_msgs
                    success += 1
                    msg_text = state["text"] if state["mode"] == "text" else \
                               f"[Forwarded message #{state['fwd_msg_id']} from {state['fwd_source']}]"
                    msg_id   = getattr(sent_msg, "id", None)
                    await log_to_channel(name, ent_id, msg_id, msg_text)
                except Exception:
                    failed += 1
            except (ChatWriteForbiddenError, UserBannedInChannelError, ChannelPrivateError):
                failed += 1
            except Exception:
                failed += 1

            if idx < total and state["running"]:
                await asyncio.sleep(state["send_interval"])

        state["last_success"] = success
        state["last_failed"]  = failed

        await send_msg_func(
            f"🏁 *Round {state['round_num']} done*\n"
            f"✅ {success} sent   ❌ {failed} failed\n"
            f"💤 Sleeping *{fmt_seconds(state['round_interval'])}* before next round…"
        )

        # Round interval sleep (interruptible)
        elapsed = 0
        while elapsed < state["round_interval"] and state["running"]:
            await asyncio.sleep(1)
            elapsed += 1

    await send_msg_func("⏹ *Broadcast stopped.*")


# ══════════════════════════════════════════════
#  Bot handlers
# ══════════════════════════════════════════════

@owner_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        main_menu_text(),
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )
    return ST_MAIN


@owner_only
async def btn_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    # ── Refresh groups ────────────────────────
    if data == "refresh_groups":
        future = asyncio.run_coroutine_threadsafe(fetch_groups(), userbot.loop)
        groups = future.result(timeout=30)
        await query.edit_message_text(
            main_menu_text() + f"\n✅ *Refreshed:* {len(groups)} groups found.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
        return ST_MAIN

    # ── Status ────────────────────────────────
    if data == "status":
        await query.edit_message_text(
            main_menu_text(),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
        return ST_MAIN

    # ── Setup / show log channel ──────────────
    if data == "setup_log":
        await query.answer("⏳ Setting up log channel…", show_alert=False)
        future = asyncio.run_coroutine_threadsafe(setup_log_channel(), userbot.loop)
        try:
            link = future.result(timeout=30)
            text = (
                f"📋 *Log Channel Ready!*\n\n"
                f"Your broadcast log channel has been created.\n"
                f"Every message sent by the bot will be logged there "
                f"with the group name, a direct message link, and the content.\n\n"
                f"👉 [Open Log Channel]({link})\n\n"
                f"_(Bookmark this link — logs appear in real time)_"
            )
        except Exception as e:
            text = f"❌ *Failed to create log channel:*\n`{e}`"

        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("« Back to Menu", callback_data="back_main")]
            ])
        )
        return ST_MAIN

    # ── Toggle broadcast ──────────────────────
    if data == "toggle_broadcast":
        if state["running"]:
            state["running"] = False
            await query.edit_message_text(
                main_menu_text() + "\n⏹ *Stopping…* (finishes current send)",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard()
            )
        else:
            # Validation
            if not state["groups"]:
                await query.answer("⚠️ No groups loaded! Tap 'Refresh Groups' first.", show_alert=True)
                return ST_MAIN
            if not state["mode"]:
                await query.answer("⚠️ Set a message mode first (Text or Forward).", show_alert=True)
                return ST_MAIN
            if state["mode"] == "text" and not state["text"]:
                await query.answer("⚠️ No text message set!", show_alert=True)
                return ST_MAIN
            if state["mode"] == "forward" and not state["fwd_source_ent"]:
                await query.answer("⚠️ Forward source not set!", show_alert=True)
                return ST_MAIN

            state["running"]   = True
            state["round_num"] = 0
            chat_id = query.message.chat_id

            async def send_msg_func(text):
                try:
                    await ctx.bot.send_message(chat_id, text, parse_mode="Markdown")
                except Exception:
                    pass

            def run_broadcast():
                asyncio.run_coroutine_threadsafe(
                    do_broadcast(send_msg_func), userbot.loop
                )

            t = threading.Thread(target=run_broadcast, daemon=True)
            t.start()

            await query.edit_message_text(
                main_menu_text() + "\n🟢 *Broadcast started!*",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard()
            )
        return ST_MAIN

    # ── Set text mode ─────────────────────────
    if data == "set_text":
        await query.edit_message_text(
            "📝 *Send me your message text.*\n\nJust type and send it as a normal message.\nSupports multiple lines.",
            parse_mode="Markdown"
        )
        return ST_AWAIT_TEXT

    # ── Set forward mode ──────────────────────
    if data == "set_forward":
        await query.edit_message_text(
            "↪️ *Forward Mode*\n\n"
            "Send me the *source chat* username or ID.\n\n"
            "Example: `@mychannel` or `-1001234567890`\n\n"
            "💡 Forward any message from the chat to @userinfobot to get its ID.",
            parse_mode="Markdown"
        )
        return ST_AWAIT_FWD_SOURCE

    # ── Set send interval ─────────────────────
    if data == "set_send_interval":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("3s",  callback_data="si_3"),
             InlineKeyboardButton("5s",  callback_data="si_5"),
             InlineKeyboardButton("10s", callback_data="si_10")],
            [InlineKeyboardButton("15s", callback_data="si_15"),
             InlineKeyboardButton("30s", callback_data="si_30"),
             InlineKeyboardButton("60s", callback_data="si_60")],
            [InlineKeyboardButton("✏️ Custom", callback_data="si_custom")],
            [InlineKeyboardButton("« Back",    callback_data="back_main")],
        ])
        await query.edit_message_text(
            f"⏱ *Send Interval*\nCurrent: *{state['send_interval']}s*\n\nChoose gap between each group send:",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return ST_MAIN

    # ── Set round interval ────────────────────
    if data == "set_round_interval":
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("5 min",  callback_data="ri_300"),
             InlineKeyboardButton("15 min", callback_data="ri_900"),
             InlineKeyboardButton("30 min", callback_data="ri_1800")],
            [InlineKeyboardButton("1 hour", callback_data="ri_3600"),
             InlineKeyboardButton("2 hour", callback_data="ri_7200"),
             InlineKeyboardButton("6 hour", callback_data="ri_21600")],
            [InlineKeyboardButton("✏️ Custom", callback_data="ri_custom")],
            [InlineKeyboardButton("« Back",    callback_data="back_main")],
        ])
        await query.edit_message_text(
            f"🔄 *Round Interval*\nCurrent: *{fmt_seconds(state['round_interval'])}*\n\nChoose sleep time after all groups are messaged:",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return ST_MAIN

    # ── Send interval presets ─────────────────
    if data.startswith("si_"):
        val = data[3:]
        if val == "custom":
            await query.edit_message_text(
                "⏱ Send me the send interval in *seconds* (just a number):",
                parse_mode="Markdown"
            )
            ctx.user_data["awaiting"] = "send_interval"
            return ST_AWAIT_SEND_INTERVAL
        state["send_interval"] = float(val)
        await query.edit_message_text(
            main_menu_text() + f"\n✅ Send interval set to *{val}s*",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
        return ST_MAIN

    # ── Round interval presets ────────────────
    if data.startswith("ri_"):
        val = data[3:]
        if val == "custom":
            await query.edit_message_text(
                "🔄 Send me the round interval in *seconds* (just a number):",
                parse_mode="Markdown"
            )
            ctx.user_data["awaiting"] = "round_interval"
            return ST_AWAIT_ROUND_INTERVAL
        state["round_interval"] = float(val)
        await query.edit_message_text(
            main_menu_text() + f"\n✅ Round interval set to *{fmt_seconds(float(val))}*",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
        return ST_MAIN

    # ── Back to main ──────────────────────────
    if data == "back_main":
        await query.edit_message_text(
            main_menu_text(),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
        return ST_MAIN

    return ST_MAIN


@owner_only
async def msg_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    awaiting = ctx.user_data.get("awaiting")

    # ── Custom send interval ──────────────────
    if awaiting == "send_interval":
        try:
            val = float(text.strip())
            state["send_interval"] = val
            ctx.user_data["awaiting"] = None
            await update.message.reply_text(
                main_menu_text() + f"\n✅ Send interval set to *{val}s*",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard()
            )
        except ValueError:
            await update.message.reply_text("❌ Please send a valid number.")
        return ST_MAIN

    # ── Custom round interval ─────────────────
    if awaiting == "round_interval":
        try:
            val = float(text.strip())
            state["round_interval"] = val
            ctx.user_data["awaiting"] = None
            await update.message.reply_text(
                main_menu_text() + f"\n✅ Round interval set to *{fmt_seconds(val)}*",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard()
            )
        except ValueError:
            await update.message.reply_text("❌ Please send a valid number.")
        return ST_MAIN

    return ST_MAIN


@owner_only
async def recv_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["mode"] = "text"
    state["text"] = update.message.text
    preview = state["text"][:80]
    await update.message.reply_text(
        f"✅ *Text message saved!*\n\nPreview:\n`{preview}{'...' if len(state['text'])>80 else ''}`",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )
    return ST_MAIN


@owner_only
async def recv_fwd_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    source = update.message.text.strip()
    await update.message.reply_text("⏳ Resolving chat…")

    future = asyncio.run_coroutine_threadsafe(resolve_source(source), userbot.loop)
    try:
        entity = future.result(timeout=15)
    except Exception:
        entity = None

    if not entity:
        await update.message.reply_text(
            "❌ *Could not resolve that chat.*\n\n"
            "Make sure:\n"
            "• Your userbot account is a member of that chat\n"
            "• You used `@username` or the correct numeric ID with `-100` prefix\n"
            "• Forward a message from the chat to @userinfobot to confirm the ID",
            parse_mode="Markdown"
        )
        return ST_AWAIT_FWD_SOURCE

    state["fwd_source"]     = source
    state["fwd_source_ent"] = entity
    await update.message.reply_text(
        f"✅ *Source chat found:* `{getattr(entity, 'title', source)}`\n\n"
        "Now send me the *Message ID* to forward.\n"
        "_(Right-click message → Copy Link → last number in URL)_",
        parse_mode="Markdown"
    )
    return ST_AWAIT_FWD_MSGID


@owner_only
async def recv_fwd_msgid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        msg_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Message ID must be a number. Try again:")
        return ST_AWAIT_FWD_MSGID

    state["mode"]       = "forward"
    state["fwd_msg_id"] = msg_id
    await update.message.reply_text(
        f"✅ *Forward message set!*\n"
        f"Source: `{state['fwd_source']}`\n"
        f"Message ID: `{msg_id}`",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )
    return ST_MAIN


@owner_only
async def recv_send_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(update.message.text.strip())
        state["send_interval"] = val
        await update.message.reply_text(
            main_menu_text() + f"\n✅ Send interval set to *{val}s*",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
        return ST_MAIN
    except ValueError:
        await update.message.reply_text("❌ Please send a valid number:")
        return ST_AWAIT_SEND_INTERVAL


@owner_only
async def recv_round_interval(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(update.message.text.strip())
        state["round_interval"] = val
        await update.message.reply_text(
            main_menu_text() + f"\n✅ Round interval set to *{fmt_seconds(val)}*",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
        return ST_MAIN
    except ValueError:
        await update.message.reply_text("❌ Please send a valid number:")
        return ST_AWAIT_ROUND_INTERVAL


# ══════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════

def run_userbot_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


async def start_userbot():
    global userbot
    userbot = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await userbot.start()
    me = await userbot.get_me()
    print(f"✔  Userbot logged in as: {me.first_name} (@{me.username})")
    print("⏳  Fetching groups…")
    groups = await fetch_groups()
    print(f"✔  Found {len(groups)} groups.")

    # ── Auto-create log channel on startup ────
    print("⏳  Setting up log channel…")
    try:
        link = await setup_log_channel()
        print(f"✔  Log channel ready! Invite link: {link}")
        print(f"   Share this link with the bot admin to monitor broadcasts.")
    except Exception as e:
        print(f"⚠️  Could not create log channel: {e}")


def main():
    # Validate config
    missing = [k for k,v in {
        "API_ID": API_ID, "API_HASH": API_HASH,
        "BOT_TOKEN": BOT_TOKEN, "OWNER_ID": OWNER_ID
    }.items() if not v]
    if missing:
        print(f"❌  Please fill in these values in the script: {', '.join(missing)}")
        return

    # Start userbot in its own thread/loop
    ub_loop = asyncio.new_event_loop()
    ub_thread = threading.Thread(target=run_userbot_loop, args=(ub_loop,), daemon=True)
    ub_thread.start()

    future = asyncio.run_coroutine_threadsafe(start_userbot(), ub_loop)
    future.result(timeout=60)

    # Patch userbot.loop so broadcast can use it
    userbot.loop = ub_loop

    # ── Notify admin of log channel link via bot ──
    if state["log_channel_link"]:
        async def _notify():
            import telegram
            bot = telegram.Bot(token=BOT_TOKEN)
            await bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    f"🚀 *Broadcaster Bot started!*\n\n"
                    f"📋 *Log channel created:*\n"
                    f"[Open Log Channel]({state['log_channel_link']})\n\n"
                    f"Every message sent during broadcasts will be logged there "
                    f"with the group name, message link, and content.\n\n"
                    f"Use /start or /menu to open the control panel."
                ),
                parse_mode="Markdown",
            )
        try:
            asyncio.run(_notify())
        except Exception as e:
            print(f"⚠️  Could not send startup message to admin: {e}")

    # Build bot application
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start),
                      CommandHandler("menu",  cmd_start)],
        states={
            ST_MAIN: [
                CallbackQueryHandler(btn_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_handler),
            ],
            ST_AWAIT_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_text),
            ],
            ST_AWAIT_FWD_SOURCE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_fwd_source),
            ],
            ST_AWAIT_FWD_MSGID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_fwd_msgid),
            ],
            ST_AWAIT_SEND_INTERVAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_send_interval),
            ],
            ST_AWAIT_ROUND_INTERVAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_round_interval),
            ],
        },
        fallbacks=[CommandHandler("start", cmd_start),
                   CommandHandler("menu",  cmd_start)],
        per_chat=True,
    )

    app.add_handler(conv)

    print(f"\n🤖  Bot started! Open Telegram and send /start to your bot.")
    print(f"    Press Ctrl+C to stop.\n")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
