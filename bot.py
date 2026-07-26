import discord
import json
import os
import re
import csv
import asyncio
from io import StringIO
from datetime import datetime
from discord import app_commands, Embed
from discord.ext import commands
from dotenv import load_dotenv
from pathlib import Path

# FIX #1: Load environment variables FIRST (critical!)
load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    print('ERROR: DISCORD_TOKEN environment variable not set')
    print('Create a .env file with: DISCORD_TOKEN=your_token_here')
    exit(1)

# Prevent bot from starting in GitHub Actions
if os.getenv('GITHUB_ACTIONS') == 'true':
    print('Running in GitHub Actions - Bot startup skipped')
    print('Run bot.py on your server separately')
    exit(0)

# ============================================
# Configuration - Data Sources
# ============================================

# Termux Specific Configuration - Auto-detect home directory
HOME_DIR = os.path.expanduser("~")
print(f"🏠 Home directory detected: {HOME_DIR}")

# Local CSV path for Termux (most reliable - no internet needed)
CSV_LOCAL_PATH = os.path.join(HOME_DIR, "Doraemon-CSV-DB", "database") + "/"
print(f"📂 CSV local path: {CSV_LOCAL_PATH}")

# Option A: Live fetch (fallback if local fails)
csv_raw_base_url = 'https://raw.githubusercontent.com/star884/Doraemon-CSV-DB/main/database/'

current_source = {
    'id': 1,
    'name': 'JSON Database (Primary)',
    'url': 'https://github.com/star884/Build-Doraemon-Databasev3',
    'type': 'json',
    'files': ['database/search_index.json']
}

# Global data storage with thread-safe lock
csv_stories = []
csv_data_loaded = False
data_load_lock = asyncio.Lock()  # FIX #2: Lock prevents race conditions

available_sources = [
    {
        'id': 1,
        'name': 'JSON Database (Primary)',
        'url': 'https://github.com/star884/Build-Doraemon-Databasev3',
        'type': 'json',
        'files': ['database/search_index.json']
    },
    {
        'id': 2,
        'name': 'CSV Database (Local Clone - Termux)',
        'url': CSV_LOCAL_PATH,
        'type': 'csv',
        'files': ['story_index.csv', 'manga_details.csv'],
        'fetch_method': 'local_file'
    }
]

EPISODES = []
DB = {}

bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())

# ============================================
# Data Loading Functions
# ============================================
def load_json_database():
    """Load data from local JSON database"""
    global EPISODES, DB
    try:
        db_path = 'database/search_index.json'
        
        with open(db_path, encoding='utf-8') as f:
            DB = json.load(f)
        EPISODES = DB.get('items', [])
        print(f'✅ Loaded {len(EPISODES)} episodes from JSON database')
        print(f'   Episodes with JP refs: {sum(1 for e in EPISODES if e.get("jp_story_numbers"))}')
        return True
    except FileNotFoundError:
        EPISODES = []
        print('⚠️ JSON database file not found at database/search_index.json')
        return False
    except json.JSONDecodeError as e:
        EPISODES = []
        print(f'❌ Invalid JSON in database file: {e}')
        return False
    except Exception as e:
        EPISODES = []
        print(f'❌ Error loading JSON database: {e}')
        return False

