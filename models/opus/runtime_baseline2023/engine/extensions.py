"""Explicit fleet availability, energy budgets, policy and storage constraints."""
import numpy as np
import pandas as pd

def physical_constraints(n, scenario, design):
    m = n.model
    assets = n.meta['planning_assets']
    emissions = []
    for name, a in assets.items():
        if name not in n.generators.index:
            continue
        dispatch = m.variables['Generator-p'].sel(name=name)
        if 'annual_capacity_factor' in a:
            # A constant annual energy budget scaled to the modelled duration.
            # Short-window validation is not a claim about annual chronology.
            m.add_constraints(dispatch.sum() <= a['capacity_MW'] * a['annual_capacity_factor'] * len(n.snapshots), name='energy_budget_'+a['id'])
        rate = a.get('emissions_t_per_MWh', 0.)
        if rate:
            emissions.append(dispatch.sum() * rate)
    cap = scenario['assumptions']['co2_limit_Mt']
    if emissions:
        m.add_constraints(sum(emissions) <= cap * 1e6 * len(n.snapshots) / 8760, name='operational_co2_budget')
    from policy import add_constraints
    add_constraints(n, scenario)
    # Dispatch envelope is exposed for the agent to respond to local feeder failures.
    for name, row in n.storage_units.iterrows():
        if name in assets:  # Existing unit operating constraints are fixed inputs.
            continue
        bus = row.bus
        charge = design.get(f'storage_{bus}_max_charge_fraction', 1.)
        discharge = design.get(f'storage_{bus}_max_discharge_fraction', 1.)
        nominal = m.variables['StorageUnit-p_nom'].sel(name=name)
        m.add_constraints(m.variables['StorageUnit-p_store'].sel(name=name) <= charge * nominal, name='charge_envelope_'+bus)
        m.add_constraints(m.variables['StorageUnit-p_dispatch'].sel(name=name) <= discharge * nominal, name='discharge_envelope_'+bus)

def report_physics(n, scenario, context):
    arrays = n.generators_t.p
    assets = n.meta['planning_assets']
    emissions = sum(float(arrays[name].sum()) * a.get('emissions_t_per_MWh', 0) for name, a in assets.items() if name in arrays)
    budgets=[]
    for name,a in assets.items():
        if name in arrays and 'annual_capacity_factor' in a:
            actual=float(arrays[name].sum()); limit=a['capacity_MW']*a['annual_capacity_factor']*len(n.snapshots)
            budgets.append(dict(asset=name,actual_MWh=actual,limit_MWh=limit,passes=actual<=limit+1e-3))
    state_errors={}
    for name,row in n.storage_units.iterrows():
        soc=n.storage_units_t.state_of_charge[name].to_numpy()
        charge=n.storage_units_t.p_store[name].to_numpy(); dis=n.storage_units_t.p_dispatch[name].to_numpy()
        previous=np.roll(soc,1)
        residual=soc-previous*(1-row.standing_loss)-charge*row.efficiency_store+dis/row.efficiency_dispatch
        state_errors[name]=float(np.max(np.abs(residual)))
    # Node balance includes both ends of every (bidirectional) inter-area link.
    balances={}
    for bus in n.buses.index:
        val=np.zeros(len(n.snapshots))
        for table,times,field in [(n.generators,n.generators_t,'p'),(n.storage_units,n.storage_units_t,'p'),(n.stores,n.stores_t,'p')]:
            names=table.index[table.bus==bus] if len(table) else []
            if len(names):val+=getattr(times,field)[names].sum(axis=1).to_numpy()
        names=n.loads.index[n.loads.bus==bus]
        if len(names):val-=n.loads_t.p[names].sum(axis=1).to_numpy()
        for field,side in [('p0','bus0'),('p1','bus1')]:
            names=n.links.index[n.links[side]==bus]
            if len(names):val-=getattr(n.links_t,field)[names].sum(axis=1).to_numpy()
        balances[bus]=float(np.max(np.abs(val)))
    from policy import report
    return dict(hours_modelled=len(n.snapshots),annual_equivalent_objective=len(n.snapshots)!=8760,
                policy_check=report(n,scenario),
                annualization_weight=scenario.get('objective_weight',1),co2_emitted_Mt=emissions/1e6,
                co2_scope='operational direct emissions from dispatched existing thermal assets; lifecycle/import emissions excluded',
                energy_budget_checks=budgets,cyclic_storage=True,
                max_storage_balance_residual_MWh=max(state_errors.values(),default=0),
                max_nodal_balance_residual_MW=max(balances.values(),default=0),
                model_assumptions=scenario['assumptions'])
