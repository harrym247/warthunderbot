import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
import aiohttp
from bs4 import BeautifulSoup
import asyncpg
import os
import functools
from datetime import datetime, timedelta

load_dotenv('linkdata.env')
token = os.getenv('DISCORD_TOKEN')

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

class MyClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

bot = MyClient()
player_data = {}
db_pool = None
user_messages = {}  # Store message IDs by user ID for deletion when they leave

# Define monitored voice channels
MONITORED_VOICE_CHANNELS = [1213375603919818802, 1213597037661261845]
TEXT_CHANNEL_ID = 1324178202129600532  # Channel where vehicle messages are posted

squadrons = {
    'Blackfoot': 'https://warthunder.com/en/community/claninfo/Blackfoot',
    'Blackfoot 54': 'https://warthunder.com/en/community/claninfo/Blackfoot%2054',
    'Blackfoot X-Ray': 'https://warthunder.com/en/community/claninfo/Blackfoot%20X-Ray'
}

# Role to squadron mapping
role_squadron_mapping = {
    'BLKFT Member': 'Blackfoot',
    'BKF54 Member': 'Blackfoot 54', 
    'BFXRY Member': 'Blackfoot X-Ray'
}

@bot.event
async def on_ready():
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            min_size=1,
            max_size=10,
            command_timeout=60
        )
        print("✅ Connected to PostgreSQL.")
    except Exception as e:
        print(f"❌ Failed to connect to PostgreSQL: {e}")
        print("Please check your database connection settings in the .env file")
        return

    try:
        guild = discord.Object(id=779462911713607690)
        await bot.tree.sync(guild=guild)
        print("✅ Slash commands synced to guild.")
    except Exception as e:
        print(f"❌ Failed to sync commands: {e}")

    print(f'Logged in as {bot.user.name}')
    for command in bot.tree.get_commands(guild=discord.Object(id=779462911713607690)):
        print(f"✅ Slash command registered in guild: /{command.name}")
    
    # Start the squadron data update task
    update_squadron_data.start()
    print("✅ Squadron data update task started (runs every 6 hours)")
    print(f"📢 Monitoring voice channels: {MONITORED_VOICE_CHANNELS}")
    print(f"📝 Posting vehicle messages to channel: {TEXT_CHANNEL_ID}")
    
    # Check for users already in monitored voice channels (failsafe)
    await check_existing_voice_users()

# ──────────────── FAILSAFE FUNCTION FOR STARTUP ────────────────

