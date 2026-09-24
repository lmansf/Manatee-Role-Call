"""The prediction model: tomorrow's researchers' count at Blue Spring (spec section 4).

- `features` turns the stored counts, gauge temperature and air weather into one feature row per
  prediction day.
- `train` builds the training set, fits the count regression and decides when to retrain.
- `predict` makes and stores the daily prediction.
- `score` compares scored predictions with persistence.
- `store` owns the model's tables.
"""
