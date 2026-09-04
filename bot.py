import os
import re
import time
import itertools
import asyncio
import datetime
import random
import unicodedata
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from google import genai

# ==========================================
# CONFIGURATION & ENVIRONNEMENT
# ==========================================

TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

# Modèle d'origine inchangé
MODEL_NAME = "gemini-3.5-flash-lite"

# Salons & Catégories fixes
RECAP_CHANNEL_ID = int(os.getenv("RECAP_CHANNEL_ID", 0))
CATEGORY_TRIO_ID = 1541397070898921482
CATEGORY_QUATUOR_ID = 1541397227744927835
RESULTATS_CHANNEL_ID = 1545186500960985148

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
    "equipe jaune",
    "destin lies",
    "destins lies",
    "destin lie"
]

# Rôles génériques à ignorer pour trouver le rôle personnel du joueur
ROLES_GENERIQUES_A_IGNORER = [
    "arrivants",
    "everyone",
    "@everyone",
    "candidat",
    "candidats",
    "spectateur",
    "spectateurs",
    "orga",
    "orgas",
    "admin",
    "administrateur",
    "bot",
    "booster"
]

# Initialisation du client IA et du Bot Discord
gemini_client = genai.Client(api_key=GEMINI_KEY)

intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Variables globales en mémoire
DERNIERS_BINOMES_TIRES = []
ROLES_PERSO_EN_PAUSE = {}  # {member_id: role_id}
CHRONOS_EN_COURS = {}      # {key: start_datetime}

CONFIG_EPREUVE_GLOBALE = {
    "questions": [],
    "temps_par_defaut": 15,
    "active": False
}


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


def formater_nom_salon(nom: str) -> str:
    """Nettoie et formate un pseudo ou nom de rôle pour un salon Discord."""
    nom_clean = nettoyer_texte(nom)
    return re.sub(r"[^a-z0-9_-]", "", nom_clean.replace(" ", "-"))


def est_categorie_candidate(category: discord.CategoryChannel) -> bool:
    """Vérifie si le nom de la catégorie (nettoyé) contient l'un des mots-clés."""
    if not category:
        return False
    cat_nom_propre = nettoyer_texte(category.name)
    return any(cible in cat_nom_propre for cible in CATEGORIES_CIBLES)


def get_spectateur_overwrites() -> discord.PermissionOverwrite:
    """Définit les droits stricts en lecture seule pour les Spectateurs (Textuel)."""
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


def get_spectateur_voice_overwrites() -> discord.PermissionOverwrite:
    """Définit les droits stricts en écoute seule pour les Spectateurs (Vocal)."""
    return discord.PermissionOverwrite(
        view_channel=True,
        connect=True,
        speak=False,
        stream=False,
        use_voice_activation=False,
        use_soundboard=False,
        use_external_sounds=False,
        add_reactions=False
    )


