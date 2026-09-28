"""Steel Plant Optimization (SPO) — green-steel TEA dispatch model.

a linear program for single year of hourly resolution
to find the cost-minimising dispatch and equipment sizing for an
integrated power + electrolyser + DRI/EAF steel complex.

Objective
---------
Levelised cost of steel (LCOS, $/tcs), defined as

    LCOS = (annualised CAPEX + annual OPEX) / annual_steel_production

Annualised CAPEX is scaled by nameplate `steel_capacity`; OPEX flows with
actual production. `annual_steel_production = steel_capacity * annual_cf`
(see `InitialSetting`).

Decision variables (per hour)
-----------------------------
  - Steel_Produced[t]     (tcs/h) finished steel output
  - Stage_{stage}[t]      (t-input/h) per-stage physical throughput
  - Buffer_{buf}[t]       (t-material) intermediate-product stocks
  - Elec_{stage}[t], Elec_electrolyser[t], Elec_Used[t], ...   (kW)
  - Elec_FromGrid[t], Elec_ToGrid[t], Elec_Curtailed[t]        (kW)
  - Elec_Str_Charge/Discharge/Lev[t]                           (kW/kWh)
  - H2_Used[t], H2_Str_Charge/Discharge/Lev[t]                 (kg-H2/h, kg-H2)
  - H2_Purchased[t]                                            (kg-H2/h, pipeline)
  - Bank_Deposit/Withdraw/Lev[t]                               (kW, kWh)

Decision variables (sizing)
---------------------------
  - Cap_{tech} for each tech in `opt_params['Investment options']`:
    Solar (kW), Wind (kW), Battery (kWh), Electrolyser (kW), H2Storage (kg-H2)

Procurement modes
-----------------
The model handles 6 power-procurement modes (M1–M6) and 3 H2-procurement
modes (H1–H3) via per-scenario parameters in opt_params:

  Power modes — `onsite_share`, `wheeling_charge`, `css_charge`,
                `electricity_duty_waiver`, `banking_capacity_hours`,
                `banking_efficiency`, `banking_fee`.

  H2 modes    — `purchased_baseline_share`, `purchased_delivered_cost`.

Unit conventions
----------------
All Stage_{stage}[t] are in **physical tonnes of that stage's input
material per hour**. The 'Input' column of opt_params['breakdown_loads']
gives the physical tonnes required per 1 tcs of finished steel. At full
nameplate (CF=1) a stage runs at (steel_capacity / nHours) * Input[stage].

Per-tcs cost/SEC/ emission columns are already scaled per tcs of
finished steel (by `Modules_GSE.process_together`). Any
hourly coupling that uses those parameters converts back to
steel-equivalent via Stage_{stage}[t]/Input[stage].

Role identification
-------------------
Two stages get special wiring, resolved by name prefix:
  - DRI stage  — consumes H2
  - Steelmaking stage  — Steel_Produced[t] target

Typical usage
-------------
    setting = SPO.InitialSetting(target_year, region, opt_params, dat)
    m, DV   = SPO.define_DV(Model("Plant"), setting)
    m, DV   = SPO.add_constraints(m, DV, setting, CF)
    m, DV   = SPO.set_objective(m, DV, setting, dat)
    m.optimize()
    results = SPO.print_results(m, DV, setting, dat)
"""

from __future__ import annotations
import logging

import numpy as np
import pandas as pd
import lp_backend as gp          # the HiGHS modelling layer (model/lp_backend.py)
from lp_backend import Model, GRB

import config
import Modules_utility as util

log = logging.getLogger(__name__)

# ===========================================================================
# Process-chain aggregation helpers
# ===========================================================================
def process_together(process_params: pd.DataFrame,
                     capex_overfactor: float = config.CAPEX_OVERFACTOR) -> pd.DataFrame:
    """Cascade per-stage Excel inputs into per-tcs values.

    Excel sheets give values per tonne of each stage's own input material
    (e.g. DRI-stage electricity in kWh per tonne of DRI). To convert
    these to per-tcs-of-finished-steel we multiply by the cumulative material
    requirement going downstream. The final stage's cumulative input is 1.0.

    The 'Input' column in the returned DataFrame holds that cumulative
    multiplier and is exactly what `InitialSetting` reads.

    CAPEX is additionally multiplied by `capex_overfactor` (default
    `config.CAPEX_OVERFACTOR`) to cover owner's costs not in the reference
    figure.
    """
    df = process_params.copy()
    df.index.name = 'Process'

    # Cumulative downstream Input multiplier: walk from final stage upstream.
    reversed_df = df.iloc[::-1].reset_index().set_index('Step')
    reversed_df['raw_input'] = reversed_df['Input'].fillna(1)
    reversed_df['cum_input'] = reversed_df['raw_input'].cumprod()
    reversed_df['step_input'] = reversed_df['cum_input'].shift(1, fill_value=1)
    df_out = reversed_df.sort_index()
    df_out = df_out.drop(columns=['Input', 'raw_input', 'cum_input']).rename(columns={'step_input': 'Input'})

    # Scale all numeric cost / SEC / emission columns by Input.
    cols_to_scale = [c for c in df_out.columns if c not in ('Input', 'Process')]
    df_out[cols_to_scale] = df_out[cols_to_scale].mul(df_out['Input'], axis=0)
    
    # Owner's-cost markup on CAPEX only.
    if 'steel CAPEX annualized' in df_out.columns:
        df_out['steel CAPEX annualized'] *= capex_overfactor
    return df_out.round(4)

def _build_buffers(production_stages: list[str]) -> list[tuple[str, str, str]]:
    """Return [(upstream, downstream, buffer_name)] for each consecutive pair."""
    return [
        (production_stages[i], production_stages[i + 1], f"{production_stages[i]}_out")
        for i in range(len(production_stages) - 1)
    ]

def _find_dri_stage(production_stages: list[str]) -> str | None:
    """First entry in `production_stages` whose name starts with 'DRI', else None."""
    for stage in production_stages:
        if stage.startswith('DRI'):
            return stage
    return None

def _find_steelmaking_stage(production_stages: list[str]) -> str | None:
    """First entry whose name starts with 'EAF' or 'BOF', else None.

    Deliberately excludes 'ESF' so that routes like DRI-FB -> ESF-Hot ->
    BOF-Hot correctly identify BOF-Hot (the final converter) as the
    steelmaking stage. Pure-ESF routes are not supported by this resolver;
    pass an explicit override via `opt_params['steelmaking_stage']` if you
    need one.
    """
    for stage in production_stages:
        if stage.startswith('EAF') or stage.startswith('BOF'):
            return stage
    return None



# ===========================================================================
# Setting (parameter container)
# ===========================================================================

