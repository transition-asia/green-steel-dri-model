"""Solve one case and compare it with the archived result.

    python solve_one.py BASE_R1b_F4_M2_H1
    python solve_one.py BASE_R1b_F4_M2s_H1 --algorithm ipm --verbose

The run identifier is a Run_ID of the RunMatrix sheet in Scenario_Sheets.xlsx. Expect one to two
minutes for firm-contract and natural-gas cases and one to two hours for banked hourly cases.
The campaign files are not touched; the row is saved to results/solve_one_<Run_ID>.csv.
Run from india/run/.
"""
import argparse, os, sys, time

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument('run_id')
ap.add_argument('--algorithm', choices=['choose', 'simplex', 'ipm'], default='choose', help='HiGHS algorithm (default: HiGHS chooses)')
ap.add_argument('--verbose', action='store_true', help='show the solver log')
args = ap.parse_args()

import pandas as pd
import main

main.SOLVER_PARAMS['output_flag'] = bool(args.verbose)
main.SOLVER_PARAMS['solver'] = args.algorithm

sheets = main.GSE.load_scenario_sheets(main.scenario_file)
if args.run_id not in sheets['run_matrix'].index:
    sys.exit(f'{args.run_id} is not a Run_ID of the RunMatrix sheet.')
print(f'Solving {args.run_id} with HiGHS ({args.algorithm}) ...', flush=True)
t0 = time.time()
res = main.run_one_scenario(args.run_id, sheets, {})
dt = time.time() - t0
if len(res) == 0:
    sys.exit(f'No optimal solution ({dt:.0f} s).')
lcos = float(res['LCOS'].iloc[0])
out = f'../results/solve_one_{args.run_id}.csv'
res.to_csv(out, index=False)
print(f'LCOS = {lcos:.2f} $/tcs, CO2 = {float(res["CO2 intensity"].iloc[0]):.4f} tCO2/tcs  ({dt:.0f} s)  -> {out}')

ref = pd.read_csv('../results/Results_ALL.csv')
hit = ref[ref.Run_ID == args.run_id]
if len(hit):
    r = float(hit.LCOS.iloc[0])
    print(f'Archived result (results/Results_ALL.csv): {r:.2f} $/tcs (difference {lcos - r:+.2f})')
else:
    print('No archived row for this case in results/Results_ALL.csv.')
