"""Model assignment dashboard routes: model info/options/recommended default, auxiliary + MoA slots, /api/model/set.

Extracted from ``hermes_cli.web_server``; helpers/state that tests monkeypatch on
``web_server`` stay there and are resolved late at call time (cycle-safe).
"""

import asyncio
import concurrent.futures
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from hermes_cli.web_deps import LateState, late
from hermes_cli.web_server_config import (
    _AUX_TASK_SLOTS, _UNSET, _apply_model_assignment_sync, _dashboard_code_skew_guard,
    _prepare_main_assignment,
)
from agent.model_metadata import is_local_endpoint
from starlette.concurrency import run_in_threadpool
from hermes_cli.web_models import (
    ModelAssignment, ModelOverrideDelete, ModelOverrideUpsert, MoaConfigPayload, MoaModelSlot,
)
from hermes_cli.config import read_raw_config
from hermes_cli.web_routers._common import _CONFIG_MUTATION_LOCK, config_write_scope, http_failure

_log = logging.getLogger("hermes_cli.web_server")
router = APIRouter()

# Late-bound so a test's monkeypatch on the owning module wins at call time.
_config_profile_scope = late("_config_profile_scope", "hermes_cli.web_server_profiles")
_profile_scope = late("_profile_scope", "hermes_cli.web_server_profiles")
load_config = late("load_config", "hermes_cli.config")
save_config = late("save_config", "hermes_cli.config")


_EMPTY_MODEL_INFO: dict = {
    "model": "", "provider": "", "auto_context_length": 0, "config_context_length": 0,
    "effective_context_length": 0, "capabilities": {},
}
_CAPABILITY_FIELDS = ("supports_tools", "supports_vision", "supports_reasoning", "context_window",
                      "max_output_tokens", "model_family")

# Canonical override keys — the ONLY key space the model_overrides engine
# accepts (agent/models_dev.py). The catalog endpoints read/modify exactly
# this key space; nothing else is invented here.
_MODEL_OVERRIDE_KEYS = (
    "context_window", "max_output_tokens", "supports_tools",
    "supports_vision", "supports_reasoning", "model_family",
)


def _main_model_fields(model_cfg) -> tuple[str, str]:
    """(model, provider) from config's ``model`` section, which may be a plain string."""
    if isinstance(model_cfg, dict):
        return model_cfg.get("default", model_cfg.get("name", "")), model_cfg.get("provider", "")
    return (str(model_cfg) if model_cfg else ""), ""


def _load_config_scoped(profile: Optional[str]) -> dict:
    with _profile_scope(profile):
        return load_config()


# Blocking budget for /api/model/info's context-length resolution. The resolver
# chain (agent.model_metadata.get_model_context_length) runs several sequential
# provider probes, each with its own multi-second timeout, so an unreachable or
# blackholed model.base_url can hold this response for tens of seconds — and the
# Desktop Model Settings page waits on it (#63214).
_MODEL_INFO_PROBE_BUDGET_S = 5.0


def _bounded_context_length_probe(model: str, base_url: str, provider: str) -> int:
    """``get_model_context_length`` with the route's blocking budget.

    On timeout the abandoned probe keeps running in its worker thread (bounded
    by its own per-request timeouts) while the response degrades to
    ``auto_context_length = 0`` ("auto-detected: unknown").
    """
    from agent.model_metadata import get_model_context_length

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="model-info-probe")
    try:
        return pool.submit(
            get_model_context_length, model=model, base_url=base_url, provider=provider,
            config_context_length=None
        ).result(timeout=_MODEL_INFO_PROBE_BUDGET_S)
    except concurrent.futures.TimeoutError:
        _log.warning(
            "GET /api/model/info: context-length probe for %r at %s exceeded %.1fs — returning unknown",
            model, base_url or "<default>", _MODEL_INFO_PROBE_BUDGET_S,
        )
        return 0
    finally:
        # wait=False: never block the response (or interpreter exit) on the
        # abandoned probe.
        pool.shutdown(wait=False)


