"""
Deduplication utilities for event data.
"""
import hashlib
import re
from datetime import datetime
from typing import Any, Dict, List


def normalize_text(text: str) -> str:
    """
    Normalize text for comparison:
    - Lowercase
    - Remove extra whitespace
    - Remove special characters that might vary between sources
    """
    if not text:
        return ""
    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)
    # Remove common punctuation variations that shouldn't affect identity
    text = re.sub(r'[^\w\säöå\-]', '', text)
    return text


def normalize_date(date_str: str) -> str:
    """
    Normalize date string to YYYY-MM-DD format for consistent hashing.
    Handles various input formats.
    """
    if not date_str:
        return ""
    
    # If it's already ISO format, return as is
    if re.match(r'^\d{4}-\d{2}-\d{2}', date_str):
        return date_str[:10]
    
    # Try parsing common formats
    formats = [
        "%d.%m.%Y",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y",
        "%m/%d/%Y",
    ]
    
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str.split('+')[0].split('Z')[0].strip(), fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    
    # Fallback: return original if parsing fails
    return date_str


def generate_event_hash(event: Dict[str, Any]) -> str:
    """
    Generate a unique hash for an event based on key identifying fields.
    
    Uses:
    - Normalized title
    - Normalized date (start time)
    - Normalized venue/name
    - Time (if available, to distinguish multiple events same day same venue)
    """
    title = normalize_text(event.get('title', ''))
    venue = normalize_text(event.get('venue', '') or event.get('name', ''))
    
    # Handle date normalization
    start_time = event.get('start_time', '')
    date_only = normalize_date(start_time)
    
    # Extract time if present (HH:MM)
    time_part = ""
    if start_time and 'T' in start_time:
        time_part = start_time.split('T')[1][:5] if len(start_time.split('T')) > 1 else ""
    elif start_time and ' ' in start_time:
        parts = start_time.split(' ')
        if len(parts) > 1:
            time_part = parts[1][:5]
    
    # Create a unique string representation
    unique_string = f"{title}|{date_only}|{time_part}|{venue}"
    
    # Generate MD5 hash (sufficient for deduplication, not security)
    return hashlib.md5(unique_string.encode('utf-8')).hexdigest()


def merge_event_fields(base_event: Dict[str, Any], duplicate_event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Merge fields from a duplicate event into the base event.
    Prioritizes keeping richer data (e.g., genres, descriptions).
    
    Args:
        base_event: The event being kept
        duplicate_event: The duplicate event to merge data from
        
    Returns:
        Updated base_event with merged data
    """
    # Handle both 'genre' and 'genres' keys for compatibility
    # Prefer 'genre' key as used in the scraper
    base_genres_key = 'genre' if 'genre' in base_event else 'genres'
    dup_genres_key = 'genre' if 'genre' in duplicate_event else 'genres'
    
    base_genres = set(base_event.get(base_genres_key, []))
    dup_genres = set(duplicate_event.get(dup_genres_key, []))
    
    if dup_genres and not base_genres:
        # Base has no genres, take all from duplicate
        base_event[base_genres_key] = sorted(list(dup_genres))
    elif dup_genres and base_genres:
        # Both have genres, merge unique ones
        combined_genres = base_genres.union(dup_genres)
        base_event[base_genres_key] = sorted(list(combined_genres))
    
    # Merge descriptions - prefer longer/more detailed description
    base_desc = base_event.get('description', '') or ''
    dup_desc = duplicate_event.get('description', '') or ''
    
    if not base_desc and dup_desc:
        base_event['description'] = dup_desc
    elif len(dup_desc) > len(base_desc) and dup_desc:
        # Keep longer description if it's significantly more detailed
        base_event['description'] = dup_desc
    
    # Merge URL if base doesn't have one but duplicate does
    if not base_event.get('url') and duplicate_event.get('url'):
        base_event['url'] = duplicate_event['url']
    
    # Merge price info if base doesn't have one but duplicate does
    if not base_event.get('price') and duplicate_event.get('price'):
        base_event['price'] = duplicate_event['price']
    
    return base_event


def deduplicate_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Remove duplicate events from a list, merging valuable data from duplicates.
    
    When duplicates are found, keeps the first occurrence but merges
    missing or richer data (genres, descriptions, URLs) from duplicates.
    
    Args:
        events: List of event dictionaries
        
    Returns:
        List of unique events with merged data
    """
    seen_hashes = {}
    unique_events = []
    duplicates_merged = 0
    
    for event in events:
        event_hash = generate_event_hash(event)
        
        if event_hash not in seen_hashes:
            # First occurrence - keep as is
            seen_hashes[event_hash] = len(unique_events)
            unique_events.append(event)
        else:
            # Duplicate found - merge valuable data
            duplicates_merged += 1
            base_index = seen_hashes[event_hash]
            unique_events[base_index] = merge_event_fields(
                unique_events[base_index], 
                event
            )
    
    if duplicates_merged > 0:
        print(f"Deduplication: Merged data from {duplicates_merged} duplicate events, kept {len(unique_events)} unique events.")
    
    return unique_events
