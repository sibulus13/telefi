# Security

## Reporting a vulnerability

Please open a private security advisory on GitHub (Security -> Report a vulnerability) rather than
a public issue. Include the version, what you did, and what happened.

## Security model

- **One chat.** The hub serves only `TELEGRAM_CHAT_ID`; updates from any other chat are dropped.
- **Credentials stay in the hub.** The bot token lives only in the hub's environment; producers
  write files and never see it. Error records never include request URLs (the token is in them).
- **Untrusted input is validated at the boundary.** Every envelope, inbound event, config and
  command manifest is checked against its JSON Schema. Ids and namespaces are restricted to
  file-name-safe characters (no path separators or `..`), and links must be `http(s)`.
- **Buttons are bound to stored messages.** A tap is resolved against the envelope TeleFi sent;
  the handler namespace comes from that envelope, never from the button's data.
- **Command sinks run argv, never a shell.** The owner's text reaches a sink only as JSON on stdin.
- **Delivery is at-least-once.** A message is retried with backoff and quarantined after repeated
  failures; a reply being handled when the hub stops may be handled again after a restart.

## Known limitations

- Button data is not cryptographically signed (planned). Anyone who can post in the owner's chat
  can press the owner's buttons.
- The hub trusts the local spool folder: anyone who can write to it can send messages.