@router.get("/api/model/info")
def get_model_info(profile: Optional[str] = None):
    """Resolved metadata for the configured model: auto-detected vs configured
    context length (so the UI can show "Auto-detected: 200K" beside the
    override) plus models.dev capabilities when available."""
    try:
        model_cfg = _load_config_scoped(profile).get("model", "")
        model_name, provider = _main_model_fields(model_cfg)
        base_url = model_cfg.get("base_url", "") if isinstance(model_cfg, dict) else ""
        config_ctx = model_cfg.get("context_length") if isinstance(model_cfg, dict) else None

        if not model_name:
            return dict(_EMPTY_MODEL_INFO, provider=provider)

        try:
            # config_context_length=None: ignore the override — we want the auto value.
            # Bounded: the resolver's provider probes can hang for tens of seconds
            # when model.base_url is unreachable (#63214).
            auto_ctx = _bounded_context_length_probe(model_name, base_url, provider)
        except Exception:
            auto_ctx = 0

        config_ctx_int = config_ctx if isinstance(config_ctx, int) and config_ctx > 0 else 0

        caps = {}
        try:
            from agent.models_dev import get_model_capabilities
            mc = get_model_capabilities(provider=provider, model=model_name)
            if mc is not None:
                caps = {name: getattr(mc, name) for name in _CAPABILITY_FIELDS}
        except Exception:
            pass

        return {
            "model": model_name, "provider": provider, "auto_context_length": auto_ctx,
            "config_context_length": config_ctx_int,
            "effective_context_length": config_ctx_int or auto_ctx,  # what the agent actually uses
            "capabilities": caps,
        }
    except HTTPException:
        # Unknown/invalid profile must surface as 404, not degrade into a
        # 200 with empty model info (which would render as "no model set").
        raise
    except Exception:
        _log.exception("GET /api/model/info failed")
        return dict(_EMPTY_MODEL_INFO)


@router.get("/api/model/options")
async def get_model_options(
    profile: Optional[str] = None,
    refresh: bool = False,
    include_unconfigured: bool = False,
    explicit_only: bool = False,
):
    """Authenticated providers + curated model lists — REST twin of the ``model.options``
    JSON-RPC on tui_gateway, same response shape so ``ModelPickerDialog`` shares the types.
    ``profile`` scopes the picker context so the Models page reads the SAME profile
    /api/model/set writes. ``refresh`` busts the per-provider model-id disk cache
    (picker's explicit "Refresh Models"); normal opens stay on the 1h cache."""
    with http_failure("GET /api/model/options failed", 500, detail="Failed to list model options"):
        skew_msg = _dashboard_code_skew_guard()
        if skew_msg:
            _log.warning("GET /api/model/options refused: %s", skew_msg)
            raise HTTPException(status_code=503, detail=f"Restart required: {skew_msg}")

        from hermes_cli.inventory import build_model_options_payload, load_picker_context

        def _build_payload_scoped() -> dict:
            # Full sync picker build off the event loop under the requested profile.
            # _config_profile_scope (contextvar only, no skill-module lock): the build can
            # block 15s on a models.dev cache miss, and _profile_scope's RLock held across
            # that starves concurrent /api/config and freezes the server.
            with _config_profile_scope(profile):
                return build_model_options_payload(
                    load_picker_context(), explicit_only=bool(explicit_only),
                    include_unconfigured=bool(include_unconfigured), refresh=bool(refresh))

        return await run_in_threadpool(_build_payload_scoped)


def _nous_recommended_default() -> dict:
    from hermes_cli.models import recommended_nous_default_model
    return recommended_nous_default_model()


