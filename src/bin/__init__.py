import asyncio
import logging
import os
import aiohttp
import re
import io
import json
import html
import uuid
from collections import OrderedDict

import discord
import discord.ui
from discord.ext import commands
from telegram import Update, InputMediaPhoto, InputFile, constants, User as TGUser
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes,
    CommandHandler,
    Defaults,
)
from telegram.ext import MessageReactionHandler
from telegram import ReactionTypeEmoji # Specific reaction type
from telegram.error import TelegramError, BadRequest
from PIL import Image, UnidentifiedImageError

# --- Configuration ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
_tg_group_id_str = os.environ.get("TELEGRAM_GROUP_ID")
TELEGRAM_GROUP_ID = int(_tg_group_id_str) if _tg_group_id_str else None

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID"))

_dc_voice_id_str = os.environ.get("DISCORD_VOICE_CHANNEL_ID")
DISCORD_VOICE_CHANNEL_ID = int(_dc_voice_id_str) if _dc_voice_id_str else None

_dc_human_role_id_str = os.environ.get("DISCORD_HUMAN_ROLE_ID")
DISCORD_HUMAN_ROLE_ID = int(_dc_human_role_id_str) if _dc_human_role_id_str else None

USER_MAP_FILE = os.environ.get("USER_MAP_FILE")
MAX_MESSAGE_MAP_SIZE = 200 # Max number of messages (pairs) to keep track of for edits/replies
MAX_MESSAGE_LENGTH_TG = 4096
MAX_CAPTION_LENGTH_TG = 1024

# --- Logging ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- State Management ---

# User Map (File-based)
# Format: {"discord:<discord_id>": <telegram_id>, "telegram:<telegram_id>": <discord_id>}
def load_user_map():
    try:
        if os.path.exists(USER_MAP_FILE):
            with open(USER_MAP_FILE, 'r') as f:
                # Convert loaded keys back if needed (json saves keys as strings)
                # Keep as is for now, handle conversion during lookup if necessary
                return json.load(f)
        else:
            return {}
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading user map from {USER_MAP_FILE}: {e}. Starting with empty map.")
        return {}

def save_user_map(user_map_dict):
    try:
        with open(USER_MAP_FILE, 'w') as f:
            json.dump(user_map_dict, f, indent=4)
    except IOError as e:
        logger.error(f"Error saving user map to {USER_MAP_FILE}: {e}")

user_map = load_user_map() # Load at startup

# Message Map (In-memory, Limited Size)
# Format: OrderedDict
# Keys: "discord:<discord_msg_id>", "telegram:<telegram_msg_id>"
# Values: {"other_id": <id>, "original_content": "...", "is_media": bool}
message_map = OrderedDict()

def add_message_mapping(dc_msg_id, tg_msg_id, is_media_tg=False, is_media_dc=False):
    """Adds a mapping between a Discord and Telegram message, managing map size."""
    global message_map
    if len(message_map) // 2 >= MAX_MESSAGE_MAP_SIZE:
        try:
            # Remove the oldest pair (2 entries)
            message_map.popitem(last=False)
            message_map.popitem(last=False)
            logger.info(f"Message map size limit ({MAX_MESSAGE_MAP_SIZE}) reached. Removed oldest entry pair.")
        except KeyError:
            logger.warning("Tried to pop from empty message map.") # Should not happen if check is correct

    message_map[f"discord:{dc_msg_id}"] = {
        "other_id": tg_msg_id,
        "is_media": is_media_tg
    }
    message_map[f"telegram:{tg_msg_id}"] = {
        "other_id": dc_msg_id,
        "is_media": is_media_dc
    }
    logger.info(f"Mapped DC:{dc_msg_id} to TG:{tg_msg_id}. Map size: {len(message_map) // 2}")


# --- Helper Functions ---

# URL detection regex pattern
# This pattern matches common URL formats including http, https, ftp protocols
# as well as www. prefixed URLs
URL_PATTERN = re.compile(
    r'(https?://|www\.)[^\s<>\[\]{}|\\^~`"\'\s]+[^\s<>\[\]{}|\\^~`"\',.:;?!]',
    re.IGNORECASE
)

def detect_urls(text: str) -> list[tuple[int, int, str]]:
    """
    Detects URLs in text and returns a list of tuples with (start_index, end_index, url).
    """
    if not text:
        return []
    
    return [(match.start(), match.end(), match.group(0)) for match in URL_PATTERN.finditer(text)]

def protect_urls_for_telegram(text: str) -> tuple[str, dict]:
    """
    Replaces URLs in text with placeholders for safe HTML conversion.
    Returns the modified text and a dictionary mapping placeholders to original URLs.
    """
    if not text:
        return text, {}
    
    url_map = {}
    
    def replacer(match):
        url = match.group(0)
        # Use a placeholder that is unlikely to be markdown-interpreted or conflict with HTML
        placeholder = f"%%URLPLACEHOLDER{str(uuid.uuid4()).replace('-', '')}%%"
        url_map[placeholder] = url
        return placeholder

    # Replace all found URLs with placeholders using URL_PATTERN
    modified_text = URL_PATTERN.sub(replacer, text)
    return modified_text, url_map

def restore_urls_for_telegram(text: str, url_map: dict) -> str:
    """
    Restores URLs from placeholders, wrapping them in HTML anchor tags.
    """
    if not text or not url_map:
        return text
    
    result = text
    for placeholder, url in url_map.items():
        # Create a proper HTML link
        # If URL doesn't start with a protocol, add https://
        href_url = url if url.startswith(('http://', 'https://', 'ftp://')) else f"https://{url}"
        html_link = f'<a href="{escape_html(href_url)}">{escape_html(url)}</a>'
        result = result.replace(placeholder, html_link)
    
    return result

def escape_discord_markdown_preserve_urls(text: str) -> str:
    """
    Escapes Discord markdown characters while preserving URLs.
    """
    if not text:
        return ""
    
    # Find all URLs in the text
    urls = detect_urls(text)
    
    # If no URLs, use the regular escape function
    if not urls:
        return escape_discord_markdown(text)
    
    # Replace URLs with placeholders
    url_map = {}
    
    def replacer(match):
        url = match.group(0)
        placeholder = f"%%URLPLACEHOLDER{str(uuid.uuid4()).replace('-', '')}%%"
        url_map[placeholder] = url
        return placeholder

    modified_text = URL_PATTERN.sub(replacer, text)
    
    # Escape markdown in the modified text
    escaped_text = escape_discord_markdown(modified_text)
    
    # Restore URLs
    for placeholder, url in url_map.items():
        escaped_text = escaped_text.replace(placeholder, url)
    
    return escaped_text

