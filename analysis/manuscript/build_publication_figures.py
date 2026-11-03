#!/usr/bin/env python3
"""Publication vector artwork from frozen evidence; no inference or numerical solves."""
from pathlib import Path
import os, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ArchiveResolver
_ARCHIVE = ArchiveResolver(Path(os.environ.get("GRID_AGENT_ARCHIVE_ROOT", "external-archive")), __import__("json").loads(os.environ.get("GRID_AGENT_ARCHIVE_MAP", "{}")))
_WORKSPACE = Path(os.environ.get("GRID_AGENT_MANUSCRIPT_ROOT", "manuscript-workspace")).resolve()
import hashlib
import json
import math

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Polygon, Circle, Wedge, FancyArrowPatch
from matplotlib.lines import Line2D
from matplotlib.offsetbox import AnnotationBbox, DrawingArea
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib import patheffects
import numpy as np

import build_geographic_figures as geo

HERE = _WORKSPACE
OUT = HERE / 'figures'
EV = HERE / 'evidence/geographic_20261003'
SOURCE = EV / 'planning_maps.json'
MM = 1 / 25.4
INK = '#22343C'
TEXT = '#000000'
LIGHT = '#E3E9EA'
SOLAR = '#E79A28'
WIND = '#447EAA'
NUCLEAR = '#8B639F'
BATTERY = '#188A88'
TRANSFER = '#C4486B'
RED = '#BB5C46'
TECHS = ['wind', 'solar', 'nuclear', 'battery']
COLORS = dict(zip(TECHS, [WIND, SOLAR, NUCLEAR, BATTERY]))
NAMES = dict(zip(geo.REGIONS, geo.NAMES))
plt.rcParams.update({'font.family': 'Arial', 'font.size': 7, 'pdf.fonttype': 42,
                     'ps.fonttype': 42, 'svg.fonttype': 'none', 'savefig.facecolor': 'white',
                     'axes.linewidth': .5, 'text.color': TEXT, 'axes.labelcolor': TEXT,
                     'axes.titlecolor': TEXT, 'xtick.color': TEXT, 'ytick.color': TEXT})

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')

def data_load():
    data = json.loads(SOURCE.read_text())
    assert len(data['cases']) == 8
    for c in data['cases'].values():
        assert c['accepted'] and c['demand_year'] == c['weather_year'] == 2035
        for t in TECHS:
            assert math.isclose(sum(n['new_technologies_MW'].get(t, 0) for n in c['nodes']) / 1000,
                                c['new_capacities_GW'][t], abs_tol=1e-5)
        assert math.isclose(sum(n['new_battery_MWh'] for n in c['nodes']) / 1000,
                            c['new_capacities_GW']['battery_energy_GWh'], abs_tol=1e-5)
    f=data['cases']['ercot']['feeder_case']
    assert f['initial']['line_normal_amps']=={'l1':400.,'l2':400.}
    assert f['selected']['line_normal_amps']=={'l1':600.,'l2':600.}
    assert f['config']['line_upgrade_impedance_factor']==.5
    return data

def audit(stem, data, **values):
    exts = ['pdf', 'png', 'svg']
    dump(OUT / (stem + '_audit.json'), {
        'schema_version': 2, 'builder_sha256': sha(__file__), 'evidence_sha256': sha(SOURCE),
        'source_manifest_sha256': sha(EV / 'source_manifest.json'), 'demand_year': 2035,
        'weather_year': 2035, 'source_spatial_semantics': data['spatial_semantics'],
        'rendering_semantics': {'technology_colours': 'Wind blue, solar orange, nuclear purple, battery teal',
                                'pie_area': 'Increases with added generation plus battery power, with a minimum visible area',
                                'locations': 'Representative model zones; black plant points only from archived US inventory',
                                'transfer': 'Straight model corridors; width uses selected new plus official-project capacity',
                                'feeder_voltage': 'Annual numerical minimum and maximum reported as text, not inferred bus colours',
                                'feeder_current': 'Line colour uses annual peak current divided by that line rating'},
        'typography_policy': 'Black text; semantic colour only; explanatory paragraphs are in manuscript captions',
        'editorial_policy': 'Essential labels, units, legends and quantitative ledgers only inside artwork',
        'physical_acceptance_required': True,
        'new_inference_calls': 0, 'new_optimizer_calls': 0, 'new_power_flow_calls': 0,
        'outputs': {ext: sha(OUT / (stem + '.' + ext)) for ext in exts if (OUT / (stem + '.' + ext)).exists()},
        **values})

