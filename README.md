# Regulations.gov Morning Slack Bot

This project sends a Slack digest every morning with:
- How many new proposed rules were published on Regulations.gov yesterday
- How many new final rules were published yesterday
- Which agencies published those rules

## How it works

The script in `get_new_rules.py` now:
1. Queries the Regulations.gov API for documents posted during the previous UTC day.
2. Filters to rule documents and classifies each as proposed or final.
3. Groups results by agency.
4. Posts one or more Slack messages (chunked if needed) with per-agency proposed/final counts.

Window behavior:
- Scheduled run: reports yesterday (UTC).
- Manual GitHub Actions run (`workflow_dispatch`): reports today so far (UTC).
- Local run: defaults to yesterday (UTC), or use `--today-so-far`.

## Required secrets and variables

Set these in GitHub repository settings:

### Secrets
- `SLACK_API_TOKEN`: Bot token for your Slack app.
- `REGULATIONS_API_KEY`: Your Regulations.gov API key.

### Variables
- `SLACK_CHANNEL` (optional): Slack channel name. Defaults to `slack-bots`.

## GitHub Actions schedule

Workflow file: `.github/workflows/slacky.yaml`

- Runs daily at `12:00 UTC`.
- Also supports manual `workflow_dispatch` runs.

If you want a different morning delivery time, edit the cron expression.

## Local run

Install dependencies:

```bash
pip install requests slackclient
```

Dry-run (prints digest instead of posting to Slack):

```bash
export REGULATIONS_API_KEY="your-key"
python get_new_rules.py --dry-run

python get_new_rules.py --dry-run --today-so-far
```

Post to Slack:

```bash
export REGULATIONS_API_KEY="your-key"
export SLACK_API_TOKEN="xoxb-..."
export SLACK_CHANNEL="slack-bots"
python get_new_rules.py

python get_new_rules.py --today-so-far
```

## Notes

- The "previous day" window is based on UTC.
- If there are many rules, the bot posts multiple messages so no data is cut off.
