
import pandas as pd
import numpy as np
import os

# Custom rounding: 4 digits for <1, 2 digits otherwise
def smart_round(x):
    return round(x, 4) if x < 1 else round(x, 2)

def forecast_commodity_prices(price_ref: pd.DataFrame,
                            price_growth: pd.DataFrame,
                            ref_year:int, rounder=None) -> pd.DataFrame:
    """
    Generate commodity price forecasts using compound annual growth rates.

    Parameters:
    - price_ref (pd.DataFrame): Reference commodity prices with index as 'Item' and columns as regions.
    - price_growth (pd.DataFrame): Annual growth rates; same structure as price_ref.
    - ref_year (int): The reference year for the initial prices.
    Returns:
    - pd.DataFrame: A DataFrame in long format with columns ['Region', 'Year', 'Item', 'Price'].
    """
    # Create price forecasts using compound annual growth
    price_ref = price_ref.loc[price_growth.index]
    price_forecast = {ref_year: price_ref}


    for year in range(ref_year + 1, ref_year + 50):
        price_forecast[year] = price_forecast[year - 1] * (1 + price_growth)
    # Convert to long format DataFrame ['Item','Region','Year','Price']
    price_forecast = pd.concat(price_forecast, names=['Year']).reset_index()
    price_forecast = price_forecast.melt(
        id_vars=['Year', 'Item'],
        var_name='Region',
        value_name='Price'
    )

    # # Clean and format
    price_forecast['Year'] = price_forecast['Year'].astype(int)
    #convert np.int64 to int
    price_forecast['Year'] = price_forecast['Year'].apply(int)
    price_forecast['Price'] = price_forecast['Price'].apply(smart_round)

    # 4) Pivot: rows=(Region, Year), cols=Item
    price_forecast = (
        price_forecast
        .reset_index()
        .pivot_table(index=['Region', 'Year'], columns='Item', values='Price')
    )
    return price_forecast


def calculate_wacc(
    finance_params
):
    """
    Calculate nominal and (optionally) real WACC.

    Parameters:
    - debt_ratio: float (e.g., 0.6 for 60%)
    - cost_of_debt: float (e.g., 0.05 for 5%)
    - equity_ratio: float (e.g., 0.4 for 40%)
    - cost_of_equity: float (e.g., 0.1 for 10%)
    - tax_rate: float (e.g., 0.25 for 25%)
    - inflation_rate: float or None (e.g., 0.02 for 2%). If None, real WACC not calculated.

    Returns:
    - Dictionary with nominal WACC and (optionally) real WACC
    """
    # Nominal WACC
    finance_params['Equity ratio']=finance_params['Equity ratio'] if 'Equity ratio' in finance_params else 1 - finance_params['Debt ratio']
        
    wacc_nominal = (
        finance_params['Debt ratio'] * finance_params['Cost of debt'] * (1 - finance_params['Tax rate']) +
        finance_params['Cost of equity'] * finance_params['Equity ratio']
    )

    result = {'wacc_nominal': wacc_nominal}

    if finance_params['Inflation rate'] is not None:
        # Fisher equation for real WACC
        wacc_real = (1 + wacc_nominal) / (1 + finance_params['Inflation rate']) - 1
        result['wacc_real'] = wacc_real

    return result


def CRF(WACC, Lifetime):
    return (WACC * (1 + WACC)**Lifetime) / ((1 + WACC)**Lifetime - 1)


def capacity_ratio(t, k, midpoint):
    return 1 / (1 + np.exp(-k * (t - midpoint)))


def capex_forecast(capex_ref,lr_parameters,ref_year=2024, lr_multipliers=None):
    df = []
    years = np.arange(ref_year, ref_year+50)

    lr_parameters = lr_parameters.reset_index().set_index(['Technology','Region'])
    capex_ref = capex_ref.reset_index().set_index(['Technology','Region'])
    filled_lr = lr_parameters.copy()
    # Loop over all (Tech, Region) pairs in capex_ref
    for (tech, region) in capex_ref.index:
        if (tech, region) not in lr_parameters.index:
            # If missing, but Global exists → use Global row
            if (tech, 'Global') in lr_parameters.index:
                filled_lr.loc[(tech, region),:] = lr_parameters.loc[(tech, 'Global'),:]
            else:
                raise ValueError(f"No LR parameters found for {(tech, region)} or Global")

    capex_lr=pd.merge(filled_lr,capex_ref,left_index=True,right_index=True,how='inner').reset_index()
    for ir,row in capex_lr.iterrows():
        tech = row['Technology']
        try:
            lr_multiplier = lr_multipliers[tech]
        except:
            lr_multiplier=1

        alpha = -np.log2(1 - row['LR'] * lr_multiplier)
        k = 2 * np.log(19) / row['GT']

        # Define year range
        # Logistic function for cumulative capacity ratio
        # Compute capacity ratio and cumulative capacity
        f_t = capacity_ratio(years, k, row['MP'])
        N_t = f_t * row['SP']

        # Compute reference cumulative capacity at year 2020
        f_ref = capacity_ratio(ref_year, k, row['MP'])
        N_ref = f_ref * row['SP']

        # Compute forecasted CAPEX using Wright's law
        capex_forecast_tmp = row['CAPEX'] * (N_t / N_ref) ** (-alpha)
        capex_forecast_tmp = pd.DataFrame(capex_forecast_tmp.round(2), index=years, columns=['CAPEX'])
        capex_forecast_tmp['Technology'] = row['Technology']
        capex_forecast_tmp['Region'] = row['Region']
        capex_forecast_tmp = capex_forecast_tmp.reset_index()
        capex_forecast_tmp.rename(columns={'index': 'Year'}, inplace=True)
        df.append(capex_forecast_tmp)
    df = pd.concat(df)
    return df

def load_renewable_data(renewable_data_folder, lat, lon, correction_factors={'Wind':1, 'Solar':1}):
    small_names={'Wind':'wind','Solar':'pv'}
    CF = {}
    for var in ['Wind','Solar']:
        cf_data=pd.read_csv(
        f"{renewable_data_folder}/ninja_{small_names[var]}_%.4f_%.4f_uncorrected.csv" % (lat, lon), skiprows=3)
        cf = cf_data['electricity'].values* correction_factors[var]  # Apply correction factor for wind
        CF[var] = cf
    return CF