async def download_file(url: str) -> bytes | None:
    """Downloads a file from a URL."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
                else:
                    logger.error(f"Failed to download file from {url}: Status {resp.status}")
                    return None
    except aiohttp.ClientError as e:
        logger.error(f"Network error downloading file from {url}: {e}")
        return None

def escape_html(text: str | None) -> str:
    """Escapes HTML special characters."""
    if not text:
        return ""
    return html.escape(text)

def escape_discord_markdown(text: str | None) -> str:
    """Escapes Discord markdown characters."""
    if not text:
        return ""
    # Escape *, _, ~, `, ||, >
    escape_chars = r'([*_~`|>\\])'
    return re.sub(escape_chars, r'\\\1', text)


def format_discord_message_for_telegram(message: discord.Message) -> str:
    """Formats a Discord message content for Telegram HTML, handling markdown, mentions, and URLs."""
    raw_content = message.content

    # 1. Protect URLs using %%URLPLACEHOLDER...%% style
    content_with_placeholders, url_map = protect_urls_for_telegram(raw_content)
    
    # 2. Handle Mentions (on content with URL placeholders)
    #    This converts Discord's <@id>, <@&id>, <#id> into HTML links or @display_name.
    #    The %%...%% placeholders will not be affected by these regexes.
    content_after_mentions = content_with_placeholders

    # User Mentions (<@!id> or <@id>)
    def user_mention_repl(match_obj):
        user_id_str = match_obj.group(1)
        user_id = int(user_id_str)
        mentioned_user = next((u for u in message.mentions if u.id == user_id), None)
        display_name = mentioned_user.display_name if mentioned_user else f"Unknown User ({user_id})"
        tg_user_id = user_map.get(f"discord:{user_id_str}")
        if tg_user_id:
            return f'<a href="tg://user?id={tg_user_id}">{escape_html(display_name)}</a>'
        else:
            return f"@{escape_html(display_name)}"
    content_after_mentions = re.sub(r'<@!?(\d+)>', user_mention_repl, content_after_mentions)

    # Role Mentions (<@&id>)
    def role_mention_repl(match_obj):
        role_id = int(match_obj.group(1))
        role = message.guild.get_role(role_id) if message.guild else None
        role_name = role.name if role else f"Unknown Role ({role_id})"
        return f"@{escape_html(role_name)}"
    content_after_mentions = re.sub(r'<@&(\d+)>', role_mention_repl, content_after_mentions)

    # Channel Mentions (<#id>)
    def channel_mention_repl(match_obj):
        channel_id = int(match_obj.group(1))
        channel = message.guild.get_channel(channel_id) if message.guild else None
        channel_name = channel.name if channel else f"Unknown Channel ({channel_id})"
        return f"#{escape_html(channel_name)}"
    content_after_mentions = re.sub(r'<#(\d+)>', channel_mention_repl, content_after_mentions)
    
    # Handle @everyone/@here (literal strings)
    # message.mention_everyone is a boolean, but we need to replace the string if present.
    if "@everyone" in content_after_mentions:
        content_after_mentions = content_after_mentions.replace('@everyone', '@\u200Beveryone')
    if "@here" in content_after_mentions:
        content_after_mentions = content_after_mentions.replace('@here', '@\u200Bhere')

    # 3. Basic Markdown to HTML (on content with resolved mentions [now HTML] and %%...%% URL placeholders)
    #    The %%...%% placeholders are designed not to be affected by these markdown rules.
    #    For example, _%%P%%_ would become <i>%%P%%</i>, which is fine for restoration.
    html_content = content_after_mentions
    html_content = re.sub(r'```(\w+)?\n(.*?)```', r'<pre><code class="language-\1">\2</code></pre>', html_content, flags=re.DOTALL | re.IGNORECASE)
    html_content = re.sub(r'```(.*?)```', r'<pre>\1</pre>', html_content, flags=re.DOTALL)
    html_content = re.sub(r'`(.*?)`', r'<code>\1</code>', html_content)
    html_content = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', html_content)
    html_content = re.sub(r'__(.*?)__', r'<u>\1</u>', html_content) # Underline
    html_content = re.sub(r'(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)', r'<i>\1</i>', html_content) # *italic*
    html_content = re.sub(r'(?<!_)_(?!_)(.*?)(?<!_)_(?!_)', r'<i>\1</i>', html_content) # _italic_
    html_content = re.sub(r'~~(.*?)~~', r'<s>\1</s>', html_content) # Strikethrough
    html_content = re.sub(r'\|\|(.*?)\|\|', r'<span class="tg-spoiler">\1</span>', html_content) # Spoiler

    # 4. Restore URLs from %%...%% placeholders
    final_content = restore_urls_for_telegram(html_content, url_map)
    
    return final_content

async def get_telegram_sender_name(tg_user: TGUser) -> str:
    """Gets a display name for a Telegram user, HTML escaped."""
    name = escape_html(tg_user.full_name) # Prefer full name
    username = escape_html(tg_user.username)

    if username:
        return f"{name} (@{username})"
    elif name:
        return name
    else:
        return f"User ({tg_user.id})"

async def get_discord_sender_name(discord_user: discord.User | discord.Member) -> str:
    """Gets a display name for a Discord user, Markdown escaped."""
    # Prefer display_name (nickname) if available, else global_name, else name
    name = discord_user.display_name if isinstance(discord_user, discord.Member) else discord_user.global_name
    if not name:
        name = discord_user.name # Fallback to username

    return escape_discord_markdown(name)

async def find_discord_id_from_telegram(tg_identifier: int | str) -> int | None:
    """Looks up Discord ID from Telegram ID or Username."""
    global user_map
    if isinstance(tg_identifier, int): # Lookup by ID
        return user_map.get(f"telegram:{tg_identifier}")
    elif isinstance(tg_identifier, str): # Lookup by username
        tg_id = user_map.get(f"telegram_username:{tg_identifier.lower()}")
        if tg_id:
            return user_map.get(f"telegram:{tg_id}")
    return None

# --- Telegram Bot Logic ---

async def find_discord_id_from_telegram(tg_identifier: int | str) -> int | None:
    """Looks up Discord ID from Telegram ID or Username."""
    global user_map
    if isinstance(tg_identifier, int): # Lookup by ID
        return user_map.get(f"telegram:{tg_identifier}")
    elif isinstance(tg_identifier, str): # Lookup by username
        tg_id = user_map.get(f"telegram_username:{tg_identifier.lower()}")
        if tg_id:
            return user_map.get(f"telegram:{tg_id}")
    return None

async def telegram_forward_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forwards messages, photos, stickers from Telegram to Discord."""
    logger.info(f"TG_FWD: Received update. Chat ID: {update.effective_chat.id if update.effective_chat else 'N/A'}")

    if not update.effective_chat or not update.message:
        logger.warning("TG_FWD: Update ignored: No effective chat or message.")
        return
    # Check group ID only if it's configured
    if TELEGRAM_GROUP_ID is not None and update.effective_chat.id != TELEGRAM_GROUP_ID:
         logger.warning(f"TG_FWD: Update ignored: Chat ID {update.effective_chat.id} != configured TELEGRAM_GROUP_ID {TELEGRAM_GROUP_ID}")
         return
    elif TELEGRAM_GROUP_ID is None:
        logger.warning("TG_FWD: Update ignored: TELEGRAM_GROUP_ID is not set (forwarding disabled).")
        return

    message = update.message
    logger.info(f"TG_FWD: Processing message {message.message_id} from TG User ID: {message.from_user.id}")

    sender_name = await get_telegram_sender_name(message.from_user) # Escaped HTML Name
    discord_channel = context.bot_data.get("discord_channel")
    if not discord_channel:
        logger.warning("TG_FWD: Discord channel not found in bot_data.")
        return

    # --- Process Text Content with Entities ---
    text_content = message.text or message.caption or ""
    entities = message.entities or message.caption_entities or []
    processed_parts = []
    current_pos = 0

    # Sort entities by offset to process in order
    sorted_entities = sorted(entities, key=lambda e: e.offset)

    logger.debug(f"TG_FWD: Processing {len(sorted_entities)} entities for message {message.message_id}")

    # Iterate over the newly created sorted list
    for entity in sorted_entities:
        # 1. Append and escape text *before* the current entity
        part_before = text_content[current_pos:entity.offset]
        # Use the new escape function that preserves URLs
        processed_parts.append(escape_discord_markdown_preserve_urls(part_before))

        # 2. Process the entity itself
        entity_text = text_content[entity.offset : entity.offset + entity.length]
        discord_id = None

        if entity.type == entity.type.MENTION: # @username
            username = entity_text.lstrip('@')
            logger.debug(f"TG_FWD: Found MENTION entity: @{username}")
            discord_id = await find_discord_id_from_telegram(username)
        elif entity.type == entity.type.TEXT_MENTION: # User mentioned by name
            if entity.user:
                tg_user_id = entity.user.id
                logger.debug(f"TG_FWD: Found TEXT_MENTION entity for TG User ID: {tg_user_id}")
                discord_id = await find_discord_id_from_telegram(tg_user_id)
            else:
                logger.warning("TG_FWD: TEXT_MENTION entity found but 'user' attribute is missing.")

        
        # 3. Append the processed entity part
        if discord_id:
            ping_text = f"<@{discord_id}>"
            processed_parts.append(ping_text)
            logger.info(f"TG_FWD: Translated mention '{entity_text}' to Discord ping '{ping_text}'")
        else:
            # If not found or not a mention type we handle, append escaped original
            # Use the new escape function that preserves URLs
            if entity.type == entity.type.URL or entity.type == entity.type.TEXT_LINK:
                # For URLs and text links, we don't want to escape them further for Discord
                # as Discord handles them well. If it's a text_link, entity_text is the URL.
                # For text_link, the display text is handled by Telegram; we just pass the URL.
                processed_parts.append(entity_text)
            elif entity.type == entity.type.SPOILER:
                # wrap spoilers in || x ||
                processed_parts.append("||" + escape_discord_markdown_preserve_urls(entity_text) + "||")
            else:
                processed_parts.append(escape_discord_markdown_preserve_urls(entity_text))


        # 4. Update position
        current_pos = entity.offset + entity.length

    # 5. Append and escape any remaining text after the last entity
    part_after = text_content[current_pos:]
    # Use the new escape function that preserves URLs
    processed_parts.append(escape_discord_markdown_preserve_urls(part_after))

    # 6. Join the parts
    final_discord_content = "".join(processed_parts)
    logger.debug(f"TG_FWD: Final processed content for Discord: {final_discord_content[:100]}...")

    # --- Prepare Base Message for Discord ---
    # Use the newly processed content
    forward_content_base = f"**{escape_discord_markdown(sender_name)}:** {final_discord_content}" # Escape sender name too


    # --- Handle Replies (Keep existing logic) ---
    discord_reply_target = None
    if message.reply_to_message:
        tg_reply_to_msg_id = message.reply_to_message.message_id
        map_entry = message_map.get(f"telegram:{tg_reply_to_msg_id}")
        if map_entry:
            reply_to_discord_msg_id = map_entry.get("other_id")
            if reply_to_discord_msg_id:
                try:
                    discord_reply_target = discord.MessageReference(
                        message_id=reply_to_discord_msg_id,
                        channel_id=discord_channel.id,
                        fail_if_not_exists=False
                    )
                except Exception as e:
                    logger.warning(f"TG_FWD: Could not create Discord reply reference: {e}")
        else:
            # Append note about unmapped reply to the *processed* content
            forward_content_base += "\n*(in reply to an unmapped message)*"


    # --- Handle Media (Keep existing logic) ---
    discord_files = []
    is_media = False
    try:
        if message.photo:
            is_media = True
            # ... (photo handling logic remains the same) ...
            file_id = message.photo[-1].file_id
            tg_file = await context.bot.get_file(file_id)
            file_data = await tg_file.download_as_bytearray()
            try:
                image_stream = io.BytesIO(file_data)
                img = Image.open(image_stream)
                re_encoded_image = io.BytesIO()
                img.save(re_encoded_image, "JPEG")
                re_encoded_image.seek(0)
                discord_files.append(discord.File(re_encoded_image, filename="photo.jpg"))
            except (UnidentifiedImageError, OSError, ValueError) as e:
                logger.warning(f"TG_FWD: Error re-encoding Telegram photo: {e}. Sending original.")
                discord_files.append(discord.File(io.BytesIO(file_data), filename="photo_original.jpg"))

        elif message.sticker:
            is_media = True
            if message.sticker.thumbnail:
                 file_id = message.sticker.file_id
                 try:
                     tg_file = await context.bot.get_file(file_id)
                     file_data = await tg_file.download_as_bytearray()
                     try:
                         img = Image.open(io.BytesIO(file_data)).convert("RGBA")
                         png_image = io.BytesIO()
                         img.save(png_image, "PNG")
                         png_image.seek(0)
                         discord_files.append(discord.File(png_image, filename="sticker.png"))
                     except (UnidentifiedImageError, OSError, ValueError) as e:
                          logger.warning(f"TG_FWD: Could not convert sticker {file_id} to PNG: {e}. Skipping attachment.")
                          forward_content_base = f"**{escape_discord_markdown(sender_name)}:** [Sent a sticker (preview failed)]"
                          final_discord_content = "[Sent a sticker (preview failed)]"
                 except TelegramError as e:
                     logger.error(f"TG_FWD: Failed to download sticker {file_id}: {e}")
                     forward_content_base = f"**{escape_discord_markdown(sender_name)}:** [Sent a sticker (download failed)]"
                     final_discord_content = "[Sent a sticker (download failed)]"
            elif message.sticker.is_animated or message.sticker.is_video:
                 forward_content_base = f"**{escape_discord_markdown(sender_name)}:** [Sent an animated/video sticker]"
                 final_discord_content = "[Sent an animated/video sticker]" # Update content being sent

        # --- Send to Discord ---
        sent_discord_message = None
        # Send only if there's actual text content OR files
        if discord_files or final_discord_content.strip():
             # Determine content to send: Use base which includes sender name + processed text
             # If sending files, Discord uses the 'content' as the caption/message text.
             # If sending only text, 'content' is the message.
             content_to_send = forward_content_base

             # Trim if too long for Discord
             if len(content_to_send) > 2000: # Discord message limit
                  content_to_send = content_to_send[:1997] + "..."

             logger.info(f"TG_FWD: Attempting to send to Discord channel {discord_channel.id}...")
             sent_discord_message = await discord_channel.send(
                 content=content_to_send,
                 files=discord_files or None,
                 reference=discord_reply_target,
                 allowed_mentions=discord.AllowedMentions(users=True) # IMPORTANT: Allow user pings!
             )
             logger.info(f"TG_FWD: Successfully sent message to Discord. DC ID: {sent_discord_message.id}")

        # --- Store Message ID Mapping ---
        if sent_discord_message:
            # Store the *final* processed Discord content for potential future edits from Discord
            add_message_mapping(
                dc_msg_id=sent_discord_message.id,
                tg_msg_id=message.message_id,
                is_media_dc=bool(discord_files),
                is_media_tg=is_media
            )

    except discord.HTTPException as e:
        logger.error(f"TG_FWD: Failed to send message/media to Discord: {e}")
    except TelegramError as e:
        logger.error(f"TG_FWD: Telegram API error during forward: {e}")
    except Exception as e:
        logger.error(f"TG_FWD: Unexpected error during Telegram -> Discord forward: {e}", exc_info=True)


