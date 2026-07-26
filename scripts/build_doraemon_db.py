#!/usr/bin/env python3
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import json, os, re
from datetime import datetime

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
                    in_ep = cells[1].get_text(strip=True).upper()
                    alt_in_ep = cells[2].get_text(strip=True)
                    title = cells[3].get_text(strip=True)
                    if not alt_in_ep or alt_in_ep.strip() in ['———', '-', '', '—']:
                        alt_in_ep = None
                else:
                    in_ep = cells[1].get_text(strip=True).upper()
                    alt_in_ep = None
                    title = cells[2].get_text(strip=True)
                
                jp_raw_clean = re.sub(r'[^0-9\s/]', '', jp_raw)
                jp_numbers = re.findall(r'\d+', jp_raw_clean)
                jp_primary = jp_numbers[0] if jp_numbers else None
                
                story_parts = [s.strip() for s in title.split('/') if s.strip()]
                story_a = story_parts[0] if story_parts else title
                story_b = story_parts[1] if len(story_parts) > 1 else None
                
                search_terms = [story_a, story_b if story_b else '', in_ep, section] + jp_numbers
                search_blob = ' '.join(str(x) for x in search_terms if x)
                
                episode_entry = {
                    'raw_episode': in_ep,
                    'in_season_episode': in_ep,
                    'alt_in_episode': alt_in_ep,
                    'jp_story_numbers': jp_numbers,
                    'jp_story_primary': jp_primary,
                    'jp_story_all': jp_raw_clean,
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
                print(f"  ✓ {in_ep} | JP: {jp_primary}{alt_str} | {story_a[:35]}")
        
        browser.close()
        return episodes

def main():
    episodes = scrape_with_playwright()
    
    print(f"\n{'='*50}")
    print(f"✅ SCRAPE COMPLETE: {len(episodes)} episodes")
    
    categories = {}
    alt_count = 0
    for ep in episodes:
        cat = ep.get('category', 'unknown')
        categories[cat] = categories.get(cat, 0) + 1
        if ep.get('alt_in_episode'):
            alt_count += 1
    
    print("\n📊 Category Distribution:")
    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        print(f"  {cat}: {count}")
    
    print(f"\n🔢 Specials with Alt IN Numbers: {alt_count}")
    
    os.makedirs('database', exist_ok=True)
    
    search_index = {'items': episodes, 'metadata': {'total': len(episodes), 'generated_at': datetime.now().isoformat(), 'source': 'doraemon-hindi-1979.netlify.app', 'specials_with_alt_in': alt_count}}
    with open('database/search_index.json', 'w', encoding='utf-8') as f:
        json.dump(search_index, f, indent=2, ensure_ascii=False)
    
    with open('database/episodes.json', 'w', encoding='utf-8') as f:
        json.dump({'episodes': episodes, 'metadata': {'total': len(episodes), 'generated_at': datetime.now().isoformat()}}, f, indent=2, ensure_ascii=False)
    
    with open('database/metadata.json', 'w', encoding='utf-8') as f:
        json.dump({'total_episodes': len(episodes), 'categories': list(categories.keys()), 'generated_at': datetime.now().isoformat()}, f, indent=2)
    
    with open('database/summary.json', 'w', encoding='utf-8') as f:
        json.dump({'total_episodes': len(episodes), 'category_breakdown': categories, 'generated_at': datetime.now().isoformat()}, f, indent=2)
    
    with open('database/manga.json', 'w', encoding='utf-8') as f:
        json.dump({'metadata': {'source': 'Google Sheets', 'generated_at': datetime.now().isoformat()}, 'items': [], 'episode_count': len(episodes)}, f, indent=2)
    
    print("\n💾 Saved to database/")
    print("✅ Database build complete!")

if __name__ == '__main__':
    main()
