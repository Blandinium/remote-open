# remote-open

Open remote files from an SSH session in an editor on your local desktop.

`remote-open` follows the model of `EMACSCLIENT_TRAMP` for Emacs. It works with
any application that can open remote files. It sends paths through a forwarded Unix
socket. The workstation turns them into SFTP URLs. No remote
filesystem is mounted.

More background in [this blog post](https://tijl.blog/posts/remote-open/)

## Terms

- **Workstation:** the local machine with the desktop app.
- **Remote machine:** the machine with the files and SSH shell.

## Requirements

- Python 3 on both machines.
- The `file` command on remote machines.
- OpenSSH with Unix socket forwarding.
- An app that opens SFTP URLs.

Kate and Kompare work. GNOME Text Editor should work through GIO.

## Try it without a service

Run these steps from a checkout on the workstation.

Copy and edit a temporary config:

```sh
cp examples/config.json /tmp/remote-open-config.json
$EDITOR /tmp/remote-open-config.json
```

Set one target alias and its real SFTP URL. Start the bridge in a workstation
terminal. Replace `WORKSTATION_UID` with the output of `id -u`:

```sh
python3 remote_open.py bridge \
    --socket /tmp/remote-open-WORKSTATION_UID.sock \
    --config /tmp/remote-open-config.json
```

Leave it running. In a second workstation terminal, copy the script:

```sh
scp remote_open.py REMOTE_ALIAS:/tmp/remote-open.py
```

Open one SSH connection with a test forward:

```sh
ssh -S none \
    -o ExitOnForwardFailure=yes \
    -o StreamLocalBindMask=0177 \
    -o StreamLocalBindUnlink=yes \
    -R /home/REMOTE_USER/.remote-open-test.socket:/tmp/remote-open-WORKSTATION_UID.sock \
    REMOTE_USER@REMOTE_HOST
```

`-S none` keeps this test separate from an existing SSH master. Inside that
remote shell, run:

```sh
env REMOTE_OPEN_SOCKET="$HOME/.remote-open-test.socket" \
    REMOTE_OPEN_TARGET="TARGET_ALIAS" \
    python3 /tmp/remote-open.py edit /path/to/file.txt
```

Use an alias found in the temporary config. Exit the forwarded SSH session,
then remove the test files from the remote machine in a workstation terminal:

```sh
ssh -S none -o ClearAllForwardings=yes REMOTE_USER@REMOTE_HOST \
    'rm -f ~/.remote-open-test.socket /tmp/remote-open.py'
```

Stop the bridge with `Ctrl-C`, then remove
`/tmp/remote-open-config.json` on the workstation.

## Install

Install `remote_open.py` on the workstation:

```sh
install -Dm755 remote_open.py ~/.local/bin/remote-open
```

Install it on each remote machine from the workstation:

```sh
ssh REMOTE_ALIAS 'install -d -m755 "$HOME/.local/bin"'
scp remote_open.py REMOTE_ALIAS:.local/bin/remote-open
ssh REMOTE_ALIAS 'chmod 755 "$HOME/.local/bin/remote-open"'
```

On the workstation, install the service and config:

```sh
install -Dm644 contrib/remote-open.service ~/.config/systemd/user/remote-open.service
install -Dm644 examples/config.json ~/.config/remote-open/config.json
```

Edit `~/.config/remote-open/config.json`. Add each remote machine as a target.

For KDE with Kate and Kompare:

```json
{
  "targets": {
    "work": {
      "url_prefix": "sftp://alice@work.example.net"
    },
    "lab": {
      "url_prefix": "sftp://alice@lab.example.net"
    }
  },
  "commands": {
    "open": ["kioclient", "exec", "{url}", "{mime_type}"],
    "edit": ["kate"],
    "edit_wait": ["kate", "--block"],
    "diff": ["kompare", "-c"]
  }
}
```

The remote `file` command determines the MIME type. The bridge substitutes
`{url}` and `{mime_type}` in the configured open command. KDE's KIO integration
can download a temporary copy when the selected application does not support
remote URLs.

`edit_wait` must remain running until editing is finished. Kate provides this
behavior with `--block`.

Only the operations you want to use need command entries. If an operation is
not configured, the bridge returns an error to the remote client without
starting an application.

Start the bridge:

```sh
systemctl --user daemon-reload
systemctl --user enable --now remote-open.service
```

## SSH forwarding

Add one block per remote machine in the workstation's `~/.ssh/config`:

```sshconfig
Host REMOTE_ALIAS
    HostName REMOTE_HOST
    User REMOTE_USER
    ControlMaster auto
    ControlPath ~/.ssh/control-%C
    ControlPersist 10m
    RemoteForward /home/REMOTE_USER/.remote-open.socket /run/user/WORKSTATION_UID/remote-open.sock
    ExitOnForwardFailure yes
```

Because this is a remote forward, `sshd` creates the listening socket on the
remote machine.  Configure these options in `/etc/ssh/sshd_config` on every
remote machine:

```sshconfig
StreamLocalBindMask 0177
StreamLocalBindUnlink yes
```

Validate the configuration and reload `sshd`; the exact service name varies by
distribution:

```sh
sudo sshd -t
sudo systemctl reload sshd
sudo sshd -T | grep '^streamlocalbind'
```

The last command must report both `streamlocalbindmask 0177` and
`streamlocalbindunlink yes`.

Connect with the exact alias:

```sh
ssh REMOTE_ALIAS
```

## Shell setup

Set a target alias on each remote machine. It must match the workstation config.

Bash, in `~/.bashrc`:

```sh
export REMOTE_OPEN_TARGET="work"
```

Fish, as a universal exported variable:

```fish
set -Ux REMOTE_OPEN_TARGET "work"
```

Use a different alias on each machine.

The default remote socket is `~/.remote-open.socket`. Its variable is optional.

Bash, in `~/.bashrc`:

```sh
export REMOTE_OPEN_SOCKET="$HOME/.remote-open.socket"
```

Fish, as a universal exported variable:

```fish
set -Ux REMOTE_OPEN_SOCKET "$HOME/.remote-open.socket"
```

The workstation config path can also be set with `REMOTE_OPEN_CONFIG`.

Bash:

```sh
export REMOTE_OPEN_CONFIG="$HOME/.config/remote-open/config.json"
```

Fish:

```fish
set -Ux REMOTE_OPEN_CONFIG "$HOME/.config/remote-open/config.json"
```

The supplied service uses that path without this variable.

## Use

On the remote machine:

```sh
remote-open open document.pdf
remote-open open photo.jpg song.flac
remote-open edit file.txt
remote-open edit --wait COMMIT_EDITMSG
remote-open edit one.txt two.txt
remote-open diff old.txt new.txt
```

`open` accepts existing files and opens each one separately in its default
application.

`edit` accepts a missing file. It does not create it on the remote machine.
The editor creates it on save. Its parent directory must exist.

`edit --wait` blocks until the configured editor finishes. Other requests can
continue through the bridge while it waits. To use it for Git commit messages:

```sh
git config --global core.editor 'remote-open edit --wait'
```

`diff` requires two existing paths. They may be files or directories.

Paths are made absolute before they are sent. Spaces, Unicode, and newlines are
safe. The protocol sends an operation, a target alias, and paths. The
workstation accepts configured aliases only. App commands stay on the
workstation.

## Test

```sh
python3 -m unittest discover -s tests -v
```
