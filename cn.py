from keep_alive import keep_alive
import discord
from discord.ext import commands
import re
import asyncio
import os
import json
from discord import app_commands
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import time
import discord
print("Discord.py Version:", discord.__version__)

keep_alive()

TOKEN = os.getenv('DISCORD_TOKEN') or 'DeinTokenHier'

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
intents.webhooks = True

bot = commands.Bot(command_prefix='!', intents=intents)
tree = bot.tree

# ------------------------
# WHITELIST & SETTINGS
# ------------------------
WHITELIST = {843180408152784936, 1159469934989025290,
             662596869221908480, 830212609961754654,
             235148962103951360, 557628352828014614,
}

AUTO_KICK_IDS = {968608295101292544, 1378153838732640347,
                 1048582528455430184, 1325204584829947914,
                 1318475995291848724, 1399007414488928286,
}

DELETE_TIMEOUT = 3600

invite_violations = {}
user_timeouts = {}
webhook_violations = {}
kick_violations = defaultdict(int)
ban_violations = defaultdict(int)

AUTHORIZED_ROLE_IDS = (1397807696639561759)
MAX_ALLOWED_KICKS = 3
MAX_ALLOWED_BANS = 3

SETUP_BACKUP_WHITELIST = {
    1159469934989025290,  # Beispielhafte User-IDs
    843180408152784936,
}

def is_setup_whitelisted(user_id: int) -> bool:
    return user_id in SETUP_BACKUP_WHITELIST

invite_pattern = re.compile(
    r"(https?:\/\/)?(www\.)?(discord\.gg|discord(app)?\.com\/(invite|oauth2\/authorize))\/\w+|(?:discord(app)?\.com.*invite)", re.I
)

# ------------------------
# Timeout-Spam Tracking (5 Timeouts in 120 Sek -> Kick)
# ------------------------

timeout_actions = defaultdict(list)  # moderator_id : [timestamps]
TIMEOUT_SPAM_LIMIT = 5
TIME_WINDOW = 120  # Sekunden

async def register_timeout_action(guild, moderator_id):
    now = time.time()
    actions = timeout_actions[moderator_id]
    actions.append(now)
    # Alte Aktionen entfernen, die außerhalb des Fensters sind
    timeout_actions[moderator_id] = [t for t in actions if now - t <= TIME_WINDOW]

    if len(timeout_actions[moderator_id]) >= TIMEOUT_SPAM_LIMIT:
        member = guild.get_member(moderator_id)
        if member:
            try:
                await member.kick(reason="Timeout-Spam (mehr als 5 Timeouts in 120 Sekunden)")
                print(f"🥾 {member} wurde wegen Timeout-Spam gekickt.")
                timeout_actions[moderator_id] = []  # Reset nach Kick
            except Exception as e:
                print(f"❌ Fehler beim Kick bei Timeout-Spam: {e}")
@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    # Prüfen, ob 'communication_disabled_until' bei beiden Membern existiert
    if hasattr(before, "communication_disabled_until") and hasattr(after, "communication_disabled_until"):
        if before.communication_disabled_until != after.communication_disabled_until:
            # Wenn ein Timeout neu gesetzt wurde (nicht entfernt)
            if after.communication_disabled_until is not None:
                # Wer hat den Timeout vergeben? → Audit Log prüfen
                async for entry in after.guild.audit_logs(limit=1, action=discord.AuditLogAction.member_update):
                    if entry.target.id == after.id:
                        moderator_id = entry.user.id
                        if not is_whitelisted(moderator_id):
                            await register_timeout_action(after.guild, moderator_id)
                        break

# ------------------------
# HILFSFUNKTIONEN
# ------------------------

def is_whitelisted(user_id):
    return user_id in WHITELIST

async def reset_rules_for_user(user, guild):
    member = guild.get_member(user.id)
    if member:
        try:
            roles_to_remove = [r for r in member.roles if r.name != "@everyone"]
            await member.remove_roles(*roles_to_remove, reason="Reset nach 2x Webhook-Verstoß")
            print(f"🔁 Rollen von {user} entfernt.")
        except Exception as e:
            print(f"❌ Fehler bei Rollenentfernung: {e}")

# ------------------------
# BACKUP / RESET SERVER
# ------------------------

backup_data = {}

