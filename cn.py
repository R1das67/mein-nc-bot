import os
import re
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque

import discord
from discord import AuditLogAction, Forbidden, HTTPException, NotFound, Member

# --------------------------
# Konfiguration
# --------------------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Umgebungsvariable DISCORD_TOKEN fehlt.")

# Feste IDs im Code
WHITELIST_IDS = {662596869221908480,843180408152784936,
                 1271186898408308789,1197862712408014909,
                 557628352828014614,651095740390834176,
                 235148962103951360,
}

# Regex für Discord-Invite-Links
INVITE_REGEX = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord\.com/invite)/[A-Za-z0-9\-]+",
    re.IGNORECASE,
)

# Anti-Invite-Spanm: max 5 Invites in 15s
INVITE_WINDOW_SECONDS = 15
INVITE_MAX_IN_WINDOW = 5
TIMEOUT_DURATION = timedelta(hours=1)

# Webhook-Versuche bis Kick
WEBHOOK_MAX_ATTEMPTS = 3

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("guardbot")

# --------------------------
# Discord Client / Intents
# --------------------------
intents = discord.Intents.default()
intents.message_content = True     # Für Invite-Erkennung
intents.members = True             # Für Member-Events
intents.guilds = True
intents.guild_reactions = False
intents.presences = False

bot = discord.Client(intents=intents)

# --------------------------
# Zustände / Speicher
# --------------------------
# user_id -> deque[timestamps] der Invite-Posts
invite_events: dict[int, deque[datetime]] = defaultdict(lambda: deque(maxlen=50))

# user_id -> Anzahl nicht erlaubter Webhook-Erstellungen
webhook_attempts: defaultdict[int, int] = defaultdict(int)

# Kleine Hilfs-Rate-Limiter fürs Audit-Log (um API zu schonen)
last_audit_lookup: dict[tuple[int, str], datetime] = {}

# --------------------------
# Utilities
# --------------------------
def is_whitelisted(user_id: int) -> bool:
    return user_id in WHITELIST_IDS

async def safe_kick(guild: discord.Guild, member: Member, reason: str):
    try:
        if member and guild.get_member(member.id):
            await guild.kick(member, reason=reason)
            log.info(f"Gekickt: {member} ({member.id}) – {reason}")
    except Forbidden:
        log.warning(f"Kick verboten für {member} ({member.id}) – fehlende Rechte?")
    except HTTPException as e:
        log.warning(f"Kick fehlgeschlagen für {member} ({member.id}): {e}")

async def try_timeout(member: Member, duration: timedelta, reason: str) -> bool:
    try:
        until = discord.utils.utcnow() + duration
        await member.edit(timed_out_until=until, reason=reason)
        log.info(f"Timeout: {member} ({member.id}) für {duration} – {reason}")
        return True
    except Forbidden:
        log.warning(f"Timeout verboten für {member} – versuche Kick …")
        return False
    except HTTPException as e:
        log.warning(f"Timeout fehlgeschlagen für {member}: {e}")
        return False

async def find_audit_actor(
    guild: discord.Guild,
    action: AuditLogAction,
    target_id: int | None = None,
    within_seconds: int = 20,
) -> discord.User | None:
    """
    Hole den Täter (actor) aus dem Audit-Log für eine frische Aktion.
    Optional: auf target_id matchen und nur Einträge der letzten N Sekunden betrachten.
    """
    # Rate-Limit pro Aktion
    key = (guild.id, f"{action}:{target_id}")
    now = datetime.now(timezone.utc)
    if key in last_audit_lookup and (now - last_audit_lookup[key]).total_seconds() < 1.0:
        await asyncio.sleep(1)
    last_audit_lookup[key] = now

    try:
        async for entry in guild.audit_logs(limit=8, action=action):
            # Zeitfenster prüfen
            if entry.created_at and (now - entry.created_at).total_seconds() > within_seconds:
                continue
            if target_id is not None:
                # target kann Snowflake mit .id sein
                tgt = getattr(entry.target, "id", None)
                if tgt != target_id:
                    continue
            return entry.user  # Täter
    except Forbidden:
        log.warning("Keine Berechtigung: View Audit Log")
    except HTTPException as e:
        log.warning(f"Audit-Log-Fehler: {e}")
    return None

# --------------------------
# Events
# --------------------------
@bot.event
async def on_ready():
    log.info(f"Eingeloggt als {bot.user} (ID: {bot.user.id})")
    log.info(f"Whitelist: {sorted(WHITELIST_IDS) if WHITELIST_IDS else 'leer'}")

@bot.event
async def on_message(message: discord.Message):
    # Bot-eigene Nachrichten ignorieren
    if message.author.bot or message.guild is None:
        return

    # Invite-Links finden & löschen
    if INVITE_REGEX.search(message.content or ""):
        try:
            await message.delete()
            log.info(f"Invite gelöscht von {message.author} in #{message.channel}")
        except (Forbidden, NotFound):
            pass
        except HTTPException as e:
            log.warning(f"Invite-Löschung fehlgeschlagen: {e}")

        # Anti-Invite-Spam (Whitelist ausgenommen)
        if not is_whitelisted(message.author.id):
            now = datetime.now(timezone.utc)
            dq = invite_events[message.author.id]
            dq.append(now)

            # Fenster säubern
            while dq and (now - dq[0]).total_seconds() > INVITE_WINDOW_SECONDS:
                dq.popleft()

            if len(dq) >= INVITE_MAX_IN_WINDOW:
                member = message.guild.get_member(message.author.id)
                if member:
                    reason = f"Invite-Spam: ≥{INVITE_MAX_IN_WINDOW} in {INVITE_WINDOW_SECONDS}s"
                    ok = await try_timeout(member, TIMEOUT_DURATION, reason)
                    if not ok:
                        await safe_kick(message.guild, member, reason)

