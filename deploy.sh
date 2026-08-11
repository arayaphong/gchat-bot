#!/usr/bin/env bash
set -euo pipefail

force_deploy=false

while getopts ":f" option; do
	case "$option" in
		f) force_deploy=true ;;
		*)
			echo "Usage: $0 [-f] <exact-40-character-commit-sha>" >&2
			exit 2
			;;
	esac
done
shift $((OPTIND - 1))

if [ "$#" -ne 1 ] || [[ ! "$1" =~ ^[0-9a-f]{40}$ ]]; then
	echo "An exact lowercase 40-character commit SHA is required." >&2
	exit 2
fi
release_sha="$1"

allowed_hostname="ThinkPad-T495"
current_hostname="$(hostname -s)"
if [ "$current_hostname" != "$allowed_hostname" ] && [ "$force_deploy" != true ]; then
	echo "Invalid deployment host: $current_hostname." >&2
	echo "Use $0 -f <sha> only after verifying the target topology." >&2
	exit 1
fi

application_root="$HOME/gchat-bot"
release_root="$application_root/releases"
release_dir="$release_root/$release_sha"
current_link="$application_root/current"
next_link="$application_root/.current.next"
environment_file="$HOME/.config/gchat-bot/env"
user_unit_dir="$HOME/.config/systemd/user"
service_name="gchat-bot.service"
archive_url="https://github.com/arayaphong/gchat-bot/archive/$release_sha.tar.gz"

for command_name in curl tar python3 systemctl; do
	command -v "$command_name" >/dev/null || {
		echo "Missing deployment command: $command_name" >&2
		exit 1
	}
done
if [ ! -f "$environment_file" ]; then
	echo "Missing production environment file: $environment_file" >&2
	exit 1
fi
if [ -e "$release_dir" ] || [ -L "$release_dir" ]; then
	echo "Release already exists; refusing to overlay it: $release_sha" >&2
	exit 1
fi
if [ -e "$current_link" ] && [ ! -L "$current_link" ]; then
	echo "Current release path must be a symlink; migrate the legacy deployment first." >&2
	exit 1
fi

mkdir -p "$release_root" "$user_unit_dir"
staging_dir="$(mktemp -d "$release_root/.stage.$release_sha.XXXXXX")"
archive_file="$staging_dir/source.tar.gz"
source_dir="$release_dir"
release_installed=false
cleanup() {
	if [[ "$staging_dir" == "$release_root"/.stage."$release_sha".* ]]; then
		rm -rf -- "$staging_dir"
	fi
	if [ "$release_installed" != true ] && [ -d "$release_dir" ]; then
		rm -rf -- "$release_dir"
	fi
}
trap cleanup EXIT

curl --fail --location --silent --show-error "$archive_url" --output "$archive_file"
mkdir "$source_dir"
tar --extract --gzip --file "$archive_file" --directory "$source_dir" --strip-components=1
printf '%s\n' "$release_sha" > "$source_dir/RELEASE_SHA"

python3 -m venv "$source_dir/.venv"
"$source_dir/.venv/bin/python" -m pip install --disable-pip-version-check \
	--requirement "$source_dir/requirements-dev.txt"
"$source_dir/.venv/bin/python" -m pip check
(
	cd "$source_dir"
	.venv/bin/python -m unittest discover -s tests -v
)
"$source_dir/.venv/bin/ruff" check "$source_dir"
"$source_dir/.venv/bin/python" "$source_dir/scripts/release_gate.py" \
	--source-dir "$source_dir" --expected-sha "$release_sha"

set -a
# shellcheck disable=SC1090
. "$environment_file"
set +a
# Exercise a clean schema without migrating the live ledger while the previous
# release still owns it. The post-restart readiness/lease gates verify live state.
JINX_CHAT_HISTORY_STATE_DIR="$staging_dir/preflight-state" \
	"$source_dir/.venv/bin/python" "$source_dir/scripts/chat_history_admin.py" \
	preflight --remote

cp "$release_dir/deploy/gchat-bot.service" "$user_unit_dir/$service_name"
systemctl --user daemon-reload

previous_release=""
if [ -L "$current_link" ]; then
	previous_release="$(readlink -f "$current_link")"
fi
ln -s "$release_dir" "$next_link"
mv -Tf "$next_link" "$current_link"

rollback() {
	if [ -n "$previous_release" ] && [ -d "$previous_release" ]; then
		ln -s "$previous_release" "$next_link"
		mv -Tf "$next_link" "$current_link"
		systemctl --user restart "$service_name"
	else
		systemctl --user stop "$service_name" || true
	fi
}

if ! systemctl --user restart "$service_name"; then
	rollback
	echo "Restart failed; previous release restored." >&2
	exit 1
fi

healthy=false
for _attempt in 1 2 3 4 5 6 7 8 9 10; do
	if curl --fail --silent "http://127.0.0.1:8080/" >/dev/null; then
		readiness="$(curl --fail --silent "http://127.0.0.1:8080/readyz" || true)"
		if RELEASE_READINESS="$readiness" EXPECTED_RELEASE_SHA="$release_sha" \
			"$release_dir/.venv/bin/python" -c \
			'import json, os; p=json.loads(os.environ["RELEASE_READINESS"]); raise SystemExit(0 if p.get("status") == "ready" and p.get("release_sha") == os.environ["EXPECTED_RELEASE_SHA"] else 1)'
		then
			healthy=true
			break
		fi
	fi
	sleep 2
done

if [ "$healthy" != true ]; then
	rollback
	echo "Post-restart verification failed; previous release restored." >&2
	exit 1
fi

if ! "$release_dir/.venv/bin/python" \
	"$release_dir/scripts/chat_history_admin.py" preflight --require-worker
then
	rollback
	echo "Singleton worker verification failed; previous release restored." >&2
	exit 1
fi
release_installed=true
echo "Deployed exact release $release_sha"
