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
# 0. GESTION DES POLICES (POUR LA UNE)
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
            print(f"⚠️ Impossible de télécharger {path_font}: {e}")

# ==========================================
# 1. CONFIG & VARIABLES GLOBALES
# ==========================================
TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = "gemini-2.5-flash"

RECAP_CHANNEL_ID = 1545076756384579726
SALON_QUESTIONS_RECAP_ID = 1546598533333909728
SALON_BILAN_CANDIDATS_ID = 1549193526007435345
SALON_BILAN_ORGAS_ID = 1549193526007435345
CATEGORY_TRIO_ID = 1541397070898921482
CATEGORY_QUATUOR_ID = 1541397227744927835
RESULTATS_CHANNEL_ID = 1545186500960985148
SALON_REMARQUES_QUESTIONS_ID = 1545503543405060178
SALON_CHAT_SPECTATEURS_ID = 1544355721024635061
SALON_RECAP_SPECTATEURS_ID = 1546598600555888670
SALON_ANNONCES_TRAVAIL_ID = 1545823720676003890
SALON_ANNONCES_CANDIDATS_ID = 1537439670340681828
SALON_ARCHIVES_ANNONCES_ID = 1545823386700087456
SALON_PRESENTATION_ORGAS_ID = 1546603467580383332

MAX_CHANNELS_PER_CATEGORY = 45
ROLE_SPECTATEURS_NAME = "Spectateurs"
ROLE_ORGAS_NAME = "Orgas"
NOM_CATEGORIE_ARCHIVE = "📦 ARCHIVES DUOS"

CATEGORIES_CIBLES = ["confessional", "confessionnal", "camps", "camp", "duo jaune", "duo rouge", "trio", "quatuor", "equipe rouge", "equipe jaune", "destin lies", "destins lies", "destin lie", "Alliés de l’ombre"]
ROLES_GENERIQUES_A_IGNORER = ["arrivants", "everyone", "@everyone", "candidat", "candidats", "spectateur", "spectateurs", "orga", "orgas", "admin", "administrateur", "bot", "booster"]
MOTS_VIDES_FR = {"le", "la", "les", "un", "une", "des", "du", "de", "d", "l", "au", "aux", "et", "ou", "mais", "donc", "car", "ni", "or", "si", "que", "qui", "quoi", "dont", "ou", "quand", "comment", "pourquoi", "est", "sont", "a", "ont", "ai", "as", "suis", "es", "etre", "avoir", "faire", "fait", "je", "tu", "il", "elle", "on", "nous", "vous", "ils", "elles", "me", "te", "se", "lui", "leur", "y", "en", "ce", "cet", "cette", "ces", "mon", "ton", "son", "ma", "ta", "sa", "mes", "tes", "ses", "notre", "votre", "nos", "vos", "pour", "dans", "sur", "par", "avec", "sans", "sous", "vers", "chez", "tout", "tous", "toute", "toutes", "plus", "moins", "tres", "bien", "aussi", "trop", "peu", "pas", "ne", "non", "oui", "ca", "cest", "c", "va", "vais", "vas", "vont", "meme", "comme", "alors", "apres", "avant"}

gemini_client = genai.Client(api_key=GEMINI_KEY)
intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

DERNIERS_BINOMES_TIRES = []
ROLES_PERSO_EN_PAUSE = {}
CHRONOS_EN_COURS = {}
SESSIONS_RECHERCHE_ACTIVES = {}
MINUTEURS_ACTIFS = {}
SESSIONS_DECODEUR = {}
CONFIG_EPREUVE_GLOBALE = {"questions": [], "temps_par_defaut": 15, "active": False}
ETATS_EPREUVES_SALONS = {}
ETAT_COMPOSITION = {"actif": False, "channel_id": None, "capitaine_1": None, "role_1": None, "capitaine_2": None, "role_2": None, "tour": 1}
DERNIER_JOUR_RECAP = None


# ==========================================
# 2. UTILS
# ==========================================
def est_orga_ou_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild: return False
    if interaction.user.guild_permissions.administrator: return True
    role = discord.utils.get(interaction.guild.roles, name=ROLE_ORGAS_NAME)
    return role in interaction.user.roles if role else False

def nettoyer_texte(texte: str) -> str:
    if not texte: return ""
    norm = unicodedata.normalize("NFD", texte)
    sans_accents = "".join(c for c in norm if unicodedata.category(c) != "Mn")
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9\s]', '', sans_accents.lower())).strip()

def formater_nom_salon(nom: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "", nettoyer_texte(nom).replace(" ", "-"))

def est_categorie_candidate(category: discord.CategoryChannel) -> bool:
    if not category: return False
    cat_nom = nettoyer_texte(category.name)
    return any(c in cat_nom for c in CATEGORIES_CIBLES)

def get_spectateur_overwrites():
    return discord.PermissionOverwrite(view_channel=True, read_messages=True, read_message_history=True, send_messages=False, send_messages_in_threads=False, create_public_threads=False, create_private_threads=False, add_reactions=False)

def get_spectateur_voice_overwrites():
    return discord.PermissionOverwrite(view_channel=True, connect=True, speak=False, stream=False, use_voice_activation=False)

def trouver_role_personnel(member: discord.Member, role_equipe: discord.Role = None) -> discord.Role:
    nom_clean, pseudo_clean = nettoyer_texte(member.display_name), nettoyer_texte(member.name)
    req_clean = nettoyer_texte(role_equipe.name) if role_equipe else ""
    for r in member.roles:
        if r.is_default(): continue
        if nettoyer_texte(r.name) in (nom_clean, pseudo_clean): return r
    for r in member.roles:
        r_clean = nettoyer_texte(r.name)
        if r.is_default() or (role_equipe and r.id == role_equipe.id) or (role_equipe and r_clean == req_clean): continue
        if r_clean in [nettoyer_texte(ign) for ign in ROLES_GENERIQUES_A_IGNORER]: continue
        return r
    return None

