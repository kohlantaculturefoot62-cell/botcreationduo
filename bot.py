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
import textwrap
import urllib.request
from zoneinfo import ZoneInfo
from collections import Counter, defaultdict

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from google import genai
from PIL import Image, ImageDraw, ImageFont

# ==========================================
# GESTION AUTOMATIQUE DES POLICES POUR PILLOW
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
            print(f"⚠️ Impossible de télécharger la police {path_font}: {e}")

# ==========================================
# CONFIGURATION & ENVIRONNEMENT
# ==========================================

TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

MODEL_NAME = "gemini-2.5-flash"

# Salons & Catégories fixes
RECAP_CHANNEL_ID = 1545076756384579726            # 📰 Journal Stratégique Global
SALON_QUESTIONS_RECAP_ID = 1546598533333909728    # 🎙️ Fiches Questions & Conseil
SALON_BILAN_CANDIDATS_ID = 1549193526007435345    # 📊 Fiches Bilans Candidats
SALON_BILAN_ORGAS_ID = 1549193526007435345        # 🛠️ Fiches Bilans Organisateurs
CATEGORY_TRIO_ID = 1541397070898921482
CATEGORY_QUATUOR_ID = 1541397227744927835
RESULTATS_CHANNEL_ID = 1545186500960985148
SALON_REMARQUES_QUESTIONS_ID = 1545503543405060178

# Salons Spectateurs
SALON_CHAT_SPECTATEURS_ID = 1544355721024635061
SALON_RECAP_SPECTATEURS_ID = 1546598600555888670

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
ROLES_PERSO_EN_PAUSE = {}
CHRONOS_EN_COURS = {}
SESSIONS_RECHERCHE_ACTIVES = {}
MINUTEURS_ACTIFS = {}
SESSIONS_DECODEUR = {}

CONFIG_EPREUVE_GLOBALE = {
    "questions": [],
    "temps_par_defaut": 15,
    "active": False
}

ETATS_EPREUVES_SALONS = {}

ETAT_COMPOSITION = {
    "actif": False,
    "channel_id": None,
    "capitaine_1": None,
    "role_1": None,
    "capitaine_2": None,
    "role_2": None,
    "tour": 1
}

DERNIER_JOUR_RECAP = None
DERNIER_JOUR_QUESTIONS = None


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
    """Retire les accents, ponctuation et met en minuscules."""
    if not texte:
        return ""
    texte_norm = unicodedata.normalize("NFD", texte)
    sans_accents = "".join(c for c in texte_norm if unicodedata.category(c) != "Mn")
    texte_min = sans_accents.lower()
    texte_propre = re.sub(r'[^a-z0-9\s]', '', texte_min)
    return re.sub(r'\s+', ' ', texte_propre).strip()


def formater_nom_salon(nom: str) -> str:
    """Nettoie et formate un nom pour un salon Discord."""
    nom_clean = nettoyer_texte(nom)
    return re.sub(r"[^a-z0-9_-]", "", nom_clean.replace(" ", "-"))


def est_categorie_candidate(category: discord.CategoryChannel) -> bool:
    """Vérifie si le nom de la catégorie contient l'un des mots-clés de jeu."""
    if not category:
        return False
    cat_nom_propre = nettoyer_texte(category.name)
    return any(cible in cat_nom_propre for cible in CATEGORIES_CIBLES)


def get_spectateur_overwrites() -> discord.PermissionOverwrite:
    """Définit les droits en lecture seule pour les Spectateurs."""
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
    """Définit les droits en écoute seule pour les Spectateurs."""
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
    """Trouve le rôle spécifique du joueur."""
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
    """Découpe un texte long sans couper au milieu d'un mot."""
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
# 1. RÉSUMÉ DU SOIR & QUESTIONS CONFESSIONNAL
# =======================================================

