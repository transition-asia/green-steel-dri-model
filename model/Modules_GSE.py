"""Green Steel Economics (GSE) — per-stage TEA data assembly.

This module is the bridge between the Excel input workbook and the
optimization model. It walks the process chain of a route (e.g.
DRI-SF -> EAF-Hot, or DRI-FB -> ESF-Hot -> BOF-Hot) and returns a tidy
DataFrame that the optimization model consumes as
`opt_params['breakdown_loads']`.

Unit conventions (important — easy to get wrong):

    Excel input sheets (Params_{stage}, Params_H2, Params_EAF/BOF/ESF):
        Values are expressed per tonne of THAT STAGE'S own input or
        output material, e.g. kWh per tonne of DRI for DRI-SF and
        kWh per tonne of hot metal for ESF-Hot.

    `run_GSE_step` output:
        Same units as the Excel sheets — per tonne of the stage's own
        material. No cascading is applied here.

    `process_together` (in Module_optimization_new):
        Cascades the per-stage values into per-tcs-of-finished-steel by
        multiplying each row by the cumulative material requirement
        Input[stage]. Also applies `config.CAPEX_OVERFACTOR`.

    `opt_params['breakdown_loads']` (what the optimizer sees):
        All cost / SEC / emission columns are per tcs of finished steel;
        the 'Input' column is in tonnes per tcs.

Public API
----------
    load_input         : read Excel workbook and build a Dat instance
    run_GSE_step       : assemble per-stage TEA row for one process step
    GSE_full           : run all process steps in a chain
    scope_emissions    : per-scope emission breakdown for one Dat
    emissions_all_scope: convenience wrapper concatenating all 3 scopes
    operational_cost   : per-stage variable OPEX in $/tonne-of-stage-input
    Dat                : container class for one stage's TEA inputs
"""

import pandas as pd
import warnings

import config
import Modules_utility as util
from Modules_process import DRI, EAF

warnings.simplefilter(action='ignore', category=pd.errors.PerformanceWarning)
warnings.simplefilter(action='ignore', category=FutureWarning)


# ===========================================================================
# Per-stage driver: assemble one row of opt_params['breakdown_loads']
# ===========================================================================

def run_GSE_step(steel_tech: str,
                 ore_setting: dict,
                 target_year: int,
                 input_file: str,
                 region: str,
                 grid_price=None, grid_EF=None,
                 exclude_electricity: bool = True,
                 # DRI knobs
                 H2_onsite=None, H2_ratio: float = 1, Briquetting=None, H2_price=None,
                 # EAF knob
                 scrap_ratio=None):
    """Assemble a one-row DataFrame of TEA parameters for one process step.

    Returns
    -------
    (row_df, dat) : (pd.DataFrame, Dat)
        `row_df` is a single-row DataFrame indexed by `steel_tech` with columns
        `steel CAPEX annualized`, `steel_OM`, `steel_electricity`,
        `steel_OPEX`, `steel_emission`. Values are per tonne of THIS stage's
        own input material (cascaded to per-tcs downstream).

        `dat` is the underlying Dat instance, useful for additional lookups
        in the caller.

    Notes
    -----
    `exclude_electricity=True` (default) strips electricity OPEX and Scope 2
    grid emissions from the per-stage figures because both are accounted
    for endogenously by the optimization model via the hourly electricity
    balance. Set False only when running standalone TEA calculations.
    """
    dat = load_input(
        steel_tech=steel_tech, ore_setting=ore_setting,
        target_year=target_year, input_file=input_file, region=region,
        grid_price=grid_price, grid_EF=grid_EF,
        Briquetting=Briquetting, H2_onsite=H2_onsite,
        H2_ratio=H2_ratio, scrap_ratio=scrap_ratio, H2_price=H2_price,
    )

    tech_key  = config.steel_investment[steel_tech]
    steel_capex     = dat.tech.loc[tech_key, 'CAPEX_ref']
    steel_OM        = steel_capex * dat.tech.loc[tech_key, 'om_to_capex']
    steel_OPEX      = operational_cost(dat)['OPEX ($/tcs)']
    steel_emissions = emissions_all_scope(dat)
    lifetime        = dat.tech.loc[tech_key, 'economic_life_yr']

    # Optionally strip out electricity OPEX and Scope 2 emissions (handled
    # endogenously by the optimization model via the electricity balance).
    if exclude_electricity:
        # The electricity line item is named 'Grid Electricity', not
        # 'Electricity', so the old `!= 'Electricity'` filter matched nothing and
        # left stage electricity in base_OPEX (priced at the grid retail tariff)
        # while the hourly balance ALSO bought it via PPA -> double-count.
        # Match any electricity-named line so it is excluded from base_OPEX and
        # costed once, endogenously, through the electricity balance.
        _is_elec = steel_OPEX.index.to_series().astype(str).str.contains('Electricity', case=False, na=False)
        base_OPEX = steel_OPEX[~_is_elec.values].sum()
        emission_sum = steel_emissions.loc[steel_emissions['Scope'] != 2, 'tCO2e/Unit'].sum()
    else:
        base_OPEX = steel_OPEX.sum()
        emission_sum = steel_emissions['tCO2e/Unit'].sum()

    row = {
        'steel CAPEX annualized': steel_capex * util.CRF(dat.WACC, lifetime),
        'steel_OM':                steel_OM,
        'steel_electricity':       dat.steel.loc['Grid Electricity', 'Value'],
        'steel_OPEX':              base_OPEX,
        'steel_emission':          emission_sum,
    }
    return pd.DataFrame([row], index=[dat.steel_tech]), dat


