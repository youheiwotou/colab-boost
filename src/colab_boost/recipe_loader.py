"""Declarative recipe manifest loader, validator, and input environment builder."""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from colab_boost.gguf_planner import validate_hf_repo_id
from colab_boost.models import GPU_ACCELERATORS, RecipeManifest

logger = logging.getLogger(__name__)

RECIPE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
INPUT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ALLOWED_CAPABILITIES = frozenset({"hf_token", "civitai_token"})
SKIP_DIR_NAMES = frozenset({"__pycache__"})
MAX_MANIFEST_BYTES = 64 * 1024
MAX_SCRIPT_BYTES = 512 * 1024
MAX_TEXT_INPUT_CHARS = 4096
MAX_SECRET_INPUT_CHARS = 4096
COLAB_RESERVED_PORT = 8080


class RecipeValidationError(ValueError):
    def __init__(self, message: str, field: str = "") -> None:
        super().__init__(message)
        self.field = field


@dataclass
class LoadedRecipe:
    manifest: RecipeManifest
    directory: Path
    script_path: Path
    script_text: str


def env_name_for_input(input_id: str) -> str:
    return "BOOST_INPUT_" + input_id.upper().replace("-", "_")


def _validate_non_empty_en_label(label: dict[str, str], what: str, field: str) -> None:
    en_val = label.get("en")
    if not isinstance(en_val, str) or not en_val.strip():
        raise RecipeValidationError(f"{what} must include non-empty 'en' label", field=field)


def _validate_single_line_input(value: str, field: str, max_len: int) -> None:
    if any(ch in value for ch in ("\x00", "\r", "\n")):
        raise RecipeValidationError(
            f"Field '{field}' cannot contain NUL or newline characters",
            field=field,
        )
    if len(value) > max_len:
        raise RecipeValidationError(
            f"Field '{field}' exceeds maximum length of {max_len} characters",
            field=field,
        )


def _parse_strict_int(val: Any, field: str) -> int:
    if isinstance(val, bool) or isinstance(val, float):
        raise RecipeValidationError(f"Field '{field}' must be an integer", field=field)
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        cleaned = val.strip()
        if not cleaned:
            raise RecipeValidationError(f"Field '{field}' must be an integer", field=field)
        try:
            return int(cleaned, 10)
        except ValueError as exc:
            raise RecipeValidationError(f"Field '{field}' must be an integer", field=field) from exc
    raise RecipeValidationError(f"Field '{field}' must be an integer", field=field)


