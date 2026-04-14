import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from slack import WebClient
from slack.errors import SlackApiError

URL = "https://opendata.maryland.gov/resource/uxq4-6wxf.json"
STATE_FILE = Path("outage_state.json")
HISTORY_FILE = Path("outages.json")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "slack-bots")
DEFAULT_ALERT_WINDOW_HOURS = 2
API_LIMIT = int(os.environ.get("API_LIMIT", "5000"))
MAX_MESSAGES_PER_RUN = int(os.environ.get("MAX_MESSAGES_PER_RUN", "4"))


def safe_int(value):
    # The API occasionally returns empty or malformed values; default to zero.
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def estimated_affected_customers(customers, percent_out):
    # percent_out is a percentage value, so divide by 100.
    return round(customers * (percent_out / 100.0))


def area_outage_count(area_snapshot):
    # Backward compatible with older snapshots that stored plain integers.
    if isinstance(area_snapshot, dict):
        return safe_int(area_snapshot.get("outages"))
    return safe_int(area_snapshot)


def parse_dt_stamp(value):
    if not value:
        return None

    try:
        # API emits timestamps like 2026-03-22T13:15:55.000
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_recent_row(row, lookback_hours):
    report_dt = parse_dt_stamp(row.get("dt_stamp", ""))
    if report_dt is None:
        return False

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    return report_dt >= cutoff


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--alert-window-hours",
        type=int,
        default=None,
        help="Override alert lookback window in hours (default: 12)",
    )
    parser.add_argument(
        "--include-existing-window",
        action="store_true",
        help="Report outages in the window even if rows were previously seen",
    )
    return parser.parse_args()


def resolve_alert_window_hours(cli_value):
    if cli_value is not None:
        return max(1, cli_value)

    env_value = os.environ.get("ALERT_WINDOW_HOURS")
    if env_value:
        try:
            return max(1, int(env_value))
        except ValueError:
            return DEFAULT_ALERT_WINDOW_HOURS

    return DEFAULT_ALERT_WINDOW_HOURS


def parse_bool(value):
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def resolve_include_existing_window(cli_value):
    if cli_value:
        return True

    return parse_bool(os.environ.get("INCLUDE_EXISTING_WINDOW", "false"))


def fetch_current_outages():
    response = requests.get(
        URL,
        params={
            "$order": "dt_stamp DESC",
            "$limit": API_LIMIT,
        },
        timeout=30,
    )
    response.raise_for_status()
    rows = response.json()

    # Keep only the newest row per county so source ordering does not matter.
    by_area = {}
    latest_dt_by_area = {}
    latest_dt = None
    for row in rows:
        area = row.get("area")
        if not area:
            continue

        dt_stamp = row.get("dt_stamp", "")
        dt_value = parse_dt_stamp(dt_stamp)
        previous_area_dt = latest_dt_by_area.get(area)

        if previous_area_dt is not None and dt_value is not None and dt_value <= previous_area_dt:
            continue

        outages = safe_int(row.get("outages"))
        customers = safe_int(row.get("customers"))
        percent_out = safe_float(row.get("percent_out"))
        by_area[area] = {
            "outages": outages,
            "customers": customers,
            "percent_out": percent_out,
            "affected_customers": estimated_affected_customers(customers, percent_out),
        }

        if dt_value is not None:
            latest_dt_by_area[area] = dt_value
            if latest_dt is None or dt_value > latest_dt:
                latest_dt = dt_value

    latest_dt_stamp = latest_dt.isoformat(timespec="seconds") if latest_dt else ""

    return {
        "latest_dt_stamp": latest_dt_stamp,
        "areas": by_area,
    }, rows


def row_key(row):
    uid = row.get("uid")
    if uid:
        return uid

    # Fallback key if uid is missing.
    return f"{row.get('area', '')}|{row.get('dt_stamp', '')}"