@router.get("/api/model/recommended-default")
def get_recommended_default_model(provider: str = "", profile: Optional[str] = None):
    """Recommended default model for a freshly-authenticated provider, mirroring
    ``hermes model``'s curation so GUI onboarding lands on a sensible default.
    Nous honors the user's free/paid tier. Any other provider gets the preferred
    silent default when its curated list carries it, else the first curated model —
    aggregator lists lead with the priciest Anthropic flagship, which must never be
    the model a user lands on without explicitly picking it.
    Response: {"provider", "model", "free_tier": bool | None} — free_tier only for
    Nous; ``model`` may be empty (caller degrades gracefully)."""
    slug = (provider or "").strip().lower()

    if slug == "nous":
        try:
            # The tier, Portal URL and recommendation caches are all per profile home.
            with _config_profile_scope(profile):
                return _nous_recommended_default()
        except HTTPException:
            raise  # an unknown ?profile= is the scope's 404, not an empty recommendation
        except Exception:
            _log.exception("GET /api/model/recommended-default (nous) failed")
            return {"provider": "nous", "model": "", "free_tier": None}

    try:
        from hermes_cli.inventory import build_models_payload, load_picker_context
        from hermes_cli.models import pick_silent_default_model

        # build_models_payload -> list_authenticated_providers -> _save_discovered_models_to_config:
        # this GET lazily PERSISTS discovered custom-provider models, so it needs the scope too.
        with _config_profile_scope(profile):
            payload = build_models_payload(load_picker_context())
        for row in payload.get("providers", []):
            if str(row.get("slug", "")).lower() == slug:
                models = [str(m) for m in (row.get("models") or [])]
                return {"provider": slug, "model": pick_silent_default_model(models, provider=slug), "free_tier": None}
        return {"provider": slug, "model": "", "free_tier": None}
    except HTTPException:
        raise  # an unknown ?profile= is the scope's 404, not an empty recommendation
    except Exception:
        _log.exception("GET /api/model/recommended-default failed")
        return {"provider": slug, "model": "", "free_tier": None}


@router.get("/api/model/auxiliary")
def get_auxiliary_models(profile: Optional[str] = None):
    """Current auxiliary task assignments: ``{"tasks": [{task, provider, model,
    base_url}, ...], "main": {provider, model}}``. ``profile`` scopes the read —
    without it the Models page would show the dashboard profile's pins while
    /api/model/set wrote the selected profile's."""
    with http_failure("GET /api/model/auxiliary failed", 500, detail="Failed to read auxiliary config"):
        cfg = _load_config_scoped(profile)
        aux_cfg = cfg.get("auxiliary", {})
        if not isinstance(aux_cfg, dict):
            aux_cfg = {}

        tasks = []
        for slot in _AUX_TASK_SLOTS:
            slot_cfg = aux_cfg.get(slot, {}) if isinstance(aux_cfg.get(slot), dict) else {}
            base_url = str(slot_cfg.get("base_url", "") or "")
            tasks.append({
                "task": slot, "provider": str(slot_cfg.get("provider", "auto") or "auto"),
                "model": str(slot_cfg.get("model", "") or ""), "base_url": base_url,
                "reasoning_effort": str(slot_cfg.get("reasoning_effort") or "") or None,
                # Lets the UI tell a free local/LAN pin from a forgotten paid-provider pin.
                "local_endpoint": is_local_endpoint(base_url),
            })

        model, provider = _main_model_fields(cfg.get("model", {}))
        return {"tasks": tasks, "main": {"provider": str(provider or ""), "model": str(model or "")}}


@router.get("/api/model/moa")
def get_moa_models(profile: Optional[str] = None):
    """Return the configured Mixture-of-Agents provider/model slots."""
    with http_failure("GET /api/model/moa failed", 500, detail="Failed to read MoA config"):
        from hermes_cli.moa_config import normalize_moa_config

        with _profile_scope(profile):
            cfg = load_config()
            return normalize_moa_config(cfg.get("moa") if isinstance(cfg, dict) else {})


_MOA_PRESET_FIELDS = (
    "reference_temperature", "aggregator_temperature", "reference_timeout",
    "degraded_reference_policy", "fanout", "enabled",
)


