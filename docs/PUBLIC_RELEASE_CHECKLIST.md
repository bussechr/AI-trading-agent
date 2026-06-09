# Public Release Checklist

Use this checklist before making the repository public.

## Repository Contents

- [ ] No `.env`, `.secrets/`, keys, tokens, passwords, private keys, account identifiers, broker-specific account details, live order tickets, or account statements are tracked.
- [ ] No generated runtime artifacts, model bundles, local databases, logs, compiled files, or build outputs are tracked.
- [ ] README links to `SECURITY.md`, `SAFETY.md`, and `DISCLAIMER.md`.
- [ ] All example settings use placeholders or local-only defaults.
- [ ] Issue templates warn users not to post secrets or account details.

## Git History

- [ ] Full Git history has been scanned for secrets and personal data.
- [ ] If sensitive data ever appeared in history, rewrite history or create a fresh public repository.
- [ ] Any credential that ever appeared in Git, logs, screenshots, chats, or issues has been rotated.

## GitHub Settings

- [ ] GitHub Actions workflows are absent or disabled if CI is not intended.
- [ ] Vulnerability alerts are enabled.
- [ ] Secret scanning is enabled where available.
- [ ] Private vulnerability reporting is enabled where available.
- [ ] Wiki and Projects are disabled unless actively maintained.
- [ ] Branch protection is configured only if it matches the intended workflow.

## Trading Safety

- [ ] Default documentation tells users to run demo first.
- [ ] Documentation warns that this is not financial advice.
- [ ] Lot-size, max-position, bridge-auth, and risk-limit settings are documented.
- [ ] No live account screenshots or performance claims are included.