def decouper_texte_intelligent(texte: str, limite: int = 1900):
    if len(texte) <= limite: return [texte]
    morceaux, rest = [], texte.strip()
    while len(rest) > limite:
        cut = rest.rfind("\n\n", 0, limite)
        if cut == -1: cut = rest.rfind("\n", 0, limite)
        if cut == -1: cut = rest.rfind(" ", 0, limite)
        if cut == -1: cut = limite
        morceaux.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest: morceaux.append(rest)
    return morceaux

# ==========================================
# 3. TÂCHES DE FOND (HORLOGE)
# ==========================================
@tasks.loop(minutes=1)
async def horloge_serveur():
    global DERNIER_JOUR_RECAP
    tz = ZoneInfo("Europe/Paris")
    now = datetime.datetime.now(tz)
    jour = now.strftime("%Y-%m-%d")
    if now.hour == 23 and now.minute == 30 and DERNIER_JOUR_RECAP != jour:
        DERNIER_JOUR_RECAP = jour
        for guild in bot.guilds:
            try:
                ch = bot.get_channel(RECAP_CHANNEL_ID)
                if ch: await generer_et_envoyer_recap_quotidien(guild, ch)
                await asyncio.sleep(5)
                await traiter_resume_spectateurs(guild)
            except Exception as e:
                print(f"Erreur task 23h30: {e}")

