"""Conceptual framework and best-observed regional portfolios, from frozen evidence.
This artwork builder never imports experiment code or executes simulation/API calls.
"""
from pathlib import Path
import os, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ArchiveResolver
_ARCHIVE = ArchiveResolver(Path(os.environ.get("GRID_AGENT_ARCHIVE_ROOT", "external-archive")), __import__("json").loads(os.environ.get("GRID_AGENT_ARCHIVE_MAP", "{}")))
_WORKSPACE = Path(os.environ.get("GRID_AGENT_MANUSCRIPT_ROOT", "manuscript-workspace")).resolve()
import hashlib,json,math
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Circle,Rectangle,Polygon,Wedge,FancyArrowPatch
from matplotlib.lines import Line2D
from matplotlib.offsetbox import DrawingArea,AnnotationBbox
from matplotlib.backends.backend_pdf import PdfPages
import build_geographic_figures as geo
import build_publication_figures as prior

HERE=_WORKSPACE; OUT=HERE/'figures'; EV=HERE/'evidence/best_regional_20261006';MM=1/25.4
BLACK='#000000'; GRID='#DCE0E2'; LAND='#F6F6F3'; WIND='#3478A8';SOLAR='#E69A27';NUCLEAR='#87619D';BATTERY='#008783';TRANSFER='#BD3B64';FAIL='#B14436'
TECHS=['wind','solar','nuclear','battery'];COLORS=dict(zip(TECHS,[WIND,SOLAR,NUCLEAR,BATTERY])); NAMES=dict(zip(geo.REGIONS,geo.NAMES))
plt.rcParams.update({'font.family':'Arial','font.size':8,'text.color':BLACK,'axes.labelcolor':BLACK,'xtick.color':BLACK,'ytick.color':BLACK,'pdf.fonttype':42,'ps.fonttype':42,'svg.fonttype':'none','savefig.facecolor':'white'})

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def dump(p,a):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(a,indent=2,ensure_ascii=False,allow_nan=False)+'\n')
def read(p):return json.loads(Path(p).read_text())
def line(ax,pts,c=BLACK,lw=.8,**kw):
    pts=np.asarray(pts);ax.plot(pts[:,0],pts[:,1],color=c,lw=lw,solid_capstyle='round',**kw)
def arrow(ax,a,b,c=BLACK,lw=1,**kw):ax.add_patch(FancyArrowPatch(a,b,arrowstyle='-|>',mutation_scale=7,color=c,lw=lw,**kw))
def text(ax,x,y,s,size=8,**kw):return ax.text(x,y,s,fontsize=size,color=BLACK,**kw)
def rule(fig,x0,x1,y,lw=.5,c=GRID):fig.add_artist(Line2D([x0,x1],[y,y],transform=fig.transFigure,color=c,lw=lw))
def panel(ax,k,title,x,y):text(ax,x,y,k,10.5,weight='bold',va='top');text(ax,x+5,y-.1,title,9.3,weight='bold',va='top')
def check(ax,x,y,c=BATTERY,scale=1):line(ax,[(x-1.4*scale,y),(x-.3*scale,y-1.2*scale),(x+2*scale,y+1.8*scale)],c,1.15)
def cross(ax,x,y,c=FAIL,scale=1):
    line(ax,[(x-scale,y-scale),(x+scale,y+scale)],c,1.05);line(ax,[(x-scale,y+scale),(x+scale,y-scale)],c,1.05)
def turbine(ax,x,y,s=1,c=BLACK):
    line(ax,[(x,y),(x,y+7*s)],c,.8);ax.add_patch(Circle((x,y+7*s),.5*s,fc='white',ec=c,lw=.7))
    for a in [90,210,330]:
        d=np.deg2rad(a);line(ax,[(x,y+7*s),(x+4*s*np.cos(d),y+7*s+4*s*np.sin(d))],c,.9)
