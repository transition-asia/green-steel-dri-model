# Steelmaking process parameters: derivation and literature ranges

This note explains where the process coefficients in `india/data/Model_input_India.xlsx` come from and how they sit against the literature. The values the model actually uses are those in the workbook, which is authoritative where the two differ.

## 1. The three direct-reduction reactors

The model carries three direct-reduction technologies, each with a 100% natural-gas pole and a 100% hydrogen pole; intermediate blends (30% and 70% hydrogen) are interpolated along the reductant curve in `india/data/H2-NG.csv`.

| Tag | Technology | Reactor | Ore feed | Operating temperature | Metallisation in the model | Maturity | Reference plants |
|---|---|---|---|---|---|---|---|
| DRI-SF | Shaft furnace | Vertical moving bed, counter-current gas | DR-grade pellet (Fe 67%) | 900–950 °C | 95% | Commercial on gas; hydrogen at pilot scale, first commercial units under construction | Midrex and Energiron plants; HYBRIT |
| DRI-FB | Fluidised bed | Multi-stage fluidised cascade, counter-current | Ore fines (Fe 58%) | about 400 / 650 / 700 °C by stage | 93% | Pilot | Circored, HYFOR (Primetals), HyREX (POSCO) |
| DRI-RK | Rotary kiln (gas or hydrogen) | Inclined rotating tube, counter-flow | Fines or lump | 600–800 °C on hydrogen | 90% | Early commercial; India's kiln fleet is coal-fired | HyIron Oshivela (Namibia); Indian coal-based kilns |

Hydrogen reduces faster than the CO-rich syngas made from natural gas, so operating temperature falls from gas to hydrogen in the fluidised bed and the kiln; the shaft furnace keeps a similar temperature because its design needs hot gas at the inlet. There is no commercial natural-gas rotary kiln at scale, so the kiln's gas-case coefficients are extrapolated from its hydrogen case and from coal-based practice.

## 2. Material balance

### 2.1 Reductant

Hydrogen demand is anchored to the oxygen removed from the ore rather than to the mass of DRI. The stoichiometric requirement is 0.126 kg of hydrogen per kg of oxygen (M(H₂)/M(O); Fe₂O₃ + 3H₂ → 2Fe + 3H₂O, about 54 kg H₂ per tonne of iron), and plants need an allowance above that for top-gas losses, purge and incomplete gas utilisation. The shaft furnace is set at 58 kg/tDRI, which implies an allowance of about 18% over stoichiometry at 95% metallisation. The fines routes apply the same allowance to the oxygen their lower-grade DRI actually gives up (less oxygen per tonne of DRI, because a fifth of the DRI is gangue), which gives 49.1 kg/tDRI for the fluidised bed and 47.7 for the rotary kiln, inside the 49–51 kg/tDRI range given in the industry-expert review. One consistent process allowance across the three reactors is the point of this construction; a flat 58 kg/tDRI for all three would have implied allowances of 17%, 38% and 34%. Because the fines routes make more DRI per tonne of steel, their hydrogen per tonne of crude steel is higher than the shaft furnace's.

| Source | Shaft furnace | Fluidised bed | Rotary kiln |
|---|---|---|---|
| Vogl et al. 2018 | 51 | | |
| Bhaskar et al. 2020 | 54 | | |
| Patisson and Mirgaux 2020 | 50–55 | 55–65 | |
| HYBRIT pilot | 58–65 | | |
| Otto et al. 2017 | 52 | | |
| IEA Iron and Steel Roadmap 2020 | 50–60 | 55–65 | |
| Spreitzer et al. 2022 (HYFOR) | | 49–54 | |
| POSCO HyREX 2024 | | 58–62 | |
| Rahbari et al. 2025 | | about 57 | |
| Kim et al. 2026 | | 55–65 | |
| HyIron 2024 | | | 60–65 |
| Industry expert consultation | | 49–50 | |
| **Model, kg H₂ per tDRI** | **58.0** | **49.1** | **47.7** |
| **Model, kg H₂ per tonne of crude steel** | **53.7** | **60.8** | **59.4** |

