"""Scenario-sweep driver: H2/NG-DRI techno-economic analysis.

Walks the RunMatrix sheet and solves the dispatch+sizing LP for each enabled scenario combination. 

Scenario sheets
---------------------
  Archetype  : Layer-1 (S1-S4) — carbon price, NG price, subsidies, WACC override
  Route      : Layer-2 (R1-R3 x F1-F4) — steel_techs, H2_ratio, scrap_ratio
  PowerMode  : Layer-3 power (M1-M4) — onsite_share, wheeling, CSS, banking, duty waiver
  H2Mode     : Layer-3 H2 (H1-H3) — purchased_baseline_share, delivered_cost, GHCI
  RunMatrix  : the decision-relevant subset (~30 runs); set Enabled=0 to skip a row

Plus Tech/Commodities/Grid/Params_* sheets.

Workflow per run
----------------
  1. Look up route -> assemble breakdown_loads via GSE.GSE_full + SPO.process_together
  2. Build opt_params from the scenario sheets (archetype + route + power mode + H2 mode)
  3. Load the steelmaking-stage Dat (for WACC, Tech sheet, location, H2 params)
  4. Build and solve the LP
  5. Append the result row to the campaign CSV
"""

import os
import sys
import time

import pandas as pd

code_path = '../../model/'
if code_path not in sys.path:
    sys.path.append(code_path)

from lp_backend import Model, BACKEND      # HiGHS modelling layer (model/lp_backend.py)

import Modules_GSE as GSE
import Modules_utility as util
import Modules_optimization as SPO

# ===========================================================================
# Campaign configuration
# ===========================================================================
GSE_input_folder      = '../data/'
input_file            = GSE_input_folder + 'Model_input_India.xlsx'
renewable_data_folder = '../data/renewable/'
output_csv            = '../results/Results_Scenario_Campaign.csv'

region                = 'IN'
steel_capacity        = 9_600_000   # tonnes of crude steel a year: Phase 2 nameplate
annual_cf             = 0.85        # for final steel
investment_options    = ['Electrolyser', 'H2Storage', 'Battery', 'Solar', 'Wind']
# Emission cap is applied per-archetype using the 'emission limit' column of
# the Archetype sheet, unless the RunMatrix row carries an Emission_Cap_Override.
apply_emission_cap    = True
baseline_emission     = 0.0         # CCTS free-allowance benchmark
scenario_file = './Scenario_Sheets.xlsx'

# Per-stage ore settings, indexed by route.
#
# Pricing convention:
#   - INTERMEDIATE products (output of an upstream stage in the chain) get
#     `price=0` and `emission=0`: their cost and emissions are accounted in the
#     upstream stage.
#   - MARKET INPUTS (the ore feeding the first iron-producing stage) read
#     their price and upstream EF from the Ore_Price sheet.
#
# Route-specific settings applied in get_ore_settings():
#   - R1b : DRI-SF reads Pellet-Domestic at market (plus the DR-grade premium)
#   - R1c : DRI-SFBF reads Pellet-BF at market
#   - R2  : DRI-FB reads Fines-Low at market
#   - R3  : DRI-RK reads Fines-Low at market
_BASE_ORE = {
    # Stage : ore identity (no `price` key -> looked up from Ore_Price sheet)
    'DRI-SF':         {'ore': 'Pellet',      'ore_type': 'Hematite', 'price': 0, 'emission': 0},
    # R2/R3 fines feeds: take Fines-Low from Ore_Price
    'DRI-FB':         {'ore': 'Fines-Low',   'ore_type': 'Hematite', 'fe_grade': 58},
    'DRI-RK':         {'ore': 'Fines-Low',   'ore_type': 'Hematite', 'fe_grade': 58},
    # R1c BF-grade pellet feed (market lookup set in get_ore_settings)
    'DRI-SFBF':       {'ore': 'Pellet',      'ore_type': 'Hematite', 'price': 0, 'emission': 0},
    # Downstream stages take HBI as intermediate (no market price)
    'EAF-Hot':        {'ore': 'HBI', 'price': 0, 'emission': 0},
    'BOF-Hot':        {'ore': 'HBI', 'price': 0, 'emission': 0},
    'ESF-Hot':        {'ore': 'HBI', 'price': 0, 'emission': 0},
}


