import test from 'node:test';
import assert from 'node:assert/strict';
import {
  PERIODS,selectUtcWindow,selectUtcDates,previousUtcWindow,nextUtcWindow,
  createLightningHistoryClient,formatHistoryLabel
} from '../history-window.mjs';

const base='https://raw.githubusercontent.com/Ventusltd/lightning/main/data/history/index.json';
const flash=(time,id)=>({
 type:'Feature',id,geometry:{type:'Point',coordinates:[-2,51]},
 properties:{source_product:'MTG LI L2-LFL',observation_kind:'optical_lightning_flash',time_utc:time}
});
function fixtures(){
 const days=[
  {date:'2026-10-24',path:'days/2026/10/24.json',count:2,first_observation_utc:'2026-10-24T10:00:00Z',
   last_observation_utc:'2026-10-24T23:59:59Z'},
  {date:'2026-10-25',path:'days/2026/10/25.json',count:1,first_observation_utc:'2026-10-25T01:00:00Z',
   last_observation_utc:'2026-10-25T01:00:00Z'}
 ];
 const index={
  schema:'uk-lightning-history-index.v1',generated_utc:'2026-10-26T13:00:00Z',
  observation:'satellite optical lightning flashes, not ground strikes',
  cutoff_date_utc:'2025-10-26',days,archived_observations:3,
  first_observation_utc:'2026-10-24T10:00:00Z',last_observation_utc:'2026-10-25T01:00:00Z',
  availability_note:'Missing archives mean unknown, not no lightning',
  scan_window_note:'Successful EUMETSAT search windows are not a coverage guarantee'
 };
 const body=new Map([
  [base,index],
  [new URL(days[0].path,base).href,{
   schema:'uk-lightning-history-day.v1',type:'FeatureCollection',
   metadata:{date_utc:days[0].date},features:[
    flash('2026-10-24T10:00:00Z','first'),flash('2026-10-24T23:59:59Z','second')]}],
  [new URL(days[1].path,base).href,{
   schema:'uk-lightning-history-day.v1',type:'FeatureCollection',
   metadata:{date_utc:days[1].date},features:[flash('2026-10-25T01:00:00Z','third')]}]
 ]);
 const requests=[];
 const fetchImpl=async (url)=>{
  requests.push(url);
  return body.has(url)?{ok:true,json:async()=>body.get(url)}:{ok:false,status:404};
 };
 return {body,requests,fetchImpl};
}

test('V6-like names, leap-year calendar and exact UTC window boundaries',()=>{
 assert.equal(PERIODS['12m'],'1 year');
 const end='2026-10-26T14:00:00Z';
 const year=selectUtcWindow({period:'12m',end});
 assert.equal(year.start,'2025-10-26T14:00:00.000Z');
 assert.equal(year.end,'2026-10-26T14:00:00.000Z');
 assert.equal(year.bounds,'[start,end)');
 assert.equal(selectUtcWindow({period:'12m',end:'2024-02-29T12:00:00Z'}).start,
  '2023-02-28T12:00:00.000Z');
 const dates=selectUtcDates('2026-10-24','2026-10-25');
 assert.equal(dates.start,'2026-10-24T00:00:00.000Z');
 assert.equal(dates.end,'2026-10-26T00:00:00.000Z');
 assert.throws(()=>selectUtcDates('2026-02-30','2026-03-01'),RangeError);
});

test('Previous and next period navigation uses UTC and avoids future lookahead',()=>{
 const initial=selectUtcWindow({period:'7d',end:'2026-10-26T14:00:00Z'});
 const prev=previousUtcWindow(initial);
 assert.equal(prev.end,initial.start);
 assert.equal(nextUtcWindow(prev,'2026-10-26T14:00:00Z').end,initial.end);
 assert.strictEqual(nextUtcWindow(initial,'2026-10-26T14:00:00Z'),initial);
});

test('Year lookback reads index only; no 365 day downloads on mobile',async()=>{
 const f=fixtures();
 const client=createLightningHistoryClient({indexUrl:base,fetchImpl:f.fetchImpl});
 const annual=await client.loadWindow(selectUtcWindow({period:'12m',end:'2026-10-26T14:00:00Z'}));
 assert.equal(annual.mode,'overview');
 assert.equal(annual.archivedDayCount,3);
 assert.equal(annual.dailyCounts.length,2);
 assert.deepEqual(f.requests,[base]);
 assert.match(formatHistoryLabel(annual),/Selected/);
 assert.match(formatHistoryLabel(annual),/Historical gaps are unknown/);
});

