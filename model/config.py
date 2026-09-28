"""Project-wide configuration constants.

This module holds three things:
  - `steel_investment`: maps user-facing process labels in
    `opt_params['breakdown_loads']['Process']` (e.g. 'EAF-Hot') to the
    technology key used to look up CAPEX, CF, ramp, and lifetime parameters
    in `dat.tech` (e.g. 'EAF'). Hot/cold variants share the same equipment
    family, hence the many-to-one mapping.
  - `CAPEX_OVERFACTOR`: a flat multiplier applied to per-stage CAPEX inside
    `process_together` to account for owner's costs not present in the
    reference CAPEX (grid connection, water, land, contingency, etc.).
  - `onsite_generation_list`: which entries in
    `opt_params['Investment options']` are dispatchable from hourly capacity
    factors (Solar/Wind), as opposed to sizing-only investments (Battery,
    Electrolyser, H2Storage).
"""
steel_investment = {
    'DRI-SF':         'DRI-SF',
    'DRI-SFBF':       'DRI-SF',   # R1c: shaft furnace on BF-grade pellet — same equipment family
    'DRI-FB':         'DRI-FB',
    'DRI-RK':         'DRI-RK',
    'BOF-Cold':       'BOF',
    'BOF-Hot':        'BOF',
    'EAF-Cold':       'EAF',
    'EAF-Hot':        'EAF',
    'ESF-Cold':       'ESF',
    'ESF-Hot':        'ESF',
}


# Flat owner's-cost multiplier on per-stage CAPEX. Applied inside
# `process_together`. Documented here so it's discoverable from the
# objective function.
CAPEX_OVERFACTOR = 1.2

# Investment options whose hourly dispatch follows an exogenous capacity
# factor profile (passed in `CF` to `add_constraints`).
onsite_generation_list = ['Wind', 'Solar']