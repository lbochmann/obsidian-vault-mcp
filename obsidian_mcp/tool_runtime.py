import json
import time


def serialize_tool_result(payload) -> str:
    """Return plain strings unchanged and serialize structured payloads as JSON."""
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, indent=2)


def finalize_tracked_call(
    tracker,
    kind: str,
    name: str,
    started_at: float,
    args: dict,
    result,
    *,
    meta: dict | None = None,
    baseline_result_tokens: int | None = None,
    status: str = "ok",
) -> str:
    """Serialize a tool result, write telemetry, and return the serialized payload."""
    serialized_result = serialize_tool_result(result)
    tracker.log_call(
        kind=kind,
        name=name,
        args=args,
        result=serialized_result,
        duration_ms=(time.perf_counter() - started_at) * 1000,
        status=status,
        meta=meta,
        baseline_result_tokens=baseline_result_tokens,
    )
    return serialized_result


def tracked_error(
    tracker,
    kind: str,
    name: str,
    started_at: float,
    args: dict,
    result,
    *,
    meta: dict | None = None,
    baseline_result_tokens: int | None = None,
) -> str:
    """Record a tracked error result with consistent telemetry semantics."""
    return finalize_tracked_call(
        tracker,
        kind,
        name,
        started_at,
        args,
        result,
        meta=meta,
        baseline_result_tokens=baseline_result_tokens,
        status="error",
    )