def _slot_dict(slot: MoaModelSlot) -> dict:
    # Drop unset optionals so saved slots stay minimal ({provider, model}).
    return {k: v for k, v in slot.dict().items() if v is not None}


def _preset_dict(preset) -> dict:
    """Raw preset dict from a MoaPresetPayload or the flat MoaConfigPayload fields."""
    return {
        "reference_models": [_slot_dict(slot) for slot in preset.reference_models],
        "aggregator": _slot_dict(preset.aggregator),
        **{name: getattr(preset, name) for name in _MOA_PRESET_FIELDS},
    }


@router.put("/api/model/moa")
def set_moa_models(body: MoaConfigPayload, profile: Optional[str] = None):
    """Persist the Mixture-of-Agents provider/model slots."""
    with http_failure("PUT /api/model/moa failed", 500, detail="Failed to save MoA config"):
        from hermes_cli.moa_config import normalize_moa_config, validate_moa_payload

        # load→mutate→save runs on a worker thread (sync-def endpoint); the
        # desktop's debounced PUT /api/config autosave races it, so the whole
        # span holds _CONFIG_MUTATION_LOCK or one of the two saves is dropped.
        with config_write_scope(body.profile or profile):
            cfg = load_config()
            if body.presets:
                raw = {
                    "default_preset": body.default_preset,
                    "active_preset": body.active_preset,
                    "presets": {name: _preset_dict(preset) for name, preset in body.presets.items()},
                }
            else:
                raw = _preset_dict(body)  # legacy flat payload from older clients

            # Reject-don't-repair: normalize_moa_config() silently swaps any preset with
            # incomplete slots for the hardcoded defaults — correct tolerance at READ time,
            # silent data loss at WRITE time (desktop autosave of a half-filled slot replaced
            # the user's whole preset). Refuse loudly so no client can corrupt config here.
            # See #64156.
            problems = validate_moa_payload(raw)
            if problems:
                raise HTTPException(status_code=422, detail="Invalid MoA config: " + "; ".join(problems))
            normalized = normalize_moa_config(raw)
            # Merge, don't overwrite: hand-edited keys not in MoaConfigPayload (save_traces, trace_dir) survive.
            # See issue #58819. Write ONLY the moa section (merge_existing deep-merges it over the
            # on-disk raw file): saving the whole default-expanded ``cfg`` snapshot re-persisted
            # every other section too, so a Desktop MoA autosave could wipe a chain another
            # surface wrote meanwhile (#89184, ``fallback_providers: []``).
            moa_section = dict(cfg.get("moa") or {})
            moa_section.update(normalized)
            save_config({"moa": moa_section}, merge_existing=True)
            return {"ok": True, **normalized}


