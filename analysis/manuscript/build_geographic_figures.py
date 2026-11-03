#!/usr/bin/env python3
"""Render geographic planning figures from committed numerical evidence only.
No agent, optimizer, API or power-flow invocation. --root only reads source files.
"""
from pathlib import Path
import os, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ArchiveResolver
_ARCHIVE = ArchiveResolver(Path(os.environ.get("GRID_AGENT_ARCHIVE_ROOT", "external-archive")), __import__("json").loads(os.environ.get("GRID_AGENT_ARCHIVE_MAP", "{}")))
_WORKSPACE = Path(os.environ.get("GRID_AGENT_MANUSCRIPT_ROOT", "manuscript-workspace")).resolve()
from collections import defaultdict
from datetime import datetime, timezone
import argparse, csv, hashlib, json, math
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.path import Path as MarkerPath
from matplotlib.lines import Line2D
from matplotlib.colors import Normalize, TwoSlopeNorm
from matplotlib.cm import ScalarMappable
from matplotlib.offsetbox import AnnotationBbox, DrawingArea
from matplotlib.patches import Rectangle
import matplotlib.patheffects as pe
from pyproj import CRS, Transformer
import geopandas as gpd
from shapely.geometry import box

HERE=_WORKSPACE
EV=HERE/'evidence/geographic_20261003'
REGIONS=['miso','ercot','nyiso','south_africa','australia','new_zealand','mexico','brazil']
NAMES=['MISO','ERCOT','NYISO','South Africa','Australia (NEM)','New Zealand','Mexico','Brazil']
FRAMES=dict(zip(REGIONS,[(-102,-82,29,50),(-107,-93,25,37),(-80,-70.5,40,45.5),(16,34,-35,-22),(134,155,-44,-24),(165,179,-48,-33),(-118,-86,14,33.5),(-75,-32,-35,6)]))
GEN={'wind','solar','nuclear','coal','gas','oil','hydro','other','biomass','waste','csp','geothermal','fuel_cell'}
TEAL='#188a94'; PINK='#bd4269'
FACTORY=MarkerPath([(-.85,-.65),(-.85,.05),(-.4,.05),(-.4,.8),(-.12,.8),(-.12,.05),(.16,.05),(.16,1),(.44,1),(.44,.05),(.85,.05),(.85,-.65),(-.85,-.65)], [MarkerPath.MOVETO]+[MarkerPath.LINETO]*11+[MarkerPath.CLOSEPOLY])
plt.rcParams.update({'font.family':'Arial','font.size':9,'pdf.fonttype':42,'ps.fonttype':42,'axes.titleweight':'bold','axes.titlepad':10,'savefig.facecolor':'white'})

def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def dump(p,x): p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,indent=2,ensure_ascii=False)+'\n')
def rows(p):
    with Path(p).open(newline='',encoding='utf-8-sig') as f:return list(csv.DictReader(f))
def number(v):return float(v.replace(',','')) if isinstance(v,str) and v.strip() else float(v or 0)