def load_csv_database_local(base_path):
    """FIX #3: Load CSV data from local files (no internet required - Termux optimized)"""
    global csv_stories, csv_data_loaded
    
    try:
        print(f'📥 Loading CSV data from local path: {base_path}')
        
        # Verify directory exists
        if not os.path.isdir(base_path):
            print(f'❌ Directory not found: {base_path}')
            print('💡 Run: git clone https://github.com/star884/Doraemon-CSV-DB.git ~/Doraemon-CSV-DB')
            csv_stories = []
            csv_data_loaded = False
            return False
        
        story_file = os.path.join(base_path, 'story_index.csv')
        
        if not os.path.isfile(story_file):
            print(f'❌ Story index not found at {story_file}')
            csv_stories = []
            csv_data_loaded = False
            return False
        
        # Read CSV file directly from disk
        with open(story_file, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            csv_stories = list(reader)
        
        csv_data_loaded = True
        print(f'✅ Loaded {len(csv_stories)} stories from local CSV (offline mode)')
        return True
        
    except PermissionError:
        print(f'❌ Permission denied accessing: {base_path}')
        csv_stories = []
        csv_data_loaded = False
        return False
    except Exception as e:
        print(f'❌ Error loading local CSV: {type(e).__name__}: {e}')
        csv_stories = []
        csv_data_loaded = False
        return False

async def load_csv_database_live(source_url):
    """Fetch CSV data LIVE from GitHub Raw URLs (fallback option)"""
    global csv_stories, csv_data_loaded
    
    # FIX #4: Check for aiohttp dependency before use
    try:
        import aiohttp
    except ImportError:
        print('❌ ERROR: aiohttp not installed. Run: pip install aiohttp')
        csv_stories = []
        csv_data_loaded = False
        return False
    
    async with data_load_lock:
        try:
            print('🌐 Fetching CSV data from GitHub...')
            
            story_url = csv_raw_base_url + 'story_index.csv'
            
            async with aiohttp.ClientSession() as session:
                async with session.get(story_url, timeout=30) as resp:
                    if resp.status == 200:
                        content = await resp.text(encoding='utf-8')
                        
                        reader = csv.DictReader(StringIO(content))
                        csv_stories = list(reader)
                        
                        csv_data_loaded = True
                        print(f'✅ Loaded {len(csv_stories)} stories from CSV (online mode)')
                        return True
                    else:
                        print(f'❌ Failed to fetch CSV: HTTP {resp.status}')
                        csv_stories = []
                        csv_data_loaded = False
                        return False
                        
        except asyncio.TimeoutError:
            print('⏱️ Timeout fetching CSV from GitHub')
            csv_stories = []
            csv_data_loaded = False
            return False
        except Exception as e:
            print(f'❌ Error fetching CSV: {type(e).__name__}: {e}')
            csv_stories = []
            csv_data_loaded = False
            return False

def convert_csv_to_episode_format(csv_stories):
    """Map CSV data to standard EPISODES format for compatibility"""
    episodes = []
    for story in csv_stories:
        ep = {
            'in_season_episode': story.get('1979 Anime Episode', story.get('2005 Anime Episode', 'N/A')),
            'alt_in_episode': story.get('Alt Episode', ''),
            'jp_story_numbers': story.get('Japanese Story #', ''),
            'jp_story_all': story.get('Japanese Story #', ''),
            'story_a': story.get('English title', story.get('Japanese title', '')),
            'story_b': '',
            'category': 'manga_stories',
            'title': story.get('English title', ''),
            'jp_parsed': [{
                'number': str(story.get('Japanese Story #', '')),
                'is_special': False,
                'is_standalone_special': False
            }] if story.get('Japanese Story #') else [],
            'English title': story.get('English title', ''),
            'Japanese title': story.get('Japanese title', ''),
            'Publication date': story.get('Publication date', ''),
            'Magazine code': story.get('Magazine code', ''),
            '1979 Anime Episode': story.get('1979 Anime Episode', ''),
            '2005 Anime Episode': story.get('2005 Anime Episode', ''),
            'jp_has_specials': False,
            'jp_has_standalone_specials': False
        }
        episodes.append(ep)
    return episodes

async def load_csv_database(source_config):
    """Unified loader - supports both local and remote CSV"""
    global EPISODES, csv_stories, csv_data_loaded
    
    fetch_method = source_config.get('fetch_method', 'github_api')
    source_url = source_config['url']
    
    if fetch_method == 'local_file':
        success = load_csv_database_local(source_url)
        # Optional: fallback to remote if local fails
        if not success:
            print('💡 Local load failed, attempting remote fallback...')
            success = await load_csv_database_live(csv_raw_base_url)
    else:
        success = await load_csv_database_live(source_url)
    
    if success:
        EPISODES = convert_csv_to_episode_format(csv_stories)
        print(f'✅ CSV database ready with {len(EPISODES)} stories')
        return True
    
    return False

def check_csv_source():
    """Check if CSV source is available"""
    if not csv_data_loaded or len(csv_stories) == 0:
        embed = Embed(
            title='❌ CSV Not Available',
            description=(
                'CSV database is not loaded.\n\n'
                '**Possible causes:**\n'
                '- Directory not found: `~/Doraemon-CSV-DB/database/`\n'
                '- No internet connection (for online mode)\n'
                '- GitHub temporarily unavailable\n\n'
                f'**Current source:** **{current_source["name"]}**\n\n'
                '**Fix:** Clone the repo:\n```\ngit clone https://github.com/star884/Doraemon-CSV-DB.git ~/Doraemon-CSV-DB\n```'
            ),
            color=0xcc0000
        )
        return False, embed
    return True, None

# ============================================
# SOURCE MANAGEMENT COMMANDS
# ============================================
@bot.tree.command(name='source', description='Shows the current database source')
async def source_cmd(interaction):
    embed = Embed(title=f'📊 Current Database Source', color=0x6d4aff)
    embed.add_field(name='Source Name', value=current_source['name'], inline=True)
    embed.add_field(name='Type', value=current_source['type'].title(), inline=True)
    
    if 'http' in current_source['url'] or current_source['url'].startswith('/'):
        embed.add_field(name='URL/Path', value=f'{current_source["url"][:50]}...', inline=False)
    
    if current_source['type'] == 'csv':
        status = '✅ Loaded' if csv_data_loaded else '⏳ Not loaded'
        embed.add_field(name='Status', value=status, inline=True)
        embed.add_field(name='Records', value=str(len(csv_stories)), inline=True)
    elif current_source['type'] == 'json':
        embed.add_field(name='Total Episodes', value=str(len(EPISODES)), inline=True)
    
    embed.set_footer(text=f'Source ID: {current_source["id"]} | Switch with /source change <id>')
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='source_all', description='Lists all available database sources')
async def source_all_cmd(interaction):
    embed = Embed(title='📚 Available Database Sources', color=0x6d4aff)
    
    description = ''
    for src in available_sources:
        desc = f'**{src["id"]}) {src["name"]}**\n'
        desc += f'> Type: {src["type"].title()} | Method: {src.get("fetch_method", "default").title()}\n'
        if src.get('files'):
            desc += f'> Files: {", ".join(src["files"])}\n'
        if src['id'] == current_source['id']:
            desc += '> 🟢 **Currently Active**\n'
        description += desc + '\n'
    
    embed.description = description
    embed.add_field(name='How to switch', 
                   value='Use `/source change <number>` to switch\nExample: `/source change 2`',
                   inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='source_change', description='Changes the active database source')