def GSE_full(steel_techs: list,
             ore_settings: dict,
             target_year: int,
             input_file: str,
             region: str,
             grid_price=None, grid_EF=None,
             exclude_electricity: bool = True,
             # DRI knobs
             H2_onsite=None, H2_ratio: float = 1, Briquetting=None, H2_price=None,
             # EAF knob
             scrap_ratio=None) -> pd.DataFrame:
    """Run `run_GSE_step` for each stage in `steel_techs` and stack the rows.

    The returned DataFrame includes two extra columns:
      - 'Step'      : 0-based stage order (used by `process_together` for
                      the cumulative cascade)
      - 'Input'     : raw stage-input requirement, t per t-of-stage-output
                      (for stages other than the first); pulled from
                      dat.steel.loc['Ore','Value']
      - 'H2 Demand' : kg-H2 per t-stage-output for DRI stages
    """
    rows = []
    for step, steel_tech in enumerate(steel_techs):
        row_df, dat = run_GSE_step(
            steel_tech=steel_tech,
            ore_setting=ore_settings[steel_tech],
            target_year=target_year,
            input_file=input_file, region=region,
            grid_price=grid_price, grid_EF=grid_EF,
            exclude_electricity=exclude_electricity,
            Briquetting=Briquetting, H2_onsite=H2_onsite,
            H2_ratio=H2_ratio, H2_price=H2_price,
            scrap_ratio=scrap_ratio,
        )
        row_df['Step'] = step
        # First stage takes raw ore — no upstream material requirement.
        if step != 0:
            row_df['Input'] = dat.steel.loc['Ore', 'Value']
        if steel_tech.startswith('DRI'):
            row_df['H2 Demand'] = dat.steel.loc['H2', 'Value']
        rows.append(row_df)
    return pd.concat(rows)


# ===========================================================================
# Excel reader: build a Dat instance for one process step
# ===========================================================================

