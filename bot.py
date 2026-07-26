import discord
import json
import os
import re
from discord import app_commands, Embed
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')

bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())

try:
    with open('database/search_index.json') as f:
        DB = json.load(f)
    EPISODES = DB.get('items', [])
    print(f'✅ Loaded {len(EPISODES)} episodes from database')
    print(f'  Episodes with JP refs: {sum(1 for e in EPISODES if e.get("jp_story_numbers"))}')
except FileNotFoundError:
    EPISODES = []
    print('❌ Database file not found')


@bot.tree.command(name='search', description='Advanced search by JP story #, IN episode, SPE, or CE')
@app_commands.describe(query='Search term (e.g., 150, 150 S, S16, s04e35, spe01, ce1, nobita)')
async def search_cmd(interaction, query: str):
    q = query.lower().strip()
    results = []
    search_type = None

    # ── JP Story Number ──────────────────────────────────────
    # Handles: regular numbers (150), S-suffix specials (150 S),
    # and standalone specials (S16)
    if q.startswith('jp:') or q.isdigit() or re.match(r'^s\d+\s*s?$', q, re.I):
        search_type = 'jp'
        jp_query = q.replace('jp:', '').strip()

        # Case 1: "S16" — standalone special
        standalone_match = re.match(r'^s(\d+)$', jp_query, re.I)
        # Case 2: "150 S" or "150s" — regular number WITH S suffix
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
                    # Plain number — match ANY story with this number (both regular and special)
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
            '• Title: `nobita`',
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

    footer = f'Searched by: {search_type} | Matches: {len(results)}'
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

    # Parse query type
    standalone_match = re.match(r'^s(\d+)$', q, re.I)          # S16
    reg_special_match = re.match(r'^(\d+)\s*s$', q, re.I)      # 150 S or 150S

    for ep in EPISODES:
        for jp in ep.get('jp_parsed', []):
            if standalone_match:
                # Standalone special search (S16, S22, etc.)
                if jp.get('is_standalone_special') and jp.get('number') == standalone_match.group(1):
                    results.append(ep)
                    break
            elif reg_special_match:
                # Regular number WITH S suffix — match only the special variant
                target_num = reg_special_match.group(1)
                if (not jp.get('is_standalone_special') and
                        jp.get('is_special') and
                        jp.get('number') == target_num):
                    results.append(ep)
                    break
            elif q.isdigit():
                # Plain number — match ANY story with this number (both regular and special)
                if jp.get('number') == q:
                    results.append(ep)
                    break

    if not results:
        embed = Embed(
            title="❌ Not Found",
            description=f'No episode found for JP Story #{jp_number}',
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
    embed.set_footer(text=f'Matches: {len(results)}')

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
        embed = Embed(title="❌ Page Not Found", description=f'Page {page} does not exist.', color=0xcc0000)
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
    embed.set_footer(text=f'Page {page} of {total_pages}')

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
        f'**Breakdown:**'
    )
    for cat, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        embed.add_field(name=cat.replace('_', ' ').title(), value=f'**{cnt}**', inline=True)

    embed.set_footer(text=f'Generated: {DB.get("metadata", {}).get("generated_at", "unknown")}')

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name='help', description='Show available commands')
async def help_cmd(interaction):
    embed = Embed(title='🤖 Doraemon Search Bot Help', color=0x6d4aff)
    embed.description = '''
**Available Commands:**
• `/search <query>` - Search by JP#, IN#, SPE, CE, or title
• `/jp <number>` - Lookup by Japanese Story Number
• `/list [page] [season]` - Browse episodes with pagination
• `/stats` - Show database statistics
• `/help` - Show this message

**JP Story Number Formats:**
• `1313` - Regular story #1313
• `1313 S` - Story #1313 marked as special
• `S16` - Standalone special #16 (S01-S30)

**Search Examples:**
• `/search 150` - Find JP Story #150 (returns both regular and S-variant)
• `/search 150 S` - Find ONLY the special variant of JP Story #150
• `/search S16` - Find JP Standalone Special #16
• `/search s04e35` - Find IN Episode S04E35
• `/search spe01` - Find Indian Special Episode 01
• `/search ce1` - Find Classic Episode 1
• `/search nobita` - Search by story title

**JP Lookup Examples:**
• `/jp 1020` - Find all episodes with JP Story #1020 (both `1020` and `1020 S`)
• `/jp 1020 S` - Find ONLY episodes with `1020 S` (special variant)
• `/jp S16` - Find standalone special #16
'''
    embed.set_footer(text='🇯🇵 ↔ 🇮🇳 Bidirectional Cross-Reference')
    await interaction.response.send_message(embed=embed)


@bot.event
async def on_ready():
    print(f'✅ Logged in as {bot.user}')
    synced = await bot.tree.sync()
    print(f'✅ Synced {len(synced)} GLOBAL commands')
    if bot.guilds:
        print(f'Bot is in {len(bot.guilds)} guild(s)')

bot.run(TOKEN)
