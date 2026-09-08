// Simulate the exact flow of index.html

const FALLBACK_RAW = [
["2026-09-06","15:00","Test Event","Venue","rock",0,"http://test.com"],
["2026-09-07","18:00","Another Event","Venue2","pop",0,"http://test2.com"]
];

let EVENTS = [];
let calYear = 2026, calMonth = 8; // September
let selectedDay = null;

function toEvents(raw){
  return raw.map((r,i)=>({id:i,date:r[0],time:r[1],title:r[2],venue:r[3],genre:r[4],free:!!r[5],url:r[6]}));
}

async function loadData(){
  try{
    // Simulating fetch failure - will use fallback
    throw new Error('simulated fetch failure');
  } catch(err){
    console.log('Using fallback data...');
    EVENTS = toEvents(FALLBACK_RAW);
  }
  console.log('EVENTS loaded:', EVENTS.length, 'events');
  console.log('First event date:', EVENTS[0].date);
  console.log('calMonth:', calMonth, '(should be 8 for September)');
  console.log('selectedDay:', selectedDay, '(should be null)');
  
  // Simulate renderDayPanel logic
  if(!selectedDay){
    const monthPrefix = `${calYear}-${String(calMonth+1).padStart(2,'0')}-`;
    console.log('monthPrefix:', monthPrefix);
    const monthEvents = EVENTS.filter(e => e.date && e.date.startsWith(monthPrefix));
    console.log('Events matching month prefix:', monthEvents.length);
    monthEvents.forEach(e => console.log('  -', e.date, e.title));
  }
}

loadData();
