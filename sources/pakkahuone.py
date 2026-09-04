import requests
from bs4 import BeautifulSoup
from datetime import datetime
import re
import urllib3

# Disable SSL warnings for certificate mismatch
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://tullikamari.fi"

def parse_finnish_date(date_str):
    """Parse Finnish date format like 'To29/10/2026' to YYYY-MM-DD"""
    # Remove weekday prefix (To, Pe, La, Su, Ma, Ti, Ke)
    clean_date = re.sub(r'^[A-Za-zäö]+', '', date_str)
    try:
        dt = datetime.strptime(clean_date, "%d/%m/%Y")
        return dt.strftime("%Y-%m-%d"), dt
    except ValueError:
        return None, None

def scrape():
    events = []
    
    try:
        # Fetch the main events page - verify=False to handle cert issues
        response = requests.get(f"{BASE_URL}/ohjelma/", timeout=10, verify=False)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Find all event feed items
        feed_items = soup.find_all(class_='event-feed-item')
        
        for item in feed_items:
            try:
                # Get all text lines from the event
                text_lines = [t.strip() for t in item.get_text().split('\n') if t.strip()]
                
                if len(text_lines) < 4:
                    continue
                
                # First line is date (e.g., "To29/10/2026")
                date_str = text_lines[0]
                event_date, dt_obj = parse_finnish_date(date_str)
                if not event_date:
                    continue
                
                # Second line is title
                title = text_lines[1]
                
                # Third line is artist/description
                description = text_lines[2] if len(text_lines) > 2 else ""
                
                # Fourth line is time
                time_str = text_lines[3] if len(text_lines) > 3 else "20:00"
                
                # Fifth line is venue
                venue = text_lines[4] if len(text_lines) > 4 else "Tullikamari"
                if venue.lower() in ['k18', 'ovet', 'alk.', 'lue', 'lisää']:
                    venue = "Tullikamari"
                
                # Find link
                link_elem = item.find('a', href=True)
                event_url = link_elem['href'] if link_elem else None
                
                # Price info (look for "alk." or "€")
                price = None
                for line in text_lines[5:]:
                    if '€' in line or 'vapaapääsy' in line.lower():
                        price = line
                        break
                
                # Genre guessing
                genre = "rock"
                title_lower = (title + " " + description).lower()
                if "jazz" in title_lower:
                    genre = "jazz"
                elif "metal" in title_lower or "death" in title_lower or "black" in title_lower:
                    genre = "metal"
                elif "electro" in title_lower or "techno" in title_lower or "dj" in title_lower:
                    genre = "electronic"
                elif "punk" in title_lower:
                    genre = "punk"
                elif "klassinen" in title_lower or "classical" in title_lower:
                    genre = "classical"

                events.append({
                    "title": title,
                    "date": event_date,
                    "time": time_str,
                    "venue": venue,
                    "price": price,
                    "url": event_url,
                    "source": "tullikamari",
                    "genre": genre,
                    "description": f"{title} - {description}" if description else title
                })
                
            except Exception as e:
                continue
                
    except Exception as e:
        print(f"Error scraping Tullikamari/Pakkahuone: {e}")
        
    return events

if __name__ == "__main__":
    events = scrape()
    print(f"Found {len(events)} events from Tullikamari")
    for e in events[:5]:
        print(f"  {e['date']} {e['time']} - {e['title']} @ {e['venue']}")
