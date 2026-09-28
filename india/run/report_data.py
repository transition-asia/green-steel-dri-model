"""Single source of the report numbers, computed from the results table.

Both the figure generators and (for reference) the report text draw from
here, so the figures stay in sync with results/Results_ALL.csv.

Route labels map to descriptive names used in the report:
    R1b -> 'SF'  (shaft furnace, purchased DR-pellet)
    R2  -> 'FB'  (fluidised-bed)
    R3  -> 'RK'  (rotary-kiln)
"""
import os
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
# ---------------------------------------------------------------------------
# This module is a thin accessor over results/Results_ALL.csv.
# Rebuild the table with build_results_table.py after any new solve.
ALL_RESULTS = os.path.join(HERE, '..', 'results', 'Results_ALL.csv')
if not os.path.exists(ALL_RESULTS):
    raise FileNotFoundError(
        '../results/Results_ALL.csv not found. Build it with:  python build_results_table.py')
_df = pd.read_csv(ALL_RESULTS)
CAMPAIGN = ALL_RESULTS

# column aliases used by the figure notebook
_df['LCOS'] = _df['LCOS_usd_tcs']
_df['CO2 intensity'] = _df['CO2_cradle']
_df['_Arch'] = _df['Case']


BF_BOF_LCOS = 536.0
BF_BOF_EMIT = 2.6
ROUTE2NAME = {'R1b': 'SF', 'R2': 'FB', 'R3': 'RK'}
FUELS = ['F1', 'F2', 'F3', 'F4']
CENTRAL_PM = 'M2s'   # group captive on an as-generated hybrid portfolio, banked



def _base_case():
    """Base-case rows (BASE / M2s / H1): no carbon price, with India's hydrogen support
    (SIGHT and the electrolyser capital subsidy) in place."""
    return _df[(_df._Arch == 'BASE') & (_df.PowerMode == CENTRAL_PM) & (_df.H2Mode == 'H1')]


def lcos_by_route_fuel_basecase():
    """Base-case LCOS ($/tcs): no carbon price, hydrogen support in place."""
    s = _base_case()
    out = {}
    for r, name in ROUTE2NAME.items():
        out[name] = {f: float(s[(s.Route == r) & (s.Fuel == f)]['LCOS'].iloc[0]) for f in FUELS}
    return out


def co2_by_route_fuel_basecase():
    """Base-case emission intensity (tCO2/tcs)."""
    s = _base_case()
    out = {}
    for r, name in ROUTE2NAME.items():
        out[name] = {f: float(s[(s.Route == r) & (s.Fuel == f)]['CO2 intensity'].iloc[0]) for f in FUELS}
    return out
