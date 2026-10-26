/**
 * Standalone UTC date-range and history client, inspired by GlobalGrid2050
 * UK Energy Tracker V6: named periods, previous/next navigation, daily
 * summaries for long windows, lazy historical loading and Promise caches.
 *
 * NO DOM, CSS, window.fetch rewrite, or original GridAtlas UI mutation.
 * Historical shard filenames end with .json to bypass the original global
 * V8 legacy *.geojson -> internal Parquet resolver.
 * EUMETSAT LI optical flashes are NOT confirmed cloud-to-ground strikes.
 */
const DAY=86400000;
export const PERIODS=Object.freeze({
 '12h':'12 hours','24h':'24 hours','48h':'48 hours',
 '7d':'7 days','30d':'30 days','3m':'3 months',
 '6m':'6 months','12m':'1 year','custom':'Custom UTC dates'
});
const FIXED=Object.freeze({'12h':12*3600000,'24h':DAY,'48h':2*DAY,'7d':7*DAY,'30d':30*DAY});
const MONTHS=Object.freeze({'3m':3,'6m':6,'12m':12});
function utc(v,name){
 const d=v instanceof Date?new Date(v.getTime()):new Date(v);
 if(!Number.isFinite(d.getTime()))throw new RangeError(name+' must be a valid UTC date');
 return d;
}
function minusMonths(d,count){
 const first=new Date(Date.UTC(d.getUTCFullYear(),d.getUTCMonth()-count,1,
  d.getUTCHours(),d.getUTCMinutes(),d.getUTCSeconds(),d.getUTCMilliseconds()));
 const length=new Date(Date.UTC(first.getUTCFullYear(),first.getUTCMonth()+1,0)).getUTCDate();
 first.setUTCDate(Math.min(d.getUTCDate(),length));return first;
}
function pretty(iso){return iso.slice(0,16).replace('T',' ')+' UTC';}
/** All windows are UTC and [start, end) so midnight events cannot double count. */
export function selectUtcWindow({period='24h',end=new Date(),start=null}={}){
 if(!Object.hasOwn(PERIODS,period))throw new RangeError('Unsupported period: '+period);
 const e=utc(end,'end');
 const s=period==='custom'?utc(start,'start'):MONTHS[period]?minusMonths(e,MONTHS[period]):
   new Date(e.getTime()-FIXED[period]);
 if(s>=e)throw new RangeError('Start must precede end');
 if(s<minusMonths(e,12))throw new RangeError('Maximum archive lookback is one calendar year');
 return Object.freeze({period,start:s.toISOString(),end:e.toISOString(),timezone:'UTC',bounds:'[start,end)'});
}
/** Date-only input includes the selected end day in full (UTC). */
export function selectUtcDates(fromYmd,toYmd){
 for(const x of [fromYmd,toYmd]){
  if(!/^\d{4}-\d{2}-\d{2}$/.test(x))throw new RangeError('Date must be YYYY-MM-DD');
  if(utc(x+'T00:00:00Z','date').toISOString().slice(0,10)!==x)throw new RangeError('Invalid calendar date');
 }
 return selectUtcWindow({period:'custom',start:fromYmd+'T00:00:00Z',
   end:new Date(Date.parse(toYmd+'T00:00:00Z')+DAY)});
}
export function previousUtcWindow(w){
 const e=utc(w.start,'previous end');
 return w.period==='custom'?selectUtcWindow({period:'custom',end:e,
   start:new Date(e.getTime()-(Date.parse(w.end)-Date.parse(w.start)))}):
   selectUtcWindow({period:w.period,end:e});
}
export function nextUtcWindow(w,now=new Date()){
 const end=Math.min(Date.parse(w.end)+(Date.parse(w.end)-Date.parse(w.start)),utc(now,'now').getTime());
 if(end<=Date.parse(w.end))return w;
 return w.period==='custom'?selectUtcWindow({period:'custom',end:new Date(end),
   start:new Date(end-(Date.parse(w.end)-Date.parse(w.start)))}):
   selectUtcWindow({period:w.period,end:new Date(end)});
}
function validateIndex(m){
 if(m?.schema!=='uk-lightning-history-index.v1'||!Array.isArray(m.days)||
   m.observation!=='satellite optical lightning flashes, not ground strikes')
   throw new TypeError('Invalid historical lightning index');
 for(const row of m.days){
  if(!/^\d{4}-\d{2}-\d{2}$/.test(row.date)||
     !/^days\/\d{4}\/\d{2}\/\d{2}\.json$/.test(row.path)||
     !Number.isSafeInteger(row.count)||row.count<0)
   throw new TypeError('Invalid dated lightning history shard');
 }
 return m;
}
function chosenDays(index,w){
 const start=Date.parse(w.start),end=Date.parse(w.end);
 return index.days.filter(row=>{
  const from=Date.parse(row.date+'T00:00:00Z');
  return from<end&&from+DAY>start;
 });
}
/** Honest time, provenance, count and availability; never say "strikes". */
export function formatHistoryLabel(result){
 const w=result.window,index=result.index;
 const n=result.mode==='detail'?result.totalMatchingObservations:result.archivedDayCount;
 const number=result.mode==='detail'
   ?result.shownObservations+' shown of '+n+' archived optical flashes'
   :n+' flashes recorded on overlapping UTC dates; narrow dates for exact count';
 const actual=result.mode==='detail'
   ?'Actual detections '+(result.firstObservationUtc||'none archived')+' to '+(result.lastObservationUtc||'none archived')
   :'Archive detections '+(index.first_observation_utc||'none archived')+' to '+(index.last_observation_utc||'none archived');
 return 'Selected '+pretty(w.start)+' to '+pretty(w.end)+' (exclusive end) · '+number+
   ' · '+actual+' · Refreshed '+index.generated_utc+
   ' · Historical gaps are unknown, not zero lightning';
}
/** Makes no UI; a Claude UI implant can consume only this data contract. */
export function createLightningHistoryClient({
 indexUrl='https://raw.githubusercontent.com/Ventusltd/lightning/main/data/history/index.json',
 fetchImpl=fetch,maxDetailDays=31,maxMapPoints=40000
}={}){
 let cachedIndex=null,lastRevision=null;
 const cachedDays=new Map();
 async function readJson(url){
  const res=await fetchImpl(url,{cache:'no-store'});
  if(!res.ok)throw new Error('History HTTP '+res.status+': '+url);
  return res.json();
 }
 async function index({refresh=false}={}){
  if(!cachedIndex||refresh){
   cachedIndex=readJson(indexUrl).then(validateIndex).then(m=>{
    if(lastRevision!==m.generated_utc){cachedDays.clear();lastRevision=m.generated_utc;}
    return m;
   }).catch(e=>{cachedIndex=null;throw e;});
  }
  return cachedIndex;
 }
 function dayFile(row){
  const url=new URL(row.path,indexUrl).href;
  if(!cachedDays.has(url)){
   cachedDays.set(url,readJson(url).then(obj=>{
    if(obj?.schema!=='uk-lightning-history-day.v1'||
      obj?.type!=='FeatureCollection'||!Array.isArray(obj.features)||
      obj.metadata?.date_utc!==row.date)
     throw new TypeError('Invalid flash history day '+row.date);
    return obj.features;
   }).catch(e=>{cachedDays.delete(url);throw e;}));
  }
  return cachedDays.get(url);
 }
 async function loadWindow(window,{detail=false,refreshIndex=false}={}){
  const w=selectUtcWindow({period:window.period,start:window.start,end:window.end});
  const m=await index({refresh:refreshIndex});
  const rows=chosenDays(m,w);
  const sum=rows.reduce((n,r)=>n+r.count,0);
  const result={mode:'overview',window:w,index:m,selectedDays:rows,
    archivedDayCount:sum,dailyCounts:rows.map(r=>({date:r.date,count:r.count})),
    archiveAvailability:'PARTIAL_ARCHIVE_UNLESS_BACKFILLED',
    availabilityNote:m.availability_note,scanWindowNote:m.scan_window_note,
    canDrillDown:true};
  const days=(Date.parse(w.end)-Date.parse(w.start))/DAY;
  if(!detail||days>maxDetailDays)
   return {...result,detailReason:days>maxDetailDays
     ?'Choose at most '+maxDetailDays+' days for exact point positions'
     :'Long date ranges load small daily summaries until user selects details'};
  // Lazy parallelism capped at four independent archived-day requests.
  const chunks=new Array(rows.length);let pos=0;
  await Promise.all(Array.from({length:Math.min(4,rows.length)},async()=>{
   while(pos<rows.length){const i=pos++;chunks[i]=await dayFile(rows[i]);}
  }));
  const found=new Map(),start=Date.parse(w.start),end=Date.parse(w.end);
  for(const part of chunks)for(const feature of part){
   if(feature?.type!=='Feature'||feature.geometry?.type!=='Point'||
     feature.properties?.observation_kind!=='optical_lightning_flash')continue;
   const t=Date.parse(feature.properties?.time_utc);
   if(!Number.isFinite(t)||t<start||t>=end||typeof feature.id!=='string')continue;
   found.set(feature.id,feature);
  }
  const events=[...found.values()].sort((a,b)=>a.properties.time_utc.localeCompare(b.properties.time_utc)||
    a.id.localeCompare(b.id));
  const shown=events.length>maxMapPoints?events.slice(-maxMapPoints):events;
  return {...result,mode:'detail',totalMatchingObservations:events.length,
    shownObservations:shown.length,truncated:events.length>shown.length,
    firstObservationUtc:events[0]?.properties.time_utc||null,
    lastObservationUtc:events.at(-1)?.properties.time_utc||null,
    geojson:{type:'FeatureCollection',features:shown}};
 }
 return Object.freeze({index,loadWindow,periods:PERIODS});
}
