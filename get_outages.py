import requests
from datetime import datetime
from datetime import timedelta
import os
from slack import WebClient
from slack.errors import SlackApiError

slack_token = os.environ.get('SLACK_API_TOKEN')

client = WebClient(token=slack_token)

url = "https://opendata.maryland.gov/resource/uxq4-6wxf.json"
min_outages = 1
local_file_path = 'outages.json'
right_now = datetime.now()
today = right_now - timedelta(hours=4)  # Adjusting for Eastern Time (UTC-4)
last_24_hours = right_now - timedelta(hours=28)  # To ensure we capture all outages from the last 24 hours

response = requests.get(url)
response.raise_for_status()

data = response.json()

filtered_outages = [o for o in data if int(o["outages"]) >= min_outages] 
outages_in_last_24_hours = [o for o in filtered_outages if o["dt_stamp"] >= last_24_hours.isoformat()]
sum_outages_last_24_hours = sum(int(o["outages"]) for o in outages_in_last_24_hours)
msg = f"Total power outages reported in Maryland in the last 24 hours: {sum_outages_last_24_hours}"

try:
    response = client.chat_postMessage(
        channel="slack-bots",
        text=msg,
        unfurl_links=True, 
        unfurl_media=True
    )
except SlackApiError as e:
    assert e.response["ok"] is False
    assert e.response["error"]
    print(f"Got an error: {e.response['error']}")