@bot.event
async def on_member_join(member: Member):
    # Bot hinzugefügt?
    if member.bot:
        await asyncio.sleep(1)  # kurz warten, bis Audit-Log geschrieben ist
        actor = await find_audit_actor(member.guild, AuditLogAction.bot_add, target_id=member.id)
        if actor and not is_whitelisted(actor.id):
            # Bot kicken
            await safe_kick(member.guild, member, "Nicht-whitelisteter Bot-Invite")
            # Einladenden kicken
            inviter_member = member.guild.get_member(actor.id)
            if inviter_member:
                await safe_kick(member.guild, inviter_member, "Bot-Invite ohne Whitelist")

@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    await asyncio.sleep(1)
    actor = await find_audit_actor(channel.guild, AuditLogAction.channel_delete, target_id=channel.id)
    if actor and not is_whitelisted(actor.id):
        m = channel.guild.get_member(actor.id)
        if m:
            await safe_kick(channel.guild, m, "Kanal gelöscht ohne Whitelist")

@bot.event
async def on_guild_role_delete(role: discord.Role):
    await asyncio.sleep(1)
    actor = await find_audit_actor(role.guild, AuditLogAction.role_delete, target_id=role.id)
    if actor and not is_whitelisted(actor.id):
        m = role.guild.get_member(actor.id)
        if m:
            await safe_kick(role.guild, m, "Rolle gelöscht ohne Whitelist")

@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User | discord.Member):
    await asyncio.sleep(1)
    actor = await find_audit_actor(guild, AuditLogAction.ban, target_id=user.id)
    if actor and not is_whitelisted(actor.id):
        m = guild.get_member(actor.id)
        if m:
            await safe_kick(guild, m, "Ban ohne Whitelist")

@bot.event
async def on_member_remove(member: Member):
    """
    Unterscheide freiwilligen Leave vs Kick:
    Prüfe Audit-Log auf jüngsten MEMBER_KICK für dieses Target.
    """
    await asyncio.sleep(1)
    guild = member.guild
    actor = await find_audit_actor(guild, AuditLogAction.kick, target_id=member.id)
    if actor and not is_whitelisted(actor.id):
        m = guild.get_member(actor.id)
        if m:
            await safe_kick(guild, m, "Kick ohne Whitelist")

@bot.event
async def on_webhooks_update(channel: discord.abc.GuildChannel):
    """
    Wird ausgelöst, wenn Webhooks eines Kanals geändert wurden.
    - Suche im Audit-Log nach WEBHOOK_CREATE/UPDATE.
    - Lösche Webhooks von nicht-whitelisteten Erstellern.
    - Nach 3 Versuchen: Kick des Erstellers.
    """
    guild = channel.guild
    await asyncio.sleep(1)

    # Prüfe neueste Webhook-Create-Einträge
    try:
        async for entry in guild.audit_logs(limit=6, action=AuditLogAction.webhook_create):
            # nur sehr frische Einträge betrachten
            now = datetime.now(timezone.utc)
            if entry.created_at and (now - entry.created_at).total_seconds() > 30:
                continue

            actor = entry.user
            target_webhook = entry.target  # sollte ein Webhook-Objekt mit id sein
            if actor is None or target_webhook is None:
                continue

            if is_whitelisted(actor.id):
                continue  # erlaubt

            # Versuche, den konkreten Webhook per ID zu finden und zu löschen
            wh = None
            try:
                hooks = await channel.webhooks()
                wh = next((w for w in hooks if w.id == getattr(target_webhook, "id", None)), None)
                if wh is None:
                    # Fallback: in gesamter Guild suchen
                    for ch in guild.text_channels:
                        try:
                            for w in await ch.webhooks():
                                if w.id == getattr(target_webhook, "id", None):
                                    wh = w
                                    break
                            if wh:
                                break
                        except Forbidden:
                            continue
                if wh:
                    await wh.delete(reason="Webhook von nicht-whitelistetem Nutzer")
                    log.info(f"Webhook gelöscht (ID {wh.id}) – Actor {actor} ({actor.id})")
            except Forbidden:
                log.warning("Fehlende Rechte zum Löschen von Webhooks.")
            except HTTPException as e:
                log.warning(f"Webhook-Löschung fehlgeschlagen: {e}")

            # Versuche zählen und ggf. User kicken
            webhook_attempts[actor.id] += 1
            if webhook_attempts[actor.id] >= WEBHOOK_MAX_ATTEMPTS:
                member = guild.get_member(actor.id)
                if member:
                    await safe_kick(guild, member, "Mehrfach unzulässige Webhook-Erstellung")
                # Zähler zurücksetzen, damit nicht sofort erneut getriggert wird
                webhook_attempts[actor.id] = 0

    except Forbidden:
        log.warning("Keine Berechtigung auf Audit-Log oder Webhooks.")
    except HTTPException as e:
        log.warning(f"Audit-Log/Webhook Fehler: {e}")

# --------------------------
# Start
# --------------------------
if __name__ == "__main__":
    # Hinweis: Auf Railway genügt "python main.py"
    bot.run(TOKEN)

