import argparse
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
from slack import WebClient
from slack.errors import SlackApiError

REGULATIONS_API_BASE = "https://api.regulations.gov/v4/documents"
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "slack-bots")
PAGE_SIZE = 250
MAX_SLACK_TEXT_LENGTH = 3500


def previous_utc_day_bounds():
    now_utc = datetime.now(timezone.utc)
    yesterday = (now_utc - timedelta(days=1)).date()
    day_value = yesterday.isoformat()
    return day_value, day_value


def today_so_far_utc_bounds():
    today = datetime.now(timezone.utc).date()
    day_value = today.isoformat()
    return day_value, day_value


def regulations_headers():
    api_key = os.environ.get("REGULATIONS_API_KEY")
    if not api_key:
        raise RuntimeError("REGULATIONS_API_KEY is not set")

    return {
        "X-Api-Key": api_key,
        "Accept": "application/json",
    }


def parse_document_type(raw_value):
    doc_type = (raw_value or "").strip().lower()

    if "proposed" in doc_type and "rule" in doc_type:
        return "proposed"

    if "rule" in doc_type:
        return "final"

    return None


def document_agency(attributes):
    agency_name = attributes.get("agencyName")
    if agency_name:
        return agency_name

    agency_id = attributes.get("agencyId")
    if agency_id:
        return agency_id

    agency_ids = attributes.get("agencyIds")
    if isinstance(agency_ids, list) and agency_ids:
        return str(agency_ids[0])

    return "Unknown Agency"


def fetch_documents_for_day(day_value):
    params = {
        "filter[postedDate][eq]": day_value,
        "filter[documentType]": "Proposed Rule,Rule",
        "sort": "postedDate",
        "page[size]": PAGE_SIZE,
    }

    documents = []
    next_url = REGULATIONS_API_BASE
    headers = regulations_headers()

    while next_url:
        response = requests.get(next_url, headers=headers, params=params if next_url == REGULATIONS_API_BASE else None, timeout=30)
        response.raise_for_status()
        payload = response.json()

        data = payload.get("data", [])
        if isinstance(data, list):
            documents.extend(data)

        links = payload.get("links", {})
        next_url = links.get("next")

    return documents


def build_digest_rows(documents):
    proposed_count = 0
    final_count = 0
    agencies = defaultdict(lambda: {"proposed": 0, "final": 0})

    for item in documents:
        attributes = item.get("attributes", {})
        if not isinstance(attributes, dict):
            continue

        category = parse_document_type(attributes.get("documentType"))
        if category is None:
            continue

        if category == "proposed":
            proposed_count += 1
        else:
            final_count += 1

        agency = document_agency(attributes)
        agencies[agency][category] += 1

    return proposed_count, final_count, agencies


def build_message_lines(window_label, proposed_count, final_count, agencies):
    total = proposed_count + final_count

    lines = [
        f"Regulations.gov digest for {window_label}",
        f"Total new rules: {total}",
        f"Proposed rules: {proposed_count}",
        f"Final rules: {final_count}",
        f"Agencies with new rules: {len(agencies)}",
        "",
    ]

    if total == 0:
        lines.append(f"No new proposed or final rules found for {window_label}.")
        return lines

    for agency_name in sorted(agencies.keys()):
        proposed_for_agency = agencies[agency_name]["proposed"]
        final_for_agency = agencies[agency_name]["final"]

        lines.append(
            f"{agency_name} - proposed: {proposed_for_agency}, final: {final_for_agency}"
        )

    return lines


def chunk_message_lines(lines):
    chunks = []
    current_lines = []
    current_size = 0

    for line in lines:
        line_size = len(line) + 1

        if current_lines and (current_size + line_size) > MAX_SLACK_TEXT_LENGTH:
            chunks.append("\n".join(current_lines).strip())
            current_lines = [line]
            current_size = line_size
            continue

        current_lines.append(line)
        current_size += line_size

    if current_lines:
        chunks.append("\n".join(current_lines).strip())

    return [chunk for chunk in chunks if chunk]


def send_to_slack(chunks):
    slack_token = os.environ.get("SLACK_API_TOKEN")
    if not slack_token:
        raise RuntimeError("SLACK_API_TOKEN is not set")

    client = WebClient(token=slack_token)

    for chunk in chunks:
        try:
            client.chat_postMessage(
                channel=SLACK_CHANNEL,
                text=chunk,
                unfurl_links=False,
                unfurl_media=False,
            )
        except SlackApiError as error:
            raise RuntimeError(f"Slack API error: {error.response['error']}") from error


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the digest to stdout instead of posting to Slack",
    )
    parser.add_argument(
        "--today-so-far",
        action="store_true",
        help="Use today's UTC window from 00:00 through now",
    )
    parser.add_argument(
        "--yesterday",
        action="store_true",
        help="Force the previous UTC day window",
    )
    return parser.parse_args()


def resolve_window(today_so_far_flag, yesterday_flag):
    if today_so_far_flag and yesterday_flag:
        raise RuntimeError("Choose either --today-so-far or --yesterday, not both")

    if today_so_far_flag:
        day_label, day_start = today_so_far_utc_bounds()
        return f"{day_label} so far (UTC)", day_start

    if yesterday_flag:
        day_label, day_start = previous_utc_day_bounds()
        return f"{day_label} (yesterday UTC)", day_start

    github_event_name = os.environ.get("GITHUB_EVENT_NAME", "").strip().lower()
    if github_event_name == "workflow_dispatch":
        day_label, day_start = today_so_far_utc_bounds()
        return f"{day_label} so far (UTC)", day_start

    day_label, day_start = previous_utc_day_bounds()
    return f"{day_label} (yesterday UTC)", day_start


def main(dry_run=False, today_so_far=False, yesterday=False):
    window_label, day_start = resolve_window(today_so_far, yesterday)
    documents = fetch_documents_for_day(day_start)
    proposed_count, final_count, agencies = build_digest_rows(documents)
    lines = build_message_lines(window_label, proposed_count, final_count, agencies)
    chunks = chunk_message_lines(lines)

    if dry_run:
        print("\n\n---\n\n".join(chunks))
        return

    send_to_slack(chunks)
    print(
        "Posted Regulations.gov digest to Slack "
        f"({proposed_count} proposed, {final_count} final across {len(agencies)} agencies)."
    )


if __name__ == "__main__":
    args = parse_args()
    main(
        dry_run=args.dry_run,
        today_so_far=args.today_so_far,
        yesterday=args.yesterday,
    )
