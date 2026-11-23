#!/usr/bin/env python3
"""Run the adversarial batch on a disposable GCP Compute Engine VM.

The VM uses the same Ubuntu + Docker environment as the local pipeline, so the
Harbor task images, verifiers, and cleanup behavior are unchanged. It is meant
for short-lived batches: ``up`` provisions, ``run`` starts a batch in tmux and
uploads the results to GCS, then the VM deletes itself. ``fetch`` downloads the
GCS tarball; ``down`` removes the SSH firewall rule.

Only the Compute Engine REST API and plain SSH are used. The local gcloud
``compute`` command group is not required (it can be slow on some installs).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATE_DIR = REPO / 'work' / 'gcp-batch'
STATE_PATH = STATE_DIR / 'state.json'
DEFAULT_IMAGE = 'projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _log(msg: str) -> None:
    print(msg, flush=True)


def _gcloud() -> str:
    candidates = [
        os.environ.get('GCLOUD'),
        shutil.which('gcloud'),
        str(Path.home() / 'Desktop' / 'ai_builder_tutorial' / 'vllm'
            / 'google-cloud-sdk' / 'bin' / 'gcloud'),
        str(Path.home() / 'google-cloud-sdk' / 'bin' / 'gcloud'),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise SystemExit('gcloud not found; set GCLOUD=/path/to/gcloud')


def _project() -> str:
    if os.environ.get('GOOGLE_CLOUD_PROJECT'):
        return os.environ['GOOGLE_CLOUD_PROJECT']
    try:
        return subprocess.check_output(
            [_gcloud(), 'config', 'get-value', 'project'],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ''


def _token() -> str:
    return subprocess.check_output(
        [_gcloud(), 'auth', 'print-access-token'],
        text=True, stderr=subprocess.DEVNULL,
    ).strip()


class Rest:
    def __init__(self, project: str):
        self.project = project
        self.token = _token()

    def call(self, method: str, url: str, body: dict | None = None,
             allow_404: bool = False) -> dict | None:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={
                'Authorization': f'Bearer {self.token}',
                'Content-Type': 'application/json',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode(errors='replace')
            if exc.code == 404 and allow_404:
                return None
            raise RuntimeError(f'{method} {url} -> {exc.code}: {payload[:800]}') from exc

    def wait_operation(self, operation: dict | None, *, timeout: int = 900) -> dict:
        if not operation:
            return {}
        name = operation.get('name', '')
        deadline = time.time() + timeout
        while time.time() < deadline:
            current = self.call('GET', operation['selfLink'])
            if current and current.get('status') == 'DONE':
                if current.get('error'):
                    raise RuntimeError(f'operation failed: {json.dumps(current["error"])[:800]}')
                return current
            time.sleep(3)
        raise TimeoutError(f'operation {name} did not finish in {timeout}s')

    def instance_url(self, zone: str, name: str) -> str:
        return (f'https://compute.googleapis.com/compute/v1/projects/{self.project}'
                f'/zones/{zone}/instances/{name}')

    def get_instance(self, zone: str, name: str) -> dict | None:
        return self.call('GET', self.instance_url(zone, name), allow_404=True)

    def start_instance(self, zone: str, name: str) -> dict | None:
        op = self.call('POST', f'{self.instance_url(zone, name)}/start')
        return self.wait_operation(op)

    def delete_instance(self, zone: str, name: str) -> None:
        op = self.call('DELETE', self.instance_url(zone, name), allow_404=True)
        if op:
            self.wait_operation(op)

    def project_info(self) -> dict:
        return self.call(
            'GET',
            f'https://compute.googleapis.com/compute/v1/projects/{self.project}',
        ) or {}

    def bucket_url(self, bucket: str) -> str:
        return f'https://storage.googleapis.com/storage/v1/b/{bucket}'

    def get_bucket(self, bucket: str) -> dict | None:
        return self.call('GET', self.bucket_url(bucket), allow_404=True)

    def create_bucket(self, bucket: str, location: str) -> dict | None:
        op = self.call(
            'POST',
            f'https://storage.googleapis.com/storage/v1/b?project={self.project}',
            {
                'name': bucket,
                'location': location,
                'storageClass': 'STANDARD',
                'iamConfiguration': {'uniformBucketLevelAccess': {'enabled': True}},
                'lifecycle': {'rule': [
                    {'action': {'type': 'Delete'}, 'condition': {'age': 90}},
                ]},
            },
        )
        return op

    def get_bucket_iam(self, bucket: str) -> dict:
        return self.call('GET', f'{self.bucket_url(bucket)}/iam') or {}

    def set_bucket_iam(self, bucket: str, policy: dict) -> dict:
        return self.call('PUT', f'{self.bucket_url(bucket)}/iam', policy) or {}

    def get_service_account(self, email: str) -> dict | None:
        encoded = urllib.parse.quote(email, safe='@')
        return self.call(
            'GET',
            f'https://iam.googleapis.com/v1/projects/{self.project}/serviceAccounts/{encoded}',
            allow_404=True,
        )

    def create_service_account(self, account_id: str, display_name: str) -> dict:
        return self.call(
            'POST',
            f'https://iam.googleapis.com/v1/projects/{self.project}/serviceAccounts',
            {'accountId': account_id, 'serviceAccount': {'displayName': display_name}},
        ) or {}

    def get_instance_iam(self, zone: str, name: str) -> dict:
        return self.call(
            'GET', f'{self.instance_url(zone, name)}/getIamPolicy',
        ) or {}

    def set_instance_iam(self, zone: str, name: str, policy: dict) -> dict:
        return self.call(
            'POST', f'{self.instance_url(zone, name)}/setIamPolicy',
            {'policy': policy},
        ) or {}

    def firewall_url(self, name: str) -> str:
        return (f'https://compute.googleapis.com/compute/v1/projects/{self.project}'
                f'/global/firewalls/{name}')

    def get_firewall(self, name: str) -> dict | None:
        return self.call('GET', self.firewall_url(name), allow_404=True)

    def delete_firewall(self, name: str) -> None:
        op = self.call('DELETE', self.firewall_url(name), allow_404=True)
        if op:
            self.wait_operation(op)


def _state() -> dict:
    if STATE_PATH.is_file():
        return json.loads(STATE_PATH.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + '\n')


def _public_ip() -> str:
    with urllib.request.urlopen('https://api.ipify.org', timeout=20) as resp:
        return resp.read().decode().strip()


def _ssh_opts(key: Path) -> list[str]:
    return [
        '-i', str(key),
        '-o', 'StrictHostKeyChecking=no',
        '-o', 'UserKnownHostsFile=/dev/null',
        '-o', 'ConnectTimeout=10',
        '-o', 'ServerAliveInterval=30',
        '-o', 'ServerAliveCountMax=240',
    ]


def _ssh(state: dict, command: str, *, check: bool = True,
         capture: bool = False, timeout: int | None = None) -> subprocess.CompletedProcess:
    args = ['ssh', *_ssh_opts(Path(state['key'])),
            f"{state['ssh_user']}@{state['ip']}", command]
    return subprocess.run(
        args, check=check, text=True, timeout=timeout,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def _wait_ssh(state: dict, *, timeout: int = 300) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if _ssh(state, 'true', check=False, capture=True, timeout=15).returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(5)
    raise TimeoutError(f'SSH to {state["ip"]} did not become ready')


def _wait_startup(state: dict, timeout: int = 900) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _ssh(state, 'test -f /var/lib/adv-batch-ready && echo ready',
                      check=False, capture=True, timeout=30)
        if 'ready' in (result.stdout or ''):
            return
        time.sleep(10)
    raise TimeoutError('startup script did not finish; see /var/log/adv-batch-startup.log')


def _startup_script(ssh_user: str) -> str:
    return f'''#!/bin/bash
set -euxo pipefail
exec >>/var/log/adv-batch-startup.log 2>&1
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg git rsync tmux \
  python3 python3-venv python3-pip jq
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${{VERSION_CODENAME}} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
usermod -aG docker {ssh_user}
su - {ssh_user} -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
su - {ssh_user} -c '$HOME/.local/bin/uv tool install harbor==0.24.0'
touch /var/lib/adv-batch-ready
'''


def _sync_repo(state: dict) -> None:
    remote = state['remote_dir']
    _ssh(state, f'mkdir -p {shlex.quote(remote)}')
    excludes = [
        '.git', '.venv', 'work', '__pycache__', '.pytest_cache',
        '.cursorrules', '*.pyc', '.DS_Store',
    ]
    cmd = ['rsync', '-rlptz', '--delete', *[f'--exclude={e}' for e in excludes],
           '-e', ' '.join(shlex.quote(x) for x in ['ssh', *_ssh_opts(Path(state['key']))]),
           f'{REPO}/', f"{state['ssh_user']}@{state['ip']}:{remote}/"]
    subprocess.run(cmd, check=True)
    env_file = REPO / '.env'
    if env_file.is_file():
        subprocess.run(
            ['scp', *_ssh_opts(Path(state['key'])), str(env_file),
             f"{state['ssh_user']}@{state['ip']}:{remote}/.env"],
            check=True,
        )
        _ssh(state, f'chmod 600 {shlex.quote(remote)}/.env')


def _remote_setup(state: dict) -> None:
    remote = shlex.quote(state['remote_dir'])
    _ssh(state, f'cd {remote} && python3 -m venv .venv && '
                '.venv/bin/pip install -q --upgrade pip && '
                ".venv/bin/pip install -q -e '.[test]' && "
                '.venv/bin/python -c "import adversarial; print(adversarial.__file__)"')


def _ensure_bucket(rest: Rest, bucket: str, location: str, service_account: str) -> None:
    if not service_account:
        raise RuntimeError('No default compute service account; cannot configure GCS upload')
    if not rest.get_bucket(bucket):
        _log(f'Creating GCS bucket gs://{bucket} in {location}')
        try:
            rest.create_bucket(bucket, location)
        except RuntimeError:
            if not rest.get_bucket(bucket):
                raise
    policy = rest.get_bucket_iam(bucket)
    bindings = policy.get('bindings', [])
    member = f'serviceAccount:{service_account}'
    role = 'roles/storage.objectAdmin'
    changed = False
    for binding in bindings:
        if binding.get('role') == role:
            if member not in binding.get('members', []):
                binding.setdefault('members', []).append(member)
                changed = True
            break
    else:
        bindings.append({'role': role, 'members': [member]})
        changed = True
    if changed:
        policy['bindings'] = bindings
        rest.set_bucket_iam(bucket, policy)
        _log(f'Granted {role} on gs://{bucket} to {service_account}')


def _ensure_service_account(rest: Rest, account_id: str, display_name: str) -> str:
    email = f'{account_id}@{rest.project}.iam.gserviceaccount.com'
    if rest.get_service_account(email):
        return email
    _log(f'Creating service account {email}')
    try:
        rest.create_service_account(account_id, display_name)
    except RuntimeError:
        if not rest.get_service_account(email):
            raise
    return email


def _grant_instance_admin(rest: Rest, zone: str, name: str, member: str) -> None:
    policy = rest.get_instance_iam(zone, name)
    bindings = policy.get('bindings', [])
    role = 'roles/compute.instanceAdmin.v1'
    principal = f'serviceAccount:{member}'
    for binding in bindings:
        if binding.get('role') == role:
            if principal not in binding.get('members', []):
                binding.setdefault('members', []).append(principal)
            break
    else:
        bindings.append({'role': role, 'members': [principal]})
    policy['bindings'] = bindings
    rest.set_instance_iam(zone, name, policy)


def _download_gcs_tarball(bucket: str, prefix: str, dest: Path) -> str:
    name = f'{prefix}/results.tar.gz'
    encoded = urllib.parse.quote(name, safe='')
    url = (f'https://storage.googleapis.com/storage/v1/b/{bucket}'
           f'/o/{encoded}?alt=media')
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {_token()}'})
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tarball = STATE_DIR / 'results.tar.gz'
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            with tarball.open('wb') as handle:
                shutil.copyfileobj(resp, handle)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise FileNotFoundError(name) from exc
        raise
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(tarball, 'r:gz') as archive:
            archive.extractall(dest)
    finally:
        tarball.unlink(missing_ok=True)
    return name


def cmd_up(args: argparse.Namespace) -> int:
    if not args.project:
        args.project = _project()
    if not args.project:
        raise SystemExit('No GCP project; pass --project or run `gcloud config set project ...`')
    state = _state()
    rest = Rest(args.project)
    region = args.zone.rsplit('-', 1)[0]
    service_account = _ensure_service_account(
        rest, 'adv-batch-runner', 'Adversarial batch VM runner')
    bucket = args.bucket or f'adv-eval-results-{args.project}'
    _ensure_bucket(rest, bucket, region, service_account)

    existing = (rest.get_instance(state['zone'], state['name'])
                if state.get('name') else None)
    if existing:
        state['gcs_bucket'] = bucket
        state['service_account'] = service_account
        attached = (existing.get('serviceAccounts') or [{}])[0].get('email')
        if attached and attached != service_account:
            _log(f'WARNING: VM uses {attached}, not {service_account}; '
                 'run down/up to recreate it with the least-privilege account')
        ip = (existing.get('networkInterfaces', [{}])[0]
              .get('accessConfigs', [{}])[0].get('natIP'))
        if ip:
            state['ip'] = ip
        _save_state(state)
        _log(f"VM already exists: {state['name']} ({state.get('ip')}); resuming setup")
        _wait_ssh(state)
        _wait_startup(state, args.startup_timeout)
        _sync_repo(state)
        _remote_setup(state)
        _log(f"Ready. SSH: ssh -i {state['key']} {args.ssh_user}@{state['ip']}")
        return 0

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    key = STATE_DIR / 'id_ed25519'
    if not key.is_file():
        subprocess.run(['ssh-keygen', '-t', 'ed25519', '-N', '', '-f', str(key),
                        '-C', 'adversarial-batch'], check=True)
        key.chmod(0o600)
    pubkey = (STATE_DIR / 'id_ed25519.pub').read_text().strip()
    public_ip = _public_ip()
    name = args.name or f"adv-batch-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    tag = 'adv-batch'
    fw_name = f'adv-batch-ssh-{args.ssh_user}'

    fw = rest.get_firewall(fw_name)
    created_firewall = False
    if not fw:
        _log(f'Creating firewall {fw_name} for {public_ip}/32 → tcp:22')
        op = rest.call('POST',
                       f'https://compute.googleapis.com/compute/v1/projects/{args.project}/global/firewalls',
                       {
                           'name': fw_name,
                           'network': f'projects/{args.project}/global/networks/{args.network}',
                           'direction': 'INGRESS',
                           'priority': 1000,
                           'sourceRanges': [f'{public_ip}/32'],
                           'allowed': [{'IPProtocol': 'tcp', 'ports': ['22']}],
                           'targetTags': [tag],
                       })
        rest.wait_operation(op)
        created_firewall = True
    elif f'{public_ip}/32' not in fw.get('sourceRanges', []):
        _log(f'Updating firewall {fw_name} for {public_ip}/32')
        op = rest.call('PATCH', rest.firewall_url(fw_name),
                       {'sourceRanges': [f'{public_ip}/32']})
        rest.wait_operation(op)

    _log(f'Creating {args.machine_type} VM {name} in {args.zone} '
         f'({args.disk_size_gb} GB, network {args.network})')
    _save_state({
        'project': args.project, 'zone': args.zone, 'region': region,
        'name': name, 'firewall': fw_name, 'created_firewall': created_firewall,
        'network': args.network, 'subnet': args.subnet,
        'created_at': _now(), 'phase': 'creating',
    })
    op = rest.call('POST',
                   f'https://compute.googleapis.com/compute/v1/projects/{args.project}/zones/{args.zone}/instances',
                   {
                       'name': name,
                       'machineType': f'projects/{args.project}/zones/{args.zone}/machineTypes/{args.machine_type}',
                       'disks': [{
                           'boot': True,
                           'autoDelete': True,
                           'initializeParams': {
                               'sourceImage': args.image,
                               'diskSizeGb': str(args.disk_size_gb),
                               'diskType': f'projects/{args.project}/zones/{args.zone}/diskTypes/{args.disk_type}',
                           },
                       }],
                       'networkInterfaces': [{
                           'network': f'projects/{args.project}/global/networks/{args.network}',
                           'subnetwork': f'projects/{args.project}/regions/{region}/subnetworks/{args.subnet}',
                           'accessConfigs': [{
                               'name': 'External NAT',
                               'type': 'ONE_TO_ONE_NAT',
                           }],
                       }],
                       'tags': {'items': [tag]},
                       'labels': {'purpose': 'adversarial-eval'},
                       'serviceAccounts': [{
                           'email': service_account,
                           'scopes': ['https://www.googleapis.com/auth/cloud-platform'],
                       }],
                       'metadata': {'items': [
                           {'key': 'ssh-keys', 'value': f'{args.ssh_user}:{pubkey}'},
                           {'key': 'enable-oslogin', 'value': 'FALSE'},
                           {'key': 'startup-script', 'value': _startup_script(args.ssh_user)},
                       ]},
                       'scheduling': {
                           'automaticRestart': True,
                           'onHostMaintenance': 'MIGRATE',
                           'preemptible': False,
                       },
                   })
    rest.wait_operation(op)
    try:
        _grant_instance_admin(rest, args.zone, name, service_account)
    except Exception:
        rest.delete_instance(args.zone, name)
        raise

    instance = rest.get_instance(args.zone, name)
    if not instance:
        raise RuntimeError(f'VM {name} was not found after creation')
    ip = (instance.get('networkInterfaces', [{}])[0]
          .get('accessConfigs', [{}])[0].get('natIP'))
    if not ip:
        raise RuntimeError(f'VM {name} has no external IP')

    state = {
        'project': args.project,
        'zone': args.zone,
        'region': region,
        'name': name,
        'ip': ip,
        'ssh_user': args.ssh_user,
        'key': str(key),
        'firewall': fw_name,
        'created_firewall': created_firewall,
        'network': args.network,
        'subnet': args.subnet,
        'remote_dir': args.remote_dir,
        'gcs_bucket': bucket,
        'service_account': service_account,
        'created_at': _now(),
    }
    _save_state(state)
    _log(f'VM running at {ip}; waiting for SSH…')
    _wait_ssh(state)

    _log('Waiting for startup (Docker + Harbor)…')
    _wait_startup(state, args.startup_timeout)

    _log('Syncing repository and .env…')
    _sync_repo(state)
    _log('Installing the local package in a venv…')
    _remote_setup(state)
    _log(f'Ready. SSH: ssh -i {key} {args.ssh_user}@{ip}')
    return 0


def _batch_wrapper_script(*, remote: str, jobs_dir: str, batch_cmd: str,
                          auto_delete: bool, bucket: str, prefix: str,
                          project: str, zone: str, instance: str) -> str:
    q = shlex.quote
    finish_call = ''
    if auto_delete:
        finish_call = (
            f'  bash "$HOME/{remote}/scripts/gcp_finish.sh" '
            f'{q(bucket)} {q(prefix)} {q(project)} {q(zone)} {q(instance)} '
            f'"$HOME/{remote}/{jobs_dir}" "$rc"\n'
        )
    return f'''#!/usr/bin/env bash
set -o pipefail
cd "$HOME/{remote}" || exit 1
export PATH="$HOME/.local/bin:$PATH"
rc=1
finished=0
finish() {{
  if [ "$finished" -eq 1 ]; then return; fi
  finished=1
  echo "$rc" > "$HOME/{remote}/{jobs_dir}/EXIT_CODE"
{finish_call}}}
trap finish EXIT
trap 'rc=130; exit 130' INT
trap 'rc=143; exit 143' TERM
trap 'rc=129; exit 129' HUP
{batch_cmd} 2>&1 | tee "$HOME/{remote}/{jobs_dir}/batch.log"
rc=${{PIPESTATUS[0]}}
'''


def cmd_run(args: argparse.Namespace) -> int:
    state = _state()
    if not state.get('ip'):
        raise SystemExit('No VM state; run `gcp_batch.py up` first')
    jobs_dir = args.jobs_dir
    output = args.output or f'{jobs_dir}/SUMMARY.json'
    remote = state['remote_dir']
    _log('Syncing repository and task list…')
    _sync_repo(state)
    auto_delete = not args.no_auto_delete
    bucket = state.get('gcs_bucket') or f'adv-eval-results-{state["project"]}'
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    prefix = f"runs/{state['name']}/{run_id}"
    if auto_delete:
        rest = Rest(state['project'])
        service_account = state.get('service_account', '')
        if not service_account:
            service_account = rest.project_info().get('defaultServiceAccount', '')
        _ensure_bucket(rest, bucket, state.get('region', 'us-central1'), service_account)
        state['service_account'] = service_account
    state.update({
        'gcs_bucket': bucket,
        'gcs_prefix': prefix,
        'auto_delete': auto_delete,
        'run_id': run_id,
    })
    _save_state(state)

    q = shlex.quote
    batch_cmd = (
        f'.venv/bin/python -m adversarial batch --tasks {q(args.tasks)} --workers {args.workers} '
        f'--policy-version {q(args.policy_version)} '
        f'--agent-harness {q(args.agent_harness)} '
        f'--review-harness {q(args.review_harness)} '
        f'--jobs-dir {q(jobs_dir)} --output {q(output)} '
        f'{"" if args.resume else "--no-resume "}'
    ).rstrip()
    script = _batch_wrapper_script(
        remote=remote, jobs_dir=jobs_dir, batch_cmd=batch_cmd,
        auto_delete=auto_delete, bucket=bucket, prefix=prefix,
        project=state['project'], zone=state['zone'], instance=state['name'],
    )
    script_path = f'{q(remote)}/run_gcp_batch.sh'
    _ssh(state, f"cat > {script_path} <<'ADVEOF'\n{script}\nADVEOF\nchmod +x {script_path}")
    session = q(args.session)
    _ssh(state,
         f'mkdir -p {q(remote)}/{q(jobs_dir)} && '
         f'tmux kill-session -t {session} 2>/dev/null || true; '
         f"tmux new-session -d -s {session} 'bash {script_path}'")
    _log(f'Started tmux session {args.session!r} on {state["ip"]}')
    if auto_delete:
        _log(f'Auto-delete ON: results → gs://{bucket}/{prefix}/results.tar.gz, then VM delete')
    else:
        _log('Auto-delete OFF: remember to fetch results and run down')
    _log(f'Status: python {Path(__file__).name} status')
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    state = _state()
    if not state.get('project'):
        raise SystemExit('No VM state; run `gcp_batch.py up` first')
    rest = Rest(state['project'])
    instance = rest.get_instance(state['zone'], state['name']) if state.get('name') else None
    if not instance:
        _log('VM: deleted')
        if state.get('gcs_bucket') and state.get('gcs_prefix'):
            _log(f"Results: gs://{state['gcs_bucket']}/{state['gcs_prefix']}/results.tar.gz")
            _log(f'Fetch: python {Path(__file__).name} fetch')
        return 0
    status = instance.get('status')
    if status != 'RUNNING':
        if status in ('STOPPING', 'STOPPED', 'TERMINATED') and state.get('auto_delete'):
            _log(f'VM: {status} (auto-delete/upload finishing)')
            if state.get('gcs_bucket') and state.get('gcs_prefix'):
                _log(f"Results: gs://{state['gcs_bucket']}/{state['gcs_prefix']}/results.tar.gz")
                _log(f'Fetch: python {Path(__file__).name} fetch')
        else:
            _log(f'VM: {status}')
            _log(f'Start: python {Path(__file__).name} start')
        return 0
    ip = (instance.get('networkInterfaces', [{}])[0]
          .get('accessConfigs', [{}])[0].get('natIP'))
    if ip and ip != state.get('ip'):
        state['ip'] = ip
        _save_state(state)
    session = shlex.quote(args.session)
    jobs_dir = shlex.quote(args.jobs_dir)
    remote = shlex.quote(state['remote_dir'])
    cmd = (
        f'cd {remote} && '
        f'tmux has-session -t {session} 2>/dev/null && echo "SESSION: running" || echo "SESSION: stopped"; '
        f'tmux capture-pane -pt {session} -S -80 2>/dev/null | tail -40; '
        f'echo "--- RESULTS ---"; tail -5 {jobs_dir}/RESULTS.jsonl 2>/dev/null || echo "(none yet)"; '
        f'echo "--- DOCKER ---"; docker ps --format "{{{{.Image}}}} {{{{.Status}}}}" 2>/dev/null | head'
    )
    result = _ssh(state, cmd, check=False, capture=True, timeout=60)
    print(result.stdout or '')
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return 0 if result.returncode == 0 else 1


def cmd_fetch(args: argparse.Namespace) -> int:
    state = _state()
    local = REPO / args.jobs_dir
    if state.get('gcs_bucket') and state.get('gcs_prefix'):
        try:
            name = _download_gcs_tarball(state['gcs_bucket'], state['gcs_prefix'], local)
            _log(f'Fetched gs://{state["gcs_bucket"]}/{name} to {local}')
            return 0
        except FileNotFoundError:
            _log('No GCS tarball yet; trying the running VM…')
    if not state.get('ip'):
        raise SystemExit('No VM state; run `gcp_batch.py up` first')
    local.mkdir(parents=True, exist_ok=True)
    cmd = ['rsync', '-az', '--progress',
           '-e', ' '.join(shlex.quote(x) for x in ['ssh', *_ssh_opts(Path(state['key']))]),
           f"{state['ssh_user']}@{state['ip']}:{state['remote_dir']}/{args.jobs_dir}/",
           f'{local}/']
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        _log('rsync failed; the VM may have auto-deleted. Check `status`, or rerun fetch later.')
        return result.returncode
    _log(f'Fetched to {local}')
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    state = _state()
    if not state.get('name'):
        raise SystemExit('No VM in state')
    rest = Rest(state['project'])
    instance = rest.get_instance(state['zone'], state['name'])
    if not instance:
        raise SystemExit('VM was deleted; fetch results from GCS instead')
    if instance.get('status') != 'RUNNING':
        _log(f"Starting {state['name']}…")
        rest.start_instance(state['zone'], state['name'])
        instance = rest.get_instance(state['zone'], state['name']) or instance
    ip = (instance.get('networkInterfaces', [{}])[0]
          .get('accessConfigs', [{}])[0].get('natIP'))
    if ip:
        state['ip'] = ip
        _save_state(state)
    _wait_ssh(state)
    _log(f"VM running at {state['ip']}")
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    state = _state()
    if not state.get('project'):
        _log('No state file; nothing to delete')
        return 0
    if not state.get('name'):
        _log('No VM name in state; nothing to delete')
        return 0
    rest = Rest(state['project'])
    _log(f"Deleting VM {state['name']}…")
    rest.delete_instance(state['zone'], state['name'])
    if state.get('firewall', '').startswith('adv-batch-ssh-'):
        _log(f"Deleting firewall {state['firewall']}…")
        rest.delete_firewall(state['firewall'])
    state['deleted_at'] = _now()
    _save_state(state)
    _log('Deleted. The boot disk was auto-deleted.')
    return 0


def cmd_ssh(args: argparse.Namespace) -> int:
    state = _state()
    if not state.get('ip'):
        raise SystemExit('No VM state; run `gcp_batch.py up` first')
    subprocess.run(['ssh', *_ssh_opts(Path(state['key'])),
                    f"{state['ssh_user']}@{state['ip']}"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', default=os.environ.get('GOOGLE_CLOUD_PROJECT', ''))
    sub = parser.add_subparsers(dest='command', required=True)

    up = sub.add_parser('up', help='create a VM and install Docker/Harbor')
    up.add_argument('--name')
    up.add_argument('--zone', default='us-central1-a')
    up.add_argument('--machine-type', default='c4d-highcpu-8')
    up.add_argument('--disk-size-gb', type=int, default=100)
    up.add_argument('--disk-type', default='hyperdisk-balanced')
    up.add_argument('--image', default=DEFAULT_IMAGE)
    up.add_argument('--network', default='cs5416')
    up.add_argument('--subnet', default='cs5416')
    up.add_argument('--ssh-user', default='ubuntu')
    up.add_argument('--remote-dir', default='adversarial_eval')
    up.add_argument('--bucket', help='GCS results bucket (default: adv-eval-results-<project>)')
    up.add_argument('--startup-timeout', type=int, default=900)
    up.set_defaults(func=cmd_up)

    run = sub.add_parser('run', help='start a batch in tmux')
    run.add_argument('--tasks', default='tasks.txt')
    run.add_argument('--workers', type=int, default=2)
    run.add_argument('--policy-version', default='v5')
    run.add_argument('--agent-harness', default='codex')
    run.add_argument('--review-harness', default='codex')
    run.add_argument('--jobs-dir', default='work/runs/v5-batch')
    run.add_argument('--output')
    run.add_argument('--session', default='advbatch')
    run.add_argument('--resume', dest='resume', action='store_true', default=True)
    run.add_argument('--no-resume', dest='resume', action='store_false')
    run.add_argument('--no-auto-delete', action='store_true',
                     help='keep the VM running after the batch (default: upload results and delete)')
    run.set_defaults(func=cmd_run)

    status = sub.add_parser('status', help='show tmux output and latest results')
    status.add_argument('--jobs-dir', default='work/runs/v5-batch')
    status.add_argument('--session', default='advbatch')
    status.set_defaults(func=cmd_status)

    fetch = sub.add_parser('fetch', help='download GCS results, or copy from a running VM')
    fetch.add_argument('--jobs-dir', default='work/runs/v5-batch')
    fetch.set_defaults(func=cmd_fetch)

    start = sub.add_parser('start', help='start a stopped VM (upload-failure recovery)')
    start.set_defaults(func=cmd_start)

    down = sub.add_parser('down', help='delete the VM and firewall rule')
    down.set_defaults(func=cmd_down)

    ssh = sub.add_parser('ssh', help='open an SSH shell on the VM')
    ssh.set_defaults(func=cmd_ssh)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