def serialize_channel(channel: discord.abc.GuildChannel):
    data = {
        "name": channel.name,
        "type": channel.type,
        "position": channel.position,
        "category_id": channel.category_id,
    }
    if isinstance(channel, discord.TextChannel):
        data.update({
            "topic": channel.topic,
            "nsfw": channel.nsfw,
            "slowmode_delay": channel.slowmode_delay,
            "bitrate": None,
            "user_limit": None,
        })
    elif isinstance(channel, discord.VoiceChannel):
        data.update({
            "bitrate": channel.bitrate,
            "user_limit": channel.user_limit,
            "topic": None,
            "nsfw": None,
            "slowmode_delay": None,
        })
    else:
        data.update({
            "topic": None,
            "nsfw": None,
            "slowmode_delay": None,
            "bitrate": None,
            "user_limit": None,
        })
    return data

async def create_channel_from_backup(guild: discord.Guild, data):
    category = guild.get_channel(data["category_id"]) if data["category_id"] else None

    if data["type"] == discord.ChannelType.text:
        return await guild.create_text_channel(
            name=data["name"],
            topic=data["topic"],
            nsfw=data["nsfw"],
            slowmode_delay=data["slowmode_delay"],
            category=category,
            position=data["position"]
        )
    elif data["type"] == discord.ChannelType.voice:
        return await guild.create_voice_channel(
            name=data["name"],
            bitrate=data["bitrate"],
            user_limit=data["user_limit"],
            category=category,
            position=data["position"]
        )
    elif data["type"] == discord.ChannelType.category:
        return await guild.create_category(
            name=data["name"],
            position=data["position"]
        )
    else:
        return None

@tree.command(name="backup", description="Erstelle ein Backup aller Kanäle im Server.")
async def backup(interaction: discord.Interaction):
    if not is_setup_whitelisted(interaction.user.id):
        await interaction.response.send_message("❌ Du bist nicht berechtigt, diesen Befehl zu verwenden.", ephemeral=True)
        return

    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("❌ Kein Server gefunden.", ephemeral=True)
        return

    channels_data = []
    channels_sorted = sorted(guild.channels, key=lambda c: c.position)

    for ch in channels_sorted:
        channels_data.append(serialize_channel(ch))

    backup_data[guild.id] = channels_data
    await interaction.response.send_message(f"✅ Backup für **{guild.name}** mit {len(channels_data)} Kanälen wurde gespeichert.")

@tree.command(name="reset", description="Starte Reset-Aktion. Optionen: 'server'")
@app_commands.describe(option="Option für Reset, z.B. 'server'")
async def reset(interaction: discord.Interaction, option: str):
    if not is_setup_whitelisted(interaction.user.id):
        await interaction.response.send_message("❌ Du bist nicht berechtigt, diesen Befehl zu verwenden.", ephemeral=True)
        return

    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("❌ Kein Server gefunden.", ephemeral=True)
        return

    if option.lower() != "server":
        await interaction.response.send_message("❌ Unbekannte Option. Nur 'server' ist erlaubt.", ephemeral=True)
        return

    if guild.id not in backup_data:
        await interaction.response.send_message("❌ Kein Backup für diesen Server gefunden. Bitte erst /backup ausführen.", ephemeral=True)
        return

    await interaction.response.send_message("⚠️ Starte Server Reset: Kanäle werden gelöscht und aus Backup wiederhergestellt...", ephemeral=True)

    for ch in guild.channels:
        try:
            await ch.delete(reason="Reset Server durch Bot")
            await asyncio.sleep(0.6)
        except Exception as e:
            print(f"Fehler beim Löschen von Kanal {ch.name}: {e}")

    await asyncio.sleep(3)

    channels_backup = backup_data[guild.id]

    categories = [c for c in channels_backup if c["type"] == discord.ChannelType.category]
    category_map = {}

    for cat_data in categories:
        cat = await create_channel_from_backup(guild, cat_data)
        if cat:
            category_map[cat_data["name"]] = cat

    for ch_data in channels_backup:
        if ch_data["type"] == discord.ChannelType.category:
            continue

        if ch_data["category_id"]:
            orig_cat = guild.get_channel(ch_data["category_id"])
            cat_name = orig_cat.name if orig_cat else None
            if cat_name in category_map:
                ch_data["category_id"] = category_map[cat_name].id
            else:
                ch_data["category_id"] = None
        else:
            ch_data["category_id"] = None

        await create_channel_from_backup(guild, ch_data)
        await asyncio.sleep(0.6)
    await interaction.followup.send("✅ Server Reset abgeschlossen. Kanäle wurden wiederhergestellt.")

# ------------------------
# EVENTS
# ------------------------

@bot.event
async def on_ready():
    print(f'✅ {bot.user} ist online!')
    try:
        synced = await tree.sync()
        print(f"🔃 {len(synced)} Slash-Commands synchronisiert.")
    except Exception as e:
        print("❌ Fehler beim Slash-Sync:", e)