async def check_existing_voice_users():
    """Check for users already in monitored voice channels when bot starts (failsafe)"""
    if db_pool is None:
        print("❌ Database not available for startup voice check")
        return
    
    try:
        # Get current battle rating
        async with db_pool.acquire() as conn:
            br_row = await conn.fetchrow("""
                SELECT sqb_br FROM sqb_schedule
                WHERE NOW() BETWEEN sqb_date AND end_date
                LIMIT 1
            """)
            if not br_row:
                print("Debug: No current battle rating found for startup voice check")
                return
            
            br = str(br_row['sqb_br']).rstrip('.0')
        
        text_channel = bot.get_channel(TEXT_CHANNEL_ID)
        if not text_channel:
            print(f"❌ Could not find text channel {TEXT_CHANNEL_ID} for startup voice check")
            return
        
        users_processed = 0
        for guild in bot.guilds:
            for channel_id in MONITORED_VOICE_CHANNELS:
                voice_channel = guild.get_channel(channel_id)
                if voice_channel and voice_channel.members:
                    print(f"🔍 Startup check: Found {len(voice_channel.members)} users in voice channel {channel_id}")
                    
                    for member in voice_channel.members:
                        if member.bot:  # Skip bots
                            continue
                        
                        # Skip if user already has a message posted (avoid duplicates)
                        if member.id in user_messages:
                            continue
                        
                        user_id = f"{member.name}#{member.discriminator}"
                        warthunder_user = member.nick.split("|")[0].strip() if member.nick and "|" in member.nick else (member.nick or member.name)
                        
                        print(f"Debug: Processing startup user {member.name} in voice channel {channel_id}")
                        
                        # Get user's vehicles for current BR
                        async with db_pool.acquire() as conn:
                            vehicles = await conn.fetch("""
                                SELECT vt.vehicle_name, vt.vehicle_type, n.nation_name
                                FROM discord_data_gathered dg
                                JOIN vehicle_table vt ON vt.vehicle_id = dg.vehicle_id
                                JOIN nations n ON vt.nation_id = n.nation_id
                                WHERE dg.user_id = $1 AND TRIM(TRAILING '.0' FROM vt.vehicle_br::TEXT) = $2
                                AND vt.vehicle_name NOT ILIKE '%no vehicle%'
                                AND vt.vehicle_name NOT ILIKE '%n/a%'
                                ORDER BY 
                                    CASE WHEN n.nation_id = 11 THEN 1 ELSE 0 END,
                                    n.nation_id,
                                    vt.vehicle_type,
                                    vt.vehicle_name
                            """, user_id, br)
                        
                        # Post vehicle message (same logic as voice state update)
                        if not vehicles:
                            embed = discord.Embed(
                                title="⚠️ No Vehicles Set",
                                description=f"<@{member.id}> You have no vehicles listed for **BR {br}**.",
                                color=0xFF6B6B
                            )
                            embed.add_field(
                                name="💡 How to fix this",
                                value="Use `/sqb_queue` to select your vehicles",
                                inline=False
                            )
                            
                            # Get squadron data for the user
                            squadron_data = await get_squadron_data_for_user(member, warthunder_user)
                            if squadron_data:
                                embed.add_field(
                                    name=f"🏆 {squadron_data['squadron']} Stats",
                                    value=f"**Points:** {squadron_data['points']}\n**Activity:** {squadron_data['activity']}",
                                    inline=False
                                )
                            
                            embed.set_footer(text="🔄 Posted on bot startup")
                            message = await text_channel.send(embed=embed)
                            user_messages[member.id] = message.id
                            print(f"Debug: Posted 'no vehicles' startup message for {member.name}")
                        else:
                            # Group vehicles by type for better organization
                            vehicles_by_type = {}
                            for v in vehicles:
                                vtype = v['vehicle_type'].lower()
                                # Categorize vehicle types
                                if vtype in ['tank', 'ground', 'medium tank', 'heavy tank', 'light tank', 'tank destroyer']:
                                    category = 'Ground'
                                    emoji = '🛡️'
                                elif vtype in ['spaa', 'anti-aircraft']:
                                    category = 'SPAA'
                                    emoji = '🎯'
                                elif vtype in ['aircraft', 'air', 'fighter', 'bomber', 'attacker']:
                                    category = 'Aircraft'
                                    emoji = '✈️'
                                elif vtype in ['helicopter', 'heli']:
                                    category = 'Helicopters'
                                    emoji = '🚁'
                                else:
                                    category = 'Other'
                                    emoji = '❓'
                                
                                if category not in vehicles_by_type:
                                    vehicles_by_type[category] = {'emoji': emoji, 'vehicles': []}
                                
                                vehicles_by_type[category]['vehicles'].append(f"{v['vehicle_name']} ({v['nation_name']})")

                            embed = discord.Embed(
                                title=f"🎮 {warthunder_user} was in voice chat",
                                description=f"**Battle Rating:** {br}",
                                color=0x9C27B0  # Purple color to differentiate from join/update messages
                            )

                            # Add fields for each vehicle type (only if vehicles exist)
                            for category, data in vehicles_by_type.items():
                                if data['vehicles']:  # Only show categories that have vehicles
                                    vehicle_list = "\n".join([f"• {vehicle}" for vehicle in data['vehicles']])
                                    embed.add_field(
                                        name=f"{data['emoji']} {category} ({len(data['vehicles'])})",
                                        value=vehicle_list,
                                        inline=True
                                    )

                            # Get squadron data for the user
                            squadron_data = await get_squadron_data_for_user(member, warthunder_user)
                            if squadron_data:
                                embed.add_field(
                                    name=f"🏆 {squadron_data['squadron']} Stats",
                                    value=f"**Points:** {squadron_data['points']}\n**Activity:** {squadron_data['activity']}",
                                    inline=False
                                )

                            # Add footer with total count and startup indicator
                            total_vehicles = sum(len(data['vehicles']) for data in vehicles_by_type.values())
                            embed.set_footer(text=f"Total vehicles: {total_vehicles} • 🔄 Posted on bot startup")
                            
                            message = await text_channel.send(embed=embed)
                            user_messages[member.id] = message.id
                            print(f"Debug: Posted vehicle list startup message for {member.name}")
                        
                        users_processed += 1
        
        if users_processed > 0:
            print(f"✅ Startup failsafe: Posted vehicle messages for {users_processed} users already in voice channels")
        else:
            print("✅ Startup failsafe: No users found in monitored voice channels")
            
    except Exception as e:
        print(f"❌ Error in startup voice check: {e}")

# ──────────────── SQUADRON DATA CACHING SYSTEM ────────────────

