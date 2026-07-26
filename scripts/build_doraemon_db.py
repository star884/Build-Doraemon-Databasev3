#!/usr/bin/env python3
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import json, os, re
from datetime import datetime

def parse_jp_numbers(jp_raw):
    """
    Parse JP Story Number field properly.
    Formats:
      '1313'          → regular story 1313
      '1313 S'        → story 1313 marked as special
      'S16'           → standalone special #16
      '875 / 736'     → two regular stories
      'S16 / 656'     → standalone special + regular story
      'S22 / 758 S'   → standalone special + regular special
    """
    parts = [p.strip() for p in jp_raw.split('/')]
    parsed = []
    
    for part in parts:
        part = part.strip()
        if not part:
            continue
        
        # Check if it's a standalone special (S16, S30, etc.)
        standalone_match = re.match(r'^S(\d+)$', part, re.I)
        if standalone_match:
            parsed.append({
                'number': standalone_match.group(1),
                'is_standalone_special': True,
                'is_special': True,
                'display': f"S{standalone_match.group(1)}"
            })
            continue
        
        # Check if it's a regular number with optional S suffix (1313, 758 S)
        reg_match = re.match(r'^(\d+)\s*(S?)$', part, re.I)
        if reg_match:
            num = reg_match.group(1)
            has_s = bool(reg_match.group(2))
            parsed.append({
                'number': num,
                'is_standalone_special': False,
                'is_special': has_s,
                'display': f"{num}{' S' if has_s else ''}"
            })
            continue
        
        # Fallback: extract any digits
        digits = re.search(r'(\d+)', part)
        if digits:
            parsed.append({
                'number': digits.group(1),
                'is_standalone_special': False,
                'is_special': 'S' in part.upper(),
                'display': part
            })
    
    return parsed


def scrape_with_playwright():
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
            heading = None
            for prev in table.find_all_previous(['h1', 'h2', 'h3']):
                heading = prev.get_text(strip=True).upper()
                break
            
            section = 'unknown'
            is_specials = False
            
            if heading:
                if 'SEASON' in heading:
                    num = re.search(r'\d+', heading)
                    section = f'season_{num.group(0)}' if num else 'unknown'
                elif 'SPECIAL' in heading:
                    section = 'special_episodes'
                    is_specials = True
                elif 'CLASSIC' in heading:
                    section = 'classic_doraemon'
            
            print(f"\n📺 Table {table_idx+1}: {section}")
            
            rows = table.find_all('tr')[1:]
            
            for row in rows:
                cells = row.find_all(['td', 'th'])
                if len(cells) < 3:
                    continue
                
                jp_raw = cells[0].get_text(strip=True)
                
                if is_specials and len(cells) >= 4:
                    # Specials: Col 0=JP | Col 1=SPE | Col 2=Alt IN | Col 3=Title
                    in_ep = cells[1].get_text(strip=True).upper()
                    alt_in_ep = cells[2].get_text(strip=True)
                    title = cells[3].get_text(strip=True)
                    if not alt_in_ep or alt_in_ep.strip() in ['———', '-', '', '—']:
                        alt_in_ep = None
                else:
                    # Seasons/Classic: Col 0=JP | Col 1=IN | Col 2=Title
                    in_ep = cells[1].get_text(strip=True).upper()
                    alt_in_ep = None
                    title = cells[2].get_text(strip=True)
                
                # Parse JP numbers with special designation
                jp_parsed = parse_jp_numbers(jp_raw)
                
                # Build arrays for compatibility
                jp_numbers = [p['number'] for p in jp_parsed]
                jp_primary = jp_numbers[0] if jp_numbers else None
                jp_display = ' / '.join([p['display'] for p in jp_parsed]) if jp_parsed else jp_raw
                
                # Track special designations
                jp_has_specials = any(p['is_special'] for p in jp_parsed)
                jp_has_standalone = any(p['is_standalone_special'] for p in jp_parsed)
                
                # Parse story parts
                story_parts = [s.strip() for s in title.split('/') if s.strip()]
                story_a = story_parts[0] if story_parts else title
                story_b = story_parts[1] if len(story_parts) > 1 else None
                
                # Build search blob (include S designations for searchability)
                search_terms = list(jp_numbers)
                search_terms.append(jp_display)
                search_terms.extend([story_a, story_b if story_b else '', in_ep, section])
                if alt_in_ep:
                    search_terms.append(alt_in_ep)
                search_blob = ' '.join(str(x) for x in search_terms if x)
                
                episode_entry = {
                    'raw_episode': in_ep,
                    'in_season_episode': in_ep,
                    'alt_in_episode': alt_in_ep,
                    'jp_story_numbers': jp_numbers,
                    'jp_story_primary': jp_primary,
                    'jp_story_all': jp_display,
                    'jp_story_raw': jp_raw,
                    'jp_parsed': jp_parsed,
                    'jp_has_specials': jp_has_specials,
                    'jp_has_standalone_specials': jp_has_standalone,
                    'episode_number': re.search(r'\d+', in_ep).group(0) if re.search(r'\d+', in_ep) else None,
                    'title': title[:150],
                    'story_a': story_a,
                    'story_b': story_b,
                    'category': section,
                    'search_blob': search_blob,
                    'has_jp_reference': bool(jp_numbers),
                    'scraped_at': datetime.now().isoformat()
                }
                episodes.append(episode_entry)
                
                alt_str = f" | Alt IN: {alt_in_ep}" if alt_in_ep else ""
                print(f"  ✓ {in_ep} | JP: {jp_display}{alt_str} | {story_a[:35]}")
        
        browser.close()
        return episodes


