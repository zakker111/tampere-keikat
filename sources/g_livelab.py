"""
G Livelab Tampere Scraper
Scrapes events from https://www.g-livelab.fi/tampere
One of Tampere's biggest live music venues
"""

import requests
from bs4 import BeautifulSoup
import json
import re
from datetime import datetime
from typing import List, Dict, Optional
import logging

logger = logging.getLogger(__name__)

BASE_URL = "https://www.g-livelab.fi"
TAMPERE_URL = f"{BASE_URL}/tampere"

def get_tampere_event_links(soup):
    """Extract event links specifically from Tampere page"""
    event_links = []
    
    # Find the events tab section
    events_div = soup.find('div', attrs={'data-tab': 'events'})
    if not events_div:
        logger.warning("Could not find events tab section")
        return event_links
    
    # Find all event links in the listing
    for link in events_div.find_all('a', href=True):
        href = link['href']
        # Look for tampere/events/ pattern (exclude app store links etc)
        if '/tampere/events/' in href:
            event_links.append(href)
    
    # Remove duplicates
    return list(set(event_links))

def parse_finnish_date(date_str: str) -> Optional[datetime]:
    """Parse Finnish date format like '4.9.2026 20:00'"""
    try:
        # Remove any extra text
        date_str = date_str.strip()
        # Try Finnish format: d.m.yyyy HH:MM
        return datetime.strptime(date_str, "%d.%m.%Y %H:%M")
    except ValueError:
        try:
            # Try without time
            return datetime.strptime(date_str, "%d.%m.%Y")
        except ValueError:
            logger.warning(f"Could not parse date: {date_str}")
            return None

def guess_genre(title: str, description: str = "") -> str:
    """Guess genre based on keywords"""
    text = (title + " " + description).lower()
    
    if any(word in text for word in ["rock", "metal", "punk", "hardcore"]):
        return "rock"
    elif any(word in text for word in ["jazz", "blues"]):
        return "jazz"
    elif any(word in text for word in ["electronic", "techno", "house", "dj"]):
        return "electronic"
    elif any(word in text for word in ["pop", "indie"]):
        return "pop"
    elif any(word in text for word in ["hip hop", "rap"]):
        return "hiphop"
    elif any(word in text for word in ["folk", "acoustic"]):
        return "folk"
    else:
        return "general"

def scrape() -> List[Dict]:
    """Scrape events from G Livelab Tampere"""
    events = []
    
    try:
        # Fetch main page
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "fi-FI,fi;q=0.9,en;q=0.8",
        }
        
        response = requests.get(TAMPERE_URL, headers=headers, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Get Tampere-specific event links
        event_links = get_tampere_event_links(soup)
        
        logger.info(f"Found {len(event_links)} Tampere event links on G Livelab")
        
        # Scrape each event page
        for event_url in event_links:
            try:
                event_response = requests.get(event_url, headers=headers, timeout=10)
                event_response.raise_for_status()
                
                event_soup = BeautifulSoup(event_response.text, 'html.parser')
                
                # Extract title from meta tag or h1
                title = None
                og_title = event_soup.find('meta', property='og:title')
                if og_title and og_title.get('content'):
                    title = og_title['content'].replace(' - G Livelab', '').strip()
                
                if not title:
                    h1 = event_soup.find('h1')
                    if h1:
                        title = h1.get_text().strip()
                
                if not title:
                    logger.warning(f"No title found for {event_url}")
                    continue
                
                # Extract date/time from meta tags
                start_date = None
                showtime_meta = event_soup.find('meta', property='article:modified_time')
                
                # Look for itemprop startDate
                start_meta = event_soup.find('span', itemprop='startDate')
                if start_meta and start_meta.get('content'):
                    try:
                        # Format: 2026-09-04T20:00:00+03:00
                        start_date = datetime.fromisoformat(start_meta['content'].replace('+03:00', ''))
                    except:
                        pass
                
                # Alternative: look in data-ticket JSON
                if not start_date:
                    ticket_div = event_soup.find('div', class_='ticket', attrs={'data-ticket': True})
                    if ticket_div:
                        try:
                            ticket_data = json.loads(ticket_div['data-ticket'].replace('&quot;', '"'))
                            if '_event' in ticket_data and 'startDate' in ticket_data['_event']:
                                finnish_date = ticket_data['_event']['startDate']
                                start_date = parse_finnish_date(finnish_date)
                        except:
                            pass
                
                if not start_date:
                    logger.warning(f"No date found for {event_url}")
                    continue
                
                # Extract price
                price = None
                price_span = event_soup.find('span', class_='prices')
                if price_span:
                    price_text = price_span.get_text().strip()
                    # Extract number from "23 €" or similar
                    price_match = re.search(r'(\d+)\s*€', price_text)
                    if price_match:
                        price = int(price_match.group(1))
                
                # Alternative: from data-ticket JSON
                if price is None:
                    ticket_div = event_soup.find('div', class_='ticket', attrs={'data-ticket': True})
                    if ticket_div:
                        try:
                            ticket_data = json.loads(ticket_div['data-ticket'].replace('&quot;', '"'))
                            if 'ticketPrice' in ticket_data:
                                price = ticket_data['ticketPrice'] / 100  # Convert cents to euros
                        except:
                            pass
                
                # Check if sold out
                sold_out = bool(event_soup.find('div', class_='cart-sold-out'))
                
                # Extract venue from location itemprop
                venue = None
                location_span = event_soup.find('span', itemprop='location')
                if location_span:
                    location_text = location_span.get_text()
                    if 'Tampere' in location_text:
                        venue = "G Livelab Tampere"
                    elif 'Helsinki' in location_text or 'Yrjönkatu' in location_text:
                        venue = "G Livelab Helsinki"
                    else:
                        venue = "G Livelab"
                
                # Skip if not Tampere venue
                if venue != "G Livelab Tampere":
                    logger.debug(f"Skipping non-Tampere event: {title} at {venue}")
                    continue
                
                logger.debug(f"Processing Tampere event: {title}")
                
                # Extract description
                description = ""
                content_div = event_soup.find('div', class_='content')
                if content_div:
                    # Get first paragraph or so
                    paragraphs = content_div.find_all('p')
                    if paragraphs:
                        description = paragraphs[0].get_text().strip()[:500]
                
                # Guess genre
                genre = guess_genre(title, description)
                
                event = {
                    "title": title,
                    "date": start_date.strftime("%Y-%m-%d"),
                    "time": start_date.strftime("%H:%M"),
                    "venue": venue,
                    "price": f"{price}€" if price else ("Sold Out" if sold_out else "Unknown"),
                    "url": event_url,
                    "source": "g_livelab",
                    "description": description,
                    "genre": genre
                }
                
                events.append(event)
                logger.debug(f"Parsed event: {title} on {start_date.date()}")
                
            except Exception as e:
                logger.error(f"Error parsing event {event_url}: {e}")
                continue
        
        logger.info(f"Successfully scraped {len(events)} events from G Livelab Tampere")
        
    except Exception as e:
        logger.error(f"Error scraping G Livelab Tampere: {e}")
    
    return events

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    events = scrape()
    print(f"Found {len(events)} events")
    for event in events[:5]:
        print(f"  - {event['title']} ({event['date']})")