def save(fig, stem, data, **values):
    OUT.mkdir(exist_ok=True)
    for ext in ('pdf', 'svg'):
        fig.savefig(OUT / (stem + '.' + ext), metadata={'Creator': 'Frozen-evidence publication artwork'})
    fig.savefig(OUT / (stem + '.png'), dpi=360)
    audit(stem, data, width_mm=fig.get_figwidth()*25.4, height_mm=fig.get_figheight()*25.4, **values)

def panel(ax, letter, title, x, y, size=8.2):
    ax.text(x, y, letter, fontsize=9.5, weight='bold', va='top', color=TEXT)
    ax.text(x+4.2, y-.15, title, fontsize=size, weight='bold', va='top', color=TEXT)

def line(ax, points, color=INK, lw=.8, **kw):
    a = np.asarray(points)
    ax.plot(a[:, 0], a[:, 1], color=color, lw=lw, solid_capstyle='round', **kw)

def arrow(ax, a, b, c=INK, lw=1, style='-|>', connection=None):
    kw={} if connection is None else {'connectionstyle':connection}
    ax.add_patch(FancyArrowPatch(a,b,arrowstyle=style,mutation_scale=6,color=c,lw=lw,**kw))

def pie(ax, xy, node, detail=False):
    vals=[node['new_technologies_MW'].get(t,0)/1000 for t in TECHS]
    total=sum(vals)
    if total < 1e-7:
        ax.plot(*xy,marker='o',ms=3.2 if detail else 2.3,mfc='white',mec=INK,mew=.75,zorder=8)
        return
    radius=(1.02 if detail else .55)*math.sqrt(9+total*1.0)
    d=DrawingArea(2*radius+2,2*radius+2,0,0)
    theta=90
    for t,v in zip(TECHS,vals):
        if v<=1e-8:continue
        delta=360*v/total
        d.add_artist(Wedge((radius+1,radius+1),radius,theta,theta+delta,facecolor=COLORS[t],edgecolor='white',lw=.35))
        theta+=delta
    d.add_artist(Circle((radius+1,radius+1),radius,fc='none',ec='white',lw=.7))
    ax.add_artist(AnnotationBbox(d,xy,frameon=False,pad=0,zorder=8))

def model_routes(ax, case, xy, detail=False):
    for q in case['corridors']:
        a,b=xy[q['bus0']],xy[q['bus1']]
        ax.plot([a[0],b[0]],[a[1],b[1]],color=INK,lw=.45 if not detail else .65,
                alpha=.43,ls=(0,(2,1.5)) if q['existing_rating_assumed'] else '-',zorder=2)
        gw=(q.get('new_capacity_MW',0)+q.get('official_project_capacity_MW',0))/1000
        if gw>1e-8:
            ax.plot([a[0],b[0]],[a[1],b[1]],color='white',lw=1.5+gw*.9,zorder=4)
            ax.plot([a[0],b[0]],[a[1],b[1]],color=TRANSFER,lw=.8+gw*.9,zorder=5)

def black_plants(ax, case, trans, detail=False):
    ps=case['operating_2023_plant_points']
    if ps:
        x,y=trans.transform([p['longitude'] for p in ps],[p['latitude'] for p in ps])
        ax.scatter(x,y,s=1.7 if not detail else 2.1,c=INK,alpha=.33,linewidths=0,zorder=3)

def short(reg, code):
    if reg=='miso':return {'0001':'1','0004':'4','0006':'6','0027':'2+7','0035':'3+5','8910':'8+9+10'}[code]
    if reg=='nyiso':return code.replace('ZON','')
    if reg=='australia':return code.replace('1','')
    return code

def transfer_total(c):
    return sum(q.get('new_capacity_MW',0)+q.get('official_project_capacity_MW',0) for q in c['corridors'])/1000

def format_value(v, precision=2):
    if abs(v)<1e-7:return '—'
    if abs(v)<10**(-precision):return '<'+str(10**(-precision))
    return f'{v:,.{precision}f}'

