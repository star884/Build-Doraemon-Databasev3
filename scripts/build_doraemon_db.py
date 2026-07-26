#!/usr/bin/env python3
"""
build_database.py - Builds Doraemon episode database using Playwright scraping.
Format: JP story numbers like "1020 S" (with space between number and S).
For GitHub Actions CI/CD only - does NOT run Discord bot.
"""
import json
import os
import re
from datetime import datetime
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

def parse_jp_numbers(jp_raw):
    """
    Parse JP story references into structured format.
    
    Handles formats:
    - "1020" → regular story
    - "1020 S" → special story (with space)
    - "1020S" → special story (no space)
    - "S16" → standalone special
    
    Returns list of dicts:
    [
      {'number': '1020', 'is_special': False, 'is_standalone_special': False},
      {'number': '1020', 'is_special': True, 'is_standalone_special': False},   # Has S suffix
      {'number': '16', 'is_special': False, 'is_standalone_special': True},     # S16
    ]
    """
    # First, check for standalone specials (S16, s16, S-16, s-16)
    standalone_pattern = r'\b[sS][-\.]?(\d+)\b'
    standalone_matches = re.findall(standalone_pattern, jp_raw)
    
    # Then, check for numbered stories with optional S suffix
    # Handle: "1020 S", "1020S", "1020"
    numbered_pattern = r'\b(\d+)(?:[\s\.\-\*]+[sS])?\b'
    numbered_matches = re.findall(numbered_pattern, jp_raw)
    
    parsed = []
    
    # Process standalone specials first
    for num in standalone_matches:
        parsed.append({
            'number': num,
            'is_special': False,
            'is_standalone_special': True
        })
    
    # Process numbered stories (exclude if already captured as standalone)
    # Check if each number has S suffix by looking ahead in the original string
    seen_numbers = set()
    for match in re.finditer(numbered_pattern, jp_raw):
        full_match = match.group(0)  # e.g., "1020 S" or "1020"
        number = match.group(1)      # e.g., "1020"
        
        # Skip if we've already processed this number position
        if number in seen_numbers and number in standalone_matches:
            continue
        
        # Check if there's an S after the number (with optional space/separators)
        has_s_suffix = bool(re.search(r'\d+[\s\.\-\*]*[sS]\b', full_match))
        
        # Only add if not already covered by standalone pattern
        if not re.match(r'^[sS][-\.]?\d+$', full_match, re.I):
            parsed.append({
                'number': number,
                'is_special': has_s_suffix,
                'is_standalone_special': False
            })
            seen_numbers.add(number)
    
    return parsed

def detect_section_from_heading(soup, table):
    """Find section heading before a table."""
    for prev in table.find_all_previous(['h1', 'h2', 'h3']):
        heading = prev.get_text(strip=True).upper()
        if 'SEASON' in heading:
            num = re.search(r'\d+', heading)
            return f'season_{num.group(0)}' if num else 'unknown', False
        elif 'SPECIAL' in heading:
            return 'special_episodes', True
        elif 'CLASSIC' in heading:
            return 'classic_doraemon', False
    return 'unknown', False