@bot.event
async def on_ready():
    await bot.tree.sync()
    if not horloge_serveur.is_running(): horloge_serveur.start()
    print(f"✅ Bot connecté: {bot.user}")

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot: return
    if before.channel and not after.channel: print(f"⚠️ {member.display_name} a quitté le vocal {before.channel.name}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot: return

    # Décodeur
    if message.channel.id in SESSIONS_DECODEUR:
        cd = SESSIONS_DECODEUR[message.channel.id]
        if message.author.id == cd["user_id"] and len(message.content.strip()) >= 2:
            async with message.channel.typing():
                trad = await decoder_charabia_ia(message.author.display_name, message.content.strip())
                embed = discord.Embed(description=f"🌐 **DÉCODEUR OFFICIEL — `{message.author.display_name}`**\n\n{trad}", color=discord.Color.teal())
                await message.reply(embed=embed, mention_author=False)

    # Recherche orga
    if message.channel.id in SESSIONS_RECHERCHE_ACTIVES:
        sess = SESSIONS_RECHERCHE_ACTIVES[message.channel.id]
        if message.author.id == sess["candidat_id"] and len(message.content.strip()) >= 4:
            s_orga = bot.get_channel(SALON_REMARQUES_QUESTIONS_ID)
            if s_orga:
                asyncio.create_task(traiter_suggestion_orga(message.channel, s_orga, message.author, sess["joueur"], message.content.strip()))

    # Draft
    if ETAT_COMPOSITION["actif"] and message.channel.id == ETAT_COMPOSITION["channel_id"]:
        cap1, cap2, tour = ETAT_COMPOSITION["capitaine_1"], ETAT_COMPOSITION["capitaine_2"], ETAT_COMPOSITION["tour"]
        cap_actif = cap1 if tour == 1 else cap2
        r_actif = ETAT_COMPOSITION["role_1"] if tour == 1 else ETAT_COMPOSITION["role_2"]
        if message.author.id == cap_actif.id and message.mentions:
            cible = message.mentions[0]
            if cible.bot:
                await message.channel.send("❌ Pas de bot.")
            elif ETAT_COMPOSITION["role_1"] in cible.roles or ETAT_COMPOSITION["role_2"] in cible.roles:
                await message.channel.send("⚠️ Déjà pris !")
            else:
                try:
                    await cible.add_roles(r_actif)
                    ETAT_COMPOSITION["tour"] = 2 if tour == 1 else 1
                    nxt = cap2 if tour == 1 else cap1
                    embed = discord.Embed(title="🤝 NOUVELLE RECRUE !", description=f"🔴 **{cible.mention}** rejoint {r_actif.mention} !\n👉 Au tour de {nxt.mention} !", color=r_actif.color)
                    await message.channel.send(embed=embed)
                except Exception as e:
                    await message.channel.send(f"❌ Erreur: {e}")

    await bot.process_commands(message)

@bot.tree.error
async def on_app_command_error(interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        msg = "⛔ **Accès refusé :** Commande réservée aux Orgas."
        if interaction.response.is_done(): await interaction.followup.send(msg, ephemeral=True)
        else: await interaction.response.send_message(msg, ephemeral=True)


# ==========================================
# 4. FONCTIONS IA DE BASE
# ==========================================
async def decoder_charabia_ia(nom, texte):
    prompt = f"Tu traduis avec un humour bienveillant un dialecte alien. Nom: {nom}. Texte: {texte}. Donne: \n🗣️ **Traduction littérale :**\n🔬 **Analyse sémiotique :**"
    try:
        res = await asyncio.to_thread(gemini_client.models.generate_content, model=MODEL_NAME, contents=prompt)
        return res.text.strip()
    except Exception as e: return f"Erreur IA : {e}"

async def suggerer_reponse_recherche_ia(joueur, question):
    prompt = f"Arbitre de foot. Joueur: {joueur}. Question: {question}. Format: VERDICT: <OUI/NON> \nEXPLICATION: <fait> \nSOURCES: <sources>"
    try:
        res = await asyncio.to_thread(gemini_client.models.generate_content, model=MODEL_NAME, contents=prompt)
        txt = res.text.strip()
        verdict = re.search(r"VERDICT\s*:\s*\**([A-Za-z\-]+)\**", txt, re.I)
        v = verdict.group(1).upper() if verdict else "INDÉTERMINÉ"
        exp = txt.split("EXPLICATION:", 1)[1].split("SOURCES:", 1)[0].strip() if "EXPLICATION:" in txt else txt
        src = txt.split("SOURCES:", 1)[1].strip() if "SOURCES:" in txt else "N/A"
        return {"verdict": v, "explication": exp, "sources": src}
    except Exception as e: return {"verdict": "ERREUR", "explication": str(e), "sources": "N/A"}

async def traiter_suggestion_orga(c_src, c_dest, cand, joueur, q):
    res = await suggerer_reponse_recherche_ia(joueur, q)
    v = res["verdict"]
    c = discord.Color.green() if v == "OUI" else (discord.Color.red() if v == "NON" else discord.Color.orange())
    e = "🟢 **OUI**" if v == "OUI" else ("🔴 **NON**" if v == "NON" else f"🟡 **{v}**")
    emb = discord.Embed(title=f"💡 SUGGESTION — {joueur.upper()}", color=c)
    emb.add_field(name="👤 Candidat", value=f"{cand.mention} dans {c_src.mention}", inline=False)
    emb.add_field(name="❓ Question", value=f"*{q}*", inline=False)
    emb.add_field(name="👉 Recommandé", value=e, inline=False)
    emb.add_field(name="📚 Justification", value=f"{res['explication']}\n\n**Sources:** `{res['sources']}`", inline=False)
    await c_dest.send(embed=emb)


# ==========================================
# 5. GESTION DES SALONS (DUOS, EQUIPES...)
# ==========================================
@bot.tree.command(name="creer_duos", description="Crée tous les duos d'une équipe.")
@app_commands.check(est_orga_ou_admin)
async def creer_duos(inter: discord.Interaction, role_equipe: discord.Role, nom_categorie: str):
    await inter.response.defer(ephemeral=True)
    g = inter.guild
    mb = [m for m in g.members if not m.bot and role_equipe in m.roles]
    if len(mb) < 2: return await inter.followup.send("❌ Pas assez de membres.", ephemeral=True)
    
    cd = [{"m": m, "r": trouver_role_personnel(m, role_equipe), "n": formater_nom_salon(m.display_name)} for m in mb]
    r_spec, r_org = discord.utils.get(g.roles, name=ROLE_SPECTATEURS_NAME), discord.utils.get(g.roles, name=ROLE_ORGAS_NAME)
    duos = list(itertools.combinations(cd, 2))
    
    cat = discord.utils.find(lambda c: nettoyer_texte(c.name) == nettoyer_texte(nom_categorie), g.categories) or await g.create_category(nom_categorie)
    idx = 1
    
    for c1, c2 in duos:
        if len(cat.channels) >= MAX_CHANNELS_PER_CATEGORY:
            idx += 1
            cat = await g.create_category(f"{nom_categorie} - {idx}")
        ow = {g.default_role: discord.PermissionOverwrite(read_messages=False), g.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)}
        if c1["r"]: ow[c1["r"]] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        if c2["r"]: ow[c2["r"]] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        if r_spec: ow[r_spec] = get_spectateur_overwrites()
        if r_org: ow[r_org] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        await g.create_text_channel(name=f"duo-{c1['n']}-{c2['n']}", category=cat, overwrites=ow)
    await inter.followup.send(f"✅ {len(duos)} duos créés !", ephemeral=True)

@bot.tree.command(name="eliminer_candidat", description="Archive les salons d'un éliminé.")
@app_commands.check(est_orga_ou_admin)
async def eliminer_candidat(inter: discord.Interaction, role_candidat: discord.Role):
    await inter.response.defer(ephemeral=True)
    g = inter.guild
    chs = [c for c in g.text_channels if (c.name.startswith("duo-") or c.name.startswith("🔗") or c.name.startswith("🔺")) and role_candidat in c.overwrites]
    if not chs: return await inter.followup.send("❌ Aucun salon trouvé.", ephemeral=True)
    
    r_spec, r_org = discord.utils.get(g.roles, name=ROLE_SPECTATEURS_NAME), discord.utils.get(g.roles, name=ROLE_ORGAS_NAME)
    cats = [c for c in g.categories if nettoyer_texte(NOM_CATEGORIE_ARCHIVE) in nettoyer_texte(c.name)]
    cat = cats[-1] if cats else await g.create_category(NOM_CATEGORIE_ARCHIVE)
    idx = len(cats) or 1
    
    for c in chs:
        if len(cat.channels) >= MAX_CHANNELS_PER_CATEGORY:
            idx += 1
            cat = await g.create_category(f"{NOM_CATEGORIE_ARCHIVE} - {idx}")
        ow = {g.default_role: discord.PermissionOverwrite(read_messages=False), g.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)}
        if r_spec: ow[r_spec] = get_spectateur_overwrites()
        if r_org: ow[r_org] = discord.PermissionOverwrite(read_messages=True, send_messages=False)
        nb = c.name.replace("duo-", "").replace("🔗・", "").replace("🔺・", "")
        await c.edit(name=f"🔒arch-{nb}", category=cat, overwrites=ow)
    await inter.followup.send(f"✅ {len(chs)} salons archivés pour {role_candidat.name}.", ephemeral=True)

@bot.tree.command(name="creer_trio", description="Crée un salon trio.")
@app_commands.check(est_orga_ou_admin)
async def creer_trio(inter: discord.Interaction, role_1: discord.Role, role_2: discord.Role, role_3: discord.Role):
    await inter.response.defer(ephemeral=True)
    g = inter.guild
    cat = g.get_channel(CATEGORY_TRIO_ID)
    roles = [role_1, role_2, role_3]
    ow = {g.default_role: discord.PermissionOverwrite(read_messages=False), g.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)}
    for r in roles: ow[r] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    noms = [formater_nom_salon(r.name) for r in roles]
    ch = await g.create_text_channel(name=f"🔺・{'-'.join(noms)}", category=cat, overwrites=ow)
    await inter.followup.send(f"✅ Trio créé : {ch.mention}", ephemeral=True)