def extract(root):
    root=Path(root); pins={}; cases={}
    src=HERE/'evidence/completed_weather_20261003/results.json'; allruns=json.loads(src.read_text())['runs']
    def pin(p,expected=None):
        h=sha(p)
        if expected:assert h==expected, f'source changed: {p}'
        pins[str(p)]=h
        return h
    pin(src)
    for region in REGIONS:
        r=next(x for x in allruns if x['case_id']==f'opus_5_5/2035/{region}/multi')
        assert r['demand_year']==r['weather_year']==2035 and r['completed'] and r['selected']['acceptance']['passes']
        finalpath=_ARCHIVE(r['source_output'])/'final.json'; pin(finalpath,r['final_sha256']); f=json.loads(finalpath.read_text())
        for fn in ['inventory.json','network.json','config.json']:pin(root/'weather_prediction/inputs/2035'/region/fn,r['input_sha256'][fn])
        nodepath=root/'experiment'/region/'nodes.csv'; pin(nodepath)
        nodes={n['id']:{'id':n['id'],'latitude':float(n['latitude']),'longitude':float(n['longitude']),'location_basis':n['location_basis'],'existing_generation_MW':0.,'new_generation_MW':0.,'new_technologies_MW':{},'new_battery_MW':0.,'new_battery_MWh':0.} for n in rows(nodepath)}
        for a in f['final_results']['assets']:
            bus=a['bus'].split('::')[-1]; n=nodes[bus]; tech=a['technology']; cap=number(a.get('capacity_MW'))
            if tech in GEN:n['existing_generation_MW' if a['existing'] else 'new_generation_MW']+=cap
            if not a['existing']:
                n['new_technologies_MW'][tech]=n['new_technologies_MW'].get(tech,0.)+cap
                if tech=='battery':n['new_battery_MW']+=cap;n['new_battery_MWh']+=number(a.get('energy_MWh'))
        totals=f['final_results']['new_capacities_GW']
        for t in ['wind','solar','nuclear','battery']:
            assert abs(sum(n['new_technologies_MW'].get(t,0) for n in nodes.values())/1000-totals[t])<1e-5
        plants=[]
        if region in ['miso','ercot','nyiso']:
            raw=root/'experiment'/region/'raw'; a=raw/'generator_inventory_with_zones.csv';pin(a)
            matches=raw/'plant_zone_matches.csv';pin(matches); matched={str(int(float(z['plant_code']))):z for z in rows(matches)}
            dedup={}
            for z in rows(a):
                if z.get('Status')!='OP':continue
                k=str(int(float(z['Plant Code']))); m=matched.get(k,z)
                lat=m.get('latitude',z.get('Latitude'));lon=m.get('longitude',z.get('Longitude'))
                if not lat or not lon:continue
                dedup.setdefault(k,{'plant_code':k,'plant_name':z['Plant Name'],'latitude':float(lat),'longitude':float(lon),'capacity_MW':0.})['capacity_MW']+=number(z.get('Nameplate Capacity (MW)'))
            plants=list(dedup.values())
        distribution=f['final_distribution_check']
        for q in f['final_results']['transmission']['corridors']:
            q['existing_rating_assumed']=(region in {'miso','ercot','mexico','south_africa'} or (region=='new_zealand' and q['existing_capacity_MW']>=49999))
        c={'case_id':r['case_id'],'demand_year':2035,'weather_year':2035,'model':r['model'],'final_sha256':r['final_sha256'],'candidate_sha256':r['selected']['candidate_sha256'],'accepted':True,'nodes':list(nodes.values()),'operating_2023_plant_points':plants,'corridors':f['final_results']['transmission']['corridors'],'new_capacities_GW':totals,'distribution_summary':{k:v for k,v in distribution.items() if k!='nodes'},'annual_all_in_cost_billion':r['selected']['all_in_cost_billion']}
        if region=='ercot':
            log=_ARCHIVE(r['source_output'])/'log.jsonl';pin(log)
            for line in log.open():
                e=json.loads(line)
                if e.get('event')=='simulation':first=e['payload'];break
            assert not first['distribution_check']['passes'] and distribution['passes']
            cfg=json.loads((root/'weather_prediction/inputs/2035'/region/'config.json').read_text())['distribution']
            c['feeder_case']={'node':'FWES','initial':first['distribution_check']['nodes']['FWES'],'selected':distribution['nodes']['FWES'],'initial_design':first['design'],'selected_design':f['final_design'],'config':cfg,'initial_cycle':first['cycle']}
        cases[region]=c
    data={'schema_version':1,'selection':'One completed 2035 multi-agent trajectory per region, Claude Opus 5.5; selected physically accepted candidate. Illustrative common workflow, no deployment ranking.','spatial_semantics':{'basemap':'Natural Earth 1:50m country/state administrative boundaries, version 5.1.1, WGS84 reprojected to local Lambert azimuthal equal-area.','nodes':'Prepared model-zone representative coordinates; generation and storage additions are zonal totals, not proposed plant sites.','corridors':'Straight schematic connections between model nodes, not GIS conductor routes. Existing proxy ratings do not determine line width. Added width uses committed new plus official-project MW.','plant_points':'2023 OP-status EIA inventory joined to archived plant coordinates in the three US regions only. No exact plant dots inferred for the other regions.','feeder':'Stylized 12.47 kV three-bus representative feeder; not mapped physical distribution routes. Feeder voltage symbols encode annual feeder minimum and maximum voltage; line colours encode annual per-line peak current divided by the line rating in each candidate.'},'cases':cases}
    for p in EV.glob('ne_*.zip'):pin(p)
    dump(EV/'planning_maps.json',data)
    dump(EV/'source_manifest.json',{'created_utc':datetime.now(timezone.utc).isoformat(),'files':pins,'basemap':{'provider':'Natural Earth','version':'5.1.1','scale':'1:50m','license':'public domain','country_url':'https://naturalearthdata.com/downloads/50m-cultural-vectors/50m-admin-0-countries-2/','state_url':'https://naturalearthdata.com/downloads/50m-cultural-vectors/50m-admin-1-states-provinces/','terms_url':'https://naturalearthdata.com/about/terms-of-use/','download_urls':['https://naturalearth.s3.amazonaws.com/50m_cultural/ne_50m_admin_0_countries.zip','https://naturalearth.s3.amazonaws.com/50m_cultural/ne_50m_admin_1_states_provinces.zip']}})
    return data

