# Green steel DRI model

A plant-level techno-economic model of green steel made by direct reduction, with country studies as folders. The first study, `india/`, is the analysis behind *Is Green Steel Within Reach in India? Green DRI Economics, Policy Drivers and Site Feasibility in Andhra Pradesh* (Transition Asia and TERI, 2026). It computes the levelised cost of steel (LCOS) and the emission intensity of a 9.6 Mtpa direct-reduction steel plant on the Andhra Pradesh coast, for three gas-based direct-reduction routes (shaft furnace on DR-grade pellet; fluidised bed and rotary kiln on low-grade fines), across fuels from 100% natural gas to 100% green hydrogen, and under the power- and hydrogen-procurement options available in the state.

The repository holds the model (`model/`, shared by every study) and, for the India study, its inputs, the scenario definitions, the solved cases, and the three post-processing steps that turn solver output into the report's tables: the results table, the emissions-boundary accounting (cradle-to-gate, CCTS and CBAM) and the project-finance layer. The report's figures are not included. Everything runs on open-source software; the LP solver is HiGHS.

## Layout

```
model/           the plant model, shared by every study: mass-energy balances per stage
                 (Modules_GSE, Modules_process), the hourly cost-minimising LP that sizes and
                 dispatches renewables, electrolyser, storage and the reactors
                 (Modules_optimization), the HiGHS modelling layer (lp_backend), helpers
                 (Modules_utility), config
india/data/      Model_input_India.xlsx (technical, price, policy and emission inputs, with
                 sources in the sheet notes); H2-NG.csv (reductant blend table); hourly solar
                 and wind profiles for the site (renewables.ninja, 2019)
india/run/       solving: main.py (campaign driver), Scenario_Sheets.xlsx (scenario layers and
                 the RunMatrix of cases), rerun_route_power_h2.py (stage 2), run_campaign.sh,
                 solve_one.py (one case, compared with its archived row)
                 post-processing: build_results_table.py (results table), boundary_scope_split.py,
                 boundary_calc.py, make_boundary_csvs.py (emissions boundaries), bankability.py
                 (project finance, Table 7.2), report_data.py (accessor)
india/results/   Results_ALL.csv (one row per solved case) and the emissions-boundary tables
                 (boundary_scope_split.csv, boundary_result.csv, CCTS_boundary.csv,
                 CBAM_boundary.csv); new solves are written here too
india/docs/      Steelmaking_Reference.md (derivation of the process coefficients and
                 literature ranges)
```

A further country study would add a sibling folder with the same four parts and reuse `model/` unchanged; each study's inputs, scenario sheet and results are self-contained.

## Setup

```bash
conda env create -f environment.yml && conda activate green-steel-ap
```

or `pip install -r requirements.txt` into a Python 3.11+ environment (pandas, numpy, scipy, openpyxl and highspy). All scripts are run from `india/run/`. Everything downstream of `india/results/Results_ALL.csv` needs only pandas and numpy.

## What the model does

For one case (a route, a fuel blend, a power-procurement mode, a hydrogen-supply mode and a policy archetype) the model:

1. builds the mass and energy balance of every stage from ore to crude steel (`Modules_GSE`, `Modules_process`): ore, pellet or fines, reductant, electricity, fluxes, carbon, scrap, and the emissions of each input;
2. solves an hourly linear programme over 8,760 hours (`Modules_optimization`) that chooses the least-cost sizes of solar, wind, electrolyser, hydrogen storage and battery, and their dispatch against the plant's load, within each reactor's turndown and ramp limits, under the contract terms of the power mode (as-generated or firm supply, wheeling and surcharges, utility banking, grid purchases) and any emission cap;
3. reports the levelised cost of steel in 2025 US dollars per tonne of crude steel, the emission intensity, and the cost and physical breakdown of the solution.

Costs are annualised at the weighted average cost of capital over each asset's life; the plant is 9.6 Mtpa at an 85% capacity factor, decided in 2030. Hydrogen support (the SIGHT incentive and the state electrolyser capital subsidy) is included in the base case; the no-subsidy case removes it. Carbon prices are not applied inside the model: the value of the CCTS, the fuel-switch thresholds and the EU border charge are computed from the solved cases and the boundary tables.

## Workflow

