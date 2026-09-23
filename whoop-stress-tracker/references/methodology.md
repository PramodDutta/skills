# Methodology

Every number in the report comes from one of the calculations below. All
statistics are relative to the person's own baseline, never to population norms.

## Daily stress index (WHOOP recovery data)

WHOOP measures you while you sleep, and a WHOOP cycle starts at sleep onset. So the
HRV, resting heart rate and respiratory rate reported on the morning of day D reflect
the load of day D-1 plus that night. The report calls D-1 the **load day**.

- Inputs per morning: HRV (RMSSD, ms), resting heart rate, respiratory rate.
- Baseline: the previous 28 days (at least 7 valid mornings). Early in the data it
  falls back to the surrounding ±28 days, excluding the day itself.
- Robust z-scores: median and 1.4826 × MAD. HRV is taken on the log scale because it
  is log-normally distributed. Floors keep tiny spreads from exploding: 0.04 log
  units for HRV, 1 bpm for resting heart rate, 0.3 breaths/min for respiratory rate.
- `index = (-z_HRV + z_RHR + 0.5 * z_resp) / 2.5` (divided by 2 when respiratory rate
  is missing), clamped to ±4.
- Levels: ≥ 1 high, ≥ 0.5 elevated, ≤ -0.5 calm, otherwise normal.
- **Heavy load days**: a weekday whose mornings-after average ≥ 0.4 and sit ≥ 0.4
  above the median weekday. Needs ≥ 14 scored mornings and ≥ 5 weekdays with ≥ 2 each.
- Bedtime effect: Spearman correlation between bedtime and that morning's index
  (≥ 14 nights). Bedtime variability is the robust SD of bedtimes.

## Intraday stress score (minute-level heart rate)

1. Smooth with a 5-minute centered rolling median.
2. Remove minutes that are not "awake at rest":
   - WHOOP sleeps and naps;
   - WHOOP workouts plus 20 minutes after (heart rate stays up while recovering);
   - sustained exercise-like rises (≥ +35 bpm or 4 SD above baseline for 10+ minutes,
     plus a 20-minute tail);
   - on nights with no WHOOP sleep record, the assumed sleep hours (`--night`,
     default 23-7).
3. Baseline per day: the awake minutes of that day and the 13 before it. Median and
   robust SD, with the SD floored at 3 bpm. Under 600 minutes, the whole dataset is used.
4. `score = clamp((HR - median) / SD, 0, 3)`. The bands match WHOOP's Stress Monitor
   (0-1 low, 1-2 medium, 2-3 high). WHOOP's own score also uses HRV and motion, which
   a minute heart-rate stream doesn't carry, so treat this as the heart-rate half of it.

**Stress log input** (e.g. transcribed Stress Monitor graphs): levels are used as
given, with low/medium/high mapped to 0.5/1.5/2.5.

## Stress windows

- 30-minute slots, evaluated for three kinds of group: weekdays, weekends, and each
  single weekday.
- A slot counts when it has data on ≥ 40% of that group's days (at least 2) and ≥ 30
  minutes in total.
- Hot slots: mean ≥ max(group mean + 0.2, 75th percentile of the group's slot means).
  One slightly cooler slot between two hot ones is bridged; contiguous hot slots form
  a window.
- A weekday or weekend window is kept when its mean is ≥ 0.75, at least 0.3 above the
  group mean, and it **spikes** on ≥ 25% of covered days. A spike means ≥ 10 high
  minutes, or a window mean ≥ group mean + 0.5.
- Single-weekday windows (e.g. "Sundays 20:00-21:00") need more: mean ≥ 1.0, ≥ 0.5
  above, ≥ 3 covered days and ≥ 50% recurrence. They are dropped when a weekday or
  weekend window already covers that time.
- Ranked by **weekly load** = excess over the group mean × hours × recurrence × days
  per week (5 for weekdays, 2 for weekends, 1 for a single weekday).
- Calm windows use the same logic in reverse, restricted to 08:00-20:00 so they are
  plannable.

**Episodes**: 10+ minutes at score ≥ 2, with gaps of up to 3 minutes bridged.

## Calendar

- Event score = mean score over its minutes (needs ≥ 50% of the minutes scored).
  **Lift** = event score minus that day's awake mean.
- Events are grouped by title with dates and numbers stripped. Stressful: mean lift
  > 0.15. Calming: mean lift < -0.15 with ≥ 2 occurrences.
- Each window lists the events that most often overlap it.
- Meeting-load correlation: Spearman between meetings per weekday slot and the slot's
  mean score.
- This does not separate a meeting from the people in it. For per-person effects,
  use whoops' ridge regression (`python3 -m whoops score`), which reads the same
  calendar format.

## Night-time heart rate (WHOOP sleep streams, API only)

- Per night: 10-minute rolling mean, the night's low point, and when it happens as a
  fraction of the sleep. Also the first-hour mean minus that low.
- **Late low** = the low falls in the last third of sleep. Nights are grouped by the
  evening before. A late low repeatedly after the same evening is the flag.

## Journal (WHOOP app export)

- WHOOP files each morning's answers under the cycle whose recovery they affect, so
  answer and effect share a cycle. Checked on a public 3-year export: "Have any
  alcoholic drinks?" = yes lines up with -35% HRV on the same cycle, against -9% one
  cycle later.
- Effect = mean index (yes) - mean index (no), with Welch's t. The report lists every
  question with ≥ 5 answers each way. A recommendation needs t ≥ 2, ≥ 8 "yes" answers
  and an effect ≥ 0.3.

## Before / after (`--since-change`)

- Daily: geometric-mean HRV, mean resting heart rate and mean recovery, before vs after.
- Intraday: both periods are re-scored against the **before** period's baseline. The
  normal 14-day moving baseline would absorb an improvement and hide it.
- A window's week-to-week noise is roughly ±0.2-0.3. Treat smaller changes as noise.

## Validation

- **Synthetic demo** (`scripts/demo_data.py`, 28 days, planted patterns). Recovers:
  - the weekday 10:00-11:30 window (19 of 20 weekdays) and the 16:30-18:00 window;
  - the Sunday 20:00-21:00 window;
  - Monday and Friday as carry-over days;
  - the planted alcohol journal effect (t ≈ 8), with the unplanted "screens" question
    null (t ≈ -0.6);
  - late night-time lows after Monday evenings.
  Workouts are excluded, not read as stress, and weekend noise produces no false windows.
- **whoops' synthetic data** (its heart-rate cache and calendar, read unchanged): the
  most stressful events are the meetings with whoops' planted top stressor
  (`pm_growth`), and the calming ones are those with its planted calming person
  (`senior_dev`).
- **Real 3-year WHOOP export** (public dataset, 1,027 days): Friday and Saturday
  nights come out heavy (HRV -24% and -11% the next morning), and alcohol is the
  dominant journal factor (t ≈ 15).

## Limits

- Heart rate also responds to movement, posture, caffeine, heat, digestion and
  illness. Without motion data, a commute, a walk or stairs look like stress.
- Everything here is correlational.
- Small samples are noisy. Hour-level windows need about 2 weeks of intraday data,
  and weekday patterns about 3 weeks of mornings.
- WHOOP's API and export evolve. Parsers match headers loosely, but check the
  "Data and method" section of the report after a WHOOP app update.