Natural gas covers both reduction and process heat. The shaft furnace is set at 9.5 MMBtu/tDRI (about 10 GJ), in line with Midrex and Energiron practice on high-grade pellet. The fines routes scale the reduction duty with the oxygen removed, add a 10% gas-utilisation penalty for the less favourable gas-solid contact, and add their heating duty at 80% burner efficiency: 9.3 MMBtu/tDRI for the fluidised bed and 8.8 for the rotary kiln. Ore drying in the fines routes uses smelter and converter waste heat and is not charged.

| Source | Shaft furnace | Fluidised bed | Rotary kiln |
|---|---|---|---|
| Midrex | 9.8–10.4 GJ | | |
| Energiron | 9.5–10.5 GJ | | |
| HYFOR, FINMET | | 10–11 GJ | |
| Indian coal-based kilns | | | 14–18 GJ (coal) |
| **Model, MMBtu per tDRI** | **9.5** | **9.3** | **8.8** |

### 2.2 Ore

Ore per tonne of DRI follows from the iron balance: the iron content of the feed, the metallisation reached, and the gangue carried through. The shaft furnace charges 1.414 t of DR-grade pellet per tonne of DRI. The fines routes charge wet fines (12% moisture) at 1.611 (fluidised bed, 93% metallisation) and 1.600 (rotary kiln, 90%) t/tDRI. Per tonne of crude steel this is 1.31 t of pellet for the shaft furnace and 1.99 t of fines for either fines route, because the smelter routes need about a third more DRI per tonne of steel (1.24 t against 0.93 t for the shaft furnace, whose EAF also takes 15% scrap).

Domestic fines and pellet are priced per wet (as-received) tonne, the convention of NMDC price notifications and IBM/CEIC average sale prices, while Fe grades are quoted on a dry basis; the ore intensities in the model are per as-received tonne to match. The shaft furnace's DR-grade pellet is priced at the domestic BF-grade pellet series plus a premium of $12.6/t (10%), a conservative estimate of in-house pellet made from high-grade Bailadila-type ore rather than a market price: published premiums for DR-grade over BF-grade pellet are about $30/t domestically and $45–60/t on the seaborne market, and the shaft-furnace versus rotary-kiln ranking is sensitive to this input (Section 4.2 of the report).

Cooling water is 1.25 t/tDRI on every route (literature range 0.5–1.5; HYBRIT environmental statement, Patisson and Mirgaux 2020).

## 3. Electricity at the reduction stage

The DRI stage's electricity is the sum of three terms in the parameter sheets: `E_heating` (electric heating of the reducing gas and, for the fines routes, of the ore), `E_aux` (ore handling, off-gas cleaning, fans, pumps and controls, independent of the reductant) and `E_H2_recirc` (recompression of recycled hydrogen at 0.4 kWh per kg).

On natural gas the heating duty is met by combustion, so only the auxiliaries draw electricity. On hydrogen the reduction is endothermic (about 100 kJ/mol) and the heat is electric. The stage totals at 100% hydrogen follow industry-expert guidance for each reactor type; `E_heating` is the remainder after the auxiliaries and recirculation.

| kWh per tDRI | Shaft furnace | Fluidised bed | Rotary kiln |
|---|---|---|---|
| E_aux | 100 | 140 | 200 |
| E_H2_recirc (0.4 kWh/kg H₂) | 23.2 | 19.6 | 19.1 |
| E_heating at 100% hydrogen | 576.8 | 640.4 | 430.9 |
| **Total at 100% hydrogen** | **700** | **800** | **650** |
| **Total at 100% natural gas** | **100** | **140** | **200** |

