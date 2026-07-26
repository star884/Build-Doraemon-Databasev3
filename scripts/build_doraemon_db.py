#!/usr/bin/env python3
"""
Doraemon Database Builder
Fetches JSON data from API endpoint and generates database files.
Runs in GitHub Actions - does NOT start Discord bot.

Location: Build-Doraemon-Databasev3/scripts/build_doraemon_db.py
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

# Try to import httpx
try:
    import httpx
except ImportError:
    print("❌ httpx not installed. Run: pip install httpx")
    sys.exit(1)

# Configuration
DEFAULT_URL = "https://doraemon-hindi-1979.netlify.app"
DATABASE_DIR = Path("database")
OUTPUT_FILES = {
    "search_index": "search_index.json",
    "episodes": "episodes.json",
    "manga": "manga.json",
    "summary": "summary.json",
    "metadata": "metadata.json"
}

# API Endpoints (customize if your site has different paths)
API_ENDPOINTS = {
    "episodes": "/api/episodes",
    "manga": "/api/manga",
    "summary": "/api/summary"
}

# Ensure database directory exists
DATABASE_DIR.mkdir(exist_ok=True)

def log(level: str, message: str):
    """Print formatted log message"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level:8}] {message}")

class DoraemonAPI:
    """
    Fetches Doraemon database data from API endpoints.
    Uses httpx for async HTTP requests.
    """
    
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip('/')
        self.episodes: List[Dict[str, Any]] = []
        self.manga_details: List[Dict[str, Any]] = []
        self.summary_data: Dict[str, Any] = {}
        self.timeout = 30  # seconds
        
    async def fetch_endpoint(self, endpoint: str) -> Optional[Any]:
        """Fetch data from API endpoint"""
        url = f"{self.base_url}{endpoint}"
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url)
                response.raise_for_status()
                log("INFO", f"Fetched: {url} ({response.status_code})")
                return response.json()
        except httpx.HTTPStatusError as e:
            log("ERROR", f"HTTP error fetching {endpoint}: {e}")
            return None
        except httpx.TimeoutException:
            log("ERROR", f"Timeout fetching {endpoint}")
            return None
        except Exception as e:
            log("ERROR", f"Error fetching {endpoint}: {e}")
            return None
    
    async def fetch_all_data(self) -> bool:
        """Fetch all data from API endpoints"""
        log("INFO", f"Fetching data from: {self.base_url}")
        
        # Fetch episodes
        log("INFO", "Fetching episodes...")
        episodes_data = await self.fetch_endpoint(API_ENDPOINTS["episodes"])
        
        if episodes_data:
            self.episodes = self.process_episodes(episodes_data)
            log("INFO", f"Successfully fetched {len(self.episodes)} episodes")
        else:
            log("WARN", "Failed to fetch episodes - will use empty dataset")
            self.episodes = []
        
        # Fetch manga details
        log("INFO", "Fetching manga details...")
        manga_data = await self.fetch_endpoint(API_ENDPOINTS["manga"])
        
        if manga_data:
            self.manga_details = self.process_manga(manga_data)
            log("INFO", f"Successfully fetched {len(self.manga_details)} manga entries")
        else:
            log("WARN", "Failed to fetch manga - will use empty dataset")
            self.manga_details = []
        
        # Fetch summary (optional)
        log("INFO", "Fetching summary data...")
        summary_data = await self.fetch_endpoint(API_ENDPOINTS["summary"])
        
        if summary_data:
            self.summary_data = summary_data
            log("INFO", "Successfully fetched summary data")
        else:
            log("INFO", "No summary endpoint found - will generate stats")
        
        return len(self.episodes) > 0
    
    def process_episodes(self, data: Any) -> List[Dict[str, Any]]:
        """
        Process episode data from API into standard format.
        Customize this based on your actual API response structure.
        """
        episodes = []
        
        # Handle different possible response formats
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict) and 'items' in data:
            items = data['items']
        elif isinstance(data, dict) and 'episodes' in data:
            items = data['episodes']
        else:
            items = [data] if isinstance(data, dict) else []
        
        for idx, item in enumerate(items):
            episode = self.convert_to_format(item, idx)
            if episode:
                episodes.append(episode)
        
        return episodes
    
    def process_manga(self, data: Any) -> List[Dict[str, Any]]:
        """
        Process manga data from API into standard format.
        """
        details = []
        
        if isinstance(data, list):
            details = data
        elif isinstance(data, dict) and 'details' in data:
            details = data['details']
        
        return details
    
    def convert_to_format(self, item: Dict[str, Any], index: int = 0) -> Optional[Dict[str, Any]]:
        """
        Convert API item to internal episode format.
        Customize field mappings based on your API response.
        """
        try:
            # Extract fields - ADAPT THESE FIELD NAMES TO MATCH YOUR API
            in_episode = item.get('in_season_episode') or item.get('episode') or ""
            story_a = item.get('story_a') or item.get('title') or item.get('storyTitle') or ""
            story_b = item.get('story_b') or item.get('title2') or item.get('storyTitle2') or None
            jp_stories = item.get('jp_story_numbers') or item.get('jpRefs') or []
            category = item.get('category') or item.get('season') or "season_1"
            
            # Parse JP references into structured format
            jp_parsed = []
            jp_story_all = []
            
            for jp_ref in jp_stories:
                if isinstance(jp_ref, str):
                    jp_num = jp_ref.replace('#', '').strip()
                    jp_parsed.append({
                        "number": jp_num,
                        "is_special": jp_ref.endswith(' S') if isinstance(jp_ref, str) else False,
                        "is_standalone_special": jp_ref.upper().startswith('S') and jp_ref[1:].isdigit()
                    })
                    jp_story_all.append(jp_ref)
                elif isinstance(jp_ref, dict):
                    jp_parsed.append({
                        "number": jp_ref.get('number', ''),
                        "is_special": jp_ref.get('is_special', False),
                        "is_standalone_special": jp_ref.get('is_standalone_special', False)
                    })
                    jp_story_all.append(jp_ref.get('full', jp_ref.get('number', '')))
            
            # Build episode record
            episode = {
                "in_season_episode": in_episode,
                "jp_story_numbers": jp_stories,
                "jp_parsed": jp_parsed,
                "jp_story_all": ", ".join(jp_story_all) if jp_story_all else "",
                "story_a": str(story_a)[:200] if story_a else "",
                "story_b": str(story_b)[:200] if story_b else None,
                "category": category,
                "alt_in_episode": item.get('alt_in_episode') or item.get('alternativeEpisode'),
                "title": item.get('title') or story_a or "Unknown",
                "publication_date": item.get('publication_date') or item.get('date') or "",
                "magazine_code": item.get('magazine_code') or "",
                "notes": item.get('notes') or ""
            }
            
            # Skip if no meaningful data
            if not in_episode and not story_a:
                return None
                
            return episode
            
        except Exception as e:
            log("ERROR", f"Error converting episode {index}: {e}")
            return None
    
    def generate_summary(self) -> Dict[str, Any]:
        """Generate summary statistics"""
        categories = {}
        seasons = {}
        jp_count = 0
        total_with_refs = 0
        
        for ep in self.episodes:
            # Count by category
            cat = ep.get("category", "unknown")
            categories[cat] = categories.get(cat, 0) + 1
            
            # Count by season
            season = cat.replace("_", " ").title() if cat != "unknown" else "Unknown"
            seasons[season] = seasons.get(season, 0) + 1
            
            # JP references
            if ep.get("jp_story_numbers"):
                total_with_refs += 1
            if ep.get("jp_parsed"):
                jp_count += len(ep["jp_parsed"])
        
        return {
            "total_episodes": len(self.episodes),
            "categories": categories,
            "seasons": seasons,
            "jp_story_references": jp_count,
            "episodes_with_jp_refs": total_with_refs,
            "fetched_at": datetime.now().isoformat(),
            "source_url": self.base_url
        }
    
    def generate_metadata(self) -> Dict[str, Any]:
        """Generate metadata about the database"""
        return {
            "name": "Doraemon Manga Database",
            "version": "1.0.0",
            "source_url": self.base_url,
            "generated_at": datetime.now().isoformat(),
            "total_items": len(self.episodes),
            "generator": "build_doraemon_db.py (API)",
            "format": "JSON"
        }
    
    def save_database_files(self):
        """Save all JSON database files"""
        log("INFO", "Saving database files...")
        
        # search_index.json (main lookup - used by Discord bot)
        search_index = {
            "metadata": self.generate_metadata(),
            "items": self.episodes
        }
        self.save_json(OUTPUT_FILES["search_index"], search_index)
        
        # episodes.json (episode listing)
        self.save_json(OUTPUT_FILES["episodes"], {
            "episodes": self.episodes,
            "count": len(self.episodes)
        })
        
        # manga.json (manga details)
        self.save_json(OUTPUT_FILES["manga"], {
            "details": self.manga_details,
            "count": len(self.manga_details)
        })
        
        # summary.json (statistics)
        summary = self.summary_data if self.summary_data else self.generate_summary()
        self.save_json(OUTPUT_FILES["summary"], summary)
        
        # metadata.json (standalone metadata)
        self.save_json(OUTPUT_FILES["metadata"], self.generate_metadata())
        
        log("INFO", f"All database files saved to {DATABASE_DIR}/")
    
    def save_json(self, filename: str, data: Any):
        """Save data to JSON file"""
        filepath = DATABASE_DIR / filename
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        log("INFO", f"Saved: {filepath}")
    
    async def run(self):
        """Main execution flow"""
        log("INFO", "=" * 60)
        log("INFO", "Starting Doraemon Database Builder (API)")
        log("INFO", "=" * 60)
        
        try:
            # Fetch all data
            success = await self.fetch_all_data()
            
            # Even if fetch failed, save whatever we have
            self.save_database_files()
            
            # Print summary
            log("INFO", "=" * 60)
            log("INFO", "BUILD SUMMARY")
            log("INFO", "=" * 60)
            log("INFO", f"Total episodes: {len(self.episodes)}")
            log("INFO", f"Total manga details: {len(self.manga_details)}")
            log("INFO", f"Output directory: {DATABASE_DIR.absolute()}")
            log("INFO", "=" * 60)
            
        except KeyboardInterrupt:
            log("WARN", "Build interrupted by user")
        except Exception as e:
            log("ERROR", f"Fatal error: {e}")
            import traceback
            traceback.print_exc()

def main():
    """Entry point"""
    import asyncio
    
    # Get site URL from environment or use default
    site_url = os.getenv('DORAEMON_SITE_URL', DEFAULT_URL)
    
    log("INFO", f"Site URL: {site_url}")
    
    # Create API client and run async
    api = DoraemonAPI(site_url)
    asyncio.run(api.run())
    
    log("INFO", "Build completed!")

if __name__ == '__main__':
    main()
