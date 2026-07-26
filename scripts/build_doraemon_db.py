#!/usr/bin/env python3
"""
build_database.py - Builds Doraemon episode database using Playwright scraping.
For GitHub Actions CI/CD only - includes DEBUG MODE for troubleshooting.
"""
import json
import os
import re
import unicodedata
from datetime import datetime
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

# Toggle this to True for detailed debug output
DEBUG_MODE = True

def normalize_unicode(text):
    """Normalize unicode characters."""
    normalized = ''.join(
        c if ord(c) < 128 else unicodedata.normalize('NFKC', c)[0] 
        for c in text
    )
    normalized = normalized.replace('ˢ', 'S').replace('ˢ', 'S')
    normalized = re.sub(r'\[S\]|\(S\)|\*|†|‡|⁽ˢ⁾|⁽ˢ⁾', '[S]', normalized)
    normalized = re.sub(r'(\d+)\s*S\s*(?!\d)', r'\1 S', normalized)
    return normalized

def parse_jp_numbers(jp_raw, jp_cell_element=None):
    """Parse JP story references into structured format."""
    normalized = normalize_unicode(jp_raw)
    cleaned = re.sub(r'[^\w\s/]', '', normalized)
    tokens = re.split(r'[\s/\-,]+', cleaned)
    parsed = []
    
    for token in tokens:
        if not token:
            continue
        
        # Method 1: Standalone special - S16, s16
        stand_match = re.match(r'^s[\-\.]?(\d+)$', token, re.I)
        if stand_match:
            parsed.append({
                'number': stand_match.group(1),
                'is_special': False,
                'is_standalone_special': True
            })
            continue
        
        # Method 2: Regular number with S suffix
        regular_match = re.match(r'^(\d+)(?:[\s\.]*[sS]|[S]|$)$', token)
        if regular_match:
            has_s_suffix = bool(re.search(r'[sS]$', token))
            parsed.append({
                'number': regular_match.group(1),
                'is_special': has_s_suffix,
                'is_standalone_special': False
            })
            continue
        
        # Method 3: Plain number
        plain_match = re.match(r'^(\d+)$', token)
        if plain_match:
            parsed.append({
                'number': plain_match.group(1),
                'is_special': False,
                'is_standalone_special': False
            })
    
    # Check HTML markers
    if jp_cell_element:
        special_indicators = (
            jp_cell_element.find(class_=re.compile(r'special|starred')) or
            jp_cell_element.find('sup') or
            jp_cell_element.find('span', string=re.compile(r'\[S\]|\(S\)|\*')) or
            '*' in jp_cell_element.get_text()
        )
        if special_indicators and DEBUG_MODE:
            print(f"      🏷️  Found special marker in cell")
        if special_indicators:
            for p in parsed:
                if not p.get('is_standalone_special'):
                    p['is_special'] = True
    
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

def collect_unique_jp_formats(episodes):
    """Collect unique JP formats for analysis."""
    formats = set()
    for ep in episodes:
        if ep.get('jp_story_all'):
            formats.add(ep['jp_story_all'])
    return sorted(formats)

