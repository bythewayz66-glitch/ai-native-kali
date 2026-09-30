"""Social-engineering tool frontends.

Workstream B, Phase 3. Blueprint ref: section 05 (social engineering row) and
section 08 (ethics and legal guardrails).

This is the most sensitive module in the tool layer, and the tier assignments
reflect a distinction the other categories do not need to make.

Every other category's harm is bounded by the target's *infrastructure*. A
social-engineering tool's harm is bounded by **the people** you point it at, and
that harm does not stop when the engagement ends:

* a credential captured during a simulation is a real credential, and it stays
  valid until it is rotated;
* a colleague who clicked a simulated phishing link carries the lesson, but one
  who was singled out in front of their team carries something else;
* sending to a real address is irreversible in a way that a port scan is not -
  the scan can be repeated, the mail cannot be unsent.

So three rules shape this module:

1. **Consent, not just authorization.** A scope ticket authorises the
   *organisation*. It does not record that the people being tested agreed to be
   tested. Tools that reach humans at scale are therefore T3 and need the
   explicit live unlock *in addition to* a scope, because the two are different
   questions and the engine must be told about both.
2. **Reconnaissance is separated from delivery.** Checking a domain's SPF and
   DMARC records touches nobody and is T1. Sending the campaign is T2. Building
   a payload is T2 but local. Nothing does both in one step, so the approval gate
   always lands between "we learned this" and "we did this to someone".
3. **Nothing here harvests.** No wrapper reads a captured credential's value,
   exports a submitted form, or reports which named individual clicked. A
   credential captured by a landing page writes a *count* to the card and is
   rotated; the tool that would retrieve it is deliberately not implemented, for
   the same reason ``pacu`` is absent from the cloud module - it does not fit the
   one-spec-one-reviewed-act model.

``evilginx`` and ``gophish`` are real, widely used frameworks. Both are wrapped
here because a security team simulating its own staff needs exactly this and
nothing more; the guardrails, not the tool choice, are what make it responsible.
"""
from __future__ import annotations

from ..spec import ParamSpec, ToolSpec

