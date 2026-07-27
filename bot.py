import discord
import json
import os
import re
import csv
import asyncio
from datetime import datetime
from discord import app_commands, Embed
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    print('ERROR: DISCORD_TOKEN environment variable not set')
    print('Create a .env file with: DISCORD_TOKEN=your_token_here')
    exit(1)

if os.getenv('GITHUB_ACTIONS') == 'true':
    print('Running in GitHub Actions - Bot startup skipped')
    exit(0)

# ============================================
# Configuration & Constants
# ============================================

EMBED_FIELD_NAME_MAX = 256
EMBED_FIELD_VALUE_MAX = 1024
EMBED_TOTAL_MAX = 6000
EMBED_FIELD_MAX_COUNT = 25
PAGE_SIZE = 10

COLOR_PRIMARY = 0x6d4aff
COLOR_ERROR = 0xcc0000
COLOR_WARNING = 0xffaa00

ZERO_WIDTH_SPACE = '\u200b'

HOME_DIR = os.path.expanduser("~")
print(f"Home directory: {HOME_DIR}")

CSV_LOCAL_PATH = os.path.join(HOME_DIR, "Doraemon-CSV-DB", "database") + "/"
print(f"CSV local path: {CSV_LOCAL_PATH}")

CSV_RAW_BASE_URL = 'https://raw.githubusercontent.com/star884/Doraemon-CSV-DB/main/database/'

CSV_FIELD_NAMES = [
    'Magazine code', 'Publication Date', 'English title', 'Japanese title',
    'English Kindle', 'Bilingual Print', 'Tankōbon', 'Complete Collection',
    '1979 anime', '1979 anime extra', '2005 anime', '2005 anime extra 1',
    '2005 anime extra 2', 'Movie', 'Movie extra', 'Short film', 'Notes'
]

VOLUME_COLUMNS = {
    'tankobon':  'Tankōbon',
    'complete':  'Complete Collection',
    'kindle':    'English Kindle',
    'bilingual': 'Bilingual Print',
}

KNOWN_MAGAZINES = {'YK', 'KG', 'G1', 'G2', 'G3', 'G4', 'G5', 'G6', 'BK', 'SD', 'TK', 'CC', 'CD'}

bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())

EPISODES: list[dict] = []
DB: dict = {}
csv_stories: list[dict] = []
csv_data_loaded: bool = False
data_load_lock = asyncio.Lock()

current_source: dict = {
    'id': 1,
    'name': 'JSON Database (Primary)',
    'url': 'https://github.com/star884/Build-Doraemon-Databasev3',
    'type': 'json',
    'files': ['database/search_index.json']
}

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

# ============================================
# Helper Functions
# ============================================

def truncate(text: str, max_len: int) -> str:
    """Safely truncate text to fit Discord limits."""
    if text and len(text) > max_len:
        return text[:max_len - 1] + '…'
    return text or ZERO_WIDTH_SPACE

def safe_add_field(embed: Embed, *, name: str, value: str = '', inline: bool = False) -> None:
    """Add a field to an embed, enforcing Discord limits and preventing empty values."""
    name = truncate(name or ZERO_WIDTH_SPACE, EMBED_FIELD_NAME_MAX)
    value = value if value else ZERO_WIDTH_SPACE
    value = truncate(value, EMBED_FIELD_VALUE_MAX)
    embed.add_field(name=name, value=value, inline=inline)

def word_boundary_match(query: str, text: str) -> bool:
    """Match query as a whole word/number within text, preventing substring false positives."""
    pattern = r'\b' + re.escape(query.lower()) + r'\b'
    return re.search(pattern, text.lower()) is not None

def build_csv_result_fields(embed: Embed, story: dict) -> None:
    """Populate an embed with standard CSV story fields."""
    mag = story.get('Magazine code', '') or 'N/A'
    pub_date = story.get('Publication Date', '') or 'N/A'
    jp_title = story.get('Japanese title', '') or 'N/A'
    anime_1979 = story.get('1979 anime', '') or 'None'
    anime_2005 = story.get('2005 anime', '') or 'None'
    tankobon = story.get('Tankōbon', '') or 'None'
    complete = story.get('Complete Collection', '') or 'None'
    kindle = story.get('English Kindle', '') or 'None'
    movie = story.get('Movie', '') or ''
    short_film = story.get('Short film', '') or ''
    notes = story.get('Notes', '') or ''

    safe_add_field(embed, name='Magazine Code', value=mag, inline=True)
    safe_add_field(embed, name='Publication Date', value=pub_date, inline=True)
    safe_add_field(embed, name='Japanese Title', value=truncate(jp_title, EMBED_FIELD_VALUE_MAX), inline=False)
    safe_add_field(embed, name='1979 Anime', value=truncate(anime_1979, EMBED_FIELD_VALUE_MAX), inline=True)
    safe_add_field(embed, name='2005 Anime', value=truncate(anime_2005, EMBED_FIELD_VALUE_MAX), inline=True)
    safe_add_field(embed, name='Tankōbon', value=tankobon, inline=True)
    safe_add_field(embed, name='Complete Collection', value=complete, inline=True)
    safe_add_field(embed, name='English Kindle', value=kindle, inline=True)

    extras = []
    if movie:
        extras.append(f'**Movie:** {movie}')
    if short_film:
        extras.append(f'**Short Film:** {short_film}')
    if notes:
        extras.append(f'**Notes:** {truncate(notes, 200)}')
    if extras:
        safe_add_field(embed, name='Additional Info', value='\n'.join(extras), inline=False)

