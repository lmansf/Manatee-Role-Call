# Scheduling on Ubuntu

Two systemd **user** timers: the daily run at 19:00, and a weekly backup. Both are set to
`Persistent=true`, so a run missed while the machine was off or asleep fires as soon as it's
back. Both jobs take the same lock file, so they can't open the database at the same time.

The timers run from a runner clone of the repo at `~/roll-call-runner`. It stays on `main` and
is never used for editing. The daily run ends by committing the dashboard CSVs and pushing them
to `main`, and it refuses to do that on another branch or with other uncommitted changes. Keep
the clone you edit in somewhere else. If the runner clone isn't at `~/roll-call-runner`, change
the paths in the two `.service` files.

## 1. Create the runner clone

```sh
git clone https://github.com/lmansf/Manatee-Role-Call.git ~/roll-call-runner
cd ~/roll-call-runner
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env        # then fill it in
.venv/bin/python -m pytest  # should pass
```

The database file lives in the runner clone's `data/` folder. If you already have one in another
clone, move it here before the first run.

The backup needs `BACKUP_DIR` in `.env` pointing at a folder on the second disk. That disk must
be mounted at boot, through `/etc/fstab` or the Disks app's "Mount at system startup" option.
If it isn't mounted, the backup fails on purpose.

## 2. Give the runner clone push access

The push uses a fine-grained personal access token. It's a password-like string that GitHub
limits to the one repo and the one permission you pick. It acts as you, and it expires, so
you'll renew it when it does.

### Create the token on GitHub

1. Click your profile picture in the top right, then Settings.
2. At the bottom of the left sidebar, open Developer settings.
3. Open Personal access tokens, then Fine-grained tokens, then Generate new token.
4. Name it `roll-call-runner`.
5. Pick an expiration. Longer means fewer renewals. Put a reminder in your calendar a week
   before the date.
6. Under Repository access, choose "Only select repositories" and pick `Manatee-Role-Call`.
7. Under Permissions, open Repository permissions and set Contents to "Read and write". Leave
   everything else as it is. GitHub adds read-only Metadata by itself.
8. Click Generate token and copy it. GitHub shows it only once.

### Store it in the runner clone

Tell git to remember credentials for this clone only, then run a push that sends nothing:

```sh
cd ~/roll-call-runner
git config credential.helper store
git config credential.useHttpPath true
git push --dry-run origin main
```

Git asks for a username and a password. Enter your GitHub username, and paste the token as
the password. Git saves both to `~/.git-credentials`, a plain-text file only your user can
read. Check that with `ls -l ~/.git-credentials`: the permissions should read `-rw-------`.
The dry run should end with "Everything up-to-date". A 403 error means the token lacks
"Read and write" on Contents, or was limited to a different repo.

Run the same push again. It should not ask for anything this time, which is what the timer
needs.

### Give the runner clone a git identity

The publish step makes commits, and git refuses to commit without a name and email:

```sh
git config user.name "Roll Call runner"
git config user.email "you@example.com"
```

Use your own email, or the private noreply address GitHub shows under Settings, then Emails.
This sets the identity for the runner clone only.

### When the token expires

The daily run's push starts failing, systemd marks the run failed, and after two days the
dashboard shows its stale-data warning. To renew:

1. On the token's page on GitHub, click Regenerate token, or create a new one with the same
   settings. Copy it.
2. Open `~/.git-credentials` in an editor and delete the line containing `github.com`.
3. Run `git push --dry-run origin main` in the runner clone and paste the new token when asked.

## 3. Run each job by hand

Run each job once and confirm it succeeds before scheduling it. A job that fails by hand will
also fail on the timer, just more quietly.

```sh
cd ~/roll-call-runner
.venv/bin/python jobs/ingest_daily.py
.venv/bin/python jobs/backup_db.py
```

After the daily run, `git log -1` shows the data commit if the dashboard CSVs changed. Vercel
then starts a new build of the site.

## 4. Create the unit files

```sh
mkdir -p ~/.config/systemd/user
```

`~/.config/systemd/user/roll-call.service`:

```ini
[Unit]
Description=Roll Call daily run
# Give up after 4 failed attempts in 2 hours; the next day's run catches up anyway.
StartLimitIntervalSec=2h
StartLimitBurst=4

[Service]
Type=oneshot
WorkingDirectory=%h/roll-call-runner
ExecStart=/usr/bin/flock %h/roll-call-runner/data/.run.lock %h/roll-call-runner/.venv/bin/python jobs/ingest_daily.py
# Right after waking, the network is often not up yet. Retry instead of losing the day.
Restart=on-failure
RestartSec=5min
```

`~/.config/systemd/user/roll-call.timer`:

```ini
[Unit]
Description=Roll Call daily run at 19:00

[Timer]
OnCalendar=*-*-* 19:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

`~/.config/systemd/user/roll-call-backup.service`:

```ini
[Unit]
Description=Roll Call weekly backup

[Service]
Type=oneshot
WorkingDirectory=%h/roll-call-runner
ExecStart=/usr/bin/flock %h/roll-call-runner/data/.run.lock %h/roll-call-runner/.venv/bin/python jobs/backup_db.py
```

`~/.config/systemd/user/roll-call-backup.timer`:

```ini
[Unit]
Description=Roll Call weekly backup, Sundays at noon

[Timer]
OnCalendar=Sun *-*-* 12:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

`%h` is systemd's shorthand for your home folder. `Restart=on-failure` on a one-shot service
needs systemd 244 or later. Every supported Ubuntu release has that. Check yours with
`systemctl --version`.

## 5. Turn it on

```sh
systemctl --user daemon-reload
systemctl --user enable --now roll-call.timer roll-call-backup.timer
sudo loginctl enable-linger "$USER"   # run the timers even when you're not logged in
```

`OnCalendar` uses the machine's local time zone. Check it's `America/New_York`, or whatever you
intend, with `timedatectl`.

## 6. Check it

```sh
systemctl --user list-timers roll-call*          # next and last run times
systemctl --user start roll-call.service         # run now, as the timer would
journalctl --user -u roll-call.service -n 50     # the run's log output
systemctl --user status roll-call.service        # did the last run succeed?
systemd-analyze calendar "*-*-* 19:00:00"        # confirm when 19:00 next falls
```

A run missed while the machine was off shows up in `list-timers` as a last run time from
before the shutdown. It fires within a minute or so of the next boot or wake.

## Waking the machine to run on time

This is optional. A user timer can't wake a suspended machine: `WakeSystem=true` needs
privileges only the system-wide service manager has. If you want the machine woken at 19:00,
install the daily timer as a system unit instead. Put the same two files in
`/etc/systemd/system/`. In the service, add `User=<your username>` and replace `%h` with your
full home path. In the timer, add `WakeSystem=true`. Then run
`sudo systemctl enable --now roll-call.timer`. The machine still won't suspend itself again
afterwards. Without this, a suspended machine simply runs the job when it wakes, which the
catch-up rule already handles.

## Turning it off

```sh
systemctl --user disable --now roll-call.timer roll-call-backup.timer
```

## Troubleshooting

- **Publishing refuses to run.** The publish step only commits `dashboard/sources/roll_call/`
  and `data/records/`. Any other changed or untracked file in the runner clone stops it, on
  purpose. Run `git status` in `~/roll-call-runner` and remove or commit whatever is there.
- **The pull before publishing fails.** It uses `git pull --ff-only`, which stops when the
  runner clone and GitHub have diverged. That happens, for example, after a local commit whose
  push failed. Run `git log origin/main..main` to see the stranded commit, then
  `git pull --rebase` and push by hand.
