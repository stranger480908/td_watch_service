# Infrastructure

AWS account `400875701866`, region `us-east-2` (Ohio).

## What is where

Three separate things create infrastructure here, and the split matters — only
one of them is reproducible.

| Layer | Created by | Reproducible |
|---|---|---|
| `bootstrap-oidc.yaml` → stack `tmwatch-oidc` | manual `aws cloudformation deploy` | yes |
| `template.yaml` → stack `tmwatch` | GitHub Actions on push to `main` | yes |
| RDS, VPC plumbing, IAM user | manual CLI commands | **no** |

The third row is the risk. Nothing captures it, so if the account were lost it
would have to be rebuilt from this document. Moving it into a template is worth
doing before there is customer data.

## Manually created (not in any template)

```
IAM user             tmwatch-admin            AdministratorAccess, CLI only, no console
VPC                  vpc-0038dbefbc3016567    default VPC, 172.31.0.0/16
Subnets              subnet-09476b6061e6dcbf7 us-east-2a
                     subnet-08c480c14d573bbdc us-east-2b
                     subnet-0728832d5f436244d us-east-2c (unused)
Route table          rtb-00a159a94a68ea5d7
RDS subnet group     tmwatch-db               spans 2a and 2b
RDS security group   sg-0eb457ab64b6b7680     inbound 5432 from 172.31.0.0/16 only
RDS instance         tmwatch                  db.t4g.micro, postgres 18, 20 GB gp3,
                                              not publicly accessible, single-AZ
```

Endpoint: `tmwatch.c9u0kq4yqg5r.us-east-2.rds.amazonaws.com:5432`
Database `tmwatch`, master user `tmwatch`. Password is in the `DATABASE_URL`
GitHub secret and nowhere else in this repo.

Default VPC subnets are public (they route to an internet gateway). The template
parameter is still called `PrivateSubnets`. This is harmless — what avoids a NAT
Gateway is the S3 gateway endpoint, not the subnet type — but the name is
misleading and should be corrected if the VPC is ever rebuilt properly.

## Stack: tmwatch-oidc

Deployed by hand, deliberately not by CI. CI must not be able to widen its own
permissions.

Creates the GitHub OIDC identity provider and the `tmwatch-github-deploy` role.

```bash
aws cloudformation deploy \
  --template-file bootstrap-oidc.yaml \
  --stack-name tmwatch-oidc \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides GitHubOwner=stranger480908 GitHubRepo=td_watch_service
```

Role ARN: `arn:aws:iam::400875701866:role/tmwatch-github-deploy`

## Stack: tmwatch

Deployed by `.github/workflows/ci.yml` on every push to `main`.

```
RawBucket            tmwatch-raw-400875701866-us-east-2   versioned, IA at 30d, Glacier IR at 120d
FetchFunction        outside VPC, daily cron 09:15 UTC
LoadFunction         tmwatch-LoadFunction-wStICvlZps1h    inside VPC
S3GatewayEndpoint    free; this is what avoids a NAT Gateway
LambdaSecurityGroup  egress only
OdpApiKey            Secrets Manager
AlarmTopic           SNS → email
IngestFailureAlarm   LoadFunction errors > 0 in 1h
NoRecordsAlarm       LoadFunction invocations < 1 in 36h
Budget               $25/month, alerts at 80% actual
```

### Why two Lambda functions

`FetchFunction` sits outside the VPC because it needs public internet for USPTO
and Secrets Manager. `LoadFunction` sits inside because it talks to RDS, and
reaches S3 through the gateway endpoint. DB credentials arrive as a
KMS-decrypted environment variable, resolved by the Lambda service before
invocation, so the VPC function needs no Secrets Manager reachability either.

Net effect: no NAT Gateway. Collapsing these into one VPC-attached function
reintroduces roughly $32/month of standing cost before a byte moves.

### Why fetch invokes load directly

An S3 `ObjectCreated` notification would make `RawBucket` depend on
`LoadFunction` (for the notification target) while `LoadFunction` depends on
`RawBucket` (for its IAM policy). CloudFormation rejects that as a circular
dependency, and `cfn-lint` does not catch it.

`FetchFunction` calls `LoadFunction` directly instead. This is also better: the
fetch already knows the bucket and key it just wrote, and replaying one day is a
single invoke with a known payload.

## GitHub configuration

