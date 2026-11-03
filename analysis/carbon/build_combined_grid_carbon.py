#!/usr/bin/env python3
"""Paired carbon-constrained investment and direct-emission maps from saved dispatch.

Only reads completed evidence; no model, solver or power-flow calls.
python build_combined_grid_carbon.py --project /path/to/grid_agent --out /path/to/output
"""
from pathlib import Path
from archive_paths import resolve, analysis_root, configure, drawing_modules
import argparse
import hashlib
import json
import math
import os
import sys
import tempfile

os.environ.setdefault('MPLCONFIGDIR', str(Path(tempfile.gettempdir()) / 'carbon-combined-mpl'))
sys.dont_write_bytecode = True
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.colors import LogNorm
from matplotlib.cm import ScalarMappable
from matplotlib.lines import Line2D
import numpy as np
import xarray as xr

NAMES = {'nyiso': 'NYISO', 'miso': 'MISO', 'south_africa': 'South Africa',
         'brazil': 'Brazil', 'mexico': 'Mexico'}
SOURCES = {'nyiso': 'RGGI', 'miso': 'MISO Future 2', 'south_africa': 'IRP 2025',
           'mexico': 'PROSENER', 'brazil': 'PDE 2035 SIN'}
MAIN = [('nyiso', 2035), ('miso', 2035), ('south_africa', 2035),
        ('brazil', 2035), ('mexico', 2030)]
TECHS = ['wind', 'solar', 'nuclear', 'battery']
NORM = LogNorm(.01, 200, clip=True)
CMAP = plt.get_cmap('YlOrRd')
PINS = {}


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def pin(p, expected=None):
    h = sha(p)
    if expected:
        assert h == expected, str(p)
    PINS[str(p)] = h
    return h


def read(p):
    pin(p)
    return json.loads(Path(p).read_text())


def dump(p, d):
    p.write_text(json.dumps(d, indent=2, ensure_ascii=False, allow_nan=False) + '\n')


