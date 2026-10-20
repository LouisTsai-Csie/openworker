"""Messaging connectors — Slack/Telegram adapters, the gateway, and the send_message tool."""

from __future__ import annotations

from coworker.connectors.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageSource,
    MessageType,
    SendResult,
    SessionSource,
    format_target,
    parse_target,
)
from coworker.connectors.adapters import (
    SlackAdapter,
    TelegramAdapter,
    make_adapter,
    slack_event_to_event,
    telegram_message_to_event,
)
from coworker.connectors.config import ConnectorSettings, TeamAuth, is_authorized, load_settings
from coworker.connectors.relay_client import SlackRelayAdapter
from coworker.connectors.slack_addr import qualify as slack_qualify, split as slack_split
from coworker.connectors.descriptors import ConnectorDescriptor, get_descriptor, list_descriptors
from coworker.connectors.fake import FakeAdapter
from coworker.connectors.gateway import Gateway
from coworker.connectors.senders import DEFAULT_SENDERS
from coworker.connectors.setup import (
    connect_connector,
    connector_list,
    disconnect_connector,
    experimental_enabled,
    set_experimental_enabled,
    update_connector_tools,
)
from coworker.connectors.integration_tools import make_integration_tools
from coworker.connectors.tools import make_send_file_tool, make_send_message_tool
from coworker.connectors.tool_defs import connector_for_tool

__all__ = [
    "DEFAULT_SENDERS",
    "BasePlatformAdapter",
    "ConnectorDescriptor",
    "ConnectorSettings",
    "FakeAdapter",
    "Gateway",
    "MessageEvent",
    "MessageSource",
    "MessageType",
    "SendResult",
    "SessionSource",
    "SlackAdapter",
    "SlackRelayAdapter",
    "TeamAuth",
    "TelegramAdapter",
    "connect_connector",
    "connector_for_tool",
    "connector_list",
    "disconnect_connector",
    "experimental_enabled",
    "format_target",
    "get_descriptor",
    "is_authorized",
    "list_descriptors",
    "load_settings",
    "make_adapter",
    "make_integration_tools",
    "make_send_file_tool",
    "make_send_message_tool",
    "parse_target",
    "set_experimental_enabled",
    "slack_event_to_event",
    "slack_qualify",
    "slack_split",
    "telegram_message_to_event",
    "update_connector_tools",
]
