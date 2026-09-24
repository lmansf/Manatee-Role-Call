# Roll Call

A daily pipeline that watches the Blue Spring manatee counts and the weather around them,
and predicts the counts. Its real subject is noticing when a source changes underneath it.

## Language

### Counting

**Roll call**:
The morning event at which manatees in the Blue Spring run are counted.
_Avoid_: survey, count (for the event itself)

**Counter**:
The party that produces a count at a roll call: the researchers (Save the Manatee Club) or the
park staff.
_Avoid_: source, observer

**Count**:
The number of manatees one counter reports at one roll call. The researchers' count is the
modelling target; the park count is a separate series, never a substitute for it.
_Avoid_: total, sighting, "the number"

**Estimate**:
A count the researchers describe as approximate rather than tallied. Still a count, still the
target, always marked as an estimate.
_Avoid_: guess, rough count

**Counter disagreement**:
The gap between the researchers' count and the park count at the same roll call. Watched over
time; never a reason to doubt either count on its own.
_Avoid_: discrepancy, error

**Late arrivals**:
Manatees reported arriving after the roll call ended. Never part of a count.
_Avoid_: additional count, new count

**River temperature**:
The St. Johns River's water temperature near the park. It has two independent readings: the
**report temperature**, taken by the researchers at roll call, and the **gauge temperature**,
recorded continuously by the USGS gauge downstream near DeLand. Distinct from the spring's own
temperature, which is constant.
_Avoid_: water temp (ambiguous with the spring), temp

### Days

**Counted**:
A day with a count from the counter in question. Zero is a valid count.

**Not counted**:
A day whose report says no roll call took place. Absence of data, not zero.
_Avoid_: zero day, missing

**Unreported**:
A day with no report at all, such as most weekends.
_Avoid_: missing, gap (without saying which kind)

**Season**:
The span during which roll calls are expected. It opens at the first report of the winter and
closes after a run of silent weekdays once March has begun.

**Latest plausible start**:
The date after which a season that has not yet opened is treated as a failure to hear from the
source, not a late winter.

### Predicting

**Forecast**:
Open-Meteo's prediction of future weather. Only ever weather.
_Avoid_: using it for the model's output

**Prediction**:
The model's estimate of the researchers' count for the next calendar day. Made every day of an
open season; scored only on days that turn out counted.
_Avoid_: forecast, projection

**Persistence**:
The prediction that the next count equals the last count. The bar every prediction must beat;
losing to it over recent scored predictions is what triggers a retrain.
_Avoid_: naive model, baseline (baseline means something else here)

### Watching

**Baseline**:
The fixed reference a check compares against. Replaced once a year when the season closes, with
the old and new versions kept side by side. For weather, it is the normal for each day of the
year over the trailing 30 years.
_Avoid_: norm, rolling average

**Quarantined observation**:
A single observation held out of training because a check doubted it, until a person clears it.
_Avoid_: bad row, dropped row

**Clearing**:
A person's recorded decision on a quarantined observation: confirmed real, or rejected, with a
reason.

**Incident**:
A failed source-level check, such as a stale or empty source. It has no rows to hold back; it is
alerted, logged, and closes when the check passes again.
_Avoid_: alert (the alert is the notification, not the thing)