def load_input(steel_tech: str,
               ore_setting: dict,
               target_year: int,
               input_file: str,
               region: str,
               WACC=None,
               shipping_cost=0.0,
               grid_price=None, grid_EF=None,
               H2_onsite=None, H2_ratio: float = 1,
               Briquetting=None, H2_price=None,
               scrap_ratio=None):
    """Read the Excel workbook and build the per-stage Dat container.

    Parameters
    ----------
    steel_tech   : process label, e.g. 'DRI-SF', 'ESF-Hot', 'EAF-Hot'
    ore_setting  : dict with at least {'ore', 'ore_type', 'fe_grade'} for the
                   first stage; downstream stages typically pass
                   {'ore', 'price': 0, 'emission': 0} to disable Excel ore
                   lookup (the upstream stage's output becomes this stage's
                   ore, and is already costed/emitted upstream).
    target_year  : year to forecast prices to (CAGR applied via Commodities sheet)
    input_file   : path to the Model_input_*.xlsx workbook
    region       : region key matching the Renewable_Resource and Params_Finance sheets
    WACC         : optional override; otherwise pulled from Params_Finance for `region`
    shipping_cost: $/tcs added to OPEX; default 0
    grid_price   : optional override for $/kWh; otherwise from Grid sheet
    grid_EF      : optional override for tCO2/kWh; otherwise from Grid sheet
    H2_onsite, H2_ratio, Briquetting, H2_price : DRI-only parameters (see Modules_process.DRI)
    scrap_ratio  : EAF-only mass fraction of scrap in feed (0–1)
    """
    print(f"Steelmaking technology: {steel_tech}")

    # -------------------------------------------------------------------
    # Technology table and commodity price forecast
    # -------------------------------------------------------------------
    params_tech = pd.read_excel(input_file, sheet_name='Tech', skiprows=1, index_col='Item')

    # Resolve CAPEX_ref for the target year. The Tech sheet now has CAPEX_2025
    # / CAPEX_2030 / CAPEX_2035 columns instead of a single `CAPEX_ref`. Pick
    # the nearest available year column (linear interp would be slightly better
    # but a step function is defensible and matches the data's vintage).
    if 'CAPEX_ref' not in params_tech.columns:
        # Find columns of the form 'CAPEX_<year>'
        capex_year_cols = {}
        for c in params_tech.columns:
            if isinstance(c, str) and c.startswith('CAPEX_'):
                try:
                    yr = int(c.split('_', 1)[1])
                    capex_year_cols[yr] = c
                except ValueError:
                    pass
        if capex_year_cols:
            # Pick nearest year (preferring the year ≤ target_year if ties)
            target_yr = int(target_year)
            nearest = min(capex_year_cols.keys(),
                          key=lambda y: (abs(y - target_yr), y - target_yr))
            params_tech['CAPEX_ref'] = params_tech[capex_year_cols[nearest]]
            # Print once for diagnostics
            # print(f'CAPEX_ref resolved from {capex_year_cols[nearest]} (target={target_yr})')
    commodity   = pd.read_excel(input_file, sheet_name='Commodities', skiprows=1, index_col='Item')
    commodity_forecast = (
        commodity['price_ref']
        * (1 + commodity['Annual_real_growth']) ** (target_year - commodity['price_ref_year'])
    ).to_frame(name='price')

    # Ore price: either supplied directly in ore_setting (for intermediate
    # stages where the upstream output is the input) or looked up by grade.
    if 'price' in ore_setting:
        commodity_forecast.loc['Ore', 'price'] = ore_setting['price']
    else:
        ore_table = (pd.read_excel(input_file, sheet_name='Ore_Price', skiprows=1)
                       .set_index(['Item', 'Type', 'Grade', 'price_ref_year'])
                       .drop(columns=['Unit']))
        _row = ore_table.loc[
            ore_setting['ore'], ore_setting['ore_type'],
            ore_setting['fe_grade'], target_year,
        ]
        # `_row` may be a scalar (single matching row) or a Series (multiple). Handle both.
        _price = _row['price_ref']
        if hasattr(_price, 'iloc'):
            _price = _price.iloc[0]
        # Apply the ore-grade premium (e.g. DR-grade uplift over the BF-grade domestic
        # base for R1b). Previously the 'premium' field set in get_ore_settings was never
        # consumed, so the route-sheet ore_grade_premium was silently dropped; this adds it.
        _price = float(_price) + float(ore_setting.get('premium', 0.0) or 0.0)
        commodity_forecast.loc['Ore', 'price'] = _price

    if grid_price is None:
        grid_price = pd.read_excel(
            input_file, sheet_name='Grid', skiprows=1, index_col='Year'
        ).loc[target_year, 'Price ($/kWh)']
    commodity_forecast.loc['Grid Electricity', 'price'] = grid_price

    # -------------------------------------------------------------------
    # Emission factors
    # -------------------------------------------------------------------
    Emission_Factor = pd.read_excel(
        input_file, sheet_name='Emission_Factor', skiprows=1, index_col='Item'
    )['Value']
    if 'emission' in ore_setting:
        Emission_Factor.loc['Ore'] = ore_setting['emission']
    else:
        Emission_Factor.loc['Ore'] = Emission_Factor.loc[ore_setting['ore']]

    if grid_EF is None:
        grid_EF = pd.read_excel(
            input_file, sheet_name='Grid', skiprows=1, index_col='Year'
        ).loc[target_year, 'Emission Intensity (tCO2/kWh)']
    Emission_Factor.loc['Grid Electricity'] = grid_EF

    # -------------------------------------------------------------------
    # Per-stage process parameters
    # -------------------------------------------------------------------
    # Hot/cold variants share an Excel sheet — strip the suffix for those.
    if steel_tech.startswith('EAF'):
        params_steel = pd.read_excel(input_file, sheet_name='Params_EAF')
    elif steel_tech.startswith('BOF'):
        params_steel = pd.read_excel(input_file, sheet_name='Params_BOF')
    elif steel_tech.startswith('ESF'):
        params_steel = pd.read_excel(input_file, sheet_name='Params_ESF')
    else:
        params_steel = pd.read_excel(input_file, sheet_name=f'Params_{steel_tech}')
    params_steel = params_steel.set_index('Item')

    # Hydrogen sheet only relevant for DRI; non-DRI stages pick their 'Value'
    # column directly from the ore-type column with a Global fallback.
    if steel_tech.startswith('DRI'):
        params_h2 = pd.read_excel(input_file, sheet_name='Params_H2').set_index('Item')
    else:
        params_h2 = None
        if 'ore_type' in ore_setting and ore_setting['ore_type'] in params_steel.columns:
            params_steel['Value'] = params_steel[ore_setting['ore_type']].fillna(params_steel['Global'])
        else:
            params_steel['Value'] = params_steel['Global']

    # Stage-specific post-processing.
    if steel_tech.startswith('DRI'):
        params_steel, commodity = DRI(
            steel_tech=steel_tech,
            params_steel=params_steel,
            params_h2=params_h2,
            commodity=commodity,
            H2_ratio=H2_ratio,
            H2_onsite=H2_onsite,
            Briquetting=Briquetting,
            H2_price=H2_price,
        )
    elif steel_tech.startswith('EAF'):
        if scrap_ratio is None:
            raise ValueError(
                "scrap_ratio is required for EAF technologies. Pass the mass "
                "fraction of scrap in EAF feed (0-1)."
            )
        params_steel.loc['Scrap ratio', 'Value'] = scrap_ratio
        params_steel = EAF(steel_tech, params_steel)
    elif steel_tech == 'BOF-Cold':
        params_steel.loc['NG', 'Value'] = (
            params_steel.loc['NG', 'Value'] + params_steel.loc['NG_cold_charge_penalty', 'Value']
        )
    elif steel_tech == 'ESF-Hot':
        params_steel.loc['Grid Electricity', 'Value'] = (
            params_steel.loc['E_smelting', 'Value'] + params_steel.loc['E_base', 'Value']
        )
    elif steel_tech == 'ESF-Cold':
        params_steel.loc['Grid Electricity', 'Value'] = (
            params_steel.loc['E_smelting', 'Value']
            + params_steel.loc['E_base', 'Value']
            + params_steel.loc['E_hot_charge', 'Value']
        )

    params_steel = params_steel[['Type', 'Value', 'Emission Scope']]

    # -------------------------------------------------------------------
    # WACC and location
    # -------------------------------------------------------------------
    if WACC is None:
        WACC = pd.read_excel(input_file, sheet_name='Params_Finance').set_index(
            'Region'
        ).loc[region, 'WACC']

    location = pd.read_excel(
        input_file, sheet_name='Renewable_Resource'
    ).set_index('Region').loc[region]

    return Dat(
        region=region,
        steel_tech=steel_tech,
        steel=params_steel,
        h2=params_h2,
        tech=params_tech,
        EF=Emission_Factor,
        commodity=commodity_forecast,
        shipping_cost=shipping_cost,
        WACC=WACC,
        location=location,
    )


