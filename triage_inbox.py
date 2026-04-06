#!/usr/bin/env python3
"""
Triage Gmail inbox using persona.md as context.
- Important  → stays in inbox
- Maybe      → archived to Triage/Maybe
- Skim       → archived to Triage/Skim
Nothing is ever deleted.
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


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def run_gws(*args) -> dict:
    result = subprocess.run(["gws"] + list(args), capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def get_or_create_label(name: str) -> str:
    """Return label ID, creating the label if it doesn't exist."""
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


def fetch_inbox_ids(max_results: int = 100) -> list[str]:
    data = run_gws("gmail", "users", "messages", "list",
                   "--params", json.dumps({"userId": "me", "maxResults": max_results,
                                           "labelIds": ["INBOX"]}),
                   "--format", "json")
    return [m["id"] for m in data.get("messages", [])]


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


def apply_label_and_archive(msg_id: str, label_id: str):
    """Add a label and remove from inbox (archive)."""
    run_gws("gmail", "users", "messages", "modify",
            "--params", json.dumps({"userId": "me", "id": msg_id}),
            "--json", json.dumps({"addLabelIds": [label_id], "removeLabelIds": ["INBOX"]}),
            "--format", "json")


# ── Claude classification ─────────────────────────────────────────────────────

def classify_emails(emails: list[dict], persona: str) -> list[dict]:
    """Ask Claude to classify each email as important/maybe/skim."""
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
- Sent-from-real-person emails lean important/maybe
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

    # Extract JSON from response (claude may add surrounding text)
    text = result.stdout.strip()
    start = text.find("[")
    end = text.rfind("]") + 1
    if start == -1 or end == 0:
        raise ValueError(f"No JSON array found in Claude response:\n{text[:500]}")

    return json.loads(text[start:end])


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("📬 emailai — triaging inbox\n")

    # Load persona
    if not os.path.exists(PERSONA_FILE):
        print("Error: persona.md not found. Run learn_persona.py first.", file=sys.stderr)
        sys.exit(1)
    with open(PERSONA_FILE) as f:
        persona = f.read()

    # Ensure labels exist
    print("Step 1: Setting up Gmail labels...")
    label_maybe_id = get_or_create_label(LABEL_MAYBE)
    label_skim_id = get_or_create_label(LABEL_SKIM)
    label_ids = {"maybe": label_maybe_id, "skim": label_skim_id}
    print(f"  Labels ready: {LABEL_MAYBE}, {LABEL_SKIM}\n")

    # Fetch inbox
    print("Step 2: Fetching inbox...")
    ids = fetch_inbox_ids(max_results=100)
    print(f"  Found {len(ids)} messages, fetching metadata (parallel)...")
    emails = []
    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(get_message_meta, mid): mid for mid in ids}
        for future in as_completed(futures):
            meta = future.result()
            if meta:
                emails.append(meta)
    print(f"  Got metadata for {len(emails)} messages\n")

    # Classify
    print("Step 3: Classifying with Claude...")
    classifications = classify_emails(emails, persona)

    counts = {"important": 0, "maybe": 0, "skim": 0, "unknown": 0}
    for c in classifications:
        cat = c.get("category", "unknown")
        counts[cat] = counts.get(cat, 0) + 1

    print(f"  Results: {counts['important']} important, "
          f"{counts['maybe']} maybe, {counts['skim']} skim\n")

    # Apply labels
    print("Step 4: Applying labels and archiving...")
    archived = 0
    errors = 0
    for c in classifications:
        cat = c.get("category")
        if cat in ("maybe", "skim"):
            try:
                apply_label_and_archive(c["id"], label_ids[cat])
                archived += 1
            except Exception as e:
                print(f"  Warning: could not process {c['id']}: {e}", file=sys.stderr)
                errors += 1

    print(f"  Archived {archived} messages ({errors} errors)")
    print(f"  {counts['important']} messages remain in inbox\n")

    # Summary
    print("─" * 50)
    print(f"✓ Inbox: {counts['important']} important messages")
    print(f"  Gmail › {LABEL_MAYBE}: {counts['maybe']} messages")
    print(f"  Gmail › {LABEL_SKIM}: {counts['skim']} messages")
    print("\nNothing was deleted. All messages are still accessible by label.")


if __name__ == "__main__":
    main()
