import os
import itertools
import asyncio
import discord
from discord import app_commands
from discord.ext import commands

intents = discord.Intents.default()
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

MAX_CHANNELS_PER_CATEGORY = 45  # Marge de sécurité sous la limite Discord de 50

# Rôles avec accès automatique à tous les duos
ROLE_SPECTATEURS_NAME = "Spectateurs"
ROLE_ORGAS_NAME = "Orgas"


@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"🤖 Bot connecté en tant que : {bot.user}")


@bot.tree.command(name="creer_duos", description="Génère tous les salons duos privés avec accès Orgas & Spectateurs.")
@app_commands.describe(
    nom_equipe="Nom de base pour les catégories (ex: Duos Rouge)",
    roles="Mentionne les rôles séparés par des espaces (ex: @Candidat1 @Candidat2 ...)"
)
async def creer_duos(interaction: discord.Interaction, nom_equipe: str, roles: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # 1. Extraction des rôles mentionnés pour les candidats
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

        # Permissions : Seuls r1, r2, Spectateurs, Orgas et le Bot voient le salon
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

        # Ajout Orgas (Lecture + Écriture pour modération)
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

bot.run(os.getenv("DISCORD_TOKEN"))
