# nginx: redirect the bare IP to hub.modoku.tech

`hub-ip-redirect.conf` makes every request that isn't for `hub.modoku.tech`
(the VPS IP `152.42.226.86`, or any other name pointed at the server) redirect
to `https://hub.modoku.tech`, keeping the path. The app itself already builds
every emailed link from `PUBLIC_BASE_URL` (Fix118); this stops people and bots
using the IP at all.

Run on the VPS, one step at a time. `/path/to/modoku-crm` is wherever the app
is checked out (where you run `git pull`).

1. Back up the current nginx config:

       sudo cp -a /etc/nginx /root/nginx-backup-$(date +%F)

2. Check the certificate folder name (expect `hub.modoku.tech`):

       sudo ls /etc/letsencrypt/live/

   If it's different, change the two `ssl_certificate` lines in the conf file.

3. See which site currently claims `default_server` (usually the stock `default` site):

       sudo grep -rn "default_server" /etc/nginx/sites-enabled/ /etc/nginx/conf.d/

4. Install the file and enable it:

       cd /path/to/modoku-crm && git pull
       sudo cp deploy/nginx/hub-ip-redirect.conf /etc/nginx/sites-available/
       sudo ln -s /etc/nginx/sites-available/hub-ip-redirect.conf /etc/nginx/sites-enabled/

5. Remove the other `default_server` claims found in step 3 (only one site may
   have it). For the stock site: `sudo rm /etc/nginx/sites-enabled/default`.
   For hub.modoku.tech's own file, delete just the words `default_server`.

6. Test, then reload (reload only if the test says "successful"):

       sudo nginx -t
       sudo systemctl reload nginx

   If `nginx -t` fails with `socket() [::]:80 failed (97: Address family not supported)`,
   the server has IPv6 off: delete the two `listen [::]...` lines from
   `/etc/nginx/sites-available/hub-ip-redirect.conf` and test again.

7. Check:

       curl -sI  http://152.42.226.86/purchase-orders/3 | grep -i -E "^HTTP|^location"
       curl -skI https://152.42.226.86/purchase-orders/3 | grep -i -E "^HTTP|^location"
       curl -sI  https://hub.modoku.tech/login | grep -i "^HTTP"

   The first two should show `301` and `location: https://hub.modoku.tech/purchase-orders/3`;
   the last `200`.

8. Make sure the app isn't reachable around nginx:

       sudo ss -ltnp | grep -E "gunicorn|:8000"

   If it shows `0.0.0.0:8000` (or `*:8000`), change gunicorn's bind to
   `127.0.0.1:8000` in its systemd service (`-b 127.0.0.1:8000`), then
   `sudo systemctl daemon-reload` and restart the app service. nginx's
   `proxy_pass` to `127.0.0.1:8000` keeps working.

Undo: `sudo rm /etc/nginx/sites-enabled/hub-ip-redirect.conf`, put back what
step 5 removed (from the backup in step 1), `sudo nginx -t && sudo systemctl reload nginx`.
