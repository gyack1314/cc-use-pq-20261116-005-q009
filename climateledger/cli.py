from __future__ import annotations

from argparse import ArgumentParser, Namespace
from pathlib import Path
from typing import Any
import json
import sys

from .intake import write_manifest
from .registry import ContractError
from .service import LedgerService


def parser() -> ArgumentParser:
    root = ArgumentParser(prog="climateledger", description="Offline climate indicator batch ledger")
    root.add_argument("--db", default="climateledger.sqlite3", help="SQLite database path")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("indicator", "source", "unit"):
        sub = commands.add_parser("register-" + name)
        sub.add_argument("contract", help="local UTF-8 JSON contract")
    ingest = commands.add_parser("ingest")
    ingest.add_argument("manifest", help="local batch manifest")
    approve = commands.add_parser("approve")
    approve.add_argument("batch", type=int)
    approve.add_argument("revision", type=int)
    preview = commands.add_parser("preview")
    preview.add_argument("batch", type=int)
    preview.add_argument("revision", type=int)
    publish = commands.add_parser("publish")
    publish.add_argument("batch", type=int)
    publish.add_argument("revision", type=int)
    batch = commands.add_parser("batch")
    batch.add_argument("id", type=int)
    batches = commands.add_parser("batches")
    batches.add_argument("--status")
    exported = commands.add_parser("export")
    exported.add_argument("version", type=int)
    exported.add_argument("--output", help="write bundle to a local file")
    verify = commands.add_parser("verify")
    verify.add_argument("bundle", help="exported local bundle")
    lineage = commands.add_parser("lineage")
    lineage.add_argument("version", type=int)
    series = commands.add_parser("series")
    series.add_argument("indicator")
    series.add_argument("--version", type=int)
    series.add_argument("--source")
    events = commands.add_parser("events")
    events.add_argument("--subject")
    commands.add_parser("status")
    compare = commands.add_parser("compare")
    compare.add_argument("old_version", type=int)
    compare.add_argument("new_version", type=int)
    impact = commands.add_parser("impact")
    impact.add_argument("batch", type=int)
    stats = commands.add_parser("statistics")
    stats.add_argument("indicator")
    stats.add_argument("version", type=int)
    stats.add_argument("--source")
    commands.add_parser("audit")
    coverage = commands.add_parser("coverage")
    coverage.add_argument("version", type=int)
    coverage.add_argument("--indicator")
    coverage.add_argument("--source")
    batch_coverage = commands.add_parser("batch-coverage")
    batch_coverage.add_argument("batch", type=int)
    discrepancies = commands.add_parser("discrepancies")
    discrepancies.add_argument("version", type=int)
    discrepancies.add_argument("indicator")
    replay = commands.add_parser("replay")
    replay.add_argument("manifest")
    replay.add_argument("batch", type=int)
    pending = commands.add_parser("pending-review")
    pending.add_argument("--source")
    rejected = commands.add_parser("rejected")
    rejected.add_argument("--source")
    history = commands.add_parser("history")
    history.add_argument("source")
    history.add_argument("external_id")
    receipt = commands.add_parser("receipt")
    receipt.add_argument("kind", choices=("source", "indicator", "batch", "release"))
    receipt.add_argument("key")
    demo = commands.add_parser("demo")
    demo.add_argument("--workspace", default="fixtures")
    return root


