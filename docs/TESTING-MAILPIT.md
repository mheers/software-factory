# Email in a test — Mailpit, and never a stub

Read this before writing a test, a verify script or a golden path that needs a mail to *arrive*.

**Whenever a test, a verify script or a golden path needs a mail to *arrive*, it gets a real mailbox,
and the only supported one is [Mailpit](https://github.com/axllent/mailpit).** Not a hand-written
fake SMTP server in Go, not an in-memory mailer, not a fixture that prints the token to stdout, and
not a test suite that mocks SMTP because "no application logic depends on it". That last sentence is
the one to be suspicious of: it is usually true and it is why the bug is still there.

| What | Value |
|---|---|
| Image | `axllent/mailpit` — **pinned by digest**, like every other image in this workspace |
| SMTP | `1025` — what the sending side is pointed at |
| Web UI **and** REST API | `8025` |
| Interactive API reference | `http://localhost:8025/api/v1/` |

**Why this is a hard rule and not a preference.** A sign-in link is a one-time credential, and
everything that can go wrong with one happens *between* the provider answering `200` and a human
clicking something: the ten-minute lifetime, the sixty-second resend throttle, the browser binding,
the mandatory `origin`. A stub asserts the shape of a string the application was handed, which is
exactly what Casdoor's `{"status","msg"}` stub does, and exactly why the four Casdoor findings had to
be re-verified by hand against the pinned digest.

And the sharper reason, which is the one to remember: **Casdoor answers every refusal with HTTP
`200`.** A throttle, an unknown address, a refused relay and a delivered message are the same status
code. So `200` is not weak evidence that a mail was sent, it is **no evidence at all** — and a check
that stops at the API response is a check that cannot fail. **The mailbox is the only place this
becomes observable at all.**

## The e2e shape

```sh
# Pinned by digest, read from the test topology — never from a copy typed in a script.
docker run -d --name <workspace>-mailpit -p 8025:8025 -p 1025:1025 "axllent/mailpit@sha256:<digest>"

curl -fsS -X DELETE http://127.0.0.1:8025/api/v1/messages     # start from an empty mailbox
# … start the provider pointed at mailpit:1025, then ask for a sign-in link …
curl -fsS 'http://127.0.0.1:8025/api/v1/search?query=to:someone@example.org'
curl -fsS http://127.0.0.1:8025/api/v1/message/latest/raw    # the one-time link is in here
```

- `GET /api/v1/messages` — the mailbox, newest first. `DELETE` on the same path with no body empties
  it.
- `GET /api/v1/message/{ID}/raw` — the full source. **`{ID}` accepts the literal `latest`**, which is
  what turns "read the mail" into a two-line poll instead of a parse-then-retry loop.
- `GET /api/v1/search?query=…` — Mailpit's search syntax; `to:` and `subject:` are the useful ones.
- `GET /api/v1/info` — totals plus `SMTPAccepted` / `SMTPRejected`. This is how a check asserts that
  **the relay was used** rather than that an API answered.
- **Empty the mailbox first, every run.** A long-lived Mailpit accumulates, and "the link from the
  last message" otherwise passes against yesterday's mail — which is the same class of bug as
  asserting against a fixture nobody rebuilt.

## The failure path needs a second mechanism, because `200` cannot express it

Since a delivered message and a refused one are indistinguishable from the API, "the mail relay is
down" cannot be tested by asking the API anything. It is tested with Mailpit's **Chaos** feature, which
returns a chosen SMTP error code at a chosen probability — and the `Authentication` trigger is
**ignored unless authentication is configured**, which Mailpit has none of by default.

**Chaos is disabled at startup and cannot be switched on while running.** Start Mailpit with
`--enable-chaos` (env `MP_ENABLE_CHAOS=true`) to allow configuration later, or with
`--chaos-triggers Sender:451:100` (env `MP_CHAOS_TRIGGERS`) to enable and configure in one step. A
`PUT /api/v1/chaos` against a Mailpit that was not started that way returns an error rather than
arming anything, so a test that "sets chaos" against a default container asserts nothing at all.

## Mailpit is a test fixture. It is never a relay.

- **It belongs to the test topology only.** A deployment's `docker-compose.yml` must not carry it and
  no real provider may be configured to deliver into it: it accepts everything and keeps only the most
  recent 500 messages by default, so it is a mailbox that quietly forgets.
- **It needs no credential, so a configuration that has one is wrong.** Mailpit's SMTP server uses
  **no encryption and no authentication by default** — there is nothing to authenticate against, and
  an SMTP password sitting in a compose file, a fixture or a committed provisioning file is a
  committed secret for no benefit.
- **It is a container image like any other, so it is pinned by digest.** `axllent/mailpit:latest` is
  the same kind of moving pointer as `casbin/casdoor:latest`, and that one shipped a build with a
  broken `/login/oauth/authorize`. The digest belongs in the test topology file and the check reads it
  from there — a script carrying its own copy of a digest can pass while the deployment runs a
  different build.

## When the identity provider is Casdoor — where its SMTP lives, and it is **not** in `conf/app.conf`

Written down because finding it cost real time and it is not where anyone looks first. Casdoor has
**no `smtpHost` key in `conf/app.conf`** — its shipped file has no SMTP entry at all. Mail is sent by
`object.SendEmail` through an `email` provider selected by `provider.Type`, and the settings are
**fields on a `Provider` row in Casdoor's own database**, not environment variables:

| Field | Meaning |
|---|---|
| `category` | `"Email"` — this is what makes it a mail provider at all |
| `host` / `port` | the relay: `mailpit`, `1025` |
| `clientId` / `clientSecret` | the SMTP user and password; **leave empty for Mailpit** |
| `clientId2` | the from-address; falls back to `clientId` |
| `sslMode` | `"Auto"` (default, deferred to gomail's port heuristic), `"Enable"`, `"Disable"` |

- **Set `sslMode: "Disable"` rather than `Auto` for a plain relay.** `disableSsl` is deprecated in
  favour of `sslMode`, and `Auto` means "let the library decide from the port number" — correct today
  and a silent behaviour change the day it is not.
- **`init_data.json` *can* carry it, under a `"providers"` key — and that is the trap.** A committed
  Mailpit provider points a real deployment at a mailbox that does not exist on that host, and the
  symptom is a sign-in link that vanishes with nothing logged. Declare it in the e2e script or a
  compose override, for exactly the reason the committed file declares no client secret.
- **`initDataNewOnly: "false"` makes `initDefinedProvider` delete and recreate the provider on every
  start**, regenerating the SMTP credential each time — the same trap as the applications, which is
  why this workspace sets it to `true`.

This rule applies to every repository that sends or receives a mail in a test. A repository that has
no mail path today still follows the rule the day a ticket gives it one; what it must not do is grow
a second mail abstraction to avoid it.
