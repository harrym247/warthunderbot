import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import aiohttp
from bs4 import BeautifulSoup
import asyncpg
import os

load_dotenv()
token = os.getenv('DISCORD_TOKEN')

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class MyClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

bot = MyClient()
player_data = {}
db_pool = None

squadrons = {
    'Blackfoot': 'https://warthunder.com/en/community/claninfo/Blackfoot',
    'Blackfoot 54': 'https://warthunder.com/en/community/claninfo/Blackfoot%2054',
    'Blackfoot X-Ray': 'https://warthunder.com/en/community/claninfo/Blackfoot%20X-Ray'
}

@bot.event
async def on_ready():
    global db_pool
    db_pool = await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME
    )

    # Instant sync for dev/testing in your guild
    guild = discord.Object(id=1369447013732712589)
    bot.tree.clear_commands(guild=guild)
    await bot.tree.sync(guild=guild)

    print(f'Logged in as {bot.user.name}')
    print("✅ Slash commands cleared and re-synced to guild.")
    print("✅ Connected to PostgreSQL.")

    for command in bot.tree.get_commands(guild=guild):
        print(f"✅ Slash command registered in guild: /{command.name}")




async def fetch_data(squadron_name):
    global player_data
    url = squadrons[squadron_name]
    player_data[squadron_name] = []

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                html = await response.text()
                soup = BeautifulSoup(html, 'html.parser')
                grid_items = soup.find_all('div', class_='squadrons-members__grid-item')

                for i in range(0, len(grid_items), 6):
                    try:
                        name = grid_items[i + 1].get_text(strip=True)
                        rating = grid_items[i + 2].get_text(strip=True)
                        if not rating.isdigit():
                            continue
                        player_data[squadron_name].append({
                            'name': name,
                            'rating': rating,
                            'activity': grid_items[i + 3].get_text(strip=True),
                            'role': grid_items[i + 4].get_text(strip=True),
                            'join_date': grid_items[i + 5].get_text(strip=True)
                        })
                    except IndexError:
                        continue

def format_paginated_embed(title, players, page, per_page=25):
    start = page * per_page
    end = start + per_page
    current_players = players[start:end]

    embed = discord.Embed(title=f"{title} (Page {page+1})", color=discord.Color.green())
    for idx, p in enumerate(current_players, start=1 + start):
        embed.add_field(
            name=f"{idx}. {p['name']}",
            value=(
                f"**Rating:** {p['rating']} | "
                f"**Activity:** {p['activity']} | "
                f"**Role:** {p['role']} | "
                f"**Joined:** {p['join_date']}"
            ),
            inline=False
        )
    return embed

class PaginatedEmbedView(discord.ui.View):
    def __init__(self, players, title, per_page=25):
        super().__init__(timeout=60)
        self.players = players
        self.title = title
        self.per_page = per_page
        self.page = 0
        self.max_page = (len(players) - 1) // per_page

        self.add_item(PreviousPageButton(self))
        self.add_item(NextPageButton(self))
        self.add_item(BackToMenuButton())
        self.add_item(ExitButton())

    async def send_initial(self, interaction: discord.Interaction):
        embed = format_paginated_embed(self.title, self.players, self.page, self.per_page)
        await interaction.response.send_message(embed=embed, view=self, ephemeral=True)

class PreviousPageButton(discord.ui.Button):
    def __init__(self, view: PaginatedEmbedView):
        super().__init__(label="⬅ Previous", style=discord.ButtonStyle.secondary)
        self.view_ref = view

    async def callback(self, interaction: discord.Interaction):
        view = self.view_ref
        if view.page > 0:
            view.page -= 1
            embed = format_paginated_embed(view.title, view.players, view.page, view.per_page)
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.defer()

class NextPageButton(discord.ui.Button):
    def __init__(self, view: PaginatedEmbedView):
        super().__init__(label="Next ➡", style=discord.ButtonStyle.secondary)
        self.view_ref = view

    async def callback(self, interaction: discord.Interaction):
        view = self.view_ref
        if view.page < view.max_page:
            view.page += 1
            embed = format_paginated_embed(view.title, view.players, view.page, view.per_page)
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.defer()

