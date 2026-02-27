"""
Modal file service for file operations across volumes.
Provides functions to interact with files stored in Modal Volumes.
"""

import modal
from typing import Optional

# Create Modal image with FastAPI for web endpoints
image = modal.Image.debian_slim().pip_install("fastapi[standard]")

# Create Modal app
app = modal.App("file-service", image=image)

# Create or reference user volume
user_volume = modal.Volume.from_name("user-default-user", create_if_missing=True, version=2)

# Keep 'volume' as alias for backwards compatibility
volume = user_volume


@app.function(volumes={"/default-user": volume})
def list_files(session_id: str) -> dict:
    """
    List all files in a session's folder.

    Args:
        session_id: Session ID

    Returns:
        Dictionary with 'files' key containing list of file metadata
    """
    try:
        # Reload volume to get latest changes
        volume.reload()

        # List files in the session folder
        path = f"/session-storage/{session_id}"
        files = volume.listdir(path, recursive=True)

        # Convert to serializable format
        file_list = []
        for f in files:
            # Remove session-storage prefix
            file_path = f.path
            if file_path.startswith(f"/session-storage/{session_id}/"):
                file_path = file_path.replace(f"/session-storage/{session_id}/", "", 1)
            elif file_path.startswith(f"session-storage/{session_id}/"):
                file_path = file_path.replace(f"session-storage/{session_id}/", "", 1)

            file_list.append({
                "path": file_path,
                "size": f.size,
                "type": f.type,
                "mtime": getattr(f, 'mtime', None)
            })

        return {"files": file_list}

    except FileNotFoundError:
        # Session folder doesn't exist yet
        return {"files": []}
    except Exception as e:
        print(f"Error listing files for session {session_id}: {e}")
        raise


@app.function(volumes={"/default-user": volume})
def read_file(session_id: str, file_path: str) -> dict:
    """
    Read file content from a session's folder.

    Args:
        session_id: Session ID
        file_path: Path to file relative to session folder

    Returns:
        Dictionary with 'content' key containing file content as string
    """
    try:
        # Reload volume to get latest changes
        volume.reload()

        # Read the file
        full_path = f"/session-storage/{session_id}/{file_path}"
        content_bytes = b"".join(volume.read_file(full_path))

        # Decode with error handling for non-text files
        try:
            content = content_bytes.decode('utf-8')
        except UnicodeDecodeError:
            content = content_bytes.decode('utf-8', errors='replace')

        return {"content": content}

    except FileNotFoundError:
        raise FileNotFoundError(f"File not found: {file_path}")
    except Exception as e:
        print(f"Error reading file {file_path} for session {session_id}: {e}")
        raise


@app.function(volumes={"/default-user": volume})
def read_file_bytes(session_id: str, file_path: str) -> dict:
    """
    Read file content as base64-encoded bytes from a session's folder.
    Used for binary files like docx, xlsx, pptx, pdf.

    Args:
        session_id: Session ID
        file_path: Path to file relative to session folder

    Returns:
        Dictionary with 'content_b64' (base64 encoded content) and 'mime' (MIME type)
    """
    import base64
    import mimetypes

    try:
        # Reload volume to get latest changes
        volume.reload()

        # Read the file as bytes
        full_path = f"/session-storage/{session_id}/{file_path}"
        content_bytes = b"".join(volume.read_file(full_path))

        # Encode as base64
        content_b64 = base64.b64encode(content_bytes).decode('ascii')

        # Determine MIME type
        mime_type, _ = mimetypes.guess_type(file_path)
        if mime_type is None:
            mime_type = "application/octet-stream"

        return {"content_b64": content_b64, "mime": mime_type}

    except FileNotFoundError:
        raise FileNotFoundError(f"File not found: {file_path}")
    except Exception as e:
        print(f"Error reading file bytes {file_path} for session {session_id}: {e}")
        raise


@app.function(volumes={"/default-user": volume})
def update_file(session_id: str, file_path: str, content: str) -> dict:
    """
    Update or create a file in a session's folder.

    Args:
        session_id: Session ID
        file_path: Path to file relative to session folder
        content: File content as string

    Returns:
        Dictionary with 'success' boolean
    """
    try:
        import io

        # Upload the file
        full_path = f"/session-storage/{session_id}/{file_path}"
        with volume.batch_upload(force=True) as batch:
            batch.put_file(
                io.BytesIO(content.encode('utf-8')),
                full_path
            )

        return {"success": True}

    except Exception as e:
        print(f"Error updating file {file_path} for session {session_id}: {e}")
        return {"success": False, "error": str(e)}


@app.function(volumes={"/default-user": volume})
def upload_file_bytes(session_id: str, filename: str, content_b64: str) -> dict:
    """
    Upload a binary file to a session's uploads folder.

    Args:
        session_id: Session ID
        filename: Name of the file to create
        content_b64: Base64-encoded file content

    Returns:
        Dictionary with 'success' boolean and file metadata
    """
    import base64
    import io
    import mimetypes

    try:
        # Decode base64 content
        content_bytes = base64.b64decode(content_b64)

        # Upload to uploads subfolder
        full_path = f"/session-storage/{session_id}/uploads/{filename}"
        with volume.batch_upload(force=True) as batch:
            batch.put_file(
                io.BytesIO(content_bytes),
                full_path
            )

        # Determine MIME type
        mime_type, _ = mimetypes.guess_type(filename)
        if mime_type is None:
            mime_type = "application/octet-stream"

        return {
            "success": True,
            "path": f"uploads/{filename}",
            "size": len(content_bytes),
            "mimeType": mime_type,
        }

    except Exception as e:
        print(f"Error uploading file {filename} for session {session_id}: {e}")
        return {"success": False, "error": str(e)}