async def telegram_edit_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forwards message edits from Telegram to Discord."""
    if not update.effective_chat or update.effective_chat.id != TELEGRAM_GROUP_ID or not update.edited_message:
        return

    edited_message = update.edited_message
    tg_msg_id = edited_message.message_id
    map_entry = message_map.get(f"telegram:{tg_msg_id}")
    discord_channel = context.bot_data.get("discord_channel")

    if not map_entry or not discord_channel:
        logger.info("message edited but no mapping")
        return # No mapping or channel unavailable

    discord_msg_id = map_entry.get("other_id")
    if not discord_msg_id: return

    try:
        discord_message = await discord_channel.fetch_message(discord_msg_id)

        sender_name = await get_telegram_sender_name(edited_message.from_user) # Escaped
        new_text_content = edited_message.text or edited_message.caption or ""
        # Use the new escape function that preserves URLs
        safe_new_text_content = escape_discord_markdown_preserve_urls(new_text_content)
        new_discord_content = f"**{sender_name}:** {safe_new_text_content}"

        # Check if original had media - Discord edits replace everything.
        # If it was media, we can only edit the 'content' part (caption).
        # If it was text, we edit the 'content'.
        await discord_message.edit(content=new_discord_content)

        # Update the stored original content in the map
        map_entry["original_content"] = new_discord_content # Update the stored DC markdown
        message_map[f"telegram:{tg_msg_id}"] = map_entry # Re-assign to update OrderedDict if needed

        logger.info(f"Edited Discord message {discord_msg_id} based on TG edit {tg_msg_id}")

    except discord.NotFound:
        logger.warning(f"Discord message {discord_msg_id} not found for editing.")
        # Clean up stale map entries
        message_map.pop(f"telegram:{tg_msg_id}", None)
        message_map.pop(f"discord:{discord_msg_id}", None)
    except discord.HTTPException as e:
        logger.error(f"Failed to edit Discord message {discord_msg_id}: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred during Telegram -> Discord edit: {e}", exc_info=True)

# --- Discord Bot Logic ---

async def discord_forward_message(message: discord.Message, tg_bot: 'telegram.Bot'):
    """Handles forwarding a new Discord message to Telegram."""
    if TELEGRAM_GROUP_ID is None: return

    base_html_content = format_discord_message_for_telegram(message) # Already handles mentions/formatting
    sender_name_html = f"<b>{escape_html(message.author.display_name)}:</b>" # Use display_name, escaped

    # --- Handle Replies ---
    reply_to_tg_msg_id = None
    if message.reference and message.reference.message_id:
        map_entry = message_map.get(f"discord:{message.reference.message_id}")
        if map_entry:
            reply_to_tg_msg_id = map_entry.get("other_id")
        else:
            # Append notation about reply to unmapped message?
             base_html_content += "\n<i>(in reply to an unmapped message)</i>"


    sent_tg_message = None
    media_files_to_send = []
    is_media_dc = False


    # --- Handle Attachments (Images) ---
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            is_media_dc = True
            image_bytes = await download_file(attachment.url)
            if image_bytes:
                # Add some basic info to caption if sending as media group
                caption = f"{sender_name_html}\n{base_html_content}" if not media_files_to_send else None
                media_files_to_send.append(InputMediaPhoto(media=image_bytes, caption=caption[:MAX_CAPTION_LENGTH_TG] if caption else None, parse_mode=constants.ParseMode.HTML))


    # --- Handle Custom Emojis (Send as separate photos) ---
    # Regex to find custom emojis and extract ID and animated status
    custom_emoji_pattern = re.compile(r'<(a?):(\w+):(\d+)>')
    emoji_tasks = []
    for match in custom_emoji_pattern.finditer(message.content):
        is_animated, _name, emoji_id = match.groups()
        emoji_url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{'gif' if is_animated else 'png'}"
        emoji_tasks.append(asyncio.create_task(download_file(emoji_url)))

    emoji_results = await asyncio.gather(*emoji_tasks)
    valid_emoji_bytes = [data for data in emoji_results if data]

    # Decide how to send: separate text, media group, or single photo with caption
    try:
        final_text_content = f"{sender_name_html}\n{base_html_content}" # Combine sender + message

        if media_files_to_send:
             # Send as media group (caption only on first usually) or single photo
             if len(media_files_to_send) == 1:
                 # Use send_photo for single image with better caption support
                 media = media_files_to_send[0] # Already has caption set
                 sent_tg_message = await tg_bot.send_photo(
                     chat_id=TELEGRAM_GROUP_ID,
                     photo=media.media,
                     caption=media.caption, # Use pre-formatted caption
                     parse_mode=constants.ParseMode.HTML,
                     reply_to_message_id=reply_to_tg_msg_id
                 )
             else:
                 # Send as media group. Send text first if caption doesn't cover it.
                 # Note: Media group captions are tricky. Let's send text separately first for clarity.
                 # await tg_bot.send_message(chat_id=TELEGRAM_GROUP_ID, text=final_text_content, parse_mode=constants.ParseMode.HTML, reply_to_message_id=reply_to_tg_msg_id, disable_web_page_preview=True)
                 # Then send the media group without individual captions (first item's caption is already set)
                 # This might send the text twice effectively. Let's stick to caption on first item.
                 sent_media_group = await tg_bot.send_media_group(
                     chat_id=TELEGRAM_GROUP_ID,
                     media=media_files_to_send,
                     reply_to_message_id=reply_to_tg_msg_id
                 )
                 sent_tg_message = sent_media_group[0] # Map to the first message in the group

        elif final_text_content.strip() != sender_name_html.strip(): # Don't send if only sender name remains
            # Send as simple text message
             sent_tg_message = await tg_bot.send_message(
                 chat_id=TELEGRAM_GROUP_ID,
                 text=final_text_content[:MAX_MESSAGE_LENGTH_TG],
                 parse_mode=constants.ParseMode.HTML,
                 reply_to_message_id=reply_to_tg_msg_id,
                 disable_web_page_preview=False # Enable link previews
             )

        # Send custom emojis as separate photos *after* the main message
        # Use the main message's ID for reply context if available
        reply_context_id = sent_tg_message.message_id if sent_tg_message else reply_to_tg_msg_id
        for i, emoji_bytes in enumerate(valid_emoji_bytes):
            try:
                # Send individually
                emoji_msg = await tg_bot.send_photo(
                     chat_id=TELEGRAM_GROUP_ID,
                     photo=emoji_bytes,
                     reply_to_message_id=reply_context_id
                 )
                # Map the *original* discord message only to the *first* message sent to TG
                if i == 0 and not sent_tg_message:
                    sent_tg_message = emoji_msg # If emojis were the *only* thing sent

            except TelegramError as e_emoji:
                 logger.error(f"Failed to send Discord custom emoji as photo: {e_emoji}")

        # --- Store Message ID Mapping ---
        if sent_tg_message:
            add_message_mapping(
                dc_msg_id=message.id,
                tg_msg_id=sent_tg_message.message_id,
                is_media_dc=is_media_dc or bool(valid_emoji_bytes), # Mark as media if attachments or emojis sent
                is_media_tg=bool(media_files_to_send) or bool(valid_emoji_bytes)
            )

    except BadRequest as e:
        if "reply message not found" in str(e):
             logger.warning(f"Reply message not found on Telegram for DC msg {message.id}. Sending without reply.")
             # Retry without reply_to_message_id (implement retry logic or just log)
        else:
             logger.error(f"Telegram API Bad Request during Discord->TG forward: {e}")
    except TelegramError as e:
        logger.error(f"Telegram API error during Discord->TG forward: {e}")
    except Exception as e:
         logger.error(f"An unexpected error occurred during Discord -> Telegram forward: {e}", exc_info=True)


async def discord_edit_message(before: discord.Message, after: discord.Message, tg_bot: 'telegram.Bot'):
    """Handles forwarding message edits from Discord to Telegram."""
    if TELEGRAM_GROUP_ID is None or before.content == after.content: return

    map_entry = message_map.get(f"discord:{after.id}")
    if not map_entry: return # Not a message we forwarded/mapped

    tg_msg_id = map_entry.get("other_id")
    is_media_tg = map_entry.get("is_media", False) # Was the original TG message media?
    if not tg_msg_id: return

    try:
        new_html_content = format_discord_message_for_telegram(after)
        sender_name_html = f"<b>{escape_html(after.author.display_name)}:</b>"
        final_new_text = f"{sender_name_html}\n{new_html_content}"

        # Check for reactions - if reactions were added, they'll be handled by reaction event
        # This edit is purely for content change. We use the *latest* formatted text.

        if is_media_tg:
            # Edit caption if the Telegram message had media
            await tg_bot.edit_message_caption(
                chat_id=TELEGRAM_GROUP_ID,
                message_id=tg_msg_id,
                caption=final_new_text[:MAX_CAPTION_LENGTH_TG],
                parse_mode=constants.ParseMode.HTML
            )
        else:
            # Edit text if the Telegram message was text-based
            await tg_bot.edit_message_text(
                chat_id=TELEGRAM_GROUP_ID,
                message_id=tg_msg_id,
                text=final_new_text[:MAX_MESSAGE_LENGTH_TG],
                parse_mode=constants.ParseMode.HTML,
                disable_web_page_preview=False # Enable link previews
            )

        # Update the stored original content in the map
        map_entry["original_content"] = final_new_text # Update stored TG HTML
        message_map[f"discord:{after.id}"] = map_entry # Re-assign to update OrderedDict

        logger.info(f"Edited Telegram message {tg_msg_id} based on DC edit {after.id}")

    except BadRequest as e:
        if "message is not modified" in str(e):
            logger.info(f"Edit resulted in no change for TG msg {tg_msg_id}")
        elif "message to edit not found" in str(e) or "message can't be edited" in str(e):
             logger.warning(f"Telegram message {tg_msg_id} not found or can't be edited for DC edit {after.id}. Removing map entry.")
             message_map.pop(f"discord:{after.id}", None)
             message_map.pop(f"telegram:{tg_msg_id}", None)
        else:
             logger.error(f"Failed to edit Telegram message {tg_msg_id}: {e}")
    except TelegramError as e:
        logger.error(f"Telegram API error during Discord->TG edit: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred during Discord -> Telegram edit: {e}", exc_info=True)


async def update_telegram_reactions(reaction: discord.Reaction, tg_bot: 'telegram.Bot'):
    """
    Updates the corresponding Telegram message with the bot's reactions
    to mirror the unique emojis present on the Discord message.
    """
    if TELEGRAM_GROUP_ID is None: return

    message = reaction.message
    map_entry = message_map.get(f"discord:{message.id}")
    if not map_entry: return

    tg_msg_id = map_entry.get("other_id")
    if not tg_msg_id:
        logger.warning(f"DC_REACT: Found map entry for DC msg {message.id}, but other_id (tg_msg_id) is missing.")
        return

    logger.debug(f"DC_REACT: Update for DC msg {message.id} -> TG msg {tg_msg_id}")

    try:
        # --- Fetch Latest Discord Reactions ---
        try:
            fetched_message = await message.channel.fetch_message(message.id)
            current_reactions = fetched_message.reactions
            logger.debug(f"DC_REACT: Fetched {len(current_reactions)} unique reaction types from DC msg {message.id}")
        except (discord.NotFound, discord.HTTPException) as e:
            logger.warning(f"DC_REACT: Could not fetch Discord message {message.id} to update reactions: {e}. Aborting update.")
            return

        # --- Collect Unique Emojis from Discord ---
        unique_unicode_emojis = set()
        for react in current_reactions:
            if isinstance(react.emoji, str):
                unique_unicode_emojis.add(react.emoji)

        # --- Prepare Reaction List for Telegram ---
        tg_reactions_to_set = []
        max_reactions_to_send = 11 # Telegram might have a limit
        for emoji in list(unique_unicode_emojis)[:max_reactions_to_send]:
            tg_reactions_to_set.append(ReactionTypeEmoji(emoji=emoji))

        emoji_list_str = ", ".join([r.emoji for r in tg_reactions_to_set]) # For logging
        logger.debug(f"DC_REACT: Prepared {len(tg_reactions_to_set)} reactions to set on TG msg {tg_msg_id}: [{emoji_list_str}]")

        # --- Set Reactions on Telegram ---
        await tg_bot.set_message_reaction(
            chat_id=TELEGRAM_GROUP_ID,
            message_id=tg_msg_id,
            reaction=tg_reactions_to_set if tg_reactions_to_set else None,
        )
        logger.info(f"DC_REACT: Successfully updated reactions on TG msg {tg_msg_id} to reflect DC msg {message.id} (Sent: [{emoji_list_str}])")

    except BadRequest as e:
        failed_emojis_str = ", ".join([r.emoji for r in tg_reactions_to_set]) # Use prepared list
        logger.error(
            f"DC_REACT: Telegram BadRequest setting reaction for TG msg {tg_msg_id}. "
            f"Error: '{e}'. Attempted emojis: [{failed_emojis_str}]" # Log attempted list
        )
        # Optional: More specific logging based on error content
        if "message reactions are unavailable" in str(e) or "MESSAGE_ID_INVALID" in str(e):
             logger.warning(f"DC_REACT: TG message {tg_msg_id} unavailable for reactions.")
        elif "reaction invalid" in str(e):
             logger.warning(f"DC_REACT: At least one emoji in [{failed_emojis_str}] is invalid for TG reactions API.")

    except TelegramError as e:
        logger.error(f"DC_REACT: Telegram API error setting reaction for TG msg {tg_msg_id}: {e}")
    except Exception as e:
        logger.error(f"DC_REACT: Unexpected error during Discord reaction update for TG msg {tg_msg_id}: {e}", exc_info=True)


# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True # Needed for resolving user names reliably
intents.voice_states = True # <<< --- ADD THIS INTENT ---
discord_bot = commands.Bot(command_prefix="!", intents=intents)
discord_ready_event = asyncio.Event()

telegram_app = None # Placeholder

# --- Ensure on_ready sets the event correctly ---
@discord_bot.event
async def on_ready():
    # Prevent re-initialization on reconnect
    if discord_ready_event.is_set():
        logger.info("Discord reconnected.")
        return

    logger.info(f"Discord bot connected as {discord_bot.user}")
    # ... (rest of your existing on_ready including channel check) ...
    if not DISCORD_CHANNEL_ID:
        logger.error("DISCORD_CHANNEL_ID is not set. Cannot proceed.")
        await discord_bot.close()
        return

    discord_channel = discord_bot.get_channel(DISCORD_CHANNEL_ID)
    if discord_channel is None:
        logger.error(f"Could not find Discord channel with ID {DISCORD_CHANNEL_ID}. Aborting.")
        await discord_bot.close()
        return # Don't set the event if channel not found

    logger.info(f"Found Discord channel: {discord_channel.name} ({discord_channel.id})")

    # Build and store Telegram app instance
    try:
        telegram_app = build_telegram_bot(discord_channel)
        if telegram_app:
            discord_bot.telegram_app = telegram_app
            logger.info("Telegram bot configuration finished.")
            # <<< --- Signal that Discord is ready AND Telegram app is built --- >>>
            discord_ready_event.set()
            logger.info("Discord ready event set.")
        else:
            logger.error("Failed to build Telegram bot. Check token/config. Aborting.")
            await discord_bot.close()
    except Exception as e:
        logger.error(f"Exception during Telegram bot build in on_ready: {e}", exc_info=True)
        await discord_bot.close()

async def should_skip_discord_forwarding(guild: discord.Guild) -> bool:
    """
    Checks if forwarding from Discord to Telegram should be skipped based on
    voice channel presence of members with the 'human' role.
    Returns True if forwarding should be skipped, False otherwise.
    """
    # --- Check if feature is configured ---
    if DISCORD_VOICE_CHANNEL_ID is None or DISCORD_HUMAN_ROLE_ID is None:
        # logger.debug("Skipping voice presence check: Voice Channel ID or Human Role ID not configured.")
        return False # Don't skip if not configured

    # --- Get Role Object ---
    human_role = guild.get_role(DISCORD_HUMAN_ROLE_ID)
    if not human_role:
        logger.warning(f"Cannot perform voice check: Human Role ID {DISCORD_HUMAN_ROLE_ID} not found in guild {guild.name}.")
        return False # Don't skip if role not found

    # --- Get Voice Channel Object ---
    voice_channel = guild.get_channel(DISCORD_VOICE_CHANNEL_ID)
    # Ensure it's actually a VoiceChannel (get_channel can return TextChannel etc.)
    if not voice_channel or not isinstance(voice_channel, discord.VoiceChannel):
        logger.warning(f"Cannot perform voice check: Voice Channel ID {DISCORD_VOICE_CHANNEL_ID} not found or is not a voice channel in guild {guild.name}.")
        return False # Don't skip if channel not found/invalid

    # --- Get Members with Human Role (excluding bots) ---
    try:
        # role.members requires member cache or fetching - ensure intents are sufficient
        human_role_members = {member.id for member in human_role.members if not member.bot}
        if not human_role_members:
            # logger.debug("Voice check: No human members found with the specified role. Skipping forwarding block.")
            return False # Don't skip if no one has the role
    except Exception as e:
        logger.error(f"Error getting members for role {DISCORD_HUMAN_ROLE_ID}: {e}. Cannot perform voice check.", exc_info=True)
        return False # Don't skip on error

    # --- Get Members in Voice Channel (excluding bots) ---
    try:
        voice_channel_members = {member.id for member in voice_channel.members if not member.bot}
    except Exception as e:
        logger.error(f"Error getting members for voice channel {DISCORD_VOICE_CHANNEL_ID}: {e}. Cannot perform voice check.", exc_info=True)
        return False # Don't skip on error

    # --- Perform the Check ---
    # Are all members with the human role currently in the target voice channel?
    all_humans_in_voice = human_role_members.issubset(voice_channel_members)

    if all_humans_in_voice:
        logger.info(f"Skipping Discord -> Telegram forward: All {len(human_role_members)} members with role '{human_role.name}' are in voice channel '{voice_channel.name}'.")
        return True # Skip forwarding
    else:
        # Optional: Log who is missing for debugging
        missing_members_ids = human_role_members - voice_channel_members
        if missing_members_ids:
             missing_names = []
             for member_id in missing_members_ids:
                  member = guild.get_member(member_id) # Requires member cache/fetch
                  missing_names.append(member.display_name if member else f"ID:{member_id}")
             logger.debug(f"Not skipping forward: Missing members in voice: {', '.join(missing_names)}")
        else:
             logger.debug("Not skipping forward: Not all human role members are in voice.")

        return False # Do not skip forwarding

@discord_bot.event
async def on_message(message: discord.Message):
    if message.author == discord_bot.user: return # Ignore self
    if not message.guild: return # Ignore DMs

    original_content = message.content
    await discord_bot.process_commands(message) # Process commands first

    # --- Check if it was a command attempt ---
    is_command_attempt = False
    prefix_to_check = discord_bot.command_prefix
    # (Keep the existing command prefix checking logic here)
    if isinstance(prefix_to_check, str):
        if original_content.startswith(prefix_to_check): is_command_attempt = True
    elif isinstance(prefix_to_check, (list, tuple)):
        if any(original_content.startswith(p) for p in prefix_to_check): is_command_attempt = True

    if is_command_attempt:
        # logger.info(f"Ignoring message as it starts with command prefix: {message.content[:50]}...")
        return # Don't forward command attempts

    # --- Check channel and Telegram config ---
    if message.channel.id != DISCORD_CHANNEL_ID or TELEGRAM_GROUP_ID is None:
        return

    # --- <<< --- ADD THE VOICE CHANNEL PRESENCE CHECK --- >>> ---
    try:
        if await should_skip_discord_forwarding(message.guild):
            # If the function returns True, stop processing for forwarding
            return # Don't proceed to forward
    except Exception as e_check:
        # Log error during check but proceed with forwarding as a fallback? Or block?
        # Let's log and proceed for now.
        logger.error(f"Error during voice channel presence check: {e_check}. Proceeding with potential forward.", exc_info=True)
    # --- <<< --- END OF VOICE CHANNEL CHECK --- >>> ---


    # --- Proceed with forwarding ---
    if hasattr(discord_bot, 'telegram_app') and discord_bot.telegram_app:
        # logger.info(f"Forwarding regular message from Discord: {message.id}")
        await discord_forward_message(message, discord_bot.telegram_app.bot)
    else:
        logger.warning("Telegram app not ready, cannot forward Discord message.")


@discord_bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if after.author == discord_bot.user: return # Ignore self edits
    if after.channel.id != DISCORD_CHANNEL_ID: return # Ignore other channels
    # Ignore edits that only change embeds (e.g., link previews)
    if before.content == after.content and before.attachments == after.attachments:
        return

    if hasattr(discord_bot, 'telegram_app') and discord_bot.telegram_app:
        await discord_edit_message(before, after, discord_bot.telegram_app.bot)
    else:
        logger.warning("Telegram app not ready, cannot forward Discord edit.")


@discord_bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User):
    if user == discord_bot.user: return # Ignore self reactions
    if reaction.message.channel.id != DISCORD_CHANNEL_ID: return # Ignore other channels

    if hasattr(discord_bot, 'telegram_app') and discord_bot.telegram_app:
        await update_telegram_reactions(reaction, discord_bot.telegram_app.bot)
    else:
        logger.warning("Telegram app not ready, cannot update reactions.")


@discord_bot.event
async def on_reaction_remove(reaction: discord.Reaction, user: discord.User):
    if user == discord_bot.user: return # Ignore self reactions
    if reaction.message.channel.id != DISCORD_CHANNEL_ID: return # Ignore other channels

    if hasattr(discord_bot, 'telegram_app') and discord_bot.telegram_app:
        await update_telegram_reactions(reaction, discord_bot.telegram_app.bot) # Same function handles both add/remove logic
    else:
        logger.warning("Telegram app not ready, cannot update reactions.")

@discord_bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Announces when a user joins the configured voice channel."""

    # --- Configuration Check ---
    if DISCORD_VOICE_CHANNEL_ID is None:
        return # Feature disabled if voice channel ID not set

    # --- Ignore Bots ---
    if member.bot:
        return

    # --- Check if user JOINED the TARGET voice channel ---
    # Condition: They are now in the target channel AND they were not in it before
    # (This covers joining from nowhere AND moving from another channel into the target one)
    joined_target_channel = (
        after.channel is not None and
        after.channel.id == DISCORD_VOICE_CHANNEL_ID and
        (before.channel is None or before.channel.id != DISCORD_VOICE_CHANNEL_ID)
    )

    if not joined_target_channel:
        # logger.debug(f"Ignoring voice state update for {member.display_name}: Not a join to target channel {DISCORD_VOICE_CHANNEL_ID}")
        return

    logger.info(f"VOICE_UPDATE: {member.display_name} joined voice channel {after.channel.name} ({after.channel.id})")

    # --- Get Target Channels for Announcements ---
    discord_text_channel = discord_bot.get_channel(DISCORD_CHANNEL_ID)
    telegram_bot = getattr(discord_bot, 'telegram_app', None) # Check if telegram bot is ready

    if not discord_text_channel:
        logger.error(f"VOICE_UPDATE: Cannot announce join - Discord text channel {DISCORD_CHANNEL_ID} not found.")
        # No point proceeding if Discord channel missing
        return

    # --- Get Members Currently in the Channel ---
    current_members = after.channel.members
    # Filter out the user who just joined to get the list of 'others'
    other_members = [m for m in current_members if m.id != member.id]

    # --- Format Messages ---
    joined_member_name_dc = escape_discord_markdown(member.display_name)
    joined_member_name_tg = escape_html(member.display_name)
    channel_name_dc = escape_discord_markdown(after.channel.name)
    channel_name_tg = escape_html(after.channel.name)

    # --- Discord Message ---
    discord_message = f"🎙️ **{joined_member_name_dc}** has joined the voice channel: **{channel_name_dc}**"
    if other_members:
        other_names_dc = [escape_discord_markdown(m.display_name) for m in other_members]
        discord_message += f"\n👥 Already present: {', '.join(other_names_dc)}"
    else:
        discord_message += "\n✨ They are the first one here!"

    # --- Telegram Message ---
    telegram_message = f"🎙️ <b>{joined_member_name_tg}</b> has joined the voice channel: <b>{channel_name_tg}</b>"
    if other_members:
        other_names_tg = [escape_html(m.display_name) for m in other_members]
        telegram_message += f"\n👥 Already present: {', '.join(other_names_tg)}"
    else:
        telegram_message += "\n✨ They are the first one here!"


    # --- Send Announcements ---
    # Send to Discord Text Channel
    try:
        await discord_text_channel.send(discord_message)
        logger.info(f"VOICE_UPDATE: Sent Discord announcement for {member.display_name} joining.")
    except discord.Forbidden:
        logger.error(f"VOICE_UPDATE: Bot lacks permissions to send messages in Discord channel {DISCORD_CHANNEL_ID}.")
    except discord.HTTPException as e:
        logger.error(f"VOICE_UPDATE: Failed to send Discord announcement: {e}")

    # Send to Telegram Group (if configured and ready)
    if telegram_bot and TELEGRAM_GROUP_ID is not None:
        try:
            await telegram_bot.bot.send_message(
                chat_id=TELEGRAM_GROUP_ID,
                text=telegram_message,
                parse_mode=constants.ParseMode.HTML
            )
            logger.info(f"VOICE_UPDATE: Sent Telegram announcement for {member.display_name} joining.")
        except BadRequest as e:
            logger.error(f"VOICE_UPDATE: Telegram BadRequest sending announcement: {e}")
        except TelegramError as e:
            logger.error(f"VOICE_UPDATE: Telegram API error sending announcement: {e}")
        except Exception as e:
             logger.error(f"VOICE_UPDATE: Unexpected error sending Telegram announcement: {e}", exc_info=True)


