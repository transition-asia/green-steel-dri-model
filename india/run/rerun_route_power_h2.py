"""Stage 2 of the campaign: power and hydrogen procurement for the fines routes.

Solves the RunMatrix rows marked Stage 2 in Scenario_Sheets.xlsx: the fluidised bed and the
rotary kiln at 100% hydrogen under the procurement modes that Section 7 tests for the shaft
furnace. These hourly solves are slower than the Stage 1 rows. Each row is written as it completes; rows already in the
output file are solved again and replaced.

The self-build (M1) rows here run on the case's emission cap. The shaft-furnace self-build row
of Stage 1 carries an Emission_Cap_Override of 0.40 tCO2/tcs, which the fines routes cannot
meet, so their self-build emissions are not like-for-like with that row.
"""
import sys, time, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as M, Modules_GSE as GSE, pandas as pd


OUT = '../results/Results_RoutePowerH2.csv'

sheets = GSE.load_scenario_sheets(M.scenario_file)
rm = sheets['run_matrix']
RUNS = rm[rm['Stage'] == 2].index.tolist()

print(f'Stage 2: {len(RUNS)} rows, writing to {OUT}', flush=True)
t_start = time.time()

for i, rid in enumerate(RUNS, 1):
    t0 = time.time()
    try:
        res = M.run_one_scenario(rid, sheets, {})
        if not len(res):
            print(f'[{i}/{len(RUNS)}] {rid} EMPTY ({time.time()-t0:.0f}s)', flush=True)
            continue
        row = res.iloc[0].to_dict()
        prev = pd.read_csv(OUT) if os.path.exists(OUT) else pd.DataFrame()
        if len(prev):
            prev = prev[prev.Run_ID != rid]
        pd.concat([prev, pd.DataFrame([row])], ignore_index=True).to_csv(OUT, index=False)
        print(f'[{i}/{len(RUNS)}] {rid} LCOS={row["LCOS"]:.1f} '
              f'CO2={row["CO2 intensity"]:.3f} ({time.time()-t0:.0f}s) '
              f'[elapsed {(time.time()-t_start)/60:.0f}m]', flush=True)
    except Exception as e:
        print(f'[{i}/{len(RUNS)}] {rid} ERR {type(e).__name__}: {e} '
              f'({time.time()-t0:.0f}s)', flush=True)

print(f'ALL DONE in {(time.time()-t_start)/60:.0f} min', flush=True)
