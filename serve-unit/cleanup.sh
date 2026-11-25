#!/bin/bash
set -eu

DIR=/srv/audio
KEEP_DAYS=30     # この日数アクセスされていない曲を消す
MAX_MB=10240     # 合計サイズの上限(MB)

# 1. 取得に失敗した一時ファイル(2時間以上前のもの)
find "$DIR" -maxdepth 1 -name '.tmp-*' -mmin +120 -print -delete

# 2. 一定期間アクセスされていない MP3
find "$DIR" -maxdepth 1 -name '*.mp3' -atime +"$KEEP_DAYS" -print -delete

# 3. 合計サイズが上限を超えていたら、古い順に消す
total=$(du -sm "$DIR" | cut -f1)
if [ "$total" -gt "$MAX_MB" ]; then
  find "$DIR" -maxdepth 1 -name '*.mp3' -printf '%A@ %p\n' | sort -n | while read -r _ f; do
    echo "$f"
    rm -f -- "$f"
    total=$(du -sm "$DIR" | cut -f1)
    if [ "$total" -le "$MAX_MB" ]; then
      break
    fi
  done
fi

echo "cleanup done: $(du -sm "$DIR" | cut -f1) MB used"