def get_ore_settings(route_code: str, ore_grade_premium: float = 0.0) -> dict:
    """Per-route ore settings.
    Pricing rules:
      - R1b (purchased DR-grade pellet): DRI-SF reads Pellet-Domestic at market price.
      - R1c (BF-grade pellet): DRI-SFBF reads Pellet-BF at market price.
      - R2  (fluidised bed): DRI-FB reads Fines-Low at market price.
      - R3  (rotary kiln): DRI-RK reads Fines-Low at market price.

    The optional `ore_grade_premium` ($/t) adds to the DR-pellet base for R1b.
    """
    s = {k: dict(v) for k, v in _BASE_ORE.items()}  # deep copy
    if route_code == 'R1b':
        # Purchased domestic DR-grade pellet: the Pellet-Domestic (Magnetite, Fe 67) row of Ore_Price.
        # Upstream (Scope 3) emission factor 0.139 tCO2/t: 127 kg/t induration on a straight-grate
        # plant (TERI, Technical Report on Decarbonization of Iron Ore Pellet Manufacturing) plus
        # 12 kg/t mining. Counted cradle-to-gate, matching CBAM's treatment of pellet as a precursor.
        s['DRI-SF'] = {
            'ore':       'Pellet-Domestic',
            'ore_type':  'Magnetite',
            'fe_grade':  67,
            'emission':  0.139,
            'premium':   ore_grade_premium,
        }
    # R1c (shaft furnace on BF-grade pellet -> ESF -> BOF):
    # DRI-SFBF reads the BF-grade domestic pellet series (PELLEX base, Fe 63,
    # no DR premium). Same explicit induration+mining Scope-3 EF as R1b's
    # pellet (0.139 tCO2/t): same process, lower grade.
    if route_code == 'R1c':
        s['DRI-SFBF'] = {
            'ore':       'Pellet-BF',
            'ore_type':  'Magnetite',
            'fe_grade':  63,
            'emission':  0.139,
            'premium':   ore_grade_premium,
        }
        s['ESF-Hot']['ore_type'] = 'SFBF'
    # Route-tag the ESF stage so load_input picks the Params_ESF 'FB'/'RK'
    # columns (DRI input per tonne of hot metal from the Fe balance, and the
    # high-gangue flux) instead of the Global fallback.
    if route_code == 'R2':
        s['ESF-Hot']['ore_type'] = 'FB'
    elif route_code == 'R3':
        s['ESF-Hot']['ore_type'] = 'RK'
    return s


# Alias kept for notebooks that import ORE_SETTINGS
ORE_SETTINGS = _BASE_ORE

# HiGHS solver options for the campaign (HiGHS option names; see model/lp_backend.py).
SOLVER_PARAMS = {
    'output_flag': False,    # silence per-run logs; set True to debug
    'solver':      'choose', # let HiGHS pick simplex or interior point per case; 'ipm' or 'simplex' to force
    'threads':     0,        # 0 = all available cores
    # no time_limit by default: the banked hourly cases take one to two hours in HiGHS;
    # set SOLVER_PARAMS['time_limit'] = seconds to cap a run
}


# ===========================================================================
# Helpers
# ===========================================================================

def append_result(result: pd.DataFrame, csv_path: str) -> None:
    """Append a result row with column alignment.

    Different routes emit different per-stage result columns (R1b has two
    stages, R2/R3 three), so a raw `mode='a'` append misaligns fields
    whenever the new row's schema differs from the file's header row.
    Read + concat + rewrite keeps one consistent union schema; at campaign
    scale (tens of rows) the rewrite cost is negligible.
    """
    if os.path.exists(csv_path):
        existing = pd.read_csv(csv_path)
        combined = pd.concat([existing, result], ignore_index=True)
    else:
        combined = result
    combined.to_csv(csv_path, index=False)


def already_done(csv_path: str) -> set:
    """Return Run_IDs already in the campaign CSV (resume support)."""
    if not os.path.exists(csv_path):
        return set()
    try:
        existing = pd.read_csv(csv_path, usecols=['Run_ID'])
        return set(existing['Run_ID'].dropna().astype(str).tolist())
    except Exception:
        return set()