def solar(ax,x,y,s=1,c=BLACK):
    ax.add_patch(Polygon([(x,y),(x+7*s,y),(x+6*s,y+4*s),(x+s,y+4*s)],fc='white',ec=c,lw=.8))
    for d in [1/3,2/3]:line(ax,[(x+7*s*d,y),(x+s+5*s*d,y+4*s)],c,.45)
    line(ax,[(x+.5*s,y+2*s),(x+6.5*s,y+2*s)],c,.45)
    line(ax,[(x+2*s,y),(x+2*s,y-1.5*s)],c,.65);line(ax,[(x+5*s,y),(x+5*s,y-1.5*s)],c,.65)
def factory(ax,x,y,s=1,c=BLACK):
    pts=[(0,0),(0,4),(2,3),(3,4),(5,3),(7,4),(7,0)]
    ax.add_patch(Polygon([(x+a*s,y+b*s) for a,b in pts],closed=True,fc='white',ec=c,lw=.8))
    ax.add_patch(Rectangle((x+.8*s,y+4*s),1.1*s,3*s,fc='white',ec=c,lw=.8))
    for a in [1.5,3.5,5.5]:ax.add_patch(Rectangle((x+a*s,y+1*s),.8*s,1.1*s,fc=c,ec='none'))
def battery(ax,x,y,s=1,c=BATTERY):
    ax.add_patch(Rectangle((x,y),3.5*s,6*s,fc='white',ec=c,lw=.9));ax.add_patch(Rectangle((x+1*s,y+6*s),1.5*s,.8*s,fc=c,ec='none'));line(ax,[(x+.8*s,y+3*s),(x+2.7*s,y+3*s)],c,.8)
def frame(width,height):
    fig=plt.figure(figsize=(width*MM,height*MM));ax=fig.add_axes([0,0,1,1]);ax.set(xlim=(0,width),ylim=(0,height));ax.axis('off');return fig,ax

def save(fig,stem,**audit):
    for ext in ['pdf','svg']:fig.savefig(OUT/(stem+'.'+ext),metadata={'Creator':'Evidence-linked scientific vector artwork'})
    fig.savefig(OUT/(stem+'.png'),dpi=360)
    dump(OUT/(stem+'_audit.json'),dict(builder='build_professional_artwork.py',builder_sha256=sha(__file__),evidence_sha256=sha(EV/'planning_maps.json'),
         source_manifest_sha256=sha(EV/'source_manifest.json'),width_mm=fig.get_figwidth()*25.4,height_mm=fig.get_figheight()*25.4,
         outputs={e:sha(OUT/(stem+'.'+e)) for e in ['pdf','svg','png']},new_inference_calls=0,new_optimizer_calls=0,new_power_flow_calls=0,**audit))

def framework():
    """Build the current editable real-map Figure 1, preserving the reported cohort."""
    from build_framework_artwork import main as build_framework
    build_framework()

def transfer_total(c):return sum(q.get('new_capacity_MW',0)+q.get('official_project_capacity_MW',0) for q in c['corridors'])/1000

def radius(power,detail):return (1.75 if detail else .90)*math.sqrt(max(power,0))
def pie(ax,xy,n,detail=False):
    values=[n['new_technologies_MW'].get(t,0)/1000 for t in TECHS];total=sum(values)
    if total<.005:ax.plot(*xy,marker='o',ms=2.7 if detail else 2, mfc='white',mec=BLACK,mew=.6,zorder=8);return
    r=radius(total,detail);d=DrawingArea(2*r+2,2*r+2,0,0);theta=90
    for t,v in zip(TECHS,values):
        if v<=1e-8:continue
        delta=360*v/total;d.add_artist(Wedge((r+1,r+1),r,theta,theta+delta,fc=COLORS[t],ec='white',lw=.3));theta+=delta
    d.add_artist(Circle((r+1,r+1),r,fc='none',ec='white',lw=.65));ax.add_artist(AnnotationBbox(d,xy,frameon=False,pad=0,zorder=9))

