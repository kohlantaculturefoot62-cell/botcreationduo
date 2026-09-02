import os
import re
import itertools
import asyncio
import datetime
import unicodedata
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from google import genai

# --- CONFIGURATION & ENVIRONNEMENT ---
TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

# Modèle économique et rapide Flash-Lite
MODEL_NAME = "gemini-3.5-flash-lite"

# Salon secret pour le journal quotidien et les fiches confessionnal
RECAP_CHANNEL_ID = int(os.getenv("RECAP_CHANNEL_ID", 0))

# Planification des tâches automatiques (Fuseau Paris)
HEURE_RECAP = datetime.time(hour=23, minute=0, tzinfo=ZoneInfo("Europe/Paris"))
HEURE_QUESTIONS = datetime.time(hour=9, minute=0, tzinfo=ZoneInfo("Europe/Paris"))

MAX_CHANNELS_PER_CATEGORY = 45
ROLE_SPECTATEURS_NAME = "Spectateurs"
ROLE_ORGAS_NAME = "Orgas"
NOM_CATEGORIE_ARCHIVE = "📦 ARCHIVES DUOS"

# Mots-clés des catégories candidates à analyser
CATEGORIES_CIBLES = [
    "confessional",
    "confessionnal",
    "camps",
    "camp",
    "duo jaune",
    "duo rouge",
    "trio",
    "quatuor",
    "equipe rouge",
    "equipe jaune"
]

# Initialisation
gemini_client = genai.Client(api_key=GEMINI_KEY)

intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ==========================================
# 0. FONCTIONS UTILITAIRES & SÉCURITÉ
# ==========================================

def est_orga_ou_admin(interaction: discord.Interaction) -> bool:
    """Vérifie si l'utilisateur est Administrateur ou possède le rôle Orgas."""
    if not interaction.guild:
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    role_orga = discord.utils.get(interaction.guild.roles, name=ROLE_ORGAS_NAME)
    return (role_orga in interaction.user.roles) if role_orga else False


def nettoyer_texte(texte: str) -> str:
    """Retire les accents, les émojis, la ponctuation et met en minuscules."""
    if not texte:
        return ""
    texte_norm = unicodedata.normalize("NFD", texte)
    sans_accents = "".join(c for c in texte_norm if unicodedata.category(c) != "Mn")
    texte_min = sans_accents.lower()
    texte_propre = re.sub(r'[^a-z0-9\s]', '', texte_min)
    return re.sub(r'\s+', ' ', texte_propre).strip()


def est_categorie_candidate(category: discord.CategoryChannel) -> bool:
    """Vérifie si le nom de la catégorie (nettoyé) contient l'un des mots-clés."""
    if not category:
        return False
    cat_nom_propre = nettoyer_texte(category.name)
    return any(cible in cat_nom_propre for cible in CATEGORIES_CIBLES)


def get_spectateur_overwrites() -> discord.PermissionOverwrite:
    """Définit les droits stricts en lecture seule pour les Spectateurs."""
    return discord.PermissionOverwrite(
        view_channel=True,
        read_messages=True,
        read_message_history=True,
        send_messages=False,
        send_messages_in_threads=False,
        create_public_threads=False,
        create_private_threads=False,
        add_reactions=False,
        use_external_emojis=False,
        use_external_stickers=False,
        send_voice_messages=False
    )


# =======================================================
# 1. FONCTIONS DE RÉCAPITULATIF JOURNALIER GLOBAL
# =======================================================