def build_more_matches_field(embed: Embed, results: list[dict], is_csv: bool, max_shown: int = 5) -> None:
    """Add a 'More Matches' field listing additional results."""
    count = min(len(results) - 1, max_shown)
    if count <= 0:
        return

    lines = []
    for r in results[1:max_shown + 1]:
        if is_csv:
            t = r.get('English title', r.get('story_a', '?'))[:40]
            m = r.get('Magazine code', '?')
            d = r.get('Publication Date', '?')
            lines.append(f'- [{m} {d}] {t}')
        else:
            ep = r.get('in_season_episode', '?')
            title = r.get('story_a', '?')[:40]
            lines.append(f'- {ep}: {title}')

    safe_add_field(embed, name=f'More Matches ({len(results) - 1})', value='\n'.join(lines), inline=False)

def is_csv_source() -> bool:
    """Shortcut to check if current source is CSV."""
    return current_source['type'] == 'csv'

# ============================================
# Data Loading Functions
# ============================================

def load_json_database() -> bool:
    """Load data from local JSON database."""
    global EPISODES, DB
    try:
        db_path = 'database/search_index.json'
        with open(db_path, encoding='utf-8') as f:
            DB = json.load(f)
        EPISODES = DB.get('items', [])
        print(f'Loaded {len(EPISODES)} episodes from JSON database')
        return True
    except FileNotFoundError:
        EPISODES = []
        print('JSON database file not found at database/search_index.json')
        return False
    except json.JSONDecodeError as e:
        EPISODES = []
        print(f'Invalid JSON in database file: {e}')
        return False
    except Exception as e:
        EPISODES = []
        print(f'Error loading JSON database: {e}')
        return False

def _parse_csv_rows(reader) -> list[dict]:
    """Shared CSV parsing logic using csv.reader with column indices."""
    stories = []
    header = next(reader, None)

    num_cols = len(header) if header else len(CSV_FIELD_NAMES)
    print(f'CSV has {num_cols} columns')

    for row in reader:
        if not row or all(c.strip() == '' for c in row):
            continue
        while len(row) < len(CSV_FIELD_NAMES):
            row.append('')
        story = {}
        for i, name in enumerate(CSV_FIELD_NAMES):
            story[name] = row[i].strip() if i < len(row) else ''
        story['_raw'] = row
        stories.append(story)

    return stories

def load_csv_database_local(base_path: str) -> bool:
    """Load CSV data from local files using csv.reader with column indices."""
    global csv_stories, csv_data_loaded

    try:
        print(f'Loading CSV data from: {base_path}')

        if not os.path.isdir(base_path):
            print(f'Directory not found: {base_path}')
            print('Run: git clone https://github.com/star884/Doraemon-CSV-DB.git ~/Doraemon-CSV-DB')
            csv_stories = []
            csv_data_loaded = False
            return False

        story_file = os.path.join(base_path, 'story_index.csv')

        if not os.path.isfile(story_file):
            print(f'Story index not found at {story_file}')
            csv_stories = []
            csv_data_loaded = False
            return False

        with open(story_file, 'r', encoding='utf-8') as f:
            stories = _parse_csv_rows(csv.reader(f))

        csv_stories = stories
        csv_data_loaded = True
        print(f'Loaded {len(csv_stories)} stories from local CSV (offline mode)')

        if csv_stories:
            s = csv_stories[0]
            print(f'  First story: {s.get("English title", "?")} | Magazine: {s.get("Magazine code", "?")} | Date: {s.get("Publication Date", "?")}')
            print(f'  1979 anime: {s.get("1979 anime", "?")} | 2005 anime: {s.get("2005 anime", "?")}')

        return True

    except PermissionError:
        print(f'Permission denied accessing: {base_path}')
        csv_stories = []
        csv_data_loaded = False
        return False
    except Exception as e:
        print(f'Error loading local CSV: {type(e).__name__}: {e}')
        csv_stories = []
        csv_data_loaded = False
        return False

async def load_csv_database_live(source_url: str) -> bool:
    """Fetch CSV data from GitHub (fallback)."""
    global csv_stories, csv_data_loaded

    try:
        import aiohttp
    except ImportError:
        print('ERROR: aiohttp not installed')
        csv_stories = []
        csv_data_loaded = False
        return False

    async with data_load_lock:
        try:
            print('Fetching CSV data from GitHub...')
            story_url = CSV_RAW_BASE_URL + 'story_index.csv'

            async with aiohttp.ClientSession() as session:
                async with session.get(story_url, timeout=30) as resp:
                    if resp.status != 200:
                        print(f'Failed to fetch CSV: HTTP {resp.status}')
                        csv_stories = []
                        csv_data_loaded = False
                        return False

                    content = await resp.text(encoding='utf-8')
                    lines = content.splitlines()

                    if not lines:
                        csv_stories = []
                        csv_data_loaded = False
                        return False

                    stories = _parse_csv_rows(csv.reader(lines))

                    csv_stories = stories
                    csv_data_loaded = True
                    print(f'Loaded {len(csv_stories)} stories from CSV (online mode)')
                    return True

        except Exception as e:
            print(f'Error fetching CSV: {type(e).__name__}: {e}')
            csv_stories = []
            csv_data_loaded = False
            return False

