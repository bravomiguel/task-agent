"""Lazy Modal backend with volume commit/reload support."""

import os

from deepagents_cli.integrations.modal import ModalBackend
from deepagents.backends.protocol import SandboxBackendProtocol

# Token files the agent must not touch directly via file tools.
# Only fetch_auth.py (run via execute) should write these.
_AUTH_DIR = "/mnt/auth/"
_AUTH_SCRIPT = "fetch_auth.py"


def _is_auth_token(path: str) -> bool:
    """Return True if path points to an auth token file (not the script)."""
    if not path.startswith(_AUTH_DIR):
        return False
    basename = os.path.basename(path)
    return basename != _AUTH_SCRIPT


class LazyModalBackend(SandboxBackendProtocol):
    """A lazy wrapper that defers ModalBackend creation until first use.

    This is needed because FilesystemMiddleware calls the backend factory
    from wrap_model_call with Runtime (no state), but we need state to get
    sandbox_id. The actual backend is created lazily when methods are called
    from tool context (ToolRuntime with state).
    """

    def __init__(self, runtime):
        self._runtime = runtime
        self._backend = None
        self._sandbox = None

    def _get_sandbox(self):
        if self._sandbox is None:
            import modal
            if not hasattr(self._runtime, 'state'):
                raise RuntimeError("Cannot access backend - no state available in runtime context")
            sandbox_id = self._runtime.state.get("modal_sandbox_id")
            if not sandbox_id:
                raise RuntimeError("Modal sandbox not initialized")
            self._sandbox = modal.Sandbox.from_id(sandbox_id)
        return self._sandbox

    def _get_backend(self):
        if self._backend is None:
            self._backend = ModalBackend(self._get_sandbox())
        return self._backend

    def _reload_volumes(self):
        """Reload all mounted volumes in the sandbox to see latest changes.

        Skips reload when ``_skip_volume_reload`` is set in agent state.
        This avoids a race condition on cold sandboxes where
        reload_volumes() causes the volume to appear empty while the
        remount propagates.  The flag is set by ModalSandboxMiddleware on
        new sandbox creation and cleared after the first model turn.
        """
        if hasattr(self._runtime, 'state') and self._runtime.state.get("_skip_volume_reload"):
            return
        self._get_sandbox().reload_volumes()

    def _sync_volume(self, mount_path: str):
        """Sync a volume to persist changes for other processes.

        Uses the `sync` command inside the sandbox which forces an immediate
        commit of all pending writes to the distributed storage.
        """
        process = self._get_sandbox().exec("sync", mount_path, timeout=30)
        process.wait()

    # Implement SandboxBackendProtocol methods by delegation
    def ls_info(self, path):
        # Reload before listing /mnt/ to see all files
        if path.startswith("/mnt"):
            self._reload_volumes()
        return self._get_backend().ls_info(path)

    def read(self, file_path, offset=0, limit=2000):
        if _is_auth_token(file_path):
            raise PermissionError(
                f"Access denied: {file_path} is a protected auth token. "
                "Use `python3 /mnt/auth/fetch_auth.py <service>` to refresh tokens."
            )
        if file_path.startswith("/mnt"):
            self._reload_volumes()
        return self._get_backend().read(file_path, offset, limit)

    def write(self, file_path, content):
        if _is_auth_token(file_path):
            raise PermissionError(
                f"Access denied: {file_path} is a protected auth token. "
                "Use `python3 /mnt/auth/fetch_auth.py <service>` to refresh tokens."
            )
        result = self._get_backend().write(file_path, content)
        # Sync volume to persist changes immediately for other processes
        if file_path.startswith("/mnt"):
            self._sync_volume("/mnt")
        return result

    def edit(self, file_path, old_string, new_string, replace_all=False):
        if _is_auth_token(file_path):
            raise PermissionError(
                f"Access denied: {file_path} is a protected auth token. "
                "Use `python3 /mnt/auth/fetch_auth.py <service>` to refresh tokens."
            )
        result = self._get_backend().edit(file_path, old_string, new_string, replace_all)
        # Sync volume to persist changes immediately for other processes
        if file_path.startswith("/mnt"):
            self._sync_volume("/mnt")
        return result

    def grep_raw(self, pattern, path=None, glob=None):
        # Reload before searching in /mnt/ to see all files
        if path and path.startswith("/mnt"):
            self._reload_volumes()

        # Custom grep with better flags (copied from BaseSandbox, modified)
        search_path = path or "."

        # Build grep command with enhanced flags:
        # -r: recursive, -H: with filename, -n: with line number
        # -E: extended regex (| works without escaping)
        # -i: case insensitive
        # -I: skip binary files
        # -s: suppress error messages
        grep_opts = "-rHnEiIs"

        # Add glob pattern if specified
        glob_pattern = ""
        if glob:
            glob_pattern = f"--include='{glob}'"

        # Escape pattern for shell
        pattern_escaped = pattern.replace("'", "'\\''")

        cmd = f"grep {grep_opts} {glob_pattern} -e '{pattern_escaped}' '{search_path}' 2>/dev/null || true"
        result = self._get_backend().execute(cmd)

        output = result.output.rstrip()
        if not output:
            return []

        # Parse grep output into GrepMatch objects
        matches = []
        for line in output.split("\n"):
            # Format is: path:line_number:text
            parts = line.split(":", 2)
            if len(parts) >= 3:
                matches.append({
                    "path": parts[0],
                    "line": int(parts[1]),
                    "text": parts[2],
                })

        return matches

    def glob_info(self, pattern, path="/"):
        # Reload before globbing in /mnt/ to see all files
        if path.startswith("/mnt"):
            self._reload_volumes()
        return self._get_backend().glob_info(pattern, path)

    def execute(self, command):
        # Reload volumes before execute if command references them (might read)
        if "/mnt" in command:
            self._reload_volumes()

        result = self._get_backend().execute(command)

        # Sync volumes after execute if command references them (might write)
        if "/mnt" in command:
            self._sync_volume("/mnt")

        return result

    @property
    def id(self):
        # Return a placeholder if backend not yet created
        # This allows isinstance() checks to pass without triggering backend creation
        if self._backend is None:
            return "lazy-modal-backend"
        return self._backend.id