class Dat:
    """Container for one stage's TEA inputs assembled from the Excel workbook.

    All attributes are populated by `load_input`. Downstream code reads
    from this object; nothing mutates it after construction.
    """

    def __init__(self, region: str, steel_tech: str, steel, h2, tech, EF,
                 commodity, shipping_cost, WACC, location):
        self.region        = region
        self.steel_tech    = steel_tech
        self.steel         = steel          # Params_{stage} with computed 'Value' column
        self.h2            = h2             # Params_H2 (None for non-DRI stages)
        self.tech          = tech           # Tech sheet: CAPEX_ref, om_to_capex, min_cf, ...
        self.EF            = EF             # Emission factors (tCO2/unit, indexed by Item)
        self.commodity     = commodity      # Year-forecasted commodity prices
        self.shipping_cost = shipping_cost  # $/tcs surcharge added to OPEX
        self.WACC          = WACC
        self.location      = location       # Renewable_Resource row (lat/lon for CF lookup)


# ===========================================================================
# Emission accounting
# ===========================================================================

def scope_emissions(dat: Dat, scope: int) -> pd.DataFrame:
    """Emissions for one scope, indexed by emitting item.

    Returns a DataFrame with columns ['tCO2e/Unit', 'Scope']. Returns a
    single zero-row when no items in `dat.steel` carry the requested scope.
    """
    items = dat.steel[dat.steel['Emission Scope'] == scope].reset_index()
    items = items.set_index('Item')[['Value', 'Type']]
    if len(items) == 0:
        out = pd.DataFrame({'tCO2e/Unit': [0.0]})
        out.index.name = 'Item'
    else:
        out = items['Value']*dat.EF.loc[items.index]
        out=out.to_frame(name='tCO2e/Unit')
    out['Scope'] = scope
    return out

