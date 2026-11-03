import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
const MODULES=process.env.RUNTIME_NODE_MODULES ?? path.resolve('node_modules');
const runtimeRequire=createRequire(path.join(MODULES,'../package.json'));
const {Presentation,PresentationFile}=await import(pathToFileURL(runtimeRequire.resolve('@oai/artifact-tool')));
const ROOT=path.resolve(process.env.GRID_AGENT_MANUSCRIPT_ROOT ?? 'manuscript-workspace');
const SKILL=process.env.PRESENTATIONS_SKILL_DIR;
if (!SKILL) throw new Error('Set PRESENTATIONS_SKILL_DIR to the external presentation validation tools directory.');
process.env.RUNTIME_NODE_MODULES=MODULES;
const PY=process.env.RUNTIME_PYTHON ?? 'python3';
const {finalizePresentation}=await import(pathToFileURL(path.join(SKILL,'container_tools/artifact_tool_utils.mjs')));
const D=JSON.parse(await fs.readFile(path.join(ROOT,'evidence/integrated_loop_20261006/framework_geometry.json'),'utf8'));
const W=1500,H=1060;
const P=Presentation.create({slideSize:{width:W,height:H}});
const s=P.slides.add();s.background.fill='#FFFFFF';
const C={black:'#000000',white:'#FFFFFF',land:'#F4F3ED',solar:'#E69A27',nuclear:'#87619D',battery:'#008783',wind:'#3478A8',feedback:'#B14436'};
let serial=0;
function rect(x,y,w,h,fill='none',stroke='none',lw=0,name='rect'){
 return s.shapes.add({geometry:'rect',name:`${name}-${serial++}`,position:{left:x,top:y,width:w,height:h},fill,line:{fill:stroke,width:lw}});
}
function txt(x,y,w,h,t,size=22,bold=false,align='left',name='label'){
 const o=s.shapes.add({geometry:'textbox',name:`${name}-${serial++}`,position:{left:x,top:y,width:w,height:h},fill:'none',line:{fill:'none',width:0}});
 o.text=t;o.text.style={typeface:'Arial',fontSize:size,bold,color:C.black,alignment:align,verticalAlignment:'middle',autoFit:'none'};
 return o;
}
function polylines(lines,{fill='none',stroke=C.black,lw=1,closed=false,name='path',dash='solid'}={}){
 const all=lines.flat(); if(!all.length)return;
 let x0=Math.min(...all.map(p=>p[0])),y0=Math.min(...all.map(p=>p[1])),x1=Math.max(...all.map(p=>p[0])),y1=Math.max(...all.map(p=>p[1]));
 const w=Math.max(.01,x1-x0),h=Math.max(.01,y1-y0);
 const commands=[];
 for(const line of lines){if(!line.length)continue;commands.push({moveTo:{x:line[0][0]-x0,y:line[0][1]-y0}});for(const p of line.slice(1))commands.push({lineTo:{x:p[0]-x0,y:p[1]-y0}});if(closed)commands.push({close:{}});}
 return s.shapes.add({geometry:'custom',name:`${name}-${serial++}`,position:{left:x0,top:y0,width:w,height:h},fill,line:{fill:stroke,width:lw,style:dash},customPaths:[{width:w,height:h,commands}]});
}
function ln(points,color=C.black,lw=1.5,name='line',dash='solid'){return polylines([points],{stroke:color,lw,name,dash});}
function circle(x,y,r,fill=C.black,stroke='none',lw=0,name='point'){
 return s.shapes.add({geometry:'ellipse',name:`${name}-${serial++}`,position:{left:x-r,top:y-r,width:2*r,height:2*r},fill,line:{fill:stroke,width:lw}});
}
function arrow(points,color=C.black,lw=1.8,name='arrow'){
 ln(points,color,lw,name);
 let [a,b]=points.slice(-2),dx=b[0]-a[0],dy=b[1]-a[1],l=Math.hypot(dx,dy);dx/=l;dy/=l;
 polylines([[[b[0],b[1]],[b[0]-9*dx+4*dy,b[1]-9*dy-4*dx],[b[0]-9*dx-4*dy,b[1]-9*dy+4*dx]]],{fill:color,stroke:'none',lw:0,closed:true,name:name+'-head'});
}
function projection(bounds,x,y,w,h){const k=Math.min(w/(bounds[2]-bounds[0]),h/(bounds[3]-bounds[1]));const xp=x+(w-k*(bounds[2]-bounds[0]))/2,yp=y+(h-k*(bounds[3]-bounds[1]))/2;return p=>[xp+(p[0]-bounds[0])*k,yp+(bounds[3]-p[1])*k];}
function wedge(x,y,r,a,b,color,id){
 const n=Math.max(3,Math.ceil((b-a)*14));const points=[[x,y]];for(let i=0;i<=n;i++){let t=a+(b-a)*i/n;points.push([x+r*Math.cos(t),y+r*Math.sin(t)]);}
 return polylines([points],{fill:color,stroke:C.white,lw:.8,closed:true,name:id});
}
function curve(points,color=C.black,lw=1.8,name='flow'){arrow(points,color,lw,name);}
function bez(a,b,c,d,n=32){return Array.from({length:n+1},(_,i)=>{let t=i/n,u=1-t;return [u*u*u*a[0]+3*u*u*t*b[0]+3*u*t*t*c[0]+t*t*t*d[0],u*u*u*a[1]+3*u*u*t*b[1]+3*u*t*t*c[1]+t*t*t*d[1]];});}
// Global scope: every model zone is plotted at its representative coordinate.
txt(22,10,620,43,'Eight power systems · 58 model zones',28,true);
const f=projection(D.bounds,20,84,585,295);
const study=new Set(['United States of America','Mexico','Brazil','South Africa','Australia','New Zealand']);
for(const c of D.world)polylines(c.polygons.map(p=>p.map(f)),{fill:study.has(c.name)?C.land:C.white,stroke:C.black,lw:.55,closed:true,name:'geography-'+c.name});
for(const r of D.regions)for(const n of r.nodes){let p=f(n.xy);circle(p[0],p[1],2.15,C.black,'none',0,'model-zone-'+r.id+'-'+n.id);}
// Names sit in clear margins; elbow leaders identify their model-zone clusters.
const labels={
 miso:{box:[94,58,116,36],route:[[171,122],[151,102]]},
 nyiso:{box:[238,58,116,36],route:[[240,132],[292,102]]},
 ercot:{box:[15,170,108,36],route:[[139,188],[128,188]]},
 mexico:{box:[20,235,112,36],route:[[150,253],[139,253]]},
 brazil:{box:[93,318,110,36],route:[[192,302],[192,336],[183,336]]},
 south_africa:{box:[260,352,165,36],route:[[354,334],[345,343]]},
 australia:{box:[442,352,140,36],route:[[523,339],[510,343]]},
 new_zealand:{box:[474,391,176,32],route:[[594,358],[594,382]]}
};
for(const r of D.regions){const {box:[x,y,w,h],route}=labels[r.id],p=f(r.center);ln([p,...route],C.black,1,'geographic-label-'+r.id);txt(x,y,w,h,r.name,23,true,'left','geographic-name-'+r.id);}
txt(23,426,580,34,'2023 · 2030 · 2035     |     96 trajectories',24,true);
// Two explicitly sourced input streams merge into each year-matched scenario.
txt(23,466,310,37,'Observed regional data',25,true);
txt(23,507,315,34,'Assets · demand · ERA5',23);
txt(23,575,320,36,'NASA NEX-GDDP-CMIP6',24,true);
txt(23,615,312,34,'2030 / 2035 + ERA5 intraday',22);
curve(bez([310,519],[339,519],[343,526],[365,526]),C.black,1.7,'observations-to-prepared-input');
curve(bez([312,618],[350,618],[330,548],[365,548]),C.black,1.7,'NASA-to-prepared-input');
txt(365,460,226,68,'Year-matched\nhourly inputs',25,true,'center');
// Frozen prepared resource traces, independently normalized; semantic sources in the caption.
const px=377,py=535,pw=190,ph=77;
for(const [key,col,yoff] of [['demand_MW',C.black,0],['wind_cf',C.wind,28],['solar_cf',C.solar,56]]){
 const v=D.profiles[key],lo=Math.min(...v),hi=Math.max(...v);
 ln(v.map((v,i)=>[px+pw*i/167,py+yoff+23-23*(v-lo)/(hi-lo)]),col,1.55,'prepared-profile-'+key);
}
txt(366,621,224,33,'PySAM + wind curve',21,false,'center');
curve(bez([592,550],[659,550],[602,256],[817,223]),C.black,2.3,'prepared-scenario-to-agent');
// Agent state is shown as an actual decision structure, not a stage box.
txt(789,18,624,43,'Single agent or specialist team',28,true,'center');
txt(935,66,355,39,'Propose & revise',29,true,'center');
const hub=[1084,188],roles=[{p:[922,140],t:'Siting',x:839,y:102,w:134},{p:[1239,140],t:'Capacity',x:1199,y:102,w:153},{p:[922,256],t:'Storage',x:838,y:269,w:150},{p:[1239,256],t:'Network',x:1200,y:269,w:148}];
for(const r of roles){ln([r.p,hub],C.black,1.8,'agent-design-relationship');circle(...r.p,7,C.white,C.black,1.8,'design-variable');txt(r.x,r.y,r.w,35,r.t,24,false,'center');}
circle(...hub,39,C.white,C.black,2,'orchestrated-proposal');
// Central decision junction with four inward evidence paths.
for(const [yy,x0,x1] of [[176,1066,1094],[188,1077,1103],[200,1063,1086]]){ln([[1061,yy],[1107,yy]],C.black,.8,'bound-domain');ln([[x0,yy],[x1,yy]],C.black,3,'allowed-bound');circle(x0,yy,2.6,C.black);circle(x1,yy,2.6,C.black);}
txt(968,238,235,36,'Design bounds',23,true,'center');
// The fixed regional scenario is propagated with each design into the numerical experiment.
curve(bez([1293,269],[1428,295],[1450,359],[1392,404]),C.black,2.3,'design-to-system-solve');
// Existing ERCOT topology provides an engineering representation of the system solve.
txt(1153,405,324,39,'PyPSA',29,true,'center');
txt(1117,447,360,37,'Investment + hourly dispatch',22,false,'center');
const xy=D.topology.nodes,pts=Object.values(xy),bb=[Math.min(...pts.map(p=>p[0])),Math.min(...pts.map(p=>p[1])),Math.max(...pts.map(p=>p[0])),Math.max(...pts.map(p=>p[1]))];
const nf=projection(bb,1171,501,174,132),np=Object.fromEntries(Object.entries(xy).map(([k,p])=>[k,nf(p)]));
for(const e of D.topology.corridors)ln([np[e.bus0],np[e.bus1]],C.black,1.5,'existing-model-link');
for(const [k,p] of Object.entries(np))circle(...p,4.3,C.white,C.black,1.7,'existing-model-bus-'+k);
const vg=D.regions.find(r=>r.id==='ercot').nodes;
for(const n of vg){let p=np[n.id];if(!p)continue;let total=['wind','solar','nuclear','battery'].reduce((z,k)=>z+(n.new_MW[k]??0),0);let a=-Math.PI/2;for(const k of ['wind','solar','nuclear','battery']){let t=2*Math.PI*(n.new_MW[k]??0)/(total||1);if(t>0)wedge(p[0],p[1],4+Math.sqrt(total/1000)*1.3,a,a+t,C[k],'zonal-'+n.id+'-'+k);a+=t;}}
// Selected accepted dispatch, directly read from its hashed NetCDF checkpoint.
const seq=['existing','nuclear','wind','solar','battery'];
const dispatchTop=Array(168).fill(0);let prev=Array(168).fill(0);
const peak=Math.max(...Array.from({length:168},(_,i)=>seq.reduce((a,k)=>a+D.dispatch[k][i],0)));
for(const k of seq){let curr=prev.map((v,i)=>v+D.dispatch[k][i]);const hi=curr.map((v,i)=>[1365+i*100/167,629-v/peak*117]);const lo=prev.map((v,i)=>[1365+i*100/167,629-v/peak*117]).reverse();polylines([[...hi,...lo]],{fill:k==='existing'?C.black:C[k],stroke:'none',lw:0,closed:true,name:'selected-dispatch-'+k});prev=curr;}
ln([[1363,507],[1363,630],[1468,630]],C.black,1,'dispatch-axis');
txt(1355,637,122,34,'8,760 h',22,false,'center');
curve(bez([1437,672],[1430,723],[1354,758],[1298,758]),C.black,2.3,'dispatch-to-feeder-check');
// A radial, three-bus electrical schematic encodes the actual representative-feeder abstraction.
txt(915,707,364,39,'OpenDSS',29,true,'center');
txt(871,749,451,37,'Representative-feeder checks',23,false,'center');
const y=826;
ln([[894,y],[946,y]],C.black,2,'source-to-transformer');
circle(960,y,14,C.white,C.black,1.8,'transformer-primary');circle(978,y,14,C.white,C.black,1.8,'transformer-secondary');
ln([[992,y],[1248,y]],C.black,2,'radial-feeder');
for(const x of [1027,1131,1237]){ln([[x,y-13],[x,y+13]],C.black,2.6,'feeder-bus');ln([[x,y+13],[x,y+37]],C.black,1.8,'load-connection');polylines([[[x-9,y+37],[x+9,y+37],[x,y+51]]],{fill:C.white,stroke:C.black,lw:1.5,closed:true,name:'feeder-load'});}
txt(866,879,475,36,'Voltage · thermal · system limits',23,false,'center');
// Numerical evidence closes the loop; the acceptance branch does not end exploration.
curve(bez([874,838],[750,838],[696,729],[703,630]),C.feedback,2.8,'numerical-feedback-to-agent-memory');
txt(700,413,350,40,'Evidence & memory',28,true,'center');
const rows=[['Cost',482],['Adequacy',524],['Voltage / loading',566]];
for(let i=0;i<rows.length;i++){let [label,yy]=rows[i];ln([[719,yy+15],[740,yy+15]],C.black,1.4,'evidence-record');circle(727,yy+15,3,C.black);txt(752,yy-4,250,35,label,24);}
curve(bez([699,394],[673,328],[723,249],[817,192]),C.feedback,2.8,'critique-to-next-proposal');
txt(671,310,159,37,'Revise',23,true,'center');
// Unboxed centre keeps the recurrent structure visually dominant.
txt(954,540,157,97,'Adaptive\nplanning\nloop',25,true,'center');
// Accepted designs are retained in parallel with further exploration.
curve(bez([886,893],[814,966],[720,963],[632,895]),C.black,2.2,'accepted-design-retention');
txt(661,936,220,68,'Retain accepted\ncandidates',23,true,'center');
// Full eight-region output, using the unchanged selected 2035 totals.
txt(23,690,575,42,'Regional plans',29,true);
txt(23,733,563,36,'Selected 2035 additions · GW',24);
const tx=189,bw=370,scale=bw/220,ystart=790,rh=28;
for(let i=0;i<D.regions.length;i++){
 const r=D.regions[i],y=ystart+i*rh;txt(23,y-13,163,29,r.name,22);
 let x=tx,total=0;for(const k of ['wind','solar','nuclear','battery']){const v=r.new_capacities_GW[k]??0;if(v>0)rect(x,y-6,v*scale,13,C[k],'none',0,'selected-'+r.id+'-'+k+'-GW');x+=v*scale;total+=v;}
 txt(x+7,y-14,78,31,total.toFixed(1),21);
}
const ls=[['wind','Wind'],['solar','Solar'],['nuclear','Nuclear'],['battery','Battery'],['black','Existing']];
for(let i=0;i<ls.length;i++){let x=24+143*i;rect(x,1021,14,14,C[ls[i][0]]);txt(x+21,1007,120,40,ls[i][1],22);}
// A concise study denominator; details remain in manuscript tables.
txt(929,1001,547,42,'2 deployments × 2 architectures × 3 years',22,false,'center');
const notes=`Integrated Figure 1, with no lettered panels. Eight systems and all 58 model-zone coordinates are plotted against Natural Earth 1:50m country outlines, version 5.1.1, public domain: https://www.naturalearthdata.com/downloads/50m-cultural-vectors/50m-admin-0-countries-2/. Geographic outlines are context, not service boundaries or exact new-plant sites. Real assets, network, demand, costs, policy and ERA5-based environmental data define prepared regional scenarios. NASA NEX-GDDP-CMIP6 (MPI-ESM1-2-HR, SSP2-4.5, 2030/2035) daily fields use ERA5 2023 intraday reconstruction. The visible normalized 168-hour demand, solar and wind traces are frozen from the prepared ERCOT COAS 2035 profile, not observed target-year operation. The PyPSA dispatch inset shows the first 168 hours of the retained accepted ERCOT 2035 Sol multi-agent dispatch, whose checkpoint hash matches the selected candidate; black denotes inherited generators and storage, and colours denote new technologies. Stacks show generation plus positive storage discharge, including energy used for charging; the numerical solve spans 8,760 hours. Input hashes are recorded in evidence/integrated_loop_20261006/framework_geometry.json. Agent proposals determine permissions, bounds, siting and operating rules; PyPSA determines numerical investment and dispatch; OpenDSS checks representative feeders. The small network is the existing ERCOT model topology with selected 2035 zonal additions; straight links are not conductor GIS routes. The radial three-bus schematic shows the representative-feeder abstraction rather than an actual surveyed circuit. Red arrows encode numerical feedback, critique and memory. Both successful and unsuccessful committed experiments may inform the next revision; accepted candidates are retained through exploration and the lowest all-in annual cost is reported at termination, not a certified global optimum. Bars show unchanged best observed accepted 2035 new-power totals for every region, including battery power (not energy); ties and all 96 case results are retained in the manuscript. Technology colours: wind blue, solar orange, nuclear purple, battery teal. Input connections and all text are black. All geometry, profiles, schematics, bars and text are editable native PowerPoint shapes; no inference or physical simulation is run by this authoring process.`;
s.speakerNotes.textFrame.setText(notes);
const runId=String(Date.now());
const TMP=path.join(ROOT,'work/ppt',runId);
const FINAL=path.join(ROOT,'work/final',runId,'figure1.pptx');
await fs.mkdir(TMP,{recursive:true});await fs.mkdir(path.dirname(FINAL),{recursive:true});
const candidate=path.join(TMP,'candidate.pptx');
await (await PresentationFile.exportPptx(P)).save(candidate);
const blob=await P.export({slide:s,format:'png',scale:1.5});
await fs.writeFile(path.join(TMP,'preview.png'),new Uint8Array(await blob.arrayBuffer()));
const lay=await s.export({format:'layout'});await fs.writeFile(path.join(TMP,'layout.json'),await lay.text());
const result=await finalizePresentation({workspaceDir:ROOT,candidatePath:candidate,finalPath:FINAL,pythonExecutable:PY,integrityValidatorPath:path.join(SKILL,'container_tools/inspect_presentation_package_integrity.py'),layoutValidatorPath:path.join(SKILL,'container_tools/inspect_presentation_layout_geometry.py'),explicitTotalSlideCount:1,requiredNativeTableOwnerSlides:[],requiredNativeChartOwnerSlides:[],fontPolicy:{basis:'design',families:['Arial']},layoutArgs:['--expected-slide-size-emu',`${W*9525},${H*9525}`,'--validate-bullet-geometry','--validate-heading-fit'],verifyArtifactToolImport:true,receiptPath:path.join(TMP,'validation.json')});
await fs.copyFile(FINAL,path.join(ROOT,'figures/fig1_framework.pptx'));
await fs.copyFile(path.join(TMP,'validation.json'),path.join(ROOT,'evidence/integrated_loop_20261006/presentation_validation.json'));
await fs.copyFile(path.join(TMP,'preview.png'),path.join(ROOT,'work/ppt/preview-final.png'));
console.log(JSON.stringify({stage:'finalized',objects:serial,path:path.join(ROOT,'figures/fig1_framework.pptx'),sha256:result.finalSha256,slideCount:1}));
