import discord
from discord import app_commands
from discord.ext import commands
import itertools
import asyncio

intents = discord.Intents.default()
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Bot connecté en tant que {bot.user}")

@bot.tree.command(name="creer_duos", description="Génère tous les salons duos privés pour une liste de rôles.")
@app_commands.describe(
    nom_equipe="Nom de l'équipe / de la catégorie",
    roles="Mentionne les rôles séparés par des espaces (ex: @Candidat1 @Candidat3 @Candidat4)"
)
async def creer_duos(interaction: discord.Interaction, nom_equipe: str, roles: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    # 1. Extraction des IDs de rôles mentionnés
    role_ids = [int(r.strip("<@&>")) for r in roles.split() if r.startswith("<@&") and r.endswith(">")]
    roles_list = [guild.get_role(r_id) for r_id in role_ids if guild.get_role(r_id) is not None]

    if len(roles_list) < 2:
        await interaction.followup.send("❌ Veuillez mentionner au moins 2 rôles valides.", ephemeral=True)
        return

    # 2. Création de la catégorie dédiée
    category = await guild.create_category(nom_equipe)

    # 3. Génération des paires uniques
    duos = list(itertools.combinations(roles_list, 2))
    
    for r1, r2 in duos:
        # Permissions : invisible pour @everyone, visible uniquement pour r1 et r2
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, view_channel=False),
            r1: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True),
            r2: discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, view_channel=True)
        }
        
        nom_salon = f"duo-{r1.name.lower().replace(' ', '-')}-{r2.name.lower().replace(' ', '-')}"
        await guild.create_text_channel(name=nom_salon, category=category, overwrites=overwrites)
        await asyncio.sleep(0.5) # Pause anti-rate-limit

    await interaction.followup.send(f"✅ {len(duos)} salons duos créés dans la catégorie **{nom_equipe}** !", ephemeral=True)

import os
bot.run(os.getenv("DISCORD_TOKEN"))