@router.post("/api/model/set")
async def set_model_assignment(body: ModelAssignment, profile: Optional[str] = None):
    """Assign a model to the main slot or an auxiliary task slot. Writes
    ``~/.hermes/config.yaml`` — applies to **new** sessions only; a running chat
    PTY hot-swaps via the ``/model`` slash command instead."""
    scope, task = (body.scope or "").strip().lower(), (body.task or "").strip().lower()
    provider, model = (body.provider or "").strip(), (body.model or "").strip()
    base_url, api_key = (body.base_url or "").strip(), (body.api_key or "").strip()

    if scope not in {"main", "auxiliary"}:
        raise HTTPException(status_code=400, detail="scope must be 'main' or 'auxiliary'")

    with http_failure("POST /api/model/set failed", 500, detail="Failed to save model assignment"):
        # #99859 (R2): the options picker already refuses on code skew; the WRITE path
        # must too — a stale process persisting a post-update model string is the
        # invalid-model-serving failure the reporter hit.
        skew_msg = _dashboard_code_skew_guard()
        if skew_msg:
            _log.warning("POST /api/model/set refused: %s", skew_msg)
            raise HTTPException(status_code=503, detail=f"Restart required: {skew_msg}")

        # Expensive-model warning runs BEFORE the profile scope is entered: _profile_scope
        # must never be held across an await (the RLock is reentrant per-thread, so a second
        # coroutine interleaving on the event-loop thread could cross-restore module globals).
        if model and not body.confirm_expensive_model:
            try:
                from hermes_cli.model_selection_guards import combined_selection_warning

                # Pricing lookup can hit models.dev / a /models endpoint on a cache miss — off the loop.
                warning = await asyncio.to_thread(combined_selection_warning, model, provider=provider, base_url=base_url)
            except Exception:
                warning = None
            if warning is not None:
                return {"ok": False, "scope": scope, "provider": provider, "model": model,
                        "confirm_required": True, "confirm_message": warning.message}

        reasoning_effort = body.reasoning_effort if "reasoning_effort" in body.model_fields_set else _UNSET

        def _apply_assignment():
            # Same RMW span as PUT /api/config: applyMainModel fires this while the
            # settings-page autosave is in flight — hold the mutation lock. switch_model's
            # catalog fetches / endpoint probes are network I/O, so they run BEFORE the lock;
            # only load→apply→save holds it.
            with _profile_scope(body.profile or profile):
                prepared = (_prepare_main_assignment(load_config(), provider, model, base_url, api_key)
                            if scope == "main" else None)
                with _CONFIG_MUTATION_LOCK:
                    return _apply_model_assignment_sync(
                        scope, provider, model, task, base_url, api_key,
                        reasoning_effort=reasoning_effort, prepared=prepared)

        return await asyncio.to_thread(_apply_assignment)


# ============================================================================
# Model catalog with overrides — browse the resolved catalog and patch the
# config.yaml `model_overrides` section from the dashboard.
#
# The override ENGINE itself is native (agent/models_dev.py): canonical schema
# (context_window / max_output_tokens / supports_tools / supports_vision /
# supports_reasoning / model_family), explicit entries patch known catalog
# models, `_default` entries fill gaps only for models the catalog doesn't
# know. These endpoints just read/modify that config section safely — they
# never invent semantics of their own.
# ============================================================================

# Canonical override keys — the ONLY key space consumers accept.
_MODEL_OVERRIDE_KEYS = (
    "context_window", "max_output_tokens", "supports_tools",
    "supports_vision", "supports_reasoning", "model_family",
)