class InitialSetting:
    """Container for all scalar / dict parameters consumed by the LP.

    Construction reads from `opt_params` (a dict the caller assembles, see
    module docstring for the canonical keys) and from `dat` (the
    `Modules_GSE.Dat` instance with Excel-sheet contents). Anything that
    isn't strictly needed by the LP itself is also stored here so the
    reporting functions can read it back without re-parsing the workbook.
    """

    def __init__(self, target_year:int, country:str, opt_params:dict, dat, region: str | None = None):
        # ------------------------------------------------------------------
        # Process chain
        # ------------------------------------------------------------------
        self.production_stages=opt_params['breakdown_loads']['Process'].to_list()
        self.dri_stage           = _find_dri_stage(self.production_stages)
        self.steelmaking_stage   = _find_steelmaking_stage(self.production_stages)
        self.intermediate_buffers = _build_buffers(self.production_stages)
        # Allow caller to override the steelmaking stage (ESF-only routes,
        # custom processes, etc.).
        if 'steelmaking_stage' in opt_params:
            self.steelmaking_stage = opt_params['steelmaking_stage']

        # ------------------------------------------------------------------
        # Plant scale and time
        # ------------------------------------------------------------------
        self.target_year = target_year
        self.country=country
        self.region          = region if region is not None else country
        self.nHours          = int(opt_params.get("nHours", 8760))
        self.steel_capacity  = float(opt_params.get('steel_capacity', 1_000))  # tpa nameplate
        self.annual_cf       = float(opt_params.get('annual_cf', 0.85))        # fraction
        self.steel_annual_production = self.steel_capacity * self.annual_cf

        # ------------------------------------------------------------------
        # Investment options and capacity bounds
        # ------------------------------------------------------------------
        self.investment_list = list(opt_params['Investment options'])
        self.generation_list = [v for v in self.investment_list if v in config.onsite_generation_list]

        self.MaxCap={}
        self.MinCap={}
        self.MinCF={}
        self.MaxCF={}
        self.Efficiency={}
        self.RampRate={}
        
        for var in self.investment_list:
            self.MaxCap[var] =dat.tech.loc[var].get('capacity_max',np.nan)
            self.MinCap[var] =dat.tech.loc[var].get('capacity_min',0)
            self.MinCF[var] =dat.tech.loc[var].get('min_cf',0)
            self.MaxCF[var] =dat.tech.loc[var].get('max_cf',1)
            self.Efficiency[var]= dat.tech.loc[var].get('efficiency',1)
            self.RampRate[var]=dat.tech.loc[var].get('ramp_rate_per_hr',1)

        # ------------------------------------------------------------------
        # Per-stage parameters from opt_params['breakdown_loads']
        # ------------------------------------------------------------------
        # The DataFrame is expected to have one row per stage, with columns:
        #     'steel_electricity'        kWh/tcs   (process electricity)
        #     'steel CAPEX annualized'   $/tcs/yr  (already CRF-annualised)
        #     'steel_OM'                 $/tcs     (fixed OM, scaled with nameplate)
        #     'steel_emission'           tCO2/tcs  (Scope 1 only)
        #     'steel_OPEX'               $/tcs     (variable OPEX, excl. electricity, fuel, water, carbon)
        #     'Input'                    t/tcs     (physical input per tcs)
        # Columns can be missing; defaults below kick in.
        agg = opt_params['breakdown_loads'].reset_index(drop=True).set_index('Process')
        self.electricity_sec       = {s: 0.0 for s in self.production_stages}
        self.steel_CAPEX_breakdown = {s: 0.0 for s in self.production_stages}
        self.steel_OM_breakdown    = {s: 0.0 for s in self.production_stages}
        self.emission_breakdown    = {s: 0.0 for s in self.production_stages}
        self.base_OPEX_breakdown   = {s: 0.0 for s in self.production_stages}
        self.Input                 = {s: 1.0 for s in self.production_stages}

        if 'steel_electricity' in agg.columns:
            self.electricity_sec.update(agg['steel_electricity'].dropna().to_dict())
        if 'steel CAPEX annualized' in agg.columns:
            self.steel_CAPEX_breakdown.update(agg['steel CAPEX annualized'].dropna().to_dict())
        if 'steel_OM' in agg.columns:
            self.steel_OM_breakdown.update(agg['steel_OM'].dropna().to_dict())
        if 'steel_emission' in agg.columns:
            self.emission_breakdown.update(agg['steel_emission'].dropna().to_dict())
        if 'steel_OPEX' in agg.columns:
            self.base_OPEX_breakdown.update(agg['steel_OPEX'].dropna().to_dict())
        if 'Input' in agg.columns:
            self.Input.update({k: float(v) for k, v in agg['Input'].dropna().to_dict().items()
                               if k in self.Input})

        # Lumped totals (per tcs) for the objective.
        self.base_electricity   = sum(self.electricity_sec.values())       # kWh/tcs
        self.steel_CAPEX_annual = sum(self.steel_CAPEX_breakdown.values()) # $/tcs/yr
        self.steel_OM           = sum(self.steel_OM_breakdown.values())    # $/tcs
        self.base_emission      = sum(self.emission_breakdown.values())    # tCO2/tcs
        self.base_OPEX          = sum(self.base_OPEX_breakdown.values())   # $/tcs


        # ------------------------------------------------------------------
        # Per-stage dispatch flexibility (process_flex=True) parameters
        # ------------------------------------------------------------------
        self.process_flex = bool(opt_params.get('process_flex', True))

        self.StageMinCF   = {}
        self.StageMaxCF   = {}
        self.StageRampRate = {}

        for stage in self.production_stages:
            tech_key = config.steel_investment[stage]
            self.StageMinCF[stage]    = float(dat.tech.loc[tech_key].get('min_cf', 0))
            self.StageMaxCF[stage]    = float(dat.tech.loc[tech_key].get('max_cf', 1))
            self.StageRampRate[stage] = float(dat.tech.loc[tech_key].get('ramp_rate_per_hr', 1))

        # Buffer capacity (tonnes of intermediate material). Defaults to
        # effectively unbounded; pass `opt_params['buffer_capacity']` to
        # constrain specific buffers.
        buffer_caps = opt_params.get('buffer_capacity', {})
        DEFAULT_BUFFER_CAP = 1e9  # effectively unbounded for typical plant sizes
        self.BufferCap = {
            buf: float(buffer_caps.get(buf, DEFAULT_BUFFER_CAP))
            for _, _, buf in self.intermediate_buffers
        }
        
        # ------------------------------------------------------------------
        # Feasibility sanity check
        # ------------------------------------------------------------------
        # Annual CF must be achievable given the steelmaking stage's MaxCF;
        # otherwise the AnnualProductionTarget + Stage_max constraints will
        # conflict and the LP will report infeasibility.
        if self.steelmaking_stage is not None:
            sm_max = self.StageMaxCF[self.steelmaking_stage]
            if self.annual_cf > sm_max + 1e-9:
                raise ValueError(
                    f"annual_cf={self.annual_cf:.3f} exceeds the steelmaking stage "
                    f"'{self.steelmaking_stage}' MaxCF={sm_max:.3f}. "
                    f"The LP would be infeasible. Reduce annual_cf or raise MaxCF."
                )
        

        # ------------------------------------------------------------------
        # Commodity prices and emission factors
        # ------------------------------------------------------------------
        self.WaterCost = dat.commodity.loc['Water','price']  # $/kg H2O 
        self.Grid_Cost =dat.commodity.loc['Grid Electricity','price']   # $/kWh
        self.Grid_EF= dat.EF.loc['Grid Electricity'] # tCO2/kWh
        self.WaterUse = dat.h2.loc['H2_Water','Value']  # kg H2O/kg H2
        self.WACC = dat.WACC

        # ------------------------------------------------------------------
        # H2 demand and electrolyser parameters
        # ------------------------------------------------------------------
        self.E_H2_production = dat.h2.loc['E_H2_kWh','Value']  # H2 Energy Content (kWh/kg)
        self.H2_Demand_per_DRI = dat.steel.loc['H2','Value']  # kgH2/t-DRI
        self.E_H2_storage_compression = dat.h2.loc['E_H2_comp_Storage','Value']  

        # ------------------------------------------------------------------
        # Storage parameters (Battery only; H2Storage handled separately)
        # ------------------------------------------------------------------
        self.storage_decay={} #self decay of storage (battery and h2 storage)\
        self.storage_duration={}
        for var in ['Battery']:
            self.storage_decay[var]=dat.tech.loc[var].get('Storage_Self_Decay',0)
            # Tech sheet column is 'storage_duration_hr'; the old key never existed,
            # so duration silently defaulted to 4 h for every storage technology.
            _dur = dat.tech.loc[var].get('storage_duration_hr',
                                         dat.tech.loc[var].get('Duration (hr)', 4))
            try:
                _dur = int(_dur)
            except (TypeError, ValueError):
                _dur = 4
            self.storage_duration[var] = _dur if _dur > 0 else 4

        # ------------------------------------------------------------------
        # Carbon pricing
        # ------------------------------------------------------------------
        # Global carbon price applies to grid (Scope 2) emissions.
        # Per-stage carbon prices apply to Scope 1 emissions and let the
        # caller model differentiated policies (CBAM coverage, sector-
        # specific CCTS, etc.). Defaults to the global price for all stages.
        self.carbon_price = float(opt_params['carbon_price'])

        # cp_breakdown = opt_params.get('carbon_price_breakdown', None)
        # if cp_breakdown is not None:
        #     self.carbon_price_breakdown = {
        #         s: float(cp_breakdown.get(s, self.carbon_price))
        #         for s in self.production_stages
        #     }
        # else:
        #     self.carbon_price_breakdown = {s: self.carbon_price for s in self.production_stages}

        # ------------------------------------------------------------------
        # Emission policy: optional hard cap on intensity
        # ------------------------------------------------------------------
        # When apply_emission_cap=True, the LP enforces
        #     CO2_Annual <= target_emission * annual_steel_production.
        # When False (default), emissions are only charged via carbon prices.
        # `baseline_emission` is the free-allowance benchmark subtracted in
        # the carbon-cost term (e.g. CCTS sectoral benchmark).
        self.apply_emission_cap = bool(opt_params.get('apply_emission_cap', False))
        self.target_emission    = float(opt_params.get('target_emission', np.inf))
        self.baseline_emission  = float(opt_params.get('baseline_emission', 0.0))

        # ------------------------------------------------------------------
        # Grid and electricity-selling configuration
        # ------------------------------------------------------------------
        self.grid_connection      = bool(opt_params.get('grid_connection', True))
        self.Elec_FromGrid_Max    = float(opt_params.get('Elec_FromGrid_Max', 1e10))
        self.ElectricitySelling   = bool(opt_params.get('ElectricitySelling', False))
        # The two parameters below only apply when ElectricitySelling=True.
        self.Grid_Selling_Price   = float(opt_params.get('Electricity Selling Price', 0.0))
        self.Grid_Selling_Max              = float(opt_params.get('maxsell', 1.0))
     
        
        # ------------------------------------------------------------------
        # Subsidies (placeholder for sensitivity runs)
        # ------------------------------------------------------------------
        self.subsidy = {
            'renew_capex':      float(opt_params.get('subsidy_renew_capex', 0.0)),
            'elec_capex':       float(opt_params.get('subsidy_elec_capex', 0.0)),
            'grid_electricity': float(opt_params.get('subsidy_grid_electricity', 0.0)),
        }
        

        # ------------------------------------------------------------------
        # Open Access power-procurement charges and PPA structure
        # ------------------------------------------------------------------
        # Two distinct procurement modes, governed by onsite_share:
        #
        #   ON-SITE (onsite_share = 1.0, M1):
        #     - LP sizes Cap_Solar / Cap_Wind / Cap_Battery as decision vars
        #     - Plant pays CAPEX + OM on those capacities
        #     - No wheeling, no CSS, no PPA
        #
        #   OFF-SITE (onsite_share = 0.0, M2-M5):
        #     - On-site renewables and battery are disabled (MaxCap forced to 0)
        #     - Plant buys energy from off-site PPA at a flat $/kWh tariff
        #     - PPA energy traverses the grid -> wheeling + CSS charges apply
        #     - Banking (where allowed by mode) lets the LP time-shift PPA draw
        #       against load
        #
        #   GRID-ONLY (onsite_share = 0.0, re_ppa_capacity_mw = 0, M6):
        #     - All electricity from DISCOM grid at Grid_Cost
        #
        # The flat-PPA structure assumes firm delivery (developer-side firming
        # via storage, baked into the PPA price). This matches AP's Group
        # Captive and hybrid procurement structures where the offtaker
        # contracts for a fixed $/kWh blended tariff regardless of hourly
        # generation pattern.
        #
        # Parameters:
        #   onsite_share        : 1.0 (on-site) or 0.0 (off-site/grid)
        #   wheeling_charge     : $/kWh on PPA energy through the grid
        #   css_charge          : $/kWh on PPA energy + grid imports
        #   electricity_duty_waiver : fraction (0-1) reducing effective Grid_Cost
        #   re_ppa_price        : $/kWh flat PPA tariff for off-site RE
        #   re_ppa_capacity_mw  : MW cap on hourly PPA draw (0 = no PPA, e.g. M1, M6)
        self.onsite_share            = float(opt_params.get('onsite_share', 1.0))
        self.wheeling_charge         = float(opt_params.get('wheeling_charge', 0.0))
        self.css_charge              = float(opt_params.get('css_charge', 0.0))
        self.electricity_duty_waiver = float(opt_params.get('electricity_duty_waiver', 0.0))
        self.re_ppa_price            = float(opt_params.get('re_ppa_price', 0.0))
        self.re_ppa_capacity_mw      = float(opt_params.get('re_ppa_capacity_mw', 0.0))
        self.re_ppa_capacity_kw      = self.re_ppa_capacity_mw * 1000.0

        # Behind-the-meter storage on a CONTRACTED supply. Default 0 keeps the
        # historic behaviour (a plant that contracts its power builds nothing).
        # Set to 1 to let the LP size a battery while still buying its energy
        # under a PPA — i.e. self-firming instead of relying on DISCOM banking.
        self.allow_onsite_storage    = float(opt_params.get('allow_onsite_storage', 0.0))

        # Enforce procurement-mode capacity constraints:
        # If onsite_share == 0, on-site renewables cannot be sized; the battery
        # is blocked too unless allow_onsite_storage is set.
        # If re_ppa_capacity_mw == 0, no PPA energy can be drawn (grid-only or M1).
        if self.onsite_share == 0.0:
            _blocked = ('Solar', 'Wind') if self.allow_onsite_storage else ('Solar', 'Wind', 'Battery')
            for tech in _blocked:
                if tech in self.MaxCap:
                    self.MaxCap[tech] = 0.0
                if tech in self.MinCap:
                    self.MinCap[tech] = 0.0

        # ------------------------------------------------------------------
        # Banking (Open Access banking provision; AP solar policy = 100% annual)
        # ------------------------------------------------------------------
        # banking_capacity_hours : maximum stored kWh = banking_capacity_hours *
        #                          steel_capacity / nHours (i.e. expressed as hours
        #                          of plant nameplate equivalent). Set to 0 for
        #                          no-banking modes (M5); 8760 for full annual
        #                          (M2). Intermediate values approximate monthly /
        #                          quarterly settlement windows.
        # banking_efficiency     : round-trip efficiency (1.0 = lossless)
        # banking_fee            : $/kWh on deposit (charged when energy enters the
        #                          banking account)
        # The LP enables banking iff banking_capacity_hours > 0; otherwise the
        # banking DVs are pinned to zero.
        self.banking_capacity_hours = float(opt_params.get('banking_capacity_hours', 0.0))
        self.banking_efficiency     = float(opt_params.get('banking_efficiency', 1.0))
        self.banking_fee            = float(opt_params.get('banking_fee', 0.0))
        self.enable_banking         = self.banking_capacity_hours > 0

        # ------------------------------------------------------------------
        # Shaped (as-generated) PPA + monthly banking settlement
        # ------------------------------------------------------------------
        # ppa_delivery:
        #   'firm'   (default, legacy): Elec_FromPPA is a free dispatch
        #            variable capped at re_ppa_capacity_kw — developer-side
        #            firming is bundled into re_ppa_price. NOTE: against a
        #            firm flat tariff, banking is economically inert (the LP
        #            never deposits).
        #   'shaped': the PPA delivers as-generated energy from contracted
        #            solar/wind capacity. Cap_PPA_Solar / Cap_PPA_Wind are
        #            decision variables (their sum bounded by
        #            re_ppa_capacity_mw); the plant pays
        #            re_ppa_price_solar / re_ppa_price_wind on ALL generated
        #            energy (must-take PPA). Undelivered surplus is credited
        #            at banking_buyback_price (default 0 = lapses). This is
        #            the mode in which banking has real economic content.
        #
        # banking_settlement:
        #   'annual'  (default): circular annual account (hour 0 links to
        #             hour 8759), i.e. free annual carry-forward.
        #   'monthly': the Andhra Pradesh distribution-utility regime used in
        #             the report: monthly billing-cycle banking, 92% effective
        #             (8% banking charge in kind, via banking_efficiency),
        #             no carry-forward: the account resets each month and any
        #             month-end residual lapses (credited at
        #             banking_buyback_price, default 0).
        #
        # banking_monthly_cap_share: optional regulatory cap, monthly
        #             deposits <= share x monthly plant consumption. Off by
        #             default (0 = no cap); set the sheet column to 0.30 to
        #             impose the 30% cap of the AP 2024 open-access regulation.
        self.ppa_delivery = str(opt_params.get('ppa_delivery') or 'firm').strip().lower()
        self.re_ppa_price_solar        = float(opt_params.get('re_ppa_price_solar', 0.0))
        self.re_ppa_price_wind         = float(opt_params.get('re_ppa_price_wind', 0.0))
        self.banking_settlement = str(opt_params.get('banking_settlement') or 'annual').strip().lower()
        self.banking_monthly_cap_share = float(opt_params.get('banking_monthly_cap_share', 0.0))
        self.banking_buyback_price     = float(opt_params.get('banking_buyback_price', 0.0))
        if self.ppa_delivery not in ('firm', 'shaped'):
            raise ValueError(f"ppa_delivery must be 'firm' or 'shaped', got {self.ppa_delivery!r}")
        if self.banking_settlement not in ('annual', 'monthly'):
            raise ValueError(f"banking_settlement must be 'annual' or 'monthly', got {self.banking_settlement!r}")

        # ------------------------------------------------------------------
        # H2 procurement (pipeline-delivered baseline + on-site marginal)
        # ------------------------------------------------------------------
        # purchased_baseline_share : fraction of annual H2 demand met at a
        #                            constant rate from off-site pipeline / hub.
        #                            0 = H1 (full on-site); 1.0 = H2 (full
        #                            pipeline); 0 < x < 1 = H3 (hybrid).
        # purchased_delivered_cost : $/kg-H2 for the pipeline-delivered portion
        # ghci_compliance_opex     : $/kg-H2 certification OPEX (S3 archetype)
        # h2_subsidy_per_kg        : $/kg-H2 production subsidy (SIGHT)
        self.purchased_baseline_share = float(opt_params.get('purchased_baseline_share', 0.0))
        self.purchased_delivered_cost = float(opt_params.get('purchased_delivered_cost', 0.0))
        self.ghci_compliance_opex     = float(opt_params.get('ghci_compliance_opex', 0.0))
        self.h2_subsidy_per_kg        = float(opt_params.get('h2_subsidy_per_kg', 0.0))

        # CBAM border price ($/tCO2) — paid by the EU importer on the excess of
        # plant emissions above the EU CBAM benchmark, applied only to the
        # export share. Article 9 of CBAM Reg 2023/956 permits deduction of any
        # carbon price already paid in the country of origin (domestic CCTS).
        self.cbam_price = float(opt_params.get('cbam_price', 0.0))
        # EU CBAM benchmark (tCO2/tcs). NaN/None => CBAM term skipped.
        _eub = opt_params.get('eu_benchmark', None)
        self.eu_benchmark = (None if _eub is None or
                             (isinstance(_eub, float) and np.isnan(_eub))
                             else float(_eub))
        # Export share (fraction of annual production exposed to CBAM).
        self.export_share = float(opt_params.get('export_share', 0.0))

        # NG price override (Layer-1 archetype sensitivity)
        ng_price_override = opt_params.get('ng_price', None)
        if ng_price_override is not None and 'NG' in dat.commodity.index:
            dat.commodity.loc['NG', 'price'] = float(ng_price_override)

        # WACC override (Layer-1 archetype)
        wacc_override = opt_params.get('wacc', None)
        if wacc_override is not None and not (isinstance(wacc_override, float) and np.isnan(wacc_override)):
            self.WACC = float(wacc_override)

        # Run identifier (for reporting / CSV joining)
        self.run_id        = opt_params.get('run_id', 'default')
        self.scenario_tags = opt_params.get('scenario_tags', {})  # dict of Archetype/Route/Fuel/PowerMode/H2Mode

        # Objective tag (currently always LCOS; kept for forward compatibility).
        self.objective = opt_params.get('Objective', 'LCOS')


