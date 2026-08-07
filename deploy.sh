#!/bin/bash
set -e

force_deploy=false

while getopts ":f" option; do
	case "$option" in
		f) force_deploy=true ;;
		*)
			echo "Usage: $0 [-f]"
			exit 2
			;;
	esac
done

allowed_hostname="ThinkPad-T495"
current_hostname="$(hostname -s)"

if [ "$current_hostname" != "$allowed_hostname" ] && [ "$force_deploy" != true ]; then
	echo "Invalid deployment host: $current_hostname."
	echo "Use $0 -f to force deployment."
	exit 1
fi

# 1. Cleanup old download
rm -rf ~/development.zip ~/gchat-bot-development/

# 2. Download development branch
wget -O ~/development.zip https://github.com/arayaphong/gchat-bot/archive/refs/heads/development.zip

# 3. Extract
unzip -o ~/development.zip -d ~/

# 4. Sync to gchat-bot folder
rsync -av ~/gchat-bot-development/ ~/gchat-bot/

# 5. Install Python dependencies
pip install -r ~/gchat-bot/requirements.txt

# 6. Build AGENTS.md with Google Chat rules
mkdir -p ~/.openclaw/workspace
workspace_agents_file="$HOME/.openclaw/workspace/AGENTS.md"
backup_agents_file="$workspace_agents_file.bak"

if [ ! -f "$workspace_agents_file" ]; then
	echo "Missing workspace AGENTS.md: $workspace_agents_file"
	exit 1
fi

cp -f "$workspace_agents_file" "$backup_agents_file"
temporary_agents_file="$(mktemp)"
awk -v extra_file="$HOME/gchat-bot/harness/AGENTS-EXTRA.md" '
	/^## Make It Yours$/ {
		while ((getline line < extra_file) > 0) print line
		close(extra_file)
		print ""
		inserted = 1
	}
	{ print }
	END { if (!inserted) exit 1 }
' "$workspace_agents_file" > "$temporary_agents_file"
mv -f "$temporary_agents_file" "$workspace_agents_file"

# 7. Cleanup
rm -rf ~/development.zip ~/gchat-bot-development/

echo "Done"
