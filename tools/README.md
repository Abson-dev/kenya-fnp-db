# Data download tools

Helper scripts for the external datasets that cannot be fetched by the main
pipeline because they need a browser, an account, or a large clip step. Each one
writes into the folders the pipeline already reads, so after running them you
just rebuild the affected layer.

## Remote sensing: CHIRPS rainfall and MODIS NDVI

Recommended (both products, one script, Earth Engine):

    pip install earthengine-api requests
    earthengine authenticate
    python tools/download_remote_sensing_gee.py --project YOUR_EE_PROJECT --start 2010 --end 2024

This writes data/raw/chirps_rainfall/chirps_YYYY.tif and
data/raw/modis_ndvi/ndvi_YYYY.tif, clipped to Kenya.

Rainfall only, without Earth Engine:

    pip install requests rasterio
    python tools/download_chirps_direct.py --start 2010 --end 2024

Then build the layer:

    python run_all.py --layer geography
    python analyze.py

## Soil micronutrients, full coverage: iSDAsoil (recommended)

    pip install earthengine-api requests
    earthengine authenticate
    python tools/download_isda_gee.py --project YOUR_EE_PROJECT
    # every property:  python tools/download_isda_gee.py --project YOUR_EE_PROJECT --props all

This writes data/raw/isda/isda_<property>.tif for extractable phosphorus,
potassium, zinc and iron (back-transformed to natural units, 0-30 cm), giving
all 47 counties. Then:

    python run_all.py --layer soil
    python check_soil.py
    python analyze.py

## Soil micronutrients: AfSIS

    pip install requests
    python tools/download_afsis.py --list      # inventory the whole bucket
    python tools/download_afsis.py             # download all tabular files

Then:

    python run_all.py --layer soil
    python check_soil.py

Note on AfSIS coverage. AfSIS Phase I is a sentinel-site survey, so even the
complete download is clustered in a few counties. For extractable phosphorus,
potassium, zinc and iron across all 47 counties, a gridded product such as
iSDAsoil is the appropriate source, with AfSIS kept as a sparse validation set.
