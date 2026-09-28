#!/bin/zsh
# Solve the full campaign: the RunMatrix rows of Scenario_Sheets.xlsx marked Stage 1 (40 rows, main.py)
# and Stage 2 (14 fines-route power and hydrogen procurement rows, rerun_route_power_h2.py).
# Resumable: both stages skip rows already present in their CSV. Expect a few days in total
# (banked hourly solves take one to two hours each in HiGHS).
# Usage: PY=/path/to/python ./run_campaign.sh
cd "$(dirname "$0")"
PY=${PY:-python}
echo "=== stage 1: main campaign $(date) ==="
for i in $(seq 1 60); do
  n=$($PY -c "import pandas as pd,os; p='../results/Results_Scenario_Campaign.csv'; print(len(pd.read_csv(p)) if os.path.exists(p) else 0)")
  if [ "$n" -ge 40 ]; then echo "stage 1 done ($n rows) $(date)"; break; fi
  echo "--- attempt $i (have $n rows) $(date) ---"
  $PY -c "import sys; sys.path.insert(0,'.'); import main; main.SOLVER_PARAMS['threads']=3; main.main()" 2>&1 | tail -3
done
echo "=== stage 2: route x power/hydrogen rows $(date) ==="
for i in $(seq 1 20); do
  n=$($PY -c "import pandas as pd,os; p='../results/Results_RoutePowerH2.csv'; print(len(pd.read_csv(p)) if os.path.exists(p) else 0)")
  if [ "$n" -ge 14 ]; then echo "stage 2 done ($n rows) $(date)"; break; fi
  $PY rerun_route_power_h2.py 2>&1 | tail -3
done
echo "=== finished $(date). Next: python build_results_table.py ==="