def convert_csv_to_episode_format(stories: list[dict]) -> list[dict]:
    """Map CSV data to standard EPISODES format."""
    episodes = []
    for story in stories:
        anime_1979 = story.get('1979 anime', '')
        anime_2005 = story.get('2005 anime', '')
        anime_1979_extra = story.get('1979 anime extra', '')
        anime_2005_extra = story.get('2005 anime extra 1', '')

        anime_1979_full = f"{anime_1979}; {anime_1979_extra}" if anime_1979_extra else anime_1979
        anime_2005_full = f"{anime_2005}; {anime_2005_extra}" if anime_2005_extra else anime_2005

        in_ep = anime_1979 or anime_2005 or 'N/A'

        ep = {
            'in_season_episode': in_ep,
            'alt_in_episode': '',
            'jp_story_numbers': '',
            'jp_story_all': '',
            'story_a': story.get('English title', '') or story.get('Japanese title', ''),
            'story_b': story.get('Japanese title', ''),
            'category': 'manga_stories',
            'title': story.get('English title', '') or story.get('Japanese title', ''),
            'Magazine code': story.get('Magazine code', ''),
            'Publication Date': story.get('Publication Date', ''),
            'English title': story.get('English title', ''),
            'Japanese title': story.get('Japanese title', ''),
            'English Kindle': story.get('English Kindle', ''),
            'Bilingual Print': story.get('Bilingual Print', ''),
            'Tankōbon': story.get('Tankōbon', ''),
            'Complete Collection': story.get('Complete Collection', ''),
            '1979 anime': anime_1979_full,
            '2005 anime': anime_2005_full,
            'Movie': story.get('Movie', ''),
            'Short film': story.get('Short film', ''),
            'Notes': story.get('Notes', ''),
            'jp_parsed': [],
            'jp_has_specials': False,
            'jp_has_standalone_specials': False,
        }
        episodes.append(ep)
    return episodes

async def load_csv_database(source_config: dict) -> bool:
    """Unified loader supporting both local and remote CSV."""
    global EPISODES, csv_stories, csv_data_loaded

    fetch_method = source_config.get('fetch_method', 'github_api')
    source_url = source_config['url']

    if fetch_method == 'local_file':
        success = load_csv_database_local(source_url)
        if not success:
            print('Local load failed, attempting remote fallback...')
            success = await load_csv_database_live(CSV_RAW_BASE_URL)
    else:
        success = await load_csv_database_live(source_url)

    if success:
        EPISODES = convert_csv_to_episode_format(csv_stories)
        print(f'CSV database ready with {len(EPISODES)} stories')
        return True

    return False

def check_csv_source() -> tuple[bool, Embed | None]:
    """Check if CSV source is available."""
    if not csv_data_loaded or len(csv_stories) == 0:
        embed = Embed(
            title='CSV Not Available',
            description=(
                'CSV database is not loaded.\n\n'
                '**Possible causes:**\n'
                '- Directory not found: `~/Doraemon-CSV-DB/database/`\n'
                '- No internet connection (for online mode)\n\n'
                f'Current source: **{current_source["name"]}**\n\n'
                '**Fix:** Clone the repo:\n'
                '```\ngit clone https://github.com/star884/Doraemon-CSV-DB.git ~/Doraemon-CSV-DB\n```'
            ),
            color=COLOR_ERROR
        )
        return False, embed
    return True, None

# ============================================
# SOURCE MANAGEMENT COMMANDS
# ============================================

@bot.tree.command(name='source', description='Shows the current database source')
async def source_cmd(interaction: discord.Interaction):
    embed = Embed(title='Current Database Source', color=COLOR_PRIMARY)
    embed.add_field(name='Source Name', value=current_source['name'], inline=True)
    embed.add_field(name='Type', value=current_source['type'].title(), inline=True)

    url_display = current_source['url'][:60] + '...' if len(current_source['url']) > 60 else current_source['url']
    embed.add_field(name='URL/Path', value=url_display, inline=False)

    if current_source['type'] == 'csv':
        status = 'Loaded' if csv_data_loaded else 'Not loaded'
        embed.add_field(name='Status', value=status, inline=True)
        embed.add_field(name='Records', value=str(len(csv_stories)), inline=True)
    elif current_source['type'] == 'json':
        embed.add_field(name='Total Episodes', value=str(len(EPISODES)), inline=True)

    embed.set_footer(text=f'Source ID: {current_source["id"]} | Switch with /source_change <id>')
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='source_all', description='Lists all available database sources')
async def source_all_cmd(interaction: discord.Interaction):
    embed = Embed(title='Available Database Sources', color=COLOR_PRIMARY)

    description = ''
    for src in available_sources:
        desc = f'**{src["id"]}) {src["name"]}**\n'
        desc += f'> Type: {src["type"].title()} | Method: {src.get("fetch_method", "default").title()}\n'
        if src.get('files'):
            desc += f'> Files: {", ".join(src["files"])}\n'
        if src['id'] == current_source['id']:
            desc += '> **Currently Active**\n'
        description += desc + '\n'

    embed.description = description
    safe_add_field(embed, name='How to switch',
                   value='Use `/source_change <number>` to switch\nExample: `/source_change 2`', inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='source_change', description='Changes the active database source')