def emissions_all_scope(dat: Dat) -> pd.DataFrame:
    scope1_emissions=scope_emissions(dat,1)
    scope2_emissions=scope_emissions(dat,2)
    scope3_emissions=scope_emissions(dat,3)
    emissions= pd.concat([scope1_emissions, scope2_emissions, scope3_emissions], axis=0)
    return emissions



def operational_cost(dat: Dat) -> pd.DataFrame:
    """Variable OPEX for one stage, indexed by input item.

    Returns a DataFrame with column 'OPEX ($/tcs)' covering material, fuel,
    electricity, labor consumption (the items whose 'Type' is one of
    'Materials', 'Fuel', 'Electricity', 'Labor' in the Params sheet),
    plus a 'Shipping' row from dat.shipping_cost.

    Note: values are per tonne of THIS stage's own input/output material —
    cascading to per-tcs-of-finished-steel happens in `process_together`.
    """
    is_opex = dat.steel['Type'].isin(['Materials', 'Fuel', 'Electricity', 'Labor'])
    opex_items = dat.steel[is_opex]
    if len(opex_items) > 0:
        cost = (dat.commodity['price'] * opex_items['Value']).loc[opex_items.index]
        cost_df = cost.to_frame(name='OPEX ($/tcs)')
    else:
        cost_df = pd.DataFrame(columns=['OPEX ($/tcs)'],index=[0])
    shipping_df = pd.DataFrame({'OPEX ($/tcs)': [dat.shipping_cost]}, index=['Shipping'])
    total_cost=pd.concat([cost_df,shipping_df])
    return total_cost

