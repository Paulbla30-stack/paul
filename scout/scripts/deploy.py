#!/usr/bin/env python3
"""Package and deploy the scout.

The SAM CLI is not installed here, and it is not needed: a SAM template is
a CloudFormation template with a transform, and CloudFormation processes
that transform server-side. So this script does what `sam deploy` would —
zip the source, put it in S3, point CodeUri at it, and hand the template to
CloudFormation.

    python3 scripts/deploy.py --profile openclaw
    python3 scripts/deploy.py --profile openclaw --dry-run

Nothing here creates an hourly-billed resource. Lambda, DynamoDB on-demand,
EventBridge Scheduler, SES and S3 are all pay-per-use.
"""
import argparse
import hashlib
import io
import os
import sys
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STACK = "jarvis-scout"
INCLUDE_DIRS = ("src",)
INCLUDE_FILES = ("config.toml",)
SKIP = {"__pycache__", ".pyc", ".local"}


def build_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for d in INCLUDE_DIRS:
            for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, d)):
                dirnames[:] = [x for x in dirnames if x not in SKIP]
                for fn in filenames:
                    if any(s in fn for s in SKIP):
                        continue
                    full = os.path.join(dirpath, fn)
                    z.write(full, os.path.relpath(full, ROOT))
        for f in INCLUDE_FILES:
            z.write(os.path.join(ROOT, f), f)
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    import boto3
    from botocore.exceptions import ClientError

    sess = boto3.Session(profile_name=a.profile) if a.profile else boto3.Session()
    acct = sess.client("sts").get_caller_identity()["Account"]
    bucket = f"jarvis-scout-artifacts-{acct}"
    s3 = sess.client("s3", region_name=a.region)
    cfn = sess.client("cloudformation", region_name=a.region)

    payload = build_zip()
    digest = hashlib.sha256(payload).hexdigest()
    key = f"code/{digest}.zip"
    print(f"package: {len(payload):,} bytes  sha256 {digest}")

    if a.dry_run:
        print("dry run: nothing uploaded, nothing deployed")
        return 0

    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError:
        print(f"creating artifact bucket {bucket}")
        kw = {"Bucket": bucket}
        if a.region != "us-east-1":
            kw["CreateBucketConfiguration"] = {"LocationConstraint": a.region}
        s3.create_bucket(**kw)
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True, "IgnorePublicAcls": True,
                "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        s3.put_bucket_encryption(
            Bucket=bucket,
            ServerSideEncryptionConfiguration={"Rules": [
                {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})

    s3.put_object(Bucket=bucket, Key=key, Body=payload)
    print(f"uploaded s3://{bucket}/{key}")

    # The HMAC key that signs approval links. CloudFormation cannot create a
    # SecureString, so it is created here, once, and never printed or logged.
    ssm = sess.client("ssm", region_name=a.region)
    param = "/jarvis/scout/approval-key"
    try:
        ssm.get_parameter(Name=param, WithDecryption=False)
        print(f"approval key present at {param}")
    except ssm.exceptions.ParameterNotFound:
        import secrets
        ssm.put_parameter(Name=param, Value=secrets.token_urlsafe(48),
                          Type="SecureString", Overwrite=False,
                          Description="HMAC key signing jarvis-scout approval links")
        print(f"created approval key at {param} (SecureString, 48 bytes, not shown)")

    tpl = open(os.path.join(ROOT, "template.yaml")).read()
    tpl = tpl.replace("CodeUri: ./", f"CodeUri: s3://{bucket}/{key}")

    exists = True
    try:
        st = cfn.describe_stacks(StackName=STACK)["Stacks"][0]["StackStatus"]
        if st in ("ROLLBACK_COMPLETE", "REVIEW_IN_PROGRESS"):
            print(f"stack is {st}; deleting the failed stack first")
            cfn.delete_stack(StackName=STACK)
            cfn.get_waiter("stack_delete_complete").wait(StackName=STACK)
            exists = False
    except ClientError:
        exists = False

    args = dict(StackName=STACK, TemplateBody=tpl,
                Capabilities=["CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"],
                Tags=[{"Key": "project", "Value": "jarvis-scout"}])
    try:
        if exists:
            cfn.update_stack(**args)
            waiter = "stack_update_complete"
        else:
            cfn.create_stack(**args)
            waiter = "stack_create_complete"
    except ClientError as e:
        if "No updates are to be performed" in str(e):
            print("no infrastructure changes; updating function code only")
            lam = sess.client("lambda", region_name=a.region)
            lam.update_function_code(FunctionName="jarvis-scout",
                                     S3Bucket=bucket, S3Key=key)
            print("function code updated")
            return 0
        raise

    print(f"waiting for {waiter} …")
    t0 = time.time()
    cfn.get_waiter(waiter).wait(StackName=STACK,
                                WaiterConfig={"Delay": 10, "MaxAttempts": 90})
    print(f"stack {STACK} ready in {time.time()-t0:.0f}s\n")
    for o in cfn.describe_stacks(StackName=STACK)["Stacks"][0].get("Outputs", []):
        print(f"  {o['OutputKey']:16s} {o['OutputValue']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