def load_recipe_from_dir(directory: Path, *, bundled: bool = True) -> LoadedRecipe:
    if directory.is_symlink():
        raise RecipeValidationError("Recipe directory cannot be a symlink", field="directory")
    manifest_path = directory / "recipe.toml"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RecipeValidationError(f"Missing recipe.toml in {directory.name}", field="recipe.toml")
    raw_bytes = manifest_path.read_bytes()
    if len(raw_bytes) > MAX_MANIFEST_BYTES:
        raise RecipeValidationError("recipe.toml exceeds 64 KiB limit", field="recipe.toml")

    try:
        parsed = tomllib.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise RecipeValidationError(f"Invalid TOML in recipe.toml: {exc}", field="recipe.toml") from exc

    parsed["bundled"] = bundled
    try:
        manifest = RecipeManifest.model_validate(parsed)
    except Exception as exc:
        raise RecipeValidationError(f"Schema error in recipe.toml: {exc}", field="recipe.toml") from exc

    if manifest.api != 1:
        raise RecipeValidationError(f"Unsupported recipe api={manifest.api} (expected 1)", field="api")
    if not RECIPE_ID_RE.match(manifest.id):
        raise RecipeValidationError(f"Invalid recipe id: {manifest.id!r}", field="id")
    if directory.name != manifest.id:
        raise RecipeValidationError(
            f"Directory name {directory.name!r} must match recipe id {manifest.id!r}",
            field="id",
        )
    _validate_non_empty_en_label(manifest.name, "Recipe [name]", "name")

    for cap in manifest.needs:
        if cap not in ALLOWED_CAPABILITIES:
            raise RecipeValidationError(f"Unknown capability {cap!r} in needs", field="needs")

    if manifest.provision.min_vram_mib > 0 and manifest.provision.accelerator not in GPU_ACCELERATORS:
        raise RecipeValidationError(
            "min_vram_mib > 0 requires a GPU accelerator", field="provision.min_vram_mib"
        )

    # Validate script path safely within directory: check symlinks BEFORE .resolve()
    rel_script = manifest.run.script
    rel_path = Path(rel_script)
    if not rel_script or ".." in rel_path.parts or rel_path.is_absolute():
        raise RecipeValidationError(f"Unsafe script path: {rel_script!r}", field="run.script")
    if not (rel_script.endswith(".py") or rel_script.endswith(".sh")):
        raise RecipeValidationError("Worker script must end with .py or .sh", field="run.script")

    cur_path = directory
    for part in rel_path.parts:
        cur_path = cur_path / part
        if cur_path.is_symlink():
            raise RecipeValidationError(
                f"Worker script {rel_script!r} cannot be or traverse a symlink",
                field="run.script",
            )
    raw_script = directory / rel_path
    if not raw_script.is_file():
        raise RecipeValidationError(f"Worker script {rel_script!r} not found", field="run.script")

    resolved_dir = directory.resolve()
    script_path = raw_script.resolve()
    if resolved_dir not in script_path.parents:
        raise RecipeValidationError("Worker script escapes recipe directory", field="run.script")
    script_bytes = script_path.read_bytes()
    if len(script_bytes) > MAX_SCRIPT_BYTES:
        raise RecipeValidationError("Worker script exceeds 512 KiB limit", field="run.script")

    # Validate inputs, defaults, bounds & env collisions
    seen_env_names: set[str] = set()
    input_map = {}
    for inp in manifest.inputs:
        field_name = f"inputs.{inp.id}"
        if not INPUT_ID_RE.match(inp.id):
            raise RecipeValidationError(f"Invalid input id {inp.id!r}", field=field_name)
        env_key = env_name_for_input(inp.id)
        if env_key in seen_env_names:
            raise RecipeValidationError(
                f"Input id {inp.id!r} collides on environment variable {env_key}",
                field=field_name,
            )
        seen_env_names.add(env_key)
        input_map[inp.id] = inp

        _validate_non_empty_en_label(inp.label, f"Input {inp.id!r}", field_name)
        if inp.type == "secret" and inp.default is not None:
            raise RecipeValidationError(f"Secret input {inp.id!r} cannot have a default", field=field_name)
        if inp.type == "select":
            if not inp.options:
                raise RecipeValidationError(f"Select input {inp.id!r} requires options", field=field_name)
            if inp.default is not None:
                if not isinstance(inp.default, str) or inp.default not in inp.options:
                    raise RecipeValidationError(
                        f"Select input {inp.id!r} default must be in options", field=field_name
                    )
        elif inp.options is not None:
            raise RecipeValidationError(
                f"Only select inputs may define options ({inp.id!r})", field=field_name
            )
        if inp.type != "number" and (inp.min is not None or inp.max is not None):
            raise RecipeValidationError(
                f"Bounds min/max are only valid on number inputs ({inp.id!r})", field=field_name
            )

        if inp.type == "number":
            if inp.min is not None and inp.max is not None and inp.min > inp.max:
                raise RecipeValidationError(
                    f"Number input {inp.id!r} has min ({inp.min}) > max ({inp.max})",
                    field=field_name,
                )
            if inp.default is not None:
                if isinstance(inp.default, bool) or not isinstance(inp.default, int):
                    raise RecipeValidationError(
                        f"Number input {inp.id!r} default must be an integer",
                        field=field_name,
                    )
                if inp.min is not None and inp.default < inp.min:
                    raise RecipeValidationError(
                        f"Number input {inp.id!r} default {inp.default} is below min {inp.min}",
                        field=field_name,
                    )
                if inp.max is not None and inp.default > inp.max:
                    raise RecipeValidationError(
                        f"Number input {inp.id!r} default {inp.default} exceeds max {inp.max}",
                        field=field_name,
                    )
        elif inp.type == "bool":
            if inp.default is not None and not isinstance(inp.default, bool):
                raise RecipeValidationError(
                    f"Bool input {inp.id!r} default must be a boolean",
                    field=field_name,
                )
        elif inp.type in ("hf-gguf", "hf-repo"):
            if inp.default is not None:
                if not isinstance(inp.default, str):
                    raise RecipeValidationError(
                        f"Repository input {inp.id!r} default must be a string",
                        field=field_name,
                    )
                if inp.default != "":
                    try:
                        validate_hf_repo_id(inp.default)
                    except ValueError as exc:
                        raise RecipeValidationError(str(exc), field=field_name) from exc
        elif inp.type == "text":
            if inp.default is not None:
                if not isinstance(inp.default, str):
                    raise RecipeValidationError(
                        f"Text input {inp.id!r} default must be a string",
                        field=field_name,
                    )
                _validate_single_line_input(inp.default, field_name, MAX_TEXT_INPUT_CHARS)

    # Validate stages
    stage_ids: set[str] = set()
    for st in manifest.stages:
        if not st.id or st.id in stage_ids:
            raise RecipeValidationError(f"Invalid or duplicate stage id {st.id!r}", field="stages")
        stage_ids.add(st.id)
        _validate_non_empty_en_label(st.label, f"Stage {st.id!r}", f"stages.{st.id}")

    # Validate endpoints (port 8080 belongs to Colab's internal node server)
    seen_endpoint_ids: set[str] = set()
    seen_port_inputs: dict[str, str] = {}
    seen_ports: dict[int, str] = {}
    for ep in manifest.endpoints:
        ep_field = f"endpoints.{ep.id}"
        if not ep.id or ep.id in seen_endpoint_ids:
            raise RecipeValidationError(f"Invalid or duplicate endpoint id {ep.id!r}", field=ep_field)
        seen_endpoint_ids.add(ep.id)
        _validate_non_empty_en_label(ep.label, f"Endpoint {ep.id!r}", ep_field)

        has_fixed = ep.port is not None
        has_input = ep.port_input is not None
        if has_fixed == has_input:
            raise RecipeValidationError(
                f"Endpoint {ep.id!r} must specify exactly one of 'port' or 'port_input'",
                field=ep_field,
            )
        if ep.port is not None:
            if ep.port == COLAB_RESERVED_PORT or not (1024 <= ep.port <= 65535):
                raise RecipeValidationError(
                    f"Endpoint {ep.id!r} port {ep.port} is reserved (8080) or out of range 1024..65535",
                    field=ep_field,
                )
            if ep.port in seen_ports:
                raise RecipeValidationError(
                    f"Endpoint {ep.id!r} port {ep.port} conflicts with endpoint {seen_ports[ep.port]!r}",
                    field=ep_field,
                )
            seen_ports[ep.port] = ep.id
        if ep.port_input is not None:
            ref = input_map.get(ep.port_input)
            if ref is None or ref.type != "number":
                raise RecipeValidationError(
                    f"Endpoint {ep.id!r} port_input {ep.port_input!r} must reference a number input",
                    field=ep_field,
                )
            if ep.port_input in seen_port_inputs:
                raise RecipeValidationError(
                    f"Endpoint {ep.id!r} port_input {ep.port_input!r} conflicts with endpoint {seen_port_inputs[ep.port_input]!r}",
                    field=ep_field,
                )
            seen_port_inputs[ep.port_input] = ep.id
            if ref.default == COLAB_RESERVED_PORT:
                raise RecipeValidationError(
                    "Default VM port 8080 belongs to Colab; choose another port",
                    field=f"inputs.{ref.id}",
                )
            if isinstance(ref.default, int):
                if not (1024 <= ref.default <= 65535):
                    raise RecipeValidationError(
                        f"Default VM port {ref.default} for endpoint {ep.id!r} is outside 1024..65535",
                        field=f"inputs.{ref.id}",
                    )
                if ref.default in seen_ports:
                    raise RecipeValidationError(
                        f"Endpoint {ep.id!r} default port {ref.default} conflicts with endpoint {seen_ports[ref.default]!r}",
                        field=ep_field,
                    )
                seen_ports[ref.default] = ep.id

    for kp in manifest.keep:
        if not kp.path.startswith("/") or ".." in kp.path.split("/"):
            raise RecipeValidationError(f"Invalid keep path {kp.path!r}", field="keep")
        _validate_non_empty_en_label(kp.label, f"Keep path {kp.path!r}", "keep")

    return LoadedRecipe(
        manifest=manifest,
        directory=directory,
        script_path=script_path,
        script_text=script_bytes.decode("utf-8"),
    )