def run_one_scenario(run_id: str,
                     scenario_sheets: dict,
                     CF_cache: dict) -> pd.DataFrame:
    """Solve one scenario end-to-end. Empty DataFrame on infeasibility/error."""
    rm    = scenario_sheets['run_matrix'].loc[run_id]
    arch  = scenario_sheets['archetype'].loc[rm['Archetype']]
    target_year = int(arch['target_year'])

    route_def   = GSE.get_route_definition(run_id, scenario_sheets)
    steel_techs = route_def['steel_techs']
    H2_ratio    = route_def['H2_ratio']
    scrap_ratio = route_def['scrap_ratio']

    # Route-aware ore settings.
    route_code = rm['Route']
    route_row  = scenario_sheets['route'].loc[(route_code, rm['Fuel'])]
    ore_premium = float(route_row.get('ore_grade_premium', 0) or 0)
    ore_settings = get_ore_settings(route_code, ore_grade_premium=ore_premium)

    # Per-archetype emission cap: read 'emission limit' column from the Archetype sheet.
    archetype_emission_cap = float(arch.get('emission limit', 2.2))
    # Per-run override: the RunMatrix Emission_Cap_Override column supersedes the archetype cap
    # when set (the shaft-furnace self-build row is held at the 0.40 tCO2/tcs near-zero threshold).
    try:
        _cap_override = float(rm.get('Emission_Cap_Override'))
        if not pd.isna(_cap_override):
            archetype_emission_cap = _cap_override
    except (TypeError, ValueError):
        pass

    # Per-archetype CCTS free-allowance baseline (Indian sector benchmark).
    # Defaults to the global `baseline_emission` (0.0) if not provided.
    try:
        _bl = float(arch.get('baseline_emission', baseline_emission))
        archetype_baseline_emission = baseline_emission if pd.isna(_bl) else _bl
    except (TypeError, ValueError):
        archetype_baseline_emission = baseline_emission

    # Step 1: per-stage TEA assembly for this route.
    process_params = GSE.GSE_full(
        steel_techs=steel_techs,
        ore_settings={s: ore_settings[s] for s in steel_techs},
        target_year=target_year,
        input_file=input_file,
        region=region,
        Briquetting=False,
        H2_onsite=True,                # H1/H3 size on-site electrolyser; H2 still passes for cost wiring
        H2_ratio=H2_ratio,
        H2_price=0,
        scrap_ratio=scrap_ratio,
        exclude_electricity=True,
    )
    breakdown_loads = SPO.process_together(process_params)

    # Step 2: build opt_params for this run.
    opt_params = GSE.build_opt_params_for_run(
        run_id=run_id,
        scenario_sheets=scenario_sheets,
        breakdown_loads=breakdown_loads,
        steel_capacity=steel_capacity,
        annual_cf=annual_cf,
        investment_options=investment_options,
        apply_emission_cap=apply_emission_cap,
        target_emission=archetype_emission_cap,   # per-archetype cap
        baseline_emission=archetype_baseline_emission,   # per-archetype CCTS baseline
    )

    # Step 3: load steelmaking-stage Dat (for WACC, Tech sheet, location, H2 params).
    sm_tech = SPO._find_dri_stage(steel_techs)
    dat = GSE.load_input(
        steel_tech=sm_tech,
        ore_setting=ore_settings[sm_tech],
        target_year=target_year,
        region=region,
        input_file=input_file,
        Briquetting=False,
        H2_onsite=True,
        H2_ratio=H2_ratio,
        H2_price=0,
        scrap_ratio=scrap_ratio,
    )

    # CF profiles: cache by (lat, lon) so we don't reload per run.
    site_key = (round(dat.location.Latitude, 4), round(dat.location.Longitude, 4))
    if site_key not in CF_cache:
        CF_cache[site_key] = util.load_renewable_data(
            renewable_data_folder, dat.location.Latitude, dat.location.Longitude,
        )
    CF = CF_cache[site_key]

    # Step 4: build and solve LP.
    setting = SPO.InitialSetting(
        target_year=target_year, country=region, opt_params=opt_params, dat=dat,
    )

    m = Model(f"SPO_{run_id}")
    for k, v in SOLVER_PARAMS.items():
        m.setParam(k, v)
    m, DV = SPO.define_DV(m, setting)
    m, DV = SPO.add_constraints(m, DV, setting, CF)
    m, DV = SPO.set_objective(m, DV, setting, dat)
    m.optimize()

    return SPO.print_results(m, DV, setting, dat)


# ===========================================================================
# Campaign loop
# ===========================================================================

def main():
    print(f"Loading scenario sheets from {scenario_file} ...")
    scenario_sheets = GSE.load_scenario_sheets(scenario_file)
    print(f"  Archetypes: {list(scenario_sheets['archetype'].index)}")
    print(f"  Routes:     {list(scenario_sheets['route'].index)}")
    print(f"  PowerModes: {list(scenario_sheets['power_mode'].index)}")
    print(f"  H2Modes:    {list(scenario_sheets['h2_mode'].index)}")
    print(f"  RunMatrix:  {len(scenario_sheets['run_matrix'])} total rows")

    run_matrix = scenario_sheets['run_matrix']
    # Stage 1 rows are solved here; Stage 2 rows by rerun_route_power_h2.py (longer time limit).
    if 'Stage' in run_matrix.columns:
        enabled = run_matrix[run_matrix['Stage'] == 1].index.tolist()
    else:
        enabled = run_matrix.index.tolist()
    print(f"  Stage 1:    {len(enabled)} runs to solve\n")

    done = already_done(output_csv)
    if done:
        print(f"Found {len(done)} previously-completed runs in {output_csv}; skipping those.")
    pending = [r for r in enabled if r not in done]

    if not pending:
        print("Nothing to do.")
        return

    CF_cache = {}
    successes, failures = 0, 0
    t_start = time.time()

    for i, run_id in enumerate(pending, 1):
        print(f"[{i}/{len(pending)}] {run_id} ...", end=' ', flush=True)
        t0 = time.time()
        try:
            result = run_one_scenario(run_id, scenario_sheets, CF_cache)
            if len(result) == 0:
                print(f"INFEASIBLE / non-optimal ({time.time() - t0:.1f}s)")
                failures += 1
                continue
            append_result(result, output_csv)
            lcos = result['LCOS'].iloc[0]
            print(f"LCOS={lcos:.2f} $/tcs ({time.time() - t0:.1f}s)")
            successes += 1
        except Exception as e:
            print(f"ERROR {type(e).__name__}: {e}")
            failures += 1

    elapsed = time.time() - t_start
    print(f"\nDone: {successes} ok, {failures} failed, {elapsed/60:.1f} min total.")
    print(f"Results -> {output_csv}")


if __name__ == '__main__':
    main()