class Top5Button(discord.ui.Button):
    def __init__(self, squadron_name):
        super().__init__(label="Top 5 Players", style=discord.ButtonStyle.success)
        self.squadron_name = squadron_name

    async def callback(self, interaction: discord.Interaction):
        top = sorted(player_data[self.squadron_name], key=lambda x: int(x['rating']), reverse=True)[:5]
        embed = format_paginated_embed(f"Top 5 Players in {self.squadron_name}", top, page=0, per_page=25)
        await interaction.response.send_message(embed=embed, ephemeral=True)

class Over850Button(discord.ui.Button):
    def __init__(self, squadron_name):
        super().__init__(label=">850 Points", style=discord.ButtonStyle.success)
        self.squadron_name = squadron_name

    async def callback(self, interaction: discord.Interaction):
        filtered = [p for p in player_data[self.squadron_name] if int(p['rating']) > 850]
        if not filtered:
            await interaction.response.send_message("No players with more than 850 points.", ephemeral=True)
            return
        view = PaginatedEmbedView(filtered, f">850 Rating in {self.squadron_name}")
        await view.send_initial(interaction)

class ZeroPointsButton(discord.ui.Button):
    def __init__(self, squadron_name):
        super().__init__(label="Players on 0 Points", style=discord.ButtonStyle.danger)
        self.squadron_name = squadron_name

    async def callback(self, interaction: discord.Interaction):
        zero_players = [p for p in player_data[self.squadron_name] if p['rating'] == "0"]
        if not zero_players:
            await interaction.response.send_message("✅ No players with 0 points.", ephemeral=True)
            return
        view = PaginatedEmbedView(zero_players, f"Players on 0 Points in {self.squadron_name}")
        await view.send_initial(interaction)

class BackToMenuButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Back to Menu", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Select a squadron:", view=SquadronMenu())

class ExitButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Exit", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        await interaction.message.delete()

class SquadronActionView(discord.ui.View):
    def __init__(self, squadron_name):
        super().__init__(timeout=60)
        self.add_item(Top5Button(squadron_name))
        self.add_item(Over850Button(squadron_name))
        self.add_item(ZeroPointsButton(squadron_name))
        self.add_item(BackToMenuButton())
        self.add_item(ExitButton())

class BlackfootButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Blackfoot", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        await fetch_data("Blackfoot")
        await interaction.response.edit_message(content="Blackfoot loaded. Choose an option:", view=SquadronActionView("Blackfoot"))