@bot.event
async def on_member_join(member):
    # Bot-Join-Schutz und Auto-Kick IDs (Kein Account-Alter-Check mehr)
    if member.bot and not is_whitelisted(member.id):
        async for entry in member.guild.audit_logs(limit=5, action=discord.AuditLogAction.bot_add):
            if entry.target.id == member.id:
                adder = entry.user
                if adder and not is_whitelisted(adder.id):
                    try:
                        await adder.ban(reason="🛡️ Bot-Join-Schutz: Nutzer hat Bot hinzugefügt")
                        await member.ban(reason="🛡️ Bot-Join-Schutz: Bot wurde entfernt")
                        print(f"🥾 {adder} und Bot {member} wurden wegen Bot-Join-Schutz gekickt.")
                    except Exception as e:
                        print(f"❌ Fehler beim Kick (Bot-Join-Schutz): {e}")
                break
        return

    if member.id in AUTO_KICK_IDS:
        try:
            await member.kick(reason="Auto-Kick: Gelistete ID")
            print(f"🥾 {member} wurde automatisch gekickt (gelistete ID).")
        except Exception as e:
            print(f"❌ Fehler beim Auto-Kick: {e}")
        return

@bot.event
async def on_webhooks_update(channel):
    print(f"🔄 Webhook Update erkannt in {channel.name}")
    await asyncio.sleep(0)
    try:
        webhooks = await channel.webhooks()
        for webhook in webhooks:
            print(f"🧷 Webhook gefunden: {webhook.name} ({webhook.id})")
            if webhook.user and is_whitelisted(webhook.user.id):
                print(f"✅ Whitelisted: {webhook.user}")
                continue
            user = None
            async for entry in channel.guild.audit_logs(limit=10, action=discord.AuditLogAction.webhook_create):
                if entry.target and entry.target.id == webhook.id:
                    user = entry.user
                    break
            await webhook.delete(reason="🔒 Unautorisierter Webhook")
            print(f"❌ Webhook {webhook.name} gelöscht")
            if user and not is_whitelisted(user.id):
                count = webhook_violations.get(user.id, 0) + 1
                webhook_violations[user.id] = count
                print(f"⚠ Webhook-Verstoß #{count} von {user}")
                if count >= 2:
                    await reset_rules_for_user(user, channel.guild)
    except Exception as e:
        print("❌ Fehler bei Webhook Handling:")
        import traceback
        traceback.print_exc()

@bot.event
async def on_message(message):
    if is_whitelisted(message.author.id):
        await bot.process_commands(message)
        return
    now_ts = datetime.now(timezone.utc).timestamp()
    if message.author.id in user_timeouts:
        if user_timeouts[message.author.id] > now_ts:
            try:
                await message.delete()
                print(f"🚫 Nachricht von getimtem User {message.author} gelöscht.")
            except:
                pass
            return
        else:
            del user_timeouts[message.author.id]
    if invite_pattern.search(message.content):
        try:
            await message.delete()
            print(f"🚫 Invite-Link gelöscht von {message.author}")
        except Exception as e:
            print(f"❌ Fehler beim Invite-Löschen: {e}")
        count = invite_violations.get(message.author.id, 0) + 1
        invite_violations[message.author.id] = count
        print(f"⚠ Invite-Verstoß #{count} von {message.author}")
        if count >= 3:
            try:
                await message.author.timeout(duration=DELETE_TIMEOUT, reason="🔇 3x Invite-Verstoß")
                user_timeouts[message.author.id] = now_ts + DELETE_TIMEOUT
                print(f"⏱ {message.author} wurde für 1 Stunde getimeoutet.")
            except Exception as e:
                print(f"❌ Fehler beim Timeout: {e}")
    await bot.process_commands(message)

# ------------------------
# Rollenlösch-, Kanallösch- & Kanal-Erstell-Schutz mit Kick (Ersetzt Nr.6)
# ------------------------

@bot.event
async def on_guild_role_delete(role):
    guild = role.guild
    async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.role_delete):
        if entry.target.id == role.id:
            user = entry.user
            break
    else:
        return
    if not user or is_whitelisted(user.id):
        return
    member = guild.get_member(user.id)
    if member:
        try:
            await member.kick(reason="🧪 Rolle gelöscht ohne Erlaubnis")
            print(f"🥾 {member} wurde gekickt (Rolle gelöscht).")
        except Exception as e:
            print(f"❌ Fehler beim Kick: {e}")

@bot.event
async def on_guild_channel_delete(channel):
    guild = channel.guild
    async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.channel_delete):
        if entry.target.id == channel.id:
            user = entry.user
            break
    else:
        return
    if not user or is_whitelisted(user.id):
        return
    member = guild.get_member(user.id)
    if member:
        try:
            await member.kick(reason="🧪 Kanal gelöscht ohne Erlaubnis")
            print(f"🥾 {member} wurde gekickt (Kanal gelöscht).")
        except Exception as e:
            print(f"❌ Fehler beim Kick: {e}")

