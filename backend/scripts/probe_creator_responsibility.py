"""Explicit, bounded, suggestion-only production diagnostic; no credential copying."""

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    from app.config import get_settings
    from app.creator_responsibility_matching import FEATURE, probe_sample

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor-id", type=UUID, required=True, help="Current active system Owner authorization ID")
    parser.add_argument(
        "--contact-id", type=UUID, action="append", required=True, help="At most five confirmed creators"
    )
    parser.add_argument(
        "--use-handle-key", action="store_true", help="Explicitly authorized temporary credential use only"
    )
    args = parser.parse_args()
    if not 1 <= len(set(args.contact_id)) <= 5:
        parser.error("Supply 1–5 distinct creators; this is not a full-library queue")
    outcomes = probe_sample(
        get_settings(),
        args.actor_id,
        args.contact_id,
        key_feature="creator_handle_matching" if args.use_handle_key else FEATURE,
    )
    print(json.dumps(outcomes, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