@bot.tree.command(name="creer_quatuor", description="Crée un salon quatuor.")
@app_commands.check(est_orga_ou_admin)
async def creer_quatuor(inter: discord.Interaction, r1: discord.Role, r2: discord.Role, r3: discord.Role, r4: discord.Role):
    await inter.response.defer(ephemeral=True)
    g, cat = inter.guild, inter.guild.get_channel(CATEGORY_QUATUOR_ID)
    roles = [r1, r2, r3, r4]
    ow = {g.default_role: discord.PermissionOverwrite(read_messages=False), g.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)}
    for r in roles: ow[r] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    noms = [formater_nom_salon(r.name) for r in roles]
    ch = await g.create_text_channel(name=f"🔶・{'-'.join(noms)}", category=cat, overwrites=ow)
    await inter.followup.send(f"✅ Quatuor créé : {ch.mention}", ephemeral=True)

@bot.tree.command(name="creer_vocal", description="Crée un vocal privé (2-5 membres).")
@app_commands.check(est_orga_ou_admin)
async def creer_vocal(inter: discord.Interaction, nom_cat: str, r1: discord.Role, r2: discord.Role, r3: discord.Role=None, r4: discord.Role=None, r5: discord.Role=None):
    await inter.response.defer(ephemeral=True)
    g = inter.guild
    cat = discord.utils.find(lambda c: nettoyer_texte(c.name) == nettoyer_texte(nom_cat), g.categories)
    roles = [r for r in [r1, r2, r3, r4, r5] if r]
    ow = {g.default_role: discord.PermissionOverwrite(view_channel=False), g.me: discord.PermissionOverwrite(view_channel=True, connect=True)}
    for r in roles: ow[r] = discord.PermissionOverwrite(view_channel=True, connect=True, speak=True)
    noms = [formater_nom_salon(r.name) for r in roles]
    ch = await g.create_voice_channel(name=f"🔊・{'-'.join(noms)}", category=cat, overwrites=ow)
    await inter.followup.send(f"✅ Vocal créé : {ch.mention}", ephemeral=True)

@bot.tree.command(name="supprimer_categorie", description="Supprime une catégorie et ses salons.")
@app_commands.check(est_orga_ou_admin)
async def supprimer_categorie(inter: discord.Interaction, nom: str):
    await inter.response.defer(ephemeral=True)
    cat = discord.utils.find(lambda c: nettoyer_texte(c.name) == nettoyer_texte(nom), inter.guild.categories)
    if not cat: return await inter.followup.send("❌ Catégorie introuvable.", ephemeral=True)
    for ch in cat.channels: await ch.delete()
    await cat.delete()
    await inter.followup.send("✅ Supprimé.", ephemeral=True)

@bot.tree.command(name="purger_equipe_duos", description="Supprime les catégories commençant par ce préfixe.")
@app_commands.check(est_orga_ou_admin)
async def purger_equipe_duos(inter: discord.Interaction, pref: str):
    await inter.response.defer(ephemeral=True)
    cats = [c for c in inter.guild.categories if nettoyer_texte(c.name).startswith(nettoyer_texte(pref))]
    for cat in cats:
        for ch in cat.channels: await ch.delete()
        await cat.delete()
    await inter.followup.send("✅ Purge terminée.", ephemeral=True)

@bot.tree.command(name="effacer_salon", description="Réinitialise le salon.")
@app_commands.check(est_orga_ou_admin)
async def effacer_salon(inter: discord.Interaction, salon: discord.TextChannel = None):
    await inter.response.defer(ephemeral=True)
    ch = salon or inter.channel
    nch = await ch.clone()
    await ch.delete()
    await nch.send("🧹 Salon réinitialisé.")

@bot.tree.command(name="vider_categorie", description="Supprime les salons d'une catégorie.")
@app_commands.check(est_orga_ou_admin)
async def vider_categorie(inter: discord.Interaction, nom: str):
    await inter.response.defer(ephemeral=True)
    cat = discord.utils.find(lambda c: nettoyer_texte(c.name) == nettoyer_texte(nom), inter.guild.categories)
    for ch in cat.channels: await ch.delete()
    await inter.followup.send("✅ Catégorie vidée.", ephemeral=True)


# ==========================================
# 6. PERMISSIONS & SPECTATEURS
# ==========================================
@bot.tree.command(name="ajouter_spectateurs_salon", description="Donne accès lecture seule aux spectateurs.")
@app_commands.check(est_orga_ou_admin)
async def ajouter_spectateurs_salon(inter: discord.Interaction, salon: discord.TextChannel = None):
    await inter.response.defer(ephemeral=True)
    ch = salon or inter.channel
    rs = discord.utils.get(inter.guild.roles, name=ROLE_SPECTATEURS_NAME)
    if rs: await ch.set_permissions(rs, overwrite=get_spectateur_overwrites())
    await inter.followup.send("✅ Spectateurs ajoutés.", ephemeral=True)

@bot.tree.command(name="ajouter_spectateurs_categorie", description="Applique les droits specs à une catégorie.")
@app_commands.check(est_orga_ou_admin)
async def ajouter_spectateurs_categorie(inter: discord.Interaction, nom: str):
    await inter.response.defer(ephemeral=True)
    cat = discord.utils.find(lambda c: nettoyer_texte(c.name) == nettoyer_texte(nom), inter.guild.categories)
    rs = discord.utils.get(inter.guild.roles, name=ROLE_SPECTATEURS_NAME)
    if cat and rs:
        for ch in cat.channels:
            if isinstance(ch, discord.TextChannel): await ch.set_permissions(rs, overwrite=get_spectateur_overwrites())
    await inter.followup.send("✅ Appliqué à la catégorie.", ephemeral=True)


