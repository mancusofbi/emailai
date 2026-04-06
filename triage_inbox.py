#!/usr/bin/env python3
"""
Triage Gmail inbox using persona.md as context.
- Important  → stays in inbox
- Maybe      → archived to Triage/Maybe
- Skim       → archived to Triage/Skim
Nothing is ever deleted. Processes all inbox messages via pagination.
"""

from __future__ import annotations
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

PERSONA_FILE = os.path.join(os.path.dirname(__file__), "persona.md")
LABEL_MAYBE = "Triage/Maybe"
LABEL_SKIM = "Triage/Skim"
CLASSIFY_BATCH = 75   # emails per Claude call
META_WORKERS = 20     # parallel metadata fetches
LABEL_WORKERS = 20    # parallel label operations


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def run_gws(*args) -> dict:
    result = subprocess.run(["gws"] + list(args), capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
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
    """Paginate through all inbox messages and return every ID."""
    all_ids = []
    page_token = None
    page = 1
    while True:
        params = {"userId": "me", "maxResults": 500, "labelIds": ["INBOX"]}
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
    """Apply labels to a batch in parallel. Returns (archived, errors)."""
    to_archive = [(c["id"], label_ids[c["category"]])
                  for c in classifications if c.get("category") in ("maybe", "skim")]
    archived = errors = 0
    with ThreadPoolExecutor(max_workers=LABEL_WORKERS) as executor:
        futures = {executor.submit(apply_label_and_archive, mid, lid): mid
                   for mid, lid in to_archive}
        for future in as_completed(futures):
            try:
                future.result()
                archived += 1
            except Exception as e:
                print(f"  Warning: {e}", file=sys.stderr)
                errors += 1
    return archived, errors


# ── Claude classification ─────────────────────────────────────────────────────

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
- maybe: could be relevant, worth a quick look, archive but keep accessible
- skim: almost certainly junk/marketing/noise, archive to skim pile

Rules:
- Anything with [LIST] tag is a newsletter/marketing list — default to skim unless it directly matches a known deep interest
- Transactional emails (order confirmations, bills, appointments, shipping) → important
- Real-person emails → important or maybe
- Use the persona to judge relevance — don't guess generically

Respond with ONLY valid JSON, no explanation, no markdown fences:
[
  {{"id": "MESSAGE_ID", "category": "important|maybe|skim", "reason": "one short phrase"}},
  ...
]"""

    result = subprocess.run(
        ["claude", "-p"],
        input=prompt, capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI error: {result.stderr.strip()}")

    text = result.stdout.strip()
    start, end = text.find("["), text.rfind("]") + 1
    if start == -1 or end == 0:
        raise ValueError(f"No JSON in Claude response:\n{text[:300]}")
    return json.loads(text[start:end])


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("📬 emailai — triaging inbox (all messages)\n")

    if not os.path.exists(PERSONA_FILE):
        print("Error: persona.md not found. Run learn_persona.py first.", file=sys.stderr)
        sys.exit(1)
    with open(PERSONA_FILE) as f:
        persona = f.read()

    print("Step 1: Setting up Gmail labels...")
    label_maybe_id = get_or_create_label(LABEL_MAYBE)
    label_skim_id = get_or_create_label(LABEL_SKIM)
    label_ids = {"maybe": label_maybe_id, "skim": label_skim_id}
    print(f"  Labels ready.\n")

    print("Step 2: Fetching all inbox message IDs...")
    all_ids = fetch_all_inbox_ids()
    total = len(all_ids)
    print(f"  Total inbox messages: {total}\n")

    totals = {"important": 0, "maybe": 0, "skim": 0}
    total_archived = 0
    total_errors = 0
    processed = 0

    print(f"Step 3: Processing in batches of {CLASSIFY_BATCH}...\n")

    # Chunk IDs into batches
    for batch_start in range(0, total, CLASSIFY_BATCH):
        batch_ids = all_ids[batch_start:batch_start + CLASSIFY_BATCH]
        batch_num = batch_start // CLASSIFY_BATCH + 1
        total_batches = (total + CLASSIFY_BATCH - 1) // CLASSIFY_BATCH

        print(f"  Batch {batch_num}/{total_batches} "
              f"(messages {batch_start + 1}–{min(batch_start + CLASSIFY_BATCH, total)})...")

        # Fetch metadata
        emails = fetch_metadata_batch(batch_ids)
        print(f"    Metadata fetched ({len(emails)} messages). Classifying...", flush=True)

        # Classify
        try:
            classifications = classify_batch(emails, persona)
        except Exception as e:
            print(f"    Classification error: {e}. Skipping batch.", file=sys.stderr)
            continue

        # Count
        for c in classifications:
            cat = c.get("category", "skim")
            totals[cat] = totals.get(cat, 0) + 1

        # Archive
        archived, errors = archive_batch(classifications, label_ids)
        total_archived += archived
        total_errors += errors
        processed += len(batch_ids)

        print(f"    Archived {archived} | Running total: "
              f"{totals['important']} important, "
              f"{totals['maybe']} maybe, "
              f"{totals['skim']} skim "
              f"({processed}/{total} processed)\n")

    print("─" * 50)
    print(f"✓ Done. Processed {processed} messages.")
    print(f"  Inbox (important): {totals['important']}")
    print(f"  Triage/Maybe:      {totals['maybe']}")
    print(f"  Triage/Skim:       {totals['skim']}")
    if total_errors:
        print(f"  Errors:            {total_errors}")
    print("\nNothing was deleted.")


if __name__ == "__main__":
    main()
