import os
from slack import WebClient
from slack.errors import SlackApiError

slack_token = os.environ.get('SLACK_API_TOKEN')

client = WebClient(token=slack_token)
msg = "testing!"
try:
    response = client.chat_postMessage(
        channel="slack-bots",
        text=msg,
        unfurl_links=True, 
        unfurl_media=True
    )
    print("success!")
except SlackApiError as e:
    assert e.response["ok"] is False
    assert e.response["error"]
    print(f"Got an error: {e.response['error']}")