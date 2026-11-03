#!/usr/bin/env python3
"""Rebuild carbon manuscript artwork from saved evidence only. No simulator/API imports."""
import argparse, csv, hashlib, json, math
from pathlib import Path
from archive_paths import resolve, analysis_root, configure, drawing_modules
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from matplotlib.backends.backend_pdf import PdfPages
import numpy as np

NAMES={'nyiso':'NYISO','miso':'MISO','mexico':'Mexico','brazil':'Brazil','south_africa':'South Africa'}
METHODS=[('sol','single'),('sol','multi'),('opus','single'),('opus','multi')]
MLABEL=['Sol single','Sol multi','Opus single','Opus multi']
COLORS=['#235789','#159895','#CF762D','#9564A4']
CASES=[('nyiso',2023),('nyiso',2030),('nyiso',2035),('miso',2030),('miso',2035),('mexico',2030),('brazil',2030),('brazil',2035),('south_africa',2030),('south_africa',2035)]
LABELS=[f'{NAMES[r]} {y}' for r,y in CASES]
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':8,'axes.labelsize':8,'axes.titlesize':10,'xtick.labelsize':7,'ytick.labelsize':8,'pdf.fonttype':42,'ps.fonttype':42,'svg.fonttype':'none','axes.spines.top':False,'axes.spines.right':False,'axes.linewidth':.6,'savefig.facecolor':'white'})

def read(p):return json.loads(Path(p).read_text())
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def dump(p,x):Path(p).write_text(json.dumps(x,indent=2,allow_nan=False)+'\n')

def extract(project,out):
    base=read(project/'Nature_Energy/evidence/reported96_20261006/results.json')
    aliases={'sol_6_1':'sol','claude_opus':'opus','opus_5_5':'opus'}
    bases={(aliases[b['deployment']],b['agent'],b['region'],b['year']):b for b in base['runs']}
    rows=[];manifest=[]
    for d in sorted((project/'co2_constraint/runs').iterdir()):
        if not (d/'campaign.json').exists():continue
        c=read(d/'campaign.json');assert c['status']=='completed'
        for j in c['jobs']:
            assert j['status']=='completed'
            p=resolve(j['output']);f=read(p/'final.json');b=read(p/'best_accepted.json');s=b['results'];dc=b['distribution_check'];cap=f['acceptance']['co2_budget']
            assert f['acceptance']['passes'] and cap['passes'] and b['acceptance']['passes']
            assert f['final_design']==b['design'] and f['final_results']==s and f['final_distribution_check']==dc
            cost=s['total_cost_billion']+dc['distribution_capex_billion'];assert math.isclose(cost,b['all_in_cost_billion'],abs_tol=1e-8)
            assert s['hours_modelled']==8760 and s['co2_emitted_Mt']<=cap['annual_cap_Mt']+cap['tolerance_Mt']
            m=f['metrics'];u=f['api_usage_summary'];k=(j['model_key'],j['agent'],j['case']['region'],j['case']['year']);br=bases[k];bp=resolve(br['source_output'])/'final.json';bf=read(bp)
            assert br['selected']['design']==bf['final_design']
            assert math.isclose(br['selected']['all_in_cost_billion'],bf['final_results']['total_cost_billion']+bf['final_distribution_check']['distribution_capex_billion'],abs_tol=1e-8)
            bc=br['selected']['all_in_cost_billion'];be=bf['final_results']['co2_emitted_Mt']
            row=dict(model=k[0],agent=k[1],region=k[2],year=k[3],cost=cost,lower=s['cost_lower_bound_billion']+dc['distribution_capex_billion'],system_cost=s['total_cost_billion'],distribution_cost=dc['distribution_capex_billion'],emissions=s['co2_emitted_Mt'],cap=cap['annual_cap_Mt'],mip_gap=s['mip_gap'],accepted=True,new_capacities=s['new_capacities_GW'],capacities=s['capacities_GW'],committed_requests=m['sim_runs'],verification_requests=m.get('verification_sims',0),api_calls=u['api_calls'],input_tokens=u['input_tokens'],output_tokens=u['output_tokens'],cycles=f.get('cycles_run'),selected_source=b['source'],selection_differs=f['final_selection']['selected_differs_from_last_attempt'],baseline_cost=bc,baseline_lower=br['selected']['all_in_cost_lower_bound_billion'],baseline_emissions=be,baseline_cap=f['co2_constraint']['original_cap_Mt'],output=str(p),baseline_output=str(bp.parent),execution_revision=bool(f.get('execution_revision')),logical_requests=m['sim_runs']+m.get('verification_sims',0))
            assert row['baseline_cap']==1000 and be<1000
            row['input_hashes_match']=all(br['input_sha256'].get(a)==v for a,v in f['input_sha256'].items())
            assert row['input_hashes_match']
            rows.append(row)
            for src in [p/'final.json',p/'best_accepted.json',bp]:manifest.append(dict(path=str(src),sha256=sha(src)))
    assert len(rows)==40
    rows.sort(key=lambda r:(CASES.index((r['region'],r['year'])),METHODS.index((r['model'],r['agent']))))
    winners=[]
    for reg,year in CASES:
        group=[r for r in rows if (r['region'],r['year'])==(reg,year)];minimum=min(r['cost'] for r in group)
        ties=[r for r in group if math.isclose(r['cost'],minimum,abs_tol=1e-8,rel_tol=0)]
        winner=min(ties,key=lambda r:METHODS.index((r['model'],r['agent'])));winners.append(dict(winner,tied_methods=[r['model']+' '+r['agent'] for r in ties]))
    stats=dict(runs=40,accepted=40,case_count=10,systems=5,base_year_runs=4,future_runs=36,baseline_exceeds_new_cap=sum(r['baseline_emissions']>r['cap']+1e-6 for r in rows),carbon_binding_at_1e_5_Mt=sum(abs(r['emissions']-r['cap'])<1e-5 for r in rows),logical_requests=sum(r['logical_requests'] for r in rows),api_calls=sum(r['api_calls'] for r in rows),input_tokens=sum(r['input_tokens'] for r in rows),output_tokens=sum(r['output_tokens'] for r in rows),methods={})
    for method,label in zip(METHODS,MLABEL):
        subset=[r for r in rows if (r['model'],r['agent'])==method]
        stats['methods'][label]=dict(unique_minima=sum(len(w['tied_methods'])==1 and (w['model'],w['agent'])==method for w in winners),minima_including_ties=sum(method[0]+' '+method[1] in w['tied_methods'] for w in winners),logical_requests=sum(r['logical_requests'] for r in subset),api_calls=sum(r['api_calls'] for r in subset))
    dump(out/'carbon_data.json',dict(rows=rows,winners=winners,stats=stats));dump(out/'source_manifest.json',manifest)
    with (out/'carbon_results.csv').open('w') as h:
        keys=[k for k,v in rows[0].items() if not isinstance(v,(list,dict))];w=csv.DictWriter(h,fieldnames=keys,extrasaction='ignore');w.writeheader();w.writerows(rows)
    return rows,winners,stats

