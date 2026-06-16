#!/bin/bash

# Soulseek Similar Rinser - Batch Runner
# Usage: ./rinse_all.sh genre_list.txt

SCRIPT_DIR="/root/soulseek-similar-rinser"
CONFIG_FILE="$SCRIPT_DIR/pushover_config.py"
PYTHON_SCRIPT="$SCRIPT_DIR/beatport-local.py"

if [ "$#" -ne 1 ]; then
    echo "Usage: $0 <genre_list_file>"
    exit 1
fi

GENRE_FILE=$1

if [ ! -f "$GENRE_FILE" ]; then
    echo "Error: File $GENRE_FILE not found."
    exit 1
fi

# Extract Pushover credentials from the Python config file
TOKEN=$(grep "PUSHOVER_API_TOKEN" "$CONFIG_FILE" | awk -F'"' '{print $2}')
USER_KEY=$(grep "PUSHOVER_USER_KEY" "$CONFIG_FILE" | awk -F'"' '{print $2}')

send_notification() {
    local title="$1"
    local message="$2"
    if [[ -n "$TOKEN" && -n "$USER_KEY" ]]; then
        curl -s \
            --form-string "token=$TOKEN" \
            --form-string "user=$USER_KEY" \
            --form-string "title=$title" \
            --form-string "message=$message" \
            https://api.pushover.net/1/messages.json > /dev/null
    fi
}

START_TIME=$(date +"%T")
COUNT=$(wc -l < "$GENRE_FILE" | xargs)

echo "🚀 Starting batch download for $COUNT genres..."
send_notification "Soulseek Batch Started" "Starting process for $COUNT genres at $START_TIME."

while IFS= read -r genre || [[ -n "$genre" ]]; do
    # Skip empty lines or comments
    [[ -z "$genre" || "$genre" =~ ^# ]] && continue
    
    echo "--------------------------------------------------"
    echo "🎵 Processing Genre: $genre"
    echo "--------------------------------------------------"
    
    python3 "$PYTHON_SCRIPT" "$genre" --download
done < "$GENRE_FILE"

send_notification "Soulseek Batch Complete" "Finished processing all genres from $GENRE_FILE."
echo "✅ Batch processing complete."