def extract(project):
    data = read(analysis_root(project) / 'geographic_carbon_evidence.json')
    summary = read(analysis_root(project) / 'carbon_data.json')
    winners = {(w['region'],w['year']):w for w in summary['winners']}
    # The previously audited map evidence stores the same selected accepted candidates.
    assert len(data['cases']) == 10
    for c in data['cases']:
        source = resolve(c['source'])
        winner = winners[c['region'],c['year']]
        assert winner['output'] == str(source)
        assert (winner['model'],winner['agent']) == (c['model_key'],c['agent'])
        assert winner['cost'] == c['annual_all_in_cost_billion']
        best = read(source / 'best_accepted.json')
        assert best['acceptance']['passes'] and best['acceptance']['co2_budget']['passes']
        pin(source/'best_accepted.json', c['best_accepted_sha256'])
        assert c['model_key'] == 'sol' and c['agent'] == 'single'
        assert math.isclose(best['all_in_cost_billion'], c['annual_all_in_cost_billion'], abs_tol=1e-10)
        cp = best['dispatch_checkpoint']
        assert cp['available']
        path = source / cp['path']
        pin(path, cp['sha256'])
        meta_record = read(source/cp['metadata_path'])
        assert meta_record['sha256'] == cp['sha256']
        zone = {n['id']: 0. for n in c['nodes']}
        generators = []
        with xr.open_dataset(path) as ds:
            assert ds.sizes['snapshots'] == 8760
            meta = json.loads(ds.attrs['meta'])
            assert meta['design'] == best['design']
            dates = ds['snapshots_snapshot'].values
            assert np.all(np.diff(dates).astype('timedelta64[h]').astype(int) == 1)
            dispatch = ds['generators_t_p'].sum('snapshots')
            annual = dict(zip(map(str, dispatch['generators_t_p_i'].values), map(float, dispatch.values)))
            buses = dict(zip(map(str, ds['generators_i'].values), map(str, ds['generators_bus'].values)))
            for gid, asset in meta['planning_assets'].items():
                ef = float(asset.get('emissions_t_per_MWh', 0))
                if ef == 0:
                    continue
                assert gid in buses
                bus = buses[gid].split('::')[-1]
                assert bus == asset['bus'].split('::')[-1] and bus in zone
                output = annual.get(gid, 0.)
                if gid not in annual:
                    # PyPSA omits columns identical to the static zero default.
                    assert ('generators_p' not in ds or float(ds['generators_p'].sel(generators_i=gid)) == 0.)
                assert output >= -1e-5
                co2 = output * ef / 1e6
                zone[bus] += co2
                generators.append({'generator': gid, 'bus': bus, 'annual_MWh': output,
                                   'emissions_t_per_MWh': ef, 'co2_Mt': co2,
                                   'static_zero_dispatch': gid not in annual})
        aggregate = sum(zone.values())
        error = abs(aggregate - best['results']['co2_emitted_Mt'])
        assert error <= 1e-8, (c['case_id'], error)
        assert abs(aggregate-c['co2_emitted_Mt']) <= 1e-8
        for n in c['nodes']:
            n['direct_co2_Mt'] = zone[n['id']]
        c['dispatch_checkpoint'] = {'path': str(path), 'sha256': cp['sha256'],
                                     'candidate_sha256': cp['candidate_sha256']}
        c['dispatch_hours'] = 8760
        c['generator_emissions'] = generators
        c['zonal_co2_total_Mt'] = aggregate
        c['reconciliation_absolute_error_Mt'] = error
        c['budget_source_tag'] = SOURCES[c['region']]
        c['cap_utilization_percent'] = 100*aggregate/c['co2_cap_Mt']
        c['main_figure'] = (c['region'], c['year']) in MAIN
    data['emission_calculation'] = ('For each saved generator, sum its 8,760 hourly dispatch MW '
        '(one hour each), multiply by planning_assets.emissions_t_per_MWh, divide by 1e6, '
        'and aggregate by generators_bus. This coefficient is per MWh of electrical output; '
        'there is no efficiency divisor. Missing dynamic columns use the zero static default.')
    data['emission_map_semantics'] = {'quantity': 'Annual direct generator CO2 per model zone (Mt)',
        'glyph': 'Fixed-size circle at the prepared model-zone representative location',
        'colour': 'One common logarithmic scale, 0.01 to 200 Mt per year; positive values below '
                  '0.01 use the minimum colour, zero uses a hollow point.',
        'exclusions': 'Not a spatial emissions inventory; no life-cycle or imported-power emissions.',
        'budget': 'System-wide research budget, never allocated to individual model zones.'}
    data['main_selection'] = 'Latest available carbon-constrained year for each of five systems; Mexico 2030, others 2035.'
    return data


def draw_emissions(ax, c, geo, pub, detail=False):
    trans = geo.basemap(ax, c['region'], scale_bar=False)
    xy = geo.coords(c, trans)
    for n in c['nodes']:
        v = n['direct_co2_Mt']
        ax.scatter(*xy[n['id']], s=27 if detail else 14,
                   c=[CMAP(NORM(max(v, .01)))] if v > 1e-10 else ['white'],
                   edgecolors=pub.INK, linewidths=.3, zorder=9)
    if detail:
        labels(ax, c, xy, pub, is_emissions=True)
    return xy


def labels(ax, c, xy, pub, is_emissions=False):
    ax.figure.canvas.draw()
    points = [ax.transData.transform(xy[n['id']]) for n in c['nodes']]
    ranked = sorted(range(len(points)), key=lambda i: points[i][0])
    split = (len(ranked)+1)//2
    for side, indices in [('left', ranked[:split]), ('right', ranked[split:])]:
        indices = sorted(indices, key=lambda i: points[i][1], reverse=True)
        ys = np.linspace(.90, .10, len(indices)) if len(indices)>1 else [.5]
        for i, y in zip(indices, ys):
            n = c['nodes'][i]
            power = sum(n['new_technologies_MW'].get(t,0)/1000 for t in TECHS)
            shrink = 4 if is_emissions else 1.02*math.sqrt(9+power)+2
            ax.annotate(str(i+1), xy[n['id']], xytext=(-.07 if side=='left' else 1.07,y),
                        textcoords='axes fraction', annotation_clip=False, ha='center', va='center',
                        fontsize=6.6, weight='bold', zorder=12,
                        bbox=dict(boxstyle='circle,pad=.17',fc='white',ec=pub.INK,lw=.45),
                        arrowprops=dict(arrowstyle='-',color=pub.INK,alpha=.5,lw=.4,shrinkA=4,shrinkB=shrink))


