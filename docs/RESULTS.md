# Results

All numbers were measured on 2 November 2026 on one 2-core Linux machine.

## Benchmark: does prediction help?

**Method.** Each trial starts a fresh system, warms up for 15 seconds, injects one failure, then counts what the synthetic users experienced over 100 seconds at 18 requests a second. There are 7 failures, 3 setups and 2 rounds, so 42 trials in total. Run it with `python benchmarks/run_benchmark.py --repeats 2`.

| Setup | Requests succeeded | Failed requests | Seconds with over 5% errors | Instances that actually failed |
|---|---:|---:|---:|---:|
| No protection | 66.53% | 8,423 | 801 | 14 of 14 |
| Recover after failure | 97.39% | 642 | 115 | 14 of 14 |
| Predict and recover | 98.92% | 265 | 53 | 4 of 14 |

Failed requests by failure, totalled across both rounds:

| Failure | No protection | Recover after failure | Predict and recover |
|---|---:|---:|---:|
| Memory leak, Payment | 209 | 6 | 0 |
| Connection pool leak, Orders | 128 | 22 | 0 |
| Latency rise, Inventory | 1,179 | 27 | 0 |
| Rising errors, Auth | 1,847 | 469 | 130 |
| CPU overload, Catalog | 1 | 4 | 0 |
| Sudden crash, Catalog | 1,520 | 8 | 0 |
| Sudden freeze, Inventory | 3,539 | 106 | 135 |
| **Total** | **8,423** | **642** | **265** |

**What this shows**
- All 10 trials of failures that build up were replaced before the instance broke.
- Restarting after failure already removes most of the damage. Prediction removes most of what remains.
- Prediction didn't help with a sudden freeze: 135 failed requests against 106 without it. Treat these as about the same, as two rounds can't separate them.
- Rising errors still cost 130 failed requests, because users see some errors before the model is confident.

**Limits**
- Without protection, a failed service stays failed, so those numbers grow with the 100-second window.
- There are only two rounds, on one machine, with simulated traffic. Small differences may be noise.

## Model evaluation

**Data.** 204 experiments and 35,093 samples, recorded from the running system. 50 experiments were held out whole, split so each failure type appears in the test set. Progressive runs that ended before failing were dropped, because their outcome is unknown.

| Measure, on held-out experiments | Result |
|---|---:|
| Failures that build up, predicted before failing | 23 of 23 |
| Median warning | 25 s |
| False alarms on experiments with no failure | 0 of 22 |
| AUC, per second | 0.996 |
| Time-to-failure error (mean absolute) | 5.7 s |
| Failure type accuracy | 97% |
| Sudden crash or hang, warned | 0 of 5 |

**Safer estimate.** The test set is small. Cross-validation on the training experiments gave 94% of failures caught and false alarms on 4% of experiments with no failure.

**Alarm level.** Set at 60% using training data only: the most failures caught while keeping false alarms under 10%. The alarm needs smoothed risk over that level for 3 seconds.

### A failure type it was never taught

Each row retrains the model with that type removed, then tests on it.

| Failure removed from training | Still predicted | Median warning |
|---|---:|---:|
| Memory leak | 0 of 19 | n/a |
| CPU saturation | 2 of 15 | 85 s |
| Connection pool exhaustion | 21 of 26 | 1 s |
| Latency degradation | 5 of 18 | 63 s |
| Rising error rate | 1 of 14 | 24 s |

**The model doesn't generalise well to failure types it hasn't seen.** Treat the warnings above with care too: warnings far longer than the 30-second window may be chance rather than real foresight. New failure types need new training data, and fast restart is the safety net.

## Testing

- 31 unit tests of the decision logic.
- 25 end-to-end tests that run real processes and inject real failures.
- A browser walkthrough of every page during a full guided demo, in light and dark themes and at phone width, with no console errors.
