"""Modal sandbox middleware for managing sandbox lifecycle."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, NotRequired

import modal
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.runnables.config import var_child_runnable_config
from langgraph.runtime import Runtime

from agent.utils import fetch_composio_gdrive_token

modal.enable_output()

# Modal image with rclone, document processing tools, and skill dependencies
rclone_image = (
    modal.Image.debian_slim(python_version="3.11")
    # System packages for document processing and utilities
    .apt_install(
        # Base utilities
        "curl",
        "unzip",
        "jq",
        "ripgrep",
        # Document conversion and processing
        "pandoc",
        "libreoffice",
        # PDF tools (poppler-utils)
        "poppler-utils",
        # PDF manipulation CLI
        "qpdf",
        # OCR support
        "tesseract-ocr",
        # Virtual framebuffer for headless LibreOffice
        "xvfb",
        # Node.js for pptx/docx JavaScript libraries
        "nodejs",
        "npm",
    )
    # Python packages for document processing skills
    .pip_install(
        # PDF processing
        "pypdf",
        "pdfplumber",
        "reportlab",
        "pdf2image",
        "pytesseract",
        "pypdfium2",
        # Image processing
        "Pillow",
        # Data analysis
        "pandas",
        # Excel processing
        "openpyxl",
        # PowerPoint processing
        "python-pptx",
        # Secure XML parsing for OOXML
        "defusedxml",
        "lxml",
        # Python 2/3 compatibility (used by pptx rearrange.py)
        "six",
        # Text extraction from presentations
        "markitdown[pptx]",
    )
    # Node.js global packages for presentation/document creation
    .run_commands(
        # Install Node.js packages globally
        "npm install -g pptxgenjs playwright react-icons react react-dom docx",
        # Install Playwright browsers (chromium for HTML rendering)
        "npx playwright install chromium",
        "npx playwright install-deps chromium",
    )
    # Install sharp globally (dependency for html2pptx, which agent extracts locally per skill instructions)
    .run_commands(
        "npm install -g sharp",
    )
    # Install rclone for Google Drive sync
    .run_commands(
        "curl -O https://downloads.rclone.org/rclone-current-linux-amd64.zip",
        "unzip -q rclone-current-linux-amd64.zip",
        "cp rclone-*-linux-amd64/rclone /usr/local/bin/",
        "chmod 755 /usr/local/bin/rclone",
        "rm -rf rclone-*",
        "rclone version",
    )
)


class ModalSandboxState(AgentState):
    """Extended state schema with Modal sandbox ID and Google Drive access."""
    modal_sandbox_id: NotRequired[str]
    thread_id: NotRequired[str]
    modal_snapshot_id: NotRequired[str]
    gdrive_access_token: NotRequired[str]


class ModalSandboxMiddleware(AgentMiddleware[ModalSandboxState, Any]):
    """Middleware that manages Modal sandbox lifecycle per thread.

    Creates a new Modal sandbox when a thread starts and relies on Modal's
    idle_timeout to automatically terminate inactive sandboxes.
    """

    state_schema = ModalSandboxState

    def __init__(
        self,
        workdir: str = "/workspace",
        startup_timeout: int = 180,
        idle_timeout: int = 60 * 3,  # 3 minutes
        max_timeout: int = 60 * 60 * 24,   # 24 hours
        volume_name: str = "threads",
        memory_volume_name: str = "memories",
        skills_volume_name: str = "skills",
    ):
        super().__init__()
        self._workdir = workdir
        self._startup_timeout = startup_timeout
        self._idle_timeout = idle_timeout
        self._max_timeout = max_timeout
        self._volume_name = volume_name
        self._memory_volume_name = memory_volume_name
        self._skills_volume_name = skills_volume_name

    def before_agent(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Create or reconnect to Modal sandbox for this thread."""
        import modal

        existing_sandbox_id = state.get("modal_sandbox_id")

        if existing_sandbox_id:
            try:
                sandbox = modal.Sandbox.from_id(existing_sandbox_id)

                # Verify sandbox is alive
                try:
                    process = sandbox.exec("echo", "alive", timeout=5)
                    process.wait()
                    if process.returncode == 0:
                        return None  # Reuse existing
                except Exception:
                    pass
            except Exception:
                pass

        # Get thread_id from config, state, or generate new
        # Must determine thread_id BEFORE creating sandbox to set workdir
        thread_id = state.get("thread_id")
        if not thread_id:
            # Try to get from RunnableConfig context var
            config = var_child_runnable_config.get()
            if config:
                thread_id = config.get("configurable", {}).get("thread_id")
        if not thread_id and runtime.context and hasattr(runtime.context, 'thread_id'):
            thread_id = runtime.context.thread_id
        if not thread_id:
            thread_id = str(uuid.uuid4())

        # Get or create v2 volumes for persistent storage
        thread_volume = modal.Volume.from_name(
            self._volume_name,
            create_if_missing=True,
            version=2
        )
        memory_volume = modal.Volume.from_name(
            self._memory_volume_name,
            create_if_missing=True,
            version=2
        )
        skills_volume = modal.Volume.from_name(
            self._skills_volume_name,
            create_if_missing=True,
            version=2,
        )

        # Check if we should restore from a snapshot
        snapshot_id = state.get("modal_snapshot_id")
        image = None
        if snapshot_id:
            try:
                image = modal.Image.from_id(snapshot_id)
            except Exception:
                # Snapshot not found or expired, proceed without it
                pass

        # Use rclone image if no snapshot to restore
        if image is None:
            image = rclone_image

        # Fetch Google Drive token from Composio
        gdrive_access_token = None
        try:
            gdrive_env = fetch_composio_gdrive_token()
            # Extract access token from the token JSON for API calls
            token_json_str = gdrive_env.get("RCLONE_CONFIG_GDRIVE_TOKEN", "{}")
            token_data = json.loads(token_json_str)
            gdrive_access_token = token_data.get("access_token")
        except Exception as e:
            # Log error but continue without Google Drive access
            print(f"Warning: Could not fetch Google Drive token: {e}")
            gdrive_env = {}

        # Add NODE_PATH so Node.js can find globally installed npm packages
        sandbox_env = {
            "NODE_PATH": "/usr/local/lib/node_modules",
            **gdrive_env,
        }

        # Create new sandbox with volumes mounted and workdir set to workspace
        app = modal.App.lookup("agent-sandbox", create_if_missing=True)
        sandbox = modal.Sandbox.create(
            app=app,
            image=image,
            workdir="/workspace",
            timeout=self._max_timeout,
            idle_timeout=self._idle_timeout,
            volumes={
                "/threads": thread_volume,
                "/memories": memory_volume,
                "/skills": skills_volume,
            },
            env=sandbox_env,
            verbose=True,
        )

        # Poll until ready
        for _ in range(self._startup_timeout // 2):
            if sandbox.poll() is not None:
                raise RuntimeError("Modal sandbox terminated during startup")
            try:
                process = sandbox.exec("echo", "ready", timeout=5)
                process.wait()
                if process.returncode == 0:
                    break
            except Exception:
                pass
            time.sleep(2)
        else:
            sandbox.terminate()
            raise RuntimeError(
                f"Modal sandbox failed to start within {self._startup_timeout}s")

        # Create thread subfolder if it doesn't exist (safety measure)
        # The volume is mounted at /threads, so mkdir ensures the subdirectory exists
        process = sandbox.exec(
            "mkdir", "-p", f"/threads/{thread_id}", timeout=10)
        process.wait()

        state_updates = {
            "modal_sandbox_id": sandbox.object_id,
            "thread_id": thread_id,
        }

        # Add access token to state if available
        if gdrive_access_token:
            state_updates["gdrive_access_token"] = gdrive_access_token

        return state_updates

    async def abefore_agent(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync implementation."""
        return self.before_agent(state, runtime)

    def after_agent(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Create filesystem snapshot to preserve /workspace state."""
        import modal

        sandbox_id = state.get("modal_sandbox_id")
        if not sandbox_id:
            return None

        try:
            # Reconnect to sandbox
            sandbox = modal.Sandbox.from_id(sandbox_id)

            # Create filesystem snapshot
            snapshot = sandbox.snapshot_filesystem(timeout=55)

            return {
                "modal_snapshot_id": snapshot.object_id,
            }
        except Exception:
            # If snapshotting fails, continue without updating snapshot
            return None

    async def aafter_agent(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync implementation."""
        return self.after_agent(state, runtime)
