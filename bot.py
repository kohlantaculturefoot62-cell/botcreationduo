import os
import itertools
import asyncio
import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from google import genai

# --- CONFIGURATION & ENVIRONNEMENT ---
TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

# Salon secret pour le récapitulatif quotidien (Orgas / Spectateurs / Admins)
# Définissez RECAP_CHANNEL_ID dans vos variables d'environnement
RECAP_CHANNEL_ID = int(os.getenv("RECAP_CHANNEL_ID", 0))

# Heure du récapitulatif automatique chaque soir (Fuseau Paris)
HEURE_RECAP = datetime.time(hour=23, minute=0, tzinfo=ZoneInfo("Europe/Paris"))

MAX_CHANNELS_PER_CATEGORY = 45  # Marge de sécurité sous la limite Discord de 50
ROLE_SPECTATEURS_NAME = "Spectateurs"
ROLE_ORGAS_NAME = "Orgas"

# Initialisation du client IA et du Bot Discord
gemini_client = genai.Client(api_key=GEMINI_KEY)

intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ==========================================
# 1. FONCTIONS DE RÉCAPITULATIF JOURNALIER
# ==========================================

async def generer_et_envoyer_recap_quotidien(guild: discord.Guild, target_channel: discord.TextChannel):
    """Scanne tous les salons duos des dernières 24h et publie une synthèse IA."""
    now = datetime.datetime.now(datetime.timezone.utc)
    depuis = now - datetime.timedelta(hours=24)

    duo_transcripts = []

    # Scanner tous les salons de duos
    for channel in guild.text_channels:
        if channel.name.startswith("duo-"):
            messages = [
                msg async for msg in channel.history(after=depuis, oldest_first=True)
                if not msg.author.bot and msg.content.strip()
            ]

            if messages:
                duo_name = channel.name.replace("duo-", "").replace("-", " & ")
                lines = [f"[{msg.created_at.strftime('%H:%M')}] {msg.author.display_name}: {msg.content}" for msg in messages]
                duo_transcripts.append(f"=== DUO : {duo_name} ({len(messages)} messages) ===\n" + "\n".join(lines))

    if not duo_transcripts:
        await target_channel.send("😴 **Journal du jour :** Aucun échange dans les salons duos au cours des dernières 24 heures.")
        return

    full_context = "\n\n".join(duo_transcripts)

    prompt = (
        "Tu es l'arbitre en chef d'un jeu de stratégie et d'alliances (type Koh-Lanta / Survivor / Secret Story).\n"
        "Voici l'ensemble des discussions privées échangées aujourd'hui dans les différents salons duos :\n\n"
        f"{full_context}\n\n"
        "Rédige le **Journal de Bord Stratégique de la Journée** pour l'équipe d'organisation (Orgas/Spectateurs).\n"
        "Structure ta réponse avec des titres clairs et des emojis :\n"
        "1. 🌍 **Synthèse Générale de la Journée** (ambiance, intensité des complots, dynamiques)\n"
        "2. 🤝 **Alliances & Pactes confirmés** (qui s'associe avec qui ?)\n"
        "3. 🎯 **Cibles & Stratégies d'Élimination** (qui est en danger ? qui mène les votes ?)\n"
        "4. ⚠️ **Trahisons & Double-Jeu** (incohérences, mensonges découverts, candidats en ballotage)\n"
        "5. 📌 **Point rapide par duo actif** (1 ou 2 phrases résumant l'essentiel par salon)"
    )

    try:
        response = gemini_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt
        )
        recap_text = response.text

        date_str = datetime.datetime.now(ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y")
        header = f"📰 **JOURNAL STRATÉGIQUE DU {date_str} — DUOS**\n*(Réservé aux Orgas, Spectateurs et Admins)*\n\n"
        full_message = header + recap_text

        # Découpage si le texte dépasse 1900 caractères
        for chunk in [full_message[i:i + 1900] for i in range(0, len(full_message), 1900)]:
            await target_channel.send(chunk)

    except Exception as e:
        await target_channel.send(f"❌ Erreur lors de la génération du récapitulatif : {e}")


@tasks.loop(time=HEURE_RECAP)
async def tache_recap_quotidien():
    """Tâche automatique planifiée chaque soir à 23h00."""
    if RECAP_CHANNEL_ID == 0:
        return
    channel = bot.get_channel(RECAP_CHANNEL_ID)
    if channel:
        await generer_et_envoyer_recap_quotidien(channel.guild, channel)
    else:
        print(f"⚠️ Salon de récapitulatif (ID: {RECAP_CHANNEL_ID}) introuvable.")


# ==========================================
# 2. ÉVÉNEMENT ON_READY
# ==========================================

@bot.event
async def on_ready():
    await bot.tree.sync()
    if not tache_recap_quotidien.is_running() and RECAP_CHANNEL_ID != 0:
        tache_recap_quotidien.start()
    print(f"🤖 Bot connecté en tant que : {bot.user}")


# ==========================================
# 3. COMMANDES SLASH DUOS & CATÉGORIES
# ==========================================

@bot.tree.command(name="creer_duos", description="Génère tous les salons duos privés avec accès Orgas & Spectateurs.")
@app_commands.describe(
    nom_equipe="Nom de base pour les catégories (ex: Duos Rouge)",
    roles="Mentionne les rôles séparés par des espaces (ex: @Candidat1 @Candidat2 ...)"
)
async def creer_duos(interaction: discord.Interaction, nom_equipe: str, roles: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # 1. Extraction des rôles candidats
    role_ids = [int(r.strip("<@&>")) for r in roles.split() if r.startswith("<@&") and r.endswith(">")]
    roles_list = [guild.get_role(r_id) for r_id in role_ids if guild.get_role(r_id) is not None]

    if len(roles_list) < 2:
        await interaction.followup.send("❌ Veuillez mentionner au moins 2 rôles valides.", ephemeral=True)
        return

    # 2. Récupération des rôles Spectateurs et Orgas
    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    # 3. Génération des paires
    duos = list(itertools.combinations(roles_list, 2))
    total_duos = len(duos)

    category_index = 1
    current_category = await guild.create_category(f"{nom_equipe} - {category_index}")
    channel_count_in_current_cat = 0

    for r1, r2 in duos:
        # Nouvelle catégorie si la courante est pleine
        if channel_count_in_current_cat >= MAX_CHANNELS_PER_CATEGORY:
            category_index += 1
            current_category = await guild.create_category(f"{nom_equipe} - {category_index}")
            channel_count_in_current_cat = 0
            await asyncio.sleep(1)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, view_channel=False),
            r1: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True),
            r2: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True)
        }

        # Ajout Spectateurs (Lecture seule)
        if role_spectateurs:
            overwrites[role_spectateurs] = discord.PermissionOverwrite(
                read_messages=True, 
                view_channel=True, 
                read_message_history=True, 
                send_messages=False
            )

        # Ajout Orgas (Lecture + Écriture)
        if role_orgas:
            overwrites[role_orgas] = discord.PermissionOverwrite(
                read_messages=True, 
                view_channel=True, 
                read_message_history=True, 
                send_messages=True
            )

        nom_salon = f"duo-{r1.name.lower().replace(' ', '-')}-{r2.name.lower().replace(' ', '-')}"
        await guild.create_text_channel(name=nom_salon, category=current_category, overwrites=overwrites)
        channel_count_in_current_cat += 1

        await asyncio.sleep(0.6)  # Pause anti-rate-limit

    await interaction.followup.send(
        f"✅ Succès : **{total_duos} salons duos** créés avec accès pour **{role_spectateurs.name if role_spectateurs else 'Spectateurs (non trouvé)'}** et **{role_orgas.name if role_orgas else 'Orgas (non trouvé)'}** !", 
        ephemeral=True
    )