@tasks.loop(hours=6)
async def update_squadron_data():
    """Update squadron member data every 6 hours"""
    if db_pool is None:
        print("❌ Database not available for squadron data update")
        return
    
    print(f"🔄 Starting squadron data update at {datetime.now()}")
    
    try:
        async with db_pool.acquire() as conn:
            # Create the squadron_cache table if it doesn't exist
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS squadron_cache (
                    player_name TEXT PRIMARY KEY,
                    squadron_name TEXT NOT NULL,
                    points INTEGER NOT NULL,
                    activity INTEGER NOT NULL,
                    last_updated TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                )
            """)
            
            # Clear old data
            await conn.execute("DELETE FROM squadron_cache")
            print("🗑️ Cleared old squadron cache data")
            
            # Scrape data from all squadrons
            total_players = 0
            for squadron_name, squadron_url in squadrons.items():
                print(f"🔍 Scraping {squadron_name}...")
                players_data = await scrape_squadron_data(squadron_url, squadron_name)
                
                if players_data:
                    # Insert data into cache table
                    for player_data in players_data:
                        await conn.execute("""
                            INSERT INTO squadron_cache (player_name, squadron_name, points, activity)
                            VALUES ($1, $2, $3, $4)
                            ON CONFLICT (player_name) DO UPDATE SET
                                squadron_name = EXCLUDED.squadron_name,
                                points = EXCLUDED.points,
                                activity = EXCLUDED.activity,
                                last_updated = NOW()
                        """, player_data['name'], squadron_name, player_data['points'], player_data['activity'])
                    
                    total_players += len(players_data)
                    print(f"✅ Cached {len(players_data)} players from {squadron_name}")
                else:
                    print(f"❌ Failed to scrape data from {squadron_name}")
            
            print(f"✅ Squadron data update complete! Cached {total_players} total players")
            
    except Exception as e:
        print(f"❌ Error updating squadron data: {e}")

@update_squadron_data.before_loop
async def before_update_squadron_data():
    """Wait for bot to be ready before starting the loop"""
    await bot.wait_until_ready()

async def scrape_squadron_data(squadron_url, squadron_name):
    """Scrape all player data from a squadron page"""
    async with aiohttp.ClientSession() as session:
        async with session.get(squadron_url) as response:
            if response.status == 200:
                html = await response.text()
                soup = BeautifulSoup(html, 'html.parser')
                
                players_data = []
                
                # Find all grid items
                grid_items = soup.find_all('div', class_='squadrons-members__grid-item')
                
                # Process grid items in groups of 6 (each player has 6 columns)
                for i in range(0, len(grid_items), 6):
                    if i + 5 < len(grid_items):  # Ensure we have all 6 columns
                        # Column 2: Player name
                        player_div = grid_items[i + 1]
                        player_link = player_div.find('a')
                        
                        if player_link:
                            player_name = player_link.get_text(strip=True)
                            
                            # Clean platform suffixes from player names
                            player_name = clean_player_name(player_name)
                            
                            # Column 3: Personal clan rating (points)
                            points_div = grid_items[i + 2]
                            points_text = points_div.get_text(strip=True)
                            points = int(points_text) if points_text.isdigit() else 0
                            
                            # Column 4: Activity
                            activity_div = grid_items[i + 3]
                            activity_text = activity_div.get_text(strip=True)
                            activity = int(activity_text) if activity_text.isdigit() else 0
                            
                            players_data.append({
                                'name': player_name,
                                'points': points,
                                'activity': activity
                            })
                
                return players_data
            else:
                return None

async def get_squadron_data_for_user(member, warthunder_user):
    """Get squadron points and activity for a user from cache"""
    squadron_name = None
    
    # Check user's roles to determine which squadron they belong to
    for role in member.roles:
        if role.name in role_squadron_mapping:
            squadron_name = role_squadron_mapping[role.name]
            break
    
    if not squadron_name:
        print(f"Debug: No squadron role found for {member.name}")
        return None
    
    if db_pool is None:
        print("Debug: Database not available for squadron data lookup")
        return None
    
    try:
        async with db_pool.acquire() as conn:
            # Get data from cache table
            row = await conn.fetchrow("""
                SELECT squadron_name, points, activity, last_updated
                FROM squadron_cache
                WHERE player_name = $1 AND squadron_name = $2
            """, clean_player_name(warthunder_user), squadron_name)
            
            if row:
                print(f"Debug: Found cached data for {warthunder_user} in {squadron_name} - Points: {row['points']}, Activity: {row['activity']}")
                return {
                    'squadron': row['squadron_name'],
                    'points': str(row['points']),
                    'activity': str(row['activity'])
                }
            else:
                print(f"Debug: No cached data found for {warthunder_user} in {squadron_name}")
                # Check if cache is empty (might need manual update)
                cache_count = await conn.fetchval("SELECT COUNT(*) FROM squadron_cache WHERE squadron_name = $1", squadron_name)
                if cache_count == 0:
                    print(f"Debug: Cache is empty for {squadron_name}, triggering manual update")
                    # Trigger a manual update for this squadron
                    squadron_url = squadrons.get(squadron_name)
                    if squadron_url:
                        players_data = await scrape_squadron_data(squadron_url, squadron_name)
                        if players_data:
                            for player_data in players_data:
                                await conn.execute("""
                                    INSERT INTO squadron_cache (player_name, squadron_name, points, activity)
                                    VALUES ($1, $2, $3, $4)
                                    ON CONFLICT (player_name) DO UPDATE SET
                                        squadron_name = EXCLUDED.squadron_name,
                                        points = EXCLUDED.points,
                                        activity = EXCLUDED.activity,
                                        last_updated = NOW()
                                """, player_data['name'], squadron_name, player_data['points'], player_data['activity'])
                            
                            # Try to get the data again
                            row = await conn.fetchrow("""
                                SELECT squadron_name, points, activity
                                FROM squadron_cache
                                WHERE player_name = $1 AND squadron_name = $2
                            """, clean_player_name(warthunder_user), squadron_name)
                            
                            if row:
                                return {
                                    'squadron': row['squadron_name'],
                                    'points': str(row['points']),
                                    'activity': str(row['activity'])
                                }
                
                return None
                
    except Exception as e:
        print(f"Debug: Error getting squadron data from cache: {e}")
        return None

@bot.tree.command(name="sqb_queue", description="Select your vehicles for the current battle rating", guild=discord.Object(id=779462911713607690))
async def sqb_queue(interaction: discord.Interaction):
    br = await get_current_battle_rating()
    if not br:
        await interaction.response.send_message("❌ Could not determine current battle rating.", ephemeral=True)
        return

    all_vehicles = await get_all_vehicles_for_br(br)
    if not all_vehicles:
        await interaction.response.send_message(f"❌ No vehicles found for BR {br}.", ephemeral=True)
        return

    # Debug: Print vehicle types to see what we're getting
    print(f"Debug: Found {len(all_vehicles)} vehicles for BR {br}")
    vehicle_types_found = set()
    for v in all_vehicles:
        vehicle_types_found.add(v['vehicle_type'].lower())
    print(f"Debug: Vehicle types found: {vehicle_types_found}")

    vehicles_by_type = {'ground': [], 'spaa': [], 'air': [], 'heli': []}
    for v in all_vehicles:
        vtype = v['vehicle_type'].lower()
        # Handle different possible vehicle type names
        if vtype in ['tank', 'ground', 'medium tank', 'heavy tank', 'light tank', 'tank destroyer']:
            vehicles_by_type['ground'].append(v)
        elif vtype in ['spaa', 'anti-aircraft']:
            vehicles_by_type['spaa'].append(v)
        elif vtype in ['aircraft', 'air', 'fighter', 'bomber', 'attacker']:
            vehicles_by_type['air'].append(v)
        elif vtype in ['helicopter', 'heli']:
            vehicles_by_type['heli'].append(v)
        else:
            # If we don't recognize the type, put it in ground as default
            print(f"Debug: Unknown vehicle type '{vtype}' for {v['vehicle_name']}, adding to ground")
            vehicles_by_type['ground'].append(v)

    # Debug: Print how many vehicles per type
    for vtype, vehicles in vehicles_by_type.items():
        print(f"Debug: {vtype}: {len(vehicles)} vehicles")

    user_id = f"{interaction.user.name}#{interaction.user.discriminator}"
    if interaction.user.nick and "|" in interaction.user.nick:
        warthunder_user = interaction.user.nick.split("|")[0].strip()
    elif interaction.user.nick:
        warthunder_user = interaction.user.nick.strip()
    else:
        warthunder_user = interaction.user.name

    existing_vehicle_ids = await get_user_vehicle_ids(user_id, br)
    selected_ids = {str(v_id) for v_id in existing_vehicle_ids}
    await interaction.response.defer(ephemeral=True)

    async def show_next_selection(interaction, user_id, warthunder_user, br, vehicles_by_type, selected_ids, index=0):
        type_order = ['ground', 'spaa', 'air', 'heli']
        
        # Find the next type that has vehicles, but don't skip any types automatically
        while index < len(type_order):
            current_type = type_order[index]
            vehicles = vehicles_by_type.get(current_type, [])
            
            print(f"Debug: Processing {current_type} with {len(vehicles)} vehicles")
            
            # Always show the selection, even if empty (let user see there are no vehicles)
            view = VehicleSelectionView(
                vehicles,
                user_id,
                warthunder_user,
                is_air=(current_type == 'air'),
                next_callback=functools.partial(show_next_selection, interaction, user_id, warthunder_user, br, vehicles_by_type, selected_ids, index + 1),
                selected_ids=selected_ids
            )

            if vehicles:
                message = f"📋 Select your **{current_type.upper()}** vehicles for BR {br}:"
            else:
                message = f"📋 No **{current_type.upper()}** vehicles available for BR {br}. Click to continue."

            await interaction.followup.send(message, view=view, ephemeral=True)
            return
        
        # All vehicle selections complete - check if user is in any monitored voice channel
        member = interaction.user
        if member.voice and member.voice.channel and member.voice.channel.id in MONITORED_VOICE_CHANNELS:
            await post_user_vehicles_and_cleanup(member, user_id, warthunder_user, br)
            return  # Don't send the completion message since we posted the vehicle message
        
        # If we've gone through all types
        await interaction.followup.send("✅ All vehicle selections have been saved.", ephemeral=True)

    await show_next_selection(interaction, user_id, warthunder_user, br, vehicles_by_type, selected_ids)

@bot.event
async def on_voice_state_update(member, before, after):
    # Check if user is leaving any monitored voice channel
    if before.channel and before.channel.id in MONITORED_VOICE_CHANNELS:
        # User left a monitored channel, delete their message if it exists
        if member.id in user_messages:
            try:
                channel = bot.get_channel(TEXT_CHANNEL_ID)
                if channel:
                    message = await channel.fetch_message(user_messages[member.id])
                    await message.delete()
                    print(f"Debug: Deleted message for {member.name} who left voice channel {before.channel.id}")
            except discord.NotFound:
                print(f"Debug: Message not found for {member.name}, already deleted")
            except Exception as e:
                print(f"Debug: Error deleting message for {member.name}: {e}")
            finally:
                # Remove from tracking regardless of deletion success
                del user_messages[member.id]
    
    # Only trigger when user joins any monitored voice channel
    if after.channel is None or after.channel.id not in MONITORED_VOICE_CHANNELS:
        return
    
    # Don't trigger if user was already in the same channel
    if before.channel == after.channel:
        return

    print(f"Debug: {member.name} joined monitored voice channel {after.channel.id}")

    channel = bot.get_channel(TEXT_CHANNEL_ID)
    if channel is None:
        print(f"Debug: Could not find text channel {TEXT_CHANNEL_ID}")
        return

    user_id = f"{member.name}#{member.discriminator}"
    warthunder_user = member.nick.split("|")[0].strip() if member.nick and "|" in member.nick else (member.nick or member.name)

    async with db_pool.acquire() as conn:
        br_row = await conn.fetchrow("""
            SELECT sqb_br FROM sqb_schedule
            WHERE NOW() BETWEEN sqb_date AND end_date
            LIMIT 1
        """)
        if not br_row:
            print("Debug: No current battle rating found in schedule")
            return

        br = str(br_row['sqb_br']).rstrip('.0')
        
        # Query using user_id to match how vehicles are stored
        vehicles = await conn.fetch("""
            SELECT vt.vehicle_name, vt.vehicle_type, n.nation_name
            FROM discord_data_gathered dg
            JOIN vehicle_table vt ON vt.vehicle_id = dg.vehicle_id
            JOIN nations n ON vt.nation_id = n.nation_id
            WHERE dg.user_id = $1 AND TRIM(TRAILING '.0' FROM vt.vehicle_br::TEXT) = $2
            AND vt.vehicle_name NOT ILIKE '%no vehicle%'
            AND vt.vehicle_name NOT ILIKE '%n/a%'
            ORDER BY 
                CASE WHEN n.nation_id = 11 THEN 1 ELSE 0 END,
                n.nation_id,
                vt.vehicle_type,
                vt.vehicle_name
        """, user_id, br)

    if not vehicles:
        embed = discord.Embed(
            title="⚠️ No Vehicles Set",
            description=f"<@{member.id}> You have no vehicles listed for **BR {br}**.",
            color=0xFF6B6B
        )
        embed.add_field(
            name="💡 How to fix this",
            value="Use `/sqb_queue` to select your vehicles",
            inline=False
        )
        
        # Get squadron data for the user
        squadron_data = await get_squadron_data_for_user(member, warthunder_user)
        if squadron_data:
            embed.add_field(
                name=f"🏆 {squadron_data['squadron']} Stats",
                value=f"**Points:** {squadron_data['points']}\n**Activity:** {squadron_data['activity']}",
                inline=False
            )
        
        message = await channel.send(embed=embed)
        # Store the message ID for potential deletion
        user_messages[member.id] = message.id
        print(f"Debug: Posted 'no vehicles' message for {member.name}")
    else:
        # Group vehicles by type for better organization
        vehicles_by_type = {}
        for v in vehicles:
            vtype = v['vehicle_type'].lower()
            # Categorize vehicle types
            if vtype in ['tank', 'ground', 'medium tank', 'heavy tank', 'light tank', 'tank destroyer']:
                category = 'Ground'
                emoji = '🛡️'
            elif vtype in ['spaa', 'anti-aircraft']:
                category = 'SPAA'
                emoji = '🎯'
            elif vtype in ['aircraft', 'air', 'fighter', 'bomber', 'attacker']:
                category = 'Aircraft'
                emoji = '✈️'
            elif vtype in ['helicopter', 'heli']:
                category = 'Helicopters'
                emoji = '🚁'
            else:
                category = 'Other'
                emoji = '❓'
            
            if category not in vehicles_by_type:
                vehicles_by_type[category] = {'emoji': emoji, 'vehicles': []}
            
            vehicles_by_type[category]['vehicles'].append(f"{v['vehicle_name']} ({v['nation_name']})")

        embed = discord.Embed(
            title=f"🎮 {warthunder_user} joined voice chat",
            description=f"**Battle Rating:** {br}",
            color=0x4CAF50
        )

        # Add fields for each vehicle type (only if vehicles exist)
        for category, data in vehicles_by_type.items():
            if data['vehicles']:  # Only show categories that have vehicles
                vehicle_list = "\n".join([f"• {vehicle}" for vehicle in data['vehicles']])
                embed.add_field(
                    name=f"{data['emoji']} {category} ({len(data['vehicles'])})",
                    value=vehicle_list,
                    inline=True
                )

        # Get squadron data for the user
        squadron_data = await get_squadron_data_for_user(member, warthunder_user)
        if squadron_data:
            embed.add_field(
                name=f"🏆 {squadron_data['squadron']} Stats",
                value=f"**Points:** {squadron_data['points']}\n**Activity:** {squadron_data['activity']}",
                inline=False
            )

        # Add footer with total count
        total_vehicles = sum(len(data['vehicles']) for data in vehicles_by_type.values())
        embed.set_footer(text=f"Total vehicles: {total_vehicles}")
        
        message = await channel.send(embed=embed)
        # Store the message ID for potential deletion
        user_messages[member.id] = message.id
        print(f"Debug: Posted vehicle list message for {member.name}")

# ──────────────── HELPER FUNCTIONS ────────────────

async def get_current_battle_rating():
    if db_pool is None:
        print("❌ Database connection not available")
        return None
    
    try:
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
    except Exception as e:
        print(f"❌ Database error in get_current_battle_rating: {e}")
        return None

async def get_all_vehicles_for_br(br):
    if db_pool is None:
        print("❌ Database connection not available")
        return []
    
    try:
        async with db_pool.acquire() as conn:
            return await conn.fetch("""
                SELECT vt.vehicle_id, vt.vehicle_name, vt.vehicle_type, n.nation_name, n.nation_id
                FROM vehicle_table vt
                JOIN nations n ON vt.nation_id = n.nation_id
                WHERE TRIM(TRAILING '.0' FROM vt.vehicle_br::TEXT) = $1
                ORDER BY 
                    CASE WHEN n.nation_id = 11 THEN 1 ELSE 0 END,
                    n.nation_id,
                    vt.vehicle_name
            """, br)
    except Exception as e:
        print(f"❌ Database error in get_all_vehicles_for_br: {e}")
        return []

async def get_user_vehicle_ids(user_id, br):
    if db_pool is None:
        print("❌ Database connection not available")
        return set()
    
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT vt.vehicle_id
                FROM discord_data_gathered dg
                JOIN vehicle_table vt ON dg.vehicle_id = vt.vehicle_id
                WHERE dg.user_id = $1 AND TRIM(TRAILING '.0' FROM vt.vehicle_br::TEXT) = $2
            """, user_id, br)
            return {row['vehicle_id'] for row in rows}
    except Exception as e:
        print(f"❌ Database error in get_user_vehicle_ids: {e}")
        return set()

