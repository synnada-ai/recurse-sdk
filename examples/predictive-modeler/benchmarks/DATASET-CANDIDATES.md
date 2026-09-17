# Proposed datasets for validation and final holdout

These are metadata-researched candidates, not populated or evaluated holdout collections.
No files from these collections were downloaded or fitted during this research. Freeze the
source-family allocation, conversion, checksums, task, sampling rule and budget before evaluating
agent designs. Public dataset familiarity remains a limitation: unseen to this development
campaign does not mean unseen during language-model pretraining.

| Task | Validation candidate | Separate final-holdout candidate | Proposed contract and preparation |
| --- | --- | --- | --- |
| Binary | [Wisconsin Diagnostic Breast Cancer](https://archive.ics.uci.edu/dataset/17/breast+cancer+wisconsin+diagnostic): 569 observations, 30 measurements | [Banknote Authentication](https://archive.ics.uci.edu/dataset/267/banknote+authentication): 1,372 observations, four measurements | Add published headers, exclude diagnostic ID, explicitly name positive class. UCI metadata identifies CC BY 4.0. |
| Multiclass | [Wine](https://archive.ics.uci.edu/dataset/109/wine): 178 observations, 13 measurements, three cultivars | [Glass Identification](https://archive.ics.uci.edu/dataset/42/glass+identification): 214 observations, nine measurements, six represented classes | Add headers, remove Glass ID, preserve labels; macro F1. Check rare-class split feasibility before locking evaluation. Both UCI CC BY 4.0. |
| Multilabel | [Music Emotions](https://mulan.sourceforge.net/datasets-mlc.html): 593 tracks, 72 features, six binary labels | [Yeast multilabel](https://mulan.sourceforge.net/datasets-mlc.html): 2,417 examples, 103 features, 14 binary labels | Convert published ARFF/XML to CSV, preserve official test partition, derive validation only from training. Dataset reuse terms need verification; Mulan software licensing does not establish dataset licensing. |
| Regression | [Airfoil Self-Noise](https://archive.ics.uci.edu/dataset/291/airfoil+self+noise): 1,503 observations, five predictors | [Abalone](https://archive.ics.uci.edu/dataset/1/abalone): 4,177 observations, eight mixed predictors | Target sound pressure for Airfoil and explicitly Rings for Abalone; MAE. Distinguish interpolation across Airfoil rows from transfer to unseen experimental configurations. Both UCI CC BY 4.0. |
| Single forecast | [US Births](https://zenodo.org/records/4656049): 7,305 daily observations | [Saugeen River Flow](https://zenodo.org/records/4656058): 23,741 daily observations | TSF to date/value without aggregation; proposed 30-day horizon, history/calendar only, MAE or explicitly scaled MASE. Preserve daily timestamps and leap days. |
| Panel forecast | [Hospital](https://zenodo.org/records/4656014): 767 monthly series, 84 observations each | [FRED-MD](https://zenodo.org/records/4654833): 107 monthly series, 728 observations each | TSF to series/date/value; proposed 12-month horizon and equal-series/origin MASE. If necessary, predeclare 32 series by a fixed hash of IDs before inspecting outcomes. |

The [Monash dataset card](https://huggingface.co/datasets/Monash-University/monash_tsf)
identifies CC BY 4.0 and research-purpose use. Verify the specific archive/version metadata and
retain original-source attribution when preparing each conversion. Hospital archive metadata
was unavailable during this check; its [original source documentation](https://pkg.robjhyndman.com/expsmooth/reference/hospital.html)
describes its selection of series with mean counts at least ten and no zeros. FRED-MD contains
published differencing/log transformations; the task must predict those transformed values,
not silently claim original economic units.

## Multilabel contingency

[UCI Flags](https://archive.ics.uci.edu/dataset/40/flags) has 194 real country observations and
seven binary flag-color labels with explicit CC BY 4.0 metadata. It is a limited fallback,
not an adequate substitute for broad multilabel evaluation. Use only specified non-color
attributes; exclude main hue, corner colors and total color count as label-derived predictors.
Prefer resolving Emotions/Yeast provenance before choosing this fallback.

## Exclusions and partition integrity

- [UCI Mushroom](https://archive.ics.uci.edu/dataset/73/mushroom) describes hypothetical samples,
  so it does not meet the preference for real observations.
- [NN5 weekly](https://zenodo.org/records/4656125) describes filling missing daily values using
  same-weekday medians across the whole series before aggregation. That risks future leakage
  in a chronological benchmark, so exclude this preprocessed version.
- Use original collection identity as the family key. Mirrors, conversions, row subsets,
  task paraphrases, frequencies and different objectives from the same source stay in one
  partition. Hosting several unrelated collections on UCI or Monash does not make them one family.
- Choose separate additional development collections for tuning. Do not consume the proposed
  final collection during prompt refinement; any inspected outcomes retire it as a final holdout.