def save(fig,out,stem,caption):
    fig.savefig(out/(stem+'.pdf'),bbox_inches='tight');fig.savefig(out/(stem+'.svg'),bbox_inches='tight');fig.savefig(out/(stem+'.png'),dpi=240,bbox_inches='tight')
    dump(out/(stem+'_audit.json'),dict(caption=caption,data_sha256=sha(out/'carbon_data.json'),builder_sha256=sha(__file__),new_api_calls=0,new_optimizer_calls=0,outputs={e:sha(out/(stem+'.'+e)) for e in ['pdf','svg','png']}));plt.close(fig)

def heat(ax,data,title,fmt,cmap='Blues',vmin=None,vmax=None):
    data=np.array(data);im=ax.imshow(data,aspect='auto',cmap=cmap,vmin=vmin,vmax=vmax)
    ax.set_xticks(range(4),MLABEL,rotation=28,ha='right');ax.set_yticks(range(10),LABELS);ax.set_title(title,loc='left',pad=12,fontweight='bold')
    for i in range(10):
        for j in range(4):
            val=data[i,j];rgba=im.cmap(im.norm(val));lum=.2126*rgba[0]+.7152*rgba[1]+.0722*rgba[2];ax.text(j,i,fmt.format(val),ha='center',va='center',fontsize=7.5,color='white' if lum<.52 else '#182A36')
    return im

