import pandas as pd
import numpy as np

def DRI(steel_tech,
        params_steel,
        params_h2,
        commodity,
        H2_ratio=None,
        H2_onsite=None,
        H2_price=None,
        Briquetting=None):
    params_steel['Value']=params_steel['H2']*H2_ratio+params_steel['NG']*(1-H2_ratio)
    # Blend curve: H2-NG.csv (Muller et al.) gives the NONLINEAR fuel split vs
    # H2 energy share, anchored at 55.33 kg H2 (100% H2) and 9.479 MMBtu NG
    # (100% NG) for a shaft furnace. Each route sheet carries its own poles
    # (Params_DRI-*: H2 column of 'H2' row; NG column of 'NG' row), so we scale
    # the curve to the route's poles rather than using the SF values for all
    # routes. E_heating uses the route's own kWh/kg coefficient (sheet
    # E_heating(H2)/H2 pole: SF 4.5, FB 9.5, RK 11.0); H2-recycle compression
    # (E_H2_recirc, 0.4 kWh/kg) is included in the stage electricity.
    ratio_checksheet=pd.read_csv('../data/H2-NG.csv',index_col='% H2')
    H2_curve=np.interp(H2_ratio, ratio_checksheet.index, ratio_checksheet['H2'])
    NG_curve=np.interp(H2_ratio, ratio_checksheet.index, ratio_checksheet['NG'])
    H2_pole=params_steel.loc['H2','H2']          # route kg-H2/tDRI at 100% H2
    NG_pole=params_steel.loc['NG','NG']          # route MMBtu/tDRI at 100% NG
    csv_H2_pole=ratio_checksheet['H2'].iloc[-1]  # 55.331
    csv_NG_pole=ratio_checksheet['NG'].iloc[0]   # 9.4787
    H2_per_DRI=H2_curve*(H2_pole/csv_H2_pole if pd.notna(H2_pole) else 1.0)
    NG_per_DRI=NG_curve*(NG_pole/csv_NG_pole if pd.notna(NG_pole) else 1.0)
    e_heat_pole=params_steel.loc['E_heating','H2']
    heat_coeff=(e_heat_pole/H2_pole) if (pd.notna(e_heat_pole) and pd.notna(H2_pole) and H2_pole>0) else 4.5
    if 'E_H2_recirc' in params_steel.index and pd.notna(params_steel.loc['E_H2_recirc','H2']) and pd.notna(H2_pole) and H2_pole>0:
        recirc_coeff=params_steel.loc['E_H2_recirc','H2']/H2_pole
    else:
        recirc_coeff=0.0
    params_steel.loc['H2','Value']=H2_per_DRI
    params_steel.loc['NG','Value']=NG_per_DRI
    params_steel.loc['E_heating','Value']=H2_per_DRI*heat_coeff
    params_steel.loc['E_H2_recirc','Value']=H2_per_DRI*recirc_coeff
    params_steel['Value']=params_steel['Value'].fillna(params_steel['Global'])

    if H2_onsite is None:
        raise ValueError('‼️ H2 onsite parameter is not set. Please specify if H2 is produced onsite or purchased externally. ‼️')
    elif H2_onsite==True:
        print("=====The H2 emission and cost need to be calculated seperately====")
        E_H2=params_h2.loc['E_H2_kWh','Value']*H2_per_DRI # energy required to produce H2 to make 1tcs
        params_steel.loc['H2','Type']=''
        params_steel.loc['H2','Emission Scope']=0 
    elif H2_onsite==False:
        params_steel.loc['H2','Type']='Fuel'
        params_steel.loc['H2','Emission Scope']=3
        E_H2=0
        if H2_price is None:
            raise ValueError('H2 price must be provided if H2 onsite is False')
        else:
            commodity.loc['H2','price']=H2_price
                    
        if Briquetting is None:
            raise ValueError('‼️ Briquetting parameter is not set. Please specify if HBI briquetting is used. ‼️')
        elif Briquetting==True:
            params_steel.loc['E_briquetting','Value']=params_steel.loc['E_briquetting','Global']
        elif Briquetting==False:
            params_steel.loc['E_briquetting','Value']=0
    params_steel.loc['E_H2','Value']=E_H2
    params_steel.loc['Grid Electricity','Value']=(params_steel.loc['E_briquetting','Value']
        +params_steel.loc['E_heating','Value']+params_steel.loc['E_aux','Value']
        +params_steel.loc['E_H2_recirc','Value'])
    return params_steel,commodity


def EAF(steel_tech,params_steel):
    scrap_ratio=params_steel.loc['Scrap ratio','Value'] # mass fraction of scrap in EAF feed, 0-1
    HBI_grade=params_steel.loc['HBI Grade','Value']  # e.g., 1 for HBI-DRI, 2 for HBI-BF
    Average_Fe=scrap_ratio+(1-scrap_ratio)*HBI_grade  # average Fe content in the feed, tFe/tcs
    metallic_charge=params_steel.loc['Full HBI','Value']/Average_Fe  # t metallic charge per tcs, assuming all HBI is charged as metallic feed

    m_HBI=metallic_charge*(1-scrap_ratio)  # t HBI/tcs
    m_scrap=metallic_charge*scrap_ratio  # t scrap/tcs

    # Write the recomputed mass balance back to the Ore (HBI) and
    # Scrap rows. Previously only Grid Electricity was written, so the sheet's
    # precomputed values (frozen at scrap_ratio=0.10: Ore 0.9863, Scrap 0.1096)
    # drove the upstream cascade and the scrap OPEX regardless of the route
    # sheet's scrap_ratio — the whole DRI/pellet/H2/power chain was sized at
    # 10% scrap while the EAF electricity used the requested ratio. The 'Ore'
    # row doubles as the stage Input multiplier in GSE_full, so this makes the
    # upstream chain scale with scrap_ratio. At scrap_ratio=0.10 this
    # reproduces the sheet values exactly.
    params_steel.loc['Ore','Value']=m_HBI      # tHBI/tcs -> cascade Input
    params_steel.loc['Scrap','Value']=m_scrap  # t scrap/tcs -> scrap OPEX
    # Burnt Limestone is quoted per tHBI (sheet unit t/tHBI) but the cascade
    # treats stage rows as per tcs; convert using the actual HBI charge.
    params_steel.loc['Burnt Limestone','Value']=(
        params_steel.loc['Burnt Limestone','Value']*m_HBI)  # t/tcs

    E_base    = params_steel.loc['E_base','Value']        # kWh/tcs, e.g., 420 (hot charge reference)
    E_slope_per_tDRI= params_steel.loc['E_slope_per_tDRI','Value']   # kWh/tDRI, ~120–180, assuming metaliization rate of 94%
    E_coldHBI      = params_steel.loc['E_coldHBI','Value']         # kWh/tDRI penalty if receiving cold HBI, ~70–120

    # EAF-site electricity
    E_EAF_electricity = E_base + E_slope_per_tDRI*m_HBI   # kWh/tcs
    if steel_tech=='EAF-Cold':
        E_HBI_thermal_penalty= E_coldHBI*m_HBI    # kWh/tcs
    else:
        E_HBI_thermal_penalty=0
    E_EAF_total = E_EAF_electricity + E_HBI_thermal_penalty   # kWh/tcs
    params_steel.loc['Grid Electricity','Value']=E_EAF_total
    return params_steel


