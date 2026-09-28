"""Write CCTS_boundary.csv and CBAM_boundary.csv (read by Report_Figures.ipynb,
Figures 5.1 and 5.2) from results/boundary_result.csv. Re-run after boundary_calc.py.

The CBAM file's ETS/charge/landed columns use the report's $86 -> $140 path; the
notebook reads only E_cbam from it and applies its own ETS array.
"""
import os
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
BF_LCOS, BF_CRADLE, BF_S1, BF_S2, BF_CBAM = 536.0, 2.60, 2.10, 0.13, 2.20
BF_CCTS = BF_S1 + BF_S2
BM = {'DRI': 0.475, 'BF': 1.364}                               # IR 2025/2621
YEARS = np.arange(2026, 2035)
FREE = np.array([.975, .95, .90, .775, .515, .39, .265, .14, .0])  # Guidance No. 4, Table 2-1
ETS = 86.0 + 6.75 * (YEARS - 2026)
NAME = {'R1b': 'SF', 'R2': 'FB', 'R3': 'RK', 'R1c': 'SF-BF'}
H2 = {'F1': 0.0, 'F2': 30.0, 'F3': 70.0, 'F4': 100.0}


def ccts_rows(t):
    rows = []
    for _, r in t.iterrows():
        prem = r.LCOS - BF_LCOS
        rows.append(dict(Route=r.Route, RouteName=NAME[r.Route], Fuel=r.Fuel, H2_pct=H2[r.Fuel],
                         LCOS_usd_tcs=round(r.LCOS, 2), E_ccts=round(r.E_ccts, 4),
                         Scope1=round(r.S1, 4), Scope2=round(r.E_ccts - r.S1, 4),
                         E_cradle=round(r.E_report, 4),
                         Xover_ccts=round(prem / (BF_CCTS - r.E_ccts), 2),
                         Xover_cradle=round(prem / (BF_CRADLE - r.E_report), 2),
                         Gap_max_at_24=round((BF_CCTS - r.E_ccts) - prem / 24.0, 4)))
    rows.append(dict(Route='BF-BOF', RouteName='BF-BOF', Fuel=np.nan, H2_pct=np.nan,
                     LCOS_usd_tcs=BF_LCOS, E_ccts=BF_CCTS, Scope1=BF_S1, Scope2=BF_S2,
                     E_cradle=BF_CRADLE))
    return pd.DataFrame(rows)


def cbam_rows(t):
    bf_charge = ETS * np.maximum(0.0, BF_CBAM - BM['BF'] * FREE)
    bf_landed = BF_LCOS + bf_charge
    rows = []
    for _, r in t.iterrows():
        ded = BM['DRI'] * FREE
        charge = ETS * np.maximum(0.0, r.E_cbam - ded)
        paid = np.nonzero(charge > 0)[0]
        first = float(YEARS[paid[0]]) if len(paid) else np.nan
        for i, yr in enumerate(YEARS):
            rows.append(dict(Route=r.Route, RouteName=NAME[r.Route], Fuel=r.Fuel, H2_pct=H2[r.Fuel],
                             Year=yr, LCOS_usd_tcs=round(r.LCOS, 2), E_cbam=round(r.E_cbam, 4),
                             Benchmark=BM['DRI'], CBAM_factor_free=FREE[i],
                             Deduction=round(ded[i], 4), ETS_usd_tCO2=ETS[i],
                             Charge_usd_tcs=round(charge[i], 2),
                             Landed_usd_tcs=round(r.LCOS + charge[i], 2),
                             Advantage_vs_BFBOF=round(bf_landed[i] - r.LCOS - charge[i], 2),
                             First_pay_year=first,
                             Xover_cbam=round((r.LCOS - BF_LCOS) / (BF_CBAM - r.E_cbam), 2)))
    for i, yr in enumerate(YEARS):
        rows.append(dict(Route='BF-BOF', RouteName='BF-BOF', Fuel=np.nan, H2_pct=np.nan, Year=yr,
                         LCOS_usd_tcs=BF_LCOS, E_cbam=BF_CBAM, Benchmark=BM['BF'],
                         CBAM_factor_free=FREE[i], Deduction=round(BM['BF'] * FREE[i], 4),
                         ETS_usd_tCO2=ETS[i], Charge_usd_tcs=round(bf_charge[i], 2),
                         Landed_usd_tcs=round(bf_landed[i], 2), Advantage_vs_BFBOF=0.0,
                         First_pay_year=np.nan, Xover_cbam=np.nan))
    return pd.DataFrame(rows)


if __name__ == '__main__':
    t = pd.read_csv(os.path.join(HERE, '..', 'results', 'boundary_result.csv'))
    ccts_rows(t).to_csv(os.path.join(HERE, '..', 'results', 'CCTS_boundary.csv'), index=False)
    cbam_rows(t).to_csv(os.path.join(HERE, '..', 'results', 'CBAM_boundary.csv'), index=False)
    print('wrote results/CCTS_boundary.csv and results/CBAM_boundary.csv')