# ===========================================================================
# Scenario sheet loaders
# ===========================================================================
# These read the scenario-matrix sheets the user adds to Model_input_*.xlsx
# and return tidy DataFrames keyed by their natural primary key. Used by the
# scenario-sweep driver in main.py to build per-run opt_params dicts.

def load_scenario_sheets(input_file: str) -> dict:
    """Read the five scenario-matrix sheets from the Excel workbook.

    Expected sheets and primary keys:
        Archetype  -> indexed by 'Archetype'   (S1, S2, S3, S4)
        Route      -> indexed by ('Route','Fuel') (R1-F4, R2-F1, ...)
        PowerMode  -> indexed by 'Mode'        (M1, M2, M3, M4, M5, M6)
        H2Mode     -> indexed by 'Mode'        (H1, H2, H3)
        RunMatrix  -> indexed by 'Run_ID'      (one row per run)

    Returns
    -------
    dict with keys 'archetype', 'route', 'power_mode', 'h2_mode', 'run_matrix';
    each value is a pandas DataFrame ready for lookup-by-index.

    Notes
    -----
    Missing sheets are reported via KeyError with a clear message; missing
    optional columns are filled with defaults (zero or NaN) at consumption
    time, not here.
    """
    sheets = {}
    sheets['archetype']  = pd.read_excel(input_file, sheet_name='Archetype').set_index('Archetype')
    sheets['route']      = pd.read_excel(input_file, sheet_name='Route').set_index(['Route', 'Fuel'])
    sheets['power_mode'] = pd.read_excel(input_file, sheet_name='PowerMode').set_index('Mode')
    sheets['h2_mode']    = pd.read_excel(input_file, sheet_name='H2Mode').set_index('Mode')
    sheets['run_matrix'] = pd.read_excel(input_file, sheet_name='RunMatrix').set_index('Run_ID')
    return sheets