async def store_user_vehicle(user_id, vehicle_id, warthunder_user):
    if db_pool is None:
        print("❌ Database connection not available")
        return
    
    try:
        async with db_pool.acquire() as conn:
            exists = await conn.fetchval("""
                SELECT 1 FROM discord_data_gathered
                WHERE user_id = $1 AND vehicle_id = $2
            """, user_id, vehicle_id)
            if not exists:
                await conn.execute("""
                    INSERT INTO discord_data_gathered (user_id, vehicle_id, warthunder_user)
                    VALUES ($1, $2, $3)
                """, user_id, vehicle_id, warthunder_user)
                print(f"Debug: Stored vehicle {vehicle_id} for user {user_id}")
    except Exception as e:
        print(f"❌ Database error in store_user_vehicle: {e}")

# ──────────────── HELPER FUNCTION FOR POSTING USER VEHICLES ────────────────

async def post_user_vehicles_and_cleanup(member, user_id, warthunder_user, br):
    """Post user's vehicles to the monitored channel and clean up old messages"""
    channel = bot.get_channel(TEXT_CHANNEL_ID)
    if not channel:
        return
    
    # Delete any existing message for this user
    if member.id in user_messages:
        try:
            old_message = await channel.fetch_message(user_messages[member.id])
            await old_message.delete()
            print(f"Debug: Deleted old message for {member.name} after /sqb_queue completion")
        except discord.NotFound:
            print(f"Debug: Old message not found for {member.name}, already deleted")
        except Exception as e:
            print(f"Debug: Error deleting old message for {member.name}: {e}")
    
    # Get user's current vehicles
    if db_pool is None:
        return
    
    try:
        async with db_pool.acquire() as conn:
            vehicles = await conn.fetch("""
                SELECT vt.vehicle_name, vt.vehicle_type, n.nation_name
                FROM discord_data_gathered dg
                JOIN vehicle_table vt ON vt.vehicle_id = dg.vehicle_id
                JOIN nations n ON vt.nation_id = n.nation_id
                WHERE dg.user_id = $1 AND TRIM(TRAILING '.0' FROM vt.vehicle_br::TEXT) = $2
                AND vt.vehicle_name NOT ILIKE '%no vehicle%'
                AND vt.vehicle_name NOT ILIKE '%n/a%'
                ORDER BY 
                    CASE WHEN n.nation_id = 11 THEN 1 ELSE 0 END,
                    n.nation_id,
                    vt.vehicle_type,
                    vt.vehicle_name
            """, user_id, br)
    except Exception as e:
        print(f"❌ Database error in post_user_vehicles_and_cleanup: {e}")
        return

    # Get squadron data for the user
    squadron_data = await get_squadron_data_for_user(member, warthunder_user)

    if not vehicles:
        embed = discord.Embed(
            title="⚠️ No Vehicles Set",
            description=f"<@{member.id}> You have no vehicles listed for **BR {br}**.",
            color=0xFF6B6B
        )
        embed.add_field(
            name="💡 How to fix this",
            value="Use `/sqb_queue` to select your vehicles",
            inline=False
        )
        
        # Add squadron info if available
        if squadron_data:
            embed.add_field(
                name=f"🏆 {squadron_data['squadron']} Stats",
                value=f"**Points:** {squadron_data['points']}\n**Activity:** {squadron_data['activity']}",
                inline=False
            )
        
        message = await channel.send(embed=embed)
        user_messages[member.id] = message.id
    else:
        # Group vehicles by type for better organization
        vehicles_by_type = {}
        for v in vehicles:
            vtype = v['vehicle_type'].lower()
            # Categorize vehicle types
            if vtype in ['tank', 'ground', 'medium tank', 'heavy tank', 'light tank', 'tank destroyer']:
                category = 'Ground'
                emoji = '🛡️'
            elif vtype in ['spaa', 'anti-aircraft']:
                category = 'SPAA'
                emoji = '🎯'
            elif vtype in ['aircraft', 'air', 'fighter', 'bomber', 'attacker']:
                category = 'Aircraft'
                emoji = '✈️'
            elif vtype in ['helicopter', 'heli']:
                category = 'Helicopters'
                emoji = '🚁'
            else:
                category = 'Other'
                emoji = '❓'
            
            if category not in vehicles_by_type:
                vehicles_by_type[category] = {'emoji': emoji, 'vehicles': []}
            
            vehicles_by_type[category]['vehicles'].append(f"{v['vehicle_name']} ({v['nation_name']})")

        embed = discord.Embed(
            title=f"🎮 {warthunder_user} updated their vehicles",
            description=f"**Battle Rating:** {br}",
            color=0x2196F3  # Blue color to differentiate from join messages
        )

        # Add fields for each vehicle type (only if vehicles exist)
        for category, data in vehicles_by_type.items():
            if data['vehicles']:  # Only show categories that have vehicles
                vehicle_list = "\n".join([f"• {vehicle}" for vehicle in data['vehicles']])
                embed.add_field(
                    name=f"{data['emoji']} {category} ({len(data['vehicles'])})",
                    value=vehicle_list,
                    inline=True
                )

        # Add squadron info if available
        if squadron_data:
            embed.add_field(
                name=f"🏆 {squadron_data['squadron']} Stats",
                value=f"**Points:** {squadron_data['points']}\n**Activity:** {squadron_data['activity']}",
                inline=False
            )

        # Add footer with total count
        total_vehicles = sum(len(data['vehicles']) for data in vehicles_by_type.values())
        embed.set_footer(text=f"Total vehicles: {total_vehicles}")
        
        message = await channel.send(embed=embed)
        user_messages[member.id] = message.id

