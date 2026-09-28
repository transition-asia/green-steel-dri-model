# -*- coding: utf-8 -*-
"""Project-finance layer for Section 7.2 (Table 7.2): DSCR / IRR / break-even steel price.

Real 2025 USD throughout.
Assumptions (Table 7.2 note): 70% gearing, 7% real interest over a
15-year tenor, 25.17% corporate tax, 20-year straight-line depreciation, a
four-year construction draw (15/30/35/20) and 40 years of operation, with the
electrolyser re-spent every 10 years and the battery every 15.
"""
import numpy as np
import pandas as pd
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CAMPAIGN = os.path.join(HERE, '..', 'results', 'Results_ALL.csv')
STEEL_TPA = 8_160_000.0

WACC        = 0.10
GEARING     = 0.70
DEBT_RATE   = 0.07
TENOR       = 15
TAX         = 0.2517
DEP_YEARS   = 20
BUILD       = [0.15, 0.30, 0.35, 0.20]
LIFE        = 40
STEEL_PRICE = 650.0

def crf(r, n):
    return r * (1 + r) ** n / ((1 + r) ** n - 1)

CRF = {'steel': crf(WACC, 40), 'solar': crf(WACC, 30), 'wind': crf(WACC, 30),
       'battery': crf(WACC, 15), 'electrolyser': crf(WACC, 10), 'h2storage': crf(WACC, 25)}
RELIFE = {'electrolyser': 10, 'battery': 15}

_df = pd.read_csv(CAMPAIGN)


def overnight(row):
    """Overnight capital by component ($bn), from the annualized $/tcs columns.
    'Total CAPEX' is net of the electrolyser subsidy and covers steel + every
    energy asset, so the electrolyser is what remains once the explicitly
    reported generation and storage components are removed."""
    p = STEEL_TPA
    ann = {'steel': float(row['Total Steel CAPEX'])}
    for k, col in (('solar', 'Solar CAPEX'), ('wind', 'Wind CAPEX'),
                   ('battery', 'Battery CAPEX'), ('h2storage', 'H2Storage CAPEX')):
        v = row.get(col, 0.0)
        v = 0.0 if pd.isna(v) else float(v)
        if v:
            ann[k] = v
    energy = float(row['Total CAPEX']) - ann['steel']
    elec = energy - sum(v for k, v in ann.items() if k != 'steel')
    if elec > 0:
        ann['electrolyser'] = elec
    return {k: v * p / CRF[k] / 1e9 for k, v in ann.items() if v > 0}


def cashflows(row, steel_price=STEEL_PRICE):
    ov = overnight(row)
    capex = sum(ov.values())
    cash_opex = float(row['Total OPEX'])                      # $/tcs, excludes capital
    ebitda = (steel_price - cash_opex) * STEEL_TPA / 1e9      # $bn/yr

    debt = capex * GEARING
    svc = debt * crf(DEBT_RATE, TENOR)                        # level annuity
    dep = capex / DEP_YEARS

    bal, eq_cf, dscr = debt, [], []
    for i, f in enumerate(BUILD):                              # construction
        eq_cf.append(-capex * f * (1 - GEARING))
    for y in range(1, LIFE + 1):
        respend = sum(ov[k] for k, n in RELIFE.items() if k in ov and y % n == 0 and y < LIFE)
        interest = bal * DEBT_RATE if y <= TENOR else 0.0
        principal = (svc - interest) if y <= TENOR else 0.0
        bal = max(bal - principal, 0.0)
        taxable = ebitda - interest - (dep if y <= DEP_YEARS else 0.0)
        tax = max(taxable, 0.0) * TAX
        cfads = ebitda - tax
        if y <= TENOR:
            dscr.append(cfads / svc)
        eq_cf.append(cfads - (svc if y <= TENOR else 0.0) - respend)
    return dict(capex=capex, debt=debt, ebitda=ebitda, service=svc,
                min_dscr=min(dscr), avg_dscr=float(np.mean(dscr)), eq_cf=eq_cf)


def irr(cf, lo=-0.5, hi=1.0):
    f = lambda r: sum(c / (1 + r) ** i for i, c in enumerate(cf))
    if f(lo) * f(hi) > 0:
        return float('nan')
    for _ in range(200):
        mid = (lo + hi) / 2
        if f(lo) * f(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def price_for_dscr(row, target=1.3):
    lo, hi = 400.0, 1200.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if cashflows(row, mid)['min_dscr'] < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def report(run_id, steel_price=STEEL_PRICE):
    row = _df[_df.Run_ID == run_id].iloc[0]
    r = cashflows(row, steel_price)
    return dict(run=run_id, lcos=float(row['LCOS']), capex=r['capex'], debt=r['debt'],
                ebitda=r['ebitda'], min_dscr=r['min_dscr'], avg_dscr=r['avg_dscr'],
                irr=irr(r['eq_cf']), p13=price_for_dscr(row))


if __name__ == '__main__':
    # Table 7.2 of the report (group captive banked, group captive self-firmed, third-party open
    # access, self-build on the case's emission cap), then the hub-hydrogen variant of Section 7.3.
    ROWS = [('Group captive, banked (base case)', 'BASE_R1b_F4_M2s_H1'), ('Group captive, self-firmed', 'BASE_R1b_F4_M2b_H1'),
            ('Third-party open access, banked', 'BASE_R1b_F4_M5_H1'), ('Self-build', 'BASE_R1b_F4_M1u_H1'),
            ('Base case with hub hydrogen (Section 7.3)', 'BASE_R1b_F4_M2s_H2')]
    for label, rid in ROWS:
        d = report(rid)
        print(f"{label:42s} LCOS {d['lcos']:6.1f} | capital ${d['capex']:5.2f}bn | debt ${d['debt']:5.2f}bn | "
              f"EBITDA ${d['ebitda']:.2f}bn | min DSCR {d['min_dscr']:.2f} | equity IRR {d['irr']:.1%} | "
              f"steel price for 1.3x ${d['p13']:.0f}")
