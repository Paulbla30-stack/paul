#!/usr/bin/env python3
"""
Import a Hugging Face model into Amazon Bedrock (Custom Model Import) and
print the imported-model ARN to use as ``llm.model``.

What it does, end to end, with boto3 and the caller's AWS credentials:

1. Launches a temporary Amazon Linux 2023 instance with a large disk and
   the ``openclaw-model-fetcher`` role (SSM + write access to the bucket).
2. Over SSM, installs the Hugging Face CLI, downloads the repo (safetensors,
   config and tokenizer only) and syncs it to ``s3://<bucket>/<name>/``.
3. Terminates the instance.
4. Creates a Bedrock model-import job using the ``openclaw-bedrock-import``
   role, waits for it, and prints the ARN.

Usage:
  python3 aws/scripts/bedrock_import.py --hf-repo Qwen/Qwen2.5-7B-Instruct --name openclaw-qwen25-7b
  python3 aws/scripts/bedrock_import.py --s3-uri s3://bucket/prefix/ --name my-model   # weights already in S3

For gated repos (Llama, Mistral) pass --hf-token-secret <Secrets Manager name>
holding a Hugging Face read token; the fetch instance reads it with its role.
Custom Model Import is available in us-east-1 and us-west-2 only.
"""

import argparse
import sys
import time

import boto3

FETCH_SCRIPT = r"""#!/bin/bash
set -euo pipefail
REPO="$1"; DEST="$2"; TOKEN_SECRET="${3:-}"; REGION="$4"
dnf -y install python3.11 python3.11-pip awscli-2 >/dev/null 2>&1 || dnf -y install python3.11 python3.11-pip >/dev/null
python3.11 -m pip install --quiet --no-cache-dir "huggingface_hub[cli,hf_transfer]"
mkdir -p /data/model
if [ -n "$TOKEN_SECRET" ]; then
  export HF_TOKEN="$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$TOKEN_SECRET" --query SecretString --output text)"
fi
export HF_HUB_ENABLE_HF_TRANSFER=1
echo "[fetch] downloading $REPO"
python3.11 -m huggingface_hub.commands.huggingface_cli download "$REPO" \
  --include "*.safetensors" "*.json" "*.txt" "*.model" "*.tiktoken" "*.jinja" \
  --exclude "*consolidated*" "original/*" \
  --local-dir /data/model
echo "[fetch] files:"; ls -la /data/model | head -40
du -sh /data/model
echo "[fetch] syncing to $DEST"
aws s3 sync /data/model "$DEST" --region "$REGION" --only-show-errors
echo "[fetch] done"
"""


def log(msg):
    print(f"[import] {msg}", flush=True)


def latest_al2023(ec2):
    imgs = ec2.describe_images(Owners=["amazon"], Filters=[
        {"Name": "name", "Values": ["al2023-ami-2023.*-x86_64"]},
        {"Name": "architecture", "Values": ["x86_64"]}])["Images"]
    return sorted(imgs, key=lambda a: a["CreationDate"])[-1]["ImageId"]


def wait_ssm(ssm, iid, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        info = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [iid]}])["InstanceInformationList"]
        if info and info[0]["PingStatus"] == "Online":
            return True
        time.sleep(10)
    return False


def run_ssm(ssm, iid, commands, timeout, poll=15):
    cmd = ssm.send_command(InstanceIds=[iid], DocumentName="AWS-RunShellScript",
                           Parameters={"commands": commands, "executionTimeout": [str(timeout)]},
                           TimeoutSeconds=600)["Command"]["CommandId"]
    last = ""
    while True:
        time.sleep(poll)
        try:
            inv = ssm.get_command_invocation(CommandId=cmd, InstanceId=iid)
        except ssm.exceptions.InvocationDoesNotExist:
            continue
        out = inv.get("StandardOutputContent", "")
        if out != last:
            print(out[len(last):], end="", flush=True)
            last = out
        if inv["Status"] not in ("Pending", "InProgress", "Delayed"):
            if inv["Status"] != "Success":
                print(inv.get("StandardErrorContent", "")[-3000:], file=sys.stderr)
                raise SystemExit(f"fetch failed: {inv['Status']}")
            return