def build_opt_params_for_run(run_id: str,
                             scenario_sheets: dict,
                             breakdown_loads: pd.DataFrame,
                             *,
                             steel_capacity: float,
                             annual_cf: float,
                             investment_options: list,
                             apply_emission_cap: bool = False,
                             target_emission: float = 0.40,
                             baseline_emission: float = 0.0) -> dict:
    """Compose opt_params for one scenario-matrix row.

    Looks up the archetype, route, power-mode, and H2-mode rows referenced
    by `run_id` in `scenario_sheets['run_matrix']`, and assembles a single
    `opt_params` dict the optimizer can consume.

    Parameters
    ----------
    run_id            : key into scenario_sheets['run_matrix'].
    scenario_sheets   : output of `load_scenario_sheets`.
    breakdown_loads   : already-cascaded per-stage TEA DataFrame for THIS
                        run's route (caller is responsible for assembling
                        one breakdown_loads per route; the same one is
                        reused across power/H2-mode combinations for that
                        route).
    steel_capacity, annual_cf, investment_options :
                        run-level inputs that aren't part of any scenario
                        sheet.
    apply_emission_cap, target_emission, baseline_emission :
                        emission policy knobs (typically held constant across
                        a scenario campaign).

    Returns
    -------
    dict suitable for `SPO.InitialSetting`.
    """
    rm     = scenario_sheets['run_matrix'].loc[run_id]
    arch   = scenario_sheets['archetype'].loc[rm['Archetype']]
    route  = scenario_sheets['route'].loc[(rm['Route'], rm['Fuel'])]
    pmode  = scenario_sheets['power_mode'].loc[rm['PowerMode']]
    h2mode = scenario_sheets['h2_mode'].loc[rm['H2Mode']]

    def _f(d, k, default=0.0):
        """Safe float lookup with NaN -> default fallback."""
        v = d.get(k, default)
        try:
            v = float(v)
        except (TypeError, ValueError):
            return default
        return default if pd.isna(v) else v

    def _s(d, k, default=''):
        """Safe string lookup with NaN/blank -> default fallback."""
        v = d.get(k, default)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return default
        v = str(v).strip()
        return v if v else default

    opt_params = {
        # Required
        'breakdown_loads':           breakdown_loads,
        'Investment options':        investment_options,
        'carbon_price':              _f(arch, 'carbon_price'),
        # Plant scale
        'steel_capacity':            steel_capacity,
        'annual_cf':                 annual_cf,
        # Identifying tags
        'run_id':                    run_id,
        'scenario_tags': {
            'Archetype':  rm['Archetype'],
            'Route':      rm['Route'],
            'Fuel':       rm['Fuel'],
            'PowerMode':  rm['PowerMode'],
            'H2Mode':     rm['H2Mode'],
        },
        # Archetype (Layer 1)
        'cbam_price':                _f(arch, 'cbam_price'),
        # EU CBAM benchmark (tCO2/tcs) — free-allowance equivalent above which
        # the importer pays. Defaults to NaN (=> CBAM term skipped) for non-S3
        # archetypes. For S3/2030 a value of ~1.0 brackets the EU ETS Phase 5
        # benchmark (~1.2) phased in by the CBAM factor (~48.5% in 2030).
        'eu_benchmark':              _f(arch, 'eu_benchmark', default=None),
        # Export share — fraction of annual production exposed to CBAM.
        'export_share':              _f(arch, 'export_share', default=0.0),
        'ng_price':                  _f(arch, 'ng_price', default=None),
        'h2_subsidy_per_kg':         _f(arch, 'h2_subsidy_per_kg'),
        'subsidy_elec_capex':        _f(arch, 'electrolyser_capex_subsidy'),
        'wacc':                      _f(arch, 'wacc_override', default=None),
        # Route (Layer 2) — caller already provided breakdown_loads built for
        # this route; remaining route-level params travel via scrap_ratio /
        # H2_ratio in run_GSE_step (handled by main.py before calling here).
        # Power mode (Layer 3)
        'onsite_share':              _f(pmode, 'onsite_share', default=1.0),
        'wheeling_charge':           _f(pmode, 'wheeling_charge'),
        'css_charge':                _f(pmode, 'css_charge'),
        'electricity_duty_waiver':   _f(pmode, 'electricity_duty_waiver'),
        're_ppa_price':              _f(pmode, 're_ppa_price'),
        're_ppa_capacity_mw':        _f(pmode, 're_ppa_capacity_mw'),
        'allow_onsite_storage':      _f(pmode, 'allow_onsite_storage', default=0.0),
        'banking_capacity_hours':    _f(pmode, 'banking_capacity_hours'),
        'banking_efficiency':        _f(pmode, 'banking_efficiency', default=1.0),
        'banking_fee':               _f(pmode, 'banking_fee'),
        # Shaped (as-generated) PPA + monthly banking settlement.
        # 'firm'/'annual' defaults preserve legacy behaviour for modes that
        # don't carry the new columns.
        'ppa_delivery':              _s(pmode, 'ppa_delivery', default='firm'),
        're_ppa_price_solar':        _f(pmode, 're_ppa_price_solar'),
        're_ppa_price_wind':         _f(pmode, 're_ppa_price_wind'),
        'banking_settlement':        _s(pmode, 'banking_settlement', default='annual'),
        'banking_monthly_cap_share': _f(pmode, 'banking_monthly_cap_share'),
        'banking_buyback_price':     _f(pmode, 'banking_buyback_price'),
        # H2 mode — purchased_delivered_cost ($/kg) reads from the archetype's
        # `h2_price` column (Layer 1 macro story) when present; otherwise falls
        # back to the H2Mode's own `purchased_delivered_cost` for back-compat.
        'purchased_baseline_share':  _f(h2mode, 'purchased_baseline_share'),
        'purchased_delivered_cost':  _f(arch, 'h2_price', default=_f(h2mode, 'purchased_delivered_cost')),
        'ghci_compliance_opex':      _f(h2mode, 'ghci_compliance_opex'),
        # Emission policy (constant across campaign in typical usage)
        'apply_emission_cap':        apply_emission_cap,
        'target_emission':           target_emission,
        'baseline_emission':         baseline_emission,
        # Reporting tag
        'Objective':                 'LCOS',
    }
    return opt_params