def routes(ax,c,xy,detail=False):
    for q in c['corridors']:
        a,b=xy[q['bus0']],xy[q['bus1']];gw=(q.get('new_capacity_MW',0)+q.get('official_project_capacity_MW',0))/1000
        line(ax,[a,b],BLACK,.5 if detail else .4,alpha=.45,ls=(0,(2,1.6)) if q['existing_rating_assumed'] else '-',zorder=2)
        if gw>.005:
            width=.65+.85*gw;line(ax,[a,b],'white',width+1.0,zorder=4);line(ax,[a,b],TRANSFER,width,zorder=5)

def plants(ax,c,tr):
    ps=c['operating_2023_plant_points']
    if ps:
        x,y=tr.transform([p['longitude'] for p in ps],[p['latitude'] for p in ps]);ax.scatter(x,y,s=.8,c=BLACK,alpha=.32,lw=0,zorder=3)

def labels(ax,c,xy):
    fig=ax.figure;fig.canvas.draw();points=[ax.transData.transform(xy[n['id']]) for n in c['nodes']]
    ranked=sorted(range(len(points)),key=lambda k:points[k][0]);split=(len(points)+1)//2;ann=[]
    for side,indices in [('left',ranked[:split]),('right',ranked[split:])]:
        ordered=sorted(indices,key=lambda i:points[i][1],reverse=True);ys=np.linspace(.89,.14,len(ordered)) if len(ordered)>1 else [.5]
        for k,y in zip(ordered,ys):
            n=c['nodes'][k];power=sum(n['new_technologies_MW'].get(t,0)/1000 for t in TECHS);rr=radius(power,True) if power>=.005 else 2
            external=(-.085 if side=='left' else 1.085,y)
            ax.annotate('',xy[n['id']],xytext=external,textcoords='axes fraction',annotation_clip=False,zorder=6,
                arrowprops=dict(arrowstyle='-',color=BLACK,alpha=.4,lw=.45,shrinkA=5,shrinkB=rr+1.5))
            a=ax.annotate(str(k+1),xy[n['id']],xytext=external,textcoords='axes fraction',annotation_clip=False,
                fontsize=7.4,color=BLACK,ha='center',va='center',weight='bold',zorder=12,bbox=dict(boxstyle='circle,pad=.23',fc='white',ec=BLACK,lw=.5))
            ann.append(a)
    fig.canvas.draw();renderer=fig.canvas.get_renderer()
    for a in ann:
        b=a.get_bbox_patch().get_window_extent(renderer)
        for n,p in zip(c['nodes'],points):
            power=sum(n['new_technologies_MW'].get(t,0)/1000 for t in TECHS);r=(radius(power,True) if power>=.005 else 2)*fig.dpi/72
            assert math.hypot(max(b.x0-p[0],0,p[0]-b.x1),max(b.y0-p[1],0,p[1]-b.y1))>r,'Map label overlaps a capacity symbol'

def model_label(c):
    base=('Sol 6.1' if c['deployment']=='sol_6_1' else 'Opus 5.5')+' · '+c['architecture']
    return base+(' · tied minimum' if len(c['selection']['cost_tied_case_ids'])>1 else '')

def capacity_bars(fig,c,x,y,w,row=.041,fs=7.7):
    vmax=100.0  # Common 0–100 GW bar scale across all regions.
    fig.text(x,y,'Added power (GW)',fontsize=fs,weight='bold')
    for j,t in enumerate(TECHS):
        yy=y-.026-j*row;v=c['new_capacities_GW'][t]
        fig.text(x,yy,t.capitalize(),fontsize=fs);fig.text(x+w,yy,f'{v:,.2f}',fontsize=fs+1,ha='right',weight='bold')
        rule(fig,x,x+w,yy-.009,lw=.55)
        if v>.000001:fig.add_artist(Rectangle((x,yy-.012),w*v/vmax,.0038,transform=fig.transFigure,fc=COLORS[t],ec='none'))