def scrape_with_playwright():
    """Scrape episodes from Doraemon website using Playwright."""
    url = os.getenv('DORAEMON_SITE_URL', 'https://doraemon-hindi-1979.netlify.app')
    print(f"🔍 Launching browser: {url}")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until='networkidle', timeout=90000)
        
        import time
        time.sleep(5)
        
        episodes = []
        html_content = page.content()
        soup = BeautifulSoup(html_content, 'html.parser')
        tables = soup.find_all('table')
        
        for table_idx, table in enumerate(tables):
            section, is_specials = detect_section_from_heading(soup, table)
            print(f"\n📺 Table {table_idx+1}: {section}")
            
            rows = table.find_all('tr')[1:]  # Skip header row
            
            for row in rows:
                cells = row.find_all(['td', 'th'])
                if len(cells) < 3:
                    continue
                
                # Extract JP story references
                jp_cell = cells[0]
                jp_raw = jp_cell.get_text(strip=True)
                jp_parsed = parse_jp_numbers(jp_raw)
                
                # DEBUG: Show parsing results for verification
                has_specials = any(jp.get('is_special') for jp in jp_parsed)
                if table_idx < 3 and 'special' in section.lower():
                    print(f"  🧪 Raw: '{jp_raw}' | Parsed: {jp_parsed}")
                
                # Determine Indian episode numbers
                if is_specials and len(cells) >= 4:
                    in_ep = cells[1].get_text(strip=True).upper()
                    alt_in_ep = cells[2].get_text(strip=True)
                    title = cells[3].get_text(strip=True)
                    
                    if not alt_in_ep or alt_in_ep.strip() in ['———', '-', '', '—']:
                        alt_in_ep = None
                else:
                    in_ep = cells[1].get_text(strip=True).upper()
                    alt_in_ep = None
                    title = cells[2].get_text(strip=True)
                
                # Split title into story parts
                story_parts = [s.strip() for s in title.split('/') if s.strip()]
                story_a = story_parts[0] if story_parts else title
                story_b = story_parts[1] if len(story_parts) > 1 else None
                
                # Build searchable blob
                search_terms = [story_a, story_b if story_b else '', in_ep, section]
                search_terms.extend([jp['number'] for jp in jp_parsed])
                search_blob = ' '.join(str(x) for x in search_terms)
                
                episode_entry = {
                    # Core fields for display
                    'in_season_episode': in_ep,
                    'alt_in_episode': alt_in_ep,
                    'title': title[:150],
                    'story_a': story_a,
                    'story_b': story_b,
                    'category': section,
                    
                    # JP references (structured for search)
                    'jp_parsed': jp_parsed,
                    'jp_story_numbers': [jp['number'] for jp in jp_parsed],
                    'jp_story_all': jp_raw,
                    'jp_has_specials': any(jp.get('is_special') for jp in jp_parsed),
                    'jp_has_standalone_specials': any(jp.get('is_standalone_special') for jp in jp_parsed),
                    
                    # Metadata
                    'search_blob': search_blob,
                    'has_jp_reference': bool(jp_parsed),
                    'scraped_at': datetime.now().isoformat()
                }
                
                episodes.append(episode_entry)
                
                # Print progress with special indicator
                jp_display = ', '.join(f"{jp['number']}" + (" S" if jp.get('is_special') else "") for jp in jp_parsed[:3])
                alt_str = f" | Alt IN: {alt_in_ep}" if alt_in_ep else ""
                
                if any(jp.get('is_special') for jp in jp_parsed):
                    print(f"  ✓ {in_ep} | JP: {jp_display}(Special){alt_str} | {story_a[:35]}")
                else:
                    print(f"  ✓ {in_ep} | JP: {jp_display}{alt_str} | {story_a[:35]}")
        
        browser.close()
        return episodes

def main():
    """Main entry point for database building."""
    print("🔨 Starting Doraemon Database Build...")
    print("=" * 50)
    
    # Scrape episodes
    episodes = scrape_with_playwright()
    
    # Statistics
    print("\n" + "=" * 50)
    print(f"✅ SCRAPE COMPLETE: {len(episodes)} episodes")
    
    categories = {}
    with_jp = 0
    jp_specials = 0
    jp_standalone = 0
    
    for ep in episodes:
        cat = ep.get('category', 'unknown')
        categories[cat] = categories.get(cat, 0) + 1
        
        if ep.get('jp_story_numbers'):
            with_jp += 1
        if ep.get('jp_has_specials'):
            jp_specials += 1
        if ep.get('jp_has_standalone_specials'):
            jp_standalone += 1
    
    print("\n📊 Category Distribution:")
    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {count}")
    
    print(f"\n🔢 Episodes with JP refs: {with_jp}")
    print(f"🔢 With JP Specials (S suffix): {jp_specials}")
    print(f"🔢 With Standalone Specials (S##): {jp_standalone}")
    
    # Create database directory
    os.makedirs('database', exist_ok=True)
    
    # Build search index
    search_index = {
        'items': episodes,
        'metadata': {
            'total_episodes': len(episodes),
            'generated_at': datetime.utcnow().isoformat(),
            'source': os.getenv('DORAEMON_SITE_URL', 'unknown'),
            'episodes_with_jp_refs': with_jp,
            'jp_specials_count': jp_specials,
            'jp_standalone_count': jp_standalone
        }
    }
    
    # Save all database files
    with open('database/search_index.json', 'w', encoding='utf-8') as f:
        json.dump(search_index, f, indent=2, ensure_ascii=False)
    
    with open('database/episodes.json', 'w', encoding='utf-8') as f:
        json.dump({'episodes': episodes, 'metadata': {
            'total': len(episodes),
            'generated_at': datetime.utcnow().isoformat()
        }}, f, indent=2, ensure_ascii=False)
    
    with open('database/metadata.json', 'w', encoding='utf-8') as f:
        json.dump({
            'total_episodes': len(episodes),
            'categories': list(categories.keys()),
            'category_breakdown': categories,
            'generated_at': datetime.utcnow().isoformat()
        }, f, indent=2, ensure_ascii=False)
    
    print("\n💾 Saved to database/")
    print("  • search_index.json (main)")
    print("  • episodes.json (full data)")
    print("  • metadata.json (summary)")
    print("✅ Database build complete!")
    
    return 0

if __name__ == '__main__':
    try:
        exit(main())
    except Exception as e:
        print(f"\n❌ Database build failed: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