@app_commands.describe(source_id='Source number to switch to (1=JSON, 2=CSV)')
async def source_change_cmd(interaction: discord.Interaction, source_id: int):
    source = next((s for s in available_sources if s['id'] == source_id), None)

    if not source:
        embed = Embed(title='Invalid Source',
                      description=f'Source ID {source_id} not found.\nUse `/source_all` to see available sources.',
                      color=COLOR_ERROR)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    old_source = current_source.copy()

    try:
        if source['type'] == 'csv':
            success = await load_csv_database(source)
            if not success:
                embed = Embed(
                    title='Warning',
                    description=(
                        'Could not load CSV data.\n\n'
                        '**Troubleshooting:**\n'
                        '- Verify folder exists: `~/Doraemon-CSV-DB/database/`\n'
                        '- Run: `git clone https://github.com/star884/Doraemon-CSV-DB.git ~/Doraemon-CSV-DB`\n'
                        '- Then switch back: `/source_change 1`'
                    ),
                    color=COLOR_WARNING
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                current_source.update(old_source)
                return
            current_source.update(source)
        elif source['type'] == 'json':
            success = load_json_database()
            if not success:
                embed = Embed(title='Warning',
                              description='Could not load JSON data.',
                              color=COLOR_WARNING)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                current_source.update(old_source)
                return
            current_source.update(source)

        record_count = len(csv_stories) if source['type'] == 'csv' else len(EPISODES)
        embed = Embed(title='Source Changed Successfully', color=COLOR_PRIMARY)
        embed.add_field(name='Previous Source', value=old_source['name'], inline=False)
        embed.add_field(name='New Source', value=source['name'], inline=False)
        embed.add_field(name='Records Loaded', value=str(record_count), inline=True)
        embed.set_footer(text='Use /source to view current')
        await interaction.response.send_message(embed=embed)

    except Exception as e:
        print(f'Error during source change: {e}')
        current_source.update(old_source)
        embed = Embed(title='Error',
                      description='An error occurred while switching sources.',
                      color=COLOR_ERROR)
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ============================================
# SEARCH COMMANDS
# ============================================

@bot.tree.command(name='search', description='Search by title, 1979/2005 anime episode, or magazine code')
@app_commands.describe(query='Search term (e.g., nobita, Ep 7, YK, 1970-01)')
async def search_cmd(interaction: discord.Interaction, query: str):
    if not EPISODES:
        embed = Embed(
            title='Database Empty',
            description='No episodes loaded. Use `/source` to check status.',
            color=COLOR_ERROR
        )
        await interaction.response.send_message(embed=embed)
        return

    q = query.lower().strip()
    results = []
    search_type = None

    csv_mode = is_csv_source()

    if csv_mode:
        # 1. Magazine code search
        if len(q) <= 4 and q.upper() in KNOWN_MAGAZINES:
            search_type = 'magazine'
            results = [ep for ep in EPISODES if ep.get('Magazine code', '').lower() == q]

        # 2. 1979 anime episode search (e.g., "ep 7", "ep7")
        elif q.startswith('ep ') or (q.startswith('ep') and len(q) > 2 and q[2:3].isdigit()):
            search_type = '1979_anime'
            ep_num = q.replace('ep ', '').replace('ep', '').strip()
            for ep in EPISODES:
                val = ep.get('1979 anime', '').lower()
                if ep_num in val:
                    results.append(ep)

        # 3. 2005 anime episode search (e.g., "2005 16A")
        elif q.startswith('2005') and len(q) > 5:
            search_type = '2005_anime'
            ep_num = q[5:].strip()
            for ep in EPISODES:
                val = ep.get('2005 anime', '').lower()
                if ep_num in val:
                    results.append(ep)

        # 4. Publication date search (e.g., "1970-01")
        elif re.match(r'^\d{4}-\d{2}$', q):
            search_type = 'pub_date'
            results = [ep for ep in EPISODES if ep.get('Publication Date', '').lower() == q]

        # 5. Title search (fallback - substring matching for natural language)
        if not results:
            search_type = 'title'
            for ep in EPISODES:
                story_a = ep.get('story_a', '').lower()
                story_b = ep.get('story_b', '').lower() if ep.get('story_b') else ''
                if q in story_a or q in story_b:
                    results.append(ep)
            results = results[:10]

    else:
        # JSON source - original search logic
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
            for ep in EPISODES:
                story_a = ep.get('story_a', '').lower()
                story_b = ep.get('story_b', '').lower() if ep.get('story_b') else ''
                if q in story_a or q in story_b:
                    results.append(ep)
            results = results[:5]

    if not results:
        tips_csv = (
            '**CSV Search Formats:**\n'
            '- Title: `nobita` or `doraemon`\n'
            '- Magazine code: `YK`, `G1`, `KG`, `G4`\n'
            '- 1979 anime: `Ep 7` or `ep7`\n'
            '- 2005 anime: `2005 16A`\n'
            '- Publication date: `1970-01`'
        )
        tips_json = (
            '**JSON Search Formats:**\n'
            '- JP Story #: `150` or `jp:150`\n'
            '- JP Special: `150 S` or `S16`\n'
            '- IN Episode: `s04e35`\n'
            '- Title: `nobita`'
        )
        embed = Embed(
            title='No Results Found',
            description=f'No episodes found for "**{query}**"\n\n'
                        + (tips_csv if csv_mode else tips_json)
                        + f'\n\n**Current Source:** {current_source["name"]}',
            color=COLOR_ERROR
        )
        await interaction.response.send_message(embed=embed)
        return

    first = results[0]

    if csv_mode:
        title = first.get('English title', '') or first.get('Japanese title', 'Unknown')
        embed = Embed(title=f'Found: {truncate(title, 70)}', color=COLOR_PRIMARY)
        embed.description = f'**Category:** Manga Stories'
        build_csv_result_fields(embed, first)
    else:
        in_ep = first.get('in_season_episode', 'N/A')
        alt_ep = first.get('alt_in_episode')
        jp_display = first.get('jp_story_all', 'Not indexed')
        story_a = first.get('story_a', 'Unknown')
        story_b = first.get('story_b')
        category = first.get('category', 'unknown').replace('_', ' ').title()
        title = first.get('title', 'Unknown')[:70]

        embed = Embed(title=f'Episode Found: {title}', color=COLOR_PRIMARY)
        embed.description = f'**Category:** {category}'
        embed.add_field(name='Indian Episode', value=in_ep, inline=True)
        if alt_ep:
            embed.add_field(name='Alt IN Number', value=alt_ep, inline=True)
        embed.add_field(name='Japanese Story #', value=jp_display, inline=True)
        safe_add_field(embed, name='Story A', value=truncate(story_a, EMBED_FIELD_VALUE_MAX), inline=False)
        if story_b:
            safe_add_field(embed, name='Story B', value=truncate(story_b, EMBED_FIELD_VALUE_MAX), inline=False)

    footer = f'Searched by: {search_type} | Matches: {len(results)} | Source: {current_source["name"]}'
    embed.set_footer(text=footer)

    if len(results) > 1:
        build_more_matches_field(embed, results, csv_mode)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='jp', description='Lookup by Japanese Story Number (JSON source only)')