def framework(out):
    fig,ax=plt.subplots(figsize=(10,7.2));ax.set(xlim=(0,10.2),ylim=(0,7));ax.axis('off')
    def box(x,y,w,h,text,color='#E8F0F3'):
        ax.add_patch(FancyBboxPatch((x,y),w,h,boxstyle='round,pad=.08',fc=color,ec='#66808C',lw=.8));ax.text(x+w/2,y+h/2,text,ha='center',va='center',fontsize=9,linespacing=1.5)
    def arrow(a,b):ax.add_patch(FancyArrowPatch(a,b,arrowstyle='-|>',mutation_scale=13,lw=1,color='#405966'))
    box(.25,6.05,9.5,.7,'10 system-year scenarios · 5 electricity systems · 8,760 hours\nFixed demand, weather, inherited assets and annual operational CO₂ budget')
    box(.25,4.3,4.3,1.0,'Single agent\nOne conversation and persistent strategy notes\nAgent chooses tool order and stopping')
    box(5.1,4.3,4.65,1.0,'Specialist team · same model within each run\nFour independent proposals → orchestrator\nOne committed design per cycle')
    arrow((2.4,6.0),(2.4,5.4));arrow((7.4,6.0),(7.4,5.4))
    box(.25,2.5,4.3,1.0,'System optimization · PyPSA / Gurobi\nChronological investment and dispatch\nFixed CO₂ budget enforced in every solve','#E2EFE9')
    box(5.1,2.5,4.65,1.0,'Representative-feeder validation · OpenDSS\nReplay the committed dispatch\nVoltage, thermal and convergence checks','#F8EBDD')
    arrow((2.4,4.2),(2.4,3.6));arrow((7.4,4.2),(4.0,3.6));arrow((4.65,3),(5.0,3))
    box(.25,.95,9.5,.85,'Numerical feedback informs the next action or stopping decision\nSingle agent updates its conversation and notes\nTeam calls evaluator → critic → refiner → compressor → judge')
    arrow((7.4,2.4),(7.4,1.9))
    ax.plot([9.85,10.05,10.05,9.85],[1.35,1.35,4.8,4.8],c='#405966',lw=1);arrow((10.05,4.8),(9.85,4.8))
    ax.text(5,.25,'Retain the lowest-cost accepted committed design; report all four trajectories per scenario.',ha='center',fontsize=9,fontweight='bold')
    save(fig,out,'fig1_carbon_framework','Carbon budget fixed by scenario and immutable to the agents. The specialist cycle executes the numerical chain after orchestration and before evaluation. Judge may request up to two uncommitted system-only verifications per cycle; these are not accepted portfolios. Diagram shows the committed simulation path; single-agent tool order remains free.')

