"""Scope-1/2/3 decomposition per route, replicating GSE_full + process_together."""
import sys, os
sys.path.insert(0, os.path.abspath('../../model'))
import pandas as pd, numpy as np
import config, Modules_GSE as GSE
import main as M

XLS, YEAR, REGION = '../data/Model_input_India.xlsx', 2030, 'IN'
CHAINS = {'R1b': ['DRI-SF','EAF-Hot'],
          'R2':  ['DRI-FB','ESF-Hot','BOF-Hot'],
          'R3':  ['DRI-RK','ESF-Hot','BOF-Hot'],
          'R1c': ['DRI-SFBF','ESF-Hot','BOF-Hot']}
H2RATIO = {'F1':0.0, 'F2':0.30, 'F3':0.70, 'F4':1.0}
SCRAP = 0.15
PREM  = {'R1b': 12.6}

rows = []
for route, chain in CHAINS.items():
    ores = M.get_ore_settings(route, PREM.get(route, 0.0))
    for fuel, hr in H2RATIO.items():
        stage_e, inputs = [], []
        for step, stage in enumerate(chain):
            _, dat = GSE.run_GSE_step(
                steel_tech=stage, ore_setting=ores[stage], target_year=YEAR,
                input_file=XLS, region=REGION, H2_ratio=hr, Briquetting=False,
                H2_onsite=True, scrap_ratio=SCRAP, exclude_electricity=True)
            e = GSE.emissions_all_scope(dat)
            stage_e.append(e)
            inputs.append(1.0 if step == 0 else float(dat.steel.loc['Ore','Value']))
        # cumulative downstream multiplier (final stage = 1.0)
        cum = [1.0]*len(chain)
        for k in range(len(chain)-2, -1, -1):
            cum[k] = cum[k+1] * inputs[k+1]
        agg = {'S1':0.0,'S2':0.0,'S3':0.0,'S3_ore':0.0}
        detail = {}
        for k, e in enumerate(stage_e):
            for item, r in e.iterrows():
                v = float(r['tCO2e/Unit']) * cum[k]
                sc = int(r['Scope'])
                agg[f'S{sc}'] += v
                if sc == 3 and item == 'Ore':
                    agg['S3_ore'] += v
                detail[(chain[k], item, sc)] = v
        rows.append(dict(Route=route, Fuel=fuel, **agg,
                         S3_other=agg['S3']-agg['S3_ore'],
                         detail=detail))

df = pd.DataFrame(rows)
pd.set_option('display.width',220)
print(df[['Route','Fuel','S1','S2','S3','S3_ore','S3_other']].round(4).to_string())
df.drop(columns=['detail']).to_csv('../results/boundary_scope_split.csv', index=False)
print()
print('--- detail, R1b F4 ---')
for k,v in rows[3]['detail'].items():
    if abs(v)>1e-6: print('   ', k, round(v,4))
print('--- detail, R3 F4 ---')
r3 = [r for r in rows if r['Route']=='R3' and r['Fuel']=='F4'][0]
for k,v in r3['detail'].items():
    if abs(v)>1e-6: print('   ', k, round(v,4))
