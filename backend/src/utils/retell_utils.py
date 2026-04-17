"""
Retell AI helpers for Paul backend:
- signature verification (optional)
- fetch/update agent, list voices, publish agent
- fetch/update conversation flow
- normalize webhook payload (call_id, transcript, recording, etc.)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


def verify_retell_signature(raw_body: str, signature: Optional[str]) -> bool:
    """
    Retell webhook signature verification (Retell SDK).
    Controlled by env VERIFY_RETELL_WEBHOOK (default true).
    """
    if os.getenv("VERIFY_RETELL_WEBHOOK", "true").lower() in ("0", "false", "no"):
        return True
    api_key = os.getenv("RETELL_API_KEY")
    if not api_key or not signature:
        return False
    try:
        from retell import Retell

        client = Retell(api_key=str(api_key))
        return bool(client.verify(raw_body, api_key=str(api_key), signature=str(signature)))
    except Exception as e:
        logger.error("Retell signature verify failed: %s", e)
        return False


def ms_epoch_to_datetime(ms: Any) -> Optional[datetime]:
    if ms is None:
        return None
    try:
        v = int(ms)
        return datetime.fromtimestamp(v / 1000.0, tz=timezone.utc)
    except Exception:
        return None


def extract_retell_event_and_call(payload: dict[str, Any]) -> tuple[Optional[str], dict[str, Any]]:
    """Normalize Retell webhook payload to (event, call_dict)."""
    if not isinstance(payload, dict):
        return None, {}
    ev = payload.get("event") or payload.get("event_type") or payload.get("type")
    if isinstance(ev, str):
        ev = ev.strip().lower() or None
    raw_call = payload.get("call")
    call: dict[str, Any] = dict(raw_call) if isinstance(raw_call, dict) else {}
    spill_keys = (
        "call_id",
        "agent_id",
        "from_number",
        "to_number",
        "start_timestamp",
        "end_timestamp",
        "duration_ms",
        "disconnection_reason",
        "transcript",
        "transcript_object",
        "transcript_with_tool_calls",
        "recording_url",
        "recording_multi_channel_url",
        "call_analysis",
        "metadata",
        "collected_dynamic_variables",
    )
    for k in spill_keys:
        if k in payload and payload[k] is not None and (k not in call or call[k] is None):
            call[k] = payload[k]
    cid = call.get("call_id")
    if cid is not None:
        call["call_id"] = str(cid)
    return ev, call


def _headers() -> dict[str, str]:
    api_key = (os.getenv("RETELL_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("RETELL_API_KEY is not configured")
    return {"Authorization": f"Bearer {api_key}"}


def retell_get_agent(agent_id: str) -> dict[str, Any]:
    import requests

    aid = (agent_id or "").strip()
    if not aid:
        raise RuntimeError("RETELL_AGENT_ID is not configured")
    r = requests.get(f"https://api.retellai.com/get-agent/{aid}", headers=_headers(), timeout=45)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected get-agent response")
    return data


def retell_update_agent(agent_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    import requests

    aid = (agent_id or "").strip()
    if not aid:
        raise RuntimeError("RETELL_AGENT_ID is not configured")
    if not isinstance(updates, dict) or not updates:
        raise RuntimeError("updates must be a non-empty object")
    r = requests.patch(
        f"https://api.retellai.com/update-agent/{aid}",
        headers={**_headers(), "Content-Type": "application/json"},
        json=updates,
        timeout=45,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected update-agent response")
    return data


def retell_publish_agent(agent_id: str) -> None:
    import requests

    aid = (agent_id or "").strip()
    if not aid:
        raise RuntimeError("RETELL_AGENT_ID is not configured")
    r = requests.post(f"https://api.retellai.com/publish-agent/{aid}", headers=_headers(), timeout=45)
    r.raise_for_status()


def retell_list_voices() -> list[dict[str, Any]]:
    import requests

    r = requests.get("https://api.retellai.com/list-voices", headers=_headers(), timeout=45)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError("Unexpected list-voices response")
    return [v for v in data if isinstance(v, dict)]


def retell_create_phone_call(
    *,
    from_number: str,
    to_number: str,
    override_agent_id: str | None = None,
    override_agent_version: int | None = None,
    metadata: dict[str, Any] | None = None,
    dynamic_variables: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Create a new outbound phone call (Retell v2).
    Docs: POST https://api.retellai.com/v2/create-phone-call
    """
    import requests

    fn = (from_number or "").strip()
    tn = (to_number or "").strip()
    if not fn:
        raise RuntimeError("from_number is required (configure RETELL_FROM_NUMBER)")
    if not tn:
        raise RuntimeError("to_number is required")

    body: dict[str, Any] = {"from_number": fn, "to_number": tn}
    if override_agent_id:
        body["override_agent_id"] = str(override_agent_id).strip()
    if override_agent_version is not None:
        body["override_agent_version"] = int(override_agent_version)
    if isinstance(metadata, dict) and metadata:
        body["metadata"] = metadata
    if isinstance(dynamic_variables, dict) and dynamic_variables:
        # Retell expects string values
        body["retell_llm_dynamic_variables"] = {str(k): str(v) for k, v in dynamic_variables.items()}

    r = requests.post(
        "https://api.retellai.com/v2/create-phone-call",
        headers={**_headers(), "Content-Type": "application/json"},
        json=body,
        timeout=45,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected create-phone-call response")
    return data


def retell_get_conversation_flow(flow_id: str) -> dict[str, Any]:
    import requests

    fid = (flow_id or "").strip()
    if not fid:
        raise RuntimeError("RETELL_CONVERSATION_FLOW_ID is not configured")
    r = requests.get(f"https://api.retellai.com/get-conversation-flow/{fid}", headers=_headers(), timeout=45)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected get-conversation-flow response")
    return data


def retell_update_conversation_flow(flow_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    import requests

    fid = (flow_id or "").strip()
    if not fid:
        raise RuntimeError("RETELL_CONVERSATION_FLOW_ID is not configured")
    if not isinstance(updates, dict) or not updates:
        raise RuntimeError("updates must be a non-empty object")
    r = requests.patch(
        f"https://api.retellai.com/update-conversation-flow/{fid}",
        headers={**_headers(), "Content-Type": "application/json"},
        json=updates,
        timeout=45,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise RuntimeError("Unexpected update-conversation-flow response")
    return data


def build_transcript_snapshot(call: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Small transcript snapshot we can store in DB."""
    if not isinstance(call, dict):
        return None
    tw = call.get("transcript_with_tool_calls")
    tobj = call.get("transcript_object")
    t = call.get("transcript")
    # store the richest available shape
    if isinstance(tw, list):
        return {"transcript_with_tool_calls": tw}
    if isinstance(tobj, list):
        return {"transcript_object": tobj}
    if isinstance(t, (list, dict, str)):
        return {"transcript": t}
    return None