def figures(rows,winners,out):
    matrix=lambda key:[[r[key] for r in rows[i*4:i*4+4]] for i in range(10)]
    fig,axs=plt.subplots(1,2,figsize=(10,6.3),layout='constrained')
    heat(axs[0],matrix('cost'),'a  Selected total cost (billion USD yr⁻¹)','{:.3f}')
    ratios=[[100*r['emissions']/r['cap'] for r in rows[i*4:i*4+4]] for i in range(10)]
    heat(axs[1],ratios,'b  Emissions / scenario budget (%)','{:.1f}',vmin=0,vmax=100,cmap='YlGnBu')
    save(fig,out,'fig2_carbon_outcomes','All 40 accepted selected trajectories. Cost includes system and annualized representative-feeder interventions. Emissions include modelled direct operational CO2 only. Cells in panel b round to one decimal; numerical compliance is checked at full precision.')
    fig,axs=plt.subplots(2,2,figsize=(10,8),layout='constrained');techs=['wind','solar','nuclear'];tc=['#447EAA','#E79A28','#8B639F'];y=np.arange(10)
    for ax in axs.flat:ax.set_yticks(y,LABELS);ax.invert_yaxis();ax.grid(axis='x',alpha=.15);ax.set_axisbelow(True)
    left=np.zeros(10)
    for t,c in zip(techs,tc):
        val=np.array([w['new_capacities'].get(t,0) for w in winners]);axs[0,0].barh(y,val,left=left,label=t.capitalize(),color=c,height=.65);left+=val
    axs[0,0].set_title('a  Added generation',loc='left',fontweight='bold');axs[0,0].set_xlabel('GW');axs[0,0].legend(frameon=False,fontsize=7,loc='lower right')
    for ax,key,title,unit in [(axs[0,1],'battery','b  Added battery power','GW'),(axs[1,0],'battery_energy_GWh','c  Added battery energy','GWh')]:
        ax.barh(y,[w['new_capacities'].get(key,0) for w in winners],color='#188A88',height=.65);ax.set_title(title,loc='left',fontweight='bold');ax.set_xlabel(unit)
    ax=axs[1,1];ax.set_title('d  Minimum-cost configurations',loc='left',fontweight='bold');ax.set_xlim(0,1);ax.set_ylim(9.8,-.8);ax.set_xticks([]);ax.grid(False)
    for i,w in enumerate(winners):
        tied_labels=[MLABEL[METHODS.index(tuple(method.split()))] for method in w['tied_methods']]
        lab='\n'.join(', '.join(tied_labels[j:j+2]) for j in range(0,len(tied_labels),2))
        ax.text(.02,i,lab,va='center',fontsize=8,linespacing=1.1);ax.text(.98,i,f"{w['cost']:.3f}",va='center',ha='right',fontsize=8)
    ax.set_xlabel('Minimum cost (billion USD yr⁻¹)')
    save(fig,out,'fig3_carbon_portfolios','Lowest observed accepted total cost among four trajectories per scenario. Panel d lists every configuration tied within 1e-8 billion USD (USD 10) of the minimum. Panels a-c retain Sol single as the representative, also used by the atlas. Battery energy is displayed separately from generation and battery power; these are new additions, not total installed capacities. No claim of globally optimal design or deployment.')
    fig,axs=plt.subplots(1,3,figsize=(13,6.5),layout='constrained')
    heat(axs[0],matrix('logical_requests'),'a  System-evaluation requests','{:.0f}')
    heat(axs[1],matrix('api_calls'),'b  Received API responses','{:.0f}',cmap='PuBuGn')
    heat(axs[2],[[r['output_tokens']/1000 for r in rows[i*4:i*4+4]] for i in range(10)],'c  Output tokens (thousands)','{:.1f}',cmap='YlOrBr')
    save(fig,out,'fig4_carbon_search','System-evaluation requests are committed simulations plus hypothetical verifications, including exact-result reuse. Received responses and tokens use saved API ledgers. Output tokens include reasoning where reported. Requests are not physical optimizer calls; wall-clock runtime is not compared because pauses, recovery and runtime caching differ.')
    fig,axs=plt.subplots(2,5,figsize=(13,6),layout='constrained')
    for i,ax in enumerate(axs.flat):
        rs=rows[i*4:i*4+4];minimum=min(r['cost'] for r in rs)
        for k,r in enumerate(rs):
            lo=100*(r['lower']/minimum-1);hi=100*(r['cost']/minimum-1)
            ax.plot([lo,hi],[k,k],c=COLORS[k],lw=2);ax.plot(hi,k,'o',c=COLORS[k],ms=5)
        ax.axvline(0,color='#8A9398',ls=':',lw=.7);ax.set_title(LABELS[i],fontsize=9);ax.set_yticks(range(4),MLABEL if i%5==0 else []);ax.set_ylim(3.5,-.5);ax.grid(axis='x',alpha=.15);ax.set_xlabel('Cost vs case minimum (%)')
    save(fig,out,'figS2_carbon_cost_intervals','Points are achieved total costs relative to the lowest observed accepted cost in the case; segments extend from candidate-specific solver lower bound plus fixed distribution cost to achieved cost. These are numerical bounds conditional on each selected design, not confidence intervals or bounds over agent search.')
    fig,axs=plt.subplots(1,2,figsize=(11,6.3),layout='constrained');y=np.arange(10)
    for k,(method,label,color) in enumerate(zip(METHODS,MLABEL,COLORS)):
        rs=[r for r in rows if (r['model'],r['agent'])==method];yp=y+(k-1.5)*.14
        for ax,vals in [(axs[0],[100*r['baseline_emissions']/r['cap'] for r in rs]),(axs[1],[100*(r['cost']/r['baseline_cost']-1) for r in rs])]:
            if k>=2:
                ax.scatter(vals[1:],yp[1:],c=color,s=20,label=label,zorder=3)
                ax.scatter(vals[0],yp[0],facecolors='white',edgecolors=color,s=30,marker='^',zorder=4)
            else:ax.scatter(vals,yp,c=color,s=20,label=label,zorder=3)
    for ax in axs:ax.set_yticks(y,LABELS);ax.invert_yaxis();ax.grid(axis='x',alpha=.15)
    axs[0].axvline(100,color='#A23B3B',ls='--',lw=1);axs[0].set_xlabel('Loose-cap emissions / new budget (%)');axs[0].set_title('a  Historical baseline tested against new budget',loc='left',fontweight='bold')
    axs[1].axvline(0,color='#888888',ls=':',lw=.8);axs[1].set_xlabel('Change in selected total cost (%)');axs[1].set_title('b  Carbon-constrained vs loose-cap outcome',loc='left',fontweight='bold');axs[1].legend(frameon=False,fontsize=8,loc='lower right')
    fig.text(.5,-.025,'Open triangles identify the two Opus 2023 pairs with different output-token ceilings.',ha='center',fontsize=8)
    save(fig,out,'figS1_carbon_vs_loose_cap','40 matched model/architecture/system/year comparisons against historical 1,000 Mt annual-cap runs with matching original input hashes. Selected baseline emissions are assessed retrospectively against the new scenario budgets. Cost changes are differences between searched outcomes, not proven carbon-policy prices. Opus NYISO2023 changes output-token limit from16000 to64000; runtime reuse/protocol differences also require disclosure. No repeated stochastic runs or causal architecture claims.')

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--project',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);args=ap.parse_args();configure(args.project,args.out);args.out.mkdir(parents=True,exist_ok=True)
    rows,winners,stats=extract(args.project,args.out);framework(args.out);figures(rows,winners,args.out)
    dump(args.out/'build_summary.json',stats)
    print(json.dumps(stats,indent=2))

if __name__=='__main__':main()
