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

# Salon secret pour le journal quotidien (Orgas / Spectateurs / Admins)
# S'il n'est pas défini, il vaudra 0 et la tâche automatique ne plantera pas.
RECAP_CHANNEL_ID = int(os.getenv("RECAP_CHANNEL_ID", 0))

# Heure du récapitulatif automatique chaque soir (Fuseau Paris)
HEURE_RECAP = datetime.time(hour=23, minute=0, tzinfo=ZoneInfo("Europe/Paris"))

MAX_CHANNELS_PER_CATEGORY = 45  # Marge de sécurité sous la limite Discord de 50
ROLE_SPECTATEURS_NAME = "Spectateurs"
ROLE_ORGAS_NAME = "Orgas"
NOM_CATEGORIE_ARCHIVE = "📦 ARCHIVES DUOS"

# Initialisation
gemini_client = genai.Client(api_key=GEMINI_KEY)

intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ==========================================
# 1. FONCTIONS DE RÉCAPITULATIF JOURNALIER
# ==========================================

async def generer_et_envoyer_recap_quotidien(guild: discord.Guild, target_channel: discord.TextChannel):
    """Scanne tous les salons duos actifs des dernières 24h et publie une synthèse IA."""
    now = datetime.datetime.now(datetime.timezone.utc)
    depuis = now - datetime.timedelta(hours=24)

    duo_transcripts = []

    for channel in guild.text_channels:
        # On ignore les salons archivés (qui commencent par 🔒arch-)
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
        "1. 🌍 **Synthèse Générale de la Journée**\n"
        "2. 🤝 **Alliances & Pactes confirmés**\n"
        "3. 🎯 **Cibles & Stratégies d'Élimination**\n"
        "4. ⚠️ **Trahisons & Double-Jeu**\n"
        "5. 📌 **Point rapide par duo actif**"
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

        # Découpage si le texte dépasse la limite de Discord (1900 caractères par sécurité)
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
# 3. COMMANDES DUOS & GESTION DES ÉLIMINATIONS
# ==========================================

@bot.tree.command(
    name="creer_duos", 
    description="Génère tous les salons duos privés (utilise la catégorie existante si trouvée)."
)
@app_commands.describe(
    nom_equipe="Nom de la catégorie existante ou à créer (ex: DUOS ROUGE)",
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

    # 3. Récupération de la catégorie existante ou création
    clean_target_name = nom_equipe.strip().lower()
    existing_category = discord.utils.find(lambda c: c.name.lower() == clean_target_name, guild.categories)

    category_index = 1
    if existing_category:
        current_category = existing_category
        channel_count_in_current_cat = len(existing_category.channels)
    else:
        current_category = await guild.create_category(nom_equipe)
        channel_count_in_current_cat = 0

    # 4. Génération des paires
    duos = list(itertools.combinations(roles_list, 2))
    total_duos = len(duos)

    for r1, r2 in duos:
        # Création d'une nouvelle catégorie numérotée si la limite de 45 est atteinte
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
        f"✅ Succès : **{total_duos} salons duos** créés dans la catégorie **{current_category.name}** !", 
        ephemeral=True
    )


@bot.tree.command(
    name="eliminer_candidat", 
    description="Archive tous les salons duos d'un candidat éliminé et retire les accès des 2 participants."
)
@app_commands.describe(
    role_candidat="Le rôle du candidat éliminé (ex: @Lucas)"
)
@app_commands.default_permissions(manage_messages=True)
async def eliminer_candidat(interaction: discord.Interaction, role_candidat: discord.Role):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    # 1. Identifier tous les salons duos où ce rôle a un accès explicite
    targeted_channels = []
    for channel in guild.text_channels:
        if channel.name.startswith("duo-") and role_candidat in channel.overwrites:
            targeted_channels.append(channel)

    if not targeted_channels:
        await interaction.followup.send(
            f"ℹ️ Aucun salon duo actif trouvé pour le rôle {role_candidat.mention}.", 
            ephemeral=True
        )
        return

    # 2. Récupérer ou créer la catégorie d'archive
    archive_categories = [c for c in guild.categories if c.name.lower().startswith(NOM_CATEGORIE_ARCHIVE.lower())]
    if archive_categories:
        current_archive_cat = archive_categories[-1]
    else:
        current_archive_cat = await guild.create_category(NOM_CATEGORIE_ARCHIVE)

    archived_count = 0
    cat_index = len(archive_categories) or 1

    # 3. Archiver et verrouiller chaque salon duo
    for channel in targeted_channels:
        # Créer une nouvelle catégorie d'archive si la précédente est pleine
        if len(current_archive_cat.channels) >= MAX_CHANNELS_PER_CATEGORY:
            cat_index += 1
            current_archive_cat = await guild.create_category(f"{NOM_CATEGORIE_ARCHIVE} - {cat_index}")
            await asyncio.sleep(1)

        # Nouvelle configuration de permissions (retire les deux candidats, conserve Orgas/Spectateurs)
        new_overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, view_channel=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True)
        }

        if role_spectateurs:
            new_overwrites[role_spectateurs] = discord.PermissionOverwrite(
                read_messages=True, 
                view_channel=True, 
                read_message_history=True, 
                send_messages=False
            )

        if role_orgas:
            new_overwrites[role_orgas] = discord.PermissionOverwrite(
                read_messages=True, 
                view_channel=True, 
                read_message_history=True, 
                send_messages=False
            )

        # Renommer et déplacer dans les archives
        new_name = f"🔒arch-{channel.name.replace('duo-', '')}"
        await channel.edit(
            name=new_name,
            category=current_archive_cat,
            overwrites=new_overwrites,
            reason=f"Élimination du candidat {role_candidat.name}"
        )

        archived_count += 1
        await asyncio.sleep(0.6)

    await interaction.followup.send(
        f"🏆 **Élimination enregistrée :** {role_candidat.mention}\n"
        f"📦 **{archived_count} salons duos** ont été archivés dans **{current_archive_cat.name}**.\n"
        f"🔒 Les deux candidats de chaque duo n'ont plus accès à ces salons.",
        ephemeral=True
    )


