# Backup and restore

`scripts/backup.sh` saves everything needed to rebuild an install into one
archive. `scripts/restore.sh` brings it back, on the same machine or on a
new one. A backup you have never restored is only a hope: try a restore
on a spare machine or VM before you need it.

## What is saved

| Saved | Where it comes from | Why |
| --- | --- | --- |
| Open WebUI data: accounts, chats, settings, uploaded documents, search index | `WEBUI_DATA_DIR` | Everything your users created |
| Caddy data: certificates and the local CA | `CADDY_DATA_DIR` | Clients that trust the CA keep working after a restore |
| `.env` | repository | Domain, secrets, versions, data folders |
| List of installed models | `ollama list` | Models are downloaded again, not saved |
| Local edits to tracked files, as a patch | `git diff` | For reference; restore doesn't apply them |

Not saved:

- **Models.** They are large and can be downloaded again;
  `scripts/pull-models.sh` also rebuilds `brainharbor-hu`. Ollama always
  downloads the current version of a tag, which may differ from the one
  you had.
- **Caches** that Open WebUI fills again on its own (`cache/tiktoken`,
  `cache/whisper`, `cache/embedding`).

**The archive holds secrets** (`.env`, the CA's private key) and every chat.
`backup.sh` makes it readable by root only; keep any copy of it just as
private, and encrypt copies that leave the machine (see
[Offsite copies](#offsite-copies)).

## Making a backup

```sh
sudo scripts/backup.sh
```

It needs root because the containers write their data as root, and it
needs `rsync`. Settings in `.env`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `BACKUP_DIR` | `./backups` | Where backups go. Best on another disk. |
| `BACKUP_KEEP` | `7` | Number of archives to keep; older ones are removed. `0` keeps only `latest/`. |

Each run updates `BACKUP_DIR/latest/`, a plain copy of the newest backup,
and packs it into `BACKUP_DIR/brainharbor-YYYYMMDD-HHMMSS.tar.gz`. Only what
changed since the last run is copied into `latest/`.

**Consistency.** Open WebUI keeps its data in SQLite databases, and a
database copied while it is being written can be broken. So:

- **On ZFS**, `backup.sh` takes a snapshot of the data datasets and copies
  from it. Nothing stops. A snapshot catches the databases the way a power
  cut would, and SQLite is built to recover from that.
- **Elsewhere**, Open WebUI is stopped while its data is copied, usually for
  a second or two (users see an error page meanwhile). Caddy keeps running.

Then the databases in the copy are checked with SQLite's integrity check.
If one is damaged, the script stops with an error and makes no archive.

## Every night

A systemd timer runs the backup at 02:30, and catches up after the machine
was off. Replace `/srv/brainharbor` with the folder that has `compose.yaml`.

`/etc/systemd/system/brainharbor-backup.service`:

```ini
[Unit]
Description=brAInharbor backup
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
ExecStart=/srv/brainharbor/scripts/backup.sh
```

`/etc/systemd/system/brainharbor-backup.timer`:

```ini
[Unit]
Description=Nightly brAInharbor backup

[Timer]
OnCalendar=*-*-* 02:30
Persistent=true

[Install]
WantedBy=timers.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now brainharbor-backup.timer
systemctl list-timers brainharbor-backup.timer   # next run
journalctl -u brainharbor-backup                 # output of past runs
```

Check the log now and then: a backup that fails silently every night
looks just like one that works.

## Offsite copies

A backup on the same machine doesn't survive a fire, a theft or
ransomware. Keep a copy somewhere else, encrypted.

[restic](https://restic.net) encrypts, and only uploads what changed.
Back up `BACKUP_DIR/latest/` rather than the archives: restic splits
files into chunks and only stores new ones, but a `.tar.gz` changes
throughout whenever anything in it changes. restic can store to a USB
disk, a NAS over SFTP, or S3-compatible storage.

```sh
sudo apt install restic                    # or your distribution's package
sudo install -m 600 /dev/null /root/.restic-password
sudoedit /root/.restic-password            # a long random password
export RESTIC_REPOSITORY=sftp:backup@nas.example:/srv/restic/brainharbor
export RESTIC_PASSWORD_FILE=/root/.restic-password
sudo -E restic init
```

Keep the password somewhere else too, such as a password manager: without
it the copies can't be read.

To copy after every backup, add these lines to the `[Service]` section of
`brainharbor-backup.service` (with your own repository and folder):

```ini
Environment=RESTIC_REPOSITORY=sftp:backup@nas.example:/srv/restic/brainharbor
Environment=RESTIC_PASSWORD_FILE=/root/.restic-password
ExecStartPost=/usr/bin/restic backup --tag brainharbor /srv/brainharbor/backups/latest
ExecStartPost=/usr/bin/restic forget --tag brainharbor --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --prune
```

To restore from restic, get `latest/` back first and pass that folder to
`restore.sh`:

```sh
sudo -E restic restore latest --tag brainharbor --target /tmp/brainharbor-restore
sudo scripts/restore.sh /tmp/brainharbor-restore/srv/brainharbor/backups/latest
```

## ZFS snapshots

With the dataset layout in [zfs.md](zfs.md), snapshots give you quick
local undo on top of the nightly archives, for example after a bad
upgrade. [sanoid](https://github.com/jimsalterjrs/sanoid) takes and prunes
them on a schedule. In `/etc/sanoid/sanoid.conf` (with your pool name):

```ini
[tank/brainharbor/data/webui]
	use_template = brainharbor_hourly

[tank/brainharbor/data/caddy]
	use_template = brainharbor_daily

[tank/brainharbor]
	use_template = brainharbor_daily

[template_brainharbor_hourly]
	frequently = 0
	hourly = 24
	daily = 14
	weekly = 0
	monthly = 3
	yearly = 0
	autosnap = yes
	autoprune = yes

[template_brainharbor_daily]
	frequently = 0
	hourly = 0
	daily = 14
	weekly = 0
	monthly = 3
	yearly = 0
	autosnap = yes
	autoprune = yes
```

`tank/brainharbor/data/models` is left out on purpose: models can be
downloaded again.

To go back to a snapshot, stop the service that uses the dataset first.
`zfs rollback -r` also destroys every snapshot newer than the one you go
back to:

```sh
zfs list -t snapshot tank/brainharbor/data/webui
docker compose stop open-webui
sudo zfs rollback -r tank/brainharbor/data/webui@autosnap_2026-10-27_02:00:01_hourly
docker compose start open-webui
```

If you have a second machine with ZFS, `syncoid` (part of sanoid) can copy
the snapshots there, e.g.
`sudo syncoid --no-sync-snap tank/brainharbor/data/webui root@backuphost:backup/brainharbor-webui`.
Snapshots are no replacement for the archives: they live in the same pool,
and `restore.sh` works from archives.

## Restoring

`restore.sh` takes an archive or a `latest/` folder:

```sh
sudo scripts/restore.sh backups/brainharbor-20261027-023000.tar.gz
```

It shows the backup's details and what it will do, and asks before it
starts. Then it:

1. stops brAInharbor if it is running,
2. installs the `.env` from the backup,
3. puts back Open WebUI's and Caddy's data,
4. starts brAInharbor, then downloads and builds the models.

Nothing is deleted. Data that was in the way is renamed to
`<folder>.before-restore-<date>`, or on ZFS kept in a
`@before-restore-<date>` snapshot. Remove these once everything works.

Options:

- `--keep-env`: keep this machine's `.env` instead of the backup's (the
  backup's copy is saved as `.env.from-backup-<date>`). Use it when the new
  machine is different: other data folders, no GPU, other ports. Keep
  `WEBUI_SECRET_KEY` from the backup if you can: with a new one, everybody
  has to log in again.
- `--no-models`: skip the model downloads; run `scripts/pull-models.sh`
  later.
- `--yes`: don't ask.

### On a new machine

1. Install Docker as in the [README](../README.md), and `rsync` for later
   backups. On ZFS, create the datasets first ([zfs.md](zfs.md)).
2. Clone the repository and copy the archive over:

   ```sh
   git clone https://github.com/brainharbor/brainharbor.git /srv/brainharbor
   cd /srv/brainharbor
   scp oldhost:/srv/brainharbor/backups/brainharbor-20261027-023000.tar.gz /tmp/
   ```

3. Restore:

   ```sh
   sudo scripts/restore.sh /tmp/brainharbor-20261027-023000.tar.gz
   ```

4. Point the domain at the new machine (DNS, or `/etc/hosts` on clients),
   open it in a browser and log in with an existing account.

Browsers keep trusting the certificate, because Caddy's local CA came back
with the backup.