def append_new_rows(rows):
    existing_rows = []
    if HISTORY_FILE.exists():
        try:
            with HISTORY_FILE.open("r", encoding="utf-8") as infile:
                loaded = json.load(infile)
                if isinstance(loaded, list):
                    existing_rows = loaded
        except (json.JSONDecodeError, OSError):
            existing_rows = []

    seen_keys = {row_key(row) for row in existing_rows}
    new_rows = [row for row in rows if row_key(row) not in seen_keys]

    if new_rows:
        existing_rows.extend(new_rows)
        with HISTORY_FILE.open("w", encoding="utf-8") as outfile:
            json.dump(existing_rows, outfile, indent=2)
            outfile.write("\n")

    return new_rows


def load_previous_snapshot():
    if not STATE_FILE.exists():
        return None

    try:
        with STATE_FILE.open("r", encoding="utf-8") as infile:
            return json.load(infile)
    except (json.JSONDecodeError, OSError):
        # If state is missing/corrupt, treat this run as a fresh baseline.
        return None


def save_snapshot(snapshot):
    with STATE_FILE.open("w", encoding="utf-8") as outfile:
        json.dump(snapshot, outfile, indent=2, sort_keys=True)
        outfile.write("\n")


def find_increases(previous_snapshot, current_snapshot):
    if not previous_snapshot:
        return []

    previous_areas = previous_snapshot.get("areas", {})
    current_areas = current_snapshot.get("areas", {})

    increases = []
    for area, current_area_data in current_areas.items():
        current_count = area_outage_count(current_area_data)
        previous_count = area_outage_count(previous_areas.get(area))
        # Alert only on upward movement to avoid noisy "no change" updates.
        if current_count > previous_count:
            increases.append((area, previous_count, current_count, current_area_data))

    increases.sort(key=lambda item: item[2] - item[1], reverse=True)
    return increases


def build_message(current_snapshot, increases):
    latest_dt_stamp = current_snapshot.get("latest_dt_stamp", "unknown")
    total_current = sum(
        area_outage_count(area_data)
        for area_data in current_snapshot.get("areas", {}).values()
    )

    lines = [
        f"New power outages reported in Maryland since last check ({latest_dt_stamp}).",
        f"Current statewide outages: {total_current}",
        "Increases by area:",
    ]

    # Keep message compact for Slack by listing top changes only.
    for area, previous_count, current_count, current_area_data in increases[:15]:
        delta = current_count - previous_count
        customers = safe_int(current_area_data.get("customers"))
        percent_out = safe_float(current_area_data.get("percent_out"))
        affected_customers = safe_int(current_area_data.get("affected_customers"))
        lines.append(
            f"- {area}: outages {previous_count} -> {current_count} (+{delta}), "
            f"customers={customers}, percent_out={percent_out}%, "
            f"affected~{affected_customers}"
        )

    return "\n".join(lines)


def build_recent_reports_message(current_snapshot, recent_rows, alert_window_hours):
    areas = current_snapshot.get("areas", {})
    recent_areas = sorted({row.get("area") for row in recent_rows if row.get("area")})

    # Show highest outage counties first.
    recent_areas.sort(
        key=lambda area: area_outage_count(areas.get(area, {})),
        reverse=True,
    )

    latest_report = max(
        (row.get("dt_stamp", "") for row in recent_rows),
        default="unknown",
    )

    impacted_areas = []
    for area in recent_areas:
        area_data = areas.get(area, {})
        outages = area_outage_count(area_data)
        if outages > 0:
            impacted_areas.append((area, area_data, outages))

    impacted_areas.sort(key=lambda item: item[2], reverse=True)

    lines = [
        f"Outage update ({latest_report}) - reports in last {alert_window_hours}h",
        f"Counties with outages: {len(impacted_areas)}",
    ]

    for area, area_data, outages in impacted_areas[:MAX_COUNTIES_PER_MESSAGE]:
        customers = safe_int(area_data.get("customers"))
        percent_out = safe_float(area_data.get("percent_out"))
        affected_customers = safe_int(area_data.get("affected_customers"))
        lines.append(
            f"- {area}: out={outages}, affected~{affected_customers} "
            f"({percent_out:.2f}% of {customers})"
        )

    remaining = len(impacted_areas) - MAX_COUNTIES_PER_MESSAGE
    if remaining > 0:
        lines.append(f"- +{remaining} more counties")

    return "\n".join(lines)