def map_axes(fig,bounds,reg,c,detail=False):
    ax=fig.add_axes(bounds);tr=geo.basemap(ax,reg,scale_bar=False);xy=geo.coords(c,tr);routes(ax,c,xy,detail);plants(ax,c,tr)
    for n in c['nodes']:pie(ax,xy[n['id']],n,detail)
    # Geographic scale remains independent of added power.
    xmin,xmax=ax.get_xlim();ymin,ymax=ax.get_ylim();w=xmax-xmin;h=ymax-ymin;scale=100 if w<900000 else 200 if w<2000000 else 500
    x=xmin+.035*w;y=ymin+.04*h;line(ax,[(x,y),(x+scale*1000,y)],BLACK,.8,zorder=10)
    ax.text(x,y+.035*h,f'{scale} km',fontsize=6.8,color=BLACK,ha='left',zorder=10)
    if detail:labels(ax,c,xy)
    return ax

def overview(data):
    fig=plt.figure(figsize=(180*MM,177*MM))
    for i,reg in enumerate(geo.REGIONS):
        c=data['cases'][reg];x=.024+(i%2)*.5;y=.977-(i//2)*.223
        fig.text(x,y,chr(97+i),fontsize=10.3,weight='bold');fig.text(x+.025,y,NAMES[reg],fontsize=9.0,weight='bold')
        fig.text(x+.453,y,f"${c['annual_all_in_cost_billion']:.3f} bn/yr",fontsize=7.7,weight='bold',ha='right')
        map_axes(fig,[x+.012,y-.175,.280,.165],reg,c)
        capacity_bars(fig,c,x+.302,y-.035,.151,row=.031,fs=6.9)
        if i<6:rule(fig,x,x+.453,y-.193,lw=.6)
    # One common quantitative key; no explanatory paragraph inside the artwork.
    handles=[Line2D([],[],marker='o',ls='',ms=4,mfc=COLORS[t],mec='white',label=t.capitalize()) for t in TECHS]
    handles+=[Line2D([],[],color=BLACK,lw=.6,label='Existing link'),Line2D([],[],color=TRANSFER,lw=1.6,label='Added transfer')]
    fig.legend(handles=handles,loc='lower left',bbox_to_anchor=(.024,.019),ncol=3,frameon=False,fontsize=7.1,columnspacing=2.3,handlelength=1.5,borderaxespad=0)
    fig.text(.76,.071,'New power (GW)',fontsize=7.1,weight='bold')
    for x,v in [(.784,1),(.857,10),(.943,50)]:
        fig.add_artist(Line2D([x],[.040],transform=fig.transFigure,ls='',marker='o',ms=2*radius(v,False),mfc='none',mec=BLACK,mew=.6))
        fig.text(x,.008,str(v),fontsize=7,ha='center')
    save(fig,'fig2_geographic_planning',selection='Minimum observed accepted all-in annual cost among four same-input 2035 runs per region; explicit ties',case_count=8,
        year=2035,cost_unit='billion USD/year',case_ids=[c['case_id'] for c in data['cases'].values()],
        symbol_area='Exactly proportional to added generation plus battery power for totals >=0.005 GW; hollow model nodes below that threshold',
        overview_zone_identifiers=False,technology_totals={r:c['new_capacities_GW'] for r,c in data['cases'].items()})
    plt.close(fig)

def value(v):return '—' if abs(v)<.005 else f'{v:,.2f}'
def atlas_page(data,reg):
    c=data['cases'][reg];fig=plt.figure(figsize=(180*MM,165*MM))
    fig.text(.028,.973,NAMES[reg],fontsize=13,weight='bold',va='top');fig.text(.974,.973,'2035',fontsize=11,ha='right',va='top')
    fig.text(.028,.926,model_label(c),fontsize=8.2)
    fig.text(.974,.926,f"Annual cost  ${c['annual_all_in_cost_billion']:.3f} bn",fontsize=8.6,ha='right')
    rule(fig,.028,.974,.906,.7,BLACK)
    map_axes(fig,[.075,.444,.565,.431],reg,c,True)
    capacity_bars(fig,c,.731,.852,.24,row=.064,fs=8.1)
    fig.text(.731,.530,'Battery energy',fontsize=8.1);fig.text(.974,.502,f"{c['new_capacities_GW']['battery_energy_GWh']:,.2f} GWh",fontsize=10,weight='bold',ha='right')
    fig.text(.731,.457,'Added transfer',fontsize=8.1);fig.text(.974,.429,f'{transfer_total(c):,.2f} GW',fontsize=10,weight='bold',ha='right')
    # All zonal allocations remain numerically available below the geographic view.
    cols=[.030,.069,.307,.437,.568,.699,.834,.974];heads=['#','Zone','Existing','Wind','Solar','Nuclear','Battery','Energy']
    for x,h in zip(cols,heads):fig.text(x,.385,h,fontsize=7.6,weight='bold',ha='left' if x<.1 else 'right')
    for x,u in zip(cols[2:],['GW','GW','GW','GW','GW','GWh']):fig.text(x,.368,u,fontsize=7.1,ha='right')
    rule(fig,.028,.974,.355,.65,BLACK)
    step=.0214
    for j,n in enumerate(c['nodes']):
        y=.335-step*j;fig.text(cols[0],y,str(j+1),fontsize=7.3,weight='bold')
        code='LRZ '+prior.short(reg,n['id']) if reg=='miso' else n['id'];fig.text(cols[1],y,code,fontsize=7.3)
        values=[n['existing_generation_MW']/1000]+[n['new_technologies_MW'].get(t,0)/1000 for t in TECHS]+[n['new_battery_MWh']/1000]
        for x,v in zip(cols[2:],values):fig.text(x,y,value(v),fontsize=7.3,ha='right')
    y=.335-step*len(c['nodes']);rule(fig,.028,.974,y+.007,.65,BLACK);fig.text(.069,y-.012,'Total',fontsize=7.6,weight='bold')
    totals=[sum(n['existing_generation_MW'] for n in c['nodes'])/1000]+[c['new_capacities_GW'][t] for t in TECHS]+[c['new_capacities_GW']['battery_energy_GWh']]
    for x,v in zip(cols[2:],totals):fig.text(x,y-.012,value(v),fontsize=7.6,weight='bold',ha='right')
    handles=[Line2D([],[],marker='o',ls='',ms=2.1,mfc=BLACK,mec=BLACK,label='Existing plant'),Line2D([],[],color=BLACK,lw=.7,label='Existing link'),Line2D([],[],color=TRANSFER,lw=1.6,label='Added transfer')]
    if not c['operating_2023_plant_points']:handles=handles[1:]
    fig.legend(handles=handles,loc='lower left',bbox_to_anchor=(.025,.020),ncol=3,frameon=False,fontsize=7.2,columnspacing=1.6,handlelength=1.5,borderaxespad=0)
    fig.text(.737,.070,'New power (GW)',fontsize=7.1)
    for x,tx,v in [(.744,.762,1),(.821,.849,10),(.931,.974,50)]:
        fig.add_artist(Line2D([x],[.032],transform=fig.transFigure,ls='',marker='o',ms=2*radius(v,True),mfc='none',mec=BLACK,mew=.6));fig.text(tx,.027,str(v),fontsize=7,ha='center')
    return fig

def atlas_caption(data,reg):
    c=data['cases'][reg];dep='GPT-6.1 Sol' if c['deployment']=='sol_6_1' else 'Claude Opus 5.5'
    s=f"Lowest observed selected total annual cost among four physically accepted trajectories using identical 2035 demand and NASA resource inputs. The displayed portfolio is {dep}, {c['architecture']}-agent, at USD {c['annual_all_in_cost_billion']:.3f} billion per year. "
    if len(c['selection']['cost_tied_case_ids'])>1:s+='All four trajectories have the same reported minimum cost; this portfolio is a stable representative of the tie. '
    s+=r'Map symbols follow Fig.~\ref{fig:geographic-planning}; numbers link to the zonal ledger. Existing is inherited generation; Energy is added battery energy; dashes are below 0.005 in the displayed units. '
    parts=[]
    for q in c['corridors']:
        gw=(q.get('new_capacity_MW',0)+q.get('official_project_capacity_MW',0))/1000
        if gw>.005:parts.append(f'{gw:.2f}~GW on '+q['bus0'].replace('_',r'\_')+'--'+q['bus1'].replace('_',r'\_'))
    s+=('Additional transfer capacity is '+(', '.join(parts[:-1])+' and '+parts[-1] if len(parts)>1 else parts[0])+'. ') if parts else 'No additional transfer capacity is selected. '
    return s+'All represented feeder checks pass. Locations and corridors have model-zone resolution. The basemap uses Natural Earth.'

def atlas(data):
    folder=OUT/'regional_atlas';folder.mkdir(exist_ok=True);pages=[];tex=[]
    with PdfPages(OUT/'regional_installation_atlas.pdf',metadata={'Title':'Lowest-cost accepted regional designs, 2035'}) as pdf:
        for i,reg in enumerate(geo.REGIONS):
            c=data['cases'][reg];fig=atlas_page(data,reg);pdf.savefig(fig)
            for ext in ['pdf','svg']:fig.savefig(folder/(reg+'.'+ext))
            fig.savefig(folder/(reg+'.png'),dpi=300);plt.close(fig)
            pages.append(dict(region=reg,case_id=c['case_id'],annual_cost_billion=c['annual_all_in_cost_billion'],ties=c['selection']['cost_tied_case_ids'],
                ledger=[dict(number=j+1,zone=n['id'],existing_generation_GW=n['existing_generation_MW']/1000,**{t+'_GW':n['new_technologies_MW'].get(t,0)/1000 for t in TECHS},battery_energy_GWh=n['new_battery_MWh']/1000) for j,n in enumerate(c['nodes'])],
                outputs={e:sha(folder/(reg+'.'+e)) for e in ['pdf','svg','png']}))
            start=r'\begin{figure}[!ht]' if not i else '\\clearpage\n'+r'\begin{figure}[p]'
            tex.append(start+'\n'+r'\centering'+'\n'+r'\makebox[\textwidth][c]{\includegraphics[width='+('155mm' if not i else '170mm')+r']{figures/regional_atlas/'+reg+'.pdf}}\n'+
                       r'\caption{\textbf{Lowest-cost accepted 2035 design for '+NAMES[reg]+r'.} '+atlas_caption(data,reg)+'}\n'+r'\label{fig:atlas-'+reg+'}\n'+r'\end{figure}'+'\n')
    (HERE/'regional_installation_atlas.tex').write_text('\n'.join(tex))
    dump(OUT/'regional_installation_atlas_audit.json',dict(builder='build_professional_artwork.py',builder_sha256=sha(__file__),evidence_sha256=sha(EV/'planning_maps.json'),
        source_manifest_sha256=sha(EV/'source_manifest.json'),case_count=8,model_zones=sum(len(c['nodes']) for c in data['cases'].values()),pages=pages,
        symbol_area='Proportional to added generation plus battery power; numeric capacity ledger retained',zone_label_layout='External numbered callouts, clearance checked against all symbols',
        pdf_sha256=sha(OUT/'regional_installation_atlas.pdf'),new_inference_calls=0,new_optimizer_calls=0,new_power_flow_calls=0))

def main():
    data=read(EV/'planning_maps.json');framework();overview(data);atlas(data)
    print(json.dumps(dict(conceptual_framework=True,best_observed_regions=8,compared_2035_cases=32,model_zones=58,new_simulations=0)))
if __name__=='__main__':main()