def analyze_special_markers(all_jp_texts):
    """Analyze what special markers exist in the data."""
    special_patterns = {
        'asterisk': [],
        'bracket_s': [],
        'parenthesis_s': [],
        'superscript': [],
        'uppercase_s_suffix': [],
        'lowercase_s_suffix': [],
        'none': []
    }
    
    for jp_text in all_jp_texts:
        if '*' in jp_text or '★' in jp_text:
            special_patterns['asterisk'].append(jp_text)
        elif '[S]' in jp_text or '[s]' in jp_text:
            special_patterns['bracket_s'].append(jp_text)
        elif '(S)' in jp_text or '(s)' in jp_text:
            special_patterns['parenthesis_s'].append(jp_text)
        elif re.search(r'\d+[ˢˢ]', jp_text):
            special_patterns['superscript'].append(jp_text)
        elif re.search(r'\d+S(?!\d)', jp_text):
            special_patterns['uppercase_s_suffix'].append(jp_text)
        elif re.search(r'\d+s(?!\d)', jp_text):
            special_patterns['lowercase_s_suffix'].append(jp_text)
        else:
            special_patterns['none'].append(jp_text)
    
    return special_patterns

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
        
        # Collect all JP texts for analysis
        all_jp_texts = []
        
        for table_idx, table in enumerate(tables):
            section, is_specials = detect_section_from_heading(soup, table)
            print(f"\n📺 Table {table_idx+1}: {section}")
            
            rows = table.find_all('tr')[1:]
            
            for row in rows:
                cells = row.find_all(['td', 'th'])
                if len(cells) < 3:
                    continue
                
                # Extract JP story references
                jp_cell = cells[0]
                jp_raw = jp_cell.get_text(strip=True)
                all_jp_texts.append(jp_raw)
                
                # DEBUG: Print full HTML for first few rows
                if DEBUG_MODE and table_idx < 2 and list(rows).index(row) < 5:
                    print(f"      📋 Raw JP Text: '{jp_raw}'")
                    print(f"      📋 HTML: {jp_cell.prettify()[:300]}...")
                    print(f"      📋 Parent table class: {table.get('class')}")
                
                jp_parsed = parse_jp_numbers(jp_raw, jp_cell)
                
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
                
                story_parts = [s.strip() for s in title.split('/') if s.strip()]
                story_a = story_parts[0] if story_parts else title
                story_b = story_parts[1] if len(story_parts) > 1 else None
                
                search_terms = [story_a, story_b if story_b else '', in_ep, section]
                search_terms.extend([jp['number'] for jp in jp_parsed])
                search_blob = ' '.join(str(x) for x in search_terms)
                
                episode_entry = {
                    'in_season_episode': in_ep,
                    'alt_in_episode': alt_in_ep,
                    'title': title[:150],
                    'story_a': story_a,
                    'story_b': story_b,
                    'category': section,
                    'jp_parsed': jp_parsed,
                    'jp_story_numbers': [jp['number'] for jp in jp_parsed],
                    'jp_story_all': jp_raw,
                    'jp_has_specials': any(jp.get('is_special') for jp in jp_parsed),
                    'jp_has_standalone_specials': any(jp.get('is_standalone_special') for jp in jp_parsed),
                    'search_blob': search_blob,
                    'has_jp_reference': bool(jp_parsed),
                    'scraped_at': datetime.now().isoformat()
                }
                
                episodes.append(episode_entry)
                
                jp_display = ', '.join(f"{jp['number']}" + (" S" if jp.get('is_special') else "") for jp in jp_parsed[:3])
                alt_str = f" | Alt IN: {alt_in_ep}" if alt_in_ep else ""
                
                if any(jp.get('is_special') for jp in jp_parsed):
                    print(f"  ✓ {in_ep} | JP: {jp_display}(Special){alt_str} | {story_a[:35]}")
                else:
                    print(f"  ✓ {in_ep} | JP: {jp_display}{alt_str} | {story_a[:35]}")
        
        browser.close()
        
        # ANALYZE SPECIAL MARKERS AFTER SCRAPING
        print("\n" + "=" * 50)
        print("🔬 SPECIAL MARKER ANALYSIS")
        print("=" * 50)
        
        special_analysis = analyze_special_markers(all_jp_texts)
        
        for pattern_type, items in special_analysis.items():
            if items:
                print(f"\n{pattern_type.upper()}: {len(items)} occurrences")
                if items[:5]:  # Show sample
                    print(f"  Samples: {', '.join(items[:5])}")
        
        # Show all unique JP formats
        unique_formats = collect_unique_jp_formats(episodes)
        print(f"\n📝 Unique JP Formats ({len(unique_formats)} total):")
        for fmt in unique_formats[:20]:
            print(f"  • {fmt}")
        
        return episodes

def main():
    """Main entry point for database building."""
    print("🔨 Starting Doraemon Database Build...")
    print("=" * 50)
    
    episodes = scrape_with_playwright()
    
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
    
    os.makedirs('database', exist_ok=True)
    
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
    print("✅ Database build complete!")
    print("\n🔧 TIP: Set DEBUG_MODE = True to see HTML structure of JP cells")
    
    return 0

if __name__ == '__main__':
    try:
        exit(main())
    except Exception as e:
        print(f"\n❌ Database build failed: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