@app_commands.describe(source_id='Source number to switch to (1=JSON, 2=CSV)')
async def source_change_cmd(interaction, source_id: int):
    source = next((s for s in available_sources if s['id'] == source_id), None)
    
    if not source:
        embed = Embed(title='❌ Invalid Source', 
                     description=f'Source ID {source_id} not found.\nUse `/source all` to see available sources.',
                     color=0xcc0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    old_source = current_source.copy()
    
    try:
        if source['type'] == 'csv':
            success = await load_csv_database(source)
            if not success:
                embed = Embed(
                    title='⚠️ Warning',
                    description=(
                        'Could not load CSV data.\n\n'
                        '**Troubleshooting:**\n'
                        '- Verify folder exists: `~/Doraemon-CSV-DB/database/`\n'
                        '- Run: `git clone https://github.com/star884/Doraemon-CSV-DB.git ~/Doraemon-CSV-DB`\n'
                        '- Then switch back: `/source change 1`\n\n'
                        f'Reverting to previous source.'
                    ),
                    color=0xffaa00
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                current_source.update(old_source)
                return
            elif EPISODES:
                current_source.update(source)
        elif source['type'] == 'json':
            success = load_json_database()
            if not success:
                embed = Embed(title='⚠️ Warning',
                             description='Could not load JSON data.\nReverting to previous source.',
                             color=0xffaa00)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                current_source.update(old_source)
                return
            else:
                current_source.update(source)
        
        record_count = len(csv_stories) if source['type'] == 'csv' else len(EPISODES)
        embed = Embed(title='✅ Source Changed Successfully', color=0x6d4aff)
        embed.add_field(name='Previous Source', value=old_source['name'], inline=False)
        embed.add_field(name='New Source', value=source['name'], inline=False)
        embed.add_field(name='Records Loaded', value=str(record_count), inline=True)
        embed.set_footer(text='Use /source to view current')
        await interaction.response.send_message(embed=embed)
        
    except Exception as e:
        print(f'❌ Error during source change: {e}')
        current_source.update(old_source)
        embed = Embed(title='❌ Error', 
                     description=f'An error occurred while switching sources.\nError: {str(e)[:100]}',
                     color=0xcc0000)
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================
# SEARCH COMMANDS
# ============================================
@bot.tree.command(name='search', description='Advanced search by JP story #, IN episode, SPE, or CE')
@app_commands.describe(query='Search term (e.g., 150, 150 S, S16, s04e35, spe01, ce1, nobita)')
async def search_cmd(interaction, query: str):
    if not EPISODES:
        embed = Embed(
            title='⚠️ Database Empty',
            description='No episodes loaded. Use `/source` to check status.',
            color=0xcc0000
        )
        await interaction.response.send_message(embed=embed)
        return
        
    q = query.lower().strip()
    results = []
    search_type = None
    
    if q.startswith('jp:') or q.isdigit() or re.match(r'^s\d+\s*s?$', q, re.I):
        search_type = 'jp'
        jp_query = q.replace('jp:', '').strip()
        
        standalone_match = re.match(r'^s(\d+)$', jp_query, re.I)
        reg_special_match = re.match(r'^(\d+)\s*s$', jp_query, re.I)
        
        for ep in EPISODES:
            jp_parsed = ep.get('jp_parsed', [])
            for jp in jp_parsed:
                if standalone_match:
                    if jp.get('is_standalone_special') and str(jp.get('number')) == standalone_match.group(1):
                        results.append(ep)
                        break
                elif reg_special_match:
                    target_num = reg_special_match.group(1)
                    if (not jp.get('is_standalone_special') and
                            jp.get('is_special') and
                            str(jp.get('number')) == target_num):
                        results.append(ep)
                        break
                elif jp_query.isdigit():
                    if str(jp.get('number')) == jp_query:
                        results.append(ep)
                        break
    
    elif re.match(r'^s\d{2}e\d{2}$', q):
        search_type = 'in_episode'
        results = [e for e in EPISODES if e.get('in_season_episode', '').lower() == q]
    
    elif q.startswith('spe'):
        search_type = 'special'
        spe_num = re.sub(r'[^0-9]', '', q.replace('spe', ''))
        results = [e for e in EPISODES if e.get('in_season_episode', '').upper().startswith('SPE') and spe_num in e.get('in_season_episode', '')]
    
    elif q.startswith('ce') or q.startswith('classic'):
        search_type = 'classic'
        ce_num = re.sub(r'[^0-9]', '', q.replace('ce', '').replace('classic', ''))
        results = [e for e in EPISODES if e.get('in_season_episode', '').upper().startswith('CE') and ce_num in e.get('in_season_episode', '')]
    
    if not results:
        search_type = 'title'
        results = []
        for ep in EPISODES:
            story_a = ep.get('story_a', '').lower()
            story_b = ep.get('story_b', '').lower() if ep.get('story_b') else ''
            if q in story_a or q in story_b:
                results.append(ep)
        results = results[:5]
    
    if not results:
        embed = Embed(
            title='❌ No Results Found',
            description=f'No episodes found for "**{query}"\n\n'
            '**Supported formats:**\n'
            '- JP Story #: `150` or `jp:150`\n'
            '- JP Special (S suffix): `150 S`\n'
            '- JP Standalone Special: `S16`\n'
            '- IN Episode: `s04e35`\n'
            '- Special: `spe01` or `spe1`\n'
            '- Classic: `ce1` or `classic1`\n'
            '- Title: `nobita`\n\n'
            f'**Current Source:** {current_source["name"]}',
            color=0xcc0000
        )
        await interaction.response.send_message(embed=embed)
        return
    
    first = results[0]
    in_ep = first.get('in_season_episode', 'N/A')
    alt_ep = first.get('alt_in_episode')
    jp_display = first.get('jp_story_all', 'Not indexed')
    story_a = first.get('story_a', 'Unknown')
    story_b = first.get('story_b')
    category = first.get('category', 'unknown').replace('_', ' ').title()
    title = first.get('title', 'Unknown')[:70]
    
    embed = Embed(title=f'✅ Episode Found: {title}', color=0x6d4aff)
    embed.description = f'**Category:** {category}'
    embed.add_field(name='Indian Episode', value=in_ep, inline=True)
    if alt_ep:
        embed.add_field(name='Alt IN Number', value=alt_ep, inline=True)
    embed.add_field(name='Japanese Story #', value=jp_display, inline=True)
    embed.add_field(name='Story A', value=story_a[:200], inline=False)
    if story_b:
        embed.add_field(name='Story B', value=story_b[:200], inline=False)
    
    footer = f'Searched by: {search_type} | Matches: {len(results)} | Source: {current_source["name"]}'
    embed.set_footer(text=footer)
    
    if len(results) > 1:
        additional = [f'- {r.get("in_season_episode", "?")}: {r.get("story_a", "?")[:40]}' for r in results[1:5]]
        embed.add_field(name=f'More Matches ({len(results)-1})', value='\n'.join(additional), inline=False)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='jp', description='Lookup by Japanese Story Number')
@app_commands.describe(jp_number='Japanese Story Number (e.g., 150, 150 S, or S16)')
async def jp_cmd(interaction, jp_number: str):
    if not EPISODES:
        embed = Embed(
            title='⚠️ Database Empty',
            description='No episodes loaded. Use `/source` to check status.',
            color=0xcc0000
        )
        await interaction.response.send_message(embed=embed)
        return
        
    results = []
    q = jp_number.strip()
    
    standalone_match = re.match(r'^s(\d+)$', q, re.I)
    reg_special_match = re.match(r'^(\d+)\s*s$', q, re.I)
    
    for ep in EPISODES:
        for jp in ep.get('jp_parsed', []):
            if standalone_match:
                if jp.get('is_standalone_special') and str(jp.get('number')) == standalone_match.group(1):
                    results.append(ep)
                    break
            elif reg_special_match:
                target_num = reg_special_match.group(1)
                if (not jp.get('is_standalone_special') and
                        jp.get('is_special') and
                        str(jp.get('number')) == target_num):
                    results.append(ep)
                    break
            elif q.isdigit():
                if str(jp.get('number')) == q:
                    results.append(ep)
                    break
    
    if not results:
        embed = Embed(
            title='❌ Not Found',
            description=f'No episode found for JP Story #{jp_number}\n\n'
            f'**Current Source:** {current_source["name"]}',
            color=0xcc0000
        )
        await interaction.response.send_message(embed=embed)
        return
    
    first = results[0]
    embed = Embed(title=f'📘 JP Story #{jp_number}', color=0x6d4aff)
    embed.add_field(name='IN Episode', value=first.get('in_season_episode', 'N/A'), inline=True)
    embed.add_field(name='Category', value=first.get('category', 'unknown').replace('_', ' ').title(), inline=True)
    if first.get('alt_in_episode'):
        embed.add_field(name='Alt IN', value=first.get('alt_in_episode'), inline=True)
    embed.add_field(name='Story A', value=first.get('story_a', 'Unknown')[:200], inline=False)
    if first.get('story_b'):
        embed.add_field(name='Story B', value=first.get('story_b')[:200], inline=False)
    embed.add_field(name='All JP Stories', value=first.get('jp_story_all', '-'), inline=False)
    embed.set_footer(text=f'Matches: {len(results)} | Source: {current_source["name"]}')
    
    if len(results) > 1:
        additional = [f'- {r.get("in_season_episode", "?")}: {r.get("story_a", "?")[:40]}' for r in results[1:5]]
        embed.add_field(name=f'More Matches ({len(results)-1})', value='\n'.join(additional), inline=False)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='list', description='List episodes with pagination')