# --- Discord UI Class ---
class TelegramUserSelectView(discord.ui.View):
    def __init__(self, *, timeout=180, author_id: int, telegram_admins: list[dict]):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.selected_tg_id = None
        self.telegram_admins = telegram_admins # Store the raw admin data

        options = []
        if not telegram_admins:
             # Handle case where admin list couldn't be fetched or is empty
             # Maybe add a disabled option or placeholder
            options.append(discord.SelectOption(label="No admins found or bot lacks permissions.", value="-1", disabled=True))
        else:
            for admin in telegram_admins[:25]: # Limit to 25 options for one dropdown
                user = admin.get('user')
                if user and not user.get('is_bot'): # Exclude bots from the list
                    user_id = user.get('id')
                    name = user.get('first_name', '')
                    last_name = user.get('last_name')
                    username = user.get('username')
                    if last_name: name += f" {last_name}"
                    if username: name += f" (@{username})"
                    if not name: name = f"User ID: {user_id}" # Fallback

                    options.append(discord.SelectOption(label=name[:100], value=str(user_id))) # Label limit is 100

        # Add placeholder if needed
        placeholder = "Select your corresponding Telegram account..." if options else "Could not load Telegram admins."

        self.user_select = discord.ui.Select(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            custom_id="telegram_user_select" # Optional custom ID
        )
        self.user_select.callback = self.select_callback # Assign the callback
        self.add_item(self.user_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Only allow the original command author to interact
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Sorry, only the person who ran the command can use this menu.", ephemeral=True)
            return False
        return True


    async def select_callback(self, interaction: discord.Interaction):
        global user_map
        selected_tg_id_str = self.user_select.values[0]
        if selected_tg_id_str == "-1":
            await interaction.response.edit_message(content="Selection cancelled or failed.", view=None)
            return

        selected_tg_id = int(selected_tg_id_str)
        discord_id_str = str(interaction.user.id)
        discord_id_int = interaction.user.id # Store as int too

        # --- Find selected admin info to get username ---
        selected_admin_info = None
        selected_option = next((opt for opt in self.user_select.options if opt.value == selected_tg_id_str), None)
        selected_label = f"ID {selected_tg_id}" # Fallback label
        if selected_option:
             selected_label = selected_option.label
             # We need the original admin data used to create the options
             # Let's assume self.telegram_admins was stored during init
             if hasattr(self, 'telegram_admins'):
                 selected_admin_info = next((admin for admin in self.telegram_admins if str(admin.get('user', {}).get('id')) == selected_tg_id_str), None)


        tg_username = None
        if selected_admin_info and selected_admin_info.get('user'):
             tg_username = selected_admin_info['user'].get('username') # Can be None


        # --- Update map with all necessary keys ---
        # Remove potential old links first
        existing_tg_id = user_map.pop(f"discord:{discord_id_str}", None)
        if existing_tg_id:
            user_map.pop(f"telegram:{existing_tg_id}", None)
            # Also try removing old username link if it existed (we need username for this though)
            # This part is tricky without storing the old username. Safer to just overwrite below.

        # Add new links
        user_map[f"discord:{discord_id_str}"] = selected_tg_id
        user_map[f"telegram:{selected_tg_id}"] = discord_id_int
        if tg_username:
             # Remove any previous link for this username
             old_id_for_username = user_map.pop(f"telegram_username:{tg_username.lower()}", None)
             if old_id_for_username and f"telegram:{old_id_for_username}" in user_map:
                 # If this username was previously linked to a *different* TG ID, remove that reverse link too
                 linked_discord_id = user_map.get(f"telegram:{old_id_for_username}")
                 if linked_discord_id:
                      user_map.pop(f"discord:{linked_discord_id}", None)

             user_map[f"telegram_username:{tg_username.lower()}"] = selected_tg_id # Store username link
             logger.info(f"Storing username mapping: {tg_username.lower()} -> {selected_tg_id}")

        save_user_map(user_map)

        # Confirmation message
        confirm_text = f"✅ Linked your Discord account ({interaction.user.mention}) to Telegram account: **{selected_label}**"
        if tg_username:
            confirm_text += f" (@{tg_username})"

        # Disable the select menu after selection
        self.user_select.disabled = True
        await interaction.response.edit_message(content=confirm_text, view=self)
        self.stop() # Stop the view from listening further

    async def on_timeout(self):
         # Edit message on timeout
        if hasattr(self, 'message') and self.message:
            try:
                self.user_select.disabled = True
                await self.message.edit(content="Link selection timed out.", view=self)
            except discord.HTTPException:
                 pass # Ignore if message was deleted
        self.stop()

# --- Discord Commands ---
@discord_bot.command(name="link")
# Remove the telegram_id argument, as it will be selected
async def link_telegram_select(ctx: commands.Context):
    """Links your Discord account to a Telegram Admin account via selection."""
    global user_map

    if TELEGRAM_GROUP_ID is None:
        await ctx.reply("❌ Telegram Group ID is not configured for this bot.")
        return

    if not hasattr(discord_bot, 'telegram_app') or not discord_bot.telegram_app:
         await ctx.reply("❌ Telegram Bot connection is not ready.")
         return

    tg_bot = discord_bot.telegram_app.bot
    discord_id_str = str(ctx.author.id)

    # Check if already linked
    if f"discord:{discord_id_str}" in user_map:
        linked_tg_id = user_map[f"discord:{discord_id_str}"]
        # You could try to fetch the TG user info here to show the name, but it adds complexity.
        await ctx.reply(f"ℹ️ Your account is already linked to Telegram ID `{linked_tg_id}`. Use `!unlink` first to change.")
        return

    try:
        logger.info(f"Fetching administrators for Telegram group {TELEGRAM_GROUP_ID}...")
        # Make sure the bot has admin rights in the group!
        admins = await tg_bot.get_chat_administrators(chat_id=TELEGRAM_GROUP_ID)
        admin_list = [admin.to_dict() for admin in admins] # Convert to dicts for easier handling
        logger.info(f"Found {len(admin_list)} administrators.")

        if not admin_list:
            await ctx.reply("❌ Could not fetch administrators from the Telegram group. Ensure the bot is an administrator there.")
            return

        # Create and send the selection view
        view = TelegramUserSelectView(author_id=ctx.author.id, telegram_admins=admin_list)
        # Store the message reference in the view for editing on timeout
        view.message = await ctx.reply("Please select your corresponding Telegram admin account from the list below:", view=view)

    except TelegramError as e:
         logger.error(f"Failed to get Telegram admins for group {TELEGRAM_GROUP_ID}: {e}")
         await ctx.reply(f"❌ An error occurred fetching Telegram admins: {e}")
    except Exception as e:
        logger.error(f"Error during !link command: {e}", exc_info=True)
        await ctx.reply("❌ An unexpected error occurred.")


@discord_bot.command(name="unlink")
async def unlink_telegram(ctx: commands.Context):
    """Unlinks your Discord account from any associated Telegram ID."""
    global user_map
    discord_id_str = str(ctx.author.id)
    unlinked = False

    # Find the corresponding Telegram ID
    tg_id = user_map.pop(f"discord:{discord_id_str}", None)

    if tg_id:
        unlinked = True
        user_map.pop(f"telegram:{tg_id}", None)
        # --- Remove potential username link ---
        # We need to find the username associated with this tg_id
        username_to_remove = None
        for key, linked_tg_id in user_map.items():
            if key.startswith("telegram_username:") and linked_tg_id == tg_id:
                username_to_remove = key
                break
        if username_to_remove:
            user_map.pop(username_to_remove, None)
            logger.info(f"Removed username link {username_to_remove} during unlink for Discord user {discord_id_str}")
        # --- End username link removal ---

    if unlinked:
        save_user_map(user_map) # Persist changes
        await ctx.reply(f"✅ Unlinked your Discord account ({ctx.author.mention}).", mention_author=False)
        logger.info(f"Unlinked Discord user {discord_id_str}")
    else:
        await ctx.reply("ℹ️ Your Discord account was not linked.", mention_author=False)




# --- Telegram Bot Setup ---

def build_telegram_bot(discord_channel_obj):
    """Builds and configures the Telegram Application."""
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN not set.")
        return None

    # Prepare the data dictionary
    bot_data_to_assign = {"discord_channel": discord_channel_obj}
    defaults = Defaults(parse_mode=constants.ParseMode.HTML)

    # Configure the builder
    app_builder = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).defaults(defaults)
    # Consider increasing connect/read timeouts if experiencing issues
    # app_builder.connect_timeout(30).read_timeout(30)

    # Build the application FIRST
    app = app_builder.build()

    # Assign bot_data to the built application object AFTER building
    app.bot_data.update(bot_data_to_assign)

    app.add_handler(CommandHandler("myid", my_telegram_id_command))

    # Now add handlers to the 'app' object
    if TELEGRAM_GROUP_ID:
        logger.info(f"Telegram handlers configured for group ID: {TELEGRAM_GROUP_ID}")
        # Message handler (Text, Photo, Sticker - excluding commands and edits)
        # CORRECTED LINE: Use filters.Sticker.ALL
        app.add_handler(
            MessageHandler(
                filters.Chat(chat_id=TELEGRAM_GROUP_ID) & (filters.TEXT | filters.PHOTO | filters.Sticker.ALL) & ~filters.COMMAND & ~filters.UpdateType.EDITED_MESSAGE,
                telegram_forward_message,
            )
        )
        # Edit handler
        app.add_handler(
             MessageHandler(
                filters.Chat(chat_id=TELEGRAM_GROUP_ID) & filters.UpdateType.EDITED_MESSAGE,
                telegram_edit_message
            )
        )
        # Command to get chat ID (still useful for diagnostics)
        app.add_handler(CommandHandler("chatid", get_chat_id_command, filters=filters.Chat(chat_id=TELEGRAM_GROUP_ID)))
        app.add_handler(MessageReactionHandler(handle_telegram_reaction, chat_id=TELEGRAM_GROUP_ID))

    else:
        logger.warning("TELEGRAM_GROUP_ID not set. Forwarding from Telegram disabled.")
        # Allow chatid command anywhere if group not set, for setup purposes
        app.add_handler(CommandHandler("chatid", get_chat_id_command))

    app.add_error_handler(telegram_error_handler)

    # Return the fully configured application object
    return app