def define_DV(m: Model, Setting: InitialSetting) -> tuple[Model, dict]:
    DV = {}
    nHours = Setting.nHours

    # === Decision Variables ===
    for cap_type in Setting.investment_list:
        DV['Cap_'+cap_type] = m.addVar(
            lb=(0 if np.isnan(Setting.MinCap[cap_type]) else Setting.MinCap[cap_type]), 
            ub=(GRB.INFINITY if np.isnan(Setting.MaxCap[cap_type]) else Setting.MaxCap[cap_type]),
            name=cap_type+"Cap")  # Capacity, kW/kWh/kgH2   
    
    # Per-stage hourly throughput (physical t-input/h) and intermediate buffers.
    for stage in Setting.production_stages:
        DV[f'Stage_{stage}'] = m.addVars(nHours, lb=0, name=f"Stage_{stage}")
    for _, _, buf_name in Setting.intermediate_buffers:
        DV[f'Buffer_{buf_name}'] = m.addVars(nHours, lb=0, name=f"Buffer_{buf_name}")

    # Finished steel output and electricity flows.
    DV['Steel_Produced']  = m.addVars(nHours, lb=0, name="Steel_Produced")    # tcs/h
    DV['Elec_Used']       = m.addVars(nHours, lb=0, name="Elec_Used")         # kW
    DV['Elec_Curtailed']  = m.addVars(nHours, lb=0, name="Elec_Curtailed")    # kW
    DV['Elec_FromGrid']   = m.addVars(nHours, lb=0, name="Elec_FromGrid")     # kW
    DV['Elec_ToGrid']     = m.addVars(nHours, lb=0, name="Elec_ToGrid")       # kW
    for stage in Setting.production_stages:
        DV[f'Elec_{stage}'] = m.addVars(nHours, lb=0, name=f"Elec_{stage}")
    DV['Elec_electrolyser'] = m.addVars(nHours, lb=0, name="Elec_electrolyser")
    
    ###### Electricity Storage
    DV['Elec_Str_Charge'] = m.addVars(nHours, lb=0, name="ElecCharged")  # kW #Charge into storage
    DV['Elec_Str_Discharge'] = m.addVars(nHours, lb=0, name="ElecDischarged")  # kW #Discharge from storage
    DV['Elec_Str_Lev'] = m.addVars(nHours, lb=0, name="ElecInStorage")  # kWh #Energy level in storage

    # H2 production and storage.
    DV['H2_Used'] = m.addVars(nHours, lb=0, name="H2Used")
    DV['H2_Str_Charge'] = m.addVars(nHours, lb=0, name="H2Charged")  # kgH2 #H2 compressed for storage
    DV['H2_Str_Discharge'] = m.addVars(nHours, lb=0, name="H2Discharged")  # kgH2 #H2 discharged from storage
    DV['H2_Str_Lev'] = m.addVars(nHours, lb=0, name="H2Stored")  # kgH2 #H2 level in storage

    # H2 purchased from pipeline/hub (constant baseline rate, kg-H2/h).
    DV['H2_Purchased'] = m.addVars(nHours, lb=0, name="H2_Purchased")

    # Off-site renewable PPA: flat hourly delivery cap at re_ppa_capacity_kw,
    # purchased at re_ppa_price ($/kWh). The cap defaults to 0 (no PPA) for
    # on-site captive (M1) and grid-only (M6); off-site modes set it explicitly.
    DV['Elec_FromPPA'] = m.addVars(nHours, lb=0,
                                   ub=Setting.re_ppa_capacity_kw,
                                   name="Elec_FromPPA")

    # Shaped-PPA sizing: contracted as-generated solar/wind capacity
    # and hourly undelivered surplus. Pinned to zero in 'firm' mode (see
    # add_constraints); in 'shaped' mode Cap_PPA_Solar + Cap_PPA_Wind is
    # bounded by re_ppa_capacity_kw and hourly delivery follows the CF
    # profiles.
    DV['Cap_PPA_Solar'] = m.addVar(lb=0, ub=Setting.re_ppa_capacity_kw, name="Cap_PPA_Solar")
    DV['Cap_PPA_Wind']  = m.addVar(lb=0, ub=Setting.re_ppa_capacity_kw, name="Cap_PPA_Wind")
    DV['PPA_Surplus']   = m.addVars(nHours, lb=0, name="PPA_Surplus")

    # Banking account: deposits/withdrawals in kW (= kWh/h); level in kWh.
    # The banking DVs are unconditionally added so the constraint code is uniform,
    # but they're pinned to zero when Setting.enable_banking is False (see
    # add_constraints).
    DV['Bank_Deposit']  = m.addVars(nHours, lb=0, name="Bank_Deposit")
    DV['Bank_Withdraw'] = m.addVars(nHours, lb=0, name="Bank_Withdraw")
    DV['Bank_Lev']      = m.addVars(nHours, lb=0, name="Bank_Lev")

    # Reporting / accounting scalars (set by `set_objective`).
    # NOTE: opex_cost and co2_cost can be NEGATIVE under CCTS free-allowance
    # accounting (a plant emitting below the sector benchmark earns Carbon
    # Credit Certificates — net revenue, not cost). Allow these to go negative.
    DV['CO2_Annual'] = m.addVar(lb=0, name="CO2_Annual") #t CO2
    DV['inv_cost'] = m.addVar(lb=0, name="InvestmentCostH2")
    DV['opex_cost'] = m.addVar(lb=-gp.GRB.INFINITY, name="OperationCostH2")
    DV['co2_cost'] = m.addVar(lb=-gp.GRB.INFINITY, name="CO2Cost")
    DV['water_cost'] = m.addVar(name="WaterCost")
    DV['om_cost'] = m.addVar(name="OMCost")
    DV['grid_cost'] = m.addVar(name="GridCost")
    DV['total_cost'] = m.addVar(name="TotalCost")
    if Setting.ElectricitySelling:
        DV['grid_rev'] = m.addVar(name="GridRevenue")
    return m,DV


