# Tourism Monthly source archive

`tourism.zip` is the unmodified `tourism_monthly_dataset.zip` (199,791 bytes), renamed locally.
It is included so the panel forecasting example can run during source-host outages. The loader
checks the SHA-256 recorded in `../sources.json`; observations, series and evaluation are unchanged.

- Dataset: [Tourism Monthly Dataset, version 3](https://doi.org/10.5281/zenodo.4656096).
- Archive curators: Rakshitha Godahewa, Christoph Bergmeir, Geoffrey I. Webb, Rob J. Hyndman,
  and Pablo Montero-Manso, *Monash Time Series Forecasting Archive* (2021).
- Original data: Athanasopoulos, G., Hyndman, R. J., Song, H., and Wu, D. C. (2011),
  [The tourism forecasting competition](https://doi.org/10.1016/j.ijforecast.2010.04.009),
  *International Journal of Forecasting*, 27(3), 822–844.
- License: [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/),
  as identified in [Monash's dataset metadata](https://huggingface.co/datasets/Monash-University/monash_tsf).
- No data modifications. The source archive retains its original attribution header. The runtime
  converts TSF observations to a long-form table without changing values; this example's 12-month
  evaluation horizon is documented in the main README.