@app_commands.describe(jp_number='Japanese Story Number (e.g., 150, 150 S, or S16)')
async def jp_cmd(interaction: discord.Interaction, jp_number: str):
    if not EPISODES:
        embed = Embed(title='Database Empty', description='No episodes loaded.', color=COLOR_ERROR)
        await interaction.response.send_message(embed=embed)
        return

    if is_csv_source():
        embed = Embed(
            title='Not Available for CSV',
            description='JP Story Number lookup requires the JSON database source.\n'
                        'Use `/source_change 1` to switch to JSON.\n\n'
                        'For CSV, use `/search <title>` or `/search_1979 <episode>` instead.',
            color=COLOR_WARNING
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
            title='Not Found',
            description=f'No episode found for JP Story #{jp_number}\n\n'
                        f'**Current Source:** {current_source["name"]}',
            color=COLOR_ERROR
        )
        await interaction.response.send_message(embed=embed)
        return

    first = results[0]
    embed = Embed(title=f'JP Story #{jp_number}', color=COLOR_PRIMARY)
    embed.add_field(name='IN Episode', value=first.get('in_season_episode', 'N/A'), inline=True)
    embed.add_field(name='Category', value=first.get('category', 'unknown').replace('_', ' ').title(), inline=True)
    if first.get('alt_in_episode'):
        embed.add_field(name='Alt IN', value=first.get('alt_in_episode'), inline=True)
    safe_add_field(embed, name='Story A', value=truncate(first.get('story_a', 'Unknown'), EMBED_FIELD_VALUE_MAX), inline=False)
    if first.get('story_b'):
        safe_add_field(embed, name='Story B', value=truncate(first.get('story_b'), EMBED_FIELD_VALUE_MAX), inline=False)
    embed.add_field(name='All JP Stories', value=first.get('jp_story_all', '-'), inline=False)
    embed.set_footer(text=f'Matches: {len(results)} | Source: {current_source["name"]}')

    if len(results) > 1:
        build_more_matches_field(embed, results, is_csv=False)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='list', description='List episodes with pagination')