# ===========================================================================
# Constraints
# ===========================================================================

def add_constraints(m,DV,Setting,CF):
    """Attach all LP constraints. `CF` is a dict {tech: array of length nHours}."""
    nHours           = Setting.nHours
    hourly_steel_cap = Setting.steel_capacity / nHours  # tcs/h at full nameplate

    if Setting.steelmaking_stage is None:
        raise ValueError(
            "No steelmaking stage identified in production_stages "
            f"(expected one starting with 'EAF' or 'BOF'; got {Setting.production_stages}). "
            "Pass opt_params['steelmaking_stage'] explicitly for non-standard routes."
        )

    # -----------------------------------------------------------------
    # 1. Production cascade — either lockstep or per-stage flexible
    # -----------------------------------------------------------------

    if not Setting.process_flex:
        # Every stage runs in physical lockstep with finished-steel output;
        # buffers are pinned to zero.
        for t in range(nHours):
            for stage in Setting.production_stages:
                m.addConstr(DV[f'Stage_{stage}'][t] == DV['Steel_Produced'][t]*Setting.Input[stage],
                            name=f"StageLockstep_{stage}_{t}")
            for _, _, buf_name in Setting.intermediate_buffers:
                m.addConstr(DV[f'Buffer_{buf_name}'][t] == 0,name=f"BufferZero_{buf_name}_{t}")
    else:
        # Each stage dispatches under its own CF / ramp bounds. The
        # nameplate scales by Input[stage] because Stage_{stage}[t] is in
        # physical tonnes of the stage's input material per hour.
        
        for stage in Setting.production_stages:
            stage_nameplate = hourly_steel_cap * Setting.Input[stage]
            stage_max  = Setting.StageMaxCF[stage]    * stage_nameplate
            stage_min  = Setting.StageMinCF[stage]    * stage_nameplate
            stage_ramp = Setting.StageRampRate[stage] * stage_nameplate
            for t in range(nHours):
                prev = (t - 1) if t > 0 else (nHours - 1)
                m.addConstr(DV[f'Stage_{stage}'][t] <= stage_max, name=f"Stage_{stage}_max_{t}")
                m.addConstr(DV[f'Stage_{stage}'][t] >= stage_min, name=f"Stage_{stage}_min_{t}")
                delta=DV[f'Stage_{stage}'][t] - DV[f'Stage_{stage}'][prev]
                m.addConstr(delta <= stage_ramp,name=f"Stage_{stage}_RampUp_{t}")
                m.addConstr(delta >= -stage_ramp,name=f"Stage_{stage}_RampDown_{t}")

        # Steel output is the steelmaking stage's throughput converted back
        # to finished-steel via its Input multiplier (typically 1.0 for
        # EAF-Hot / BOF-Hot fed by DRI/HBI).
        sm_input = Setting.Input[Setting.steelmaking_stage]
        for t in range(nHours):
            m.addConstr(DV[f'Stage_{Setting.steelmaking_stage}'][t]
                        == DV['Steel_Produced'][t] * sm_input,
                        name=f"SteelmakingToOutput_{t}")

        # Buffer mass balance: level[t] = level[t-1] + in[t] - out[t].
        # The downstream stage is measured in its own input tonnes.
        # The upstream stage's output is in upstream-input units; convert via
        #   buffer_in[t] = (Stage_upstream[t] / Input[upstream]) * Input[downstream]
        #
        # The balance holds for ALL hours including t=0; the t=0 form uses
        # Buffer[nHours-1] as the "previous" level. 

        for upstream, downstream, buf_name in Setting.intermediate_buffers:
            conv = Setting.Input[downstream] / Setting.Input[upstream]
            for t in range(nHours):
                prev = (t - 1) if t > 0 else (nHours - 1)
                m.addConstr(DV[f'Buffer_{buf_name}'][t] <= Setting.BufferCap[buf_name],
                            name=f"BufferCap_{buf_name}_{t}")
                m.addConstr(
                    DV[f'Buffer_{buf_name}'][t]
                        == DV[f'Buffer_{buf_name}'][prev]
                    + DV[f'Stage_{upstream}'][t] * conv
                    - DV[f'Stage_{downstream}'][t],
                    name=f"BufferBalance_{buf_name}_{t}")

    # -----------------------------------------------------------------
    # 2. Annual production target (contractual offtake)
    # -----------------------------------------------------------------
    m.addConstr(gp.quicksum(DV['Steel_Produced'][t] for t in range(nHours))
        == Setting.steel_annual_production,
        name="AnnualProductionTarget")
    
    # -----------------------------------------------------------------
    # 3. Electricity balance and per-stage demand
    # -----------------------------------------------------------------
    # Energy sources (LHS):
    #   - On-site renewable generation (zero when onsite_share=0 because
    #     MaxCap is forced to 0 for Solar/Wind in InitialSetting)
    #   - Grid import (DISCOM tariff)
    #   - Off-site PPA delivery (flat $/kWh tariff, capped at re_ppa_capacity_kw)
    #   - Battery discharge
    #   - Banking withdrawal
    # Energy sinks (RHS):
    #   - Process and electrolyser use
    #   - Curtailment (free, used when on-site RE oversupplies)
    #   - Battery charge
    #   - Grid export (only if ElectricitySelling=True)
    #   - Banking deposit
    #
    # When the LP is in pure off-site mode (M2-M5), re_gen is structurally zero
    # and all electricity must come from Elec_FromPPA + Elec_FromGrid.
    for t in range(nHours):
        if not Setting.grid_connection:
            m.addConstr(DV['Elec_FromGrid'][t]==0, name=f"NoGridConnection_{t}")
        else:
            m.addConstr(DV['Elec_FromGrid'][t]<=Setting.Elec_FromGrid_Max, name=f"GridMax_{t}")
        if not Setting.ElectricitySelling:
            m.addConstr(DV['Elec_ToGrid'][t]==0, name=f"NoElectricitySelling_{t}")

        # On-site renewable generation.
        # In off-site procurement modes the LP has Cap_Solar = Cap_Wind = 0
        # (enforced via MaxCap in InitialSetting), so this term is zero.
        re_gen = gp.LinExpr()
        for var in Setting.generation_list:
            re_gen += CF[var][t] * DV[f'Cap_{var}'] * Setting.Efficiency[f'{var}']

        # Electricity balance.
        m.addConstr(re_gen
                    + DV['Elec_FromGrid'][t]
                    + DV['Elec_FromPPA'][t]
                    + DV['Elec_Str_Discharge'][t]
                    + DV['Bank_Withdraw'][t]
                    ==
                    DV['Elec_Used'][t]
                    + DV['Elec_Curtailed'][t]
                    + DV['Elec_Str_Charge'][t]
                    + DV['Elec_ToGrid'][t]
                    + DV['Bank_Deposit'][t],
                    name=f"EleBalance_{t}")

        # Per-stage process electricity.
        # electricity_sec[stage] is in kWh per tcs of finished steel; convert
        # the physical-tonne Stage variable back to steel-equivalent before
        # applying the SEC.
        for stage in Setting.production_stages:
            m.addConstr(DV[f'Elec_{stage}'][t]
                         == Setting.electricity_sec[stage] * DV[f'Stage_{stage}'][t]/ Setting.Input[stage],
                        name=f"Elec_{stage}_{t}")

        # Electrolyser load (production + storage compression).
        elec_for_h2 = ((DV['H2_Used'][t] + DV['H2_Str_Charge'][t]) * Setting.E_H2_production
                       + DV['H2_Str_Charge'][t] * Setting.E_H2_storage_compression)
        m.addConstr(DV['Elec_electrolyser'][t] == elec_for_h2, name=f"Elec_electrolyser_{t}")

        # Total electricity used = electrolyser + sum of process stages.
        process_elec_sum = gp.quicksum(DV[f'Elec_{s}'][t] for s in Setting.production_stages)
        m.addConstr(DV['Elec_Used'][t] == DV['Elec_electrolyser'][t] + process_elec_sum,
                    name=f"ElectUsedBalance_{t}")

        # Electrolyser sizing: instantaneous electrical load <= installed kW.
        m.addConstr((DV['H2_Used'][t] + DV['H2_Str_Charge'][t]) * Setting.E_H2_production
                    <= DV[f'Cap_Electrolyser'], name=f"ElectrolyzerLimit_{t}")

        if Setting.ElectricitySelling:
            m.addConstr(DV['Elec_ToGrid'][t] <= Setting.Grid_Selling_Max, name=f"GridSellingMax_{t}")

    # -----------------------------------------------------------------
    # 3a. Shaped-PPA delivery profile
    # -----------------------------------------------------------------
    # In 'shaped' mode the PPA delivers as-generated energy from contracted
    # solar/wind capacity (Cap_PPA_Solar / Cap_PPA_Wind decision variables):
    #     Elec_FromPPA[t] + PPA_Surplus[t] == Cap_s*CF_s[t] + Cap_w*CF_w[t]
    # The plant pays for ALL generated energy (must-take PPA — see
    # set_objective); PPA_Surplus is the undelivered remainder (lapses, or is
    # credited at banking_buyback_price). Banked energy must be OA-wheeled RE,
    # so deposits are capped by the hour's PPA delivery — this stops the LP
    # laundering cheap grid power through the banking account.
    # In 'firm' mode (legacy) these DVs are pinned to zero and Elec_FromPPA
    # keeps its flat dispatchable cap.
    if Setting.ppa_delivery == 'shaped':
        eff_s = float(Setting.Efficiency['Solar'])
        eff_w = float(Setting.Efficiency['Wind'])
        for t in range(nHours):
            gen_t = (DV['Cap_PPA_Solar'] * CF['Solar'][t] * eff_s
                     + DV['Cap_PPA_Wind'] * CF['Wind'][t] * eff_w)
            m.addConstr(DV['Elec_FromPPA'][t] + DV['PPA_Surplus'][t] == gen_t,
                        name=f"PPAShape_{t}")
            m.addConstr(DV['Bank_Deposit'][t] <= DV['Elec_FromPPA'][t],
                        name=f"BankFromPPAOnly_{t}")
        m.addConstr(DV['Cap_PPA_Solar'] + DV['Cap_PPA_Wind']
                    <= Setting.re_ppa_capacity_kw, name="PPACapTotal")
        # Annual generated energy per source (capacity x annual CF sum); the
        # objective charges the must-take energy payment on these.
        cf_solar_sum = float(sum(CF['Solar'][t] for t in range(nHours))) * eff_s
        cf_wind_sum  = float(sum(CF['Wind'][t]  for t in range(nHours))) * eff_w
        DV['PPA_Gen_Solar_annual'] = m.addVar(lb=0, name="PPA_Gen_Solar_annual")
        DV['PPA_Gen_Wind_annual']  = m.addVar(lb=0, name="PPA_Gen_Wind_annual")
        m.addConstr(DV['PPA_Gen_Solar_annual'] == DV['Cap_PPA_Solar'] * cf_solar_sum,
                    name="PPAGenSolarBind")
        m.addConstr(DV['PPA_Gen_Wind_annual'] == DV['Cap_PPA_Wind'] * cf_wind_sum,
                    name="PPAGenWindBind")
    else:
        m.addConstr(DV['Cap_PPA_Solar'] == 0, name="NoShapedPPASolar")
        m.addConstr(DV['Cap_PPA_Wind']  == 0, name="NoShapedPPAWind")
        for t in range(nHours):
            m.addConstr(DV['PPA_Surplus'][t] == 0, name=f"NoPPASurplus_{t}")

    # -----------------------------------------------------------------
    # 3b. Banking account dynamics
    # -----------------------------------------------------------------
    # Two settlement regimes (Setting.banking_settlement):
    #
    #   'monthly': the Andhra Pradesh distribution-utility regime used in the
    #   report: monthly billing-cycle banking, 92% effective (the 8% banking
    #   charge is taken in kind via banking_efficiency on deposit), and no
    #   carry-forward: the account starts at zero each calendar month and any
    #   month-end residual lapses (credited at banking_buyback_price in the
    #   objective; default 0). banking_capacity_hours acts only as the on/off
    #   switch in this regime; the monthly reset bounds the level. An optional
    #   regulatory cap (banking_monthly_cap_share > 0) limits monthly deposits
    #   to a share of monthly plant consumption; off by default.
    #
    #   'annual': circular account (hour 0 links to hour 8759), i.e. free
    #   annual carry-forward (the older state solar-policy framing). The level
    #   cap is banking_capacity_hours x contracted PPA capacity.
    if Setting.enable_banking:
        if Setting.banking_settlement == 'monthly':
            if nHours == 8760:
                month_len = [744, 672, 744, 720, 744, 720, 744, 744, 720, 744, 720, 744]
            else:   # non-standard horizon: 12 equal blocks
                month_len = [nHours // 12] * 12
                month_len[-1] += nHours - sum(month_len)
            bounds = np.cumsum([0] + month_len)
            for mi in range(12):
                t0, t1 = int(bounds[mi]), int(bounds[mi + 1])
                for t in range(t0, t1):
                    if t == t0:   # month opens empty: no carry-forward
                        m.addConstr(
                            DV['Bank_Lev'][t]
                            == Setting.banking_efficiency * DV['Bank_Deposit'][t]
                            - DV['Bank_Withdraw'][t],
                            name=f"BankBalance_{t}")
                    else:
                        m.addConstr(
                            DV['Bank_Lev'][t]
                            == DV['Bank_Lev'][t - 1]
                            + Setting.banking_efficiency * DV['Bank_Deposit'][t]
                            - DV['Bank_Withdraw'][t],
                            name=f"BankBalance_{t}")
                if Setting.banking_monthly_cap_share > 0:
                    m.addConstr(
                        gp.quicksum(DV['Bank_Deposit'][t] for t in range(t0, t1))
                        <= Setting.banking_monthly_cap_share
                        * gp.quicksum(DV['Elec_Used'][t] for t in range(t0, t1)),
                        name=f"BankMonthlyCap_{mi}")
            # Month-end hours, used by set_objective (residual buyback credit)
            # and print_results (lapsed-energy reporting).
            Setting.bank_month_ends = [int(b) - 1 for b in bounds[1:]]
        else:
            bank_capacity_kWh = Setting.banking_capacity_hours * Setting.re_ppa_capacity_kw
            for t in range(nHours):
                prev = (t - 1) if t > 0 else (nHours - 1)
                m.addConstr(DV['Bank_Lev'][t] <= bank_capacity_kWh,
                            name=f"BankCapMax_{t}")
                m.addConstr(
                    DV['Bank_Lev'][t]
                    == DV['Bank_Lev'][prev]
                    + Setting.banking_efficiency * DV['Bank_Deposit'][t]
                    - DV['Bank_Withdraw'][t],
                    name=f"BankBalance_{t}",
                )
            Setting.bank_month_ends = []
    else:
        # Banking disabled: pin all bank DVs to zero.
        for t in range(nHours):
            m.addConstr(DV['Bank_Deposit'][t]  == 0, name=f"NoBankDeposit_{t}")
            m.addConstr(DV['Bank_Withdraw'][t] == 0, name=f"NoBankWithdraw_{t}")
            m.addConstr(DV['Bank_Lev'][t]      == 0, name=f"NoBankLev_{t}")
        Setting.bank_month_ends = []

    # -----------------------------------------------------------------
    # 4. H2 demand at the DRI stage
    # -----------------------------------------------------------------
    # H2 supply (on-site electrolyser + storage discharge + pipeline purchase)
    # must meet DRI's hourly H2 demand.
    # H2_Demand_per_DRI is kg-H2 per tonne of DRI (route pole, Fe-scaled: the
    # reductant requirement is tied to Fe reduced, ~0.054 tH2/tFe stoich + excess,
    # so a lower-grade DRI carries proportionally less H2 per tDRI). Stage_dri[t]
    # is the DRI throughput (= Steel_Produced * Input[dri_stage], i.e. tDRI/h), so
    # hourly demand = H2_Demand_per_DRI * Stage_dri[t]. This makes total H2 scale
    # with DRI mass (fines routes make more tDRI/tcs -> more H2/tcs), the
    # physically correct basis. (The earlier /Input[dri_stage] erroneously
    # cancelled the DRI mass, pinning H2 to crude steel at a flat 58 kg/tcs.)
    if Setting.dri_stage is not None:
        dri_input = Setting.Input[Setting.dri_stage]
        # Annual H2 demand = H2_Demand_per_DRI * sum_t Stage_dri[t]
        #   = H2_Demand_per_DRI * steel_annual_production * dri_input [kg-H2/yr]
        annual_h2_demand = Setting.H2_Demand_per_DRI * Setting.steel_annual_production*dri_input
        baseline_kg_per_hr = (Setting.purchased_baseline_share * annual_h2_demand / nHours)
        for t in range(nHours):
            m.addConstr(DV['H2_Purchased'][t] == baseline_kg_per_hr,
                        name=f"H2PurchasedBaseline_{t}")
            m.addConstr(
                DV['H2_Used'][t] + DV['H2_Str_Discharge'][t] + DV['H2_Purchased'][t]
                >= Setting.H2_Demand_per_DRI * DV[f'Stage_{Setting.dri_stage}'][t],
                name=f"H2Demand_{t}",
            )
    else:
        # No DRI stage in the route — pin H2_Purchased to zero.
        for t in range(nHours):
            m.addConstr(DV['H2_Purchased'][t] == 0, name=f"NoH2Purchased_{t}")
    

    # -----------------------------------------------------------------
    # 5. Battery storage dynamics
    # -----------------------------------------------------------------
    for t in range(nHours):
        
        m.addConstr(DV['Elec_Str_Charge'][t] <= DV['Cap_Battery'] / Setting.storage_duration['Battery'],
                    name=f"BattChargeMax_{t}")
        m.addConstr(DV['Elec_Str_Discharge'][t] <= DV['Cap_Battery'] / Setting.storage_duration['Battery'],
                    name=f"BattDischargeMax_{t}")
        m.addConstr(DV['Elec_Str_Lev'][t] <= DV['Cap_Battery'],
                    name=f"BattLevMax_{t}")
        prev = (t - 1) if t > 0 else (nHours - 1)
        m.addConstr(
            DV['Elec_Str_Lev'][t]
            == DV['Elec_Str_Lev'][prev] * (1 - Setting.storage_decay['Battery'])
            + Setting.Efficiency['Battery'] * DV['Elec_Str_Charge'][t]
            - DV['Elec_Str_Discharge'][t],
            name=f"BattBalance_{t}",
        )
    
    # -----------------------------------------------------------------
    # 6. Electrolyser CF/ramp limits
    # -----------------------------------------------------------------
    # Cap_Electrolyser is in kW; convert to kg-H2/h via /E_H2_production
    # so ramping/CF bounds line up with the H2-throughput variables.
    elec_cap_in_h2 = DV['Cap_Electrolyser'] / Setting.E_H2_production  # kg-H2/h-equivalent       
        
    for t in range(nHours):
        prev = (t - 1) if t > 0 else (nHours - 1)
        h2_through = DV['H2_Used'][t] + DV['H2_Str_Charge'][t]
        h2_through_prev = DV['H2_Used'][prev] + DV['H2_Str_Charge'][prev]
        m.addConstr(h2_through >= Setting.MinCF['Electrolyser']*elec_cap_in_h2)
        m.addConstr(h2_through <= Setting.MaxCF['Electrolyser']*elec_cap_in_h2)
        m.addConstr(h2_through - h2_through_prev <=  Setting.RampRate['Electrolyser'] * elec_cap_in_h2,
                    name=f"ElectrolyserRampUp_{t}")
        m.addConstr(h2_through - h2_through_prev >= -Setting.RampRate['Electrolyser'] * elec_cap_in_h2,
                    name=f"ElectrolyserRampDown_{t}")

    # -----------------------------------------------------------------
    # 7. H2 storage dynamics
    # -----------------------------------------------------------------
    for t in range(nHours):
        prev = (t - 1) if t > 0 else (nHours - 1)
        m.addConstr(DV['H2_Str_Lev'][t] <= DV['Cap_H2Storage'],
                    name=f"H2StrLevMax_{t}")
        m.addConstr(DV['H2_Str_Discharge'][t] <= DV['Cap_H2Storage']*Setting.MaxCF['H2Storage'],
                    name=f"H2StrDischargeMax_{t}")
        m.addConstr(DV['H2_Str_Discharge'][t] >= DV['Cap_H2Storage']*Setting.MinCF['H2Storage'],
                    name=f"H2StrDischargeMin_{t}")
        m.addConstr(DV['H2_Str_Lev'][t] 
        == DV['H2_Str_Lev'][prev] 
        + Setting.Efficiency['H2Storage']*DV['H2_Str_Charge'][t]
        -DV['H2_Str_Discharge'][t],name=f"H2StrBalance_{t}")
        # H2 Out Ramping
        delta=DV['H2_Str_Discharge'][t] - DV['H2_Str_Discharge'][prev]
        m.addConstr(delta <= Setting.RampRate['H2Storage']*DV['Cap_H2Storage'])
        m.addConstr(delta >= -Setting.RampRate['H2Storage']*DV['Cap_H2Storage'])


    # -----------------------------------------------------------------
    # 8. Annual CO2 accounting (and optional cap)
    # -----------------------------------------------------------------
    annual_grid_kWh = gp.quicksum(DV['Elec_FromGrid'][t] for t in range(nHours))
    grid_emissions  = annual_grid_kWh * Setting.Grid_EF
    total_emissions = grid_emissions + Setting.base_emission * Setting.steel_annual_production
    m.addConstr(DV['CO2_Annual'] == total_emissions, name="AnnualCO2")
    if Setting.apply_emission_cap:
        m.addConstr(DV['CO2_Annual'] <= Setting.target_emission * Setting.steel_annual_production,
                    name="CO2Cap")

    log.info("Constraint set complete.")
    return m,DV


# ===========================================================================
# Objective
# ===========================================================================

def set_objective(m: Model, DV: dict, Setting: InitialSetting, dat) -> tuple[Model, dict]:
    """Build the LCOS objective and bind cost-component variables.

    Cost lines now include:
      - Annualised CAPEX (with renewable subsidy and electrolyser SIGHT subsidy)
      - OM cost (fraction of CAPEX, plus fixed steel-plant OM scaled with nameplate)
      - Carbon cost (full cradle-to-gate intensity: base_emission Scope 1+3 + grid Scope 2, minus free allowance; plus CBAM border price)
      - Water OPEX (electrolyser feedwater)
      - Grid electricity import cost (with electricity-duty waiver applied)
      - Wheeling + CSS charges on off-site RE and on grid imports
      - Banking deposit fee
      - H2 pipeline purchase cost (H2/H3 procurement modes)
      - GHCI compliance OPEX (export certification, S3 archetype)
      - SIGHT production subsidy (subtracted from H2 cost)
      - Grid-export revenue (subtracted)
    """
    nHours = Setting.nHours

    # ---------- Annualised investment cost ($/yr) ----------
    # Renewable / battery / electrolyser CAPEX. Subsidies enter as multiplicative
    # discounts: (1 - subsidy_frac) * CAPEX. The renewable subsidy applies to
    # Solar/Wind/Battery; the SIGHT electrolyser subsidy applies to the electrolyser only.
    inv_cost = gp.LinExpr()
    om_cost  = gp.LinExpr()
    for tech in Setting.investment_list:
        crf      = util.CRF(Setting.WACC, dat.tech.loc[tech]['economic_life_yr'])
        capex_ref = dat.tech.loc[tech]['CAPEX_ref']
        om_frac  = dat.tech.loc[tech]['om_to_capex']
        # Apply tech-specific subsidies.
        if tech == 'Electrolyser':
            net_capex = (1 - Setting.subsidy['elec_capex']) * capex_ref
        elif tech in config.onsite_generation_list + ['Battery']:
            net_capex = (1 - Setting.subsidy['renew_capex']) * capex_ref
        else:
            net_capex = capex_ref
        inv_cost += crf * net_capex * DV[f'Cap_{tech}']
        om_cost  += om_frac * net_capex * DV[f'Cap_{tech}']
    # Steel-plant fixed OM scales with nameplate (always nameplate, never net of subsidy).
    om_cost += Setting.steel_OM * Setting.steel_capacity

    # ---------- Carbon cost (full cradle-to-gate intensity) ----------
    # base_emission carries the per-tcs Scope 1 (process) AND Scope 3 (embedded
    # ore/pellet, fluxes, electrodes) emissions; the grid term adds Scope 2 hourly.
    # So the charge below is on the FULL cradle-to-gate intensity, not just Scope 1+2.
    # A strict domestic CCTS would charge only Scope 1+2; applying the full
    # cradle-to-gate intensity is a deliberate, conservative convention (it folds in
    # the purchased-feed Scope 3 the CCTS would not charge, understating the credit)
    # and is exactly the basis the EU CBAM uses. CBAM (export levy) is added separately below.
    annual_grid_kWh_obj    = gp.quicksum(DV['Elec_FromGrid'][t] for t in range(nHours))
    annual_prod            = Setting.steel_annual_production
    # Total annual plant emissions (tCO2/yr): base_emission (Scope 1+3) x prod + grid (Scope 2).
    total_annual_emissions = (Setting.base_emission * annual_prod
                              + annual_grid_kWh_obj * Setting.Grid_EF)
    gross_co2_cost         = total_annual_emissions * Setting.carbon_price
    free_allowance         = Setting.baseline_emission * annual_prod * Setting.carbon_price
    co2_cost               = gross_co2_cost - free_allowance

    # ---------- CBAM (EU border levy, export share only) ----------
    # Conservative LP rule (no Art 9 deduction inside the LP):
    #   levy_per_yr     = max(0, total_annual_emissions - eu_benchmark*annual_prod) * cbam_price
    #   total_cbam_cost = export_share * levy_per_yr
    # The max(0, ·) is LP-linearized via one non-negative aux variable (auto-
    # minimized because it appears in the cost direction of the objective).
    # Article 9 deduction (subtract domestic carbon price paid on the same
    # emissions) is NOT applied in the LP because the deduction term creates
    # an unbounded aux variable in pure-LP form. Instead it is applied as a
    # post-processing step in post-processing, where:
    #     Art9_deduction = max(0, total_emissions - ccts_baseline*annual_prod) * carbon_price
    #     Net CBAM cost (reported) = max(0, levy_per_yr - Art9_deduction) * export_share
    # For S3 plants whose LP-optimal CO2 falls below the CCTS sector benchmark
    # (2.6 tCO2/tcs), Art9 deduction = 0 and the LP value equals the net.
    if (Setting.eu_benchmark is not None
            and Setting.cbam_price > 0
            and Setting.export_share > 0):
        cbam_excess = m.addVar(lb=0, name='CBAM_excess_tonnes_per_yr')
        m.addConstr(cbam_excess >=
                    total_annual_emissions - Setting.eu_benchmark * annual_prod,
                    name='CBAMexcess_floor')
        cbam_cost = Setting.export_share * cbam_excess * Setting.cbam_price
        DV['cbam_excess'] = cbam_excess
    else:
        cbam_cost = 0.0

    # ---------- Water OPEX ----------
    h2o_cost = (Setting.WaterCost * Setting.WaterUse
                * gp.quicksum(DV['H2_Used'][t] + DV['H2_Str_Charge'][t]
                              for t in range(nHours)))

    # ---------- Grid electricity import cost ----------
    # The electricity-duty waiver (AP green H2 policy: 5-year exemption) reduces
    # the effective Grid_Cost. Duty is typically ~₹0.10–0.30/kWh in AP; the
    # waiver fraction multiplies that out of the Grid_Cost rate the caller supplies.
    effective_grid_cost = Setting.Grid_Cost * (1 - Setting.electricity_duty_waiver)
    grid_electricity_cost = gp.quicksum(DV['Elec_FromGrid'][t] * effective_grid_cost
                                        for t in range(nHours))

    # ---------- Off-site PPA cost and OA charges ----------
    # In off-site procurement modes (M2-M5), all renewable energy flows
    # through Elec_FromPPA at a flat $/kWh PPA tariff (re_ppa_price). That
    # tariff already bundles developer CAPEX + OM + margin and developer-side
    # firming costs, so the LP does NOT pay separate CAPEX on Solar/Wind in
    # those modes (their MaxCap is pinned to 0 in InitialSetting).
    #
    # On top of the PPA tariff, two grid-traversal charges apply:
    #   - Wheeling charge: $/kWh on PPA energy passing through the grid
    #   - CSS / additional surcharge: $/kWh on PPA energy (since it uses
    #     DISCOM infrastructure) and on grid imports
    #
    # Banking is treated separately (no wheeling on banked energy, since
    # banking exists as a regulatory carve-out for OA-procured RE).
    annual_ppa_kWh = gp.quicksum(DV['Elec_FromPPA'][t] for t in range(nHours))
    if Setting.ppa_delivery == 'shaped':
        # Must-take PPA: pay per-source tariffs on ALL generated energy
        # (annual gen vars bound in add_constraints), less any buyback credit
        # on (a) undelivered surplus and (b) month-end banked residuals that
        # lapse under the monthly settlement (both 0-valued by default:
        # banking_buyback_price = 0, as the regime has no compensation
        # provision).
        ppa_energy_cost = (Setting.re_ppa_price_solar * DV['PPA_Gen_Solar_annual']
                           + Setting.re_ppa_price_wind * DV['PPA_Gen_Wind_annual'])
        if Setting.banking_buyback_price > 0:
            buyback_kWh = gp.quicksum(DV['PPA_Surplus'][t] for t in range(nHours))
            if getattr(Setting, 'bank_month_ends', None):
                buyback_kWh += gp.quicksum(DV['Bank_Lev'][t]
                                           for t in Setting.bank_month_ends)
            ppa_energy_cost = ppa_energy_cost - Setting.banking_buyback_price * buyback_kWh
    else:
        ppa_energy_cost = Setting.re_ppa_price * annual_ppa_kWh
    # Wheeling/CSS are charged on energy wheeled into the plant (Elec_FromPPA).
    # Banked energy passes through Elec_FromPPA on the deposit side, so it
    # pays wheeling/CSS exactly once; the withdrawal is not charged again
    # (the 8% banking charge covers the banking service itself).
    wheeling_cost   = Setting.wheeling_charge * annual_ppa_kWh
    css_on_ppa      = Setting.css_charge      * annual_ppa_kWh
    css_on_import   = Setting.css_charge      * annual_grid_kWh_obj
    oa_charges      = wheeling_cost + css_on_ppa + css_on_import

    # ---------- Banking deposit fee ----------
    bank_fee_cost = Setting.banking_fee * gp.quicksum(DV['Bank_Deposit'][t]
                                                     for t in range(nHours))

    # ---------- H2 purchasing (pipeline) ----------
    # Pipeline-delivered H2 charged at $/kg; GHCI compliance applies to all
    # H2 used (whether on-site or purchased) since the certification covers
    # plant-gate consumption.
    annual_h2_purchased = gp.quicksum(DV['H2_Purchased'][t] for t in range(nHours))
    annual_h2_total     = gp.quicksum(DV['H2_Used'][t] + DV['H2_Purchased'][t]
                                      for t in range(nHours))
    h2_purchase_cost      = Setting.purchased_delivered_cost * annual_h2_purchased
    h2_ghci_cost          = Setting.ghci_compliance_opex * annual_h2_total
    h2_subsidy_credit     = Setting.h2_subsidy_per_kg * gp.quicksum(DV['H2_Used'][t]
                                                                    for t in range(nHours))

    # ---------- Grid export revenue ----------
    if Setting.ElectricitySelling:
        grid_selling_revenue = (gp.quicksum(DV['Elec_ToGrid'][t] for t in range(nHours))
                                * Setting.Grid_Selling_Price)
    else:
        grid_selling_revenue = 0

    # ---------- OPEX aggregate ----------
    ope_cost = (
        co2_cost
        + cbam_cost                         # CBAM export levy (0 if not S3)
        + h2o_cost
        + grid_electricity_cost
        + ppa_energy_cost
        + oa_charges
        + bank_fee_cost
        + h2_purchase_cost
        + h2_ghci_cost
        - h2_subsidy_credit
        - grid_selling_revenue
        + om_cost
        + Setting.base_OPEX * Setting.steel_annual_production
    )

    # ---------- CAPEX (incl. steel plant CAPEX, scaled by nameplate) ----------
    total_inv_cost = inv_cost + Setting.steel_CAPEX_annual * Setting.steel_capacity

    # ---------- Total cost and LCOS ----------
    total_cost = ope_cost + total_inv_cost
    unit_cost  = total_cost / Setting.steel_annual_production  # $/tcs

    # Bind reporting variables.
    m.addConstr(DV['inv_cost']   == total_inv_cost,        name="InvestmentCost")
    m.addConstr(DV['opex_cost']  == ope_cost,              name="OperationCost")
    m.addConstr(DV['water_cost'] == h2o_cost,              name="WaterCost")
    m.addConstr(DV['co2_cost']   == co2_cost,              name="CO2Cost")
    m.addConstr(DV['grid_cost']  == grid_electricity_cost, name="GridCost")
    m.addConstr(DV['om_cost']    == om_cost,               name="OMCost")
    m.addConstr(DV['total_cost'] == total_cost,            name="TotalCost")
    if Setting.ElectricitySelling:
        m.addConstr(DV['grid_rev'] == grid_selling_revenue, name="GridRevenue")
    # New reporting handles for the LCOS stack.
    DV['oa_cost_var']     = m.addVar(name="OACost")
    DV['bank_fee_var']    = m.addVar(name="BankFee")
    DV['h2_purch_var']    = m.addVar(name="H2PurchaseCost")
    DV['h2_ghci_var']     = m.addVar(name="H2GHCICost")
    DV['h2_subsidy_var']  = m.addVar(name="H2Subsidy")
    DV['ppa_cost_var']    = m.addVar(name="PPACost")
    m.addConstr(DV['oa_cost_var']     == oa_charges,            name="OACostBind")
    m.addConstr(DV['bank_fee_var']    == bank_fee_cost,         name="BankFeeBind")
    m.addConstr(DV['h2_purch_var']    == h2_purchase_cost,      name="H2PurchaseBind")
    m.addConstr(DV['h2_ghci_var']     == h2_ghci_cost,          name="H2GHCIBind")
    m.addConstr(DV['h2_subsidy_var']  == h2_subsidy_credit,     name="H2SubsidyBind")
    m.addConstr(DV['ppa_cost_var']    == ppa_energy_cost,       name="PPACostBind")

    m.setObjective(unit_cost, GRB.MINIMIZE)
    return m, DV

# ===========================================================================
# Reporting
# ===========================================================================
def print_results(m: Model, DV: dict, Setting: InitialSetting, dat) -> pd.DataFrame:
    """Pull the optimised solution into a one-row DataFrame for CSV-append.

    Returns an empty DataFrame when the LP is not solved to optimality.
    Caller can detect this via `len(results) == 0`.
    """
    if m.Status != GRB.OPTIMAL:
        log.warning("Optimization did not find an optimal solution (status=%s).", m.Status)
        return pd.DataFrame()
    
    nHours = Setting.nHours
    annual_prod = Setting.steel_annual_production
    hourly_cap  = Setting.steel_capacity / nHours

    results = pd.DataFrame({'Region': Setting.region}, index=[0])
    results['Run_ID']      = Setting.run_id
    results['target_year'] = Setting.target_year
    results['WACC']        = Setting.WACC
    results['annual_cf']   = Setting.annual_cf
    results['carbon_price']     = Setting.carbon_price
    results['cbam_price']       = Setting.cbam_price
    results['baseline_emission']= Setting.baseline_emission
    results['apply_emission_cap'] = Setting.apply_emission_cap

    # Scenario tags (Archetype/Route/Fuel/PowerMode/H2Mode).
    for k, v in Setting.scenario_tags.items():
        results[k] = v

    results['LCOS'] = DV['total_cost'].X /annual_prod  # $/tpa
    results['CO2 intensity'] = DV['CO2_Annual'].X/annual_prod
    results['Total Cost']=DV['total_cost'].X
    results['Total CAPEX'] = DV['inv_cost'].X/ annual_prod
    # steel_CAPEX_annual is $/t-nameplate/yr (line 249): scale by nameplate
    # before dividing by production, mirroring the objective (line ~1179).
    results['Total Steel CAPEX'] = (Setting.steel_CAPEX_annual
                                    * Setting.steel_capacity / annual_prod)
    # Per-investment capacity and CAPEX contribution.
    for tech in Setting.investment_list:
        lifetime = dat.tech.loc[tech]['economic_life_yr']
        crf      = util.CRF(Setting.WACC, lifetime)
        results[f'{tech} Capacity'] = DV[f'Cap_{tech}'].X
        results[f'{tech} CAPEX']    = (dat.tech.loc[tech]['CAPEX_ref']
                                       * DV[f'Cap_{tech}'].X * crf / annual_prod)

    results['Total OPEX'] = DV['opex_cost'].X/annual_prod
    results['Electricity Cost'] = DV['grid_cost'].X/annual_prod
    results['CO2 Cost'] = DV['co2_cost'].X/annual_prod
    results['Water Cost'] = DV['water_cost'].X/annual_prod
    results['OM Cost'] = DV['om_cost'].X/annual_prod
    results['Total Steel OPEX'] = Setting.base_OPEX
    results['Electricity Rev']= (DV['grid_rev'].X / annual_prod
                                   if Setting.ElectricitySelling else 0)

    # New cost lines from Part-2 additions (all $/tcs).
    results['OA Charges']           = DV['oa_cost_var'].X    / annual_prod
    results['Banking Fee']          = DV['bank_fee_var'].X   / annual_prod
    results['PPA Energy Cost']      = DV['ppa_cost_var'].X   / annual_prod
    results['H2 Purchase Cost']     = DV['h2_purch_var'].X   / annual_prod
    results['H2 GHCI OPEX']         = DV['h2_ghci_var'].X    / annual_prod
    results['H2 Subsidy Credit']    = DV['h2_subsidy_var'].X / annual_prod
    # CBAM reporting ($/tcs). Zero unless S3 (cbam_price > 0, eu_benchmark set).
    if 'cbam_excess' in DV:
        cbam_excess_yr = DV['cbam_excess'].X            # tCO2/yr in excess of EU benchmark
        cbam_gross_levy = (Setting.export_share * cbam_excess_yr
                           * Setting.cbam_price / annual_prod)  # $/tcs
        # Art 9 deduction (post-processing): only positive when plant > CCTS baseline.
        emission_per_tcs = (Setting.base_emission
                            + (sum(DV['Elec_FromGrid'][t].X for t in range(Setting.nHours))
                               * Setting.Grid_EF / annual_prod))
        art9_per_tcs = max(0.0, emission_per_tcs - Setting.baseline_emission) * Setting.carbon_price
        net_per_tcs  = max(0.0, cbam_gross_levy / max(Setting.export_share, 1e-9) - art9_per_tcs)
        results['CBAM_excess_tCO2_per_yr'] = cbam_excess_yr
        results['CBAM Gross Levy']         = cbam_gross_levy
        results['CBAM Art9 Deduction']     = Setting.export_share * art9_per_tcs
        results['CBAM Net Levy']           = Setting.export_share * net_per_tcs
    else:
        results['CBAM_excess_tCO2_per_yr'] = 0.0
        results['CBAM Gross Levy']         = 0.0
        results['CBAM Art9 Deduction']     = 0.0
        results['CBAM Net Levy']           = 0.0
    results['eu_benchmark']  = Setting.eu_benchmark if Setting.eu_benchmark else 0.0
    results['export_share']  = Setting.export_share

    # Procurement-mode summary (helps explain LCOS variation across modes).
    results['onsite_share']            = Setting.onsite_share
    results['wheeling_charge']         = Setting.wheeling_charge
    results['css_charge']              = Setting.css_charge
    results['electricity_duty_waiver'] = Setting.electricity_duty_waiver
    results['re_ppa_price']            = Setting.re_ppa_price
    results['re_ppa_capacity_mw']      = Setting.re_ppa_capacity_mw
    results['banking_capacity_hours']  = Setting.banking_capacity_hours
    results['banking_efficiency']      = Setting.banking_efficiency
    results['banking_fee']             = Setting.banking_fee
    results['purchased_baseline_share']= Setting.purchased_baseline_share
    results['purchased_delivered_cost']= Setting.purchased_delivered_cost

    # Banking utilisation (kWh deposited / withdrawn over the year).
    annual_bank_deposit  = sum(DV['Bank_Deposit'][t].X  for t in range(nHours))
    annual_bank_withdraw = sum(DV['Bank_Withdraw'][t].X for t in range(nHours))
    results['Bank_Deposit_annual_kWh']  = annual_bank_deposit
    results['Bank_Withdraw_annual_kWh'] = annual_bank_withdraw

    # Shaped-PPA reporting: contracted capacity, generation, surplus,
    # and banked energy lapsing at month end (monthly settlement).
    results['ppa_delivery']       = Setting.ppa_delivery
    results['banking_settlement'] = Setting.banking_settlement
    results['PPA_Contracted_Solar_MW'] = DV['Cap_PPA_Solar'].X / 1000.0
    results['PPA_Contracted_Wind_MW']  = DV['Cap_PPA_Wind'].X / 1000.0
    results['PPA_Surplus_annual_kWh']  = sum(DV['PPA_Surplus'][t].X for t in range(nHours))
    month_ends = getattr(Setting, 'bank_month_ends', []) or []
    results['Bank_Lapsed_annual_kWh']  = sum(DV['Bank_Lev'][t].X for t in month_ends)

    # PPA utilisation (kWh drawn from off-site PPA over the year).
    annual_ppa = sum(DV['Elec_FromPPA'][t].X for t in range(nHours))
    results['PPA_annual_kWh']         = annual_ppa
    # Grid import: the residual the DISCOM supplies — the emissions
    # driver for the grid-leaning modes (M4/M5).
    results['Grid_Import_annual_kWh'] = sum(DV['Elec_FromGrid'][t].X for t in range(nHours))
    results['PPA_avg_load_factor']    = (annual_ppa / (Setting.re_ppa_capacity_kw * nHours)
                                          if Setting.re_ppa_capacity_kw > 0 else 0)

    # H2 sourcing breakdown (annual kg-H2).
    annual_h2_onsite    = sum(DV['H2_Used'][t].X for t in range(nHours))
    annual_h2_purchased = sum(DV['H2_Purchased'][t].X for t in range(nHours))
    results['H2_Onsite_annual_kg']    = annual_h2_onsite
    results['H2_Purchased_annual_kg'] = annual_h2_purchased
    results['H2_Total_annual_kg']     = annual_h2_onsite + annual_h2_purchased

    # Per-stage reporting.
    results['process_flex'] = Setting.process_flex
    for stage in Setting.production_stages:
        annual_kwh = sum(DV[f'Elec_{stage}'][t].X for t in range(nHours))
        results[f'Elec_{stage}_annual_kWh']  = annual_kwh
        results[f'Elec_{stage}_kWh_per_tcs'] = annual_kwh / annual_prod
        results[f'CAPEX_{stage}_per_tcs']     = (Setting.steel_CAPEX_breakdown[stage]
                                                 * Setting.steel_capacity / annual_prod)
        results[f'OM_{stage}_per_tcs']        = Setting.steel_OM_breakdown[stage]
        results[f'Emission_{stage}_per_tcs']  = Setting.emission_breakdown[stage]
        results[f'OPEX_{stage}_per_tcs']      = Setting.base_OPEX_breakdown[stage]
        #results[f'CarbonPrice_{stage}']       = Setting.carbon_price_breakdown[stage]
        results[f'Input_{stage}_t_per_tcs']   = Setting.Input[stage]

        # Hourly throughput stats (physical tonnes); CF normalised by the
        # Input-scaled nameplate so 100% physical CF reads as 1.0.
        hourly = np.array([DV[f'Stage_{stage}'][t].X for t in range(nHours)])
        stage_nameplate = hourly_cap * Setting.Input[stage]
        results[f'Stage_{stage}_annual_tonnes'] = hourly.sum()
        results[f'Stage_{stage}_avg_CF']        = hourly.mean() / stage_nameplate
        results[f'Stage_{stage}_min_CF']        = hourly.min()  / stage_nameplate
        results[f'Stage_{stage}_max_CF']        = hourly.max()  / stage_nameplate

    annual_kwh_electrolyser = sum(DV['Elec_electrolyser'][t].X for t in range(nHours))
    results['Elec_electrolyser_annual_kWh']  = annual_kwh_electrolyser
    results['Elec_electrolyser_kWh_per_tcs'] = annual_kwh_electrolyser / annual_prod

    # Buffer utilisation.
    for _, _, buf_name in Setting.intermediate_buffers:
        levels = np.array([DV[f'Buffer_{buf_name}'][t].X for t in range(nHours)])
        results[f'Buffer_{buf_name}_max_tonnes'] = levels.max()
        results[f'Buffer_{buf_name}_avg_tonnes'] = levels.mean()
        # Hours of full-finished-steel-rate coverage.
        results[f'Buffer_{buf_name}_max_hours']  = levels.max() / hourly_cap

    return results