async def generer_et_envoyer_recap_quotidien(guild: discord.Guild, target_channel: discord.TextChannel):
    """Scanne les salons cibles (et log-deplacements) des dernières 24h et publie une synthèse IA."""
    now = datetime.datetime.now(datetime.timezone.utc)
    depuis = now - datetime.timedelta(hours=24)

    salons_transcripts = []

    for channel in guild.text_channels:
        est_salon_log = (channel.name.lower() == "log-deplacements")
        
        if est_categorie_candidate(channel.category) or est_salon_log:
            if channel.name.startswith("🔒arch-"):
                continue

            lines = []
            async for msg in channel.history(after=depuis, oldest_first=True):
                if msg.author.bot and not est_salon_log:
                    continue

                texte_msg = msg.content.strip()

                if msg.attachments:
                    for att in msg.attachments:
                        if att.filename.endswith(".txt"):
                            try:
                                file_bytes = await att.read()
                                texte_fichier = file_bytes.decode('utf-8')
                                texte_msg += f"\n\n--- 📄 CONTENU DU FICHIER {att.filename} ---\n{texte_fichier}\n---------------------------------------\n"
                            except Exception as e:
                                print(f"Impossible de lire le fichier {att.filename} : {e}")

                if texte_msg.strip():
                    lines.append(f"[{msg.created_at.strftime('%H:%M')}] {msg.author.display_name}: {texte_msg.strip()}")

            if lines:
                cat_nom = channel.category.name if channel.category else "Sans Catégorie"
                salons_transcripts.append(
                    f"=== [{cat_nom.upper()}] #{channel.name} ({len(lines)} éléments) ===\n" + "\n".join(lines)
                )

    if not salons_transcripts:
        await target_channel.send("😴 **Journal du jour :** Aucun échange dans les salons candidats ni de logs au cours des dernières 24 heures.")
        return

    full_context = "\n\n".join(salons_transcripts)

    prompt = (
        "Tu es l'arbitre en chef et showrunner d'un jeu de stratégie et de survie (type Koh-Lanta / Survivor / Secret Story).\n"
        "Voici l'ensemble des discussions de la journée échangées dans les différents espaces de jeu (Camps, Duos, Équipes, Confessionnaux) "
        "ainsi que les journaux de logs (déplacements, objets, événements) :\n\n"
        f"{full_context}\n\n"
        "Rédige le **Journal de Bord Stratégique Global de la Journée** pour l'équipe d'organisation (Orgas/Spectateurs).\n"
        "Structure ta réponse avec des titres clairs et des emojis :\n"
        "1. 🌍 **Synthèse Générale & Ambiance Globale**\n"
        "2. 🤝 **Alliances, Pactes & Négociations**\n"
        "3. 🎯 **Cibles, Votes & Stratégies d'Élimination**\n"
        "4. ⚠️ **Trahisons, Secrets & Double-Jeu**\n"
        "5. 🎙️ **Points Clés des Confessionnaux & Duos**\n"
        "6. 🗺️ **Mouvements & Événements Importants (Logs)**\n"
        "7. 📌 **Résumé rapide par zone/salon actif**"
    )

    max_tentatives = 3
    for tentative in range(max_tentatives):
        try:
            response = gemini_client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt
            )
            recap_text = response.text

            date_str = datetime.datetime.now(ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y")
            header = f"📰 **JOURNAL STRATÉGIQUE GLOBAL DU {date_str}**\n*(Réservé aux Orgas, Spectateurs et Admins)*\n\n"
            full_message = header + recap_text

            for chunk in [full_message[i:i + 1900] for i in range(0, len(full_message), 1900)]:
                await target_channel.send(chunk)
            
            return

        except Exception as e:
            if "503" in str(e) and tentative < max_tentatives - 1:
                await asyncio.sleep(3)
            else:
                await target_channel.send("❌ Impossible de générer le journal aujourd'hui (serveurs IA surchargés).")
                return


# =======================================================
# 2. FONCTIONS DU BOT JOURNALISTE (CONFESSIONNAL OBJECTIF)
# =======================================================

async def generer_questions_confessionnal(target_recap_channel: discord.TextChannel, candidat_nom: str = None) -> str:
    """Lit le dernier récapitulatif disponible et génère des questions d'interview neutres et objectives."""
    recap_messages = [
        msg.content async for msg in target_recap_channel.history(limit=6, oldest_first=False)
        if not msg.content.startswith("😴") and "JOURNAL STRATÉGIQUE" in msg.content
    ]

    if not recap_messages:
        return "⚠️ Aucun journal stratégique récent trouvé dans le salon dédié."

    dernier_recap = "\n---\n".join(reversed(recap_messages))

    consigne_cible = (
        f"Concentre-toi UNIQUEMENT sur le candidat **{candidat_nom}**." 
        if candidat_nom else 
        "Choisis librement 3 ou 4 candidats ayant des choix stratégiques majeurs à faire ou au cœur des dynamiques du jour."
    )

    prompt = (
        "Tu es le journaliste/interviewer professionnel et IMPARTIAL d'un jeu de stratégie et d'aventure télévisé (type Koh-Lanta / Survivor / Big Brother).\n"
        "Ton rôle est d'aider les organisateurs à préparer les entretiens individuels au confessionnal.\n\n"
        "RÈGLES D'OR DE L'INTERVIEW :\n"
        "- NEUTRALITÉ ABSOLUE : Tu ne juges jamais les actions (pas de morale, pas de reproches, pas d'expressions accusatrices).\n"
        "- OBJECTIVITÉ : Tu constates les faits et tu poses des questions ouvertes sur les choix, réflexions et dilemmes.\n"
        "- NON-DIVULGATION : Tu ne révèles jamais ce que les autres candidats font ou disent en secret.\n"
        "- POSTURE : L'organisation est un miroir neutre qui pousse le joueur à expliciter sa stratégie.\n\n"
        f"Voici le récapitulatif des derniers événements du jeu :\n\n{dernier_recap}\n\n"
        f"Consigne : {consigne_cible}\n\n"
        "Pour chaque candidat sélectionné, structure la fiche ainsi :\n"
        "👤 **Candidat : [Nom]**\n"
        "🎯 **Situation constatée** (résumé factuel de sa posture en 1 phrase)\n"
        "❓ **3 Questions ouvertes** (au tutoiement, ton calme et journalistique)\n"
        "💡 **Objectif de l'interview** (comprendre sa réflexion ou sa gestion du risque)"
    )

    max_tentatives = 3
    for tentative in range(max_tentatives):
        try:
            response = gemini_client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt
            )
            return response.text
        except Exception as e:
            if "503" in str(e) and tentative < max_tentatives - 1:
                await asyncio.sleep(2)
            else:
                return f"❌ Erreur IA lors de la génération des questions : {e}"