def fetch_to_s3(args, session):
    ec2 = session.client("ec2")
    ssm = session.client("ssm")
    dest = f"s3://{args.bucket}/{args.name}/"
    log(f"launching fetch instance {args.instance_type} with {args.disk_gb} GB disk")
    inst = ec2.run_instances(
        ImageId=latest_al2023(ec2), InstanceType=args.instance_type, MinCount=1, MaxCount=1,
        IamInstanceProfile={"Name": "openclaw-model-fetcher"},
        BlockDeviceMappings=[{"DeviceName": "/dev/xvda", "Ebs": {
            "VolumeSize": args.disk_gb, "VolumeType": "gp3", "DeleteOnTermination": True}}],
        MetadataOptions={"HttpTokens": "required"},
        TagSpecifications=[{"ResourceType": "instance", "Tags": [
            {"Key": "Name", "Value": f"openclaw-model-fetch-{args.name}"}, {"Key": "Project", "Value": "openclaw"}]}],
        InstanceInitiatedShutdownBehavior="terminate",
    )["Instances"][0]
    iid = inst["InstanceId"]
    log(f"instance {iid}; waiting for SSM")
    try:
        if not wait_ssm(ssm, iid):
            raise SystemExit("fetch instance never registered with SSM")
        script_b64 = __import__("base64").b64encode(FETCH_SCRIPT.encode()).decode()
        run_ssm(ssm, iid, [
            f"echo {script_b64} | base64 -d > /root/fetch.sh && chmod +x /root/fetch.sh",
            f"/root/fetch.sh '{args.hf_repo}' '{dest}' '{args.hf_token_secret or ''}' '{args.region}'",
        ], timeout=args.fetch_timeout)
    finally:
        log(f"terminating {iid}")
        ec2.terminate_instances(InstanceIds=[iid])
    return dest


def import_model(args, session, s3_uri):
    br = session.client("bedrock")
    sts = session.client("sts")
    acct = sts.get_caller_identity()["Account"]
    role_arn = f"arn:aws:iam::{acct}:role/openclaw-bedrock-import"
    job_name = f"{args.name}-{int(time.time())}"
    log(f"creating import job {job_name} from {s3_uri}")
    br.create_model_import_job(
        jobName=job_name, importedModelName=args.name, roleArn=role_arn,
        modelDataSource={"s3DataSource": {"s3Uri": s3_uri}},
    )
    while True:
        job = br.get_model_import_job(jobIdentifier=job_name)
        status = job["status"]
        if status in ("Completed", "Failed", "Stopped"):
            break
        log(f"job {status}; waiting")
        time.sleep(30)
    if status != "Completed":
        raise SystemExit(f"import {status}: {job.get('failureMessage', '')}")
    arn = job["importedModelArn"]
    log("import completed")
    print()
    print("Imported model ARN (set as llm.model):")
    print(arn)
    print()
    print("Quick test:")
    print(f"  aws bedrock-runtime converse --region {args.region} --model-id {arn} "
          "--messages '[{\"role\":\"user\",\"content\":[{\"text\":\"Reply with OK\"}]}]'")
    return arn


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True, help="Imported model name (letters, digits, hyphens)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--hf-repo", help="Hugging Face repo id, e.g. Qwen/Qwen2.5-7B-Instruct")
    src.add_argument("--s3-uri", help="Weights already in S3 (s3://bucket/prefix/)")
    ap.add_argument("--bucket", default=None, help="Bucket for weights (default openclaw-models-<account>)")
    ap.add_argument("--region", default="us-west-2")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--hf-token-secret", default=None, help="Secrets Manager secret holding a HF read token (gated repos)")
    ap.add_argument("--instance-type", default="m6i.xlarge")
    ap.add_argument("--disk-gb", type=int, default=300)
    ap.add_argument("--fetch-timeout", type=int, default=7200, help="seconds allowed for the download+sync")
    args = ap.parse_args()

    session = boto3.Session(region_name=args.region, profile_name=args.profile)
    if not args.bucket:
        acct = session.client("sts").get_caller_identity()["Account"]
        args.bucket = f"openclaw-models-{acct}"
    s3_uri = args.s3_uri or fetch_to_s3(args, session)
    import_model(args, session, s3_uri)


if __name__ == "__main__":
    main()