@app_commands.describe(page='Page number (1-999)', season='Filter by magazine code (CSV) or season (JSON)')
async def list_cmd(interaction: discord.Interaction, page: int = 1, season: str = None):
    if not EPISODES:
        embed = Embed(title='Database Empty', description='No episodes loaded.', color=COLOR_ERROR)
        await interaction.response.send_message(embed=embed)
        return

    filtered = EPISODES[:]

    if season:
        s = season.lower()
        if is_csv_source():
            filtered = [e for e in EPISODES if e.get('Magazine code', '').lower() == s]
        else:
            if s == 'special':
                filtered = [e for e in EPISODES if e.get('category') == 'special_episodes']
            elif s == 'classic':
                filtered = [e for e in EPISODES if e.get('category') == 'classic_doraemon']
            elif s.isdigit():
                filtered = [e for e in EPISODES if e.get('category') == f'season_{s}']

    if page < 1:
        page = 1

    total_pages = (len(filtered) + PAGE_SIZE - 1) // PAGE_SIZE if filtered else 1

    if page > total_pages:
        embed = Embed(
            title='Page Not Found',
            description=f'Page {page} does not exist. Valid pages: 1-{total_pages}\n**Source:** {current_source["name"]}',
            color=COLOR_ERROR
        )
        await interaction.response.send_message(embed=embed)
        return

    start = (page - 1) * PAGE_SIZE
    end = min(start + PAGE_SIZE, len(filtered))

    chunk = filtered[start:end]
    filter_display = season if season else 'All'
    embed = Embed(title=f'Doraemon Episodes - Page {page} ({filter_display})', color=COLOR_PRIMARY)
    embed.description = f'Total: **{len(filtered)}** | Showing {start + 1}-{end}'

    csv_mode = is_csv_source()

    for r in chunk:
        if csv_mode:
            mag = r.get('Magazine code', '?')
            pub_date = r.get('Publication Date', '?')
            title = r.get('English title', '') or r.get('Japanese title', 'Unknown')
            jp_title = r.get('Japanese title', '')
            anime_1979 = r.get('1979 anime', '')
            anime_2005 = r.get('2005 anime', '')
            field_name = f'[{mag}] {pub_date} | {truncate(title, 40)}'
            field_value = jp_title[:50]
            if anime_1979:
                field_value += f' | 1979: {anime_1979}'
            if anime_2005:
                field_value += f' | 2005: {anime_2005}'
            safe_add_field(embed, name=field_name, value=field_value, inline=False)
        else:
            in_ep = r.get('in_season_episode', 'N/A')
            jp_st = r.get('jp_story_all', '-') or '-'
            story_a = r.get('story_a', 'Unknown')[:40]
            story_b = r.get('story_b', '')
            title_display = story_a
            if story_b:
                title_display += f' / {truncate(story_b, 40)}'
            safe_add_field(embed, name=f'{in_ep} | JP: {jp_st}', value=title_display, inline=False)

    embed.set_footer(text=f'Page {page} of {total_pages} | Source: {current_source["name"]}')
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='stats', description='Show database statistics')
async def stats_cmd(interaction: discord.Interaction):
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

    embed = Embed(title='Database Statistics', color=COLOR_PRIMARY)

    if is_csv_source():
        magazines = {}
        for e in EPISODES:
            mag = e.get('Magazine code', 'Unknown')
            magazines[mag] = magazines.get(mag, 0) + 1

        embed.description = (
            f'**Total Stories:** {len(EPISODES)}\n'
            f'**Source:** {current_source["name"]}\n'
            f'**Path:** {truncate(current_source["url"], 60)}\n\n'
            f'**By Magazine Code:**'
        )
        for mag, cnt in sorted(magazines.items(), key=lambda x: -x[1]):
            embed.add_field(name=mag, value=f'**{cnt}**', inline=True)

        has_1979 = sum(1 for e in EPISODES if e.get('1979 anime', '').strip())
        has_2005 = sum(1 for e in EPISODES if e.get('2005 anime', '').strip())
        has_movie = sum(1 for e in EPISODES if e.get('Movie', '').strip())

        embed.add_field(name='Has 1979 Anime', value=f'**{has_1979}**', inline=True)
        embed.add_field(name='Has 2005 Anime', value=f'**{has_2005}**', inline=True)
        embed.add_field(name='Has Movie', value=f'**{has_movie}**', inline=True)
    else:
        embed.description = (
            f'**Total Episodes:** {len(EPISODES)}\n'
            f'**JP Indexed:** {with_jp}\n'
            f'**JP Special Stories:** {jp_specials}\n'
            f'**JP Standalone Specials:** {jp_standalone}\n\n'
            f'**Source:** {current_source["name"]} ({current_source["type"].title()})\n'
            f'**URL:** {current_source["url"]}\n\n'
            f'**Breakdown:**'
        )
        for cat, cnt in sorted(counts.items(), key=lambda x: -x[1]):
            embed.add_field(name=cat.replace('_', ' ').title(), value=f'**{cnt}**', inline=True)

    if is_csv_source() and csv_data_loaded:
        embed.add_field(name='CSV Rows', value=str(len(csv_stories)), inline=True)

    embed.set_footer(text=f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")} | Source: {current_source["name"]}')
    await interaction.response.send_message(embed=embed)

# ============================================
# HELP COMMAND
# ============================================

@bot.tree.command(name='help', description='Show available commands')
async def help_cmd(interaction: discord.Interaction):
    embed = Embed(title='Doraemon Search Bot Help', color=COLOR_PRIMARY)

    if is_csv_source():
        embed.description = (
            '**Available Commands (CSV Mode):**\n\n'
            '**Core Commands**\n'
            '- `/search <query>` - Search by title, magazine code, 1979/2005 anime ep, or date\n'
            '- `/list [page] [magazine]` - Browse stories (filter by magazine: G1, YK, etc.)\n'
            '- `/stats` - Show database statistics\n\n'
            '**Source Commands**\n'
            '- `/source` - Shows current source\n'
            '- `/source_all` - Lists all sources\n'
            '- `/source_change <id>` - Switch source (1=JSON, 2=CSV)\n\n'
            '**CSV-Only Commands**\n'
            '- `/search_jp_title <keyword>` - Search by Japanese title\n'
            '- `/search_magazine <code>` - Search by magazine code\n'
            '- `/search_1979 <episode>` - Search by 1979 anime episode\n'
            '- `/search_2005 <episode>` - Search by 2005 anime episode\n'
            '- `/search_volume <type> <number>` - Search by compilation volume (Tankōbon, Kindle, etc.)\n\n'
            '**Info**\n'
            '- `/help` - Show this message\n\n'
            f'**Current Source:** {current_source["name"]}\n'
            f'**Total Records:** {len(EPISODES)}'
        )
    else:
        embed.description = (
            '**Available Commands (JSON Mode):**\n\n'
            '**Core Commands**\n'
            '- `/search <query>` - Search by JP#, IN#, SPE, CE, or title\n'
            '- `/jp <number>` - Lookup by Japanese Story Number\n'
            '- `/list [page] [season]` - Browse episodes with pagination\n'
            '- `/stats` - Show database statistics\n\n'
            '**Source Commands**\n'
            '- `/source` - Shows current source\n'
            '- `/source_all` - Lists all sources\n'
            '- `/source_change <id>` - Switch source (1=JSON, 2=CSV)\n\n'
            '**Info**\n'
            '- `/help` - Show this message\n\n'
            f'**Current Source:** {current_source["name"]}\n'
            f'**Total Records:** {len(EPISODES)}'
        )

    embed.set_footer(text='Doraemon Search Bot')
    await interaction.response.send_message(embed=embed)

# ============================================
# CSV-SPECIFIC COMMANDS
# ============================================

@bot.tree.command(name='search_jp_title', description='Search by Japanese title (CSV only)')
@app_commands.describe(query='Japanese title or keyword')
async def search_jp_title_cmd(interaction: discord.Interaction, query: str):
    is_available, error_embed = check_csv_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return

    q = query.lower()
    results = [s for s in csv_stories
               if q in s.get('Japanese title', '').lower() or q in s.get('English title', '').lower()]

    if not results:
        embed = Embed(title='No Results Found',
                      description=f'No stories found for "**{query}**"',
                      color=COLOR_ERROR)
        await interaction.response.send_message(embed=embed)
        return

    first = results[0]
    embed = Embed(title=f'JP Title Match: {truncate(first.get("English title", "Unknown"), 70)}',
                  color=COLOR_PRIMARY)
    embed.add_field(name='Japanese Title', value=first.get('Japanese title', 'N/A'), inline=False)
    embed.add_field(name='1979 Episode', value=first.get('1979 anime', 'N/A'), inline=True)
    embed.add_field(name='2005 Episode', value=first.get('2005 anime', 'N/A'), inline=True)
    embed.add_field(name='Magazine', value=first.get('Magazine code', 'N/A'), inline=True)
    embed.add_field(name='Publication Date', value=first.get('Publication Date', 'N/A'), inline=True)

    if len(results) > 1:
        additional = '\n'.join(f'- {truncate(r.get("English title", "?"), 40)}' for r in results[1:6])
        safe_add_field(embed, name=f'More Matches ({len(results) - 1})', value=additional, inline=False)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='search_magazine', description='Search by magazine code (CSV only)')
