"""Cloud and container tool frontends.

Workstream B, Phase 3. Blueprint ref: section 05 - the cloud row of the Kali
tool table.

Tier discipline for cloud work is different from network work, and the
difference is worth stating because it drives every tier assignment below.

A network tool's blast radius scales with how *intrusive* it is: a port scan
touches a host, an exploit changes it. Cloud tooling's blast radius scales with
**whose account the API call is made in**:

* **T0** - the tool reads a local artefact (a Terraform plan, a credential file).
  Nothing is called anywhere.
* **T1** - the tool calls a **read-only** provider API using *our* credentials,
  or queries **public** metadata. It observes the account; it does not change it
  and does not attempt to defeat an access control.
* **T2** - the tool **probes an access control** - asking an unauthenticated
  question of someone else's bucket to learn whether it is misconfigured. This is
  what makes it intrusive: it is a real attempt at authorisation, it is logged in
  a place the target can see, and it can trip a security response.
* **T3** - reserved for tools that would *use* a recovered secret. None are
  implemented here, deliberately: using a found credential is a separate,
  explicit act that should be wrapped and reviewed on its own, not a flag on an
  enumeration tool.

Several familiar names are deliberately absent. ``pacu``, ``cloudfox`` and
similar frameworks enumerate aggressively across whole accounts; a wrapper that
runs one of them is not a discrete tool invocation with a checkable target, so
it does not fit the one-spec-one-act model the guardrail engine enforces. They
belong behind an agent skill with their own scope discipline, not here.
"""
from __future__ import annotations

from ..spec import ParamSpec, ToolSpec

