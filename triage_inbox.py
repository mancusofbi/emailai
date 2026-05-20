#!/usr/bin/env python3
"""
Triage Gmail inbox using persona.md as context.
- Important  → stays in inbox
- Orders     → archived to Triage/Orders (order receipts, shipment notifications)
- Maybe      → archived to Triage/Maybe
- Skim       → archived to Triage/Skim
Nothing is ever deleted. Processes all inbox messages via pagination.
"""

from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

PERSONA_FILE = os.path.join(os.path.dirname(__file__), "persona.md")
RUN_LOG_FILE = os.path.join(os.path.dirname(__file__), "triage_runs.jsonl")
CHECKPOINT_FILE = os.path.join(os.path.dirname(__file__), ".triage_checkpoint")
LABEL_ORDERS = "Triage/Orders"
LABEL_MAYBE = "Triage/Maybe"
LABEL_SKIM = "Triage/Skim"
CLASSIFY_BATCH = 50   # emails per Claude call
META_WORKERS = 20     # parallel metadata fetches
LABEL_WORKERS = 20    # parallel label operations
CLASSIFY_RETRIES = 1  # retries on timeout/error
LABEL_RETRIES = 2     # retries on label application error

# Exit code used when gws auth has expired — callers (launchd, run_triage.sh)
# can treat this as a soft failure rather than spamming tracebacks.
EXIT_AUTH_EXPIRED = 75


class AuthExpiredError(RuntimeError):
    """Raised when gws reports invalid_grant — the token needs re-login."""


