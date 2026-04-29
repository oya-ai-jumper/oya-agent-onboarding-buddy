---
name: slack-send-message
display_name: "Slack Send Message"
description: "Send a message to a Slack thread"
category: communication
icon: message-square
skill_type: sandbox
catalog_type: platform
resource_requirements:
  - env_var: SLACK_BOT_TOKEN
    name: "Slack Bot Token"
    description: "Slack Bot OAuth token (xoxb-...)"
tool_schema:
  name: slack_send_message
  description: "Send a message to a Slack thread (thread_ts is the root message timestamp)"
  parameters:
    type: object
    properties:
      channel:
        type: "string"
        description: "Channel ID"
      thread_ts:
        type: "string"
        description: "Thread ID (timestamp of the root message, e.g. 1709827000.000001)"
      text:
        type: "string"
        description: "Message text"
    required: [channel, thread_ts, text]
---
# Slack Send Message
Send a message to a Slack thread. Requires channel, thread_ts, and text.
