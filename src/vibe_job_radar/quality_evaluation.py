"""Completeness-gated metrics for independently entered review annotations."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .quality_packet import (
    NEGATIVE, REVIEW_STATES, anchor, decode_json, label_list, load_packet,
    positive_ai, require, sha256, validate_span, write_new,
)
from .utils import json_text, parse_time, utc_now

DOCUMENT_KEYS = {"record_id", "review_state", "author", "reviewed_at", "roles",
                 "requirements_complete", "requirements", "notes"}
REQUIREMENT_KEYS = {"start", "end", "quote", "capability", "relation", "strength"}
PAIR_KEYS = {"left", "right", "same_job", "review_state", "author", "reviewed_at", "notes"}


def _review(row):
    require(isinstance(row["review_state"], str) and row["review_state"] in REVIEW_STATES,
            "unknown review state")
    require(all(isinstance(row[k], str) for k in ("author", "reviewed_at", "notes")),
            "review author/date/notes must be strings")
    if row["review_state"] != "pending":
        require(bool(row["author"].strip()) and bool(row["reviewed_at"].strip()),
                "review author and timestamp required")
    if row["reviewed_at"]:
        parse_time(row["reviewed_at"])


def validate_annotations(value, manifest, data):
    require(type(value) is dict and set(value) == {"schema_version", "packet_id", "documents", "dedup_pairs"},
            "unknown or missing annotation fields")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1, "unsupported annotations")
    require(value["packet_id"] == manifest["packet_id"], "annotations belong to another packet")
    require(type(value["documents"]) is list and type(value["dedup_pairs"]) is list,
            "annotations documents/pairs must be arrays")
    corpus = {d["record_id"]: d for d in data["corpus"]}
    documents = {}
    for row in value["documents"]:
        require(type(row) is dict and set(row) == DOCUMENT_KEYS, "unknown or missing document fields")
        rid = row["record_id"]
        require(isinstance(rid, str) and rid in corpus and rid not in documents,
                "unknown or repeated annotation record")
        _review(row)
        require(type(row["requirements_complete"]) is bool and type(row["requirements"]) is list,
                "invalid requirement completeness/list")
        if row["roles"] is not None:
            label_list(row["roles"], data["labels"]["roles"], "annotated roles")
        seen = set()
        for req in row["requirements"]:
            require(type(req) is dict and set(req) == REQUIREMENT_KEYS, "unknown or missing requirement fields")
            validate_span(req, corpus[rid]["text"], data["labels"]["capabilities"])
            require(anchor(req) not in seen, "repeated/conflicting annotated span")
            seen.add(anchor(req))
        documents[rid] = row
    require(set(documents) == set(corpus), "all packet documents must remain in the annotation file")
    seen_pairs = set()
    for row in value["dedup_pairs"]:
        require(type(row) is dict and set(row) == PAIR_KEYS, "unknown or missing pair fields")
        _review(row)
        left, right = row["left"], row["right"]
        require(isinstance(left, str) and isinstance(right, str) and
                left in corpus and right in corpus and left != right, "invalid dedup pair records")
        pair = tuple(sorted((left, right)))
        require(pair not in seen_pairs, "repeated/reversed dedup pair")
        seen_pairs.add(pair)
        require(row["same_job"] is None or type(row["same_job"]) is bool, "same_job must be boolean/null")
        require(row["review_state"] != "human_confirmed" or type(row["same_job"]) is bool,
                "confirmed pair needs explicit same_job")
    return documents


def _fraction(top, bottom):
    return top / bottom if bottom else None


def counts(tp, fp, fn):
    return {"tp": tp, "fp": fp, "fn": fn, "predicted": tp + fp, "gold": tp + fn,
            "precision": _fraction(tp, tp + fp), "recall": _fraction(tp, tp + fn),
            "f1": _fraction(2 * tp, 2 * tp + fp + fn)}


def _key(rid, row, semantic=False):
    return (rid, *anchor(row), *((row["relation"], row["strength"]) if semantic else ()))


def _error_key(key):
    value = dict(zip(("record_id", "start", "end", "capability", "relation", "strength"), key))
    return value


def compare(predicted, gold):
    return {**counts(len(predicted & gold), len(predicted - gold), len(gold - predicted)),
            "false_positives": [_error_key(k) for k in sorted(predicted - gold)],
            "false_negatives": [_error_key(k) for k in sorted(gold - predicted)]}


def evaluate_packet(packet: str | Path, annotations: str | Path | None = None):
    manifest, data = load_packet(packet)
    annotation_path = Path(annotations) if annotations is not None else Path(packet) / "annotations.json"
    raw = annotation_path.read_bytes()
    value = decode_json(raw)
    documents = validate_annotations(value, manifest, data)
    corpus = {d["record_id"]: d for d in data["corpus"]}
    human = {rid for rid, row in documents.items() if row["review_state"] == "human_confirmed"}
    complete = {rid for rid in human if documents[rid]["requirements_complete"]}
    extraction_ids = complete & {rid for rid, d in corpus.items() if d["extraction_eligible"]}
    role_ids = {rid for rid in human if documents[rid]["roles"] is not None
                and corpus[rid]["classification_eligible"]}
    prediction_rows = [r for r in data["predictions"] if r["record_id"] in extraction_ids]
    gold_rows = [(rid, r) for rid in sorted(extraction_ids) for r in documents[rid]["requirements"]]
    pred = {_key(r["record_id"], r) for r in prediction_rows}
    gold = {_key(rid, r) for rid, r in gold_rows}
    pred_semantic = {_key(r["record_id"], r, True) for r in prediction_rows}
    gold_semantic = {_key(rid, r, True) for rid, r in gold_rows}
    accepted = {_key(r["record_id"], r, True) for r in prediction_rows if r["accepted_positive_ai"]}
    gold_ai = {_key(rid, r, True) for rid, r in gold_rows if positive_ai(r)}
    pred_map = {_key(r["record_id"], r): r for r in prediction_rows}
    gold_map = {_key(rid, r): r for rid, r in gold_rows}
    negative_gold = {key for key, row in gold_map.items() if row["strength"] in NEGATIVE}
    negative_pred = {key for key, row in pred_map.items() if row["strength"] in NEGATIVE}
    aligned_negative = negative_gold & pred
    correct_negative = sum(pred_map[k]["strength"] == gold_map[k]["strength"] for k in aligned_negative)
    negation = {
        "aligned_negative_gold": len(aligned_negative), "correct_strength": correct_negative,
        "accuracy": _fraction(correct_negative, len(aligned_negative)),
        "missed_negative_gold": len(negative_gold - pred),
        "spurious_negative_predictions": len(negative_pred - negative_gold),
        "wrong_negative_kind": sum(pred_map[k]["strength"] != gold_map[k]["strength"]
                                   for k in negative_pred & negative_gold),
        "missed": [_error_key(k) for k in sorted(negative_gold - pred)],
        "spurious": [_error_key(k) for k in sorted(negative_pred - negative_gold)],
    }
    role_scope = set(data["source"]["role_scope"])
    per_role = {role: [0, 0, 0] for role in sorted(role_scope)}
    exact_roles, role_errors = 0, []
    for rid in sorted(role_ids):
        predicted = set(corpus[rid]["predicted_roles"])
        annotated = set(documents[rid]["roles"]) & role_scope
        exact_roles += predicted == annotated
        if predicted != annotated:
            role_errors.append({"record_id": rid, "predicted": sorted(predicted), "gold": sorted(annotated)})
        for role, c in per_role.items():
            c[0] += role in predicted and role in annotated
            c[1] += role in predicted and role not in annotated
            c[2] += role not in predicted and role in annotated
    role_totals = [sum(c[i] for c in per_role.values()) for i in range(3)]
    pair_states, pair_counts, pair_errors, incomparable = Counter(), [0, 0, 0], [], []
    comparable, correct_pairs = 0, 0
    for pair in value["dedup_pairs"]:
        pair_states[pair["review_state"]] += 1
        if pair["review_state"] != "human_confirmed":
            continue
        left, right = corpus[pair["left"]], corpus[pair["right"]]
        if not left["dedup_eligible"] or not right["dedup_eligible"]:
            incomparable.append({"left": pair["left"], "right": pair["right"],
                                 "reason": "requires_original_selected_full_text_groups"})
            continue
        predicted = left["group_key"] == right["group_key"]
        annotated = pair["same_job"]
        comparable += 1
        correct_pairs += predicted == annotated
        pair_counts[0] += predicted and annotated
        pair_counts[1] += predicted and not annotated
        pair_counts[2] += not predicted and annotated
        if predicted != annotated:
            pair_errors.append({"left": pair["left"], "right": pair["right"],
                                "predicted_same_job": predicted, "gold_same_job": annotated})
    states = Counter(row["review_state"] for row in documents.values())
    coverage = {
        "documents": len(corpus), "original_prediction_rows": len(data["predictions"]),
        "review_states": {state: states[state] for state in sorted(REVIEW_STATES)},
        "extraction_eligible_documents": sum(d["extraction_eligible"] for d in corpus.values()),
        "extraction_evaluated_documents": len(extraction_ids),
        "human_complete_documents": len(complete),
        "human_complete_but_extraction_ineligible": len(complete - extraction_ids),
        "human_partial_documents": len(human - complete),
        "classification_eligible_documents": sum(d["classification_eligible"] for d in corpus.values()),
        "classification_evaluated_documents": len(role_ids),
        "roles_declared_but_ineligible": sum(documents[r]["roles"] is not None for r in human - role_ids),
    }
    return {
        "schema_version": 1, "packet_id": manifest["packet_id"], "created_at": utc_now(),
        "annotations_sha256": sha256(raw), "source_mode": data["source"]["mode"],
        "coverage": coverage,
        "extraction": {"span_capability": compare(pred, gold),
                       "semantic": compare(pred_semantic, gold_semantic),
                       "accepted_positive_ai": compare(accepted, gold_ai), "negation": negation},
        "classification": {"role_scope": sorted(role_scope), "reviewed_documents": len(role_ids),
                           "micro": counts(*role_totals),
                           "exact_match": {"correct": exact_roles, "total": len(role_ids),
                                           "accuracy": _fraction(exact_roles, len(role_ids))},
                           "per_role": {role: counts(*c) for role, c in per_role.items()}, "errors": role_errors},
        "deduplication": {"declared_pairs": len(value["dedup_pairs"]),
                          "review_states": {s: pair_states[s] for s in sorted(REVIEW_STATES)},
                          "comparable_human_pairs": comparable, "unassessable_pairs": incomparable,
                          "scores": counts(*pair_counts), "correct_pairs": correct_pairs,
                          "accuracy": _fraction(correct_pairs, comparable), "errors": pair_errors},
        "limitations": [
            "Only explicitly human-confirmed labels count; reviewer identity is a declaration, not verified by this tool.",
            "Extraction uses completed selected full-text representatives; this is not end-to-end pipeline recall.",
            "Strict Unicode span boundaries; a boundary mismatch counts as FP and FN.",
            "All serialized original rows enter span/semantic metrics; only accepted positive AI rows enter accepted_positive_ai.",
            "This evaluates the pinned report output, which may include earlier business reviews, not necessarily a pure rule engine.",
            "Metrics cover reviewed subsets and declared comparable pairs, not the market, whole corpus or personal competence.",
        ],
    }


def write_evaluation(packet: str | Path, output: str | Path, annotations: str | Path | None = None):
    output = Path(output).resolve()
    _, data = load_packet(packet)
    report = Path(data["source"]["report_path"]).resolve()
    require(output != report and report not in output.parents, "evaluation must be outside the original report")
    require(not output.exists(), "evaluation output must be a new file")
    result = evaluate_packet(packet, annotations)
    output.parent.mkdir(parents=True, exist_ok=True)
    write_new(output, (json_text(result) + "\n").encode("utf-8"))
    return result
