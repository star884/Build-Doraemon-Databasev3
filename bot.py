import discord
import json
import os
import re
import asyncio
from discord import app_commands, Embed
from discord.ext import commands
from dotenv import load_dotenv
from pathlib import Path
# Prevent bot from starting in GitHub Actions
if os.getenv('GITHUB_ACTIONS') == 'true':
    print('⚠️ Running in GitHub Actions - Bot startup skipped')
    print('   Run bot.py on your server separately')
    exit(0)

# Validate Discord token
if not TOKEN:
    print('❌ ERROR: DISCORD_TOKEN environment variable not set')
    print('   The bot requires a Discord token to run.')
    print('   Set DISCORD_TOKEN in your .env file or server environment.')
    exit(1)
load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')

# ============================================
# Source Management Configuration
# ============================================
current_source = {
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
        'name': 'CSV Database (GitHub)',
        'url': 'https://github.com/star884/Doraemon-CSV-DB/blob/main/database/',
        'type': 'csv',
        'files': ['story_index.csv', 'manga_details.csv']
    }
]

# Global data storage
EPISODES = []
DB = {}
active_plugin = None

bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())

# ============================================
# Data Loading Functions
# ============================================
def load_json_database():
    """Load data from local JSON database"""
    global EPISODES, DB
    try:
        with open('database/search_index.json') as f:
            DB = json.load(f)
        EPISODES = DB.get('items', [])
        print(f'✅ Loaded {len(EPISODES)} episodes from JSON database')
        print(f'  Episodes with JP refs: {sum(1 for e in EPISODES if e.get("jp_story_numbers"))}')
        return True
    except FileNotFoundError:
        EPISODES = []
        print('❌ JSON database file not found')
        return False
    except Exception as e:
        EPISODES = []
        print(f'❌ Error loading JSON database: {e}')
        return False

async def load_csv_database(source_url):
    """Load data from CSV source via plugin"""
    global EPISODES, active_plugin
    
    try:
        # Check if CSV plugin already loaded
        if active_plugin:
            success = await active_plugin.parser.load_from_github(source_url)
            if success:
                EPISODES = active_plugin.parser.stories
                return True
            return False
        
        # Try to import CSV plugin from external repo
        csv_repo_path = Path('Doraemon-CSV-DB')
        if csv_repo_path.exists():
            import sys
            sys.path.insert(0, str(csv_repo_path))
            
            from csv_plugin import CSVPlugin
            
            active_plugin = CSVPlugin()
            success = await active_plugin.initialize(source_url)
            
            if success:
                EPISODES = active_plugin.parser.stories
                print(f'✅ Loaded {len(EPISODES)} stories from CSV database')
                return True
            else:
                print('❌ Failed to load CSV database')
                return False
        else:
            print('⚠️ CSV repository not found locally (Doraemon-CSV-DB/)')
            print('   To enable CSV source, clone it: git clone https://github.com/star884/Doraemon-CSV-DB.git')
            return False
            
    except ImportError as e:
        print(f'⚠️ CSV plugin import failed: {e}')
        print('   Make sure csv_plugin.py exists in Doraemon-CSV-DB/')
        return False
    except Exception as e:
        print(f'❌ Error loading CSV database: {e}')
        return False

async def reload_current_source():
    """Reload data from currently active source"""
    global current_source, EPISODES
    
    if current_source['type'] == 'json':
        return load_json_database()
    elif current_source['type'] == 'csv':
        return await load_csv_database(current_source['url'])
    return False

def check_csv_source():
    """Check if CSV source is active, return tuple (is_active, error_embed)"""
    if current_source['type'] != 'csv' or not active_plugin:
        embed = Embed(
            title="❌ Not Available",
            description=f'This command only works with CSV database.\n\n'
                       f'Current source: **{current_source["name"]}**\n\n'
                       f'To enable CSV features:\n'
                       f'1. Run `/source change 2`\n'
                       f'2. Make sure Doraemon-CSV-DB/ is cloned locally',
            color=0xcc0000
        )
        return False, embed
    return True, None

