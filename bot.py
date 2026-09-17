import os
import re
import time
import io
import zipfile
import itertools
import asyncio
import datetime
import random
import unicodedata
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from google import genai

from PIL import Image, ImageDraw, ImageFont, ImageOps

# ==========================================
# CONFIGURATION & ENVIRONNEMENT
# ==========================================

TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

MODEL_NAME = "gemini-3.5-flash-lite"

# Salons & Catégories fixes
RECAP_CHANNEL_ID = 1545076756384579726            # 📰 Journal Stratégique Global
SALON_QUESTIONS_RECAP_ID = 1546598533333909728    # 🎙️ Fiches Questions & Conseil
SALON_BILAN_CANDIDATS_ID = 1549193526007435345    # 📊 Fiches Bilans Candidats
CATEGORY_TRIO_ID = 1541397070898921482
CATEGORY_QUATUOR_ID = 1541397227744927835
RESULTATS_CHANNEL_ID = 1545186500960985148
SALON_REMARQUES_QUESTIONS_ID = 1545503543405060178
SALON_BILAN_ORGAS_ID = 1549193526007435345        # 🛠️ Fiches Bilans Organisateurs (ou l'ID de ton choix)
# Salons Spectateurs
SALON_CHAT_SPECTATEURS_ID = 1544355721024635061
SALON_RECAP_SPECTATEURS_ID = 1546598600555888670
CATEGORY_EPREUVE_ID = 1547679122099277944  # ⚔️ Catégorie Épreuves
# Salons Annonces & Présentations Orgas
SALON_ANNONCES_TRAVAIL_ID = 1545823720676003890
SALON_ANNONCES_CANDIDATS_ID = 1537439670340681828
SALON_ARCHIVES_ANNONCES_ID = 1545823386700087456
SALON_PRESENTATION_ORGAS_ID = 1546603467580383332

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
    "destin lie",
    "Alliés de l’ombre"
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
SESSIONS_RECHERCHE_ACTIVES = {}  # {channel_id: {"joueur": "...", "candidat_id": 123}}
# Suivi des minuteurs actifs : {channel_id: {"task": asyncio.Task, "start": datetime, "total_seconds": int}}
MINUTEURS_ACTIFS = {}
# Suivi du traducteur en direct : {channel_id: {"user_id": int, "nom": str}}
SESSIONS_DECODEUR = {}
CONFIG_EPREUVE_GLOBALE = {
    "questions": [],
    "temps_par_defaut": 15,
    "active": False
}

# Suivi de l'état d'épreuve par salon
ETATS_EPREUVES_SALONS = {}

# Suivi de la sélection d'équipes interactive (Draft)
ETAT_COMPOSITION = {
    "actif": False,
    "channel_id": None,
    "capitaine_1": None,
    "role_1": None,
    "capitaine_2": None,
    "role_2": None,
    "tour": 1
}

# Suivi de la planification quotidienne
DERNIER_JOUR_RECAP = None
DERNIER_JOUR_QUESTIONS = None

# ==========================================
# TÉLÉCHARGEMENT AUTOMATIQUE DES POLICES
# ==========================================
os.makedirs("fonts", exist_ok=True)
URLS_FONTS = {
    "fonts/Anton-Regular.ttf": "https://raw.githubusercontent.com/google/fonts/main/ofl/anton/Anton-Regular.ttf",
    "fonts/Roboto-Bold.ttf": "https://raw.githubusercontent.com/google/fonts/main/apache/roboto/Roboto-Bold.ttf",
    "fonts/Roboto-Regular.ttf": "https://raw.githubusercontent.com/google/fonts/main/apache/roboto/Roboto-Regular.ttf"
}
for path_font, url_font in URLS_FONTS.items():
    if not os.path.exists(path_font):
        try:
            urllib.request.urlretrieve(url_font, path_font)
        except Exception as e:
            print(f"⚠️ Police {path_font} non récupérée: {e}")

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


def decouper_texte_intelligent(texte: str, limite: int = 1900) -> list[str]:
    """Découpe un texte long sans jamais couper au milieu d'un mot ou d'une phrase."""
    if len(texte) <= limite:
        return [texte]

    morceaux = []
    texte_restant = texte.strip()

    while len(texte_restant) > limite:
        coupure = texte_restant.rfind("\n\n", 0, limite)
        if coupure == -1:
            coupure = texte_restant.rfind("\n", 0, limite)
        if coupure == -1:
            coupure = texte_restant.rfind(" ", 0, limite)
        if coupure == -1:
            coupure = limite

        morceau = texte_restant[:coupure].strip()
        if morceau:
            morceaux.append(morceau)

        texte_restant = texte_restant[coupure:].strip()

    if texte_restant:
        morceaux.append(texte_restant)

    return morceaux

# =======================================================
# 1. RÉSUMÉ DU SOIR & QUESTIONS CONFESSIONNAL ADAPTÉES
# =======================================================

async def poster_questions_automatiques(texte_recap: str):
    """Génère des questions d'interview courtes, précises et sans spoil pour les confessionnaux Discord."""
    salon_q = bot.get_channel(SALON_QUESTIONS_RECAP_ID)
    if not salon_q:
        print(f"❌ [ERREUR] Salon questions introuvable ({SALON_QUESTIONS_RECAP_ID})")
        return

    prompt_q = (
        "Tu es l'interviewer et showrunner d'un jeu de stratégie et de déduction communautaire joué SUR DISCORD.\n"
        "Voici le Journal Stratégique de la journée :\n\n"
        f"{texte_recap}\n\n"
        "Rédige une FICHE DE QUESTIONS DIRECTES ET COURTES pour l'équipe d'organisation (Staff/Orgas).\n\n"
        "RÈGLES D'OR ABSOLUES :\n"
        "1. CONTEXTE DISCORD : Le jeu se passe sur des salons, en vocal, en duos/trios et par messages. Oublie totalement les allusions à une île, au feu, aux cabanes ou à la survie physique.\n"
        "2. QUESTIONS COURTES ET IMPACTANTES : Maximum 1 à 2 phrases par question. Pose des questions directes, percutantes et fluides.\n"
        "3. NEUTRALITÉ ET ZÉRO SPOIL : Ne trahis aucun complot secret, aucune alliance cachée et ne donne aucun indice sur ce que les autres manigancent dans leur dos.\n"
        "4. MIROIR STRATÉGIQUE : Pousse le joueur à verbaliser sa lecture du jeu, sa confiance envers ses contacts et son positionnement pour les prochains votes.\n\n"
        "STRUCTURE ATTENDUE :\n\n"
        "## 🎙️ 1. QUESTIONS CONFESSIONNAL (INDIVIDUELLES)\n"
        "Sélectionne 3 à 4 candidats clés de la journée. Pour chacun :\n"
        "- **👤 [Nom du Candidat]**\n"
        "  - *Q1 :* [Question courte sur sa confiance ou son ressenti du moment sur le serveur]\n"
        "  - *Q2 :* [Question directe sur un dilemme, un choix de vote ou sa posture tactique]\n\n"
        "## ⚖️ 2. QUESTIONS CONSEIL / DÉBAT (GÉNÉRALES)\n"
        "- 3 questions ouvertes et courtes sur l'ambiance générale du serveur, la lisibilité des alliances ou la tension avant les votes (sans citer de noms).\n\n"
        "Renvoie UNIQUEMENT le texte formaté, prêt à l'emploi."
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt_q
        )
        questions_texte = response.text.strip()
        
        paris_tz = ZoneInfo("Europe/Paris")
        date_str = datetime.datetime.now(paris_tz).strftime("%d/%m/%Y")

        header = f"🎙️ **SUGGESTIONS D'INTERVIEWS DISCORD & CONSEIL — {date_str}**\n*(Réservé aux Orgas • Zéro spoil)*\n\n"
        full_msg = header + questions_texte

        for chunk in decouper_texte_intelligent(full_msg, 1900):
            await salon_q.send(chunk)
            await asyncio.sleep(0.4)
        print(f"✅ Questions postées dans #{salon_q.name} ({salon_q.id})")

    except Exception as e:
        print(f"❌ Erreur génération questions neutres : {e}")


async def generer_et_envoyer_recap_quotidien(guild: discord.Guild, target_channel: discord.TextChannel):
    """Scanne les discussions du serveur et génère le Journal Stratégique adapté à Discord."""
    tz_paris = ZoneInfo("Europe/Paris")
    maintenant_paris = datetime.datetime.now(tz_paris)
    
    debut_journee_paris = maintenant_paris.replace(hour=0, minute=0, second=0, microsecond=0)
    debut_journee_utc = debut_journee_paris.astimezone(datetime.timezone.utc)

    # Contexte des récaps précédents (limité aux 2 derniers pour économiser les tokens)
    historique_recaps = []
    async for msg in target_channel.history(limit=8, oldest_first=False):
        if msg.author.id == bot.user.id and msg.content.strip():
            if not msg.content.startswith("📋") and not msg.content.startswith("🎙️"):
                historique_recaps.append(msg.content[:1200])
        if len(historique_recaps) >= 2:
            break

    historique_recaps.reverse()
    texte_contexte_passe = (
        "\n\n--- [RÉCAP PRÉCÉDENT] ---\n\n".join(historique_recaps)
        if historique_recaps
        else "Aucun récapitulatif antérieur (Début de l'aventure)."
    )

    salons_transcripts = []
    pieces_audio_gemini = []

    for channel in guild.text_channels:
        est_salon_log = (channel.name.lower() == "log-deplacements")
        
        if est_categorie_candidate(channel.category) or est_salon_log:
            if channel.name.startswith("🔒arch-"):
                continue

            lines = []
            async for msg in channel.history(after=debut_journee_utc, oldest_first=True):
                if msg.author.bot and not est_salon_log:
                    continue

                texte_msg = msg.content.strip()
                date_paris_msg = msg.created_at.astimezone(tz_paris)
                tag_heure = date_paris_msg.strftime('%H:%M')

                if msg.attachments:
                    for att in msg.attachments:
                        # 1. Fichiers texte
                        if att.filename.endswith(".txt"):
                            try:
                                file_bytes = await att.read()
                                texte_fichier = file_bytes.decode('utf-8')
                                texte_msg += f"\n[Fichier {att.filename}]: {texte_fichier[:1200]}"
                            except Exception as e:
                                print(f"Impossible de lire le fichier {att.filename} : {e}")

                        # 2. Notes vocales Discord / Fichiers audio
                        elif any(att.filename.lower().endswith(ext) for ext in [".ogg", ".mp3", ".wav", ".m4a"]):
                            try:
                                audio_bytes = await att.read()
                                mime = att.content_type or "audio/ogg"
                                tag_audio = f"[{tag_heure}] Note vocale de {msg.author.display_name} dans #{channel.name} :"
                                
                                pieces_audio_gemini.append(tag_audio)
                                pieces_audio_gemini.append(
                                    genai.types.Part.from_bytes(data=audio_bytes, mime_type=mime)
                                )
                                texte_msg += f" 🎙️ [Note vocale envoyée par {msg.author.display_name}]"
                            except Exception as e:
                                print(f"Impossible de charger la note vocale {att.filename} : {e}")

                if texte_msg.strip():
                    lines.append(f"[{tag_heure}] {msg.author.display_name}: {texte_msg.strip()}")

            if lines:
                cat_nom = channel.category.name if channel.category else "Sans Catégorie"
                salons_transcripts.append(
                    f"=== [{cat_nom.upper()}] #{channel.name} ({len(lines)} messages) ===\n" + "\n".join(lines)
                )

    if not salons_transcripts and not pieces_audio_gemini:
        await target_channel.send("😴 **Journal du jour :** Aucun échange sur le serveur aujourd'hui.")
        return

    full_context = "\n\n".join(salons_transcripts)
    date_str = maintenant_paris.strftime("%d/%m/%Y")

    prompt = (
        "Tu es l'analyste stratégique et showrunner d'un jeu de stratégie, d'alliances et d'éliminations joué SUR DISCORD.\n"
        f"JOURNÉE DU {date_str} (Heure de Paris).\n\n"
        "=== CONTEXTE RÉCENT (DERNIERS JOURS) ===\n"
        f"{texte_contexte_passe}\n\n"
        "=== DISCUSSIONS DE LA JOURNÉE SUR LES SALONS DISCORD ===\n"
        f"{full_context}\n\n"
        "NOTE IMPORTANTE SUR LES AUDIO : Plusieurs notes vocales réelles des candidats sont jointes en pièces jointes à cette requête. "
        "Écoute-les attentivement et intègre leurs propos, complots, aveux et hésitations dans le journal exactement comme les messages écrits.\n\n"
        "Rédige le **Journal de Bord Stratégique Global de la Journée** pour l'équipe d'organisation.\n"
        "Consignes de cadrage :\n"
        "1. CONTEXTE RÉEL : C'est un jeu sur serveur Discord. Parle de salons textuels, vocaux, discussions de camp, confessionnaux, logs et pactes. Zéro cliché d'île déserte, de sable ou de jungle.\n"
        "2. Sois précis sur les dynamiques entre joueurs, les trahisons, les hésitations et les cibles de vote.\n"
        "3. Structure avec ces sections obligatoires et des émojis :\n\n"
        "   - 🌍 **Ambiance Générale & Dynamique du Serveur**\n"
        "   - 🤝 **Pactes, Alliances & Négociations**\n"
        "   - 🎯 **Cibles Évoquées, Plans & Votes**\n"
        "   - ⚠️ **Double-Jeu, Secrets & Fuites d'Infos**\n"
        "   - 🎙️ **Points Clés des Confessionnaux & Salons Privés** (inclus l'analyse des notes vocales)\n"
        "   - 🗺️ **Mouvements & Logs Notables**\n"
        "   - 📌 **Synthèse par Salon Actif**\n"
        "   - 🕸️ **Cartographie des Liens** (Qui joue avec qui, les pivots, les joueurs isolés)\n"
        "   - 🏆 **Power Ranking Stratégique du Jour** :\n"
        "       🟢 *En position de force* (Bien entourés, maîtres du jeu, sous les radars)\n"
        "       🟡 *En équilibre* (Charnières, indécis, observateurs)\n"
        "       🔴 *En danger immédiat* (Cibles désignées, isolés, alliances percées)\n"
        "   - 🤡 **Le Bêtisier du Serveur (Moments Drôles & Perles)** : 3 à 5 punchlines, quiproquos, fails ou messages comiques sortis aujourd'hui.\n\n"
        "4. Reste analytique, percutant et direct."
    )

    payload_gemini = [prompt] + pieces_audio_gemini

    max_tentatives = 3
    for tentative in range(max_tentatives):
        try:
            response = await asyncio.to_thread(
                gemini_client.models.generate_content,
                model=MODEL_NAME,
                contents=payload_gemini
            )
            recap_text = response.text

            header = f"📰 **JOURNAL STRATÉGIQUE GLOBAL DU {date_str}**\n*(Réservé aux Orgas, Spectateurs et Admins • Écrit & Vocaux analysés)*\n\n"
            full_message = header + recap_text

            for chunk in decouper_texte_intelligent(full_message, 1900):
                await target_channel.send(chunk)
                await asyncio.sleep(0.4)
            
            print(f"✅ Journal posté dans #{target_channel.name} ({target_channel.id})")
            
            await asyncio.sleep(4)
            await poster_questions_automatiques(recap_text)
            return

        except Exception as e:
            if "503" in str(e) and tentative < max_tentatives - 1:
                await asyncio.sleep(4)
            else:
                await target_channel.send(f"❌ Erreur lors de la génération du journal : {e}")
                return

# =======================================================
# 2. COMMANDE MANUELLE /questions_confessionnal
# =======================================================

async def generer_questions_confessionnal(target_recap_channel: discord.TextChannel, candidat_nom: str = None) -> str:
    """Génère des questions d'interview courtes et adaptées à Discord."""
    recap_messages = [
        msg.content async for msg in target_recap_channel.history(limit=4, oldest_first=False)
        if not msg.content.startswith("😴") and "JOURNAL STRATÉGIQUE" in msg.content
    ]

    if not recap_messages:
        return "⚠️ Aucun journal stratégique récent trouvé dans le salon dédié."

    dernier_recap = "\n---\n".join(reversed(recap_messages))

    consigne_cible = (
        f"Concentre-toi UNIQUEMENT sur le joueur **{candidat_nom}**." 
        if candidat_nom else 
        "Choisis 3 ou 4 joueurs clés ayant des décisions stratégiques ou des votes cruciaux à gérer."
    )

    prompt = (
        "Tu es l'interviewer officiel d'un jeu de stratégie sur Discord.\n"
        "Ton rôle est d'aider les orgas à préparer des interviews courtes et percutantes au confessionnal.\n\n"
        "RÈGLES D'INTERVIEW DISCORD :\n"
        "- Contexte 100% Discord : discussions de salons, duos, alliances, votes (zéro mention de survie/île/flambeaux).\n"
        "- Questions courtes, directes et incisives (1 à 2 phrases max).\n"
        "- Neutralité absolue : pas de jugement ni de spoil des actions des autres.\n\n"
        f"Derniers événements :\n{dernier_recap}\n\n"
        f"Consigne : {consigne_cible}\n\n"
        "Structure pour chaque candidat :\n"
        "👤 **Joueur : [Nom]**\n"
        "🎯 **Position actuelle :** [1 phrase courte résumant sa situation]\n"
        "❓ **Questions (2 à 3 max) :**\n"
        "  - [Question courte sur sa confiance et ses alliances]\n"
        "  - [Question courte sur son dilemme ou son vote à venir]\n"
        "💡 **Objectif orga :** [Ce qu'on cherche à lui faire exprimer]"
    )

    max_tentatives = 3
    for tentative in range(max_tentatives):
        try:
            response = await asyncio.to_thread(
                gemini_client.models.generate_content,
                model=MODEL_NAME,
                contents=prompt
            )
            return response.text
        except Exception as e:
            if "503" in str(e) and tentative < max_tentatives - 1:
                await asyncio.sleep(2)
            else:
                return f"❌ Erreur IA lors de la génération des questions : {e}"


# =======================================================
# 2. RÉSUMÉ SPECTATEURS (AVEC BÊTISIER DE LA TRIBUNE)
# =======================================================

async def traiter_resume_spectateurs(guild: discord.Guild):
    """Extrait le chat spectateurs et génère le résumé structuré avec son bêtisier."""
    chat_spec = guild.get_channel(SALON_CHAT_SPECTATEURS_ID)
    dest_spec = guild.get_channel(SALON_RECAP_SPECTATEURS_ID)

    if not chat_spec or not dest_spec:
        return False, f"Salon chat (`{SALON_CHAT_SPECTATEURS_ID}`) ou récap (`{SALON_RECAP_SPECTATEURS_ID}`) introuvable."

    paris_tz = ZoneInfo("Europe/Paris")
    maintenant_paris = datetime.datetime.now(paris_tz)
    debut_journee_paris = maintenant_paris.replace(hour=0, minute=0, second=0, microsecond=0)
    debut_journee_utc = debut_journee_paris.astimezone(datetime.timezone.utc)

    messages = []
    async for msg in chat_spec.history(limit=2500, after=debut_journee_utc, oldest_first=True):
        if not msg.author.bot and msg.content.strip():
            date_m = msg.created_at.astimezone(paris_tz)
            messages.append(f"[{date_m.strftime('%H:%M')}] [{msg.author.display_name}] : {msg.content.strip()}")

    if not messages:
        return False, "Aucun message envoyé par les spectateurs aujourd'hui."

    texte_brut = "\n".join(messages)

    prompt = (
        "Tu es l'observateur officiel d'un jeu de stratégie. Voici l'intégralité des discussions "
        f"du salon des spectateurs aujourd'hui ({maintenant_paris.strftime('%d/%m/%Y')}) :\n\n"
        f"{texte_brut[:25000]}\n\n"
        "Rédige un récapitulatif structuré STRICTEMENT en 3 parties distinctes :\n\n"
        "## 🍿 1. DISCUSSIONS HORS-SUJET & BRUITS DE COULOIR\n"
        "- Résume les débats inutiles, délires, blagues, mèmes et hors-sujets abordés.\n\n"
        "## 🧠 2. ANALYSES STRATÉGIQUES & AVIS PERTINENTS\n"
        "- Résume leurs théories lucides, observations sur les erreurs/masterclass des candidats et pronostics de votes.\n\n"
        "## 🤡 3. LE BÊTISIER DE LA TRIBUNE (PERLES & PUNCHLINES DU CHAT)\n"
        "- Relève 3 à 5 messages hilarants, réactions à chaud absurdes, clashs bon enfant ou punchlines mémorables postées par les spectateurs.\n\n"
        "Garde un ton synthétique, fluide et structuré."
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt
        )
        rapport = response.text.strip()
    except Exception as e:
        return False, f"Erreur IA : {e}"

    date_str = maintenant_paris.strftime("%d/%m/%Y")
    await dest_spec.send(f"📊 **RÉSUMÉ DU CHAT SPECTATEURS — {date_str}**")
    for bloc in decouper_texte_intelligent(rapport, limite=1900):
        await dest_spec.send(bloc)
        await asyncio.sleep(0.3)

    return True, f"Résumé spectateurs envoyé dans {dest_spec.mention} !"


# =======================================================
# 3. INTERVIEWS DU CONFESSIONNAL (JOURNALISTE OBJECTIF)
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
            response = await asyncio.to_thread(
                gemini_client.models.generate_content,
                model=MODEL_NAME,
                contents=prompt
            )
            return response.text
        except Exception as e:
            if "503" in str(e) and tentative < max_tentatives - 1:
                await asyncio.sleep(2)
            else:
                return f"❌ Erreur IA lors de la génération des questions : {e}"


# ========================================================
# 3.BIS. ÉPREUVE DE RECHERCHE : MOTEURS D'ANALYSE ET IA
# ========================================================

async def suggerer_reponse_recherche_ia(joueur_mystere: str, question: str) -> dict:
    """Analyse minutieusement la question posée et renvoie la réponse exacte (OUI/NON) avec sources."""
    prompt = (
        "Tu es l'arbitre factuel absolu d'un jeu de déduction sur le football et le sport.\n"
        f"🎯 JOUEUR CIBLE MYSTÈRE : **{joueur_mystere}**\n"
        f"❓ QUESTION DU CANDIDAT : \"{question}\"\n\n"
        "DIRECTIVES DE VÉRIFICATION FACTUELLE STRICTE :\n"
        f"1. Vérifie méticuleusement la réalité factuelle concernant UNIQUEMENT {joueur_mystere} (nationalité, clubs, années, postes, trophées, stats, sélections, vie publique).\n"
        "2. Détermine si la réponse factuelle à cette question fermée est STRICTEMENT OUI ou STRICTEMENT NON.\n"
        "3. Si la question n'est pas fermée ou est inapplicable/ambiguë, indique-le clairement.\n"
        "4. Fournis une justification courte et irréfutable (dates précises, noms de clubs, trophées, palmarès).\n\n"
        "FORMAT DE RÉPONSE STRICT (Respecte exactement ces balises) :\n"
        "VERDICT: <OUI / NON / AMBIGU / NON-CONCERNE>\n"
        "EXPLICATION: <1 à 2 phrases courtes résumant le fait vérifié>\n"
        "SOURCES: <Détails précis : clubs, années, matchs ou palmarès officiel>"
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt
        )
        texte = response.text.strip()

        verdict_match = re.search(r"VERDICT\s*:\s*\**([A-Za-z\-]+)\**", texte, re.IGNORECASE)
        verdict = verdict_match.group(1).upper() if verdict_match else "INDÉTERMINÉ"

        explication = ""
        if "EXPLICATION:" in texte:
            partie = texte.split("EXPLICATION:", 1)[1]
            explication = partie.split("SOURCES:", 1)[0].strip()

        sources = ""
        if "SOURCES:" in texte:
            sources = texte.split("SOURCES:", 1)[1].strip()

        return {
            "verdict": verdict,
            "explication": explication or texte,
            "sources": sources or "Base de données sportive"
        }
    except Exception as e:
        return {
            "verdict": "ERREUR",
            "explication": f"Erreur IA : {e}",
            "sources": "N/A"
        }


