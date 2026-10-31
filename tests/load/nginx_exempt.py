"""Temporarily exempt the load generator (and the local sampler) from the
commroom limit_req zones. Run on the server as root:
    python3 nginx_exempt.py /etc/nginx/sites-enabled/commroom
Restore by copying back the backup taken before (the script refuses to run
twice). nginx cannot change the key of an existing zone on reload, so the
commroom zones get new names (*_lt); `general` stays defined under its own
name too, because snippets/tg-console.conf and snippets/victus-q.conf use it.
"""
import re
import sys

EXEMPT = ["132.243.238.233", "87.242.85.49", "127.0.0.1"]  # sg, mannov.ru VM, local sampler
path = sys.argv[1]
s = open(path, encoding="utf-8").read()
if "$lt_key" in s:
    sys.exit("already patched")
head = ("# LOADTEST: temporary, restore the backup when done\n"
        "geo $lt_exempt { default 0; " + " ".join(f"{ip} 1;" for ip in EXEMPT) + " }\n"
        "map $lt_exempt $lt_key { 0 $binary_remote_addr; 1 \"\"; }\n"
        "limit_req_zone $binary_remote_addr zone=general:10m rate=30r/s;  # snippets\n")
s, n_zones = re.subn(r"^limit_req_zone \$binary_remote_addr zone=([a-z_0-9]+):",
                     r"limit_req_zone $lt_key zone=\1_lt:", s, flags=re.M)
s, n_uses = re.subn(r"(limit_req zone=)([a-z_0-9]+) ", r"\1\2_lt ", s)
# The per-IP connection limit too (the all-clients safety net stays as is):
# a far-away generator holds many connections open just from its round trip.
s, n_conn = re.subn(r"^limit_conn_zone \$binary_remote_addr zone=commroom_ip:",
                    r"limit_conn_zone $lt_key zone=commroom_ip_lt:", s, flags=re.M)
s = s.replace("limit_conn commroom_ip ", "limit_conn commroom_ip_lt ")
open(path, "w", encoding="utf-8").write(head + s)
print(f"zones renamed: {n_zones}, uses renamed: {n_uses}, conn zones: {n_conn}")