def technology_legend(fig, pub, y, fontsize=6.1):
    handles = [Line2D([],[],marker='o',ls='',mfc=pub.COLORS[t],mec='white',ms=4.5,label=t.capitalize()) for t in TECHS]
    handles += [Line2D([],[],color=pub.INK,lw=.7,label='Existing link'),
                Line2D([],[],color=pub.TRANSFER,lw=1.5,label='Added transfer')]
    fig.legend(handles=handles, loc='upper left', bbox_to_anchor=(.025,y), ncol=6,
               frameon=False, fontsize=fontsize, handlelength=1.2, columnspacing=1.35, borderaxespad=0)


def carbon_legend(fig, rect, pub, fontsize=6):
    ax = fig.add_axes(rect)
    cb = fig.colorbar(ScalarMappable(norm=NORM,cmap=CMAP), cax=ax, orientation='horizontal',
                      ticks=[.01,.1,1,10,100,200])
    cb.ax.set_xticklabels(['0.01','0.1','1','10','100','200'])
    cb.ax.tick_params(labelsize=fontsize, width=.4, length=2, pad=1)
    cb.ax.minorticks_off()
    cb.outline.set_linewidth(.4)


def save(fig, out, name):
    for ext in ['pdf','svg']:
        fig.savefig(out/f'{name}.{ext}')
    fig.savefig(out/f'{name}.png', dpi=350)


def main_figure(data, out, geo, pub):
    fig = plt.figure(figsize=(170/25.4,155/25.4))
    fig.text(.025,.98,'Carbon-constrained portfolios and their annual emissions',fontsize=8.8,weight='bold',va='top')
    fig.text(.025,.944,'Latest available year per system',fontsize=6.3)
    fig.text(.452,.944,'Optimized additions',fontsize=6.9,weight='bold',ha='center')
    fig.text(.818,.944,'Zonal direct CO$_2$',fontsize=6.9,weight='bold',ha='center')
    lookup = {(c['region'],c['year']):c for c in data['cases']}
    for i, key in enumerate(MAIN):
        c=lookup[key]
        top=.91-i*.149
        bottom=top-.137
        tie_mark='*' if len(c['tied_minimum_job_ids'])>1 else ''
        fig.text(.025,top,f'{chr(97+i)}  {NAMES[c["region"]]} {c["year"]}{tie_mark}',fontsize=7.3,weight='bold',va='top')
        fig.text(.025,top-.032,f'Annual cost  US$ {c["annual_all_in_cost_billion"]:.2f} bn',fontsize=6.3)
        fig.text(.025,top-.056,f'CO$_2$  {c["co2_emitted_Mt"]:.2f} Mt',fontsize=6.3)
        fig.text(.025,top-.080,f'Budget  {c["co2_cap_Mt"]:.2f} Mt ({c["cap_utilization_percent"]:.1f}% used)',fontsize=6.3)
        fig.text(.025,top-.104,f'Research basis  {SOURCES[c["region"]]}',fontsize=6.0)
        ax=fig.add_axes([.315,bottom,.275,.129])
        trans=geo.basemap(ax,c['region'],scale_bar=False)
        xy=geo.coords(c,trans)
        pub.model_routes(ax,c,xy)
        for n in c['nodes']:
            pub.pie(ax,xy[n['id']],n)
        ax=fig.add_axes([.68,bottom,.275,.129])
        draw_emissions(ax,c,geo,pub)
        if i<4:
            fig.add_artist(Line2D([.025,.97],[bottom-.007,bottom-.007],transform=fig.transFigure,color='#D9DEDF',lw=.45))
    technology_legend(fig,pub,.158,6.0)
    fig.text(.025,.111,'CO$_2$ per model zone',fontsize=6.2,weight='bold')
    fig.text(.025,.090,'Mt per year, logarithmic colour scale',fontsize=5.9)
    carbon_legend(fig,[.37,.103,.58,.015],pub,5.8)
    fig.text(.025,.073,'* Four trajectories tied at the minimum recorded cost; Sol single-agent is shown.',fontsize=5.8)
    fig.text(.025,.057,'Hollow emission points = zero; positive values below 0.01 use the lightest colour.',fontsize=5.8)
    fig.text(.025,.030,'Model-zone locations and schematic links. Budgets apply to each whole system.',fontsize=5.9)
    save(fig,out,'combined_grid_carbon')
    plt.close(fig)