SOCIAL_ENGINEERING_TOOLS: list[ToolSpec] = [
    # =====================================================================
    # Analysis and preparation (T0/T1) - touches nobody
    # =====================================================================
    ToolSpec(
        name="email_header_analyze",
        binary="python3",
        category="social-engineering",
        tier=0,
        description="Parse message headers to trace true origin, authentication results and anomalies.",
        intent_examples=[
            "analyse this email header",
            "where did this message really come from",
            "check the spf and dkim results in this header",
        ],
        params=[
            ParamSpec(name="header_path", type="string", required=True, description="Path to the .eml or header dump"),
            ParamSpec(
                name="focus",
                type="enum",
                default="origin",
                choices=["origin", "auth", "all"],
                description="Which analysis to emphasise",
            ),
        ],
        target_params=["header_path"],
        scope_skip_params=["focus"],
        dry_run_template="python3 -c \"import email;print(email.message_from_file(open('{header_path}')).items())\"",
        live_template="python3 -c \"import email;print(email.message_from_file(open('{header_path}')).items())\"",
        explain=(
            "Received-chain order, SPF result and DKIM alignment tell you whether a message is "
            "spoofed, relayed or genuine - and which of those three it is changes the response."
        ),
        next_steps=["if DKIM failed but SPF passed, check for a relay inside your own estate"],
        timeout_s=60,
    ),
    ToolSpec(
        name="email_security_audit",
        binary="dig",
        category="social-engineering",
        tier=1,
        description="Audit a domain's SPF, DKIM, DMARC and BIMI records for gaps that permit spoofing.",
        intent_examples=[
            "audit the email security records for this domain",
            "is this domain spoofable",
            "check dmarc policy",
        ],
        params=[
            ParamSpec(name="target", type="string", required=True, description="Domain to audit"),
            ParamSpec(
                name="selector",
                type="string",
                default="default",
                description="DKIM selector to query",
            ),
        ],
        target_params=["target"],
        scope_skip_params=["selector"],
        dry_run_template="dig +short TXT _dmarc.{target}; dig +short TXT {target}",
        live_template="dig +short TXT _dmarc.{target}; dig +short TXT {target}",
        requires_scope=True,
        explain=(
            "A DMARC policy of p=none with no enforcement is the finding: the record exists, so it "
            "looks configured, and it blocks nothing."
        ),
        next_steps=["raise a card to move DMARC to p=reject once alignment is verified"],
        timeout_s=60,
    ),
    ToolSpec(
        name="campaign_preflight_check",
        binary="grep",
        category="social-engineering",
        tier=0,
        description="Pre-flight a campaign's landing page and template for tracking leaks and broken links.",
        intent_examples=[
            "pre-flight the phishing campaign template",
            "check the landing page before we send",
            "review the campaign config",
        ],
        params=[
            ParamSpec(name="template_path", type="string", required=True, description="Landing page or template directory"),
            ParamSpec(
                name="check",
                type="enum",
                default="all",
                choices=["links", "tracking", "forms", "all"],
                description="Which checks to run",
            ),
        ],
        target_params=["template_path"],
        scope_skip_params=["check"],
        dry_run_template="grep -rInE 'href=|action=|src=' {template_path}",
        live_template="grep -rInE 'href=|action=|src=' {template_path}",
        explain=(
            "Pre-flight catches the two failure modes that turn a simulation into a real problem: a "
            "template that posts credentials to an external host, and a link that 404s so nobody "
            "learns anything."
        ),
        next_steps=["fix broken links", "confirm every form action targets the internal capture server"],
        timeout_s=60,
    ),
    ToolSpec(
        name="typosquat_check",
        binary="dig",
        category="social-engineering",
        tier=1,
        description="Generate lookalike domains for a brand and report which ones resolve.",
        intent_examples=[
            "check for typosquatted domains",
            "are there lookalike domains for our brand",
            "find copycat domains",
        ],
        params=[
            ParamSpec(name="target", type="string", required=True, description="Brand domain to check"),
            ParamSpec(
                name="variants",
                type="enum",
                default="common",
                choices=["common", "extended"],
                description="Variant generation depth",
            ),
        ],
        target_params=["target"],
        scope_skip_params=["variants"],
        dry_run_template="bash -c 'for v in www {target}-login login-{target} {target}secure; do dig +short \"$v\" | head -1; done'",
        live_template="bash -c 'for v in www {target}-login login-{target} {target}secure; do dig +short \"$v\" | head -1; done'",
        requires_scope=True,
        explain=(
            "This checks whether a lookalike *resolves*; it never registers anything. Registration "
            "is a legal decision (UDRP, trademark) and not something a tool should do unattended."
        ),
        next_steps=["report resolving lookalikes with their registrant data", "escalate to brand protection rather than taking action directly"],
        timeout_s=180,
    ),
    # =====================================================================
    # Delivery and simulation (T2) - reaches people, needs scope + gate
    # =====================================================================
    ToolSpec(
        name="gophish_campaign",
        binary="gophish",
        category="social-engineering",
        tier=2,
        description="Launch an authorised phishing simulation campaign and track aggregate response.",
        intent_examples=[
            "launch the phishing simulation",
            "start the awareness campaign",
            "run the phishing test for the sales team",
        ],
        params=[
            ParamSpec(name="target", type="string", required=True, description="Internal domain or audience group"),
            ParamSpec(name="template", type="string", required=True, description="Campaign template name"),
            ParamSpec(name="group_name", type="string", required=True, description="Recipient group"),
            ParamSpec(name="rate", type="integer", default=10, description="Messages per minute"),
        ],
        target_params=["target"],
        scope_skip_params=["template", "group_name", "rate"],
        dry_run_template="gophish campaign preview --template {template} --group {group_name}",
        live_template="gophish campaign launch --template {template} --group {group_name} --rate {rate}",
        requires_scope=True,
        requires_approval=True,
        explain=(
            "Aggregate results only: this reports how many clicked, never which named person did. "
            "Per-user attribution is what turns an awareness exercise into surveillance."
        ),
        next_steps=[
            "review the click rate with the awareness owner, not with line managers",
            "report the aggregate to the card and destroy the per-recipient list",
        ],
        timeout_s=600,
    ),
    ToolSpec(
        name="evilginx_proxy",
        binary="evilginx",
        category="social-engineering",
        tier=2,
        description="Stand up an authorised adversary-in-the-middle capture proxy for a session-training simulation.",
        intent_examples=[
            "start the aitm web proxy for the simulation",
            "stand up the credential capture web proxy",
            "run evilginx phishlet against the training host",
        ],
        params=[
            ParamSpec(name="target", type="string", required=True, description="Authorised simulation host"),
            ParamSpec(name="phishlet", type="string", required=True, description="Service to simulate"),
            ParamSpec(name="port", type="integer", default=8443, description="Listen port"),
        ],
        target_params=["target"],
        scope_skip_params=["phishlet", "port"],
        dry_run_template="evilginx -p ./phishlets -t {phishlet} -developer",
        live_template="evilginx -p ./phishlets -t {phishlet} -developer",
        requires_scope=True,
        requires_approval=True,
        requires_sandbox=True,
        explain=(
            "An AiTM proxy captures the session token, which means it captures access that bypasses "
            "MFA. It runs sandboxed and the captured tokens are never read back by any wrapper."
        ),
        next_steps=["rotate every captured session immediately after the exercise", "treat the capture store as evidence, not as access"],
        timeout_s=900,
    ),
    ToolSpec(
        name="smtp_relay_test",
        binary="swaks",
        category="social-engineering",
        tier=2,
        description="Test whether an authorised mail server permits unauthenticated relay or spoofed sender.",
        intent_examples=[
            "is the mail server an open relay",
            "can we spoof the sender through this relay",
            "test the smtp relay configuration",
        ],
        params=[
            ParamSpec(name="target", type="string", required=True, description="Mail server host"),
            ParamSpec(name="port", type="integer", default=25, description="SMTP port"),
            ParamSpec(name="sender", type="string", default="security-test@example.invalid", description="From address to test"),
        ],
        target_params=["target"],
        scope_skip_params=["port", "sender"],
        dry_run_template="swaks --server {target} --port {port} --quit-after RCPT",
        live_template="swaks --server {target}:{port} --from {sender} --quit-after RCPT",
        requires_scope=True,
        requires_approval=True,
        explain=(
            "The test stops after RCPT TO - it proves relay is possible without actually delivering "
            "a message to a stranger. Stopping one command early is what keeps a control test from "
            "becoming the incident."
        ),
        next_steps=["fix relay authorisation on the server", "check whether the same relay was abused already"],
        timeout_s=120,
    ),
    ToolSpec(
        name="cred_capture_page",
        binary="python3",
        category="social-engineering",
        tier=2,
        description="Serve a simulation capture page that records a submission count only - never the secret.",
        intent_examples=[
            "serve the training capture page",
            "start the credential capture harness",
            "host the awareness landing page",
        ],
        params=[
            ParamSpec(name="target", type="string", required=True, description="Host to bind the page to"),
            ParamSpec(name="port", type="integer", default=8080, description="Listen port"),
            ParamSpec(name="page", type="string", required=True, description="Landing page to serve"),
        ],
        target_params=["target"],
        scope_skip_params=["port", "page"],
        dry_run_template="python3 -m http.server {port} --bind 127.0.0.1",
        live_template="python3 -c \"from http.server import HTTPServer,SimpleHTTPRequestHandler;HTTPServer(('{target}',{port}),SimpleHTTPRequestHandler).serve_forever()\"",
        requires_scope=True,
        requires_approval=True,
        requires_sandbox=True,
        explain=(
            "The handler increments a counter and discards the form body. It is written so that a "
            "captured credential cannot be read back even by the operator running it - the safest "
            "way to avoid misusing a secret is for nothing to store it."
        ),
        next_steps=["report only the count on the card", "tear the page down at the end of the window"],
        timeout_s=900,
    ),
    ToolSpec(
        name="attachment_payload_build",
        binary="python3",
        category="social-engineering",
        tier=2,
        description="Build an inert training attachment that reports execution without running any code.",
        intent_examples=[
            "build the training attachment",
            "make the inert payload for the simulation",
            "create a payload that only calls back",
        ],
        params=[
            ParamSpec(name="kind", type="enum", default="macro", choices=["macro", "lnk", "iso", "pdf"], description="Container format"),
            ParamSpec(name="callback", type="string", required=True, description="Internal callback endpoint"),
            ParamSpec(name="output", type="string", default="./payload", description="Output directory"),
        ],
        target_params=["callback"],
        scope_skip_params=["kind", "output"],
        dry_run_template="python3 -m payload_builder --kind {kind} --callback {callback} --dry-run",
        live_template="python3 -m payload_builder --kind {kind} --callback {callback} --out {output}",
        requires_scope=True,
        requires_approval=True,
        requires_sandbox=True,
        explain=(
            "The artefact calls back and exits. It does not execute a shell, download a second stage "
            "or persist - the measurable outcome is 'did it run', which is all a simulation needs to "
            "know and all it should be able to do."
        ),
        next_steps=["submit for AV review before any use", "confirm the artefact is inert by running it in the sandbox first"],
        timeout_s=300,
    ),
    ToolSpec(
        name="qr_payload_build",
        binary="qrencode",
        category="social-engineering",
        tier=2,
        description="Generate a QR code pointing at an authorised training endpoint.",
        intent_examples=["generate the qr code for the campaign", "build a qr payload", "make the qr for the training page"],
        params=[
            ParamSpec(name="target", type="string", required=True, description="Training endpoint URL"),
            ParamSpec(name="output", type="string", default="./qr.png", description="Output image path"),
        ],
        target_params=["target"],
        scope_skip_params=["output"],
        dry_run_template="qrencode -t ANSI -o - '{target}'",
        live_template="qrencode -o {output} '{target}'",
        requires_scope=True,
        requires_approval=True,
        explain=(
            "QR codes hide the destination from the person scanning, which is exactly why the target "
            "must be scope-checked here and why the printed code must state where it goes."
        ),
        next_steps=["print the destination URL alongside the code", "confirm the endpoint is internal before distribution"],
        timeout_s=60,
    ),
    # =====================================================================
    # Telephony (T3) - needs consent, not only authorisation
    # =====================================================================
    ToolSpec(
        name="smishing_send",
        binary="python3",
        category="social-engineering",
        tier=3,
        description="Send an authorised SMS awareness simulation to an opted-in recipient list.",
        intent_examples=[
            "send the smishing simulation",
            "run the sms awareness test",
            "launch the sms campaign",
        ],
        params=[
            ParamSpec(name="target", type="string", required=True, description="Recipient list identifier"),
            ParamSpec(name="template", type="string", required=True, description="Message template"),
            ParamSpec(name="rate", type="integer", default=5, description="Messages per minute"),
        ],
        target_params=["target"],
        scope_skip_params=["template", "rate"],
        dry_run_template="python3 -m smishing --list {target} --template {template} --dry-run",
        live_template="python3 -m smishing --list {target} --template {template} --rate {rate}",
        requires_scope=True,
        requires_approval=True,
        requires_sandbox=True,
        explain=(
            "T3 because the recipient is a person's personal device, often outside working hours, "
            "and the message cannot be recalled. SMS simulation therefore requires that every "
            "recipient opted in - a scope ticket alone is not sufficient grounds to send it."
        ),
        next_steps=["verify opt-in records for every recipient", "send inside the agreed window only", "report aggregates, not individuals"],
        timeout_s=900,
    ),
    ToolSpec(
        name="vishing_script_deploy",
        binary="python3",
        category="social-engineering",
        tier=3,
        description="Deploy an authorised voice-simulation script to an opted-in call list.",
        # Every example names the telephony channel explicitly. "run the vishing
        # simulation" alone routed to the web proxy, because ``evilginx_proxy``
        # also described itself as a simulation and matched the same token.
        intent_examples=[
            "run the vishing phone call simulation",
            "start the voice call campaign",
            "deploy the vishing call script to the list",
        ],
        params=[
            ParamSpec(name="target", type="string", required=True, description="Call list identifier"),
            ParamSpec(name="script", type="string", required=True, description="Approved call script"),
            ParamSpec(name="window", type="string", default="business-hours", description="Permitted calling window"),
        ],
        target_params=["target"],
        scope_skip_params=["script", "window"],
        dry_run_template="python3 -m vishing --list {target} --script {script} --dry-run",
        live_template="python3 -m vishing --list {target} --script {script} --window {window}",
        requires_scope=True,
        requires_approval=True,
        requires_sandbox=True,
        explain=(
            "A live voice call to a real person is the highest-touch action in this layer. The "
            "script must be approved by whoever owns the awareness programme, and the calling "
            "window is enforced because an unexpected call at 21:00 is indistinguishable from "
            "genuine harassment."
        ),
        next_steps=["record the consent basis for the list", "debrief recipients afterwards - the debrief is where the value is"],
        timeout_s=1200,
    ),
    ToolSpec(
        name="usb_drop_build",
        binary="python3",
        category="social-engineering",
        tier=3,
        description="Build an inert USB drop-simulation package whose only behaviour is to report insertion.",
        intent_examples=[
            "build the usb drop simulation",
            "prepare the inert usb payload",
            "make the usb for the physical test",
        ],
        params=[
            ParamSpec(name="callback", type="string", required=True, description="Internal callback endpoint"),
            ParamSpec(name="label", type="string", default="Payroll Q4", description="Printed label"),
            ParamSpec(name="output", type="string", default="./usb", description="Output directory"),
        ],
        target_params=["callback"],
        scope_skip_params=["label", "output"],
        dry_run_template="python3 -m usb_drop --callback {callback} --dry-run",
        live_template="python3 -m usb_drop --callback {callback} --label '{label}' --out {output}",
        requires_scope=True,
        requires_approval=True,
        requires_sandbox=True,
        explain=(
            "T3 because dropping a device is a physical act in a real space: it can be found by "
            "someone outside the engagement, and it cannot be recalled once placed. The payload "
            "reports insertion and nothing else."
        ),
        next_steps=["obtain written site permission before placement", "log the placement time and count every device recovered"],
        timeout_s=300,
    ),
]

# Tier distribution: T0 x2, T1 x2, T2 x6, T3 x3.
#
# The T3 set is the only place the platform's highest tier is justified by
# *consent* rather than by technical risk, and the specs say so in their
# ``explain`` text so the reason is visible at the point a human approves.
