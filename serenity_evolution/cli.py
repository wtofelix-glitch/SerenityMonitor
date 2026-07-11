from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .candidate import FactorEvidence, WeightCandidateGenerator
from .engine import EvolutionEngine, StrategySeries
from .store import EvolutionStore


def _load(path: str) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _series(path: str) -> StrategySeries:
    payload = _load(path)
    if not isinstance(payload, dict):
        raise ValueError("series file must contain a JSON object")
    return StrategySeries(
        daily_returns=payload["daily_returns"],
        trade_returns=payload["trade_returns"],
        turnover=float(payload["turnover"]),
        executable_rate=float(payload["executable_rate"]),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serenity safety-first evolution controller")
    parser.add_argument("--db", default="serenity.db", help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sub.add_parser("status")

    propose = sub.add_parser("propose")
    propose.add_argument("--baseline-version", required=True)
    propose.add_argument("--weights", required=True, help="JSON object: factor -> weight")
    propose.add_argument("--ic", required=True, help="JSON object: factor -> IC value array")

    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--candidate-id", required=True)
    evaluate.add_argument("--candidate-series", required=True)
    evaluate.add_argument("--baseline-series", required=True)
    evaluate.add_argument("--data-quality-ok", action="store_true")
    evaluate.add_argument("--costs-included", action="store_true")
    evaluate.add_argument("--market-rules-included", action="store_true")

    promote = sub.add_parser("promote-live")
    promote.add_argument("--candidate-id", required=True)
    promote.add_argument("--approval-ref", required=True)
    promote.add_argument("--canary-days", type=int, required=True)
    promote.add_argument(
        "--enable-live-apply",
        action="store_true",
        help="Deliberate second switch; keep absent until all launch gates are approved",
    )

    rollback = sub.add_parser("rollback-live")
    rollback.add_argument("--reason", required=True)
    rollback.add_argument("--approval-ref", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = EvolutionStore(args.db)
    store.migrate()

    if args.command == "migrate":
        print(json.dumps({"ok": True, "schema_version": 1}))
        return 0
    if args.command == "status":
        print(json.dumps(store.status(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "propose":
        weights = _load(args.weights)
        ic = _load(args.ic)
        if not isinstance(weights, dict) or not isinstance(ic, dict):
            raise ValueError("weights and IC inputs must be JSON objects")
        evidence = [
            FactorEvidence(str(name), tuple(float(x) for x in values))
            for name, values in ic.items()
        ]
        candidate = WeightCandidateGenerator().generate(
            args.baseline_version,
            {str(name): float(value) for name, value in weights.items()},
            evidence,
        )
        EvolutionEngine(store).register(candidate)
        print(json.dumps(candidate.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "evaluate":
        comparison, result = EvolutionEngine(store).evaluate(
            args.candidate_id,
            _series(args.candidate_series),
            _series(args.baseline_series),
            data_quality_ok=args.data_quality_ok,
            costs_included=args.costs_included,
            market_rules_included=args.market_rules_included,
        )
        print(
            json.dumps(
                {"comparison": asdict(comparison), "gate": asdict(result)},
                ensure_ascii=False,
                indent=2,
                default=lambda item: item.value,
            )
        )
        return 0 if result.passed else 2
    if args.command == "promote-live":
        store.promote_live(
            args.candidate_id,
            approval_ref=args.approval_ref,
            canary_days=args.canary_days,
            live_apply_enabled=args.enable_live_apply,
        )
        print(json.dumps({"ok": True, "stage": "LIVE"}))
        return 0
    if args.command == "rollback-live":
        restored = store.rollback_live(reason=args.reason, approval_ref=args.approval_ref)
        print(json.dumps({"ok": True, "restored_candidate_id": restored}))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
