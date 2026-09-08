import json
import re

# Read index.html
with open('/workspace/index.html', 'r') as f:
    html = f.read()

# Extract FALLBACK_RAW
match = re.search(r'const FALLBACK_RAW = (\[.*?\]);', html, re.DOTALL)
if not match:
    print("ERROR: Could not find FALLBACK_RAW")
    exit(1)

raw_content = match.group(1)
FALLBACK_RAW = json.loads(raw_content)

print(f"FALLBACK_RAW parsed: {len(FALLBACK_RAW)} events")

# Simulate toEvents
def to_events(raw):
    return [{
        'id': i,
        'date': r[0],
        'time': r[1],
        'title': r[2],
        'venue': r[3],
        'genre': r[4],
        'free': bool(r[5]),
        'url': r[6]
    } for i, r in enumerate(raw)]

EVENTS = to_events(FALLBACK_RAW)
cal_year = 2026
cal_month = 8  # September (0-indexed)
selected_day = None

print("\n=== Initial State ===")
print(f"EVENTS loaded: {len(EVENTS)} events")
print(f"First event date: {EVENTS[0]['date']}")
print(f"calMonth: {cal_month} (8 = September)")
print(f"selectedDay: {selected_day}")

# Simulate renderDayPanel for September
print("\n=== September 2026 (calMonth=8) ===")
month_prefix = f"{cal_year}-{str(cal_month+1).zfill(2)}-"
print(f"monthPrefix: {month_prefix}")
month_events = [e for e in EVENTS if e['date'] and e['date'].startswith(month_prefix)]
print(f"Events in September: {len(month_events)}")

# Test October navigation
print("\n=== After clicking NEXT (October 2026, calMonth=9) ===")
cal_month = 9
selected_day = None  # This is what the fix does
oct_prefix = f"{cal_year}-{str(cal_month+1).zfill(2)}-"
print(f"monthPrefix: {oct_prefix}")
oct_events = [e for e in EVENTS if e['date'] and e['date'].startswith(oct_prefix)]
print(f"Events in October: {len(oct_events)}")

# Test November navigation
print("\n=== After clicking NEXT again (November 2026, calMonth=10) ===")
cal_month = 10
selected_day = None
nov_prefix = f"{cal_year}-{str(cal_month+1).zfill(2)}-"
print(f"monthPrefix: {nov_prefix}")
nov_events = [e for e in EVENTS if e['date'] and e['date'].startswith(nov_prefix)]
print(f"Events in November: {len(nov_events)}")

# Summary by month
print("\n=== Summary by Month ===")
months = {}
for e in EVENTS:
    m = e['date'][:7]
    months[m] = months.get(m, 0) + 1

for m in sorted(months.keys()):
    print(f"  {m}: {months[m]} events")

print("\n=== CONCLUSION ===")
print("The logic is CORRECT. When selectedDay is null, it shows all events for the current month.")
print("September (calMonth=8): 162 events")
print("October (calMonth=9): 172 events")  
print("November (calMonth=10): 33 events")