Literature for the auxiliaries: shaft furnace 65–110 kWh/tDRI (Vogl et al. 2018; Bhaskar et al. 2020; Midrex); fluidised bed 120–160 (Primetals, HYFOR); rotary kiln about 200 (pilot data). For hydrogen heating the literature spans 3–6 kWh per kg of hydrogen for the shaft furnace depending on heat recovery (Vogl et al. 2018; Bhaskar et al. 2020; HYBRIT); the fluidised bed's industrial flowsheet (Spreitzer et al. 2022) recovers off-gas heat and preheats ore with bleed gas, which is why its total sits close to the shaft furnace's rather than far above it. The kiln has less heat recovery and a larger shell loss, but at 600–800 °C it also needs less heat per tonne; the 650 kWh/tDRI total reflects pilot experience.

### 3.1 Operating flexibility in the hourly dispatch

The hourly model lets each unit move within a turndown range and a ramp limit while every stage averages 85% of nameplate over the year (9.6 Mtpa of nameplate makes 8.16 Mtpa of crude steel). Headroom above 85% lets a reactor concentrate output in hours of cheap renewable generation; the floor and the ramp set how far it can back off. Following the industry-expert review, no reactor carries an availability ceiling below 100%: the reactors differ in turndown, not in a cap.

| Unit | Operating range, % of nameplate | Ramp, % of nameplate per hour |
|---|---|---|
| Shaft furnace | 50–100 | 5 |
| Fluidised bed | 70–100 | 10 |
| Rotary kiln | 40–100 | 20 |
| Electric smelting furnace | 80–100 | 5 |
| Electric arc furnace, basic oxygen furnace | fully flexible | |
| Electrolyser | 10–100 | 50 |

The smelter's 80% floor is a modelling compromise: the expert view is that it runs at 90–100% when running, essentially as baseload, but the model has no planned-outage concept and solves to an 85% annual capacity factor, so an hourly floor above 85% would be infeasible.

## 4. Downstream stages

### 4.1 Electrolyser

Alkaline electrolysis at 51.2 kWh per kg of hydrogen (33.3 kWh/kg lower heating value at 65% system efficiency), 31.5 kg of water per kg of hydrogen including the purification reject (the stoichiometric requirement is 9 kg), and 2 kWh/kg for compression into storage. Capital cost, life and operating limits are in the `Tech` sheet of the workbook.

### 4.2 Electric arc furnace (shaft-furnace route)

| Parameter | Model | Literature and basis |
|---|---|---|
| Electricity at a pure-scrap charge | 600 kWh/tcs | LBNL and published EAF energy balances |
| Extra electricity per tonne of DRI charged | 80 kWh/tDRI | 785 kWh/t for pure DRI against 699 for pure scrap, cold-charged; Vogl et al. 2018 about 80 |
| Cold-HBI penalty | 0 | Hot charging: the DRI plant and the furnace are co-located |
| Scrap share of the metallic charge | 15% (Route sheet) | 10–30% for an Indian plant; scrap is short in India |
| Iron yield | Residual DRI oxide and a slag at 20% FeO | Burnt lime set by a slag basicity (CaO:SiO₂) of 2.0 |
| Oxygen | 50 Nm³/tcs | 30–60 |
| Graphite electrodes | 0.002 t/tcs | 0.0015–0.0025 |
| Refractories, ferroalloys | 0.002, 0.01 t/tcs | Typical values |

### 4.3 Electric smelting furnace (fines routes and the BF-grade-pellet variant)

DRI from low-grade fines carries about a fifth gangue, too much for an arc furnace, so the fines routes melt it in an electric smelting furnace (ESF) that runs at low basicity (CaO:SiO₂ of 1.1), takes the gangue off as slag and delivers hot metal to a basic oxygen furnace. The hot metal is specified at 96% Fe, 3.5% C and 0.4% Si (Hatch 2026). The smelter is modelled with no iron loss to its slag: all iron reports to hot metal, and the yield loss appears only in the converter's 20% FeO slag, which slightly flatters the fines routes' yield.