def validate_and_build_env(
    manifest: RecipeManifest, raw_inputs: dict[str, Any]
) -> tuple[dict[str, str], list[str], dict[str, int]]:
    """Validates user input values against the manifest.

    Returns ``(env_vars, secret_values_to_redact, resolved_endpoint_ports)``.
    """
    known_ids = {inp.id for inp in manifest.inputs}
    unknown = set(raw_inputs.keys()) - known_ids
    if unknown:
        first = sorted(unknown)[0]
        raise RecipeValidationError(f"Unknown input field: {first!r}", field=first)

    env_vars: dict[str, str] = {}
    secrets: list[str] = []
    resolved_numbers: dict[str, int] = {}

    for spec in manifest.inputs:
        val = raw_inputs.get(spec.id)
        if val is None or val == "":
            if spec.default is not None:
                val = spec.default
            elif spec.required:
                raise RecipeValidationError(f"Field '{spec.id}' is required", field=spec.id)
            else:
                continue

        env_key = env_name_for_input(spec.id)
        if spec.type in ("hf-gguf", "hf-repo"):
            try:
                repo = validate_hf_repo_id(str(val))
            except ValueError as exc:
                raise RecipeValidationError(str(exc), field=spec.id) from exc
            env_vars[env_key] = repo
        elif spec.type == "select":
            sval = str(val)
            if spec.options and sval not in spec.options:
                raise RecipeValidationError(
                    f"Invalid option {sval!r} for '{spec.id}' (allowed: {', '.join(spec.options)})",
                    field=spec.id,
                )
            env_vars[env_key] = sval
        elif spec.type == "bool":
            if isinstance(val, bool):
                bval = val
            elif str(val).lower() in ("1", "true", "yes", "on"):
                bval = True
            elif str(val).lower() in ("0", "false", "no", "off"):
                bval = False
            else:
                raise RecipeValidationError(f"Invalid boolean for '{spec.id}'", field=spec.id)
            env_vars[env_key] = "1" if bval else "0"
        elif spec.type == "number":
            ival = _parse_strict_int(val, spec.id)
            if spec.min is not None and ival < spec.min:
                raise RecipeValidationError(f"Field '{spec.id}' must be >= {spec.min}", field=spec.id)
            if spec.max is not None and ival > spec.max:
                raise RecipeValidationError(f"Field '{spec.id}' must be <= {spec.max}", field=spec.id)
            env_vars[env_key] = str(ival)
            resolved_numbers[spec.id] = ival
        elif spec.type == "secret":
            sval = str(val)
            _validate_single_line_input(sval, spec.id, MAX_SECRET_INPUT_CHARS)
            if sval:
                env_vars[env_key] = sval
                secrets.append(sval)
        else:  # text
            sval = str(val)
            _validate_single_line_input(sval, spec.id, MAX_TEXT_INPUT_CHARS)
            env_vars[env_key] = sval

    endpoint_ports: dict[str, int] = {}
    used_ports: dict[int, str] = {}
    for ep in manifest.endpoints:
        port = ep.port if ep.port is not None else resolved_numbers.get(ep.port_input or "")
        if port is None:
            raise RecipeValidationError(f"Missing port value for endpoint '{ep.id}'", field=ep.port_input or ep.id)
        if port == COLAB_RESERVED_PORT or not (1024 <= port <= 65535):
            raise RecipeValidationError(
                f"VM port {port} is reserved by Colab (8080) or outside 1024..65535",
                field=ep.port_input or ep.id,
            )
        if port in used_ports:
            raise RecipeValidationError(
                f"Endpoint '{ep.id}' port {port} conflicts with endpoint '{used_ports[port]}'",
                field=ep.port_input or ep.id,
            )
        used_ports[port] = ep.id
        endpoint_ports[ep.id] = port

    return env_vars, secrets, endpoint_ports


def _iter_recipe_subdirs(base_dir: Path) -> list[Path]:
    if not base_dir.is_dir():
        return []
    return [
        child
        for child in sorted(base_dir.iterdir())
        if child.is_dir() and not child.name.startswith(".") and child.name not in SKIP_DIR_NAMES
    ]


def discover_all_recipes(bundled_dir: Path, user_dir: Path) -> dict[str, LoadedRecipe]:
    recipes: dict[str, LoadedRecipe] = {}
    for child in _iter_recipe_subdirs(bundled_dir):
        try:
            loaded = load_recipe_from_dir(child, bundled=True)
        except (RecipeValidationError, OSError) as exc:
            logger.warning("Skipping invalid bundled recipe directory %r: %s", child.name, exc)
            continue
        recipes[loaded.manifest.id] = loaded

    for child in _iter_recipe_subdirs(user_dir):
        try:
            loaded = load_recipe_from_dir(child, bundled=False)
        except (RecipeValidationError, OSError) as exc:
            logger.warning("Skipping invalid user recipe directory %r: %s", child.name, exc)
            continue
        # Bundled recipes cannot be silently overwritten by user recipes with same ID
        if loaded.manifest.id not in recipes:
            recipes[loaded.manifest.id] = loaded
    return recipes