def read_json(path: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("input_json", f"cannot read JSON: {exc}") from None
    if not isinstance(value, dict):
        raise ContractError("input_shape", "contract must be a JSON object")
    return value


def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def demo(workspace: str) -> dict[str, Any]:
    root = Path(workspace)
    db = root / "demo.sqlite3"
    root.mkdir(parents=True, exist_ok=True)
    with LedgerService(db) as service:
        service.register_indicator({"key": "co2_ppm", "title": "Synthetic carbon dioxide",
                                    "canonical_unit": "ppm", "cadence": "month", "minimum": "0",
                                    "maximum": "1000", "precision": 2})
        service.register_source({"key": "sample_lab", "title": "Synthetic local lab",
                                 "license": "Local synthetic fixture", "allowed_indicators": ["co2_ppm"]})
        service.register_unit({"indicator": "co2_ppm", "source_unit": "ppb", "factor": "0.001"})
        rows = [{"indicator": "co2_ppm", "period": "2026-01", "value": "428000", "unit": "ppb"},
                {"indicator": "co2_ppm", "period": "2026-02", "value": "429000", "unit": "ppb"}]
        manifest = write_manifest(root, source="sample_lab", batch_id="demo_batch", rows=rows)
        ingested = service.ingest(manifest)
        batch = ingested["batch"]
        if batch["status"] not in {"accepted", "published"}:
            raise ContractError("demo_quality", "synthetic batch did not pass quality gate")
        release = service.publish(batch["id"], batch["revision"])
        bundle = service.export(release["version"])
        if not service.verify(bundle) or len(bundle["entries"]) != 2:
            raise ContractError("demo_verify", "export could not be verified")
        return {"batch_id": batch["id"], "idempotent": ingested["idempotent"],
                "release_version": release["version"], "digest": release["digest"],
                "entries": len(bundle["entries"]), "verified": True}


def execute(args: Namespace) -> Any:
    if args.command == "demo":
        return demo(args.workspace)
    with LedgerService(args.db) as service:
        if args.command == "register-indicator":
            return service.register_indicator(read_json(args.contract))
        if args.command == "register-source":
            return service.register_source(read_json(args.contract))
        if args.command == "register-unit":
            return service.register_unit(read_json(args.contract))
        if args.command == "ingest":
            return service.ingest(args.manifest)
        if args.command == "approve":
            return service.approve(args.batch, args.revision)
        if args.command == "preview":
            return service.preview(args.batch, args.revision)
        if args.command == "publish":
            return service.publish(args.batch, args.revision)
        if args.command == "batch":
            return service.batch(args.id)
        if args.command == "batches":
            return service.batches(args.status)
        if args.command == "export":
            bundle = service.export(args.version)
            if args.output:
                Path(args.output).write_text(json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
                return {"path": args.output, "digest": bundle["digest"]}
            return bundle
        if args.command == "verify":
            bundle = read_json(args.bundle)
            return {"verified": service.verify(bundle)}
        if args.command == "lineage":
            return service.lineage(args.version)
        if args.command == "series":
            return service.series(args.indicator, args.version, args.source)
        if args.command == "events":
            return service.events(args.subject)
        if args.command == "status":
            return service.status()
        if args.command == "compare":
            return service.compare(args.old_version, args.new_version)
        if args.command == "impact":
            return service.impact(args.batch)
        if args.command == "statistics":
            return service.statistics(args.indicator, args.version, args.source)
        if args.command == "audit":
            return service.audit_all()
        if args.command == "coverage":
            return service.coverage(args.version, args.indicator, args.source)
        if args.command == "batch-coverage":
            return service.batch_coverage_report(args.batch)
        if args.command == "discrepancies":
            return service.discrepancies(args.version, args.indicator)
        if args.command == "replay":
            return service.replay(args.manifest, args.batch)
        if args.command == "pending-review":
            return service.pending_review(args.source)
        if args.command == "rejected":
            return service.rejected_batches(args.source)
        if args.command == "history":
            return service.batch_history(args.source, args.external_id)
        if args.command == "receipt":
            if args.kind == "source":
                return service.receipts.source_summary(args.key)
            if args.kind == "indicator":
                return service.receipts.indicator_summary(args.key)
            if args.kind == "batch":
                return service.receipts.batch_receipt(int(args.key))
            return service.receipts.release_receipt(int(args.key))
    raise ContractError("unknown_command", "unrecognized command")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        emit(execute(args))
        return 0
    except (ContractError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