def trouver_role_personnel(member: discord.Member, role_equipe: discord.Role = None) -> discord.Role:
    """Trouve le rôle spécifique/personnel du joueur (ex: @Ugo)."""
    nom_membre_clean = nettoyer_texte(member.display_name)
    pseudo_global_clean = nettoyer_texte(member.name)
    role_equipe_clean = nettoyer_texte(role_equipe.name) if role_equipe else ""

    for r in member.roles:
        if r.is_default():
            continue
        r_clean = nettoyer_texte(r.name)
        if r_clean in (nom_membre_clean, pseudo_global_clean):
            return r

    for r in member.roles:
        r_clean = nettoyer_texte(r.name)
        if r.is_default() or (role_equipe and r.id == role_equipe.id):
            continue
        if role_equipe and r_clean == role_equipe_clean:
            continue
        if r_clean in [nettoyer_texte(ign) for ign in ROLES_GENERIQUES_A_IGNORER]:
            continue
        return r

    return None


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
# 3. TÂCHES AUTOMATIQUES & ÉVÉNEMENTS
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


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Anti-triche vocal : Alerte en console si un joueur quitte ou se mute en pleine épreuve."""
    if member.bot:
        return

    if before.channel and not after.channel:
        print(f"⚠️ ALERTE TRICHE : {member.display_name} a QUITTÉ le salon vocal {before.channel.name} !")

    if not before.self_deaf and after.self_deaf:
        print(f"⚠️ ALERTE : {member.display_name} a COUPÉ SON CASQUE (Deafen).")

    if not before.self_mute and after.self_mute:
        print(f"⚠️ INFO : {member.display_name} s'est MUTÉ.")


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
# 4. GESTION DES DUOS & CANDIDATS
# ==========================================

@bot.tree.command(
    name="creer_duos", 
    description="Génère tous les salons duos possibles pour tous les membres possédant un rôle d'équipe."
)
@app_commands.describe(
    role_equipe="Le rôle de l'équipe à diviser en duos (ex: @Jaune ou @Rouge)",
    nom_categorie="Nom de la catégorie où créer les salons (ex: 🟡 DUOS JAUNE)"
)
@app_commands.check(est_orga_ou_admin)
async def creer_duos(interaction: discord.Interaction, role_equipe: discord.Role, nom_categorie: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    membres = []
    async for member in guild.fetch_members(limit=None):
        if not member.bot and role_equipe in member.roles:
            membres.append(member)

    if len(membres) < 2:
        await interaction.followup.send(
            f"❌ Seulement {len(membres)} membre(s) trouvé(s) avec le rôle {role_equipe.mention} (minimum 2 requis).", 
            ephemeral=True
        )
        return

    candidats_data = []
    for m in membres:
        r_perso = trouver_role_personnel(m, role_equipe)
        candidats_data.append({
            "member": m,
            "role": r_perso,
            "display_name": m.display_name,
            "clean_name": formater_nom_salon(m.display_name)
        })

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    clean_target_name = nettoyer_texte(nom_categorie)
    existing_category = discord.utils.find(lambda c: nettoyer_texte(c.name) == clean_target_name, guild.categories)

    category_index = 1
    if existing_category:
        current_category = existing_category
        channel_count_in_current_cat = len(existing_category.channels)
    else:
        current_category = await guild.create_category(nom_categorie)
        channel_count_in_current_cat = 0

    duos = list(itertools.combinations(candidats_data, 2))
    total_duos = len(duos)

    await interaction.followup.send(
        f"⏳ Création de **{total_duos} salons duos** pour les **{len(membres)} membres** de {role_equipe.mention} dans **{nom_categorie}**...",
        ephemeral=True
    )

    for c1, c2 in duos:
        if channel_count_in_current_cat >= MAX_CHANNELS_PER_CATEGORY:
            category_index += 1
            current_category = await guild.create_category(f"{nom_categorie} - {category_index}")
            channel_count_in_current_cat = 0
            await asyncio.sleep(1)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, view_channel=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True)
        }

        if c1["role"]:
            overwrites[c1["role"]] = discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True)

        if c2["role"]:
            overwrites[c2["role"]] = discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True)

        if role_spectateurs:
            overwrites[role_spectateurs] = get_spectateur_overwrites()

        if role_orgas:
            overwrites[role_orgas] = discord.PermissionOverwrite(
                read_messages=True, view_channel=True, read_message_history=True, send_messages=True
            )

        nom_salon = f"duo-{c1['clean_name']}-{c2['clean_name']}"
        await guild.create_text_channel(name=nom_salon, category=current_category, overwrites=overwrites)
        channel_count_in_current_cat += 1

        await asyncio.sleep(0.6)

    await interaction.followup.send(
        f"✅ **Terminé !** **{total_duos} salons duos** créés dans la catégorie **{current_category.name}**.", 
        ephemeral=True
    )


@bot.tree.command(
    name="eliminer_candidat", 
    description="Archive tous les salons duos d'un candidat éliminé et retire les accès des participants."
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
        if (channel.name.startswith("duo-") or channel.name.startswith("🔗・") or channel.name.startswith("🔺・") or channel.name.startswith("🔶・")) and role_candidat in channel.overwrites:
            targeted_channels.append(channel)

    if not targeted_channels:
        await interaction.followup.send(f"ℹ️ Aucun salon actif trouvé pour le rôle {role_candidat.mention}.", ephemeral=True)
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

        nom_base = channel.name.replace("duo-", "").replace("🔗・", "").replace("🔺・", "").replace("🔶・", "")
        new_name = f"🔒arch-{nom_base}"
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
        f"📦 **{archived_count} salons** ont été archivés dans **{current_archive_cat.name}**.\n"
        f"🔒 Les candidats n'ont plus accès à ces salons.",
        ephemeral=True
    )


# ========================================================
# 5. SALONS SPÉCIFIQUES (TRIOS, QUATUORS & VOCAUX)
# ========================================================

@bot.tree.command(
    name="creer_trio",
    description="Crée un salon trio privé dans la catégorie dédiée à partir des 3 rôles choisis."
)
@app_commands.describe(
    role_1="Rôle du premier candidat",
    role_2="Rôle du deuxième candidat",
    role_3="Rôle du troisième candidat"
)
@app_commands.check(est_orga_ou_admin)
async def creer_trio(
    interaction: discord.Interaction,
    role_1: discord.Role,
    role_2: discord.Role,
    role_3: discord.Role
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    categorie = guild.get_channel(CATEGORY_TRIO_ID)
    if not categorie or not isinstance(categorie, discord.CategoryChannel):
        await interaction.followup.send(f"❌ Catégorie Trio introuvable (ID: `{CATEGORY_TRIO_ID}`).", ephemeral=True)
        return

    roles_choisis = [role_1, role_2, role_3]
    if len(set(roles_choisis)) < 3:
        await interaction.followup.send("❌ Veuillez spécifier 3 rôles distincts.", ephemeral=True)
        return

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False, view_channel=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True)
    }

    for r in roles_choisis:
        overwrites[r] = discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True)

    if role_spectateurs:
        overwrites[role_spectateurs] = get_spectateur_overwrites()

    if role_orgas:
        overwrites[role_orgas] = discord.PermissionOverwrite(
            read_messages=True, view_channel=True, read_message_history=True, send_messages=True
        )

    noms = [formater_nom_salon(r.name) for r in roles_choisis]
    nom_salon = f"🔺・{'-'.join(noms)}"

    salon = await guild.create_text_channel(name=nom_salon, category=categorie, overwrites=overwrites)
    await interaction.followup.send(
        f"✅ **Trio créé :** {salon.mention} dans **{categorie.name}**\n"
        f"👥 Rôles autorisés : {role_1.mention}, {role_2.mention}, {role_3.mention}",
        ephemeral=True
    )


@bot.tree.command(
    name="creer_quatuor",
    description="Crée un salon quatuor privé dans la catégorie dédiée à partir des 4 rôles choisis."
)
@app_commands.describe(
    role_1="Rôle du premier candidat",
    role_2="Rôle du deuxième candidat",
    role_3="Rôle du troisième candidat",
    role_4="Rôle du quatrième candidat"
)
@app_commands.check(est_orga_ou_admin)
async def creer_quatuor(
    interaction: discord.Interaction,
    role_1: discord.Role,
    role_2: discord.Role,
    role_3: discord.Role,
    role_4: discord.Role
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    categorie = guild.get_channel(CATEGORY_QUATUOR_ID)
    if not categorie or not isinstance(categorie, discord.CategoryChannel):
        await interaction.followup.send(f"❌ Catégorie Quatuor introuvable (ID: `{CATEGORY_QUATUOR_ID}`).", ephemeral=True)
        return

    roles_choisis = [role_1, role_2, role_3, role_4]
    if len(set(roles_choisis)) < 4:
        await interaction.followup.send("❌ Veuillez spécifier 4 rôles distincts.", ephemeral=True)
        return

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False, view_channel=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True)
    }

    for r in roles_choisis:
        overwrites[r] = discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True)

    if role_spectateurs:
        overwrites[role_spectateurs] = get_spectateur_overwrites()

    if role_orgas:
        overwrites[role_orgas] = discord.PermissionOverwrite(
            read_messages=True, view_channel=True, read_message_history=True, send_messages=True
        )

    noms = [formater_nom_salon(r.name) for r in roles_choisis]
    nom_salon = f"🔶・{'-'.join(noms)}"

    salon = await guild.create_text_channel(name=nom_salon, category=categorie, overwrites=overwrites)
    await interaction.followup.send(
        f"✅ **Quatuor créé :** {salon.mention} dans **{categorie.name}**\n"
        f"👥 Rôles autorisés : {role_1.mention}, {role_2.mention}, {role_3.mention}, {role_4.mention}",
        ephemeral=True
    )


@bot.tree.command(
    name="creer_vocal",
    description="Crée un salon vocal privé pour 2 à 5 candidats (Spectateurs en écoute seule & Orgas inclus)."
)
@app_commands.describe(
    nom_categorie="Nom de la catégorie où placer le salon vocal",
    role_1="Premier candidat obligatoire",
    role_2="Deuxième candidat obligatoire",
    role_3="Troisième candidat optionnel",
    role_4="Quatrième candidat optionnel",
    role_5="Cinquième candidat optionnel"
)
@app_commands.check(est_orga_ou_admin)
async def creer_vocal(
    interaction: discord.Interaction,
    nom_categorie: str,
    role_1: discord.Role,
    role_2: discord.Role,
    role_3: discord.Role = None,
    role_4: discord.Role = None,
    role_5: discord.Role = None
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    target_clean = nettoyer_texte(nom_categorie)
    category = discord.utils.find(lambda c: nettoyer_texte(c.name) == target_clean, guild.categories)
    if not category:
        await interaction.followup.send(f"❌ Catégorie **{nom_categorie}** introuvable.", ephemeral=True)
        return

    roles_fournis = [r for r in [role_1, role_2, role_3, role_4, role_5] if r is not None]
    if len(set(roles_fournis)) < len(roles_fournis):
        await interaction.followup.send("❌ Veuillez ne pas sélectionner deux fois le même rôle.", ephemeral=True)
        return

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, mute_members=True)
    }

    for r in roles_fournis:
        overwrites[r] = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            stream=True,
            use_voice_activation=True
        )

    if role_spectateurs:
        overwrites[role_spectateurs] = get_spectateur_voice_overwrites()

    if role_orgas:
        overwrites[role_orgas] = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            speak=True,
            mute_members=True,
            deafen_members=True,
            move_members=True
        )

    noms = [formater_nom_salon(r.name) for r in roles_fournis]
    nom_vocal = f"🔊・{'-'.join(noms)}"

    salon_vocal = await guild.create_voice_channel(name=nom_vocal, category=category, overwrites=overwrites)
    
    mentions_roles = ", ".join([r.mention for r in roles_fournis])
    await interaction.followup.send(
        f"✅ **Salon vocal créé :** {salon_vocal.mention} dans **{category.name}**\n"
        f"👥 Candidats autorisés : {mentions_roles}\n"
        f"👁️ Spectateurs configurés en écoute seule.",
        ephemeral=True
    )


# ==========================================
# 6. SUPPRESSION & NETTOYAGE
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
# 7. PERMISSIONS SPECTATEURS
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
# 8. RÉSUMÉS IA & JOURNALISME
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


# ==========================================
# 9. CONTRÔLE DES TÂCHES PLANIFIÉES
# ==========================================

@bot.tree.command(
    name="pause_taches",
    description="Met en pause l'envoi automatique du récap du soir et des questions du matin."
)
@app_commands.check(est_orga_ou_admin)
async def pause_taches(interaction: discord.Interaction):
    if tache_recap_quotidien.is_running():
        tache_recap_quotidien.stop()
    if tache_questions_matin.is_running():
        tache_questions_matin.stop()

    await interaction.response.send_message(
        "⏸️ **Tâches automatiques mises en pause :**\n- 🌙 Récap du soir (23h00) : **Arrêté**\n- 🎙️ Questions du matin (09h00) : **Arrêté**",
        ephemeral=True
    )


@bot.tree.command(
    name="reprendre_taches",
    description="Réactive l'envoi automatique du récap du soir et des questions du matin."
)
@app_commands.check(est_orga_ou_admin)
async def reprendre_taches(interaction: discord.Interaction):
    if not tache_recap_quotidien.is_running():
        tache_recap_quotidien.start()
    if not tache_questions_matin.is_running():
        tache_questions_matin.start()

    await interaction.response.send_message(
        "▶️ **Tâches automatiques réactivées :**\n- 🌙 Récap du soir (23h00) : **Actif**\n- 🎙️ Questions du matin (09h00) : **Actif**",
        ephemeral=True
    )


# ==========================================
# 10. ANIMATION DE TIRAGE AU SORT (BOULE NOIRE)
# ==========================================

@bot.tree.command(
    name="tirage_boules",
    description="Lance le tirage au sort des boules (blanches / noires) avec animation et suspense."
)
@app_commands.describe(
    participants="Mentionne les candidats participant au tirage (ex: @Sarah @Lucas @Maxime ...)",
    nombre_boules_noires="Nombre de boules noires dans le sac (par défaut : 1)",
    couleur_sauveur="Couleur des boules sécurisées (ex: Blanche ⚪, Rouge 🔴, Jaune 🟡)"
)
@app_commands.choices(couleur_sauveur=[
    app_commands.Choice(name="⚪ Boule Blanche (Classique)", value="⚪ Blanche"),
    app_commands.Choice(name="🔴 Boule Rouge (Équipe Rouge)", value="🔴 Rouge"),
    app_commands.Choice(name="🟡 Boule Jaune (Équipe Jaune)", value="🟡 Jaune")
])
@app_commands.check(est_orga_ou_admin)
async def tirage_boules(
    interaction: discord.Interaction, 
    participants: str, 
    nombre_boules_noires: int = 1,
    couleur_sauveur: app_commands.Choice[str] = None
):
    guild = interaction.guild
    channel = interaction.channel

    role_ids = [int(r.strip("<@&>")) for r in participants.split() if r.startswith("<@&") and r.endswith(">")]
    candidats = [guild.get_role(r_id) for r_id in role_ids if guild.get_role(r_id) is not None]

    if len(candidats) < 2:
        await interaction.response.send_message("❌ Mentionnez au moins 2 rôles de candidats pour le tirage.", ephemeral=True)
        return

    if nombre_boules_noires >= len(candidats) or nombre_boules_noires < 1:
        await interaction.response.send_message("❌ Le nombre de boules noires doit être compris entre 1 et le nombre de candidats - 1.", ephemeral=True)
        return

    await interaction.response.send_message("🏺 Lancement du tirage au sort dans le salon...", ephemeral=True)

    symbole_sauve = couleur_sauveur.value if couleur_sauveur else "⚪ Blanche"

    embed_intro = discord.Embed(
        title="🏺 LE TIRAGE DES BOULES",
        description=(
            f"**{len(candidats)} aventuriers** s'avancent vers le sac pour sceller leur destin.\n\n"
            f"📦 **Composition du sac :**\n"
            f"- {len(candidats) - nombre_boules_noires}x {symbole_sauve}\n"
            f"- {nombre_boules_noires}x ⚫ **Boule Noire**\n\n"
            "*(Chaque aventurier va plonger sa main dans le sac...)*"
        ),
        color=discord.Color.dark_grey()
    )
    embed_intro.set_footer(text="Le destin est en marche...")
    message_principal = await channel.send(embed=embed_intro)

    await asyncio.sleep(4)

    sac = (["⚫ Noire"] * nombre_boules_noires) + ([symbole_sauve] * (len(candidats) - nombre_boules_noires))
    random.shuffle(sac)

    ordre_passage = list(candidats)
    random.shuffle(ordre_passage)

    victimes_boule_noire = []
    texte_revelations = ""

    for i, candidat in enumerate(ordre_passage, 1):
        boule_tiree = sac.pop()

        if "Noire" in boule_tiree:
            victimes_boule_noire.append(candidat)
            symbole_affichage = "⚫ **BOULE NOIRE !**"
        else:
            symbole_affichage = f"{symbole_sauve} *(Sauf !)*"

        texte_revelations += f"**{i}.** {candidat.mention} plonge sa main dans le sac...\n➡️ Résultat : {symbole_affichage}\n\n"

        embed_update = discord.Embed(
            title="🏺 LE TIRAGE DES BOULES — EN COURS",
            description=texte_revelations,
            color=discord.Color.orange() if not victimes_boule_noire else discord.Color.red()
        )
        await message_principal.edit(embed=embed_update)
        await asyncio.sleep(3.5)

    mentions_victimes = ", ".join([v.mention for v in victimes_boule_noire])
    verdict_text = (
        f"{texte_revelations}"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"☠️ **VERDICT DU DESTIN :**\n"
        f"{mentions_victimes} {'ont' if len(victimes_boule_noire) > 1 else 'a'} tiré la **Boule Noire** !"
    )

    embed_final = discord.Embed(
        title="🏺 LE TIRAGE DES BOULES — VERDICT FINAL",
        description=verdict_text,
        color=discord.Color.dark_red()
    )
    embed_final.set_footer(text="La sentence du tirage au sort est irrévocable.")
    await message_principal.edit(embed=embed_final)


# ========================================================
# 11. GESTION DES BINÔMES (DESTINS LIÉS EN 2 ÉTAPES)
# ========================================================

@bot.tree.command(
    name="tirer_binomes",
    description="Étape 1 : Tire au sort les binômes avec animation (sans créer les salons)."
)
@app_commands.describe(
    role_equipe_a="Premier rôle d'équipe (ex: @Jaune)",
    role_equipe_b="Deuxième rôle d'équipe (ex: @Rouge)"
)
@app_commands.check(est_orga_ou_admin)
async def tirer_binomes(
    interaction: discord.Interaction,
    role_equipe_a: discord.Role,
    role_equipe_b: discord.Role
):
    global DERNIERS_BINOMES_TIRES
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    channel = interaction.channel

    membres_a = [m for m in role_equipe_a.members if not m.bot]
    membres_b = [m for m in role_equipe_b.members if not m.bot]

    if not membres_a or not membres_b:
        await interaction.followup.send("❌ Au moins une des deux équipes ne contient aucun membre.", ephemeral=True)
        return

    if len(membres_a) != len(membres_b):
        await interaction.followup.send(
            f"❌ Les équipes ne sont pas équilibrées : {len(membres_a)} membres dans {role_equipe_a.mention} contre {len(membres_b)} dans {role_equipe_b.mention}.",
            ephemeral=True
        )
        return

    total_binomes = len(membres_a)

    candidats_a = [
        {
            "member": m,
            "role": trouver_role_personnel(m, role_equipe_a),
            "display_name": m.display_name,
            "clean_name": formater_nom_salon(m.display_name)
        }
        for m in membres_a
    ]

    candidats_b = [
        {
            "member": m,
            "role": trouver_role_personnel(m, role_equipe_b),
            "display_name": m.display_name,
            "clean_name": formater_nom_salon(m.display_name)
        }
        for m in membres_b
    ]

    random.shuffle(candidats_a)
    random.shuffle(candidats_b)

    DERNIERS_BINOMES_TIRES = list(zip(candidats_a, candidats_b))

    await interaction.followup.send(
        f"🏺 Lancement du tirage au sort des **{total_binomes} binômes** en direct...",
        ephemeral=True
    )

    embed_intro = discord.Embed(
        title="⚡ LE TIRAGE DES DESTINS LIÉS ⚡",
        description=(
            f"Les destins de **{role_equipe_a.mention}** et **{role_equipe_b.mention}** vont être scellés !\n\n"
            f"**{total_binomes} binômes mixtes** vont être formés.\n\n"
            "*(Tirage au sort en cours...)*"
        ),
        color=discord.Color.gold()
    )
    embed_intro.set_footer(text="Formation des duos par tirage aléatoire...")
    message_principal = await channel.send(embed=embed_intro)

    await asyncio.sleep(3)

    texte_binomes = ""
    for i, (ca, cb) in enumerate(DERNIERS_BINOMES_TIRES, 1):
        texte_binomes += f"🔗 **Binôme #{i} :** **{ca['display_name']}** ({ca['member'].mention}) & **{cb['display_name']}** ({cb['member'].mention})\n"

        embed_update = discord.Embed(
            title="⚡ LE TIRAGE DES DESTINS LIÉS — EN COURS ⚡",
            description=texte_binomes,
            color=discord.Color.orange()
        )
        await message_principal.edit(embed=embed_update)
        await asyncio.sleep(3)

    embed_final = discord.Embed(
        title="⚡ DESTINS LIÉS — TIRAGE TERMINÉ ⚡",
        description=(
            f"{texte_binomes}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👉 *Pour ouvrir les salons privés, utilisez :*\n"
            f"`/creer_salons_binomes nom_categorie:🔥 DESTINS LIÉS`"
        ),
        color=discord.Color.green()
    )
    embed_final.set_footer(text="Tirage validé. Prêt pour la création des espaces privés.")
    await message_principal.edit(embed=embed_final)


@bot.tree.command(
    name="creer_salons_binomes",
    description="Étape 2 : Crée les salons privés pour le dernier tirage de binômes effectué."
)
@app_commands.describe(nom_categorie="Nom de la catégorie où créer les salons (ex: 🔥 DESTINS LIÉS)")
@app_commands.check(est_orga_ou_admin)
async def creer_salons_binomes(interaction: discord.Interaction, nom_categorie: str):
    global DERNIERS_BINOMES_TIRES
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    if not DERNIERS_BINOMES_TIRES:
        await interaction.followup.send(
            "❌ Aucun tirage en attente. Lancez d'abord `/tirer_binomes`.",
            ephemeral=True
        )
        return

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    clean_target_name = nettoyer_texte(nom_categorie)
    existing_category = discord.utils.find(lambda c: nettoyer_texte(c.name) == clean_target_name, guild.categories)

    category_index = 1
    if existing_category:
        current_category = existing_category
        channel_count_in_current_cat = len(existing_category.channels)
    else:
        current_category = await guild.create_category(nom_categorie)
        channel_count_in_current_cat = 0

    total_salons = len(DERNIERS_BINOMES_TIRES)
    await interaction.followup.send(
        f"⏳ Création des **{total_salons} salons de binômes** dans **{nom_categorie}**...",
        ephemeral=True
    )

    salons_crees = []

    for ca, cb in DERNIERS_BINOMES_TIRES:
        if channel_count_in_current_cat >= MAX_CHANNELS_PER_CATEGORY:
            category_index += 1
            current_category = await guild.create_category(f"{nom_categorie} - {category_index}")
            channel_count_in_current_cat = 0
            await asyncio.sleep(1)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, view_channel=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True)
        }

        if ca["role"]:
            overwrites[ca["role"]] = discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True)

        if cb["role"]:
            overwrites[cb["role"]] = discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True)

        if role_spectateurs:
            overwrites[role_spectateurs] = get_spectateur_overwrites()

        if role_orgas:
            overwrites[role_orgas] = discord.PermissionOverwrite(
                read_messages=True, view_channel=True, read_message_history=True, send_messages=True
            )

        nom_salon = f"🔗・{ca['clean_name']}-{cb['clean_name']}"
        salon = await guild.create_text_channel(name=nom_salon, category=current_category, overwrites=overwrites)
        channel_count_in_current_cat += 1
        salons_crees.append(salon.mention)

        await asyncio.sleep(0.5)

    DERNIERS_BINOMES_TIRES = []

    await interaction.followup.send(
        f"✅ **{total_salons} salons créés avec succès** dans **{current_category.name}** !\n\n" + "\n".join(salons_crees),
        ephemeral=True
    )


# ========================================================
# 12. GESTION DU CONSEIL (ISOLATION & RESTAURATION)
# ========================================================

@bot.tree.command(
    name="activer_conseil",
    description="Isole une équipe pour le conseil : retire leurs rôles perso (accès camp & confessionnal uniquement)."
)
@app_commands.describe(role_equipe="L'équipe qui se rend au conseil (ex: @Jaune ou @Rouge)")
@app_commands.check(est_orga_ou_admin)
async def activer_conseil(interaction: discord.Interaction, role_equipe: discord.Role):
    global ROLES_PERSO_EN_PAUSE
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    membres_cibles = []
    async for member in guild.fetch_members(limit=None):
        if not member.bot and role_equipe.id in [r.id for r in member.roles]:
            membres_cibles.append(member)

    if not membres_cibles:
        await interaction.followup.send(f"❌ Aucun candidat trouvé avec le rôle {role_equipe.mention}.", ephemeral=True)
        return

    isoles = 0
    erreurs = []

    for m in membres_cibles:
        r_perso = trouver_role_personnel(m, role_equipe)
        if r_perso:
            if r_perso >= guild.me.top_role:
                erreurs.append(f"⚠️ Le rôle {r_perso.name} est plus haut que le rôle du bot !")
                continue
            
            try:
                await m.remove_roles(r_perso, reason=f"Activation du Conseil pour {role_equipe.name}")
                ROLES_PERSO_EN_PAUSE[m.id] = r_perso.id
                isoles += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                erreurs.append(f"❌ {m.display_name} : {e}")
        else:
            erreurs.append(f"❓ Aucun rôle personnel détecté pour **{m.display_name}**")

    texte_reponse = (
        f"🔒 **Conseil Activé pour {role_equipe.mention} !**\n"
        f"- **{isoles}/{len(membres_cibles)} candidat(s)** ont perdu temporairement leur rôle personnel.\n"
        f"- Ils n'ont plus accès qu'à leur camp (`#discussion-generale`) et leur confessionnal.\n"
    )

    if erreurs:
        texte_reponse += "\n**Détails / Alertes :**\n" + "\n".join(erreurs[:5])

    await interaction.followup.send(texte_reponse, ephemeral=True)


@bot.tree.command(
    name="desactiver_conseil",
    description="Restaure les rôles personnels des candidats d'une équipe après le conseil."
)
@app_commands.describe(role_equipe="L'équipe qui revient du conseil (ex: @Jaune ou @Rouge)")
@app_commands.check(est_orga_ou_admin)
async def desactiver_conseil(interaction: discord.Interaction, role_equipe: discord.Role):
    global ROLES_PERSO_EN_PAUSE
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    restaures = 0
    async for member in guild.fetch_members(limit=None):
        if member.bot or role_equipe not in member.roles:
            continue

        role_a_rendre = None

        if member.id in ROLES_PERSO_EN_PAUSE:
            role_id = ROLES_PERSO_EN_PAUSE[member.id]
            role_a_rendre = guild.get_role(role_id)

        if not role_a_rendre:
            nom_clean = nettoyer_texte(member.display_name)
            for r in guild.roles:
                if nettoyer_texte(r.name) == nom_clean and not any(ign in r.name.lower() for ign in ROLES_GENERIQUES_A_IGNORER):
                    role_a_rendre = r
                    break

        if role_a_rendre and role_a_rendre not in member.roles:
            try:
                await member.add_roles(role_a_rendre, reason=f"Fin du Conseil pour {role_equipe.name}")
                restaures += 1
                if member.id in ROLES_PERSO_EN_PAUSE:
                    del ROLES_PERSO_EN_PAUSE[member.id]
                await asyncio.sleep(0.3)
            except Exception as e:
                print(f"Erreur lors de la remise du rôle à {member.display_name}: {e}")

    await interaction.followup.send(
        f"🔓 **Conseil Désactivé pour {role_equipe.mention} !**\n"
        f"- **{restaures} candidat(s)** ont récupéré leur rôle personnel.\n"
        f"- Leurs accès aux salons duos, binômes et discussions privées sont rouverts.",
        ephemeral=True
    )


# ========================================================
# 13. CHRONOMÈTRES, QUESTION FLASH ET ANTI-TRICHE
# ========================================================

@bot.tree.command(
    name="poser_question_flash",
    description="Pose une seule question flash ultra-lisible avec chrono dynamique Discord."
)
@app_commands.describe(
    question="La question à poser au candidat",
    secondes="Temps limite en secondes (ex: 15)"
)
@app_commands.check(est_orga_ou_admin)
async def poser_question_flash(
    interaction: discord.Interaction,
    question: str,
    secondes: int = 15
):
    channel = interaction.channel
    now = datetime.datetime.now(datetime.timezone.utc)
    fin_timestamp = int((now + datetime.timedelta(seconds=secondes)).timestamp())

    embed_question = discord.Embed(
        description=(
            f"# {question}\n\n"
            f"## ⏳ Fin du temps : <t:{fin_timestamp}:R>"
        ),
        color=discord.Color.from_rgb(255, 69, 0)
    )

    await interaction.response.send_message(embed=embed_question)
    original_msg = await interaction.original_response()

    def check(m: discord.Message):
        return m.channel.id == channel.id and not m.author.bot

    debut_time = datetime.datetime.now()

    try:
        reponse_msg = await bot.wait_for("message", timeout=secondes, check=check)
        temps_pris = round((datetime.datetime.now() - debut_time).total_seconds(), 2)

        embed_fige = discord.Embed(
            description=(
                f"# {question}\n\n"
                f"## ⏱️ Répondu en `{temps_pris}s`"
            ),
            color=discord.Color.green()
        )
        await original_msg.edit(embed=embed_fige)

        embed_reponse = discord.Embed(
            description=(
                f"# ✅ RÉPONSE VALIDÉE\n\n"
                f"💬 **Réponse :** `{reponse_msg.content}`\n"
                f"⚡ **Temps :** `{temps_pris}s`"
            ),
            color=discord.Color.green()
        )
        await channel.send(embed=embed_reponse)

    except asyncio.TimeoutError:
        embed_timeout_fige = discord.Embed(
            description=(
                f"# {question}\n\n"
                f"## 🛑 TEMPS ÉCOULÉ"
            ),
            color=discord.Color.dark_red()
        )
        await original_msg.edit(embed=embed_timeout_fige)

        embed_fin = discord.Embed(
            description="# 🛑 TEMPS ÉCOULÉ !",
            color=discord.Color.dark_red()
        )
        await channel.send(embed=embed_fin)


@bot.tree.command(
    name="chrono_go",
    description="Lance le top départ d'une épreuve de recherche/fouille et démarre le chronomètre."
)
@app_commands.describe(
    cible="Le candidat ou l'équipe (ex: @Lucas ou @Jaune)",
    epreuve="Nom ou objectif de l'épreuve"
)
@app_commands.check(est_orga_ou_admin)
async def chrono_go(
    interaction: discord.Interaction,
    cible: discord.Role,
    epreuve: str = "Épreuve de recherche"
):
    channel = interaction.channel
    now = datetime.datetime.now(datetime.timezone.utc)
    
    cle = f"{channel.id}_{cible.id}"
    CHRONOS_EN_COURS[cle] = datetime.datetime.now()

    timestamp_actuel = int(now.timestamp())

    embed = discord.Embed(
        title="🟢 TOP DÉPART — CHRONOMÈTRE LANCÉ !",
        description=(
            f"🎯 **Épreuve :** {epreuve}\n"
            f"👤 **Candidat / Équipe :** {cible.mention}\n\n"
            f"⏱️ **Chronomètre en cours :** <t:{timestamp_actuel}:R>\n"
            f"*(L'orga utilisera `/chrono_stop` dès validation de la trouvaille)*"
        ),
        color=discord.Color.green()
    )
    embed.set_footer(text="Que le meilleur gagne !")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="chrono_stop",
    description="Stoppe le chronomètre de l'épreuve et calcule le temps total exact."
)
@app_commands.describe(
    cible="Le candidat ou l'équipe concernée (ex: @Lucas ou @Jaune)"
)
@app_commands.check(est_orga_ou_admin)
async def chrono_stop(
    interaction: discord.Interaction,
    cible: discord.Role
):
    channel = interaction.channel
    cle = f"{channel.id}_{cible.id}"

    if cle not in CHRONOS_EN_COURS:
        await interaction.response.send_message(
            f"❌ Aucun chronomètre en cours pour {cible.mention} dans ce salon.",
            ephemeral=True
        )
        return

    debut = CHRONOS_EN_COURS.pop(cle)
    fin = datetime.datetime.now()
    duree_totale = (fin - debut).total_seconds()

    minutes = int(duree_totale // 60)
    secondes = round(duree_totale % 60, 2)

    if minutes > 0:
        temps_affiche = f"{minutes} min {secondes} s"
    else:
        temps_affiche = f"{secondes} secondes"

    embed = discord.Embed(
        title="🏁 FIN DE L'ÉPREUVE — TEMPS VALIDÉ !",
        description=(
            f"👤 **Candidat / Équipe :** {cible.mention}\n\n"
            f"⏱️ **Temps réalisé :** `{temps_affiche}`\n"
            f"*(Précision brute : {round(duree_totale, 2)}s)*"
        ),
        color=discord.Color.gold()
    )
    embed.set_footer(text="Performance enregistrée par les Orgas.")
    await interaction.response.send_message(embed=embed)


# ========================================================
# 14. QUIZ & ÉPREUVES AUTOMATISÉES (CONFIDENTIALITÉ TOTALE)
# ========================================================

class GlobalQuizLancementView(discord.ui.View):
    def __init__(self, candidat: discord.Member):
        super().__init__(timeout=1800)
        self.candidat = candidat

    @discord.ui.button(label="🚀 DÉMARRER MON ÉPREUVE", style=discord.ButtonStyle.green, custom_id="btn_global_quiz_start")
    async def demarrer_quiz(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not CONFIG_EPREUVE_GLOBALE["active"]:
            await interaction.response.send_message("⛔ L'épreuve est actuellement clôturée ou non configurée.", ephemeral=True)
            return

        if interaction.user.id != self.candidat.id:
            await interaction.response.send_message("⛔ Seul le candidat concerné peut lancer son épreuve.", ephemeral=True)
            return

        button.disabled = True
        button.label = "⏳ ÉPREUVE EN COURS..."
        button.style = discord.ButtonStyle.grey
        await interaction.response.edit_message(view=self)

        channel = interaction.channel
        questions = CONFIG_EPREUVE_GLOBALE["questions"]

        if not questions:
            await channel.send("❌ Erreur : Aucune question chargée dans la configuration.")
            return

        msg_decompte = await channel.send("⚠️ **Attention... L'épreuve commence dans : 3**")
        for k in range(2, 0, -1):
            await asyncio.sleep(1)
            await msg_decompte.edit(content=f"⚠️ **Attention... L'épreuve commence dans : {k}**")
        await asyncio.sleep(1)
        await msg_decompte.delete()

        resultats = []

        for i, q in enumerate(questions, 1):
            secondes = q["secondes"]

            def build_description(secondes_restantes: int, fige_msg: str = None):
                if fige_msg:
                    return f"# Question {i}/{len(questions)}\n\n# **{q['question']}**\n\n## {fige_msg}"
                
                pourcentage = max(0, min(1, secondes_restantes / secondes))
                blocs_pleins = int(pourcentage * 10)
                barre = "🟩" * blocs_pleins + "⬛" * (10 - blocs_pleins)

                return (
                    f"# Question {i}/{len(questions)}\n\n"
                    f"# **{q['question']}**\n\n"
                    f"## ⏳ Temps restant : **{secondes_restantes}s** / {secondes}s\n"
                    f"{barre}"
                )

            embed_q = discord.Embed(
                description=build_description(secondes),
                color=discord.Color.from_rgb(255, 69, 0)
            )
            q_msg = await channel.send(embed=embed_q)

            def check_reponse(m: discord.Message):
                return m.channel.id == channel.id and m.author.id == self.candidat.id

            start_perf = time.perf_counter()
            tache_reponse = asyncio.create_task(bot.wait_for("message", check=check_reponse))

            reponse_recue = False
            reponse_msg = None
            temps_ecoule = secondes

            while not tache_reponse.done():
                ecoule_live = time.perf_counter() - start_perf
                restant_live = int(secondes - ecoule_live)

                if restant_live <= 0:
                    break

                try:
                    reponse_msg = await asyncio.wait_for(asyncio.shield(tache_reponse), timeout=3.0)
                    reponse_recue = True
                    temps_ecoule = round(time.perf_counter() - start_perf, 2)
                    break
                except asyncio.TimeoutError:
                    sec_rest = max(0, int(secondes - (time.perf_counter() - start_perf)))
                    embed_maj = discord.Embed(
                        description=build_description(sec_rest),
                        color=discord.Color.from_rgb(255, 69, 0)
                    )
                    try:
                        await q_msg.edit(embed=embed_maj)
                    except Exception:
                        pass

            if not reponse_recue and not tache_reponse.done():
                tache_reponse.cancel()

            if reponse_recue and reponse_msg:
                temps_restant = round(max(0, secondes - temps_ecoule), 2)
                embed_fige = discord.Embed(
                    description=build_description(0, fige_msg=f"⏱️ Répondu en `{temps_ecoule}s` *(restait {temps_restant}s)*"),
                    color=discord.Color.green()
                )
                try:
                    await q_msg.edit(embed=embed_fige)
                except Exception:
                    pass

                resultats.append({
                    "index": i,
                    "question": q["question"],
                    "reponse": reponse_msg.content,
                    "temps_pris": f"{temps_ecoule}s",
                    "statut": "✅ Répondu"
                })
            else:
                embed_fige = discord.Embed(
                    description=build_description(0, fige_msg="🛑 TEMPS ÉCOULÉ"),
                    color=discord.Color.dark_red()
                )
                try:
                    await q_msg.edit(embed=embed_fige)
                except Exception:
                    pass

                resultats.append({
                    "index": i,
                    "question": q["question"],
                    "reponse": "*Aucune réponse*",
                    "temps_pris": f"{secondes}s",
                    "statut": "❌ Hors délai"
                })

            await asyncio.sleep(4.0)
            try:
                await q_msg.delete()
                if reponse_msg:
                    await reponse_msg.delete()
            except Exception:
                pass

        embed_fin_joueur = discord.Embed(
            title="🏁 ÉPREUVE TERMINÉE",
            description="Tes réponses et tes temps ont bien été transmis aux Organisateurs. Merci !",
            color=discord.Color.green()
        )
        await channel.send(embed=embed_fin_joueur)

        result_channel = bot.get_channel(RESULTATS_CHANNEL_ID)
        if result_channel:
            lignes_recap = []
            for r in resultats:
                lignes_recap.append(
                    f"**Q{r['index']}. {r['question']}**\n"
                    f"💬 `{r['reponse']}` ({r['statut']} en `{r['temps_pris']}`)\n"
                )

            embed_recap_orga = discord.Embed(
                title=f"📊 RÉSULTATS ÉPREUVE — {self.candidat.display_name}",
                description="\n".join(lignes_recap),
                color=discord.Color.gold()
            )
            embed_recap_orga.set_footer(text=f"ID Candidat : {self.candidat.id} • Confessionnal : #{channel.name}")
            
            recap_str = "\n".join(lignes_recap)
            if len(recap_str) > 3900:
                await result_channel.send(f"📊 **RÉSULTATS DE L'ÉPREUVE — {self.candidat.mention}**")
                for chunk in [recap_str[j:j+1900] for j in range(0, len(recap_str), 1900)]:
                    await result_channel.send(chunk)
            else:
                await result_channel.send(embed=embed_recap_orga)


@bot.tree.command(
    name="configurer_epreuve",
    description="Étape 1 : Charge et enregistre la banque de questions pour tous les candidats."
)
@app_commands.describe(
    salon_questions="Le salon secret où se trouvent les questions",
    temps_par_defaut="Temps par défaut en secondes si non spécifié (ex: 15)"
)
@app_commands.check(est_orga_ou_admin)
async def configurer_epreuve(
    interaction: discord.Interaction,
    salon_questions: discord.TextChannel,
    temps_par_defaut: int = 15
):
    await interaction.response.defer(ephemeral=True)
    global CONFIG_EPREUVE_GLOBALE

    questions = []
    async for msg in salon_questions.history(limit=25, oldest_first=False):
        if not msg.author.bot and msg.content.strip():
            for ligne in msg.content.strip().split("\n"):
                ligne = ligne.strip()
                if not ligne:
                    continue
                if "|" in ligne:
                    parties = ligne.split("|")
                    q_txt = parties[0].strip()
                    try:
                        t_sec = int(parties[1].strip())
                    except ValueError:
                        t_sec = temps_par_defaut
                else:
                    q_txt = ligne
                    t_sec = temps_par_defaut
                
                questions.append({"question": q_txt, "secondes": t_sec})
            if questions:
                break

    if not questions:
        await interaction.followup.send("❌ Aucune question valide trouvée dans le salon source.", ephemeral=True)
        return

    CONFIG_EPREUVE_GLOBALE["questions"] = questions
    CONFIG_EPREUVE_GLOBALE["temps_par_defaut"] = temps_par_defaut
    CONFIG_EPREUVE_GLOBALE["active"] = True

    await interaction.followup.send(
        f"✅ **Configuration enregistrée avec succès !**\n"
        f"- 📝 **{len(questions)} questions** chargées depuis {salon_questions.mention}.\n"
        f"- ⏱️ **Temps de base :** `{temps_par_defaut}s` par question.\n"
        f"- 📬 Les récaps seront envoyés sur <#{RESULTATS_CHANNEL_ID}>.\n\n"
        f"👉 *Lance maintenant `/lancer_epreuve`.*",
        ephemeral=True
    )


@bot.tree.command(
    name="lancer_epreuve",
    description="Étape 2 : Déploie l'épreuve pour un candidat précis (ou en masse sur une catégorie)."
)
@app_commands.describe(
    candidat="Optionnel : le candidat ciblé pour qui déployer l'épreuve",
    salon_cible="Optionnel : le salon où déployer (par défaut : le salon actuel)",
    nom_categorie="Optionnel : nom de la catégorie pour déployer dans tous les confessionnaux en masse"
)
@app_commands.check(est_orga_ou_admin)
async def lancer_epreuve(
    interaction: discord.Interaction,
    candidat: discord.Member = None,
    salon_cible: discord.TextChannel = None,
    nom_categorie: str = None
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    if not CONFIG_EPREUVE_GLOBALE["active"] or not CONFIG_EPREUVE_GLOBALE["questions"]:
        await interaction.followup.send("❌ Aucune épreuve n'est configurée. Lance d'abord `/configurer_epreuve`.", ephemeral=True)
        return

    if candidat:
        target_ch = salon_cible or interaction.channel
        if not isinstance(target_ch, discord.TextChannel):
            await interaction.followup.send("❌ Le salon cible doit être un salon textuel.", ephemeral=True)
            return

        view = GlobalQuizLancementView(candidat=candidat)
        embed_invit = discord.Embed(
            title="🏺 ÉPREUVE DE RAPIDITÉ",
            description=(
                f"Bienvenue {candidat.mention} pour ton épreuve.\n\n"
                f"📌 **Consignes :**\n"
                f"- Les questions s'enchaînent automatiquement.\n"
                f"- Écris ta réponse directement ici.\n"
                f"- Les questions et réponses s'effaceront au fur et à mesure pour la confidentialité.\n\n"
                f"👉 **Clique sur le bouton vert ci-dessous dès que tu es prêt :**"
            ),
            color=discord.Color.dark_gold()
        )

        await target_ch.send(embed=embed_invit, view=view)
        await interaction.followup.send(
            f"🚀 **Épreuve déployée pour {candidat.mention}** dans {target_ch.mention} !",
            ephemeral=True
        )
        return

    if nom_categorie:
        cat_clean = nettoyer_texte(nom_categorie)
        category = discord.utils.find(lambda c: nettoyer_texte(c.name) == cat_clean, guild.categories)
        if not category:
            await interaction.followup.send(f"❌ Catégorie **{nom_categorie}** introuvable.", ephemeral=True)
            return

        salons_cibles = [ch for ch in category.channels if isinstance(ch, discord.TextChannel)]
        deplois = 0

        for ch in salons_cibles:
            candidat_trouve = None
            for cible, overwrite in ch.overwrites.items():
                if isinstance(cible, discord.Member) and not cible.bot:
                    candidat_trouve = cible
                    break
                elif isinstance(cible, discord.Role) and cible.name not in [ROLE_ORGAS_NAME, ROLE_SPECTATEURS_NAME, "@everyone"]:
                    for m in ch.guild.members:
                        if cible in m.roles and not m.bot:
                            candidat_trouve = m
                            break
                    if candidat_trouve:
                        break

            if not candidat_trouve:
                continue

            view = GlobalQuizLancementView(candidat=candidat_trouve)
            embed_invit = discord.Embed(
                title="🏺 ÉPREUVE DE RAPIDITÉ",
                description=(
                    f"Bienvenue {candidat_trouve.mention} pour ton épreuve.\n\n"
                    f"📌 **Consignes :**\n"
                    f"- Les questions s'enchaînent automatiquement.\n"
                    f"- Écris ta réponse directement ici.\n"
                    f"- Les questions et réponses s'effaceront au fur et à mesure pour la confidentialité.\n\n"
                    f"👉 **Clique sur le bouton vert ci-dessous dès que tu es prêt :**"
                ),
                color=discord.Color.dark_gold()
            )

            await ch.send(embed=embed_invit, view=view)
            deplois += 1
            await asyncio.sleep(0.4)

        await interaction.followup.send(
            f"🚀 **Épreuve déployée sur {deplois} salon(s)** de la catégorie **{category.name}** !",
            ephemeral=True
        )
        return

    await interaction.followup.send("❌ Veuillez renseigner un `candidat` ou un `nom_categorie`.", ephemeral=True)


@bot.tree.command(
    name="terminer_epreuve",
    description="Étape 3 : Clôture définitivement l'épreuve en cours et réinitialise la configuration."
)
@app_commands.check(est_orga_ou_admin)
async def terminer_epreuve(interaction: discord.Interaction):
    global CONFIG_EPREUVE_GLOBALE
    CONFIG_EPREUVE_GLOBALE["active"] = False
    CONFIG_EPREUVE_GLOBALE["questions"] = []

    await interaction.response.send_message(
        "🛑 **Épreuve clôturée !**\n"
        "- Les boutons encore actifs ne peuvent plus lancer de questions.\n"
        "- La configuration en mémoire a été réinitialisée.",
        ephemeral=True
    )


# ========================================================
# 15. PRÉSENTATIONS (EXTRACTION PAR IA)
# ========================================================

async def analyser_candidat_ia(texte: str) -> dict:
    if not texte:
        return {"nom": "Candidat", "texte": ""}

    prompt = (
        "Voici la présentation d'un participant à un jeu :\n\n"
        f"\"\"\"{texte}\"\"\"\n\n"
        "Consignes :\n"
        "1. Donne le prénom de la personne qui se présente.\n"
        "2. Corrige les fautes d'orthographe et la ponctuation sans modifier le style.\n\n"
        "Format de réponse obligatoire :\n"
        "PRENOM: <prénom seul>\n"
        "TEXTE: <texte corrigé>"
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt
        )
        
        rep = response.text.strip()

        prenom_match = re.search(r"PRENOM\s*:\s*\**([A-Za-zÀ-ÿ\-]+)\**", rep, re.IGNORECASE)
        prenom = prenom_match.group(1).capitalize() if prenom_match else ""

        texte_propre = texte.strip()
        if "TEXTE:" in rep:
            texte_propre = rep.split("TEXTE:", 1)[1].strip()

        if prenom:
            return {"nom": prenom, "texte": texte_propre}

    except Exception as e:
        print(f"Erreur lors de l'appel Gemini : {e}")

    lignes = [l.strip() for l in texte.split("\n") if l.strip()]
    premier_mot = lignes[0].split()[0].replace(":", "").capitalize() if lignes else "Candidat"
    return {"nom": premier_mot, "texte": texte.strip()}


def creer_embed_presentation_pure(
    prenom: str,
    texte: str,
    image_url: str = None
) -> discord.Embed:
    """Génère la fiche avec le prénom en titre et la photo du candidat."""
    embed = discord.Embed(
        title=f"🌴 {prenom.upper()}",
        description=texte,
        color=discord.Color.gold()
    )

    if image_url:
        embed.set_image(url=image_url)

    embed.set_footer(text=f"Aventurier : {prenom} • Fiche officielle")
    return embed


@bot.tree.command(
    name="formater_presentation",
    description="Publie la présentation d'un message unique sous forme de fiche propre avec sa photo."
)
@app_commands.describe(
    message_id_ou_lien="L'ID du message ou son lien Discord",
    salon_destination="Optionnel : salon où envoyer l'embed (par défaut : salon actuel)"
)
@app_commands.check(est_orga_ou_admin)
async def formater_presentation(
    interaction: discord.Interaction,
    message_id_ou_lien: str,
    salon_destination: discord.TextChannel = None
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    dest_channel = salon_destination or interaction.channel

    msg_id = message_id_ou_lien.strip().split("/")[-1]
    try:
        msg_id_int = int(msg_id)
    except ValueError:
        await interaction.followup.send("❌ Lien ou ID de message invalide.", ephemeral=True)
        return

    source_msg = None
    try:
        source_msg = await interaction.channel.fetch_message(msg_id_int)
    except Exception:
        for ch in guild.text_channels:
            try:
                source_msg = await ch.fetch_message(msg_id_int)
                if source_msg:
                    break
            except Exception:
                continue

    if not source_msg:
        await interaction.followup.send("❌ Message introuvable sur le serveur.", ephemeral=True)
        return

    texte_brut = source_msg.content.strip()
    image_url = None
    if source_msg.attachments:
        for att in source_msg.attachments:
            if att.content_type and att.content_type.startswith("image/"):
                image_url = att.url
                break

    res_ia = await analyser_candidat_ia(texte_brut)
    embed = creer_embed_presentation_pure(
        prenom=res_ia["nom"],
        texte=res_ia["texte"],
        image_url=image_url
    )

    await dest_channel.send(embed=embed)
    await interaction.followup.send(f"✅ Fiche publiée dans {dest_channel.mention} !", ephemeral=True)


# ========================================================
# 16. SCAN DU FIL PAR PAIRES (AVEC QUOTA CANDIDATS)
# ========================================================

@bot.tree.command(
    name="scanner_fil_presentations",
    description="Extrait le nombre demandé de présentations (Texte ➔ Photo) depuis un fil."
)
@app_commands.describe(
    salon_destination="Le salon où afficher les fiches finales (ex: #presentation)",
    nombre_candidats="Nombre exact de candidats à extraire (ex: 18)",
    fil="Optionnel : Le fil contenant les présentations (par défaut : fil actuel)"
)
@app_commands.check(est_orga_ou_admin)
async def scanner_fil_presentations(
    interaction: discord.Interaction,
    salon_destination: discord.TextChannel,
    nombre_candidats: int,
    fil: discord.Thread = None
):
    await interaction.response.defer(ephemeral=True)

    if nombre_candidats <= 0:
        await interaction.followup.send("❌ Le nombre de candidats doit être supérieur à 0.", ephemeral=True)
        return

    source_thread = fil
    if not source_thread:
        if isinstance(interaction.channel, discord.Thread):
            source_thread = interaction.channel
        else:
            await interaction.followup.send("❌ Exécute la commande dans le fil ou mentionne-le.", ephemeral=True)
            return

    raw_messages = [msg async for msg in source_thread.history(limit=250, oldest_first=True)]
    messages = [m for m in raw_messages if not m.author.bot and (m.content.strip() or m.attachments)]

    if not messages:
        await interaction.followup.send("❌ Aucun message trouvé dans ce fil.", ephemeral=True)
        return

    await interaction.followup.send(
        f"⏳ Extraction de **{nombre_candidats} candidats** depuis {source_thread.mention} vers {salon_destination.mention}...",
        ephemeral=True
    )

    paires = []
    i = 0
    total = len(messages)

    while i < total:
        msg = messages[i]
        texte = msg.content.strip()
        image_url = None

        if msg.attachments:
            for att in msg.attachments:
                if att.content_type and att.content_type.startswith("image/"):
                    image_url = att.url
                    break

        if texte and not image_url and (i + 1 < total):
            next_msg = messages[i + 1]
            if next_msg.attachments:
                for att in next_msg.attachments:
                    if att.content_type and att.content_type.startswith("image/"):
                        image_url = att.url
                        i += 1
                        break

        if texte:
            paires.append({"texte": texte, "image_url": image_url})

        if len(paires) >= nombre_candidats:
            break

        i += 1

    total_publies = 0
    for item in paires:
        res_ia = await analyser_candidat_ia(item["texte"])
        prenom = res_ia["nom"]
        texte_corrige = res_ia["texte"]

        embed = creer_embed_presentation_pure(
            prenom=prenom,
            texte=texte_corrige,
            image_url=item["image_url"]
        )

        await salon_destination.send(embed=embed)
        total_publies += 1
        await asyncio.sleep(2.0)

    await salon_destination.send(f"✨ **Les {total_publies} fiches d'aventuriers ont été publiées avec succès !**")
    await interaction.followup.send(
        f"✅ Terminé ! **{total_publies}/{nombre_candidats} fiches** publiées dans {salon_destination.mention}.",
        ephemeral=True
    )


# ==========================================
# DÉMARRAGE DU BOT
# ==========================================
bot.run(TOKEN)
