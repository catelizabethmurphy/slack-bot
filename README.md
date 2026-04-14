# slack-bot-sandbox

A repository for experimenting with the Slack API in Python by students in JOUR328, News Application Development.

## Automated Outage Monitoring

This repo includes a GitHub Actions workflow at [.github/workflows/slacky.yaml](.github/workflows/slacky.yaml) that runs every 2 hours.

Behavior:
- Fetches the latest Maryland outage data.
- Detects newly discovered outage rows and alerts only when those reports are from the last 2 hours.
- Sends a single compact Slack summary per run with update time, total affected customers, and affected county count.
- On first run with no prior state/history, it still sends alerts for outage reports found in the last 2 hours.
- Saves the latest snapshot to `outage_state.json` and commits it so the next run has state.

Required GitHub configuration:
- Repository secret: `SLACK_API_TOKEN`
- Optional repository variable: `SLACK_CHANNEL` (defaults to `slack-bots`)
- Optional repository variable: `ALERT_WINDOW_HOURS` (workflow default is `2`)
- Optional repository variable: `API_LIMIT` (default `5000`, fetches more rows ordered by newest timestamp)
- Optional repository variable: `MAX_MESSAGES_PER_RUN` (default `4`)

Manual trigger option:
- When running `workflow_dispatch`, you can set `alert_window_hours` (default `2`).
- You can also set `include_existing_window=true` to report outages in the window even if they were already seen before.

Local override flag:
- Run `python get_outages.py --alert-window-hours 2` to force a 2-hour window in terminal runs.
- Run `python get_outages.py --alert-window-hours 2 --include-existing-window` to force a manual backfill-style report for the last 2 hours.

Alert recency behavior:
- The bot only sends Slack alerts when newly discovered outage rows have report timestamps inside the alert window.
- Example: with `ALERT_WINDOW_HOURS=2`, delayed rows reported within the last 2 hours still trigger an alert when first discovered.

Spam controls:
- Slack messages are compact and send one summary per run.
- If too many timestamp groups are detected in one run, included groups are capped to `MAX_MESSAGES_PER_RUN` (most recent groups).