def is_auth_error(stderr_text: str) -> bool:
    """True if gws stderr indicates the OAuth token is no longer valid."""
    if not stderr_text:
        return False
    lower = stderr_text.lower()
    return "invalid_grant" in lower or "authentication failed" in lower


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def run_gws(*args) -> dict:
    result = subprocess.run(["gws"] + list(args), capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if is_auth_error(stderr):
            raise AuthExpiredError(stderr.splitlines()[-1] if stderr else "auth expired")
        raise RuntimeError(stderr)
    return json.loads(result.stdout)


def get_or_create_label(name: str) -> str:
    data = run_gws("gmail", "users", "labels", "list",
                   "--params", json.dumps({"userId": "me"}), "--format", "json")
    for label in data.get("labels", []):
        if label["name"] == name:
            return label["id"]
    created = run_gws("gmail", "users", "labels", "create",
                      "--params", json.dumps({"userId": "me"}),
                      "--json", json.dumps({"name": name}),
                      "--format", "json")
    print(f"  Created label: {name}")
    return created["id"]


def fetch_all_inbox_ids() -> list[str]:
    """Paginate through unclassified inbox messages (excludes already-triaged)."""
    all_ids = []
    page_token = None
    page = 1
    while True:
        params = {"userId": "me", "maxResults": 500, "labelIds": ["INBOX"],
                  "q": "-label:Triage/Orders -label:Triage/Maybe -label:Triage/Skim"}
        if page_token:
            params["pageToken"] = page_token
        data = run_gws("gmail", "users", "messages", "list",
                       "--params", json.dumps(params), "--format", "json")
        messages = data.get("messages", [])
        all_ids.extend(m["id"] for m in messages)
        print(f"  Page {page}: {len(all_ids)} IDs fetched...", flush=True)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        page += 1
    return all_ids


def get_message_meta(msg_id: str) -> dict | None:
    try:
        data = run_gws("gmail", "users", "messages", "get",
                       "--params", json.dumps({
                           "userId": "me", "id": msg_id,
                           "format": "metadata",
                           "metadataHeaders": ["From", "Subject", "Date", "List-Unsubscribe"],
                       }), "--format", "json")
        headers = {h["name"]: h["value"]
                   for h in data.get("payload", {}).get("headers", [])}
        snippet = data.get("snippet", "").encode("ascii", "ignore").decode().strip()
        return {
            "id": msg_id,
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "snippet": snippet[:150],
            "is_newsletter": "List-Unsubscribe" in headers,
        }
    except AuthExpiredError:
        raise
    except Exception:
        return None


def fetch_metadata_batch(ids: list[str]) -> list[dict]:
    """Fetch metadata for a list of IDs in parallel."""
    results = []
    with ThreadPoolExecutor(max_workers=META_WORKERS) as executor:
        futures = {executor.submit(get_message_meta, mid): mid for mid in ids}
        for future in as_completed(futures):
            meta = future.result()
            if meta:
                results.append(meta)
    return results


def apply_label_and_archive(msg_id: str, label_id: str):
    run_gws("gmail", "users", "messages", "modify",
            "--params", json.dumps({"userId": "me", "id": msg_id}),
            "--json", json.dumps({"addLabelIds": [label_id], "removeLabelIds": ["INBOX"]}),
            "--format", "json")


def archive_batch(classifications: list[dict], label_ids: dict) -> tuple[int, int]:
    """Apply labels to a batch in parallel with retries. Returns (archived, errors)."""
    to_archive = [(c["id"], label_ids[c["category"]])
                  for c in classifications if c.get("category") in ("orders", "maybe", "skim")]
    archived = errors = 0
    failed = []

    with ThreadPoolExecutor(max_workers=LABEL_WORKERS) as executor:
        futures = {executor.submit(apply_label_and_archive, mid, lid): (mid, lid)
                   for mid, lid in to_archive}
        for future in as_completed(futures):
            mid, lid = futures[future]
            try:
                future.result()
                archived += 1
            except AuthExpiredError:
                raise
            except Exception:
                failed.append((mid, lid))

    # Retry failed label applications
    for mid, lid in failed:
        for attempt in range(LABEL_RETRIES):
            try:
                time.sleep(2 ** attempt)
                apply_label_and_archive(mid, lid)
                archived += 1
                break
            except AuthExpiredError:
                raise
            except Exception as e:
                if attempt == LABEL_RETRIES - 1:
                    print(f"  Warning: label failed after retries for {mid}: {e}", file=sys.stderr)
                    errors += 1

    return archived, errors


# ── Claude classification ─────────────────────────────────────────────────────

def parse_claude_json(text: str) -> list[dict]:
    """Extract the JSON array from a Claude CLI response. Raises ValueError if absent."""
    if not text:
        raise ValueError("empty Claude response")
    start, end = text.find("["), text.rfind("]") + 1
    if start == -1 or end == 0:
        raise ValueError(f"No JSON array in Claude response:\n{text[:300]}")
    return json.loads(text[start:end])


def classify_batch(emails: list[dict], persona: str) -> list[dict]:
    email_list = "\n".join(
        f'ID:{e["id"]} | {"[LIST]" if e["is_newsletter"] else ""} FROM:{e["from"]} | '
        f'SUBJECT:{e["subject"]} | SNIPPET:{e["snippet"]}'
        for e in emails
    )
    prompt = f"""You are triaging a Gmail inbox. Using the persona profile below, classify each email.

## Persona
{persona}

## Emails to classify
{email_list}

## Instructions
Classify each email as exactly one of:
- important: almost certainly matters to this person, should stay in inbox
- orders: order receipts, order confirmations, shipment notifications, delivery updates, tracking emails
- maybe: could be relevant, worth a quick look, archive but keep accessible
- skim: almost certainly junk/marketing/noise, archive to skim pile

Rules:
- Anything with [LIST] tag is a newsletter/marketing list — default to skim unless it directly matches a known deep interest
- Order receipts, purchase confirmations, shipment/tracking/delivery notifications → orders
- Bills and payment due notices: if you are confident it is a real bill from a legitimate sender (e.g. known healthcare provider, utility, bank, insurer — sent from a matching official domain), classify as important. If it looks like a bill but the sender domain seems off, generic, or suspicious, classify as maybe — never skim a potential bill
- Transportation/ride service notifications (passenger on board, pickup confirmed, driver en route, trip status) → important
- Emails from trusted domains always classify as important: coffeymodica.com
- Appointments, real-person emails → important or maybe
- Use the persona to judge relevance — don't guess generically

Respond with ONLY valid JSON, no explanation, no markdown fences:
[
  {{"id": "MESSAGE_ID", "category": "important|orders|maybe|skim", "reason": "one short phrase"}},
  ...
]"""

    last_exc = None
    for attempt in range(CLASSIFY_RETRIES + 1):
        try:
            result = subprocess.run(
                ["claude", "-p"],
                input=prompt, capture_output=True, text=True, timeout=300
            )
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
                raise RuntimeError(f"claude CLI error: {detail}")
            return parse_claude_json(result.stdout.strip())
        except Exception as e:
            last_exc = e
            if attempt < CLASSIFY_RETRIES:
                print(f"    Classify attempt {attempt + 1} failed ({e}), retrying...", flush=True)
                time.sleep(5)
    raise last_exc


# ── Checkpoint helpers ────────────────────────────────────────────────────────

def load_checkpoint() -> set[str]:
    """Return set of message IDs already processed this run."""
    if not os.path.exists(CHECKPOINT_FILE):
        return set()
    try:
        with open(CHECKPOINT_FILE) as f:
            data = json.load(f)
        return set(data.get("processed_ids", []))
    except Exception:
        return set()


def save_checkpoint(processed_ids: set[str]):
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump({"processed_ids": list(processed_ids), "ts": datetime.now(timezone.utc).isoformat()}, f)


def clear_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)