GEO={}
def basemap(ax,reg,scale_bar=True):
    xmin,xmax,ymin,ymax=FRAMES[reg]; crs=CRS.from_proj4(f'+proj=laea +lat_0={(ymin+ymax)/2} +lon_0={(xmin+xmax)/2} +datum=WGS84 +units=m');trans=Transformer.from_crs('EPSG:4326',crs,always_xy=True)
    for tag,fill,lw,color in [('ne_50m_admin_0_countries','#f2f1ed',.6,'#aeb6ba'),('ne_50m_admin_1_states_provinces','none',.3,'#c5cbce')]:
        if tag not in GEO:GEO[tag]=gpd.read_file('zip://'+str(EV/(tag+'.zip')))
        g=GEO[tag];g=g.loc[g.intersects(box(xmin,ymin,xmax,ymax))].copy()
        if len(g):
            g.geometry=g.geometry.intersection(box(xmin,ymin,xmax,ymax));g=g.loc[~g.is_empty];g.to_crs(crs).plot(ax=ax,facecolor=fill,edgecolor=color,linewidth=lw,zorder=0)
    pts=np.array([trans.transform(x,y) for x in [xmin,xmax] for y in [ymin,ymax]])
    ax.set_xlim(pts[:,0].min(),pts[:,0].max());ax.set_ylim(pts[:,1].min(),pts[:,1].max());ax.set_aspect('equal');ax.axis('off')
    w=ax.get_xlim()[1]-ax.get_xlim()[0];h=ax.get_ylim()[1]-ax.get_ylim()[0];scale=100 if w<900000 else 200 if w<2000000 else 500;x=ax.get_xlim()[0]+.05*w;y=ax.get_ylim()[0]+.06*h
    if scale_bar:
        ax.plot([x,x+scale*1000],[y,y],color='#000000',lw=1.0)
        ax.text(x+scale*500,y+.022*h,f'{scale} km',ha='center',color='#000000',fontsize=7)
    return trans

def coords(c,t):return {n['id']:t.transform(n['longitude'],n['latitude']) for n in c['nodes']}
def routes(ax,c,xy,add=True):
    for q in c['corridors']:
        x0,y0=xy[q['bus0']];x1,y1=xy[q['bus1']];proxy=q['existing_rating_assumed']
        ax.plot([x0,x1],[y0,y1],color='#657178',lw=.7,alpha=.7,ls=(0,(3,2)) if proxy else '-',zorder=1)
        gw=(q.get('new_capacity_MW',0)+q.get('official_project_capacity_MW',0))/1000
        if add and gw>0:ax.plot([x0,x1],[y0,y1],color=PINK,lw=.8+.9*gw,zorder=2,alpha=.9)
def plants(ax,c,t):
    p=c['operating_2023_plant_points']
    if p:
        x,y=t.transform([q['longitude'] for q in p],[q['latitude'] for q in p]);ax.scatter(x,y,s=3,c='#20272a',alpha=.45,linewidths=0,zorder=3)
def node_label(ax,n,xy,reg,detail=False):
    lab=n['id']
    if reg=='miso':lab={'0001':'LRZ 1','0004':'4','0006':'6','0027':'2+7','0035':'3+5','8910':'8+9+10'}[lab]
    if reg=='nyiso':lab=lab.replace('ZON','')
    offsets={'COAS':(15,-14),'EAST':(17,0),'NCEN':(12,13),'SCEN':(0,-18),'SOUT':(6,-13),'FWES':(-9,10),'WEST':(-12,-20),'NRTH':(0,14),'GP':(0,-19),'MP':(18,3),'HYDRA':(-13,-2),'NW':(-13,10),'FS':(10,-13),'NC':(-10,11),'EC':(15,-4),'WC':(0,-13),'ZONG':(-12,14),'ZONH':(-13,0),'ZONI':(-19,-9),'ZONJ':(5,-22),'ZONK':(15,9),'SE':(8,22),'S':(-14,-15),'NOR':(10,12),'NTE':(-10,14),'NES':(12,14),'CORI':(10,-15),'0004':(-18,-9),'0006':(18,-5),'0035':(-10,17),'KZN':(18,-1)}
    off=offsets.get(n['id'],(0,12))
    ax.annotate(lab,xy,textcoords='offset points',xytext=off,ha='center',va='center',fontsize=8.4 if not detail else 8.7,color='#000000',zorder=8,path_effects=[pe.withStroke(linewidth=2.5,foreground='white')])