async def verifier_echanges_recherche_ia(joueur_mystere: str, transcript: str) -> str:
    """Analyse minutieusement les réponses données par les orgas sur le joueur mystère."""
    prompt = (
        "Tu es un juge et arbitre expert, STRICT, MINUTIEUX et IMPLACABLE pour un jeu d'enquête sportive/culturelle.\n"
        f"🎯 JOUEUR / PERSONNE MYSTÈRE CIBLE : **{joueur_mystere}**\n\n"
        "Voici la transcription complète des échanges dans le salon de jeu (Questions des candidats et Réponses OUI/NON des organisateurs) :\n"
        f"\"\"\"\n{transcript}\n\"\"\"\n\n"
        "MISSIONS CRITIQUES DE VÉRIFICATION :\n"
        "1. Isole chaque question fermée posée et la réponse apportée par l'organisation (Oui, Non, ou variantes).\n"
        f"2. Pour CHAQUE question, vérifie la réalité FACTUELLE STRICTE concernant exclusivement **{joueur_mystere}** "
        "(palmarès, clubs, nationalité, statistiques, biographie, dates, postes, etc.).\n"
        "3. Vérifie si la réponse donnée par l'orga est STRICTEMENT VRAIE ou FAUSSE / TROMPEUSE.\n"
        "4. En cas d'erreur ou d'ambiguïté, fournis la correction exacte avec les FAITS et SOURCES précises (historique des clubs, palmarès officiel, etc.).\n\n"
        "FORMAT DE RÉPONSE OBLIGATOIRE (STRICT) :\n\n"
        "Si AUCUNE erreur n'a été commise par les orgas :\n"
        "✅ **AUDIT VALIDE : AUCUNE ERREUR DÉTECTÉE**\n"
        "- Toutes les réponses données correspondent parfaitement aux faits concernant ce joueur.\n"
        "[Ajoute un récapitulatif ultra-rapide des questions/réponses validées sous forme de puces]\n\n"
        "Si AU MOINS UNE erreur ou réponse inexacte a été donnée :\n"
        "⚠️ **ALERTE ERREUR(S) DÉTECTÉE(S) DANS LES RÉPONSES !**\n\n"
        "Pour chaque erreur relevée :\n"
        "❌ **Question litigieuse :** [Texte de la question]\n"
        "🔴 **Réponse donnée par l'orga :** [Oui / Non]\n"
        "🟢 **Ce qu'il fallait répondre :** [Oui / Non]\n"
        "📚 **Explication & Faits vérifiés (Sources) :** [Détails précis, clubs, années, matchs ou trophées attestant l'erreur]\n\n"
        "Puis termine par un court récapitulatif des questions qui étaient bien correctes."
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        return f"❌ Erreur lors de l'audit IA avec Gemini : {e}"


async def traiter_suggestion_orga(channel_src, salon_dest, candidat, joueur_mystere: str, question: str):
    """Génère la réponse suggérée et l'envoie dans le salon retour orgas."""
    resultat = await suggerer_reponse_recherche_ia(joueur_mystere, question)

    verdict = resultat["verdict"]
    couleur = discord.Color.green() if verdict == "OUI" else (discord.Color.red() if verdict == "NON" else discord.Color.orange())
    emoji_v = "🟢 **OUI**" if verdict == "OUI" else ("🔴 **NON**" if verdict == "NON" else f"🟡 **{verdict}**")

    embed = discord.Embed(
        title=f"💡 SUGGESTION RÉPONSE — {joueur_mystere.upper()}",
        color=couleur
    )
    embed.add_field(name="👤 Candidat", value=f"{candidat.mention} dans {channel_src.mention}", inline=False)
    embed.add_field(name="❓ Question posée", value=f"*{question}*", inline=False)
    embed.add_field(name="👉 Réponse recommandée", value=emoji_v, inline=False)
    embed.add_field(name="📚 Justification & Sources", value=f"{resultat['explication']}\n\n**Détails :** `{resultat['sources']}`", inline=False)
    embed.set_footer(text="Aide aux Orgas • Vérification factuelle instantanée")

    await salon_dest.send(embed=embed)


# ==========================================
# 4. HORLOGE AUTOMATIQUE (PARIS 23H30 & 09H00)
# ==========================================

@tasks.loop(minutes=1)
async def horloge_serveur():
    """Horloge robuste calée sur l'heure de Paris."""
    global DERNIER_JOUR_RECAP, DERNIER_JOUR_QUESTIONS

    paris_tz = ZoneInfo("Europe/Paris")
    maintenant = datetime.datetime.now(paris_tz)
    jour_actuel = maintenant.strftime("%Y-%m-%d")

    # 1. Déclenchement automatique du Récapitulatif à 23h30 Paris
    if maintenant.hour == 23 and maintenant.minute == 30 and DERNIER_JOUR_RECAP != jour_actuel:
        DERNIER_JOUR_RECAP = jour_actuel
        print(f"⏰ [23:30 Paris] Lancement automatique des résumés du {jour_actuel}...")
        for guild in bot.guilds:
            try:
                target_channel = bot.get_channel(RECAP_CHANNEL_ID)
                if target_channel:
                    await generer_et_envoyer_recap_quotidien(guild, target_channel)
                else:
                    print(f"❌ [ERREUR] Salon RECAP_CHANNEL_ID ({RECAP_CHANNEL_ID}) introuvable !")
                
                await asyncio.sleep(5)
                await traiter_resume_spectateurs(guild)
            except Exception as e:
                print(f"❌ Erreur lors de la tâche automatique de 23h30 : {e}")
                
@bot.event
async def on_ready():
    await bot.tree.sync()
    if not horloge_serveur.is_running():
        horloge_serveur.start()
    print(f"🤖 Bot connecté en tant que : {bot.user}")
    print(f"⏰ Horloge active : Récapitulatif à 23h30 Paris | Questions à 09h00 Paris.")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Anti-triche vocal."""
    if member.bot:
        return

    if before.channel and not after.channel:
        print(f"⚠️ ALERTE TRICHE : {member.display_name} a QUITTÉ le salon vocal {before.channel.name} !")

    if not before.self_deaf and after.self_deaf:
        print(f"⚠️ ALERTE : {member.display_name} a COUPÉ SON CASQUE (Deafen).")

    if not before.self_mute and after.self_mute:
        print(f"⚠️ INFO : {member.display_name} s'est MUTÉ.")


@bot.event
async def on_message(message: discord.Message):
    """Écouteur de messages pour la recherche en direct, la draft et les commandes préfixes."""
    if message.author.bot:
        return
    # 1. Décodeur / Traducteur humoristique en direct
    # Interception du brouilleur de salon
    if message.channel.id in SALONS_BROUILLEUR_ACTIFS and message.content.strip():
        # Ignorer les commandes commençant par '!' ou '/'
        if not message.content.startswith(("!", "/")):
            texte_source = message.content.strip()
            auteur = message.author
            channel = message.channel

            try:
                # 1. Suppression immédiate du message original
                await message.delete()

                # 2. Génération du charabia
                texte_brouille = await transformer_en_charabia_ia(texte_source)

                # 3. Récupération ou création d'un webhook pour usurper l'avatar/pseudo
                webhooks = await channel.webhooks()
                webhook = next((w for w in webhooks if w.user.id == bot.user.id), None)
                if not webhook:
                    webhook = await channel.create_webhook(name="Brouilleur")

                # 4. Envoi du message sous l'identité de l'auteur
                await webhook.send(
                    content=texte_brouille,
                    username=auteur.display_name,
                    avatar_url=auteur.display_avatar.url
                )
                return
            except Exception as e:
                print(f"Erreur lors du brouillage du message : {e}")
                
    if message.channel.id in SESSIONS_DECODEUR:
        cible_data = SESSIONS_DECODEUR[message.channel.id]
        if message.author.id == cible_data["user_id"]:
            texte = message.content.strip()
            # On ignore les messages vides ou trop courts
            if len(texte) >= 2:
                async with message.channel.typing():
                    traduction = await decoder_charabia_ia(message.author.display_name, texte)
                    embed_decodeur = discord.Embed(
                        description=f"🌐 **DÉCODEUR OFFICIEL — Langue parlée : `{message.author.display_name}`**\n\n{traduction}",
                        color=discord.Color.teal()
                    )
                    embed_decodeur.set_footer(text="Service de traduction automatique en temps réel")
                    await message.reply(embed=embed_decodeur, mention_author=False)
                    
    # 1. Détection des questions posées pendant l'épreuve de recherche
    if message.channel.id in SESSIONS_RECHERCHE_ACTIVES:
        session = SESSIONS_RECHERCHE_ACTIVES[message.channel.id]
        if message.author.id == session["candidat_id"]:
            texte_q = message.content.strip()
            if len(texte_q) >= 4:
                salon_orgas = bot.get_channel(SALON_REMARQUES_QUESTIONS_ID)
                if salon_orgas:
                    asyncio.create_task(
                        traiter_suggestion_orga(
                            channel_src=message.channel,
                            salon_dest=salon_orgas,
                            candidat=message.author,
                            joueur_mystere=session["joueur"],
                            question=texte_q
                        )
                    )

    # 2. Gestion de la draft interactive des équipes
    if ETAT_COMPOSITION["actif"] and message.channel.id == ETAT_COMPOSITION["channel_id"]:
        cap1 = ETAT_COMPOSITION["capitaine_1"]
        cap2 = ETAT_COMPOSITION["capitaine_2"]
        tour = ETAT_COMPOSITION["tour"]
        
        cap_actif = cap1 if tour == 1 else cap2
        role_actif = ETAT_COMPOSITION["role_1"] if tour == 1 else ETAT_COMPOSITION["role_2"]
        cap_suivant = cap2 if tour == 1 else cap1

        if message.author.id == cap_actif.id:
            if message.mentions:
                cible = message.mentions[0]

                role_1 = ETAT_COMPOSITION["role_1"]
                role_2 = ETAT_COMPOSITION["role_2"]

                if cible.bot:
                    await message.channel.send(f"❌ {cap_actif.mention}, tu ne peux pas recruter un bot !")
                    return

                if role_1 in cible.roles or role_2 in cible.roles:
                    await message.channel.send(f"⚠️ {cap_actif.mention}, **{cible.display_name}** a déjà été choisi dans une équipe !")
                    return

                try:
                    await cible.add_roles(role_actif, reason=f"Choisi par le capitaine {cap_actif.display_name}")
                    
                    ETAT_COMPOSITION["tour"] = 2 if tour == 1 else 1

                    embed_choix = discord.Embed(
                        title="🤝 NOUVELLE RECRUE !",
                        description=(
                            f"🔴 **{cible.mention}** rejoint l'équipe {role_actif.mention} !\n\n"
                            f"👉 **Au tour de {cap_suivant.mention}** de faire son choix (mentionne un candidat) !"
                        ),
                        color=role_actif.color if role_actif.color.value != 0 else discord.Color.gold()
                    )
                    await message.channel.send(embed=embed_choix)

                except discord.Forbidden:
                    await message.channel.send(f"❌ Erreur de permissions : le bot ne peut pas attribuer le rôle {role_actif.mention}.")
                except Exception as e:
                    await message.channel.send(f"❌ Erreur lors de l'attribution du rôle : {e}")

    await bot.process_commands(message)


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
# 5. GESTION DES DUOS & CANDIDATS
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
        if (channel.name.startswith("duo-") or channel.name.startswith("🔗・") or channel.name.startswith("🔺・")) and role_candidat in channel.overwrites:
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
# 6. SALONS SPÉCIFIQUES (TRIOS, QUATUORS & VOCAUX)
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
# 7. SUPPRESSION & NETTOYAGE
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
# 8. PERMISSIONS SPECTATEURS (SÉCURISÉES ANTI-RATE LIMIT)
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
    
    try:
        await target_channel.set_permissions(
            role_spectateurs, 
            overwrite=overwrites, 
            reason=f"Accès spectateur ajouté par {interaction.user.display_name}"
        )
        await interaction.followup.send(
            f"👁️ Accès **Spectateurs** appliqué au salon {target_channel.mention} !",
            ephemeral=True
        )
    except discord.HTTPException as e:
        if e.status == 429:
            await interaction.followup.send(
                "⏳ **Discord est temporairement surchargé (Rate Limit).** Réessaye dans 2 minutes.",
                ephemeral=True
            )
        else:
            await interaction.followup.send(f"❌ Erreur Discord : {e}", ephemeral=True)


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
            await ch.set_permissions(
                role_spectateurs, 
                overwrite=overwrites, 
                reason=f"Accès spectateurs par lot ({interaction.user.display_name})"
            )
            mis_a_jour += 1
            await asyncio.sleep(1.0)
        except discord.HTTPException as e:
            if e.status == 429:
                await asyncio.sleep(5.0)
            else:
                pass

    await interaction.followup.send(
        f"👁️ Accès **Spectateurs** appliqué sur **{mis_a_jour}/{len(channels_list)} salon(s)** de **{category.name}** !",
        ephemeral=True
    )


# ==========================================
# 9. RÉSUMÉS IA & COMMANDES DE GESTION
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

    # 1. On va chercher les messages les plus récents (oldest_first=False)
    messages_bruts = [msg async for msg in channel.history(limit=limite, oldest_first=False)]
    
    # 2. On filtre les bots, messages vides et commandes
    user_messages = [
        msg for msg in messages_bruts 
        if not msg.author.bot 
        and msg.content.strip() 
        and not msg.content.startswith(("/", "!"))
    ]

    if len(user_messages) < 3:
        await interaction.followup.send("⚠️ Pas assez de messages récents pour générer un résumé pertinent.", ephemeral=True)
        return

    # 3. Remise en ordre chronologique pour l'analyse IA
    user_messages.reverse()

    transcript = "\n".join([f"{msg.author.display_name}: {msg.content}" for msg in user_messages])

    if format.value == "court":
        prompt = (
            "Tu es l'arbitre d'un jeu de stratégie. "
            f"Voici la transcription des messages récents du salon #{channel.name} :\n\n"
            f"{transcript}\n\n"
            "Fais un résumé **TRÈS COURT, CONCIS ET DIRECT** en 3 à 5 bullet points maximum :\n"
            "- 🎯 Sujet central en 1 phrase\n"
            "- 🤝 Décisions / Alliances évoquées\n"
            "- ⚠️ Orientations stratégiques ou cibles mentionnées\n"
            "- 🎭 Dynamique des échanges (Accord, Réserves, Négociation)"
        )
    else:
        prompt = (
            "Tu es l'analyste stratégique d'un jeu d'aventure/téléréalité. "
            f"Voici la transcription des messages récents échangés dans le salon #{channel.name} :\n\n"
            f"{transcript}\n\n"
            "Fais un **RÉSUMÉ DÉTAILLÉ ET STRUCTURÉ** en français, avec les sections suivantes :\n"
            "1. 🎯 **Analyse Thématique** (synthèse factuelle des sujets abordés)\n"
            "2. 🤝 **Accords & Propositions** (points de convergence ou de divergence)\n"
            "3. ⚠️ **Scénarios & Votes évoqués** (noms mentionnés, alternatives)\n"
            "4. 🎭 **Dynamique relationnelle** (postures observées)\n"
            "5. 💬 **Citations ou Moments Clés**"
        )

    max_tentatives = 3
    for tentative in range(max_tentatives):
        try:
            response = await asyncio.to_thread(
                gemini_client.models.generate_content,
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
            embed.set_footer(text=f"Analyse basée sur les {len(user_messages)} derniers messages.")

            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        except Exception as e:
            if "503" in str(e) and tentative < max_tentatives - 1:
                await asyncio.sleep(2)
            else:
                await interaction.followup.send(f"❌ Erreur IA après tentatives : {e}", ephemeral=True)
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

    # 1. On récupère les VRAIS derniers messages (oldest_first=False)
    messages_bruts = [msg async for msg in channel.history(limit=limite, oldest_first=False)]

    # 2. Filtrer les bots, messages vides et commandes
    user_messages = [
        msg for msg in messages_bruts 
        if not msg.author.bot 
        and msg.content.strip() 
        and not msg.content.startswith(("/", "!"))
    ]

    if len(user_messages) < 3:
        await interaction.followup.send("⚠️ Pas assez de messages récents pour générer un compte-rendu pertinent.", ephemeral=True)
        return

    # 3. Remettre dans l'ordre chronologique pour que l'IA comprenne le fil de la discussion
    user_messages.reverse()

    transcript = "\n".join([f"{msg.author.display_name}: {msg.content}" for msg in user_messages])

    if format.value == "court":
        prompt = (
            "Tu es l'assistant de direction d'une équipe d'organisation d'un jeu.\n"
            f"Voici les {len(user_messages)} derniers messages échangés dans le salon #{channel.name} :\n\n"
            f"{transcript}\n\n"
            "Fais un résumé TRÈS COURT, CONCIS ET DIRECT en 3 à 5 bullet points maximum :\n"
            "- 🎯 Objectif/Sujet principal de la discussion\n"
            "- 🛠️ Décisions importantes actées\n"
            "- 📋 Actions à faire (Qui fait quoi ?)\n"
            "- 📅 Prochaines étapes"
        )
    else:
        prompt = (
            "Tu es l'assistant de direction d'une équipe d'organisation d'un jeu.\n"
            f"Voici les {len(user_messages)} derniers messages échangés dans le salon #{channel.name} :\n\n"
            f"{transcript}\n\n"
            "Rédige un COMPTE-RENDU DÉTAILLÉ ET PROFESSIONNEL en français avec ces sections :\n"
            "1. 🎯 **Sujets abordés**\n"
            "2. 🛠️ **Décisions prises** (validé / refusé)\n"
            "3. 📋 **Répartition des tâches** (qui fait quoi)\n"
            "4. 💡 **Idées & Propositions en attente**\n"
            "5. 📅 **Prochaines étapes & Deadlines**"
        )

    max_tentatives = 3
    for tentative in range(max_tentatives):
        try:
            response = await asyncio.to_thread(
                gemini_client.models.generate_content,
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
            embed.set_footer(text=f"Analyse basée sur les {len(user_messages)} derniers messages.")

            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        except Exception as e:
            if "503" in str(e) and tentative < max_tentatives - 1:
                await asyncio.sleep(2)
            else:
                await interaction.followup.send(f"❌ Erreur IA : {e}", ephemeral=True)
                return

@bot.tree.command(
    name="questions_confessionnal",
    description="Génère des questions journalistiques objectives pour les confessionnaux (sur-mesure ou global)."
)
@app_commands.describe(candidat="Optionnel : mentionnez le rôle d'un candidat précis (laisser vide pour les profils clés du jour)")
@app_commands.check(est_orga_ou_admin)
async def questions_confessionnal(interaction: discord.Interaction, candidat: discord.Role = None):
    await interaction.response.defer(ephemeral=True)

    source_recap_channel = bot.get_channel(RECAP_CHANNEL_ID) or interaction.channel
    candidat_nom = candidat.name if candidat else None

    resultat_text = await generer_questions_confessionnal(source_recap_channel, candidat_nom)

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
    description="Génère immédiatement le journal stratégique global dans le salon dédié."
)
@app_commands.check(est_orga_ou_admin)
async def forcer_recap_jour(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    target_channel = bot.get_channel(RECAP_CHANNEL_ID)
    if not target_channel:
        await interaction.followup.send(f"❌ Salon journal introuvable (ID: `{RECAP_CHANNEL_ID}`).", ephemeral=True)
        return

    await interaction.followup.send(f"⏳ Analyse des salons candidats en cours pour {target_channel.mention}...", ephemeral=True)
    await generer_et_envoyer_recap_quotidien(interaction.guild, target_channel)


@bot.tree.command(
    name="forcer_recap_spec",
    description="Force immédiatement la génération et l'envoi du récapitulatif du chat spectateurs."
)
@app_commands.check(est_orga_ou_admin)
async def forcer_recap_spec(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    succes, msg = await traiter_resume_spectateurs(interaction.guild)
    if succes:
        await interaction.followup.send(f"✅ {msg}", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ {msg}", ephemeral=True)


@bot.tree.command(
    name="resume_spectateurs",
    description="Génère à la demande le récapitulatif du salon spectateurs (Hors-sujet vs Analyses vs Bêtisier)."
)
@app_commands.check(est_orga_ou_admin)
async def resume_spectateurs_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    succes, msg = await traiter_resume_spectateurs(interaction.guild)
    if succes:
        await interaction.followup.send(f"✅ {msg}", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ {msg}", ephemeral=True)


@bot.tree.command(
    name="pause_taches",
    description="Met en pause l'envoi automatique du récap du soir et des questions du matin."
)
@app_commands.check(est_orga_ou_admin)
async def pause_taches(interaction: discord.Interaction):
    if horloge_serveur.is_running():
        horloge_serveur.stop()

    await interaction.response.send_message(
        "⏸️ **Tâches automatiques mises en pause :**\n- 🌙 Récap du soir (23h30 Paris) : **Arrêté**\n- 🎙️ Questions du matin (09h00 Paris) : **Arrêté**",
        ephemeral=True
    )


@bot.tree.command(
    name="reprendre_taches",
    description="Réactive l'envoi automatique du récap du soir et des questions du matin."
)
@app_commands.check(est_orga_ou_admin)
async def reprendre_taches(interaction: discord.Interaction):
    if not horloge_serveur.is_running():
        horloge_serveur.start()

    await interaction.response.send_message(
        "▶️ **Tâches automatiques réactivées :**\n- 🌙 Récap du soir (23h30 Paris) : **Actif**\n- 🎙️ Questions du matin (09h00 Paris) : **Actif**",
        ephemeral=True
    )


# ========================================================
# 9.BIS. COMMANDES ÉPREUVE DE RECHERCHE & AUDIT
# ========================================================

@bot.tree.command(
    name="lancer_aide_recherche",
    description="Active l'assistance IA en direct pour suggérer les réponses (OUI/NON) aux orgas."
)
@app_commands.describe(
    joueur_mystere="Nom exact du joueur à deviner (ex: Karim Benzema, Zinedine Zidane...)",
    candidat="Le candidat qui passe l'épreuve dans ce salon",
    salon="Optionnel : salon où se déroule l'épreuve (par défaut : salon actuel)"
)
@app_commands.check(est_orga_ou_admin)
async def lancer_aide_recherche(
    interaction: discord.Interaction,
    joueur_mystere: str,
    candidat: discord.Member,
    salon: discord.TextChannel = None
):
    await interaction.response.defer(ephemeral=True)
    ch = salon or interaction.channel

    SESSIONS_RECHERCHE_ACTIVES[ch.id] = {
        "joueur": joueur_mystere.strip(),
        "candidat_id": candidat.id
    }

    salon_orgas = bot.get_channel(SALON_REMARQUES_QUESTIONS_ID)
    mention_orga = salon_orgas.mention if salon_orgas else f"ID `{SALON_REMARQUES_QUESTIONS_ID}`"

    await interaction.followup.send(
        f"🟢 **Aide de recherche activée en direct !**\n"
        f"- 🎯 **Joueur cible :** `{joueur_mystere}`\n"
        f"- 👤 **Candidat suivi :** {candidat.mention}\n"
        f"- 💬 **Salon surveillé :** {ch.mention}\n"
        f"- 📬 **Suggestions envoyées dans :** {mention_orga}\n\n"
        f"*(Tape `/arreter_aide_recherche` une fois l'épreuve terminée)*",
        ephemeral=True
    )


@bot.tree.command(
    name="arreter_aide_recherche",
    description="Désactive l'assistance IA en direct pour ce salon."
)
@app_commands.describe(salon="Optionnel : salon de l'épreuve (par défaut : salon actuel)")
@app_commands.check(est_orga_ou_admin)
async def arreter_aide_recherche(interaction: discord.Interaction, salon: discord.TextChannel = None):
    ch = salon or interaction.channel

    if ch.id in SESSIONS_RECHERCHE_ACTIVES:
        joueur = SESSIONS_RECHERCHE_ACTIVES.pop(ch.id)["joueur"]
        await interaction.response.send_message(
            f"🛑 **Aide de recherche désactivée** pour le salon {ch.mention} (Joueur cible : `{joueur}`).",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"ℹ️ Aucune session d'aide de recherche n'était active dans {ch.mention}.",
            ephemeral=True
        )


@bot.tree.command(
    name="audit_reponses_recherche",
    description="Vérifie minutieusement si les réponses OUI/NON des orgas sur le joueur mystère sont correctes."
)
@app_commands.describe(
    joueur_mystere="Nom exact du joueur à trouver (ex: Antoine Griezmann)",
    limite_messages="Nombre de messages récents à analyser (par défaut : 50)",
    salon_audit="Optionnel : salon de jeu à vérifier (par défaut : salon actuel)"
)
@app_commands.check(est_orga_ou_admin)
async def audit_reponses_recherche(
    interaction: discord.Interaction,
    joueur_mystere: str,
    limite_messages: int = 50,
    salon_audit: discord.TextChannel = None
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    target_channel = salon_audit or interaction.channel

    if not isinstance(target_channel, discord.TextChannel):
        await interaction.followup.send("❌ Le salon analysé doit être un salon textuel.", ephemeral=True)
        return

    messages = [msg async for msg in target_channel.history(limit=limite_messages, oldest_first=True)]
    messages_valides = [m for m in messages if not m.author.bot and m.content.strip()]

    if not messages_valides:
        await interaction.followup.send(f"❌ Aucun message trouvé dans {target_channel.mention}.", ephemeral=True)
        return

    transcript_lignes = []
    for m in messages_valides:
        transcript_lignes.append(f"[{m.author.display_name}]: {m.content.strip()}")

    transcript_texte = "\n".join(transcript_lignes)

    await interaction.followup.send(
        f"⏳ **Audit factuel minutieux en cours...**\n"
        f"🎯 Cible : **{joueur_mystere}**\n"
        f"💬 Salon analysé : {target_channel.mention} ({len(messages_valides)} messages)",
        ephemeral=True
    )

    resultat_audit = await verifier_echanges_recherche_ia(joueur_mystere, transcript_texte)
    salon_orgas = bot.get_channel(SALON_REMARQUES_QUESTIONS_ID) or interaction.channel
    date_str = datetime.datetime.now(ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y à %H:%M")

    embed = discord.Embed(
        title=f"🔎 AUDIT FACTUEL — JOUEUR : {joueur_mystere.upper()}",
        description=resultat_audit if len(resultat_audit) <= 3900 else None,
        color=discord.Color.green() if "✅ **AUDIT VALIDE" in resultat_audit else discord.Color.red()
    )
    embed.set_footer(text=f"Salon audité : #{target_channel.name} • Demandé par {interaction.user.display_name} • {date_str}")

    if len(resultat_audit) > 3900:
        await salon_orgas.send(f"🔎 **RAPPORT D'AUDIT COMPLET — {joueur_mystere} (#{target_channel.name})**")
        for chunk in decouper_texte_intelligent(resultat_audit, limite=1900):
            await salon_orgas.send(chunk)
            await asyncio.sleep(0.3)
    else:
        await salon_orgas.send(embed=embed)

    await interaction.followup.send(
        f"✅ **Audit terminé !** Le rapport a été envoyé dans {salon_orgas.mention}.",
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
        title="⚡ QUESTION FLASH",
        description=(
            f"**{question}**\n\n"
            f"⏳ **Fin du chrono :** <t:{fin_timestamp}:R> *(à <t:{fin_timestamp}:T>)*"
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
            title="⚡ QUESTION FLASH",
            description=(
                f"**{question}**\n\n"
                f"⏱️ **Répondu en `{temps_pris}s`**"
            ),
            color=discord.Color.green()
        )
        await original_msg.edit(embed=embed_fige)

        embed_reponse = discord.Embed(
            title="✅ RÉPONSE VALIDÉE",
            description=(
                f"💬 **Réponse :** `{reponse_msg.content}`\n"
                f"⚡ **Temps :** `{temps_pris}s`"
            ),
            color=discord.Color.green()
        )
        await channel.send(embed=embed_reponse)

    except asyncio.TimeoutError:
        embed_timeout_fige = discord.Embed(
            title="⚡ QUESTION FLASH",
            description=(
                f"**{question}**\n\n"
                f"🛑 **TEMPS ÉCOULÉ**"
            ),
            color=discord.Color.dark_red()
        )
        await original_msg.edit(embed=embed_timeout_fige)

        embed_fin = discord.Embed(
            title="🛑 TEMPS ÉCOULÉ !",
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
# 14. QUIZ & ÉPREUVES AUTOMATISÉES (2 OPTIONS : EMBED OU TEXTE 5S)
# ========================================================

class GlobalQuizLancementView(discord.ui.View):
    def __init__(self, candidat: discord.Member, mode_affichage: str = "visuel"):
        super().__init__(timeout=1800)
        self.candidat = candidat
        self.mode_affichage = mode_affichage

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
            await channel.send("❌ Erreur : Aucune question chargée.")
            return

        ETATS_EPREUVES_SALONS[channel.id] = {
            "pause_demandee": False,
            "event": asyncio.Event()
        }
        ETATS_EPREUVES_SALONS[channel.id]["event"].set()

        msg_decompte = await channel.send("⚠️ **L'épreuve commence dans : 3**")
        for k in range(2, 0, -1):
            await asyncio.sleep(1)
            await msg_decompte.edit(content=f"⚠️ **L'épreuve commence dans : {k}**")
        await asyncio.sleep(1)
        await msg_decompte.delete()

        resultats = []

        for i, q in enumerate(questions, 1):
            if ETATS_EPREUVES_SALONS.get(channel.id, {}).get("pause_demandee"):
                msg_pause = await channel.send("⏸️ **ÉPREUVE EN PAUSE (Attente des Organisateurs)**")
                await ETATS_EPREUVES_SALONS[channel.id]["event"].wait()
                try:
                    await msg_pause.delete()
                except Exception:
                    pass

                msg_reprise = await channel.send("▶️ **Reprise dans 3 secondes...**")
                await asyncio.sleep(3)
                try:
                    await msg_reprise.delete()
                except Exception:
                    pass

            duree = int(q["secondes"])
            texte_q = q["question"]

            def check_reponse(m: discord.Message):
                return m.channel.id == channel.id and m.author.id == self.candidat.id

            debut_question = time.perf_counter()
            reponse_recue = False
            reponse_msg = None
            temps_pris = float(duree)

            # Option 1 : Mode visuel Embed
            if self.mode_affichage == "visuel":
                now = datetime.datetime.now(datetime.timezone.utc)
                fin_ts = int((now + datetime.timedelta(seconds=duree)).timestamp())

                embed_q = discord.Embed(
                    title=f"📋 QUESTION {i} / {len(questions)}",
                    description=(
                        f"# {texte_q}\n\n"
                        f"⏳ **Fin du chrono :** <t:{fin_ts}:R> *(Temps alloué : `{duree}s`)*"
                    ),
                    color=discord.Color.gold()
                )
                msg_principal = await channel.send(embed=embed_q)

                try:
                    reponse_msg = await bot.wait_for("message", timeout=duree, check=check_reponse)
                    temps_pris = round(time.perf_counter() - debut_question, 2)
                    reponse_recue = True

                    embed_rep = discord.Embed(
                        title=f"📋 QUESTION {i} / {len(questions)}",
                        description=f"# {texte_q}\n\n✅ **Réponse validée en `{temps_pris}s` !**",
                        color=discord.Color.green()
                    )
                    await msg_principal.edit(embed=embed_rep)
                except asyncio.TimeoutError:
                    embed_out = discord.Embed(
                        title=f"📋 QUESTION {i} / {len(questions)}",
                        description=f"# {texte_q}\n\n🛑 **TEMPS ÉCOULÉ !**",
                        color=discord.Color.dark_red()
                    )
                    await msg_principal.edit(embed=embed_out)

            # Option 2 : Mode secours texte pur décrémenté toutes les 5s
            else:
                msg_principal = await channel.send(f"# {texte_q}\n\n## ⏱️ Temps restant : **{duree}s**")
                tache_ecoute = asyncio.create_task(bot.wait_for("message", check=check_reponse))

                while not tache_ecoute.done():
                    ecoule = time.perf_counter() - debut_question
                    restant = max(0, int(duree - ecoule))

                    if restant <= 0:
                        break

                    try:
                        reponse_msg = await asyncio.wait_for(asyncio.shield(tache_ecoute), timeout=5.0)
                        reponse_recue = True
                        temps_pris = round(time.perf_counter() - debut_question, 2)
                        break
                    except asyncio.TimeoutError:
                        if not tache_ecoute.done():
                            sec_restantes = max(0, int(duree - (time.perf_counter() - debut_question)))
                            try:
                                await msg_principal.edit(content=f"# {texte_q}\n\n## ⏱️ Temps restant : **{sec_restantes}s**")
                            except Exception:
                                pass

                if not reponse_recue and not tache_ecoute.done():
                    tache_ecoute.cancel()

                if reponse_recue and reponse_msg:
                    try:
                        await msg_principal.edit(content=f"# {texte_q}\n\n## ✅ Répondu en **{temps_pris}s** !")
                    except Exception:
                        pass
                else:
                    try:
                        await msg_principal.edit(content=f"# {texte_q}\n\n## 🛑 TEMPS ÉCOULÉ !")
                    except Exception:
                        pass

            if reponse_recue and reponse_msg:
                resultats.append({
                    "index": i,
                    "question": texte_q,
                    "reponse": reponse_msg.content,
                    "temps": temps_pris,
                    "statut": "✅ Répondu"
                })
            else:
                resultats.append({
                    "index": i,
                    "question": texte_q,
                    "reponse": "*Aucune réponse*",
                    "temps": float(duree),
                    "statut": "❌ Hors délai"
                })

            await asyncio.sleep(2.0)
            try:
                await msg_principal.delete()
                if reponse_msg:
                    await reponse_msg.delete()
            except Exception:
                pass

            if i < len(questions) and not ETATS_EPREUVES_SALONS.get(channel.id, {}).get("pause_demandee"):
                msg_tampon = await channel.send(f"⏳ **Question suivante ({i + 1}/{len(questions)})...**")

                fin_sas = time.perf_counter() + 3.0
                while time.perf_counter() < fin_sas:
                    temps_attente = max(0.1, fin_sas - time.perf_counter())
                    try:
                        parasite = await bot.wait_for("message", timeout=temps_attente, check=check_reponse)
                        try:
                            await parasite.delete()
                        except Exception:
                            pass
                    except asyncio.TimeoutError:
                        break

                try:
                    await msg_tampon.delete()
                except Exception:
                    pass

        ETATS_EPREUVES_SALONS.pop(channel.id, None)

        temps_total_brut = round(sum(r["temps"] for r in resultats), 2)
        minutes = int(temps_total_brut // 60)
        sec_rest = round(temps_total_brut % 60, 2)
        temps_total_texte = f"{minutes} min {sec_rest} s" if minutes > 0 else f"{temps_total_brut} s"

        await channel.send(
            f"🏁 **ÉPREUVE TERMINÉE !**\n"
            f"Tes réponses ont bien été enregistrées.\n"
            f"⏱️ **Temps total cumulé :** `{temps_total_texte}`\n\nMerci !"
        )

        result_channel = bot.get_channel(RESULTATS_CHANNEL_ID)
        if result_channel:
            bonnes_reponses = sum(1 for r in resultats if r["statut"] == "✅ Répondu")

            lignes_recap = [
                f"⏱️ **TEMPS TOTAL CUMULÉ :** `{temps_total_texte}` *({temps_total_brut}s)*",
                f"🎯 **Taux de complétion :** `{bonnes_reponses}/{len(resultats)} dans les temps`\n",
                "━━━━━━━━━━━━━━━━━━━━━━\n"
            ]

            for r in resultats:
                lignes_recap.append(
                    f"**Q{r['index']}. {r['question']}**\n"
                    f"💬 `{r['reponse']}` ({r['statut']} en `{r['temps']}s`)\n"
                )

            recap_str = "\n".join(lignes_recap)

            embed_recap_orga = discord.Embed(
                title=f"📊 RÉSULTATS ÉPREUVE — {self.candidat.display_name}",
                description=recap_str if len(recap_str) <= 3900 else None,
                color=discord.Color.gold()
            )
            embed_recap_orga.set_footer(text=f"Temps total : {temps_total_texte} • ID : {self.candidat.id} • #{channel.name}")

            if len(recap_str) > 3900:
                await result_channel.send(f"📊 **RÉSULTATS DE L'ÉPREUVE — {self.candidat.mention}**")
                for chunk in [recap_str[j:j+1900] for j in range(0, len(recap_str), 1900)]:
                    await result_channel.send(chunk)
            else:
                await result_channel.send(embed=embed_recap_orga)


# ========================================================
# COMMANDES ORGAS ÉPREUVE
# ========================================================

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
    salon_cible="Optionnel : le salon où déployer (par défaut : salon actuel)",
    nom_categorie="Optionnel : nom de la catégorie pour déployer dans tous les confessionnaux en masse",
    mode_affichage="Mode visuel (Embed) ou Secours (Texte pur actualisé toutes les 5s)"
)
@app_commands.choices(mode_affichage=[
    app_commands.Choice(name="🎨 Visuel (Embed & Chrono dynamique standard)", value="visuel"),
    app_commands.Choice(name="⚡ Secours (Texte pur avec baisse de 5s en 5s)", value="secours")
])
@app_commands.check(est_orga_ou_admin)
async def lancer_epreuve(
    interaction: discord.Interaction,
    candidat: discord.Member = None,
    salon_cible: discord.TextChannel = None,
    nom_categorie: str = None,
    mode_affichage: app_commands.Choice[str] = None
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    mode_choisi = mode_affichage.value if mode_affichage else "visuel"

    if not CONFIG_EPREUVE_GLOBALE["active"] or not CONFIG_EPREUVE_GLOBALE["questions"]:
        await interaction.followup.send("❌ Aucune épreuve n'est configurée. Lance d'abord `/configurer_epreuve`.", ephemeral=True)
        return

    if candidat:
        target_ch = salon_cible or interaction.channel
        if not isinstance(target_ch, discord.TextChannel):
            await interaction.followup.send("❌ Le salon cible doit être un salon textuel.", ephemeral=True)
            return

        view = GlobalQuizLancementView(candidat=candidat, mode_affichage=mode_choisi)
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
            f"🚀 **Épreuve déployée pour {candidat.mention}** dans {target_ch.mention} (Mode : `{mode_choisi}`) !",
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

            view = GlobalQuizLancementView(candidat=candidat_trouve, mode_affichage=mode_choisi)
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
            f"🚀 **Épreuve déployée sur {deplois} salon(s)** de la catégorie **{category.name}** (Mode : `{mode_choisi}`) !",
            ephemeral=True
        )
        return

    await interaction.followup.send("❌ Veuillez renseigner un `candidat` ou un `nom_categorie`.", ephemeral=True)


@bot.tree.command(
    name="pause_epreuve",
    description="Met en pause l'épreuve : laisse finir la question en cours, puis bloque avant la suivante."
)
@app_commands.describe(salon="Optionnel : salon ciblé (par défaut : salon actuel)")
@app_commands.check(est_orga_ou_admin)
async def pause_epreuve(interaction: discord.Interaction, salon: discord.TextChannel = None):
    ch = salon or interaction.channel
    if ch.id not in ETATS_EPREUVES_SALONS:
        await interaction.response.send_message("❌ Aucune épreuve active trouvée dans ce salon.", ephemeral=True)
        return

    ETATS_EPREUVES_SALONS[ch.id]["pause_demandee"] = True
    ETATS_EPREUVES_SALONS[ch.id]["event"].clear()

    await interaction.response.send_message(
        f"⏸️ **Pause programmée dans {ch.mention} !**\n"
        f"Le candidat termine sa question actuelle, puis l'épreuve se mettra en pause avant la suivante.",
        ephemeral=True
    )


@bot.tree.command(
    name="reprendre_epreuve",
    description="Reprend une épreuve mise en pause dans un salon."
)
@app_commands.describe(salon="Optionnel : salon à relancer (par défaut : salon actuel)")
@app_commands.check(est_orga_ou_admin)
async def reprendre_epreuve(interaction: discord.Interaction, salon: discord.TextChannel = None):
    ch = salon or interaction.channel
    if ch.id not in ETATS_EPREUVES_SALONS or not ETATS_EPREUVES_SALONS[ch.id]["pause_demandee"]:
        await interaction.response.send_message("❌ L'épreuve n'est pas en attente de reprise dans ce salon.", ephemeral=True)
        return

    ETATS_EPREUVES_SALONS[ch.id]["pause_demandee"] = False
    ETATS_EPREUVES_SALONS[ch.id]["event"].set()

    await interaction.response.send_message(f"▶️ **Épreuve relancée avec succès dans {ch.mention} !**", ephemeral=True)


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
# 15. PRÉSENTATIONS (CANDIDATS & ORGAS AVEC PRÉNOM FORCÉ)
# ========================================================

async def analyser_candidat_ia(texte: str) -> dict:
    """Extrait le prénom et nettoie le texte via Gemini sans bloquer Discord."""
    if not texte:
        return {"nom": "Candidat", "texte": ""}

    prompt = (
        "Voici la présentation d'un participant à un jeu :\n\n"
        f"\"\"\"{texte}\"\"\"\n\n"
        "Consignes :\n"
        "1. Donne le prénom de la personne qui se présente (ex: Thomas, Simon, Sarah).\n"
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


def creer_embed_presentation_pure(prenom: str, texte: str, image_url: str = None) -> discord.Embed:
    """Génère la fiche officielle d'un aventurier."""
    embed = discord.Embed(
        title=f"🌴 {prenom.upper()}",
        description=texte,
        color=discord.Color.gold()
    )
    if image_url:
        embed.set_image(url=image_url)
    embed.set_footer(text=f"Aventurier : {prenom} • Fiche officielle")
    return embed


def creer_embed_presentation_orga(prenom: str, texte: str, image_url: str = None) -> discord.Embed:
    """Génère la fiche officielle d'un membre de l'organisation."""
    embed = discord.Embed(
        title=f"🛠️ {prenom.upper()} — ORGANISATION",
        description=texte,
        color=discord.Color.red()
    )
    if image_url:
        embed.set_image(url=image_url)
    embed.set_footer(text=f"Organisateur : {prenom} • Fiche Staff Officielle")
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


@bot.tree.command(
    name="formater_presentation_orga",
    description="Publie la fiche soignée d'un orga (détection automatique ou prénom forcé)."
)
@app_commands.describe(
    message_id_ou_lien="L'ID du message ou son lien Discord contenant la présentation de l'orga",
    prenom_force="Optionnel : forcer un prénom précis si l'IA risque de se tromper (ex: Sarah)",
    salon_destination="Optionnel : salon où envoyer l'embed (par défaut : salon orgas)"
)
@app_commands.check(est_orga_ou_admin)
async def formater_presentation_orga(
    interaction: discord.Interaction,
    message_id_ou_lien: str,
    prenom_force: str = None,
    salon_destination: discord.TextChannel = None
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    dest_channel = salon_destination or bot.get_channel(SALON_PRESENTATION_ORGAS_ID) or interaction.channel

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
    prenom_final = prenom_force.strip().capitalize() if prenom_force else res_ia["nom"]

    embed = creer_embed_presentation_orga(
        prenom=prenom_final,
        texte=res_ia["texte"],
        image_url=image_url
    )

    await dest_channel.send(embed=embed)
    await interaction.followup.send(
        f"✅ Fiche Orga de **{prenom_final}** publiée avec succès dans {dest_channel.mention} !",
        ephemeral=True
    )


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


@bot.tree.command(
    name="publier_derniere_presentation",
    description="Extrait et publie uniquement la toute dernière présentation postée dans le fil."
)
@app_commands.describe(
    salon_destination="Le salon où afficher la fiche (ex: #presentation)",
    fil="Optionnel : Le fil contenant la présentation (par défaut : fil actuel)"
)
@app_commands.check(est_orga_ou_admin)
async def publier_derniere_presentation(
    interaction: discord.Interaction,
    salon_destination: discord.TextChannel,
    fil: discord.Thread = None
):
    await interaction.response.defer(ephemeral=True)

    source_thread = fil or (interaction.channel if isinstance(interaction.channel, discord.Thread) else None)
    if not source_thread:
        await interaction.followup.send("❌ Exécute la commande dans le fil ou mentionne-le.", ephemeral=True)
        return

    raw_messages = [msg async for msg in source_thread.history(limit=15, oldest_first=False)]
    messages = [m for m in raw_messages if not m.author.bot and (m.content.strip() or m.attachments)]

    if not messages:
        await interaction.followup.send("❌ Aucun message récent trouvé dans ce fil.", ephemeral=True)
        return

    texte = None
    image_url = None

    for msg in messages:
        if not image_url and msg.attachments:
            for att in msg.attachments:
                if att.content_type and att.content_type.startswith("image/"):
                    image_url = att.url
                    break
        
        if not texte and msg.content.strip():
            texte = msg.content.strip()

        if texte:
            break

    if not texte:
        await interaction.followup.send("❌ Impossible de trouver un texte de présentation récent.", ephemeral=True)
        return

    res_ia = await analyser_candidat_ia(texte)
    prenom = res_ia["nom"]
    texte_corrige = res_ia["texte"]

    embed = creer_embed_presentation_pure(
        prenom=prenom,
        texte=texte_corrige,
        image_url=image_url
    )

    await salon_destination.send(embed=embed)
    await interaction.followup.send(
        f"✅ Dernière présentation publiée : **{prenom}** dans {salon_destination.mention} !",
        ephemeral=True
    )


# ========================================================
# 16. RELECTURE, REMPLACEMENT & ANALYSE D'AMBIGUÏTÉ
# ========================================================

async def traiter_et_analyser_questions_ia(lignes_brutes: list[str]) -> dict:
    """Corrige la formulation (format abécédaire inclus) et analyse les ambiguïtés."""
    texte_questions = "\n".join(lignes_brutes)

    prompt = (
        "Tu es l'arbitre en chef et concepteur d'épreuves de jeux télévisés (type Koh-Lanta / Survivor / Motus / Grand Concours).\n"
        "Voici une liste de questions brutes rédigées par des organisateurs pour une épreuve.\n"
        "NOTE IMPORTANTE : Il peut s'agir d'un Abécédaire (ex: 'A. Question dont la réponse commence par A', 'B - Question...', etc.) "
        "ou de questions avec un temps alloué à la fin (ex: '... | 15').\n\n"
        f"{texte_questions}\n\n"
        "MISSIONS :\n"
        "1. QUESTIONS_CORRIGEES : Réécris chaque question en corrigeant l'orthographe, la syntaxe et la ponctuation. "
        "Conserve IMPÉRATIVEMENT les lettres d'abécédaire (A, B, C...) et les timers éventuels (| 15) à la fin. Ne change jamais le fond ni la réponse attendue.\n"
        "2. ANALYSE_AMBIGUITE : Détecte les pièges, formulations floues, questions pouvant accepter plusieurs réponses valides non prévues, "
        "ou incohérences avec la lettre de l'abécédaire.\n\n"
        "Format STRICT attendu :\n"
        "===QUESTIONS===\n"
        "[Liste exacte des questions corrigées, une par ligne, sans numérotation ajoutée]\n"
        "===ANALYSE===\n"
        "[Remarques détaillées avec puces par question litigieuse, ou 'Aucune ambiguïté détectée. Questions claires.']"
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt
        )
        rep = response.text.strip()

        questions_corrigees = []
        analyse_texte = "Aucune ambiguïté majeure détectée."

        if "===QUESTIONS===" in rep and "===ANALYSE===" in rep:
            parties = rep.split("===ANALYSE===")
            partie_q = parties[0].replace("===QUESTIONS===", "").strip()
            analyse_texte = parties[1].strip()
            questions_corrigees = [q.strip() for q in partie_q.split("\n") if q.strip()]
        else:
            questions_corrigees = [l.strip() for l in rep.split("\n") if l.strip()]

        return {
            "questions": questions_corrigees if questions_corrigees else lignes_brutes,
            "analyse": analyse_texte
        }

    except Exception as e:
        print(f"Erreur analyse questions Gemini : {e}")
        return {
            "questions": lignes_brutes,
            "analyse": f"⚠️ Erreur lors de l'analyse automatique : {e}"
        }


@bot.tree.command(
    name="corriger_salon_questions",
    description="Remplace le message brut par la version propre et envoie l'analyse des ambiguïtés aux orgas."
)
@app_commands.describe(
    salon_questions="Optionnel : Le salon où se trouvent les questions (par défaut : salon actuel)"
)
@app_commands.check(est_orga_ou_admin)
async def corriger_salon_questions(
    interaction: discord.Interaction,
    salon_questions: discord.TextChannel = None
):
    await interaction.response.defer(ephemeral=True)
    target_channel = salon_questions or interaction.channel

    message_cible = None
    async for msg in target_channel.history(limit=30, oldest_first=False):
        if not msg.author.bot and msg.content.strip():
            message_cible = msg
            break

    if not message_cible:
        await interaction.followup.send(f"❌ Aucun message trouvé dans {target_channel.mention}.", ephemeral=True)
        return

    lignes = [l.strip() for l in message_cible.content.strip().split("\n") if l.strip()]
    if not lignes:
        await interaction.followup.send("❌ Le message cible ne contient pas de texte valide.", ephemeral=True)
        return

    await interaction.followup.send(
        f"⏳ Traitement de **{len(lignes)} questions** (Correction + Audit d'ambiguïté)...",
        ephemeral=True
    )

    resultat = await traiter_et_analyser_questions_ia(lignes)
    questions_finales = resultat["questions"]
    texte_questions_clean = "\n".join(questions_finales)

    try:
        await message_cible.delete()
    except Exception as e:
        print(f"Impossible de supprimer le message original : {e}")

    await target_channel.send(texte_questions_clean)

    salon_remarques = bot.get_channel(SALON_REMARQUES_QUESTIONS_ID)
    if salon_remarques:
        embed_remarques = discord.Embed(
            title=f"🔎 AUDIT & AMBIGUÏTÉS — #{target_channel.name}",
            description=resultat["analyse"],
            color=discord.Color.orange()
        )
        embed_remarques.set_footer(text=f"Total : {len(questions_finales)} questions vérifiées.")

        if len(resultat["analyse"]) > 3900:
            await salon_remarques.send(f"🔎 **AUDIT DES QUESTIONS — #{target_channel.name}**")
            for chunk in [resultat["analyse"][i:i+1900] for i in range(0, len(resultat["analyse"]), 1900)]:
                await salon_remarques.send(chunk)
        else:
            await salon_remarques.send(embed=embed_remarques)

    await interaction.followup.send(
        f"✅ **Terminé !**\n"
        f"- Le message dans {target_channel.mention} a été remplacé par la version propre.\n"
        f"- Le rapport d'ambiguïté a été transmis sur <#{SALON_REMARQUES_QUESTIONS_ID}>.",
        ephemeral=True
    )


# ========================================================
# 17. GESTION DES ANNONCES (FORMATAGE & PUBLICATION)
# ========================================================

async def formater_annonce_ia(texte_brut: str) -> str:
    """Corrige l'orthographe, aère et optimise la mise en page Discord sans altérer le fond."""
    prompt = (
        "Tu es l'assistant de communication officiel d'un jeu d'aventure / téléréalité (type Koh-Lanta / Survivor).\n"
        "Voici le texte brut d'une annonce rédigée par l'organisation pour les candidats :\n\n"
        f"\"\"\"{texte_brut}\"\"\"\n\n"
        "CONSIGNES DE MISE EN PAGE DISCORD :\n"
        "1. Ne change JAMAIS les consignes, les règles, les horaires ou le sens du texte.\n"
        "2. Corrige les fautes d'orthographe, la grammaire et la ponctuation.\n"
        "3. Aère le texte de façon fluide et lisible pour mobile et PC (retours à la ligne, mise en gras **...** des éléments clés).\n"
        "4. Sois sobre sur les émojis : 1 à 2 émojis bien placés au total (ex: 📢, ⚠️, 🌴), pas de surcharge visuelle.\n"
        "5. Renvoie UNIQUEMENT le texte formaté prêt à être publié, sans aucune formule de politesse introductive."
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        print(f"Erreur formatage annonce Gemini : {e}")
        return texte_brut.strip()


@bot.tree.command(
    name="formater_annonce",
    description="Met en page l'annonce dans le salon de travail avec bloc copiable."
)
@app_commands.describe(
    nombre_messages="Nombre de messages successifs du brouillon à fusionner (par défaut : 1)",
    salon_source="Optionnel : salon contenant le brouillon d'orga (par défaut : salon actuel)"
)
@app_commands.check(est_orga_ou_admin)
async def formater_annonce(
    interaction: discord.Interaction,
    nombre_messages: int = 1,
    salon_source: discord.TextChannel = None
):
    await interaction.response.defer(ephemeral=True)

    src_channel = salon_source or interaction.channel
    dest_channel = bot.get_channel(SALON_ANNONCES_TRAVAIL_ID)

    if not dest_channel:
        await interaction.followup.send(f"❌ Salon de travail introuvable (ID: `{SALON_ANNONCES_TRAVAIL_ID}`).", ephemeral=True)
        return

    raw_messages = [msg async for msg in src_channel.history(limit=nombre_messages * 3, oldest_first=False)]
    messages_valides = [m for m in raw_messages if not m.author.bot and m.content.strip()][:nombre_messages]

    if not messages_valides:
        await interaction.followup.send(f"❌ Aucun message trouvé dans {src_channel.mention}.", ephemeral=True)
        return

    messages_valides.reverse()
    texte_brut = "\n\n".join([m.content.strip() for m in messages_valides])

    texte_ameliore = await formater_annonce_ia(texte_brut)

    chunks = decouper_texte_intelligent(texte_ameliore, limite=1900)
    for bloc in chunks:
        await dest_channel.send(bloc)

    chunks_bruts = decouper_texte_intelligent(texte_ameliore, limite=1850)
    for i, chunk_b in enumerate(chunks_bruts, 1):
        suffixe = f" (Partie {i}/{len(chunks_bruts)})" if len(chunks_bruts) > 1 else ""
        await dest_channel.send(f"📋 **Texte brut à copier/coller{suffixe} :**\n```{chunk_b}```")

    await interaction.followup.send(
        f"✅ **Annonce mise en page et bloc copiable envoyés dans {dest_channel.mention} !**",
        ephemeral=True
    )


@bot.tree.command(
    name="publier_annonce",
    description="Publie l'annonce validée chez les candidats, l'archive et nettoie le salon de travail."
)
@app_commands.check(est_orga_ou_admin)
async def publier_annonce(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    source_channel = bot.get_channel(SALON_ANNONCES_TRAVAIL_ID)
    salon_candidats = bot.get_channel(SALON_ANNONCES_CANDIDATS_ID)
    salon_archive = bot.get_channel(SALON_ARCHIVES_ANNONCES_ID)

    if not source_channel:
        await interaction.followup.send(f"❌ Salon de travail introuvable (`{SALON_ANNONCES_TRAVAIL_ID}`).", ephemeral=True)
        return
    if not salon_candidats:
        await interaction.followup.send(f"❌ Salon candidats introuvable (`{SALON_ANNONCES_CANDIDATS_ID}`).", ephemeral=True)
        return

    messages = [msg async for msg in source_channel.history(limit=50, oldest_first=True)]
    messages_annonces = []

    for m in messages:
        if m.author.bot:
            continue
        contenu = m.content.strip()
        if contenu.startswith("📋 **Texte brut") or contenu.startswith("```"):
            continue
        if contenu:
            messages_annonces.append(contenu)

    if not messages_annonces:
        for m in messages:
            contenu = m.content.strip()
            if "```" in contenu:
                match = re.search(r"```(?:text)?\n?(.*?)\n?```", contenu, re.DOTALL)
                if match:
                    messages_annonces.append(match.group(1).strip())

    if not messages_annonces:
        await interaction.followup.send(f"❌ Aucun texte d'annonce trouvé dans {source_channel.mention}.", ephemeral=True)
        return

    texte_complet = "\n\n".join(messages_annonces)
    morceaux = decouper_texte_intelligent(texte_complet, limite=1900)

    for bloc in morceaux:
        await salon_candidats.send(bloc)
        await asyncio.sleep(0.4)

    if salon_archive:
        date_str = datetime.datetime.now(ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y à %H:%M")
        embed_arch = discord.Embed(
            title=f"📦 ARCHIVE ANNONCE — {date_str}",
            description=texte_complet if len(texte_complet) <= 3900 else None,
            color=discord.Color.blue()
        )
        embed_arch.set_footer(text=f"Posté dans #{salon_candidats.name} par {interaction.user.display_name}")

        if len(texte_complet) > 3900:
            await salon_archive.send(f"📦 **ARCHIVE ANNONCE — {date_str} (Posté dans {salon_candidats.mention})**")
            for bloc in morceaux:
                await salon_archive.send(bloc)
        else:
            await salon_archive.send(embed=embed_arch)

    for m in messages:
        try:
            await m.delete()
            await asyncio.sleep(0.2)
        except Exception:
            pass

    await interaction.followup.send(
        f"✅ **Annonce publiée avec succès !**\n"
        f"- 📢 Diffusée dans {salon_candidats.mention}\n"
        f"- 📦 Archivée dans <#{SALON_ARCHIVES_ANNONCES_ID}>\n"
        f"- 🧹 {source_channel.mention} a été vidé.",
        ephemeral=True
    )


# ========================================================
# 18. GESTION DES PRÉFIXES DE PSEUDOS (TAGS RÔLES)
# ========================================================

async def appliquer_tag_role(guild: discord.Guild, nom_role: str, tag: str) -> dict:
    """Ajoute un préfixe (ex: [SPEC]) aux pseudos des membres possédant un rôle."""
    role = discord.utils.get(guild.roles, name=nom_role)
    if not role:
        return {"succes": False, "erreur": f"Rôle **{nom_role}** introuvable."}

    tag_propre = f"{tag.strip()} "
    modifies = 0
    deja_faits = 0
    erreurs = 0

    async for member in guild.fetch_members(limit=None):
        if member.bot or role not in member.roles:
            continue

        pseudo_actuel = member.display_name

        if pseudo_actuel.lower().startswith(tag.lower()):
            deja_faits += 1
            continue

        if member.top_role >= guild.me.top_role and member.id != guild.me.id:
            erreurs += 1
            continue

        longueur_dispo = 32 - len(tag_propre)
        nouveau_pseudo = f"{tag_propre}{pseudo_actuel[:longueur_dispo].strip()}"

        try:
            await member.edit(nick=nouveau_pseudo, reason=f"Application du tag {tag}")
            modifies += 1
            await asyncio.sleep(0.4)
        except Exception as e:
            print(f"Impossible de renommer {member.display_name}: {e}")
            erreurs += 1

    return {
        "succes": True,
        "modifies": modifies,
        "deja_faits": deja_faits,
        "erreurs": erreurs,
        "total": len([m for m in role.members if not m.bot])
    }


@bot.tree.command(
    name="taguer_spectateurs",
    description="Ajoute automatiquement le préfixe [SPEC] devant le nom de tous les Spectateurs."
)
@app_commands.check(est_orga_ou_admin)
async def taguer_spectateurs(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    resultat = await appliquer_tag_role(
        guild=interaction.guild,
        nom_role=ROLE_SPECTATEURS_NAME,
        tag="[SPEC]"
    )

    if not resultat["succes"]:
        await interaction.followup.send(f"❌ {resultat['erreur']}", ephemeral=True)
        return

    await interaction.followup.send(
        f"👁️ **Tags Spectateurs appliqués !**\n"
        f"- ✏️ **{resultat['modifies']}** membre(s) renommé(s)\n"
        f"- ⏩ **{resultat['deja_faits']}** avaient déjà le tag\n"
        f"- ⚠️ **{resultat['erreurs']}** ignoré(s) (permissions supérieures au bot)",
        ephemeral=True
    )


@bot.tree.command(
    name="taguer_orgas",
    description="Ajoute automatiquement le préfixe [ORGA] devant le nom de tous les Orgas."
)
@app_commands.check(est_orga_ou_admin)
async def taguer_orgas(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    resultat = await appliquer_tag_role(
        guild=interaction.guild,
        nom_role=ROLE_ORGAS_NAME,
        tag="[ORGA]"
    )

    if not resultat["succes"]:
        await interaction.followup.send(f"❌ {resultat['erreur']}", ephemeral=True)
        return

    await interaction.followup.send(
        f"🛠️ **Tags Orgas appliqués !**\n"
        f"- ✏️ **{resultat['modifies']}** membre(s) renommé(s)\n"
        f"- ⏩ **{resultat['deja_faits']}** avaient déjà le tag\n"
        f"- ⚠️ **{resultat['erreurs']}** ignoré(s) (permissions supérieures au bot)",
        ephemeral=True
    )


# ========================================================
# 19. ROAST ADAPTATIF DRÔLE & BIENVEILLANT (SECOND DEGRÉ)
# ========================================================

@bot.tree.command(
    name="roast",
    description="Envoie un taquet plein d'esprit, drôle et bon enfant (100% second degré)."
)
@app_commands.describe(
    cible="Le membre à taquiner",
    contexte="Optionnel : contexte particulier pour orienter la vanne"
)
@app_commands.check(est_orga_ou_admin)
async def roast_cmd(
    interaction: discord.Interaction,
    cible: discord.Member,
    contexte: str = None
):
    await interaction.response.defer()
    guild = interaction.guild

    role_spectateur = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orga = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    if role_orga and role_orga in cible.roles:
        statut = "ORGA"
        instruction_statut = "La cible est un Orga. Taquine-le gentiment sur son zèle, ses règles alambiquées, ses retards d'animation ou ses airs de grand maître du jeu."
    elif role_spectateur and role_spectateur in cible.roles:
        statut = "SPECTATEUR"
        instruction_statut = "La cible est un Spectateur. Taquine-le sur son rôle de sélectionneur assis dans son canapé, son stock infini de popcorn et ses pronostics toujours à côté de la plaque."
    else:
        statut = "CANDIDAT"
        instruction_statut = "La cible est un Candidat. Taquine-le sur ses hésitations stratégiques, ses fausses promesses maladroites, sa discrétion sur le camp ou ses talents d'acteur ratés."

    messages_recents = []
    maintenant = datetime.datetime.now(datetime.timezone.utc)
    depuis = maintenant - datetime.timedelta(days=3)

    for ch in guild.text_channels:
        if ch.name.startswith("🔒arch-") or ch.name.lower() == "log-deplacements":
            continue

        try:
            async for msg in ch.history(limit=50, after=depuis, oldest_first=False):
                if msg.author.id == cible.id and msg.content.strip():
                    messages_recents.append(f"- {msg.content.strip()[:200]}")
                if len(messages_recents) >= 25:
                    break
        except Exception:
            continue

        if len(messages_recents) >= 25:
            break

    contexte_messages = "\n".join(messages_recents) if messages_recents else "Aucun message récent (chambre-le gentiment sur son mode fantôme)."
    contexte_orga = f"Contexte imposé par l'organisation : {contexte}" if contexte else ""

    prompt = (
        "Tu es un humoriste et maître de cérémonie dans un jeu d'aventure amical.\n"
        "Ton rôle est d'envoyer un roast plein d'esprit, drôle, créatif et taquin, mais avec BIENVEILLANCE et complicité.\n\n"
        f"CIBLE DU ROAST : {cible.display_name} (Statut : {statut})\n"
        f"CADRAGE STATUT : {instruction_statut}\n\n"
        f"SES MESSAGES SUR LE SERVEUR :\n{contexte_messages}\n\n"
        f"{contexte_orga}\n\n"
        "RÈGLES D'OR DE LA BIENVEILLANCE ET DE L'HUMOUR :\n"
        "1. SECOND DEGRÉ ABSOLU : C'est du chambrage entre potes. Reste bon enfant, zéro méchanceté gratuite, zéro attaque personnelle dégradante.\n"
        "2. CRÉATIF ET INATTENDU : Évite les clichés réchauffés de télé-réalité. Rebondis avec finesse sur ses messages, ses tics de langage, son énergie ou ses contradictions dans le jeu.\n"
        "3. FORME : 2 à 4 phrases bien ficelées qui font sourire la personne et tout le serveur.\n"
        "4. Renvoie UNIQUEMENT le texte du roast sans message introductif."
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt
        )
        punchline = response.text.strip()
    except Exception as e:
        punchline = f"{cible.mention}, j'allais te sortir ma meilleure vanne, mais ta présence ici est déjà un divertissement en soi !"

    badge = "🍿 [SPECTATEUR]" if statut == "SPECTATEUR" else ("🛠️ [STAFF]" if statut == "ORGA" else "🌴 [CANDIDAT]")
    await interaction.followup.send(f"🔥 **ROAST (100% Love & Second Degré) — {badge}** {cible.mention}\n\n{punchline}")


# ========================================================
# 20. COMMANDE DE PRAISE / COMPLIMENT THÉÂTRAL & DRÔLE
# ========================================================

@bot.tree.command(
    name="praise",
    description="Envoie une ode / compliment exagéré, hilarant et plein d'amour à un membre."
)
@app_commands.describe(
    cible="Le membre à glorifier",
    contexte="Optionnel : contexte particulier (ex: a carry l'épreuve, met l'ambiance, orga au top...)"
)
@app_commands.check(est_orga_ou_admin)
async def praise_cmd(
    interaction: discord.Interaction,
    cible: discord.Member,
    contexte: str = None
):
    await interaction.response.defer()
    guild = interaction.guild

    role_spectateur = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orga = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    if role_orga and role_orga in cible.roles:
        statut = "ORGA"
        instruction_statut = (
            "La cible est un Orga. Glorifie son travail de génie, sa patience infinie avec les candidats "
            "et son statut de divinité bienveillante qui fait tourner la boutique."
        )
    elif role_spectateur and role_spectateur in cible.roles:
        statut = "SPECTATEUR"
        instruction_statut = (
            "La cible est un Spectateur. Traite-le comme l'analyste le plus brillant de la commu, "
            "le roi incontesté du popcorn et le pilier moral de la tribune."
        )
    else:
        statut = "CANDIDAT"
        instruction_statut = (
            "La cible est un Candidat. Encense sa présence solaire, son génie tactique (même s'il est bancal), "
            "son mental d'acier ou sa simple capacité à survivre avec panache."
        )

    messages_recents = []
    maintenant = datetime.datetime.now(datetime.timezone.utc)
    depuis = maintenant - datetime.timedelta(days=3)

    for ch in guild.text_channels:
        if ch.name.startswith("🔒arch-") or ch.name.lower() == "log-deplacements":
            continue

        try:
            async for msg in ch.history(limit=50, after=depuis, oldest_first=False):
                if msg.author.id == cible.id and msg.content.strip():
                    messages_recents.append(f"- {msg.content.strip()[:200]}")
                if len(messages_recents) >= 25:
                    break
        except Exception:
            continue

        if len(messages_recents) >= 25:
            break

    contexte_messages = "\n".join(messages_recents) if messages_recents else "Aucun message récent (glorifie son aura mystérieuse et son silence de légende)."
    contexte_orga = f"Contexte imposé par l'organisation : {contexte}" if contexte else ""

    prompt = (
        "Tu es le fan numéro un, poète officiel et maître de cérémonie lyrique d'un jeu d'aventure.\n"
        "Ton rôle est de faire un compliment ULTRA-EXAGÉRÉ, hilarant, plein d'emphase et d'amour à la cible.\n\n"
        f"CIBLE : {cible.display_name} (Statut : {statut})\n"
        f"CADRAGE STATUT : {instruction_statut}\n\n"
        f"SES MESSAGES SUR LE SERVEUR :\n{contexte_messages}\n\n"
        f"{contexte_orga}\n\n"
        "RÈGLES D'OR DE LA GLORIFICATION :\n"
        "1. EXAGÉRATION TOTALE : Traite la personne comme une légende vivante, un monument de charisme ou un stratège niveau 3000 QI.\n"
        "2. ANCRAGE DRÔLE : Utilise ses vrais messages, tics de phrases ou faits d'armes pour transformer des détails banals en exploits mythiques.\n"
        "3. FORME : 2 à 4 phrases lyriques, rythmées et solennelles.\n"
        "4. Renvoie UNIQUEMENT le texte du compliment sans formule d'introduction."
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt
        )
        eloge = response.text.strip()
    except Exception as e:
        eloge = f"{cible.mention}, même les étoiles refusent de briller quand tu es là tellement tu leur fais de l'ombre !"

    badge = "🍿 [SPECTATEUR STAR]" if statut == "SPECTATEUR" else ("🛠️ [DIVINITÉ DU STAFF]" if statut == "ORGA" else "🌴 [LÉGENDE DE L'ÎLE]")
    await interaction.followup.send(f"👑 **PRAISE & GLOIRE — {badge}** {cible.mention}\n\n{eloge}")


# ========================================================
# 21. STATISTIQUES D'ACTIVITÉ : COMPTEUR DE MESSAGES
# ========================================================

@bot.tree.command(
    name="stats_messages_candidats",
    description="Affiche le nombre de messages envoyés par chaque candidat dans les salons de jeu."
)
@app_commands.describe(
    periode="Période d'analyse des messages",
    role_equipe="Optionnel : filtrer uniquement les candidats d'une équipe précise"
)
@app_commands.choices(periode=[
    app_commands.Choice(name="📅 Aujourd'hui (depuis 00h00 heure de Paris)", value="aujourdhui"),
    app_commands.Choice(name="⏱️ Dernières 24 heures", value="24h"),
    app_commands.Choice(name="⏳ Dernières 48 heures", value="48h"),
    app_commands.Choice(name="♾️ Tout l'historique récent (Max 300 msg/salon)", value="tout")
])
@app_commands.check(est_orga_ou_admin)
async def stats_messages_candidats(
    interaction: discord.Interaction,
    periode: app_commands.Choice[str] = None,
    role_equipe: discord.Role = None
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    choix_periode = periode.value if periode else "aujourdhui"
    tz_paris = ZoneInfo("Europe/Paris")
    maintenant = datetime.datetime.now(tz_paris)

    date_limite_utc = None
    if choix_periode == "aujourdhui":
        debut_jour_paris = maintenant.replace(hour=0, minute=0, second=0, microsecond=0)
        date_limite_utc = debut_jour_paris.astimezone(datetime.timezone.utc)
        texte_periode = f"Aujourd'hui ({maintenant.strftime('%d/%m/%Y')} depuis 00h00)"
    elif choix_periode == "24h":
        date_limite_utc = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
        texte_periode = "Dernières 24 heures"
    elif choix_periode == "48h":
        date_limite_utc = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=48)
        texte_periode = "Dernières 48 heures"
    else:
        texte_periode = "Historique récent complet"

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    compteur_candidats = {}

    async for member in guild.fetch_members(limit=None):
        if member.bot:
            continue
        if role_equipe and role_equipe not in member.roles:
            continue
        if role_orgas and role_orgas in member.roles:
            continue
        if role_spectateurs and role_spectateurs in member.roles and not role_equipe:
            continue

        compteur_candidats[member.id] = {
            "nom": member.display_name,
            "mention": member.mention,
            "count": 0
        }

    if not compteur_candidats:
        await interaction.followup.send("❌ Aucun candidat trouvé pour cette sélection.", ephemeral=True)
        return

    total_messages_jeu = 0
    for channel in guild.text_channels:
        if not est_categorie_candidate(channel.category) and channel.name.lower() != "log-deplacements":
            continue
        if channel.name.startswith("🔒arch-"):
            continue

        try:
            async for msg in channel.history(limit=300, after=date_limite_utc, oldest_first=False):
                if not msg.author.bot and msg.author.id in compteur_candidats:
                    compteur_candidats[msg.author.id]["count"] += 1
                    total_messages_jeu += 1
        except Exception:
            continue

    classement = sorted(compteur_candidats.values(), key=lambda x: x["count"], reverse=True)

    lignes_stats = []
    for i, c in enumerate(classement, 1):
        icone = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else f"`#{i}`"))
        lignes_stats.append(f"{icone} **{c['nom']}** ({c['mention']}) : **{c['count']}** message(s)")

    description = f"⏱️ **Période :** `{texte_periode}`\n"
    description += f"💬 **Total messages analysés :** `{total_messages_jeu}`\n"
    if role_equipe:
        description += f"👥 **Équipe :** {role_equipe.mention}\n"
    description += "\n━━━━━━━━━━━━━━━━━━━━━━\n\n" + "\n".join(lignes_stats)

    embed = discord.Embed(
        title="📊 ACTIVITÉ & STATISTIQUES DES CANDIDATS",
        description=description if len(description) <= 3900 else description[:3900] + "\n...",
        color=discord.Color.blue()
    )
    embed.set_footer(text="Comptabilisé dans les camps, confessionnaux et duos.")

    await interaction.followup.send(embed=embed, ephemeral=True)


# ========================================================
# 22. ROAST HARDCORE (ANALYSE COMPORTEMENTALE PURE)
# ========================================================

@bot.tree.command(
    name="roast_hardcore",
    description="Analyse au laser les messages d'un membre pour un clash chirurgical, inventif et sans filtre KL."
)
@app_commands.describe(
    cible="Le membre à passer au crible",
    contexte="Optionnel : un détail ou contexte particulier pour appuyer là où ça fait mal"
)
@app_commands.check(est_orga_ou_admin)
async def roast_hardcore_cmd(
    interaction: discord.Interaction,
    cible: discord.Member,
    contexte: str = None
):
    await interaction.response.defer()
    guild = interaction.guild

    messages_recents = []
    maintenant = datetime.datetime.now(datetime.timezone.utc)
    depuis = maintenant - datetime.timedelta(days=4)

    for ch in guild.text_channels:
        if ch.name.startswith("🔒arch-") or ch.name.lower() == "log-deplacements":
            continue

        try:
            async for msg in ch.history(limit=60, after=depuis, oldest_first=False):
                if msg.author.id == cible.id and msg.content.strip():
                    messages_recents.append(f"[#{ch.name}] {msg.content.strip()[:200]}")
                if len(messages_recents) >= 30:
                    break
        except Exception:
            continue

        if len(messages_recents) >= 30:
            break

    if not messages_recents:
        await interaction.followup.send(
            f"💀 {cible.mention}, même pas besoin d'analyse : tu n'as envoyé aucun message récent. "
            "Difficile de clasher quelqu'un qui a le charisme et la présence d'un salon vide."
        )
        return

    contexte_messages = "\n".join(messages_recents)
    contexte_orga = f"Détail supplémentaire : {contexte}" if contexte else ""

    prompt = (
        "Tu es un observateur cynique, un sniper d'ego et un maître du stand-up roast de haut niveau.\n"
        "OUBLIE TOTALEMENT les références aux jeux de survie, îles, flambeaux ou télé-réalité. "
        "Fais une autopsie brute, inventive et ultra-précise de la PERSONNE à travers ses propres écrits sur Discord.\n\n"
        f"CIBLE DU CLASH : {cible.display_name}\n\n"
        f"EXTRAITS DE SES VRAIS MESSAGES RÉCENTS SUR LE SERVEUR :\n{contexte_messages}\n\n"
        f"{contexte_orga}\n\n"
        "DIRECTIVES DU ROAST CHIRURGICAL :\n"
        "1. PRÉCISION ET CITATIONS : Rebondis sur ses tournures exactes, ses hésitations, ses contradictions flagrantes, son usage excessif d'émojis/ponctuation ou ses postures ridicules.\n"
        "2. ATTAQUE PSYCHOLOGIQUE : Moque-toi de son besoin d'attention, de ses justifications bancales, de sa fausse assurance ou de son énergie de victime incomprise.\n"
        "3. SANS INSULTE NI HAINE : Pas de grossièretés basiques, d'insultes dégradantes ou de discrimination. L'impact doit venir de la justesse de l'observation et du cynisme élégant.\n"
        "4. RYTHME : 3 à 5 phrases courtes, denses et percutantes qui montent en puissance.\n"
        "5. Renvoie UNIQUEMENT le texte du clash, sans intro ni conclusion."
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt
        )
        punchline = response.text.strip()
    except Exception as e:
        punchline = f"{cible.mention}, j'ai essayé d'analyser tes messages, mais le niveau de vide sidéral a fait surchauffer l'algorithme."

    await interaction.followup.send(f"⚡ **ROAST HARDCORE — AUTOPSIE** {cible.mention}\n\n{punchline}")


# ========================================================
# 23. COMMANDES DE COMPOSITION INTERACTIVE DES ÉQUIPES
# ========================================================

@bot.tree.command(
    name="lancer_composition_equipes",
    description="Lance la session interactive de sélection des équipes au tour par tour entre 2 capitaines."
)
@app_commands.describe(
    capitaine_1="Le premier capitaine à choisir",
    role_equipe_1="Le rôle de la 1ère équipe (ex: @Rouge)",
    capitaine_2="Le deuxième capitaine",
    role_equipe_2="Le rôle de la 2ème équipe (ex: @Jaune)",
    salon="Optionnel : salon où se déroule la sélection (par défaut : salon actuel)"
)
@app_commands.check(est_orga_ou_admin)
async def lancer_composition_equipes(
    interaction: discord.Interaction,
    capitaine_1: discord.Member,
    role_equipe_1: discord.Role,
    capitaine_2: discord.Member,
    role_equipe_2: discord.Role,
    salon: discord.TextChannel = None
):
    await interaction.response.defer(ephemeral=True)
    target_channel = salon or interaction.channel

    if capitaine_1.id == capitaine_2.id:
        await interaction.followup.send("❌ Les deux capitaines doivent être des membres distincts.", ephemeral=True)
        return

    try:
        await capitaine_1.add_roles(role_equipe_1, reason="Capitaine Équipe 1")
        await capitaine_2.add_roles(role_equipe_2, reason="Capitaine Équipe 2")
    except discord.Forbidden:
        await interaction.followup.send("❌ Erreur : Le bot n'a pas les permissions pour modifier les rôles de ces capitaines.", ephemeral=True)
        return

    ETAT_COMPOSITION["actif"] = True
    ETAT_COMPOSITION["channel_id"] = target_channel.id
    ETAT_COMPOSITION["capitaine_1"] = capitaine_1
    ETAT_COMPOSITION["role_1"] = role_equipe_1
    ETAT_COMPOSITION["capitaine_2"] = capitaine_2
    ETAT_COMPOSITION["role_2"] = role_equipe_2
    ETAT_COMPOSITION["tour"] = 1

    embed_intro = discord.Embed(
        title="⚔️ LA COMPOSITION DES ÉQUIPES COMMENCE !",
        description=(
            f"Les chefs de tribus ont été désignés :\n\n"
            f"👑 **Capitaine 1 :** {capitaine_1.mention} ➔ {role_equipe_1.mention}\n"
            f"👑 **Capitaine 2 :** {capitaine_2.mention} ➔ {role_equipe_2.mention}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 **Règles de la sélection :**\n"
            "- Chaque capitaine mentionne à tour de rôle le candidat qu'il souhaite intégrer.\n"
            "- Le rôle est attribué instantanément et automatiquement.\n\n"
            f"👉 **C'est parti ! {capitaine_1.mention}, mentionne le premier aventurier que tu choisis !**"
        ),
        color=discord.Color.gold()
    )
    embed_intro.set_footer(text="Système de Draft automatique • Mentionnez un membre pour le recruter")

    await target_channel.send(embed=embed_intro)
    await interaction.followup.send(f"✅ Composition des équipes lancée avec succès dans {target_channel.mention} !", ephemeral=True)


@bot.tree.command(
    name="arreter_composition_equipes",
    description="Arrête et clôture la session de sélection des équipes."
)
@app_commands.check(est_orga_ou_admin)
async def arreter_composition_equipes(interaction: discord.Interaction):
    if not ETAT_COMPOSITION["actif"]:
        await interaction.response.send_message("❌ Aucune session de composition d'équipes n'est actuellement en cours.", ephemeral=True)
        return

    ETAT_COMPOSITION["actif"] = False
    ch_id = ETAT_COMPOSITION["channel_id"]
    r1 = ETAT_COMPOSITION["role_1"]
    r2 = ETAT_COMPOSITION["role_2"]

    channel = bot.get_channel(ch_id) or interaction.channel

    embed_fin = discord.Embed(
        title="🛑 COMPOSITION DES ÉQUIPES TERMINÉE",
        description=(
            f"Les tribus {r1.mention} et {r2.mention} sont désormais formées et prêtes pour l'aventure !\n\n"
            f"👥 **Membres {r1.name} :** {len(r1.members)}\n"
            f"👥 **Membres {r2.name} :** {len(r2.members)}"
        ),
        color=discord.Color.dark_grey()
    )
    await channel.send(embed=embed_fin)
    await interaction.response.send_message("✅ Session de composition des équipes clôturée.", ephemeral=True)


# ========================================================
# 24. COMMANDE DE TEASING D'ANNONCE (STYLE KOH-LANTA SOBRE & PROPRE)
# ========================================================

@bot.tree.command(
    name="teasing_annonce",
    description="Publie un teasing sobre avec compte à rebours dynamique avant une annonce."
)
@app_commands.describe(
    minutes="Temps d'attente en minutes avant l'annonce (ex: 5, 10, 30)",
    titre_teasing="Optionnel : Intitulé du teasing (ex: Conseil, Épreuve, Destins Liés...)",
    salon_destination="Optionnel : Salon cible (par défaut : salon annonces candidats)"
)
@app_commands.check(est_orga_ou_admin)
async def teasing_annonce(
    interaction: discord.Interaction,
    minutes: int,
    titre_teasing: str = "COMMUNICATION OFFICIELLE",
    salon_destination: discord.TextChannel = None
):
    await interaction.response.defer(ephemeral=True)

    dest_channel = salon_destination or bot.get_channel(SALON_ANNONCES_CANDIDATS_ID) or interaction.channel
    if not isinstance(dest_channel, discord.TextChannel):
        await interaction.followup.send("❌ Le salon cible doit être un salon textuel.", ephemeral=True)
        return

    maintenant_utc = datetime.datetime.now(datetime.timezone.utc)
    fin_attente = maintenant_utc + datetime.timedelta(minutes=minutes)
    timestamp_fin = int(fin_attente.timestamp())

    embed_teasing = discord.Embed(
        title=f"📜 ━━━ **{titre_teasing.upper()}** ━━━",
        description=(
            "🔥 **Aventuriers, tenez-vous prêts pour l'annonce qui arrive !**\n\n"
            "```yaml\n"
            "Statut : Transmission imminente\n"
            "Accès  : Tous les participants\n"
            "```\n"
            f"⏳ **Révélation :** <t:{timestamp_fin}:R> *(à <t:{timestamp_fin}:T>)*\n\n"
            "⚠️ *Restez attentifs sur ce salon. La sentence sera irrévocable.*"
        ),
        color=discord.Color.from_rgb(220, 150, 30)
    )
    embed_teasing.set_footer(
        text="Koh-Lanta • Message de l'Organisation",
        icon_url=interaction.guild.icon.url if interaction.guild.icon else None
    )

    await dest_channel.send(embed=embed_teasing)
    await interaction.followup.send(
        f"✅ Teasing envoyé dans {dest_channel.mention} (Révélation : <t:{timestamp_fin}:T>) !",
        ephemeral=True
    )


# ========================================================
# 25. SALONS D'ÉPREUVES GROUPÉES & VOCAUX INDIVIDUELS
# ========================================================

@bot.tree.command(
    name="creer_vocaux_individuels",
    description="Génère un salon vocal individuel privé pour chaque membre ayant un rôle d'équipe."
)
@app_commands.describe(
    role_equipe="Le rôle d'équipe ciblé (ex: @Jaune, @Rouge ou rôle candidats)",
    nom_categorie="Nom de la catégorie où créer les vocaux individuels"
)
@app_commands.check(est_orga_ou_admin)
async def creer_vocaux_individuels(
    interaction: discord.Interaction,
    role_equipe: discord.Role,
    nom_categorie: str
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    membres = [m for m in role_equipe.members if not m.bot]
    if not membres:
        await interaction.followup.send(f"❌ Aucun membre trouvé avec le rôle {role_equipe.mention}.", ephemeral=True)
        return

    clean_cat_name = nettoyer_texte(nom_categorie)
    categorie = discord.utils.find(lambda c: nettoyer_texte(c.name) == clean_cat_name, guild.categories)
    if not categorie:
        categorie = await guild.create_category(nom_categorie)

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    crees = 0
    for membre in membres:
        role_perso = trouver_role_personnel(membre, role_equipe)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, mute_members=True)
        }

        if role_perso:
            overwrites[role_perso] = discord.PermissionOverwrite(
                view_channel=True, connect=True, speak=True, stream=True, use_voice_activation=True
            )
        else:
            overwrites[membre] = discord.PermissionOverwrite(
                view_channel=True, connect=True, speak=True, stream=True, use_voice_activation=True
            )

        if role_spectateurs:
            overwrites[role_spectateurs] = get_spectateur_voice_overwrites()

        if role_orgas:
            overwrites[role_orgas] = discord.PermissionOverwrite(
                view_channel=True, connect=True, speak=True, mute_members=True, deafen_members=True, move_members=True
            )

        nom_vocal = f"🔊・{formater_nom_salon(membre.display_name)}"
        await guild.create_voice_channel(name=nom_vocal, category=categorie, overwrites=overwrites)
        crees += 1
        await asyncio.sleep(0.5)

    await interaction.followup.send(
        f"✅ **{crees} salons vocaux individuels créés** dans la catégorie **{categorie.name}** !",
        ephemeral=True
    )


@bot.tree.command(
    name="creer_epreuve_groupe",
    description="Crée automatiquement un salon textuel ET un salon vocal privés dans la catégorie épreuves."
)
@app_commands.describe(
    nom_salon="Nom de base de l'épreuve (ex: epreuve-orientation, mort-subite...)",
    candidat_1="1er candidat",
    candidat_2="2ème candidat",
    candidat_3="3ème candidat (optionnel)",
    candidat_4="4ème candidat (optionnel)",
    candidat_5="5ème candidat (optionnel)",
    candidat_6="6ème candidat (optionnel)",
    candidat_7="7ème candidat (optionnel)",
    candidat_8="8ème candidat (optionnel)",
    candidat_9="9ème candidat (optionnel)",
    candidat_10="10ème candidat (optionnel)"
)
@app_commands.check(est_orga_ou_admin)
async def creer_epreuve_groupe(
    interaction: discord.Interaction,
    nom_salon: str,
    candidat_1: discord.Member,
    candidat_2: discord.Member,
    candidat_3: discord.Member = None,
    candidat_4: discord.Member = None,
    candidat_5: discord.Member = None,
    candidat_6: discord.Member = None,
    candidat_7: discord.Member = None,
    candidat_8: discord.Member = None,
    candidat_9: discord.Member = None,
    candidat_10: discord.Member = None
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    categorie = guild.get_channel(CATEGORY_EPREUVE_ID)
    if not categorie or not isinstance(categorie, discord.CategoryChannel):
        await interaction.followup.send(f"❌ Catégorie Épreuves introuvable (ID: `{CATEGORY_EPREUVE_ID}`).", ephemeral=True)
        return

    participants = [c for c in [candidat_1, candidat_2, candidat_3, candidat_4, candidat_5, candidat_6, candidat_7, candidat_8, candidat_9, candidat_10] if c is not None]
    participants = list(set(participants))

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    # 1. Overwrites Textuel
    overwrites_text = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False, read_messages=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, read_messages=True, send_messages=True)
    }
    # 2. Overwrites Vocal
    overwrites_voice = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False, connect=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, speak=True, mute_members=True)
    }

    for p in participants:
        r_perso = trouver_role_personnel(p)
        cible_perm = r_perso if r_perso else p

        overwrites_text[cible_perm] = discord.PermissionOverwrite(
            view_channel=True, read_messages=True, read_message_history=True, send_messages=True
        )
        overwrites_voice[cible_perm] = discord.PermissionOverwrite(
            view_channel=True, connect=True, speak=True, stream=True, use_voice_activation=True
        )

    if role_spectateurs:
        overwrites_text[role_spectateurs] = get_spectateur_overwrites()
        overwrites_voice[role_spectateurs] = get_spectateur_voice_overwrites()

    if role_orgas:
        overwrites_text[role_orgas] = discord.PermissionOverwrite(
            view_channel=True, read_messages=True, read_message_history=True, send_messages=True
        )
        overwrites_voice[role_orgas] = discord.PermissionOverwrite(
            view_channel=True, connect=True, speak=True, mute_members=True, deafen_members=True, move_members=True
        )

    nom_clean = formater_nom_salon(nom_salon)

    # Création simultanée des deux salons
    salon_texte = await guild.create_text_channel(
        name=f"⚔️・{nom_clean}",
        category=categorie,
        overwrites=overwrites_text
    )
    salon_vocal = await guild.create_voice_channel(
        name=f"🔊・{nom_clean}",
        category=categorie,
        overwrites=overwrites_voice
    )

    mentions = ", ".join([p.mention for p in participants])
    await salon_texte.send(
        f"⚔️ **SALON D'ÉPREUVE GROUPÉE**\n"
        f"Bienvenue {mentions}.\n"
        f"🔊 Le salon vocal associé est disponible : {salon_vocal.mention}"
    )

    await interaction.followup.send(
        f"✅ **Duo Écrit + Vocal créé dans {categorie.name} !**\n"
        f"- 💬 **Textuel :** {salon_texte.mention}\n"
        f"- 🔊 **Vocal :** {salon_vocal.mention}\n"
        f"👥 **Candidats autorisés ({len(participants)}) :** {mentions}",
        ephemeral=True
    )

@bot.tree.command(
    name="supprimer_epreuve_groupe",
    description="Supprime un salon d'épreuve une fois l'épreuve terminée."
)
@app_commands.describe(salon="Optionnel : le salon d'épreuve à supprimer (par défaut : salon actuel)")
@app_commands.check(est_orga_ou_admin)
async def supprimer_epreuve_groupe(interaction: discord.Interaction, salon: discord.abc.GuildChannel = None):
    await interaction.response.defer(ephemeral=True)
    target_channel = salon or interaction.channel

    nom_salon = target_channel.name
    try:
        await target_channel.delete(reason=f"Épreuve terminée par {interaction.user.display_name}")
        if target_channel.id != interaction.channel_id:
            await interaction.followup.send(f"🗑️ Le salon d'épreuve **{nom_salon}** a été supprimé avec succès.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Impossible de supprimer le salon : {e}", ephemeral=True)


# ========================================================
# 26. MESSAGE SECRET ÉPHÉMÈRE EN CONFESSIONNAL
# ========================================================

class SecretConfessionnalView(discord.ui.View):
    def __init__(self, candidat_id: int, texte_secret: str):
        super().__init__(timeout=None)
        self.candidat_id = candidat_id
        self.texte_secret = texte_secret

    @discord.ui.button(label="👁️ RÉVÉLER MON CODE SECRET", style=discord.ButtonStyle.danger, custom_id="btn_reveal_secret")
    async def reveler_secret(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        if interaction.user.id != self.candidat_id and not interaction.user.guild_permissions.administrator:
            await interaction.followup.send(
                "⛔ **Accès refusé :** Ce secret est personnel et réservé au candidat de ce confessionnal.", 
                ephemeral=True
            )
            return

        embed_perso = discord.Embed(
            title="🗝️ TON CODE SECRET PERSONNEL",
            description=(
                f"Voici ta transmission secrète :\n\n"
                f"```{self.texte_secret}```\n"
                "*(Ce message est éphémère : tu es le seul à le voir sur ton écran)*"
            ),
            color=discord.Color.gold()
        )
        await interaction.followup.send(embed=embed_perso, ephemeral=True)


@bot.tree.command(
    name="deposer_secret_confessionnal",
    description="Dépose un bouton à secret dans le confessionnal (visible uniquement par le candidat en éphémère)."
)
@app_commands.describe(
    candidat="Le candidat concerné",
    texte_secret="Le code secret, énigme ou consigne confidentielle",
    salon="Optionnel : confessionnal cible (par défaut : salon actuel)"
)
@app_commands.check(est_orga_ou_admin)
async def deposer_secret_confessionnal(
    interaction: discord.Interaction,
    candidat: discord.Member,
    texte_secret: str,
    salon: discord.TextChannel = None
):
    await interaction.response.defer(ephemeral=True)
    target_channel = salon or interaction.channel

    view = SecretConfessionnalView(candidat_id=candidat.id, texte_secret=texte_secret)

    embed_annonce = discord.Embed(
        title="🗝️ TRANSMISSION CONFIDENTIELLE DE L'ORGANISATION",
        description=(
            f"{candidat.mention}, une information secrète a été déposée dans ton confessionnal.\n\n"
            "👉 **Clique sur le bouton rouge ci-dessous pour l'afficher sur ton écran.**\n\n"
            "*(Sécurité active : aucun spectateur ne peut voir son contenu)*"
        ),
        color=discord.Color.dark_red()
    )
    embed_annonce.set_footer(text="Système de confidentialité Koh-Lanta")

    await target_channel.send(content=candidat.mention, embed=embed_annonce, view=view)
    await interaction.followup.send(
        f"✅ Message secret déposé dans {target_channel.mention} pour {candidat.mention} (invisible aux spectateurs) !",
        ephemeral=True
    )


@bot.tree.command(
    name="creer_salons_depuis_message",
    description="Crée les salons de binômes directement à partir du message de tirage validé."
)
@app_commands.describe(
    message_id_ou_lien="L'ID ou le lien du message Discord avec le tirage des Destins Liés",
    nom_categorie="Nom de la catégorie où créer les salons (ex: 🔥 DESTINS LIÉS)"
)
@app_commands.check(est_orga_ou_admin)
async def creer_salons_depuis_message(
    interaction: discord.Interaction,
    message_id_ou_lien: str,
    nom_categorie: str = "🔥 DESTINS LIÉS"
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    msg_id = message_id_ou_lien.strip().split("/")[-1]
    try:
        msg_id_int = int(msg_id)
    except ValueError:
        await interaction.followup.send("❌ Lien ou ID de message invalide.", ephemeral=True)
        return

    target_msg = None
    try:
        target_msg = await interaction.channel.fetch_message(msg_id_int)
    except Exception:
        for ch in guild.text_channels:
            try:
                target_msg = await ch.fetch_message(msg_id_int)
                if target_msg:
                    break
            except Exception:
                continue

    if not target_msg or not target_msg.embeds:
        await interaction.followup.send("❌ Message de tirage introuvable ou sans embed.", ephemeral=True)
        return

    description = target_msg.embeds[0].description
    lignes = [l for l in description.split("\n") if "Binôme" in l and "&" in l]
    
    if not lignes:
        await interaction.followup.send("❌ Impossible d'extraire les binômes depuis cet embed.", ephemeral=True)
        return

    binomes_recuperes = []
    for ligne in lignes:
        ids_trouves = re.findall(r"<@!?(\d+)>", ligne)
        if len(ids_trouves) >= 2:
            m1 = guild.get_member(int(ids_trouves[0]))
            m2 = guild.get_member(int(ids_trouves[1]))
            if m1 and m2:
                binomes_recuperes.append((
                    {
                        "member": m1,
                        "role": trouver_role_personnel(m1),
                        "clean_name": formater_nom_salon(m1.display_name)
                    },
                    {
                        "member": m2,
                        "role": trouver_role_personnel(m2),
                        "clean_name": formater_nom_salon(m2.display_name)
                    }
                ))

    if not binomes_recuperes:
        await interaction.followup.send("❌ Aucun membre valide trouvé dans les mentions du message.", ephemeral=True)
        return

    clean_target_name = nettoyer_texte(nom_categorie)
    existing_category = discord.utils.find(lambda c: nettoyer_texte(c.name) == clean_target_name, guild.categories)

    category_index = 1
    if existing_category:
        current_category = existing_category
        channel_count_in_current_cat = len(existing_category.channels)
    else:
        current_category = await guild.create_category(nom_categorie)
        channel_count_in_current_cat = 0

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    salons_crees = []
    for ca, cb in binomes_recuperes:
        if channel_count_in_current_cat >= MAX_CHANNELS_PER_CATEGORY:
            category_index += 1
            current_category = await guild.create_category(f"{nom_categorie} - {category_index}")
            channel_count_in_current_cat = 0
            await asyncio.sleep(1)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, view_channel=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True)
        }

        cible_1 = ca["role"] if ca["role"] else ca["member"]
        overwrites[cible_1] = discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True)

        cible_2 = cb["role"] if cb["role"] else cb["member"]
        overwrites[cible_2] = discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True)

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

    await interaction.followup.send(
        f"✅ **{len(salons_crees)} salons de binômes créés avec succès** dans **{current_category.name}** !\n\n" + "\n".join(salons_crees),
        ephemeral=True
    )