def atlas_page(c,geo,pub):
    fig=plt.figure(figsize=(180/25.4,195/25.4))
    fig.text(.025,.975,f'{NAMES[c["region"]]} {c["year"]}',fontsize=10.5,weight='bold',va='top')
    fig.text(.975,.975,'GPT-6.1 Sol single-agent',fontsize=7.5,ha='right',va='top')
    fig.text(.025,.943,f'Annual cost  US$ {c["annual_all_in_cost_billion"]:.3f} billion',fontsize=7.3)
    fig.text(.975,.943,f'CO$_2$  {c["co2_emitted_Mt"]:.3f} / {c["co2_cap_Mt"]:.3f} Mt budget',fontsize=7.3,ha='right')
    fig.text(.025,.919,f'Research budget basis  {SOURCES[c["region"]]}',fontsize=6.9)
    fig.text(.975,.919,f'{c["cap_utilization_percent"]:.2f}% of the system budget used',fontsize=6.9,ha='right')
    fig.text(.25,.887,'a  Optimized additions',fontsize=7.8,weight='bold',ha='center')
    fig.text(.75,.887,'b  Zonal direct CO$_2$',fontsize=7.8,weight='bold',ha='center')
    ax=fig.add_axes([.075,.565,.35,.30])
    trans=geo.basemap(ax,c['region'],scale_bar=False)
    xy=geo.coords(c,trans)
    pub.model_routes(ax,c,xy,True)
    for n in c['nodes']:pub.pie(ax,xy[n['id']],n,True)
    labels(ax,c,xy,pub)
    ax=fig.add_axes([.575,.565,.35,.30])
    draw_emissions(ax,c,geo,pub,True)
    technology_legend(fig,pub,.548,6.3)
    fig.text(.025,.503,'CO$_2$ per zone (Mt yr$^{-1}$)',fontsize=6.3)
    carbon_legend(fig,[.37,.501,.58,.012],pub,6.)
    xcols=[.027,.067,.305,.408,.511,.614,.717,.828,.975]
    heads=['#','Zone','Existing','Wind','Solar','Nuclear','Battery','Energy','Direct CO$_2$']
    for x,h in zip(xcols,heads):
        fig.text(x,.458,h,fontsize=6.8,weight='bold',ha='left' if x<.1 else 'right')
    for x,unit in zip(xcols[2:],['GW','GW','GW','GW','GW','GWh','Mt yr$^{-1}$']):
        fig.text(x,.442,unit,fontsize=6.5,ha='right')
    fig.add_artist(Line2D([.025,.978],[.432,.432],transform=fig.transFigure,color=pub.INK,lw=.6))
    for i,n in enumerate(c['nodes']):
        y=.412-i*.021
        fig.text(xcols[0],y,str(i+1),fontsize=6.8,weight='bold')
        name=('LRZ '+pub.short(c['region'],n['id'])) if c['region']=='miso' else n['id']
        fig.text(xcols[1],y,name,fontsize=6.7)
        vals=[n['existing_generation_MW']/1000]+[n['new_technologies_MW'].get(t,0)/1000 for t in TECHS]+[n['new_battery_MWh']/1000,n['direct_co2_Mt']]
        for j,(x,v) in enumerate(zip(xcols[2:],vals)):
            text=f'{v:.3f}' if j==6 else pub.format_value(v)
            fig.text(x,y,text,fontsize=6.7,ha='right')
    bottom=.412-len(c['nodes'])*.021
    fig.add_artist(Line2D([.025,.978],[bottom+.008,bottom+.008],transform=fig.transFigure,color=pub.INK,lw=.6))
    fig.text(.067,bottom-.011,'Total',fontsize=6.9,weight='bold')
    vals=[sum(n['existing_generation_MW'] for n in c['nodes'])/1000]+[c['new_capacities_GW'][t] for t in TECHS]+[c['new_capacities_GW']['battery_energy_GWh'],c['co2_emitted_Mt']]
    for j,(x,v) in enumerate(zip(xcols[2:],vals)):
        fig.text(x,bottom-.011,f'{v:.3f}' if j==6 else pub.format_value(v),fontsize=6.8,weight='bold',ha='right')
    tie=len(c['tied_minimum_job_ids'])
    fig.text(.025,.119,f'Added transfer  {pub.transfer_total(c):.2f} GW',fontsize=7.)
    selection=f'Representative of {tie} trajectories tied at the lowest recorded cost.' if tie>1 else 'Lowest recorded cost among four accepted trajectories.'
    fig.text(.025,.094,selection,fontsize=6.7)
    fig.text(.025,.069,'Existing is inherited generation; additions include battery power and energy.',fontsize=6.5)
    fig.text(.025,.047,'Emission points represent 8,760-hour dispatch totals at model-zone locations.',fontsize=6.5)
    fig.text(.025,.025,'One common logarithmic colour scale; hollow points indicate zero emissions. Research budgets are system-wide.',fontsize=6.2)
    return fig


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project',required=True,type=Path)
    p.add_argument('--out',required=True,type=Path)
    args=p.parse_args()
    configure(args.project, args.out)
    args.out.mkdir(parents=True,exist_ok=True)
    geo, pub = drawing_modules(args.project)
    plt.rcParams.update({'font.family':'DejaVu Sans','pdf.fonttype':42,'svg.fonttype':'none'})
    for path in [Path(__file__),Path(geo.__file__),Path(pub.__file__)]:
        pin(path)
    for path in geo.EV.glob('ne_*.zip'):pin(path)
    data=extract(args.project)
    dump(args.out/'combined_grid_carbon_data.json',data)
    main_figure(data,args.out,geo,pub)
    with PdfPages(args.out/'combined_grid_carbon_atlas.pdf',metadata={'Title':'Accepted investments and zonal direct CO2 under research carbon budgets'}) as pdf:
        for c in data['cases']:
            fig=atlas_page(c,geo,pub)
            pdf.savefig(fig)
            plt.close(fig)
    outputs=['combined_grid_carbon.pdf','combined_grid_carbon.svg','combined_grid_carbon.png','combined_grid_carbon_atlas.pdf','combined_grid_carbon_data.json']
    dump(args.out/'combined_grid_carbon_audit.json',{
        'cases':10,'main_cases':[f'{r}/{y}' for r,y in MAIN],
        'main_width_mm':170,'main_height_mm':155,
        'source_sha256':PINS,'output_sha256':{x:sha(args.out/x) for x in outputs},
        'maximum_aggregate_co2_error_Mt':max(c['reconciliation_absolute_error_Mt'] for c in data['cases']),
        'checks':['All selected best_accepted candidates pass physical and carbon acceptance',
                  'Every checkpoint hash matches the accepted metadata',
                  'Every checkpoint design exactly equals selected best_accepted design',
                  'All ten geographic selections exactly match the independently extracted minimum-cost winners',
                  'Exactly 8,760 consecutive one-hour snapshots per case',
                  'Generator-to-bus mapping agrees with planning asset metadata',
                  'All zonal emission totals reconcile to reported accepted emissions within 1e-8 Mt'],
        'emission_map_semantics':data['emission_map_semantics'],
        'new_optimizer_calls':0,'new_api_calls':0,'new_power_flow_calls':0})
    print(json.dumps({'out':str(args.out),'cases':10,'max_co2_error_Mt':max(c['reconciliation_absolute_error_Mt'] for c in data['cases'])}))


if __name__=='__main__':main()