def labelled_nodes(ax, case, xy, reg, detail=False):
    """Keep zone identifiers outside the geographic panel, with unambiguous leaders."""
    if not detail:
        return
    ax.figure.canvas.draw()
    nodes=case['nodes'];points=[ax.transData.transform(xy[n['id']]) for n in nodes]
    ranked=sorted(range(len(nodes)),key=lambda i:points[i][0])
    split=(len(nodes)+1)//2
    labels=[]
    for side,indices in [('left',ranked[:split]),('right',ranked[split:])]:
        ordered=sorted(indices,key=lambda i:points[i][1],reverse=True)
        positions=np.linspace(.88,.12,len(ordered)) if len(ordered)>1 else [.5]
        for i,ypos in zip(ordered,positions):
            node=nodes[i];vals=[node['new_technologies_MW'].get(t,0)/1000 for t in TECHS]
            radius=1.02*math.sqrt(9+sum(vals)) if sum(vals)>1e-7 else 2.4
            anno=ax.annotate(str(i+1),xy[node['id']],xytext=(-.075 if side=='left' else 1.075,ypos),
                             textcoords='axes fraction',annotation_clip=False,ha='center',va='center',
                             fontsize=7,weight='bold',color=TEXT,zorder=12,
                             bbox=dict(boxstyle='circle,pad=.20',fc='white',ec=TEXT,lw=.55),
                             arrowprops=dict(arrowstyle='-',color=INK,alpha=.5,lw=.5,
                                             shrinkA=3,shrinkB=radius+1.5))
            labels.append(anno)
    # Verify the rendered label patches clear all generation and battery symbols.
    ax.figure.canvas.draw();renderer=ax.figure.canvas.get_renderer()
    for anno in labels:
        bounds=anno.get_bbox_patch().get_window_extent(renderer)
        for node,pt in zip(nodes,points):
            power=sum(node['new_technologies_MW'].get(t,0)/1000 for t in TECHS)
            rad=(1.02*math.sqrt(9+power) if power>1e-7 else 2.4)*ax.figure.dpi/72
            dx=max(bounds.x0-pt[0],0,pt[0]-bounds.x1)
            dy=max(bounds.y0-pt[1],0,pt[1]-bounds.y1)
            assert math.hypot(dx,dy)>rad,(reg,anno.get_text(),node['id'],'label obscures capacity symbol')

def framework():
    """Build the current editable real-map Figure 1, preserving the reported cohort."""
    from build_framework_artwork import main as build_framework
    build_framework()