def build_grouped_recent_reports_message(current_snapshot, grouped_rows, alert_window_hours):
    areas = current_snapshot.get("areas", {})
    affected_area_names = {
        row.get("area")
        for _, rows_for_time in grouped_rows
        for row in rows_for_time
        if row.get("area")
    }

    total_affected_customers = 0
    for area in affected_area_names:
        area_data = areas.get(area, {})
        total_affected_customers += safe_int(area_data.get("affected_customers"))

    latest_report_dt = None
    for report_time, _ in grouped_rows:
        dt = parse_dt_stamp(report_time)
        if dt is not None and (latest_report_dt is None or dt > latest_report_dt):
            latest_report_dt = dt

    if latest_report_dt is not None:
        formatted_time = latest_report_dt.strftime("%B %d, %Y %I:%M %p UTC")
    else:
        formatted_time = "unknown"

    window_label = "last hour" if alert_window_hours == 1 else f"last {alert_window_hours} hours"

    lines = [
        f"Outage update, {window_label}: {formatted_time}",
        f"Total affected customers: {total_affected_customers}",
        f"Affected counties: {len(affected_area_names)}",
    ]

    return "\n".join(lines)


def group_rows_by_report_time(rows):
    grouped = {}
    for row in rows:
        dt_stamp = row.get("dt_stamp", "unknown")
        grouped.setdefault(dt_stamp, []).append(row)

    # Keep chronological order inside one grouped message.
    return sorted(grouped.items(), key=lambda item: item[0])


def send_slack_message(message):
    slack_token = os.environ.get("SLACK_API_TOKEN")
    if not slack_token:
        raise RuntimeError("SLACK_API_TOKEN is not set")

    client = WebClient(token=slack_token)

    try:
        client.chat_postMessage(
            channel=SLACK_CHANNEL,
            text=message,
            unfurl_links=False,
            unfurl_media=False,
        )
    except SlackApiError as error:
        raise RuntimeError(f"Slack API error: {error.response['error']}") from error


def main(alert_window_hours, include_existing_window):
    previous_snapshot = load_previous_snapshot()
    current_snapshot, raw_rows = fetch_current_outages()
    new_rows = append_new_rows(raw_rows)

    # If state is missing, treat this as bootstrap and evaluate the full current feed.
    bootstrap_mode = previous_snapshot is None
    candidate_rows = raw_rows if (include_existing_window or bootstrap_mode) else new_rows

    if include_existing_window:
        print(
            f"Manual include-existing mode enabled: evaluating all rows from the last "
            f"{alert_window_hours} hours."
        )
    elif bootstrap_mode:
        print(
            f"No previous state found: evaluating all current rows from the last "
            f"{alert_window_hours} hours."
        )

    recent_new_outage_rows = [
        row
        for row in candidate_rows
        if safe_int(row.get("outages")) > 0 and is_recent_row(row, alert_window_hours)
    ]

    if recent_new_outage_rows:
        grouped_rows = group_rows_by_report_time(recent_new_outage_rows)
        if len(grouped_rows) > MAX_MESSAGES_PER_RUN:
            skipped = len(grouped_rows) - MAX_MESSAGES_PER_RUN
            grouped_rows = grouped_rows[-MAX_MESSAGES_PER_RUN:]
            print(
                f"Capped grouped timestamps to {MAX_MESSAGES_PER_RUN} this run "
                f"(skipped {skipped} older timestamp groups)."
            )

        message = build_grouped_recent_reports_message(current_snapshot, grouped_rows, alert_window_hours)
        send_slack_message(message)
        print(f"Posted grouped outage report update to Slack ({len(grouped_rows)} timestamp groups).")
    elif previous_snapshot:
        print(f"No new outage reports from the last {alert_window_hours} hours. No Slack message sent.")
    else:
        print(
            f"No previous state found and no new outage reports from the last {alert_window_hours} hours. "
            "Saved baseline without sending a Slack message."
        )

    save_snapshot(current_snapshot)
    print(f"Appended {len(new_rows)} new rows to outages.json")


if __name__ == "__main__":
    args = parse_args()
    main(
        resolve_alert_window_hours(args.alert_window_hours),
        resolve_include_existing_window(args.include_existing_window),
    )
