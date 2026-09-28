"""What changes if CCTS and CBAM are scored on their OWN counting boundaries
instead of the report's cradle-to-gate one."""
import pandas as pd, numpy as np, sys, os
sys.path.insert(0, os.path.abspath('.'))
pd.set_option('display.width', 240)

# ---- 1. scope split from boundary_scope_split.py (S2 there = stage elec at grid EF; ignore) ----
sp = pd.read_csv('../results/boundary_scope_split.csv').set_index(['Route','Fuel'])

# ---- 2. solved results ----
import report_data as R
d = R._df.copy()
d['grid_kwh_tcs'] = d['Grid_Import_annual_kWh'] / 8_160_000.0
GRID_EF = 0.0004                      # tCO2/kWh, Grid sheet 2030
BF_LCOS, BF_CG = 536.0, 2.60          # cradle-to-gate comparator

# BF-BOF scope split (integrated Indian mill). Scope 1 dominates because coke
# ovens, sinter, BF, BOF, lime and the on-site power plant on BF/COG all burn
# on site. Range tested below.
BF_S1, BF_S2, BF_S3 = 2.10, 0.13, 0.37     # sums to 2.60

# CBAM-countable share of each model Scope-3 line
ELECTRODE_OXID = 3.67/4.45      # in-furnace oxidation -> direct (mass balance)
PELLET_INDUR   = 127.0/139.0    # TERI: induration 127, mining 12 kg/t

def cbam_green(route, fuel):
    s = sp.loc[(route, fuel)]
    s1 = s.S1
    # pellet routes: agglomerated ore CN 2601 12 00 is a CBAM precursor
    ore = s.S3_ore * PELLET_INDUR if route in ('R1b','R1c') else 0.0
    # electrode oxidation is a direct emission under the mass-balance rule
    # electrode consumption: ESF 0.0013 (RK, R1c) / 0.00125 (FB)
    # t/tHM x 1.048 tHM/tcs x 4.45 tCO2/t; EAF 0.002 t/tcs unchanged
    elec_ox = {'R1b':0.0089,'R1c':0.0061,'R2':0.0058,'R3':0.0061}[route]*ELECTRODE_OXID
    # ferro-alloys are CBAM goods (direct-only); keep them
    fa = 0.0219
    return s1 + ore + elec_ox + fa

rows=[]
for route in ('R1b','R2','R3','R1c'):
    for fuel in ('F1','F2','F3','F4'):
        r = d[(d.Case=='BASE')&(d.Route==route)&(d.Fuel==fuel)&
              (d.PowerMode=='M2s')&(d.H2Mode=='H1')]
        if not len(r): continue
        r = r.iloc[0]
        s = sp.loc[(route,fuel)]
        s2_actual = r['grid_kwh_tcs']*GRID_EF
        rows.append(dict(Route=route, Fuel=fuel, LCOS=r['LCOS'],
            E_report=r['CO2 intensity'], S1=s.S1, S2=s2_actual, S3=s.S3,
            E_ccts=s.S1+s2_actual, E_cbam=cbam_green(route,fuel)))
t = pd.DataFrame(rows)

def xover(L,E,BF_L,BF_E): return (L-BF_L)/(BF_E-E)
t['xover_report'] = xover(t.LCOS,t.E_report,BF_LCOS,BF_CG)
t['xover_ccts']   = xover(t.LCOS,t.E_ccts, BF_LCOS,BF_S1+BF_S2)
t['xover_cbam']   = xover(t.LCOS,t.E_cbam, BF_LCOS,BF_S1)

print('=== emissions intensity (tCO2/tcs) and equal-benchmark crossover ($/tCO2) ===')
print(t.round(3).to_string(index=False))
print()
print('BF-BOF: cradle-to-gate %.2f | CCTS (S1+S2) %.2f | CBAM (direct) %.2f'
      % (BF_CG, BF_S1+BF_S2, BF_S1))
t.to_csv('../results/boundary_result.csv', index=False)

# ---- 3. CBAM landed-cost schedule, Fig 6.2 logic ----
BENCH = 1.55
FREE  = {2026:0.975, 2027:0.95, 2028:0.90, 2029:0.775, 2030:0.635,
         2031:0.495, 2032:0.355, 2033:0.215, 2034:0.0}
ETS   = {2026:86, 2027:92, 2028:98, 2029:105, 2030:112,
         2031:118, 2032:125, 2033:132, 2034:140}   # $/tCO2, report path
def charge(E, yr): return ETS[yr]*max(0.0, E - BENCH*FREE[yr])

print('\n=== CBAM charge $/t and advantage vs BF-BOF, report boundary vs CBAM boundary ===')
for route,lab in (('R1b','SF'),('R3','RK'),('R2','FB')):
    r = t[(t.Route==route)&(t.Fuel=='F4')].iloc[0]
    dL = r.LCOS - BF_LCOS
    out=[]
    for yr in (2026,2030,2032,2033,2034):
        a_rep = (charge(BF_CG,yr) - charge(r.E_report,yr)) - dL
        a_cbam= (charge(BF_S1,yr) - charge(r.E_cbam,yr))  - dL
        out.append((yr, round(charge(r.E_report,yr)), round(charge(r.E_cbam,yr)),
                    round(a_rep), round(a_cbam)))
    print(f'{lab} (LCOS premium ${dL:.0f}) E_rep={r.E_report:.3f} E_cbam={r.E_cbam:.3f}')
    print('   yr  green-pays(rep) green-pays(cbam) | advantage(rep) advantage(cbam)')
    for o in out: print('   %d   %6d %10d          %8d %10d' % o)

# ---- 4. CCTS transfers at a common benchmark ----
print('\n=== CCTS transfers, common benchmark 1.0 and 2.2 tCO2/tcs, $24/tCO2 ===')
for bench in (1.0, 2.2):
    for route,lab in (('R1b','SF'),('R3','RK'),('R2','FB')):
        r = t[(t.Route==route)&(t.Fuel=='F4')].iloc[0]
        cr_rep = (bench - r.E_report)*24; cr_cc = (bench - r.E_ccts)*24
        bf_rep = (bench - BF_CG)*24;      bf_cc = (bench - (BF_S1+BF_S2))*24
        net_rep = (r.LCOS - cr_rep) - (BF_LCOS - bf_rep)
        net_cc  = (r.LCOS - cr_cc ) - (BF_LCOS - bf_cc)
        print('  bench %.1f %s: credit rep $%.0f / ccts $%.0f ; BF pays rep $%.0f / ccts $%.0f'
              ' ; net gap rep $%.0f / ccts $%.0f' %
              (bench, lab, cr_rep, cr_cc, -bf_rep, -bf_cc, net_rep, net_cc))
