import hashlib
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_SIMPLE_TOKEN_PATTERN = re.compile(r"\w+|[^\w\s]", re.UNICODE)


class TokenTracker:
    def __init__(
        self,
        *,
        enabled: bool,
        log_path: Path,
        tokenizer_name: str,
        hash_filepaths: bool,
        include_vault_stats: bool,
        vault_path: Path | None,
        ignored_folders: list[str] | None,
    ) -> None:
        self.enabled = enabled
        self.log_path = log_path
        self.tokenizer_name = tokenizer_name
        self.hash_filepaths = hash_filepaths
        self.include_vault_stats = include_vault_stats
        self.vault_path = vault_path
        self.ignored_folders = set(ignored_folders or [])
        self._lock = threading.Lock()
        self._encoding = None

        if self.enabled and tokenizer_name == "cl100k_base":
            try:
                import tiktoken

                self._encoding = tiktoken.get_encoding("cl100k_base")
            except ImportError:
                self.tokenizer_name = "simple"

    @classmethod
    def from_config(
        cls,
        *,
        config: dict[str, Any],
        base_dir: Path,
        vault_path: Path | None,
        ignored_folders: list[str] | None,
    ) -> "TokenTracker":
        telemetry_config = config.get("telemetry", {})
        log_path = Path(telemetry_config.get("log_path", ".mcp-telemetry/tool_usage.jsonl"))
        if not log_path.is_absolute():
            log_path = (base_dir / log_path).resolve()

        return cls(
            enabled=telemetry_config.get("enabled", False),
            log_path=log_path,
            tokenizer_name=telemetry_config.get("tokenizer", "cl100k_base"),
            hash_filepaths=telemetry_config.get("hash_filepaths", True),
            include_vault_stats=telemetry_config.get("include_vault_stats", True),
            vault_path=vault_path,
            ignored_folders=ignored_folders,
        )

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        if self._encoding is not None:
            return len(self._encoding.encode(text))
        return len(_SIMPLE_TOKEN_PATTERN.findall(text))

    def count_json_tokens(self, payload: dict[str, Any]) -> int:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return self.count_tokens(serialized)

    def path_ref(self, path_value: str | None) -> str | None:
        if path_value is None:
            return None
        if not self.hash_filepaths:
            return path_value
        digest = hashlib.sha256(path_value.encode("utf-8")).hexdigest()
        return digest[:16]

    def _vault_snapshot(self) -> dict[str, int]:
        if not self.include_vault_stats or not self.vault_path or not self.vault_path.exists():
            return {}

        note_count = 0
        total_md_bytes = 0
        for root, dirs, filenames in os.walk(self.vault_path):
            dirs[:] = [d for d in dirs if d not in self.ignored_folders and not d.startswith(".")]
            for name in filenames:
                if not name.endswith(".md"):
                    continue
                note_count += 1
                file_path = Path(root) / name
                try:
                    total_md_bytes += file_path.stat().st_size
                except OSError:
                    continue

        return {
            "vault_note_count": note_count,
            "vault_total_md_bytes": total_md_bytes,
        }

    def log_call(
        self,
        *,
        kind: str,
        name: str,
        args: dict[str, Any],
        result: str,
        duration_ms: float,
        status: str = "ok",
        meta: dict[str, Any] | None = None,
        baseline_result_tokens: int | None = None,
    ) -> None:
        if not self.enabled:
            return

        arg_tokens = self.count_json_tokens(args)
        result_tokens = self.count_tokens(result)
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "name": name,
            "status": status,
            "duration_ms": round(duration_ms, 3),
            "tokenizer": self.tokenizer_name,
            "arg_tokens": arg_tokens,
            "result_tokens": result_tokens,
            "total_tokens": arg_tokens + result_tokens,
        }

        if baseline_result_tokens is not None:
            saved_tokens = max(baseline_result_tokens - result_tokens, 0)
            record["baseline_result_tokens"] = baseline_result_tokens
            record["saved_result_tokens"] = saved_tokens
            record["saved_result_ratio"] = round(saved_tokens / baseline_result_tokens, 4) if baseline_result_tokens else 0.0

        if meta:
            record["meta"] = meta

        record.update(self._vault_snapshot())

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _parse_timestamp(self, value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            normalized = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None

    def _read_records(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []

        records: list[dict[str, Any]] = []
        with open(self.log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    records.append(payload)
        return records

    def summarize_records(
        self,
        *,
        days: int = 30,
        include_prompts: bool = True,
        name_filter: str = "",
        exclude_names: set[str] | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days) if days > 0 else None
        exclude_names = exclude_names or set()
        records = self._read_records()

        totals = {
            "calls": 0,
            "ok_calls": 0,
            "error_calls": 0,
            "arg_tokens": 0,
            "result_tokens": 0,
            "total_tokens": 0,
            "baseline_result_tokens": 0,
            "saved_result_tokens": 0,
            "duration_ms": 0.0,
        }
        by_name: dict[str, dict[str, Any]] = {}
        first_ts: datetime | None = None
        last_ts: datetime | None = None
        latest_vault_note_count: int | None = None
        latest_vault_total_md_bytes: int | None = None

        for record in records:
            name = str(record.get("name", "unknown"))
            kind = str(record.get("kind", "tool"))
            if name in exclude_names:
                continue
            if not include_prompts and kind == "prompt":
                continue
            if name_filter and name != name_filter:
                continue

            ts = self._parse_timestamp(record.get("ts"))
            if cutoff and ts and ts < cutoff:
                continue

            if ts and (first_ts is None or ts < first_ts):
                first_ts = ts
            if ts and (last_ts is None or ts > last_ts):
                last_ts = ts

            latest_vault_note_count = record.get("vault_note_count", latest_vault_note_count)
            latest_vault_total_md_bytes = record.get("vault_total_md_bytes", latest_vault_total_md_bytes)

            entry = by_name.setdefault(
                name,
                {
                    "name": name,
                    "kind": kind,
                    "calls": 0,
                    "ok_calls": 0,
                    "error_calls": 0,
                    "arg_tokens": 0,
                    "result_tokens": 0,
                    "total_tokens": 0,
                    "baseline_result_tokens": 0,
                    "saved_result_tokens": 0,
                    "duration_ms": 0.0,
                },
            )

            status = str(record.get("status", "ok"))
            arg_tokens = int(record.get("arg_tokens", 0) or 0)
            result_tokens = int(record.get("result_tokens", 0) or 0)
            total_tokens = int(record.get("total_tokens", 0) or 0)
            baseline_tokens = int(record.get("baseline_result_tokens", 0) or 0)
            saved_tokens = int(record.get("saved_result_tokens", 0) or 0)
            duration_ms = float(record.get("duration_ms", 0.0) or 0.0)

            entry["calls"] += 1
            entry["ok_calls"] += 1 if status == "ok" else 0
            entry["error_calls"] += 0 if status == "ok" else 1
            entry["arg_tokens"] += arg_tokens
            entry["result_tokens"] += result_tokens
            entry["total_tokens"] += total_tokens
            entry["baseline_result_tokens"] += baseline_tokens
            entry["saved_result_tokens"] += saved_tokens
            entry["duration_ms"] += duration_ms

            totals["calls"] += 1
            totals["ok_calls"] += 1 if status == "ok" else 0
            totals["error_calls"] += 0 if status == "ok" else 1
            totals["arg_tokens"] += arg_tokens
            totals["result_tokens"] += result_tokens
            totals["total_tokens"] += total_tokens
            totals["baseline_result_tokens"] += baseline_tokens
            totals["saved_result_tokens"] += saved_tokens
            totals["duration_ms"] += duration_ms

        tool_rows = []
        for entry in by_name.values():
            calls = entry["calls"]
            baseline = entry["baseline_result_tokens"]
            entry["avg_duration_ms"] = round(entry["duration_ms"] / calls, 2) if calls else 0.0
            entry["saved_result_ratio"] = round(entry["saved_result_tokens"] / baseline, 4) if baseline else None
            tool_rows.append(entry)

        tool_rows.sort(
            key=lambda item: (
                item["saved_result_tokens"],
                item["total_tokens"],
                item["calls"],
                item["name"],
            ),
            reverse=True,
        )

        totals["avg_duration_ms"] = round(totals["duration_ms"] / totals["calls"], 2) if totals["calls"] else 0.0
        totals["saved_result_ratio"] = (
            round(totals["saved_result_tokens"] / totals["baseline_result_tokens"], 4)
            if totals["baseline_result_tokens"]
            else None
        )

        return {
            "generated_at": now.isoformat(),
            "telemetry_enabled": self.enabled,
            "log_path": str(self.log_path),
            "log_exists": self.log_path.exists(),
            "tokenizer": self.tokenizer_name,
            "days": days,
            "include_prompts": include_prompts,
            "name_filter": name_filter,
            "record_count": totals["calls"],
            "first_ts": first_ts.isoformat() if first_ts else None,
            "last_ts": last_ts.isoformat() if last_ts else None,
            "latest_vault_note_count": latest_vault_note_count,
            "latest_vault_total_md_bytes": latest_vault_total_md_bytes,
            "totals": totals,
            "by_name": tool_rows,
        }

    def render_markdown_report(self, summary: dict[str, Any]) -> str:
        totals = summary["totals"]
        lines = [
            "# MCP Token Usage Report",
            "",
            f"- Generated: {summary['generated_at']}",
            f"- Telemetry enabled: {'yes' if summary['telemetry_enabled'] else 'no'}",
            f"- Log path: `{summary['log_path']}`",
            f"- Time window: last {summary['days']} day(s)" if summary["days"] > 0 else "- Time window: all recorded events",
            f"- Include prompts: {'yes' if summary['include_prompts'] else 'no'}",
            f"- Tokenizer: `{summary['tokenizer']}`",
        ]

        if summary.get("name_filter"):
            lines.append(f"- Filtered tool: `{summary['name_filter']}`")
        if summary.get("first_ts"):
            lines.append(f"- First event in window: {summary['first_ts']}")
        if summary.get("last_ts"):
            lines.append(f"- Last event in window: {summary['last_ts']}")
        if summary.get("latest_vault_note_count") is not None:
            lines.append(f"- Vault notes: {summary['latest_vault_note_count']}")
        if summary.get("latest_vault_total_md_bytes") is not None:
            lines.append(f"- Vault markdown bytes: {summary['latest_vault_total_md_bytes']}")

        lines.extend(
            [
                "",
                "## Summary",
                "",
                f"- Calls: {totals['calls']}",
                f"- Successful calls: {totals['ok_calls']}",
                f"- Error calls: {totals['error_calls']}",
                f"- Total tokens: {totals['total_tokens']}",
                f"- Arg tokens: {totals['arg_tokens']}",
                f"- Result tokens: {totals['result_tokens']}",
                f"- Estimated baseline result tokens: {totals['baseline_result_tokens']}",
                f"- Estimated saved result tokens: {totals['saved_result_tokens']}",
                f"- Estimated saved result ratio: {self._format_ratio(totals['saved_result_ratio'])}",
                f"- Average duration: {totals['avg_duration_ms']} ms",
            ]
        )

        if not summary["log_exists"]:
            lines.extend(
                [
                    "",
                    "## Status",
                    "",
                    "- No telemetry log file exists yet.",
                ]
            )
            return "\n".join(lines)

        if not summary["by_name"]:
            lines.extend(
                [
                    "",
                    "## Status",
                    "",
                    "- No telemetry events matched the current filter.",
                ]
            )
            return "\n".join(lines)

        lines.extend(
            [
                "",
                "## By Tool",
                "",
                "| Tool | Kind | Calls | Errors | Total Tokens | Saved Tokens | Saved % | Avg ms |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )

        for entry in summary["by_name"]:
            lines.append(
                "| {name} | {kind} | {calls} | {error_calls} | {total_tokens} | {saved_result_tokens} | {saved_result_ratio} | {avg_duration_ms} |".format(
                    name=entry["name"],
                    kind=entry["kind"],
                    calls=entry["calls"],
                    error_calls=entry["error_calls"],
                    total_tokens=entry["total_tokens"],
                    saved_result_tokens=entry["saved_result_tokens"],
                    saved_result_ratio=self._format_ratio(entry["saved_result_ratio"]),
                    avg_duration_ms=entry["avg_duration_ms"],
                )
            )

        return "\n".join(lines)

    def _format_ratio(self, ratio: float | None) -> str:
        if ratio is None:
            return "n/a"
        return f"{round(ratio * 100, 2)}%"