# ==========================================
# 3. TÂCHES AUTOMATIQUES PLANIFIÉES
# ==========================================

@tasks.loop(time=HEURE_RECAP)
async def tache_recap_quotidien():
    """Tâche automatique planifiée chaque soir à 23h00."""
    if RECAP_CHANNEL_ID == 0:
        return
    channel = bot.get_channel(RECAP_CHANNEL_ID)
    if channel:
        await generer_et_envoyer_recap_quotidien(channel.guild, channel)


@tasks.loop(time=HEURE_QUESTIONS)
async def tache_questions_matin():
    """Tâche automatique planifiée chaque matin à 09h00."""
    if RECAP_CHANNEL_ID == 0:
        return
    channel = bot.get_channel(RECAP_CHANNEL_ID)
    if channel:
        questions_text = await generer_questions_confessionnal(channel)
        date_str = datetime.datetime.now(ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y")
        header = f"🎙️ **FICHES CONFESSIONNAL DU {date_str} — SUGGESTIONS D'INTERVIEWS**\n*(Pour les Orgas)*\n\n"
        full_msg = header + questions_text
        for chunk in [full_msg[i:i + 1900] for i in range(0, len(full_msg), 1900)]:
            await channel.send(chunk)


@bot.event
async def on_ready():
    await bot.tree.sync()
    if not tache_recap_quotidien.is_running() and RECAP_CHANNEL_ID != 0:
        tache_recap_quotidien.start()
    if not tache_questions_matin.is_running() and RECAP_CHANNEL_ID != 0:
        tache_questions_matin.start()
    print(f"🤖 Bot connecté en tant que : {bot.user}")


# Gestionnaire d'erreur pour bloquer les non-orgas
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        msg = "⛔ **Accès refusé :** Cette commande est strictement réservée aux membres ayant le rôle **Orgas** ou **Administrateur**."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    else:
        print(f"Erreur d'application Discord : {error}")


# ==========================================
# 4. COMMANDES DE GESTION DUOS & CANDIDATS
# ==========================================

@bot.tree.command(
    name="creer_duos", 
    description="Génère tous les salons duos privés (utilise la catégorie existante si trouvée)."
)
@app_commands.describe(
    nom_equipe="Nom de la catégorie (ex: 🔴 DUOS ROUGE)",
    roles="Mentionne les rôles séparés par des espaces (ex: @Candidat1 @Candidat2 ...)"
)
@app_commands.check(est_orga_ou_admin)
async def creer_duos(interaction: discord.Interaction, nom_equipe: str, roles: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    role_ids = [int(r.strip("<@&>")) for r in roles.split() if r.startswith("<@&") and r.endswith(">")]
    roles_list = [guild.get_role(r_id) for r_id in role_ids if guild.get_role(r_id) is not None]

    if len(roles_list) < 2:
        await interaction.followup.send("❌ Veuillez mentionner au moins 2 rôles valides.", ephemeral=True)
        return

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    clean_target_name = nettoyer_texte(nom_equipe)
    existing_category = discord.utils.find(lambda c: nettoyer_texte(c.name) == clean_target_name, guild.categories)

    category_index = 1
    if existing_category:
        current_category = existing_category
        channel_count_in_current_cat = len(existing_category.channels)
    else:
        current_category = await guild.create_category(nom_equipe)
        channel_count_in_current_cat = 0

    duos = list(itertools.combinations(roles_list, 2))
    total_duos = len(duos)

    for r1, r2 in duos:
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

        if role_spectateurs:
            overwrites[role_spectateurs] = get_spectateur_overwrites()

        if role_orgas:
            overwrites[role_orgas] = discord.PermissionOverwrite(
                read_messages=True, view_channel=True, read_message_history=True, send_messages=True
            )

        nom_salon = f"duo-{r1.name.lower().replace(' ', '-')}-{r2.name.lower().replace(' ', '-')}"
        await guild.create_text_channel(name=nom_salon, category=current_category, overwrites=overwrites)
        channel_count_in_current_cat += 1

        await asyncio.sleep(0.6)

    await interaction.followup.send(
        f"✅ Succès : **{total_duos} salons duos** créés dans la catégorie **{current_category.name}** !", 
        ephemeral=True
    )


@bot.tree.command(
    name="eliminer_candidat", 
    description="Archive tous les salons duos d'un candidat éliminé et retire les accès des 2 participants."
)
@app_commands.describe(role_candidat="Le rôle du candidat éliminé (ex: @Lucas)")
@app_commands.check(est_orga_ou_admin)
async def eliminer_candidat(interaction: discord.Interaction, role_candidat: discord.Role):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    targeted_channels = []
    for channel in guild.text_channels:
        if channel.name.startswith("duo-") and role_candidat in channel.overwrites:
            targeted_channels.append(channel)

    if not targeted_channels:
        await interaction.followup.send(f"ℹ️ Aucun salon duo actif trouvé pour le rôle {role_candidat.mention}.", ephemeral=True)
        return

    clean_arch_name = nettoyer_texte(NOM_CATEGORIE_ARCHIVE)
    archive_categories = [c for c in guild.categories if clean_arch_name in nettoyer_texte(c.name)]
    if archive_categories:
        current_archive_cat = archive_categories[-1]
    else:
        current_archive_cat = await guild.create_category(NOM_CATEGORIE_ARCHIVE)

    archived_count = 0
    cat_index = len(archive_categories) or 1

    for channel in targeted_channels:
        if len(current_archive_cat.channels) >= MAX_CHANNELS_PER_CATEGORY:
            cat_index += 1
            current_archive_cat = await guild.create_category(f"{NOM_CATEGORIE_ARCHIVE} - {cat_index}")
            await asyncio.sleep(1)

        new_overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, view_channel=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True)
        }

        if role_spectateurs:
            new_overwrites[role_spectateurs] = get_spectateur_overwrites()

        if role_orgas:
            new_overwrites[role_orgas] = discord.PermissionOverwrite(
                read_messages=True, view_channel=True, read_message_history=True, send_messages=False
            )

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
        f"🔒 Les deux candidats n'ont plus accès à ces salons.",
        ephemeral=True
    )