async def poster_questions_automatiques(texte_recap: str):
    """Génère des questions d'interview directes et sans spoil pour les confessionnaux Discord."""
    salon_q = bot.get_channel(SALON_QUESTIONS_RECAP_ID)
    if not salon_q:
        return

    prompt_q = (
        "Tu es l'interviewer et showrunner d'un jeu de stratégie et de déduction communautaire joué SUR DISCORD.\n"
        "Voici le Journal Stratégique de la journée :\n\n"
        f"{texte_recap}\n\n"
        "Rédige une FICHE DE QUESTIONS DIRECTES ET COURTES pour l'équipe d'organisation (Staff/Orgas).\n\n"
        "RÈGLES D'OR ABSOLUES :\n"
        "1. CONTEXTE DISCORD : Le jeu se passe sur des salons, en vocal, en duos/trios et par messages. Oublie totalement les allusions à une île, au feu, aux cabanes ou à la survie physique.\n"
        "2. QUESTIONS COURTES ET IMPACTANTES : Maximum 1 à 2 phrases par question. Pose des questions directes et percutantes.\n"
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

    except Exception as e:
        print(f"❌ Erreur génération questions : {e}")


async def generer_et_envoyer_recap_quotidien(guild: discord.Guild, target_channel: discord.TextChannel):
    """Scanne les discussions du serveur et génère le Journal Stratégique adapté à Discord."""
    tz_paris = ZoneInfo("Europe/Paris")
    maintenant_paris = datetime.datetime.now(tz_paris)
    debut_journee_paris = maintenant_paris.replace(hour=0, minute=0, second=0, microsecond=0)
    debut_journee_utc = debut_journee_paris.astimezone(datetime.timezone.utc)

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
                if msg.attachments:
                    for att in msg.attachments:
                        if att.filename.endswith(".txt"):
                            try:
                                file_bytes = await att.read()
                                texte_fichier = file_bytes.decode('utf-8')
                                texte_msg += f"\n[Fichier {att.filename}]: {texte_fichier[:1200]}"
                            except Exception:
                                pass

                if texte_msg.strip():
                    date_paris_msg = msg.created_at.astimezone(tz_paris)
                    lines.append(f"[{date_paris_msg.strftime('%H:%M')}] {msg.author.display_name}: {texte_msg.strip()}")

            if lines:
                cat_nom = channel.category.name if channel.category else "Sans Catégorie"
                salons_transcripts.append(
                    f"=== [{cat_nom.upper()}] #{channel.name} ({len(lines)} messages) ===\n" + "\n".join(lines)
                )

    if not salons_transcripts:
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
        "Rédige le **Journal de Bord Stratégique Global de la Journée** pour l'équipe d'organisation.\n"
        "Consignes de cadrage :\n"
        "1. CONTEXTE RÉEL : C'est un jeu sur serveur Discord. Parle de salons textuels, vocaux, discussions de camp, confessionnaux, logs et pactes. Zéro cliché d'île déserte, de sable ou de jungle.\n"
        "2. Sois précis sur les dynamiques entre joueurs, les trahisons, les hésitations et les cibles de vote.\n"
        "3. Structure avec ces sections obligatoires et des émojis :\n\n"
        "   - 🌍 **Ambiance Générale & Dynamique du Serveur**\n"
        "   - 🤝 **Pactes, Alliances & Négociations**\n"
        "   - 🎯 **Cibles Évoquées, Plans & Votes**\n"
        "   - ⚠️ **Double-Jeu, Secrets & Fuites d'Infos**\n"
        "   - 🎙️ **Points Clés des Confessionnaux & Salons Privés**\n"
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

    max_tentatives = 3
    for tentative in range(max_tentatives):
        try:
            response = await asyncio.to_thread(
                gemini_client.models.generate_content,
                model=MODEL_NAME,
                contents=prompt
            )
            recap_text = response.text

            header = f"📰 **JOURNAL STRATÉGIQUE GLOBAL DU {date_str}**\n*(Réservé aux Orgas, Spectateurs et Admins)*\n\n"
            full_message = header + recap_text

            for chunk in decouper_texte_intelligent(full_message, 1900):
                await target_channel.send(chunk)
                await asyncio.sleep(0.4)

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
        "- Résume les débats, mèmes et hors-sujets abordés.\n\n"
        "## 🧠 2. ANALYSES STRATÉGIQUES & AVIS PERTINENTS\n"
        "- Résume leurs théories lucides, observations sur les erreurs/masterclass des candidats et pronostics de votes.\n\n"
        "## 🤡 3. LE BÊTISIER DE LA TRIBUNE (PERLES & PUNCHLINES DU CHAT)\n"
        "- Relève 3 à 5 messages hilarants ou réactions absurdes postées par les spectateurs.\n\n"
        "Garde un ton synthétique et percutant."
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
# 3. INTERVIEWS DU CONFESSIONNAL (MANUEL)
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


# ========================================================
# 3.BIS. ÉPREUVE DE RECHERCHE : MOTEURS D'ANALYSE ET IA
# ========================================================

async def suggerer_reponse_recherche_ia(joueur_mystere: str, question: str) -> dict:
    """Analyse la question posée et renvoie la réponse exacte (OUI/NON) avec justification."""
    prompt = (
        "Tu es l'arbitre factuel absolu d'un jeu de déduction sur le football et le sport.\n"
        f"🎯 JOUEUR CIBLE MYSTÈRE : **{joueur_mystere}**\n"
        f"❓ QUESTION DU CANDIDAT : \"{question}\"\n\n"
        "DIRECTIVES DE VÉRIFICATION FACTUELLE STRICTE :\n"
        f"1. Vérifie méticuleusement la réalité factuelle concernant UNIQUEMENT {joueur_mystere} (nationalité, clubs, années, postes, trophées, stats, sélections).\n"
        "2. Détermine si la réponse factuelle à cette question fermée est STRICTEMENT OUI ou STRICTEMENT NON.\n"
        "3. Fournis une justification courte et irréfutable.\n\n"
        "FORMAT DE RÉPONSE STRICT :\n"
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
    """Analyse les réponses données par les orgas sur le joueur mystère."""
    prompt = (
        "Tu es un juge et arbitre expert pour un jeu d'enquête sportive/culturelle.\n"
        f"🎯 JOUEUR / PERSONNE MYSTÈRE CIBLE : **{joueur_mystere}**\n\n"
        "Voici la transcription complète des échanges dans le salon de jeu :\n"
        f"\"\"\"\n{transcript}\n\"\"\"\n\n"
        "MISSIONS :\n"
        "1. Isole chaque question posée et la réponse apportée par l'organisation.\n"
        f"2. Pour CHAQUE question, vérifie la réalité FACTUELLE STRICTE concernant exclusivement **{joueur_mystere}**.\n"
        "3. Vérifie si la réponse donnée par l'orga est STRICTEMENT VRAIE ou FAUSSE.\n\n"
        "FORMAT DE RÉPONSE OBLIGATOIRE :\n"
        "Si AUCUNE erreur :\n"
        "✅ **AUDIT VALIDE : AUCUNE ERREUR DÉTECTÉE**\n\n"
        "Si AU MOINS UNE erreur :\n"
        "⚠️ **ALERTE ERREUR(S) DÉTECTÉE(S) DANS LES RÉPONSES !**\n"
        "❌ **Question litigieuse :** [Texte de la question]\n"
        "🔴 **Réponse donnée par l'orga :** [Oui / Non]\n"
        "🟢 **Ce qu'il fallait répondre :** [Oui / Non]\n"
        "📚 **Explication & Faits vérifiés :** [Détails précis]"
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
# 4. HORLOGE AUTOMATIQUE (PARIS 23H30)
# ==========================================

@tasks.loop(minutes=1)
async def horloge_serveur():
    """Horloge calée sur l'heure de Paris."""
    global DERNIER_JOUR_RECAP

    paris_tz = ZoneInfo("Europe/Paris")
    maintenant = datetime.datetime.now(paris_tz)
    jour_actuel = maintenant.strftime("%Y-%m-%d")

    if maintenant.hour == 23 and maintenant.minute == 30 and DERNIER_JOUR_RECAP != jour_actuel:
        DERNIER_JOUR_RECAP = jour_actuel
        print(f"⏰ [23:30 Paris] Lancement automatique des résumés du {jour_actuel}...")
        for guild in bot.guilds:
            try:
                target_channel = bot.get_channel(RECAP_CHANNEL_ID)
                if target_channel:
                    await generer_et_envoyer_recap_quotidien(guild, target_channel)
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


async def decoder_charabia_ia(nom_membre: str, texte_brut: str) -> str:
    """Traduit une phrase incompréhensible avec humour et second degré."""
    prompt = (
        "Tu es un linguiste d'élite, anthropologue du futur et expert en déchiffrage de dialectes cosmiques.\n"
        f"Un utilisateur sur Discord nommé **{nom_membre}** vient d'envoyer ce message lunaire / difficile à comprendre :\n"
        f"\"\"\"{texte_brut}\"\"\"\n\n"
        "TON RÔLE :\n"
        "Rédige une TRADUCTION / EXPLICATION HILARANTE de ce qu'il a voulu dire en français intelligible.\n\n"
        "DIRECTIVES D'HUMOUR :\n"
        "1. SECOND DEGRÉ & BIENVEILLANCE : C'est du chambrage amical entre potes sur Discord.\n"
        "2. FORME : Structure ta réponse de façon courte et rythmée (2 à 3 lignes max) :\n"
        "   - 🗣️ **Traduction littérale :** (Ce que son cerveau a tenté d'exprimer)\n"
        "   - 🔬 **Analyse sémiotique :** (Pourquoi c'est sorti de façon aussi chaotique)\n"
        "3. Renvoie UNIQUEMENT le texte formaté."
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        return f"🗣️ **Traduction d'urgence :** Le message est tellement cryptique que les serveurs ont planté ({e})."


@bot.event
async def on_message(message: discord.Message):
    """Écouteur de messages pour la recherche en direct, le décodeur et la draft."""
    if message.author.bot:
        return

    # 1. Décodeur / Traducteur humoristique en direct
    if message.channel.id in SESSIONS_DECODEUR:
        cible_data = SESSIONS_DECODEUR[message.channel.id]
        if message.author.id == cible_data["user_id"]:
            texte = message.content.strip()
            if len(texte) >= 2:
                async with message.channel.typing():
                    traduction = await decoder_charabia_ia(message.author.display_name, texte)
                    embed_decodeur = discord.Embed(
                        description=f"🌐 **DÉCODEUR OFFICIEL — Langue parlée : `{message.author.display_name}`**\n\n{traduction}",
                        color=discord.Color.teal()
                    )
                    embed_decodeur.set_footer(text="Service de traduction automatique en temps réel")
                    await message.reply(embed=embed_decodeur, mention_author=False)

    # 2. Détection des questions posées pendant l'épreuve de recherche
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

    # 3. Gestion de la draft interactive des équipes
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
    description="Génère tous les salons duos possibles pour les membres d'une équipe."
)
@app_commands.describe(
    role_equipe="Le rôle de l'équipe à diviser en duos",
    nom_categorie="Nom de la catégorie où créer les salons"
)
@app_commands.check(est_orga_ou_admin)
async def creer_duos(interaction: discord.Interaction, role_equipe: discord.Role, nom_categorie: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    membres = [m for m in guild.members if not m.bot and role_equipe in m.roles]

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
        await asyncio.sleep(0.5)

    await interaction.followup.send(
        f"✅ **Terminé !** **{total_duos} salons duos** créés dans **{current_category.name}**.",
        ephemeral=True
    )


@bot.tree.command(
    name="eliminer_candidat",
    description="Archive tous les salons d'un candidat éliminé et retire les accès."
)
@app_commands.describe(role_candidat="Le rôle du candidat éliminé")
@app_commands.check(est_orga_ou_admin)
async def eliminer_candidat(interaction: discord.Interaction, role_candidat: discord.Role):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    targeted_channels = [
        channel for channel in guild.text_channels
        if (channel.name.startswith("duo-") or channel.name.startswith("🔗・") or channel.name.startswith("🔺・"))
        and role_candidat in channel.overwrites
    ]

    if not targeted_channels:
        await interaction.followup.send(f"ℹ️ Aucun salon actif trouvé pour le rôle {role_candidat.mention}.", ephemeral=True)
        return

    clean_arch_name = nettoyer_texte(NOM_CATEGORIE_ARCHIVE)
    archive_categories = [c for c in guild.categories if clean_arch_name in nettoyer_texte(c.name)]
    current_archive_cat = archive_categories[-1] if archive_categories else await guild.create_category(NOM_CATEGORIE_ARCHIVE)

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
        await asyncio.sleep(0.5)

    await interaction.followup.send(
        f"🏆 **Élimination enregistrée :** {role_candidat.mention}\n"
        f"📦 **{archived_count} salons** archivés dans **{current_archive_cat.name}**.",
        ephemeral=True
    )


# ========================================================
# 6. SALONS SPÉCIFIQUES (TRIOS, QUATUORS & VOCAUX)
# ========================================================

@bot.tree.command(
    name="creer_trio",
    description="Crée un salon trio privé dans la catégorie dédiée pour 3 candidats."
)
@app_commands.describe(
    role_1="Rôle du premier candidat",
    role_2="Rôle du deuxième candidat",
    role_3="Rôle du troisième candidat"
)
@app_commands.check(est_orga_ou_admin)
async def creer_trio(interaction: discord.Interaction, role_1: discord.Role, role_2: discord.Role, role_3: discord.Role):
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

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEATORS_NAME if "ROLE_SPECTATEATORS_NAME" in globals() else ROLE_SPECTATEURS_NAME)
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
        f"✅ **Trio créé :** {salon.mention} dans **{categorie.name}** !",
        ephemeral=True
    )


@bot.tree.command(
    name="creer_quatuor",
    description="Crée un salon quatuor privé dans la catégorie dédiée pour 4 candidats."
)
@app_commands.describe(
    role_1="Rôle du premier candidat",
    role_2="Rôle du deuxième candidat",
    role_3="Rôle du troisième candidat",
    role_4="Rôle du quatrième candidat"
)
@app_commands.check(est_orga_ou_admin)
async def creer_quatuor(interaction: discord.Interaction, role_1: discord.Role, role_2: discord.Role, role_3: discord.Role, role_4: discord.Role):
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
        f"✅ **Quatuor créé :** {salon.mention} dans **{categorie.name}** !",
        ephemeral=True
    )


@bot.tree.command(
    name="creer_vocal",
    description="Crée un salon vocal privé pour 2 à 5 candidats (Spectateurs en écoute)."
)
@app_commands.describe(
    nom_categorie="Nom de la catégorie",
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
            view_channel=True, connect=True, speak=True, stream=True, use_voice_activation=True
        )

    if role_spectateurs:
        overwrites[role_spectateurs] = get_spectateur_voice_overwrites()
    if role_orgas:
        overwrites[role_orgas] = discord.PermissionOverwrite(
            view_channel=True, connect=True, speak=True, mute_members=True, deafen_members=True, move_members=True
        )

    noms = [formater_nom_salon(r.name) for r in roles_fournis]
    nom_vocal = f"🔊・{'-'.join(noms)}"

    salon_vocal = await guild.create_voice_channel(name=nom_vocal, category=category, overwrites=overwrites)
    await interaction.followup.send(
        f"✅ **Salon vocal créé :** {salon_vocal.mention} dans **{category.name}** !",
        ephemeral=True
    )


# ==========================================
# 7. SUPPRESSION & NETTOYAGE
# ==========================================

@bot.tree.command(name="supprimer_categorie", description="Supprime une catégorie entière et ses salons.")
@app_commands.describe(nom_categorie="Nom exact de la catégorie")
@app_commands.check(est_orga_ou_admin)
async def supprimer_categorie(interaction: discord.Interaction, nom_categorie: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    target_clean = nettoyer_texte(nom_categorie)
    category = discord.utils.find(lambda c: nettoyer_texte(c.name) == target_clean, guild.categories)
    if not category:
        await interaction.followup.send(f"❌ Catégorie **{nom_categorie}** introuvable.", ephemeral=True)
        return

    channels_to_delete = list(category.channels)
    total_channels = len(channels_to_delete)

    for channel in channels_to_delete:
        try:
            await channel.delete(reason="Nettoyage")
            await asyncio.sleep(0.3)
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
        for ch in list(cat.channels):
            try:
                await ch.delete(reason="Purge")
                total_channels += 1
                await asyncio.sleep(0.3)
            except Exception:
                pass
        await cat.delete(reason="Purge")
        await asyncio.sleep(0.4)

    await interaction.followup.send(f"🗑️ Nettoyage : **{len(categories_to_delete)} catégories** et **{total_channels} salons** supprimés !", ephemeral=True)


@bot.tree.command(
    name="effacer_salon",
    description="Supprime tous les messages d'un salon (clone et recrée le salon à neuf)."
)
@app_commands.describe(salon="Optionnel : salon à vider (par défaut : salon actuel)")
@app_commands.check(est_orga_ou_admin)
async def effacer_salon(interaction: discord.Interaction, salon: discord.TextChannel = None):
    await interaction.response.defer(ephemeral=True)
    target = salon or interaction.channel

    if not isinstance(target, discord.TextChannel):
        await interaction.followup.send("❌ Seuls les salons textuels peuvent être réinitialisés.", ephemeral=True)
        return

    try:
        nouveau_salon = await target.clone(reason=f"Salon vidé par {interaction.user.display_name}")
        await target.delete(reason=f"Salon vidé par {interaction.user.display_name}")
        await nouveau_salon.send("🧹 **Le salon a été réinitialisé et vidé avec succès.**")
    except Exception as e:
        await interaction.followup.send(f"❌ Erreur lors de la réinitialisation : {e}", ephemeral=True)


@bot.tree.command(
    name="vider_categorie",
    description="Supprime tous les salons d'une catégorie tout en conservant la catégorie vide."
)
@app_commands.describe(nom_categorie="Nom de la catégorie cible")
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

    for channel in salons_a_supprimer:
        try:
            await channel.delete(reason=f"Nettoyage par {interaction.user.display_name}")
            await asyncio.sleep(0.3)
        except Exception:
            pass

    await interaction.followup.send(
        f"🧹 **{total_salons} salon(s)** supprimé(s) dans la catégorie **{category.name}**.",
        ephemeral=True
    )


# ========================================================
# 8. PERMISSIONS SPECTATEURS
# ========================================================

@bot.tree.command(
    name="ajouter_spectateurs_salon",
    description="Donne l'accès Spectateurs strict (lecture seule) à un salon précis."
)
@app_commands.describe(salon="Optionnel : salon à configurer (par défaut : salon actuel)")
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
        await interaction.followup.send(f"❌ Rôle **{ROLE_SPECTATEURS_NAME}** introuvable.", ephemeral=True)
        return

    await target_channel.set_permissions(
        role_spectateurs,
        overwrite=get_spectateur_overwrites(),
        reason=f"Accès spectateur ajouté par {interaction.user.display_name}"
    )
    await interaction.followup.send(f"👁️ Accès **Spectateurs** appliqué au salon {target_channel.mention} !", ephemeral=True)


@bot.tree.command(
    name="ajouter_spectateurs_categorie",
    description="Donne l'accès Spectateurs strict à tous les salons d'une catégorie."
)
@app_commands.describe(nom_categorie="Nom de la catégorie cible")
@app_commands.check(est_orga_ou_admin)
async def ajouter_spectateurs_categorie(interaction: discord.Interaction, nom_categorie: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    if not role_spectateurs:
        await interaction.followup.send(f"❌ Rôle **{ROLE_SPECTATEURS_NAME}** introuvable.", ephemeral=True)
        return

    target_clean = nettoyer_texte(nom_categorie)
    category = discord.utils.find(lambda c: nettoyer_texte(c.name) == target_clean, guild.categories)

    if not category:
        await interaction.followup.send(f"❌ Catégorie **{nom_categorie}** introuvable.", ephemeral=True)
        return

    channels_list = [ch for ch in category.channels if isinstance(ch, discord.TextChannel)]
    mis_a_jour = 0

    for ch in channels_list:
        try:
            await ch.set_permissions(
                role_spectateurs,
                overwrite=get_spectateur_overwrites(),
                reason=f"Accès spectateurs ({interaction.user.display_name})"
            )
            mis_a_jour += 1
            await asyncio.sleep(0.6)
        except Exception:
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
    format="Choisissez entre un résumé synthétique ou une analyse complète",
    limite="Nombre de messages récents à analyser (par défaut: 100)"
)
@app_commands.choices(format=[
    app_commands.Choice(name="⚡ Résumé Court (Points clés rapides)", value="court"),
    app_commands.Choice(name="📖 Résumé Long (Analyse détaillée)", value="long")
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
            "Tu es l'arbitre d'un jeu de stratégie sur Discord. "
            f"Voici la transcription des messages du salon #{channel.name} :\n\n"
            f"{transcript}\n\n"
            "Fais un résumé **TRÈS COURT, CONCIS ET DIRECT** en 3 à 5 bullet points maximum :\n"
            "- 🎯 Sujet central en 1 phrase\n"
            "- 🤝 Décisions / Alliances évoquées\n"
            "- ⚠️ Orientations stratégiques ou cibles mentionnées\n"
            "- 🎭 Dynamique des échanges"
        )
    else:
        prompt = (
            "Tu es l'analyste stratégique d'un jeu d'aventure sur Discord. "
            f"Voici la transcription des messages du salon #{channel.name} :\n\n"
            f"{transcript}\n\n"
            "Fais un **RÉSUMÉ DÉTAILLÉ ET STRUCTURÉ** en français avec :\n"
            "1. 🎯 **Analyse Thématique**\n"
            "2. 🤝 **Accords & Propositions**\n"
            "3. ⚠️ **Scénarios & Votes évoqués**\n"
            "4. 🎭 **Dynamique relationnelle**\n"
            "5. 💬 **Citations Clés**"
        )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt
        )
        embed = discord.Embed(
            title=f"{'⚡ Résumé Flash' if format.value == 'court' else '📖 Résumé Détaillé'} — #{channel.name}",
            description=response.text,
            color=discord.Color.gold() if format.value == "court" else discord.Color.purple()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Erreur IA : {e}", ephemeral=True)


@bot.tree.command(
    name="resumer_conv_orga",
    description="Génère un compte-rendu IA axé sur l'organisation et les décisions staff."
)
@app_commands.describe(
    format="Choisissez entre un résumé synthétique ou un compte-rendu complet",
    limite="Nombre de messages récents à analyser (par défaut: 100)"
)
@app_commands.choices(format=[
    app_commands.Choice(name="⚡ Résumé Court (Actions rapides)", value="court"),
    app_commands.Choice(name="📖 Résumé Long (Compte-rendu détaillé)", value="long")
])
@app_commands.check(est_orga_ou_admin)
async def resumer_conv_orga(interaction: discord.Interaction, format: app_commands.Choice[str], limite: int = 100):
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel

    messages = [msg async for msg in channel.history(limit=limite, oldest_first=True)]
    user_messages = [msg for msg in messages if not msg.author.bot and msg.content.strip()]

    if len(user_messages) < 3:
        await interaction.followup.send("⚠️ Pas assez de messages pour générer un compte-rendu.", ephemeral=True)
        return

    transcript = "\n".join([f"{msg.author.display_name}: {msg.content}" for msg in user_messages])

    prompt = (
        f"Tu es l'assistant de direction du staff. Voici les échanges de #{channel.name} :\n\n{transcript}\n\n"
        "Rédige un compte-rendu structuré avec les sujets abordés, décisions actées, répartition des tâches et prochaines étapes."
    )

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=MODEL_NAME,
            contents=prompt
        )
        embed = discord.Embed(
            title=f"📋 Compte-Rendu Staff — #{channel.name}",
            description=response.text,
            color=discord.Color.blue()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Erreur IA : {e}", ephemeral=True)


@bot.tree.command(
    name="questions_confessionnal",
    description="Génère des questions directes pour les confessionnaux (sur-mesure ou global)."
)
@app_commands.describe(candidat="Optionnel : rôle d'un candidat précis")
@app_commands.check(est_orga_ou_admin)
async def questions_confessionnal(interaction: discord.Interaction, candidat: discord.Role = None):
    await interaction.response.defer(ephemeral=True)
    source_recap_channel = bot.get_channel(RECAP_CHANNEL_ID) or interaction.channel
    candidat_nom = candidat.name if candidat else None

    resultat_text = await generer_questions_confessionnal(source_recap_channel, candidat_nom)
    embed = discord.Embed(
        title=f"🎙️ Interview Confessionnal — {candidat.name}" if candidat else "🎙️ Suggestions Confessionnal du Jour",
        description=resultat_text,
        color=discord.Color.red()
    )
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
    await interaction.followup.send(f"⏳ Analyse en cours pour {target_channel.mention}...", ephemeral=True)
    await generer_et_envoyer_recap_quotidien(interaction.guild, target_channel)


@bot.tree.command(
    name="forcer_recap_spec",
    description="Force immédiatement la génération du récapitulatif spectateurs."
)
@app_commands.check(est_orga_ou_admin)
async def forcer_recap_spec(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    succes, msg = await traiter_resume_spectateurs(interaction.guild)
    await interaction.followup.send(f"{'✅' if succes else '❌'} {msg}", ephemeral=True)


@bot.tree.command(
    name="pause_taches",
    description="Met en pause l'envoi automatique du récap du soir."
)
@app_commands.check(est_orga_ou_admin)
async def pause_taches(interaction: discord.Interaction):
    if horloge_serveur.is_running():
        horloge_serveur.stop()
    await interaction.response.send_message("⏸️ **Récap automatique du soir mis en pause.**", ephemeral=True)


@bot.tree.command(
    name="reprendre_taches",
    description="Réactive l'envoi automatique du récap du soir."
)
@app_commands.check(est_orga_ou_admin)
async def reprendre_taches(interaction: discord.Interaction):
    if not horloge_serveur.is_running():
        horloge_serveur.start()
    await interaction.response.send_message("▶️ **Récap automatique du soir réactivé.**", ephemeral=True)


# ========================================================
# 9.BIS. COMMANDES ÉPREUVE DE RECHERCHE & AUDIT
# ========================================================

@bot.tree.command(
    name="lancer_aide_recherche",
    description="Active l'assistance IA en direct pour suggérer les réponses aux orgas."
)
@app_commands.describe(
    joueur_mystere="Nom exact du joueur à deviner",
    candidat="Le candidat qui passe l'épreuve",
    salon="Optionnel : salon de l'épreuve"
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
    SESSIONS_RECHERCHE_ACTIVES[ch.id] = {"joueur": joueur_mystere.strip(), "candidat_id": candidat.id}
    salon_orgas = bot.get_channel(SALON_REMARQUES_QUESTIONS_ID)
    mention_orga = salon_orgas.mention if salon_orgas else f"ID `{SALON_REMARQUES_QUESTIONS_ID}`"

    await interaction.followup.send(
        f"🟢 **Aide activée !** Cible : `{joueur_mystere}` | Candidat : {candidat.mention} | Suggestions : {mention_orga}",
        ephemeral=True
    )


@bot.tree.command(
    name="arreter_aide_recherche",
    description="Désactive l'assistance IA en direct pour ce salon."
)
@app_commands.describe(salon="Optionnel : salon de l'épreuve")
@app_commands.check(est_orga_ou_admin)
async def arreter_aide_recherche(interaction: discord.Interaction, salon: discord.TextChannel = None):
    ch = salon or interaction.channel
    if ch.id in SESSIONS_RECHERCHE_ACTIVES:
        joueur = SESSIONS_RECHERCHE_ACTIVES.pop(ch.id)["joueur"]
        await interaction.response.send_message(f"🛑 Aide désactivée pour {ch.mention} (Joueur : `{joueur}`).", ephemeral=True)
    else:
        await interaction.response.send_message(f"ℹ️ Aucune session active dans {ch.mention}.", ephemeral=True)


@bot.tree.command(
    name="audit_reponses_recherche",
    description="Vérifie si les réponses OUI/NON des orgas sur le joueur mystère sont correctes."
)
@app_commands.describe(
    joueur_mystere="Nom exact du joueur à trouver",
    limite_messages="Nombre de messages récents à analyser (défaut : 50)",
    salon_audit="Optionnel : salon de jeu à vérifier"
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

    messages = [msg async for msg in target_channel.history(limit=limite_messages, oldest_first=True)]
    messages_valides = [f"[{m.author.display_name}]: {m.content.strip()}" for m in messages if not m.author.bot and m.content.strip()]

    if not messages_valides:
        await interaction.followup.send(f"❌ Aucun message trouvé dans {target_channel.mention}.", ephemeral=True)
        return

    resultat_audit = await verifier_echanges_recherche_ia(joueur_mystere, "\n".join(messages_valides))
    salon_orgas = bot.get_channel(SALON_REMARQUES_QUESTIONS_ID) or interaction.channel

    embed = discord.Embed(
        title=f"🔎 AUDIT FACTUEL — {joueur_mystere.upper()}",
        description=resultat_audit if len(resultat_audit) <= 3900 else None,
        color=discord.Color.green() if "✅ **AUDIT VALIDE" in resultat_audit else discord.Color.red()
    )
    if len(resultat_audit) > 3900:
        for chunk in decouper_texte_intelligent(resultat_audit, limite=1900):
            await salon_orgas.send(chunk)
    else:
        await salon_orgas.send(embed=embed)

    await interaction.followup.send(f"✅ Audit terminé et transmis dans {salon_orgas.mention}.", ephemeral=True)


# ==========================================
# 10. ANIMATION DE TIRAGE AU SORT (BOULE NOIRE)
# ==========================================

@bot.tree.command(
    name="tirage_boules",
    description="Lance le tirage au sort des boules avec animation et suspense."
)
@app_commands.describe(
    participants="Mentionne les candidats (ex: @Sarah @Lucas ...)",
    nombre_boules_noires="Nombre de boules noires (défaut : 1)",
    couleur_sauveur="Couleur des boules sécurisées"
)
@app_commands.choices(couleur_sauveur=[
    app_commands.Choice(name="⚪ Boule Blanche", value="⚪ Blanche"),
    app_commands.Choice(name="🔴 Boule Rouge", value="🔴 Rouge"),
    app_commands.Choice(name="🟡 Boule Jaune", value="🟡 Jaune")
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

    if len(candidats) < 2 or nombre_boules_noires >= len(candidats) or nombre_boules_noires < 1:
        await interaction.response.send_message("❌ Paramètres de tirage invalides.", ephemeral=True)
        return

    await interaction.response.send_message("🏺 Lancement du tirage au sort...", ephemeral=True)
    symbole_sauve = couleur_sauveur.value if couleur_sauveur else "⚪ Blanche"

    embed_intro = discord.Embed(
        title="🏺 LE TIRAGE DES BOULES",
        description=f"**{len(candidats)} aventuriers** s'avancent vers le sac.\n- {len(candidats) - nombre_boules_noires}x {symbole_sauve}\n- {nombre_boules_noires}x ⚫ **Boule Noire**",
        color=discord.Color.dark_grey()
    )
    message_principal = await channel.send(embed=embed_intro)
    await asyncio.sleep(3)

    sac = (["⚫ Noire"] * nombre_boules_noires) + ([symbole_sauve] * (len(candidats) - nombre_boules_noires))
    random.shuffle(sac)
    ordre_passage = list(candidats)
    random.shuffle(ordre_passage)

    victimes = []
    texte_rev = ""

    for i, candidat in enumerate(ordre_passage, 1):
        boule = sac.pop()
        if "Noire" in boule:
            victimes.append(candidat)
            symbole = "⚫ **BOULE NOIRE !**"
        else:
            symbole = f"{symbole_sauve} *(Sauf !)*"

        texte_rev += f"**{i}.** {candidat.mention} ➔ {symbole}\n"
        embed_up = discord.Embed(title="🏺 TIRAGE DES BOULES — EN COURS", description=texte_rev, color=discord.Color.orange())
        await message_principal.edit(embed=embed_up)
        await asyncio.sleep(2.5)

    mentions_v = ", ".join([v.mention for v in victimes])
    embed_fin = discord.Embed(
        title="🏺 TIRAGE DES BOULES — VERDICT FINAL",
        description=f"{texte_rev}\n━━━━━━━━━━━━━━━━━━━━━━\n☠️ **VERDICT :** {mentions_v} {'ont' if len(victimes) > 1 else 'a'} tiré la **Boule Noire** !",
        color=discord.Color.dark_red()
    )
    await message_principal.edit(embed=embed_fin)


# ========================================================
# 11. GESTION DES BINÔMES (DESTINS LIÉS)
# ========================================================

@bot.tree.command(
    name="tirer_binomes",
    description="Étape 1 : Tire au sort les binômes avec animation (sans créer les salons)."
)
@app_commands.describe(role_equipe_a="Premier rôle d'équipe", role_equipe_b="Deuxième rôle d'équipe")
@app_commands.check(est_orga_ou_admin)
async def tirer_binomes(interaction: discord.Interaction, role_equipe_a: discord.Role, role_equipe_b: discord.Role):
    global DERNIERS_BINOMES_TIRES
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel

    membres_a = [m for m in role_equipe_a.members if not m.bot]
    membres_b = [m for m in role_equipe_b.members if not m.bot]

    if not membres_a or not membres_b or len(membres_a) != len(membres_b):
        await interaction.followup.send("❌ Équipes vides ou de tailles inégales.", ephemeral=True)
        return

    candidats_a = [{"member": m, "role": trouver_role_personnel(m, role_equipe_a), "display_name": m.display_name, "clean_name": formater_nom_salon(m.display_name)} for m in membres_a]
    candidats_b = [{"member": m, "role": trouver_role_personnel(m, role_equipe_b), "display_name": m.display_name, "clean_name": formater_nom_salon(m.display_name)} for m in membres_b]

    random.shuffle(candidats_a)
    random.shuffle(candidats_b)
    DERNIERS_BINOMES_TIRES = list(zip(candidats_a, candidats_b))

    embed = discord.Embed(title="⚡ DESTINS LIÉS — TIRAGE EN DIRECT ⚡", color=discord.Color.gold())
    msg = await channel.send(embed=embed)

    texte = ""
    for i, (ca, cb) in enumerate(DERNIERS_BINOMES_TIRES, 1):
        texte += f"🔗 **Binôme #{i} :** {ca['display_name']} ({ca['member'].mention}) & {cb['display_name']} ({cb['member'].mention})\n"
        embed.description = texte
        await msg.edit(embed=embed)
        await asyncio.sleep(2)

    await interaction.followup.send("✅ Tirage validé ! Utilisez `/creer_salons_binomes` pour ouvrir les salons.", ephemeral=True)


@bot.tree.command(
    name="creer_salons_binomes",
    description="Étape 2 : Crée les salons privés pour le dernier tirage de binômes."
)
@app_commands.describe(nom_categorie="Nom de la catégorie où créer les salons")
@app_commands.check(est_orga_ou_admin)
async def creer_salons_binomes(interaction: discord.Interaction, nom_categorie: str):
    global DERNIERS_BINOMES_TIRES
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    if not DERNIERS_BINOMES_TIRES:
        await interaction.followup.send("❌ Aucun tirage en attente.", ephemeral=True)
        return

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    clean_target_name = nettoyer_texte(nom_categorie)
    existing_category = discord.utils.find(lambda c: nettoyer_texte(c.name) == clean_target_name, guild.categories)
    current_category = existing_category if existing_category else await guild.create_category(nom_categorie)
    channel_count = len(current_category.channels)
    cat_index = 1

    salons_crees = []
    for ca, cb in DERNIERS_BINOMES_TIRES:
        if channel_count >= MAX_CHANNELS_PER_CATEGORY:
            cat_index += 1
            current_category = await guild.create_category(f"{nom_categorie} - {cat_index}")
            channel_count = 0

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
            overwrites[role_orgas] = discord.PermissionOverwrite(read_messages=True, view_channel=True, read_message_history=True, send_messages=True)

        salon = await guild.create_text_channel(name=f"🔗・{ca['clean_name']}-{cb['clean_name']}", category=current_category, overwrites=overwrites)
        channel_count += 1
        salons_crees.append(salon.mention)
        await asyncio.sleep(0.4)

    DERNIERS_BINOMES_TIRES = []
    await interaction.followup.send(f"✅ **{len(salons_crees)} salons créés** !\n" + "\n".join(salons_crees), ephemeral=True)


# ========================================================
# 12. GESTION DU CONSEIL (ISOLATION & RESTAURATION)
# ========================================================

@bot.tree.command(
    name="activer_conseil",
    description="Isole une équipe pour le conseil : retire leurs rôles personnels temporairement."
)
@app_commands.describe(role_equipe="L'équipe qui se rend au conseil")
@app_commands.check(est_orga_ou_admin)
async def activer_conseil(interaction: discord.Interaction, role_equipe: discord.Role):
    global ROLES_PERSO_EN_PAUSE
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    membres_cibles = [m for m in guild.members if not m.bot and role_equipe in m.roles]
    isoles = 0

    for m in membres_cibles:
        r_perso = trouver_role_personnel(m, role_equipe)
        if r_perso and r_perso < guild.me.top_role:
            try:
                await m.remove_roles(r_perso, reason="Activation Conseil")
                ROLES_PERSO_EN_PAUSE[m.id] = r_perso.id
                isoles += 1
                await asyncio.sleep(0.3)
            except Exception:
                pass

    await interaction.followup.send(f"🔒 **Conseil Activé !** {isoles}/{len(membres_cibles)} rôle(s) personnel(s) masqué(s).", ephemeral=True)


@bot.tree.command(
    name="desactiver_conseil",
    description="Restaure les rôles personnels des candidats d'une équipe après le conseil."
)
@app_commands.describe(role_equipe="L'équipe qui revient du conseil")
@app_commands.check(est_orga_ou_admin)
async def desactiver_conseil(interaction: discord.Interaction, role_equipe: discord.Role):
    global ROLES_PERSO_EN_PAUSE
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    restaures = 0

    for m in guild.members:
        if m.bot or role_equipe not in m.roles:
            continue
        role_a_rendre = None
        if m.id in ROLES_PERSO_EN_PAUSE:
            role_a_rendre = guild.get_role(ROLES_PERSO_EN_PAUSE[m.id])
        if not role_a_rendre:
            role_a_rendre = trouver_role_personnel(m, role_equipe)

        if role_a_rendre and role_a_rendre not in m.roles:
            try:
                await m.add_roles(role_a_rendre, reason="Fin Conseil")
                restaures += 1
                ROLES_PERSO_EN_PAUSE.pop(m.id, None)
                await asyncio.sleep(0.3)
            except Exception:
                pass

    await interaction.followup.send(f"🔓 **Conseil Désactivé !** {restaures} candidat(s) réactivé(s).", ephemeral=True)


# ========================================================
# 13. CHRONOMÈTRES & QUESTION FLASH
# ========================================================

@bot.tree.command(
    name="poser_question_flash",
    description="Pose une question flash avec décompte dynamique Discord."
)
@app_commands.describe(question="La question à poser", secondes="Temps limite en secondes")
@app_commands.check(est_orga_ou_admin)
async def poser_question_flash(interaction: discord.Interaction, question: str, secondes: int = 15):
    channel = interaction.channel
    fin_timestamp = int((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=secondes)).timestamp())

    embed = discord.Embed(
        title="⚡ QUESTION FLASH",
        description=f"**{question}**\n\n⏳ **Fin du chrono :** <t:{fin_timestamp}:R>",
        color=discord.Color.from_rgb(255, 69, 0)
    )
    await interaction.response.send_message(embed=embed)
    original_msg = await interaction.original_response()

    def check(m: discord.Message):
        return m.channel.id == channel.id and not m.author.bot

    debut = time.perf_counter()
    try:
        rep = await bot.wait_for("message", timeout=secondes, check=check)
        temps = round(time.perf_counter() - debut, 2)
        await original_msg.edit(embed=discord.Embed(title="⚡ QUESTION FLASH", description=f"**{question}**\n\n⏱️ Répondu en `{temps}s`", color=discord.Color.green()))
        await channel.send(embed=discord.Embed(title="✅ RÉPONSE VALIDÉE", description=f"💬 `{rep.content}` en `{temps}s`", color=discord.Color.green()))
    except asyncio.TimeoutError:
        await original_msg.edit(embed=discord.Embed(title="⚡ QUESTION FLASH", description=f"**{question}**\n\n🛑 **TEMPS ÉCOULÉ**", color=discord.Color.dark_red()))


@bot.tree.command(name="chrono_go", description="Lance le top départ et démarre le chronomètre.")
@app_commands.describe(cible="Le candidat ou rôle", epreuve="Nom de l'épreuve")
@app_commands.check(est_orga_ou_admin)
async def chrono_go(interaction: discord.Interaction, cible: discord.Role, epreuve: str = "Épreuve"):
    cle = f"{interaction.channel.id}_{cible.id}"
    CHRONOS_EN_COURS[cle] = time.perf_counter()
    ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    embed = discord.Embed(
        title="🟢 TOP DÉPART !",
        description=f"🎯 **Épreuve :** {epreuve}\n👤 **Cible :** {cible.mention}\n⏱️ **Début :** <t:{ts}:R>",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="chrono_stop", description="Stoppe le chronomètre et valide le temps exact.")
@app_commands.describe(cible="Le candidat ou rôle")
@app_commands.check(est_orga_ou_admin)
async def chrono_stop(interaction: discord.Interaction, cible: discord.Role):
    cle = f"{interaction.channel.id}_{cible.id}"
    if cle not in CHRONOS_EN_COURS:
        await interaction.response.send_message("❌ Aucun chronomètre actif.", ephemeral=True)
        return

    duree = time.perf_counter() - CHRONOS_EN_COURS.pop(cle)
    m, s = int(duree // 60), round(duree % 60, 2)
    texte = f"{m} min {s} s" if m > 0 else f"{s} s"

    embed = discord.Embed(
        title="🏁 TEMPS VALIDÉ !",
        description=f"👤 **Cible :** {cible.mention}\n⏱️ **Temps :** `{texte}` *({round(duree, 2)}s)*",
        color=discord.Color.gold()
    )
    await interaction.response.send_message(embed=embed)


# ========================================================
# 14. QUIZ & ÉPREUVES AUTOMATISÉES
# ========================================================

class GlobalQuizLancementView(discord.ui.View):
    def __init__(self, candidat: discord.Member, mode_affichage: str = "visuel"):
        super().__init__(timeout=1800)
        self.candidat = candidat
        self.mode_affichage = mode_affichage

    @discord.ui.button(label="🚀 DÉMARRER MON ÉPREUVE", style=discord.ButtonStyle.green, custom_id="btn_global_quiz_start")
    async def demarrer_quiz(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not CONFIG_EPREUVE_GLOBALE["active"] or interaction.user.id != self.candidat.id:
            await interaction.response.send_message("⛔ Accès non autorisé ou épreuve fermée.", ephemeral=True)
            return

        button.disabled = True
        button.label = "⏳ ÉPREUVE EN COURS..."
        await interaction.response.edit_message(view=self)

        channel = interaction.channel
        questions = CONFIG_EPREUVE_GLOBALE["questions"]
        ETATS_EPREUVES_SALONS[channel.id] = {"pause_demandee": False, "event": asyncio.Event()}
        ETATS_EPREUVES_SALONS[channel.id]["event"].set()

        msg_decompte = await channel.send("⚠️ **Début dans : 3**")
        for k in range(2, 0, -1):
            await asyncio.sleep(1)
            await msg_decompte.edit(content=f"⚠️ **Début dans : {k}**")
        await asyncio.sleep(1)
        await msg_decompte.delete()

        resultats = []

        for i, q in enumerate(questions, 1):
            if ETATS_EPREUVES_SALONS.get(channel.id, {}).get("pause_demandee"):
                msg_p = await channel.send("⏸️ **ÉPREUVE EN PAUSE**")
                await ETATS_EPREUVES_SALONS[channel.id]["event"].wait()
                try:
                    await msg_p.delete()
                except Exception:
                    pass

            duree = int(q["secondes"])
            texte_q = q["question"]

            def check_reponse(m: discord.Message):
                return m.channel.id == channel.id and m.author.id == self.candidat.id

            debut_q = time.perf_counter()
            now = datetime.datetime.now(datetime.timezone.utc)
            fin_ts = int((now + datetime.timedelta(seconds=duree)).timestamp())

            embed_q = discord.Embed(
                title=f"📋 QUESTION {i} / {len(questions)}",
                description=f"# {texte_q}\n\n⏳ Fin : <t:{fin_ts}:R> *(`{duree}s`)*",
                color=discord.Color.gold()
            )
            msg_p = await channel.send(embed=embed_q)

            try:
                rep_msg = await bot.wait_for("message", timeout=duree, check=check_reponse)
                t_pris = round(time.perf_counter() - debut_q, 2)
                resultats.append({"index": i, "question": texte_q, "reponse": rep_msg.content, "temps": t_pris, "statut": "✅ Répondu"})
                await msg_p.edit(embed=discord.Embed(title=f"📋 QUESTION {i}", description=f"# {texte_q}\n\n✅ Validé en `{t_pris}s`", color=discord.Color.green()))
            except asyncio.TimeoutError:
                resultats.append({"index": i, "question": texte_q, "reponse": "*Aucune*", "temps": float(duree), "statut": "❌ Hors délai"})
                await msg_p.edit(embed=discord.Embed(title=f"📋 QUESTION {i}", description=f"# {texte_q}\n\n🛑 TEMPS ÉCOULÉ", color=discord.Color.dark_red()))

            await asyncio.sleep(1.5)
            try:
                await msg_p.delete()
            except Exception:
                pass

        ETATS_EPREUVES_SALONS.pop(channel.id, None)
        t_total = round(sum(r["temps"] for r in resultats), 2)
        await channel.send(f"🏁 **ÉPREUVE TERMINÉE !** Temps total cumulé : `{t_total}s`.")

        res_ch = bot.get_channel(RESULTATS_CHANNEL_ID)
        if res_ch:
            lignes = [f"⏱️ **TEMPS TOTAL :** `{t_total}s`\n━━━━━━━━━━━━━━"]
            for r in resultats:
                lignes.append(f"**Q{r['index']}. {r['question']}**\n💬 `{r['reponse']}` ({r['statut']} en `{r['temps']}s`)")
            await res_ch.send(embed=discord.Embed(title=f"📊 RÉSULTATS — {self.candidat.display_name}", description="\n".join(lignes), color=discord.Color.gold()))


@bot.tree.command(
    name="configurer_epreuve",
    description="Étape 1 : Charge les questions de l'épreuve depuis un salon secret."
)
@app_commands.describe(salon_questions="Salon contenant les questions", temps_par_defaut="Temps en secondes")
@app_commands.check(est_orga_ou_admin)
async def configurer_epreuve(interaction: discord.Interaction, salon_questions: discord.TextChannel, temps_par_defaut: int = 15):
    await interaction.response.defer(ephemeral=True)
    global CONFIG_EPREUVE_GLOBALE
    questions = []

    async for msg in salon_questions.history(limit=25, oldest_first=False):
        if not msg.author.bot and msg.content.strip():
            for ligne in msg.content.strip().split("\n"):
                if "|" in ligne:
                    p = ligne.split("|")
                    try:
                        questions.append({"question": p[0].strip(), "secondes": int(p[1].strip())})
                    except ValueError:
                        questions.append({"question": p[0].strip(), "secondes": temps_par_defaut})
                elif ligne.strip():
                    questions.append({"question": ligne.strip(), "secondes": temps_par_defaut})
            if questions:
                break

    if not questions:
        await interaction.followup.send("❌ Aucune question trouvée.", ephemeral=True)
        return

    CONFIG_EPREUVE_GLOBALE = {"questions": questions, "temps_par_defaut": temps_par_defaut, "active": True}
    await interaction.followup.send(f"✅ **{len(questions)} questions chargées !** Lancez `/lancer_epreuve`.", ephemeral=True)


@bot.tree.command(
    name="lancer_epreuve",
    description="Étape 2 : Déploie l'épreuve pour un candidat ou une catégorie."
)
@app_commands.describe(candidat="Candidat ciblé", nom_categorie="Optionnel : catégorie entière")
@app_commands.check(est_orga_ou_admin)
async def lancer_epreuve(interaction: discord.Interaction, candidat: discord.Member = None, nom_categorie: str = None):
    await interaction.response.defer(ephemeral=True)
    if not CONFIG_EPREUVE_GLOBALE["active"]:
        await interaction.followup.send("❌ Aucune épreuve active configurée.", ephemeral=True)
        return

    if candidat:
        view = GlobalQuizLancementView(candidat=candidat)
        embed = discord.Embed(title="🏺 ÉPREUVE DE RAPIDITÉ", description=f"Prêt {candidat.mention} ? Clique ci-dessous :", color=discord.Color.dark_gold())
        await interaction.channel.send(embed=embed, view=view)
        await interaction.followup.send("✅ Épreuve déployée !", ephemeral=True)
    else:
        await interaction.followup.send("❌ Spécifiez au moins un candidat.", ephemeral=True)


# ========================================================
# 15. PRÉSENTATIONS (CANDIDATS & ORGAS)
# ========================================================

async def analyser_candidat_ia(texte: str) -> dict:
    """Extrait le prénom et nettoie le texte via Gemini."""
    if not texte:
        return {"nom": "Candidat", "texte": ""}

    prompt = (
        "Voici une présentation :\n\n"
        f"\"\"\"{texte}\"\"\"\n\n"
        "1. Donne le prénom seul.\n"
        "2. Corrige les fautes sans modifier le style.\n"
        "Format :\nPRENOM: <prénom>\nTEXTE: <texte corrigé>"
    )

    try:
        response = await asyncio.to_thread(gemini_client.models.generate_content, model=MODEL_NAME, contents=prompt)
        rep = response.text.strip()
        prenom_match = re.search(r"PRENOM\s*:\s*\**([A-Za-zÀ-ÿ\-]+)\**", rep, re.IGNORECASE)
        prenom = prenom_match.group(1).capitalize() if prenom_match else "Candidat"
        texte_clean = rep.split("TEXTE:", 1)[1].strip() if "TEXTE:" in rep else texte.strip()
        return {"nom": prenom, "texte": texte_clean}
    except Exception:
        return {"nom": "Candidat", "texte": texte.strip()}


def creer_embed_presentation_pure(prenom: str, texte: str, image_url: str = None) -> discord.Embed:
    embed = discord.Embed(title=f"🌴 {prenom.upper()}", description=texte, color=discord.Color.gold())
    if image_url:
        embed.set_image(url=image_url)
    embed.set_footer(text=f"Aventurier : {prenom} • Fiche officielle")
    return embed


def creer_embed_presentation_orga(prenom: str, texte: str, image_url: str = None) -> discord.Embed:
    embed = discord.Embed(title=f"🛠️ {prenom.upper()} — ORGANISATION", description=texte, color=discord.Color.red())
    if image_url:
        embed.set_image(url=image_url)
    embed.set_footer(text=f"Organisateur : {prenom} • Fiche Staff")
    return embed


@bot.tree.command(
    name="formater_presentation",
    description="Publie la fiche propre d'un candidat avec sa photo."
)
@app_commands.describe(message_id_ou_lien="ID ou lien Discord du message", salon_destination="Salon de destination")
@app_commands.check(est_orga_ou_admin)
async def formater_presentation(interaction: discord.Interaction, message_id_ou_lien: str, salon_destination: discord.TextChannel = None):
    await interaction.response.defer(ephemeral=True)
    msg_id = int(message_id_ou_lien.strip().split("/")[-1])
    dest = salon_destination or interaction.channel
    source_msg = await interaction.channel.fetch_message(msg_id)

    img_url = source_msg.attachments[0].url if source_msg.attachments else None
    res = await analyser_candidat_ia(source_msg.content)
    embed = creer_embed_presentation_pure(res["nom"], res["texte"], img_url)

    await dest.send(embed=embed)
    await interaction.followup.send("✅ Fiche publiée !", ephemeral=True)


# ========================================================
# 16. RELECTURE & CORRECTION DE QUESTIONS
# ========================================================

@bot.tree.command(
    name="corriger_salon_questions",
    description="Corrige les fautes des questions et analyse les ambiguïtés pour les orgas."
)
@app_commands.describe(salon_questions="Optionnel : salon contenant les questions")
@app_commands.check(est_orga_ou_admin)
async def corriger_salon_questions(interaction: discord.Interaction, salon_questions: discord.TextChannel = None):
    await interaction.response.defer(ephemeral=True)
    target = salon_questions or interaction.channel
    msg_cible = None

    async for msg in target.history(limit=10, oldest_first=False):
        if not msg.author.bot and msg.content.strip():
            msg_cible = msg
            break

    if not msg_cible:
        await interaction.followup.send("❌ Aucun message trouvé.", ephemeral=True)
        return

    prompt = (
        "Corrige l'orthographe et la syntaxe de ces questions de quiz en conservant les timers (| 15) :\n\n"
        f"{msg_cible.content}\n\n"
        "Format :\n===QUESTIONS===\n[Questions corrigées]\n===ANALYSE===\n[Points d'ambiguïté détectés]"
    )

    response = await asyncio.to_thread(gemini_client.models.generate_content, model=MODEL_NAME, contents=prompt)
    rep = response.text.strip()
    parties = rep.split("===ANALYSE===")
    questions_corrigees = parties[0].replace("===QUESTIONS===", "").strip()
    analyse = parties[1].strip() if len(parties) > 1 else "Aucune ambiguïté."

    await msg_cible.delete()
    await target.send(questions_corrigees)

    salon_remarques = bot.get_channel(SALON_REMARQUES_QUESTIONS_ID)
    if salon_remarques:
        await salon_remarques.send(embed=discord.Embed(title=f"🔎 Audit Questions — #{target.name}", description=analyse, color=discord.Color.orange()))

    await interaction.followup.send("✅ Questions corrigées et audit transmis !", ephemeral=True)


# ========================================================
# 17. ANNONCES CANDIDATS
# ========================================================

@bot.tree.command(
    name="formater_annonce",
    description="Met en page l'annonce dans le salon de travail avec bloc copiable."
)
@app_commands.describe(nombre_messages="Nombre de messages à fusionner")
@app_commands.check(est_orga_ou_admin)
async def formater_annonce(interaction: discord.Interaction, nombre_messages: int = 1):
    await interaction.response.defer(ephemeral=True)
    dest = bot.get_channel(SALON_ANNONCES_TRAVAIL_ID)
    msgs = [m.content.strip() async for m in interaction.channel.history(limit=nombre_messages) if not m.author.bot and m.content.strip()]
    msgs.reverse()

    prompt = f"Aère et formate cette annonce pour Discord de façon sobre et percutante :\n\n{' '.join(msgs)}"
    res = await asyncio.to_thread(gemini_client.models.generate_content, model=MODEL_NAME, contents=prompt)
    texte = res.text.strip()

    await dest.send(texte)
    await dest.send(f"📋 **Bloc copiable :**\n```{texte[:1800]}```")
    await interaction.followup.send(f"✅ Envoyé dans {dest.mention} !", ephemeral=True)


@bot.tree.command(
    name="publier_annonce",
    description="Publie l'annonce validée chez les candidats et l'archive."
)
@app_commands.check(est_orga_ou_admin)
async def publier_annonce(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    src = bot.get_channel(SALON_ANNONCES_TRAVAIL_ID)
    dest = bot.get_channel(SALON_ANNONCES_CANDIDATS_ID)
    arch = bot.get_channel(SALON_ARCHIVES_ANNONCES_ID)

    msgs = [m async for m in src.history(limit=20) if not m.author.bot and not m.content.startswith("📋")]
    if not msgs:
        await interaction.followup.send("❌ Aucune annonce trouvée.", ephemeral=True)
        return

    texte = "\n\n".join([m.content for m in reversed(msgs)])
    await dest.send(texte)
    if arch:
        await arch.send(embed=discord.Embed(title="📦 ARCHIVE ANNONCE", description=texte[:3900], color=discord.Color.blue()))

    for m in msgs:
        try:
            await m.delete()
        except Exception:
            pass

    await interaction.followup.send(f"✅ Annonce publiée dans {dest.mention} et archivée !", ephemeral=True)


# ========================================================
# 18. HUMOUR, ROAST & PRAISE
# ========================================================

@bot.tree.command(name="roast", description="Envoie un roast drôle et bienveillant (100% second degré).")
@app_commands.describe(cible="Membre à taquiner", contexte="Optionnel : contexte")
@app_commands.check(est_orga_ou_admin)
async def roast_cmd(interaction: discord.Interaction, cible: discord.Member, contexte: str = None):
    await interaction.response.defer()
    prompt = f"Fais un roast bienveillant et drôle sur {cible.display_name} sur Discord. Contexte: {contexte or 'Jeu communautaire'}."
    res = await asyncio.to_thread(gemini_client.models.generate_content, model=MODEL_NAME, contents=prompt)
    await interaction.followup.send(f"🔥 **ROAST —** {cible.mention}\n\n{res.text.strip()}")


@bot.tree.command(name="praise", description="Envoie un compliment ultra-exagéré et théâtral.")
@app_commands.describe(cible="Membre à glorifier", contexte="Optionnel : contexte")
@app_commands.check(est_orga_ou_admin)
async def praise_cmd(interaction: discord.Interaction, cible: discord.Member, contexte: str = None):
    await interaction.response.defer()
    prompt = f"Fais une ode lyrique et hilarante pour glorifier {cible.display_name}. Contexte: {contexte or 'Carry absolu'}."
    res = await asyncio.to_thread(gemini_client.models.generate_content, model=MODEL_NAME, contents=prompt)
    await interaction.followup.send(f"👑 **PRAISE & GLOIRE —** {cible.mention}\n\n{res.text.strip()}")


# ========================================================
# 19. ANALYSES LEXICALES & STATISTIQUES
# ========================================================

@bot.tree.command(
    name="stats_mots_candidats",
    description="Top des mots les plus prononcés par les candidats avec la répartition."
)
@app_commands.describe(top="Nombre de mots (défaut : 10)", longueur_min="Taille minimale (défaut : 4)")
@app_commands.check(est_orga_ou_admin)
async def stats_mots_candidats(interaction: discord.Interaction, top: int = 10, longueur_min: int = 4):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)

    candidats_ids = {m.id: m.display_name for m in guild.members if not m.bot and (not role_orgas or role_orgas not in m.roles) and (not role_spectateurs or role_spectateurs not in m.roles)}
    compteur = Counter()
    auteurs = defaultdict(lambda: Counter())

    for ch in guild.text_channels:
        if est_categorie_candidate(ch.category):
            async for msg in ch.history(limit=300):
                if msg.author.id in candidats_ids and msg.content.strip():
                    mots = nettoyer_texte(msg.content).split()
                    for m in mots:
                        if len(m) >= longueur_min and m not in MOTS_VIDES_FR and not m.isdigit():
                            compteur[m] += 1
                            auteurs[m][msg.author.id] += 1

    lignes = []
    for rank, (mot, total) in enumerate(compteur.most_common(min(top, 25)), 1):
        top_auteurs = ", ".join([f"**{candidats_ids[uid]}** ({c})" for uid, c in auteurs[mot].most_common(3)])
        lignes.append(f"`#{rank}` **{mot.upper()}** ({total}x) ➔ {top_auteurs}")

    embed = discord.Embed(title="📊 TOP DES MOTS LES PLUS PRONONCÉS", description="\n".join(lignes) if lignes else "Aucun mot trouvé.", color=discord.Color.purple())
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(
    name="chercher_mot_candidats",
    description="Recherche un mot précis chez les candidats et affiche publiquement le classement."
)
@app_commands.describe(mot="Le mot exact à rechercher")
@app_commands.check(est_orga_ou_admin)
async def chercher_mot_candidats(interaction: discord.Interaction, mot: str):
    await interaction.response.defer(ephemeral=False)
    guild = interaction.guild
    mot_clean = nettoyer_texte(mot)
    pattern = re.compile(rf'\b{re.escape(mot_clean)}\b', re.IGNORECASE)

    role_spectateurs = discord.utils.get(guild.roles, name=ROLE_SPECTATEURS_NAME)
    role_orgas = discord.utils.get(guild.roles, name=ROLE_ORGAS_NAME)
    candidats = {m.id: {"nom": m.display_name, "count": 0} for m in guild.members if not m.bot and (not role_orgas or role_orgas not in m.roles) and (not role_spectateurs or role_spectateurs not in m.roles)}

    for ch in guild.text_channels:
        if est_categorie_candidate(ch.category):
            async for msg in ch.history(limit=300):
                if msg.author.id in candidats and msg.content.strip():
                    occ = len(pattern.findall(nettoyer_texte(msg.content)))
                    if occ > 0:
                        candidats[msg.author.id]["count"] += occ

    actifs = sorted([c for c in candidats.values() if c["count"] > 0], key=lambda x: x["count"], reverse=True)
    lignes = [f"**{c['nom']}** : **{c['count']}** fois" for c in actifs]

    embed = discord.Embed(
        title=f"🔍 CLASSEMENT DU MOT : « {mot.upper()} »",
        description="\n".join(lignes) if lignes else "Mot jamais prononcé par les candidats.",
        color=discord.Color.gold()
    )
    await interaction.followup.send(embed=embed)


# ========================================================
# 20. BILANS CANDIDATS & ORGANISATEURS
# ========================================================

async def generer_bilan_candidat_ia(candidat_nom: str, messages_candidat: list[str], messages_sur_candidat: list[str]) -> str:
    """Génère l'évaluation complète d'un joueur Discord."""
    prompt = (
        f"🎯 CANDIDAT ÉVALUÉ : **{candidat_nom}**\n\n"
        f"CE QU'IL A DIT :\n{chr(10).join(messages_candidat[:100])}\n\n"
        f"CE QUE LES AUTRES ONT DIT SUR LUI :\n{chr(10).join(messages_sur_candidat[:100])}\n\n"
        "Fais un bilan structuré sur un jeu Discord :\n"
        "1. Note sur 10 argumentée\n"
        "2. Positionnement stratégique & Alliances réelles vs Illusions\n"
        "3. Forces & Faiblesses\n"
        "4. Bêtisier & Dingueries"
    )
    res = await asyncio.to_thread(gemini_client.models.generate_content, model=MODEL_NAME, contents=prompt)
    return res.text.strip()


@bot.tree.command(
    name="bilan_candidat",
    description="Bilan complet d'un candidat : Note sur 10, analyse stratégique et bêtisier."
)
@app_commands.describe(candidat="Le candidat à évaluer", limite_par_salon="Messages à scanner par salon")
@app_commands.check(est_orga_ou_admin)
async def bilan_candidat(interaction: discord.Interaction, candidat: discord.Member, limite_par_salon: int = 80):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    nom_clean = nettoyer_texte(candidat.display_name)

    msgs_cand = []
    msgs_sur = []

    for ch in guild.text_channels:
        if est_categorie_candidate(ch.category):
            async for msg in ch.history(limit=limite_par_salon):
                if msg.author.id == candidat.id:
                    msgs_cand.append(f"[#{ch.name}] {candidat.display_name}: {msg.content}")
                elif nom_clean in nettoyer_texte(msg.content):
                    msgs_sur.append(f"[#{ch.name}] {msg.author.display_name}: {msg.content}")

    rapport = await generer_bilan_candidat_ia(candidat.display_name, msgs_cand, msgs_sur)
    salon_dest = bot.get_channel(SALON_BILAN_CANDIDATS_ID) or interaction.channel

    embed = discord.Embed(title=f"📊 BILAN D'AVENTURE — {candidat.display_name.upper()}", description=rapport[:3900], color=discord.Color.gold())
    embed.set_thumbnail(url=candidat.display_avatar.url)
    await salon_dest.send(embed=embed)
    await interaction.followup.send(f"✅ Bilan envoyé dans {salon_dest.mention} !", ephemeral=True)


async def generer_bilan_orga_ia(orga_nom: str, messages_orga: list[str], messages_sur_orga: list[str]) -> str:
    """Génère l'audit staff d'un orga."""
    prompt = (
        f"🎯 ORGA ÉVALUÉ : **{orga_nom}**\n\n"
        f"SES ACTIONS/MESSAGES :\n{chr(10).join(messages_orga[:100])}\n\n"
        f"AVIS DU SERVEUR :\n{chr(10).join(messages_sur_orga[:100])}\n\n"
        "Rédige l'audit d'organisation :\n"
        "1. Note sur 10 globale\n"
        "2. Objectivité, Présence & Apport logistique\n"
        "3. Points forts & Fails\n"
        "4. Bêtisier de l'orga"
    )
    res = await asyncio.to_thread(gemini_client.models.generate_content, model=MODEL_NAME, contents=prompt)
    return res.text.strip()


@bot.tree.command(
    name="bilan_orga",
    description="Bilan complet d'un orga : Note sur 10, analyse des critères clés et bêtisier."
)
@app_commands.describe(orga="Le membre du staff à évaluer", limite_par_salon="Messages à scanner par salon")
@app_commands.check(est_orga_ou_admin)
async def bilan_orga(interaction: discord.Interaction, orga: discord.Member, limite_par_salon: int = 80):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    nom_clean = nettoyer_texte(orga.display_name)

    msgs_orga = []
    msgs_sur = []

    for ch in guild.text_channels:
        async for msg in ch.history(limit=limite_par_salon):
            if msg.author.id == orga.id:
                msgs_orga.append(f"[#{ch.name}] {orga.display_name}: {msg.content}")
            elif nom_clean in nettoyer_texte(msg.content):
                msgs_sur.append(f"[#{ch.name}] {msg.author.display_name}: {msg.content}")

    rapport = await generer_bilan_orga_ia(orga.display_name, msgs_orga, msgs_sur)
    salon_dest = bot.get_channel(SALON_BILAN_ORGAS_ID) or interaction.channel

    embed = discord.Embed(title=f"🛠️ BILAN D'ORGANISATEUR — {orga.display_name.upper()}", description=rapport[:3900], color=discord.Color.red())
    embed.set_thumbnail(url=orga.display_avatar.url)
    await salon_dest.send(embed=embed)
    await interaction.followup.send(f"✅ Bilan orga envoyé dans {salon_dest.mention} !", ephemeral=True)


# ========================================================
# 21. SALONS LECTURE SEULE & FOCUS
# ========================================================

@bot.tree.command(
    name="creer_salon_annonces_groupe",
    description="Crée un salon textuel où tous les candidats choisis sont en lecture seule."
)
@app_commands.describe(nom_salon="Nom du salon", nom_categorie="Nom de la catégorie", candidat_1="1er candidat", candidat_2="2ème candidat")
@app_commands.check(est_orga_ou_admin)
async def creer_salon_annonces_groupe(
    interaction: discord.Interaction,
    nom_salon: str,
    nom_categorie: str,
    candidat_1: discord.Member,
    candidat_2: discord.Member,
    candidat_3: discord.Member = None,
    candidat_4: discord.Member = None,
    candidat_5: discord.Member = None
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    cat = discord.utils.find(lambda c: nettoyer_texte(c.name) == nettoyer_texte(nom_categorie), guild.categories) or await guild.create_category(nom_categorie)

    participants = [c for c in [candidat_1, candidat_2, candidat_3, candidat_4, candidat_5] if c]
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)
    }

    perms_ro = discord.PermissionOverwrite(view_channel=True, read_messages=True, send_messages=False, add_reactions=False)
    for p in participants:
        r = trouver_role_personnel(p)
        overwrites[r if r else p] = perms_ro

    ch = await guild.create_text_channel(name=f"📢・{formater_nom_salon(nom_salon)}", category=cat, overwrites=overwrites)
    await ch.send(f"📢 **SALON D'ANNONCES**\nBienvenue {', '.join([p.mention for p in participants])} *(lecture seule)*.")
    await interaction.followup.send(f"✅ Salon créé : {ch.mention}", ephemeral=True)


@bot.tree.command(
    name="creer_salon_focus_candidat",
    description="Crée un salon où 1 candidat écrit et les autres sont en lecture seule."
)
@app_commands.describe(nom_salon="Nom du salon", nom_categorie="Catégorie", candidat_actif="Candidat qui écrit", observateur_1="Observateur")
@app_commands.check(est_orga_ou_admin)
async def creer_salon_focus_candidat(
    interaction: discord.Interaction,
    nom_salon: str,
    nom_categorie: str,
    candidat_actif: discord.Member,
    observateur_1: discord.Member,
    observateur_2: discord.Member = None,
    observateur_3: discord.Member = None
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    cat = discord.utils.find(lambda c: nettoyer_texte(c.name) == nettoyer_texte(nom_categorie), guild.categories) or await guild.create_category(nom_categorie)

    observateurs = [o for o in [observateur_1, observateur_2, observateur_3] if o and o.id != candidat_actif.id]
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)
    }

    r_act = trouver_role_personnel(candidat_actif)
    overwrites[r_act if r_act else candidat_actif] = discord.PermissionOverwrite(view_channel=True, read_messages=True, send_messages=True)

    perms_ro = discord.PermissionOverwrite(view_channel=True, read_messages=True, send_messages=False, add_reactions=False)
    for o in observateurs:
        r_o = trouver_role_personnel(o)
        overwrites[r_o if r_o else o] = perms_ro

    ch = await guild.create_text_channel(name=f"🎯・{formater_nom_salon(nom_salon)}", category=cat, overwrites=overwrites)
    await ch.send(f"🎯 **SALON FOCUS : {candidat_actif.mention}** *(écriture)* | Observateurs : {', '.join([o.mention for o in observateurs])} *(lecture seule)*")
    await interaction.followup.send(f"✅ Salon créé : {ch.mention}", ephemeral=True)


# ========================================================
# 22. MINUTEUR D'ÉPREUVE AVEC TEMPS ADDITIONNEL
# ========================================================

async def boucle_minuteur(channel: discord.TextChannel, total_seconds: int):
    start_time = time.perf_counter()
    alertes = []
    if total_seconds > 120:
        alertes.append((total_seconds // 2, f"⏳ **MI-TEMPS !** Il reste **{(total_seconds // 2) // 60} min**."))
    if total_seconds > 300:
        alertes.append((300, "⚠️ **Plus que 5 minutes !**"))
    if total_seconds >= 60:
        alertes.append((60, "🚨 **Dernière minute !**"))
    for s in [30, 10, 5, 4, 3, 2, 1]:
        if total_seconds >= s:
            alertes.append((s, f"🔥 **{s}...**" if s <= 5 else f"⏱️ **{s} secondes !**"))

    alertes.sort(key=lambda x: x[0], reverse=True)

    try:
        for t_restant, msg_alerte in alertes:
            attente = (total_seconds - t_restant) - (time.perf_counter() - start_time)
            if attente > 0:
                await asyncio.sleep(attente)
                await channel.send(msg_alerte)

        fin_reg = total_seconds - (time.perf_counter() - start_time)
        if fin_reg > 0:
            await asyncio.sleep(fin_reg)

        await channel.send(embed=discord.Embed(title="⏱️ TEMPS IMPARTI ÉCOULÉ — TEMPS ADDITIONNEL !", description="Le chrono tourne jusqu'à `/chrono_minuteur_stop`.", color=discord.Color.orange()))

        extra = 0
        while True:
            await asyncio.sleep(120)
            extra += 2
            ecoule = time.perf_counter() - start_time
            await channel.send(f"⏱️ **TEMPS ADDITIONNEL (+{extra} min)** — Chrono global : `{int(ecoule // 60)} min {int(ecoule % 60)}s`")
    except asyncio.CancelledError:
        pass


@bot.tree.command(name="chrono_minuteur", description="Lance un minuteur avec alertes et temps additionnel automatique.")
@app_commands.describe(minutes="Durée en minutes", epreuve="Nom de l'épreuve")
@app_commands.check(est_orga_ou_admin)
async def chrono_minuteur(interaction: discord.Interaction, minutes: int, epreuve: str = "Épreuve"):
    channel = interaction.channel
    if channel.id in MINUTEURS_ACTIFS:
        await interaction.response.send_message("⚠️ Un minuteur est déjà en cours. Utilisez `/chrono_minuteur_stop`.", ephemeral=True)
        return

    sec_totales = minutes * 60
    fin_ts = int((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=sec_totales)).timestamp())
    embed = discord.Embed(title="⏱️ TOP DÉPART DU MINUTEUR !", description=f"🎯 **Épreuve :** {epreuve}\n⏳ **Durée :** `{minutes} min`\n🏁 **Fin :** <t:{fin_ts}:R>", color=discord.Color.green())

    task = asyncio.create_task(boucle_minuteur(channel, sec_totales))
    MINUTEURS_ACTIFS[channel.id] = {"task": task, "start_perf": time.perf_counter(), "total_seconds": sec_totales}

    await interaction.response.send_message(f"✅ Minuteur de {minutes} min lancé !", ephemeral=True)
    await channel.send(embed=embed)


@bot.tree.command(name="chrono_minuteur_stop", description="Arrête le minuteur et donne le temps complet depuis le début.")
@app_commands.check(est_orga_ou_admin)
async def chrono_minuteur_stop(interaction: discord.Interaction):
    channel = interaction.channel
    if channel.id not in MINUTEURS_ACTIFS:
        await interaction.response.send_message("❌ Aucun minuteur actif dans ce salon.", ephemeral=True)
        return

    data = MINUTEURS_ACTIFS.pop(channel.id)
    data["task"].cancel()
    total = time.perf_counter() - data["start_perf"]
    m, s = int(total // 60), round(total % 60, 2)
    texte = f"{m} min {s} s" if m > 0 else f"{s} s"

    desc = f"🛑 **Chrono stoppé !**\n\n⏱️ **TEMPS TOTAL CUMULÉ :** `{texte}` *({round(total, 2)}s)*\n⏳ **Temps réglementaire :** `{data['total_seconds'] // 60} min`"
    if total > data["total_seconds"]:
        diff = total - data["total_seconds"]
        desc += f"\n🚨 **Temps additionnel :** `+{int(diff // 60)} min {round(diff % 60, 2)}s`"

    await channel.send(embed=discord.Embed(title="🏁 RÉSULTAT CHRONO", description=desc, color=discord.Color.gold()))
    await interaction.response.send_message("✅ Minuteur arrêté !", ephemeral=True)


# ========================================================
# 23. DÉCODEUR HUMORISTIQUE
# ========================================================

@bot.tree.command(name="lancer_decodeur", description="Active la traduction automatique humoristique d'un membre.")
@app_commands.describe(cible="Le membre surveillé")
@app_commands.check(est_orga_ou_admin)
async def lancer_decodeur(interaction: discord.Interaction, cible: discord.Member):
    SESSIONS_DECODEUR[interaction.channel.id] = {"user_id": cible.id, "nom": cible.display_name}
    await interaction.channel.send(f"🌐 **DÉCODEUR ACTIVÉ SUR {cible.mention} !**")
    await interaction.response.send_message("✅ Décodeur enclenché !", ephemeral=True)


@bot.tree.command(name="arreter_decodeur", description="Désactive la traduction automatique.")
@app_commands.check(est_orga_ou_admin)
async def arreter_decodeur(interaction: discord.Interaction):
    if interaction.channel.id in SESSIONS_DECODEUR:
        SESSIONS_DECODEUR.pop(interaction.channel.id)
        await interaction.channel.send("🔌 **Décodeur désactivé.**")
        await interaction.response.send_message("✅ Décodeur arrêté !", ephemeral=True)
    else:
        await interaction.response.send_message("ℹ️ Aucun décodeur actif.", ephemeral=True)


# ========================================================
# 24. UNE DE JOURNAL SATIRIQUE
# ========================================================

async def extraire_articles_une_ia(transcriptions: str) -> dict:
    prompt = (
        f"Génère les titres satiriques de la Une du journal L'Équipe d'après ces discussions Discord :\n{transcriptions[:20000]}\n\n"
        "Format strict :\n"
        "TITRE_PRINCIPAL: <Gros titre 4 mots>\n"
        "SOUS_TITRE_PRINCIPAL: <Explication 2 phrases>\n"
        "CANDIDAT_PRINCIPAL: <Prénom>\n"
        "ENCART_1_CATEGORIE: <Mot>\nENCART_1_TITRE: <Citation>\nENCART_1_CANDIDAT: <Prénom>\n"
        "ENCART_2_CATEGORIE: <Mot>\nENCART_2_TITRE: <Citation>\nENCART_2_CANDIDAT: <Prénom>\n"
        "ENCART_3_CATEGORIE: <Mot>\nENCART_3_TITRE: <Citation>\nENCART_3_CANDIDAT: <Prénom>"
    )
    res = await asyncio.to_thread(gemini_client.models.generate_content, model=MODEL_NAME, contents=prompt)
    data = {}
    for line in res.text.strip().split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            data[k.strip()] = v.strip()
    return data


def creer_image_une(donnees_une: dict, photos_candidats: dict) -> io.BytesIO:
    largeur, hauteur = 900, 1300
    fond = Image.new("RGB", (largeur, hauteur), color="#FFFFFF")
    draw = ImageDraw.Draw(fond)

    try:
        font_logo = ImageFont.truetype("fonts/Anton-Regular.ttf", 60)
        font_gros = ImageFont.truetype("fonts/Anton-Regular.ttf", 44)
        font_rub = ImageFont.truetype("fonts/Roboto-Bold.ttf", 16)
        font_txt = ImageFont.truetype("fonts/Roboto-Regular.ttf", 14)
        font_mini = ImageFont.truetype("fonts/Roboto-Regular.ttf", 11)
    except Exception:
        font_logo = font_gros = font_rub = font_txt = font_mini = ImageFont.load_default()

    draw.rectangle([(0, 0), (largeur, 22)], fill="#F0F0F0")
    draw.text((15, 4), "N° 24 566 • ÉDITION SPÉCIALE SERVEUR", fill="#444", font=font_mini)

    draw.rectangle([(15, 30), (280, 95)], fill="#E30613")
    draw.text((25, 26), "L'ÉQUIPE", fill="#FFF", font=font_logo)
    draw.text((295, 50), "LE QUOTIDIEN DU SERVEUR ET DE LA STRATÉGIE", fill="#222", font=font_rub)
    draw.line([(0, 105), (largeur, 105)], fill="#000", width=2)

    cat1 = donnees_une.get("ENCART_1_CATEGORIE", "ACTU").upper()
    cand1 = donnees_une.get("ENCART_1_CANDIDAT", "Candidat")
    draw.text((15, 115), cat1, fill="#E30613", font=font_rub)
    draw.text((15, 135), f"{cand1} : {donnees_une.get('ENCART_1_TITRE', '')[:60]}", fill="#222", font=font_txt)
    if cand1 in photos_candidats:
        fond.paste(photos_candidats[cand1].resize((80, 80)), (320, 115))

    draw.line([(0, 210), (largeur, 210)], fill="#000", width=2)

    cand_p = donnees_une.get("CANDIDAT_PRINCIPAL", "")
    if cand_p in photos_candidats:
        fond.paste(photos_candidats[cand_p].resize((560, 440)), (15, 225))
    else:
        draw.rectangle([(15, 225), (575, 665)], fill="#EEE")

    titre_p = donnees_une.get("TITRE_PRINCIPAL", "C'EST ENCORE LOUPE !").upper()
    draw.text((15, 680), titre_p, fill="#000", font=font_gros)

    st = textwrap.wrap(donnees_une.get("SOUS_TITRE_PRINCIPAL", ""), width=55)
    y_st = 740
    for l in st[:3]:
        draw.text((15, y_st), l, fill="#333", font=font_txt)
        y_st += 20

    buf = io.BytesIO()
    fond.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf


@bot.tree.command(name="generer_une_journal", description="Génère la Une satirique du journal L'Équipe.")
@app_commands.check(est_orga_ou_admin)
async def generer_une_journal(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    transcripts = []
    for ch in guild.text_channels:
        if est_categorie_candidate(ch.category):
            async for msg in ch.history(limit=40):
                if not msg.author.bot and msg.content.strip():
                    transcripts.append(f"{msg.author.display_name}: {msg.content.strip()}")

    if not transcripts:
        await interaction.followup.send("❌ Pas assez d'activité.", ephemeral=True)
        return

    data = await extraire_articles_une_ia("\n".join(transcripts))
    photos = {}
    async with aiohttp.ClientSession() as session:
        for m in guild.members:
            if not m.bot:
                try:
                    async with session.get(m.display_avatar.url) as r:
                        if r.status == 200:
                            img = Image.open(io.BytesIO(await r.read())).convert("RGB")
                            photos[m.display_name] = img
                            photos[m.display_name.split()[0]] = img
                except Exception:
                    pass

    buf = creer_image_une(data, photos)
    await interaction.channel.send(file=discord.File(buf, filename="une_journal.jpg"))
    await interaction.followup.send("✅ Une publiée !", ephemeral=True)


# ==========================================
# DÉMARRAGE DU BOT (AVEC RETRY ROBUSTE)
# ==========================================

def lancer_bot():
    tentatives = 0
    while True:
        try:
            print("🚀 Démarrage du bot Discord...")
            bot.run(TOKEN)
            break
        except Exception as e:
            tentatives += 1
            attente = min(15 * tentatives, 120)
            print(f"⚠️ Erreur de passerelle ou réseau ({e}). Nouvelle tentative dans {attente}s...")
            time.sleep(attente)

if __name__ == "__main__":
    lancer_bot()
