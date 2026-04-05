#!/usr/bin/env python3
"""
Analyze Gmail to learn what the user actually cares about and build persona.md.
Most inbox messages are junk — Claude finds the signal.
"""

from __future__ import annotations
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

PERSONA_FILE = os.path.join(os.path.dirname(__file__), "persona.md")


def run_gws(*args) -> dict:
    result = subprocess.run(["gws"] + list(args), capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def fetch_message_ids(label: str, max_results: int) -> list[str]:
    data = run_gws(
        "gmail", "users", "messages", "list",
        "--params", json.dumps({"userId": "me", "maxResults": max_results, "labelIds": [label]}),
        "--format", "json",
    )
    return [m["id"] for m in data.get("messages", [])]


def get_message_meta(msg_id: str) -> dict | None:
    try:
        data = run_gws(
            "gmail", "users", "messages", "get",
            "--params", json.dumps({
                "userId": "me",
                "id": msg_id,
                "format": "metadata",
                "metadataHeaders": ["From", "To", "Subject", "Date", "List-Unsubscribe"],
            }),
            "--format", "json",
        )
        headers = {h["name"]: h["value"] for h in data.get("payload", {}).get("headers", [])}
        snippet = data.get("snippet", "").encode("ascii", "ignore").decode().strip()
        return {
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "subject": headers.get("Subject", ""),
            "snippet": snippet[:200],
            "is_newsletter": "List-Unsubscribe" in headers,
        }
    except Exception:
        return None


def fetch_emails(label: str, count: int) -> list[dict]:
    print(f"  Fetching {count} {label} message IDs...", flush=True)
    ids = fetch_message_ids(label, count)
    print(f"  Fetching metadata for {len(ids)} messages (parallel)...", flush=True)

    results = []
    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(get_message_meta, mid): mid for mid in ids}
        for i, future in enumerate(as_completed(futures), 1):
            meta = future.result()
            if meta:
                meta["label"] = label
                results.append(meta)
            if i % 20 == 0:
                print(f"    {i}/{len(ids)}...", flush=True)

    return results


def build_email_summary(inbox: list[dict], sent: list[dict]) -> str:
    lines = []

    lines.append(f"=== INBOX ({len(inbox)} messages) ===")
    for e in inbox:
        flag = "[NEWSLETTER]" if e["is_newsletter"] else ""
        lines.append(f"{flag} FROM: {e['from']}")
        lines.append(f"  SUBJECT: {e['subject']}")
        if e["snippet"]:
            lines.append(f"  SNIPPET: {e['snippet']}")

    lines.append(f"\n=== SENT ({len(sent)} messages) ===")
    for e in sent:
        lines.append(f"TO: {e['to']}")
        lines.append(f"  SUBJECT: {e['subject']}")
        if e["snippet"]:
            lines.append(f"  SNIPPET: {e['snippet']}")

    return "\n".join(lines)


def analyze_with_claude(email_summary: str) -> str:
    print("  Sending to Claude CLI for analysis...", flush=True)

    prompt = f"""You are helping build a persona profile of a person based on their email patterns.
Your goal is to identify what they ACTUALLY care about — not the junk.

Most inbox emails are unsolicited newsletters, marketing, and spam. Ignore those.
Focus on:
- Emails that suggest real interests, hobbies, or passions
- Services or subscriptions they chose and use (not just signed up for)
- Domains the person operates in (professional, health, finance, etc.)
- People or organizations they correspond with
- What their sent emails reveal about their priorities and relationships
- Recurring themes that suggest goals or projects

Based on the following email data, build a detailed persona profile.

{email_summary}

Write a persona.md file with these sections:
1. **Interests & Topics** — what this person genuinely cares about
2. **Active Services & Subscriptions** — things they chose (not spam)
3. **Professional / Domain Context** — what world they operate in
4. **Regular Correspondents** — people/orgs they actually interact with
5. **Inferred Goals or Projects** — based on patterns
6. **Email Hygiene Notes** — observations about inbox noise and what to filter

Be specific and concrete. Avoid vague platitudes. Be honest if evidence is thin.
Note: almost all inbox email is unsolicited. Format as clean markdown."""

    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True, text=True, timeout=180
    )

    if result.returncode != 0:
        raise RuntimeError(f"claude CLI error: {result.stderr.strip()}")

    return result.stdout.strip()


def write_persona(content: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = f"<!-- Generated by learn_persona.py on {now} -->\n\n"
    with open(PERSONA_FILE, "w") as f:
        f.write(header + content)
    print(f"  Written to {PERSONA_FILE}")


def main():
    print("📬 emailai — learning persona from Gmail\n")

    print("Step 1: Fetching inbox emails...")
    inbox = fetch_emails("INBOX", 100)
    print(f"  Got {len(inbox)} inbox emails\n")

    print("Step 2: Fetching sent emails...")
    sent = fetch_emails("SENT", 50)
    print(f"  Got {len(sent)} sent emails\n")

    print("Step 3: Analyzing with Claude...")
    summary = build_email_summary(inbox, sent)
    persona_content = analyze_with_claude(summary)

    if not persona_content:
        print("Error: no content returned from Claude", file=sys.stderr)
        sys.exit(1)

    print("\nStep 4: Writing persona.md...")
    write_persona(persona_content)

    print("\n✓ Done. Review persona.md to see what Claude learned.")


if __name__ == "__main__":
    main()