| Parameter | Model | Basis |
|---|---|---|
| Smelting electricity | 450 kWh/tHM (fluidised bed), 472 (rotary kiln), 436 (shaft furnace on BF-grade pellet), plus a fixed 50 kWh/tHM | 398 kWh/tHM for melting, superheat and losses, plus the endothermic reduction of the residual FeO each route delivers (132 kJ/mol at 85% electrical efficiency). Literature 400–650 for low-grade feed (Hatch; Primetals) |
| DRI per tonne of hot metal | 1.182 (fluidised bed), 1.190 (rotary kiln), 1.153 (BF-grade pellet) | Iron balance of each route's DRI |
| Carburising carbon | 0.0495 t/tHM (fluidised bed), 0.0557 (rotary kiln) | The industry-expert review put the carburiser charge at about 48.5 kg per tonne of hot metal; the model builds it as 35 kg retained in the hot metal at 3.5% C plus the carbon that reduces each route's residual FeO (14.5 kg for the fluidised bed, 20.7 for the rotary kiln). Fossil carbon, fully oxidised and counted as Scope 1 |
| Burnt limestone | 0.084 t/tHM on the fines routes | Slag balance at basicity 1.1 for the gangue load of each feed |
| Graphite electrodes | 0.0013 t/tHM (rotary kiln), 0.00125 (fluidised bed) | 2.5 kg per MWh of furnace electricity (industry guidance 2–3 kg/MWh) |
| Oxygen | 25 Nm³/tHM | 20–30; the air-separation unit is shared with the BOF |

No ESF has yet operated at full steel-plant scale; its capital cost is set at the arc furnace plus 15%, consistent with the qualitative capital-cost parity reported by Wood Mackenzie and BHP, with an uncertainty of 25–30%.

### 4.4 Basic oxygen furnace

Hot metal 1.048 t per tonne of liquid steel; burnt limestone 0.0314 t at a slag basicity of 3.5; oxygen 55 Nm³; natural gas 0.1 MMBtu for ladle reheating; electricity 130 kWh including the air-separation unit; cooling water 1.5 t; refractories 0.002 t and ferroalloys 0.01 t.

## 5. Emission factors

Emission factors live in the `Emission_Factor` sheet; each material row in the `Params_*` sheets carries its scope. The reported cradle-to-gate intensity is Scope 1 (combustion and process carbon at the plant) plus Scope 3 (embedded emissions of purchased materials) plus the Scope 2 of grid electricity actually drawn in the hourly dispatch. The CCTS and CBAM boundaries are narrower and are computed in post-processing (`run/boundary_calc.py`).

| Item | Value | Basis |
|---|---|---|
| Natural gas combustion | 0.0592 tCO₂/MMBtu | IPCC 2006, 56.1 kgCO₂/GJ on a lower-heating-value basis |
| Carburising carbon | 3.667 tCO₂/t | Full oxidation of the carbon charged (44/12) |
| Graphite electrodes | 4.45 tCO₂/t | 0.78 production (upstream) plus 3.67 in-furnace oxidation, which the boundary accounting counts as a direct emission |
| Burnt limestone | 1.473 tCO₂/t | Calcination 0.785 plus kiln fuel; high side of the 1.0–1.5 range |
| Oxygen | 0.426 kgCO₂/Nm³ | Air-separation electricity at about 0.5 kWh/Nm³ on the Indian grid |
| Refractories, ferroalloys | 3.0, 2.186 tCO₂/t | Literature mid-ranges; small quantities |
| Scrap | 0 | Recycled-content cut-off |
| Purchased hydrogen | 0 | Green hydrogen from the hub |
| Ore and pellet, upstream | Fines 0.035 (low grade) and 0.025 (high grade); pellet 0.139 tCO₂/t | Mining, and for pellet 127 kg/t of straight-grate induration plus 12 kg/t of mining (TERI) |
| Grid electricity | 0.719 kgCO₂/kWh in 2025 falling to 0.477 in 2030 | Central Electricity Authority baseline database and 2030 projection; declining thereafter (`Grid` sheet) |

## 6. References

**Peer-reviewed**

