# Cloud operations

The manager hosts the single PostgreSQL 17 task and its attached Volume. A host
timer can back up that database while the API and workers continue serving
requests. A separate Linux Docker host can rehearse recovery. The DigitalOcean
metrics agent runs on each Droplet. These are operator installation procedures.
The repository checks exercise the scripts with mocks. [Swarm deployment guide](CLOUD_DEPLOYMENT.md)

## Offline review

Run from the repository root with Python 3.10 or later. Both commands default
to a plan. The tests require only the Python standard library.

```bash
python deploy/scripts/backup.py --plan
python deploy/scripts/restore_test.py --plan
python -m unittest discover -s deploy/tests -p test_cloud_operations.py -v
```

## Daily backup contract

At **03:00 UTC**, the oneshot service takes the manager's deployment lock and
locates the healthy local database task by its stack and service labels. The
task's PostgreSQL 17 `pg_dump` connects over loopback using its configured
database, user and mounted `/run/secrets/db_password`. It writes a consistent
custom-format archive. The dump covers one database, including its data and
schema. Cluster roles, Swarm secrets and point-in-time WAL recovery require
separate recovery procedures. [PostgreSQL 17 logical dumps](https://www.postgresql.org/docs/17/app-pgdump.html)

The service stages one private temporary file outside PGDATA, decodes the full
archive with `pg_restore --file=/dev/null`, computes SHA-256, and sends one
HTTPS PUT with an explicit private object ACL. The filename records the UTC
date the job began and the hash of the exact archive bytes.

```text
agenthub-backups/<stack>/postgres17/daily/YYYY-MM-DD-<64 lowercase SHA-256 hex digits>.dump
```

Only this manager may write to that prefix. Use the same deployment lock path for
every invocation. The job reads every listing page and considers only exact
archive names in that stack's daily directory. A same-day retry reuses the
existing object. Ambiguous dates and future dates stop the job. Retention keeps
the newest **seven successful UTC dates**, even across gaps. Before deleting any
older key, the job downloads and verifies all seven survivors against their
filenames. With seven or fewer copies it verifies today's object. Size the
network budget for up to seven streamed reads per pruning run.

## Prepare the manager for installation

1. Select an existing dedicated Standard Spaces bucket. Confirm **Settings →
   File Listing → Restrict File Listing**, private objects, CDN disabled, and
   no public bucket policy. Use an unversioned bucket and ensure no lifecycle
   expiration or other writer manages the daily prefix. Versioning can retain
   noncurrent bytes after deletion. [Listing permissions](https://docs.digitalocean.com/products/spaces/how-to/set-file-listing-permissions/) · [Spaces ACLs and versioning](https://docs.digitalocean.com/products/spaces/reference/s3-compatibility/)
2. Use a Spaces **limited-access** key with **Read/Write/Delete** for that bucket
   only. The manager needs list/read for verification and delete for retention.
   Give the drill operator a separate **Read** key. [Spaces access scopes](https://docs.digitalocean.com/products/spaces/how-to/manage-access/)
3. Confirm the manager has a working local Docker socket, Python 3.10+ with
   `venv`, systemd, and outbound HTTPS to the bucket's regional endpoint. Allow
   local free space for a whole compressed dump at `/var/lib/agenthub-backup`.
   This implementation accepts archives up to 4 GiB. Monitor staging space
   before approaching that size. A rotated database password must match both
   the PostgreSQL role and the mounted Swarm secret.

Run these installation commands on the manager from a reviewed checkout during
the rollout. They stage files on the host independently of Swarm deployment.

```bash
sudo install -d -m 0755 /opt/agenthub-backup
sudo install -d -m 0700 /etc/agenthub-backup
sudo install -m 0644 deploy/scripts/{backup,backup_common}.py /opt/agenthub-backup/
sudo python3 -m venv /opt/agenthub-backup/venv
sudo /opt/agenthub-backup/venv/bin/pip install 'boto3>=1.36,<2'
sudo install -m 0644 deploy/systemd/agenthub-backup.{service,timer} /etc/systemd/system/
```

Provision two root-owned mode `0600` files in `/etc/agenthub-backup` outside
the checkout using your team's secret manager or private editor.

| File | Contents |
| --- | --- |
| `config` | One systemd environment assignment per line for `STACK` (deployed stack name), `SPACES_REGION` (actual bucket region, e.g. `tor1`), and `SPACES_BUCKET` (exact bucket name). Set `DEPLOY_LOCK_PATH` if deployment overrides `/var/lock/agenthub-deploy.lock`. |
| `credentials` | AWS shared-credentials INI file with section `[agenthub-backup]` and fields `aws_access_key_id` and `aws_secret_access_key` populated privately with the limited Spaces key. |

The unit points the SDK at this file. The application stack needs no Spaces
credentials. Keep dumps, credentials, SQL output and rendered configuration out
of logs, tickets and Git. [DigitalOcean S3 SDK configuration](https://docs.digitalocean.com/products/spaces/reference/aws-sdks/)

## Enable and observe the daily job

During the approved rollout, verify the units and enable the timer on the
manager only. A persistent timer can trigger a catch-up run after downtime.

```bash
sudo systemd-analyze verify /etc/systemd/system/agenthub-backup.{service,timer}
sudo systemctl daemon-reload
sudo systemctl enable --now agenthub-backup.timer
systemctl list-timers --all agenthub-backup.timer
sudo journalctl -u agenthub-backup.service --since yesterday
systemctl show agenthub-backup.service -p Result -p ExecMainStatus
```

A successful journal entry records the verified key and retained count. Check
the latest successful date as well as the timer's next firing. A busy manager
lock returns status 75. Systemd retries after 30 minutes, with at most four
starts in four hours. Other failures require investigation. On a missed day
or nonzero service exit, check clock, lock contention, healthy database task,
staging space, SDK profile and bucket permissions. After fixing the cause, use
`sudo systemctl start agenthub-backup.service` to retry. Failed listing, dump,
upload or verification prevents pruning. A failed deletion can leave extra
copies. The next successful run recomputes retention. Investigate checksum
failures and preserve the affected archives. To suspend future runs use
`sudo systemctl disable --now agenthub-backup.timer`.

## Test one restore in isolation

Use a separate Linux Docker host with free disk for the compressed download and
expanded database. Cache the approved official PostgreSQL 17 image at the same
digest as the deployment. Set `POSTGRES_IMAGE` to that reference. Use the AWS
CLI with the read-only `agenthub-drill` profile and set `SPACES_REGION`,
`SPACES_BUCKET`, and `KEY` to the selected archive's region, bucket and full
key from the successful backup journal. Handle the download as private
application data.

```bash
umask 077
drill_dir=$(mktemp -d)
archive="$drill_dir/${KEY##*/}"
aws --profile agenthub-drill --region us-east-1 \
  --endpoint-url "https://${SPACES_REGION}.digitaloceanspaces.com" \
  s3api get-object --bucket "$SPACES_BUCKET" --key "$KEY" "$archive" &&
python3 deploy/scripts/restore_test.py --run --archive "$archive" --image "$POSTGRES_IMAGE"
```

The drill checks the SHA-256 in the original filename, then creates a uniquely
named container with networking disabled, no published ports, and a fresh
anonymous data volume. It streams the archive on stdin. All SQL goes to the
new container's Unix socket and fixed `agenthub_restore_test` database. A
single transaction restores the data and schema, skipping original ownership
and ACLs. The drill checks server major 17, a populated Alembic revision table,
and the messages and jobs tables. [PostgreSQL 17 restore options](https://www.postgresql.org/docs/17/app-pgrestore.html)

Record the date, key, image digest and exit status as drill evidence. Exit zero
also requires removal of the test container and anonymous volume. Delete the
private downloaded file when finished. After a host interruption or cleanup
error, inspect the exact generated name printed by the drill and confirm its
`agenthub.restore-test=true` label before removing that container with
`docker rm -fv <drill-container-id>`.

## Monitor both Droplets

On **each** existing Droplet (Swarm manager and worker), check
`systemctl is-active do-agent`. If installation is needed, download the official
installer into a private directory, review it, and run it on that host.

```bash
agent_dir=$(mktemp -d)
curl -fsSL https://repos.insights.digitalocean.com/install.sh -o "$agent_dir/install.sh"
less "$agent_dir/install.sh"
sudo bash "$agent_dir/install.sh"
systemctl is-enabled do-agent
systemctl is-active do-agent
```

Allow a few minutes for samples. Open **Droplets → each Droplet → Insights**
and confirm current CPU, memory and disk graphs on both. Check
`journalctl -u do-agent` if either host is missing. [Metrics agent installation and verification](https://docs.digitalocean.com/products/monitoring/how-to/install-metrics-agent/)

Under **Monitoring → Create Resource Alert**, create three policies. For each
policy select **both AgentHub Droplets**, choose **Above**, select the duration,
add a verified team email or connected Slack destination, name the alert and
save it. These are proposed starting thresholds for this deployment.

| Policy | Metric | Threshold | Duration |
| --- | --- | --- | --- |
| AgentHub CPU | CPU Utilization Percent | 80% | 10 min |
| AgentHub memory | Memory Utilization Percent | 85% | 10 min |
| AgentHub root disk | Disk Utilization Percent | 80% | 5 min |

Reopen each saved policy and verify both Droplet targets and the delivery
destination. To test delivery during an approved window, temporarily set a
threshold below the observed steady usage for one duration, confirm receipt,
then restore the recorded settings and confirm recovery. Record the timestamp
and Droplet for each notification. DigitalOcean's disk percentage covers the
root disk. Monitor the separate database Volume on the manager. [DigitalOcean resource alert metrics and notifications](https://docs.digitalocean.com/products/monitoring/how-to/manage-resource-alerts/)

### PostgreSQL Volume and service alerts

Use the trusted manager settings and reviewed scripts installed for delivery.
`check_volume.sh` checks the exact mountpoint and filesystem UUID, writable
mount options and allocated space. It exits nonzero at 80% usage by default.
`monitor.py` checks API/database connectivity, replica counts, container health
on each node and dormant setup. Schedule both independently with cron on the
manager. Configure and test a local mail relay, then install this root-owned
file at `/etc/cron.d/agenthub-health`, replacing the recipient and account
with your configured values. Cron mails command output.

```cron
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
MAILTO=operations@example.org
*/5 * * * * delivery (set -ae; source /etc/agenthub-delivery/config; timeout 30s bash /opt/agenthub/deploy/scripts/check_volume.sh) || echo "ALERT: check PostgreSQL Volume mount, UUID, space and probe timeout"
*/5 * * * * delivery (set -ae; source /etc/agenthub-delivery/config; timeout 120s python3 /opt/agenthub/deploy/scripts/monitor.py --url "http://127.0.0.1:${API_PUBLISHED_PORT:-18000}" >/dev/null) || echo "ALERT: AgentHub service health check failed or timed out"
```

To verify delivery safely, run the Volume probe with an intentionally incorrect
`POSTGRES_VOLUME_UUID` in a single scheduled test invocation. Confirm receipt,
remove that test entry, and confirm the regular check exits zero. Inspect
`df -h` on the configured mount when alerted. Cron retries every five minutes
and relies on the manager and its mail relay. DigitalOcean host alerts provide
an independent notification path. Use the authenticated smoke check to verify
worker processing. [Deployment checks](CLOUD_DEPLOYMENT.md#verification-and-saved-history)
