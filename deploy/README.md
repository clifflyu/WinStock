# Server deployment

This package installs a local `systemd` timer. It never connects to a broker and never creates real orders.

The timer runs at 16:40 Asia/Shanghai from Monday through Friday. A holiday run is harmless: the updater re-fetches the last stored trading day, records any source correction, and the next trading day is picked up normally. `Persistent=true` asks systemd to run a missed calendar job after the server returns online.

## One-time deployment

1. Copy the project to the server and install it for the Python interpreter that the service will use:

   ```bash
   cd /opt/astock/WinStock
   python3 -m pip install .
   python3 -m winstocker status
   ```

   The service user must be able to read the project and write the database directory. For a dedicated `winstock` user:

   ```bash
   sudo useradd --system --create-home --shell /usr/sbin/nologin winstock
   sudo chown -R winstock:winstock /opt/astock/WinStock
   ```

2. Install the timer. Set these variables if the server path, service user, Python, or database path differs:

   ```bash
   cd /opt/astock/WinStock
   sudo env WINSTOCK_USER=winstock WINSTOCK_DIR=/opt/astock/WinStock \
     WINSTOCK_PYTHON=/usr/bin/python3 \
     WINSTOCK_DB=/opt/astock/WinStock/data/winstock.db \
     ./deploy/install-systemd.sh
   ```

3. Run one safe manual test and inspect its logs:

   ```bash
   sudo systemctl start winstock-update.service
   journalctl -u winstock-update.service -n 100 --no-pager
   systemctl list-timers winstock-update.timer --all
   ```

Each successful update runs `update`, which automatically saves the dated candidate snapshot when no download fails, then runs `audit`. The journal is the operational record; no secret or broker credential is needed.

To disable the job:

```bash
sudo systemctl disable --now winstock-update.timer
```
