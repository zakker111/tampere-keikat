import requests
from datetime import datetime
import re

API_URL = "https://www.tampere-talo.fi/wp-json/wp/v2/events"

def scrape():
    events = []
    
    try:
        resp = requests.get(API_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        
        for event in data:
            # Skip if not published
            if event.get('status') != 'publish':
                continue
            
            # Get title (prefer Finnish version)
            title_raw = event.get('title', {}).get('rendered', '')
            # Remove HTML entities
            title = title_raw.replace('&#8211;', '-').replace('&#8217;', "'").strip()
            
            # Get date
            date_str = event.get('date', '')[:10]  # YYYY-MM-DD
            if not date_str:
                continue
            
            # Get link
            link = event.get('link', '')
            
            # Prefer Finnish URLs
            if '/en/' in link:
                # Try to find Finnish version
                finnish_link = link.replace('/en/events/', '/tapahtuma/').replace('/en/', '/')
            else:
                finnish_link = link
            
            # Extract time from content or ACF
            time_str = "19:00"  # Default
            acf = event.get('acf', {})
            
            # Try to find time in content
            content = event.get('content', {}).get('rendered', '')
            time_match = re.search(r'klo\s+(\d{1,2}:\d{2})', content)
            if time_match:
                time_str = time_match.group(1)
            
            # Get venue
            venue = "Tampere-talo"
            
            # Genre guessing
            genre = "classical"
            title_lower = title.lower()
            if "jazz" in title_lower:
                genre = "jazz"
            elif "rock" in title_lower or "pop" in title_lower:
                genre = "rock"
            elif "electro" in title_lower or "techno" in title_lower:
                genre = "electronic"
            elif "klassinen" in title_lower or "classical" in title_lower or "sinfonia" in title_lower:
                genre = "classical"
            elif "teatteri" in title_lower or "theatre" in title_lower:
                genre = "theater"
            elif "lasten" in title_lower or "children" in title_lower:
                genre = "family"
            
            events.append({
                "title": title,
                "date": date_str,
                "time": time_str,
                "venue": venue,
                "price": None,
                "url": finnish_link,
                "source": "tampere_talo",
                "genre": genre,
                "description": f"Tapahtuma Tampere-talossa: {title}"
            })
            
    except Exception as e:
        print(f"Error scraping Tampere-talo: {e}")
    
    return events

if __name__ == "__main__":
    events = scrape()
    print(f"Found {len(events)} events from Tampere-talo")
    for e in events[:5]:
        print(f"  {e['date']} {e['time']} - {e['title']} @ {e['venue']}")