@app_commands.describe(page='Page number (1-999)', season='Filter by season (1-10) or "special" or "classic"')
async def list_cmd(interaction, page: int = 1, season: str = None):
    if not EPISODES:
        embed = Embed(
            title='⚠️ Database Empty',
            description='No episodes loaded. Use `/source` to check status.',
            color=0xcc0000
        )
        await interaction.response.send_message(embed=embed)
        return
    
    filtered = EPISODES[:]
    
    if season:
        s = season.lower()
        if s == 'special':
            filtered = [e for e in EPISODES if e.get('category') == 'special_episodes']
        elif s == 'classic':
            filtered = [e for e in EPISODES if e.get('category') == 'classic_doraemon']
        elif s.isdigit():
            filtered = [e for e in EPISODES if e.get('category') == f'season_{s}']
    
    PAGE_SIZE = 10
    
    # FIX #5: Better pagination boundary checking
    if page < 1:
        page = 1
    
    total_pages = (len(filtered) + PAGE_SIZE - 1) // PAGE_SIZE if filtered else 1
    
    if page > total_pages:
        embed = Embed(
            title='❌ Page Not Found', 
            description=f'Page {page} does not exist. Valid pages: 1-{total_pages}\n**Current Source:** {current_source["name"]}', 
            color=0xcc0000
        )
        await interaction.response.send_message(embed=embed)
        return
    
    start = (page - 1) * PAGE_SIZE
    end = min(start + PAGE_SIZE, len(filtered))
    
    chunk = filtered[start:end]
    season_display = season if season else 'All'
    embed = Embed(title=f'📋 Doraemon Episodes - Page {page} ({season_display})', color=0x6d4aff)
    embed.description = f'Total: **{len(filtered)}** | Showing {start + 1}-{end}'
    
    for r in chunk:
        in_ep = r.get('in_season_episode', 'N/A')
        jp_st = r.get('jp_story_all', '-') or '-'
        story_a = r.get('story_a', 'Unknown')[:40]
        story_b = r.get('story_b', '')
        title_display = story_a
        if story_b:
            title_display += f' / {story_b[:40]}'
        embed.add_field(name=f'{in_ep} | JP: {jp_st}', value=title_display, inline=False)
    
    embed.set_footer(text=f'Page {page} of {total_pages} | Source: {current_source["name"]}')
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='stats', description='Show database statistics')
async def stats_cmd(interaction):
    counts = {}
    with_jp = 0
    jp_specials = 0
    jp_standalone = 0
    
    for e in EPISODES:
        cat = e.get('category', 'unknown')
        counts[cat] = counts.get(cat, 0) + 1
        if e.get('jp_story_numbers'):
            with_jp += 1
        if e.get('jp_has_specials'):
            jp_specials += 1
        if e.get('jp_has_standalone_specials'):
            jp_standalone += 1
    
    embed = Embed(title='📊 Database Statistics', color=0x6d4aff)
    embed.description = (
        f'**Total Episodes:** {len(EPISODES)}\n'
        f'**JP Indexed:** {with_jp}\n'
        f'**JP Special Stories (S suffix):** {jp_specials}\n'
        f'**JP Standalone Specials (S##):** {jp_standalone}\n\n'
        f'**Source:** {current_source["name"]} ({current_source["type"].title()})\n'
        f'**URL/Path:** {current_source["url"][:60]}...\n\n'
        f'**Breakdown:**'
    )
    for cat, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        embed.add_field(name=cat.replace('_', ' ').title(), value=f'**{cnt}**', inline=True)
    
    if current_source['type'] == 'csv' and csv_data_loaded:
        embed.add_field(name='CSV Rows', value=str(len(csv_stories)), inline=True)
    
    embed.set_footer(text=f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")} | Source: {current_source["name"]}')
    
    await interaction.response.send_message(embed=embed)

# ============================================
# HELP COMMAND
# ============================================
@bot.tree.command(name='help', description='Show available commands')
async def help_cmd(interaction):
    embed = Embed(title='🤖 Doraemon Search Bot Help', color=0x6d4aff)
    
    csv_note = ''
    if current_source['type'] == 'csv' and csv_data_loaded:
        csv_note = '(✅ All CSV features enabled - offline mode)\n\n'
    
    embed.description = f'''
**Available Commands:**

**Core Commands** (Work on all sources)
- `/search <query>` - Search by JP#, IN#, SPE, CE, or title
- `/jp <number>` - Lookup by Japanese Story Number
- `/list [page] [season]` - Browse episodes with pagination
- `/stats` - Show database statistics

**Source Commands** (Manage database sources)
- `/source` - Shows current database source
- `/source all` - Lists all available sources
- `/source change <id>` - Switch database source (1=JSON, 2=CSV)

**CSV-Only Commands** (Requires CSV source)
{csv_note}- `/search_jp_title <keyword>` - Search by Japanese title
- `/search_magazine <code>` - Search by magazine code (YK, KG, G1, etc.)
- `/search_1979 <ep>` - Search by 1979 anime episode number
- `/search_2005 <ep>` - Search by 2005 anime episode number

**Info**
- `/help` - Show this message

**Current Source:** {current_source["name"]}
**Total Records:** {len(EPISODES)}
'''
    embed.set_footer(text='🎯 Bidirectional Cross-Reference - CSV Features Available')
    await interaction.response.send_message(embed=embed)

# ============================================
# CSV-SPECIFIC COMMANDS
# ============================================
@bot.tree.command(name='search_jp_title', description='Search by Japanese title (CSV only)')
@app_commands.describe(query='Japanese title or keyword')
async def search_jp_title_cmd(interaction, query: str):
    """Search stories by Japanese title - CSV source only"""
    is_available, error_embed = check_csv_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return
    
    q = query.lower()
    results = [s for s in csv_stories if q in s.get('Japanese title', '').lower() or q in s.get('English title', '').lower()]
    
    if not results:
        embed = Embed(title='❌ No Results Found',
                     description=f'No stories found for "**{query}**"',
                     color=0xcc0000)
        await interaction.response.send_message(embed=embed)
        return
    
    first = results[0]
    embed = Embed(title=f'📘 JP Title Match: {first.get("English title", "Unknown")[:70]}',
                 color=0x6d4aff)
    embed.add_field(name='Japanese Title', value=first.get('Japanese title', 'N/A'), inline=False)
    embed.add_field(name='1979 Episode', value=first.get('1979 Anime Episode', 'N/A'), inline=True)
    embed.add_field(name='2005 Episode', value=first.get('2005 Anime Episode', 'N/A'), inline=True)
    embed.add_field(name='Magazine', value=first.get('Magazine code', 'N/A'), inline=True)
    embed.add_field(name='Publication Date', value=first.get('Publication date', 'N/A'), inline=True)
    
    if len(results) > 1:
        additional = [f'- {r.get("English title", "?")[:40]}' for r in results[1:5]]
        embed.add_field(name=f'More Matches ({len(results)-1})', value='\n'.join(additional), inline=False)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='search_magazine', description='Search by magazine code (CSV only)')