- Bhaskar, A., Assadi, M., Nikpey Somehsaraei, H. (2020). Decarbonization of the iron and steel industry with direct reduction of iron ore with green hydrogen. *Energies* 13(3):758. https://doi.org/10.3390/en13030758
- Bhardwaj, N., Seethamraju, S., Bandyopadhyay, S. (2024). Decarbonizing rotary kiln–induction furnace based sponge iron production. *Energy* 306:132516. https://doi.org/10.1016/j.energy.2024.132516
- Cavaliere, P. (2019). *Direct Reduced Iron: Most Efficient Technologies for Greenhouse Emissions Abatement.* In Clean Ironmaking and Steelmaking Processes, Springer. https://doi.org/10.1007/978-3-030-21209-4
- Kim, Y.R., Lee, J.C., Son, S.H., Yoon, S., Jeong, D.H. (2026). Simulation study, techno-economic and environmental assessment of hydrogen-based DRI production for a sustainable steel industry. *Processes* 14(4):651. https://doi.org/10.3390/pr14040651
- Otto, A., Robinius, M., Grube, T., Schiebahn, S., Praktiknjo, A., Stolten, D. (2017). Power-to-steel: reducing CO₂ through the integration of renewable energy and hydrogen. *Energies* 10(4):451. https://doi.org/10.3390/en10040451
- Patisson, F., Mirgaux, O. (2020). Hydrogen ironmaking: how it works. *Metals* 10(7):922. https://doi.org/10.3390/met10070922
- Rahbari, A., Shahabuddin, M., Sabah, S., Brooks, G., Pye, J. (2025). Production of green steel from low-grade ores: an end-to-end techno-economic assessment. *Cell Reports Sustainability* 2(1):100301. https://doi.org/10.1016/j.crsus.2024.100301
- Vogl, V., Åhman, M., Nilsson, L.J. (2018). Assessment of hydrogen direct reduction for fossil-free steelmaking. *Journal of Cleaner Production* 203:736–745. https://doi.org/10.1016/j.jclepro.2018.08.279
- Worrell, E., Blinde, P., Neelis, M., Blomen, E., Masanet, E. (2008). *Energy Efficiency Improvement and Cost Saving Opportunities for the U.S. Iron and Steel Industry.* LBNL-4779E.

**Institutional**

- Hall, W., Bhaskar, A., Görnerup, M. (2025). *Hydrogen Rotary Kilns for Ironmaking in India.* The Energy and Resources Institute (TERI), New Delhi.
- IEA (2020). *Iron and Steel Technology Roadmap.* Paris.
- IRENA (2020). *Reaching Zero with Renewables.* Abu Dhabi.
- Ministry of Steel, Government of India (2024). *Greening the Steel Sector in India: Roadmap and Action Plan.* New Delhi.
- The Energy and Resources Institute (2023). *Towards a Low-Carbon Steel Sector in India.* New Delhi.
- World Steel Association (2023). *Sustainability Indicators Report.*

**Industry**

- Ge, S., Joubarani, K., Chomyn, K., Estabrooks, M. (Hatch) (2026). *Electric Smelting Furnace-based Flowsheets for Sustainable Iron and Steel Production.* Presentation, Orlando, March 2026.
- HyIron (2024). *Project Oshivela: Green Iron from Namibia.* https://hyiron.com/oshivela/
- Midrex Technologies (2023). *World Direct Reduction Statistics.*
- POSCO (2024). *HyREX Technology Roadmap.*
- Primetals Technologies (2023). *HYFOR: Hydrogen-based Fine-Ore Reduction.*
- Spreitzer, D., Wurm, J., Hiebl, B., Rein, N., Wolfinger, T., Sterrer, W., Fleischanderl, A. (2022). HYFOR: hydrogen-based fine-ore reduction. *MHI Technical Review* 59(2).
- SSAB, LKAB, Vattenfall (2023). *HYBRIT Demonstration Project: Annual Report.*
- Tenova / Energiron (2022). *DRI Technology Bulletin.*
- Wood Mackenzie (2025). *Current EAF technology requires high-grade feedstock, limiting its potential.*
