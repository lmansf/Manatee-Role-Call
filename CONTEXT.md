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

**Late arrivals**:
Manatees reported arriving after the roll call ended. Never part of a count.
_Avoid_: additional count, new count

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

### Watching

**Baseline**:
The fixed reference a check compares against. Replaced once a year when the season closes, with
the old and new versions kept side by side.
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