CLOUD_TOOLS: list[ToolSpec] = [
    # =====================================================================
    # Offline review (T0) - nothing is called anywhere
    # =====================================================================
    ToolSpec(
        name="terraform_plan_review",
        binary="terraform",
        category="cloud",
        tier=0,
        description="Review a Terraform plan for public exposure and missing encryption. Fully offline.",
        intent_examples=[
            "review the terraform plan for public buckets",
            "is anything in this plan exposed to the internet",
            "check the infra plan for insecure defaults",
        ],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="plan_path", type="string", required=True, description="Path to a plan or .tf directory"),
        ],
        target_params=["plan_path"],
        dry_run_template="terraform show -json {plan_path}",
        live_template="terraform show -json {plan_path}",
        explain=(
            "Reading the plan catches the misconfiguration *before* it is applied, which is the "
            "only point at which it costs nothing to fix."
        ),
        next_steps=["fix public ACLs in the plan, then re-run", "check the applied state for drift"],
        timeout_s=120,
    ),
    ToolSpec(
        name="cloud_key_audit",
        binary="grep",
        category="cloud",
        tier=0,
        description="Scan local credential files for long-lived keys, weak permissions and stale entries.",
        intent_examples=[
            "audit the local cloud credential file",
            "are there old access keys on this host",
            "check cloud credential hygiene on disk",
        ],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="key_path", type="string", required=True, description="Credentials directory"),
            ParamSpec(
                name="pattern",
                type="enum",
                default="keys",
                choices=["keys", "secrets", "tokens", "all"],
                description="What to look for",
            ),
        ],
        target_params=["key_path"],
        scope_skip_params=["pattern"],
        dry_run_template="grep -rIl --include=* {key_path}",
        live_template="grep -rIl --include=* {key_path}",
        explain=(
            "Credential sprawl on a workstation is the most common route from a laptop compromise "
            "to a cloud account takeover."
        ),
        next_steps=["rotate anything older than the policy allows", "move keys to a short-lived role"],
        timeout_s=60,
    ),
    # =====================================================================
    # Read-only account enumeration (T1) - our credentials, no changes
    # =====================================================================
    ToolSpec(
        name="aws_iam_enum",
        binary="aws",
        category="cloud",
        tier=1,
        description="Enumerate IAM users, roles and attached policies in an AWS account (read-only).",
        intent_examples=[
            "enumerate the aws iam users",
            "what roles exist in this account",
            "list the iam policies attached to the account",
        ],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Account id or alias under review"),
            ParamSpec(
                name="region",
                type="enum",
                default="us-east-1",
                choices=["us-east-1", "us-east-2", "us-west-1", "us-west-2", "eu-west-1", "eu-central-1", "ap-southeast-1"],
                description="Region context",
            ),
            ParamSpec(name="profile", type="string", default="default", description="Named CLI profile"),
        ],
        target_params=["target"],
        scope_skip_params=["region", "profile"],
        dry_run_template="aws iam list-users --profile {profile} --region {region}",
        live_template="aws iam list-users --profile {profile} --region {region}",
        requires_scope=True,
        explain=(
            "Wildcard policies, unused users and roles trusting an external account are the three "
            "findings that matter most, and all three are visible from a read-only listing."
        ),
        next_steps=["flag any policy with Resource:* and Action:*", "check trust relationships for external accounts"],
        timeout_s=120,
    ),
    ToolSpec(
        name="aws_s3_audit",
        binary="aws",
        category="cloud",
        tier=1,
        description="Audit S3 bucket configuration: ACLs, encryption, versioning and public-access block.",
        intent_examples=[
            "audit the s3 bucket configuration",
            "is this bucket encrypted",
            "check the bucket public access settings",
        ],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Bucket name in our account"),
            ParamSpec(name="profile", type="string", default="default", description="Named CLI profile"),
        ],
        target_params=["target"],
        scope_skip_params=["profile"],
        dry_run_template="aws s3api get-bucket-acl --bucket {target} --profile {profile}",
        live_template="aws s3api get-bucket-acl --bucket {target} --profile {profile}",
        requires_scope=True,
        explain=(
            "An ACL granting AllUsers read is a finding on its own; combined with a missing "
            "public-access block it is the classic data-leak configuration."
        ),
        next_steps=["enable public-access block where it is off", "verify encryption at rest is on"],
        timeout_s=90,
    ),
    ToolSpec(
        name="azure_tenant_enum",
        binary="az",
        category="cloud",
        tier=1,
        description="Enumerate an Azure AD tenant's public configuration: domains, branding, federation.",
        intent_examples=[
            "enumerate the azure tenant",
            "is this tenant federated",
            "what is the azure ad configuration for this domain",
        ],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Tenant primary domain"),
            ParamSpec(
                name="view",
                type="enum",
                default="domains",
                choices=["domains", "federation", "branding", "all"],
                description="Which view to collect",
            ),
        ],
        target_params=["target"],
        scope_skip_params=["view"],
        dry_run_template="az rest --url https://login.microsoftonline.com/{target}/v2.0/.well-known/openid-configuration",
        live_template="az rest --url https://login.microsoftonline.com/{target}/v2.0/.well-known/openid-configuration",
        requires_scope=True,
        explain=(
            "The tenant's public metadata is retrievable without authenticating, so it is a genuine "
            "passive check - but it does tell the tenant's logs that you looked."
        ),
        next_steps=["check for legacy authentication still enabled", "review conditional-access coverage"],
        timeout_s=60,
    ),
    ToolSpec(
        name="gcp_iam_enum",
        binary="gcloud",
        category="cloud",
        tier=1,
        description="Review GCP IAM policy bindings for a project (read-only).",
        intent_examples=[
            "review the gcp iam policy",
            "who has owner on this project",
            "check for overly broad gcp roles",
        ],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Project id"),
            ParamSpec(
                name="role_filter",
                type="enum",
                default="broad",
                choices=["broad", "all", "primitive"],
                description="Which bindings to surface",
            ),
        ],
        target_params=["target"],
        scope_skip_params=["role_filter"],
        dry_run_template="gcloud projects get-iam-policy {target} --format=json",
        live_template="gcloud projects get-iam-policy {target} --format=json",
        requires_scope=True,
        explain=(
            "Primitive roles (owner/editor) granted to a service account are the usual finding; "
            "they are effectively unrevokable in practice because everything depends on them."
        ),
        next_steps=["replace primitive roles with least-privilege equivalents", "review service-account key age"],
        timeout_s=90,
    ),
    ToolSpec(
        name="k8s_api_audit",
        binary="kubectl",
        category="cloud",
        tier=1,
        description="Audit a Kubernetes API server: anonymous auth, version disclosure and admission config.",
        intent_examples=[
            "audit the kubernetes api server",
            "is anonymous auth enabled on the cluster",
            "check the k8s api exposure",
        ],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="API server endpoint"),
            ParamSpec(name="port", type="integer", default=6443, description="API server port"),
        ],
        target_params=["target"],
        dry_run_template="kubectl --server=https://{target}:{port} version --output=json",
        live_template="kubectl --server=https://{target}:{port} version --output=json",
        requires_scope=True,
        explain=(
            "Anonymous authentication plus a permissive role binding is a full cluster compromise, "
            "and it is a configuration mistake rather than an exploit."
        ),
        next_steps=["disable anonymous auth", "audit every ClusterRoleBinding to system:anonymous"],
        timeout_s=60,
    ),
    ToolSpec(
        name="k8s_rbac_audit",
        binary="kubectl",
        category="cloud",
        tier=1,
        description="Review Kubernetes RBAC for wildcard verbs, cluster-admin grants and secret read access.",
        intent_examples=[
            "audit the cluster rbac",
            "who can read secrets in this cluster",
            "find wildcard roles in kubernetes",
        ],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="API server endpoint"),
            ParamSpec(name="namespace", type="string", default="default", description="Namespace to scope the review"),
        ],
        target_params=["target"],
        scope_skip_params=["namespace"],
        dry_run_template="kubectl --server=https://{target} get clusterrolebindings -o json",
        live_template="kubectl --server=https://{target} get clusterrolebindings -o json",
        requires_scope=True,
        explain=(
            "Read access to secrets is equivalent to cluster-admin for most purposes, because every "
            "service token is stored there."
        ),
        next_steps=["remove wildcard verbs from non-system roles", "check for bindings to default service accounts"],
        timeout_s=90,
    ),
    ToolSpec(
        name="docker_registry_enum",
        binary="curl",
        category="cloud",
        tier=1,
        description="Enumerate a container registry's catalog and tags via its API.",
        intent_examples=[
            "enumerate the docker registry",
            "what images are in this registry",
            "list the registry repositories",
        ],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Registry host"),
            ParamSpec(name="repo", type="string", default="library", description="Repository namespace"),
        ],
        target_params=["target"],
        scope_skip_params=["repo"],
        dry_run_template="curl -s https://{target}/v2/_catalog",
        live_template="curl -s https://{target}/v2/_catalog",
        requires_scope=True,
        explain=(
            "An open registry catalog exposes the whole image inventory; tags then reveal which "
            "builds are old enough to carry known-vulnerable base images."
        ),
        next_steps=["check base-image age for the oldest tags", "verify the registry requires auth"],
        timeout_s=60,
    ),
    ToolSpec(
        name="serverless_enum",
        binary="aws",
        category="cloud",
        tier=1,
        description="Enumerate serverless functions and their public invocation endpoints (read-only).",
        intent_examples=[
            "enumerate the lambda functions",
            "which serverless functions are public",
            "list the function urls",
        ],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Account or project under review"),
            ParamSpec(
                name="runtime",
                type="enum",
                default="all",
                choices=["all", "python", "nodejs", "java", "go", "dotnet"],
                description="Filter by runtime",
            ),
        ],
        target_params=["target"],
        scope_skip_params=["runtime"],
        dry_run_template="aws lambda list-functions --query 'Functions[].FunctionName'",
        live_template="aws lambda list-functions --query 'Functions[].FunctionName'",
        requires_scope=True,
        explain=(
            "A function URL with AuthType NONE is an internet-facing endpoint nobody in the "
            "organisation remembers creating. That is the finding to look for."
        ),
        next_steps=["check each function URL auth type", "review the function's IAM role for wildcard permissions"],
        timeout_s=90,
    ),
    ToolSpec(
        name="ssl_cert_inventory",
        binary="openssl",
        category="cloud",
        tier=1,
        description=(
            "Inventory TLS certificates in a **local** directory: expiry, key size and issuer drift. "
            "For a certificate served by a remote host, use tls_probe instead."
        ),
        # Deliberately phrased around *local files* rather than certificates in
        # general. An earlier wording ("check the tls certificate") collided with
        # tls_probe, which probes a remote endpoint - and the router sent the
        # remote-hunting phrase to the local-file tool. The two tools answer
        # different questions and the examples now say which is which.
        intent_examples=[
            "inventory the certificates in this directory",
            "which local certs expire soon",
            "check certificate key sizes on disk",
            "audit the certificate store in this folder",
        ],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Directory holding certificates"),
            ParamSpec(name="days", type="integer", default=30, description="Expiry warning window"),
        ],
        target_params=["target"],
        dry_run_template="openssl x509 -in {target} -noout -subject -dates",
        live_template="openssl x509 -in {target} -noout -subject -dates",
        requires_scope=True,
        explain=(
            "Certificate expiry is the outage everyone sees coming and nobody fixes; an inventory "
            "turns it into a scheduled task."
        ),
        next_steps=["raise a renewal card for anything inside the window", "flag any key below 2048 bits"],
        timeout_s=120,
    ),
    # =====================================================================
    # Access-control probing (T2) - asks a question of someone else's store
    # =====================================================================
    ToolSpec(
        name="s3_bucket_probe",
        binary="curl",
        category="cloud",
        tier=2,
        description="Probe an S3 bucket's public access: list, read and ACL check without credentials.",
        # Phrased with an explicit ``s3`` token. "is this bucket public" alone was
        # ambiguous enough that the read-only account audit (which mentions
        # buckets) out-ranked the actual exposure probe.
        intent_examples=[
            "is this s3 bucket public",
            "can we list the s3 bucket contents",
            "probe the s3 bucket for misconfiguration",
        ],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Bucket name or host"),
            ParamSpec(
                name="mode",
                type="enum",
                default="list",
                choices=["list", "acl", "read", "all"],
                description="Which access to test",
            ),
        ],
        target_params=["target"],
        scope_skip_params=["mode"],
        dry_run_template="curl -s -o /dev/null -w '%{{http_code}}' https://{target}.s3.amazonaws.com/",
        live_template="curl -s https://{target}.s3.amazonaws.com/",
        requires_scope=True,
        requires_approval=True,
        explain=(
            "A 200 on the list operation means the bucket is world-readable. This is an attempt at "
            "authorisation, which is why it needs a scope *and* a human gate - not just a scope."
        ),
        next_steps=["list what the bucket exposes, then stop", "report as high severity with the exact request"],
        timeout_s=60,
    ),
    ToolSpec(
        name="azure_blob_probe",
        binary="curl",
        category="cloud",
        tier=2,
        description="Probe an Azure blob container for anonymous access and public listing.",
        intent_examples=[
            "is this azure blob container public",
            "probe azure blob storage for anonymous access",
            "can we list the azure blob container",
        ],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Storage account name"),
            ParamSpec(name="container", type="string", required=True, description="Container name"),
            ParamSpec(
                name="mode",
                type="enum",
                default="list",
                choices=["list", "read", "all"],
                description="Which access to test",
            ),
        ],
        target_params=["target"],
        scope_skip_params=["container", "mode"],
        dry_run_template="curl -s -o /dev/null -w '%{{http_code}}' 'https://{target}.blob.core.windows.net/{container}?restype=container&comp=list'",
        live_template="curl -s 'https://{target}.blob.core.windows.net/{container}?restype=container&comp=list'",
        requires_scope=True,
        requires_approval=True,
        explain=(
            "Container-level anonymous read is the Azure equivalent of a public S3 bucket and is "
            "equally common after a quick migration."
        ),
        next_steps=["set the container access level to private", "check for stored access policies with no expiry"],
        timeout_s=60,
    ),
    ToolSpec(
        name="gcp_bucket_probe",
        binary="curl",
        category="cloud",
        tier=2,
        description="Probe a GCS bucket for public IAM bindings and unauthenticated object listing.",
        intent_examples=[
            "is this gcs bucket public",
            "probe the gcs bucket for public access",
            "is this google cloud storage bucket open",
        ],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Bucket name"),
            ParamSpec(
                name="mode",
                type="enum",
                default="list",
                choices=["list", "iam", "all"],
                description="Which access to test",
            ),
        ],
        target_params=["target"],
        scope_skip_params=["mode"],
        dry_run_template="curl -s -o /dev/null -w '%{{http_code}}' https://storage.googleapis.com/storage/v1/b/{target}/o",
        live_template="curl -s https://storage.googleapis.com/storage/v1/b/{target}/o",
        requires_scope=True,
        requires_approval=True,
        explain=(
            "allUsers bound to Storage Object Viewer is the GCP equivalent of a public bucket; the "
            "IAM view is often more revealing than the object listing."
        ),
        next_steps=["remove allUsers and allAuthenticatedUsers bindings", "check for uniform bucket-level access"],
        timeout_s=60,
    ),
    ToolSpec(
        name="cloud_metadata_probe",
        binary="curl",
        category="cloud",
        tier=2,
        description="Test whether a cloud instance metadata endpoint is reachable from an exposed service.",
        intent_examples=[
            "is the metadata endpoint reachable",
            "test for ssrf to the cloud metadata service",
            "can we reach 169.254.169.254 through this host",
        ],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="URL to test through"),
            ParamSpec(name="path", type="string", default="/latest/meta-data/", description="Metadata path to request"),
        ],
        target_params=["target"],
        scope_skip_params=["path"],
        dry_run_template="curl -s -o /dev/null -w '%{{http_code}}' '{target}?url=http://169.254.169.254{path}'",
        live_template="curl -s '{target}?url=http://169.254.169.254{path}'",
        requires_scope=True,
        requires_approval=True,
        explain=(
            "Reachable instance metadata is the standard route from an SSRF to cloud credentials. "
            "On IMDSv2 this fails, which is itself the useful result."
        ),
        next_steps=["if metadata is reachable, treat every credential on the instance as disclosed", "enforce IMDSv2"],
        timeout_s=60,
    ),
    ToolSpec(
        name="cloud_bucket_bruteforce",
        binary="bash",
        category="cloud",
        tier=2,
        description="Enumerate bucket-name variants from a wordlist to find unreferenced storage.",
        intent_examples=[
            "brute force bucket names for this domain",
            "find unlisted storage buckets",
            "enumerate bucket naming variants",
        ],
        integration="cli_wrapper",
        params=[
            ParamSpec(name="target", type="string", required=True, description="Organisation domain or prefix"),
            ParamSpec(name="wordlist", type="string", default="/usr/share/wordlists/bucket-names.txt", description="Name wordlist"),
            ParamSpec(name="threads", type="integer", default=5, description="Concurrency"),
        ],
        target_params=["target"],
        scope_skip_params=["wordlist", "threads"],
        dry_run_template="bash -c 'while read n; do curl -s -o /dev/null -w \"%{{http_code}} {target}-$n\\n\"; done < {wordlist}'",
        live_template="bash -c 'while read n; do curl -s -o /dev/null -w \"%{{http_code}} {target}-$n\\n\"; done < {wordlist}'",
        requires_scope=True,
        requires_approval=True,
        explain=(
            "Shadow storage is the finding: buckets that exist but are referenced nowhere, so nobody "
            "owns them and nothing rotates their contents."
        ),
        next_steps=["confirm ownership of each hit before reporting", "never read object contents without a new gate"],
        timeout_s=300,
    ),
]

# Guardrail tier distribution for this module: T0 x2, T1 x10, T2 x5.
# No T3: see the module docstring - using a recovered secret is a separate act
# that deserves its own reviewed wrapper rather than a flag on an enumerator.
