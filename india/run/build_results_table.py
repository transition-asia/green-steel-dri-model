"""Build results/Results_ALL.csv, the results table behind the report, from solver output.

Reads the campaign files written by main.py and rerun_route_power_h2.py (results/Results_Scenario_Campaign.csv,
results/Results_RoutePowerH2.csv) and any results/solve_one_*.csv, adds the derived columns the downstream
scripts use (cost buckets, per-tonne physical quantities, emissions on the CCTS and CBAM boundaries when
results/boundary_result.csv exists), and merges them into results/Results_ALL.csv: a fresh solve replaces the
archived row of the same case, new cases are appended. Run from india/run/ after a solve; boundary_calc.py reads this
table, so after a fresh solve run this script, then the boundary scripts, then this script again.
"""
import glob
import os
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, '..', 'results')
TABLE = os.path.join(RESULTS, 'Results_ALL.csv')
TPA = 8_160_000.0                                  # tonnes of crude steel a year (9.6 Mtpa at 85%)

ROUTE_NAME = {'R1b': 'SF', 'R1c': 'SF-BF', 'R2': 'FB', 'R3': 'RK'}
H2_PCT = {'F1': 0, 'F2': 30, 'F3': 70, 'F4': 100}
STAGES = {'R1b': ['DRI-SF', 'EAF-Hot'],
          'R1c': ['DRI-SFBF', 'ESF-Hot', 'BOF-Hot'],
          'R2':  ['DRI-FB', 'ESF-Hot', 'BOF-Hot'],
          'R3':  ['DRI-RK', 'ESF-Hot', 'BOF-Hot']}
SOLVER_FILES = ['Results_Scenario_Campaign.csv', 'Results_RoutePowerH2.csv']


def _num(row, col, default=np.nan):
    try:
        return float(row.get(col, default))
    except (TypeError, ValueError):
        return np.nan


def load_solver_output():
    """Fresh solver rows from the campaign files and any single-case solves."""
    files = [os.path.join(RESULTS, f) for f in SOLVER_FILES] + sorted(glob.glob(os.path.join(RESULTS, 'solve_one_*.csv')))
    parts = [pd.read_csv(f) for f in files if os.path.exists(f)]
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True).drop_duplicates('Run_ID', keep='last')


def derive(df):
    """Derived columns for each solver row, plus every raw solver column passed through."""
    bpath = os.path.join(RESULTS, 'boundary_result.csv')
    bd = pd.read_csv(bpath).set_index(['Route', 'Fuel']) if os.path.exists(bpath) else None
    rows = []
    for _, r in df.iterrows():
        rt, f = r['Route'], r['Fuel']
        case = r['Archetype']
        st = STAGES.get(rt, [])
        feed = _num(r, 'OPEX_%s_per_tcs' % st[0]) if st else np.nan
        steel_opex = _num(r, 'Total Steel OPEX')
        plant = np.nansum([_num(r, 'CAPEX_%s_per_tcs' % s) for s in st]) if st else np.nan
        total_capex = _num(r, 'Total CAPEX')
        elec = np.nansum([_num(r, 'Elec_%s_kWh_per_tcs' % s) for s in st] + [_num(r, 'Elec_electrolyser_kWh_per_tcs', 0.0)])
        b = bd.loc[(rt, f)] if (bd is not None and (rt, f) in bd.index and case == 'BASE') else None
        rows.append(dict(
            Case=case, Route=rt, RouteName=ROUTE_NAME.get(rt, rt), Fuel=f, H2_pct=H2_PCT.get(f),
            PowerMode=r['PowerMode'], H2Mode=r['H2Mode'],
            LCOS_usd_tcs=_num(r, 'LCOS'),
            CO2_cradle=round(_num(r, 'CO2 intensity'), 4),
            CO2_ccts=round(float(b.E_ccts), 4) if b is not None else np.nan,
            CO2_cbam=round(float(b.E_cbam), 4) if b is not None else np.nan,
            Scope1=round(float(b.S1), 4) if b is not None else np.nan,
            Scope3=round(float(b.S3), 4) if b is not None else np.nan,
            # cost-stack buckets; they sum to LCOS with the water-and-other residual closing the gap
            OPEX_feed=feed,
            OPEX_downstream=(steel_opex - feed) if feed == feed else np.nan,
            Power_usd_tcs=_num(r, 'PPA Energy Cost') + _num(r, 'OA Charges'),
            CAPEX_plant=plant,
            CAPEX_energy=(total_capex - plant) if plant == plant else np.nan,
            OM_usd_tcs=_num(r, 'OM Cost'),
            CO2_cost_usd_tcs=_num(r, 'CO2 Cost'),
            H2_subsidy_credit_usd_tcs=_num(r, 'H2 Subsidy Credit'),
            CAPEX_total=round(total_capex, 2), OPEX_total=round(_num(r, 'Total OPEX'), 2),
            Elec_kWh_tcs=round(elec, 1),
            Elec_electrolyser_kWh_tcs=round(_num(r, 'Elec_electrolyser_kWh_per_tcs', 0.0), 1),
            H2_kg_tcs=round(_num(r, 'H2_Total_annual_kg') / TPA, 2),
            GridImport_kWh_tcs=round(_num(r, 'Grid_Import_annual_kWh') / TPA, 2),
            Xover_cradle=round(float(b.xover_report), 2) if b is not None else np.nan,
            Xover_ccts=round(float(b.xover_ccts), 2) if b is not None else np.nan,
            Xover_cbam=round(float(b.xover_cbam), 2) if b is not None else np.nan,
            Run_ID=r['Run_ID']))
    t = pd.DataFrame(rows)
    passthrough = [c for c in df.columns if c not in set(t.columns)]      # every raw solver column
    raw = df[['Run_ID'] + passthrough]
    return t.merge(raw, on='Run_ID', how='left')


def build():
    fresh = load_solver_output()
    table = pd.read_csv(TABLE) if os.path.exists(TABLE) else pd.DataFrame()
    if len(fresh):
        new = derive(fresh)
        if len(table):
            table = pd.concat([table[~table.Run_ID.isin(new.Run_ID)], new], ignore_index=True)
        else:
            table = new
    elif not len(table):
        raise FileNotFoundError('no solver output in results/ and no existing Results_ALL.csv')
    order = {'BASE': 0, 'NOSUB': 1}
    return table.sort_values(['Case', 'Route', 'Fuel', 'PowerMode'],
                             key=lambda c: c.map(order) if c.name == 'Case' else c).reset_index(drop=True), len(fresh)


if __name__ == '__main__':
    t, n = build()
    if n == 0:
        print('no fresh solver output in results/; %s left as it is (%d rows)' % (TABLE, len(t)))
    else:
        t.to_csv(TABLE, index=False)
        print('%d fresh solver rows merged; %d rows -> %s' % (n, len(t), TABLE))
