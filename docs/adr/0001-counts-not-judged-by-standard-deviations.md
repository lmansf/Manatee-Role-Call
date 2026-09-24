# Counts are not judged by distance from a baseline

The spec flags values beyond about three standard deviations from a source's baseline. For
manatee counts that rule is backwards: a season spans 0 to 800+, and cold-snap days, the ones
the model most needs, are extreme by definition, so the rule would quarantine the signal.
Counts are instead checked for plausibility (a hard range) and consistency (a jump that
contradicts the weather). Weather fields keep the standard-deviation rule, measured against the
normal for that day of the year.

## Considered options

- Three standard deviations on counts, as specified: rejected for the reason above.
- Flag days where the model's error is large: rejected because data quality would then depend on
  the model it feeds, and the training set would be gated by its own predictions.
