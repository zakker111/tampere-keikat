const fs = require('fs');

// Read actual FALLBACK_RAW from index.html
const html = fs.readFileSync('/workspace/index.html', 'utf8');
const match = html.match(/const FALLBACK_RAW = \[(.*?)\];/s);
if (!match) {
    console.error('Could not find FALLBACK_RAW');
    process.exit(1);
}

// Parse the array content
const rawContent = '[' + match[1] + ']';
const FALLBACK_RAW = JSON.parse(rawContent);

console.log('FALLBACK_RAW parsed:', FALLBACK_RAW.length, 'events');

// Simulate toEvents
function toEvents(raw){
  return raw.map((r,i)=>({id:i,date:r[0],time:r[1],title:r[2],venue:r[3],genre:r[4],free:!!r[5],url:r[6]}));
}

let EVENTS = toEvents(FALLBACK_RAW);
let calYear = 2026, calMonth = 8; // September
let selectedDay = null;

console.log('\n=== Initial State ===');
console.log('EVENTS loaded:', EVENTS.length, 'events');
console.log('First event date:', EVENTS[0].date);
console.log('calMonth:', calMonth, '(8 = September)');
console.log('selectedDay:', selectedDay);

// Simulate renderDayPanel for September
console.log('\n=== September 2026 (calMonth=8) ===');
const monthPrefix = `${calYear}-${String(calMonth+1).padStart(2,'0')}-`;
console.log('monthPrefix:', monthPrefix);
const monthEvents = EVENTS.filter(e => e.date && e.date.startsWith(monthPrefix));
console.log('Events in September:', monthEvents.length);

// Test October navigation
console.log('\n=== After clicking NEXT (October 2026, calMonth=9) ===');
calMonth = 9;
selectedDay = null; // This is what the fix does
const octPrefix = `${calYear}-${String(calMonth+1).padStart(2,'0')}-`;
console.log('monthPrefix:', octPrefix);
const octEvents = EVENTS.filter(e => e.date && e.date.startsWith(octPrefix));
console.log('Events in October:', octEvents.length);

// Test November navigation
console.log('\n=== After clicking NEXT again (November 2026, calMonth=10) ===');
calMonth = 10;
selectedDay = null;
const novPrefix = `${calYear}-${String(calMonth+1).padStart(2,'0')}-`;
console.log('monthPrefix:', novPrefix);
const novEvents = EVENTS.filter(e => e.date && e.date.startsWith(novPrefix));
console.log('Events in November:', novEvents.length);

// Summary by month
console.log('\n=== Summary by Month ===');
const months = {};
EVENTS.forEach(e => {
    const m = e.date.substring(0, 7);
    months[m] = (months[m] || 0) + 1;
});
Object.keys(months).sort().forEach(m => {
    console.log(`  ${m}: ${months[m]} events`);
});
