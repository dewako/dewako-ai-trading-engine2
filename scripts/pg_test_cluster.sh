#!/usr/bin/env bash
# Throw-away local PostgreSQL 16 cluster for the test suite (no Docker needed). Usage: scripts/pg_test_cluster.sh start|stop|restart|url
# Requires the PostgreSQL server binaries (Debian/Ubuntu: apt install postgresql-16). Must be run as root or as the postgres user.
set -euo pipefail
PGBIN=${PGBIN:-/usr/lib/postgresql/16/bin}
DATA=${DEWAKO_PG_DATA:-/var/lib/postgresql/dewako_test}
PORT=${DEWAKO_PG_PORT:-55432}
PASS=${DEWAKO_PG_PASSWORD:-dewako_test_pw}
run() { if [ "$(id -u)" = 0 ]; then su postgres -s /bin/bash -c "$*"; else bash -c "$*"; fi; }
case "${1:-url}" in
  start)
    if [ ! -f "$DATA/PG_VERSION" ]; then
      mkdir -p "$DATA"; chown postgres "$DATA" 2>/dev/null || true
      printf '%s' "$PASS" > /tmp/dewako_pgpw; chmod 644 /tmp/dewako_pgpw
      run "$PGBIN/initdb -D $DATA -U dewako --auth-host=scram-sha-256 --auth-local=trust --pwfile=/tmp/dewako_pgpw -E UTF8" >/dev/null
      rm -f /tmp/dewako_pgpw
    fi
    run "$PGBIN/pg_ctl -D $DATA -o '-p $PORT -c listen_addresses=127.0.0.1 -c unix_socket_directories=/tmp -c fsync=on -c max_connections=100' -l $DATA/server.log -w start" >/dev/null || true
    # create the database if missing
    PGPASSWORD=$PASS psql -h 127.0.0.1 -p $PORT -U dewako -d postgres -tc "select 1 from pg_database where datname='dewako'" | grep -q 1 \
      || PGPASSWORD=$PASS psql -h 127.0.0.1 -p $PORT -U dewako -d postgres -c "create database dewako" >/dev/null
    echo "postgresql://dewako:$PASS@127.0.0.1:$PORT/dewako" ;;
  restart) run "$PGBIN/pg_ctl -D $DATA -m immediate -o '-p $PORT -c listen_addresses=127.0.0.1 -c unix_socket_directories=/tmp -c fsync=on -c max_connections=100' -l $DATA/server.log -w restart" >/dev/null; echo restarted ;;
  stop) run "$PGBIN/pg_ctl -D $DATA -m fast stop" ;;
  url)  echo "postgresql://dewako:$PASS@127.0.0.1:$PORT/dewako" ;;
esac