@app_commands.describe(code='Magazine code (e.g., YK, KG, G1, G2, SD, TK, CC)')
async def search_magazine_cmd(interaction, code: str):
    """Search stories by magazine code - CSV source only"""
    is_available, error_embed = check_csv_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return
    
    code_upper = code.upper()
    results = [s for s in csv_stories if code_upper in s.get('Magazine code', '').upper()]
    
    if not results:
        embed = Embed(title='❌ No Results Found',
                     description=f'No stories found for magazine code "**{code.upper()}**"',
                     color=0xcc0000)
        await interaction.response.send_message(embed=embed)
        return
    
    embed = Embed(title=f'📰 Magazine: {code.upper()}', color=0x6d4aff)
    embed.description = f'Found **{len(results)}** stories'
    
    for story in results[:10]:
        title = story.get('English title', 'Unknown')[:50]
        date = story.get('Publication date', '?')
        embed.add_field(name=f'{date} - {title}', value='', inline=False)
    
    if len(results) > 10:
        embed.set_footer(text=f'Showing 10 of {len(results)} results')
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='search_1979', description='Search by 1979 anime episode number (CSV only)')
@app_commands.describe(episode='Episode number (e.g., 1020, 150, 1234)')
async def search_1979_cmd(interaction, episode: str):
    """Search stories by 1979 anime episode - CSV source only"""
    is_available, error_embed = check_csv_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return
    
    results = [s for s in csv_stories if episode in str(s.get('1979 Anime Episode', ''))]
    
    if not results:
        embed = Embed(title='❌ No Results Found',
                     description=f'No stories found for 1979 anime episode "**{episode}**"',
                     color=0xcc0000)
        await interaction.response.send_message(embed=embed)
        return
    
    embed = Embed(title=f'📺 1979 Anime Ep {episode}', color=0x6d4aff)
    embed.description = f'Found **{len(results)}** story/stories'
    
    for story in results[:10]:
        title = story.get('English title', 'Unknown')[:50]
        jp = story.get('Japanese title', '')[:30]
        desc = f'- {title}' + (f' ({jp})' if jp else '')
        embed.add_field(name=desc, value='', inline=False)
    
    if len(results) > 10:
        embed.set_footer(text=f'Showing 10 of {len(results)} results')
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='search_2005', description='Search by 2005 anime episode number (CSV only)')
@app_commands.describe(episode='Episode number (e.g., 1020, 150, 1234)')
async def search_2005_cmd(interaction, episode: str):
    """Search stories by 2005 anime episode - CSV source only"""
    is_available, error_embed = check_csv_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return
    
    results = [s for s in csv_stories if episode in str(s.get('2005 Anime Episode', ''))]
    
    if not results:
        embed = Embed(title='❌ No Results Found',
                     description=f'No stories found for 2005 anime episode "**{episode}**"',
                     color=0xcc0000)
        await interaction.response.send_message(embed=embed)
        return
    
    embed = Embed(title=f'📺 2005 Anime Ep {episode}', color=0x6d4aff)
    embed.description = f'Found **{len(results)}** story/stories'
    
    for story in results[:10]:
        title = story.get('English title', 'Unknown')[:50]
        jp = story.get('Japanese title', '')[:30]
        desc = f'- {title}' + (f' ({jp})' if jp else '')
        embed.add_field(name=desc, value='', inline=False)
    
    if len(results) > 10:
        embed.set_footer(text=f'Showing 10 of {len(results)} results')
    
    await interaction.response.send_message(embed=embed)