# ──────────────── UTILITY FUNCTIONS ────────────────

def clean_player_name(player_name):
    """Remove platform suffixes from player names"""
    if not player_name:
        return player_name
    
    # List of platform suffixes to remove
    suffixes_to_remove = ['@live', '@psn', '@xbox', '@steam', '@epic']
    
    cleaned_name = player_name
    for suffix in suffixes_to_remove:
        if cleaned_name.lower().endswith(suffix.lower()):
            # Remove the suffix (case insensitive)
            cleaned_name = cleaned_name[:-len(suffix)]
            break
    
    return cleaned_name.strip()

# ──────────────── UI CLASSES ────────────────

def format_vehicle_label(vehicle_name, nation_name):
    """Format a vehicle label ensuring it meets Discord's requirements."""
    # Create the base label
    label = f"{vehicle_name.strip()} ({nation_name.strip()})"
    
    # Ensure minimum length of 10 characters
    if len(label) < 10:
        # Pad with dots to reach minimum length
        label += "." * (10 - len(label))
    
    # Ensure maximum length of 100 characters
    if len(label) > 100:
        # Truncate and add ellipsis
        label = label[:97] + "..."
    
    return label

class VehicleSelect(discord.ui.Select):
    def __init__(self, vehicle_options, user_id, warthunder_user, is_air, next_callback=None, selected_ids=None):
        options = []
        
        # Only process vehicles if we have any
        if vehicle_options:
            for v in vehicle_options[:25]:  # Discord limit is 25 options
                label = format_vehicle_label(v['vehicle_name'], v['nation_name'])
                
                options.append(discord.SelectOption(
                    label=label,
                    value=str(v['vehicle_id']),
                    default=(str(v['vehicle_id']) in selected_ids) if selected_ids else False
                ))

        # If no options were created, add a placeholder
        if not options:
            options = [discord.SelectOption(label="No vehicles available", value="none")]
            is_disabled = True
        else:
            is_disabled = False

        super().__init__(
            placeholder="Select vehicles..." if not is_disabled else "No vehicles available",
            min_values=0,
            max_values=min(10, len(options)) if not is_disabled else 1,
            options=options,
            disabled=is_disabled
        )

        self.user_id = user_id
        self.warthunder_user = warthunder_user
        self.is_air = is_air
        self.next_callback = next_callback

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=False, ephemeral=True)

        print(f"Debug: VehicleSelect callback - disabled: {self.disabled}, values: {self.values}")

        if self.disabled or not self.values or "none" in self.values:
            print("Debug: Skipping to next callback (no vehicles or disabled)")
            if self.next_callback:
                await self.next_callback()
            else:
                await interaction.followup.send("✅ Vehicle selection complete.", ephemeral=True)
            return

        selected_ids = {int(vid) for vid in self.values}
        print(f"Debug: Selected vehicle IDs: {selected_ids}")

        async with db_pool.acquire() as conn:
            br_row = await conn.fetchrow("""
                SELECT sqb_br FROM sqb_schedule
                WHERE NOW() BETWEEN sqb_date AND end_date
                LIMIT 1
            """)
            if not br_row:
                await interaction.followup.send("❌ Failed to fetch BR.", ephemeral=True)
                return
            br = str(br_row['sqb_br']).rstrip('.0')

            # Get the vehicle IDs that are currently being shown in this selection menu
            current_menu_vehicle_ids = set()
            async with db_pool.acquire() as conn2:
                # Get all vehicles for this BR and determine which ones are in the current menu
                all_vehicles = await conn2.fetch("""
                    SELECT vt.vehicle_id, vt.vehicle_name, vt.vehicle_type, n.nation_name
                    FROM vehicle_table vt
                    JOIN nations n ON vt.nation_id = n.nation_id
                    WHERE TRIM(TRAILING '.0' FROM vt.vehicle_br::TEXT) = $1
                """, br)
                
                # Get current vehicle type from the first option in our menu
                if hasattr(self, 'options') and self.options and self.options[0].value != "none":
                    first_vehicle_id = int(self.options[0].value)
                    for v in all_vehicles:
                        if v['vehicle_id'] == first_vehicle_id:
                            current_vehicle_type = v['vehicle_type'].lower()
                            break
                    
                    # Collect all vehicle IDs of the same type as what's being shown
                    for v in all_vehicles:
                        vtype = v['vehicle_type'].lower()
                        # Use the same categorization logic as in sqb_queue
                        categorized_type = None
                        if vtype in ['tank', 'ground', 'medium tank', 'heavy tank', 'light tank', 'tank destroyer']:
                            categorized_type = 'ground'
                        elif vtype in ['spaa', 'anti-aircraft']:
                            categorized_type = 'spaa'
                        elif vtype in ['aircraft', 'air', 'fighter', 'bomber', 'attacker']:
                            categorized_type = 'air'
                        elif vtype in ['helicopter', 'heli']:
                            categorized_type = 'heli'
                        else:
                            categorized_type = 'ground'  # default
                        
                        current_categorized_type = None
                        if current_vehicle_type in ['tank', 'ground', 'medium tank', 'heavy tank', 'light tank', 'tank destroyer']:
                            current_categorized_type = 'ground'
                        elif current_vehicle_type in ['spaa', 'anti-aircraft']:
                            current_categorized_type = 'spaa'
                        elif current_vehicle_type in ['aircraft', 'air', 'fighter', 'bomber', 'attacker']:
                            current_categorized_type = 'air'
                        elif current_vehicle_type in ['helicopter', 'heli']:
                            current_categorized_type = 'heli'
                        else:
                            current_categorized_type = 'ground'  # default
                        
                        if categorized_type == current_categorized_type:
                            current_menu_vehicle_ids.add(v['vehicle_id'])

            # Only get existing vehicles that are of the same type as the current menu
            existing = await conn.fetch("""
                SELECT dg.vehicle_id FROM discord_data_gathered dg
                JOIN vehicle_table vt ON dg.vehicle_id = vt.vehicle_id
                WHERE dg.user_id = $1 AND TRIM(TRAILING '.0' FROM vt.vehicle_br::TEXT) = $2
                AND dg.vehicle_id = ANY($3::int[])
            """, self.user_id, br, list(current_menu_vehicle_ids))

            existing_ids = {row['vehicle_id'] for row in existing}
            print(f"Debug: Existing vehicle IDs for current type: {existing_ids}")
            print(f"Debug: Current menu vehicle IDs: {current_menu_vehicle_ids}")

            # Add new selections
            for vid in selected_ids - existing_ids:
                await store_user_vehicle(self.user_id, vid, self.warthunder_user)

            # Remove unselected vehicles (only from the current type being shown)
            for vid in existing_ids - selected_ids:
                await conn.execute("""
                    DELETE FROM discord_data_gathered WHERE user_id = $1 AND vehicle_id = $2
                """, self.user_id, vid)
                print(f"Debug: Removed vehicle {vid} for user {self.user_id}")

        if self.next_callback:
            await self.next_callback()
        else:
            # Only post user vehicles if this is the final callback and user is in voice channel
            member = None
            try:
                # Try to get the member object from the interaction
                if hasattr(self, 'member_ref'):
                    member = self.member_ref
                else:
                    # Fallback: try to find member by user_id
                    for guild in bot.guilds:
                        for m in guild.members:
                            if f"{m.name}#{m.discriminator}" == self.user_id:
                                member = m
                                break
                        if member:
                            break
                
                if member and member.voice and member.voice.channel and member.voice.channel.id in MONITORED_VOICE_CHANNELS:
                    # Get current BR
                    br_row = None
                    if db_pool:
                        async with db_pool.acquire() as conn:
                            br_row = await conn.fetchrow("""
                                SELECT sqb_br FROM sqb_schedule
                                WHERE NOW() BETWEEN sqb_date AND end_date
                                LIMIT 1
                            """)
                    
                    if br_row:
                        br = str(br_row['sqb_br']).rstrip('.0')
                        await post_user_vehicles_and_cleanup(member, self.user_id, self.warthunder_user, br)
                    else:
                        await interaction.followup.send("✅ Vehicle selection saved.", ephemeral=True)
                else:
                    await interaction.followup.send("✅ Vehicle selection saved.", ephemeral=True)
            except Exception as e:
                print(f"Debug: Error in final callback: {e}")
                await interaction.followup.send("✅ Vehicle selection saved.", ephemeral=True)

class VehicleSelectionView(discord.ui.View):
    def __init__(self, vehicle_rows, user_id, warthunder_user, is_air=True, next_callback=None, selected_ids=None):
        super().__init__(timeout=120)
        self.add_item(VehicleSelect(vehicle_rows, user_id, warthunder_user, is_air, next_callback, selected_ids))
        
        # Add a Next button if there's a next callback
        if next_callback:
            self.add_item(NextButton(next_callback))

class NextButton(discord.ui.Button):
    def __init__(self, next_callback):
        super().__init__(style=discord.ButtonStyle.primary, label="Next →", emoji="➡️")
        self.next_callback = next_callback

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=False, ephemeral=True)
        
        # Just proceed to the next selection without making any changes to the current selection
        if self.next_callback:
            await self.next_callback()
        else:
            await interaction.followup.send("✅ Vehicle selection complete.", ephemeral=True)

# ──────────────── RUN ────────────────

bot.run(token)
