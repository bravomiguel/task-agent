"""Lazy Modal backend with volume commit/reload support."""

from deepagents_cli.integrations.modal import ModalBackend
from deepagents.backends.protocol import SandboxBackendProtocol


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
        """Reload all mounted volumes in the sandbox to see latest changes."""
        self._get_sandbox().reload_volumes()

    # Implement SandboxBackendProtocol methods by delegation
    def ls_info(self, path):
        # Reload before listing /threads/ or /memories/ to see all files
        if path.startswith("/threads") or path.startswith("/memories"):
            self._reload_volumes()
        return self._get_backend().ls_info(path)

    def read(self, file_path, offset=0, limit=2000):
        # Reload before reading from /threads/ or /memories/ to get latest from other sandboxes
        if file_path.startswith("/threads") or file_path.startswith("/memories"):
            self._reload_volumes()
        return self._get_backend().read(file_path, offset, limit)

    def write(self, file_path, content):
        # Modal runs background commits every few seconds automatically
        return self._get_backend().write(file_path, content)

    def edit(self, file_path, old_string, new_string, replace_all=False):
        # Modal runs background commits every few seconds automatically
        return self._get_backend().edit(file_path, old_string, new_string, replace_all)

    def grep_raw(self, pattern, path=None, glob=None):
        # Reload before searching in /threads/ or /memories/ to see all files
        if path and (path.startswith("/threads") or path.startswith("/memories")):
            self._reload_volumes()
        return self._get_backend().grep_raw(pattern, path, glob)

    def glob_info(self, pattern, path="/"):
        # Reload before globbing in /threads/ or /memories/ to see all files
        if path.startswith("/threads") or path.startswith("/memories"):
            self._reload_volumes()
        return self._get_backend().glob_info(pattern, path)

    def execute(self, command):
        return self._get_backend().execute(command)

    @property
    def id(self):
        # Return a placeholder if backend not yet created
        # This allows isinstance() checks to pass without triggering backend creation
        if self._backend is None:
            return "lazy-modal-backend"
        return self._backend.id