# ==========================================
# 7. RÉSUMÉS & IA
# ==========================================
async def generer_et_envoyer_recap_quotidien(guild: discord.Guild, target_channel: discord.TextChannel):
    tz = ZoneInfo("Europe/Paris")
    debut = datetime.datetime.now(tz).replace(hour=0, minute=0, second=0).astimezone(datetime.timezone.utc)
    transcripts = []
    for ch in guild.text_channels:
        if est_categorie_candidate(ch.category):
            lines = [f"{m.author.display_name}: {m.content.strip()}" async for m in ch.history(after=debut, oldest_first=True) if not m.author.bot and m.content.strip()]
            if lines: transcripts.append(f"== #{ch.name} ==\n" + "\n".join(lines))
    if not transcripts:
        await target_channel.send("😴 Aucun échange aujourd'hui.")
        return
    prompt = f"Fais un journal stratégique percutant des événements du jour :\n\n{chr(10).join(transcripts)[:25000]}"
    res = await asyncio.to_thread(gemini_client.models.generate_content, model=MODEL_NAME, contents=prompt)
    for bloc in decouper_texte_intelligent(f"📰 **JOURNAL DU {datetime.datetime.now(tz).strftime('%d/%m/%Y')}**\n{res.text}", 1900):
        await target_channel.send(bloc)
    await asyncio.sleep(2)
    await poster_questions_automatiques(res.text)

@bot.tree.command(name="resumer", description="Résumé IA du salon (court/long).")
@app_commands.choices(f=[app_commands.Choice(name="Court", value="c"), app_commands.Choice(name="Long", value="l")])
@app_commands.check(est_orga_ou_admin)
async def resumer(inter: discord.Interaction, f: app_commands.Choice[str], lim: int = 100):
    await inter.response.defer(ephemeral=True)
    msgs = [f"{m.author.display_name}: {m.content}" async for m in inter.channel.history(limit=lim) if not m.author.bot]
    p = f"Résumé {'très court (bullet points)' if f.value == 'c' else 'détaillé'} de ce salon:\n" + "\n".join(msgs)
    res = await asyncio.to_thread(gemini_client.models.generate_content, model=MODEL_NAME, contents=p)
    await inter.followup.send(embed=discord.Embed(title="📝 Résumé", description=res.text, color=discord.Color.blue()), ephemeral=True)

@bot.tree.command(name="resumer_conv_orga", description="Résumé orienté staff.")
@app_commands.check(est_orga_ou_admin)
async def resumer_conv_orga(inter: discord.Interaction, lim: int = 100):
    await inter.response.defer(ephemeral=True)
    msgs = [f"{m.author.display_name}: {m.content}" async for m in inter.channel.history(limit=lim) if not m.author.bot]
    p = f"Compte-rendu de réunion staff avec décisions et actions :\n" + "\n".join(msgs)
    res = await asyncio.to_thread(gemini_client.models.generate_content, model=MODEL_NAME, contents=p)
    await inter.followup.send(embed=discord.Embed(title="🛠️ CR Staff", description=res.text, color=discord.Color.red()), ephemeral=True)