@bot.event
async def on_guild_channel_create(channel):
    guild = channel.guild
    async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.channel_create):
        if entry.target.id == channel.id:
            user = entry.user
            break
    else:
        return
    if not user or is_whitelisted(user.id):
        return
    member = guild.get_member(user.id)
    if member:
        try:
            await member.kick(reason="🧪 Kanal erstellt ohne Erlaubnis")
            print(f"🥾 {member} wurde gekickt (Kanal erstellt).")
        except Exception as e:
            print(f"❌ Fehler beim Kick: {e}")

# ------------------------
# 1. Fehlender Abschnitt: Channel Namen-Änderung
# ------------------------

@bot.event
async def on_guild_channel_update(before, after):
    if before.name != after.name:
        # Überprüfen ob die Änderung von einem Whitelisted User stammt
        guild = after.guild
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.channel_update):
            if entry.target.id == after.id and entry.before.name == before.name and entry.after.name == after.name:
                user = entry.user
                if not user or is_whitelisted(user.id):
                    return
                member = guild.get_member(user.id)
                if member:
                    try:
                        await member.kick(reason="🧪 Kanalnamen ohne Erlaubnis geändert")
                        print(f"🥾 {member} wurde gekickt (Kanalname geändert).")
                    except Exception as e:
                        print(f"❌ Fehler beim Kick (Channel Name Change): {e}")
                break

# ------------------------
# 2. Ban/Kick Sicherheitsmechanismus (Erweitert)
# ------------------------

@bot.event
async def on_member_ban(guild, user):
    # Erkennen, wer gebannt hat
    async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban):
        if entry.target.id == user.id:
            moderator = entry.user
            break
    else:
        return

    if moderator is None:
        return

    if is_whitelisted(moderator.id):
        return

    # Spezialrolle prüfen
    member = guild.get_member(moderator.id)
    has_special_role = False
    if member:
        has_special_role = any(role.id in AUTHORIZED_ROLE_IDS for role in member.roles)

    if has_special_role:
        ban_violations[moderator.id] += 1
        if ban_violations[moderator.id] > MAX_ALLOWED_BANS:
            try:
                await member.kick(reason="🔒 Spezialrolle hat Bann-Limit überschritten")
                print(f"🥾 {member} wurde wegen Bann-Limit gekickt.")
            except Exception as e:
                print(f"❌ Fehler beim Kick (Ban-Limit): {e}")
    else:
        # Kein Whitelist und keine Spezialrolle: Kick sofort
        try:
            if member:
                await member.kick(reason="🔒 Bann ohne Erlaubnis")
                print(f"🥾 {member} wurde wegen unautorisiertem Bann gekickt.")
        except Exception as e:
            print(f"❌ Fehler beim Kick (Ban): {e}")

@bot.event
async def on_member_kick(guild, user):
    # Discord.py hat kein on_member_kick Event, wir brauchen workaround
    pass

@bot.event
async def on_member_remove(member):
    # Hier versuchen wir rauszufinden, ob es ein Kick war
    # Wir prüfen Audit-Logs der letzten Sekunden auf Kick-Einträge
    guild = member.guild
    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
            time_diff = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
            if entry.target.id == member.id and time_diff < 10:
                moderator = entry.user
                if is_whitelisted(moderator.id):
                    return
                mod_member = guild.get_member(moderator.id)
                has_special_role = False
                if mod_member:
                    has_special_role = any(role.id in AUTHORIZED_ROLE_IDS for role in mod_member.roles)
                if has_special_role:
                    kick_violations[moderator.id] += 1
                    if kick_violations[moderator.id] > MAX_ALLOWED_KICKS:
                        try:
                            await mod_member.kick(reason="🔒 Spezialrolle hat Kick-Limit überschritten")
                            print(f"🥾 {mod_member} wurde wegen Kick-Limit gekickt.")
                        except Exception as e:
                            print(f"❌ Fehler beim Kick (Kick-Limit): {e}")
                else:
                    # Kein Whitelist und keine Spezialrolle: Kick sofort
                    try:
                        if mod_member:
                            await mod_member.kick(reason="🔒 Kick ohne Erlaubnis")
                            print(f"🥾 {mod_member} wurde wegen unautorisiertem Kick gekickt.")
                    except Exception as e:
                        print(f"❌ Fehler beim Kick (Kick): {e}")
                break
    except Exception as e:
        print(f"❌ Fehler beim Kick-Check on_member_remove: {e}")

# ------------------------
# Bot starten
# ------------------------

bot.run(TOKEN)