# ZFS layout

brAInharbor runs on any Linux filesystem. On ZFS, give each kind of data
its own dataset: they need different settings, and only some of them are
worth snapshots and backups.

| Dataset | Mounted at | Holds | Settings | Snapshots |
| --- | --- | --- | --- | --- |
| `POOL/brainharbor` | `/srv/brainharbor` | the repository, `.env` (secrets), local overrides | `compression=lz4`, `atime=off` | daily |
| `POOL/brainharbor/data/models` | `data/models` | Ollama models | `recordsize=1M` | none: models can be downloaded again |
| `POOL/brainharbor/data/webui` | `data/webui` | users, chats, uploaded documents, search index | `recordsize=16K`, `compression=zstd` | hourly and daily |
| `POOL/brainharbor/data/caddy` | `data/caddy` | certificates, Caddy's local CA | inherited | daily |

Why these settings:

- **Models** are large files, written once and read in full when a model
  loads. 1M records mean less metadata and faster reads. A snapshot would
  only pin tens of gigabytes that `scripts/pull-models.sh` can download
  again.
- **Open WebUI data** is mostly SQLite databases that rewrite small pages.
  Small records keep each change small, both on disk and in snapshots.
  `zstd` compresses chats and documents better than `lz4`.
- **`data`** is only a parent: `canmount=off` keeps it from being mounted, so
  its children land in the repository's `data/` folder. The `.env` defaults
  (`MODELS_DIR=./data/models` and so on) need no change.

Replace `POOL` with your pool's name in the commands below (`zpool list`
shows it).

## New install

```sh
POOL=tank
sudo zfs create -o mountpoint=/srv/brainharbor -o compression=lz4 -o atime=off "$POOL/brainharbor"
sudo chown "$USER": /srv/brainharbor
git clone https://github.com/brainharbor/brainharbor.git /srv/brainharbor

sudo zfs create -o canmount=off "$POOL/brainharbor/data"
sudo zfs create -o recordsize=1M "$POOL/brainharbor/data/models"
sudo zfs create -o recordsize=16K -o compression=zstd "$POOL/brainharbor/data/webui"
sudo zfs create "$POOL/brainharbor/data/caddy"

zfs list -r -o name,mountpoint,recordsize,compression,atime "$POOL/brainharbor"
```

Then continue with the quick start in the [README](../README.md).

## Moving an existing install

For an install that keeps its data in `data/` on an ordinary folder. The
repository stays where it is; only the three data folders move to
datasets with the same names as above. `POOL/brainharbor` is created with
`canmount=off`, so it never covers the repository folder.

Copying needs as much free space as `data/` uses now
(`sudo du -sh data/*`). Remove models you no longer need first
(`docker compose exec ollama ollama rm NAME`).

```sh
cd /path/to/brainharbor          # the folder with compose.yaml
POOL=tank
docker compose down

for d in models webui caddy; do sudo mv "data/$d" "data/$d.old"; done

sudo zfs create -o canmount=off -o mountpoint="$PWD" -o compression=lz4 -o atime=off "$POOL/brainharbor"
sudo zfs create -o canmount=off "$POOL/brainharbor/data"
sudo zfs create -o recordsize=1M "$POOL/brainharbor/data/models"
sudo zfs create -o recordsize=16K -o compression=zstd "$POOL/brainharbor/data/webui"
sudo zfs create "$POOL/brainharbor/data/caddy"

for d in models webui caddy; do sudo rsync -aH --info=progress2 "data/$d.old/" "data/$d/"; done

docker compose up -d
```

Check that you can log in, that old chats are there and that the models
answer. Only then delete the old copies:

```sh
for d in models webui caddy; do sudo rm -rf "data/$d.old"; done
```

Copying instead of moving keeps the old data until you've checked the new
setup. The copies are written with the new datasets' `recordsize` and
`compression`.

## Snapshots and backups

`scripts/backup.sh` notices these datasets and copies from a snapshot, so
Open WebUI keeps running during backups. Scheduled snapshots with sanoid
and copies to a second ZFS machine are in [backup.md](backup.md#zfs-snapshots).
Before an upgrade, a manual snapshot is a quick way back:

```sh
sudo zfs snapshot -r "$POOL/brainharbor@before-upgrade"
```

The models dataset is included in a recursive snapshot, but it only costs
space once models are deleted or replaced.