@bot.tree.command(name="forcer_recap_jour", description="Génère le récap global immédiatement.")
@app_commands.check(est_orga_ou_admin)
async def forcer_recap_jour(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    ch = bot.get_channel(RECAP_CHANNEL_ID)
    await inter.followup.send("⏳ Génération en cours...")
    if ch: await generer_et_envoyer_recap_quotidien(inter.guild, ch)

@bot.tree.command(name="forcer_recap_spec", description="Génère le récap spectateurs.")
@app_commands.check(est_orga_ou_admin)
async def forcer_recap_spec(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    s, m = await traiter_resume_spectateurs(inter.guild)
    await inter.followup.send(f"✅ {m}" if s else f"❌ {m}")


# ==========================================
# 8. JEUX, TIRAGES ET DRAFT
# ==========================================
@bot.tree.command(name="tirage_boules", description="Tirage au sort (boules noires).")
@app_commands.check(est_orga_ou_admin)
async def tirage_boules(inter: discord.Interaction, participants: str, nb: int = 1):
    r_ids = [int(r.strip("<@&>")) for r in participants.split() if r.startswith("<@&")]
    cands = [inter.guild.get_role(r) for r in r_ids if inter.guild.get_role(r)]
    if len(cands) < 2 or nb >= len(cands): return await inter.response.send_message("❌ Invalide.", ephemeral=True)
    await inter.response.send_message("🏺 Tirage...", ephemeral=True)
    sac = (["⚫ Noire"] * nb) + (["⚪ Blanche"] * (len(cands) - nb))
    random.shuffle(sac)
    random.shuffle(cands)
    vict = [c for c, b in zip(cands, sac) if "Noire" in b]
    res = "\n".join([f"{c.mention} ➔ {'⚫ BOULE NOIRE' if 'Noire' in b else '⚪ Blanche'}" for c, b in zip(cands, sac)])
    await inter.channel.send(embed=discord.Embed(title="🏺 VERDICT", description=f"{res}\n\n☠️ {', '.join(v.mention for v in vict)} a tiré la noire !", color=discord.Color.red()))

@bot.tree.command(name="tirer_binomes", description="Tirage des destins liés.")
@app_commands.check(est_orga_ou_admin)
async def tirer_binomes(inter: discord.Interaction, r1: discord.Role, r2: discord.Role):
    global DERNIERS_BINOMES_TIRES
    await inter.response.defer(ephemeral=True)
    m1, m2 = [m for m in r1.members if not m.bot], [m for m in r2.members if not m.bot]
    if len(m1) != len(m2) or not m1: return await inter.followup.send("❌ Équipes non valides.")
    random.shuffle(m1); random.shuffle(m2)
    DERNIERS_BINOMES_TIRES = list(zip(
        [{"m": m, "r": trouver_role_personnel(m, r1), "n": formater_nom_salon(m.display_name)} for m in m1],
        [{"m": m, "r": trouver_role_personnel(m, r2), "n": formater_nom_salon(m.display_name)} for m in m2]
    ))
    res = "\n".join([f"🔗 {a['m'].display_name} & {b['m'].display_name}" for a, b in DERNIERS_BINOMES_TIRES])
    await inter.channel.send(embed=discord.Embed(title="⚡ DESTINS LIÉS", description=res, color=discord.Color.gold()))
    await inter.followup.send("✅ Tirage fait. Fais /creer_salons_binomes.", ephemeral=True)

@bot.tree.command(name="creer_salons_binomes", description="Crée les salons du dernier tirage.")
@app_commands.check(est_orga_ou_admin)
async def creer_salons_binomes(inter: discord.Interaction, nom_cat: str):
    global DERNIERS_BINOMES_TIRES
    await inter.response.defer(ephemeral=True)
    g = inter.guild
    if not DERNIERS_BINOMES_TIRES: return await inter.followup.send("❌ Aucun tirage.")
    cat = await g.create_category(nom_cat)
    for a, b in DERNIERS_BINOMES_TIRES:
        ow = {g.default_role: discord.PermissionOverwrite(read_messages=False), g.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)}
        if a["r"]: ow[a["r"]] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        if b["r"]: ow[b["r"]] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        await g.create_text_channel(name=f"🔗-{a['n']}-{b['n']}", category=cat, overwrites=ow)
    DERNIERS_BINOMES_TIRES = []
    await inter.followup.send("✅ Salons créés.")

@bot.tree.command(name="activer_conseil", description="Isole une équipe.")
@app_commands.check(est_orga_ou_admin)
async def activer_conseil(inter: discord.Interaction, r_eq: discord.Role):
    global ROLES_PERSO_EN_PAUSE
    await inter.response.defer(ephemeral=True)
    for m in [m for m in inter.guild.members if r_eq in m.roles and not m.bot]:
        r = trouver_role_personnel(m, r_eq)
        if r and r < inter.guild.me.top_role:
            await m.remove_roles(r)
            ROLES_PERSO_EN_PAUSE[m.id] = r.id
    await inter.followup.send("🔒 Conseil activé.")

@bot.tree.command(name="desactiver_conseil", description="Restaure les rôles.")
@app_commands.check(est_orga_ou_admin)
async def desactiver_conseil(inter: discord.Interaction, r_eq: discord.Role):
    global ROLES_PERSO_EN_PAUSE
    await inter.response.defer(ephemeral=True)
    for m in [m for m in inter.guild.members if r_eq in m.roles and not m.bot]:
        if m.id in ROLES_PERSO_EN_PAUSE:
            r = inter.guild.get_role(ROLES_PERSO_EN_PAUSE[m.id])
            if r: await m.add_roles(r)
    await inter.followup.send("🔓 Conseil désactivé.")

@bot.tree.command(name="lancer_composition_equipes", description="Lance la draft d'équipes.")
@app_commands.check(est_orga_ou_admin)
async def lancer_composition_equipes(inter: discord.Interaction, cap1: discord.Member, r1: discord.Role, cap2: discord.Member, r2: discord.Role):
    await inter.response.defer(ephemeral=True)
    await cap1.add_roles(r1); await cap2.add_roles(r2)
    ETAT_COMPOSITION.update({"actif": True, "channel_id": inter.channel.id, "capitaine_1": cap1, "role_1": r1, "capitaine_2": cap2, "role_2": r2, "tour": 1})
    await inter.channel.send(embed=discord.Embed(title="⚔️ DRAFT", description=f"C'est à {cap1.mention} de taguer sa recrue !", color=discord.Color.gold()))
    await inter.followup.send("✅ Draft lancée.")

@bot.tree.command(name="arreter_composition_equipes", description="Arrête la draft.")
@app_commands.check(est_orga_ou_admin)
async def arreter_composition_equipes(inter: discord.Interaction):
    ETAT_COMPOSITION["actif"] = False
    await inter.response.send_message("🛑 Draft terminée.")


# ==========================================
# 9. MINUTEURS ET CHRONOS
# ==========================================
@bot.tree.command(name="poser_question_flash", description="Question chronométrée.")
@app_commands.check(est_orga_ou_admin)
async def poser_question_flash(inter: discord.Interaction, q: str, sec: int = 15):
    ts = int((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=sec)).timestamp())
    await inter.response.send_message(embed=discord.Embed(title="⚡ FLASH", description=f"**{q}**\n\nFin: <t:{ts}:R>", color=discord.Color.orange()))
    msg = await inter.original_response()
    try:
        r = await bot.wait_for("message", timeout=sec, check=lambda m: m.channel == inter.channel and not m.author.bot)
        await inter.channel.send(f"✅ {r.author.mention} a répondu : {r.content}")
    except asyncio.TimeoutError:
        await msg.edit(embed=discord.Embed(title="⚡ FLASH", description=f"**{q}**\n\n🛑 TEMPS ÉCOULÉ", color=discord.Color.red()))

