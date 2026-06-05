#!/usr/bin/env python3
"""
📦 Export Telegram analytics into a project workspace (raw/)

Goal:
- export participants + messages for multiple groups
- save into a chosen workspace folder (default: vahue/satia/raw)

Usage:
  PYTHONPATH=. python3 examples/export_project_analytics.py \
    --workspace vahue/satia \
    --group -1001234567890 --group "@somegroup"

  PYTHONPATH=. python3 examples/export_project_analytics.py \
    --workspace vahue/satia \
    --groups-file vahue/satia/notes/groups.txt
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# add packages/ and packages/tg_core to import path (same pattern as other examples)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = PROJECT_ROOT / "packages"
TG_CORE_DIR = PKG_DIR / "tg_core"
for p in (str(PKG_DIR), str(TG_CORE_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from tganalytics.infra.tele_client import get_client, get_client_for_session
from tganalytics.infra.limiter import get_rate_limiter, safe_call
from tganalytics.domain.groups import GroupManager


@dataclass(frozen=True)
class ResolvedGroup:
    identifier: str  # original input
    group_id: int
    title: str
    username: Optional[str]


def _now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _slugify(s: str) -> str:
    return (
        s.strip()
        .replace(" ", "_")
        .replace("/", "_")
        .replace("\\", "_")
        .replace(":", "_")
    )


def _raw_dir(workspace: str) -> Path:
    return (PROJECT_ROOT / workspace / "raw").resolve()


def _load_groups_from_file(path: str) -> List[str]:
    p = (PROJECT_ROOT / path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"groups file not found: {path}")
    items: List[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        items.append(line)
    return items


async def find_group_by_title(client, title: str) -> Optional[Any]:
    async def search_dialogs():
        dialogs = []
        async for dialog in client.iter_dialogs():
            if dialog.title and title.lower() in dialog.title.lower():
                dialogs.append(dialog)
        return dialogs

    dialogs = await safe_call(search_dialogs, operation_type="api")
    if not dialogs:
        return None

    for dialog in dialogs:
        if dialog.title.lower() == title.lower():
            return dialog
    return dialogs[0]


async def resolve_group(client, manager: GroupManager, group_identifier: str) -> ResolvedGroup:
    group_info = await manager.get_group_info(group_identifier)
    if group_info:
        return ResolvedGroup(
            identifier=group_identifier,
            group_id=int(group_info["id"]),
            title=group_info.get("title") or "Unknown",
            username=group_info.get("username"),
        )

    entity = await find_group_by_title(client, group_identifier)
    if not entity:
        raise ValueError(f"group not found: {group_identifier}")

    return ResolvedGroup(
        identifier=group_identifier,
        group_id=int(entity.id),
        title=getattr(entity, "title", "Unknown"),
        username=getattr(entity, "username", None),
    )


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def export_one_group(
    client,
    manager: GroupManager,
    raw_out: Path,
    group_identifier: str,
    participants_limit: int,
    messages_limit: Optional[int],
    min_id: int,
    export_participants_csv: bool,
) -> Dict[str, Any]:
    resolved = await resolve_group(client, manager, group_identifier)
    stamp = _now_stamp()
    group_slug = _slugify(resolved.title) or str(resolved.group_id)

    # participants
    participants = await manager.get_participants(resolved.group_id, limit=participants_limit)
    participants_path = raw_out / f"{stamp}__tg__participants__{resolved.group_id}__{group_slug}.json"
    _write_json(
        participants_path,
        {
            "group": {
                "id": resolved.group_id,
                "title": resolved.title,
                "username": resolved.username,
                "export_date": datetime.now().isoformat(),
                "limit": participants_limit,
                "total_participants_exported": len(participants),
            },
            "participants": participants,
        },
    )

    participants_csv_path: Optional[Path] = None
    if export_participants_csv:
        participants_csv_path = raw_out / f"{stamp}__tg__participants__{resolved.group_id}__{group_slug}.csv"
        ok = await manager.export_participants_to_csv(
            str(resolved.group_id),
            str(participants_csv_path),
            limit=participants_limit,
        )
        if not ok:
            participants_csv_path = None

    # messages
    messages = await manager.get_messages(resolved.group_id, limit=messages_limit, min_id=min_id)
    last_message_id = messages[-1]["id"] if messages else None
    messages_path = raw_out / f"{stamp}__tg__messages__{resolved.group_id}__{group_slug}.json"
    _write_json(
        messages_path,
        {
            "group": {
                "id": resolved.group_id,
                "title": resolved.title,
                "username": resolved.username,
                "export_date": datetime.now().isoformat(),
                "min_id": min_id,
                "last_message_id": last_message_id,
                "total_messages_exported": len(messages),
                "limit": messages_limit,
            },
            "messages": messages,
        },
    )

    return {
        "group_id": resolved.group_id,
        "title": resolved.title,
        "participants_exported": len(participants),
        "participants_json": str(participants_path),
        "participants_csv": str(participants_csv_path) if participants_csv_path else None,
        "messages_exported": len(messages),
        "messages_json": str(messages_path),
        "last_message_id": last_message_id,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Export TG analytics into a workspace/raw folder")
    parser.add_argument("--workspace", default="vahue/satia", help="Workspace folder (e.g. vahue/satia)")
    parser.add_argument(
        "--session-name",
        default=None,
        help="Optional session override (e.g. example_account). If omitted, uses default session from .env (SESSION_NAME).",
    )
    parser.add_argument("--group", action="append", default=[], help="Group id / @username / title (repeatable)")
    parser.add_argument("--groups-file", default=None, help="Path to a text file with group identifiers (one per line)")
    parser.add_argument("--participants-limit", type=int, default=2000, help="Max participants per group")
    parser.add_argument("--messages-limit", type=int, default=5000, help="Max messages per group (default 5000)")
    parser.add_argument("--min-id", type=int, default=0, help="Min message id (for incremental export)")
    parser.add_argument("--no-participants-csv", action="store_true", help="Do not export participants CSV")
    args = parser.parse_args()

    groups: List[str] = []
    if args.groups_file:
        groups.extend(_load_groups_from_file(args.groups_file))
    groups.extend(args.group or [])
    groups = [g.strip() for g in groups if g.strip()]

    if not groups:
        raise SystemExit("no groups provided: use --group ... or --groups-file ...")

    raw_out = _raw_dir(args.workspace)
    raw_out.mkdir(parents=True, exist_ok=True)

    print("🚀 export project analytics")
    print(f"   workspace: {args.workspace}")
    print(f"   raw out:   {raw_out}")
    print(f"   groups:    {len(groups)}")
    if args.session_name:
        print(f"   session:   override -> {args.session_name}")
    print()

    if args.session_name:
        session_path = (PROJECT_ROOT / "data" / "sessions" / args.session_name).resolve()
        client = get_client_for_session(str(session_path))
    else:
        client = get_client()
    try:
        await client.start()
        manager = GroupManager(client)

        results: List[Dict[str, Any]] = []
        for g in groups:
            print(f"📌 group: {g}")
            res = await export_one_group(
                client=client,
                manager=manager,
                raw_out=raw_out,
                group_identifier=g,
                participants_limit=args.participants_limit,
                messages_limit=args.messages_limit,
                min_id=args.min_id,
                export_participants_csv=(not args.no_participants_csv),
            )
            results.append(res)
            print(f"   participants: {res['participants_exported']}")
            print(f"   messages:      {res['messages_exported']}")
            print()

        limiter = get_rate_limiter()
        stats = limiter.get_stats()
        report_path = raw_out / f"{_now_stamp()}__tg__export_report.json"
        _write_json(
            report_path,
            {
                "export_date": datetime.now().isoformat(),
                "workspace": args.workspace,
                "groups": results,
                "anti_spam_stats": stats,
            },
        )

        print("✅ done")
        print(f"   report: {report_path}")
        print(f"   api calls: {stats.get('api_calls')}, flood waits: {stats.get('flood_waits')}")

    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())