# ============================================
# Source Management Commands
# ============================================
@bot.tree.command(name='source', description='Shows the current database source')
async def source_cmd(interaction):
    embed = Embed(
        title=f"📚 Current Database Source",
        color=0x6d4aff
    )
    embed.add_field(name='Source Name', value=current_source['name'], inline=True)
    embed.add_field(name='Type', value=current_source['type'].title(), inline=True)
    
    if 'http' in current_source['url']:
        embed.add_field(name='URL', value=f"[{current_source['name']}]({current_source['url']})", inline=False)
    else:
        embed.add_field(name='URL', value=current_source['url'], inline=False)
    
    if current_source['type'] == 'csv':
        files = current_source.get('files', [])
        if files:
            embed.add_field(name='Files', value=', '.join(files), inline=False)
    elif current_source['type'] == 'json':
        files = current_source.get('files', [])
        if files:
            embed.add_field(name='Main File', value=files[0], inline=False)
    
    embed.set_footer(text=f'Source ID: {current_source["id"]} | Switch with /source change <id>')
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='source_all', description='Lists all available database sources')
async def source_all_cmd(interaction):
    embed = Embed(
        title="📚 Available Database Sources",
        color=0x6d4aff
    )
    
    description = ""
    for src in available_sources:
        desc = f"**{src['id}) {src['name']}**\n"
        desc += f"> Type: {src['type'].title()} | URL: {src['url']}\n"
        if src.get('files'):
            desc += f"> Files: {', '.join(src['files'])}\n"
        if src['id'] == current_source['id']:
            desc += "> ✅ **Currently Active**\n"
        description += desc + "\n"
    
    embed.description = description
    embed.add_field(
        name="💡 How to switch",
        value="Use `/source change <number>` to switch to a different source\nExample: `/source change 2`",
        inline=False
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='source_change', description='Changes the active database source')
@app_commands.describe(source_id='Source number to switch to (1=JSON, 2=CSV)')
async def source_change_cmd(interaction, source_id: int):
    source = next((s for s in available_sources if s['id'] == source_id), None)
    
    if not source:
        embed = Embed(
            title="❌ Invalid Source",
            description=f'Source ID {source_id} not found.\nUse `/source all` to see available sources.',
            color=0xcc0000
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    old_source = current_source.copy()
    current_source.update(source)
    
    # Load data from new source
    if source['type'] == 'csv':
        # Check if CSV repo exists locally
        if not Path('Doraemon-CSV-DB').exists():
            embed = Embed(
                title="⚠️ CSV Repository Not Found",
                description=f'To use the CSV source, clone the repository first:\n\n'
                f'```bash\ngit clone https://github.com/star884/Doraemon-CSV-DB.git\n```\n\n'
                f'Then run `/source change 2` again.',
                color=0xffaa00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            # Revert source
            current_source.update(old_source)
            return
        
        success = await load_csv_database(source['url'])
        if not success:
            embed = Embed(
                title="⚠️ Warning",
                description=f'Switched to {source["name"]}, but failed to load CSV data.\nCommands may not work correctly until data is loaded.',
                color=0xffaa00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
    elif source['type'] == 'json':
        success = load_json_database()
        if not success:
            embed = Embed(
                title="⚠️ Warning",
                description=f'Switched to {source["name"]}, but failed to load JSON data.',
                color=0xffaa00
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
    
    embed = Embed(
        title="✅ Source Changed Successfully",
        color=0x6d4aff
    )
    embed.add_field(name='Previous Source', value=old_source['name'], inline=False)
    embed.add_field(name='New Source', value=source['name'], inline=False)
    embed.add_field(name='URL', value=source['url'], inline=False)
    
    if source['type'] == 'csv':
        embed.add_field(name='Loaded Files', value=', '.join(source.get('files', [])), inline=True)
        embed.add_field(name='Total Stories', value=str(len(EPISODES)), inline=True)
    elif source['type'] == 'json':
        embed.add_field(name='Total Episodes', value=str(len(EPISODES)), inline=True)
    
    embed.set_footer(text=f'Refresh commands automatically • Use /source to view current')
    await interaction.response.send_message(embed=embed)

# ============================================
# Original Search Commands (Preserved)
# ============================================
@bot.tree.command(name='search', description='Advanced search by JP story #, IN episode, SPE, or CE')
@app_commands.describe(query='Search term (e.g., 150, 150 S, S16, s04e35, spe01, ce1, nobita)')
async def search_cmd(interaction, query: str):
    q = query.lower().strip()
    results = []
    search_type = None
    
    # ── JP Story Number ──────────────────────────────────────
    if q.startswith('jp:') or q.isdigit() or re.match(r'^s\d+\s*s?$', q, re.I):
        search_type = 'jp'
        jp_query = q.replace('jp:', '').strip()
        
        standalone_match = re.match(r'^s(\d+)$', jp_query, re.I)
        reg_special_match = re.match(r'^(\d+)\s*s$', jp_query, re.I)
        
        for ep in EPISODES:
            jp_parsed = ep.get('jp_parsed', [])
            for jp in jp_parsed:
                if standalone_match:
                    if jp.get('is_standalone_special') and jp.get('number') == standalone_match.group(1):
                        results.append(ep)
                        break
                elif reg_special_match:
                    target_num = reg_special_match.group(1)
                    if (not jp.get('is_standalone_special') and
                            jp.get('is_special') and
                            jp.get('number') == target_num):
                        results.append(ep)
                        break
                elif jp_query.isdigit():
                    if jp.get('number') == jp_query:
                        results.append(ep)
                        break
    
    # ── IN Episode (S01E01 format) ───────────────────────────
    elif re.match(r'^s\d{2}e\d{2}$', q):
        search_type = 'in_episode'
        results = [e for e in EPISODES if e.get('in_season_episode', '').lower() == q]
    
    # ── Special Episodes (SPE##) ─────────────────────────────
    elif q.startswith('spe'):
        search_type = 'special'
        spe_num = re.sub(r'[^0-9]', '', q.replace('spe', ''))
        results = [e for e in EPISODES if e.get('in_season_episode', '').upper().startswith('SPE') and spe_num in e.get('in_season_episode', '')]
    
    # ── Classic Episodes (CE##) ──────────────────────────────
    elif q.startswith('ce') or q.startswith('classic'):
        search_type = 'classic'
        ce_num = re.sub(r'[^0-9]', '', q.replace('ce', '').replace('classic', ''))
        results = [e for e in EPISODES if e.get('in_season_episode', '').upper().startswith('CE') and ce_num in e.get('in_season_episode', '')]
    
    # ── Fallback: Title search ───────────────────────────────
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
            title="❌ No Results Found",
            description=f'No episodes found for "**{query}**"\n\n'
            '**Supported formats:**\n'
            '• JP Story #: `150` or `jp:150`\n'
            '• JP Special (S suffix): `150 S`\n'
            '• JP Standalone Special: `S16`\n'
            '• IN Episode: `s04e35`\n'
            '• Special: `spe01` or `spe1`\n'
            '• Classic: `ce1` or `classic1`\n'
            '• Title: `nobita`\n\n'
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
    
    embed = Embed(title=f"🔍 Episode Found: {title}", color=0x6d4aff)
    embed.description = f"**Category:** {category}"
    embed.add_field(name='🇮🇳 Indian Episode', value=in_ep, inline=True)
    if alt_ep:
        embed.add_field(name='🔢 Alt IN Number', value=alt_ep, inline=True)
    embed.add_field(name='🇯🇵 Japanese Story #', value=jp_display, inline=True)
    embed.add_field(name='📖 Story A', value=story_a[:200], inline=False)
    if story_b:
        embed.add_field(name='📖 Story B', value=story_b[:200], inline=False)
    
    footer = f'Searched by: {search_type} | Matches: {len(results)} | Source: {current_source["name"]}'
    embed.set_footer(text=footer)
    
    if len(results) > 1:
        additional = [f"• {r.get('in_season_episode', '?')}: {r.get('story_a', '?')[:40]}" for r in results[1:5]]
        embed.add_field(name=f'➕ More Matches ({len(results)-1})', value='\n'.join(additional), inline=False)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='jp', description='Lookup by Japanese Story Number')
@app_commands.describe(jp_number='Japanese Story Number (e.g., 150, 150 S, or S16)')
async def jp_cmd(interaction, jp_number: str):
    results = []
    q = jp_number.strip()
    
    standalone_match = re.match(r'^s(\d+)$', q, re.I)
    reg_special_match = re.match(r'^(\d+)\s*s$', q, re.I)
    
    for ep in EPISODES:
        for jp in ep.get('jp_parsed', []):
            if standalone_match:
                if jp.get('is_standalone_special') and jp.get('number') == standalone_match.group(1):
                    results.append(ep)
                    break
            elif reg_special_match:
                target_num = reg_special_match.group(1)
                if (not jp.get('is_standalone_special') and
                        jp.get('is_special') and
                        jp.get('number') == target_num):
                    results.append(ep)
                    break
            elif q.isdigit():
                if jp.get('number') == q:
                    results.append(ep)
                    break
    
    if not results:
        embed = Embed(
            title="❌ Not Found",
            description=f'No episode found for JP Story #{jp_number}\n\n'
            f'**Current Source:** {current_source["name"]}',
            color=0xcc0000
        )
        await interaction.response.send_message(embed=embed)
        return
    
    first = results[0]
    embed = Embed(title=f"🇯🇵 JP Story #{jp_number}", color=0x6d4aff)
    embed.add_field(name='🇮🇳 IN Episode', value=first.get('in_season_episode', 'N/A'), inline=True)
    embed.add_field(name='Category', value=first.get('category', 'unknown').replace('_', ' ').title(), inline=True)
    if first.get('alt_in_episode'):
        embed.add_field(name='🔢 Alt IN', value=first.get('alt_in_episode'), inline=True)
    embed.add_field(name='📖 Story A', value=first.get('story_a', 'Unknown')[:200], inline=False)
    if first.get('story_b'):
        embed.add_field(name='📖 Story B', value=first.get('story_b')[:200], inline=False)
    embed.add_field(name='All JP Stories', value=first.get('jp_story_all', '-'), inline=False)
    embed.set_footer(text=f'Matches: {len(results)} | Source: {current_source["name"]}')
    
    if len(results) > 1:
        additional = [f"• {r.get('in_season_episode', '?')}: {r.get('story_a', '?')[:40]}" for r in results[1:5]]
        embed.add_field(name=f'➕ More Matches ({len(results)-1})', value='\n'.join(additional), inline=False)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='list', description='List episodes with pagination')
@app_commands.describe(page='Page number (1-999)', season='Filter by season (1-10) or "special" or "classic"')
async def list_cmd(interaction, page: int = 1, season: str = None):
    filtered = EPISODES
    
    if season:
        s = season.lower()
        if s == 'special':
            filtered = [e for e in EPISODES if e.get('category') == 'special_episodes']
        elif s == 'classic':
            filtered = [e for e in EPISODES if e.get('category') == 'classic_doraemon']
        elif s.isdigit():
            filtered = [e for e in EPISODES if e.get('category') == f'season_{s}']
    
    PAGE_SIZE = 10
    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    
    if start >= len(filtered):
        embed = Embed(
            title="❌ Page Not Found", 
            description=f'Page {page} does not exist.\n**Current Source:** {current_source["name"]}', 
            color=0xcc0000
        )
        await interaction.response.send_message(embed=embed)
        return
    
    chunk = filtered[start:end]
    season_display = season if season else 'All'
    embed = Embed(title=f'Doraemon Episodes - Page {page} ({season_display})', color=0x6d4aff)
    embed.description = f'Total: **{len(filtered)}** | Showing {start + 1}-{min(end, len(filtered))}'
    
    for r in chunk:
        in_ep = r.get('in_season_episode', 'N/A')
        jp_st = r.get('jp_story_all', '-') or '-'
        story_a = r.get('story_a', 'Unknown')[:40]
        story_b = r.get('story_b', '')
        title_display = story_a
        if story_b:
            title_display += f' / {story_b[:40]}'
        embed.add_field(name=f'{in_ep} | JP: {jp_st}', value=title_display, inline=False)
    
    total_pages = (len(filtered) // PAGE_SIZE) + 1
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
    
    embed = Embed(title='Database Statistics', color=0x6d4aff)
    embed.description = (
        f'**Total Episodes:** {len(EPISODES)}\n'
        f'**JP Indexed:** {with_jp}\n'
        f'**JP Special Stories (S suffix):** {jp_specials}\n'
        f'**JP Standalone Specials (S##):** {jp_standalone}\n\n'
        f'**Source:** {current_source["name"]} ({current_source["type"].title()})\n'
        f'**URL:** {current_source["url"]}\n\n'
        f'**Breakdown:**'
    )
    for cat, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        embed.add_field(name=cat.replace('_', ' ').title(), value=f'**{cnt}**', inline=True)
    
    if current_source['type'] == 'csv' and active_plugin:
        stats = active_plugin.get_statistics()
        embed.add_field(name='CSV Columns Defined', value=str(stats.get('columns_defined', 0)), inline=True)
    
    embed.set_footer(text=f'Generated: {DB.get("metadata", {}).get("generated_at", "unknown")}')
    
    await interaction.response.send_message(embed=embed)

# ============================================
# CSV-Specific Commands (NEW!)
# ============================================
@bot.tree.command(name='search_jp_title', description='Search by Japanese title (CSV only)')
@app_commands.describe(query='Japanese title or keyword')
async def search_jp_title_cmd(interaction, query: str):
    """Search stories by Japanese title - CSV source only"""
    is_available, error_embed = check_csv_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return
    
    results = active_plugin.search_jp(query)
    
    if not results:
        embed = Embed(
            title="❌ No Results Found",
            description=f'No stories found for Japanese title "**{query}**"',
            color=0xcc0000
        )
        await interaction.response.send_message(embed=embed)
        return
    
    first = results[0]
    embed = active_plugin.create_search_embed(first, source_name=current_source['name'])
    embed.title = f"🇯🇵 JP Title Match: {first.get('English title', 'Unknown')[:70]}"
    
    if len(results) > 1:
        additional = [f"• {r.get('Publication date', '?')}: {r.get('English title', '?')[:40]}" for r in results[1:5]]
        embed.add_field(name=f'➕ More Matches ({len(results)-1})', value='\n'.join(additional), inline=False)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name='search_magazine', description='Search by magazine code (CSV only)')
@app_commands.describe(code='Magazine code (e.g., YK, KG, G1, G2, SD, TK, CC)')
async def search_magazine_cmd(interaction, code: str):
    """Search stories by magazine code - CSV source only"""
    is_available, error_embed = check_csv_source()
    if not is_available:
        await interaction.response.send_message(embed=error_embed)
        return
    
    results = active_plugin.parser.search_by_magazine(code.upper())
    
    if not results:
        embed = Embed(
            title="❌ No Results Found",
            description=f'No stories found for magazine code "**{code.upper()}**"',
            color=0xcc0000
        )
        await interaction.response.send_message(embed=embed)
        return
    
    embed = Embed(
        title=f"📰 Magazine: {code.upper()}",
        color=0x6d4aff
    )
    embed.description = f"Found **{len(results)}** stories from this magazine"
    
    chunk = results[:10]
    for story in chunk:
        title = story.get('English title', 'Unknown')[:50]
        date = story.get('Publication date', '?')
        desc = f"**{date}** — {title}"
        embed.add_field(name=desc, value="", inline=False)
    
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
    
    results = active_plugin.search_anime_1979(episode)
    
    if not results:
        embed = Embed(
            title="❌ No Results Found",
            description=f'No stories adapted in 1979 anime episode "**{episode}**"',
            color=0xcc0000
        )
        await interaction.response.send_message(embed=embed)
        return
    
    embed = Embed(
        title=f"📺 1979 Anime Ep {episode}",
        color=0x6d4aff
    )
    embed.description = f"Found **{len(results)}** story/stories adapted in this episode"
    
    chunk = results[:10]
    for story in chunk:
        title = story.get('English title', 'Unknown')[:50]
        jp = story.get('Japanese title', '')[:30]
        desc = f"• {title}"
        if jp:
            desc += f" ({jp})"
        embed.add_field(name=desc, value="", inline=False)
    
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
    
    results = active_plugin.search_anime_2005(episode)
    
    if not results:
        embed = Embed(
            title="❌ No Results Found",
            description=f'No stories adapted in 2005 anime episode "**{episode}**"',
            color=0xcc0000
        )
        await interaction.response.send_message(embed=embed)
        return
    
    embed = Embed(
        title=f"📺 2005 Anime Ep {episode}",
        color=0x6d4aff
    )
    embed.description = f"Found **{len(results)}** story/stories adapted in this episode"
    
    chunk = results[:10]
    for story in chunk:
        title = story.get('English title', 'Unknown')[:50]
        jp = story.get('Japanese title', '')[:30]
        desc = f"• {title}"
        if jp:
            desc += f" ({jp})"
        embed.add_field(name=desc, value="", inline=False)
    
    if len(results) > 10:
        embed.set_footer(text=f'Showing 10 of {len(results)} results')
    
    await interaction.response.send_message(embed=embed)

# ============================================
# Help Command
# ============================================
@bot.tree.command(name='help', description='Show available commands')
async def help_cmd(interaction):
    embed = Embed(title='🤖 Doraemon Search Bot Help', color=0x6d4aff)
    
    csv_note = ""
    if current_source['type'] == 'csv':
        csv_note = "**(All CSV features enabled)**\n\n"
    
    embed.description = f'''
**Available Commands:**

📚 **Core Commands** (Work on all sources)
• `/search <query>` - Search by JP#, IN#, SPE, CE, or title
• `/jp <number>` - Lookup by Japanese Story Number
• `/list [page] [season]` - Browse episodes with pagination
• `/stats` - Show database statistics

🔧 **Source Commands** (Manage database sources)
• `/source` - Shows current database source
• `/source all` - Lists all available sources
• `/source change <id>` - Switch database source (1=JSON, 2=CSV)

📋 **CSV-Only Commands** (Requires CSV source)
{csv_note}• `/search_jp_title <keyword>` - Search by Japanese title
• `/search_magazine <code>` - Search by magazine code (YK, KG, G1, etc.)
• `/search_1979 <ep>` - Search by 1979 anime episode number
• `/search_2005 <ep>` - Search by 2005 anime episode number

❓ **Info**
• `/help` - Show this message

**Current Source:** {current_source["name"]}
**Total Records:** {len(EPISODES)}

**JP Story Number Formats:**
• `1313` - Regular story #1313
• `1313 S` - Story #1313 marked as special
• `S16` - Standalone special #16 (S01-S30)

**Search Examples:**
• `/search 150` - Find JP Story #150
• `/search 150 S` - Find ONLY the special variant
• `/search S16` - Find JP Standalone Special #16
• `/search s04e35` - Find IN Episode S04E35
• `/search nobita` - Search by story title

**CSV Examples:**
• `/search_jp_title のび太` - Search by Japanese title
• `/search_magazine G1` - Find stories from Weekly Shonen Magazine G1
• `/search_1979 1234` - Find stories in 1979 anime episode 1234
• `/search_2005 567` - Find stories in 2005 anime episode 567
'''
    embed.set_footer(text='🇯🇵 ↔ 🇮🇳 Bidirectional Cross-Reference • CSV Features Available')
    await interaction.response.send_message(embed=embed)

# ============================================
# Event Handlers
# ============================================
@bot.event
async def on_ready():
    print(f'✅ Logged in as {bot.user}')
    synced = await bot.tree.sync()
    print(f'✅ Synced {len(synced)} GLOBAL commands')
    if bot.guilds:
        print(f'Bot is in {len(bot.guilds)} guild(s)')
    
    # Load initial data
    print('\n🔄 Loading initial database...')
    print(f'   Default source: {current_source["name"]} ({current_source["type"]})')
    
    success = await reload_current_source()
    if not success:
        print('⚠️ Initial database load failed. Some commands may not work.')
        print('   If using JSON: Check that database/search_index.json exists')
        print('   If using CSV: Clone Doraemon-CSV-DB first')
    else:
        print(f'✅ Database loaded successfully ({len(EPISODES)} records)')
    
    # Check for CSV-specific commands availability
    print(f'\n📊 Available Commands:')
    print(f'   Core commands: ✅')
    print(f'   Source commands: ✅')
    print(f'   CSV-specific commands: {"✅" if current_source["type"] == "csv" else "⏸️ (enable with /source change 2)"}')

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send('❌ Command not found. Use `/help` to see available commands.')
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send('❌ Missing required argument. Use `/help` for usage information.')
    elif isinstance(error, app_commands.errors.CommandInvokeError):
        print(f'Command invoke error: {error}')
        await ctx.send('❌ An internal error occurred. Please try again.')
    else:
        print(f'Error: {error}')

# ============================================
# Main Execution
# ============================================
if __name__ == '__main__':
    bot.run(TOKEN)
