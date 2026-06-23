# Methods note (draft)

Author: Aboubacar HEMA

## Spatial unit and linkage

All indicators are resolved to Kenya's 47 counties using a master county and sub-county crosswalk derived from the OCHA Common Operational Dataset administrative boundaries. Thematic layers are appended to this crosswalk rather than merged pairwise, preserving a single spatial key. Market price observations, which the World Food Programme tags by former province, are assigned to counties by point-in-polygon on market coordinates.

## Soil

County soil properties are zonal means of ISRIC SoilGrids 250 m coverages. The 0-5, 5-15 and 15-30 cm layers are combined into a thickness-weighted 0-30 cm topsoil value and converted from SoilGrids mapped units to conventional units (for example pH, organic carbon in g/kg, cation exchange capacity in cmol(c)/kg).

## Prices

Retail staple prices from the World Food Programme are normalised to a per-kilogram basis, then summarised per county as the median price level and the coefficient of variation as a volatility measure.

## Population and agriculture denominators

County population, households, average household size, land area and density come from the 2019 Kenya Population and Housing Census. Agricultural land (hectares) and farming households, split by subsistence and commercial purpose, come from the census agriculture tables. These denominators support per-capita and land-use-intensity indicators: agricultural land as a share of county area, farming households as a share of all households, and cropland per farming household.

## Crop area, production and yields

Crop area and production by county and year (2019-2023) are extracted from the KNBS National Agriculture Production Report 2024. For each county the latest year gives total cropped area, total production and the number of crops reported; maize is retained separately as the staple. County maize yield is production divided by area (t/ha), and maize production per capita uses the census population. Crop area, production and yield are also reported nationally by crop and year.

## Typology

Counties are grouped into soil-health zones by k-means on standardised topsoil properties, with the number of zones (k=3) selected by the silhouette criterion.

## Policy layer

The Food Systems and Land Use Action Plan 2024-2030 contributes national policy context and a small county signal. The 7-year budget is summarised by critical transition, and the agricultural growth series is retained as context. The Plan also names four counties with a stunting figure (Kilifi, West Pokot, Samburu, Kisumu); these are joined to the county table and placed beside the soil and price profile as an external, illustrative check. With four counties this is not an inferential analysis and is not used as a model input.

## Health and nutrition outcomes

County child anthropometry (stunting, wasting, underweight) comes from the 2022 Kenya Demographic and Health Survey. Prevalence is computed per county from the children (KR) recode, sample-weighted by v005/1e6, with z-score flags excluded (|z| beyond 6 SD). The county variable is detected by matching its value labels to the crosswalk. The 2022 KDHS biomarker questionnaire collected height and weight only; unlike the 2014 round it carried no haemoglobin module, so anaemia is not available from this survey and is omitted rather than reported empty. Child stunting is the outcome for the soil-to-nutrition pathway: it is regressed on topsoil quality controlling for the staple price and maize yield. The four county stunting figures the Action Plan names (Kilifi, West Pokot, Samburu, Kisumu) are compared against the survey estimates as an external validation; Kilifi at about 37 percent matches the Action Plan figure closely.

## Causal caution

All county associations reported here are descriptive. Soil quality, climate, market access, diets and care practices are jointly determined, so the soil-price, soil-yield and soil-nutrition models are read as conditional gradients with robust (HC3) standard errors, not as causal effects.