# ========================================================
# 27. ANNONCE OFFICIELLE DE LA COMPOSITION DES ÉQUIPES
# ========================================================

@bot.tree.command(
    name="annoncer_equipes",
    description="Génère une annonce visuelle soignée de la composition officielle des deux tribus."
)
@app_commands.describe(
    role_equipe_1="Rôle de la première équipe (ex: @Tribu Rouge)",
    role_equipe_2="Rôle de la deuxième équipe (ex: @Tribu Jaune)",
    capitaine_1="Optionnel : Capitaine de la première équipe",
    capitaine_2="Optionnel : Capitaine de la deuxième équipe",
    nom_tribu_1="Optionnel : Nom personnalisé (ex: Coravu, Sambor, Korok...)",
    nom_tribu_2="Optionnel : Nom personnalisé (ex: Simban, Matu, Takeo...)",
    salon_destination="Optionnel : Salon cible (par défaut : salon annonces candidats)"
)
@app_commands.check(est_orga_ou_admin)
async def annoncer_equipes(
    interaction: discord.Interaction,
    role_equipe_1: discord.Role,
    role_equipe_2: discord.Role,
    capitaine_1: discord.Member = None,
    capitaine_2: discord.Member = None,
    nom_tribu_1: str = None,
    nom_tribu_2: str = None,
    salon_destination: discord.TextChannel = None
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    dest_channel = salon_destination or bot.get_channel(SALON_ANNONCES_CANDIDATS_ID) or interaction.channel
    if not isinstance(dest_channel, discord.TextChannel):
        await interaction.followup.send("❌ Le salon de destination doit être un salon textuel.", ephemeral=True)
        return

    membres_1 = [m for m in role_equipe_1.members if not m.bot]
    membres_2 = [m for m in role_equipe_2.members if not m.bot]

    if not membres_1 and not membres_2:
        await interaction.followup.send("❌ Aucun membre trouvé dans ces deux rôles d'équipe.", ephemeral=True)
        return

    lignes_tribu_1 = []
    if capitaine_1 and capitaine_1 in membres_1:
        lignes_tribu_1.append(f"👑 **{capitaine_1.display_name}** ({capitaine_1.mention}) `[Capitaine]`")
    
    for m in membres_1:
        if capitaine_1 and m.id == capitaine_1.id:
            continue
        lignes_tribu_1.append(f"▫️ **{m.display_name}** ({m.mention})")

    lignes_tribu_2 = []
    if capitaine_2 and capitaine_2 in membres_2:
        lignes_tribu_2.append(f"👑 **{capitaine_2.display_name}** ({capitaine_2.mention}) `[Capitaine]`")

    for m in membres_2:
        if capitaine_2 and m.id == capitaine_2.id:
            continue
        lignes_tribu_2.append(f"▫️ **{m.display_name}** ({m.mention})")

    titre_1 = nom_tribu_1.upper() if nom_tribu_1 else role_equipe_1.name.upper()
    titre_2 = nom_tribu_2.upper() if nom_tribu_2 else role_equipe_2.name.upper()

    texte_tribu_1 = "\n".join(lignes_tribu_1) if lignes_tribu_1 else "*Aucun membre assigné*"
    texte_tribu_2 = "\n".join(lignes_tribu_2) if lignes_tribu_2 else "*Aucun membre assigné*"

    embed = discord.Embed(
        title="🌴 ━━━ COMPOSITION OFFICIELLE DES TRIBUS ━━━ 🌴",
        description=(
            "Aventuriers, le destin a parlé !\n"
            "Les tribus sont désormais scellées. Voici la répartition officielle de vos équipes pour la suite de l'aventure :\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=discord.Color.gold()
    )

    nom_r1_clean = nettoyer_texte(role_equipe_1.name)
    nom_r2_clean = nettoyer_texte(role_equipe_2.name)

    emoji_1 = "🔴" if "rouge" in nom_r1_clean else ("🟡" if "jaune" in nom_r1_clean else ("🔵" if "bleu" in nom_r1_clean else "🛡️"))
    emoji_2 = "🟡" if "jaune" in nom_r2_clean else ("🔴" if "rouge" in nom_r2_clean else ("🟢" if "vert" in nom_r2_clean else "⚔️"))

    embed.add_field(
        name=f"{emoji_1} TRIBU {titre_1} ({len(membres_1)} Aventuriers)",
        value=f"{role_equipe_1.mention}\n\n{texte_tribu_1}\n\n━━━━━━━━━━━━━━━━━━━━",
        inline=False
    )

    embed.add_field(
        name=f"{emoji_2} TRIBU {titre_2} ({len(membres_2)} Aventuriers)",
        value=f"{role_equipe_2.mention}\n\n{texte_tribu_2}",
        inline=False
    )

    embed.set_footer(
        text="Koh-Lanta • Que la meilleure tribu l'emporte !",
        icon_url=guild.icon.url if guild.icon else None
    )

    await dest_channel.send(
        content=f"📢 **ANNONCE OFFICIELLE DES TRIBUS** — {role_equipe_1.mention} {role_equipe_2.mention}",
        embed=embed
    )

    await interaction.followup.send(
        f"✅ Composition des équipes publiée avec succès dans {dest_channel.mention} !",
        ephemeral=True
    )


# ========================================================
# 28. EXPORT DES PRÉSENTATIONS POUR GOOGLE SHEETS / EXCEL
# ========================================================

@bot.tree.command(
    name="exporter_presentations_sheet",
    description="Exporte les fiches de présentations (Candidat | Description) dans un format prêt pour Google Sheets."
)
@app_commands.describe(
    salon_source="Le salon ou fil où se trouvent les fiches de présentation",
    limite_messages="Nombre maximum de messages à analyser (par défaut : 100)"
)
@app_commands.check(est_orga_ou_admin)
async def exporter_presentations_sheet(
    interaction: discord.Interaction,
    salon_source: discord.abc.GuildChannel,
    limite_messages: int = 100
):
    await interaction.response.defer(ephemeral=True)

    if not isinstance(salon_source, (discord.TextChannel, discord.Thread)):
        await interaction.followup.send("❌ Le salon source doit être un salon textuel ou un fil.", ephemeral=True)
        return

    lignes_tsv = ["Candidat\tDescription"]
    total_extraits = 0

    async for msg in salon_source.history(limit=limite_messages, oldest_first=True):
        if msg.embeds:
            for emb in msg.embeds:
                if emb.title and ("🌴" in emb.title or "Aventurier" in str(emb.footer.text)):
                    nom = emb.title.replace("🌴", "").strip()
                    desc = emb.description.replace("\r\n", " ").replace("\n", " ").replace("\t", " ").strip() if emb.description else ""
                    if nom and desc:
                        lignes_tsv.append(f"{nom}\t{desc}")
                        total_extraits += 1

        elif not msg.author.bot and msg.content.strip():
            texte_brut = msg.content.strip()
            lignes = [l.strip() for l in texte_brut.split("\n") if l.strip()]
            if lignes:
                nom = msg.author.display_name
                desc = " ".join(lignes).replace("\t", " ").strip()
                lignes_tsv.append(f"{nom}\t{desc}")
                total_extraits += 1

    if total_extraits == 0:
        await interaction.followup.send(f"❌ Aucune présentation trouvée dans {salon_source.mention}.", ephemeral=True)
        return

    contenu_tsv = "\n".join(lignes_tsv)

    fichier_bytes = io.BytesIO(contenu_tsv.encode("utf-8"))
    discord_file = discord.File(fichier_bytes, filename="presentations_candidats.tsv")

    if len(contenu_tsv) <= 1800:
        texte_reponse = (
            f"✅ **{total_extraits} présentations extraites !**\n\n"
            f"📋 **Copie le bloc ci-dessous et colle directement sur Google Sheets (Ctrl+V) :**\n"
            f"```{contenu_tsv}```"
        )
        await interaction.followup.send(content=texte_reponse, file=discord_file, ephemeral=True)
    else:
        texte_reponse = (
            f"✅ **{total_extraits} présentations extraites !**\n"
            "📄 Le volume étant trop grand pour Discord, télécharge le fichier `.tsv` ci-joint et importe-le sur Google Sheets (*Fichier > Importer*)."
        )
        await interaction.followup.send(content=texte_reponse, file=discord_file, ephemeral=True)


# ========================================================
# 29. EXPORT DES PHOTOS CANDIDATS (ARCHIVE ZIP RENOMMÉE)
# ========================================================

@bot.tree.command(
    name="exporter_photos_candidats",
    description="Télécharge et compile toutes les photos des candidats dans un fichier .ZIP prêt pour Drive."
)
@app_commands.describe(
    salon_source="Le salon ou fil où se trouvent les présentations avec photos",
    limite_messages="Nombre maximum de messages à analyser (par défaut : 100)"
)
@app_commands.check(est_orga_ou_admin)
async def exporter_photos_candidats(
    interaction: discord.Interaction,
    salon_source: discord.abc.GuildChannel,
    limite_messages: int = 100
):
    await interaction.response.defer(ephemeral=True)

    if not isinstance(salon_source, (discord.TextChannel, discord.Thread)):
        await interaction.followup.send("❌ Le salon source doit être un salon textuel ou un fil.", ephemeral=True)
        return

    photos_a_telecharger = []

    async for msg in salon_source.history(limit=limite_messages, oldest_first=True):
        if msg.embeds:
            for emb in msg.embeds:
                if emb.image and emb.image.url:
                    nom = emb.title.replace("🌴", "").replace("🛠️", "").replace("ORGANISATION", "").strip() if emb.title else "candidat"
                    nom_fichier = formater_nom_salon(nom)
                    photos_a_telecharger.append((nom_fichier, emb.image.url))

        elif msg.attachments:
            for att in msg.attachments:
                if att.content_type and att.content_type.startswith("image/"):
                    nom = msg.author.display_name
                    nom_fichier = formater_nom_salon(nom)
                    photos_a_telecharger.append((nom_fichier, att.url))

    if not photos_a_telecharger:
        await interaction.followup.send(f"❌ Aucune image de candidat trouvée dans {salon_source.mention}.", ephemeral=True)
        return

    await interaction.followup.send(
        f"⏳ Téléchargement et compression de **{len(photos_a_telecharger)} photo(s)** en cours...",
        ephemeral=True
    )

    zip_buffer = io.BytesIO()
    compteur_noms = {}

    async with aiohttp.ClientSession() as session:
        with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            for nom, url in photos_a_telecharger:
                try:
                    async with session.get(url, timeout=10) as resp:
                        if resp.status == 200:
                            img_data = await resp.read()

                            ext = "png"
                            if ".jpg" in url.lower() or ".jpeg" in url.lower():
                                ext = "jpg"
                            elif ".webp" in url.lower():
                                ext = "webp"

                            compteur_noms[nom] = compteur_noms.get(nom, 0) + 1
                            suffixe = f"_{compteur_noms[nom]}" if compteur_noms[nom] > 1 else ""

                            nom_final = f"{nom}{suffixe}.{ext}"
                            zip_file.writestr(nom_final, img_data)
                except Exception as e:
                    print(f"Erreur téléchargement image {nom} : {e}")

    zip_buffer.seek(0)
    taille_mo = len(zip_buffer.getvalue()) / (1024 * 1024)

    if taille_mo > 24:
        await interaction.followup.send(
            f"⚠️ L'archive est trop volumineuse pour être envoyée directement sur Discord ({taille_mo:.1f} Mo > 25 Mo).",
            ephemeral=True
        )
        return

    fichier_zip_discord = discord.File(zip_buffer, filename="photos_candidats.zip")

    await interaction.followup.send(
        content=(
            f"✅ **Archive prête !**\n"
            f"📁 **{len(photos_a_telecharger)} photo(s)** téléchargées et renommées.\n"
            f"👉 Télécharge le fichier `.zip` ci-joint, décompresse-le ou glisse-le directement sur ton dossier **Google Drive**."
        ),
        file=fichier_zip_discord,
        ephemeral=True
    )

# ========================================================
# 30. BILAN & AUDIT INDIVIDUEL CANDIDAT (NOTE, POSITION & BÊTISIER)
# ========================================================

async def generer_bilan_candidat_ia(candidat_nom: str, messages_candidat: list[str], messages_sur_candidat: list[str]) -> str:
    """Génère une analyse complète (Note/10, positionnement stratégique, bêtisier) d'un joueur Discord."""
    texte_dits = "\n".join(messages_candidat) if messages_candidat else "Aucun message direct trouvé."
    texte_sur_lui = "\n".join(messages_sur_candidat) if messages_sur_candidat else "Aucun message parlant de lui trouvé."

    prompt = (
        "Tu es l'analyste en chef, juré impartial et showrunner d'un jeu de stratégie et d'éliminations SUR DISCORD (type Koh-Lanta / Survivor / Big Brother adapté sur serveur).\n"
        f"🎯 CANDIDAT CIBLÉ : **{candidat_nom}**\n\n"
        "=== CE QUE LE CANDIDAT A DIT (EXTRAITS DE SES SALONS, CONFESSIONNAUX & DUOS) ===\n"
        f"{texte_dits[:20000]}\n\n"
        "=== CE QUE LES AUTRES CANDIDATS ONT DIT SUR LUI (COMPLOTS, DUOS, ALLIANCES, VOTES) ===\n"
        f"{texte_sur_lui[:20000]}\n\n"
        "CONSIGNES DE FOND & CADRAGE DISCORD :\n"
        "1. Contexte 100% Discord : Le jeu se passe par salons textuels, MPs/duos, vocaux et logs. Oublie totalement les allusions à une île déserte, la jungle, le sable ou la survie physique.\n"
        "2. Sois lucide, percutant, objectif et sans complaisance.\n\n"
        "STRUCTURE STRICTE DU RAPPORT ATTENDUE :\n\n"
        f"## 🏆 1. LA NOTE DU JURY : [NOTE/10] — [TITRE ÉVOCATEUR]\n"
        "- Attribue une note globale sur 10 à son aventure jusqu'ici (Stratégie, Social, Survie sur le serveur, Activité).\n"
        "- Justifie la note en 2 phrases denses.\n\n"
        "## 🧭 2. POSITIONNEMENT & DYNAMIQUE STRATÉGIQUE\n"
        "- **Son rôle sur le serveur :** (Maître du jeu, suiveur, sniper, agent double, électron libre...)\n"
        "- **Alliances réelles vs Illusions :** Avec qui joue-t-il vraiment et à qui fait-il faussement confiance ?\n"
        "- **Ce qui se trame dans son dos :** Est-il ciblé ? Les autres le sous-estiment-ils ou préparent-ils un blindside ?\n\n"
        "## ⚠️ 3. FORCES & POINTS FAIBLES\n"
        "- 🟢 **Points forts :** (2 bullet points)\n"
        "- 🔴 **Erreurs & Failles :** (2 bullet points)\n\n"
        "## 🤡 4. LE BÊTISIER & LES PLUS GROSSES DINGUERIES\n"
        "- Relève 3 à 5 de ses meilleures perles, punchlines absurdes, moments de panique comiques, contradictions flagrantes ou messages lunaires postés sur le serveur.\n\n"
        "Reste concis, structuré et dynamique."
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        return f"❌ Erreur lors de l'analyse IA : {e}"


@bot.tree.command(
    name="bilan_candidat",
    description="Génère le bilan complet d'un candidat : Note sur 10, analyse stratégique et bêtisier."
)
@app_commands.describe(
    candidat="Le candidat à évaluer",
    limite_par_salon="Nombre de messages récents à scanner par salon (par défaut : 80)"
)
@app_commands.check(est_orga_ou_admin)
async def bilan_candidat(
    interaction: discord.Interaction,
    candidat: discord.Member,
    limite_par_salon: int = 80
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    nom_candidat_clean = nettoyer_texte(candidat.display_name)
    pseudo_global_clean = nettoyer_texte(candidat.name)
    mention_id = str(candidat.id)

    messages_candidat = []
    messages_sur_candidat = []

    await interaction.followup.send(
        f"⏳ **Collecte et analyse de l'aventure de {candidat.mention} en cours...**\n"
        f"*(Scan de tous les salons textuels de jeu, confessionnaux, duos et logs)*",
        ephemeral=True
    )

    for channel in guild.text_channels:
        est_salon_log = (channel.name.lower() == "log-deplacements")
        
        if not est_categorie_candidate(channel.category) and not est_salon_log:
            continue
        if channel.name.startswith("🔒arch-"):
            continue

        try:
            async for msg in channel.history(limit=limite_par_salon, oldest_first=False):
                if msg.author.bot and not est_salon_log:
                    continue

                contenu = msg.content.strip()
                if not contenu:
                    continue

                # 1. Message écrit PAR le candidat
                if msg.author.id == candidat.id:
                    messages_candidat.append(f"[#{channel.name}] {candidat.display_name}: {contenu}")

                # 2. Message écrit PAR UN AUTRE mais parlant DU candidat
                else:
                    contenu_clean = nettoyer_texte(contenu)
                    if (
                        nom_candidat_clean in contenu_clean 
                        or pseudo_global_clean in contenu_clean 
                        or mention_id in msg.content
                    ):
                        messages_sur_candidat.append(f"[#{channel.name}] {msg.author.display_name} sur {candidat.display_name}: {contenu}")

        except Exception:
            continue

    if not messages_candidat and not messages_sur_candidat:
        await interaction.followup.send(f"⚠️ Aucune donnée ou discussion trouvée pour {candidat.mention}.", ephemeral=True)
        return

    # Analyse IA
    rapport_bilan = await generer_bilan_candidat_ia(
        candidat_nom=candidat.display_name,
        messages_candidat=messages_candidat[:120],
        messages_sur_candidat=messages_sur_candidat[:120]
    )

    date_str = datetime.datetime.now(ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y à %H:%M")
    embed = discord.Embed(
        title=f"📊 BILAN D'AVENTURE — {candidat.display_name.upper()}",
        description=rapport_bilan if len(rapport_bilan) <= 3900 else None,
        color=discord.Color.gold()
    )
    embed.set_thumbnail(url=candidat.display_avatar.url)
    embed.set_footer(text=f"Bilan Staff Officiel • Demandé par {interaction.user.display_name} • {date_str}")

    # Destination : le salon Bilan Candidats spécifié
    salon_dest = bot.get_channel(SALON_BILAN_CANDIDATS_ID) or interaction.channel

    if len(rapport_bilan) > 3900:
        await salon_dest.send(f"📊 **RAPPORT COMPLET — {candidat.mention}**")
        for chunk in decouper_texte_intelligent(rapport_bilan, limite=1900):
            await salon_dest.send(chunk)
            await asyncio.sleep(0.3)
    else:
        await salon_dest.send(embed=embed)

    await interaction.followup.send(
        f"✅ **Bilan généré avec succès !** Le rapport a été envoyé dans {salon_dest.mention}.",
        ephemeral=True
    )

# ========================================================
# 31. BILAN & AUDIT ORGANISATEUR (NOTE, CRITÈRES & BÊTISIER)
# ========================================================

async def generer_bilan_orga_ia(orga_nom: str, messages_orga: list[str], messages_sur_orga: list[str]) -> str:
    """Génère l'audit et le bilan complet d'un membre du staff sur le serveur Discord."""
    texte_dits = "\n".join(messages_orga) if messages_orga else "Aucun message direct trouvé."
    texte_sur_lui = "\n".join(messages_sur_orga) if messages_sur_orga else "Aucun message parlant de cet orga trouvé."

    prompt = (
        "Tu es le superviseur général, auditeur impartial et juge suprême d'une équipe d'organisation d'un jeu communautaire sur Discord.\n"
        f"🎯 MEMBRE DU STAFF ÉVALUÉ : **{orga_nom}**\n\n"
        "=== CE QUE L'ORGA A FAIT / DIT (SALONS STAFF, ÉPREUVES, ANNONCES, GESTION, DUOS) ===\n"
        f"{texte_dits[:20000]}\n\n"
        "=== CE QUE LES AUTRES (STAFF & CANDIDATS) ONT DIT SUR LUI ===\n"
        f"{texte_sur_lui[:20000]}\n\n"
        "DIRECTIVES D'ÉVALUATION DISCORD :\n"
        "1. Contexte 100% Discord : Animation de salons, gestion des bots/règles, présence en vocal/texte, arbitrage des litiges et écriture des épreuves.\n"
        "2. Ton : Juste, constructif, sans complaisance mais teinté d'humour bienveillant.\n\n"
        "STRUCTURE STRICTE DU RAPPORT ATTENDUE :\n\n"
        f"## 🏆 1. LA NOTE GLOBALE DU STAFF : [NOTE/10] — [TITRE ÉVOCATEUR]\n"
        "- Justification synthétique de la note en 2 phrases denses.\n\n"
        "## 📊 2. ÉVALUATION PAR CRITÈRES CLÉS\n"
        "- ⚖️ **Objectivité & Impartialité :** (Arbitrage neutre, gestion des drama, équité envers les tribus)\n"
        "- ⏱️ **Présence & Réactivité :** (Disponibilité lors des épreuves, tenue des délais, régularité sur le serveur)\n"
        "- 💡 **Apport Logistique & Créativité :** (Conception des jeux, gestion technique, animation de l'ambiance, idées apportées)\n\n"
        "## 🛠️ 3. BILAN DE COMPORTEMENT\n"
        "- 🟢 **Points forts / Masterclasses :** (2 bullet points sur ses réussites majeures)\n"
        "- 🔴 **Axes d'amélioration / Fails :** (2 bullet points sur ses retards, moments de flemme ou égarements)\n\n"
        "## 🤡 4. LE BÊTISIER DE L'ORGA (PERLES & MOMENTS LUNAIRES)\n"
        "- Relève 3 à 5 citations drôles, moments de panique en coulisses, messages incompréhensibles ou gaffes commises sur le serveur.\n\n"
        "Reste structuré, percutant et lisible."
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        return f"❌ Erreur lors de l'analyse IA de l'orga : {e}"


@bot.tree.command(
    name="bilan_orga",
    description="Bilan complet d'un orga : Note sur 10, analyse des critères clés et bêtisier."
)
@app_commands.describe(
    orga="Le membre du staff à évaluer",
    limite_par_salon="Nombre de messages récents à scanner par salon (par défaut : 80)"
)
@app_commands.check(est_orga_ou_admin)
async def bilan_orga(
    interaction: discord.Interaction,
    orga: discord.Member,
    limite_par_salon: int = 80
):
    # ... (le reste de la fonction reste identique)
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    nom_orga_clean = nettoyer_texte(orga.display_name)
    pseudo_global_clean = nettoyer_texte(orga.name)
    mention_id = str(orga.id)

    messages_orga = []
    messages_sur_orga = []

    await interaction.followup.send(
        f"⏳ **Audit et collecte des actions de {orga.mention} en cours...**\n"
        f"*(Scan complet des salons staff, épreuves, gestion et salons de jeu)*",
        ephemeral=True
    )

    # Scan de tous les salons textuels (y compris catégories staff/organisation)
    for channel in guild.text_channels:
        if channel.name.startswith("🔒arch-"):
            continue

        try:
            async for msg in channel.history(limit=limite_par_salon, oldest_first=False):
                if msg.author.bot:
                    continue

                contenu = msg.content.strip()
                if not contenu:
                    continue

                # 1. Message écrit PAR l'orga
                if msg.author.id == orga.id:
                    messages_orga.append(f"[#{channel.name}] {orga.display_name}: {contenu}")

                # 2. Message écrit PAR UN AUTRE parlant de cet orga
                else:
                    contenu_clean = nettoyer_texte(contenu)
                    if (
                        nom_orga_clean in contenu_clean 
                        or pseudo_global_clean in contenu_clean 
                        or mention_id in msg.content
                    ):
                        messages_sur_orga.append(f"[#{channel.name}] {msg.author.display_name} sur {orga.display_name}: {contenu}")

        except Exception:
            continue

    if not messages_orga and not messages_sur_orga:
        await interaction.followup.send(f"⚠️ Aucune trace d'activité trouvée pour {orga.mention}.", ephemeral=True)
        return

    # Analyse Gemini
    rapport_orga = await generer_bilan_orga_ia(
        orga_nom=orga.display_name,
        messages_orga=messages_orga[:150],
        messages_sur_orga=messages_sur_orga[:150]
    )

    date_str = datetime.datetime.now(ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y à %H:%M")
    embed = discord.Embed(
        title=f"🛠️ BILAN D'ORGANISATEUR — {orga.display_name.upper()}",
        description=rapport_orga if len(rapport_orga) <= 3900 else None,
        color=discord.Color.red()
    )
    embed.set_thumbnail(url=orga.display_avatar.url)
    embed.set_footer(text=f"Audit Staff Interne • Demandé par {interaction.user.display_name} • {date_str}")

    salon_dest = (
        bot.get_channel(SALON_BILAN_ORGAS_ID)
        or bot.get_channel(SALON_PRESENTATION_ORGAS_ID)
        or interaction.channel
    )

    if len(rapport_orga) > 3900:
        await salon_dest.send(f"🛠️ **AUDIT COMPLET DE L'ORGANISATEUR — {orga.mention}**")
        for chunk in decouper_texte_intelligent(rapport_orga, limite=1900):
            await salon_dest.send(chunk)
            await asyncio.sleep(0.3)
    else:
        await salon_dest.send(embed=embed)

    await interaction.followup.send(
        f"✅ **Bilan orga généré avec succès !** Le rapport a été transmis dans {salon_dest.mention}.",
        ephemeral=True
    )

# ========================================================
# 32. SALONS D'ANNONCES & FOCUS CANDIDAT (LECTURE SEULE)
# ========================================================

@bot.tree.command(
    name="creer_salon_annonces_groupe",
    description="Crée un salon textuel privé d'épreuve où les candidats choisis sont en lecture seule."
)
@app_commands.describe(
    nom_salon="Nom du salon (ex: annonces-epreuve-1, briefing-orientation)",
    candidat_1="1er candidat",
    candidat_2="2ème candidat",
    candidat_3="3ème candidat (optionnel)",
    candidat_4="4ème candidat (optionnel)",
    candidat_5="5ème candidat (optionnel)",
    candidat_6="6ème candidat (optionnel)",
    candidat_7="7ème candidat (optionnel)",
    candidat_8="8ème candidat (optionnel)",
    candidat_9="9ème candidat (optionnel)",
    candidat_10="10ème candidat (optionnel)"
)
@app_commands.check(est_orga_ou_admin)
async def creer_salon_annonces_groupe(
    interaction: discord.Interaction,
    nom_salon: str,
    candidat_1: discord.Member,
    candidat_2: discord.Member,
    candidat_3: discord.Member = None,
    candidat_4: discord.Member = None,
    candidat_5: discord.Member = None,
    candidat_6: discord.Member = None,
    candidat_7: discord.Member = None,
    candidat_8: discord.Member = None,
    candidat_9: discord.Member = None,
    candidat_10: discord.Member = None
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    categorie = guild.get_channel(CATEGORY_EPREUVE_ID)
    if not categorie or not isinstance(categorie, discord.CategoryChannel):
        await interaction.followup.send(f"❌ Catégorie Épreuves introuvable (ID: `{CATEGORY_EPREUVE_ID}`).", ephemeral=True)
        return

    participants = [c for c in [candidat_1, candidat_2, candidat_3, candidat_4, candidat_5, candidat_6, candidat_7, candidat_8, candidat_9, candidat_10] if c is not None]
    participants = list(set(participants))

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False, read_messages=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, read_messages=True, send_messages=True)
    }

    perms_lecture_seule = discord.PermissionOverwrite(
        view_channel=True,
        read_messages=True,
        read_message_history=True,
        send_messages=False,
        send_messages_in_threads=False,
        create_public_threads=False,
        create_private_threads=False,
        add_reactions=False
    )

    for p in participants:
        r_perso = trouver_role_personnel(p)
        cible_perm = r_perso if r_perso else p
        overwrites[cible_perm] = perms_lecture_seule

    if role_spectateurs:
        overwrites[role_spectateurs] = get_spectateur_overwrites()

    if role_orgas:
        overwrites[role_orgas] = discord.PermissionOverwrite(
            view_channel=True, read_messages=True, read_message_history=True, send_messages=True
        )

    nom_salon_clean = formater_nom_salon(nom_salon)
    salon_cree = await guild.create_text_channel(
        name=f"📢・{nom_salon_clean}",
        category=categorie,
        overwrites=overwrites
    )

    mentions = ", ".join([p.mention for p in participants])
    await salon_cree.send(
        f"📢 **SALON D'ANNONCES & CONSIGNES**\n"
        f"Bienvenue {mentions}.\n"
        f"*(Ce salon est configuré en lecture seule pour les candidats)*"
    )

    await interaction.followup.send(
        f"✅ **Salon d'annonces créé :** {salon_cree.mention} dans **{categorie.name}**\n"
        f"👥 **Candidats en lecture seule ({len(participants)}) :** {mentions}",
        ephemeral=True
    )


@bot.tree.command(
    name="creer_salon_focus_candidat",
    description="Crée un salon d'épreuve où 1 candidat écrit et les autres sont en lecture seule."
)
@app_commands.describe(
    nom_salon="Nom du salon (ex: passage-lucas, epreuve-sarah)",
    candidat_actif="Le candidat qui A LE DROIT D'ÉCRIRE",
    observateur_1="1er candidat observateur (lecture seule)",
    observateur_2="2ème candidat observateur (optionnel)",
    observateur_3="3ème candidat observateur (optionnel)",
    observateur_4="4ème candidat observateur (optionnel)",
    observateur_5="5ème candidat observateur (optionnel)",
    observateur_6="6ème candidat observateur (optionnel)",
    observateur_7="7ème candidat observateur (optionnel)",
    observateur_8="8ème candidat observateur (optionnel)",
    observateur_9="9ème candidat observateur (optionnel)"
)
@app_commands.check(est_orga_ou_admin)
async def creer_salon_focus_candidat(
    interaction: discord.Interaction,
    nom_salon: str,
    candidat_actif: discord.Member,
    observateur_1: discord.Member,
    observateur_2: discord.Member = None,
    observateur_3: discord.Member = None,
    observateur_4: discord.Member = None,
    observateur_5: discord.Member = None,
    observateur_6: discord.Member = None,
    observateur_7: discord.Member = None,
    observateur_8: discord.Member = None,
    observateur_9: discord.Member = None
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    categorie = guild.get_channel(CATEGORY_EPREUVE_ID)
    if not categorie or not isinstance(categorie, discord.CategoryChannel):
        await interaction.followup.send(f"❌ Catégorie Épreuves introuvable (ID: `{CATEGORY_EPREUVE_ID}`).", ephemeral=True)
        return

    observateurs = [c for c in [observateur_1, observateur_2, observateur_3, observateur_4, observateur_5, observateur_6, observateur_7, observateur_8, observateur_9] if c is not None and c.id != candidat_actif.id]
    observateurs = list(set(observateurs))

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False, read_messages=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, read_messages=True, send_messages=True)
    }

    # Candidat actif
    r_actif = trouver_role_personnel(candidat_actif)
    cible_actif = r_actif if r_actif else candidat_actif
    overwrites[cible_actif] = discord.PermissionOverwrite(
        view_channel=True,
        read_messages=True,
        read_message_history=True,
        send_messages=True
    )

    # Observateurs
    perms_lecture_seule = discord.PermissionOverwrite(
        view_channel=True,
        read_messages=True,
        read_message_history=True,
        send_messages=False,
        send_messages_in_threads=False,
        create_public_threads=False,
        create_private_threads=False,
        add_reactions=False
    )

    for obs in observateurs:
        r_obs = trouver_role_personnel(obs)
        cible_obs = r_obs if r_obs else obs
        overwrites[cible_obs] = perms_lecture_seule

    if role_spectateurs:
        overwrites[role_spectateurs] = get_spectateur_overwrites()

    if role_orgas:
        overwrites[role_orgas] = discord.PermissionOverwrite(
            view_channel=True, read_messages=True, read_message_history=True, send_messages=True
        )

    nom_salon_clean = formater_nom_salon(nom_salon)
    salon_cree = await guild.create_text_channel(
        name=f"🎯・{nom_salon_clean}",
        category=categorie,
        overwrites=overwrites
    )

    mentions_obs = ", ".join([o.mention for o in observateurs]) if observateurs else "*Aucun*"
    await salon_cree.send(
        f"🎯 **SALON ÉPREUVE — PASSAGE DE {candidat_actif.mention}**\n"
        f"✍️ **Candidat actif :** {candidat_actif.mention} *(peut écrire)*\n"
        f"👁️ **Observateurs :** {mentions_obs} *(lecture seule)*"
    )

    await interaction.followup.send(
        f"✅ **Salon focus créé :** {salon_cree.mention} dans **{categorie.name}**\n"
        f"✍️ **Écriture :** {candidat_actif.mention}\n"
        f"👁️ **Lecture seule ({len(observateurs)}) :** {mentions_obs}",
        ephemeral=True
    )

# ========================================================
# 33. MINUTEUR INTELLIGENT (AVEC TEMPS ADDITIONNEL & STOP)
# ========================================================

# Suivi des minuteurs actifs : {channel_id: {"task": asyncio.Task, "start_perf": float, "start_dt": datetime, "total_seconds": int}}
MINUTEURS_ACTIFS = {}


async def boucle_minuteur(channel: discord.TextChannel, total_seconds: int):
    """Gère le compte à rebours, bascule en temps additionnel puis alerte toutes les 2 min."""
    start_time = time.perf_counter()

    # 1. Alertes programmées pendant le temps réglementaire
    alertes_programmees = []

    # Alerte mi-temps (si l'épreuve dure au moins 60s)
    mi_temps = total_seconds // 2
    if mi_temps > 60:
        minutes_restantes = mi_temps // 60
        alertes_programmees.append((mi_temps, f"⏳ **MI-TEMPS ÉCOULÉE !** Il vous reste **{minutes_restantes} minute(s)**."))

    # Alerte 5 minutes restantes
    if total_seconds > 300:
        alertes_programmees.append((300, "⚠️ **ATTENTION :** Il ne reste plus que **5 minutes** !"))

    # Alertes dernière minute
    if total_seconds >= 60:
        alertes_programmees.append((60, "🚨 **DERNIÈRE MINUTE !** Plus que **60 secondes** !"))
    if total_seconds >= 30:
        alertes_programmees.append((30, "⏱️ **30 secondes restantes !**"))
    if total_seconds >= 10:
        alertes_programmees.append((10, "⚡ **10 secondes !**"))

    for s in [5, 4, 3, 2, 1]:
        if total_seconds >= s:
            alertes_programmees.append((s, f"🔥 **{s}...**"))

    alertes_programmees.sort(key=lambda x: x[0], reverse=True)

    try:
        # Déroulement du temps réglementaire
        for t_restant, texte_alerte in alertes_programmees:
            temps_ecoule = time.perf_counter() - start_time
            temps_a_attendre = (total_seconds - t_restant) - temps_ecoule

            if temps_a_attendre > 0:
                await asyncio.sleep(temps_a_attendre)
                await channel.send(texte_alerte)

        # Attente jusqu'à la fin exacte (0s)
        temps_restant_reglementaire = total_seconds - (time.perf_counter() - start_time)
        if temps_restant_reglementaire > 0:
            await asyncio.sleep(temps_restant_reglementaire)

        # 2. Passage en Temps Additionnel
        embed_extra = discord.Embed(
            title="⏱️ TEMPS IMPARTI ÉCOULÉ — TEMPS ADDITIONNEL !",
            description=(
                "🚨 **Le temps réglementaire est terminé !**\n\n"
                "Le chronomètre continue de tourner en **temps additionnel** jusqu'à ce que le staff utilise `/chrono_minuteur_stop`."
            ),
            color=discord.Color.orange()
        )
        await channel.send(embed=embed_extra)

        # 3. Boucle d'overtime : alerte toutes les 2 minutes
        minutes_extra = 0
        while True:
            await asyncio.sleep(120)
            minutes_extra += 2
            
            ecoule_total = time.perf_counter() - start_time
            min_totales = int(ecoule_total // 60)
            sec_totales = int(ecoule_total % 60)

            await channel.send(
                f"⏱️ **TEMPS ADDITIONNEL (+{minutes_extra} min)** — Chrono global : `{min_totales} min {sec_totales}s`"
            )

    except asyncio.CancelledError:
        pass
    finally:
        pass


@bot.tree.command(
    name="chrono_minuteur",
    description="Lance un minuteur avec alertes, temps additionnel automatique et suivi en direct."
)
@app_commands.describe(
    minutes="Durée initiale de l'épreuve en minutes (ex: 15, 20, 30)",
    epreuve="Optionnel : nom ou intitulé de l'épreuve",
    salon="Optionnel : salon ciblé (par défaut : salon actuel)"
)
@app_commands.check(est_orga_ou_admin)
async def chrono_minuteur(
    interaction: discord.Interaction,
    minutes: int,
    epreuve: str = "Épreuve",
    salon: discord.TextChannel = None
):
    channel = salon or interaction.channel

    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("❌ Le minuteur ne peut être lancé que dans un salon textuel.", ephemeral=True)
        return

    if minutes <= 0:
        await interaction.response.send_message("❌ La durée doit être supérieure à 0 minute.", ephemeral=True)
        return

    if channel.id in MINUTEURS_ACTIFS:
        await interaction.response.send_message(
            f"⚠️ Un minuteur est déjà en cours dans {channel.mention}. Utilise `/chrono_minuteur_stop` d'abord.",
            ephemeral=True
        )
        return

    total_seconds = minutes * 60
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    fin_timestamp = int((now_utc + datetime.timedelta(seconds=total_seconds)).timestamp())

    embed_start = discord.Embed(
        title="⏱️ TOP DÉPART DU MINUTEUR !",
        description=(
            f"🎯 **Épreuve :** {epreuve}\n"
            f"⏳ **Temps réglementaire :** `{minutes} minute(s)`\n\n"
            f"🏁 **Fin du temps imparti :** <t:{fin_timestamp}:R> *(à <t:{fin_timestamp}:T>)*\n\n"
            "*(Alertes à la mi-temps, à 5 min, décompte final, puis bascule automatique en temps additionnel)*"
        ),
        color=discord.Color.green()
    )
    embed_start.set_footer(text="Que le meilleur gagne !")

    task = asyncio.create_task(boucle_minuteur(channel, total_seconds))
    MINUTEURS_ACTIFS[channel.id] = {
        "task": task,
        "start_perf": time.perf_counter(),
        "start_dt": datetime.datetime.now(),
        "total_seconds": total_seconds
    }

    await interaction.response.send_message(f"✅ Minuteur de **{minutes} min** lancé dans {channel.mention}.", ephemeral=True)
    await channel.send(embed=embed_start)


@bot.tree.command(
    name="chrono_minuteur_stop",
    description="Arrête le minuteur/temps additionnel et valide le temps complet depuis le début."
)
@app_commands.describe(salon="Optionnel : salon où arrêter le minuteur (par défaut : salon actuel)")
@app_commands.check(est_orga_ou_admin)
async def chrono_minuteur_stop(interaction: discord.Interaction, salon: discord.TextChannel = None):
    channel = salon or interaction.channel

    if channel.id not in MINUTEURS_ACTIFS:
        await interaction.response.send_message(
            f"❌ Aucun minuteur actif dans {channel.mention}.",
            ephemeral=True
        )
        return

    minuteur = MINUTEURS_ACTIFS.pop(channel.id)
    minuteur["task"].cancel()

    # Calcul de la durée exacte
    duree_totale = time.perf_counter() - minuteur["start_perf"]
    temps_prevu = minuteur["total_seconds"]

    min_totales = int(duree_totale // 60)
    sec_totales = round(duree_totale % 60, 2)
    texte_total = f"{min_totales} min {sec_totales} s" if min_totales > 0 else f"{sec_totales} s"

    description_stop = (
        f"🛑 **L'épreuve est terminée !**\n\n"
        f"⏱️ **TEMPS TOTAL CUMULÉ :** `{texte_total}` *(Précision : {round(duree_totale, 2)}s)*\n"
        f"⏳ **Temps réglementaire prévu :** `{temps_prevu // 60} minute(s)`\n"
    )

    # Si le candidat a dépassé le temps imparti
    if duree_totale > temps_prevu:
        overtime_sec = duree_totale - temps_prevu
        min_over = int(overtime_sec // 60)
        sec_over = round(overtime_sec % 60, 2)
        texte_over = f"+{min_over} min {sec_over} s" if min_over > 0 else f"+{sec_over} s"
        description_stop += f"🚨 **Dépassement (Temps additionnel) :** `{texte_over}`\n"

    embed_stop = discord.Embed(
        title="🏁 MINUTEUR ARRÊTÉ — RÉSULTATS OFFICIELS",
        description=description_stop,
        color=discord.Color.gold()
    )
    embed_stop.set_footer(text=f"Arrêté par {interaction.user.display_name}")

    await channel.send(embed=embed_stop)
    await interaction.response.send_message(f"✅ Minuteur arrêté dans {channel.mention} (Temps total : `{texte_total}`).", ephemeral=True)

from collections import Counter, defaultdict

# Mots vides courants à ignorer pour ne garder que les mots significatifs
MOTS_VIDES_FR = {
    "le", "la", "les", "un", "une", "des", "du", "de", "d", "l", "au", "aux",
    "et", "ou", "mais", "donc", "car", "ni", "or", "si", "que", "qui", "quoi",
    "dont", "ou", "quand", "comment", "pourquoi", "est", "sont", "a", "ont",
    "ai", "as", "suis", "es", "etre", "avoir", "faire", "fait", "je", "tu",
    "il", "elle", "on", "nous", "vous", "ils", "elles", "me", "te", "se",
    "lui", "leur", "y", "en", "ce", "cet", "cette", "ces", "mon", "ton",
    "son", "ma", "ta", "sa", "mes", "tes", "ses", "notre", "votre", "nos",
    "vos", "pour", "dans", "sur", "par", "avec", "sans", "sous", "vers",
    "chez", "tout", "tous", "toute", "toutes", "plus", "moins", "tres",
    "bien", "aussi", "trop", "peu", "pas", "ne", "non", "oui", "ca", "cest",
    "c", "va", "vais", "vas", "vont", "meme", "comme", "alors", "apres", "avant"
}

@bot.tree.command(
    name="stats_mots_candidats",
    description="Top des mots les plus prononcés par les candidats avec la répartition exacte."
)
@app_commands.describe(
    top="Nombre de mots à afficher (par défaut : 10)",
    longueur_min="Taille minimale d'un mot (par défaut : 4 lettres)",
    role_equipe="Optionnel : filtrer uniquement une équipe"
)
@app_commands.check(est_orga_ou_admin)
async def stats_mots_candidats(
    interaction: discord.Interaction,
    top: int = 10,
    longueur_min: int = 4,
    role_equipe: discord.Role = None
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    candidats_ids = set()
    noms_candidats = {}

    # 1. Identification des candidats
    async for member in guild.fetch_members(limit=None):
        if member.bot:
            continue
        if role_equipe and role_equipe not in member.roles:
            continue
        if role_orgas and role_orgas in member.roles:
            continue
        if role_spectateurs and role_spectateurs in member.roles and not role_equipe:
            continue

        candidats_ids.add(member.id)
        noms_candidats[member.id] = member.display_name

    if not candidats_ids:
        await interaction.followup.send("❌ Aucun candidat trouvé pour l'analyse.", ephemeral=True)
        return

    await interaction.followup.send(
        f"⏳ **Analyse lexicale en cours sur {len(candidats_ids)} candidat(s)...**",
        ephemeral=True
    )

    # 2. Collecte & Comptage des mots
    compteur_global = Counter()
    auteurs_par_mot = defaultdict(lambda: Counter())

    for channel in guild.text_channels:
        est_salon_log = (channel.name.lower() == "log-deplacements")
        if not est_categorie_candidate(channel.category) and not est_salon_log:
            continue
        if channel.name.startswith("🔒arch-"):
            continue

        try:
            async for msg in channel.history(limit=500, oldest_first=False):
                if msg.author.id in candidats_ids and msg.content.strip():
                    # Nettoyage : retire ponctuation, liens et accents
                    texte_propre = re.sub(r'https?://\S+', '', msg.content)
                    texte_propre = nettoyer_texte(texte_propre)
                    
                    mots = texte_propre.split()
                    for m in mots:
                        if len(m) >= longueur_min and m not in MOTS_VIDES_FR and not m.isdigit():
                            compteur_global[m] += 1
                            auteurs_par_mot[m][msg.author.id] += 1
        except Exception:
            continue

    if not compteur_global:
        await interaction.followup.send("⚠️ Aucun mot significatif trouvé dans les salons analysés.", ephemeral=True)
        return

    # 3. Formatage des résultats
    top_mots = compteur_global.most_common(min(top, 25))
    lignes = []

    for rank, (mot, total) in enumerate(top_mots, 1):
        # Récupération des plus gros utilisateurs de ce mot
        auteurs = auteurs_par_mot[mot].most_common(3)
        details_auteurs = ", ".join([f"**{noms_candidats.get(uid, 'Inconnu')}** ({c})" for uid, c in auteurs])

        medaille = "🥇" if rank == 1 else ("🥈" if rank == 2 else ("🥉" if rank == 3 else f"`#{rank}`"))
        lignes.append(
            f"{medaille} **{mot.upper()}** — **{total} fois**\n"
            f"   └ 👤 *Utilisé par :* {details_auteurs}"
        )

    embed = discord.Embed(
        title="📊 TOP DES MOTS LES PLUS PRONONCÉS",
        description="\n\n".join(lignes),
        color=discord.Color.purple()
    )
    if role_equipe:
        embed.set_author(name=f"Filtre : {role_equipe.name}")
    embed.set_footer(text=f"Mots d'au moins {longueur_min} lettres • Stop-words exclus")

    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(
    name="chercher_mot_candidats",
    description="Recherche un mot précis chez les candidats et affiche publiquement le classement."
)
@app_commands.describe(
    mot="Le mot ou l'expression exacte à rechercher (ex: alliance, trahison, vote)",
    role_equipe="Optionnel : filtrer uniquement les candidats d'une équipe"
)
@app_commands.check(est_orga_ou_admin)
async def chercher_mot_candidats(
    interaction: discord.Interaction,
    mot: str,
    role_equipe: discord.Role = None
):
    # ephemeral=False pour que la réponse finale soit visible par tout le monde sur le salon
    await interaction.response.defer(ephemeral=False)
    guild = interaction.guild

    mot_recherche_clean = nettoyer_texte(mot)
    if not mot_recherche_clean:
        await interaction.followup.send("❌ Veuillez spécifier un mot valide.", ephemeral=True)
        return

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    candidats_ids = set()
    noms_candidats = {}

    # 1. Récupération des candidats
    async for member in guild.fetch_members(limit=None):
        if member.bot:
            continue
        if role_equipe and role_equipe not in member.roles:
            continue
        if role_orgas and role_orgas in member.roles:
            continue
        if role_spectateurs and role_spectateurs in member.roles and not role_equipe:
            continue

        candidats_ids.add(member.id)
        noms_candidats[member.id] = {
            "nom": member.display_name,
            "mention": member.mention,
            "count": 0
        }

    if not candidats_ids:
        await interaction.followup.send("❌ Aucun candidat trouvé pour cette analyse.", ephemeral=True)
        return

    total_occurrences = 0

    # Pattern regex pour cibler le mot exact (évite les faux positifs au milieu d'un autre mot)
    pattern = re.compile(rf'\b{re.escape(mot_recherche_clean)}\b', re.IGNORECASE)

    # 2. Scan des salons de jeu
    for channel in guild.text_channels:
        est_salon_log = (channel.name.lower() == "log-deplacements")
        if not est_categorie_candidate(channel.category) and not est_salon_log:
            continue
        if channel.name.startswith("🔒arch-"):
            continue

        try:
            async for msg in channel.history(limit=500, oldest_first=False):
                if msg.author.id in candidats_ids and msg.content.strip():
                    texte_propre = nettoyer_texte(msg.content)
                    occurrences = len(pattern.findall(texte_propre))
                    
                    if occurrences > 0:
                        noms_candidats[msg.author.id]["count"] += occurrences
                        total_occurrences += occurrences
        except Exception:
            continue

    # 3. Tri et mise en page du classement
    candidats_actifs = [c for c in noms_candidats.values() if c["count"] > 0]
    candidats_actifs.sort(key=lambda x: x["count"], reverse=True)

    if total_occurrences == 0:
        embed_vide = discord.Embed(
            title=f"🔍 RECHERCHE : « {mot.upper()} »",
            description=f"Le mot **« {mot} »** n'a **jamais été prononcé** par les candidats dans les salons de jeu.",
            color=discord.Color.dark_grey()
        )
        await interaction.followup.send(embed=embed_vide)
        return

    lignes_resultats = []
    for i, c in enumerate(candidats_actifs, 1):
        icone = "🥇" if i == 1 else ("🥈" if i == 2 else ("🥉" if i == 3 else f"`#{i}`"))
        lignes_resultats.append(f"{icone} **{c['nom']}** ({c['mention']}) : **{c['count']}** fois")

    description = (
        f"🎯 **Mot recherché :** `« {mot} »`\n"
        f"💬 **Occurrences totales :** **{total_occurrences} fois**\n"
        f"👥 **Candidats l'ayant prononcé :** `{len(candidats_actifs)}/{len(candidats_ids)}`\n"
    )
    if role_equipe:
        description += f"🛡️ **Équipe :** {role_equipe.mention}\n"

    description += "\n━━━━━━━━━━━━━━━━━━━━━━\n\n" + "\n".join(lignes_resultats)

    embed = discord.Embed(
        title=f"🔍 CLASSEMENT DU MOT : « {mot.upper()} »",
        description=description if len(description) <= 3900 else description[:3900] + "\n...",
        color=discord.Color.gold()
    )
    embed.set_footer(text=f"Recherche effectuée par {interaction.user.display_name} • Salons de jeu scannés")

    # Envoi public sur le salon
    await interaction.followup.send(embed=embed)

async def decoder_charabia_ia(nom_membre: str, texte_brut: str) -> str:
    """Traduit une phrase incompréhensible avec humour, emphase et second degré."""
    prompt = (
        "Tu es un linguiste d'élite, anthropologue du futur et expert en déchiffrage de dialectes cosmiques.\n"
        f"Un utilisateur sur Discord nommé **{nom_membre}** vient d'envoyer ce message lunaire / difficile à comprendre :\n"
        f"\"\"\"{texte_brut}\"\"\"\n\n"
        "TON RÔLE :\n"
        "Rédige une TRADUCTION / EXPLICATION HILARANTE de ce qu'il a voulu dire en français intelligible.\n\n"
        "DIRECTIVES D'HUMOUR :\n"
        "1. SECOND DEGRÉ & BIENVEILLANCE : C'est du chambrage amical entre potes sur Discord.\n"
        "2. FORME : Structure ta réponse de façon courte et rythmée (2 à 3 lignes max) :\n"
        "   - 🗣️ **Traduction littérale :** (Ce que son cerveau a tenté d'exprimer avec des mots humains)\n"
        "   - 🔬 **Analyse sémiotique :** (Pourquoi c'est sorti de façon aussi chaotique ou obscure)\n"
        "3. Renvoie UNIQUEMENT le texte formaté, sans formule de politesse."
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        return f"🗣️ **Traduction d'urgence :** Le message est tellement cryptique que même les serveurs quantiques ont planté ({e})."

# ========================================================
# 34. COMMANDES DU DÉCODEUR / TRADUCTEUR HUMORISTIQUE
# ========================================================

@bot.tree.command(
    name="lancer_decodeur",
    description="Active la traduction automatique humoristique à chaque message d'un membre."
)
@app_commands.describe(
    cible="Le spectateur ou membre qui parle en dialecte alien",
    salon="Optionnel : salon surveillé (par défaut : salon actuel)"
)
@app_commands.check(est_orga_ou_admin)
async def lancer_decodeur(
    interaction: discord.Interaction,
    cible: discord.Member,
    salon: discord.TextChannel = None
):
    channel = salon or interaction.channel

    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("❌ Cette commande ne fonctionne que dans un salon textuel.", ephemeral=True)
        return

    SESSIONS_DECODEUR[channel.id] = {
        "user_id": cible.id,
        "nom": cible.display_name
    }

    embed_activation = discord.Embed(
        title="🌐 DÉCODEUR UNIVERSEL ACTIVÉ !",
        description=(
            f"📡 **Cible verrouillée :** {cible.mention}\n\n"
            "Chacune de ses interventions dans ce salon sera désormais traduite et explicitée "
            "en direct par le laboratoire linguistique officiel.\n\n"
            "*(Pour désactiver : `/arreter_decodeur`)*"
        ),
        color=discord.Color.teal()
    )
    await channel.send(embed=embed_activation)
    await interaction.response.send_message(f"✅ Décodeur activé sur {cible.mention} dans {channel.mention}.", ephemeral=True)


@bot.tree.command(
    name="arreter_decodeur",
    description="Désactive la traduction automatique pour ce salon."
)
@app_commands.describe(salon="Optionnel : salon à libérer (par défaut : salon actuel)")
@app_commands.check(est_orga_ou_admin)
async def arreter_decodeur(interaction: discord.Interaction, salon: discord.TextChannel = None):
    channel = salon or interaction.channel

    if channel.id in SESSIONS_DECODEUR:
        cible = SESSIONS_DECODEUR.pop(channel.id)
        await channel.send(f"🔌 **Décodeur universel désactivé.** {cible['nom']} peut à nouveau s'exprimer sans filtre.")
        await interaction.response.send_message(f"✅ Décodeur désactivé dans {channel.mention}.", ephemeral=True)
    else:
        await interaction.response.send_message(f"ℹ️ Aucun décodeur n'est actif dans {channel.mention}.", ephemeral=True)


# ========================================================
# 35. GÉNÉRATEUR DE FAUSSE UNE DE JOURNAL (STYLE L'ÉQUIPE)
# ========================================================

async def extraire_articles_une_ia(transcriptions: str) -> dict:
    """Demande à Gemini de structurer les gros titres satiriques du journal."""
    prompt = (
        "Tu es le rédacteur en chef satirique d'un grand journal sportif (type L'Équipe / Le Gorafi) qui couvre une aventure Discord.\n"
        "Voici les échanges et événements de la journée sur le serveur :\n"
        f"\"\"\"{transcriptions[:25000]}\"\"\"\n\n"
        "Rédige le contenu pour la UNE DU JOURNAL du jour avec ironie, punchlines et second degré.\n\n"
        "FORMAT DE RÉPONSE OBLIGATOIRE (STRICT SANS INTRO) :\n"
        "TITRE_PRINCIPAL: <Gros titre en 3 à 6 mots percutants>\n"
        "SOUS_TITRE_PRINCIPAL: <Texte explicatif du gros événement du jour en 2 phrases>\n"
        "CANDIDAT_PRINCIPAL: <Prénom du joueur au centre de l'événement principal>\n"
        "ENCART_1_CATEGORIE: <Catégorie en 1 mot, ex: VENGEANCE, INTERNET, REBONDISSEMENT>\n"
        "ENCART_1_TITRE: <Citation ou titre drôle en 1 phrase>\n"
        "ENCART_1_CANDIDAT: <Prénom du candidat concerné>\n"
        "ENCART_2_CATEGORIE: <Catégorie en 1 mot, ex: DÉBATS, STRATÉGIE, DINGUERIE>\n"
        "ENCART_2_TITRE: <Citation ou titre drôle en 1 phrase>\n"
        "ENCART_2_CANDIDAT: <Prénom du candidat concerné>\n"
        "ENCART_3_CATEGORIE: <Catégorie en 1 mot, ex: ANIMATEURS, COULISSES, LE SAVIEZ-VOUS>\n"
        "ENCART_3_TITRE: <Citation ou fail de l'orga ou punchline spectateur>\n"
        "ENCART_3_CANDIDAT: <Prénom de la personne concernée>"
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt
        )
        texte = response.text.strip()

        data = {}
        for ligne in texte.split("\n"):
            if ":" in ligne:
                cle, val = ligne.split(":", 1)
                data[cle.strip()] = val.strip()

        return data
    except Exception as e:
        print(f"Erreur extraction Une IA : {e}")
        return {
            "TITRE_PRINCIPAL": "C'EST ENCORE LOUPE !",
            "SOUS_TITRE_PRINCIPAL": "Une journée pleine de rebondissements et de mauvaises décisions sur le serveur.",
            "CANDIDAT_PRINCIPAL": "Aventurier",
            "ENCART_1_CATEGORIE": "RUMEURS",
            "ENCART_1_TITRE": "« Je pensais que mon alliance était solide... »",
            "ENCART_1_CANDIDAT": "Candidat",
            "ENCART_2_CATEGORIE": "PERLE",
            "ENCART_2_TITRE": "« Si je sors ce soir, je porte plainte contre le bot. »",
            "ENCART_2_CANDIDAT": "Candidat",
            "ENCART_3_CATEGORIE": "STAFF",
            "ENCART_3_TITRE": "« L'épreuve était trop facile, on va sévir demain. »",
            "ENCART_3_CANDIDAT": "Orga"
        }


def creer_image_une(donnees_une: dict, photos_candidats: dict) -> io.BytesIO:
    largeur, hauteur = 900, 1300
    fond = Image.new("RGB", (largeur, hauteur), color="#FFFFFF")
    draw = ImageDraw.Draw(fond)

    # 1. Chargement des polices téléchargées
    try:
        font_logo = ImageFont.truetype("fonts/Anton-Regular.ttf", 62)
        font_gros_titre = ImageFont.truetype("fonts/Anton-Regular.ttf", 46)
        font_rubrique = ImageFont.truetype("fonts/Roboto-Bold.ttf", 16)
        font_nom_gras = ImageFont.truetype("fonts/Roboto-Bold.ttf", 15)
        font_texte = ImageFont.truetype("fonts/Roboto-Regular.ttf", 14)
        font_st = ImageFont.truetype("fonts/Roboto-Bold.ttf", 17)
        font_petit = ImageFont.truetype("fonts/Roboto-Regular.ttf", 11)
    except Exception:
        font_logo = font_gros_titre = font_rubrique = font_nom_gras = font_texte = font_st = font_petit = ImageFont.load_default()

    # 2. Bandeau supérieur (En-tête & Date)
    draw.rectangle([(0, 0), (largeur, 24)], fill="#F0F0F0")
    draw.text((15, 5), "N° 24 566 • 50 KOH • ÉDITION OFFICIELLE", fill="#444444", font=font_petit)
    date_str = datetime.datetime.now(ZoneInfo("Europe/Paris")).strftime("%A %d %B %Y").upper()
    draw.text((largeur - 260, 5), date_str, fill="#444444", font=font_petit)

    # 3. Logo L'ÉQUIPE (Rouge) & Slogan
    draw.rectangle([(15, 32), (280, 98)], fill="#E30613")
    draw.text((25, 28), "L'ÉQUIPE", fill="#FFFFFF", font=font_logo)
    draw.text((295, 48), "LE QUOTIDIEN DU SERVEUR ET DE LA STRATÉGIE", fill="#222222", font=font_rubrique)
    draw.text((295, 72), "www.discord-game.fr", fill="#777777", font=font_petit)
    draw.line([(0, 108), (largeur, 108)], fill="#000000", width=2)

    # 4. Encarts supérieurs (Gauche & Droite)
    # Encart Haut Gauche (1)
    cat1 = donnees_une.get("ENCART_1_CATEGORIE", "VENGEANCE").upper()
    tit1 = donnees_une.get("ENCART_1_TITRE", "")
    cand1 = donnees_une.get("ENCART_1_CANDIDAT", "")
    
    draw.text((15, 118), f"🔴 {cat1}", fill="#E30613", font=font_rubrique)
    draw.text((15, 138), f"{cand1} :", fill="#000000", font=font_nom_gras)
    
    y_t1 = 158
    for ligne in textwrap.wrap(tit1, width=32)[:3]:
        draw.text((15, y_t1), ligne, fill="#222222", font=font_texte)
        y_t1 += 18

    if cand1 in photos_candidats:
        fond.paste(photos_candidats[cand1].resize((85, 85)), (310, 118))

    draw.line([(415, 112), (415, 218)], fill="#DDDDDD", width=1)

    # Encart Haut Droite (2)
    cat2 = donnees_une.get("ENCART_2_CATEGORIE", "INTERNET").upper()
    tit2 = donnees_une.get("ENCART_2_TITRE", "")
    cand2 = donnees_une.get("ENCART_2_CANDIDAT", "")
    
    draw.text((430, 118), f"🔴 {cat2}", fill="#E30613", font=font_rubrique)
    draw.text((430, 138), f"{cand2} :", fill="#000000", font=font_nom_gras)
    
    y_t2 = 158
    for ligne in textwrap.wrap(tit2, width=38)[:3]:
        draw.text((430, y_t2), ligne, fill="#222222", font=font_texte)
        y_t2 += 18

    if cand2 in photos_candidats:
        fond.paste(photos_candidats[cand2].resize((85, 85)), (795, 118))

    draw.line([(0, 222), (largeur, 222)], fill="#000000", width=2)

    # 5. Zone Centrale Gauche (Grande Photo + Gros Titre)
    cand_p = donnees_une.get("CANDIDAT_PRINCIPAL", "")
    if cand_p in photos_candidats:
        fond.paste(photos_candidats[cand_p].resize((580, 480)), (15, 232))
    else:
        draw.rectangle([(15, 232), (595, 712)], fill="#EAEAEA", outline="#CCCCCC")
        draw.text((180, 450), "PHOTO À LA UNE", fill="#888888", font=font_gros_titre)

    titre_p = donnees_une.get("TITRE_PRINCIPAL", "C'EST ENCORE LOUPE !").upper()
    draw.text((15, 725), titre_p, fill="#000000", font=font_gros_titre)

    sous_titre = donnees_une.get("SOUS_TITRE_PRINCIPAL", "")
    y_st = 785
    for ligne in textwrap.wrap(sous_titre, width=55)[:4]:
        draw.text((15, y_st), ligne, fill="#222222", font=font_st)
        y_st += 22

    # 6. Colonne de Droite (Articles secondaires)
    draw.line([(610, 226), (610, 920)], fill="#DDDDDD", width=1)

    # Article droite 1 (Gastronomie / Ravitaillement)
    draw.text((625, 232), "GASTRONOMIE", fill="#E30613", font=font_rubrique)
    y_g = 256
    for ligne in textwrap.wrap("Le chef du camp a été surpris en train de cacher des rations avant l'épreuve.", width=28):
        draw.text((625, y_g), ligne, fill="#222222", font=font_texte)
        y_g += 18

    draw.line([(625, y_g + 12), (885, y_g + 12)], fill="#EEEEEE", width=1)

    # Article droite 2 / Encart 3
    cat3 = donnees_une.get("ENCART_3_CATEGORIE", "FIN DE SOIRÉE").upper()
    tit3 = donnees_une.get("ENCART_3_TITRE", "")
    cand3 = donnees_une.get("ENCART_3_CANDIDAT", "")
    
    y_e3 = y_g + 26
    draw.text((625, y_e3), cat3, fill="#E30613", font=font_rubrique)
    draw.text((625, y_e3 + 22), f"{cand3} :", fill="#000000", font=font_nom_gras)
    
    y_txt3 = y_e3 + 44
    for ligne in textwrap.wrap(tit3, width=28)[:4]:
        draw.text((625, y_txt3), ligne, fill="#222222", font=font_texte)
        y_txt3 += 18

    if cand3 in photos_candidats:
        fond.paste(photos_candidats[cand3].resize((240, 240)), (625, y_txt3 + 15))

    # 7. Bandeau Inférieur (Staff & Coulisses)
    draw.line([(0, 930), (largeur, 930)], fill="#000000", width=3)
    draw.rectangle([(0, 935), (largeur, 965)], fill="#F8F8F8")
    draw.text((15, 940), "🎙️ DANS LES COULISSES DE L'ORGANISATION", fill="#000000", font=font_rubrique)

    draw.text((15, 980), "Arbitrage & Staff :", fill="#E30613", font=font_nom_gras)
    draw.text((15, 1005), "« Aucun favoritisme constaté, mais les décisions du jury restent irrévocables. »", fill="#222222", font=font_texte)
    draw.text((15, 1030), "Les bilans complets et statistiques sont disponibles sur les salons dédiés.", fill="#666666", font=font_petit)

    # 8. Export image en mémoire
    buffer = io.BytesIO()
    fond.save(buffer, format="JPEG", quality=95)
    buffer.seek(0)
    return buffer

@bot.tree.command(
    name="generer_une_journal",
    description="Génère la Une satirique du journal L'Équipe avec les moments forts du jour."
)
@app_commands.describe(
    salon_destination="Optionnel : salon où publier la Une (par défaut : salon actuel)",
    salon_photos="Optionnel : salon contenant les photos de présentation des candidats"
)
@app_commands.check(est_orga_ou_admin)
async def generer_une_journal(
    interaction: discord.Interaction,
    salon_destination: discord.TextChannel = None,
    salon_photos: discord.abc.GuildChannel = None
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    dest_ch = salon_destination or interaction.channel

    # 1. Collecte des discussions de la journée pour l'IA
    paris_tz = ZoneInfo("Europe/Paris")
    maintenant_paris = datetime.datetime.now(paris_tz)
    debut_jour = maintenant_paris.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(datetime.timezone.utc)

    transcripts = []
    for ch in guild.text_channels:
        if est_categorie_candidate(ch.category):
            async for msg in ch.history(limit=60, after=debut_jour, oldest_first=False):
                if not msg.author.bot and msg.content.strip():
                    transcripts.append(f"{msg.author.display_name}: {msg.content.strip()}")

    if not transcripts:
        await interaction.followup.send("❌ Pas assez d'activité aujourd'hui pour générer la Une.", ephemeral=True)
        return

    await interaction.followup.send("⏳ **Rédaction des articles et mise en page du journal en cours...**", ephemeral=True)

    # 2. Extraction des articles via Gemini
    donnees_une = await extraire_articles_une_ia("\n".join(transcripts))

    # 3. Récupération des photos de profil / présentation des candidats
    photos_candidats = {}
    async with aiohttp.ClientSession() as session:
        for member in guild.members:
            if not member.bot:
                try:
                    async with session.get(member.display_avatar.url) as resp:
                        if resp.status == 200:
                            img_bytes = await resp.read()
                            img_p = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                            photos_candidats[member.display_name] = img_p
                            # Clé simplifiée (prénom seul)
                            prenom = member.display_name.split()[0]
                            photos_candidats[prenom] = img_p
                except Exception:
                    pass

    # 4. Dessin de l'image de la Une
    image_une_bytes = creer_image_une(donnees_une, photos_candidats)
    discord_file = discord.File(image_une_bytes, filename="une_lequipe_journal.jpg")

    date_str = maintenant_paris.strftime("%d/%m/%Y")
    await dest_ch.send(
        content=f"📰 **L'ÉQUIPE DU SERVEUR — ÉDITION DU {date_str}**\n*(Disponible en kiosque dès maintenant)*",
        file=discord_file
    )

    await interaction.followup.send(f"✅ **Une du journal publiée avec succès dans {dest_ch.mention} !**", ephemeral=True)

# ========================================================
# 36. TRANSCRIPTION ET RÉSUMÉ DE VOCAL À LA DEMANDE (CLIC DROIT)
# ========================================================

async def transcrire_audio_ia(audio_bytes: bytes, mime_type: str, auteur_nom: str) -> str:
    """Demande à Gemini de transcrire mot à mot et de résumer l'audio."""
    prompt = (
        f"Tu es l'assistant d'organisation d'un jeu de stratégie sur Discord.\n"
        f"Voici un message vocal envoyé par le candidat **{auteur_nom}**.\n\n"
        "MISSIONS :\n"
        "1. Transcris fidèlement les propos énoncés dans le vocal.\n"
        "2. Rédige un résumé rapide des intentions, stratégies, alliances ou infos clés divulguées.\n\n"
        "FORMAT DE RÉPONSE STRICT :\n"
        "### 📝 Transcription intégrale :\n"
        "[Texte retranscrit]\n\n"
        "### 🎯 Analyse & Points clés :\n"
        "- [Point 1]\n"
        "- [Point 2]"
    )

    contenus = [
        prompt,
        genai.types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
    ]

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=contenus
        )
        return response.text.strip()
    except Exception as e:
        return f"❌ Erreur lors de l'analyse IA de l'audio : {e}"


@bot.tree.context_menu(name="Transcrire le vocal")
@app_commands.check(est_orga_ou_admin)
async def transcrire_vocal_menu(interaction: discord.Interaction, message: discord.Message):
    """Commande Clic Droit -> Applications -> Transcrire le vocal (Éphémère)."""
    await interaction.response.defer(ephemeral=True)

    # 1. Vérification si le message contient un fichier audio
    piece_audio = None
    extensions_valides = [".ogg", ".mp3", ".wav", ".m4a"]
    
    if message.attachments:
        for att in message.attachments:
            if any(att.filename.lower().endswith(ext) for ext in extensions_valides):
                piece_audio = att
                break

    if not piece_audio:
        await interaction.followup.send(
            "❌ Ce message ne contient aucune note vocale ou fichier audio valide.",
            ephemeral=True
        )
        return

    # 2. Téléchargement de l'audio en mémoire
    try:
        audio_bytes = await piece_audio.read()
    except Exception as e:
        await interaction.followup.send(f"❌ Impossible de télécharger le fichier audio : {e}", ephemeral=True)
        return

    mime = piece_audio.content_type or "audio/ogg"
    auteur = message.author.display_name

    # 3. Traitement Gemini
    rapport = await transcrire_audio_ia(audio_bytes, mime, auteur)

    # 4. Envoi de l'embed éphémère
    embed = discord.Embed(
        title=f"🎙️ ANALYSE VOCALE — {auteur.upper()}",
        description=rapport if len(rapport) <= 3900 else rapport[:3900] + "\n...",
        color=discord.Color.teal()
    )
    embed.set_footer(text="Visible uniquement par toi • Confidentiel Staff")

    await interaction.followup.send(embed=embed, ephemeral=True)

# ========================================================
# 37. RÉSUMÉ DES X DERNIERS VOCAUX DU SALON (ÉPHÉMÈRE)
# ========================================================

async def synthetiser_multiples_vocaux_ia(pieces_audio: list[dict], nom_salon: str) -> str:
    """Envoie une liste d'audios avec leur contexte à Gemini pour synthèse globale."""
    prompt = (
        "Tu es l'analyste stratégique officiel d'un jeu communautaire sur Discord.\n"
        f"Voici les {len(pieces_audio)} dernières notes vocales envoyées dans le salon #{nom_salon}.\n\n"
        "MISSIONS :\n"
        "1. Identifie ce qui se dit dans chaque note vocale (qui parle et quel est le message principal).\n"
        "2. Rédige une SYNTHÈSE GLOBALE des intentions, des plans de vote, des alliances ou des non-dits.\n"
        "3. Détecte le ton (confiance, hésitation, panique, mensonge flagrant, colère).\n\n"
        "STRUCTURE DE RÉPONSE STRICTE :\n"
        "## 🎙️ SYNTHÈSE GLOBALE DES ÉCHANGES VOCAUX\n"
        "[Résumé fluide en 2-3 phrases de la dynamique générale de ces vocaux]\n\n"
        "## 📋 DÉTAIL PAR VOCAL (CHRONOLOGIQUE)\n"
    )

    contenus_payload = [prompt]

    for item in pieces_audio:
        tag = (
            f"\n--- VOCAL #{item['index']} ---\n"
            f"Auteur : {item['auteur']}\n"
            f"Heure  : {item['heure']}\n"
            f"Fichier: {item['nom_fichier']}\n"
        )
        contenus_payload.append(tag)
        contenus_payload.append(
            genai.types.Part.from_bytes(data=item["bytes"], mime_type=item["mime"])
        )

    contenus_payload.append(
        "\n\nPour chaque vocal ci-dessus, dresse :\n"
        "- **[Auteur] ([Heure]) :** [Ce qu'il dit] *(Ton/Posture : [ton])*."
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=contenus_payload
        )
        return response.text.strip()
    except Exception as e:
        return f"❌ Erreur lors de l'analyse IA des vocaux : {e}"


@bot.tree.command(
    name="resumer_vocaux",
    description="Résume les X dernières notes vocales d'un salon (100% éphémère / visible par toi seul)."
)
@app_commands.describe(
    nombre_vocaux="Nombre de notes vocales récentes à analyser (ex: 3, 5, 10 - défaut : 5)",
    salon="Optionnel : salon à scanner (par défaut : salon actuel)"
)
@app_commands.check(est_orga_ou_admin)
async def resumer_vocaux(
    interaction: discord.Interaction,
    nombre_vocaux: int = 5,
    salon: discord.TextChannel = None
):
    await interaction.response.defer(ephemeral=True)
    target_channel = salon or interaction.channel

    if not isinstance(target_channel, discord.TextChannel):
        await interaction.followup.send("❌ Seuls les salons textuels peuvent être analysés.", ephemeral=True)
        return

    if nombre_vocaux <= 0:
        await interaction.followup.send("❌ Le nombre de vocaux doit être supérieur à 0.", ephemeral=True)
        return

    limite_vocaux = min(nombre_vocaux, 15)  # Sécurité pour éviter de saturer la payload
    extensions_valides = [".ogg", ".mp3", ".wav", ".m4a"]
    paris_tz = ZoneInfo("Europe/Paris")

    # 1. Scan des messages pour trouver les pièces jointes audio
    vocaux_trouves = []
    async for msg in target_channel.history(limit=250, oldest_first=False):
        if msg.attachments:
            for att in msg.attachments:
                if any(att.filename.lower().endswith(ext) for ext in extensions_valides):
                    vocaux_trouves.append((msg, att))
                    if len(vocaux_trouves) >= limite_vocaux:
                        break
        if len(vocaux_trouves) >= limite_vocaux:
            break

    if not vocaux_trouves:
        await interaction.followup.send(
            f"❌ Aucune note vocale trouvée dans les derniers messages de {target_channel.mention}.",
            ephemeral=True
        )
        return

    await interaction.followup.send(
        f"⏳ **Téléchargement et analyse de {len(vocaux_trouves)} note(s) vocale(s) de {target_channel.mention} en cours...**",
        ephemeral=True
    )

    # 2. Téléchargement des audios en mémoire dans l'ordre chronologique
    vocaux_trouves.reverse()
    pieces_audio = []

    for idx, (msg, att) in enumerate(vocaux_trouves, 1):
        try:
            audio_bytes = await att.read()
            date_paris = msg.created_at.astimezone(paris_tz).strftime("%H:%M")
            mime = att.content_type or "audio/ogg"
            
            pieces_audio.append({
                "index": idx,
                "auteur": msg.author.display_name,
                "heure": date_paris,
                "nom_fichier": att.filename,
                "mime": mime,
                "bytes": audio_bytes
            })
        except Exception as e:
            print(f"Erreur téléchargement vocal {att.filename}: {e}")

    if not pieces_audio:
        await interaction.followup.send("❌ Impossible de lire les fichiers audio récupérés.", ephemeral=True)
        return

    # 3. Traitement global par Gemini
    rapport = await synthetiser_multiples_vocaux_ia(pieces_audio, target_channel.name)

    # 4. Envoi de l'embed éphémère (découpé si très long)
    if len(rapport) <= 3900:
        embed = discord.Embed(
            title=f"🎙️ SYNTHÈSE DE {len(pieces_audio)} VOCAUX — #{target_channel.name}",
            description=rapport,
            color=discord.Color.teal()
        )
        embed.set_footer(text=f"Demandé par {interaction.user.display_name} • Visible uniquement par toi")
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        morceaux = decouper_texte_intelligent(rapport, limite=1900)
        await interaction.followup.send(
            content=f"🎙️ **SYNTHÈSE DE {len(pieces_audio)} VOCAUX — #{target_channel.name}**\n*(Visible uniquement par toi)*\n\n" + morceaux[0],
            ephemeral=True
        )
        for chunk in morceaux[1:]:
            await interaction.followup.send(content=chunk, ephemeral=True)

# Suivi des salons où le brouilleur est actif : ensemble d'IDs de salons
SALONS_BROUILLEUR_ACTIFS = set()

async def transformer_en_charabia_ia(texte_original: str) -> str:
    """Transforme un message en une version à peine compréhensible / déformée."""
    prompt = (
        "Tu es un déformateur de phrases comique.\n"
        f"Voici un message : \"{texte_original}\"\n\n"
        "Consigne :\n"
        "Réécris ce message pour qu'il soit À PEINE compréhensible : utilise des synonymes bizarres, "
        "une syntaxe bancale, un argot étrange ou des tournures alambiquées, tout en gardant l'idée de base.\n"
        "Renvoie UNIQUEMENT la phrase transformée, sans guillemets ni introduction."
    )
    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt
        )
        return response.text.strip()
    except Exception:
        # Fallback si l'IA ne répond pas
        return "".join(c if random.random() > 0.15 else "..." for c in texte_original)

@bot.tree.command(
    name="activer_brouilleur",
    description="Remplace tous les messages envoyés dans ce salon par une version à peine compréhensible."
)
@app_commands.describe(salon="Optionnel : salon à cibler (par défaut : salon actuel)")
@app_commands.check(est_orga_ou_admin)
async def activer_brouilleur(interaction: discord.Interaction, salon: discord.TextChannel = None):
    ch = salon or interaction.channel
    if not isinstance(ch, discord.TextChannel):
        await interaction.response.send_message("❌ Cette commande ne fonctionne que dans un salon textuel.", ephemeral=True)
        return

    SALONS_BROUILLEUR_ACTIFS.add(ch.id)
    await interaction.response.send_message(
        f"🌀 **Brouilleur activé dans {ch.mention} !** Tous les messages envoyés seront remplacés.",
        ephemeral=True
    )


@bot.tree.command(
    name="desactiver_brouilleur",
    description="Désactive le brouillage des messages pour ce salon."
)
@app_commands.describe(salon="Optionnel : salon à cibler (par défaut : salon actuel)")
@app_commands.check(est_orga_ou_admin)
async def desactiver_brouilleur(interaction: discord.Interaction, salon: discord.TextChannel = None):
    ch = salon or interaction.channel
    if ch.id in SALONS_BROUILLEUR_ACTIFS:
        SALONS_BROUILLEUR_ACTIFS.remove(ch.id)
        await interaction.response.send_message(f"✅ **Brouilleur désactivé** dans {ch.mention}.", ephemeral=True)
    else:
        await interaction.response.send_message(f"ℹ️ Le brouilleur n'était pas actif dans {ch.mention}.", ephemeral=True)

async def generer_fun_facts_candidat_ia(candidat_nom: str, messages_candidat: list[str], messages_tiers: list[str]) -> str:
    """Génère 3 fun facts satiriques et drôles basés sur les interactions récentes."""
    texte_perso = "\n".join(messages_candidat) if messages_candidat else "Peu de messages directs."
    texte_autres = "\n".join(messages_tiers) if messages_tiers else "Peu de mentions externes."

    prompt = (
        "Tu es le commentateur officiel et biographe satirique d'un jeu de stratégie sur Discord (type Koh-Lanta / Survivor).\n"
        f"CANDIDAT CIBLÉ : **{candidat_nom}**\n\n"
        f"CE QU'IL DIT SUR LE SERVEUR :\n{texte_perso[:8000]}\n\n"
        f"CE QUE LES AUTRES DISENT DE LUI :\n{texte_autres[:6000]}\n\n"
        "MISSION :\n"
        "Rédige exactement 3 « FUN FACTS » (anecdotes comiques, statistiques absurdes ou secrets de polichinelle) "
        "sur ce candidat en te basant sur ses VRAIES interactions récentes.\n\n"
        "RÈGLES DU JEU :\n"
        "1. 100% DISCORD & STRATÉGIE : Appuie-toi sur ses hésitations, ses promesses en l'air, ses tics de langage, "
        "ses temps de réaction, son obsession pour un joueur ou ses théories bancales.\n"
        "2. HUMOUR & SECOND DEGRÉ : C'est du chambrage amical et bienveillant, pas de méchanceté gratuite.\n"
        "3. FORMAT D'AFFICHAGE (STRICT) :\n"
        "- 💡 **[Titre percutant] :** [Explication drôle en 1 ou 2 phrases]\n"
        "- 📊 **[Titre percutant] :** [Statistique inventée ou habitude comique]\n"
        "- 🕵️ **[Titre percutant] :** [Contradiction flagrante ou secret bancal]\n\n"
        "Renvoie UNIQUEMENT les 3 bullet points sans texte introductif."
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        return f"💡 **Le mystère reste entier :** Les serveurs ont surchauffé en tentant d'analyser son jeu ({e})."

@bot.tree.command(
    name="fun_fact",
    description="Génère 3 anecdotes absurdes et drôles sur un candidat d'après ses récents messages."
)
@app_commands.describe(
    candidat="Le candidat à passer au crible",
    public="Afficher publiquement dans le salon ? (Défaut : Vrai)"
)
@app_commands.check(est_orga_ou_admin)
async def fun_fact(
    interaction: discord.Interaction,
    candidat: discord.Member,
    public: bool = True
):
    await interaction.response.defer(ephemeral=not public)
    guild = interaction.guild

    nom_candidat_clean = nettoyer_texte(candidat.display_name)
    pseudo_global_clean = nettoyer_texte(candidat.name)
    mention_id = str(candidat.id)

    messages_candidat = []
    messages_tiers = []

    salons_cibles = [
        ch for ch in guild.text_channels
        if (est_categorie_candidate(ch.category) or ch.name.lower() == "log-deplacements")
        and not ch.name.startswith("🔒arch-")
    ]

    for channel in salons_cibles:
        est_salon_log = (channel.name.lower() == "log-deplacements")
        try:
            async for msg in channel.history(limit=30, oldest_first=False):
                if msg.author.bot and not est_salon_log:
                    continue
                contenu = msg.content.strip()
                if not contenu or contenu.startswith(("/", "!")):
                    continue

                if msg.author.id == candidat.id:
                    messages_candidat.append(f"[#{channel.name}] {contenu}")
                else:
                    contenu_clean = nettoyer_texte(contenu)
                    if (nom_candidat_clean in contenu_clean 
                        or pseudo_global_clean in contenu_clean 
                        or mention_id in msg.content):
                        messages_tiers.append(f"[#{channel.name}] {msg.author.display_name}: {contenu}")

                if len(messages_candidat) >= 15 and len(messages_tiers) >= 10:
                    break
        except Exception:
            continue

        if len(messages_candidat) >= 15 and len(messages_tiers) >= 10:
            break

    if not messages_candidat and not messages_tiers:
        await interaction.followup.send(
            f"👻 **Fun Fact sur {candidat.mention} :** Il est tellement discret sur le serveur qu'aucun message récent n'a pu être intercepté. Un vrai fantôme !",
            ephemeral=not public
        )
        return

    messages_candidat.reverse()
    messages_tiers.reverse()

    faits_texte = await generer_fun_facts_candidat_ia(
        candidat_nom=candidat.display_name,
        messages_candidat=messages_candidat,
        messages_tiers=messages_tiers
    )

    embed = discord.Embed(
        title=f"🎲 LE SAVIEZ-VOUS ? — {candidat.display_name.upper()}",
        description=f"Voici les dossiers confidentiels interceptés sur {candidat.mention} :\n\n{faits_texte}",
        color=discord.Color.from_rgb(244, 127, 255)
    )
    embed.set_thumbnail(url=candidat.display_avatar.url)
    embed.set_footer(text=f"Basé sur l'activité récente • Observatoire du Serveur")

    await interaction.followup.send(embed=embed, ephemeral=not public)

# ========================================================
# SYSTÈME DE BUZZER VOCAL MULTI-QUESTIONS
# ========================================================

class BuzzerMultiView(discord.ui.View):
    def __init__(self, numero_question: int = 1):
        super().__init__(timeout=None)  # Pas de timeout pour ne pas expirer en pleine épreuve
        self.numero_question = numero_question
        self.actif = True
        self.gagnant = None

    @discord.ui.button(label="🚨 BUZZER !", style=discord.ButtonStyle.danger, custom_id="btn_buzz_action")
    async def buzzer_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.actif:
            await interaction.response.send_message("⛔ Trop tard, quelqu'un a déjà buzzé !", ephemeral=True)
            return

        # Verrouillage immédiat
        self.actif = False
        self.gagnant = interaction.user

        # 1. Mise à jour visuelle du bouton buzzer (désactivé)
        button.disabled = True
        button.label = f"🔒 BUZZÉ PAR {interaction.user.display_name.upper()}"
        button.style = discord.ButtonStyle.secondary

        # 2. Bouton "Question suivante" réservé aux Orgas
        bouton_reset = discord.ui.Button(
            label="🔄 Question suivante", 
            style=discord.ButtonStyle.primary, 
            custom_id="btn_buzz_next"
        )

        async def reset_callback(reset_inter: discord.Interaction):
            if not est_orga_ou_admin(reset_inter):
                await reset_inter.response.send_message("⛔ Seul un Orga ou Admin peut relancer le buzzer.", ephemeral=True)
                return

            # Relance une nouvelle vue pour la question d'après
            nouvelle_vue = BuzzerMultiView(numero_question=self.numero_question + 1)
            
            embed_relance = discord.Embed(
                title=f"⚡ BUZZER — QUESTION #{nouvelle_vue.numero_question}",
                description="👉 *Micro ouvert ! Cliquez ci-dessous dès que vous avez la réponse.*",
                color=discord.Color.gold()
            )
            embed_relance.set_footer(text="Buzzer réarmé • Prêt pour le prochain buzz")

            # Désactive le bouton sur l'ancien message pour éviter les doubles clics
            bouton_reset.disabled = True
            await reset_inter.response.edit_message(view=self)

            # Envoie le nouveau buzzer tout frais
            await reset_inter.channel.send(embed=embed_relance, view=nouvelle_vue)

        bouton_reset.callback = reset_callback
        self.add_item(bouton_reset)

        # Met à jour le message du buzzer
        await interaction.response.edit_message(view=self)

        # Annonce le vainqueur du buzz
        embed_win = discord.Embed(
            title=f"🔔 BUZZ VALIDÉ — QUESTION #{self.numero_question}",
            description=(
                f"🥇 **{interaction.user.mention}** a buzzé en premier !\n\n"
                f"🎙️ La parole est à toi dans le vocal.\n\n"
                f"*(L'orga clique sur **« Question suivante »** dès que la réponse est traitée)*"
            ),
            color=discord.Color.green()
        )
        embed_win.set_thumbnail(url=interaction.user.display_avatar.url)
        await interaction.channel.send(embed=embed_win)


@bot.tree.command(
    name="buzzer",
    description="Lance la session de buzzer vocal avec réarmement rapide pour les questions suivantes."
)
@app_commands.describe(question="Optionnel : texte de la première question")
@app_commands.check(est_orga_ou_admin)
async def buzzer(interaction: discord.Interaction, question: str = "Question en cours..."):
    vue = BuzzerMultiView(numero_question=1)
    
    embed = discord.Embed(
        title="⚡ BUZZER — QUESTION #1",
        description=f"**{question}**\n\n👉 *Le premier qui clique ci-dessous prend la parole en vocal !*",
        color=discord.Color.gold()
    )
    embed.set_footer(text="Système anti-litige instantané")

    await interaction.response.send_message(embed=embed, view=vue)

# ========================================================
# SYSTÈME DE BUZZER VOCAL AVEC ARRÊT / CLÔTURE
# ========================================================

SESSION_BUZZER_ACTIVE = {}  # {channel_id: BuzzerMultiView}

class BuzzerMultiView(discord.ui.View):
    def __init__(self, channel_id: int, numero_question: int = 1):
        super().__init__(timeout=None)
        self.channel_id = channel_id
        self.numero_question = numero_question
        self.actif = True
        self.gagnant = None

    @discord.ui.button(label="🚨 BUZZER !", style=discord.ButtonStyle.danger, custom_id="btn_buzz_action")
    async def buzzer_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.actif:
            await interaction.response.send_message("⛔ Trop tard, quelqu'un a déjà buzzé !", ephemeral=True)
            return

        self.actif = False
        self.gagnant = interaction.user

        button.disabled = True
        button.label = f"🔒 BUZZÉ PAR {interaction.user.display_name.upper()}"
        button.style = discord.ButtonStyle.secondary

        # 1. Bouton Question Suivante (Orgas)
        bouton_next = discord.ui.Button(
            label="🔄 Question suivante", 
            style=discord.ButtonStyle.primary, 
            custom_id="btn_buzz_next"
        )

        # 2. Bouton Clôturer l'épreuve (Orgas)
        bouton_stop = discord.ui.Button(
            label="🛑 Clôturer l'épreuve", 
            style=discord.ButtonStyle.danger, 
            custom_id="btn_buzz_stop"
        )

        async def next_callback(next_inter: discord.Interaction):
            if not est_orga_ou_admin(next_inter):
                await next_inter.response.send_message("⛔ Réservé aux Orgas.", ephemeral=True)
                return

            bouton_next.disabled = True
            bouton_stop.disabled = True
            await next_inter.response.edit_message(view=self)

            nouvelle_vue = BuzzerMultiView(channel_id=self.channel_id, numero_question=self.numero_question + 1)
            SESSION_BUZZER_ACTIVE[self.channel_id] = nouvelle_vue

            embed_relance = discord.Embed(
                title=f"⚡ BUZZER — QUESTION #{nouvelle_vue.numero_question}",
                description="👉 *Micro ouvert ! Cliquez dès que vous avez la réponse.*",
                color=discord.Color.gold()
            )
            embed_relance.set_footer(text="Buzzer réarmé")
            await next_inter.channel.send(embed=embed_relance, view=nouvelle_vue)

        async def stop_callback(stop_inter: discord.Interaction):
            if not est_orga_ou_admin(stop_inter):
                await stop_inter.response.send_message("⛔ Réservé aux Orgas.", ephemeral=True)
                return

            self.clear_items()
            await stop_inter.response.edit_message(view=self)

            SESSION_BUZZER_ACTIVE.pop(self.channel_id, None)

            embed_fin = discord.Embed(
                title="🛑 ÉPREUVE BUZZER TERMINÉE",
                description=f"La session de buzzer s'arrête ici après **{self.numero_question} question(s)**.",
                color=discord.Color.dark_grey()
            )
            await stop_inter.channel.send(embed=embed_fin)

        bouton_next.callback = next_callback
        bouton_stop.callback = stop_callback

        self.add_item(bouton_next)
        self.add_item(bouton_stop)

        await interaction.response.edit_message(view=self)

        embed_win = discord.Embed(
            title=f"🔔 BUZZ VALIDÉ — QUESTION #{self.numero_question}",
            description=(
                f"🥇 **{interaction.user.mention}** a buzzé en premier !\n\n"
                f"🎙️ À toi de répondre dans le vocal."
            ),
            color=discord.Color.green()
        )
        embed_win.set_thumbnail(url=interaction.user.display_avatar.url)
        await interaction.channel.send(embed=embed_win)


@bot.tree.command(
    name="buzzer",
    description="Lance la session de buzzer vocal."
)
@app_commands.describe(question="Optionnel : texte de la question")
@app_commands.check(est_orga_ou_admin)
async def buzzer(interaction: discord.Interaction, question: str = "Question en cours..."):
    vue = BuzzerMultiView(channel_id=interaction.channel_id, numero_question=1)
    SESSION_BUZZER_ACTIVE[interaction.channel_id] = vue

    embed = discord.Embed(
        title="⚡ BUZZER — QUESTION #1",
        description=f"**{question}**\n\n👉 *Le premier qui clique ci-dessous prend la parole en vocal !*",
        color=discord.Color.gold()
    )
    embed.set_footer(text="Système de rapidité vocal")

    await interaction.response.send_message(embed=embed, view=vue)


@bot.tree.command(
    name="arreter_buzzer",
    description="Force l'arrêt du buzzer en cours dans le salon."
)
@app_commands.check(est_orga_ou_admin)
async def arreter_buzzer(interaction: discord.Interaction):
    ch_id = interaction.channel_id

    if ch_id in SESSION_BUZZER_ACTIVE:
        vue = SESSION_BUZZER_ACTIVE.pop(ch_id)
        vue.actif = False
        vue.stop()

        embed_fin = discord.Embed(
            title="🛑 BUZZER CLÔTURÉ",
            description="L'épreuve est terminée, tous les buzzers de ce salon sont désactivés.",
            color=discord.Color.dark_grey()
        )
        await interaction.response.send_message(embed=embed_fin)
    else:
        await interaction.response.send_message("ℹ️ Aucun buzzer n'est actif dans ce salon.", ephemeral=True)
# ==========================================
# DÉMARRAGE DU BOT
# ==========================================
bot.run(TOKEN)