| Step | Command (from `india/run/`) | Produces | Time |
|---|---|---|---|
| 1. Solve the campaign | `PY=python ./run_campaign.sh` | `india/results/Results_Scenario_Campaign.csv` (40 rows) and `india/results/Results_RoutePowerH2.csv` (14 rows) | days; resumable |
| 2. Update the results table | `python build_results_table.py` | `india/results/Results_ALL.csv`: one row per solved case with cost buckets and emissions on every boundary; fresh solves replace the archived rows of the same case | seconds |
| 3. Emissions boundaries | `python boundary_scope_split.py && python boundary_calc.py && python make_boundary_csvs.py` | `india/results/boundary_scope_split.csv`, `boundary_result.csv`, `CCTS_boundary.csv`, `CBAM_boundary.csv` | minutes |
| 4. Finance table (Table 7.2) | `python bankability.py` | DSCR, IRR and break-even steel price for the four power structures and the hub-hydrogen variant | seconds |

Steps 3 and 4 run against the shipped `india/results/Results_ALL.csv`, so the tables can be regenerated without re-solving. `boundary_calc.py` reads the results table and `build_results_table.py` reads `boundary_result.csv`; after a fresh solve run step 2, then step 3, then step 2 again.

To solve one case and compare it with its archived row:

```bash
python solve_one.py BASE_R1b_F4_M2_H1
python solve_one.py BASE_R1b_F4_M2s_H1 --algorithm ipm --verbose
```

## Solver

The plant model is a pure linear programme of about 210,000 variables and 330,000 constraints per case, solved with the open-source HiGHS solver through the thin modelling layer in `model/lp_backend.py`. Solver options are HiGHS option names, held in `SOLVER_PARAMS` in `main.py` (`output_flag`, `solver`, `threads`; add `time_limit` in seconds to cap a run). By default HiGHS chooses between simplex and interior point per case; `--algorithm ipm` or `simplex` forces one in `solve_one.py`.

Solve times on a laptop: one to two minutes for firm-contract and natural-gas cases, one to two hours for the banked hourly cases, so a full campaign takes a few days. `run_campaign.sh` is resumable and skips rows already solved.

## Run identifiers

Every case is named `Case_Route_Fuel_PowerMode_H2Mode` in the RunMatrix sheet of `Scenario_Sheets.xlsx` and in the `Run_ID` column of `Results_ALL.csv`; `BASE_R1b_F4_M2s_H1` is the base case.

| Dimension | Codes |
|---|---|
| Case | `BASE`: 2030 base case, no carbon price, hydrogen support included. `NOSUB`: all hydrogen support removed. |
| Route | `R1b` shaft furnace on purchased DR-grade pellet with EAF (the report's shaft furnace). `R2` fluidised bed with ESF and BOF. `R3` rotary kiln with ESF and BOF. `R1c` shaft furnace on BF-grade pellet with ESF and BOF (Section 4.2 of the report). |
| Fuel | `F1` 100% natural gas, `F2` 30% hydrogen, `F3` 70% hydrogen, `F4` 100% hydrogen |
| Power mode | `M2s` group captive, as-generated solar-wind portfolio firmed by utility banking (base case). `M2b` the same without banking. `M2` group captive on a firm round-the-clock contract. `M5` and `M5a` third-party open access, banked and unbanked. `M1` self-build (`M1u` without the 0.4 tCO2/tcs cap). `M4` grid supply. `M3b` and `M3d` are older open-access variants kept for reference. |
| Hydrogen mode | `H1` on-site electrolysis. `H2` bought from the hub at the delivered price. `H3` hub baseline with on-site top-up. |

## Results

`india/results/Results_ALL.csv` holds the 54 cases used in the report, one row each: the levelised cost (`LCOS_usd_tcs`), the emission intensity cradle-to-gate (`CO2_cradle`) and on the CCTS and CBAM boundaries, the cost-stack buckets, hydrogen and electricity per tonne, the sized capacities, and every column the solver writes. The report's base case is `BASE_R1b_F4_M2s_H1`: $606/tcs and 0.29 tCO2/tcs. The model is a pure LP with a unique optimal value, so a fresh solve of any case lands within solver tolerance of its archived row (`BASE_R1b_F4_M2_H1` reproduces to the cent).

## Data notes

Input sources are recorded in the notes columns of `india/data/Model_input_India.xlsx`; the derivation of the process coefficients is in `india/docs/Steelmaking_Reference.md`. Rupee inputs are converted at Rs 87.16 per US dollar (the 2025 average) unless the note beside a value states another rate. The electrolyser capital cost is anonymised project data from Andhra Pradesh and is not attributable to a project. Hourly solar and wind profiles come from renewables.ninja for the site coordinates and the Anantapur wind belt. Commodity prices are 2025 market anchors escalated at 2% a year real to the 2030 decision year. The BF-BOF comparator ($536/tcs, 2.6 tCO2/tcs cradle-to-gate) is taken from the IECC (2026) India study.

## Licence

MIT, see `LICENSE`.
