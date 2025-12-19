"""Tool wrapper that adds description field to any BaseTool."""

from typing import Any, Optional, Type
from pydantic import BaseModel, Field, create_model
from pydantic.fields import FieldInfo
from langchain_core.tools import BaseTool
from langchain_core.callbacks import CallbackManagerForToolRun, AsyncCallbackManagerForToolRun


# Description field definition - placed first in schema for LLM generation order
DESCRIPTION_FIELD = (
    str,
    Field(
        default="",
        description=(
            "REQUIRED - provide FIRST before other parameters. "
            "Brief explanation starting with an -ing verb (e.g., 'Searching for...', 'Reading file...', 'Creating new...'). "
            "One sentence, addressed to the user."
        )
    )
)


def create_schema_with_description(original_schema: Type[BaseModel]) -> Type[BaseModel]:
    """Dynamically create a new schema with description field FIRST, then original fields.

    This ensures the LLM generates the description parameter before other parameters,
    while still inheriting field definitions from the original schema dynamically.
    """
    # Build field definitions with description first
    field_definitions: dict[str, Any] = {
        "description": DESCRIPTION_FIELD,
    }

    # Add original fields after description (preserving their definitions)
    for field_name, field_info in original_schema.model_fields.items():
        # Reconstruct the field tuple: (type, FieldInfo)
        field_definitions[field_name] = (field_info.annotation, field_info)

    # Copy original schema's model_config to preserve settings like arbitrary_types_allowed
    original_config = getattr(original_schema, "model_config", {})

    return create_model(
        f"{original_schema.__name__}WithDescription",
        __config__=original_config,
        **field_definitions,
    )


class ToolWithDescription(BaseTool):
    """Wrapper that adds a description field to any BaseTool.

    This wrapper:
    1. Holds a reference to the original tool (for delegation)
    2. Dynamically extends the original's args_schema with a description field
    3. Delegates execution to the original tool (stripping description from args)

    This ensures upstream tool changes are automatically inherited.
    """

    wrapped_tool: BaseTool

    def __init__(self, tool: BaseTool, **kwargs):
        # Build extended schema from original
        original_schema = tool.args_schema
        extended_schema = create_schema_with_description(original_schema) if original_schema else None

        super().__init__(
            wrapped_tool=tool,
            name=tool.name,
            description=tool.description,
            args_schema=extended_schema,
            **kwargs
        )

    def _run(
        self,
        *args,
        run_manager: Optional[CallbackManagerForToolRun] = None,
        **kwargs
    ) -> Any:
        """Execute the wrapped tool, stripping the description field."""
        # Remove description from kwargs before delegating
        kwargs.pop("description", None)
        return self.wrapped_tool._run(*args, run_manager=run_manager, **kwargs)

    async def _arun(
        self,
        *args,
        run_manager: Optional[AsyncCallbackManagerForToolRun] = None,
        **kwargs
    ) -> Any:
        """Async execute the wrapped tool, stripping the description field."""
        # Remove description from kwargs before delegating
        kwargs.pop("description", None)
        return await self.wrapped_tool._arun(*args, run_manager=run_manager, **kwargs)


def wrap_tools_with_description(tools: list[BaseTool]) -> list[BaseTool]:
    """Wrap a list of tools to add description field to each.

    Tools without args_schema are passed through unchanged.
    """
    wrapped = []
    for tool in tools:
        if hasattr(tool, "args_schema") and tool.args_schema is not None:
            wrapped.append(ToolWithDescription(tool))
        else:
            wrapped.append(tool)
    return wrapped