# ============================================
# EVENT HANDLERS
# ============================================
@bot.event
async def on_ready():
    print(f'✅ Logged in as {bot.user}')
    print('🔄 Syncing slash commands...')
    synced = await bot.tree.sync()
    print(f'✅ Synced {len(synced)} GLOBAL commands')
    
    if bot.guilds:
        print(f'🏛️ Bot is in {len(bot.guilds)} guild(s)')
    
    print('\n🗄️ Loading initial database...')
    print(f'   Default source: {current_source["name"]} ({current_source["type"]})')
    
    success = load_json_database()
    if not success:
        print('⚠️ Initial database load failed. Bot will run with empty database.')
        print('💡 Use /source change 2 to load CSV (local offline mode recommended)')
    else:
        print(f'✅ Database loaded successfully ({len(EPISODES)} records)')
    
    # Try loading CSV automatically if configured
    if len([s for s in available_sources if s['id'] == 2]) > 0:
        csv_source = available_sources[1]
        print(f'\n💾 Attempting to load CSV from local path...')
        csv_success = load_csv_database_local(CSV_LOCAL_PATH)
        if csv_success:
            print(f'✅ CSV loaded successfully ({len(csv_stories)} stories - offline mode ready)')
        else:
            print('⚠️ CSV not available (run /source change 2 when ready)')
    
    print(f'\n📊 Status:')
    print(f'   Core commands: ✅ OK')
    print(f'   Source commands: ✅ OK')
    print(f'   CSV features: {"✅ Online/Offline ready" if csv_data_loaded else "⚠️ Disabled (/source change 2)"})')
    print(f'\n✨ Bot is ready! Type `!help` in Discord for more info.')

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send('❌ Command not found. Use `/help` to see available commands.')
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send('❌ Missing required argument. Use `/help` for usage information.')
    elif isinstance(error, app_commands.errors.CommandInvokeError):
        print(f'❌ Command invoke error: {error}')
        await ctx.send('❌ An internal error occurred. Please try again.')
    elif isinstance(error, commands.CheckFailure):
        print(f'⚠️ Check failure: {error}')
    else:
        print(f'❌ Unhandled error: {type(error).__name__}: {error}')
        try:
            await ctx.send('❌ An unexpected error occurred. Please contact the bot administrator.')
        except:
            pass  # Ignore errors when sending error message

# ============================================
# MAIN EXECUTION
# ============================================
if __name__ == '__main__':
    print('='*60)
    print('🤖 Starting Doraemon Search Bot...')
    print(f'📍 Running in: {HOME_DIR}')
    print(f'🔑 Token: {"Set" if TOKEN else "NOT SET"}')
    print('='*60)
    print('Press Ctrl+C to stop')
    print()
    
    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f'❌ Fatal error starting bot: {type(e).__name__}: {e}')
        print('💡 Check your DISCORD_TOKEN and permissions.')
        exit(1)
