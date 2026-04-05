# emailai

AI-powered email assistant using the Google Workspace CLI (`gws`).

## Gmail access

Authenticated as `mancusofbi@gmail.com` via `gws auth login`.
gcloud config: `emailai`

## Key gws commands

```bash
gws gmail +triage              # inbox summary (sender, subject, date)
gws gmail +send                # send an email
gws gmail +reply               # reply to a message (threaded)
gws gmail +reply-all           # reply all
gws gmail +forward             # forward a message
gws gmail +watch               # stream new emails as NDJSON
```

Full Gmail API also available:
```bash
gws gmail users messages list --params '{"userId": "me"}'
gws gmail users messages get --params '{"userId": "me", "id": "<id>"}'
```

Output is JSON by default; use `--format table` for readable output.
