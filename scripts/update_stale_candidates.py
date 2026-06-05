import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional, Set, Tuple


@dataclass
class Config:
    members_path: Path
    space_path: Path
    stale_usernames: List[str]
    stale_days: int = 180
    write_inplace: bool = False
    mark_next_top10: bool = False


def load_members_structure(path: Path) -> Tuple[Any, List[Dict[str, Any]], bool]:
    """
    Returns (root, members_list, root_is_array)
    - If root is an object with key "members" -> returns that list and root_is_array=False
    - If root is a list -> returns it and root_is_array=True
    """
    with path.open("r", encoding="utf-8") as f:
        root = json.load(f)
    if isinstance(root, list):
        return root, root, True
    if isinstance(root, dict):
        members = root.get("members")
        if isinstance(members, list):
            return root, members, False
    raise ValueError(f"Expected a JSON array or an object with 'members' array in {path}")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def iso_in_days(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


def normalize_username(username: Optional[str]) -> Optional[str]:
    if username is None:
        return None
    return username.strip().lstrip("@").lower()


def build_space_index(space_members: List[Dict[str, Any]]) -> Tuple[Set[int], Set[str]]:
    ids: Set[int] = set()
    usernames: Set[str] = set()
    for m in space_members:
        user_id = m.get("id")
        if isinstance(user_id, int):
            ids.add(user_id)
        uname = normalize_username(m.get("username"))
        if uname:
            usernames.add(uname)
    return ids, usernames


def update_in_space_flags(members: List[Dict[str, Any]], space_ids: Set[int], space_usernames: Set[str]) -> int:
    updated = 0
    for m in members:
        user_id = m.get("user_id") or m.get("id")
        username = normalize_username(m.get("username"))
        in_space = False
        if isinstance(user_id, int) and user_id in space_ids:
            in_space = True
        elif username and username in space_usernames:
            in_space = True
        prev = m.get("in_example_group")
        if prev is not None and bool(prev) == in_space:
            continue
        m["in_example_group"] = in_space
        updated += 1
    return updated


def is_stale_active(member: Dict[str, Any]) -> bool:
    stale_until = member.get("stale_until")
    if not stale_until:
        return False
    try:
        until = datetime.fromisoformat(stale_until)
    except Exception:
        return False
    return until >= datetime.now(timezone.utc)


def mark_stale(member: Dict[str, Any], days: int, reason: str) -> None:
    member["stale"] = True
    member["stale_set_at"] = iso_now()
    member["stale_until"] = iso_in_days(days)
    member["stale_reason"] = reason


def find_members_by_usernames(members: List[Dict[str, Any]], usernames: List[str]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    wanted = {normalize_username(u) for u in usernames}
    for m in members:
        uname = normalize_username(m.get("username"))
        if uname and uname in wanted:
            index[uname] = m
    return index


def pick_next_top10(members: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for m in members:
        if m.get("in_example_group"):
            continue
        if is_stale_active(m):
            continue
        offline_cnt = m.get("offline_cnt") or 0
        # Some items may lack dates; use epoch fallback
        last_offline_at_str = m.get("last_offline_at")
        try:
            last_dt = datetime.fromisoformat(last_offline_at_str) if last_offline_at_str else datetime(1970, 1, 1, tzinfo=timezone.utc)
        except Exception:
            last_dt = datetime(1970, 1, 1, tzinfo=timezone.utc)
        candidates.append((offline_cnt, last_dt, m))

    # Sort by offline_cnt desc, then last_offline_at desc
    candidates.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [m for _, __, m in candidates[:10]]


def save_json_array(path: Path, data: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def backup_file(path: Path) -> Path:
    backup = path.with_suffix(path.suffix + ".bak")
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return backup


def parse_args() -> Config:
    parser = argparse.ArgumentParser(description="Update members.json: cross-check Space, mark Stale for provided and next top-10.")
    parser.add_argument("--members", required=True, type=Path, help="Path to aggregated offline events members.json")
    parser.add_argument("--space", required=True, type=Path, help="Path to example_group_all_participants.json")
    parser.add_argument("--stale-usernames", default="", help="Comma-separated usernames to mark stale now")
    parser.add_argument("--stale-days", type=int, default=180, help="Days to keep stale active (default 180)")
    parser.add_argument("--write-inplace", action="store_true", help="Write changes in place (creates .bak)")
    parser.add_argument("--mark-next", action="store_true", help="Also mark next computed top-10 as stale")
    args = parser.parse_args()

    stale_usernames = [u.strip() for u in args.stale_usernames.split(",") if u.strip()]
    return Config(
        members_path=args.members,
        space_path=args.space,
        stale_usernames=stale_usernames,
        stale_days=args.stale_days,
        write_inplace=bool(args.write_inplace),
        mark_next_top10=bool(args.mark_next),
    )


def main() -> None:
    cfg = parse_args()
    root, members, root_is_array = load_members_structure(cfg.members_path)
    space_root, space_members, _ = load_members_structure(cfg.space_path)

    space_ids, space_usernames = build_space_index(space_members)
    changed_in_space = update_in_space_flags(members, space_ids, space_usernames)

    # Mark provided usernames stale
    provided_index = find_members_by_usernames(members, cfg.stale_usernames)
    provided_found = list(provided_index.keys())
    provided_missing = [normalize_username(u) for u in cfg.stale_usernames if normalize_username(u) not in provided_index]
    for uname, m in provided_index.items():
        mark_stale(m, cfg.stale_days, reason=f"manual-stale-set {iso_now()}")

    # Compute and optionally mark next top-10
    next_top10 = pick_next_top10(members)
    if cfg.mark_next_top10:
        for m in next_top10:
            mark_stale(m, cfg.stale_days, reason=f"auto-next-top10 {iso_now()}")

    # Save
    if cfg.write_inplace:
        backup = backup_file(cfg.members_path)
        # Preserve original root structure
        if root_is_array:
            save_json_array(cfg.members_path, members)
        else:
            # root is a dict with 'members'
            assert isinstance(root, dict)
            root["members"] = members
            with cfg.members_path.open("w", encoding="utf-8") as f:
                json.dump(root, f, ensure_ascii=False, indent=2)
        print(f"Updated members written to: {cfg.members_path}")
        print(f"Backup created at: {backup}")
    else:
        out_path = cfg.members_path.with_suffix(".updated.json")
        if root_is_array:
            save_json_array(out_path, members)
        else:
            root_out = dict(root)
            root_out["members"] = members
            with out_path.open("w", encoding="utf-8") as f:
                json.dump(root_out, f, ensure_ascii=False, indent=2)
        print(f"Dry-run output written to: {out_path}")

    # Summary
    print("\nSummary:")
    print(f"in_example_group updated: {changed_in_space}")
    print(f"Provided usernames requested: {len(cfg.stale_usernames)}")
    print(f"Provided usernames found: {len(provided_found)}")
    if provided_missing:
        print("Provided usernames missing:", ", ".join(sorted(filter(None, provided_missing))))
    if provided_found:
        print("Marked stale (provided):", ", ".join(sorted(provided_found)))
    # Show next top-10
    def fmt(m: Dict[str, Any]) -> str:
        uname = m.get("username") or "<no_username>"
        return f"{uname} (offline_cnt={m.get('offline_cnt', 0)})"
    print("Next top-10 candidates:")
    for m in next_top10:
        print(" - ", fmt(m))


if __name__ == "__main__":
    main()


