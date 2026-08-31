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

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"🤖 Bot connecté en tant que : {bot.user}")

@bot.tree.command(name="creer_duos", description="Génère tous les salons duos privés avec gestion multi-catégories.")
@app_commands.describe(
    nom_equipe="Nom de base pour les catégories (ex: Duos Rouge)",
    roles="Mentionne les rôles séparés par des espaces (ex: @Candidat1 @Candidat2 ...)"
)
async def creer_duos(interaction: discord.Interaction, nom_equipe: str, roles: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # 1. Extraction des rôles mentionnés
    role_ids = [int(r.strip("<@&>")) for r in roles.split() if r.startswith("<@&") and r.endswith(">")]
    roles_list = [guild.get_role(r_id) for r_id in role_ids if guild.get_role(r_id) is not None]

    if len(roles_list) < 2:
        await interaction.followup.send("❌ Veuillez mentionner au moins 2 rôles valides.", ephemeral=True)
        return

    # 2. Génération de toutes les combinaisons de duos
    duos = list(itertools.combinations(roles_list, 2))
    total_duos = len(duos)

    # 3. Création des salons avec bascule automatique de catégorie
    category_index = 1
    current_category = await guild.create_category(f"{nom_equipe} - {category_index}")
    channel_count_in_current_cat = 0

    for r1, r2 in duos:
        # Si la catégorie courante est pleine, on en crée une nouvelle
        if channel_count_in_current_cat >= MAX_CHANNELS_PER_CATEGORY:
            category_index += 1
            current_category = await guild.create_category(f"{nom_equipe} - {category_index}")
            channel_count_in_current_cat = 0
            await asyncio.sleep(1)  # Pause anti-rate-limit

        # Permissions strictes : seuls les 2 candidats concernés et le bot voient le salon
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, view_channel=False),
            r1: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True),
            r2: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, view_channel=True)
        }

        nom_salon = f"duo-{r1.name.lower().replace(' ', '-')}-{r2.name.lower().replace(' ', '-')}"
        await guild.create_text_channel(name=nom_salon, category=current_category, overwrites=overwrites)
        channel_count_in_current_cat += 1

        # Pause pour respecter les limites de requêtes de l'API Discord
        await asyncio.sleep(0.6)

    await interaction.followup.send(
        f"✅ Succès : **{total_duos} salons duos** ont été créés et répartis sur **{category_index} catégorie(s)** !", 
        ephemeral=True
    )

@bot.tree.command(
    name="supprimer_categorie", 
    description="Supprime une catégorie entière et tous les salons à l'intérieur."
)
@app_commands.describe(
    nom_categorie="Nom exact de la catégorie à supprimer (ex: Duos Rouge - 1)"
)
@app_commands.default_permissions(administrator=True)
async def supprimer_categorie(interaction: discord.Interaction, nom_categorie: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # 1. Rechercher la catégorie par son nom (insensible à la casse)
    category = discord.utils.find(
        lambda c: c.name.lower() == nom_categorie.strip().lower(), 
        guild.categories
    )

    if not category:
        await interaction.followup.send(f"❌ La catégorie **{nom_categorie}** est introuvable.", ephemeral=True)
        return

    # 2. Supprimer tous les salons à l'intérieur un par un
    channels_to_delete = category.channels
    total_channels = len(channels_to_delete)

    for channel in channels_to_delete:
        try:
            await channel.delete(reason="Nettoyage catégorie")
            await asyncio.sleep(0.4)  # Pause anti-rate-limit
        except Exception as e:
            print(f"Erreur lors de la suppression de {channel.name}: {e}")

    # 3. Supprimer la catégorie elle-même
    await category.delete(reason="Nettoyage catégorie")

    await interaction.followup.send(
        f"🗑️ La catégorie **{nom_categorie}** et ses **{total_channels} salons** ont été supprimés avec succès !",
        ephemeral=True
    )

@bot.tree.command(
    name="purger_equipe_duos", 
    description="Supprime toutes les catégories commençant par ce nom (ex: Duos Rouge - 1, Duos Rouge - 2...)"
)
@app_commands.describe(
    prefixe="Début du nom des catégories à supprimer (ex: Duos Rouge)"
)
@app_commands.default_permissions(administrator=True)
async def purger_equipe_duos(interaction: discord.Interaction, prefixe: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    categories_to_delete = [
        c for c in guild.categories 
        if c.name.lower().startswith(prefixe.strip().lower())
    ]

    if not categories_to_delete:
        await interaction.followup.send(f"❌ Aucune catégorie ne commence par **{prefixe}**.", ephemeral=True)
        return

    total_channels_deleted = 0
    total_cats_deleted = len(categories_to_delete)

    for cat in categories_to_delete:
        for ch in cat.channels:
            try:
                await ch.delete(reason="Purge complète")
                total_channels_deleted += 1
                await asyncio.sleep(0.4)
            except Exception:
                pass
        await cat.delete(reason="Purge complète")
        await asyncio.sleep(0.5)

    await interaction.followup.send(
        f"🗑️ Nettoyage terminé : **{total_cats_deleted} catégorie(s)** et **{total_channels_deleted} salon(s)** supprimés !",
        ephemeral=True
    )
bot.run(os.getenv("DISCORD_TOKEN"))