# --- Telegram Reaction Handler Function ---
async def handle_telegram_reaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles reaction updates from Telegram and forwards them to Discord."""
    if not update.message_reaction:
        return # Should not happen with this handler, but safety check

    logger.info(f"TG_REACT: Received reaction update for TG msg {update.message_reaction.message_id} in chat {update.message_reaction.chat.id}")

    tg_msg_id = update.message_reaction.message_id
    map_entry = message_map.get(f"telegram:{tg_msg_id}")

    if not map_entry:
        # logger.debug(f"TG_REACT: No Discord message mapping found for TG msg {tg_msg_id}")
        return # Ignore reactions on messages not mapped (e.g., old messages, native TG messages)

    discord_msg_id = map_entry.get("other_id")
    discord_channel = context.bot_data.get("discord_channel")

    if not discord_msg_id or not discord_channel:
        logger.warning(f"TG_REACT: Missing discord_msg_id ({discord_msg_id}) or discord_channel for TG msg {tg_msg_id}")
        return

    try:
        discord_message = await discord_channel.fetch_message(discord_msg_id)
        logger.debug(f"TG_REACT: Fetched corresponding Discord message {discord_msg_id}")

        # --- Process Reactions ---
        # Simplification: Add any emoji in the new_reaction list.
        # More complex logic would involve comparing old_reaction and new_reaction
        # to handle removals, but discord.py doesn't let bots remove *other users'* reactions.
        # So, we only add. If an emoji is already present, add_reaction usually does nothing.

        new_reactions = update.message_reaction.new_reaction
        if not new_reactions:
            logger.info(f"TG_REACT: No new reactions found in update for TG msg {tg_msg_id}. Nothing to add.")
            return

        added_emojis = set()
        for reaction_type in new_reactions:
            if isinstance(reaction_type, ReactionTypeEmoji):
                emoji = reaction_type.emoji
                if emoji not in added_emojis: # Avoid trying to add the same emoji multiple times from one update
                    try:
                        logger.info(f"TG_REACT: Attempting to add reaction '{emoji}' to DC msg {discord_msg_id}")
                        await discord_message.add_reaction(emoji)
                        added_emojis.add(emoji)
                    except discord.HTTPException as e:
                        # Common errors: Unknown Emoji (if it's non-standard unicode), Missing Permissions, Rate Limit
                        logger.warning(f"TG_REACT: Failed to add Discord reaction '{emoji}' to msg {discord_msg_id}: {e}")
                    except Exception as e_inner:
                        logger.error(f"TG_REACT: Unexpected error adding Discord reaction '{emoji}' to msg {discord_msg_id}: {e_inner}", exc_info=True)
            # else: logger.debug(f"TG_REACT: Ignoring non-emoji reaction type: {type(reaction_type)}") # Ignore ReactionTypeCustomEmoji etc.


    except discord.NotFound:
        logger.warning(f"TG_REACT: Discord message {discord_msg_id} not found for reaction forwarding (TG msg {tg_msg_id}). Removing map entry.")
        # Clean up stale map entries potentially
        message_map.pop(f"telegram:{tg_msg_id}", None)
        message_map.pop(f"discord:{discord_msg_id}", None)
    except discord.Forbidden:
        logger.error(f"TG_REACT: Bot lacks permissions (likely 'Add Reactions') to react on Discord message {discord_msg_id}.")
        # Consider stopping reaction forwarding or logging less frequently after first error.
    except Exception as e:
        logger.error(f"TG_REACT: Unexpected error handling Telegram reaction for TG msg {tg_msg_id}: {e}", exc_info=True)

async def my_telegram_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Replies with the user's Telegram ID."""
    if not update.effective_user:
        logger.warning("Could not identify user for /myid command.")
        # Optionally send a generic error message back if possible
        # await update.message.reply_text("Sorry, I couldn't identify your user ID.")
        return

    user_id = update.effective_user.id
    user_name = update.effective_user.full_name
    logger.info(f"COMMAND: /myid called by user {user_name} (ID: {user_id}) in chat {update.effective_chat.id}")

    reply_text = (
        f"Hello {escape_html(user_name)}!\n"
        f"Your Telegram User ID is: <code>{user_id}</code>\n\n"
        f"You can use this ID on Discord with the command:\n"
        f"<code>!link {user_id}</code>"
    )

    try:
        await update.message.reply_html(text=reply_text) # Use reply_html for convenience
        logger.info(f"Successfully sent /myid response to {user_id}")
    except TelegramError as e:
        logger.error(f"Failed to send /myid response to {user_id}: {e}")