def battery_icon(ax,xy,gw):
    side=math.sqrt(24+5*gw);d=DrawingArea(side+4,side+4,0,0)
    d.add_artist(Rectangle((1,1),side,side,facecolor='white',edgecolor=TEAL,lw=1.5));d.add_artist(Rectangle((side*.37,side+1),side*.26,2,facecolor=TEAL,edgecolor=TEAL,lw=.5));d.add_artist(Line2D([side*.3,side*.7],[side*.52,side*.52],color=TEAL,lw=1))
    a=AnnotationBbox(d,xy,xybox=(11,-9),xycoords='data',boxcoords='offset points',frameon=False,arrowprops={'arrowstyle':'-','color':TEAL,'lw':.6},pad=0,zorder=7);ax.add_artist(a)

def overview(data):
    from build_publication_figures import overview as publication_overview
    publication_overview(data)


def feeder(ax,n,title):
    ax.set_xlim(0,1);ax.set_ylim(0,1);ax.axis('off');ax.set_title(title,loc='left',fontsize=10,color='#000000')
    currentnorm=Normalize(0,1.15);voltnorm=TwoSlopeNorm(vmin=.94,vcenter=1,vmax=1.06)
    xs=[.07,.5,.93]
    for j,line in enumerate(['l1','l2']):
        ratio=n['line_peak_current_A'][line]/n['line_normal_amps'][line]
        ax.plot(xs[j:j+2],[.76,.76],lw=7,color=plt.get_cmap('YlOrRd')(currentnorm(ratio)),solid_capstyle='round',zorder=1)
        ax.text((xs[j]+xs[j+1])/2,.86,f'{line}: {ratio*100:.1f}% of rating',ha='center',fontsize=8.1)
    for x,b in zip(xs,['src','b1','b2']):
        ax.scatter(x,.76,s=210,c='#f0f2f3',edgecolors='#455c68',lw=1,zorder=3);ax.text(x,.63,b,ha='center',fontsize=8.5)
    ax.text(.35,.56,'Load + battery',ha='center',fontsize=7.2,color='#000000');ax.text(.78,.56,'PV + load + battery',ha='center',fontsize=7.2,color='#000000')
    ax.text(.02,.44,'Annual feeder voltage range',fontsize=8,color='#000000')
    vx=lambda v:.08+(v-.94)/.12*.84
    ax.add_patch(Rectangle((vx(.95),.33),vx(1.05)-vx(.95),.055,facecolor='#e8f0ec',edgecolor='none'))
    lo,hi=n['worst_low_pu'],n['worst_high_pu'];ax.plot([vx(lo),vx(hi)],[.357,.357],color='#7d8d94',lw=2,zorder=2)
    for v in [lo,hi]:ax.scatter(vx(v),.357,s=75,c=[plt.get_cmap('RdYlBu')(voltnorm(v))],edgecolors='#4b606b',lw=.6,zorder=3)
    ax.text(.5,.27,f'{lo:.3f} to {hi:.3f} p.u. (allowed 0.95-1.05)',ha='center',fontsize=8.1)
    for y,k,v in [(.18,'Voltage violation hours',n['voltage_violation_hours']),(.095,'Thermal overload hours',n['thermal_overload_hours']),(.01,'Line rating (A)',int(n['line_normal_amps']['l1']))]:
        ax.text(.02,y,k,fontsize=9,color='#000000');ax.text(.98,y,str(v),ha='right',fontsize=8.6,weight='bold',color='#000000')

def detail(data):
    from build_publication_figures import ercot_detail
    ercot_detail(data)


def save(fig,name):
    p=HERE/'figures';p.mkdir(exist_ok=True);fig.savefig(p/(name+'.pdf'));fig.savefig(p/(name+'.png'),dpi=240);plt.close(fig)
def render(data):
    from build_publication_figures import render as publication_render
    publication_render(data)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path);p.add_argument('--evidence',type=Path,default=EV/'planning_maps.json');a=p.parse_args();render(extract(a.root) if a.root else json.loads(a.evidence.read_text()))