async def boucle_minuteur(channel: discord.TextChannel, total_sec: int):
    start = time.perf_counter()
    alertes = [(total_sec//2, "⏳ MI-TEMPS !")] if total_sec > 120 else []
    for s, m in [(300, "⚠️ 5 min !"), (60, "🚨 1 min !"), (30, "⏱️ 30s"), (10, "⚡ 10s")]:
        if total_sec >= s: alertes.append((s, m))
    alertes.sort(key=lambda x: x[0], reverse=True)

    try:
        for tr, msg in alertes:
            attente = (total_sec - tr) - (time.perf_counter() - start)
            if attente > 0: await asyncio.sleep(attente); await channel.send(msg)
        reste = total_sec - (time.perf_counter() - start)
        if reste > 0: await asyncio.sleep(reste)
        await channel.send("🛑 **TEMPS ÉCOULÉ ! Début du temps additionnel.**")
        ext = 0
        while True:
            await asyncio.sleep(120); ext += 2
            await channel.send(f"⏱️ **+{ext} min** de temps additionnel...")
    except asyncio.CancelledError: pass

@bot.tree.command(name="chrono_minuteur", description="Lance un minuteur complet.")
@app_commands.check(est_orga_ou_admin)
async def chrono_minuteur(inter: discord.Interaction, mins: int):
    if inter.channel.id in MINUTEURS_ACTIFS: return await inter.response.send_message("❌ Déjà actif.", ephemeral=True)
    sec = mins * 60
    ts = int((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=sec)).timestamp())
    task = asyncio.create_task(boucle_minuteur(inter.channel, sec))
    MINUTEURS_ACTIFS[inter.channel.id] = {"task": task, "start": time.perf_counter(), "sec": sec}
    await inter.response.send_message(embed=discord.Embed(title="⏱️ DÉPART", description=f"Durée: `{mins} min`\nFin: <t:{ts}:R>", color=discord.Color.green()))

@bot.tree.command(name="chrono_minuteur_stop", description="Arrête le minuteur.")
@app_commands.check(est_orga_ou_admin)
async def chrono_minuteur_stop(inter: discord.Interaction):
    if inter.channel.id not in MINUTEURS_ACTIFS: return await inter.response.send_message("❌ Aucun chrono.", ephemeral=True)
    d = MINUTEURS_ACTIFS.pop(inter.channel.id)
    d["task"].cancel()
    tot = time.perf_counter() - d["start"]
    m, s = int(tot // 60), round(tot % 60, 2)
    txt = f"⏱️ Temps total: `{m} min {s}s`"
    if tot > d["sec"]:
        diff = tot - d["sec"]
        txt += f"\n🚨 Dépassement: `+{int(diff//60)} min {round(diff%60,2)}s`"
    await inter.response.send_message(embed=discord.Embed(title="🏁 STOP", description=txt, color=discord.Color.gold()))


# ==========================================
# 10. SALONS FOCUS & LECTURE SEULE
# ==========================================
@bot.tree.command(name="creer_salon_annonces_groupe", description="Salon où tout le monde lit, seul l'orga écrit.")
@app_commands.check(est_orga_ou_admin)
async def creer_salon_annonces_groupe(inter: discord.Interaction, nom: str, cat: str, c1: discord.Member, c2: discord.Member):
    await inter.response.defer(ephemeral=True)
    g = inter.guild
    categ = discord.utils.find(lambda c: nettoyer_texte(c.name) == nettoyer_texte(cat), g.categories) or await g.create_category(cat)
    ow = {g.default_role: discord.PermissionOverwrite(view_channel=False), g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
    for c in [c1, c2]: ow[c] = discord.PermissionOverwrite(view_channel=True, read_messages=True, send_messages=False)
    ch = await g.create_text_channel(name=f"📢-{formater_nom_salon(nom)}", category=categ, overwrites=ow)
    await inter.followup.send(f"✅ Créé : {ch.mention}", ephemeral=True)

@bot.tree.command(name="creer_salon_focus_candidat", description="1 candidat écrit, les autres lisent.")
@app_commands.check(est_orga_ou_admin)
async def creer_salon_focus(inter: discord.Interaction, nom: str, cat: str, c_actif: discord.Member, c_obs: discord.Member):
    await inter.response.defer(ephemeral=True)
    g = inter.guild
    categ = discord.utils.find(lambda c: nettoyer_texte(c.name) == nettoyer_texte(cat), g.categories) or await g.create_category(cat)
    ow = {g.default_role: discord.PermissionOverwrite(view_channel=False), g.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)}
    ow[c_actif] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    ow[c_obs] = discord.PermissionOverwrite(view_channel=True, send_messages=False)
    ch = await g.create_text_channel(name=f"🎯-{formater_nom_salon(nom)}", category=categ, overwrites=ow)
    await inter.followup.send(f"✅ Créé : {ch.mention}", ephemeral=True)


# ==========================================
# 11. STATS ET ROASTS
# ==========================================
@bot.tree.command(name="stats_mots_candidats", description="Top mots.")
@app_commands.check(est_orga_ou_admin)
async def stats_mots(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    cnt = Counter()
    for ch in inter.guild.text_channels:
        if est_categorie_candidate(ch.category):
            async for m in ch.history(limit=200):
                if not m.author.bot:
                    for w in nettoyer_texte(m.content).split():
                        if len(w) >= 4 and w not in MOTS_VIDES_FR: cnt[w] += 1
    res = "\n".join([f"**{w}** : {c}x" for w, c in cnt.most_common(10)])
    await inter.followup.send(embed=discord.Embed(title="📊 Mots fréquents", description=res, color=discord.Color.purple()))

@bot.tree.command(name="roast_hardcore", description="Roast IA cinglant basé sur les messages.")
@app_commands.check(est_orga_ou_admin)
async def roast_hardcore(inter: discord.Interaction, cible: discord.Member):
    await inter.response.defer()
    msgs = []
    for ch in inter.guild.text_channels:
        if est_categorie_candidate(ch.category):
            msgs += [m.content async for m in ch.history(limit=50) if m.author.id == cible.id]
    prompt = f"Fais un roast cinglant de {cible.display_name} basé sur ça:\n" + "\n".join(msgs[:30])
    res = await asyncio.to_thread(gemini_client.models.generate_content, model=MODEL_NAME, contents=prompt)
    await inter.followup.send(f"💀 **ROAST** {cible.mention}\n{res.text}")


# ==========================================
# 12. BILANS CANDIDATS ET ORGAS
# ==========================================
@bot.tree.command(name="bilan_candidat", description="Bilan IA complet du candidat.")
@app_commands.check(est_orga_ou_admin)
async def bilan_cand(inter: discord.Interaction, cand: discord.Member):
    await inter.response.defer(ephemeral=True)
    m_lui, m_sur = [], []
    nc = nettoyer_texte(cand.display_name)
    for ch in inter.guild.text_channels:
        if est_categorie_candidate(ch.category):
            async for m in ch.history(limit=100):
                if m.author.id == cand.id: m_lui.append(m.content)
                elif nc in nettoyer_texte(m.content): m_sur.append(m.content)
    p = f"Fais un bilan (Note/10, Stratégie, Fails) sur {cand.display_name}.\nIl a dit: {m_lui[:20]}\nOn dit de lui: {m_sur[:20]}"
    res = await asyncio.to_thread(gemini_client.models.generate_content, model=MODEL_NAME, contents=p)
    ch_d = bot.get_channel(SALON_BILAN_CANDIDATS_ID) or inter.channel
    await ch_d.send(embed=discord.Embed(title=f"📊 BILAN {cand.display_name.upper()}", description=res.text[:3900], color=discord.Color.gold()))
    await inter.followup.send("✅ Bilan fait.", ephemeral=True)

@bot.tree.command(name="bilan_orga", description="Audit IA du staff.")
@app_commands.check(est_orga_ou_admin)
async def bilan_orga(inter: discord.Interaction, orga: discord.Member):
    await inter.response.defer(ephemeral=True)
    m_lui = []
    for ch in inter.guild.text_channels:
        async for m in ch.history(limit=50):
            if m.author.id == orga.id: m_lui.append(m.content)
    p = f"Audit staff sévère mais drôle sur {orga.display_name} d'après ses messages:\n{m_lui[:30]}"
    res = await asyncio.to_thread(gemini_client.models.generate_content, model=MODEL_NAME, contents=p)
    ch_d = bot.get_channel(SALON_BILAN_ORGAS_ID) or inter.channel
    await ch_d.send(embed=discord.Embed(title=f"🛠️ AUDIT {orga.display_name.upper()}", description=res.text[:3900], color=discord.Color.red()))
    await inter.followup.send("✅ Audit orga fait.", ephemeral=True)


# ==========================================
# 13. GÉNÉRATEUR UNE L'ÉQUIPE (IMAGE PILLOW)
# ==========================================
async def extraire_articles_une(txt):
    p = f"Extrais des titres ironiques (TITRE, SOUS_TITRE, CANDIDAT_PRINCIPAL) de ça:\n{txt[:10000]}"
    res = await asyncio.to_thread(gemini_client.models.generate_content, model=MODEL_NAME, contents=p)
    return {"TITRE_PRINCIPAL": "DRAMA !", "SOUS_TITRE_PRINCIPAL": "Gros bordel en vue.", "CANDIDAT_PRINCIPAL": "Ugo"}

def creer_image_une(donnees, photos):
    l, h = 900, 1300
    fond = Image.new("RGB", (l, h), "#FFF")
    draw = ImageDraw.Draw(fond)
    try: font_titre = ImageFont.truetype("fonts/Anton-Regular.ttf", 46)
    except: font_titre = ImageFont.load_default()
    draw.rectangle([(15, 30), (280, 95)], fill="#E30613")
    draw.text((25, 26), "L'ÉQUIPE", fill="#FFF", font=font_titre)
    cp = donnees.get("CANDIDAT_PRINCIPAL", "")
    if cp in photos: fond.paste(photos[cp].resize((580, 480)), (15, 230))
    else: draw.rectangle([(15, 230), (595, 710)], fill="#EAEAEA")
    draw.text((15, 725), donnees.get("TITRE_PRINCIPAL", "BORDEL !"), fill="#000", font=font_titre)
    buf = io.BytesIO()
    fond.save(buf, format="JPEG", quality=95)
    buf.seek(0)
    return buf

@bot.tree.command(name="generer_une_journal", description="Génère la Une satirique L'Équipe.")
@app_commands.check(est_orga_ou_admin)
async def generer_une_journal(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True)
    msgs = []
    for ch in inter.guild.text_channels:
        if est_categorie_candidate(ch.category):
            msgs += [m.content async for m in ch.history(limit=20) if not m.author.bot]
    data = await extraire_articles_une("\n".join(msgs))
    photos = {}
    async with aiohttp.ClientSession() as s:
        for m in inter.guild.members:
            if not m.bot:
                try:
                    async with s.get(m.display_avatar.url) as r:
                        if r.status == 200: photos[m.display_name.split()[0]] = Image.open(io.BytesIO(await r.read())).convert("RGB")
                except: pass
    buf = creer_image_une(data, photos)
    await inter.channel.send(file=discord.File(buf, "une.jpg"))
    await inter.followup.send("✅ Une générée !", ephemeral=True)


# ==========================================
# LANCEMENT ROBUSTE
# ==========================================
def lancer_bot_avec_retry():
    tentatives = 0
    while True:
        try:
            print("🚀 Lancement du bot...")
            bot.run(TOKEN)
            break
        except Exception as e:
            tentatives += 1
            att = min(15 * tentatives, 120)
            print(f"⚠️ Erreur réseau ({e}). Reconnexion dans {att}s...")
            time.sleep(att)

if __name__ == "__main__":
    lancer_bot_avec_retry()
