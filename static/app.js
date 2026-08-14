const input=document.querySelector('#file-input');
const zone=document.querySelector('#dropzone');
const form=document.querySelector('#upload-form');
const statusBox=document.querySelector('#status');
const progress=document.querySelector('#progress');
const result=document.querySelector('#result');
const errorBox=document.querySelector('#error');
const statusKicker=document.querySelector('#status-kicker');
const statusTitle=document.querySelector('#status-title');
const statusDetail=document.querySelector('#status-detail');
const download=document.querySelector('#download');
const again=document.querySelector('#again');
const retry=document.querySelector('#retry');
const exactReport=document.querySelector('#exact-report');
let busy=false;

function bytes(n){if(!Number.isFinite(n))return'–';const u=['t','kt','Mt','Gt'];let i=0;while(n>=1024&&i<u.length-1){n/=1024;i++}return`${n<10&&i?n.toFixed(1):Math.round(n)} ${u[i]}`}
function reset(){busy=false;input.value='';statusBox.hidden=true;form.hidden=false;result.hidden=true;errorBox.hidden=true;progress.className='progress';exactReport.open=false;zone.focus()}
function fail(message){busy=false;progress.className='progress failed';statusKicker.textContent='EI KÄSITELTY';statusTitle.textContent='Tiedostoa ei muutettu';statusDetail.textContent='Palvelu hylkäsi tiedoston turvallisesti.';document.querySelector('#error-message').textContent=message;errorBox.hidden=false;result.hidden=true}
function putText(id,value){document.querySelector(id).textContent=value}
function renderList(id,sectionId,bucket,emptyText){
  const target=document.querySelector(id);target.replaceChildren();
  const count=Number(bucket?.field_count||0);const items=Array.isArray(bucket?.fields)?bucket.fields:[];
  document.querySelector(sectionId).hidden=count===0;
  if(!count){const p=document.createElement('p');p.className='report-empty';p.textContent=emptyText;target.append(p);return}
  for(const item of items){const row=document.createElement('div');const field=document.createElement('code');const value=document.createElement('span');field.textContent=String(item.field??'');value.textContent=String(item.value??'');row.append(field,value);target.append(row)}
  if(Number(bucket?.omitted_count)>0){const note=document.createElement('p');note.className='report-limit';note.textContent=`${bucket.omitted_count} lisäkenttää jätettiin näkymästä raportin kokorajan vuoksi.`;target.append(note)}
}
function renderReport(report){
  report=report||{};exactReport.open=false;
  putText('#report-method',report.method||'Ei menetelmätietoa.');
  putText('#removed-fields-total',`(${report.removed?.field_count||0})`);
  putText('#removed-tracks-total',`(${report.removed_tracks?.field_count||0})`);
  putText('#kept-fields-total',`(${report.kept?.field_count||0})`);
  putText('#kept-tracks-total',`(${report.kept_tracks?.field_count||0})`);
  renderList('#removed-fields-list','#removed-fields-section',report.removed,'Poistettavaa upotettua metadataa ei löytynyt.');
  renderList('#removed-tracks-list','#removed-tracks-section',report.removed_tracks,'Poistettavia raitoja ei löytynyt.');
  renderList('#kept-fields-list','#kept-section',report.kept,'Säilytettävää teknistä metadataa ei raportoitu.');
  renderList('#kept-tracks-list','#kept-tracks-section',report.kept_tracks,'Säilytettäviä raitoja ei raportoitu.');
  const verification=report.verification||{};
  putText('#verification-result',verification.status==='passed'&&Number(verification.remaining_count||0)===0?'Jälkitarkistus: poistettavaksi luokiteltuja kenttiä tai raitoja ei jäänyt.':'Jälkitarkistus ei vahvistanut poistojen onnistumista.');
}
async function clean(file){
  if(busy||!file)return;
  if(file.size===0){form.hidden=true;statusBox.hidden=false;fail('Tyhjä tiedosto ei ole käsiteltävissä.');return}
  busy=true;form.hidden=true;statusBox.hidden=false;result.hidden=true;errorBox.hidden=true;progress.className='progress';statusKicker.textContent='KÄSITTELY';statusTitle.textContent='Tarkistetaan tiedostoa';statusDetail.textContent='Tiedosto siirretään salatun Tailnet-yhteyden kautta.';
  const suffix=(file.name.match(/\.[A-Za-z0-9]+$/)||[''])[0].toLowerCase();
  try{
    const csrf=document.querySelector('meta[name="csrf-token"]').content;
    const response=await fetch('/api/clean',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-CSRF-Token':csrf,'X-File-Suffix':suffix},body:file});
    const payload=await response.json().catch(()=>({}));if(!response.ok)throw new Error(payload.detail||'Käsittely epäonnistui.');
    progress.className='progress done';statusKicker.textContent='VALMIS / JÄLKITARKISTETTU';statusTitle.textContent='Käsitelty kopio on valmis';statusDetail.textContent=payload.kind==='video'?'Kuva ja ääni säilytettiin ilman uudelleenpakkausta.':'Kuvan suunta säilytettiin ja löydetty upotettu metadata poistettiin.';
    putText('#removed-count',payload.removed_fields);putText('#dropped-count',payload.dropped_tracks);putText('#size-change',`${bytes(payload.input_bytes)} → ${bytes(payload.output_bytes)}`);putText('#result-note',payload.note);renderReport(payload.report);
    download.href=payload.download_url;download.textContent='Lataa käsitelty kopio ↓';result.hidden=false;
  }catch(err){fail(err.message||'Käsittely epäonnistui.')}
}
zone.addEventListener('click',()=>{if(!busy)input.click()});
zone.addEventListener('keydown',e=>{if((e.key==='Enter'||e.key===' ')&&!busy){e.preventDefault();input.click()}});
input.addEventListener('change',()=>clean(input.files[0]));
['dragenter','dragover'].forEach(name=>zone.addEventListener(name,e=>{e.preventDefault();if(!busy)zone.classList.add('dragging')}));
['dragleave','drop'].forEach(name=>zone.addEventListener(name,e=>{e.preventDefault();zone.classList.remove('dragging')}));
zone.addEventListener('drop',e=>{if(!busy)clean(e.dataTransfer.files[0])});again.addEventListener('click',reset);retry.addEventListener('click',reset);
download.addEventListener('click',()=>{download.textContent='Lataus käynnistyi';setTimeout(()=>{again.focus()},500)});form.addEventListener('submit',e=>e.preventDefault());