# ==========================================
# 4. COMMANDES DE SUPPRESSION DE CATÉGORIES
# ==========================================

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
# 5. COMMANDES DE RÉSUMÉ IA
# ==========================================

@bot.tree.command(
    name="resumer", 
    description="Génère un résumé IA (court ou détaillé) des derniers messages du salon."
)
@app_commands.describe(
    format="Choisissez entre un résumé synthétique/rapide ou une analyse complète",
    limite="Nombre de messages récents à analyser (par défaut: 100)"
)
@app_commands.choices(format=[
    app_commands.Choice(name="⚡ Résumé Court (Points clés rapides)", value="court"),
    app_commands.Choice(name="📖 Résumé Long (Analyse détaillée & stratégique)", value="long")
])
@app_commands.default_permissions(manage_messages=True)
async def resumer(interaction: discord.Interaction, format: app_commands.Choice[str], limite: int = 100):
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel

    messages = [msg async for msg in channel.history(limit=limite, oldest_first=True)]
    user_messages = [msg for msg in messages if not msg.author.bot and msg.content.strip()]

    if len(user_messages) < 3:
        await interaction.followup.send("⚠️ Pas assez de messages pour générer un résumé pertinent.", ephemeral=True)
        return

    transcript = "\n".join([f"{msg.author.display_name}: {msg.content}" for msg in user_messages])

    if format.value == "court":
        prompt = (
            "Tu es l'arbitre d'un jeu de stratégie. "
            f"Voici la transcription des messages du salon #{channel.name} :\n\n"
            f"{transcript}\n\n"
            "Fais un résumé **TRÈS COURT, CONCIS ET DIRECT** en 3 à 5 bullet points maximum :\n"
            "- 🎯 Sujet central en 1 phrase\n"
            "- 🤝 Décisions / Alliances actées\n"
            "- ⚠️ Cibles ou menaces identifiées\n"
            "- 🎭 Statut général (Accord, Tensions, Faux-semblants)"
        )
    else:
        prompt = (
            "Tu es l'arbitre et organisateur d'un jeu de stratégie/téléréalité (type Koh-Lanta/Survivor/Secret Story). "
            f"Voici la transcription des messages échangés dans le salon #{channel.name} :\n\n"
            f"{transcript}\n\n"
            "Fais un **RÉSUMÉ DÉTAILLÉ ET APPROFONDI** en français, structuré avec les sections suivantes :\n"
            "1. 🎯 **Analyse Thématique** (détail complet des sujets abordés)\n"
            "2. 🤝 **Alliances, Promesses et Accords** (qui propose quoi, qui accepte, les conditions)\n"
            "3. ⚠️ **Stratégies, Votes & Cibles** (noms des cibles évoquées, justifications, plans A et B)\n"
            "4. 🎭 **Psychologie & Dynamique** (qui manipule qui, niveau de sincérité, hésitations, rapport de force)\n"
            "5. 💬 **Citations ou Moments Clés** (phrases marquantes échangées)"
        )

    try:
        response = gemini_client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt
        )
        summary_text = response.text

        badge_titre = "⚡ Résumé Flash" if format.value == "court" else "📖 Résumé Détaillé"
        embed = discord.Embed(
            title=f"{badge_titre} — #{channel.name}",
            description=summary_text,
            color=discord.Color.gold() if format.value == "court" else discord.Color.purple()
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