async def get_chat_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the chat ID back to the user."""
    logger.info(f"COMMAND: /chatid called in chat {update.effective_chat.id}")
    chat_id = update.effective_chat.id

    # Use HTML code tags instead of backticks
    text = f"ℹ️ Chat ID: <code>{chat_id}</code>"

    if TELEGRAM_GROUP_ID is None:
        # Remove MarkdownV2 escapes, use HTML tags
        text += (
            f"\n\n⚠️ <code>TELEGRAM_GROUP_ID</code> is not set. Set it to <code>{chat_id}</code> "
            f"and restart the bot to enable forwarding from this group."
        )
    elif chat_id != TELEGRAM_GROUP_ID:
        # Remove MarkdownV2 escapes, use HTML tags
        text += (
            f"\n\n⚠️ This chat ID does not match the configured <code>TELEGRAM_GROUP_ID</code>"
            f" (<code>{TELEGRAM_GROUP_ID}</code>). Forwarding is disabled for this chat."
        )

    try:
        # Use HTML parse mode
        await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=constants.ParseMode.HTML
        )
        logger.info(f"Successfully sent /chatid response to {chat_id}")
    except TelegramError as e:
        logger.error(f"Failed to send /chatid response to {chat_id}: {e}")

async def telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Logs errors raised by the Telegram bot library."""
    logger.error(f"Telegram update {update} caused error {context.error}", exc_info=context.error)
    # Consider adding specific error handling, e.g., for Forbidden errors if bot kicked/blocked.