# ── Run log ───────────────────────────────────────────────────────────────────

def append_run_log(totals: dict, processed: int, errors: int, skipped: int):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "processed": processed,
        "errors": errors,
        "skipped_checkpoint": skipped,
        **totals,
    }
    with open(RUN_LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("📬 emailai — triaging inbox (new/unclassified messages)\n")

    if not os.path.exists(PERSONA_FILE):
        print("Error: persona.md not found. Run learn_persona.py first.", file=sys.stderr)
        sys.exit(1)
    with open(PERSONA_FILE) as f:
        persona = f.read()

    print("Step 1: Setting up Gmail labels...")
    label_orders_id = get_or_create_label(LABEL_ORDERS)
    label_maybe_id = get_or_create_label(LABEL_MAYBE)
    label_skim_id = get_or_create_label(LABEL_SKIM)
    label_ids = {"orders": label_orders_id, "maybe": label_maybe_id, "skim": label_skim_id}
    print(f"  Labels ready.\n")

    print("Step 2: Fetching unclassified inbox message IDs...")
    all_ids = fetch_all_inbox_ids()
    total = len(all_ids)
    print(f"  Total inbox messages: {total}\n")

    if total == 0:
        print("Nothing to process.")
        append_run_log({"important": 0, "orders": 0, "maybe": 0, "skim": 0}, 0, 0, 0)
        return

    # Load checkpoint — skip IDs already handled in a prior interrupted run
    checkpoint = load_checkpoint()
    pending_ids = [mid for mid in all_ids if mid not in checkpoint]
    skipped_count = len(all_ids) - len(pending_ids)
    if skipped_count:
        print(f"  Resuming: {skipped_count} already processed (checkpoint), {len(pending_ids)} remaining.\n")

    totals = {"important": 0, "orders": 0, "maybe": 0, "skim": 0}
    total_archived = 0
    total_errors = 0
    processed = 0
    processed_ids = set(checkpoint)

    print(f"Step 3: Processing in batches of {CLASSIFY_BATCH}...\n")

    pending_total = len(pending_ids)
    for batch_start in range(0, pending_total, CLASSIFY_BATCH):
        batch_ids = pending_ids[batch_start:batch_start + CLASSIFY_BATCH]
        batch_num = batch_start // CLASSIFY_BATCH + 1
        total_batches = (pending_total + CLASSIFY_BATCH - 1) // CLASSIFY_BATCH

        print(f"  Batch {batch_num}/{total_batches} "
              f"(messages {batch_start + 1}–{min(batch_start + CLASSIFY_BATCH, pending_total)})...")

        # Fetch metadata
        emails = fetch_metadata_batch(batch_ids)
        print(f"    Metadata fetched ({len(emails)} messages). Classifying...", flush=True)

        # Classify (with retry)
        try:
            classifications = classify_batch(emails, persona)
        except Exception as e:
            print(f"    Classification failed after retries: {e}. Skipping batch.", file=sys.stderr)
            continue

        # Count
        for c in classifications:
            cat = c.get("category", "skim")
            totals[cat] = totals.get(cat, 0) + 1

        # Archive (with retry)
        archived, errors = archive_batch(classifications, label_ids)
        total_archived += archived
        total_errors += errors
        processed += len(batch_ids)

        # Update checkpoint
        processed_ids.update(batch_ids)
        save_checkpoint(processed_ids)

        print(f"    Archived {archived} | Running total: "
              f"{totals['important']} important, "
              f"{totals['orders']} orders, "
              f"{totals['maybe']} maybe, "
              f"{totals['skim']} skim "
              f"({processed}/{pending_total} processed)\n")

    # Clean up checkpoint on successful completion
    clear_checkpoint()

    # Append to run log
    append_run_log(totals, processed, total_errors, skipped_count)

    print("─" * 50)
    print(f"✓ Done. Processed {processed} messages.")
    print(f"  Inbox (important): {totals['important']}")
    print(f"  Triage/Orders:     {totals['orders']}")
    print(f"  Triage/Maybe:      {totals['maybe']}")
    print(f"  Triage/Skim:       {totals['skim']}")
    if total_errors:
        print(f"  Errors:            {total_errors}")
    print("\nNothing was deleted.")


if __name__ == "__main__":
    try:
        main()
    except AuthExpiredError as e:
        print(f"gws auth expired ({e}). Run ./auth.sh to re-authenticate.", file=sys.stderr)
        sys.exit(EXIT_AUTH_EXPIRED)