def _model_catalog_rows_sync() -> Dict[str, Any]:
    """Build the resolved catalog payload (runs in a worker thread).

    Reuses the inventory builder the TUI/API-server picker uses so the
    dashboard sees the SAME provider/model inventory, then decorates each
    model row with (a) the effective metadata get_model_info resolves —
    catalog merged with any active override — and (b) which canonical
    override fields the user has set, so the UI can badge overridden rows.
    """
    from hermes_cli.inventory import build_model_options_payload, load_picker_context
    from agent.models_dev import get_model_info as _catalog_lookup

    payload = build_model_options_payload(
        load_picker_context(),
        include_unconfigured=True,
    )
    raw = read_raw_config()
    overrides_cfg = raw.get("model_overrides") if isinstance(raw, dict) else None
    overrides_cfg = overrides_cfg if isinstance(overrides_cfg, dict) else {}

    providers_out: List[Dict[str, Any]] = []
    for prov in payload.get("providers") or []:
        if not isinstance(prov, dict):
            continue
        prov_name = str(prov.get("name") or "")
        slug = str(prov.get("slug") or prov_name)
        # The override engine keys on provider IDs (models.dev ids, or the
        # Hermes id — `custom:<name>` for user-defined providers), NOT on the
        # display label. Figure out which candidate actually resolves so the
        # editor writes overrides under a key the engine will honor.
        if prov.get("is_user_defined") and not slug.lower().startswith("custom:"):
            override_key = f"custom:{prov_name}" if prov_name else slug
        else:
            override_key = slug or prov_name.lower()
        models_out: List[Dict[str, Any]] = []
        resolved_by: Optional[str] = None
        for mid in prov.get("models") or []:
            if not isinstance(mid, str) or not mid.strip():
                continue
            mid = mid.strip()
            # Effective metadata: catalog merged with the override the same
            # way the agent resolves it (allow_network=False — never block).
            # Try the id-shaped candidates until one resolves; remember the
            # first winner so override_key follows what actually works.
            info = None
            for prov_key in dict.fromkeys(
                x for x in (override_key, prov_name, slug, prov_name.lower(), slug.lower()) if x
            ):
                try:
                    info = _catalog_lookup(prov_key, mid, allow_network=False)
                except Exception:
                    info = None
                if info is not None:
                    if resolved_by is None:
                        resolved_by = prov_key
                    break
            # Which canonical fields does the user override for this model?
            # Sections may be keyed by ANY historical form (display label,
            # slug, or engine id), so probe the same candidate chain the
            # engine accepts instead of assuming one key.
            section: Optional[Dict[str, Any]] = None
            for key in dict.fromkeys(
                x for x in (override_key, prov_name, slug, prov_name.lower(), slug.lower()) if x
            ):
                sec = overrides_cfg.get(key)
                if isinstance(sec, dict):
                    section = sec
                    break
            entry = section.get(mid) if section else None
            if not isinstance(entry, dict) and section:
                # case-insensitive fallback, mirroring the engine
                lower = mid.lower()
                for k, v in section.items():
                    if k != "_default" and isinstance(v, dict) and k.lower() == lower:
                        entry = v
                        break
            active_keys = sorted(k for k in _MODEL_OVERRIDE_KEYS if isinstance(entry, dict) and k in entry) if entry else []
            models_out.append({
                "id": mid,
                "context_window": getattr(info, "context_window", 0) or 0,
                "max_output_tokens": getattr(info, "max_output", 0) or 0,
                "supports_tools": bool(getattr(info, "tool_call", False)),
                "supports_vision": bool(getattr(info, "attachment", False)),
                "supports_reasoning": bool(getattr(info, "reasoning", False)),
                "override_active": bool(active_keys),
                "override_keys": active_keys,
            })
        # Prefer a key that demonstrably resolved metadata this pass.
        if resolved_by:
            override_key = resolved_by
        providers_out.append({
            "name": prov_name,
            "slug": slug,
            "override_key": override_key,
            "authenticated": bool(prov.get("authenticated", False)),
            "is_user_defined": bool(prov.get("is_user_defined", False)),
            "models": models_out,
        })

    return {
        "providers": providers_out,
        # Echo the stored override sections so the editor can prefill the
        # form exactly with what's on disk (including _default entries).
        "overrides": overrides_cfg,
    }


@router.get("/api/model/catalog")
async def get_model_catalog(profile: Optional[str] = None):
    """Resolved model catalog + effective per-model metadata + active overrides.

    One call powers the Model Catalog card: providers with their model rows
    (effective context window / max output / capability flags after overrides),
    per-row `override_active` + `override_keys` badges, and the raw stored
    `model_overrides` config section for prefilling the editor.
    """
    try:
        with _config_profile_scope(profile):
            return await run_in_threadpool(_model_catalog_rows_sync)
    except HTTPException:
        raise
    except Exception:
        _log.exception("GET /api/model/catalog failed")
        raise HTTPException(status_code=500, detail="Failed to build model catalog")