def main():
    episodes = scrape_with_playwright()
    
    print(f"\n{'='*50}")
    print(f"✅ SCRAPE COMPLETE: {len(episodes)} episodes")
    
    categories = {}
    alt_count = 0
    jp_special_count = 0
    jp_standalone_count = 0
    
    for ep in episodes:
        cat = ep.get('category', 'unknown')
        categories[cat] = categories.get(cat, 0) + 1
        if ep.get('alt_in_episode'):
            alt_count += 1
        if ep.get('jp_has_specials'):
            jp_special_count += 1
        if ep.get('jp_has_standalone_specials'):
            jp_standalone_count += 1
    
    print("\n📊 Category Distribution:")
    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {count}")
    
    print(f"\n🔢 Specials with Alt IN Numbers: {alt_count}")
    print(f"🇯🇵 Episodes with JP Special stories (S suffix): {jp_special_count}")
    print(f"🇯🇵 Episodes with JP Standalone specials (S##): {jp_standalone_count}")
    
    os.makedirs('database', exist_ok=True)
    
    search_index = {
        'items': episodes,
        'metadata': {
            'total': len(episodes),
            'generated_at': datetime.now().isoformat(),
            'source': 'doraemon-hindi-1979.netlify.app',
            'specials_with_alt_in': alt_count,
            'jp_special_stories': jp_special_count,
            'jp_standalone_specials': jp_standalone_count,
            'note': 'JP Story Numbers: Regular (1313), Special suffix (1313 S), Standalone special (S16). S01-S30 are standalone specials separate from 1787 regular stories.'
        }
    }
    
    with open('database/search_index.json', 'w', encoding='utf-8') as f:
        json.dump(search_index, f, indent=2, ensure_ascii=False)
    
    with open('database/episodes.json', 'w', encoding='utf-8') as f:
        json.dump({'episodes': episodes, 'metadata': {'total': len(episodes), 'generated_at': datetime.now().isoformat()}}, f, indent=2, ensure_ascii=False)
    
    with open('database/metadata.json', 'w', encoding='utf-8') as f:
        json.dump({'total_episodes': len(episodes), 'categories': list(categories.keys()), 'generated_at': datetime.now().isoformat()}, f, indent=2)
    
    with open('database/summary.json', 'w', encoding='utf-8') as f:
        json.dump({'total_episodes': len(episodes), 'category_breakdown': categories, 'jp_special_stories': jp_special_count, 'jp_standalone_specials': jp_standalone_count, 'generated_at': datetime.now().isoformat()}, f, indent=2)
    
    with open('database/manga.json', 'w', encoding='utf-8') as f:
        json.dump({'metadata': {'source': 'Google Sheets', 'generated_at': datetime.now().isoformat()}, 'items': [], 'episode_count': len(episodes)}, f, indent=2)
    
    print("\n💾 Saved to database/")
    print("✅ Database build complete!")


if __name__ == '__main__':
    main()
