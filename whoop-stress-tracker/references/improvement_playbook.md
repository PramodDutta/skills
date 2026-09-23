# Improvement playbook

One change at a time, 14 days, then re-measure with `--since-change`. Changing three
things at once feels productive but teaches you nothing about what worked.

## Match the pattern to the lever

| Pattern in the report | First thing to try | Why it should help | Measure |
|---|---|---|---|
| Recurring window (e.g. weekdays 10:00-11:30) | 5 minutes of cyclic sighing, 10 minutes before the window | Daily 5-minute cyclic sighing improved mood and lowered resting breathing rate more than mindfulness meditation (Balban et al., *Cell Reports Medicine*, 2023) | window score; days it spikes |
| Window sits on a specific meeting | Agenda the day before; cut it to the decisions; 5-10 minute buffer before; move it or make it async | Microsoft's Human Factors Lab (EEG study, 2021): stress builds across back-to-back meetings; 10-minute breaks reset it | score during that meeting, next 4 occurrences |
| Stress tracks meeting load (correlation ≥ 0.3) | Batch meetings into two blocks; one protected no-meeting half-day; 25/50-minute defaults | same | high-stress minutes per day |
| Morning window (before 11:00) | Look at what precedes it: commute, the first inbox/Slack sweep, coffee on an empty stomach. Try holding the inbox until after the first focus block | - | window score |
| Afternoon window (13:00-17:00) | 10-minute walk right after lunch; caffeine only before 14:00 | Caffeine's half-life is about 5 hours | window score |
| Evening window, late night-time low, or later bedtime → higher stress | Shutdown ritual 30 minutes before the window; last meal ≥ 3 hours before bed; alcohol-free weeknights; hard training earlier in the day | Alcohol raises heart rate and suppresses HRV in the first hours of sleep, dose-dependently (Pietilä et al., *JMIR Mental Health*, 2018) | when the night's low comes; first-hour heart rate |
| Heavy load day (e.g. mornings after Mondays) | Move one recurring meeting off that day; protect 60 minutes; plan an easy evening | - | HRV the next morning |
| Journal habit with t ≥ 2 | Two weeks without it | - | index on those mornings |
| Low sleep performance or sleep debt | Go to bed 30 minutes earlier on weeknights; keep wake time fixed | - | sleep performance; recovery |

## In the moment (live monitor)

When an alert fires and you're sitting still, do one minute of cyclic sighing right
away. The monitor checks heart rate and HRV 3 minutes later, and the session summary
averages those checks. That's your personal evidence for whether the technique works
for you, alert by alert. If it keeps not working at a certain time of day, that
window needs a structural fix (a meeting moved, a buffer, a walk), not more breathing.

## Cyclic sighing, exactly

Breathe in through the nose until the lungs are nearly full, then take a second short
sip of air to top them up. Breathe out slowly through the mouth until empty. Repeat
for 5 minutes. When there's no time, 1-3 of these breaths still help.

## The 14-day experiment

1. Pick one item and write it as: "When <time or trigger>, I will <action> for
   <duration>." Example: "At 09:50 on weekdays I do 5 minutes of cyclic sighing."
2. Day 1 is the start date. Offer a daily reminder event on the calendar (ask first).
3. Keep wearing WHOOP and keep everything else as normal as possible.
4. Day 15: re-run the analysis with `--since-change <day 1>` and rebuild the report.
5. Decide: keep it if the window's score dropped by ≥ 0.3, or it spiked on fewer than
   half of the days. Otherwise drop it and try the next item.

## Reading a before/after

- A window's mean moves by roughly ±0.2-0.3 from week to week anyway. Look for more
  than that before calling it a win.
- HRV responds slowly; judge daily metrics over 2-4 weeks, not one.
- Life changes (travel, illness, a deadline week) can swamp the effect. Note them and
  extend the test rather than conclude.

## References

- Balban MY, Neri E, Kogon MM, et al. Brief structured respiration practices enhance
  mood and reduce physiological arousal. *Cell Rep Med.* 2023;4(1):100895.
- Microsoft WorkLab. Research proves your brain needs breaks. April 2021.
- Pietilä J, Helander E, Korhonen I, et al. Acute effect of alcohol intake on
  cardiovascular autonomic regulation during the first hours of sleep in a large
  real-world sample of Finnish employees. *JMIR Ment Health.* 2018;5(1):e23.
