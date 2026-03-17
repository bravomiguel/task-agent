"""Modal sandbox middleware for managing sandbox lifecycle."""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Callable, NotRequired

import modal
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langchain_core.runnables.config import var_child_runnable_config
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime
from langgraph.types import Command

logger = logging.getLogger(__name__)


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
        "npm install -g pptxgenjs react-icons react react-dom docx sharp",
    )
    # Browser automation: agent-browser CLI + Kernel CLI (for stealth/headed escalation)
    # agent-browser bundles playwright-core; its install command downloads matching Chromium
    .run_commands(
        "npm install -g agent-browser @onkernel/cli @onkernel/sdk",
        "agent-browser install --with-deps",
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
    # Install gog CLI for Google Workspace (Gmail, Calendar, Drive, Sheets, Docs)
    # Fork with GOG_ACCESS_TOKEN env var support for Composio-managed auth
    .run_commands(
        "curl -fsSL https://github.com/bravomiguel/gogcli/releases/download/v0.11.0-access-token/gogcli_0.11.0-access-token_linux_amd64.tar.gz"
        " | tar -xz -C /usr/local/bin gog",
        "chmod 755 /usr/local/bin/gog",
    )
    # Install GitHub CLI (gh)
    .run_commands(
        "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg"
        " -o /usr/share/keyrings/githubcli-archive-keyring.gpg",
        'echo "deb [arch=amd64 signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg]'
        ' https://cli.github.com/packages stable main"'
        " > /etc/apt/sources.list.d/github-cli.list",
        "apt-get update -qq && apt-get install -y -qq gh",
    )
    # Install Gemini CLI (Node.js-based) and pre-configure for API key auth
    # so it never prompts interactively. GEMINI_API_KEY env var is injected
    # at sandbox creation via Modal Secret.
    .run_commands(
        "npm install -g @google/gemini-cli",
        'mkdir -p /root/.gemini && echo \'{"security":{"auth":{"selectedType":"api-key"}}}\' > /root/.gemini/settings.json',
    )
)

# Platform-provided API keys (OPENAI_API_KEY, GEMINI_API_KEY, etc.)
# Stored as a Modal Secret, injected as env vars at sandbox creation.
platform_keys = modal.Secret.from_name("agent-platform-keys")


class ModalSandboxState(AgentState):
    """Extended state schema with Modal sandbox ID."""
    modal_sandbox_id: NotRequired[str]
    session_id: NotRequired[str]
    modal_snapshot_id: NotRequired[str]
    _skip_volume_reload: NotRequired[bool]