@app_commands.describe(code='Magazine code (e.g., YK, KG, G1, G2, G3, G4, G5, G6, BK, SD, TK, CC, CD)')
async def search_magazine_cmd(interaction: discord.Interaction, code: str):
    is_available, error_embed = check_csv_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return

    code_upper = code.upper()
    results = [s for s in csv_stories if s.get('Magazine code', '').upper() == code_upper]

    if not results:
        embed = Embed(title='No Results Found',
                      description=f'No stories found for magazine code "**{code.upper()}**"',
                      color=COLOR_ERROR)
        await interaction.response.send_message(embed=embed)
        return

    embed = Embed(title=f'Magazine: {code.upper()}', color=COLOR_PRIMARY)
    embed.description = f'Found **{len(results)}** stories'

    for story in results[:10]:
        title = story.get('English title', '') or story.get('Japanese title', 'Unknown')
        date = story.get('Publication Date', '?')
        safe_add_field(embed, name=f'{date} - {truncate(title, 50)}', value=ZERO_WIDTH_SPACE, inline=False)

    if len(results) > 10:
        embed.set_footer(text=f'Showing 10 of {len(results)} results | Source: {current_source["name"]}')
    else:
        embed.set_footer(text=f'Source: {current_source["name"]}')

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='search_1979', description='Search by 1979 anime episode (CSV only)')
@app_commands.describe(episode='Episode text (e.g., 7, Ep 7, 403, Special)')
async def search_1979_cmd(interaction: discord.Interaction, episode: str):
    is_available, error_embed = check_csv_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return

    q = episode.lower().strip()
    results = []
    for s in csv_stories:
        val = s.get('1979 anime', '').lower()
        val_extra = s.get('1979 anime extra', '').lower()
        if q in val or q in val_extra:
            results.append(s)

    if not results:
        embed = Embed(title='No Results Found',
                      description=f'No stories found for 1979 anime episode "**{episode}**"',
                      color=COLOR_ERROR)
        await interaction.response.send_message(embed=embed)
        return

    embed = Embed(title=f'1979 Anime: {episode}', color=COLOR_PRIMARY)
    embed.description = f'Found **{len(results)}** story/stories'

    for story in results[:10]:
        title = story.get('English title', '') or story.get('Japanese title', '?')
        jp = story.get('Japanese title', '')
        desc = f'- {truncate(title, 50)}' + (f' ({truncate(jp, 30)})' if jp else '')
        safe_add_field(embed, name=truncate(desc, EMBED_FIELD_NAME_MAX), value=ZERO_WIDTH_SPACE, inline=False)

    if len(results) > 10:
        embed.set_footer(text=f'Showing 10 of {len(results)} results | Source: {current_source["name"]}')
    else:
        embed.set_footer(text=f'Source: {current_source["name"]}')

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='search_2005', description='Search by 2005 anime episode (CSV only)')
@app_commands.describe(episode='Episode text (e.g., 16A, 44, 87, 1694)')
async def search_2005_cmd(interaction: discord.Interaction, episode: str):
    is_available, error_embed = check_csv_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return

    q = episode.lower().strip()
    results = []
    for s in csv_stories:
        val = s.get('2005 anime', '').lower()
        val_extra = s.get('2005 anime extra 1', '').lower()
        val_extra2 = s.get('2005 anime extra 2', '').lower()
        if q in val or q in val_extra or q in val_extra2:
            results.append(s)

    if not results:
        embed = Embed(title='No Results Found',
                      description=f'No stories found for 2005 anime episode "**{episode}**"',
                      color=COLOR_ERROR)
        await interaction.response.send_message(embed=embed)
        return

    embed = Embed(title=f'2005 Anime: {episode}', color=COLOR_PRIMARY)
    embed.description = f'Found **{len(results)}** story/stories'

    for story in results[:10]:
        title = story.get('English title', '') or story.get('Japanese title', '?')
        jp = story.get('Japanese title', '')
        desc = f'- {truncate(title, 50)}' + (f' ({truncate(jp, 30)})' if jp else '')
        safe_add_field(embed, name=truncate(desc, EMBED_FIELD_NAME_MAX), value=ZERO_WIDTH_SPACE, inline=False)

    if len(results) > 10:
        embed.set_footer(text=f'Showing 10 of {len(results)} results | Source: {current_source["name"]}')
    else:
        embed.set_footer(text=f'Source: {current_source["name"]}')

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='search_volume', description='Search by volume number across all compilation types (CSV only)')
@app_commands.describe(
    volume_type='Which compilation type to search',
    volume_number='Volume number (e.g., 1, +1, 0, L1, 197, 1-1)'
)
@app_commands.choices(volume_type=[
    app_commands.Choice(name='Tankōbon (original Japanese print)', value='tankobon'),
    app_commands.Choice(name='Complete Collection (Fujiko F. Fujio)', value='complete'),
    app_commands.Choice(name='English Kindle', value='kindle'),
    app_commands.Choice(name='Bilingual Print', value='bilingual'),
    app_commands.Choice(name='All Types', value='all'),
])
async def search_volume_cmd(
    interaction: discord.Interaction,
    volume_type: str,
    volume_number: str
):
    is_available, error_embed = check_csv_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return

    q = volume_number.strip()

    results = []

    if volume_type == 'all':
        for story in csv_stories:
            for col_name in VOLUME_COLUMNS.values():
                val = story.get(col_name, '').strip()
                if val and word_boundary_match(q, val):
                    story_copy = dict(story)
                    story_copy['_matched_column'] = col_name
                    results.append(story_copy)
                    break
    else:
        col_name = VOLUME_COLUMNS.get(volume_type, 'Tankōbon')
        for story in csv_stories:
            val = story.get(col_name, '').strip()
            if val and word_boundary_match(q, val):
                story_copy = dict(story)
                story_copy['_matched_column'] = col_name
                results.append(story_copy)

    if not results:
        embed = Embed(
            title='No Results Found',
            description=f'No stories found for volume "**{volume_number}**" in {volume_type.title()}.',
            color=COLOR_ERROR
        )
        safe_add_field(embed, name='Tip', value=(
            'Try different formats:\n'
            '- Plain number: `1`, `5`, `44`\n'
            '- Plus prefix: `+1`, `+5`\n'
            '- Volume 0: `0`\n'
            '- Long Stories: `L1`, `L5`\n'
            '- Bilingual: `1-1`, `2-3`, `3-6`\n'
            '- Kindle: `197`, `174`'
        ), inline=False)
        embed.set_footer(text=f'Source: {current_source["name"]}')
        await interaction.response.send_message(embed=embed)
        return

    type_display = {
        'tankobon':  'Tankōbon',
        'complete':  'Complete Collection',
        'kindle':    'English Kindle',
        'bilingual': 'Bilingual Print',
        'all':       'All Types',
    }.get(volume_type, volume_type.title())

    embed = Embed(
        title=f'Volume Search: "{volume_number}" ({type_display})',
        color=COLOR_PRIMARY
    )
    embed.description = f'Found **{len(results)}** story/stories'

    for story in results[:10]:
        title = story.get('English title', '') or story.get('Japanese title', 'Unknown')
        jp_title = story.get('Japanese title', '')
        matched_col = story.get('_matched_column', '?')

        vol_parts = []
        for col_label, col_key in [
            ('Tankōbon', 'Tankōbon'),
            ('Complete', 'Complete Collection'),
            ('Kindle', 'English Kindle'),
            ('Bilingual', 'Bilingual Print'),
        ]:
            v = story.get(col_key, '').strip()
            if v:
                highlight = '**' if col_key == matched_col else ''
                vol_parts.append(f'{highlight}{col_label}: {v}{highlight}')

        field_name = truncate(title, EMBED_FIELD_NAME_MAX)
        field_value = (f'({truncate(jp_title, 30)})\n' if jp_title else '') + ' | '.join(vol_parts)
        safe_add_field(embed, name=field_name, value=field_value, inline=False)

    if len(results) > 10:
        embed.set_footer(text=f'Showing 10 of {len(results)} results | Source: {current_source["name"]}')
    else:
        embed.set_footer(text=f'Source: {current_source["name"]}')

    await interaction.response.send_message(embed=embed)