# --- Main Execution ---
async def main():
    # --- Environment Variable Checks ---
    if not DISCORD_BOT_TOKEN: logger.critical("DISCORD_BOT_TOKEN not set."); return
    if not TELEGRAM_BOT_TOKEN: logger.critical("TELEGRAM_BOT_TOKEN not set."); return
    if not DISCORD_CHANNEL_ID: logger.critical("DISCORD_CHANNEL_ID not set."); return
    # TELEGRAM_GROUP_ID can be None initially, user can set it via /chatid

    # Using asyncio.gather to run both bots concurrently
    # discord_bot.start is blocking, so we need run_until_complete or similar
    # Let's try running discord bot and telegram polling separately within gather

    discord_task = None

    try:
        logger.info("Starting bots...")
        # Start Discord bot (which initializes Telegram app in on_ready)
        # discord_bot.start() is blocking, use start=False? No, run discord_bot.start in a task
        discord_task = asyncio.create_task(discord_bot.start(DISCORD_BOT_TOKEN), name="DiscordBot")

        # Wait briefly for discord bot to potentially connect and initialize telegram_app
        # this is terrible, make a channel and wait on it instead.
        await discord_ready_event.wait()

        if hasattr(discord_bot, 'telegram_app') and discord_bot.telegram_app:
            tg_app = discord_bot.telegram_app
            await tg_app.initialize() # Initialize telegram application
            await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            await tg_app.start()
            logger.info("Telegram polling started.")
            # it's okay to only wait on the discord bot
            await discord_task
        else:
            logger.warning("Telegram app was not initialized by Discord bot, only running Discord.")
            await discord_task # Wait only for discord if telegram failed


    except asyncio.CancelledError:
         logger.info("Bot tasks cancelled.")
    except Exception as e:
        logger.critical(f"Main execution loop encountered an error: {e}", exc_info=True)
    finally:
        logger.info("Shutting down...")
        if hasattr(discord_bot, 'telegram_app') and discord_bot.telegram_app and discord_bot.telegram_app.updater and discord_bot.telegram_app.updater._running:
            logger.info("Stopping Telegram polling...")
            await discord_bot.telegram_app.updater.stop()
            await discord_bot.telegram_app.stop()
            await discord_bot.telegram_app.shutdown()

        if discord_bot.is_ready():
             logger.info("Closing Discord connection...")
             await discord_bot.close()
        if discord_task and not discord_task.done():
             discord_task.cancel()

        # Wait briefly for cleanup
        await asyncio.sleep(1)
        logger.info("Shutdown complete.")

def start():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user (KeyboardInterrupt).")

if __name__ == "__main__":
    start()