# ==========================================
# 5. COMMANDES DE SUPPRESSION & NETTOYAGE
# ==========================================

@bot.tree.command(name="supprimer_categorie", description="Supprime une catégorie entière et ses salons.")
@app_commands.describe(nom_categorie="Nom exact de la catégorie (ex: Duos Rouge - 1)")
@app_commands.check(est_orga_ou_admin)
async def supprimer_categorie(interaction: discord.Interaction, nom_categorie: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    target_clean = nettoyer_texte(nom_categorie)
    category = discord.utils.find(lambda c: nettoyer_texte(c.name) == target_clean, guild.categories)
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
@app_commands.check(est_orga_ou_admin)
async def purger_equipe_duos(interaction: discord.Interaction, prefixe: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    clean_pref = nettoyer_texte(prefixe)
    categories_to_delete = [c for c in guild.categories if nettoyer_texte(c.name).startswith(clean_pref)]
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


@bot.tree.command(
    name="effacer_salon",
    description="Supprime tous les messages d'un salon (clone et recrée le salon à neuf avec les mêmes permissions)."
)
@app_commands.describe(salon="Optionnel : mentionnez le salon à vider (par défaut : le salon actuel)")
@app_commands.check(est_orga_ou_admin)
async def effacer_salon(interaction: discord.Interaction, salon: discord.TextChannel = None):
    await interaction.response.defer(ephemeral=True)
    target = salon or interaction.channel

    if not isinstance(target, discord.TextChannel):
        await interaction.followup.send("❌ Seuls les salons textuels peuvent être vidés.", ephemeral=True)
        return

    try:
        nouveau_salon = await target.clone(reason=f"Salon vidé par {interaction.user.display_name}")
        await target.delete(reason=f"Salon vidé par {interaction.user.display_name}")
        
        await nouveau_salon.send("🧹 **Le salon a été réinitialisé et vidé avec succès.**")
        if target.id != interaction.channel_id:
            await interaction.followup.send(f"✅ Le salon {nouveau_salon.mention} a été vidé.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Erreur lors de la réinitialisation : {e}", ephemeral=True)


@bot.tree.command(
    name="vider_categorie",
    description="Supprime tous les salons d'une catégorie tout en conservant la catégorie vide."
)
@app_commands.describe(nom_categorie="Nom de la catégorie dont vous souhaitez supprimer les salons")
@app_commands.check(est_orga_ou_admin)
async def vider_categorie(interaction: discord.Interaction, nom_categorie: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    target_clean = nettoyer_texte(nom_categorie)
    category = discord.utils.find(lambda c: nettoyer_texte(c.name) == target_clean, guild.categories)
    
    if not category:
        await interaction.followup.send(f"❌ Catégorie **{nom_categorie}** introuvable.", ephemeral=True)
        return

    salons_a_supprimer = [ch for ch in category.channels if isinstance(ch, discord.TextChannel)]
    total_salons = len(salons_a_supprimer)

    if total_salons == 0:
        await interaction.followup.send(f"ℹ️ La catégorie **{category.name}** ne contient aucun salon textuel.", ephemeral=True)
        return

    for channel in salons_a_supprimer:
        try:
            await channel.delete(reason=f"Nettoyage de catégorie par {interaction.user.display_name}")
            await asyncio.sleep(0.4)
        except Exception:
            pass

    await interaction.followup.send(
        f"🧹 **{total_salons} salon(s)** supprimé(s) dans la catégorie **{category.name}** (la catégorie a été conservée).",
        ephemeral=True
    )


# ========================================================
# 6. COMMANDES DE PERMISSIONS SPECTATEURS
# ========================================================

@bot.tree.command(
    name="ajouter_spectateurs_salon",
    description="Donne l'accès Spectateurs strict (lecture seule, zéro émoji/réaction) à un salon précis."
)
@app_commands.describe(salon="Optionnel : mentionnez le salon à configurer (par défaut : le salon actuel)")
@app_commands.check(est_orga_ou_admin)
async def ajouter_spectateurs_salon(interaction: discord.Interaction, salon: discord.TextChannel = None):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    target_channel = salon or interaction.channel

    if not isinstance(target_channel, discord.TextChannel):
        await interaction.followup.send("❌ Cette commande ne s'applique qu'aux salons textuels.", ephemeral=True)
        return

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    if not role_spectateurs:
        await interaction.followup.send(f"❌ Rôle **{ROLE_SPECTATEURS_NAME}** introuvable sur le serveur.", ephemeral=True)
        return

    overwrites = get_spectateur_overwrites()
    await target_channel.set_permissions(role_spectateurs, overwrite=overwrites, reason=f"Accès spectateur ajouté par {interaction.user.display_name}")

    await interaction.followup.send(
        f"👁️ Accès **Spectateurs** (lecture seule stricte) appliqué au salon {target_channel.mention} !",
        ephemeral=True
    )


@bot.tree.command(
    name="ajouter_spectateurs_categorie",
    description="Donne l'accès Spectateurs strict à tous les salons d'une catégorie."
)
@app_commands.describe(nom_categorie="Nom de la catégorie cible (ex: ⛺ CAMPS ou DUO ROUGE)")
@app_commands.check(est_orga_ou_admin)
async def ajouter_spectateurs_categorie(interaction: discord.Interaction, nom_categorie: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    if not role_spectateurs:
        await interaction.followup.send(f"❌ Rôle **{ROLE_SPECTATEURS_NAME}** introuvable sur le serveur.", ephemeral=True)
        return

    target_clean = nettoyer_texte(nom_categorie)
    category = discord.utils.find(lambda c: nettoyer_texte(c.name) == target_clean, guild.categories)

    if not category:
        await interaction.followup.send(f"❌ Catégorie **{nom_categorie}** introuvable.", ephemeral=True)
        return

    channels_list = [ch for ch in category.channels if isinstance(ch, discord.TextChannel)]
    if not channels_list:
        await interaction.followup.send(f"ℹ️ Aucun salon textuel trouvé dans **{category.name}**.", ephemeral=True)
        return

    overwrites = get_spectateur_overwrites()
    mis_a_jour = 0

    for ch in channels_list:
        try:
            await ch.set_permissions(role_spectateurs, overwrite=overwrites, reason=f"Accès spectateurs par lot ({interaction.user.display_name})")
            mis_a_jour += 1
            await asyncio.sleep(0.3)
        except Exception:
            pass

    await interaction.followup.send(
        f"👁️ Accès **Spectateurs** appliqué avec succès sur **{mis_a_jour}/{len(channels_list)} salon(s)** de la catégorie **{category.name}** !",
        ephemeral=True
    )


# ==========================================
# 7. COMMANDES DE RÉSUMÉ IA & JOURNALISME
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
@app_commands.check(est_orga_ou_admin)
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
            "- 🤝 Décisions / Alliances évoquées\n"
            "- ⚠️ Orientations stratégiques ou cibles mentionnées\n"
            "- 🎭 Dynamique des échanges (Accord, Réserves, Négociation)"
        )
    else:
        prompt = (
            "Tu es l'analyste stratégique d'un jeu d'aventure/téléréalité (type Koh-Lanta/Survivor/Secret Story). "
            f"Voici la transcription des messages échangés dans le salon #{channel.name} :\n\n"
            f"{transcript}\n\n"
            "Fais un **RÉSUMÉ DÉTAILLÉ ET STRUCTURÉ** en français, avec les sections suivantes :\n"
            "1. 🎯 **Analyse Thématique** (synthèse factuelle des sujets abordés)\n"
            "2. 🤝 **Accords & Propositions** (qui propose quoi, points de convergence ou de divergence)\n"
            "3. ⚠️ **Scénarios & Votes évoqués** (noms mentionnés, arguments avancés, alternatives)\n"
            "4. 🎭 **Dynamique relationnelle** (postures observées, équilibre de la discussion)\n"
            "5. 💬 **Citations ou Moments Clés** (phrases structurantes de l'échange)"
        )

    max_tentatives = 3
    for tentative in range(max_tentatives):
        try:
            response = gemini_client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt
            )
            summary_text = response.text

            badge_titre = "⚡ Résumé Flash" if format.value == "court" else "📖 Résumé Détaillé"
            embed = discord.Embed(
                title=f"{badge_titre} — #{channel.name}",
                description=summary_text,
                color=discord.Color.gold() if format.value == "court" else discord.Color.purple()
            )
            embed.set_footer(text=f"Analyse basée sur les {len(user_messages)} messages (Tentative {tentative + 1}).")

            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        except Exception as e:
            if "503" in str(e) and tentative < max_tentatives - 1:
                await asyncio.sleep(2)
            else:
                await interaction.followup.send(f"❌ Les serveurs IA sont surchargés après {max_tentatives} tentatives. Réessayez plus tard.", ephemeral=True)
                return


@bot.tree.command(
    name="resumer_conv_orga", 
    description="Génère un résumé IA axé sur l'organisation et les décisions (pour le Staff)."
)
@app_commands.describe(
    format="Choisissez entre un résumé synthétique ou un compte-rendu complet",
    limite="Nombre de messages récents à analyser (par défaut: 100)"
)
@app_commands.choices(format=[
    app_commands.Choice(name="⚡ Résumé Court (Décisions & Actions rapides)", value="court"),
    app_commands.Choice(name="📖 Résumé Long (Compte-rendu détaillé)", value="long")
])
@app_commands.check(est_orga_ou_admin)
async def resumer_conv_orga(interaction: discord.Interaction, format: app_commands.Choice[str], limite: int = 100):
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel

    messages = [msg async for msg in channel.history(limit=limite, oldest_first=True)]
    user_messages = [msg for msg in messages if not msg.author.bot and msg.content.strip()]

    if len(user_messages) < 3:
        await interaction.followup.send("⚠️ Pas assez de messages pour générer un compte-rendu pertinent.", ephemeral=True)
        return

    transcript = "\n".join([f"{msg.author.display_name}: {msg.content}" for msg in user_messages])

    if format.value == "court":
        prompt = (
            "Tu es l'assistant de direction d'une équipe d'organisation d'un événement / jeu. "
            f"Voici la transcription de la réunion/discussion de l'équipe dans le salon #{channel.name} :\n\n"
            f"{transcript}\n\n"
            "Fais un résumé **TRÈS COURT, CONCIS ET DIRECT** en 3 à 5 bullet points maximum :\n"
            "- 🎯 Objectif/Sujet principal de la discussion\n"
            "- 🛠️ Décisions importantes actées\n"
            "- 📋 Actions à faire (Qui fait quoi ?)\n"
            "- 📅 Prochaines étapes"
        )
    else:
        prompt = (
            "Tu es l'assistant de direction d'une équipe d'organisation d'un jeu / événement. "
            f"Voici la transcription des échanges du staff dans le salon #{channel.name} :\n\n"
            f"{transcript}\n\n"
            "Rédige un **COMPTE-RENDU DÉTAILLÉ ET PROFESSIONNEL** en français, structuré avec les sections suivantes :\n"
            "1. 🎯 **Sujets abordés** (Quels ont été les thèmes de la discussion ?)\n"
            "2. 🛠️ **Décisions prises** (Qu'est-ce qui a été validé ou refusé par l'équipe ?)\n"
            "3. 📋 **Répartition des tâches** (Qui est en charge de quoi ?)\n"
            "4. 💡 **Idées & Propositions en attente** (Ce qui doit encore être discuté ou creusé)\n"
            "5. 📅 **Prochaines étapes & Deadlines** (Ce qu'il reste à faire dans l'immédiat)"
        )

    max_tentatives = 3
    for tentative in range(max_tentatives):
        try:
            response = gemini_client.models.generate_content(
                model=MODEL_NAME,
                contents=prompt
            )
            summary_text = response.text

            badge_titre = "⚡ Compte-Rendu Flash" if format.value == "court" else "📖 Compte-Rendu Complet"
            embed = discord.Embed(
                title=f"{badge_titre} — #{channel.name}",
                description=summary_text,
                color=discord.Color.blue()
            )
            embed.set_footer(text=f"Analyse basée sur les {len(user_messages)} messages (Tentative {tentative + 1}).")

            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        except Exception as e:
            if "503" in str(e) and tentative < max_tentatives - 1:
                await asyncio.sleep(2)
            else:
                await interaction.followup.send(f"❌ Les serveurs IA sont surchargés après {max_tentatives} tentatives. Réessayez plus tard.", ephemeral=True)
                return


@bot.tree.command(
    name="questions_confessionnal",
    description="Génère des questions journalistiques objectives pour les confessionnaux (sur-mesure ou global)."
)
@app_commands.describe(candidat="Optionnel : mentionnez le rôle d'un candidat précis (laisser vide pour les profils clés du jour)")
@app_commands.check(est_orga_ou_admin)
async def questions_confessionnal(interaction: discord.Interaction, candidat: discord.Role = None):
    await interaction.response.defer(ephemeral=True)

    target_channel = bot.get_channel(RECAP_CHANNEL_ID) or interaction.channel
    candidat_nom = candidat.name if candidat else None

    resultat_text = await generer_questions_confessionnal(target_channel, candidat_nom)

    titre = f"🎙️ Interview Confessionnal — {candidat.name}" if candidat else "🎙️ Suggestions Confessionnal du Jour"
    embed = discord.Embed(
        title=titre,
        description=resultat_text,
        color=discord.Color.red()
    )
    embed.set_footer(text="Généré par l'IA Journaliste • Réservé aux Orgas")

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="forcer_recap_jour",
    description="Génère immédiatement le journal stratégique global de tous les salons candidats des dernières 24h."
)
@app_commands.check(est_orga_ou_admin)
async def forcer_recap_jour(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    target_channel = bot.get_channel(RECAP_CHANNEL_ID) or interaction.channel
    await interaction.followup.send(f"⏳ Analyse des salons candidats en cours pour {target_channel.mention}...", ephemeral=True)

    await generer_et_envoyer_recap_quotidien(interaction.guild, target_channel)


# --- DÉMARRAGE DU BOT ---
bot.run(TOKEN)