@router.post("/api/model/overrides")
async def set_model_override(body: ModelOverrideUpsert, profile: Optional[str] = None):
    """Set (or clear, when every field is unset) one model's override.

    Writes the canonical schema into config.yaml's `model_overrides` section
    via read_raw_config → save_config, so the commented YAML file survives
    (save_config re-serializes the raw dict; other sections are untouched).
    Validation mirrors the engine: positive ints for the numeric fields,
    empty model_family is dropped.
    """
    provider = (body.provider or "").strip()
    model = (body.model or "").strip()
    if not provider or not model:
        raise HTTPException(status_code=400, detail="provider and model are required")
    if any(ch in provider for ch in ("\n", "\t")) or provider.startswith("_"):
        raise HTTPException(status_code=400, detail="invalid provider key")
    if any(ch in model for ch in ("\n", "\t")) or model.startswith("_"):
        raise HTTPException(status_code=400, detail="invalid model key (keys starting with '_' are reserved)")

    o = body.overrides
    entry: Dict[str, Any] = {}
    if o.context_window is not None:
        if o.context_window <= 0:
            raise HTTPException(status_code=400, detail="context_window must be a positive integer")
        entry["context_window"] = int(o.context_window)
    if o.max_output_tokens is not None:
        if o.max_output_tokens <= 0:
            raise HTTPException(status_code=400, detail="max_output_tokens must be a positive integer")
        entry["max_output_tokens"] = int(o.max_output_tokens)
    for b in ("supports_tools", "supports_vision", "supports_reasoning"):
        v = getattr(o, b)
        if v is not None:
            entry[b] = bool(v)
    if o.model_family is not None and str(o.model_family).strip():
        entry["model_family"] = str(o.model_family).strip()

    def _apply() -> Dict[str, Any]:
        with _profile_scope(body.profile or profile):
            cfg = read_raw_config()
            sections = cfg.get("model_overrides")
            sections = sections if isinstance(sections, dict) else {}
            if not entry:
                # All fields unset → clear this model's override.
                prov_section = sections.get(provider)
                if isinstance(prov_section, dict) and model in prov_section:
                    removed = prov_section.pop(model)
                    if not prov_section:
                        sections.pop(provider, None)
                    cfg["model_overrides"] = sections
                    save_config(cfg)
                    return {"ok": True, "removed": True, "entry": removed}
                return {"ok": True, "removed": False}
            prov_section = sections.get(provider)
            prov_section = prov_section if isinstance(prov_section, dict) else {}
            prov_section[model] = entry
            sections[provider] = prov_section
            cfg["model_overrides"] = sections
            save_config(cfg)
            return {"ok": True, "provider": provider, "model": model, "entry": entry}

    try:
        return await run_in_threadpool(_apply)
    except HTTPException:
        raise
    except Exception:
        _log.exception("POST /api/model/overrides failed")
        raise HTTPException(status_code=500, detail="Failed to save model override")


@router.delete("/api/model/overrides")
async def delete_model_overrides(body: ModelOverrideDelete, profile: Optional[str] = None):
    """Remove overrides: one model (provider+model), a provider's whole
    section (model empty), or every override (both empty)."""
    provider = (body.provider or "").strip()
    model = (body.model or "").strip()

    def _apply() -> Dict[str, Any]:
        with _profile_scope(body.profile or profile):
            cfg = read_raw_config()
            sections = cfg.get("model_overrides")
            sections = sections if isinstance(sections, dict) else {}
            if not provider:
                if not sections:
                    return {"ok": True, "removed": 0}
                removed = sum(len(v) for v in sections.values() if isinstance(v, dict))
                cfg.pop("model_overrides", None)
                save_config(cfg)
                return {"ok": True, "removed": removed}
            section = sections.get(provider)
            if not isinstance(section, dict):
                return {"ok": True, "removed": 0}
            if model:
                if model in section:
                    section.pop(model)
                    removed = 1
                else:
                    removed = 0
                if not section:
                    sections.pop(provider, None)
            else:
                removed = len(section)
                sections.pop(provider, None)
            if sections:
                cfg["model_overrides"] = sections
            else:
                cfg.pop("model_overrides", None)
            save_config(cfg)
            return {"ok": True, "provider": provider, "removed": removed}

    try:
        return await run_in_threadpool(_apply)
    except HTTPException:
        raise
    except Exception:
        _log.exception("DELETE /api/model/overrides failed")
        raise HTTPException(status_code=500, detail="Failed to delete model overrides")