# ============================================
# ERROR HANDLING
# ============================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):
    """Handle slash command errors gracefully."""
    print(f'Slash command error: {type(error).__name__}: {error}')

    embed = Embed(
        title='Something went wrong',
        description='An unexpected error occurred while running this command.\n'
                    'Please try again, or contact the bot administrator if the issue persists.',
        color=COLOR_ERROR
    )

    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        print(f'Failed to send error message: {e}')

@bot.event
async def on_command_error(ctx, error):
    """Handle legacy prefix command errors (rarely triggered)."""
    if isinstance(error, commands.CommandNotFound):
        await ctx.send('Command not found. Use `/help` to see available commands.')
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send('Missing required argument. Use `/help` for usage information.')
    elif isinstance(error, commands.CheckFailure):
        print(f'Check failure: {error}')
    else:
        print(f'Unhandled error: {type(error).__name__}: {error}')
        try:
            await ctx.send('An unexpected error occurred. Please contact the bot administrator.')
        except:
            pass

# ============================================
# EVENT HANDLERS
# ============================================

@bot.event
async def on_ready():
    global EPISODES

    print(f'Logged in as {bot.user}')
    print('Syncing slash commands...')
    synced = await bot.tree.sync()
    print(f'Synced {len(synced)} GLOBAL commands')

    if bot.guilds:
        print(f'Bot is in {len(bot.guilds)} guild(s)')

    print('\nLoading initial database...')
    print(f'   Default source: {current_source["name"]} ({current_source["type"]})')

    success = load_json_database()
    if not success:
        print('Initial JSON database load failed. Bot will run with empty database.')
        print('Use /source_change 2 to load CSV (local offline mode)')
    else:
        print(f'Database loaded successfully ({len(EPISODES)} records)')

    print('\nAttempting to load CSV from local path...')
    csv_success = load_csv_database_local(CSV_LOCAL_PATH)
    if csv_success:
        EPISODES = convert_csv_to_episode_format(csv_stories)
        current_source.update(available_sources[1])
        print(f'CSV loaded successfully ({len(csv_stories)} stories - CSV mode active)')
    else:
        print('CSV not available (use /source_change 2 when ready)')

    print(f'\nStatus:')
    print(f'   Core commands: OK')
    print(f'   Source commands: OK')
    print(f'   CSV features: {"Ready" if csv_data_loaded else "Disabled (/source_change 2)"}')
    print(f'\nBot is ready!')

# ============================================
# MAIN EXECUTION
# ============================================

if __name__ == '__main__':
    print('=' * 60)
    print('Starting Doraemon Search Bot...')
    print(f'Running in: {HOME_DIR}')
    print('=' * 60)

    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f'Fatal error starting bot: {type(e).__name__}: {e}')
        print('Check your DISCORD_TOKEN and permissions.')
        exit(1)