class ModalSandboxMiddleware(AgentMiddleware[ModalSandboxState, Any]):
    """Middleware that manages Modal sandbox lifecycle per session.

    Creates a new Modal sandbox when a session starts and relies on Modal's
    idle_timeout to automatically terminate inactive sandboxes.
    """

    state_schema = ModalSandboxState

    def __init__(
        self,
        workdir: str = "/workspace",
        startup_timeout: int = 180,
        idle_timeout: int = 60,  # 1 minute (wrap_tool_call auto-recovers dead sandboxes)
        max_timeout: int = 60 * 60 * 24,   # 24 hours
        user_volume_name: str = "user-dev",
    ):
        super().__init__()
        self._workdir = workdir
        self._startup_timeout = startup_timeout
        self._idle_timeout = idle_timeout
        self._max_timeout = max_timeout
        self._user_volume_name = user_volume_name

    def before_agent(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Create or reconnect to Modal sandbox for this session."""
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
                        sandbox.reload_volumes()
                        return None  # Reuse existing
                except Exception:
                    pass
            except Exception:
                pass

        # Get session_id from config, state, or generate new
        # Must determine session_id BEFORE creating sandbox to set workdir
        session_id = state.get("session_id")
        if not session_id:
            # Try to get from RunnableConfig context var (LangGraph uses "thread_id")
            config = var_child_runnable_config.get()
            if config:
                session_id = config.get("configurable", {}).get("thread_id")
        if not session_id and runtime.context and hasattr(runtime.context, 'thread_id'):
            session_id = runtime.context.thread_id
        if not session_id:
            session_id = str(uuid.uuid4())

        # Get or create v2 user volume for persistent storage
        user_volume = modal.Volume.from_name(
            self._user_volume_name,
            create_if_missing=True,
            version=2
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

        # Add NODE_PATH so Node.js can find globally installed npm packages
        sandbox_env = {
            "NODE_PATH": "/usr/local/lib/node_modules",
            # agent-browser: headless by default, no-sandbox for container
            "AGENT_BROWSER_HEADLESS": "true",
            "AGENT_BROWSER_NO_SANDBOX": "1",
        }

        # Kernel API key for browser stealth/headed escalation (optional)
        kernel_api_key = os.environ.get("KERNEL_API_KEY")
        if kernel_api_key:
            sandbox_env["KERNEL_API_KEY"] = kernel_api_key

        # Composio API key for fetch_auth.py script (fetches OAuth tokens)
        composio_api_key = os.environ.get("COMPOSIO_API_KEY")
        if composio_api_key:
            sandbox_env["COMPOSIO_API_KEY"] = composio_api_key
        composio_entity_id = os.environ.get("COMPOSIO_ENTITY_ID")
        if composio_entity_id:
            sandbox_env["COMPOSIO_ENTITY_ID"] = composio_entity_id

        # Create new sandbox with user volume mounted and workdir set to workspace
        app = modal.App.lookup("agent-sandbox", create_if_missing=True)
        sandbox = modal.Sandbox.create(
            app=app,
            image=image,
            secrets=[platform_keys],
            workdir="/workspace",
            timeout=self._max_timeout,
            idle_timeout=self._idle_timeout,
            volumes={
                "/mnt": user_volume,
            },
            env=sandbox_env,
            verbose=True,
        )

        # Poll until sandbox is running
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

        # Create per-session directories (volume-level dirs created by reset script)
        sandbox.exec(
            "mkdir", "-p",
            f"/mnt/session-storage/{session_id}/workspace",
            f"/mnt/session-storage/{session_id}/uploads",
            f"/mnt/session-storage/{session_id}/outputs",
            timeout=10,
        ).wait()

        # NOTE: intentionally not calling reload_volumes() here.
        # The volume is already mounted with latest committed state at sandbox
        # creation. Reloading after mkdir causes the volume to appear empty
        # while the reload is in progress, leading to first-read failures.
        # Volume readiness is verified by SessionSetupMiddleware when loading
        # prompt files and skills (with expected-count retries).

        return {
            "modal_sandbox_id": sandbox.object_id,
            "session_id": session_id,
            # Skip volume reloads during the first model turn to avoid a race
            # condition where reload_volumes() causes the volume to appear empty
            # on a cold sandbox. Cleared by after_model once the cache is warm.
            "_skip_volume_reload": True,
        }

    async def abefore_agent(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync implementation."""
        return self.before_agent(state, runtime)

    def before_model(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Clear the skip-reload flag on the second model call.

        The first model call produces tool calls that should skip
        reload_volumes(). By the second model call, those tools have
        finished and the volume cache is warm, so we clear the flag.
        We detect the second call by checking for tool result messages.
        """
        if state.get("_skip_volume_reload"):
            messages = state.get("messages", [])
            has_tool_results = any(
                getattr(m, "type", None) == "tool" for m in messages
            )
            if has_tool_results:
                return {"_skip_volume_reload": False}
        return None

    async def abefore_model(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync implementation."""
        return self.before_model(state, runtime)

    def _is_sandbox_dead_error(self, exc: Exception) -> bool:
        """Check if an exception indicates a dead Modal sandbox."""
        msg = str(exc).lower()
        return "container id" in msg and ("finished" in msg or "not found" in msg)

    def _recover_sandbox(self, state: dict) -> str | None:
        """Spin up a new sandbox with snapshot restore. Returns new sandbox ID."""
        try:
            session_id = state.get("session_id")
            if not session_id:
                return None

            logger.info("[ModalSandbox] recovering dead sandbox for session %s", session_id)

            # Restore from snapshot if available
            snapshot_id = state.get("modal_snapshot_id")
            image = None
            if snapshot_id:
                try:
                    image = modal.Image.from_id(snapshot_id)
                except Exception:
                    pass
            if image is None:
                image = rclone_image

            user_volume = modal.Volume.from_name(
                self._user_volume_name, create_if_missing=True, version=2
            )

            sandbox_env = {
                "NODE_PATH": "/usr/local/lib/node_modules",
                "AGENT_BROWSER_HEADLESS": "true",
                "AGENT_BROWSER_NO_SANDBOX": "1",
            }
            for key in ("KERNEL_API_KEY", "COMPOSIO_API_KEY", "COMPOSIO_ENTITY_ID"):
                val = os.environ.get(key)
                if val:
                    sandbox_env[key] = val

            app = modal.App.lookup("agent-sandbox", create_if_missing=True)
            sandbox = modal.Sandbox.create(
                app=app,
                image=image,
                secrets=[platform_keys],
                workdir="/workspace",
                timeout=self._max_timeout,
                idle_timeout=self._idle_timeout,
                volumes={"/mnt": user_volume},
                env=sandbox_env,
                verbose=True,
            )

            # Wait for ready
            for _ in range(30):
                if sandbox.poll() is not None:
                    return None
                try:
                    process = sandbox.exec("echo", "ready", timeout=5)
                    process.wait()
                    if process.returncode == 0:
                        break
                except Exception:
                    pass
                time.sleep(2)
            else:
                return None

            logger.info("[ModalSandbox] recovered → %s", sandbox.object_id)
            return sandbox.object_id

        except Exception as exc:
            logger.warning("[ModalSandbox] recovery failed: %s", exc)
            return None

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        """Intercept tool calls — auto-recover if sandbox died."""
        try:
            return handler(request)
        except Exception as exc:
            if not self._is_sandbox_dead_error(exc):
                raise

            # Sandbox died — spin up a new one and retry
            new_id = self._recover_sandbox(request.state)
            if new_id:
                request.state["modal_sandbox_id"] = new_id
                return handler(request)

            # Recovery failed — return error to agent
            tool_call = request.tool_call
            return ToolMessage(
                content=f"Sandbox died and recovery failed: {exc}",
                name=tool_call["name"],
                tool_call_id=tool_call["id"],
                status="error",
            )

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable,
    ) -> ToolMessage | Command:
        """Async version delegates to sync."""
        return self.wrap_tool_call(request, lambda r: handler(r))

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