@bot.tree.command(name="supprimer_categorie", description="Supprime une catégorie entière et ses salons.")
@app_commands.describe(nom_categorie="Nom exact de la catégorie (ex: Duos Rouge - 1)")
@app_commands.default_permissions(administrator=True)
async def supprimer_categorie(interaction: discord.Interaction, nom_categorie: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    category = discord.utils.find(lambda c: c.name.lower() == nom_categorie.strip().lower(), guild.categories)
    if not category:
        await interaction.followup.send(f"❌ Catégorie **{nom_categorie}** introuvable.", ephemeral=True)
        return

    channels_to_delete = category.channels
    total_channels = len(channels_to_delete)

    for channel in channels_to_delete:
        try:
            await channel.delete(reason="Nettoyage")
            await asyncio.sleep(0.4)
        except Exception:
            pass

    await category.delete(reason="Nettoyage")
    await interaction.followup.send(f"🗑️ Catégorie **{nom_categorie}** et ses **{total_channels} salons** supprimés !", ephemeral=True)


@bot.tree.command(name="purger_equipe_duos", description="Supprime toutes les catégories commençant par ce nom.")
@app_commands.describe(prefixe="Début du nom des catégories (ex: Duos Rouge)")
@app_commands.default_permissions(administrator=True)
async def purger_equipe_duos(interaction: discord.Interaction, prefixe: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    categories_to_delete = [c for c in guild.categories if c.name.lower().startswith(prefixe.strip().lower())]
    if not categories_to_delete:
        await interaction.followup.send(f"❌ Aucune catégorie ne commence par **{prefixe}**.", ephemeral=True)
        return

    total_channels = 0
    for cat in categories_to_delete:
        for ch in cat.channels:
            try:
                await ch.delete(reason="Purge")
                total_channels += 1
                await asyncio.sleep(0.4)
            except Exception:
                pass
        await cat.delete(reason="Purge")
        await asyncio.sleep(0.5)

    await interaction.followup.send(f"🗑️ Nettoyage : **{len(categories_to_delete)} catégories** et **{total_channels} salons** supprimés !", ephemeral=True)


# ==========================================
# 4. COMMANDES DE RÉSUMÉ IA
# ==========================================

@bot.tree.command(
    name="resumer", 
    description="Génère un résumé IA des derniers messages du salon (visible uniquement par vous)."
)
@app_commands.describe(limite="Nombre de messages récents à analyser (par défaut: 100)")
@app_commands.default_permissions(manage_messages=True)
async def resumer(interaction: discord.Interaction, limite: int = 100):
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel

    messages = [msg async for msg in channel.history(limit=limite, oldest_first=True)]
    user_messages = [msg for msg in messages if not msg.author.bot and msg.content.strip()]

    if len(user_messages) < 3:
        await interaction.followup.send("⚠️ Pas assez de messages pour générer un résumé pertinent.", ephemeral=True)
        return

    transcript = "\n".join([f"{msg.author.display_name}: {msg.content}" for msg in user_messages])

    prompt = (
        "Tu es l'arbitre et organisateur d'un jeu de stratégie/téléréalité (type Koh-Lanta/Survivor/Secret Story). "
        f"Voici la transcription des messages échangés dans le salon #{channel.name} :\n\n"
        f"{transcript}\n\n"
        "Fais un résumé clair, synthétique et structuré en français en précisant :\n"
        "1. 🎯 **Sujets abordés**\n"
        "2. 🤝 **Accords, Alliances ou Tensions** entre les participants\n"
        "3. ⚠️ **Stratégies ou informations clés** (cibles de vote, plans, secrets partagés)\n"
        "4. 🎭 **Ambiance générale / Dynamique du groupe**"
    )

    try:
        response = gemini_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt
        )
        summary_text = response.text

        embed = discord.Embed(
            title=f"📋 Résumé IA — #{channel.name}",
            description=summary_text,
            color=discord.Color.purple()
        )
        embed.set_footer(text=f"Analyse basée sur les {len(user_messages)} derniers messages.")

        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"❌ Erreur lors de la génération du résumé : {e}", ephemeral=True)


@bot.tree.command(
    name="forcer_recap_jour",
    description="Génère immédiatement le journal stratégique des duos des dernières 24h."
)
@app_commands.default_permissions(administrator=True)
async def forcer_recap_jour(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    target_channel = bot.get_channel(RECAP_CHANNEL_ID) or interaction.channel
    await interaction.followup.send(f"⏳ Génération du journal stratégique en cours dans {target_channel.mention}...", ephemeral=True)

    await generer_et_envoyer_recap_quotidien(interaction.guild, target_channel)


# --- DÉMARRAGE DU BOT ---
bot.run(TOKEN)
