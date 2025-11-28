"""
Modal file service for thread-specific file operations.
Provides functions to interact with files stored in Modal Volumes.
"""

import modal
from typing import Optional

# Create Modal image with FastAPI for web endpoints
image = modal.Image.debian_slim().pip_install("fastapi[standard]")

# Create Modal app
app = modal.App("file-service", image=image)

# Create or reference the threads volume
volume = modal.Volume.from_name("threads", create_if_missing=True, version=2)


@app.function(volumes={"/threads": volume})
def list_files(session_id: str) -> dict:
    """
    List all files in a thread's folder.

    Args:
        session_id: Thread/session ID

    Returns:
        Dictionary with 'files' key containing list of file metadata
    """
    try:
        # Reload volume to get latest changes
        volume.reload()

        # List files in the session folder
        path = f"/{session_id}"
        files = volume.listdir(path, recursive=True)

        # Convert to serializable format
        file_list = []
        for f in files:
            # Remove session prefix - handle both /session_id/ and session_id/
            file_path = f.path
            if file_path.startswith(f"/{session_id}/"):
                file_path = file_path.replace(f"/{session_id}/", "", 1)
            elif file_path.startswith(f"{session_id}/"):
                file_path = file_path.replace(f"{session_id}/", "", 1)

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


@app.function(volumes={"/threads": volume})
def read_file(session_id: str, file_path: str) -> dict:
    """
    Read file content from a thread's folder.

    Args:
        session_id: Thread/session ID
        file_path: Path to file relative to session folder

    Returns:
        Dictionary with 'content' key containing file content as string
    """
    try:
        # Reload volume to get latest changes
        volume.reload()

        # Read the file
        full_path = f"/{session_id}/{file_path}"
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


@app.function(volumes={"/threads": volume})
def update_file(session_id: str, file_path: str, content: str) -> dict:
    """
    Update or create a file in a thread's folder.

    Args:
        session_id: Thread/session ID
        file_path: Path to file relative to session folder
        content: File content as string

    Returns:
        Dictionary with 'success' boolean
    """
    try:
        import io

        # Upload the file
        full_path = f"/{session_id}/{file_path}"
        with volume.batch_upload(force=True) as batch:
            batch.put_file(
                io.BytesIO(content.encode('utf-8')),
                full_path
            )

        return {"success": True}

    except Exception as e:
        print(f"Error updating file {file_path} for session {session_id}: {e}")
        return {"success": False, "error": str(e)}


@app.function(volumes={"/threads": volume})
def delete_file(session_id: str, file_path: str) -> dict:
    """
    Delete a file from a thread's folder.

    Note: Modal Volume API doesn't have a direct delete method yet.
    This is a placeholder for future implementation.

    Args:
        session_id: Thread/session ID
        file_path: Path to file relative to session folder

    Returns:
        Dictionary with 'success' boolean and optional 'error'
    """
    return {
        "success": False,
        "error": "Delete not implemented - Modal Volume API limitation"
    }


@app.function()
def get_sandbox_files(session_id: str, sandbox_id: str) -> dict:
    """
    Get files from an active sandbox (alternative to Volume-based access).

    Args:
        session_id: Thread/session ID
        sandbox_id: Modal sandbox ID

    Returns:
        Dictionary with file listings from sandbox
    """
    try:
        # Connect to the sandbox
        sb = modal.Sandbox.from_id(sandbox_id)

        # List files using sandbox ls command
        process = sb.exec("ls", "-la", f"/threads/{session_id}", timeout=10)
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