def overview(data=None):
    data=data or data_load();fig=plt.figure(figsize=(180*MM,164*MM))
    for i,reg in enumerate(geo.REGIONS):
        c=data['cases'][reg];x=.018+(i%2)*.5;y=.97-(i//2)*.225
        fig.text(x,y,f'{chr(97+i)}  {NAMES[reg]}',fontsize=8.7,weight='bold')
        ax=fig.add_axes([x,y-.182,.268,.169]);trans=geo.basemap(ax,reg);xy=geo.coords(c,trans)
        model_routes(ax,c,xy);black_plants(ax,c,trans)
        for n in c['nodes']:pie(ax,xy[n['id']],n)
        # Full zone identifiers are provided on the enlarged atlas, outside the map.
        sx=x+.28
        fig.text(sx,y-.017,'New power (GW)',fontsize=7,weight='bold')
        for j,t in enumerate(TECHS):
            yy=y-.043-j*.027
            fig.add_artist(Line2D([sx+.003],[yy+.003],transform=fig.transFigure,marker='o',ls='',ms=3.8,mfc=COLORS[t],mec=COLORS[t]))
            fig.text(sx+.024,yy,t.capitalize(),fontsize=7.1)
            fig.text(x+.464,yy,format_value(c['new_capacities_GW'][t]),fontsize=7.6,ha='right',weight='bold')
        fig.text(sx,y-.16,'Transfer',fontsize=7.1)
        fig.text(x+.464,y-.16,f'{transfer_total(c):.2f} GW',fontsize=7.6,ha='right',weight='bold')
        if i<6:fig.add_artist(Line2D([x,x+.472],[y-.202,y-.202],transform=fig.transFigure,color=LIGHT,lw=.6))
    handles=[Line2D([],[],marker='o',ls='',mfc=COLORS[t],mec='white',ms=5,label=t.capitalize()) for t in TECHS]
    handles += [Line2D([],[],color=INK,lw=.75,label='Existing link'),Line2D([],[],color=TRANSFER,lw=1.7,label='Added transfer')]
    fig.legend(handles=handles,loc='lower left',bbox_to_anchor=(.018,.016),ncol=3,frameon=False,
               fontsize=7.2,handlelength=1.5,columnspacing=2.2,borderaxespad=0)
    save(fig,'fig2_geographic_planning',data,case_count=8,zone_labels='No identifiers over map symbols; complete external numbering in Appendix C',
         marker_area='9 + added generation and battery power in GW, times a fixed panel-independent factor; minimum visible area',
         color_encoding={t:COLORS[t] for t in TECHS},transfer_encoding='Selected new plus official-project capacity; width = 0.8 + 0.9*GW pt',
         installed_power_by_region={r:{t:c['new_capacities_GW'][t] for t in TECHS} for r,c in data['cases'].items()},
         transfer_additions_GW={r:transfer_total(c) for r,c in data['cases'].items()})
    plt.close(fig)

def atlas_page(data, reg, index):
    c=data['cases'][reg];fig=plt.figure(figsize=(180*MM,165*MM))
    fig.text(.025,.973,NAMES[reg],fontsize=11,weight='bold',va='top')
    fig.text(.975,.973,'2035',fontsize=9,ha='right',va='top')
    handles=[Line2D([],[],marker='o',ls='',mfc=COLORS[t],mec='white',ms=4.6,label=t.capitalize()) for t in TECHS]
    handles += [Line2D([],[],color=INK,lw=.75,label='Existing link'),Line2D([],[],color=TRANSFER,lw=1.7,label='Added transfer')]
    fig.legend(handles=handles,loc='upper left',bbox_to_anchor=(.025,.938),ncol=6,frameon=False,
               fontsize=7,handlelength=1.15,columnspacing=1.4,borderaxespad=0)
    ax=fig.add_axes([.09,.415,.82,.492]);trans=geo.basemap(ax,reg);xy=geo.coords(c,trans)
    model_routes(ax,c,xy,True);black_plants(ax,c,trans,True)
    for n in c['nodes']:pie(ax,xy[n['id']],n,True)
    labelled_nodes(ax,c,xy,reg,True)
    xcols=[.027,.067,.30,.435,.57,.705,.84,.975]
    headers=['#','Zone','Existing','Wind','Solar','Nuclear','Battery','Energy']
    for x,h in zip(xcols,headers):fig.text(x,.385,h,fontsize=7.4,weight='bold',ha='left' if x<.1 else 'right')
    for x,h in zip(xcols[2:],['GW','GW','GW','GW','GW','GWh']):fig.text(x,.368,h,fontsize=7,ha='right')
    fig.add_artist(Line2D([.025,.978],[.357,.357],transform=fig.transFigure,color=TEXT,lw=.65))
    rowh=.023
    for j,n in enumerate(c['nodes']):
        y=.337-j*rowh
        fig.text(xcols[0],y,str(j+1),fontsize=7.2,weight='bold')
        code=('LRZ '+short(reg,n['id'])) if reg=='miso' else n['id']
        fig.text(xcols[1],y,code,fontsize=7.2)
        fig.text(xcols[2],y,format_value(n['existing_generation_MW']/1000),fontsize=7.2,ha='right')
        vals=[n['new_technologies_MW'].get(t,0)/1000 for t in TECHS]+[n['new_battery_MWh']/1000]
        for x,v in zip(xcols[3:],vals):fig.text(x,y,format_value(v),fontsize=7.2,ha='right')
    bottom=.337-len(c['nodes'])*rowh
    fig.add_artist(Line2D([.025,.978],[bottom+.006,bottom+.006],transform=fig.transFigure,color=TEXT,lw=.65))
    fig.text(.067,bottom-.014,'Total',fontsize=7.4,weight='bold')
    totals=[sum(n['existing_generation_MW'] for n in c['nodes'])/1000]+[c['new_capacities_GW'][t] for t in TECHS]+[c['new_capacities_GW']['battery_energy_GWh']]
    for x,v in zip(xcols[2:],totals):fig.text(x,bottom-.014,format_value(v),fontsize=7.4,weight='bold',ha='right')
    fig.text(.067,.023,'Added transfer (GW)',fontsize=7.4)
    fig.text(.30,.023,f'{transfer_total(c):.2f}',fontsize=7.4,weight='bold',ha='right')
    return fig

def atlas_caption(data, reg):
    c=data['cases'][reg]
    parts=[]
    for q in c['corridors']:
        gw=(q.get('new_capacity_MW',0)+q.get('official_project_capacity_MW',0))/1000
        if gw>1e-7:
            a=q['bus0'].replace('ZON','').replace('_',r'\_')
            b=q['bus1'].replace('ZON','').replace('_',r'\_')
            parts.append(f'{gw:.2f}~GW on '+a+'--'+b)
    text=(r'Numbered representative model locations link to the ledger. Pies and links use the encodings in Fig.~\ref{fig:geographic-planning}. Existing is inherited generation; Energy is added battery energy; a dash denotes zero at the displayed precision. '
          r'The cohort is Opus 5.5, multi-agent, with 2035 demand and matching NASA resources. ')
    text+=('Additional transfer capacity is '+(', '.join(parts[:-1])+' and '+parts[-1] if len(parts)>1 else parts[0])+'. ') if parts else 'No additional transfer capacity is selected. '
    if reg=='ercot':
        text+=r'The Far West representative-feeder intervention changes line ratings from 400 to 600~A and halves impedance; portfolio and dispatch also change. '
    return text+r'All selected representative feeders pass. Locations and straight links retain their model meaning; they do not assert exact new sites or conductor routes. The basemap uses Natural Earth.'

def atlas(data=None):
    data=data or data_load();folder=OUT/'regional_atlas';folder.mkdir(exist_ok=True)
    pages=[]
    with PdfPages(OUT/'regional_installation_atlas.pdf',metadata={'Title':'Regional installation atlas of accepted 2035 planning portfolios'}) as pdf:
        for i,reg in enumerate(geo.REGIONS):
            fig=atlas_page(data,reg,i)
            pdf.savefig(fig)
            for ext in ['pdf','svg']:
                fig.savefig(folder/(reg+'.'+ext))
            fig.savefig(folder/(reg+'.png'),dpi=300)
            pages.append({'region':reg,'case_id':data['cases'][reg]['case_id'],'model_zones':len(data['cases'][reg]['nodes']),
                          'ledger':[{'number':j+1,'zone':n['id'],'existing_generation_GW':n['existing_generation_MW']/1000,
                                     **{t+'_GW':n['new_technologies_MW'].get(t,0)/1000 for t in TECHS},
                                     'battery_energy_GWh':n['new_battery_MWh']/1000} for j,n in enumerate(data['cases'][reg]['nodes'])],
                          'outputs':{ext:sha(folder/(reg+'.'+ext)) for ext in ['pdf','svg','png']}})
            plt.close(fig)
    dump(OUT/'regional_installation_atlas_audit.json',{'builder_sha256':sha(__file__),
         'evidence_sha256':sha(SOURCE),'pages':pages,'zone_label_layout':'External left and right columns; each label patch verified clear of all capacity symbols','case_count':8,'model_zones':sum(len(c['nodes']) for c in data['cases'].values()),
         'pdf_sha256':sha(OUT/'regional_installation_atlas.pdf'),'new_inference_calls':0,'new_optimizer_calls':0,'new_power_flow_calls':0})
    tex=[]
    for i,reg in enumerate(geo.REGIONS):
        start=(r'\clearpage'+'\n'+r'\begin{figure}[p]') if i else r'\begin{figure}[!ht]'
        width='170mm' if i else '155mm'
        tex.append(start+'\n'+r'\centering'+'\n'+
                   r'\makebox[\textwidth][c]{\includegraphics[width='+width+r']{figures/regional_atlas/'+reg+r'.pdf}}'+'\n'+
                   r'\caption{\textbf{Selected zonal additions for '+NAMES[reg]+r'.} '+atlas_caption(data,reg)+'}'+'\n'+
                   r'\label{fig:atlas-'+reg+r'}'+'\n'+r'\end{figure}'+'\n')
    (HERE/'regional_installation_atlas.tex').write_text('\n'.join(tex))

def ercot_detail(data=None):
    data=data or data_load();c=data['cases']['ercot'];f=c['feeder_case']
    fig=plt.figure(figsize=(180*MM,145*MM));ax=fig.add_axes([0,0,1,1])
    ax.set(xlim=(0,180),ylim=(0,145));ax.axis('off')
    panel(ax,'a','ERCOT allocation',3,142,size=8.7)
    panel(ax,'b','Far West feeder',92,142,size=8.7)
    ma=fig.add_axes([.06,.33,.37,.60]);trans=geo.basemap(ma,'ercot');xy=geo.coords(c,trans)
    model_routes(ma,c,xy,True);black_plants(ma,c,trans,True)
    for n in c['nodes']:pie(ma,xy[n['id']],n,True)
    labelled_nodes(ma,c,xy,'ercot',True)
    line(ax,[(87,48),(87,140)],LIGHT,.65)
    for label,stage,y in [('Initial','initial',115),('Accepted','selected',78)]:
        n=f[stage]
        ax.text(93,y+14,label,fontsize=8.2,weight='bold')
        ax.text(176,y+14,f"{int(n['line_normal_amps']['l1'])} A",fontsize=8.2,ha='right')
        xs=[97,134,172]
        for j,name in enumerate(['l1','l2']):
            ratio=n['line_peak_current_A'][name]/n['line_normal_amps'][name]
            colour=plt.get_cmap('YlOrRd')(min(ratio/1.15,1))
            line(ax,[(xs[j],y),(xs[j+1],y)],colour,3.4)
            ax.text((xs[j]+xs[j+1])/2,y+4,f'{ratio*100:.1f}%',fontsize=7.5,ha='center')
        for x,name in zip(xs,['src','b1','b2']):
            ax.add_patch(Circle((x,y),1.65,fc='white',ec=TEXT,lw=.8))
            ax.text(x,y-5.8,name,ha='center',fontsize=7.2)
        ax.text(93,y-14,'Voltage (p.u.)',fontsize=7.2)
        ax.text(176,y-14,f"{n['worst_low_pu']:.3f}–{n['worst_high_pu']:.3f}",fontsize=7.5,ha='right')
    arrow(ax,(134,99),(134,94),BATTERY,1)
    cax=fig.add_axes([.65,.34,.328,.012])
    cb=fig.colorbar(plt.cm.ScalarMappable(norm=plt.Normalize(0,115),cmap='YlOrRd'),cax=cax,orientation='horizontal',ticks=[0,50,100,115])
    cb.ax.tick_params(labelsize=6.7,length=1.5,pad=2)
    ax.text(93,55,'Peak current (% rating)',fontsize=7.2)
    line(ax,[(3,43),(177,43)],LIGHT,.65)
    panel(ax,'c','New power (GW)',3,40,size=8.2)
    panel(ax,'d','Verification',92,40,size=8.2)
    for j,t in enumerate(['solar','nuclear','battery']):
        y=29-j*8.5;v=c['new_capacities_GW'][t]
        ax.text(3,y,t.capitalize(),fontsize=7.5)
        ax.text(81,y,f'{v:.2f}',fontsize=8.2,ha='right',weight='bold')
        ax.add_patch(Rectangle((30,y-.1),38*v/90,1.6,fc=COLORS[t],ec='none'))
    ax.text(3,3,'Battery energy (GWh)',fontsize=7.2)
    ax.text(81,3,f"{c['new_capacities_GW']['battery_energy_GWh']:.2f}",fontsize=7.8,ha='right',weight='bold')
    for y,label,key in [(29,'Voltage violation (h)','voltage_violation_hours'),(20.5,'Thermal overload (h)','thermal_overload_hours')]:
        ax.text(93,y,label,fontsize=7.2)
        ax.text(176,y,f"{f['initial'][key]} → {f['selected'][key]}",fontsize=8.8,ha='right',weight='bold')
    ax.text(93,12,'Feeders passed',fontsize=7.2);ax.text(176,12,'8 / 8',fontsize=8.2,ha='right',weight='bold')
    ax.text(93,3,'Feeder cost ($M/yr)',fontsize=7.2)
    ax.text(176,3,f"{c['distribution_summary']['distribution_capex_billion']*1000:.2f}",fontsize=7.8,ha='right',weight='bold')
    save(fig,'fig3_ercot_verified_expansion',data,case_count=1,
         voltage_violation_hours=[f['initial']['voltage_violation_hours'],f['selected']['voltage_violation_hours']],
         thermal_overload_hours=[f['initial']['thermal_overload_hours'],f['selected']['thermal_overload_hours']],
         feeder_geometry='Stylized three-bus numerical circuit; no asserted physical route',
         per_line_utilization={stage:{name:f[stage]['line_peak_current_A'][name]/f[stage]['line_normal_amps'][name]
                                     for name in ['l1','l2']} for stage in ['initial','selected']})
    plt.close(fig)

def render(data=None, include_framework=True):
    data=data or data_load();OUT.mkdir(exist_ok=True)
    if include_framework:framework(data)
    overview(data);atlas(data);ercot_detail(data)
    print(json.dumps({'publication_figures':['fig1_framework','fig2_geographic_planning','fig3_ercot_verified_expansion'],
                      'atlas_pages':8,'model_zones':sum(len(c['nodes']) for c in data['cases'].values()),
                      'new_inference_calls':0,'new_optimizer_calls':0,'new_power_flow_calls':0}))

if __name__=='__main__':
    render()