def get_route_definition(run_id: str, scenario_sheets: dict) -> dict:
    """Return the route-level inputs needed to build breakdown_loads.

    Pulls Route, Fuel, steel_techs (pipe-separated), H2_ratio, scrap_ratio
    for the given run_id. The caller uses these to drive GSE.GSE_full.
    """
    rm    = scenario_sheets['run_matrix'].loc[run_id]
    route = scenario_sheets['route'].loc[(rm['Route'], rm['Fuel'])]
    return {
        'Route':       rm['Route'],
        'Fuel':        rm['Fuel'],
        'steel_techs': route['steel_techs'].split('|'),
        'H2_ratio':    float(route['H2_ratio']),
        'scrap_ratio': float(route['scrap_ratio']),
    }



def simple_LCOS(capex, variable_opex, lifetime,capacity_factor,emissions, carbon_price=0,baseline_emission=0,fixed_opex=None,finance_params=None,WACC=None):
    if finance_params is not None:
        WACC = util.calculate_wacc(finance_params)['wacc_nominal']
    elif WACC is None:
        raise ValueError("Either finance_params or WACC must be provided")
    CRF=util.CRF(WACC, lifetime)
    if fixed_opex is None:
        fixed_opex=capex*0.02
        print("⚠️ Warning: fixed OPEX not provided, assuming fixed OPEX equals to 2% CAPEX")
    if carbon_price==0:
        print("⚠️ Warning: carbon price not provided, assuming carbon price equals to 0")
    if baseline_emission==0:
        print("⚠️ Warning: baseline emission not provided, assuming baseline emission equals to 0")
    LCOS_nominal = (capex * CRF+fixed_opex)/ capacity_factor + variable_opex + (emissions-baseline_emission) * carbon_price
    print(f"💰 Simple LCOS: ${LCOS_nominal:.2f}/tcs")
    return LCOS_nominal


def lifetime_LCOS(opex,investment_year=None, lifetime=25, capex=None,annualized_capex=None, 
    ETS_Emission=0, capacity_factor=None,carbon_price=0,ETS_benchmark=0,fixed_opex=None,finance_params=None,WACC=None):
    # 1. Financial Setup
    if WACC is None:
        if finance_params is not None:
            WACC = util.calculate_wacc(finance_params)['wacc_nominal']
        else:
            raise ValueError("Either finance_params or WACC must be provided")

    if capacity_factor is None:
        capacity_factor=0.85
        print("⚠️ Warning: using default capacity factor 85%")
    
    if capex is not None:
        fixed_opex = capex * 0.02 if fixed_opex is None else fixed_opex
        annualized_capex = capex * util.CRF(WACC, lifetime)+ fixed_opex
    elif annualized_capex is not None:
        pass
    else:
        raise ValueError("Either capex or annualized_capex must be provided")

    # 3. Time Range Logic
    if investment_year is not None:
        end_year=investment_year+lifetime-1
        if not isinstance(carbon_price, (int,float)):
            carbon_price = carbon_price .loc[investment_year:end_year+1]
        if not isinstance(ETS_benchmark, (int,float)):
            ETS_benchmark = ETS_benchmark .loc[investment_year:end_year+1]
        opex=opex.loc[investment_year:end_year+1]
    ETS_cost=(ETS_Emission-ETS_benchmark)*carbon_price
    LCOS_annual = annualized_capex/(capacity_factor)+(ETS_cost + opex)
    if investment_year is not None:
        LCOS_annual.index.name='Year'
    return LCOS_annual



