"""Evidence bundle for upload into Compliance Manager, Vanta or an auditor: approved documents, a manifest with hashes,
and (optionally) the customer's own Compliance Manager export filled in with status and notes."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import yaml
from openpyxl import Workbook, load_workbook

from ..models import Document, DutyResult, now_iso, stable_hash

MAPPING = Path(__file__).parent / "cm_mapping.yaml"


def load_mapping() -> dict[str, Any]:
    return yaml.safe_load(MAPPING.read_text(encoding="utf-8"))


def collect_approved(pipeline, tenant_id: str) -> list[Document]:
    """Latest approved, non-stale version of every document."""
    best: dict[tuple, Document] = {}
    for raw in pipeline.store.query("documents", tenant_id):
        d = Document.model_validate(raw)
        if d.state != "approved" or d.stale:
            continue
        key = (d.agent_id, d.doc_type, d.language, d.subject)
        if key not in best or best[key].version < d.version:
            best[key] = d
    return sorted(best.values(), key=lambda d: d.id)


def md_hash(pipeline, doc: Document) -> str:
    return stable_hash(pipeline.files.get(doc.file_paths["md"]).decode("utf-8"))


def verify_document(pipeline, tenant_id: str, doc_id: str) -> dict[str, Any]:
    """Compares the stored file with the hash recorded in the document and at approval."""
    raw = pipeline.store.get("documents", tenant_id, doc_id)
    if raw is None:
        raise KeyError(doc_id)
    d = Document.model_validate(raw)
    current = md_hash(pipeline, d)
    approvals = [a for a in pipeline.store.query("audit", tenant_id, documentId=doc_id, action="approve")]
    at_approval = approvals[-1].get("fileHash") if approvals else None
    return {
        "documentId": doc_id, "state": d.state, "currentHash": current, "documentHash": d.file_hash, "approvalHash": at_approval,
        "intact": current == d.file_hash and (at_approval is None or current == at_approval),
    }


def _status(duty: DutyResult | None, approved: bool, m: dict) -> tuple[str, str]:
    v = m["status_values"]
    if duty is None:
        return v["not_implemented"], v["not_assessed"]
    if duty.result == "fail":  # a failed check is reported even when no document is approved yet
        return v["not_implemented"], v["failed"]
    if duty.result == "unknown" or not approved:
        return v["not_implemented"], v["not_assessed"]
    return v["implemented"], v["passed"]


def rule_rows(pipeline, tenant_id: str, approved: list[Document], m: dict) -> list[dict[str, Any]]:
    """One row per mapped rule: worst result across agents, approved only if every applicable document is approved."""
    approved_ids = {(d.agent_id, d.doc_type, d.language, d.subject) for d in approved}
    duties = [DutyResult.model_validate(x) for x in pipeline.store.query("dutyResults", tenant_id)]
    rows = []
    for rule_id, keywords in m["actions"].items():
        ds = [d for d in duties if d.rule_id == rule_id and d.applicability == "applies"]
        if not ds:
            continue
        rule = pipeline.rule_set.get(rule_id)
        dt = pipeline.doc_types.get(rule.document)
        all_approved = all(
            (d.agent_id, dt.id, lang, d.subject) in approved_ids for d in ds for lang in pipeline.s.languages
        ) if dt else False
        order = {"fail": 0, "unknown": 1, "pass": 2, "none": 2}
        worst = min(ds, key=lambda d: order[d.result])
        impl, test = _status(worst, all_approved, m)
        docs = sorted({f"{d.agent_id}: {d.rule_id}" for d in ds})
        rows.append({"rule_id": rule_id, "keywords": keywords, "implementation": impl, "test": test, "agents": len(ds), "evidence": docs,
                     "notes": f"{len(ds)} agent(s). Evidence: approved documents in the bundle; see manifest.json. Generated {now_iso()}."})
    return rows


def fill_export(template: bytes, rows: list[dict[str, Any]], m: dict) -> tuple[bytes, list[str], list[str]]:
    """Writes status and notes into the customer's Compliance Manager export. Returns (xlsx, matched rule ids, unmatched rule ids)."""
    wb = load_workbook(io.BytesIO(template))
    cols = m["columns"]
    matched: list[str] = []
    for ws in wb.worksheets:
        header_row, idx = None, {}
        for r in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 30)):
            names = {str(c.value).strip(): c.column for c in r if c.value is not None}
            if cols["action"] in names:
                header_row, idx = r[0].row, names
                break
        if header_row is None:
            continue
        for r in ws.iter_rows(min_row=header_row + 1):
            name = str(ws.cell(row=r[0].row, column=idx[cols["action"]]).value or "").lower()
            for row in rows:
                if row["rule_id"] in matched:
                    continue
                if any(k in name for k in row["keywords"]):
                    for key, col in (("implementation", "implementation_status"), ("test", "test_status"), ("notes", "notes")):
                        if cols[col] in idx:
                            ws.cell(row=r[0].row, column=idx[cols[col]]).value = row[key]
                    matched.append(row["rule_id"])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue(), matched, [r["rule_id"] for r in rows if r["rule_id"] not in matched]


def standalone_sheet(rows: list[dict[str, Any]], m: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "EU-AI Action"
    ws.append(["Rule", m["columns"]["implementation_status"], m["columns"]["test_status"], m["columns"]["notes"]])
    for r in rows:
        ws.append([r["rule_id"], r["implementation"], r["test"], r["notes"]])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def build_bundle(pipeline, tenant_id: str, cm_export: bytes | None = None) -> bytes:
    m = load_mapping()
    approved = collect_approved(pipeline, tenant_id)
    rows = rule_rows(pipeline, tenant_id, approved, m)
    manifest: dict[str, Any] = {
        "generatedAt": now_iso(), "tenantId": tenant_id, "ruleSetVersion": pipeline.rule_set.version,
        "documents": [], "complianceManager": {"unverifiedColumnMapping": True},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for d in approved:
            files = {}
            for fmt, path in d.file_paths.items():
                name = f"documents/{d.id}.{fmt}"
                z.writestr(name, pipeline.files.get(path))
                files[fmt] = name
            manifest["documents"].append({
                "id": d.id, "agentId": d.agent_id, "type": d.doc_type, "language": d.language, "version": d.version, "subject": d.subject,
                "approvedBy": d.approved_by, "approvedAt": d.approved_at, "counselReviewed": d.counsel_reviewed,
                "ruleId": d.rule_id, "ruleSetVersion": d.rule_set_version, "fileHash": md_hash(pipeline, d), "files": files,
                "fieldProvenance": {k: {"source": v.source, "ref": v.ref} for k, v in d.fields.items() if v.ref},
            })
        if cm_export is not None:
            data, matched, unmatched = fill_export(cm_export, rows, m)
            z.writestr("compliance-manager-update.xlsx", data)
            manifest["complianceManager"].update(matched=matched, unmatched=unmatched)
        else:
            z.writestr("compliance-manager-update.xlsx", standalone_sheet(rows, m))
            manifest["complianceManager"].update(note="No Compliance Manager export supplied: standalone sheet written")
        z.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    return buf.getvalue()
