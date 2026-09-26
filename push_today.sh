#!/bin/bash
set -e
cd ~/Desktop/Auto-Video-Factory

git branch -D temp_daily 2>/dev/null || true

git checkout --orphan temp_daily
git rm -rf --cached . >/dev/null 2>&1 || true

git add -A
git commit -m "Batch update: $(date +'%Y-%m-%d %H:%M:%S')"

git branch -M main
git push --force origin main

git reflog expire --expire=now --all
git gc --prune=now

echo "--------------------------------------------------"
echo "Done. Today's folder and .git size:"
du -sh .git .
echo "--------------------------------------------------"
