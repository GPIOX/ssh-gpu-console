"""Workspace wire models (Sol-owned contract).

Persistent declarations only; runtime observations (scan results, transfer
progress) live in RAM and are modelled separately. `extra="forbid"` everywhere.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SHELL_META = set(";&|<>`$\n\r\x00(){}[]*?~\"'\\\\")


class ArtifactKind(StrEnum):
    CODE = "code"
    DATASET = "dataset"
    MODEL = "model"


_ID = Field(min_length=1, max_length=64)
_NAME = Field(min_length=1, max_length=120)
_DESC = Field(default="", max_length=600)


class ProjectRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = _ID
    name: str = _NAME
    description: str = _DESC
    artifact_ids: list[str] = Field(default_factory=list, max_length=64)
    launch_config_ids: list[str] = Field(default_factory=list, max_length=32)
    tags: list[str] = Field(default_factory=list, max_length=16)
    created_at: str = ""
    updated_at: str = ""


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = _NAME
    description: str = _DESC
    artifact_ids: list[str] = Field(default_factory=list, max_length=64)
    tags: list[str] = Field(default_factory=list, max_length=16)


class ProjectPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=600)
    artifact_ids: list[str] | None = Field(default=None, max_length=64)
    tags: list[str] | None = Field(default=None, max_length=16)


class ArtifactRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = _ID
    kind: ArtifactKind
    name: str = _NAME
    version: str | None = Field(default=None, max_length=64)
    description: str = _DESC
    immutable: bool = True
    created_at: str = ""
    updated_at: str = ""


class ArtifactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ArtifactKind
    name: str = _NAME
    version: str | None = Field(default=None, max_length=64)
    description: str = _DESC
    immutable: bool | None = None  # default: immutable unless kind == code


class ArtifactPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    version: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=600)
    immutable: bool | None = None


_PATH = Field(min_length=1, max_length=512)


class PlacementRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    placement_id: str = _ID
    artifact_id: str = _ID
    server_id: str = _ID
    remote_path: str = _PATH
    created_at: str = ""
    updated_at: str = ""


class PlacementCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = _ID
    server_id: str = _ID
    remote_path: str = _PATH


class PlacementPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remote_path: str | None = _PATH


class InspectionState(StrEnum):
    DECLARED = "declared"
    VERIFIED = "verified"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"


class PlacementInspection(BaseModel):
    """Runtime observation — RAM only, never persisted."""

    model_config = ConfigDict(extra="forbid")

    placement_id: str = _ID
    state: InspectionState
    file_type: str | None = None  # directory | regular file | ...
    size_b: int | None = None
    file_count: int | None = None
    checked_at: str = ""
    detail: str = ""


class LaunchConfigRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("program", "working_dir")
    @classmethod
    def _no_shell_metacharacters(cls, value: str | None) -> str | None:
        if value is not None:
            for char in _SHELL_META:
                if char in value:
                    raise ValueError(
                        "shell metacharacters are not allowed; use structured program + args"
                    )
        return value

    launch_config_id: str = _ID
    project_id: str = _ID
    name: str = _NAME
    working_dir: str | None = Field(default=None, min_length=1, max_length=512)
    program: str = Field(min_length=1, max_length=256)
    args: list[str] = Field(default_factory=list, max_length=64)
    environment: str | None = Field(default=None, max_length=120)
    env_vars: dict[str, str] = Field(default_factory=dict, max_length=32)
    required_artifact_ids: list[str] = Field(default_factory=list, max_length=64)
    gpu_count: int | None = Field(default=None, ge=1, le=64)
    min_vram_b: int | None = Field(default=None, ge=0)
    created_at: str = ""
    updated_at: str = ""


class LaunchConfigCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("program", "working_dir")
    @classmethod
    def _no_shell_metacharacters(cls, value: str | None) -> str | None:
        if value is not None:
            for char in _SHELL_META:
                if char in value:
                    raise ValueError("shell metacharacters are not allowed; use structured args")
        return value

    project_id: str = _ID
    name: str = _NAME
    working_dir: str | None = Field(default=None, min_length=1, max_length=512)
    program: str = Field(min_length=1, max_length=256)
    args: list[str] = Field(default_factory=list, max_length=64)
    environment: str | None = Field(default=None, max_length=120)
    env_vars: dict[str, str] = Field(default_factory=dict, max_length=32)
    required_artifact_ids: list[str] = Field(default_factory=list, max_length=64)
    gpu_count: int | None = Field(default=None, ge=1, le=64)
    min_vram_b: int | None = Field(default=None, ge=0)


class LaunchConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    working_dir: str | None = _PATH
    program: str | None = Field(default=None, min_length=1, max_length=256)
    args: list[str] | None = Field(default=None, max_length=64)
    environment: str | None = Field(default=None, max_length=120)
    env_vars: dict[str, str] | None = Field(default=None, max_length=32)
    required_artifact_ids: list[str] | None = Field(default=None, max_length=64)
    gpu_count: int | None = Field(default=None, ge=1, le=64)
    min_vram_b: int | None = Field(default=None, ge=0)


class ServerRoots(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_root: str | None = Field(default=None, min_length=1, max_length=512)
    dataset_root: str | None = Field(default=None, min_length=1, max_length=512)
    model_root: str | None = Field(default=None, min_length=1, max_length=512)
    output_root: str | None = Field(default=None, min_length=1, max_length=512)


class ServerRootsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_root: str | None = Field(default=None, min_length=1, max_length=512)
    dataset_root: str | None = Field(default=None, min_length=1, max_length=512)
    model_root: str | None = Field(default=None, min_length=1, max_length=512)
    output_root: str | None = Field(default=None, min_length=1, max_length=512)
