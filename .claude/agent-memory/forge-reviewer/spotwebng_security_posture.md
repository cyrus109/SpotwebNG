---
name: spotwebng-security-posture
description: SpotwebNG deployment context that makes its auth design dangerous — LAN-exposed, public repo, real Usenet creds, fail-open auth
metadata:
  type: project
---

SpotwebNG (folder `spotwebnext`, port 7979) runs on the home LAN at 192.168.90.2 over plain HTTP, its git repo is PUBLIC (github.com/Mx4flav0r/SpotwebNG), and the live `settings` table holds the user's real Newshosting credentials in plaintext. As of the 2026-07-26 security review the `auth_username`/`auth_password` rows did **not** exist, so `is_authorised()` fail-open meant every page (including `/setup`, which renders the provider password into an `<input value=...>`) was anonymously reachable.

**Why:** the auth design deliberately stays open until an account is created so a fresh install can reach its own setup wizard. That trade-off is only safe on loopback; on a LAN it is a plaintext-credential disclosure.

**How to apply:** when reviewing or extending this app, treat every route as internet-facing-equivalent. Any new feature that reads/writes provider credentials, opens outbound sockets from user input (`/setup/test`), or renders spot-supplied strings (title, description, website) must assume an unauthenticated attacker. Do not accept "it's only on my LAN" as mitigation. Related: [[forge-review-scope-spotwebng]].
