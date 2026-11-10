# Architecture

```
                    synthetic users (loadgen)
                              |
                              v
                 +------------------------+      routes + weights      +------------------+
                 |  Load balancer         | <------------------------- |  Control plane   |
                 |  weighted, breakers    |      breaker stats         |  (1 tick/second) |
                 +------------------------+ -------------------------> |                  |
                    |   every service call                             |  Monitor         |
                    v   passes through it                              |  Dependency map  |
   gateway  auth  catalog  cart  orders  inventory  payment  notify    |  Predictor (ML)  |
   (11 real OS processes, one per instance)                            |  Root cause      |
          ^                  |                                         |  Recovery engine |
          | start/stop       | /metrics, OS stats (psutil)             |  Incidents       |
          +------------------+---------------------------------------> |  Notifications   |
                                                                       +------------------+
                                                                          | REST + WebSocket
                                                                          v
                                                                      Dashboard
```

## The control loop

Every second, `control/platform.py` runs these steps in order:

1. **Observe.** Scrape every instance over HTTP and read process stats from the OS. OS stats keep working when a service hangs.
2. **Learn dependencies.** Update the map from the call counters each caller reports.
3. **Predict.** Build features from the last 30 samples and score each instance.
4. **Classify.** Give each instance a health state, separating its own faults from dependency errors.
5. **Diagnose.** Find root causes by walking the learned graph.
6. **Decide.** Replace failed instances. Replace at-risk instances early. Leave symptoms alone.
7. **Steer.** Publish weights to the load balancer, so traffic leaves risky instances early.
8. **Report.** Update incidents, send notifications and stream the snapshot to the dashboard.

## How each requirement is met

| Requirement | Where | How |
|---|---|---|
| Health monitoring | `control/telemetry.py` | Probes plus health signals per instance, each second. A single `FailureDetector` defines "failed" for both training labels and runtime. |
| Dependency mapping | `DependencyMap` | Built only from observed calls. Computes blast radius and root cause. |
| Failure prediction | `ml/`, `control/predictor.py` | Gradient boosting (risk), regression (time left), classification (type), isolation forest (anomaly). |
| Automatic restart | `control/recovery.py` | Starts the replacement first, moves traffic, retires the old instance. Backs off and escalates when restarts stop helping. |
| Intelligent load balancing | `services/balancer.py` | Random choice with odds of weight / (latency x load), with weight from predicted risk. Circuit breakers and retries. |
| Real-time notifications | `control/notifier.py` | WebSocket to the dashboard, webhooks (Slack, Discord, any JSON endpoint), SMTP email. De-duplicated, with delivery results recorded. |
| Analytics dashboard | `web/`, `Platform.analytics` | Availability, MTTR, prevented failures, by service and type, alarm accuracy. |

## Key design decisions

**Own errors versus dependency errors.** A service reports errors caused by its own code separately from errors passed up from a dependency. This one distinction drives three things: the failure label, circuit breakers and root-cause analysis. Without it, an inventory outage would make the platform restart gateway, orders and catalog for no benefit.

**One failure definition.** Training labels and live detection share `FailureDetector`, and training and serving share `compute_features`. This prevents training/serving skew.

**Replace before stop.** A proactive replacement never reduces capacity, so a false alarm costs little. That is why the alarm threshold can favour catching failures.

**Restart storm protection.** Five restarts of one service within 120 seconds means restarting isn't working. The platform stops and alerts a person.

**Evaluation by experiment, not by sample.** Neighbouring seconds of telemetry are nearly identical, so a random split of samples would leak answers into training. Whole experiments are held out instead, and success is measured per failure: caught or missed, with how much warning.

## Failure catalogue

| Type | Failure | How it is real |
|---|---|---|
| Progressive | Memory leak | Allocates memory until over the limit, then exit code 137 |
| Progressive | CPU saturation | Blocks the event loop for longer and longer |
| Progressive | Pool exhaustion | Takes worker slots and never returns them |
| Progressive | Latency degradation | Requests get steadily slower |
| Progressive | Rising errors | A growing share of requests return 500 |
| Sudden | Crash | The process exits at once |
| Sudden | Hang | The event loop blocks forever, and the process stays alive |
| Sudden | Bad release | New processes die during start-up |
| Harmless | Cache growth, CPU burst, latency blip, error blip | Look like trouble, end on their own. These teach the model what not to flag. |

## Limits of this project

- The application's business logic is simulated. Its processes, network calls and failures are real.
- It runs on one machine, supervising local processes. In production the same control loop would call the Kubernetes API instead of `Supervisor`.
- The load balancer is a single process. The control plane restarts it if it dies, but it is not highly available.
- The model is trained on these services and failure types. A different system would need its own chaos campaign, using `foresight/ml/collect.py`.
- A restart is mitigation, not a fix. A leak returns until the code is fixed, so incidents include a suggested fix.
- There is no login on the API, so it listens only on 127.0.0.1 by default.
