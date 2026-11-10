# Viva guide

## A 10-minute presentation

| Time | What to do |
|---|---|
| 1 min | State the problem: one failing service takes others down, and most tools react too late |
| 1 min | State the idea: predict the failure, move traffic, replace the instance before users notice |
| 1 min | Show the Live map and explain the learned dependencies |
| 4 min | Run the guided demo and talk through each step |
| 1 min | Show the Model page, including what it can't do |
| 1 min | Show the benchmark table on the Analytics page |
| 1 min | Limits and future scope |

**Before the demo:** start it 5 minutes early so traffic builds up. Close heavy programs. Use `python run.py --reset` if you want clean numbers.

**Backup:** the demo is real, so results vary. Keep screenshots or a screen recording in case a laptop misbehaves.

## Say it in one line

"Academia Shield predicts which microservice is about to fail and replaces it before users notice. When a failure can't be predicted, it finds the root cause and restarts only that service."

## Questions you may get

**Is the data real?**
The failures and telemetry are real: real processes, real memory use, real crashes. The shop's business logic and its users are simulated.

**How did you get training data with no production system?**
We ran a chaos campaign: 204 experiments injecting failures into the running system, including harmless spikes the model must learn to ignore.

**Why gradient boosting and not deep learning?**
The data is a table of health signals from a modest number of experiments, where gradient boosting usually works well. It also handles missing readings, and it's fast enough to score every instance every second.

**How do you avoid data leakage?**
Neighbouring seconds are nearly identical, so we hold out whole experiments rather than random samples. The alarm level is chosen using training data only.

**You scored 23 of 23. Isn't that suspicious?**
Yes, so don't rely on it alone. The test set is small and uses the same failure types as training. Cross-validation gives 94%, the safer number. With a type removed from training, the model did far worse: it caught 0 of 19 memory leaks.

**Why measure per failure instead of plain accuracy?**
Most seconds are healthy, so a model that always says "fine" would score high on plain accuracy. What matters is whether each failure was caught in time and how often alarms were false.

**Can it predict a crash?**
No. A sudden crash gives no warning in the data, and the model warned on 0 of 5. These are handled by fast detection and restart.

**What's the cost of a false alarm?**
One unnecessary replacement. The new instance starts before the old one stops, so users should see no impact. That's why the alarm level can favour catching failures.

**How do you find the root cause?**
Each service reports errors from its own code separately from errors passed up from a dependency. A service with only dependency errors is a symptom. The learned map shows what it depends on.

**What is the dependency map based on?**
Calls that services report making. The services' calls are defined in the demo's code, but the platform doesn't read that definition. It can only show links that have actually been used.

**Is "Ask Academia Shield" an LLM?**
No. It uses rules to match the question, then answers from live data. Anything that changes the system appears as a button a person must press.

**Does it work in production?**
Not yet. It's a working prototype on one machine. On Kubernetes, the control loop could stay, and the `Supervisor` module would call the cluster's API instead. A different application would need its own training data.

**Is this new? Doesn't predictive monitoring already exist?**
It does exist. Our contribution is combining prediction, a learned dependency map, root-cause-only restart and risk-aware load balancing in one loop, and measuring the effect.

**What was the hardest part?**
Be ready with your own answer. One honest example: separating a service's own errors from dependency errors, because without it, a single outage triggers restarts everywhere.

## Know these numbers

| Number | Meaning |
|---|---|
| 8 services, 11 instances | The demo system |
| 204 experiments, 35,093 samples | Training data |
| 30 s | Prediction window |
| 25 s | Median warning |
| 60%, 3 s | Alarm level and how long risk must stay over it |
| 8,423, 642, 265 | Failed requests: no protection, recover after failure, predict and recover |
| 5 restarts in 120 s | When it stops and alerts a person |

Read `docs/RESULTS.md` and `docs/ARCHITECTURE.md` before the viva, and make sure you can explain every part in your own words. If it isn't all your own work, follow your college's rules on declaring help.
