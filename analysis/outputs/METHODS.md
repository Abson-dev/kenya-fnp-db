# Methods note (draft)

Author: Aboubacar HEMA

## Spatial unit and linkage

All indicators are resolved to Kenya's 47 counties using a master county and sub-county crosswalk derived from the OCHA Common Operational Dataset administrative boundaries. Thematic layers are appended to this crosswalk rather than merged pairwise, preserving a single spatial key. Market price observations, which the World Food Programme tags by former province, are assigned to counties by point-in-polygon on market coordinates.

## Soil

County soil properties are zonal means of ISRIC SoilGrids 250 m coverages. The 0-5, 5-15 and 15-30 cm layers are combined into a thickness-weighted 0-30 cm topsoil value and converted from SoilGrids mapped units to conventional units (for example pH, organic carbon in g/kg, cation exchange capacity in cmol(c)/kg).

## Prices

Retail staple prices from the World Food Programme are normalised to a per-kilogram basis, then summarised per county as the median price level and the coefficient of variation as a volatility measure.

## Typology

Counties are grouped into soil-health zones by k-means on standardised topsoil properties, with the number of zones (k=3) selected by the silhouette criterion.

## Policy layer

The Food Systems and Land Use Action Plan 2024-2030 contributes national policy context and a small county signal. The 7-year budget is summarised by critical transition, and the agricultural growth series is retained as context. The Plan also names four counties with a stunting figure (Kilifi, West Pokot, Samburu, Kisumu); these are joined to the county table and placed beside the soil and price profile as an external, illustrative check. With four counties this is not an inferential analysis and is not used as a model input.

## Planned extension

County anthropometry and anaemia from the 2022 Kenya Demographic and Health Survey will be appended to the county table to estimate the soil to nutrition pathway, with the four Action Plan county figures serving as an external check on the survey aggregates. Current soil-price associations are descriptive: soil quality, market access and prices are jointly determined, so they are reported with robust standard errors and not interpreted causally.