@app.function(volumes={"/default-user": volume})
def upload_temp_file(temp_id: str, filename: str, content_b64: str) -> dict:
    """
    Upload a binary file to temporary uploads staging area.
    Files here will be moved to the actual session folder by middleware when the run starts.

    Args:
        temp_id: Temporary session ID (client-generated UUID)
        filename: Name of the file to create
        content_b64: Base64-encoded file content

    Returns:
        Dictionary with 'success' boolean and file metadata
    """
    import base64
    import io
    import mimetypes

    try:
        # Decode base64 content
        content_bytes = base64.b64decode(content_b64)

        # Upload to temp-uploads staging folder
        full_path = f"/.temp-uploads/{temp_id}/{filename}"
        with volume.batch_upload(force=True) as batch:
            batch.put_file(
                io.BytesIO(content_bytes),
                full_path
            )

        # Determine MIME type
        mime_type, _ = mimetypes.guess_type(filename)
        if mime_type is None:
            mime_type = "application/octet-stream"

        return {
            "success": True,
            "path": f".temp-uploads/{temp_id}/{filename}",
            "size": len(content_bytes),
            "mimeType": mime_type,
        }

    except Exception as e:
        print(f"Error uploading temp file {filename} for temp_id {temp_id}: {e}")
        return {"success": False, "error": str(e)}


@app.function(volumes={"/default-user": volume})
def delete_file(session_id: str, file_path: str, sandbox_id: str = None) -> dict:
    """
    Delete a file from a session's folder.

    Uses Modal Sandbox to delete the file since Volume API doesn't have native delete.
    If no sandbox_id is provided, creates a temporary sandbox for deletion.

    Args:
        session_id: Session ID
        file_path: Path to file relative to session folder
        sandbox_id: Optional Modal sandbox ID to use for deletion

    Returns:
        Dictionary with 'success' boolean and optional 'error'
    """
    try:
        full_path = f"/default-user/session-storage/{session_id}/{file_path}"

        if sandbox_id:
            # Use existing sandbox
            try:
                sb = modal.Sandbox.from_id(sandbox_id)
            except Exception:
                # If sandbox doesn't exist, create temporary one
                sb = modal.Sandbox.create(
                    image=modal.Image.debian_slim(),
                    volumes={"/default-user": volume},
                    timeout=60
                )
        else:
            # Create temporary sandbox for deletion
            sb = modal.Sandbox.create(
                image=modal.Image.debian_slim(),
                volumes={"/default-user": volume},
                timeout=60
            )

        # Delete the file using rm command
        process = sb.exec("rm", "-f", full_path, timeout=10)
        process.wait()

        if process.returncode == 0:
            # Sync volume from within sandbox to persist deletion
            sync_process = sb.exec("sync", "/default-user", timeout=30)
            sync_process.wait()

            # Terminate temporary sandbox if we created one
            if not sandbox_id:
                sb.terminate()

            return {"success": True}
        else:
            error_msg = process.stderr.read() if process.stderr else "Unknown error"
            print(f"Error deleting file {file_path}: {error_msg}")

            # Terminate temporary sandbox if we created one
            if not sandbox_id:
                sb.terminate()

            return {
                "success": False,
                "error": f"Failed to delete file: {error_msg}"
            }

    except Exception as e:
        print(f"Error deleting file {file_path} for session {session_id}: {e}")
        return {
            "success": False,
            "error": str(e)
        }


@app.function()
def get_sandbox_files(session_id: str, sandbox_id: str) -> dict:
    """
    Get files from an active sandbox (alternative to Volume-based access).

    Args:
        session_id: Session ID
        sandbox_id: Modal sandbox ID

    Returns:
        Dictionary with file listings from sandbox
    """
    try:
        # Connect to the sandbox
        sb = modal.Sandbox.from_id(sandbox_id)

        # List files using sandbox ls command
        process = sb.exec("ls", "-la", f"/default-user/session-storage/{session_id}", timeout=10)
        process.wait()

        if process.returncode == 0:
            return {
                "success": True,
                "output": process.stdout.read()
            }
        else:
            return {
                "success": False,
                "error": process.stderr.read()
            }

    except Exception as e:
        print(f"Error accessing sandbox {sandbox_id} for session {session_id}: {e}")
        return {
            "success": False,
            "error": str(e)
        }


# Web endpoint for health check
@app.function()
@modal.fastapi_endpoint(method="GET")
def health():
    """Health check endpoint"""
    return {"status": "healthy", "service": "file-service"}


# ==================== TRIAGE OPERATIONS ====================


@app.function(volumes={"/default-user": volume})
def read_triage_rules() -> str | None:
    """
    Read triage rules from the user memory directory.

    Returns:
        Triage rules content as string, or None if file not found
    """
    try:
        volume.reload()

        with open("/default-user/memory/triage.md", "r") as f:
            return f.read()

    except FileNotFoundError:
        print("Triage rules file not found: /default-user/memory/triage.md")
        return None
    except Exception as e:
        print(f"Error reading triage rules: {e}")
        return None