Secrets: `AWS_DEPLOY_ROLE_ARN`, `DATABASE_URL`
Variables: `AWS_REGION`, `ALERT_EMAIL`, `VPC_ID`, `PRIVATE_SUBNETS`, `ROUTE_TABLE_IDS`

`DATABASE_URL` holds a password and must be a **secret**, not a variable —
variables are stored and displayed in plain text.

## Gotchas found the hard way

**The OIDC subject claim carries numeric IDs.** GitHub sends
`repo:owner@51971339/repo@1349121543:environment:production`, not
`repo:owner/repo:environment:production`. The trust policy uses wildcards
(`${GitHubOwner}*/${GitHubRepo}*`) to tolerate this. A policy written to the
documented format fails with `Not authorized to perform
sts:AssumeRoleWithWebIdentity`, which does not hint at the cause.

To read the claim rather than guess at it, add a step before
`configure-aws-credentials`:

```yaml
      - name: Show OIDC claims
        run: |
          TOKEN=$(curl -sH "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
            "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=sts.amazonaws.com" | jq -r .value)
          echo "$TOKEN" | cut -d. -f2 | base64 -d 2>/dev/null | jq '{sub, aud, repository}'
```

**`CreateOidcProvider=false` deletes the provider.** That parameter is for
accounts that already have a GitHub OIDC provider created elsewhere. Passing it
on a redeploy makes CloudFormation delete the provider it previously created,
and the next run fails with `The web identity token provided could not be
validated`. Always redeploy this stack with `CreateOidcProvider=true`.

**`sam deploy --resolve-s3` needs `s3:TagResource`.** Missing it fails the
managed-bucket stack with `ROLLBACK_COMPLETE` and no useful message in the
workflow log. Read the real reason with:

```bash
aws cloudformation describe-stack-events --stack-name aws-sam-cli-managed-default \
  --query 'StackEvents[?ResourceStatus==`CREATE_FAILED`].[LogicalResourceId,ResourceStatusReason]' \
  --output table
```

A stack left in `ROLLBACK_COMPLETE` or `REVIEW_IN_PROGRESS` cannot be updated
and must be deleted before retrying.

**The account is on the AWS Free Plan.** It rejected
`--backup-retention-period 7` with `FreeTierRestrictionError`; RDS was created
with `1`. The Free Plan also auto-closes when credits deplete or after six
months, which would take the database with it. Upgrading to the Paid Plan does
not touch running resources, and retention can be raised in place afterwards:

```bash
aws rds modify-db-instance --db-instance-identifier tmwatch \
  --backup-retention-period 7 --apply-immediately
```

Credit: $100, expires 2027-08-27. Estimated run rate ~$15/month, mostly RDS.

## Outstanding

- Migrations `001` and `002` have not been run. The database has no tables.
- `OdpApiKey` still contains `REPLACE_ME`. A real key needs a USPTO.gov account
  linked to a validated ID.me identity — start this early, it gates ingest.
- The SNS email subscription is pending until the confirmation link is clicked.
  Until then the alarms fire into nothing.
- Remove the `Show OIDC claims` debug step from `ci.yml`. It prints token claims
  into build logs.
- Upgrade to the AWS Paid Plan before there is customer data.
- Move the manually created resources into a template.

## Runbook

Stack status and outputs:

```bash
aws cloudformation describe-stacks --stack-name tmwatch \
  --query 'Stacks[0].{Status:StackStatus,Outputs:Outputs}' --output json
```

Why a deploy failed:

```bash
aws cloudformation describe-stack-events --stack-name tmwatch \
  --query 'StackEvents[?ResourceStatus==`CREATE_FAILED`].[LogicalResourceId,ResourceStatusReason]' \
  --output table
```

Replay one day through the loader:

```bash
aws lambda invoke --function-name tmwatch-LoadFunction-wStICvlZps1h \
  --payload '{"bucket":"tmwatch-raw-400875701866-us-east-2","key":"applications/2026/08/apc260827.zip"}' \
  --cli-binary-format raw-in-base64-out /dev/stdout
```

Set the real ODP key:

```bash
aws secretsmanager put-secret-value \
  --secret-id arn:aws:secretsmanager:us-east-2:400875701866:secret:OdpApiKey-11IXFHK9Sq9E-of9pWz \
  --secret-string '{"api_key":"YOUR_KEY"}'
```

Tear down (leaves the manually created RDS and VPC resources in place):

```bash
aws cloudformation delete-stack --stack-name tmwatch
aws cloudformation delete-stack --stack-name tmwatch-oidc
```
