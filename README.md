# Academia Shield

Failure prediction and recovery for the Academia student portal.

AI-powered microservice failure prediction and recovery platform.

When one microservice fails, the services that depend on it can fail too. Academia Shield monitors a set of microservices, predicts which instance is about to fail, moves traffic away from it and replaces it before users are affected. When a failure can't be predicted, it finds the root cause and restarts only that service.

## Run it

You need **Python 3.10 or newer**. On Windows, tick "Add python.exe to PATH" when installing it. You need internet access for the first run only.

| System | Command |
|---|---|
| Windows | Double-click `start.bat` |
| Mac or Linux | `./start.sh` |

The first run installs the packages, then the dashboard opens at **http://127.0.0.1:8800**. Press `Ctrl+C` in the terminal to stop.

If your scikit-learn version differs from the one that trained the saved model, Academia Shield retrains it from the recorded data on first start. This takes a few minutes, happens once, and may change the model's reported numbers slightly.

## Show the demo

1. On **Live map**, select **Start guided demo**. It takes about four minutes and injects four real failures: a memory leak, a crash, a freeze that cascades, and a bad release.
2. To compare setups yourself, open **Failure lab**, change **Protection** at the top of the page, and inject the same failure.
3. Select **Ask Academia Shield** or press `/` and try: "What happens if inventory fails?"

See [docs/VIVA_GUIDE.md](docs/VIVA_GUIDE.md) for a presentation plan and likely questions.

## What each requirement became

| Requirement | Feature |
|---|---|
| Health monitoring | Health checks and health signals for every instance, every second |
| Dependency mapping | A live map learned from observed calls, with blast radius and root cause |
| Failure prediction using ML | Risk, time left, failure type and reasons, 30 seconds ahead |
| Automatic restart | Replacement starts first; repeated failures stop and alert a person |
| Intelligent load balancing | Traffic follows predicted risk, latency and load; circuit breakers and retries |
| Real-time notifications | Dashboard, Slack or Discord webhooks, and email |
| Failure analytics dashboard | Availability, recovery time, prevented failures, by service and type |

## Measured results

See [docs/RESULTS.md](docs/RESULTS.md) for the method and limits.

| Setup | Requests succeeded | Failed requests | Instances that actually failed |
|---|---|---:|---:|
| No protection | 66.53% | 8,423 | 14 of 14 |
| Recover after failure | 97.39% | 642 | 14 of 14 |
| Predict and recover | 98.92% | 265 | 4 of 14 |

The 4 are the sudden crashes and freezes, which can't be predicted.

## Options

```
python run.py --port 8810        use another dashboard port
python run.py --rps 30           change synthetic traffic
python run.py --mode reactive    start with autonomous, reactive or observe
python run.py --reset            delete incident history first
python run.py --no-browser       do not open a browser
```

Interactive API documentation is at `/docs`.

### Alerts

Paste a Slack or Discord webhook address on the **Alerts** page. For email, set these before starting: `FORESIGHT_SMTP_HOST`, `FORESIGHT_SMTP_PORT`, `FORESIGHT_SMTP_USER`, `FORESIGHT_SMTP_PASSWORD`, `FORESIGHT_SMTP_FROM`, `FORESIGHT_SMTP_TO`.

Webhook delivery has been tested against a local receiver, not live Slack or Discord. Email has not been tested against a real mail server.

## Reproduce the work

```
python -m pip install -r requirements-dev.txt
python -m pytest tests/test_units.py              # fast logic tests
python -m pytest tests/test_e2e.py                # real failures, about 3 minutes
python -m foresight.ml.collect --minutes 30 --out data/new.jsonl.gz   # record new telemetry
python -m foresight.ml.train --data data/new.jsonl.gz                 # train and evaluate
python benchmarks/run_benchmark.py --repeats 2    # about 80 minutes
```

Close the dashboard before running the end-to-end tests or the benchmark, because they need the CPU to themselves.

## Troubleshooting

| What you see | What to do |
|---|---|
| "Port 8800 is already in use" | Close the other copy, or run `python run.py --port 8810` |
| Windows firewall prompt | Allow it. Everything stays on your own computer (127.0.0.1) |
| "Missing packages" | Run `python -m pip install -r requirements.txt` |
| A service won't start | Read the matching file in the `logs` folder |
| A slower laptop raises odd alarms | Lower traffic with `python run.py --rps 10` |

## Tested on

Linux with Python 3.13. It has not been run on Windows or Mac, although the code and `start.bat` are written for them. Try it on your own laptop before the demo.

## Project layout

```
run.py                 start here
foresight/services/    demo microservice, load balancer, traffic generator
foresight/control/     control loop, monitoring, prediction, recovery, incidents, alerts, assistant, API
foresight/ml/          failure catalogue, features, data collection, training
foresight/web/         dashboard
models/                trained models and evaluation reports
data/telemetry...      recorded training data
tests/, benchmarks/    tests and the three-way experiment
docs/                  architecture, results, viva guide
```