class Blackfoot54Button(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Blackfoot 54", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        await fetch_data("Blackfoot 54")
        await interaction.response.edit_message(content="Blackfoot 54 loaded. Choose an option:", view=SquadronActionView("Blackfoot 54"))

class BlackfootXRayButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Blackfoot X-Ray", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        await fetch_data("Blackfoot X-Ray")
        await interaction.response.edit_message(content="Blackfoot X-Ray loaded. Choose an option:", view=SquadronActionView("Blackfoot X-Ray"))

class SquadronMenu(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(BlackfootButton())
        self.add_item(Blackfoot54Button())
        self.add_item(BlackfootXRayButton())

@bot.tree.command(name="menu", description="Open the squadron selection menu")
async def menu(interaction: discord.Interaction):
    await interaction.response.send_message("Select a squadron:", view=SquadronMenu(), ephemeral=True)

# --- vehicles command logic (continued) ---

@bot.tree.command(name="vehicle_queue", description="Select your vehicles for the current battle rating")
async def vehicles(interaction: discord.Interaction):
    br = await get_current_battle_rating()
    if not br:
        await interaction.response.send_message("❌ Could not determine current battle rating.", ephemeral=True)
        return

    all_vehicles = await get_all_vehicles_for_br(br)
    if not all_vehicles:
        await interaction.response.send_message(f"❌ No vehicles found for BR {br}.", ephemeral=True)
        return

    air_vehicles = [v for v in all_vehicles if v['vehicle_type'].lower() == 'air']
    ground_vehicles = [v for v in all_vehicles if v['vehicle_type'].lower() == 'ground']

    user_id = f"{interaction.user.name}#{interaction.user.discriminator}"
    if interaction.user.nick and "|" in interaction.user.nick:
        warthunder_user = interaction.user.nick.split("|")[0].strip()
    elif interaction.user.nick:
        warthunder_user = interaction.user.nick.strip()
    else:
        warthunder_user = interaction.user.name

    await clear_existing_user_entries(user_id)

    await interaction.response.send_message(
        f"✈️ Select your **AIR** vehicles for BR {br}:",
        view=VehicleSelectionView(air_vehicles, user_id, warthunder_user, is_air=True, ground_vehicles=ground_vehicles),
        ephemeral=True
    )

# vehicle helper functions
async def get_current_battle_rating():
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT sqb_br FROM sqb_schedule
            WHERE NOW() BETWEEN sqb_date AND end_date
            LIMIT 1
        """)
        if row:
            br = str(row['sqb_br'])
            return br.rstrip('.0') if br.endswith('.0') else br
        return None

async def get_all_vehicles_for_br(br):
    async with db_pool.acquire() as conn:
        return await conn.fetch("""
            SELECT vehicle_id, vehicle_name, vehicle_type
            FROM vehicle_table
            WHERE vehicle_br = $1
        """, br)

async def clear_existing_user_entries(user_id):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            DELETE FROM discord_data_gathered WHERE user_id = $1
        """, user_id)

async def store_user_vehicle(user_id, vehicle_id, warthunder_user):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO discord_data_gathered (user_id, vehicle_id, warthunder_user)
            VALUES ($1, $2, $3)
        """, user_id, vehicle_id, warthunder_user)

class VehicleSelect(discord.ui.Select):
    def __init__(self, vehicle_options, user_id, warthunder_user, is_air, ground_vehicles=None):
        options = [
            discord.SelectOption(label=v['vehicle_name'], value=str(v['vehicle_id']))
            for v in vehicle_options[:25]
        ]
        placeholder = "Select AIR vehicles..." if is_air else "Select GROUND vehicles..."
        super().__init__(placeholder=placeholder, min_values=0, max_values=10, options=options)
        self.user_id = user_id
        self.warthunder_user = warthunder_user
        self.is_air = is_air
        self.ground_vehicles = ground_vehicles

    async def callback(self, interaction: discord.Interaction):
        if not self.values:
            async with db_pool.acquire() as conn:
                result = await conn.fetchrow("""
                    SELECT vehicle_id FROM vehicle_table
                    WHERE vehicle_name = 'N/A' AND LOWER(vehicle_type) = $1
                    LIMIT 1
                """, 'air' if self.is_air else 'ground')
            if result:
                await store_user_vehicle(self.user_id, result['vehicle_id'], self.warthunder_user)
        else:
            for vehicle_id in self.values:
                await store_user_vehicle(self.user_id, int(vehicle_id), self.warthunder_user)

        if self.is_air and self.ground_vehicles:
            await interaction.response.send_message(
                "✅ Air vehicles saved. Now select your **GROUND** vehicles:",
                view=VehicleSelectionView(self.ground_vehicles, self.user_id, self.warthunder_user, is_air=False),
                ephemeral=True
            )
        else:
            await interaction.response.send_message("✅ All vehicle selections have been saved.", ephemeral=True)

class VehicleSelectionView(discord.ui.View):
    def __init__(self, vehicle_rows, user_id, warthunder_user, is_air=True, ground_vehicles=None):
        super().__init__(timeout=120)
        vehicle_options = [{'vehicle_id': r['vehicle_id'], 'vehicle_name': r['vehicle_name']} for r in vehicle_rows]
        if vehicle_options:
            self.add_item(VehicleSelect(vehicle_options, user_id, warthunder_user, is_air, ground_vehicles))


bot.run(token)