test('Detailed historical flash positions filter exact time and deduplicate, cache day requests',async()=>{
 const f=fixtures();
 const client=createLightningHistoryClient({indexUrl:base,fetchImpl:f.fetchImpl});
 const window=selectUtcWindow({period:'custom',start:'2026-10-24T11:00:00Z',end:'2026-10-26T00:00:00Z'});
 const once=await client.loadWindow(window,{detail:true});
 assert.equal(once.mode,'detail');
 assert.equal(once.totalMatchingObservations,2);
 assert.equal(once.geojson.features[0].id,'second');
 assert.equal(once.firstObservationUtc,'2026-10-24T23:59:59Z');
 assert.equal(once.lastObservationUtc,'2026-10-25T01:00:00Z');
 assert.equal(once.truncated,false);
 assert.equal(f.requests.length,3);
 const again=await client.loadWindow(window,{detail:true});
 assert.equal(again.totalMatchingObservations,2);
 assert.equal(f.requests.length,3);
 assert.match(formatHistoryLabel(once),/2 shown of 2 archived optical flashes/);
});

test('Long range refuses bulk exact points, and short range can display cap honestly',async()=>{
 const f=fixtures();
 const client=createLightningHistoryClient({indexUrl:base,fetchImpl:f.fetchImpl,maxMapPoints:1});
 const annual=await client.loadWindow(selectUtcWindow({period:'12m',end:'2026-10-26T14:00:00Z'}),{detail:true});
 assert.equal(annual.mode,'overview');
 assert.match(annual.detailReason,/Choose at most/);
 assert.equal(f.requests.length,1);
 const short=await client.loadWindow(selectUtcDates('2026-10-24','2026-10-25'),{detail:true});
 assert.equal(short.mode,'detail');
 assert.equal(short.totalMatchingObservations,3);
 assert.equal(short.shownObservations,1);
 assert.equal(short.truncated,true);
});

test('No archived days is unobserved/unavailable history, not zero strikes',async()=>{
 const f=fixtures();
 const client=createLightningHistoryClient({indexUrl:base,fetchImpl:f.fetchImpl});
 const empty=await client.loadWindow(selectUtcDates('2026-10-20','2026-10-21'),{detail:true});
 assert.equal(empty.totalMatchingObservations,0);
 assert.match(empty.availabilityNote,/unknown, not no lightning/);
 assert.equal(empty.geojson.features.length,0);
});

test('Archive network and invalid manifest failures are explicit, never fabricated',async()=>{
 let calls=0;
 const client=createLightningHistoryClient({indexUrl:base,fetchImpl:async()=>{
  calls++;return {ok:false,status:503};
 }});
 await assert.rejects(()=>client.loadWindow(selectUtcWindow({period:'24h'})),/HTTP 503/);
 await assert.rejects(()=>client.loadWindow(selectUtcWindow({period:'24h'})),/HTTP 503/);
 assert.equal(calls,2);
});

test('Historical requests outside actual retained year are rejected, not shown as zero',async()=>{
 const f=fixtures();
 const client=createLightningHistoryClient({indexUrl:base,fetchImpl:f.fetchImpl});
 const beyond=selectUtcWindow({period:'7d',end:'2025-10-25T00:00:00Z'});
 await assert.rejects(()=>client.loadWindow(beyond,{detail:true}),/precede available one-year archive/);
 assert.deepEqual(f.requests,[base]);
});

test('Annual label shows observed dates from selection rather than global index',async()=>{
 const f=fixtures();
 const client=createLightningHistoryClient({indexUrl:base,fetchImpl:f.fetchImpl});
 const selected=await client.loadWindow(selectUtcDates('2026-10-25','2026-10-25'));
 const label=formatHistoryLabel(selected);
 assert.match(label,/2026-10-25T01:00:00Z/);
 assert.doesNotMatch(label,/2026-10-24T10:00:00Z/);
});
