"""Operator-only migration of an exact historical SendAsPeerInvalid RPC receipt.

This reads a trusted private execution receipt, never Telegram or credentials.
It cannot release timeouts, arbitrary errors, sent messages or changed payloads.
"""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


def reconcile(ledger, receipt, operation_id):
    raw = Path(receipt).read_bytes()
    value = json.loads(raw)
    for _ in range(8):
        if value.get("error_type"):
            break
        if isinstance(value.get("result"), dict):
            value = value["result"]
        elif isinstance(value.get("structuredContent"), dict):
            value = value["structuredContent"]
        else:
            texts = [c["text"] for c in value.get("content", []) if c.get("type") == "text"]
            if len(texts) != 1:
                raise ValueError("one complete native receipt required")
            value = json.loads(texts[0])
    if (value.get("error_type") != "SendAsPeerInvalidError"
            or value.get("submitted") is not True
            or value.get("state") not in ("unknown", "rejected")):
        raise ValueError("receipt is not an exact proven sender rejection")
    evidence = hashlib.sha256(raw).hexdigest()
    with sqlite3.connect(ledger) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if (not row or row["hash"] != value.get("action_hash") or row["message"] is not None
                or row["state"] not in ("reserved", "rejected")):
            raise ValueError("operation identity or effect state mismatch")
        db.execute("CREATE TABLE IF NOT EXISTS rejection_receipts "
                   "(operation_id TEXT, action_hash TEXT, error_type TEXT, evidence TEXT)")
        if row["state"] == "reserved":
            db.execute("INSERT INTO rejection_receipts VALUES (?,?,?,?)",
                       (operation_id, row["hash"], value["error_type"], evidence))
            db.execute("UPDATE operations SET state='rejected' WHERE id=?", (operation_id,))
    return {"operation_id": operation_id, "state": "rejected", "evidence_sha256": evidence,
            "fresh_preview_required": True, "telegram_invoked": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args()
    print(json.dumps(reconcile(args.ledger, args.receipt, args.operation_id)))
