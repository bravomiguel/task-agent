"""Agent tools."""

from __future__ import annotations

import mimetypes
import os
from typing import Annotated, Literal

import httpx
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

LANGGRAPH_API_URL = os.getenv("LANGGRAPH_API_URL", "http://localhost:2024")


def _get_mime_type(filepath: str) -> str:
    """Get MIME type from file extension."""
    mime_type, _ = mimetypes.guess_type(filepath)
    return mime_type or "application/octet-stream"


@tool
def present_file(filepath: str) -> str:
    """Present a file to the user in the document viewer.

    Call this tool after creating or modifying a file that the user should see.
    The file will automatically open in the user's document viewer.

    Args:
        filepath: Relative path to the file (e.g., "outputs/report.md").
                  Must be a file in the outputs/ directory.

    Returns:
        XML with file metadata for frontend rendering.
    """
    # Extract filename from path
    name = os.path.basename(filepath)
    mime_type = _get_mime_type(filepath)

    return f"""<presented_file>
<file_path>{filepath}</file_path>
<name>{name}</name>
<mime_type>{mime_type}</mime_type>
</presented_file>"""


def _extract_event_content(state: dict) -> str | None:
    """Extract event content from the first user message."""
    messages = state.get("messages", [])
    for msg in messages:
        msg_type = getattr(msg, "type", None) or (
            msg.get("type") if isinstance(msg, dict) else None
        )
        msg_role = getattr(msg, "role", None) or (
            msg.get("role") if isinstance(msg, dict) else None
        )

        if msg_type == "human" or msg_role == "user":
            content = getattr(msg, "content", None) or (
                msg.get("content", "") if isinstance(msg, dict) else ""
            )
            return content
    return None


@tool
def route_event(
    thread_id: str,
    task_instruction: str = None,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Route the incoming event to a thread and start the task agent.

    Call this tool when you have decided which thread to route the event to.
    The tool will execute the routing and return success or an error message.
    If you get an error, you may retry up to 2 more times.

    Args:
        thread_id: Use 'new' for a new thread, or provide an existing thread UUID.
        task_instruction: Optional brief instruction for the task agent when you want
            it to focus on a specific part of the event. Omit when the task agent
            should process the entire event.

    Returns:
        Success message with details, or error message if something went wrong.
    """
    api_url = LANGGRAPH_API_URL

    if not thread_id:
        return "Error: thread_id is required. Use 'new' or an existing thread UUID."

    # Extract event content from messages (raw XML)
    event_content = _extract_event_content(state) if state else None
    if not event_content:
        return "Error: Could not extract event content from messages."

    # Build user message: instruction (if provided) + event XML
    if task_instruction:
        user_message = f"{task_instruction}\n\n{event_content}"
    else:
        user_message = event_content

    # Execute routing
    try:
        target_thread_id = thread_id

        if thread_id == "new":
            # Create new thread
            response = httpx.post(
                f"{api_url}/threads",
                headers={"Content-Type": "application/json"},
                json={},
                timeout=30,
            )
            response.raise_for_status()
            target_thread_id = response.json()["thread_id"]

        # Create run on thread with user message
        response = httpx.post(
            f"{api_url}/threads/{target_thread_id}/runs",
            headers={"Content-Type": "application/json"},
            json={
                "assistant_id": "task_agent",
                "input": {"messages": [{"role": "user", "content": user_message}]},
                "stream_resumable": True,
            },
            timeout=30,
        )
        response.raise_for_status()

        if thread_id == "new":
            return f"Success: Created new thread {target_thread_id} and started task agent."
        else:
            return f"Success: Routed to existing thread {target_thread_id} and started task agent."

    except httpx.ConnectError as e:
        return f"Error: Could not connect to LangGraph API at {api_url}. Details: {e}"
    except httpx.TimeoutException:
        return f"Error: Request timed out. The API at {api_url} may be slow or unavailable."
    except httpx.HTTPStatusError as e:
        return f"Error: API returned status {e.response.status_code}. Details: {e.response.text}"
    except Exception as e:
        return f"Error: Unexpected error during routing: {e}"


NEXTJS_API_URL = os.getenv("NEXTJS_API_URL", "http://localhost:3000")


@tool
def view_image(
    filepath: str,
    detail: Literal["high", "low", "auto"] = "high",
    state: Annotated[dict, InjectedState] = None,
) -> list[dict]:
    """View and analyze an image file.

    Call this tool when you need to visually examine an image to understand its
    contents, extract information, or answer questions about it. The image will
    be processed and returned for your visual analysis.

    Args:
        filepath: Path to the image file (e.g., "uploads/screenshot.png").
        detail: Level of detail for analysis. Use "high" for detailed analysis
                of complex images, "low" for simple/quick viewing, "auto" to
                let the system decide.

    Returns:
        Image content block that you can analyze visually.
    """
    if state is None:
        return [{"type": "text", "text": "Error: Could not access state."}]

    thread_id = state.get("thread_id")
    if thread_id is None:
        return [{"type": "text", "text": "Error: Thread ID not available."}]

    # Normalize filepath to relative path (strip /threads/{id}/ prefix if present)
    normalized_path = filepath
    if filepath.startswith("/threads/"):
        parts = filepath.split("/", 3)  # ['', 'threads', 'id', 'uploads/file.png']
        if len(parts) >= 4:
            normalized_path = parts[3]
    elif filepath.startswith("threads/"):
        parts = filepath.split("/", 2)  # ['threads', 'id', 'uploads/file.png']
        if len(parts) >= 3:
            normalized_path = parts[2]

    try:
        # Call the Next.js API to get image base64
        response = httpx.get(
            f"{NEXTJS_API_URL}/api/images/base64",
            params={
                "thread_id": thread_id,
                "path": normalized_path,
                "detail": detail,
            },
            timeout=30,
        )
        response.raise_for_status()

        data = response.json()
        b64_data = data.get("base64")
        mime_type = data.get("mime", "image/png")

        if not b64_data:
            return [{"type": "text", "text": "Error: Failed to get image"}]

        # Return in OpenAI vision format
        return [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{b64_data}",
                    "detail": detail,
                },
            }
        ]

    except httpx.HTTPStatusError as e:
        error_detail = e.response.text
        return [{"type": "text", "text": f"Error processing image: {error_detail}"}]
    except Exception as e:
        return [{"type": "text", "text": f"Error viewing image: {e}"}]
